"""Self-update from GitHub Releases.

A release is a git tag like v1.2.0 whose GitHub Release carries ClipMonster-windows.zip
and SHA256SUMS.txt (both made by .github/workflows/release.yml). Updating:

1. check()    - ask the GitHub API for the newest release and compare versions.
2. download() - stream the zip to %LOCALAPPDATA%\\ClipMonster\\updates and verify SHA-256.
3. extract()  - unpack next to the installed app (same drive, so the swap is a rename).
4. launch_swap() - the new version's own ytclip.exe (swap.py) waits for the app to exit, moves
   the app's own files into a backup folder inside the install folder (file by file if a folder
   is held open), copies the new files in, relaunches, and puts everything back if anything
   fails. (A running app can't replace its own files on Windows.)
5. tidy()       - the new version removes the unpacked update and old downloads.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__

try:  # written by build.py: the GitHub repo this build was published from
    from ._build_info import UPDATE_REPO
except ImportError:  # running from source
    UPDATE_REPO = ""

ASSET_NAME = "ClipMonster-windows.zip"
SUMS_NAME = "SHA256SUMS.txt"
APP_FOLDER = "ClipMonster"
EXE_NAME = "ClipMonster.exe"
# Test hook: point the updater at a local fake of the GitHub API.
API_BASE = os.environ.get("CLIPMONSTER_UPDATE_API", "https://api.github.com").rstrip("/")


class UpdateError(RuntimeError):
    """A plain-English reason an update check or install failed."""


class UpdateCancelled(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    name: str
    notes: str
    page_url: str
    asset_url: str
    asset_size: int
    sha256: str  # may be "" until download() reads SHA256SUMS.txt
    sums_url: str
    prerelease: bool


# ---------------------------------------------------------------- versions

_VERSION = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?(a|alpha|b|beta|rc|pre)\.?(\d*))?$", re.I)
_STAGE = {"a": 0, "alpha": 0, "pre": 0, "b": 1, "beta": 1, "rc": 2}


def parse_version(text: str) -> tuple[int, ...] | None:
    """'v1.2.3' -> (1, 2, 3, 3, 0); pre-releases sort before the final release."""
    m = _VERSION.match(text.strip())
    if not m:
        return None
    major, minor, patch, stage, num = m.groups()
    stage_rank = _STAGE[stage.lower()] if stage else 3
    return int(major), int(minor), int(patch or 0), stage_rank, int(num or 0)


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


# ---------------------------------------------------------------- GitHub API

def configured_repo(override: str = "") -> str:
    repo = (override or UPDATE_REPO or "").strip().strip("/")
    repo = re.sub(r"^(https?://)?(www\.)?github\.com/", "", repo, flags=re.I).removesuffix(".git")
    return repo if re.fullmatch(r"[\w.-]+/[\w.-]+", repo) else ""


def _safe_url(url: str) -> str:
    """Only fetch over HTTPS (plain HTTP is allowed for localhost tests)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")):
        return url
    raise UpdateError(f"Refusing to download from an insecure address: {url}")


def _request(url: str, accept: str = "application/vnd.github+json") -> urllib.request.Request:
    return urllib.request.Request(_safe_url(url), headers={
        "Accept": accept, "User-Agent": f"ClipMonster/{__version__}", "X-GitHub-Api-Version": "2022-11-28"})


def _get_json(url: str, repo: str):
    try:
        with urllib.request.urlopen(_request(url), timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError(f"No published release found at github.com/{repo}. The repository must be public "
                              "and have at least one release.") from exc
        if exc.code in (403, 429):
            raise UpdateError("GitHub's rate limit for update checks was reached. Try again in an hour.") from exc
        raise UpdateError(f"GitHub returned an error ({exc.code}).") from exc
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise UpdateError("Couldn't reach GitHub to check for updates. Check your internet connection.") from exc
    except ValueError as exc:
        raise UpdateError("GitHub sent a response ClipMonster couldn't read. Try again later.") from exc


def _to_release(data: dict) -> Release | None:
    version = parse_version(data.get("tag_name", ""))
    assets = {a.get("name"): a for a in data.get("assets") or []}
    asset = assets.get(ASSET_NAME)
    if version is None or not asset or data.get("draft"):
        return None
    digest = asset.get("digest") or ""  # GitHub reports "sha256:<hex>" for uploaded assets
    sums = assets.get(SUMS_NAME) or {}
    return Release(
        version=data["tag_name"].lstrip("vV"), tag=data["tag_name"], name=data.get("name") or data["tag_name"],
        notes=data.get("body") or "", page_url=data.get("html_url") or "",
        asset_url=asset["browser_download_url"], asset_size=int(asset.get("size") or 0),
        sha256=digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else "",
        sums_url=sums.get("browser_download_url", ""), prerelease=bool(data.get("prerelease")))


def latest_release(repo: str, include_prereleases: bool = False) -> Release | None:
    if not repo:
        raise UpdateError("Updates aren't set up: no GitHub repository is configured. "
                          "Enter it as owner/repo in Settings → Updates.")
    if include_prereleases:
        found = [_to_release(r) for r in _get_json(f"{API_BASE}/repos/{repo}/releases?per_page=20", repo)]
    else:
        found = [_to_release(_get_json(f"{API_BASE}/repos/{repo}/releases/latest", repo))]
    found = [r for r in found if r]
    return max(found, key=lambda r: parse_version(r.version)) if found else None


def check(repo: str, include_prereleases: bool = False, current: str = __version__) -> Release | None:
    """The newer release, or None when this version is already the latest."""
    rel = latest_release(repo, include_prereleases)
    return rel if rel and is_newer(rel.version, current) else None


# ---------------------------------------------------------------- download & unpack

def updates_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    path = Path(base) / "ClipMonster" / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _expected_sha(rel: Release) -> str:
    if rel.sha256:
        return rel.sha256
    if rel.sums_url:
        try:
            with urllib.request.urlopen(_request(rel.sums_url, "application/octet-stream"), timeout=20) as resp:
                for line in resp.read().decode("utf-8", "replace").splitlines():
                    parts = line.strip().split()
                    if len(parts) == 2 and parts[1].lstrip("*") == ASSET_NAME:
                        return parts[0].lower()
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            raise UpdateError("Couldn't download the update's checksum file.") from exc
    raise UpdateError("This release has no checksum, so it can't be verified. Not installing it.")


def _need_space(folder: Path, needed: int, what: str) -> None:
    free = shutil.disk_usage(folder).free
    if needed and free < needed * 1.1:
        raise UpdateError(f"Not enough disk space to {what}: about {needed / 1e6:.0f} MB is needed on "
                          f"{folder.anchor} and {free / 1e6:.0f} MB is free.")


def download(rel: Release, progress: Callable[[float], None] | None = None,
             cancelled: Callable[[], bool] | None = None) -> Path:
    """Download the release zip and verify its SHA-256. Returns the zip path."""
    expected = _expected_sha(rel)
    folder = updates_dir()
    _need_space(folder, rel.asset_size, "download the update")
    dest = folder / f"{ASSET_NAME[:-4]}-{rel.version}.zip"
    part = dest.with_suffix(".part")
    sha = hashlib.sha256()
    try:
        try:
            out = open(part, "wb")
        except OSError as exc:
            raise UpdateError(f"Couldn't save the download in {folder} ({exc.strerror or exc}).") from exc
        with out, urllib.request.urlopen(_request(rel.asset_url, "application/octet-stream"), timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or rel.asset_size or 0)
            done = 0
            while chunk := resp.read(1 << 18):
                if cancelled and cancelled():
                    raise UpdateCancelled()
                try:
                    out.write(chunk)
                except OSError as exc:  # disk full or the file got locked - not a network problem
                    raise UpdateError(f"Couldn't save the download in {folder} ({exc.strerror or exc}).") from exc
                sha.update(chunk)
                done += len(chunk)
                if progress and total:
                    progress(min(1.0, done / total))
        if cancelled and cancelled():
            raise UpdateCancelled()
        if total and done != total:
            raise UpdateError("The download was interrupted before it finished. Try again.")
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError) as exc:
        part.unlink(missing_ok=True)
        raise UpdateError("The download failed. Check your internet connection and try again.") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if sha.hexdigest() != expected:
        part.unlink(missing_ok=True)
        raise UpdateError("The downloaded update didn't match its checksum, so it was discarded.")
    part.replace(dest)
    return dest


def install_dir() -> Path | None:
    """Folder of the running ClipMonster.exe, or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def _writable(folder: Path) -> bool:
    probe = folder / f".clipmonster-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


MAX_PATH = 259  # Windows' classic limit (260 including the terminator) unless long paths are enabled
BACKUP_NAME = ".cm-old-20991231235959"  # the longest name the swap script gives its backup folder


def _path_need(path: Path, folder: bool = False) -> int:
    """Characters the file (or folder) `path` needs against MAX_PATH: Windows keeps 12 free in
    every folder path it creates."""
    return len(str(path)) + 12 if folder else max(len(str(path)), len(str(path.parent)) + 12)


def _old_app_files(target: Path, names: set[str]) -> list[Path]:
    """The installed app's own files (relative to `target`), which move into the backup folder."""
    found = []
    for name in names:
        top = target / name
        if top.is_file():
            found.append(Path(name))
        elif top.is_dir():
            for folder, _, files in os.walk(top, onerror=lambda e: None):
                found += [Path(folder, f).relative_to(target) for f in files]
    return found


def extract(zip_path: Path, target: Path, cancelled: Callable[[], bool] | None = None) -> Path:
    """Unpack next to the installed folder `target`; returns the new app folder."""
    # A short name: unpacked paths are this much deeper than the final ones, and the app
    # folder has some very deep files, so every character counts against MAX_PATH.
    staging = target.parent / ".cm-up"
    if not _writable(target.parent) or not _writable(target):
        raise UpdateError(f"ClipMonster can't write to {target.parent}. Move the ClipMonster folder somewhere "
                          "you own (e.g. Documents) or download the new version manually.")
    root = staging.resolve()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.infolist()
            for member in members:
                dest = (staging / member.filename).resolve()
                if root not in dest.parents and dest != root:
                    raise UpdateError("The update archive contains unsafe paths. Not installing it.")
            # Check up front, so a too-deep install fails cleanly instead of halfway through.
            # Both the old and the new files can end up in the backup folder inside the app
            # folder (old ones on install, new ones' folders on a file-by-file move), which is
            # the deepest place either goes.
            new = [(Path(m.filename).relative_to(APP_FOLDER), m.is_dir()) for m in members
                   if m.filename.startswith(APP_FOLDER + "/") and m.filename != APP_FOLDER + "/"]
            tops = {"ClipMonster.exe", "ytclip.exe", "_internal"} | {rel.parts[0] for rel, _ in new}
            old = [(rel, False) for rel in _old_app_files(target, tops)]
            longest = max((_path_need(target / BACKUP_NAME / rel, folder) for rel, folder in new + old), default=0)
            longest = max(longest, max((_path_need(root / m.filename, m.is_dir()) for m in members), default=0))
            if longest > MAX_PATH:
                raise UpdateError(f"The ClipMonster folder is too deep for Windows' path length limit "
                                  f"({longest} characters needed, {MAX_PATH} allowed). Move it to a shorter "
                                  f"location such as {Path.home() / 'ClipMonster'} and try again.")
            # unpacked, then copied in (the helper runs from the unpacked copy, so it stays until tidy())
            _need_space(target.parent, 2 * sum(m.file_size for m in members), "unpack the update")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir()
            for i, member in enumerate(members):
                if cancelled and i % 50 == 0 and cancelled():
                    raise UpdateCancelled()
                zf.extract(member, staging)
    except UpdateError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except UpdateCancelled:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except zipfile.BadZipFile as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("The update archive is damaged.") from exc
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"Couldn't unpack the update next to the ClipMonster folder "
                          f"({exc.strerror or exc}).") from exc
    new_app = staging / APP_FOLDER
    if not (new_app / EXE_NAME).exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("The update archive doesn't contain ClipMonster.exe.")
    return new_app


def other_instances(target: Path) -> list[str]:
    """ClipMonster.exe / ytclip.exe processes (besides this one) running from `target`;
    they would keep the folder from being replaced."""
    from .procs import app_processes

    found = app_processes(target)
    if not found:
        return []
    runner = ""
    try:
        from .jobs import JobDB

        db = JobDB()
        try:
            runner = db.meta("runner_pid")
        finally:
            db.close()
    except Exception:
        pass
    return [f"{'ClipMonster automation' if str(pid) == runner else path.name} (process {pid})" for pid, path in found]


def stop_automation(timeout: float = 90.0) -> bool:
    """A background automation run keeps the app folder in use: ask it to stop (its current video
    goes back in the queue) and have the updated app start it again. False if it didn't stop."""
    from .jobs import JobDB
    from .pipeline import runner_alive

    db = JobDB()
    try:
        if not runner_alive(db):
            return True
        db.set_meta("stop", "1")
        db.set_meta("resume_after_update", "1")
        end = time.time() + timeout
        while runner_alive(db) and time.time() < end:
            time.sleep(1)
        if runner_alive(db):
            return False
        time.sleep(3)  # let the process exit after it signs off
        return True
    finally:
        db.close()


AUTOMATION_BUSY = ("Automation is still running in the background and didn't stop within 90 seconds. "
                   "Stop it on the Automation tab (or with `ytclip auto --stop`) and try again.")


# ---------------------------------------------------------------- swap

def update_log() -> Path:
    return updates_dir() / "update.log"


def _log_line(text: str) -> None:
    try:
        with update_log().open("a", encoding="utf-8", newline="") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\r\n")
    except OSError:
        pass


def launch_swap(new_app: Path, target: Path, wait_pid: int, relaunch: str = EXE_NAME) -> None:
    """Start the detached helper that installs `new_app` over `target` once `wait_pid` exits: the new
    version's own ytclip.exe (swap.py), from the unpacked update."""
    from . import swap

    cmd = swap.command(new_app, target, wait_pid, update_log(), relaunch)
    # Marks this attempt, so an old failure isn't reported if the helper never gets to log.
    _log_line(f"Update requested: helper output goes to {updates_dir() / 'apply-update.out'}")
    # No window, in its own process group, so neither closing the app nor Ctrl+C in a terminal stops it.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    # Anything printed before the helper's own logging starts lands here instead of vanishing.
    with open(updates_dir() / "apply-update.out", "ab") as out:
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=out, close_fds=True, cwd=str(updates_dir()))
        try:  # leave any job object so the helper outlives the app
            subprocess.Popen(cmd, creationflags=flags | breakaway, **kwargs)
        except OSError:
            subprocess.Popen(cmd, creationflags=flags, **kwargs)


def tidy(target: Path | None = None) -> int:
    """After an update: remove the unpacked update next to the app folder and the downloaded zips
    (about 225 MB each, kept forever before 2.4.1). Returns how many were removed. Never fails."""
    removed = 0
    target = target or install_dir()
    if target is not None:
        staging = target.parent / ".cm-up"
        if staging.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
            removed += not staging.exists()
    try:
        zips = list(updates_dir().glob("ClipMonster-windows-*.zip"))
    except OSError:
        zips = []
    for z in zips:
        try:
            z.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------- remembered state

def _state_path() -> Path:
    from .settings import app_dir

    return app_dir() / "update_state.json"


def load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(**changes) -> None:
    state = load_state() | changes
    try:
        _state_path().write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError:
        pass


def due_for_check(hours: float = 20) -> bool:
    return time.time() - float(load_state().get("last_check", 0)) > hours * 3600


INSTALL_WINDOW = 300  # seconds: a helper that hasn't finished by then has died or given up


def finished_update_message() -> tuple[str, bool] | None:
    """After a restart: did the update we started succeed? Returns (message, ok) once."""
    pending = load_state().get("pending")
    if not pending:
        return None
    if parse_version(pending) == parse_version(__version__):
        save_state(pending="")
        tidy()
        return f"Updated to ClipMonster {__version__}.", True
    state, reason = last_attempt()
    try:
        recent = time.time() - update_log().stat().st_mtime < INSTALL_WINDOW
    except OSError:
        recent = False
    if state in ("requested", "running") and recent:  # opened again while it's still installing
        return (f"ClipMonster {pending} is still being installed. Close ClipMonster for a minute so it can "
                f"finish, then open it again.", False)
    save_state(pending="")
    if state == "requested":
        reason = f"The installer didn't start. Its output is in {updates_dir() / 'apply-update.out'}"
    return (f"The update to {pending} didn't complete, so you're still on {__version__}."
            + (f" {reason.rstrip('.')}." if reason else "") + f" Details: {update_log()}", False)


def last_attempt() -> tuple[str, str]:
    """The newest update attempt in the helper's log, as (state, reason). state is "requested"
    (the helper hasn't started), "running", "failed", "finished" or "" (no attempt logged)."""
    try:
        lines = update_log().read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return "", ""
    for line in reversed(lines[-300:]):
        if "Update failed: " in line:
            return "failed", line.split("Update failed: ", 1)[1].strip()
        if "Update finished" in line:
            return "finished", ""
        if "Update started" in line:
            return "running", ""
        if "Update requested" in line:
            return "requested", ""
    return "", ""


def last_failure() -> str:
    """Why the last update failed, from the helper's log ("" if it didn't fail or didn't say)."""
    state, reason = last_attempt()
    return reason if state == "failed" else ""

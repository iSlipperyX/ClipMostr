"""Self-update from GitHub Releases, on the channel picked in Settings → Updates.

Channels (settings.UPDATE_CHANNELS):
* stable - releases made with `python release.py X.Y.Z` (tags vX.Y.Z)
* beta   - those plus test versions (tags vX.Y.Z-beta.N, published as GitHub pre-releases)
* dev    - those plus the newest build of main: the release workflow rebuilds it on every push to
           main and publishes it as the pre-release tagged "dev", versioned "<last release>+dev.<N>"
           (N = commits since that release), which sorts after the release it builds on.

A release carries ClipMonster-windows.zip (what updates install), ClipMonster-Setup.exe (what people
download) and SHA256SUMS.txt, all made by .github/workflows/release.yml. Updating:

1. check()    - ask the GitHub API for the newest release on the channel and compare versions.
2. download() - stream the zip to %LOCALAPPDATA%\\ClipMonster\\updates and verify its SHA-256.
3. extract()  - unpack it there too (updates\\next), out of the install folder and out of OneDrive.
4. launch_swap() - the new version's own ytclip.exe (swap.py) waits for the app to exit, moves
   the app's own files into a backup folder inside the install folder (file by file if a folder
   is held open), copies the new files in, relaunches, and puts everything back if anything
   fails. (A running app can't replace its own files on Windows.) The backup is then kept as the
   previous version (updates\\previous), so Settings → Updates can go back to it.
5. tidy()     - the new version removes the download and the unpacked copy.
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

try:  # written by build.py: the GitHub repo this build was published from, and its commit
    from ._build_info import UPDATE_REPO
except ImportError:  # running from source
    UPDATE_REPO = ""
try:
    from ._build_info import BUILD_COMMIT
except ImportError:  # running from source, or built before 2.8.0
    BUILD_COMMIT = ""

ASSET_NAME = "ClipMonster-windows.zip"
SETUP_NAME = "ClipMonster-Setup.exe"
SUMS_NAME = "SHA256SUMS.txt"
DEV_TAG = "dev"  # the rolling pre-release of main's newest build
CHANNEL_RANK = {"stable": 0, "beta": 1, "dev": 2}
CHECK_HOURS = {"stable": 20, "beta": 6, "dev": 2}  # how often each channel looks for something new
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
    commit: str = ""  # a dev build's commit
    switch: bool = False  # not newer: the channel picked in Settings is older than this version's (see check)

    @property
    def channel(self) -> str:
        return channel_of(self.version)

    @property
    def label(self) -> str:
        return build_label(self.version, self.commit)


# ---------------------------------------------------------------- versions

_VERSION = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?(a|alpha|b|beta|rc|pre)\.?(\d*))?"
                      r"(?:\+dev\.?(\d+))?$", re.I)
_STAGE = {"a": 0, "alpha": 0, "pre": 0, "b": 1, "beta": 1, "rc": 2}


def parse_version(text: str) -> tuple[int, ...] | None:
    """'v1.2.3' -> (1, 2, 3, 3, 0, 0); pre-releases sort before the final release, and a dev build
    ('1.2.3+dev.4': 4 changes after 1.2.3) after the release it builds on."""
    m = _VERSION.match(text.strip())
    if not m:
        return None
    major, minor, patch, stage, num, dev = m.groups()
    stage_rank = _STAGE[stage.lower()] if stage else 3
    return int(major), int(minor), int(patch or 0), stage_rank, int(num or 0), int(dev or 0)


def is_newer(candidate: str, current: str | None = None) -> bool:
    a, b = parse_version(candidate), parse_version(__version__ if current is None else current)
    return a is not None and b is not None and a > b


def channel_of(version: str) -> str:
    """The channel a version is published on: 'dev', 'beta' (any pre-release) or 'stable'."""
    v = parse_version(version)
    if v is None:
        return "stable"
    return "dev" if v[5] else "beta" if v[3] < 3 else "stable"


def build_label(version: str | None = None, commit: str | None = None) -> str:
    """This build as people should see it: '2.8.0', '2.8.0-beta.1 (beta)', '2.8.0+dev.3 (dev build 1a2b3c4)'."""
    version = __version__ if version is None else version
    commit = BUILD_COMMIT if commit is None else commit
    channel = channel_of(version)
    if channel == "dev":
        return f"{version} (dev build{' ' + commit[:7] if commit else ''})"
    return f"{version} (beta)" if channel == "beta" else version


def channel_setting(s) -> str:
    """Settings.update_channel, sanitised."""
    return s.update_channel if s.update_channel in CHANNEL_RANK else "stable"


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


_DEV_NAME = re.compile(r"(\d+\.\d+\.\d+\S*\+dev\.?\d+)")


def _to_release(data: dict) -> Release | None:
    tag = data.get("tag_name", "")
    if tag == DEV_TAG:  # the rolling dev build says its version in its title
        m = _DEV_NAME.search(data.get("name") or "")
        version = m.group(1) if m else ""
    else:
        version = tag.lstrip("vV")
    assets = {a.get("name"): a for a in data.get("assets") or []}
    asset = assets.get(ASSET_NAME)
    if parse_version(version) is None or not asset or data.get("draft"):
        return None
    digest = asset.get("digest") or ""  # GitHub reports "sha256:<hex>" for uploaded assets
    sums = assets.get(SUMS_NAME) or {}
    commit = data.get("target_commitish") or ""
    return Release(
        version=version, tag=tag, name=data.get("name") or tag,
        notes=data.get("body") or "", page_url=data.get("html_url") or "",
        asset_url=asset["browser_download_url"], asset_size=int(asset.get("size") or 0),
        sha256=digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else "",
        sums_url=sums.get("browser_download_url", ""), prerelease=bool(data.get("prerelease")),
        commit=commit if re.fullmatch(r"[0-9a-f]{7,40}", commit) else "")


def releases(repo: str, channel: str = "stable") -> list[Release]:
    """The releases on `channel` (each channel includes the ones before it), newest first."""
    if not repo:
        raise UpdateError("Updates aren't set up: no GitHub repository is configured. "
                          "Enter it as owner/repo in Settings → Updates.")
    rank = CHANNEL_RANK.get(channel, 0)
    if rank == 0:
        found = [_to_release(_get_json(f"{API_BASE}/repos/{repo}/releases/latest", repo))]
    else:
        found = [_to_release(r) for r in _get_json(f"{API_BASE}/repos/{repo}/releases?per_page=30", repo)]
    found = [r for r in found if r and CHANNEL_RANK[r.channel] <= rank]
    return sorted(found, key=lambda r: parse_version(r.version), reverse=True)


def latest_release(repo: str, channel: str = "stable") -> Release | None:
    found = releases(repo, channel)
    return found[0] if found else None


def check(repo: str, channel: str = "stable", current: str | None = None) -> Release | None:
    """What to offer: the newest release on `channel` when it's newer than this version, else None.
    When this version is from a less tested channel than the one picked (a dev build, with Stable
    picked), the channel's newest is offered as a switch (Release.switch) even though it's older."""
    current = __version__ if current is None else current
    rel = latest_release(repo, channel)
    if rel is None:
        return None
    if is_newer(rel.version, current):
        return rel
    if CHANNEL_RANK[channel_of(current)] > CHANNEL_RANK.get(channel, 0) and parse_version(rel.version) != \
            parse_version(current):
        rel.switch = True
        return rel
    return None


# ---------------------------------------------------------------- download & unpack

def updates_dir() -> Path:
    """%LOCALAPPDATA%\\ClipMonster\\updates: downloads, the unpacked next version, the previous version
    and the update log. Never in the install folder, and never in OneDrive."""
    from .settings import local_dir

    return local_dir("updates")


def staging_dir() -> Path:
    """Where the next version is unpacked; its ytclip.exe installs it (swap.py)."""
    return updates_dir() / "next"


def previous_dir() -> Path:
    """The version installed before this one (ClipMonster\\ plus version.txt), kept by the install
    step so Settings → Updates can go back to it."""
    return updates_dir() / "previous"


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
    """Unpack the update for the installed folder `target` in staging_dir(); returns the new app folder."""
    staging = staging_dir()
    if not _writable(target):
        raise UpdateError(f"ClipMonster can't write to {target}. Install it again with {SETUP_NAME} from the "
                          "release page, which puts it in a folder you own.")
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
        raise UpdateError(f"Couldn't unpack the update in {staging} ({exc.strerror or exc}).") from exc
    new_app = staging / APP_FOLDER
    if not (new_app / EXE_NAME).exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError("The update archive doesn't contain ClipMonster.exe.")
    return new_app


def prepare(rel: Release, target: Path, progress: Callable[[float], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> Path:
    """Download, verify and unpack `rel` (or reuse it, when it's unpacked already); returns the new
    app folder. The zip is deleted as soon as it's unpacked."""
    from .swap import is_complete

    staged = staging_dir() / APP_FOLDER
    if load_state().get("staged") == rel.version and is_complete(staged):
        if progress:
            progress(1.0)
        return staged
    save_state(staged="")
    zip_path = download(rel, progress, cancelled)
    try:
        new_app = extract(zip_path, target, cancelled)
    finally:
        zip_path.unlink(missing_ok=True)
    save_state(staged=rel.version)
    return new_app


def previous_version() -> str:
    """The version Settings → Updates can go back to, or '' when there isn't one. Only versions
    from 2.8.0 on are kept, and only those can be gone back to: their installer knows how."""
    from .swap import is_complete

    try:
        version = (previous_dir() / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if parse_version(version) is None or parse_version(version) == parse_version(__version__):
        return ""
    return version if is_complete(previous_dir() / APP_FOLDER) else ""


def prepare_previous(target: Path) -> Path:
    """Copy the previous version to staging_dir(), to install it from there (its own folder can't be
    installed from: the install step keeps the version being replaced in it)."""
    version = previous_version()
    if not version:
        raise UpdateError("There's no previous version to go back to.")
    source = previous_dir() / APP_FOLDER
    size = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())
    _need_space(updates_dir(), size, "go back to the previous version")
    staged = staging_dir() / APP_FOLDER
    save_state(staged="")
    shutil.rmtree(staging_dir(), ignore_errors=True)
    try:
        shutil.copytree(source, staged)
    except OSError as exc:
        shutil.rmtree(staging_dir(), ignore_errors=True)
        raise UpdateError(f"Couldn't copy the previous version ({exc.strerror or exc}).") from exc
    save_state(staged=version)
    return staged


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


def stop_automation(timeout: float = 90.0, resume: bool = True) -> bool:
    """A background automation run keeps the app folder in use: ask it to stop (its current video
    goes back in the queue) and, with `resume`, have the updated app's window start it again. False
    if it didn't stop."""
    from .jobs import JobDB
    from .pipeline import runner_alive

    db = JobDB()
    try:
        if not runner_alive(db):
            return True
        db.set_meta("stop", "1")
        if resume:
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


def launch_swap(new_app: Path, target: Path, wait_pid: int, relaunch: str = EXE_NAME,
                relaunch_args: tuple[str, ...] = ()) -> None:
    """Start the detached helper that installs `new_app` over `target` once `wait_pid` exits: the new
    version's own ytclip.exe (swap.py), from the unpacked update. It keeps this version as the
    previous one, and starts `relaunch` (with `relaunch_args`) when it's done."""
    from . import swap

    cmd = swap.command(new_app, target, wait_pid, update_log(), relaunch, relaunch_args=relaunch_args,
                       keep_previous=previous_dir(), old_version=__version__)
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


LOG_LIMIT = 1 << 20  # the update log and the helper's output are cut back to their end past this size
PART_STALE = 3600.0  # a download nothing has written to for this long was abandoned


def _trim(path: Path, keep: int = LOG_LIMIT // 2) -> None:
    try:
        if path.stat().st_size > LOG_LIMIT:
            data = path.read_bytes()[-keep:]
            path.write_bytes(data[data.find(b"\n") + 1:])
    except OSError:
        pass


def _staged_is_next() -> bool:
    """Whether the unpacked update is a newer version still waiting to be installed."""
    staged = load_state().get("staged") or ""
    return bool(staged) and is_newer(staged, __version__) and (staging_dir() / APP_FOLDER).is_dir()


def tidy(target: Path | None = None) -> int:
    """After an update (and at each start): remove the unpacked update and the downloads (about 225 MB
    each; before 2.4.1 they were kept forever), and keep the logs short. An update that's unpacked
    and waiting to be installed stays. Returns how many were removed. Never fails."""
    removed = 0
    target = target or install_dir()
    folders = [target.parent / ".cm-up"] if target is not None else []  # where versions before 2.8.0 unpacked
    if not _staged_is_next():
        folders.append(staging_dir())
        if load_state().get("staged"):
            save_state(staged="")
    for folder in folders:
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
            removed += not folder.exists()
    try:
        files = list(updates_dir().iterdir())
    except OSError:
        files = []
    now = time.time()
    for f in files:
        try:
            stale_part = f.suffix == ".part" and now - f.stat().st_mtime > PART_STALE
            if (f.name.startswith("ClipMonster-windows-") and f.suffix == ".zip") or stale_part:
                f.unlink()
                removed += 1
        except OSError:
            pass
    for log in (update_log(), updates_dir() / "apply-update.out"):
        _trim(log)
    return removed


# ---------------------------------------------------------------- the installer, and copies from before it

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{92B949DB-584A-40C4-892B-F7016573CE28}_is1"
SETUP_MUTEX = "ClipMonsterSetup"  # packaging/ClipMonster.iss SetupMutex
APP_ITEMS = ("ClipMonster.exe", "ytclip.exe", "_internal", "whisper-cpp")  # what an old copy is made of


def setup_running() -> bool:
    """Whether ClipMonster-Setup.exe is running: it installs, so the app mustn't start an update too."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        handle = kernel32.OpenMutexW(0x00100000, False, SETUP_MUTEX)  # SYNCHRONIZE
        if handle:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return True
    except (OSError, AttributeError):
        pass
    return False


def _same_folder(a: str | Path, b: str | Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(a).rstrip("\\/"))) == \
            os.path.normcase(os.path.abspath(str(b).rstrip("\\/")))
    except (OSError, ValueError):
        return False


def refresh_uninstall_entry() -> None:
    """In-app updates replace the files the installer put there, so Windows' Installed apps list would
    keep showing the version installed with ClipMonster-Setup.exe: bring it up to date. Never fails."""
    target = install_dir()
    if target is None or sys.platform != "win32":
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            where, _ = winreg.QueryValueEx(key, "InstallLocation")
            if not _same_folder(where, target):
                return
            try:
                shown, _ = winreg.QueryValueEx(key, "DisplayVersion")
            except OSError:
                shown = ""
            if shown != __version__:
                winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, __version__)
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "ClipMonster")
    except OSError:
        pass


def _is_app_folder(folder: Path) -> bool:
    return (folder / EXE_NAME).is_file() and (folder / "_internal").is_dir()


def _old_copy_places(run_command: str = "") -> list[Path]:
    """Where copies from before the installer usually are: wherever Start with Windows pointed, the
    folder the last start ran from, and the folders people unzip into."""
    places = []
    m = re.match(r'\s*"?([^"]+?)\\ClipMonster\.exe"?', run_command or "", re.I)
    if m:
        places.append(Path(m.group(1)))
    last = load_state().get("install_dir")
    if last:
        places.append(Path(last))
    home = Path.home()
    roots = [home, home / "Desktop", home / "Documents", home / "Downloads"]
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if os.environ.get(var):
            roots += [Path(os.environ[var]) / "Desktop", Path(os.environ[var]) / "Documents"]
    places += [r / APP_FOLDER for r in roots]
    return places


def old_copies(run_command: str = "") -> list[Path]:
    """Other copies of ClipMonster on this PC (unzipped by hand before the installer) that aren't the
    one running and weren't kept on purpose. Remembers where this one is, for next time."""
    target = install_dir()
    if target is None:
        return []
    state = load_state()
    kept = state.get("kept_copies") or []
    found: list[Path] = []
    for place in _old_copy_places(run_command):
        if _same_folder(place, target) or any(_same_folder(place, k) for k in kept) or \
                any(_same_folder(place, f) for f in found):
            continue
        try:
            if _is_app_folder(place) and target not in place.resolve().parents:
                found.append(place)
        except OSError:
            pass
    if not _same_folder(state.get("install_dir") or "", target):
        save_state(install_dir=str(target))
    return found


def keep_copy(folder: Path) -> None:
    """Don't offer to remove this copy again."""
    save_state(kept_copies=[*(load_state().get("kept_copies") or []), str(folder)])


def remove_copy(folder: Path) -> str:
    """Remove an old copy's own files (your own files in the folder stay, and so does the folder when
    they're there). '' when it's gone, else why not."""
    from .procs import app_processes
    from .swap import BACKUP

    if not _is_app_folder(folder) or install_dir() is not None and _same_folder(folder, install_dir()):
        return "That isn't an old copy of ClipMonster."
    running = app_processes(folder)
    if running:
        return "It's running: close " + ", ".join(f"{p.name} (process {pid})" for pid, p in running) + " first."
    left = []
    for item in [*(folder / n for n in APP_ITEMS), *folder.glob(BACKUP + "*")]:
        try:
            if item.is_dir():
                shutil.rmtree(item)
            elif item.exists():
                item.unlink()
        except OSError as exc:
            left.append(f"{item.name} ({exc.strerror or exc})")
    try:
        folder.rmdir()  # only when nothing of yours is left in it
    except OSError:
        pass
    return ("Couldn't remove all of it: " + ", ".join(left)) if left else ""


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
    try:
        last = float(load_state().get("last_check", 0))
    except (TypeError, ValueError):
        last = 0.0
    return time.time() - last > hours * 3600


INSTALL_WINDOW = 300  # seconds: a helper that hasn't finished by then has died or given up


def finished_update_message() -> tuple[str, bool] | None:
    """After a restart: did the update we started succeed? Returns (message, ok) once."""
    pending = load_state().get("pending")
    if not pending:
        return None
    if parse_version(pending) == parse_version(__version__):
        save_state(pending="", staged="")
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

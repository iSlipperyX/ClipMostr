"""Self-update from GitHub Releases.

A release is a git tag like v1.2.0 whose GitHub Release carries ClipMonster-windows.zip
and SHA256SUMS.txt (both made by .github/workflows/release.yml). Updating:

1. check()    - ask the GitHub API for the newest release and compare versions.
2. download() - stream the zip to %LOCALAPPDATA%\\ClipMonster\\updates and verify SHA-256.
3. extract()  - unpack next to the installed app (same drive, so the swap is a rename).
4. launch_swap() - a small PowerShell script waits for the app to exit, moves the old
   folder aside, moves the new one in, relaunches, and rolls back if anything fails.
   (A running app can't replace its own files on Windows.)
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
            longest = max((len(str(target.parent / target.name / Path(m.filename).relative_to(APP_FOLDER)))
                           for m in members if m.filename.startswith(APP_FOLDER + "/")), default=0)
            longest = max(longest, max((len(str(root / m.filename)) for m in members), default=0))
            if longest > MAX_PATH:
                raise UpdateError(f"The ClipMonster folder is too deep for Windows' path length limit "
                                  f"({longest} characters needed, {MAX_PATH} allowed). Move it to a shorter "
                                  f"location such as C:\\ClipMonster and try again.")
            _need_space(target.parent, sum(m.file_size for m in members), "unpack the update")
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
    if os.name != "nt":
        return []
    query = ("Get-CimInstance Win32_Process -Filter \"Name='ClipMonster.exe' OR Name='ytclip.exe'\" | "
             "ForEach-Object { \"$($_.ProcessId)`t$($_.ExecutablePath)\" }")
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    root = str(target.resolve()).lower()
    for line in out.splitlines():
        pid, _, path = line.partition("\t")
        if pid.strip().isdigit() and int(pid) != os.getpid() and path.lower().startswith(root + os.sep):
            found.append(f"{Path(path).name} (process {pid.strip()})")
    return found


# ---------------------------------------------------------------- swap

SWAP_SCRIPT = r"""
param([int]$WaitPid, [string]$NewDir, [string]$InstallDir, [string]$Relaunch, [string]$LogFile)
$ErrorActionPreference = 'Stop'
function Log([string]$m) { Add-Content -LiteralPath $LogFile -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m) -Encoding UTF8 }
$backup = "$InstallDir.old"
$staging = Split-Path -Parent $NewDir
$parent = Split-Path -Parent $InstallDir

# Folders only ever move with [IO.Directory]::Move: a rename that either fully happens or
# changes nothing. (Move-Item half-moves a folder when a file inside is briefly locked by
# antivirus or OneDrive, and a retry then nests it.) .NET calls also take paths literally,
# so [ ] in folder names are safe.
function Retry([scriptblock]$action, [string]$what, [int]$tries = 40) {
  for ($i = 1; $i -le $tries; $i++) {
    try { & $action; return }
    catch {
      if ($i -eq $tries) { throw "Could not $what (files may be in use): $($_.Exception.Message)" }
      Start-Sleep -Milliseconds 500
    }
  }
}
function Start-App {
  if (-not $Relaunch) { return }
  try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $InstallDir $Relaunch
    $psi.WorkingDirectory = $env:USERPROFILE  # not the app folder, so nothing pins it during the next update
    $psi.UseShellExecute = $true
    [void][System.Diagnostics.Process]::Start($psi)
  } catch { Log "Could not restart ClipMonster: $($_.Exception.Message)" }
}
function Is-Complete([string]$dir) {
  (Test-Path -LiteralPath (Join-Path $dir 'ClipMonster.exe')) -and (Test-Path -LiteralPath (Join-Path $dir '_internal'))
}

try {
  Log "Update started: waiting for process $WaitPid to exit"
  $p = Get-Process -Id $WaitPid -ErrorAction SilentlyContinue
  if ($p) { if (-not $p.WaitForExit(120000)) { throw "ClipMonster did not close within 2 minutes" } }
  # A backup from an earlier run is only stale while a working install exists.
  if ((Test-Path -LiteralPath $backup) -and (Is-Complete $InstallDir)) {
    Retry { [System.IO.Directory]::Delete($backup, $true) } "remove an old backup"
  }
  Retry { [System.IO.Directory]::Move($InstallDir, $backup) } "move the old version aside"
  try {
    Retry { [System.IO.Directory]::Move($NewDir, $InstallDir) } "move the new version into place"
    if (-not (Is-Complete $InstallDir)) { throw "The new version is incomplete" }
  } catch {
    Log "Install failed, restoring the previous version: $($_.Exception.Message)"
    try {
      if (Test-Path -LiteralPath $InstallDir) {
        $aside = "$InstallDir.failed-" + (Get-Date -Format 'yyyyMMddHHmmss')
        Retry { [System.IO.Directory]::Move($InstallDir, $aside) } "set the failed version aside" 120
      }
      Retry { [System.IO.Directory]::Move($backup, $InstallDir) } "restore the previous version" 120
      Log "Previous version restored"
    } catch {
      $note = Join-Path $parent 'CLIPMONSTER-UPDATE-FAILED.txt'
      Set-Content -LiteralPath $note -Encoding UTF8 -Value @(
        "ClipMonster's update could not finish, and the previous version could not be put back automatically.",
        "To recover, rename the folder '$(Split-Path -Leaf $backup)' (next to this file) back to '$(Split-Path -Leaf $InstallDir)'.",
        "Details: $LogFile")
      Log "RESTORE FAILED: $($_.Exception.Message). Recovery instructions: $note"
    }
    throw
  }
  Log "New version installed in $InstallDir"
  Start-App
  Start-Sleep -Seconds 3
  try {
    Retry { [System.IO.Directory]::Delete($backup, $true) } "remove the backup" 20
    if (Test-Path -LiteralPath $staging) { [System.IO.Directory]::Delete($staging, $true) }
  } catch { Log "Cleanup left files behind (harmless, removed on the next update): $($_.Exception.Message)" }
  Log "Update finished"
} catch {
  Log "Update failed: $($_.Exception.Message)"
  if (Is-Complete $InstallDir) { Start-App }
}
"""


def update_log() -> Path:
    return updates_dir() / "update.log"


def launch_swap(new_app: Path, target: Path, wait_pid: int, relaunch: str = EXE_NAME) -> None:
    """Start the detached helper that installs `new_app` over `target` once `wait_pid` exits."""
    script = updates_dir() / "apply-update.ps1"
    script.write_text(SWAP_SCRIPT, encoding="utf-8-sig")  # BOM: Windows PowerShell 5.1 then reads UTF-8 paths
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
           "-File", str(script), "-WaitPid", str(wait_pid), "-NewDir", str(new_app), "-InstallDir", str(target),
           "-Relaunch", relaunch, "-LogFile", str(update_log())]
    # A hidden console of its own (not DETACHED_PROCESS: powershell.exe with no console at all
    # exits immediately without running the script) in its own process group, so neither
    # closing the app nor Ctrl+C in a terminal stops it.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    # Anything PowerShell prints before the script's own logging starts (e.g. a parse or
    # policy error) lands here instead of vanishing with the detached console.
    with open(updates_dir() / "apply-update.out", "ab") as out:
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=out, close_fds=True, cwd=str(updates_dir()))
        try:  # leave any job object so the helper outlives the app
            subprocess.Popen(cmd, creationflags=flags | breakaway, **kwargs)
        except OSError:
            subprocess.Popen(cmd, creationflags=flags, **kwargs)


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


def finished_update_message() -> tuple[str, bool] | None:
    """After a restart: did the update we started succeed? Returns (message, ok) once."""
    pending = load_state().get("pending")
    if not pending:
        return None
    save_state(pending="")
    if parse_version(pending) == parse_version(__version__):
        return f"Updated to ClipMonster {__version__}.", True
    return (f"The update to {pending} didn't complete, so you're still on {__version__}. "
            f"Details: {update_log()}", False)

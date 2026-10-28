"""ClipMonster's own YouTube sign-in: a Firefox profile kept just for it.

"Sign in to YouTube" opens Firefox with this profile (separate from your everyday Firefox, if you
have one). You sign in once and close it; from then on yt-dlp reads that sign-in's cookies straight
from the profile for every download, so YouTube treats ClipMonster's requests as a signed-in viewer
and very rarely asks this PC to confirm it's not a bot. Nothing else opens the profile, so the
cookies aren't rotated away by a browser tab, which is what usually makes a copied sign-in stop
working. Firefox is used because its cookies can be read without the encryption Chrome and Edge
now lock theirs with.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .settings import work_folder

# The value of Settings.cookies_browser that means "this profile".
CHOICE = "clipmonster"
SIGN_IN_URL = ("https://accounts.google.com/ServiceLogin?service=youtube&passive=true"
               "&continue=https%3A%2F%2Fwww.youtube.com%2F")
GET_FIREFOX_URL = "https://www.mozilla.org/firefox/new/"
# Cookies YouTube only sets for a signed-in viewer.
SIGNED_IN_COOKIES = ("LOGIN_INFO", "SAPISID", "__Secure-3PAPISID")

# Written to the profile's user.js, so it skips Firefox's first-run pages and keeps the sign-in
# when it closes. Passwords aren't saved: the cookies are all ClipMonster needs.
PREFS = {
    "browser.shell.checkDefaultBrowser": False,
    "browser.aboutwelcome.enabled": False,
    "trailhead.firstrun.didSeeAboutWelcome": True,
    "browser.startup.homepage_override.mstone": "ignore",
    "startup.homepage_welcome_url": "",
    "startup.homepage_welcome_url.additional": "",
    "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "browser.privatebrowsing.autostart": False,
    "privacy.sanitize.sanitizeOnShutdown": False,
    "privacy.clearOnShutdown.cookies": False,
    "privacy.clearOnShutdown_v2.cookiesAndStorage": False,
    "signon.rememberSignons": False,
    "browser.sessionstore.resume_from_crash": False,
}


def profile_dir() -> Path:
    """Where the profile lives: the local (non-roaming) app data, never a synced folder."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "ClipMonster" / "youtube-login"


def cookie_option(browser: str | None) -> tuple | None:
    """yt-dlp's cookiesfrombrowser for a Settings.cookies_browser value. ClipMonster's sign-in
    before it's made (or after it was forgotten) means no sign-in, not a download that fails."""
    browser = (browser or "").strip()
    if not browser:
        return None
    if browser == CHOICE:
        folder = profile_dir()
        return ("firefox", str(folder)) if (folder / "cookies.sqlite").is_file() else None
    return (browser,)


def find_firefox() -> Path | None:
    """Firefox's program, wherever it was installed (for everyone, just for you, or on PATH)."""
    found = shutil.which("firefox")
    if found:
        return Path(found)
    if sys.platform != "win32":
        return None
    try:
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe") as k:
                    path = Path(str(winreg.QueryValueEx(k, "")[0]).strip('"'))
            except OSError:
                continue
            if path.is_file():
                return path
    except ImportError:
        pass
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base and (Path(base) / "Mozilla Firefox" / "firefox.exe").is_file():
            return Path(base) / "Mozilla Firefox" / "firefox.exe"
    return None


def _pref(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def prepare_profile(folder: Path | None = None) -> Path:
    folder = folder or profile_dir()
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["// Written by ClipMonster for its YouTube sign-in. Rewritten each time it opens Firefox."]
    lines += [f'user_pref("{name}", {_pref(value)});' for name, value in PREFS.items()]
    (folder / "user.js").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def sign_in_command(firefox: Path, folder: Path) -> list[str]:
    # -no-remote: a Firefox of its own, even when your everyday Firefox is open.
    return [str(firefox), "-no-remote", "-profile", str(folder), SIGN_IN_URL]


def open_sign_in(firefox: Path | None = None, folder: Path | None = None) -> subprocess.Popen:
    """Open Firefox on Google's sign-in page for YouTube, in ClipMonster's profile.
    Raises FileNotFoundError when Firefox isn't installed."""
    firefox = firefox or find_firefox()
    if not firefox:
        raise FileNotFoundError("Firefox isn't installed")
    folder = prepare_profile(folder)
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    try:
        return subprocess.Popen(sign_in_command(firefox, folder), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError:
        if not flags & getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0):
            raise
        # Some job objects don't allow breaking away; Firefox then just closes with ClipMonster.
        return subprocess.Popen(sign_in_command(firefox, folder), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)


def firefox_open(folder: Path | None = None) -> bool:
    """Whether a Firefox has this profile open. (Firefox on Windows hands over to a second process
    as it starts, so the process ClipMonster started says nothing about that.)"""
    folder = folder or profile_dir()
    if sys.platform == "win32":
        lock = folder / "parent.lock"
        if not lock.exists():
            return False
        try:  # Firefox holds it open with no sharing while it runs
            with open(lock, "r+b"):
                return False
        except PermissionError:
            return True
        except OSError:
            return False
    return os.path.lexists(folder / "lock")


@dataclass
class SignIn:
    signed_in: bool
    expires: float = 0.0  # when the sign-in cookie runs out (a time.time()), 0 = not known


def read_sign_in(folder: Path | None = None, now: float | None = None) -> SignIn:
    """Whether the profile is signed in to YouTube. Works while Firefox is open: the newest cookies
    are still in the database's -wal file then, so that's copied too."""
    folder = folder or profile_dir()
    now = time.time() if now is None else now
    db = folder / "cookies.sqlite"
    if not db.is_file():
        return SignIn(False)
    with work_folder("login-") as tmp:
        copy = Path(tmp) / "cookies.sqlite"
        try:
            shutil.copyfile(db, copy)
            if (folder / "cookies.sqlite-wal").is_file():
                shutil.copyfile(folder / "cookies.sqlite-wal", Path(tmp) / "cookies.sqlite-wal")
        except OSError:
            return SignIn(False)
        try:
            con = sqlite3.connect(str(copy))
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
                marks = ",".join("?" * len(SIGNED_IN_COOKIES))
                rows = con.execute(f"SELECT expiry FROM moz_cookies WHERE host LIKE '%youtube.com' "
                                   f"AND name IN ({marks})", SIGNED_IN_COOKIES).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return SignIn(False)
    # Firefox 142 (database version 16) started storing expiry in milliseconds.
    expiries = [(e / 1000 if version >= 16 else e) for (e,) in rows if e]
    live = [e for e in expiries if e > now]
    if not live:
        return SignIn(False)
    return SignIn(True, min(live))


def forget(folder: Path | None = None) -> None:
    """Delete the profile (and with it the sign-in). Raises OSError while Firefox has it open."""
    folder = folder or profile_dir()
    if firefox_open(folder):
        raise OSError("Close the Firefox window ClipMonster opened first")
    if folder.exists():
        shutil.rmtree(folder)

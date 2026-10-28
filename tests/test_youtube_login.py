import os
import sqlite3
import sys
import time

import pytest
from conftest import NOW

from ytclip import cli, doctor, pipeline, youtube_login
from ytclip.download import _base_opts
from ytclip.jobs import Job, JobDB
from ytclip.settings import Settings

YEAR = 365 * 86400


def _cookies(folder, rows, version=16, wal=False):
    """A Firefox cookies.sqlite with these (host, name, expiry in seconds) rows. Firefox 142+
    (database version 16) stores expiry in milliseconds."""
    folder.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(folder / "cookies.sqlite"))
    con.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT DEFAULT '', name TEXT, "
                "value TEXT, host TEXT, path TEXT, expiry INTEGER, isSecure INTEGER)")
    con.execute(f"PRAGMA user_version = {version}")
    con.commit()
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
    scale = 1000 if version >= 16 else 1
    con.executemany("INSERT INTO moz_cookies (name, value, host, path, expiry, isSecure) VALUES (?, 'v', ?, '/', ?, 1)",
                    [(name, host, int(expiry * scale)) for host, name, expiry in rows])
    con.commit()
    return con  # still open: with wal=True the rows are only in cookies.sqlite-wal until it closes


def test_the_setting_picks_the_cookies_yt_dlp_reads(tmp_path):
    assert youtube_login.cookie_option("") is None and youtube_login.cookie_option(None) is None
    assert youtube_login.cookie_option("chrome") == ("chrome",)
    # Chosen but never signed in (or forgotten): downloads go unsigned instead of failing.
    assert youtube_login.cookie_option("clipmonster") is None
    _cookies(youtube_login.profile_dir(), [(".youtube.com", "LOGIN_INFO", NOW + YEAR)]).close()
    assert youtube_login.cookie_option("clipmonster") == ("firefox", str(youtube_login.profile_dir()))
    assert _base_opts(None, "clipmonster")["cookiesfrombrowser"] == ("firefox", str(youtube_login.profile_dir()))
    assert "cookiesfrombrowser" not in _base_opts(None, "")


def test_the_profile_lives_in_local_app_data(tmp_path):
    assert youtube_login.profile_dir() == tmp_path / "local" / "ClipMonster" / "youtube-login"
    assert not youtube_login.profile_dir().exists()  # asking where it is makes nothing


def test_the_profile_keeps_its_sign_in_and_skips_first_run_pages(tmp_path):
    folder = youtube_login.prepare_profile()
    text = (folder / "user.js").read_text(encoding="utf-8")
    assert 'user_pref("privacy.clearOnShutdown.cookies", false);' in text
    assert 'user_pref("browser.aboutwelcome.enabled", false);' in text
    assert 'user_pref("signon.rememberSignons", false);' in text  # the password is never saved
    assert 'user_pref("startup.homepage_welcome_url", "");' in text


def test_firefox_opens_its_own_window_on_the_sign_in_page(tmp_path, monkeypatch):
    calls = []

    class Proc:
        def __init__(self, cmd, **kw):
            calls.append((cmd, kw))

    monkeypatch.setattr(youtube_login.subprocess, "Popen", Proc)
    youtube_login.open_sign_in(tmp_path / "firefox.exe")
    cmd, kw = calls[0]
    folder = youtube_login.profile_dir()
    assert cmd == [str(tmp_path / "firefox.exe"), "-no-remote", "-profile", str(folder), youtube_login.SIGN_IN_URL]
    assert (folder / "user.js").is_file() and kw["stdin"] == youtube_login.subprocess.DEVNULL
    assert "accounts.google.com" in youtube_login.SIGN_IN_URL and "youtube.com" in youtube_login.SIGN_IN_URL


def test_no_firefox_says_so(monkeypatch):
    monkeypatch.setattr(youtube_login, "find_firefox", lambda: None)
    with pytest.raises(FileNotFoundError):
        youtube_login.open_sign_in()


def test_firefox_is_found_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_login.shutil, "which", lambda name: str(tmp_path / name))
    assert youtube_login.find_firefox() == tmp_path / "firefox"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows install folders")
def test_firefox_is_found_in_program_files(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_login.shutil, "which", lambda name: None)
    exe = tmp_path / "Mozilla Firefox" / "firefox.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.setenv(var, str(tmp_path / "nowhere"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    got = youtube_login.find_firefox()
    assert got is not None and got.name.lower() == "firefox.exe"  # here, or a real install the registry knows


def test_signed_in_is_read_from_youtube_cookies(tmp_path):
    folder = tmp_path / "p"
    assert not youtube_login.read_sign_in(folder, NOW).signed_in  # no profile yet
    _cookies(folder, [(".google.com", "SAPISID", NOW + YEAR), (".youtube.com", "PREF", NOW + YEAR)]).close()
    assert not youtube_login.read_sign_in(folder, NOW).signed_in  # signed in to Google, not YouTube


def test_the_sign_in_and_when_it_runs_out(tmp_path):
    folder = tmp_path / "p"
    _cookies(folder, [(".youtube.com", "LOGIN_INFO", NOW + YEAR), (".youtube.com", "SAPISID", NOW + YEAR / 2)]).close()
    got = youtube_login.read_sign_in(folder, NOW)
    assert got.signed_in and got.expires == pytest.approx(NOW + YEAR / 2)


def test_older_firefox_stores_expiry_in_seconds(tmp_path):
    folder = tmp_path / "p"
    _cookies(folder, [(".youtube.com", "LOGIN_INFO", NOW + 3600)], version=15).close()
    got = youtube_login.read_sign_in(folder, NOW)
    assert got.signed_in and got.expires == pytest.approx(NOW + 3600)


def test_a_sign_in_that_ran_out_is_signed_out(tmp_path):
    folder = tmp_path / "p"
    _cookies(folder, [(".youtube.com", "LOGIN_INFO", NOW - 60)]).close()
    assert not youtube_login.read_sign_in(folder, NOW).signed_in


def test_a_sign_in_still_in_firefoxs_wal_file_counts(tmp_path):
    folder = tmp_path / "p"
    con = _cookies(folder, [(".youtube.com", "LOGIN_INFO", NOW + YEAR)], wal=True)
    try:
        assert (folder / "cookies.sqlite-wal").stat().st_size > 0
        assert youtube_login.read_sign_in(folder, NOW).signed_in
    finally:
        con.close()


def test_a_broken_cookie_file_is_signed_out(tmp_path):
    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "cookies.sqlite").write_bytes(b"not a database")
    assert not youtube_login.read_sign_in(folder, NOW).signed_in


@pytest.mark.skipif(sys.platform == "win32", reason="Firefox locks its profile with a lock symlink off Windows")
def test_firefox_open_follows_its_profile_lock(tmp_path):
    folder = tmp_path / "p"
    folder.mkdir()
    assert not youtube_login.firefox_open(folder)
    os.symlink("127.0.0.1:+12345", folder / "lock")  # dangling, as Firefox makes it
    assert youtube_login.firefox_open(folder)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing")
def test_firefox_open_follows_its_parent_lock_on_windows(tmp_path):
    import ctypes
    from ctypes import wintypes

    folder = tmp_path / "p"
    folder.mkdir()
    lock = folder / "parent.lock"
    assert not youtube_login.firefox_open(folder)
    lock.write_bytes(b"")
    assert not youtube_login.firefox_open(folder)  # left behind by a Firefox that closed
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    handle = kernel32.CreateFileW(str(lock), 0xC0000000, 0, None, 3, 0x80, None)  # no sharing, as Firefox
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        assert youtube_login.firefox_open(folder)
    finally:
        kernel32.CloseHandle(handle)
    assert not youtube_login.firefox_open(folder)


def test_forget_deletes_the_profile_but_not_while_firefox_has_it(tmp_path, monkeypatch):
    folder = youtube_login.profile_dir()
    _cookies(folder, [(".youtube.com", "LOGIN_INFO", NOW + YEAR)]).close()
    monkeypatch.setattr(youtube_login, "firefox_open", lambda f=None: True)
    with pytest.raises(OSError):
        youtube_login.forget()
    assert folder.exists()
    monkeypatch.setattr(youtube_login, "firefox_open", lambda f=None: False)
    youtube_login.forget()
    assert not folder.exists()
    youtube_login.forget()  # nothing to forget is fine


def _signed_in_profile(expires=NOW + YEAR):
    _cookies(youtube_login.profile_dir(), [(".youtube.com", "LOGIN_INFO", expires)]).close()


def test_doctor_says_whether_downloads_are_signed_in(tmp_path):
    s = Settings(cookies_browser="clipmonster")
    got = {c.name: c for c in doctor._youtube_sign_in(s, None, NOW)}
    assert got["YouTube sign-in"].level == doctor.WARN and "Sign in to YouTube" in got["YouTube sign-in"].fix
    _signed_in_profile()
    got = {c.name: c for c in doctor._youtube_sign_in(s, None, NOW)}
    year = time.strftime("%Y", time.localtime(NOW + YEAR))
    assert got["YouTube sign-in"].level == doctor.GOOD and year in got["YouTube sign-in"].detail
    assert doctor._youtube_sign_in(Settings(), None, NOW) == []  # not asked to sign in lately: nothing to say


def test_the_pause_message_points_to_sign_in(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    line = pipeline.record_block(db, "Sign in to confirm you're not a bot.", NOW)
    assert "press Sign in to YouTube in Automation" in line and "Try now" in line
    line = pipeline.record_block(db, "Sign in to confirm you're not a bot.", NOW, "clipmonster")
    assert "may have run out" in line and "sign in again" in line
    line = pipeline.record_block(db, "Sign in to confirm you're not a bot.", NOW, "chrome")
    assert "Chrome's YouTube sign-in may have run out" in line


def _cli_env(monkeypatch, signed_after_close=True):
    """A Firefox that's open for two looks, then closed (signed in by then, or not)."""
    looks = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(youtube_login, "open_sign_in", lambda: looks.append("opened"))

    def is_open(folder=None):
        looks.append("look")
        if looks.count("look") < 3:
            return True
        if signed_after_close:
            _signed_in_profile(time.time() + YEAR)
        return False

    monkeypatch.setattr(youtube_login, "firefox_open", is_open)
    return looks


def test_ytclip_sign_in_waits_for_firefox_then_uses_the_sign_in(tmp_path, monkeypatch):
    db = JobDB()
    db.add(Job("a", "https://youtu.be/a", status="queued"), time.time())
    pipeline.record_block(db, "Sign in to confirm you're not a bot.", time.time())
    db.close()
    looks = _cli_env(monkeypatch)
    assert cli.run_sign_in([]) == 0
    assert looks[0] == "opened" and looks.count("look") == 3
    assert Settings.load().cookies_browser == "clipmonster"
    db = JobDB()
    assert not pipeline.paused_until(db, time.time())  # the sign-in ends the pause
    db.close()


def test_ytclip_sign_in_closed_without_signing_in(tmp_path, monkeypatch):
    _cli_env(monkeypatch, signed_after_close=False)
    assert cli.run_sign_in([]) == 1
    assert Settings.load().cookies_browser == ""


def test_ytclip_sign_in_status_and_forget(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_login, "open_sign_in", lambda: pytest.fail("--status opened Firefox"))
    assert cli.run_sign_in(["--status"]) == 1
    _signed_in_profile(time.time() + YEAR)
    assert cli.run_sign_in(["--status"]) == 0 and Settings.load().cookies_browser == "clipmonster"
    assert cli.run_sign_in(["--forget"]) == 0
    assert not youtube_login.profile_dir().exists() and Settings.load().cookies_browser == ""


def test_ytclip_sign_in_without_firefox(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_login, "find_firefox", lambda: None)
    assert cli.run_sign_in([]) == 1
    assert cli.main(["sign-in", "--status"]) == 1  # dispatched from the main command too

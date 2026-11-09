"""Logging in through a plain browser window (TikTok refuses logins in an automated one)."""

import os
import stat
import time

import pytest

from ytclip.posting.config import Settings
from ytclip.posting.tiktok import login
from ytclip.posting.tiktok.browser import Cancelled

pytestmark = pytest.mark.skipif(os.name == "nt", reason="uses a shell script as the stand-in browser")


def fake_browser(tmp_path, seconds: float):
    """A stand-in 'browser' that records its arguments and 'closes' after `seconds`."""
    exe = tmp_path / "msedge.exe"
    exe.write_text(f"#!/bin/sh\necho \"$@\" > '{tmp_path}/args.txt'\nsleep {seconds}\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    real_sleep = time.sleep
    monkeypatch.setattr(login.time, "sleep", lambda s: real_sleep(min(s, 0.05)))


def test_opens_a_plain_window_on_the_profile_and_waits_for_it_to_close(tmp_path):
    exe = fake_browser(tmp_path, 0.3)
    profile = tmp_path / "profile"
    profile.mkdir()
    lines = []
    assert login.login_window(Settings(), profile, lines.append, browser=exe) is True
    args = (tmp_path / "args.txt").read_text(encoding="utf-8")
    assert f"--user-data-dir={profile}" in args and args.strip().endswith(login.LOGIN_URL)
    # Only a DevTools port for reading cookies; no automation switches for TikTok to spot.
    assert "--remote-debugging-port=0" in args
    assert "enable-automation" not in args and "remote-debugging-pipe" not in args
    assert "connects by itself" in lines[0]


def test_cancel_closes_the_window(tmp_path):
    exe = fake_browser(tmp_path, 30)
    with pytest.raises(Cancelled):
        login.login_window(Settings(), tmp_path, lambda m: None, cancelled=lambda: True, browser=exe)


def test_window_left_open_too_long_times_out(tmp_path):
    exe = fake_browser(tmp_path, 30)
    with pytest.raises(TimeoutError):
        login.login_window(Settings(), tmp_path, lambda m: None, browser=exe, timeout=0.3)


def test_no_browser_found_means_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "find_browser", lambda channel: None)
    assert login.login_window(Settings(), tmp_path, lambda m: None) is False


def test_profile_already_open_is_refused(tmp_path):
    (tmp_path / "SingletonLock").symlink_to("somehost-123")
    with pytest.raises(RuntimeError, match="already open"):
        login.login_window(Settings(), tmp_path, lambda m: None, browser=fake_browser(tmp_path, 0))


def test_finds_edge_and_chrome_in_program_files(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "_ELSEWHERE", {"msedge": (), "chrome": ()})  # not a Chrome this machine has
    for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / var))
    edge = tmp_path / "PROGRAMFILES(X86)" / "Microsoft/Edge/Application/msedge.exe"
    chrome = tmp_path / "LOCALAPPDATA" / "Google/Chrome/Application/chrome.exe"
    for p in (edge, chrome):
        p.parent.mkdir(parents=True)
        p.write_bytes(b"")
    assert login.find_browser("msedge") == edge
    assert login.find_browser("chrome") == chrome


CHROMIUM = os.environ.get("TOKMANAGER_TEST_CHROMIUM")


def real_browser(tmp_path):
    """The test Chromium, headless, standing in for Edge."""
    exe = tmp_path / "msedge.exe"
    exe.write_text(f"#!/bin/sh\nexec '{CHROMIUM}' --headless=new --no-sandbox \"$@\"\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def set_session_cookie(profile, delay):
    """Log in from 'outside', as the person would: TikTok's session cookie appears in the jar."""
    import threading

    from ytclip.posting.tiktok import cdp

    def run():
        end = time.time() + 20
        while time.time() < end:
            ep = cdp.endpoint(profile)
            if ep:
                time.sleep(delay)
                with cdp.CDP(*ep) as c:
                    c.call("Storage.setCookies", {"cookies": [{
                        "name": "sessionid", "value": "abc123", "domain": ".tiktok.com", "path": "/",
                        "secure": True, "httpOnly": True, "expires": time.time() + 86400}]})
                return
            time.sleep(0.1)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


@pytest.mark.skipif(not CHROMIUM, reason="set TOKMANAGER_TEST_CHROMIUM to a Chromium binary")
def test_notices_the_login_and_closes_the_window_by_itself(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    lines = []
    set_session_cookie(profile, delay=1)
    start = time.time()
    assert login.login_window(Settings(), profile, lines.append, browser=real_browser(tmp_path),
                              url="about:blank", timeout=60) is True
    assert time.time() - start < 40
    assert any("You're logged in" in line for line in lines)
    assert not login.profile_in_use(profile)
    # The cookie was written to the profile, where the automated browser will find it.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), executable_path=CHROMIUM, headless=True)
        names = [c["name"] for c in ctx.cookies("https://www.tiktok.com")]
        ctx.close()
    assert "sessionid" in names


@pytest.mark.skipif(not CHROMIUM, reason="set TOKMANAGER_TEST_CHROMIUM to a Chromium binary")
def test_im_logged_in_button_closes_the_window(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    lines = []
    start = time.time()
    assert login.login_window(Settings(), profile, lines.append, browser=real_browser(tmp_path),
                              url="about:blank", timeout=60, finish=lambda: time.time() - start > 2) is True
    assert any("Closing the" in line for line in lines)

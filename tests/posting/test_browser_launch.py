"""How the posting browser is started: nothing Edge/Chrome warn about (and, since 3.8, nothing injected to hide
that it's automated: tests/test_account_persistence.py)."""

import os

import pytest

from ytclip.posting.config import Settings
from ytclip.posting.tiktok import browser

# Every switch on Chromium's "unsupported command-line flag" list that Playwright or the app could add
WARNED = ("--no-sandbox", "--disable-blink-features", "--enable-blink-features", "--disable-web-security",
          "--ignore-certificate-errors", "--single-process", "--disable-gpu-sandbox")


def test_launch_options_avoid_flags_that_show_a_warning_bar(tmp_path):
    opts = browser.TikTokBrowser(Settings(), tmp_path, log=lambda m: None).launch_options()
    assert not any(a.startswith(w) for a in opts["args"] for w in WARNED)
    assert opts["ignore_default_args"] == ["--enable-automation"]  # no "controlled by automated software" bar
    assert opts["chromium_sandbox"] is (os.name == "nt")  # Playwright adds --no-sandbox without this
    assert opts["channel"] == ("msedge" if os.name == "nt" else "chrome")  # Linux hosts post with Chrome


def test_a_hidden_window_stops_at_a_captcha_instead_of_waiting(tmp_path, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    exe = os.environ.get("TOKMANAGER_TEST_CHROMIUM")
    if not exe:
        pytest.skip("set TOKMANAGER_TEST_CHROMIUM to a Chromium for Playwright")
    b = browser.TikTokBrowser(Settings(), tmp_path / "p", log=lambda m: None, headless=True)
    opts = b.launch_options()
    opts.pop("channel")
    monkeypatch.setattr(b, "launch_options", lambda: {**opts, "executable_path": exe})
    with b:
        b.page.set_content("<p>profile</p>")
        assert not b._captcha_showing()
        b._wait_for_captcha(5)  # nothing to wait for
        b.page.set_content("<div id='captcha_container' style='width:200px;height:200px'>slide</div>")
        assert b._captcha_showing()
        with pytest.raises(browser.BrowserError, match="captcha"):
            b._wait_for_captcha(5)

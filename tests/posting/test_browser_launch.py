"""How the posting browser is started: nothing Edge/Chrome warn about, and navigator.webdriver like a normal browser."""

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
    assert opts["channel"] == "msedge"


def test_navigator_webdriver_reads_like_a_normal_browser(tmp_path):
    pw_mod = pytest.importorskip("playwright.sync_api")
    with pw_mod.sync_playwright() as pw:
        opts = browser.TikTokBrowser(Settings(), tmp_path, log=lambda m: None).launch_options()
        opts.pop("channel")
        opts["headless"] = True
        exe = os.environ.get("TOKMANAGER_TEST_CHROMIUM")
        try:
            ctx = pw.chromium.launch_persistent_context(str(tmp_path / "p"), **opts, **(
                {"executable_path": exe} if exe else {}))
        except Exception as exc:
            pytest.skip(f"no Chromium for Playwright ({str(exc)[:80]})")
        ctx.add_init_script(browser.WEBDRIVER_SCRIPT)
        page = ctx.pages[0]
        page.goto("data:text/html,<p>hi</p>")
        assert page.evaluate("navigator.webdriver") is False
        assert page.evaluate("'webdriver' in navigator") is True  # present, as in a normal browser
        ctx.close()


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

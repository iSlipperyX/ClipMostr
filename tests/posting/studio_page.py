"""A stand-in for TikTok Studio's upload page in a real Chromium tab (Playwright), for the uploader's tests.

`opened(tmp_path, monkeypatch)` gives make(privacy=True, html=None): a TikTokBrowser whose page shows the
stand-in (or `html`), logged in, without its waits. Skipped where there's no Chromium for Playwright; set
TOKMANAGER_TEST_CHROMIUM to point at one."""

import contextlib
import os

import pytest

from ytclip.posting.config import Settings
from ytclip.posting.tiktok import browser

STUDIO = """<!doctype html><html><body>
<input type="file" id="f">
<div id="form" hidden>
  <div class="public-DraftEditor-content" contenteditable="true">caption</div>
  {privacy}
  <button data-e2e="post_video_button" onclick="document.title='POSTED'">Post</button>
  <button id="discard" onclick="document.title='DISCARDED'">Discard</button>
</div>
<script>document.getElementById('f').onchange = () => document.getElementById('form').hidden = false;</script>
</body></html>"""
PRIVACY = """<div data-e2e="video_visibility_container"><button role="combobox">Everyone</button></div>"""


@contextlib.contextmanager
def opened(tmp_path, monkeypatch):
    """make(privacy=True, html=None): a TikTokBrowser whose page is a plain Chromium tab showing the stand-in."""
    pw_mod = pytest.importorskip("playwright.sync_api")
    pw = pw_mod.sync_playwright().start()
    chromium = None
    for opts in ({}, {"executable_path": os.environ.get("TOKMANAGER_TEST_CHROMIUM", "")}):
        if opts.get("executable_path", None) == "":
            continue
        try:
            chromium = pw.chromium.launch(**opts)
            break
        except Exception:
            pass
    if chromium is None:  # no browser installed (e.g. CI); set TOKMANAGER_TEST_CHROMIUM to point at one
        pw.stop()
        pytest.skip("no Chromium for Playwright")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(browser, "COVER_FIND", 1.0)  # a stand-in without a cover editor says so quickly

    def make(privacy=True, html=None):
        page_file = tmp_path / "studio.html"
        page_file.write_text(html or STUDIO.replace("{privacy}", PRIVACY if privacy else ""), encoding="utf-8")
        monkeypatch.setattr(browser, "UPLOAD_URL", page_file.as_uri())
        b = browser.TikTokBrowser(Settings(), tmp_path / "profile", log=lambda m: None)
        b.page = chromium.new_page()
        monkeypatch.setattr(b, "logged_in", lambda: True)
        monkeypatch.setattr(b, "_sleep", lambda s: None)
        return b

    try:
        yield make
    finally:
        chromium.close()
        pw.stop()


def clip(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"not really a video")
    return p

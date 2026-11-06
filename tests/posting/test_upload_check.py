"""The posting test: walks a stand-in for TikTok Studio's uploader and never clicks Post."""

import os
import time

import pytest

from ytclip.posting import jobs
from ytclip.posting.accounts import Account
from ytclip.posting.config import Settings
from ytclip.posting.store import Store, VideoItem
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


@pytest.fixture
def studio(tmp_path, monkeypatch):
    """A TikTokBrowser whose page is a plain Chromium tab showing a fake upload page."""
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

    def make(privacy=True):
        page_file = tmp_path / "studio.html"
        page_file.write_text(STUDIO.replace("{privacy}", PRIVACY if privacy else ""), encoding="utf-8")
        monkeypatch.setattr(browser, "UPLOAD_URL", page_file.as_uri())
        b = browser.TikTokBrowser(Settings(), tmp_path / "profile", log=lambda m: None)
        b.page = chromium.new_page()
        monkeypatch.setattr(b, "logged_in", lambda: True)
        monkeypatch.setattr(b, "_sleep", lambda s: None)
        return b

    yield make
    chromium.close()
    pw.stop()


def clip(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"not really a video")
    return p


def test_full_check_finds_everything_and_never_posts(studio, tmp_path):
    b = studio()
    titles = []
    b.page.on("framenavigated", lambda f: titles.append(b.page.title()) if f == b.page.main_frame else None)
    results = b.check_upload_page(clip(tmp_path))
    assert [(step, r) for step, r, _ in results] == [
        ("Logged in", "ok"), ("Upload page opens", "ok"), ("Video picker", "ok"),
        ("Caption box", "ok"), ("Who can watch", "ok"), ("Post button", "ok")]
    assert "POSTED" not in titles and b.page.url == "about:blank"


def test_missing_privacy_picker_is_only_a_warning(studio, tmp_path):
    results = studio(privacy=False).check_upload_page(clip(tmp_path))
    assert ("Who can watch", "warn") in [(s, r) for s, r, _ in results]
    assert results[-1][:2] == ("Post button", "ok")


def test_quick_check_stops_at_the_file_picker(studio):
    results = studio().check_upload_page(None)
    assert [s for s, _, _ in results] == ["Logged in", "Upload page opens", "Video picker"]


def test_not_logged_in_fails_first(studio, tmp_path, monkeypatch):
    b = studio()
    monkeypatch.setattr(b, "logged_in", lambda: False)
    monkeypatch.setattr(b, "screenshot", lambda name: None)
    (step, r, detail), = b.check_upload_page(clip(tmp_path))
    assert (step, r) == ("Logged in", "fail") and "Log in" in detail


# --- when the daily quick check runs ------------------------------------------------


def test_daily_check_only_for_autopilot_accounts_and_not_just_before_a_post(tmp_path):
    now = time.time()
    s = Settings()
    on = Account(autopilot=True, logged_in=True)
    off = Account(autopilot=False, logged_in=True)
    store = Store(tmp_path / "lib.json")
    assert jobs.upload_check_due(store, [off, on], s, now) is on
    on.upload_check_at = now - 3600
    assert jobs.upload_check_due(store, [off, on], s, now) is None  # checked an hour ago
    on.upload_check_at = 0
    soon = VideoItem(path="C:/x/a.mp4", status="queued", account=on.id, queue_pos=0, planned_at=now + 600)
    store.items[soon.id] = soon
    assert jobs.upload_check_due(store, [on], s, now) is None  # a post is due in 10 minutes
    logged_out = Account(autopilot=True, logged_in=False)  # nothing to check without a login
    assert jobs.upload_check_due(Store(tmp_path / "other.json"), [logged_out], s, now) is None

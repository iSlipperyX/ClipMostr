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

    def make(privacy=True, html=None):
        page_file = tmp_path / "studio.html"
        page_file.write_text(html or STUDIO.replace("{privacy}", PRIVACY if privacy else ""), encoding="utf-8")
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


# --- a big file: the wait follows TikTok's own upload progress ----------------------------------------

UPLOADING = """<!doctype html><html><body>
<input type="file" id="f">
<div id="form" hidden>
  <div class="public-DraftEditor-content" contenteditable="true"></div>
  <div data-e2e="video_visibility_container"><button role="combobox">Everyone</button></div>
  <div id="state">Uploading 0%</div>
  <button data-e2e="post_video_button" disabled
          onclick="document.body.insertAdjacentHTML('beforeend', '<p>Your video has been posted</p>')">Post</button>
</div>
<script>
document.getElementById('f').onchange = () => {
  document.getElementById('form').hidden = false;
  let pct = 0;
  const tick = setInterval(() => {
    if (pct >= STOP_AT) { clearInterval(tick); return; }
    pct += 10;
    document.getElementById('state').textContent = pct >= 100 ? 'Uploaded (101 MB)' : 'Uploading ' + pct + '%';
    if (pct >= 100) { document.querySelector('[data-e2e=post_video_button]').disabled = false; clearInterval(tick); }
  }, 400);
};
</script>
</body></html>"""


@pytest.mark.parametrize("body, pct, done", [
    ("<div>Uploading 45%</div>", 45.0, False),
    ("<div>45% uploaded</div>", 45.0, False),  # "uploaded" with a percentage under 100 is still going
    ("<div>Uploaded (101 MB)</div>", None, True),
    ("<div>Upload complete</div>", None, True),
    ("<div role='progressbar' aria-valuenow='30' aria-valuemax='60'></div><p>video.mp4</p>", 50.0, False),
    ("<div>100% uploaded</div>", 100.0, True),
    ("<div>Caption</div><div>Who can watch</div>", None, False),
])
def test_what_the_page_says_of_the_upload(studio, body, pct, done):
    b = studio(html=f"<!doctype html><html><body>{body}</body></html>")
    b.page.goto(browser.UPLOAD_URL)
    assert b._upload_state() == {"pct": pct, "done": done}


def test_a_big_upload_is_waited_for_as_long_as_it_moves(studio, tmp_path, monkeypatch):
    from ytclip.posting.tiktok import upload_wait

    monkeypatch.setattr(upload_wait, "BIG_FILE", 10)  # a few bytes count as big here
    b = studio(html=UPLOADING.replace("STOP_AT", "100"))
    said = []
    b.upload(clip(tmp_path), "A caption", "public", progress=said.append)
    assert "Your video has been posted" in b.page.inner_text("body")
    reached = [int(m.split(": ")[1].rstrip("%…")) for m in said if m.startswith("Uploading to TikTok:")]
    assert len(reached) >= 2 and reached == sorted(set(reached))  # each ten percent once, in order


def test_an_upload_that_stops_moving_is_given_up_with_the_reason(studio, tmp_path, monkeypatch):
    from ytclip.posting.tiktok import upload_wait

    monkeypatch.setattr(upload_wait, "BIG_FILE", 10)
    monkeypatch.setattr(upload_wait, "STALL_SECONDS", 1.5)
    b = studio(html=UPLOADING.replace("STOP_AT", "40"))
    monkeypatch.setattr(b, "screenshot", lambda name: None)
    with pytest.raises(browser.BrowserError, match="stopped at 40%"):
        b.upload(clip(tmp_path), "A caption", "public")
    assert "posted" not in b.page.inner_text("body")

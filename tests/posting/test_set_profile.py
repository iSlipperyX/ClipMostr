"""Applying a profile: fills in a stand-in for TikTok's Edit profile box (built like the real one,
from a read-only look at it on 27 Oct 2026) and checks what was saved."""

import json
import os

import pytest

from ytclip.posting.config import Settings
from ytclip.posting.tiktok import browser

PAGE = """<!doctype html><html><body>
<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{data}</script>
<button data-e2e="edit-profile-entrance" onclick="document.getElementById('box').hidden=false">Edit profile</button>
<div id="box" role="dialog" aria-label="Edit profile" data-e2e="edit-profile-popup" hidden>
  <div data-e2e="edit-profile-avatar"><input type="file" id="file" accept=".jpg,.png"></div>
  <div data-e2e="edit-profile-name-input">{name_field}</div>
  <textarea data-e2e="edit-profile-bio-input" placeholder="Bio"></textarea>
  <button data-e2e="edit-profile-cancel" onclick="document.getElementById('box').hidden=true">Cancel</button>
  <button data-e2e="edit-profile-save" disabled>Save</button>
</div>
<section id="crop" role="dialog" aria-modal="true" hidden>
  <div>Edit photo</div><span>Zoom</span>
  <button onclick="document.getElementById('crop').hidden=true">Cancel</button>
  <button id="apply">Apply</button>
</section>
<script>
  const box = document.getElementById('box'), save = box.querySelector('[data-e2e="edit-profile-save"]');
  const changes = {};
  const changed = () => { save.disabled = false; };
  const name = box.querySelector('[data-e2e="edit-profile-name-input"] input');
  if (name) name.addEventListener('input', () => { changes.nickname = name.value; changed(); });
  const bio = box.querySelector('textarea');
  bio.addEventListener('input', () => { changes.signature = bio.value; changed(); });
  document.getElementById('file').onchange = () => { document.getElementById('crop').hidden = false; };
  document.getElementById('apply').onclick = () => {
    document.getElementById('crop').hidden = true; changes.photo = true; changed(); };
  save.onclick = async () => {
    await fetch('/api/update/profile/', {method: 'POST', body: JSON.stringify(changes)});
    box.hidden = true;
  };
</script></body></html>"""
NAME_INPUT = '<input placeholder="Name" value="{nickname}">'
NAME_LOCKED = ('<div class="DivReadOnlyText">{nickname}</div><p>Your nickname can only be changed every 7 days. '
               'You can change it again after Nov 2, 2026.</p>')


class FakeTikTok:
    def __init__(self, name_locked=False, refuse_bio=False):
        self.user = {"uniqueId": "tester", "nickname": "Old name", "signature": "", "avatarLarger": "a1.jpg"}
        self.name_locked, self.refuse_bio = name_locked, refuse_bio
        self.saves = []

    def handle(self, route):
        url = route.request.url
        if "/api/update/profile/" in url:
            changes = json.loads(route.request.post_data or "{}")
            self.saves.append(changes)
            if "nickname" in changes:
                self.user["nickname"] = changes["nickname"]
            if "signature" in changes and not self.refuse_bio:
                self.user["signature"] = changes["signature"]
            if changes.get("photo"):
                self.user["avatarLarger"] = "a2.jpg"
            return route.fulfill(status=200, body="{}", content_type="application/json")
        scope = {"webapp.app-context": {"user": {"uniqueId": "tester"}},
                 "webapp.user-detail": {"statusCode": 0, "userInfo": {"user": dict(self.user)}}}
        field = (NAME_LOCKED if self.name_locked else NAME_INPUT).format(nickname=self.user["nickname"])
        html = PAGE.replace("{data}", json.dumps({"__DEFAULT_SCOPE__": scope})).replace("{name_field}", field)
        route.fulfill(status=200, body=html, content_type="text/html")


@pytest.fixture
def tiktok(tmp_path, monkeypatch):
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
    if chromium is None:
        pw.stop()
        pytest.skip("no Chromium for Playwright")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    photo = tmp_path / "me.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")

    def make(**kw):
        fake = FakeTikTok(**kw)
        b = browser.TikTokBrowser(Settings(), tmp_path / "profile", log=lambda m: None)
        b.page = chromium.new_page()
        b.page.route("https://www.tiktok.com/**", fake.handle)
        monkeypatch.setattr(b, "_sleep", lambda s: b.page.wait_for_timeout(50))
        return b, fake, photo

    yield make
    chromium.close()
    pw.stop()


def test_sets_photo_name_and_bio_and_checks_them(tiktok):
    b, fake, photo = tiktok()
    changed, note = b.set_profile("New name", "Daily podcast clips", photo, expected_user="tester")
    assert changed == ["photo", "name", "bio"] and note == ""
    assert fake.user["nickname"] == "New name" and fake.user["signature"] == "Daily podcast clips"
    assert fake.user["avatarLarger"] == "a2.jpg" and len(fake.saves) == 1


def test_nothing_to_do_when_it_already_matches(tiktok):
    b, fake, _ = tiktok()
    assert b.set_profile("Old name", "", None, expected_user="tester") == ([], "")
    assert fake.saves == []


def test_locked_name_saves_the_rest_and_says_why(tiktok):
    b, fake, _ = tiktok(name_locked=True)
    changed, note = b.set_profile("New name", "Daily podcast clips", None, expected_user="tester")
    assert changed == ["bio"] and fake.user["signature"] == "Daily podcast clips"
    assert "every 7 days" in note and "Nov 2, 2026" in note and fake.user["nickname"] == "Old name"


def test_only_a_locked_name_saves_nothing(tiktok):
    b, fake, _ = tiktok(name_locked=True)
    changed, note = b.set_profile("New name", "", None, expected_user="tester")
    assert changed == [] and "7 days" in note and fake.saves == []


def test_a_bio_tiktok_did_not_take_is_an_error(tiktok):
    b, fake, _ = tiktok(refuse_bio=True)
    with pytest.raises(browser.BrowserError, match="didn't take the new bio"):
        b.set_profile("", "Daily podcast clips", None, expected_user="tester")


def test_wrong_account_is_refused_before_touching_anything(tiktok):
    b, fake, photo = tiktok()
    with pytest.raises(browser.BrowserError, match="as @tester, not @someoneelse"):
        b.set_profile("New name", "x", photo, expected_user="someoneelse")
    assert fake.saves == []

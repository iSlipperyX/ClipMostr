"""Each posting account's TikTok sign-in lives in a browser profile of its own, and stays there: where the
profile may be, two accounts never sharing one, deleting one never reaching past posting's folder, the browser
always letting go of the profile, and nothing injected into TikTok's pages. (The sign-in check every few hours,
and what an expired one does, are in tests/posting/test_worker.py.)"""

import json
import sys
import types

import pytest

from ytclip.posting import accounts as A
from ytclip.posting.accounts import Account, Accounts
from ytclip.posting.config import Settings, cache_dir
from ytclip.posting.tiktok import browser


# ---------------------------------------------------------------- where a profile may be


def test_a_new_account_gets_its_own_profile_inside_postings_folder():
    a, b = Account(), Account()
    assert a.browser_profile == f"browser-profiles/{a.id}" and a.profile_path() != b.profile_path()
    assert a.profile_path().is_relative_to(cache_dir()) and a.profile_path().parent.name == A.PROFILES
    assert a.browser_dir().is_dir()


@pytest.mark.parametrize("saved", [
    "", "   ", ".", "/etc", "\\\\server\\share\\p", "C:\\Users\\me\\AppData", "C:relative", "../outside",
    "browser-profiles/../../outside", "browser-profiles/..", "a/../..",
])
def test_a_profile_saved_outside_postings_folder_is_taken_as_the_usual_one(saved):
    a = Account(id="acc1")
    a.browser_profile = saved
    assert a.profile_path() == cache_dir() / A.PROFILES / "acc1"


@pytest.mark.parametrize("saved, want", [
    ("browser-profiles/acc1", ("browser-profiles", "acc1")),
    ("browser-profiles\\old name", ("browser-profiles", "old name")),  # a Windows-style entry still works
    ("profiles/./main", ("profiles", "main")),
])
def test_a_profile_inside_postings_folder_is_kept(saved, want):
    a = Account(id="acc1", browser_profile=saved)
    assert a.profile_path() == cache_dir().joinpath(*want)


def test_a_link_out_of_postings_folder_is_not_followed(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = cache_dir() / "sneaky"
    cache_dir().mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links aren't allowed here")
    a = Account(id="acc1", browser_profile="sneaky")
    assert a.profile_path() == cache_dir() / A.PROFILES / "acc1"


# ---------------------------------------------------------------- one profile, one account


def write_accounts(path, entries):
    path.write_text(json.dumps({"version": 2, "accounts": entries}), encoding="utf-8")


def test_two_accounts_pointed_at_one_profile_are_parted(tmp_path):
    path = tmp_path / "accounts.json"
    write_accounts(path, [
        {"id": "a1", "username": "first", "browser_profile": "browser-profiles/a1", "logged_in": True},
        {"id": "a2", "username": "copy", "browser_profile": "browser-profiles/a1", "logged_in": True},
    ])
    accs = Accounts.load(path)
    first, copy = accs.get("a1"), accs.get("a2")
    assert first.logged_in and first.browser_profile == "browser-profiles/a1"
    assert copy.browser_profile == "browser-profiles/a2" and not copy.logged_in
    assert copy.session_check == "expired" and "shared a browser profile" in copy.session_detail
    accs.save()
    again = Accounts.load(path)
    assert again.get("a2").browser_profile == "browser-profiles/a2" and not again.get("a2").logged_in


def test_the_account_whose_profile_it_is_keeps_it(tmp_path):
    """A copied entry listed first can't push the profile's own account out of it."""
    path = tmp_path / "accounts.json"
    write_accounts(path, [
        {"id": "copy", "browser_profile": "browser-profiles/owner", "logged_in": True},
        {"id": "owner", "browser_profile": "browser-profiles/owner", "logged_in": True},
    ])
    accs = Accounts.load(path)
    assert accs.get("owner").logged_in and accs.get("owner").browser_profile == "browser-profiles/owner"
    assert accs.get("copy").browser_profile == "browser-profiles/copy" and not accs.get("copy").logged_in
    assert accs.ids() == ["copy", "owner"]  # the list keeps its order


def test_profiles_that_differ_only_in_case_are_one_folder_on_windows_and_macs():
    a = Account(id="a1", browser_profile="browser-profiles/Main", logged_in=True)
    b = Account(id="b2", browser_profile="browser-profiles/main", logged_in=True)
    moved = A._own_profiles([a, b])
    assert moved == [b] and b.browser_profile == "browser-profiles/b2" and a.logged_in


def test_accounts_in_their_own_profiles_are_left_alone():
    accs = [Account(logged_in=True) for _ in range(4)]
    assert A._own_profiles(accs) == [] and all(a.logged_in for a in accs)


def test_a_bad_profile_saved_before_comes_back_as_the_usual_one(tmp_path):
    path = tmp_path / "accounts.json"
    write_accounts(path, [{"id": "a1", "browser_profile": "C:\\Users\\me\\Desktop", "logged_in": True}])
    a = Accounts.load(path).get("a1")
    assert a.profile_path() == cache_dir() / A.PROFILES / "a1"


# ---------------------------------------------------------------- deleting a login


def test_deleting_a_login_removes_only_that_profile():
    a, b = Account(), Account()
    for acc in (a, b):
        (acc.browser_dir() / "Default").mkdir(parents=True)
        (acc.browser_dir() / "Default" / "Cookies").write_text("x")
    Accounts.delete_login(a)
    assert not a.profile_path().exists() and (b.profile_path() / "Default" / "Cookies").exists()
    assert cache_dir().is_dir()


@pytest.mark.parametrize("saved", ["", ".", "..", "/", "browser-profiles/..", "C:\\"])
def test_deleting_a_login_never_reaches_postings_folder_or_past_it(tmp_path, saved):
    keep = cache_dir() / "thumbnails" / "t.jpg"
    keep.parent.mkdir(parents=True)
    keep.write_text("x")
    outside = tmp_path / "precious.txt"
    outside.write_text("x")
    a = Account(id="gone")
    a.browser_profile = saved
    Accounts.delete_login(a)
    assert keep.exists() and outside.exists()


# ---------------------------------------------------------------- the browser lets go of the profile


class FakeContext:
    def __init__(self, fail_on=""):
        self.fail_on = fail_on
        self.closed = False
        self.pages = []
        self.scripts = []

    def set_default_timeout(self, ms):
        if self.fail_on == "timeout":
            raise RuntimeError("the window went away")

    def new_page(self):
        if self.fail_on == "page":
            raise RuntimeError("Target closed")
        page = types.SimpleNamespace(on=lambda *a: None)
        self.pages.append(page)
        return page

    def add_init_script(self, script):
        self.scripts.append(script)

    def close(self):
        if self.fail_on == "close":
            raise RuntimeError("already closed")
        self.closed = True


class FakePlaywright:
    def __init__(self, ctx=None, launch_error=None):
        self.ctx, self.launch_error = ctx or FakeContext(), launch_error
        self.stopped = 0
        self.launched: list[tuple] = []
        self.chromium = types.SimpleNamespace(launch_persistent_context=self.launch)

    def launch(self, profile, **opts):
        self.launched.append((profile, opts))
        if self.launch_error:
            raise self.launch_error
        return self.ctx

    def start(self):
        return self

    def stop(self):
        self.stopped += 1


@pytest.fixture
def fake_pw(monkeypatch):
    made = {}

    def install(**kw):
        pw = FakePlaywright(**kw)
        module = types.ModuleType("playwright.sync_api")
        module.sync_playwright = lambda: pw
        monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
        monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
        made["pw"] = pw
        return pw

    return install


def tiktok(account):
    logs = []
    return browser.TikTokBrowser(Settings(), account.browser_dir(), log=logs.append), logs


def test_the_browser_opens_in_the_accounts_own_profile_and_closes(fake_pw):
    pw = fake_pw()
    a = Account()
    b, _ = tiktok(a)
    with b:
        assert b.page is not None
    assert pw.launched[0][0] == str(a.profile_path())
    assert pw.ctx.closed and pw.stopped == 1 and b.ctx is None and b._pw is None


def test_nothing_is_injected_into_tiktoks_pages(fake_pw):
    pw = fake_pw()
    b, _ = tiktok(Account())
    with b:
        pass
    assert pw.ctx.scripts == []
    assert not hasattr(browser, "WEBDRIVER_SCRIPT")


@pytest.mark.parametrize("fail_on", ["timeout", "page"])
def test_a_browser_that_fails_while_opening_still_closes(fake_pw, fail_on):
    pw = fake_pw(ctx=FakeContext(fail_on))
    b, _ = tiktok(Account())
    with pytest.raises(RuntimeError):
        with b:
            pytest.fail("never reached")
    assert pw.ctx.closed and pw.stopped == 1


def test_a_browser_that_wont_launch_stops_playwright_and_says_why(fake_pw):
    pw = fake_pw(launch_error=RuntimeError("ProcessSingleton: the user data directory is already in use"))
    b, _ = tiktok(Account())
    with pytest.raises(browser.BrowserError, match="already open"):
        with b:
            pass
    assert pw.stopped == 1


def test_a_window_already_gone_still_stops_playwright(fake_pw):
    pw = fake_pw(ctx=FakeContext("close"))
    b, logs = tiktok(Account())
    with b:
        pass
    assert pw.stopped == 1 and any("didn't close cleanly" in m for m in logs)
    b.__exit__(None, None, None)  # twice is harmless
    assert pw.stopped == 1


def test_a_failure_inside_the_with_closes_the_window(fake_pw):
    pw = fake_pw()
    b, _ = tiktok(Account())
    with pytest.raises(KeyError):
        with b:
            raise KeyError("posting failed")
    assert pw.ctx.closed and pw.stopped == 1

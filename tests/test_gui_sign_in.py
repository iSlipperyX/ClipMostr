import time

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)  # no Qt or no display libraries

from ytclip import pipeline, youtube_login  # noqa: E402
from ytclip.jobs import SIGN_IN_WAIT, Job, JobDB  # noqa: E402
from ytclip.settings import Settings  # noqa: E402
from test_youtube_login import YEAR, _cookies  # noqa: E402


class FakeFirefox:
    """Stands in for Firefox: open or closed, signed in or not."""

    def __init__(self, monkeypatch, installed=True):
        self.opened = 0
        self.is_open = False
        self.installed = installed
        monkeypatch.setattr(youtube_login, "open_sign_in", self.open)
        monkeypatch.setattr(youtube_login, "firefox_open", lambda folder=None: self.is_open)
        monkeypatch.setattr(youtube_login, "find_firefox", lambda: "firefox" if self.installed else None)

    def open(self):
        if not self.installed:
            raise FileNotFoundError("Firefox isn't installed")
        self.opened += 1
        self.is_open = True

    def sign_in(self):
        _cookies(youtube_login.profile_dir(), [(".youtube.com", "LOGIN_INFO", time.time() + YEAR)]).close()


def test_the_window_opens_firefox_and_follows_the_sign_in(app, monkeypatch):
    from ytclip.gui.sign_in_dialog import SignInDialog

    ff = FakeFirefox(monkeypatch)
    dlg = SignInDialog()
    done = []
    dlg.signed_in.connect(lambda: done.append(1))
    app.processEvents()  # it opens Firefox straight away
    assert ff.opened == 1
    dlg.poll()
    assert "Waiting for you to sign in" in dlg.status.text() and not dlg.open_btn.isEnabled()
    ff.sign_in()
    dlg.poll()
    assert "close that Firefox window" in dlg.status.text() and not done
    ff.is_open = False
    dlg.poll()
    assert "Signed in to YouTube" in dlg.status.text() and done == [1]
    assert dlg.close_btn.text() == "Done" and dlg.open_btn.text() == "Open Firefox again"
    dlg.poll()
    assert done == [1]  # once
    dlg.accept()
    assert done == [1]


def test_closing_firefox_before_signing_in(app, monkeypatch):
    from ytclip.gui.sign_in_dialog import SignInDialog

    ff = FakeFirefox(monkeypatch)
    dlg = SignInDialog()
    app.processEvents()
    dlg.poll()
    ff.is_open = False
    dlg.poll()
    assert "closed before YouTube was signed in" in dlg.status.text() and dlg.open_btn.isEnabled()
    dlg.reject()


def test_no_firefox_offers_to_get_it(app, monkeypatch):
    from ytclip.gui.sign_in_dialog import SignInDialog

    FakeFirefox(monkeypatch, installed=False)
    dlg = SignInDialog()
    app.processEvents()
    assert "isn't installed" in dlg.status.text() and not dlg.get_btn.isHidden()
    dlg.reject()


def test_closing_the_window_while_signed_in_still_counts(app, monkeypatch):
    from ytclip.gui.sign_in_dialog import SignInDialog

    ff = FakeFirefox(monkeypatch)
    dlg = SignInDialog()
    done = []
    dlg.signed_in.connect(lambda: done.append(1))
    app.processEvents()
    ff.sign_in()
    dlg.reject()  # Firefox is still open
    assert done == [1]


def test_automation_signs_in_and_carries_on(app, tmp_path, monkeypatch):
    from ytclip.gui import automation

    ff = FakeFirefox(monkeypatch)
    db = JobDB(tmp_path / "jobs.db")
    db.add(Job("a", "https://youtu.be/a", status="queued", stage=SIGN_IN_WAIT, next_try=time.time() + 3600),
           time.time())
    pipeline.record_block(db, "Sign in to confirm you're not a bot.", time.time())
    state = {"s": Settings()}
    changes = []

    def change(name, value):
        changes.append((name, value))
        state["s"] = state["s"].apply({name: value})

    tab = automation.Automation(lambda: state["s"], db, launch=lambda: None, change=change)
    assert "Sign in to YouTube" in tab.sign_in_btn.text() and tab.sign_in_btn.objectName() == "primary"
    notices = []
    tab.notice.connect(notices.append)

    def fake_exec(dlg):
        ff.sign_in()
        ff.is_open = False
        dlg.poll()
        dlg.accept()
        return 1

    monkeypatch.setattr(automation.SignInDialog, "exec", fake_exec)
    tab.sign_in()
    assert changes == [("cookies_browser", "clipmonster")]
    tab.end_pause_after_sign_in()  # what the timer does a moment later
    assert not pipeline.paused_until(db, time.time())
    assert any("1 waiting video carries on" in n for n in notices)
    assert tab.sign_in_btn.text().endswith("Signed in to YouTube")
    tab.shutdown()


def test_settings_has_a_sign_in_button(app, monkeypatch):
    from PySide6.QtWidgets import QPushButton

    from ytclip.gui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(Settings(), tab="Download")
    assert any("Sign in to YouTube" in b.text() for b in dlg.findChildren(QPushButton))
    dlg._use_sign_in(True)
    assert dlg.signed_in and dlg.result_settings.cookies_browser == "clipmonster"
    assert dlg._editors["cookies_browser"].get() == "clipmonster"
    dlg._use_sign_in(False)
    assert dlg.result_settings.cookies_browser == ""
    dlg.result_settings = dlg.result_settings.apply({"cookies_browser": "chrome"})
    dlg._use_sign_in(False)  # forgetting ClipMonster's sign-in leaves another browser's alone
    assert dlg.result_settings.cookies_browser == "chrome"
    dlg.reject()

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)  # no Qt or no display libraries

from ytclip import categories  # noqa: E402
from ytclip.posting.accounts import Accounts  # noqa: E402
from ytclip.posting.config import Settings as PostingSettings  # noqa: E402
from ytclip.settings import Settings  # noqa: E402


class Host:
    def __init__(self):
        self.settings = Settings()
        self.changes = []

    def change(self, name, value):
        self.changes.append((name, value))
        self.settings = self.settings.apply({name: value})


def _wizard(app, monkeypatch):
    from ytclip.gui import setup_wizard
    from ytclip.gui.posting_state import PostingState

    monkeypatch.setattr(setup_wizard, "tools", lambda: [("ffmpeg", True, "Built in."), ("Node.js", False, "Get it.")])
    host = Host()
    state = PostingState(worker_alive=lambda: True, start_timer=False)  # never starts a worker of its own
    wiz = setup_wizard.SetupWizard(host.settings, host.change, state)
    return wiz, host, state


def _to(wiz, step):
    from ytclip.gui.setup_wizard import STEPS

    while wiz.pages.currentIndex() < STEPS.index(step):
        wiz.next.click()


def test_tools_page_says_what_is_missing(app, monkeypatch):
    wiz, _, _ = _wizard(app, monkeypatch)
    text = wiz.tool_rows.text()
    assert "ffmpeg" in text and "Node.js" in text and "✗" in text and "✓" in text
    assert not wiz.back.isEnabled() and wiz.next.text() == "Next"


def test_keys_are_saved_when_the_page_is_left(app, monkeypatch):
    wiz, host, _ = _wizard(app, monkeypatch)
    _to(wiz, "Keys")
    wiz.provider.setCurrentIndex(wiz.provider.findData("gemini"))
    wiz.gemini_key.setText("  AIza-test ")
    wiz.youtube_key.setText("AIza-yt")
    wiz.next.click()
    assert host.settings.gemini_api_key == "AIza-test" and host.settings.youtube_api_key == "AIza-yt"
    assert host.settings.ai_provider == "gemini"
    assert ("ai_api_key", "") not in host.changes  # an untouched box isn't written


def test_first_category_posts_to_the_account_added(app, monkeypatch):
    wiz, host, state = _wizard(app, monkeypatch)
    a = state.accounts.add("Gaming", state.settings)
    a.username = "gamingclips"
    state.save()
    _to(wiz, "First category")
    assert wiz.category_account.currentData() == a.id  # the one account is picked for you
    wiz.category_name.setText("Gaming")
    wiz.category_finds.setPlainText("Warzone clutch plays")
    wiz.category_daily.setValue(3)
    wiz.min_len.setValue(95)  # the wrong way round: sorted
    wiz.max_len.setValue(70)
    wiz.next.click()
    [n] = categories.load(host.settings)
    assert (n.name, n.account_id, n.account, n.finds, n.daily) == ("Gaming", a.id, "@gamingclips",
                                                                   "Warzone clutch plays", 3)
    assert (n.min_len, n.max_len) == (70, 95)


def test_schedule_applies_to_every_account(app, monkeypatch):
    wiz, _, state = _wizard(app, monkeypatch)
    state.accounts.add("One", state.settings)
    state.accounts.add("Two", state.settings)
    state.save()
    _to(wiz, "Posting schedule")
    wiz.per_day.setValue(3)
    wiz.warm.setChecked(True)
    wiz.next.click()
    assert PostingSettings.load().posts_per_day == 3
    saved = Accounts.load()
    assert [a.posts_per_day for a in saved] == [3, 3] and all(a.warm_up_start for a in saved)


def test_finish_marks_setup_done(app, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from ytclip import pipeline

    wiz, host, _ = _wizard(app, monkeypatch)
    asked = []
    monkeypatch.setattr(pipeline, "set_start_with_windows", lambda on: asked.append(on) or "")
    _to(wiz, "Start")
    assert wiz.next.text() == "Finish"
    wiz.next.click()
    assert host.settings.setup_done and asked == [True] and wiz.start_now
    assert wiz.result() == QDialog.Accepted and not wiz.problems


def test_finish_later_keeps_what_was_typed(app, monkeypatch):
    wiz, host, _ = _wizard(app, monkeypatch)
    _to(wiz, "Keys")
    wiz.claude_key.setText("sk-ant-x")
    wiz.later.click()
    assert host.settings.ai_api_key == "sk-ant-x" and not host.settings.setup_done

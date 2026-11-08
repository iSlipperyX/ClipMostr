"""The account wizard: link or create a TikTok account, the steps ticking off, and its categories."""

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from ytclip import categories  # noqa: E402
from ytclip.gui.accounts_view import AccountWizard, step_reached  # noqa: E402
from ytclip.settings import Settings  # noqa: E402


def test_the_steps_follow_the_workers_log():
    assert step_reached("", "new", "link") == 0
    log = "Log in to TikTok in the Edge window that just opened (the QR code works too)."
    assert step_reached(log, "running", "link") == 1
    log += "\nYou're logged in. Closing the Edge window…"
    assert step_reached(log, "running", "link") == 2
    log += "\nChecking the login…"
    assert step_reached(log, "running", "link") == 3
    assert step_reached(log, "done", "link") == 4


class Main:
    def __init__(self, **kw):
        self.settings = Settings().apply(kw)

    def get(self):
        return self.settings

    def change(self, name, value):
        self.settings = self.settings.apply({name: value})


def _state():
    from ytclip.gui.posting_state import PostingState

    return PostingState(worker_alive=lambda: True, start_timer=False)  # the "worker" is the test


def _worker_signs_in(state, username="newone"):
    """Stands in for the posting worker: takes the sign-in request and finishes it."""
    req = state.actions.claim(owner=1)
    assert req is not None and req.kind == "login"
    a = state.accounts.get(req.account)
    a.username, a.logged_in = username, True
    if req.args.get("mode") == "create":
        a.start_warm_up()
    state.accounts.save()
    state.actions.add_log(req.id, "Checking the login…")
    state.actions.end(req.id, True, f"Logged in as @{username}.")
    return req


def test_creating_an_account_gives_it_the_category_that_needed_one(app):
    main = Main(categories=[{"id": "g", "name": "Gaming"}, {"id": "p", "name": "Pods", "account_id": "other"}])
    state = _state()
    dlg = AccountWizard(state, main.get, main.change)
    dlg.create_btn.click()
    assert dlg.pages.currentIndex() == 1 and dlg.mode == "create" and len(state.accounts) == 1
    req = _worker_signs_in(state)
    assert req.args == {"mode": "create"}
    dlg._poll()
    assert dlg.ok and dlg.pages.currentIndex() == 2 and dlg.warm.isChecked()
    shown = [box.text() for box, _ in dlg.category_checks]
    assert shown == ["Gaming  (needs an account)"] and dlg.category_checks[0][0].isChecked()
    dlg._done()
    gaming, pods = categories.load(main.settings)
    a = next(iter(state.accounts))
    assert (gaming.account_id, gaming.account) == (a.id, "@newone") and pods.account_id == "other"
    assert a.warm_up_start


def test_cancelling_before_signing_in_leaves_no_empty_account(app):
    state = _state()
    dlg = AccountWizard(state)
    dlg.link_btn.click()
    assert len(state.accounts) == 1
    dlg.reject()
    assert len(state.accounts) == 0


def test_signing_in_again_keeps_the_account_and_starts_straight_away(app):
    state = _state()
    a = state.add_account()
    dlg = AccountWizard(state, account_id=a.id)
    assert dlg.pages.currentIndex() == 1 and "again" in dlg.steps_title.text().lower()
    req = state.actions.claim(owner=1)
    state.actions.end(req.id, False, "Login wasn't completed.")
    dlg._poll()
    assert not dlg.ok and dlg.retry_btn.isVisibleTo(dlg) and "Login wasn't completed" in dlg.where.text()
    dlg.reject()
    assert state.accounts.get(a.id) is not None  # not one this window added

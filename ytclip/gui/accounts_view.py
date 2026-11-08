"""The account wizard: add a TikTok account (one you have, or a new one) and keep it signed in.

1. Pick: Link an account I already have, or Create a new TikTok account.
2. A browser window opens on that account's own profile folder (%LOCALAPPDATA%\\ClipMonster\\posting\\
   browser-profiles\\<id>), at TikTok's login or sign-up page. You sign in there yourself (or fill in
   TikTok's own sign-up form); ClipMonster never sees or keeps your password. The steps tick off here
   as it goes, and the window closes by itself once TikTok's sign-in cookie appears.
3. The sign-in stays in that profile, so posting and stats use it from then on without signing in again;
   the posting worker checks it every 6 hours and says so if TikTok ever asks for it again.
4. Done: which categories post here, and (for a new account) a two-week warm-up.

The same window signs an existing account in again (`account_id`), starting at step 2.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from . import theme
from .posting_state import PostingState

STEPS = ("Open the account's own browser window",
         "Sign in to TikTok there",
         "ClipMonster sees you're in and closes the window",
         "Read the account's @name")
CREATE_STEPS = ("Open the account's own browser window",
                "Make the account on TikTok's sign-up page",
                "ClipMonster sees you're signed in and closes the window",
                "Read the new account's @name")


def step_reached(log: str, state: str, mode: str) -> int:
    """How many of the steps are done, from the worker's log of the sign-in (0 to 4)."""
    text = log.lower()
    if state == "done":
        return 4
    if "checking the login" in text:
        return 3
    if "closing the" in text or "you're logged in" in text:
        return 2
    if "window that just opened" in text or "window that opens" in text or "browser window" in text:
        return 1
    return 0


class _Choice(QPushButton):
    """A big button with a title and a line under it."""

    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setCheckable(False)
        self.setMinimumHeight(86)
        self.setObjectName("choice")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        head = QLabel(f"<b style='font-size:12pt'>{title}</b>")
        body = QLabel(text)
        body.setWordWrap(True)
        body.setProperty("muted", True)
        for w in (head, body):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
            lay.addWidget(w)


class AccountWizard(QDialog):
    """Add (or sign in again) one TikTok account. `account_id` after exec() is the account, if any."""

    finished_account = Signal(str)

    def __init__(self, state: PostingState, main_settings: Callable | None = None,
                 change: Callable[[str, object], None] | None = None, account_id: str = "",
                 category_id: str = "", parent=None):
        super().__init__(parent)
        self.state = state
        self._main = main_settings  # () -> ClipMonster's Settings, for the categories that post here
        self._change = change  # (setting, value) -> saves ClipMonster's settings
        self.account_id = account_id
        self.again = bool(account_id)  # signing an account in again, not adding one
        self.for_category = category_id  # opened from a category: it posts here once this is done
        self.made_here = False  # an account this window added (removed again if it's never signed in)
        self.mode = "link"
        self.rid = 0
        self.ok = False
        self.setWindowTitle("Sign in again" if self.again else "Add a TikTok account")
        self.setMinimumWidth(600)
        self.pages = QStackedWidget()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.addWidget(self.pages, 1)
        self.pages.addWidget(self._choose_page())
        self.pages.addWidget(self._steps_page())
        self.pages.addWidget(self._done_page())
        self.timer = QTimer(self, interval=600)
        self.timer.timeout.connect(self._poll)
        if self.again:
            self._start("link")

    # --- 1. which ----------------------------------------------------------------------------

    def _choose_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Add a TikTok account")
        title.setObjectName("title")
        lay.addWidget(title)
        intro = QLabel("Each account gets its own browser profile on this PC, so it stays signed in: posting and "
                       "stats use it from then on, and you only sign in once. ClipMonster never sees or keeps "
                       "your password.")
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        lay.addWidget(intro)
        lay.addSpacing(8)
        link = _Choice("Link an account I already have",
                       "Sign in to TikTok in a browser window (the QR code from the TikTok app is quickest).")
        link.clicked.connect(lambda: self._start("link"))
        create = _Choice("Create a new TikTok account",
                         "Opens TikTok's own sign-up page in that account's browser window. You fill it in; "
                         "the new account warms up for two weeks, posting less, so it doesn't look like spam.")
        create.clicked.connect(lambda: self._start("create"))
        self.link_btn, self.create_btn = link, create
        lay.addWidget(link)
        lay.addWidget(create)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        lay.addLayout(row)
        return page

    # --- 2. signing in ---------------------------------------------------------------------------

    def _steps_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        self.steps_title = QLabel()
        self.steps_title.setObjectName("title")
        lay.addWidget(self.steps_title)
        self.steps_box = QFrame(objectName="panel")
        sl = QVBoxLayout(self.steps_box)
        sl.setContentsMargins(14, 10, 14, 10)
        self.step_labels = [QLabel() for _ in STEPS]
        for label in self.step_labels:
            label.setTextFormat(Qt.RichText)
            sl.addWidget(label)
        self.steps_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)  # every step in full, never squeezed
        lay.addWidget(self.steps_box)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setProperty("muted", True)
        lay.addWidget(self.hint)
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumHeight(120)
        lay.addWidget(self.log, 1)
        self.where = QLabel()
        self.where.setWordWrap(True)
        lay.addWidget(self.where)
        row = QHBoxLayout()
        row.addStretch(1)
        self.signed_in_btn = QPushButton("I'm signed in")
        self.signed_in_btn.setObjectName("primary")
        self.signed_in_btn.setToolTip("Press once your TikTok feed shows in that window, if it hasn't closed by itself")
        self.signed_in_btn.clicked.connect(self._signed_in)
        self.retry_btn = QPushButton("Try again")
        self.retry_btn.clicked.connect(lambda: self._start(self.mode))
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        for b in (self.signed_in_btn, self.retry_btn, self.cancel_btn):
            row.addWidget(b)
        lay.addLayout(row)
        return page

    def _start(self, mode: str) -> None:
        self.mode = mode
        if not self.account_id:
            a = self.state.add_account()
            self.account_id, self.made_here = a.id, True
        a = self.state.accounts.get(self.account_id)
        self.steps_title.setText(("Sign " + a.handle + " in again") if self.again and a else
                                 "Create the account on TikTok" if mode == "create" else "Sign in to TikTok")
        self.hint.setText("A browser window opens on TikTok's sign-up page. Make the account there; once you're "
                          "signed in to it, that window closes by itself." if mode == "create" else
                          "A browser window opens on TikTok's login page. Sign in there (the QR code from the "
                          "TikTok app is quickest); it closes by itself once you're in.")
        self.rid = self.state.ask("login", account=self.account_id, mode=mode)
        self.log.clear()
        self.where.setText("")
        self.where.setStyleSheet("")
        self.signed_in_btn.setVisible(True)
        self.signed_in_btn.setEnabled(True)
        self.retry_btn.setVisible(False)
        self.cancel_btn.setText("Cancel")
        self._show_steps(0)
        self.pages.setCurrentIndex(1)
        self.adjustSize()  # the steps page is taller than the first one
        self.timer.start()
        self._poll()

    def _show_steps(self, reached: int, failed: bool = False) -> None:
        names = CREATE_STEPS if self.mode == "create" else STEPS
        for i, (label, name) in enumerate(zip(self.step_labels, names)):
            if i < reached:
                mark, colour = "✓", theme.GOOD
            elif i == reached and failed:
                mark, colour = "✖", theme.BAD
            elif i == reached:
                mark, colour = "●", theme.ACCENT_2
            else:
                mark, colour = "○", theme.MUTED
            weight = "600" if i == reached and not failed else "400"
            label.setText(f"<span style='color:{colour}'>{mark}</span>&nbsp;&nbsp;"
                          f"<span style='font-weight:{weight}'>{name}</span>")

    def _poll(self) -> None:
        req = self.state.actions.get(self.rid) if self.rid else None
        if req is None:
            return
        if req.log != self.log.toPlainText():
            self.log.setPlainText(req.log)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        if req.state == "new":
            self.where.setText("Waiting for automation's posting worker…" if self.state.worker_running() else
                               "Starting…")
        reached = step_reached(req.log, req.state, self.mode)
        if not req.done:
            self._show_steps(reached)
            return
        self.timer.stop()
        self.state.poll()
        if req.state == "done":
            self._show_steps(4)
            self.ok = True
            self._finish_page(req.result)
            return
        self._show_steps(reached, failed=True)
        self.where.setText(("✖ " + (req.result or "It didn't finish.")) if req.state != "cancelled" else "Stopped.")
        self.where.setStyleSheet(f"color: {theme.BAD}")
        self.signed_in_btn.setVisible(False)
        self.retry_btn.setVisible(True)
        self.cancel_btn.setText("Close")

    def _signed_in(self) -> None:
        if self.rid:
            self.state.finish(self.rid)
        self.signed_in_btn.setEnabled(False)
        self.where.setText("Checking the sign-in…")

    # --- 3. done ------------------------------------------------------------------------------

    def _done_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        self.done_title = QLabel()
        self.done_title.setObjectName("title")
        lay.addWidget(self.done_title)
        self.done_text = QLabel()
        self.done_text.setWordWrap(True)
        self.done_text.setProperty("muted", True)
        lay.addWidget(self.done_text)
        self.warm = QCheckBox("Warm it up for two weeks: post less while the account is new (recommended for an "
                              "account made this month)")
        lay.addWidget(self.warm)
        self.cats_head = QLabel("<b>Post these categories here</b>")
        lay.addWidget(self.cats_head)
        self.cats_box = QVBoxLayout()
        lay.addLayout(self.cats_box)
        self.category_checks: list[tuple[QCheckBox, str]] = []
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        done = QPushButton("Done")
        done.setObjectName("primary")
        done.setDefault(True)
        done.clicked.connect(self._done)
        row.addWidget(done)
        lay.addLayout(row)
        return page

    def _finish_page(self, result: str) -> None:
        a = self.state.accounts.get(self.account_id)
        handle = a.handle if a else "the account"
        self.done_title.setText(f"{handle} is signed in" if self.again else f"{handle} is added")
        self.done_text.setText((result + " " if result else "") + "It stays signed in on this PC; ClipMonster "
                               "checks every 6 hours and tells you if TikTok ever asks for the sign-in again.")
        new = self.mode == "create"
        self.warm.setVisible(not self.again)
        self.warm.setChecked(bool(a and a.warm_up_start) or new)
        self._fill_categories()
        self.pages.setCurrentIndex(2)
        self.adjustSize()

    def _fill_categories(self) -> None:
        while self.cats_box.count():
            w = self.cats_box.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.category_checks = []
        if self._main is None or self._change is None:
            self.cats_head.setVisible(False)
            return
        from .. import categories

        mine = categories.load(self._main())
        shown = [n for n in mine if not n.account_id or n.account_id == self.account_id or n.id == self.for_category]
        for n in shown:
            box = QCheckBox(n.name + ("" if n.account_id else "  (needs an account)"))
            box.setChecked(n.account_id == self.account_id or n.id == self.for_category or
                           (not n.account_id and len(shown) == 1))
            self.cats_box.addWidget(box)
            self.category_checks.append((box, n.id))
        self.cats_head.setVisible(bool(shown))

    def _done(self) -> None:
        a = self.state.accounts.get(self.account_id)
        if a is not None and not self.again:
            if self.warm.isChecked() and not a.warm_up_start:
                a.start_warm_up()
            elif not self.warm.isChecked():
                a.warm_up_start = 0.0
            self.state.save()
        if a is not None and self.category_checks and self._main is not None and self._change is not None:
            from .. import categories

            chosen = {cid for box, cid in self.category_checks if box.isChecked()}
            out = []
            for n in categories.load(self._main()):
                if n.id in chosen:
                    n = replace(n, account_id=a.id, account=a.handle)
                elif n.account_id == a.id and any(cid == n.id for _b, cid in self.category_checks):
                    n = replace(n, account_id="", account="")  # unticked here: it needs another account
                out.append(n)
            self._change("categories", categories.to_settings(out))
        self.finished_account.emit(self.account_id)
        self.accept()

    # --- closing --------------------------------------------------------------------------------

    def reject(self) -> None:  # Cancel, Esc, the window's X
        req = self.state.actions.get(self.rid) if self.rid else None
        if req is not None and not req.done:
            self.state.cancel(self.rid)
        self.timer.stop()
        a = self.state.accounts.get(self.account_id) if self.account_id else None
        if self.made_here and a is not None and not a.logged_in and not self.ok:
            self.state.remove_account(a.id)  # never signed in: no empty account left behind
            self.account_id = ""
        super().reject()

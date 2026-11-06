"""The posting tabs: Accounts (logins, schedules, profiles), Queue (what posts when, and what went up) and
Performance (each account's numbers and feed).

Nothing here talks to TikTok: the buttons ask the posting worker (PostingState.ask), which runs in
automation's background process, and the pages show what it saved.
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem, QTimeEdit, QVBoxLayout, QWidget)

from ..posting import intake, jobs, live, profile, records, results, schedule
from ..posting.accounts import Account
from ..posting.store import INTERRUPTED, VideoItem
from . import theme
from .posting_state import PostingState

PRIVACY = (("public", "Everyone"), ("friends", "Friends"), ("private", "Only me"))


def _muted(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setProperty("muted", True)
    label.setWordWrap(True)
    return label


def _h2(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("h2")
    return label


def _open(path_or_url: str) -> None:
    if not path_or_url:
        return
    if path_or_url.startswith("http"):
        QDesktopServices.openUrl(QUrl(path_or_url))
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path_or_url))


def account_status(state: PostingState, a: Account, now: float | None = None) -> str:
    """One line on how the account is doing: login, posting, the next post."""
    now = time.time() if now is None else now
    if not a.logged_in:
        return "Not logged in to TikTok: nothing posts until it is."
    parts = [f"Logged in as {a.handle}"]
    if not a.autopilot:
        parts.append("posting paused")
    else:
        nxt = jobs.next_post(state.store, a.id)
        parts.append(f"next post {schedule.fmt_when(nxt.planned_at, now)}" if nxt and nxt.planned_at else
                     "nothing waiting to post")
    return " · ".join(parts)


# =====================================================================================================
# Log in


class RequestDialog(QDialog):
    """Follows one request to the worker (log in, test posting…): its log, Cancel, and for a login the
    "I'm logged in" button."""

    def __init__(self, state: PostingState, request_id: int, title: str, intro: str, login: bool = False,
                 parent=None):
        super().__init__(parent)
        self.state, self.rid = state, request_id
        self.setWindowTitle(title)
        self.resize(560, 360)
        self.ok = False
        self.result = ""
        lay = QVBoxLayout(self)
        self.intro = QLabel(intro)
        self.intro.setWordWrap(True)
        lay.addWidget(self.intro)
        self.log = QPlainTextEdit(readOnly=True)
        lay.addWidget(self.log, 1)
        self.where = _muted()
        lay.addWidget(self.where)
        row = QHBoxLayout()
        row.addStretch(1)
        self.done_btn = QPushButton("I'm logged in")
        self.done_btn.setObjectName("primary")
        self.done_btn.setToolTip("Press once your TikTok feed shows in the browser window. It closes and the "
                                 "login is checked")
        self.done_btn.clicked.connect(lambda: (state.finish(request_id), self.done_btn.setEnabled(False)))
        self.done_btn.setVisible(login)
        row.addWidget(self.done_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        row.addWidget(self.cancel_btn)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        self.close_btn.setVisible(False)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        self.timer = QTimer(self, interval=700)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self._poll()

    def _poll(self) -> None:
        req = self.state.actions.get(self.rid)
        if req is None:
            return
        if req.log != self.log.toPlainText():
            self.log.setPlainText(req.log)
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        if req.state == "new":
            self.where.setText("Waiting for automation's posting worker to pick this up…" if
                               self.state.worker_running() else "Starting…")
        elif req.state == "running":
            self.where.setText("Working on it…")
        if req.done:
            self.timer.stop()
            self.ok, self.result = req.state == "done", req.result
            self.where.setText(("✓ " if self.ok else "✖ ") + (req.result or req.state))
            self.where.setProperty("muted", False)
            self.where.setStyleSheet(f"color: {theme.GOOD if self.ok else theme.BAD}")
            self.done_btn.setVisible(False)
            self.cancel_btn.setVisible(False)
            self.close_btn.setVisible(True)
            self.close_btn.setDefault(True)

    def _cancel(self) -> None:
        self.state.cancel(self.rid)
        self.cancel_btn.setEnabled(False)
        self.where.setText("Stopping…")

    def reject(self) -> None:  # Esc / the window's X
        req = self.state.actions.get(self.rid)
        if req is not None and not req.done:
            self._cancel()
        super().reject()


# =====================================================================================================
# Accounts


class AccountsPage(QWidget):
    notice = Signal(str)
    open_niches = Signal()

    def __init__(self, state: PostingState, main_settings, parent=None):
        super().__init__(parent)
        self.state = state
        self._main = main_settings  # () -> ClipMonster's Settings (for the niches that post here)
        self._current = ""
        self._filling = False
        self._save_timer = QTimer(self, singleShot=True, interval=600)
        self._save_timer.timeout.connect(self.state.save)
        self._build()
        state.accounts_changed.connect(self.reload)
        state.changed.connect(self._refresh_status)
        self.reload()

    # --- layout ---------------------------------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        head = QHBoxLayout()
        head.addWidget(_h2("TikTok accounts"))
        self.summary = _muted()
        head.addWidget(self.summary, 1)
        outer.addLayout(head)

        self.empty = QFrame(objectName="panel")
        el = QVBoxLayout(self.empty)
        el.addWidget(QLabel("<p style='font-size:13pt'><b>Add the TikTok account clips post to</b></p>"
                            "<p>ClipMonster posts each niche's clips to its account by itself, on a schedule, "
                            "and reads how they do. Add an account, log in to TikTok once, then pick it for "
                            "your niches in the Niches tab.</p>"))
        first = QPushButton("Add an account")
        first.setObjectName("primary")
        first.clicked.connect(self.add)
        el.addWidget(first, 0, Qt.AlignLeft)
        outer.addWidget(self.empty)

        self.split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._picked)
        ll.addWidget(self.list, 1)
        row = QHBoxLayout()
        add = QPushButton("+ Add")
        add.clicked.connect(self.add)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.remove)
        row.addWidget(add)
        row.addWidget(self.remove_btn)
        ll.addLayout(row)
        self.split.addWidget(left)

        scroll = QScrollArea(widgetResizable=True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        self.form_area = body
        bl = QVBoxLayout(body)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size: 11pt; font-weight: 600")
        bl.addWidget(self.status)
        self.detail = _muted()
        self.detail.setTextFormat(Qt.PlainText)
        bl.addWidget(self.detail)
        btns = QHBoxLayout()
        self.login_btn = QPushButton("Log in to TikTok")
        self.login_btn.setObjectName("primary")
        self.login_btn.clicked.connect(self.login)
        self.test_btn = QPushButton("Test posting")
        self.test_btn.setToolTip("Opens TikTok's upload page with a 3-second test clip and checks every step "
                                 "without posting anything")
        self.test_btn.clicked.connect(lambda: self._ask("test_posting", "Test posting", full=True))
        self.read_btn = QPushButton("Read stats now")
        self.read_btn.clicked.connect(lambda: self._ask("read_now", "Reading stats"))
        self.logout_btn = QPushButton("Log out")
        self.logout_btn.clicked.connect(self.logout)
        for b in (self.login_btn, self.test_btn, self.read_btn, self.logout_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        bl.addLayout(btns)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        def section(text: str) -> None:
            form.addRow(_h2(text))

        section("Account")
        self.name = QLineEdit(placeholderText="Optional: shown instead of the @name")
        self.name.editingFinished.connect(lambda: self._set("name", self.name.text().strip(), relist=True))
        form.addRow("Name", self.name)
        nrow = QHBoxLayout()
        self.niches = _muted()
        nrow.addWidget(self.niches, 1)
        pick = QPushButton("Niches tab")
        pick.setToolTip("Pick which niches post to this account")
        pick.clicked.connect(self.open_niches.emit)
        nrow.addWidget(pick)
        form.addRow("Niches", nrow)
        self.niche = QLineEdit(placeholderText="e.g. Rainbow Six streamer clips")
        self.niche.setToolTip("What the account posts, in a few words: the AI writes its hashtags and bio for it")
        self.niche.editingFinished.connect(lambda: self._set("niche", self.niche.text().strip()))
        form.addRow("Audience", self.niche)

        section("Schedule")
        self.autopilot = QCheckBox("Post this account's clips by themselves")
        self.autopilot.setToolTip("Off pauses posting: clips wait in its queue")
        self.autopilot.toggled.connect(lambda v: self._set("autopilot", bool(v)))
        form.addRow("", self.autopilot)
        self.per_day = QSpinBox(minimum=1, maximum=10)
        self.per_day.valueChanged.connect(lambda v: self._set("posts_per_day", int(v)))
        form.addRow("Posts a day", self.per_day)
        times = QHBoxLayout()
        self.first = QTimeEdit(displayFormat="HH:mm")
        self.last = QTimeEdit(displayFormat="HH:mm")
        self.first.timeChanged.connect(lambda t: self._set("first_post_time", t.toString("HH:mm")))
        self.last.timeChanged.connect(lambda t: self._set("last_post_time", t.toString("HH:mm")))
        times.addWidget(self.first)
        times.addWidget(QLabel("to"))
        times.addWidget(self.last)
        times.addStretch(1)
        form.addRow("Between", times)
        self.gap = QSpinBox(minimum=30, maximum=720, singleStep=15, suffix=" min")
        self.gap.valueChanged.connect(lambda v: self._set("min_gap_minutes", int(v)))
        form.addRow("At least apart", self.gap)
        self.vary = QSpinBox(minimum=0, maximum=60, suffix=" min")
        self.vary.setToolTip("Each post time moves by up to this much, differently each day, so it doesn't look "
                             "like clockwork")
        self.vary.valueChanged.connect(lambda v: self._set("vary_minutes", int(v)))
        form.addRow("Vary each time by", self.vary)
        warm = QHBoxLayout()
        self.warm = _muted()
        warm.addWidget(self.warm, 1)
        self.warm_btn = QPushButton()
        self.warm_btn.clicked.connect(self._toggle_warm_up)
        warm.addWidget(self.warm_btn)
        form.addRow("Warm-up", warm)

        section("Posts")
        self.privacy = QComboBox()
        for value, label in PRIVACY:
            self.privacy.addItem(label, value)
        self.privacy.activated.connect(lambda i: self._set("default_privacy", self.privacy.itemData(i)))
        form.addRow("Who can watch", self.privacy)
        self.hashtags = QLineEdit(placeholderText="#fyp")
        self.hashtags.setToolTip("Added to every post, after the clip's own hashtags")
        self.hashtags.editingFinished.connect(lambda: self._set("default_hashtags", self.hashtags.text().strip()))
        form.addRow("Always add", self.hashtags)
        self.live = QCheckBox("Read its views, likes and followers every few minutes")
        self.live.setToolTip("Also how ClipMonster knows a post is up, so its files can be cleared away")
        self.live.toggled.connect(lambda v: self._set("live", bool(v)))
        form.addRow("", self.live)

        section("TikTok profile")
        self.p_name = QLineEdit(placeholderText="Shown name (30 characters)")
        self.p_name.editingFinished.connect(lambda: self._set("profile_name", self.p_name.text().strip()))
        form.addRow("Name", self.p_name)
        self.p_bio = QPlainTextEdit()
        self.p_bio.setFixedHeight(64)
        self.p_bio.setPlaceholderText("Bio (80 characters)")
        self.p_bio.textChanged.connect(lambda: self._set("profile_bio", self.p_bio.toPlainText().strip(),
                                                         later=True))
        form.addRow("Bio", self.p_bio)
        photo = QHBoxLayout()
        self.p_photo = _muted()
        photo.addWidget(self.p_photo, 1)
        pick = QPushButton("Choose photo…")
        pick.clicked.connect(self._pick_photo)
        photo.addWidget(pick)
        form.addRow("Photo", photo)
        prow = QHBoxLayout()
        self.p_status = _muted()
        prow.addWidget(self.p_status, 1)
        self.apply_btn = QPushButton("Apply to TikTok")
        self.apply_btn.clicked.connect(self._apply_profile)
        prow.addWidget(self.apply_btn)
        form.addRow("", prow)
        bl.addLayout(form)
        bl.addStretch(1)
        scroll.setWidget(body)
        self.split.addWidget(scroll)
        self.split.setSizes([260, 700])
        outer.addWidget(self.split, 1)

    # --- filling ---------------------------------------------------------------------------------

    def reload(self) -> None:
        accounts = list(self.state.accounts)
        self.empty.setVisible(not accounts)
        self.split.setVisible(bool(accounts))
        with QSignalBlocker(self.list):
            self.list.clear()
            for a in accounts:
                it = QListWidgetItem()
                it.setData(Qt.UserRole, a.id)
                self.list.addItem(it)
        self._relabel()
        ids = [a.id for a in accounts]
        if self._current not in ids:
            self._current = ids[0] if ids else ""
        if self._current:
            self.list.setCurrentRow(ids.index(self._current))
        self._fill()
        logged = sum(a.logged_in for a in accounts)
        self.summary.setText(f"   {len(accounts)} account{'s' * (len(accounts) != 1)}"
                             + (f" · {logged} logged in" if accounts else ""))

    def _relabel(self) -> None:
        for i in range(self.list.count()):
            it = self.list.item(i)
            a = self.state.accounts.get(it.data(Qt.UserRole))
            if a is None:
                continue
            mark = "●" if a.logged_in else "○"
            q = len([x for x in self.state.store.queue(a.id) if x.status == "queued"])
            it.setText(f"{mark} {a.label}\n{a.handle if a.label != a.handle else ''}{' · ' if a.label != a.handle else ''}"
                       f"{q} waiting" + ("" if a.autopilot else " · paused"))
            it.setForeground(QColor(theme.TEXT if a.logged_in else theme.MUTED))

    def account(self) -> Account | None:
        return self.state.accounts.get(self._current)

    def _picked(self, cur, _prev) -> None:
        self._current = cur.data(Qt.UserRole) if cur else ""
        self._fill()

    def select(self, account_id: str) -> None:
        self._current = account_id
        self.reload()

    def _fill(self) -> None:
        a = self.account()
        self.form_area.setEnabled(a is not None)
        self.remove_btn.setEnabled(a is not None)
        if a is None:
            return
        self._filling = True
        try:
            self.name.setText(a.name)
            self.niche.setText(a.niche)
            self.autopilot.setChecked(a.autopilot)
            self.per_day.setValue(int(a.posts_per_day))
            self.first.setTime(QTime.fromString(a.first_post_time, "HH:mm"))
            self.last.setTime(QTime.fromString(a.last_post_time, "HH:mm"))
            self.gap.setValue(int(a.min_gap_minutes))
            self.vary.setValue(int(a.vary_minutes))
            self.privacy.setCurrentIndex(max(0, self.privacy.findData(a.default_privacy)))
            self.hashtags.setText(a.default_hashtags)
            self.live.setChecked(a.live)
            self.p_name.setText(a.profile_name)
            if self.p_bio.toPlainText().strip() != a.profile_bio:
                self.p_bio.setPlainText(a.profile_bio)
        finally:
            self._filling = False
        self._refresh_status()

    def _refresh_status(self) -> None:
        self._relabel()
        a = self.account()
        if a is None:
            return
        now = time.time()
        self.status.setText(account_status(self.state, a, now))
        self.status.setStyleSheet(f"font-size: 11pt; font-weight: 600; color: {theme.TEXT if a.logged_in else theme.WARN}")
        lines = []
        p = self.state.store.profile(a.id)
        if p.fetched_at:
            lines.append(f"{live.fmt(p.followers)} followers · {live.fmt(p.likes)} likes · stats read "
                         f"{live.ago(now - p.fetched_at)} ago")
        if a.upload_check_at:
            verdict = {"ok": "passed", "warn": "passed with a warning", "fail": "FAILED"}.get(a.upload_check, "")
            lines.append(f"Posting test {verdict} {live.ago(now - a.upload_check_at)} ago"
                         + (f": {a.upload_check_detail.splitlines()[0]}" if a.upload_check == "fail" and
                            a.upload_check_detail else ""))
        busy = self.state.busy(a.id)
        if busy is not None:
            lines.append(f"Working on: {busy.kind.replace('_', ' ')}…")
        if not self.state.worker_running():
            lines.append("Automation isn't running, so nothing posts by itself: start it in the Automation tab.")
        self.detail.setText("\n".join(lines))
        self.login_btn.setText("Log in again" if a.logged_in else "Log in to TikTok")
        self.login_btn.setObjectName("" if a.logged_in else "primary")
        self.login_btn.style().unpolish(self.login_btn)
        self.login_btn.style().polish(self.login_btn)
        for b in (self.test_btn, self.read_btn, self.logout_btn):
            b.setEnabled(a.logged_in)
        try:
            from .. import niches

            mine = niches.for_account(self._main(), a.id)
        except Exception:  # noqa: BLE001
            mine = []
        self.niches.setText(", ".join(n.name for n in mine) if mine else
                            "No niche posts here yet: pick this account for a niche in the Niches tab.")
        s = self.state.settings_for(a)
        notes = schedule.describe(s, dt.date.today())
        warm = schedule.warm_up_day(s, dt.date.today())
        self.warm.setText(next((n for n in notes if n.startswith("Warm-up")), "") if warm is not None else
                          "Off: posts its full schedule.")
        self.warm_btn.setText("End warm-up" if warm is not None else "Start warm-up")
        self.warm_btn.setToolTip("A new TikTok account that posts a lot straight away looks like spam: warm-up "
                                 "posts less for two weeks")
        self.p_photo.setText(Path(a.profile_photo).name if a.profile_photo else "No photo")
        self.p_status.setText(profile.status(a))
        self.apply_btn.setEnabled(a.logged_in and profile.pending(a))

    # --- changes ---------------------------------------------------------------------------------

    def _set(self, field: str, value, relist: bool = False, later: bool = False) -> None:
        a = self.account()
        if a is None or self._filling or getattr(a, field) == value:
            return
        setattr(a, field, value)
        if field in ("posts_per_day", "first_post_time", "last_post_time", "min_gap_minutes", "vary_minutes",
                     "autopilot"):
            jobs.replan(self.state.store, self.state.accounts, self.state.settings)
        self._save_timer.start()
        if relist or field == "autopilot":
            self.state.accounts_changed.emit()
        if not later:
            self._refresh_status()

    def _toggle_warm_up(self) -> None:
        a = self.account()
        if a is None:
            return
        if schedule.warm_up_day(self.state.settings_for(a), dt.date.today()) is not None:
            a.warm_up_start = 0.0
        else:
            a.start_warm_up()
        jobs.replan(self.state.store, self.state.accounts, self.state.settings)
        self.state.save()
        self._refresh_status()

    def add(self) -> None:
        a = self.state.add_account()
        self._current = a.id
        self.reload()
        self.login()

    def remove(self) -> None:
        a = self.account()
        if a is None:
            return
        q = len([i for i in self.state.store.queue(a.id) if i.status == "queued"])
        box = QMessageBox(QMessageBox.Warning, "Remove account",
                          f"Remove {a.label}? Its TikTok login on this PC is deleted"
                          + (f", and its {q} waiting clip{'s' * (q != 1)} go back to waiting for an account" if q
                             else "") + ". Nothing changes on TikTok.", parent=self)
        yes = box.addButton("Remove", QMessageBox.DestructiveRole)
        box.addButton("Keep it", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is yes:
            self.state.remove_account(a.id)
            self.notice.emit(f"Removed {a.label}.")

    def _ask(self, kind: str, title: str, **args) -> None:
        a = self.account()
        if a is None:
            return
        rid = self.state.ask(kind, account=a.id, **args)
        dlg = RequestDialog(self.state, rid, f"{title} · {a.label}", "", parent=self)
        dlg.intro.setVisible(False)
        dlg.exec()
        self.state.poll()
        self._refresh_status()

    def login(self) -> None:
        a = self.account()
        if a is None:
            return
        rid = self.state.ask("login", account=a.id)
        dlg = RequestDialog(self.state, rid, f"Log in · {a.label}",
                            "A browser window opens on TikTok's login page. Log in there (the QR code from the "
                            "TikTok app is quickest). The window closes by itself once you're in; press "
                            "“I'm logged in” if it doesn't. You only do this once per account.",
                            login=True, parent=self)
        dlg.exec()
        self.state.poll()
        self.reload()
        if dlg.ok:
            self.notice.emit(dlg.result)
            try:
                from .. import niches

                main = self._main()
                if niches.link_accounts(main, self.state.accounts):
                    main.save()
            except Exception:  # noqa: BLE001
                pass

    def logout(self) -> None:
        a = self.account()
        if a is None:
            return
        if QMessageBox.question(self, "Log out", f"Log {a.label} out of TikTok on this PC? Nothing posts to it "
                                "until it's logged in again.") != QMessageBox.Yes:
            return
        self._ask("logout", "Log out")

    def _pick_photo(self) -> None:
        a = self.account()
        if a is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Profile photo", "", "Pictures (*.jpg *.jpeg *.png *.webp)")
        if not path:
            return
        try:
            profile.use_photo(a, profile.prepare_photo(Path(path), a.id))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Profile photo", f"Couldn't use that picture: {exc}")
            return
        self.state.save()
        self._refresh_status()

    def _apply_profile(self) -> None:
        a = self.account()
        if a is None:
            return
        problems = profile.problems(a.profile_name, a.profile_bio)
        if problems:
            QMessageBox.warning(self, "TikTok profile", "\n".join(problems))
            return
        self._ask("apply_profile", "Set the TikTok profile")


# =====================================================================================================
# Queue


QUEUE_COLS = ("Posts", "Clip", "Account", "Score", "Hashtags")
POSTED_COLS = ("Posted", "Clip", "Account", "Views", "Likes", "Files")


class QueuePage(QWidget):
    notice = Signal(str)
    open_niches = Signal()

    def __init__(self, state: PostingState, parent=None):
        super().__init__(parent)
        self.state = state
        self._filling = False
        self._save_timer = QTimer(self, singleShot=True, interval=600)
        self._save_timer.timeout.connect(self.state.save)
        self._build()
        state.changed.connect(self.refresh)
        state.accounts_changed.connect(self._fill_accounts)
        state.request_done.connect(self._request_done)
        self._fill_accounts()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        head = QHBoxLayout()
        head.addWidget(_h2("Posting queue"))
        self.which = QComboBox()
        self.which.activated.connect(lambda _i: self.refresh())
        head.addWidget(self.which)
        self.summary = _muted()
        head.addWidget(self.summary, 1)
        outer.addLayout(head)
        self.waiting = QLabel()
        self.waiting.setWordWrap(True)
        self.waiting.setTextFormat(Qt.RichText)
        self.waiting.setStyleSheet(f"color: {theme.WARN}")
        self.waiting.linkActivated.connect(lambda _l: self.open_niches.emit())
        outer.addWidget(self.waiting)

        split = QSplitter(Qt.Vertical)
        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        self.queue = QTableWidget(0, len(QUEUE_COLS))
        self.queue.setHorizontalHeaderLabels(QUEUE_COLS)
        self._table(self.queue, stretch=1)
        self.queue.itemSelectionChanged.connect(self._picked)
        tl.addWidget(self.queue, 3)
        side = QFrame(objectName="panel")
        sl = QVBoxLayout(side)
        self.clip_title = QLabel()
        self.clip_title.setWordWrap(True)
        self.clip_title.setStyleSheet("font-weight: 600")
        sl.addWidget(self.clip_title)
        self.clip_info = _muted()
        sl.addWidget(self.clip_info)
        sl.addWidget(QLabel("Caption"))
        self.caption = QPlainTextEdit()
        self.caption.setFixedHeight(70)
        self.caption.textChanged.connect(lambda: self._edit("caption", self.caption.toPlainText().strip()))
        sl.addWidget(self.caption)
        sl.addWidget(QLabel("Hashtags"))
        self.tags = QLineEdit()
        self.tags.editingFinished.connect(lambda: self._edit("hashtags", self.tags.text().strip()))
        sl.addWidget(self.tags)
        row = QHBoxLayout()
        self.up_btn = QPushButton("▲")
        self.up_btn.setObjectName("icon")
        self.up_btn.setToolTip("Post sooner")
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn = QPushButton("▼")
        self.down_btn.setObjectName("icon")
        self.down_btn.setToolTip("Post later")
        self.down_btn.clicked.connect(lambda: self._move(1))
        self.now_btn = QPushButton("Post now")
        self.now_btn.clicked.connect(self._post_now)
        self.skip_btn = QPushButton("Don't post")
        self.skip_btn.setToolTip("Takes it out of the queue for good (the file stays where it is)")
        self.skip_btn.clicked.connect(self._skip)
        self.retry_btn = QPushButton("Try again")
        self.retry_btn.clicked.connect(self._retry)
        for b in (self.up_btn, self.down_btn, self.now_btn, self.retry_btn, self.skip_btn):
            row.addWidget(b)
        sl.addLayout(row)
        self.open_btn = QPushButton("Show the file")
        self.open_btn.clicked.connect(lambda: self._item() and _open(str(Path(self._item().path).parent)))
        sl.addWidget(self.open_btn)
        sl.addStretch(1)
        side.setMinimumWidth(300)
        tl.addWidget(side, 1)
        split.addWidget(top)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 6, 0, 0)
        brow = QHBoxLayout()
        brow.addWidget(_h2("Posted"))
        self.posted_summary = _muted()
        brow.addWidget(self.posted_summary, 1)
        self.record_btn = QPushButton("Open record")
        self.record_btn.clicked.connect(lambda: self._posted_item() and _open(self._posted_item().record))
        self.tiktok_btn = QPushButton("Open on TikTok")
        self.tiktok_btn.clicked.connect(lambda: self._posted_item() and _open(self._posted_item().tiktok_url))
        self.records_btn = QPushButton("Records folder")
        self.records_btn.clicked.connect(lambda: _open(str(self._records_folder())))
        for b in (self.record_btn, self.tiktok_btn, self.records_btn):
            brow.addWidget(b)
        bl.addLayout(brow)
        self.posted = QTableWidget(0, len(POSTED_COLS))
        self.posted.setHorizontalHeaderLabels(POSTED_COLS)
        self._table(self.posted, stretch=1)
        self.posted.itemSelectionChanged.connect(self._posted_picked)
        self.posted.itemDoubleClicked.connect(lambda _i: self._posted_item() and _open(self._posted_item().record))
        bl.addWidget(self.posted, 1)
        split.addWidget(bottom)
        split.setSizes([480, 320])
        outer.addWidget(split, 1)

    @staticmethod
    def _table(t: QTableWidget, stretch: int) -> None:
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.verticalHeader().setVisible(False)
        t.setShowGrid(False)
        t.setAlternatingRowColors(False)
        h = t.horizontalHeader()
        for c in range(t.columnCount()):
            h.setSectionResizeMode(c, QHeaderView.Stretch if c == stretch else QHeaderView.ResizeToContents)

    def _records_folder(self) -> Path:
        path = records.records_folder(self.state.settings)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _fill_accounts(self) -> None:
        keep = self.which.currentData()
        with QSignalBlocker(self.which):
            self.which.clear()
            self.which.addItem("All accounts", "")
            for a in self.state.accounts:
                self.which.addItem(a.label, a.id)
            self.which.setCurrentIndex(max(0, self.which.findData(keep)))
        self.refresh()

    def show_account(self, account_id: str) -> None:
        self.which.setCurrentIndex(max(0, self.which.findData(account_id)))
        self.refresh()

    # --- filling -----------------------------------------------------------------------------------

    def _rows(self) -> list[VideoItem]:
        aid = self.which.currentData() or None
        jobs.replan(self.state.store, self.state.accounts, self.state.settings)
        with self.state.store.lock:
            q = [i for i in self.state.store.items.values() if i.account and i.status in ("queued", "failed",
                                                                                           "uploading")
                 and (aid is None or i.account == aid)]
        order = {"uploading": 0, "queued": 1, "failed": 2}
        return sorted(q, key=lambda i: (order[i.status], i.planned_at or float("inf"), i.queue_pos or 0))

    def refresh(self) -> None:
        if self._filling:
            return
        now = time.time()
        keep = self._item().id if self._item() else ""
        rows = self._rows()
        self._filling = True
        try:
            self.queue.setRowCount(len(rows))
            for r, it in enumerate(rows):
                when = {"uploading": "Posting now…", "failed": "✖ Failed"}.get(it.status) or \
                    (schedule.fmt_when(it.planned_at, now) if it.planned_at else "Waiting")
                a = self.state.accounts.get(it.account)
                if a is not None and not a.autopilot and it.status == "queued":
                    when = "Paused"
                elif a is not None and not a.logged_in and it.status == "queued":
                    when = "Needs login"
                cells = (when, it.headline + (f"  ({it.part_label})" if it.part else ""),
                         a.label if a else "?", f"{it.total:.0f}" if it.total is not None else "",
                         it.hashtags)
                for c, text in enumerate(cells):
                    cell = QTableWidgetItem(text)
                    cell.setData(Qt.UserRole, it.id)
                    if it.status == "failed":
                        cell.setForeground(QColor(theme.BAD))
                        cell.setToolTip(it.last_error)
                    self.queue.setItem(r, c, cell)
                if it.id == keep:
                    self.queue.selectRow(r)
        finally:
            self._filling = False
        queued = [i for i in rows if i.status == "queued"]
        today = sum(1 for i in queued if i.planned_at and dt.date.fromtimestamp(i.planned_at) == dt.date.today())
        self.summary.setText(f"   {len(queued)} waiting · {today} posting today")
        waiting = intake.unassigned(self.state.store)
        if waiting:
            niches_ = sorted({i.niche or "no niche" for i in waiting})
            self.waiting.setText(f"⚠ {len(waiting)} clip{'s are' if len(waiting) != 1 else ' is'} waiting for an "
                                 f"account ({', '.join(niches_)}): pick the account in the "
                                 "<a href='#'>Niches tab</a>.")
        self.waiting.setVisible(bool(waiting))
        self._fill_posted(now)
        self._picked()

    def _fill_posted(self, now: float) -> None:
        aid = self.which.currentData() or None
        keep = self._posted_item().id if self._posted_item() else ""
        items = [i for i in self.state.store.posted(aid)][:200]
        self.posted.setRowCount(len(items))
        for r, it in enumerate(items):
            v = self.state.store.video_for(it)
            a = self.state.accounts.get(it.account)
            if it.cleaned_at:
                files = "Cleared ✓"
            elif it.keep_reason:
                files = "Kept: not seen yet" if "Not seen" in it.keep_reason else "Kept"
            elif not it.tiktok_id:
                files = "Waiting to see it on TikTok"
            else:
                files = "Clearing…"
            cells = (schedule.fmt_when(it.posted_at, now), it.headline, a.label if a else "Removed account",
                     live.fmt(v.views) if v else "", live.fmt(v.likes) if v else "", files)
            for c, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, it.id)
                if c == 5 and it.keep_reason:
                    cell.setToolTip(it.keep_reason)
                    cell.setForeground(QColor(theme.WARN))
                self.posted.setItem(r, c, cell)
            if it.id == keep:
                self.posted.selectRow(r)
        total = sum((self.state.store.video_for(i).views if self.state.store.video_for(i) else 0) for i in items)
        self.posted_summary.setText(f"   {len(items)} posted · {live.fmt(total)} views")
        self._posted_picked()

    # --- the selected clip ------------------------------------------------------------------------

    def _item(self) -> VideoItem | None:
        rows = self.queue.selectionModel().selectedRows() if self.queue.selectionModel() else []
        if not rows:
            return None
        cell = self.queue.item(rows[0].row(), 0)
        return self.state.store.get(cell.data(Qt.UserRole)) if cell else None

    def _posted_item(self) -> VideoItem | None:
        rows = self.posted.selectionModel().selectedRows() if self.posted.selectionModel() else []
        if not rows:
            return None
        cell = self.posted.item(rows[0].row(), 0)
        return self.state.store.get(cell.data(Qt.UserRole)) if cell else None

    def _posted_picked(self) -> None:
        it = self._posted_item()
        self.record_btn.setEnabled(bool(it and it.record and Path(it.record).exists()))
        self.tiktok_btn.setEnabled(bool(it and it.tiktok_url))

    def _picked(self) -> None:
        if self._filling:
            return
        it = self._item()
        for w in (self.caption, self.tags, self.up_btn, self.down_btn, self.now_btn, self.skip_btn, self.retry_btn,
                  self.open_btn):
            w.setEnabled(it is not None)
        if it is None:
            self.clip_title.setText("Pick a clip to change its caption, order or post it now.")
            self.clip_info.setText("")
            return
        self._filling = True
        try:
            self.clip_title.setText(it.headline)
            info = []
            if it.duration:
                info.append(f"{it.duration:.0f} s")
            if it.niche:
                info.append(it.niche)
            if it.source_channel:
                info.append(f"from {it.source_channel}")
            if it.status == "failed":
                info.append(f"\n✖ {it.last_error}")
            if it.warnings:
                info.append("\n⚠ " + "; ".join(it.warnings))
            self.clip_info.setText(" · ".join(info))
            if self.caption.toPlainText().strip() != it.caption:
                self.caption.setPlainText(it.caption)
            self.tags.setText(it.hashtags)
        finally:
            self._filling = False
        queued = it.status == "queued"
        self.up_btn.setEnabled(queued)
        self.down_btn.setEnabled(queued)
        self.now_btn.setEnabled(queued)
        self.retry_btn.setVisible(it.status == "failed")
        self.retry_btn.setToolTip("It may have gone up already: check the account on TikTok first"
                                  if it.last_error == INTERRUPTED else "Queue it again")

    def _edit(self, field: str, value: str) -> None:
        it = self._item()
        if it is None or self._filling or getattr(it, field) == value:
            return
        with self.state.store.lock:
            setattr(it, field, value)
        self._save_timer.start()

    def _move(self, delta: int) -> None:
        it = self._item()
        if it is None:
            return
        q = [i for i in self.state.store.queue(it.account)]
        ids = [i.id for i in q]
        i = ids.index(it.id)
        j = max(0, min(len(ids) - 1, i + delta))
        if i == j:
            return
        ids.insert(j, ids.pop(i))
        self.state.store.set_order(ids)
        self.state.save()
        self.refresh()

    def _post_now(self) -> None:
        it = self._item()
        if it is None:
            return
        a = self.state.accounts.get(it.account)
        if a is None or not a.logged_in:
            QMessageBox.information(self, "Post now", "Log the account in to TikTok first (Accounts tab).")
            return
        self.state.ask("post_now", account=a.id, item=it.id)
        self.notice.emit(f"Posting “{it.headline}” to {a.handle} now…")

    def _request_done(self, req) -> None:
        if req.kind == "post_now":
            self.notice.emit(("✓ " if req.state == "done" else "✖ ") + req.result)
            self.refresh()

    def _skip(self) -> None:
        it = self._item()
        if it is None:
            return
        if QMessageBox.question(self, "Don't post", f"Take “{it.headline}” out of the queue for good? The video "
                                "file stays where it is.") != QMessageBox.Yes:
            return
        self.state.store.remove([it.id])
        self.state.save()
        self.refresh()

    def _retry(self) -> None:
        it = self._item()
        if it is None or it.status != "failed":
            return
        if it.last_error == INTERRUPTED and QMessageBox.question(
                self, "Try again", "This upload was cut off part way, so it may be on TikTok already. Check the "
                "account first. Post it again?") != QMessageBox.Yes:
            return
        with self.state.store.lock:
            it.status, it.last_error = "queued", ""
        self.state.save()
        self.refresh()


# =====================================================================================================
# Performance


class PerformancePage(QWidget):
    def __init__(self, state: PostingState, parent=None):
        super().__init__(parent)
        self.state = state
        self._build()
        state.changed.connect(self.refresh)
        state.accounts_changed.connect(self._fill_accounts)
        self._fill_accounts()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        head = QHBoxLayout()
        head.addWidget(_h2("Performance"))
        self.which = QComboBox()
        self.which.activated.connect(lambda _i: self.refresh())
        head.addWidget(self.which)
        head.addStretch(1)
        outer.addLayout(head)
        self.tiles = QHBoxLayout()
        self.tile_labels = {}
        for key, label in (("followers", "Followers"), ("views", "Views today"), ("likes", "Likes today"),
                           ("new_followers", "New followers today"), ("posted", "Posted this week")):
            box = QFrame(objectName="panel")
            bl = QVBoxLayout(box)
            value = QLabel("—")
            value.setStyleSheet("font-size: 18pt; font-weight: 700")
            bl.addWidget(value)
            bl.addWidget(_muted(label))
            self.tiles.addWidget(box)
            self.tile_labels[key] = value
        outer.addLayout(self.tiles)
        self.insights = _muted()
        outer.addWidget(self.insights)
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(_h2("Top videos (30 days)"))
        self.top = QTableWidget(0, 4)
        self.top.setHorizontalHeaderLabels(("Video", "Views", "Likes", "Posted"))
        QueuePage._table(self.top, stretch=0)
        self.top.itemDoubleClicked.connect(self._open_top)
        ll.addWidget(self.top, 1)
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(_h2("Live feed"))
        self.feed = QListWidget()
        rl.addWidget(self.feed, 1)
        split.addWidget(right)
        split.setSizes([600, 400])
        outer.addWidget(split, 1)

    def _fill_accounts(self) -> None:
        keep = self.which.currentData()
        with QSignalBlocker(self.which):
            self.which.clear()
            for a in self.state.accounts:
                self.which.addItem(a.label, a.id)
            self.which.setCurrentIndex(max(0, self.which.findData(keep)))
        self.refresh()

    def refresh(self) -> None:
        aid = self.which.currentData()
        a = self.state.accounts.get(aid)
        if a is None:
            for v in self.tile_labels.values():
                v.setText("—")
            self.feed.clear()
            self.top.setRowCount(0)
            self.insights.setText("Add an account in the Accounts tab to see how it's doing.")
            return
        now = time.time()
        store = self.state.store
        p = store.profile(a.id)
        events = live.EventLog(a.id).events
        today = live.today(events, now)
        week = store.posts_since(now - 7 * 86400, a.id)
        self.tile_labels["followers"].setText(live.fmt(p.followers) if p.fetched_at else "—")
        self.tile_labels["views"].setText(live.fmt(today["views"]))
        self.tile_labels["likes"].setText(live.fmt(today["likes"]))
        self.tile_labels["new_followers"].setText(f"{today['followers']:+,}" if today["followers"] else "0")
        self.tile_labels["posted"].setText(str(week))
        rows = [o for o in results.outcomes(store, now) if o.account == a.id]
        lines = results.insights(rows)
        self.insights.setText("  ".join(lines) if lines else
                              ("Stats read " + live.ago(now - p.fetched_at) + " ago." if p.fetched_at else
                               "No stats yet: they're read every few minutes once the account is logged in."))
        vids = sorted((v for v in store.videos(a.id).values() if not v.create_time or now - v.create_time < 30 * 86400),
                      key=lambda v: v.views, reverse=True)[:50]
        self.top.setRowCount(len(vids))
        for r, v in enumerate(vids):
            for c, text in enumerate((live.video_name(v), live.fmt(v.views), live.fmt(v.likes),
                                      schedule.fmt_when(v.create_time, now) if v.create_time else "")):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.UserRole, v.url)
                self.top.setItem(r, c, cell)
        self.feed.clear()
        for e in reversed(events[-200:]):
            it = QListWidgetItem(f"{dt.datetime.fromtimestamp(e.at).strftime('%a %H:%M')}  {e.text}")
            if e.kind == "error":
                it.setForeground(QColor(theme.BAD))
            elif e.alert:
                it.setForeground(QColor(theme.GOOD))
            self.feed.addItem(it)

    def _open_top(self, cell) -> None:
        _open(cell.data(Qt.UserRole) or "")


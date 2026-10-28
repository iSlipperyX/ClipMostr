"""Sign in to YouTube window: opens Firefox in ClipMonster's own profile and follows along.

It looks every couple of seconds at whether that Firefox is still open and whether it's signed in,
and says what to do next. Once it's signed in and closed, `signed_in` fires: the caller switches
downloads to this sign-in and ends a sign-in pause.
"""

from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from .. import youtube_login
from . import theme

POLL_MS = 1500
STARTING = 15.0  # seconds Firefox gets to open before "not open" means you closed it


class SignInDialog(QDialog):
    signed_in = Signal()
    forgotten = Signal()

    def __init__(self, parent=None, open_now: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Sign in to YouTube")
        self.resize(520, 300)
        self._opened_at = 0.0
        self._seen_open = False
        self._done = False

        layout = QVBoxLayout(self)
        about = QLabel("ClipMonster opens Firefox with a profile of its own, separate from any browser you use. "
                       "Sign in to YouTube there (a spare Google account is best, not your main one), then close "
                       "that Firefox window. Downloads and automation use that sign-in from then on, which is what "
                       "keeps YouTube from asking this PC to confirm it's not a bot.<br><br>"
                       "Don't use that Firefox for anything else: ClipMonster reads the sign-in from it, and it "
                       "lasts longest when nothing else touches it. If YouTube ever asks again, sign in here again.")
        about.setWordWrap(True)
        about.setTextFormat(Qt.RichText)
        layout.addWidget(about)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.RichText)
        layout.addWidget(self.status, 1)

        row = QHBoxLayout()
        self.open_btn = QPushButton("Open Firefox")
        self.open_btn.setObjectName("primary")
        self.open_btn.clicked.connect(self.open_firefox)
        self.get_btn = QPushButton("Get Firefox")
        self.get_btn.setToolTip(youtube_login.GET_FIREFOX_URL)
        self.get_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(youtube_login.GET_FIREFOX_URL)))
        self.forget_btn = QPushButton("Forget this sign-in")
        self.forget_btn.setToolTip("Delete ClipMonster's Firefox profile, and with it the sign-in")
        self.forget_btn.clicked.connect(self.forget)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        row.addWidget(self.open_btn)
        row.addWidget(self.get_btn)
        row.addWidget(self.forget_btn)
        row.addStretch(1)
        row.addWidget(self.close_btn)
        layout.addLayout(row)

        self.timer = QTimer(self, interval=POLL_MS)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.poll()
        if open_now and not youtube_login.firefox_open():
            QTimer.singleShot(0, self.open_firefox)

    def open_firefox(self) -> None:
        try:
            youtube_login.open_sign_in()
        except FileNotFoundError:
            self._say(f"<span style='color:{theme.WARN}'>⚠ Firefox isn't installed on this PC.</span> Press Get "
                      "Firefox, install it, then press Open Firefox.")
            self.get_btn.setVisible(True)
            return
        except OSError as exc:
            self._say(f"<span style='color:{theme.BAD}'>✗ Couldn't open Firefox: {_esc(str(exc))}</span>")
            return
        self._opened_at = time.monotonic()
        self._seen_open = False
        self._done = False
        self.poll()

    def poll(self) -> None:
        is_open = youtube_login.firefox_open()
        self._seen_open |= is_open
        got = youtube_login.read_sign_in()
        starting = self._opened_at and not self._seen_open and time.monotonic() - self._opened_at < STARTING
        self.get_btn.setVisible(youtube_login.find_firefox() is None)
        self.open_btn.setText("Open Firefox again" if got.signed_in else "Open Firefox")
        self.open_btn.setEnabled(not is_open and not starting)
        self.forget_btn.setVisible(got.signed_in or youtube_login.profile_dir().exists())
        self.forget_btn.setEnabled(not is_open)
        if got.signed_in and not is_open and not starting:
            until = f" It lasts until about {datetime.fromtimestamp(got.expires):%d %b %Y}." if got.expires else ""
            self._say(f"<span style='color:{theme.GOOD}'>✓ Signed in to YouTube.</span> Downloads and automation "
                      f"use this sign-in now.{until}")
            self.close_btn.setText("Done")
            if not self._done:
                self._done = True
                self.signed_in.emit()
        elif got.signed_in:
            self._say(f"<span style='color:{theme.GOOD}'>✓ Signed in.</span> Now close that Firefox window to "
                      "finish.")
        elif is_open or starting:
            self._say(f"<span style='color:{theme.ACCENT_2}'>●</span> "
                      + ("Opening Firefox…" if starting else
                         "Waiting for you to sign in to YouTube in Firefox. Once you see your account picture on "
                         "YouTube, close that Firefox window."))
        elif self._seen_open:
            self._say(f"<span style='color:{theme.WARN}'>⚠ Firefox closed before YouTube was signed in.</span> "
                      "Press Open Firefox to try again.")
        else:
            self._say("Not signed in yet. Press Open Firefox.")

    def forget(self) -> None:
        if QMessageBox.question(self, "Forget the sign-in", "Delete ClipMonster's YouTube sign-in? Downloads won't be "
                                "signed in until you sign in again.") != QMessageBox.Yes:
            return
        try:
            youtube_login.forget()
        except OSError as exc:
            self._say(f"<span style='color:{theme.BAD}'>✗ {_esc(str(exc))}</span>")
            return
        self._done = False
        self._seen_open = False
        self._opened_at = 0.0
        self.close_btn.setText("Close")
        self.forgotten.emit()
        self.poll()

    def done(self, result: int) -> None:
        self.timer.stop()
        # Closed this window while Firefox was still open, but already signed in: that's still a sign-in.
        if not self._done and youtube_login.read_sign_in().signed_in:
            self._done = True
            self.signed_in.emit()
        super().done(result)

    def _say(self, html: str) -> None:
        if self.status.text() != html:
            self.status.setText(html)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

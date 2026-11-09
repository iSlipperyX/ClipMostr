"""Settings → Telegram, under the fields: the bot's state, Link a phone, the linked phones, Send a test.

The token is the one typed above (not saved yet is fine). Linking shows a code; while the dialog is open the
window listens to Telegram for it (telegram/pair.Linker), unless automation's bot is running as the same
bot, which links the chat itself. Every second the page reads telegram.json and the bot's heartbeat
(telegram-status.json), so a phone linked by automation shows up here too.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..telegram import pair, state
from . import forms, theme

BOTFATHER = "https://t.me/BotFather"


def _esc(text: str) -> str:
    import html

    return html.escape(str(text), quote=False)


class TelegramLink(QWidget):
    def __init__(self, token: Callable[[], str], on: Callable[[], bool], parent=None):
        super().__init__(parent)
        self._token, self._on = token, on
        self.linker: pair.Linker | None = None
        self._checked: tuple[str, str, str] | None = None  # (token, "@name", why not) from Check the token
        self._busy = False
        self._sig = None

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(8)
        how = forms.WrapLabel(
            "1. In Telegram, open <a href='%s' style='color:%s'>@BotFather</a>, send <b>/newbot</b>, and pick a name "
            "and a username for your bot.<br>2. Paste the token it gives you above, and tick Use the Telegram bot."
            "<br>3. Press <b>Link a phone</b> and send the code to your bot. Then Save, and keep automation running "
            "(Automation tab, or Start with Windows): the bot runs with it." % (BOTFATHER, theme.ACCENT_2))
        how.setOpenExternalLinks(True)
        how.setProperty("muted", True)
        v.addWidget(how)

        self.bot_line = forms.WrapLabel("")
        v.addWidget(self.bot_line)

        row = QHBoxLayout()
        self.check_btn = QPushButton("Check the token")
        self.check_btn.clicked.connect(self._check)
        self.link_btn = QPushButton("📱  Link a phone…")
        self.link_btn.setObjectName("primary")
        self.link_btn.clicked.connect(self._link)
        self.test_btn = QPushButton("Send a test")
        self.test_btn.setToolTip("Sends a hello to each linked phone")
        self.test_btn.clicked.connect(self._test)
        for b in (self.check_btn, self.link_btn, self.test_btn):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        self.code_box = QFrame()
        self.code_box.setObjectName("card")
        box = QVBoxLayout(self.code_box)
        box.setContentsMargins(14, 10, 14, 10)
        self.code_label = QLabel("")
        self.code_label.setStyleSheet("font-size: 26px; font-weight: 600; letter-spacing: 6px;")
        self.code_text = forms.WrapLabel("")
        crow = QHBoxLayout()
        self.open_btn = QPushButton("Open Telegram")
        self.open_btn.setToolTip("Opens your bot in Telegram with the code ready to send")
        self.open_btn.clicked.connect(self._open)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop_linking)
        crow.addWidget(self.open_btn)
        crow.addWidget(self.stop_btn)
        crow.addStretch(1)
        box.addWidget(self.code_label)
        box.addWidget(self.code_text)
        box.addLayout(crow)
        self.code_box.hide()
        v.addWidget(self.code_box)

        self.result = forms.WrapLabel("")
        self.result.hide()
        v.addWidget(self.result)

        head = QLabel("Linked phones")
        head.setStyleSheet("font-weight: 600;")
        v.addWidget(head)
        self.chats_box = QVBoxLayout()
        self.chats_box.setSpacing(4)
        v.addLayout(self.chats_box)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    # --- what it shows --------------------------------------------------------------------------------

    def refresh(self) -> None:
        data = state.load()
        status = state.read_status()
        token = self._token().strip()
        bot = (data.get("bot") or {}).get("username") or ""
        if self._checked and self._checked[0] == token:
            bot = self._checked[1].lstrip("@") or bot
        if not token:
            line = (f"<span style='color:{theme.MUTED}'>No bot yet: make one with @BotFather and paste its token "
                    "above.</span>")
        elif self._checked and self._checked[0] == token and self._checked[2]:
            line = f"<span style='color:{theme.BAD}'>✗ {_esc(self._checked[2])}</span>"
        elif status.get("running") and status.get("bot"):
            line = (f"<span style='color:{theme.GOOD}'>●</span> @{_esc(status['bot'])} is running in automation"
                    + (f". <span style='color:{theme.WARN}'>{_esc(status['error'])}</span>" if status.get("error")
                       else "."))
        elif bot:
            running = "" if not self._on() else " It starts with automation once you Save."
            line = f"Bot: @{_esc(bot)}.{running}"
            if status.get("error") and status.get("bot") == bot:
                line += f" <span style='color:{theme.WARN}'>Last time: {_esc(status['error'])}</span>"
        else:
            line = "Press Check the token to see the bot's name."
        if token and not self._on():
            line += f" <span style='color:{theme.WARN}'>Tick Use the Telegram bot to turn it on.</span>"
        self.bot_line.setText(line)
        self.link_btn.setEnabled(bool(token) and not self._linking())
        self.check_btn.setEnabled(bool(token) and not self._busy)
        self.test_btn.setEnabled(bool(token) and bool(data["chats"]) and not self._busy)
        self._show_linker()
        sig = tuple((c["id"], c.get("name"), c.get("username"), c.get("blocked")) for c in data["chats"])
        if sig != self._sig:
            self._sig = sig
            self._fill_chats(data["chats"])

    def _fill_chats(self, chats: list[dict]) -> None:
        while self.chats_box.count():
            item = self.chats_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                lay = item.layout()
                while lay.count():
                    sub = lay.takeAt(0).widget()
                    if sub is not None:
                        sub.deleteLater()
        if not chats:
            none = QLabel("None yet.")
            none.setProperty("muted", True)
            self.chats_box.addWidget(none)
            return
        for c in chats:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            when = time.strftime("%d %b %Y", time.localtime(c.get("linked_at") or 0)) if c.get("linked_at") else ""
            text = f"📱 {_esc(state.chat_name(c))}" + (f"<span style='color:{theme.MUTED}'> · linked {when}</span>"
                                                       if when else "")
            if c.get("blocked"):
                text += f" <span style='color:{theme.WARN}'>(blocked the bot: send it a message to carry on)</span>"
            label = QLabel(text)
            unlink = QPushButton("Unlink")
            unlink.setToolTip("This phone can't use the bot any more (link it again any time)")
            unlink.clicked.connect(lambda _=False, cid=c["id"]: self._unlink(cid))
            h.addWidget(label, 1)
            h.addWidget(unlink)
            self.chats_box.addWidget(row)

    def _linking(self) -> bool:
        return self.linker is not None and self.linker.phase in ("checking", "waiting")

    def _show_linker(self) -> None:
        lk = self.linker
        if lk is None:
            return
        if lk.phase == "checking":
            self._say("Checking the token…", theme.MUTED)
            self.code_box.hide()
        elif lk.phase == "waiting":
            self.result.hide()
            self.code_box.show()
            left = max(0, int(lk.until - time.time()))
            self.code_label.setText(lk.code)
            who = "Automation's bot" if lk.by_automation else "ClipMonster"
            self.code_text.setText(
                f"In Telegram, open <b>@{_esc(lk.bot)}</b> and send it this code, or press Open Telegram and then "
                f"Start. It works for {left // 60}:{left % 60:02d} more. {who} is listening for it."
                + (f"<br><span style='color:{theme.WARN}'>{_esc(lk.error)}</span>" if lk.error else ""))
        else:
            self.code_box.hide()
            if lk.phase == "linked":
                self._say(f"✓ Linked {_esc(lk.linked)}. Save, and the bot answers there while automation runs.",
                          theme.GOOD)
            elif lk.phase == "expired":
                self._say("The code ran out before a phone sent it. Press Link a phone for a new one.", theme.WARN)
            elif lk.phase == "error":
                self._say(f"✗ {_esc(lk.error)}", theme.BAD)
            self.linker = None

    def _say(self, text: str, colour: str) -> None:
        self.result.setText(f"<span style='color:{colour}'>{text}</span>")
        self.result.show()

    # --- buttons ------------------------------------------------------------------------------------------

    def _check(self) -> None:
        token = self._token().strip()
        self._busy = True
        self._say("Checking…", theme.MUTED)

        def work() -> None:
            name, why = pair.check_token(token)
            self._checked = (token, name, why)
            self._busy = False

        threading.Thread(target=work, daemon=True).start()
        QTimer.singleShot(300, self._check_done)

    def _check_done(self) -> None:
        if self._busy:
            QTimer.singleShot(300, self._check_done)
            return
        token, name, why = self._checked or ("", "", "")
        if why:
            self._say(f"✗ {_esc(why)}", theme.BAD)
        else:
            self._say(f"✓ Telegram knows this token: it's {_esc(name)}.", theme.GOOD)
        self.refresh()

    def _link(self) -> None:
        self._stop_linking()
        self.linker = pair.Linker(self._token())
        self.linker.start()
        self.refresh()

    def _open(self) -> None:
        if self.linker is not None and self.linker.link:
            QDesktopServices.openUrl(QUrl(self.linker.link))

    def _stop_linking(self) -> None:
        if self.linker is not None:
            self.linker.stop()
            self.linker = None
            with state.edit() as data:
                data["pairing"] = {}
        self.code_box.hide()

    def _unlink(self, chat_id: int) -> None:
        with state.edit() as data:
            state.unlink(data, chat_id)
        self._sig = None
        self.refresh()

    def _test(self) -> None:
        token = self._token().strip()
        chats = [c["id"] for c in state.load()["chats"]]
        self._busy = True
        self._say("Sending…", theme.MUTED)
        out: dict = {}

        def work() -> None:
            out["got"] = pair.send_test(token, chats)
            self._busy = False

        threading.Thread(target=work, daemon=True).start()

        def done() -> None:
            if self._busy:
                QTimer.singleShot(300, done)
                return
            reached, problem = out.get("got", (0, ""))
            if problem:
                self._say(f"Reached {reached} of {len(chats)}. ✗ {_esc(problem)}", theme.WARN if reached else theme.BAD)
            else:
                self._say(f"✓ Sent: look in Telegram ({reached} phone{'s' if reached != 1 else ''}).", theme.GOOD)
            self.refresh()

        QTimer.singleShot(300, done)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """The dialog closed: stop listening, so automation's bot can."""
        self.timer.stop()
        if self.linker is not None:
            self.linker.stop()
            self.linker = None

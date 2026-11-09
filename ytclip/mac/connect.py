"""Connecting: linking a PC by its IP address, port and the authorization code from its Settings → Mac app,
the PCs on this network (found by themselves, which fill the address in) and the ones this Mac is linked to."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from ..gui import theme
from ..remote import protocol
from . import common as c
from . import discover, hosts
from .client import Client, split_address

SEARCH_EVERY = 4000
CHECK_AFTER = 600  # ms after typing stops before asking what answers at the address


class _PCCard(QFrame):
    picked = Signal()
    forget = Signal()

    def __init__(self, title: str, sub: str, linked: bool, can_forget: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 12, 10)
        icon = QLabel("🖥")
        icon.setStyleSheet("font-size: 20pt;")
        h.addWidget(icon)
        v = QVBoxLayout()
        v.setSpacing(1)
        t = QLabel(f"<b>{c.esc(title)}</b>")
        s = QLabel(sub)
        s.setProperty("muted", True)
        v.addWidget(t)
        v.addWidget(s)
        h.addLayout(v, 1)
        h.addWidget(c.Chip("Linked" if linked else "Not linked yet", theme.GOOD if linked else theme.MUTED))
        if can_forget:
            f = QPushButton("Forget")
            f.setObjectName("flat")
            f.setToolTip("Remove this PC from the Mac (unlink it on the PC too)")
            f.clicked.connect(self.forget.emit)
            h.addWidget(f)
        go = QPushButton("Connect" if linked else "Link…")
        go.setObjectName("primary" if linked else "")
        go.clicked.connect(self.picked.emit)
        h.addWidget(go)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        self.picked.emit()


def fill_code(text: str) -> str:
    """What the code field shows for what was typed: capitals, no look-alike gaps, ABCD-EFGH."""
    clean = protocol.normalize_code(text)[: protocol.CODE_LENGTH]
    return clean if len(clean) <= 4 else f"{clean[:4]}-{clean[4:]}"


class ConnectPage(QWidget):
    """Shown until a PC is connected. Linking is by the PC's IP address, its port and the authorization code
    from its Settings → Mac app; PCs found on this network fill the address in, linked PCs connect at once."""

    connect_to = Signal(object)  # hosts.Host

    def __init__(self, tasks, parent=None):
        super().__init__(parent)
        self.tasks = tasks
        self.setObjectName("page")
        self.setAttribute(Qt.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        outer.addWidget(area)
        body = QWidget()
        body.setObjectName("page")
        area.setWidget(body)
        mid = QHBoxLayout(body)
        mid.addStretch(1)
        col = QWidget()
        col.setMaximumWidth(620)
        col.setMinimumWidth(420)
        mid.addWidget(col, 4)
        mid.addStretch(1)
        v = QVBoxLayout(col)
        v.setContentsMargins(8, 40, 8, 32)
        v.setSpacing(12)

        logo = QLabel()
        logo.setPixmap(theme.app_icon_pixmap(128).scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        v.addWidget(logo, 0, Qt.AlignHCenter)
        t = c.title("Connect to your PC")
        t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        sub = c.WrapLabel("ClipMonster Remote runs ClipMonster on your Windows PC from this Mac. The PC does the "
                          "clipping, the AI and the posting; you watch, OK and steer it from here.", muted=True)
        sub.setAlignment(Qt.AlignCenter)
        v.addWidget(sub)
        v.addSpacing(4)
        self.message = c.WrapLabel("")
        self.message.hide()
        v.addWidget(self.message)

        # --- the PC's address, port and code ---
        form, fv = c.panel(16, 10)
        fv.addWidget(c.h2("Link with the PC's address"))
        fv.addWidget(c.WrapLabel(
            "On the PC, open ClipMonster → Settings → <b>Mac app</b> and press <b>Link a Mac</b>. It shows its IP "
            "address, port and an authorization code: type them here.", muted=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.addWidget(self._label("IP address"), 0, 0)
        grid.addWidget(self._label("Port"), 0, 1)
        self.address = QLineEdit()
        self.address.setPlaceholderText("192.168.1.20")
        self.address.setToolTip("The PC's IP address, or its name (with Tailscale, like my-pc.tail1234.ts.net)")
        self.address.textEdited.connect(self._address_edited)
        self.address.editingFinished.connect(self._split_address)
        self.address.returnPressed.connect(self._go)
        self.port = QLineEdit(str(protocol.PORT))
        self.port.setValidator(QIntValidator(1, 65535, self))
        self.port.setMaxLength(5)
        self.port.setFixedWidth(96)
        self.port.setToolTip(f"Settings → Mac app → Port on the PC ({protocol.PORT} unless you changed it)")
        self.port.textEdited.connect(lambda _t: self._check_soon())
        self.port.returnPressed.connect(self._go)
        grid.addWidget(self.address, 1, 0)
        grid.addWidget(self.port, 1, 1)
        grid.addWidget(self._label("Authorization code"), 2, 0, 1, 2)
        self.code = QLineEdit()
        self.code.setPlaceholderText("ABCD-EFGH")
        self.code.setAlignment(Qt.AlignCenter)
        self.code.setStyleSheet("font-size: 20pt; font-weight: 600; letter-spacing: 4px; padding: 6px;")
        self.code.setMaxLength(12)
        self.code.textEdited.connect(self._code_edited)
        self.code.returnPressed.connect(self._go)
        grid.addWidget(self.code, 3, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        fv.addLayout(grid)
        self.pc_line = c.WrapLabel("", muted=True)  # what answers at that address: its name, ID, a code up or not
        self.pc_line.hide()
        fv.addWidget(self.pc_line)
        self.form_msg = c.WrapLabel("")
        self.form_msg.hide()
        fv.addWidget(self.form_msg)
        r = QHBoxLayout()
        r.addStretch(1)
        self.go_btn = QPushButton("Link")
        self.go_btn.setObjectName("primary")
        self.go_btn.setMinimumWidth(110)
        self.go_btn.clicked.connect(self._go)
        r.addWidget(self.go_btn)
        fv.addLayout(r)
        v.addWidget(form)

        v.addWidget(c.h2("On this network"))
        self.found_box = QVBoxLayout()
        self.found_box.setSpacing(8)
        v.addLayout(self.found_box)
        self.searching = c.WrapLabel("Looking for ClipMonster on this network…", muted=True)
        v.addWidget(self.searching)

        self.linked_head = c.h2("Linked PCs")
        v.addWidget(self.linked_head)
        self.linked_box = QVBoxLayout()
        self.linked_box.setSpacing(8)
        v.addLayout(self.linked_box)

        v.addWidget(c.WrapLabel(
            "On the PC: Settings → Mac app → tick <b>Let my Mac run ClipMonster</b>, Save, and keep automation running. "
            "Away from home, use Tailscale on both computers and type the PC's Tailscale address above.", muted=True))
        v.addStretch(1)
        self.found: list[dict] = []
        self.timer = QTimer(self)
        self.timer.setInterval(SEARCH_EVERY)
        self.timer.timeout.connect(self.search)
        self.check_timer = QTimer(self)  # looks up what answers at the typed address, once typing stops
        self.check_timer.setSingleShot(True)
        self.check_timer.setInterval(CHECK_AFTER)
        self.check_timer.timeout.connect(self._check)
        self._check_gen = 0
        self._seen: dict | None = None  # {"host", "port", "fp", "info"} of the last answer at the typed address
        self._busy = False
        self._searching = False
        self._searched = False
        last = hosts.last()
        if last is not None:
            self.address.setText(last.address)
            self.port.setText(str(last.port))
        self._update_button()

    @staticmethod
    def _label(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setStyleSheet("font-weight: 600;")
        return lab

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self.refresh()
        self.search()
        self.timer.start()
        self._check_soon()

    def hideEvent(self, e):  # noqa: N802
        self.timer.stop()
        super().hideEvent(e)

    def say(self, text: str, colour: str = "") -> None:
        self.message.setText(f"<span style='color:{colour or theme.WARN}'>{text}</span>" if text else "")
        self.message.setVisible(bool(text))

    def _form_say(self, text: str, colour: str = "") -> None:
        self.form_msg.setText(f"<span style='color:{colour or theme.WARN}'>{text}</span>" if text else "")
        self.form_msg.setVisible(bool(text))

    # --- the form ----------------------------------------------------------------------------------------

    def target(self) -> tuple[str, int | None]:
        """The typed address and port (None when the port isn't a number from 1 to 65535)."""
        host = self.address.text().strip()
        try:
            port = int(self.port.text().strip())
        except ValueError:
            port = None
        if host.count(":") == 1 or "/" in host or host.startswith("["):  # "192.168.1.20:47320" in the address box
            host, typed_port = split_address(host, default_port=0)
            port = typed_port or port
        if port is not None and not 0 < port < 65536:
            port = None
        return host, port

    def _address_edited(self, _text: str) -> None:
        self._check_soon()

    def _split_address(self) -> None:
        """'192.168.1.20:47320' or a pasted https:// link typed as the address: the port goes in its own box."""
        text = self.address.text().strip()
        if text.count(":") == 1 or "/" in text or text.startswith("["):
            host, port = split_address(text, default_port=0)
            if host and host != text:
                self.address.setText(host)
                if port:
                    self.port.setText(str(port))
                self._check_soon()

    def _code_edited(self, text: str) -> None:
        shown = fill_code(text)
        if shown != text:
            self.code.setText(shown)
        self._update_button()

    def _linked_at(self, host: str, port: int | None) -> hosts.Host | None:
        linked, _ = hosts.load()
        h = next((x for x in linked if x.address == host and x.port == port), None)
        if h is None and self._seen and self._seen["host"] == host and self._seen["port"] == port:
            h = next((x for x in linked if x.fp == self._seen["fp"]), None)  # a linked PC at a new address
        return h

    def _update_button(self) -> None:
        host, port = self.target()
        linked = self._linked_at(host, port) if host else None
        self.go_btn.setText("Connect" if linked is not None and not self.code.text().strip() else "Link")
        self.go_btn.setEnabled(not self._busy)

    def _check_soon(self) -> None:
        self._seen = None
        self.pc_line.hide()
        self._update_button()
        self.check_timer.start()

    def _check(self) -> None:
        """Ask whatever answers at the typed address who it is (no key needed), to show it before linking."""
        host, port = self.target()
        if not host or port is None:
            return
        self._check_gen += 1
        gen = self._check_gen
        cl = Client(host, port, timeout=4.0)

        def ok(info):
            if gen != self._check_gen or self.target() != (host, port):
                return
            self._seen = {"host": host, "port": port, "fp": cl.seen_fp, "info": info}
            known = self._linked_at(host, port)
            bits = [f"<b>{c.esc(info.get('name') or host)}</b>", f"ClipMonster {c.esc(info.get('version') or '?')}",
                    f"ID {protocol.short_fingerprint(cl.seen_fp)}"]
            if int(info.get("api") or 0) != protocol.API_VERSION:
                bits.append(f"<span style='color:{theme.WARN}'>needs updating to work with this app</span>")
            elif known is not None:
                bits.append(f"<span style='color:{theme.GOOD}'>linked: press Connect</span>")
            elif (info.get("pairing") or {}).get("open"):
                bits.append(f"<span style='color:{theme.GOOD}'>a code is up</span>")
            else:
                bits.append(f"<span style='color:{theme.WARN}'>no code is up yet: press Link a Mac on the PC</span>")
            self.pc_line.setText("✓ Found " + " · ".join(bits))
            self.pc_line.show()
            self._update_button()

        def fail(exc):
            if gen != self._check_gen or self.target() != (host, port):
                return
            self.pc_line.setText(f"<span style='color:{theme.MUTED}'>Nothing answers there yet: "
                                 f"{c.esc(getattr(exc, 'message', exc))}</span>")
            self.pc_line.show()

        self.tasks.run(cl.hello, ok, fail)

    def _go(self) -> None:
        if self._busy:
            return
        self._split_address()
        host, port = self.target()
        if not host:
            self._form_say("Type the PC's IP address, from Settings → Mac app on the PC.")
            self.address.setFocus()
            return
        if port is None:
            self._form_say(f"The port is a number from 1 to 65535 ({protocol.PORT} unless you changed it on the PC).")
            self.port.setFocus()
            return
        code = self.code.text().strip()
        if code:
            if not protocol.looks_like_code(code):
                self._form_say(f"The authorization code is {protocol.CODE_LENGTH} letters and numbers, like ABCD-EFGH.")
                self.code.setFocus()
                return
            self._link(host, port, code)
            return
        known = self._linked_at(host, port)
        if known is not None:
            self._connect_known(known, host, port)
            return
        # maybe a PC this Mac is linked to, at a new address: its certificate says
        self._set_busy(True, "Connecting…")
        cl = Client(host, port, timeout=6.0)

        def ok(info):
            self._set_busy(False)
            linked, _ = hosts.load()
            known = next((x for x in linked if x.fp == cl.seen_fp), None)
            if known is not None:
                self._connect_known(known, host, port)
            else:
                self._form_say(f"This Mac isn't linked to {c.esc(info.get('name') or host)} yet: type the "
                               "authorization code from its Settings → Mac app → Link a Mac.")
                self.code.setFocus()

        def fail(exc):
            self._set_busy(False)
            self._form_say(c.esc(getattr(exc, "message", exc)), theme.BAD)

        self.tasks.run(cl.hello, ok, fail)

    def _connect_known(self, h: hosts.Host, host: str, port: int) -> None:
        if (h.address, h.port) != (host, port):
            h.address, h.port = host, port
            if host not in h.addresses:
                h.addresses = (h.addresses + [host])[-5:]
            hosts.remember(h)
        self._form_say("")
        self.connect_to.emit(h)

    def _link(self, host: str, port: int, code: str) -> None:
        self._set_busy(True, "Linking… (checking the code takes a moment)")
        cl = Client(host, port, timeout=30.0)

        def ok(got):
            self._set_busy(False)
            h = hosts.Host(id=got["host_id"] or got["fp"][:16], name=got["name"], address=host, port=port,
                           fp=got["fp"], device_id=got["device_id"], linked_at=time.time(), seen_at=time.time(),
                           addresses=[host])
            hosts.remember(h, got["token"])
            self.code.clear()
            self._form_say("")
            self.connect_to.emit(h)

        def fail(exc):
            self._set_busy(False)
            self._form_say("✗ " + c.esc(getattr(exc, "message", exc)), theme.BAD)
            self.code.setFocus()
            self.code.selectAll()

        self.tasks.run(lambda: cl.pair(code), ok, fail)

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        if busy:
            self._form_say(text, theme.MUTED)
        for w in (self.address, self.port, self.code):
            w.setEnabled(not busy)
        self._update_button()

    # --- PCs found by themselves, and linked ones ---------------------------------------------------------

    def search(self) -> None:
        if self._searching:
            return
        self._searching = True

        def ok(found):
            self._searching = False
            self._searched = True
            self.found = found
            self.refresh()

        def fail(_e):
            self._searching = False

        self.tasks.run(lambda: discover.discover(1.2), ok, fail)

    def refresh(self) -> None:
        linked, _last = hosts.load()
        ids = {h.id for h in linked}
        c.clear(self.found_box)
        for f in self.found:
            sub = f"{f['address']} · port {f['port']} · ClipMonster {f['version']}"
            if f["api"] != protocol.API_VERSION:
                sub += " · needs updating to work with this app"
            card = _PCCard(f["name"], sub, f["id"] in ids)
            card.picked.connect(lambda f=f: self._pick_found(f))
            self.found_box.addWidget(card)
        if self.found:
            self.searching.setText("Not listed? Type its IP address above.")
        elif self._searched:
            self.searching.setText("No PC has answered yet. On the PC, check Settings → Mac app is on and automation "
                                   "is running, or type its IP address above. Still looking…")
        else:
            self.searching.setText("Looking for ClipMonster on this network…")
        c.clear(self.linked_box)
        others = [h for h in linked if h.id not in {f["id"] for f in self.found}]
        self.linked_head.setVisible(bool(others))
        for h in others:
            sub = f"{h.address} · port {h.port} · last connected {c.ago(h.seen_at) or 'never'}"
            card = _PCCard(h.name, sub, True, can_forget=True)
            card.picked.connect(lambda h=h: self.connect_to.emit(h))
            card.forget.connect(lambda h=h: (hosts.forget(h.id), self.refresh(), self._update_button()))
            self.linked_box.addWidget(card)

    def _pick_found(self, f: dict) -> None:
        linked, _ = hosts.load()
        h = next((x for x in linked if x.id == f["id"]), None)
        if h is not None:
            if h.address != f["address"] or h.port != f["port"]:  # the PC's address changed: same certificate?
                if f["fp"] and f["fp"] != h.fp:
                    self.say("That PC shows a different ID than when this Mac was linked, so it isn't trusted. Link it "
                             "again if you reinstalled ClipMonster on it.")
                    return
                h.address, h.port = f["address"], f["port"]
                hosts.remember(h)
            self.connect_to.emit(h)
            return
        # not linked yet: its address and port go in the form, and the code is what's left to type
        self.say("")
        self.address.setText(f["address"])
        self.port.setText(str(f["port"]))
        self._check_soon()
        self._form_say(f"Now type the authorization code {c.esc(f['name'])} shows under Settings → Mac app → "
                       "Link a Mac.", theme.MUTED)
        self.code.setFocus()

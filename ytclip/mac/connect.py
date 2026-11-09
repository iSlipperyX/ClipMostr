"""Connecting: the PCs on this network (found by themselves) and the ones this Mac is linked to, an address
to type by hand, and linking a PC with the code from its Settings → Mac app."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from ..gui import theme
from ..remote import protocol
from . import common as c
from . import discover, hosts
from .client import ApiError, Client, split_address

SEARCH_EVERY = 4000


class PairDialog(QDialog):
    """Type the code the PC shows. The PC's ID (its certificate) is shown to compare with the PC's screen."""

    def __init__(self, tasks, address: str, port: int, name: str = "", parent=None):
        super().__init__(parent)
        self.tasks = tasks
        self.address, self.port = address, port
        self.result_host: hosts.Host | None = None
        self.setWindowTitle("Link this Mac")
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 18)
        v.setSpacing(10)
        head = QLabel(f"Link this Mac to {name or address}")
        head.setObjectName("h2")
        head.setStyleSheet("font-size: 14pt; font-weight: 700;")
        v.addWidget(head)
        v.addWidget(c.WrapLabel("On the PC, open ClipMonster → Settings → <b>Mac app</b>, press <b>Link a Mac</b> and "
                                "type the code it shows here.", muted=False))
        self.code = QLineEdit()
        self.code.setPlaceholderText("ABCD-EFGH")
        self.code.setAlignment(Qt.AlignCenter)
        self.code.setStyleSheet("font-size: 22pt; font-weight: 600; letter-spacing: 4px; padding: 8px;")
        self.code.setMaxLength(12)
        self.code.textEdited.connect(self._typed)
        self.code.returnPressed.connect(self._link)
        v.addWidget(self.code)
        self.pc_id = c.WrapLabel("", muted=True)
        v.addWidget(self.pc_id)
        self.msg = c.WrapLabel("")
        v.addWidget(self.msg)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.go = QPushButton("Link")
        self.go.setObjectName("primary")
        self.go.setDefault(True)
        self.go.clicked.connect(self._link)
        row.addWidget(cancel)
        row.addWidget(self.go)
        v.addLayout(row)
        self._hello()

    def _hello(self) -> None:
        cl = Client(self.address, self.port, timeout=8.0)

        def ok(info):
            fp = cl.seen_fp
            self.pc_id.setText(f"This PC's ID: <b>{protocol.short_fingerprint(fp)}</b>. It should match the one under "
                               "Settings → Mac app on the PC.")
            if not (info.get("pairing") or {}).get("open"):
                self.msg.setText(f"<span style='color:{theme.WARN}'>No code is up on the PC yet: press Link a Mac "
                                 "there.</span>")

        def fail(exc):
            self.msg.setText(f"<span style='color:{theme.BAD}'>✗ {c.esc(getattr(exc, 'message', exc))}</span>")

        self.tasks.run(cl.hello, ok, fail)

    def _typed(self, text: str) -> None:
        clean = protocol.normalize_code(text)[: protocol.CODE_LENGTH]
        shown = clean if len(clean) <= 4 else f"{clean[:4]}-{clean[4:]}"
        if shown != text:
            self.code.setText(shown)

    def _link(self) -> None:
        code = self.code.text()
        if not protocol.looks_like_code(code):
            self.msg.setText(f"<span style='color:{theme.WARN}'>The code is {protocol.CODE_LENGTH} letters and numbers, "
                             "like ABCD-EFGH.</span>")
            return
        self.go.setEnabled(False)
        self.msg.setText("Linking… (checking the code takes a moment)")
        cl = Client(self.address, self.port, timeout=30.0)

        def ok(got):
            h = hosts.Host(id=got["host_id"] or got["fp"][:16], name=got["name"], address=self.address, port=self.port,
                           fp=got["fp"], device_id=got["device_id"], linked_at=time.time(), seen_at=time.time())
            hosts.remember(h, got["token"])
            self.result_host = h
            self.accept()

        def fail(exc):
            self.go.setEnabled(True)
            self.msg.setText(f"<span style='color:{theme.BAD}'>✗ {c.esc(getattr(exc, 'message', exc))}</span>")

        self.tasks.run(lambda: cl.pair(code), ok, fail)


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


class ConnectPage(QWidget):
    """Shown until a PC is connected."""

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
        v.setContentsMargins(8, 48, 8, 32)
        v.setSpacing(12)
        from ..gui import theme as th

        logo = QLabel()
        logo.setPixmap(th.app_icon_pixmap(128).scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        v.addWidget(logo, 0, Qt.AlignHCenter)
        t = c.title("Connect to your PC")
        t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        sub = c.WrapLabel("ClipMonster Remote runs ClipMonster on your Windows PC from this Mac. The PC does the "
                          "clipping, the AI and the posting; you watch, OK and steer it from here.", muted=True)
        sub.setAlignment(Qt.AlignCenter)
        v.addWidget(sub)
        v.addSpacing(8)
        self.message = c.WrapLabel("")
        self.message.hide()
        v.addWidget(self.message)

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

        v.addWidget(c.h2("Type its address"))
        self.address = QLineEdit()
        self.address.setPlaceholderText("192.168.1.20, or my-pc.tail1234.ts.net with Tailscale")
        self.address.returnPressed.connect(self._typed)
        go = QPushButton("Connect")
        go.clicked.connect(self._typed)
        r = QHBoxLayout()
        r.addWidget(self.address, 1)
        r.addWidget(go)
        v.addLayout(r)
        v.addWidget(c.WrapLabel(
            "On the PC: Settings → Mac app → tick <b>Let my Mac run ClipMonster</b>, Save, and keep automation running. "
            "The page there shows the PC's address. Away from home, use Tailscale on both computers.", muted=True))
        v.addStretch(1)
        self.found: list[dict] = []
        self.timer = QTimer(self)
        self.timer.setInterval(SEARCH_EVERY)
        self.timer.timeout.connect(self.search)
        self._searching = False
        self._searched = False

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self.refresh()
        self.search()
        self.timer.start()

    def hideEvent(self, e):  # noqa: N802
        self.timer.stop()
        super().hideEvent(e)

    def say(self, text: str, colour: str = "") -> None:
        self.message.setText(f"<span style='color:{colour or theme.WARN}'>{text}</span>" if text else "")
        self.message.setVisible(bool(text))

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
            sub = f"{f['address']}" + (f":{f['port']}" if f["port"] != protocol.PORT else "") + \
                  f" · ClipMonster {f['version']}"
            if f["api"] != protocol.API_VERSION:
                sub += " · needs updating to work with this app"
            card = _PCCard(f["name"], sub, f["id"] in ids)
            card.picked.connect(lambda f=f: self._pick_found(f))
            self.found_box.addWidget(card)
        if self.found:
            self.searching.setText("Not listed? Type its address below.")
        elif self._searched:
            self.searching.setText("No PC has answered yet. On the PC, check Settings → Mac app is on and automation "
                                   "is running, or type its address below. Still looking…")
        else:
            self.searching.setText("Looking for ClipMonster on this network…")
        c.clear(self.linked_box)
        others = [h for h in linked if h.id not in {f["id"] for f in self.found}]
        self.linked_head.setVisible(bool(others))
        for h in others:
            sub = f"{h.where} · last connected {c.ago(h.seen_at) or 'never'}"
            card = _PCCard(h.name, sub, True, can_forget=True)
            card.picked.connect(lambda h=h: self.connect_to.emit(h))
            card.forget.connect(lambda h=h: (hosts.forget(h.id), self.refresh()))
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
        self._pair(f["address"], f["port"], f["name"])

    def _typed(self) -> None:
        host, port = split_address(self.address.text())
        if not host:
            return
        linked, _ = hosts.load()
        h = next((x for x in linked if x.address == host and x.port == port), None)
        if h is not None:
            self.connect_to.emit(h)
            return
        # maybe a linked PC at a new address (Tailscale, or a new Wi-Fi address): its certificate says
        cl = Client(host, port, timeout=6.0)

        def ok(info):
            known = next((x for x in linked if x.fp == cl.seen_fp), None)
            if known is not None:
                known.address, known.port = host, port
                if host not in known.addresses:
                    known.addresses = (known.addresses + [host])[-5:]
                hosts.remember(known)
                self.connect_to.emit(known)
            else:
                self._pair(host, port, info.get("name") or host)

        self.say("Connecting…", theme.MUTED)
        self.tasks.run(cl.hello, ok, lambda e: self.say(c.esc(getattr(e, "message", e)), theme.BAD))

    def _pair(self, address: str, port: int, name: str) -> None:
        self.say("")
        d = PairDialog(self.tasks, address, port, name, self)
        if d.exec() and d.result_host is not None:
            self.connect_to.emit(d.result_host)

"""Settings → Mac app, under the fields: the server's state, Link a Mac, and the linked Macs.

Link a Mac writes a code to remote.json for ten minutes; automation's server (remote/server.py) checks the
Mac's proof against it and links the Mac. Every second the page reads remote.json and the server's heartbeat
(remote-status.json), so a Mac linked a moment ago shows up here.
"""

from __future__ import annotations

import html
import time
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..remote import protocol, state
from . import forms, theme

DOWNLOAD = "https://github.com/iSlipperyX/ClipMostr/releases/latest"
TAILSCALE = "https://tailscale.com/download"


def _esc(text) -> str:
    return html.escape(str(text), quote=False)


def ago(ts: float, now: float) -> str:
    s = max(0, int(now - (ts or 0)))
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return time.strftime("%d %b %Y", time.localtime(ts))


class MacLink(QWidget):
    def __init__(self, on: Callable[[], bool], port: Callable[[], int], parent=None):
        super().__init__(parent)
        self._on, self._port = on, port
        self._sig = None
        self._devices_before: set[str] | None = None  # the linked Macs when Link a Mac was pressed
        self._addr_cache: tuple[float, list[str]] | None = None

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(8)
        how = forms.WrapLabel(
            f"1. On your Mac, download <a href='{DOWNLOAD}' style='color:{theme.ACCENT_2}'>ClipMonster Remote</a> "
            "(ClipMonster-Remote-mac.dmg) and drag it to Applications.<br>2. Tick <b>Let my Mac run ClipMonster</b> "
            "above, Save, and keep automation running (Automation tab, or Start with Windows): the server runs with "
            "it. The first time, Windows asks whether ClipMonster may use the network: allow it on private networks."
            "<br>3. Press <b>Link a Mac</b>. In the Mac app, type the IP address, port and authorization code it "
            "shows (or pick this PC from the list there and type the code).")
        how.setOpenExternalLinks(True)
        how.setProperty("muted", True)
        v.addWidget(how)

        self.server_line = forms.WrapLabel("")
        v.addWidget(self.server_line)

        row = QHBoxLayout()
        self.link_btn = QPushButton("💻  Link a Mac…")
        self.link_btn.setObjectName("primary")
        self.link_btn.clicked.connect(self._link)
        row.addWidget(self.link_btn)
        row.addStretch(1)
        v.addLayout(row)

        self.code_box = QFrame()
        self.code_box.setObjectName("card")
        box = QVBoxLayout(self.code_box)
        box.setContentsMargins(14, 10, 14, 10)
        box.setSpacing(6)
        type_these = QLabel("In ClipMonster Remote on your Mac, type:")
        type_these.setStyleSheet("font-weight: 600;")
        box.addWidget(type_these)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(4)
        self.ip_label, self.port_label, self.code_label = QLabel(""), QLabel(""), QLabel("")
        self.ip_more = QLabel("")
        self.ip_more.setProperty("muted", True)
        for row, (name, value) in enumerate((("IP address", self.ip_label), ("Port", self.port_label),
                                             ("Authorization code", self.code_label))):
            n = QLabel(name)
            n.setProperty("muted", True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setStyleSheet("font-size: 20px; font-weight: 600;" if value is not self.code_label else
                                "font-size: 26px; font-weight: 600; letter-spacing: 3px;")
            grid.addWidget(n, row, 0, Qt.AlignVCenter)
            grid.addWidget(value, row, 1, Qt.AlignVCenter)
        grid.addWidget(self.ip_more, 0, 2, Qt.AlignVCenter)
        grid.setColumnStretch(2, 1)
        box.addLayout(grid)
        self.code_text = forms.WrapLabel("")
        crow = QHBoxLayout()
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop_linking)
        crow.addWidget(self.stop_btn)
        crow.addStretch(1)
        box.addWidget(self.code_text)
        box.addLayout(crow)
        self.code_box.hide()
        v.addWidget(self.code_box)

        self.result = forms.WrapLabel("")
        self.result.hide()
        v.addWidget(self.result)

        head = QLabel("Linked Macs")
        head.setStyleSheet("font-weight: 600;")
        v.addWidget(head)
        self.devices_box = QVBoxLayout()
        self.devices_box.setSpacing(4)
        v.addLayout(self.devices_box)

        away = forms.WrapLabel(
            "Away from home: install <a href='%s' style='color:%s'>Tailscale</a> on this PC and the Mac, signed in "
            "to the same account, then type this PC's Tailscale address in the Mac app instead of its IP address. The connection "
            "stays encrypted end to end, and nothing is opened to the internet." % (TAILSCALE, theme.ACCENT_2))
        away.setOpenExternalLinks(True)
        away.setProperty("muted", True)
        v.addWidget(away)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    # --- what it shows --------------------------------------------------------------------------------

    def refresh(self) -> None:
        now = time.time()
        data = state.load()
        status = state.read_status(now=now)
        running = bool(status.get("running"))
        if running:
            addrs = status.get("addresses") or []
            where = f" at <b>{_esc(addrs[0])}</b>" if addrs else ""
            line = (f"<span style='color:{theme.GOOD}'>●</span> Running in automation{where}, port "
                    f"{_esc(status.get('port'))}.")
            if status.get("fingerprint"):
                line += (f" <span style='color:{theme.MUTED}'>This PC's ID: "
                         f"{_esc(protocol.short_fingerprint(status['fingerprint']))}</span>")
        elif not self._on():
            line = f"<span style='color:{theme.MUTED}'>○ Off. Tick Let my Mac run ClipMonster, then Save.</span>"
        elif status.get("error") and now - float(status.get("at") or 0) < state.STATUS_FRESH:
            line = f"<span style='color:{theme.BAD}'>✗ {_esc(status['error'])}</span>"
        else:
            line = (f"<span style='color:{theme.WARN}'>○</span> It starts with automation once you Save: start "
                    "automation in the Automation tab if it isn't running.")
        self.server_line.setText(line)
        self._show_code(data, now, running, status)
        sig = tuple((d["id"], d.get("name"), int(d.get("seen_at") or 0) // 60, d.get("address"))
                    for d in data["devices"])
        if sig != self._sig:
            self._sig = sig
            self._fill(data["devices"], now)

    def _show_code(self, data: dict, now: float, running: bool, status: dict | None = None) -> None:
        p = state.pairing(data, now)
        if self._devices_before is not None:
            new = [d for d in data["devices"] if d["id"] not in self._devices_before]
            if new:
                self._devices_before = None
                self.code_box.hide()
                self._say(f"✓ Linked {_esc(new[-1].get('name'))}. It can use ClipMonster now.", theme.GOOD)
                self.link_btn.setEnabled(True)
                return
            if not p:
                self._devices_before = None
                self.code_box.hide()
                raw = data.get("pairing") or {}
                if int(raw.get("wrong") or 0) >= protocol.MAX_WRONG or not raw:
                    self._say("The code stopped working (it ran out, or was typed wrong too many times). Press Link "
                              "a Mac for a new one.", theme.WARN)
                else:
                    self._say("The code ran out before a Mac used it. Press Link a Mac for a new one.", theme.WARN)
                self.link_btn.setEnabled(True)
                return
        if not p:
            self.code_box.hide()
            self.link_btn.setEnabled(True)
            return
        self.link_btn.setEnabled(False)
        self.result.hide()
        self.code_box.show()
        left = max(0, int(float(p.get("until") or 0) - now))
        self.code_label.setText(protocol.show_code(p["code"]))
        status = status or {}
        addrs = (status.get("addresses") if running else None) or self._addresses()
        self.ip_label.setText(_esc(addrs[0]) if addrs else "unknown")
        self.ip_more.setText(("or " + ", ".join(_esc(a) for a in addrs[1:3])) if len(addrs) > 1 else "")
        self.port_label.setText(str(status.get("port") if running and status.get("port") else self._port()))
        tries = protocol.MAX_WRONG - int(p.get("wrong") or 0)
        text = f"The code works for {left // 60}:{left % 60:02d} more."
        if tries < protocol.MAX_WRONG:
            text += f" <span style='color:{theme.WARN}'>{tries} tr{'y' if tries == 1 else 'ies'} left.</span>"
        if not running:
            text += (f"<br><span style='color:{theme.WARN}'>Automation's server isn't running, so the Mac can't reach "
                     "this PC yet: Save with Let my Mac run ClipMonster ticked, and start automation.</span>")
        self.code_text.setText(text)

    def _addresses(self) -> list[str]:
        """This PC's addresses while the server isn't up to say (looked up once a minute at most)."""
        now = time.monotonic()
        if self._addr_cache is None or now - self._addr_cache[0] > 60:
            try:
                from ..remote.server import mac_addresses

                self._addr_cache = (now, mac_addresses())
            except Exception:  # noqa: BLE001 - only a hint on screen
                self._addr_cache = (now, [])
        return self._addr_cache[1]

    def _fill(self, devices: list[dict], now: float) -> None:
        while self.devices_box.count():
            item = self.devices_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if not devices:
            none = QLabel("None yet.")
            none.setProperty("muted", True)
            self.devices_box.addWidget(none)
            return
        for d in devices:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            linked = time.strftime("%d %b %Y", time.localtime(d.get("linked_at") or 0))
            seen = ago(float(d.get("seen_at") or 0), now)
            where = f" from {_esc(d['address'])}" if d.get("address") else ""
            label = QLabel(f"💻 {_esc(d.get('name') or 'Mac')}<span style='color:{theme.MUTED}'> · linked {linked} · "
                           f"last seen {seen}{where}</span>")
            unlink = QPushButton("Unlink")
            unlink.setToolTip("This Mac can't use ClipMonster any more (link it again any time)")
            unlink.clicked.connect(lambda _=False, did=d["id"]: self._unlink(did))
            h.addWidget(label, 1)
            h.addWidget(unlink)
            self.devices_box.addWidget(row)

    def _say(self, text: str, colour: str) -> None:
        self.result.setText(f"<span style='color:{colour}'>{text}</span>")
        self.result.show()

    # --- buttons ------------------------------------------------------------------------------------------

    def _link(self) -> None:
        with state.edit() as data:
            state.host_id(data)
            state.start_pairing(data, time.time())
            self._devices_before = {d["id"] for d in data["devices"]}
        self.result.hide()
        self.refresh()

    def _stop_linking(self) -> None:
        with state.edit() as data:
            state.stop_pairing(data)
        self._devices_before = None
        self.code_box.hide()
        self.link_btn.setEnabled(True)

    def _unlink(self, device_id: str) -> None:
        with state.edit() as data:
            state.unlink(data, device_id)
        self._sig = None
        self.refresh()

    def shutdown(self) -> None:
        """The dialog closed. A code that's up stays up for its ten minutes, so a Mac can still use it."""
        self.timer.stop()

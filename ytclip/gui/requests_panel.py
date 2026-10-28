"""The Automation tab's Niches and Requests pages, and the New request window.

Niches: one row per niche with how today is going (picked against its daily cap, videos waiting,
clips made), a pause switch that holds the niche's picking and clipping, and Find videos now.

Requests: clip requests (orders.py) with their progress, a New request button and Cancel.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton, QRadioButton,
                               QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import niches, orders, pipeline
from ..jobs import Job, JobDB
from ..policy import PickLog, picks_path
from ..research import youtube_key
from ..settings import Settings
from . import theme

NICHE_COLUMNS = ("Niche", "Account", "Picked today", "Waiting", "Clips today", "Status")
REQUEST_COLUMNS = ("Request", "Niche", "Ready", "Status", "Special requests")
ORDER_COLOURS = {"open": theme.ACCENT_2, "done": theme.GOOD, "short": theme.WARN, "cancelled": theme.MUTED}


def _table(columns: tuple[str, ...]) -> QTableWidget:
    t = QTableWidget(0, len(columns))
    t.setObjectName("panel")
    t.setHorizontalHeaderLabels(columns)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setShowGrid(False)
    t.setWordWrap(False)
    t.verticalHeader().setDefaultSectionSize(30)
    head = t.horizontalHeader()
    head.setSectionResizeMode(QHeaderView.ResizeToContents)
    head.setSectionResizeMode(0, QHeaderView.Stretch)
    return t


def _item(text: str, tip: str = "", data: Any = None) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    if tip:
        it.setToolTip(tip)
    if data is not None:
        it.setData(Qt.UserRole, data)
    return it


class NichePanel(QWidget):
    """Each niche at a glance, with Pause / Resume and Find videos now."""

    notice = Signal(str)
    request = Signal(str)  # open New request for this niche id

    def __init__(self, settings: Callable[[], Settings], change: Callable[[str, Any], None] | None,
                 db: JobDB, parent=None):
        super().__init__(parent)
        self._settings, self._change, self.db = settings, change, db
        self.picks = PickLog(picks_path())
        self._sig = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 6, 0, 0)
        self.table = _table(NICHE_COLUMNS)
        self.table.itemSelectionChanged.connect(self._buttons)
        self.table.itemDoubleClicked.connect(lambda _i: self.toggle_pause())
        self.empty = QLabel("No niches yet. Add them in the Niches tab: each one is a TikTok account's topic, with "
                            "its own sources, look and folder. Automation then picks and clips for each in turn.")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setProperty("muted", True)
        row = QHBoxLayout()
        self.pause_btn = QPushButton("❚❚  Pause niche")
        self.pause_btn.setToolTip("Nothing new is picked for it and its waiting videos wait, until you resume it")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.find_btn = QPushButton("🔎  Find videos now")
        self.find_btn.setToolTip("Check this niche's channels and searches at automation's next step, instead of "
                                 "waiting for the next scheduled check")
        self.find_btn.clicked.connect(self.find_now)
        self.ask_btn = QPushButton("＋  Request clips for it…")
        self.ask_btn.clicked.connect(lambda: self.request.emit(self.current() or ""))
        row.addWidget(self.pause_btn)
        row.addWidget(self.find_btn)
        row.addWidget(self.ask_btn)
        row.addStretch(1)
        v.addWidget(self.table, 1)
        v.addWidget(self.empty, 1)
        v.addLayout(row)
        self.refresh()

    def current(self) -> str:
        rows = self.table.selectionModel().selectedRows()
        it = self.table.item(rows[0].row(), 0) if rows else None
        return it.data(Qt.UserRole) if it else ""

    def refresh(self) -> None:
        s = self._settings()
        all_niches = niches.load(s)
        now = time.time()
        self.picks.reload()
        try:
            counts = self.db.niche_counts(now - 86400)
        except Exception:  # jobs.db busy for a moment
            counts = {}
        rows = []
        for n in all_niches:
            c = counts.get(n.id, {})
            status = ("Paused" if n.paused else "Off (Niches tab)" if not n.enabled else "Picking and clipping")
            rows.append((n.id, n.name, n.account, f"{self.picks.count_since(now - 86400, niche=n.id)} of {n.daily}",
                         str(c.get("waiting", 0)), str(c.get("clips", 0)), status, n.paused, n.on))
        if rows == self._sig:
            return
        self._sig = rows
        keep = self.current()
        self.table.setRowCount(len(rows))
        for r, (nid, name, account, picked, waiting, clips, status, paused, on) in enumerate(rows):
            cells = [_item(name, data=nid), _item(account), _item(picked, "Videos picked in the last 24 hours, of "
                                                                         "its daily limit"),
                     _item(waiting), _item(clips, "Clips ready from videos finished in the last 24 hours"),
                     _item(status)]
            cells[-1].setForeground(QColor(theme.WARN if paused else theme.GOOD if on else theme.MUTED))
            for c, cell in enumerate(cells):
                if 2 <= c <= 4:
                    cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, cell)
            if nid == keep or (not keep and r == 0):
                self.table.selectRow(r)
        self.table.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        self._buttons()

    def _buttons(self) -> None:
        n = niches.find(self._settings(), self.current())
        for b in (self.pause_btn, self.find_btn, self.ask_btn):
            b.setEnabled(n is not None)
        self.pause_btn.setText("▶  Resume niche" if n and n.paused else "❚❚  Pause niche")
        self.find_btn.setEnabled(bool(n and n.on and youtube_key(self._settings())))

    def toggle_pause(self) -> None:
        s = self._settings()
        n = niches.find(s, self.current())
        if n is None or self._change is None:
            return
        n.paused = not n.paused
        self._change("niches", niches.to_settings([n if x.id == n.id else x for x in niches.load(s)]))
        self.notice.emit(f"{n.name} is paused: nothing is picked or clipped for it until you resume it."
                         if n.paused else f"{n.name} carries on.")
        self._sig = None
        self.refresh()

    def find_now(self) -> None:
        n = niches.find(self._settings(), self.current())
        if n is None:
            return
        pipeline.request_scan(self.db, n.id)
        alive = pipeline.runner_alive(self.db)
        self.notice.emit(f"Automation checks {n.name}'s sources at its next step."
                         + ("" if alive else " Start automation for that to happen."))


class RequestPanel(QWidget):
    """Clip requests and how far along each one is."""

    notice = Signal(str)
    new = Signal()

    def __init__(self, settings: Callable[[], Settings], db: JobDB, parent=None):
        super().__init__(parent)
        self._settings, self.db = settings, db
        self._sig = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 6, 0, 0)
        self.table = _table(REQUEST_COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(len(REQUEST_COLUMNS) - 1, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._buttons)
        self.empty = QLabel("No requests yet. A request asks automation for a number of clips of a length, for a "
                            "niche, from links you give or from videos it picks, with special requests for the AI.")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setProperty("muted", True)
        row = QHBoxLayout()
        add = QPushButton("＋  New request…")
        add.setObjectName("primary")
        add.clicked.connect(self.new.emit)
        self.cancel_btn = QPushButton("Cancel request")
        self.cancel_btn.setToolTip("Its videos still waiting leave the queue. A video being clipped finishes")
        self.cancel_btn.clicked.connect(self.cancel)
        row.addWidget(add)
        row.addWidget(self.cancel_btn)
        row.addStretch(1)
        v.addWidget(self.table, 1)
        v.addWidget(self.empty, 1)
        v.addLayout(row)
        self.refresh()

    def current(self) -> str:
        rows = self.table.selectionModel().selectedRows()
        it = self.table.item(rows[0].row(), 0) if rows else None
        return it.data(Qt.UserRole) if it else ""

    def refresh(self) -> None:
        try:
            mine = self.db.orders()
        except Exception:
            return
        names = {n.id: n.name for n in niches.load(self._settings())}
        sig = [(o.id, o.status, o.made, o.message, names.get(o.niche, "")) for o in mine]
        if sig == self._sig:
            return
        self._sig = sig
        keep = self.current()
        self.table.setRowCount(len(mine))
        for r, o in enumerate(mine):
            source = "from your links" if o.links else "videos picked for it"
            cells = [_item(f"{o.count} clips of {o.length_text()}, {source}",
                           "\n".join(o.links) or "Automation picks videos for it", o.id),
                     _item(names.get(o.niche, "(deleted niche)" if o.niche else "")),
                     _item(f"{o.made} of {o.count}"),
                     _item(f"{orders.STATUS_WORDS[o.status]}: {o.message}" if o.message else
                           orders.STATUS_WORDS[o.status], o.message),
                     _item(o.notes, o.notes)]
            cells[3].setForeground(QColor(ORDER_COLOURS.get(o.status, theme.TEXT)))
            cells[2].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for c, cell in enumerate(cells):
                self.table.setItem(r, c, cell)
            if o.id == keep:
                self.table.selectRow(r)
        self.table.setVisible(bool(mine))
        self.empty.setVisible(not mine)
        self._buttons()

    def _buttons(self) -> None:
        try:
            o = self.db.get_order(self.current()) if self.current() else None
        except Exception:  # jobs.db busy for a moment: the next refresh sets it
            return
        self.cancel_btn.setEnabled(bool(o and o.is_open))

    def cancel(self) -> None:
        oid = self.current()
        if not oid:
            return
        n = pipeline.Runner(self.db, log=pipeline.file_logger()).cancel(oid)
        self.notice.emit("Request cancelled" + (f"; {n} waiting video{'s' if n != 1 else ''} left the queue." if n
                                                else "."))
        self._sig = None
        self.refresh()


class RequestDialog(QDialog):
    """New request: how many clips, how long, for which niche, from where, and special requests."""

    def __init__(self, settings: Settings, niche_id: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("New clip request")
        self.setMinimumWidth(520)
        self.s = settings
        self.order: orders.Order | None = None
        form = QFormLayout()
        form.setVerticalSpacing(9)
        self.count = QSpinBox()
        self.count.setRange(1, orders.MAX_COUNT)
        self.count.setValue(10)
        self.count.setSuffix(" clips")
        self.count.setToolTip("Clips that pass the quality check count towards it")
        form.addRow("How many", self.count)
        lengths = QWidget()
        row = QHBoxLayout(lengths)
        row.setContentsMargins(0, 0, 0, 0)
        self.min_len, self.max_len = QDoubleSpinBox(), QDoubleSpinBox()
        for box in (self.min_len, self.max_len):
            box.setRange(orders.MIN_LEN, orders.MAX_LEN)
            box.setDecimals(0)
            box.setSingleStep(5)
            box.setSuffix(" s")
        row.addWidget(self.min_len)
        row.addWidget(QLabel("to"))
        row.addWidget(self.max_len)
        row.addStretch(1)
        form.addRow("How long", lengths)
        self.niche = QComboBox(self)
        for n in niches.load(settings):
            self.niche.addItem(n.label + ("  (paused)" if n.paused else ""), n.id)
        self.niche.addItem("No niche", "")
        self.niche.setCurrentIndex(max(0, self.niche.findData(niche_id)))
        self.niche.currentIndexChanged.connect(self._niche_changed)
        if self.niche.count() > 1:
            form.addRow("For", self.niche)
        else:
            self.niche.hide()  # only "No niche": nothing to choose
        self.pick = QRadioButton("Let automation pick videos for it")
        self.from_links = QRadioButton("Clip these videos:")
        self.pick.setChecked(True)
        self.links = QPlainTextEdit()
        self.links.setPlaceholderText("One YouTube link per line")
        self.links.setFixedHeight(70)
        self.links.setEnabled(False)
        self.from_links.toggled.connect(self.links.setEnabled)
        src = QVBoxLayout()
        src.addWidget(self.pick)
        src.addWidget(self.from_links)
        src.addWidget(self.links)
        form.addRow("From", src)
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(80)
        self.notes.setPlaceholderText("Special requests, in plain words. e.g. only the funniest reactions, no "
                                      "swearing in the hook, end on the punchline")
        self.notes.setToolTip("Told to the AI that picks the moments and to the AI that writes hooks and bottom "
                              "text. Needs an Anthropic key (Settings → AI)")
        form.addRow("Special requests", self.notes)
        self.warn = QLabel()
        self.warn.setWordWrap(True)
        self.warn.setTextFormat(Qt.RichText)
        self.error = QLabel()
        self.error.setStyleSheet(f"color:{theme.BAD}")
        self.error.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Request clips")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v = QVBoxLayout(self)
        v.addLayout(form)
        v.addWidget(self.warn)
        v.addWidget(self.error)
        v.addWidget(buttons)
        self.pick.toggled.connect(self._warnings)
        self.notes.textChanged.connect(self._warnings)
        self._niche_changed()

    def _niche_changed(self) -> None:
        job = Job(id="", url="", niche=self.niche.currentData() or "")
        style = pipeline.settings_for(job, self.s)[0]  # the niche's length, else Automation's style
        self.min_len.setValue(style.min_len)
        self.max_len.setValue(style.max_len)
        self._warnings()

    def _warnings(self) -> None:
        lines = []
        if self.notes.toPlainText().strip() and not pipeline.has_ai_key(self.s):
            lines.append("Special requests need an Anthropic key (Settings → AI). Without one, smart signals pick "
                         "the clips and the special requests aren't used.")
        if self.pick.isChecked() and not youtube_key(self.s):
            lines.append("Automation needs a YouTube Data API key (Settings → Research) to pick videos. Give it "
                         "links instead, or add a key.")
        n = niches.find(self.s, self.niche.currentData() or "")
        if n and n.paused:
            lines.append(f"{n.name} is paused, so nothing happens until you resume it (Automation → Niches).")
        self.warn.setText("<br>".join(f"<span style='color:{theme.WARN}'>⚠ {x}</span>" for x in lines))
        self.warn.setVisible(bool(lines))

    def accept(self) -> None:
        links = self.links.toPlainText().split() if self.from_links.isChecked() else []
        if self.from_links.isChecked() and not links:
            self.error.setText("Paste at least one link, or let automation pick videos.")
            return
        try:
            self.order = orders.make(self.count.value(), self.min_len.value(), self.max_len.value(),
                                     niche=self.niche.currentData() or "", links=links,
                                     notes=self.notes.toPlainText())
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        super().accept()

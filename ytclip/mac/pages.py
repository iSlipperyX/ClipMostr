"""The Mac app's other pages: New video (make, clip, ask), Requests, Posted, Categories, This week and Log.

Each page loads what it shows from the PC when it's opened (load()), and again when the window says something
changed. Changes go through the window's session, so a lost connection is shown in one place.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTextBrowser,
                               QVBoxLayout, QWidget)

from ..gui import theme
from . import common as c


class Page(QWidget):
    """A page with a title row and room under it."""

    def __init__(self, window, heading: str, scroll: bool = True, parent=None):
        super().__init__(parent)
        self.window_ = window
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(24, 16, 24, 10)
        head.addWidget(c.title(heading))
        head.addStretch(1)
        self.head = head
        outer.addLayout(head)
        body = QWidget()
        body.setObjectName("page")
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(24, 4, 24, 24)
        self.body.setSpacing(12)
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            area.setWidget(body)
            outer.addWidget(area, 1)
        else:
            outer.addWidget(body, 1)

    @property
    def session(self):
        return self.window_.session

    def call(self, fn, ok=None, fail=None, busy: str = "") -> None:
        s = self.session
        if s is None:
            return
        if busy:
            self.window_.notice(busy)

        def failed(exc):
            self.window_.notice(getattr(exc, "message", str(exc)), bad=True)
            if fail is not None:
                fail(exc)

        s.call(fn, ok, failed)

    def load(self) -> None:
        pass


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setShowGrid(False)
    t.setAlternatingRowColors(False)
    t.setWordWrap(False)
    t.verticalHeader().setDefaultSectionSize(34)
    t.horizontalHeader().setHighlightSections(False)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return t


def _cell(text, tip: str = "", colour: str = "", align=None) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    if tip:
        it.setToolTip(tip)
    if colour:
        from PySide6.QtGui import QColor

        it.setForeground(QColor(colour))
    if align is not None:
        it.setTextAlignment(align)
    return it


class CategoryBox(QComboBox):
    """A category to pick (or none)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumContentsLength(18)
        self.fill([])

    def fill(self, cats: list[dict]) -> None:
        cur = self.currentData()
        self.clear()
        self.addItem("No category", "")
        for x in cats:
            label = x["name"] + (f"  ({x['account']})" if x.get("account") else "")
            if x.get("paused"):
                label += "  · paused"
            self.addItem(label, x["id"])
        i = self.findData(cur)
        self.setCurrentIndex(i if i >= 0 else (1 if cats else 0))


# --- New video ---------------------------------------------------------------------------------------------------


class AskPage(Page):
    def __init__(self, window, parent=None):
        super().__init__(window, "New video", parent=parent)
        intro = c.WrapLabel("Ask the PC for something new. It's queued in automation, and what's made shows up in "
                            "Videos (a Creator video waits there for your OK if the PC is set to hold them).", muted=True)
        self.body.addWidget(intro)
        grid = QGridLayout()
        grid.setSpacing(12)

        make, mv = c.panel(16, 10)
        mv.addWidget(c.h2("✨  Make a video about an idea"))
        mv.addWidget(c.WrapLabel("The Video Creator writes it, voices it and puts pictures to it on the PC.", muted=True))
        self.idea = QPlainTextEdit()
        self.idea.setPlaceholderText("the real story of the Bermuda Triangle")
        self.idea.setFixedHeight(80)
        mv.addWidget(self.idea)
        self.make_cat = CategoryBox()
        self.make_btn = QPushButton("Make it")
        self.make_btn.setObjectName("primary")
        self.make_btn.clicked.connect(self._make)
        mv.addLayout(c.row(QLabel("For"), self.make_cat, stretch_after=True))
        mv.addLayout(c.row(self.make_btn))
        mv.addStretch(1)

        clip, cv = c.panel(16, 10)
        cv.addWidget(c.h2("✂  Clip YouTube videos"))
        cv.addWidget(c.WrapLabel("Paste one link or several. Each video is cut into its best clips.", muted=True))
        self.links = QPlainTextEdit()
        self.links.setPlaceholderText("https://www.youtube.com/watch?v=…")
        self.links.setFixedHeight(80)
        cv.addWidget(self.links)
        self.clip_cat = CategoryBox()
        self.clip_btn = QPushButton("Clip them")
        self.clip_btn.setObjectName("primary")
        self.clip_btn.clicked.connect(self._clip)
        cv.addLayout(c.row(QLabel("For"), self.clip_cat))
        cv.addLayout(c.row(self.clip_btn))
        cv.addStretch(1)

        req, rv = c.panel(16, 10)
        rv.addWidget(c.h2("📋  Ask for clips"))
        rv.addWidget(c.WrapLabel("How many clips, how long, and anything special for the AI. With no links, "
                                 "automation picks the videos for the category.", muted=True))
        self.count = QSpinBox()
        self.count.setRange(1, 100)
        self.count.setValue(10)
        self.min_len = QDoubleSpinBox()
        self.max_len = QDoubleSpinBox()
        for sp, val in ((self.min_len, 30), (self.max_len, 45)):
            sp.setRange(0, 600)
            sp.setDecimals(0)
            sp.setSuffix(" s")
            sp.setValue(val)
        self.own_len = QCheckBox("Length")
        self.own_len.setToolTip("Off: the category's own clip length")
        self.own_len.setChecked(True)
        self.own_len.toggled.connect(lambda on: (self.min_len.setEnabled(on), self.max_len.setEnabled(on)))
        self.req_cat = CategoryBox()
        self.req_links = QLineEdit()
        self.req_links.setPlaceholderText("Links (optional)")
        self.notes = QLineEdit()
        self.notes.setPlaceholderText("Special requests, like “only clutch plays”")
        self.req_btn = QPushButton("Ask for them")
        self.req_btn.setObjectName("primary")
        self.req_btn.clicked.connect(self._request)
        rv.addLayout(c.row(self.count, QLabel("clips"), 12, self.own_len, self.min_len, QLabel("to"), self.max_len))
        rv.addLayout(c.row(QLabel("For"), self.req_cat))
        rv.addWidget(self.req_links)
        rv.addWidget(self.notes)
        rv.addLayout(c.row(self.req_btn))
        rv.addStretch(1)

        grid.addWidget(make, 0, 0)
        grid.addWidget(clip, 0, 1)
        grid.addWidget(req, 1, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.body.addLayout(grid)
        self.result = c.WrapLabel("")
        self.body.addWidget(self.result)
        self.body.addStretch(1)

    def load(self) -> None:
        self.call(lambda cl: cl.get("/api/categories"), self._cats)

    def _cats(self, got: dict) -> None:
        cats = got.get("items") or []
        for box in (self.make_cat, self.clip_cat, self.req_cat):
            box.fill(cats)

    def _said(self, text: str, good: bool = True) -> None:
        colour = theme.GOOD if good else theme.BAD
        self.result.setText(f"<span style='color:{colour}'>{'✓' if good else '✗'}</span> {c.esc(text)}")
        self.window_.notice(text, good=good, bad=not good)

    def _make(self) -> None:
        idea = " ".join(self.idea.toPlainText().split())
        if not idea:
            self._said("Say what the video should be about.", False)
            return
        self.make_btn.setEnabled(False)

        def ok(out):
            self.make_btn.setEnabled(True)
            self.idea.clear()
            self._said(out.get("message") or "Queued.")
            self.window_.refresh()

        self.call(lambda cl: cl.post("/api/make", {"idea": idea, "category": self.make_cat.currentData()}), ok,
                  lambda e: self.make_btn.setEnabled(True), busy="Asking the PC…")

    def _clip(self) -> None:
        links = self.links.toPlainText().split()
        if not links:
            self._said("Paste a YouTube link.", False)
            return
        self.clip_btn.setEnabled(False)

        def ok(out):
            self.clip_btn.setEnabled(True)
            if out.get("added"):
                self.links.clear()
            self._said(out.get("message") or "Queued.", bool(out.get("added")))
            self.window_.refresh()

        self.call(lambda cl: cl.post("/api/clip", {"links": links, "category": self.clip_cat.currentData()}), ok,
                  lambda e: self.clip_btn.setEnabled(True), busy="Asking the PC…")

    def _request(self) -> None:
        body = {"count": self.count.value(), "category": self.req_cat.currentData(),
                "links": self.req_links.text().split(), "notes": self.notes.text().strip()}
        if self.own_len.isChecked():
            lo, hi = sorted((self.min_len.value(), self.max_len.value()))
            body.update(min_len=lo, max_len=hi)
        self.req_btn.setEnabled(False)

        def ok(out):
            self.req_btn.setEnabled(True)
            self.req_links.clear()
            self.notes.clear()
            self._said(out.get("message") or "Asked.")
            self.window_.refresh()

        self.call(lambda cl: cl.post("/api/requests", body), ok, lambda e: self.req_btn.setEnabled(True),
                  busy="Asking the PC…")


# --- Requests ---------------------------------------------------------------------------------------------------


class RequestsPage(Page):
    def __init__(self, window, parent=None):
        super().__init__(window, "Requests", parent=parent)
        self.body.addWidget(c.WrapLabel("Your requests for clips and how far along each one is.", muted=True))
        self.list = QVBoxLayout()
        self.list.setSpacing(8)
        self.body.addLayout(self.list)
        self.body.addStretch(1)

    def load(self) -> None:
        self.call(lambda cl: cl.get("/api/requests"), self._show)

    def _show(self, got: dict) -> None:
        c.clear(self.list)
        items = got.get("items") or []
        if not items:
            self.list.addWidget(c.WrapLabel("No requests yet. Ask for clips on New video.", muted=True))
            return
        marks = {"open": ("⏳", theme.ACCENT), "done": ("✓", theme.GOOD), "short": ("⚠", theme.WARN),
                 "cancelled": ("✖", theme.MUTED)}
        for o in items:
            f, v = c.panel(14, 6)
            mark, colour = marks.get(o.get("status"), ("•", theme.MUTED))
            top = QHBoxLayout()
            head = c.WrapLabel(f"<b>{c.esc(o.get('title'))}</b>")
            chip = c.Chip(f"{mark} {o.get('status_text')}", colour)
            top.addWidget(head, 1)
            top.addWidget(chip, 0, Qt.AlignTop)
            v.addLayout(top)
            if o.get("notes"):
                v.addWidget(c.WrapLabel(f"“{c.esc(c.trim(o['notes'], 200))}”", muted=True))
            bar = QProgressBar()
            bar.setRange(0, max(1, int(o.get("count") or 1)))
            bar.setValue(min(int(o.get("made") or 0), int(o.get("count") or 1)))
            bar.setFormat(f"{o.get('made', 0)} of {o.get('count', 0)} clips")
            bar.setTextVisible(True)
            bar.setFixedHeight(18)
            line = QHBoxLayout()
            line.addWidget(bar, 1)
            if o.get("can_cancel"):
                btn = QPushButton("Cancel")
                btn.clicked.connect(lambda _=False, o=o: self._cancel(o))
                line.addWidget(btn)
            v.addLayout(line)
            meta = " · ".join(b for b in (o.get("message"), f"asked {c.ago(o.get('created'))}") if b)
            v.addWidget(c.WrapLabel(c.esc(meta), muted=True))
            self.list.addWidget(f)

    def _cancel(self, o: dict) -> None:
        ask = QMessageBox(QMessageBox.Question, "Cancel this request?",
                          f"Cancel the request for {o.get('title')}? Its videos still waiting leave the queue (one "
                          "being clipped finishes).", QMessageBox.No, self)
        go = ask.addButton("Cancel the request", QMessageBox.DestructiveRole)
        ask.exec()
        if ask.clickedButton() is go:
            self.call(lambda cl: cl.post(f"/api/requests/{o['id']}/cancel"),
                      lambda out: (self.window_.notice(out.get("message", "Cancelled."), good=True), self.load()))


# --- Posted -------------------------------------------------------------------------------------------------------


class PostedPage(Page):
    def __init__(self, window, parent=None):
        super().__init__(window, "Posted", scroll=False, parent=parent)
        self.body.addWidget(c.WrapLabel("The latest posts and how they're doing (the PC reads TikTok's numbers every "
                                        "few minutes). Double-click one to open it on TikTok.", muted=True))
        self.table = _table(["Posted", "Video", "Account", "Views", "Likes", "Comments", "Shares"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for i in (0, 2, 3, 4, 5, 6):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._open)
        self.body.addWidget(self.table, 1)
        self.items: list[dict] = []

    def load(self) -> None:
        self.call(lambda cl: cl.get("/api/posted", limit=100), self._show)

    def _show(self, got: dict) -> None:
        self.items = got.get("items") or []
        self.table.setRowCount(len(self.items))
        now = time.time()
        right = Qt.AlignRight | Qt.AlignVCenter
        for r, v in enumerate(self.items):
            st = v.get("stats") or {}
            self.table.setItem(r, 0, _cell(c.when(v.get("posted_at"), now)))
            self.table.setItem(r, 1, _cell(v.get("headline"), v.get("caption") or ""))
            self.table.setItem(r, 2, _cell(v.get("account_handle")))
            for col, key in ((3, "views"), (4, "likes"), (5, "comments"), (6, "shares")):
                self.table.setItem(r, col, _cell(c.number(st.get(key)) if st else "–", align=right))
        if not self.items:
            self.table.setRowCount(0)

    def _open(self, row: int, _col: int) -> None:
        if 0 <= row < len(self.items) and self.items[row].get("tiktok_url"):
            QDesktopServices.openUrl(QUrl(self.items[row]["tiktok_url"]))


# --- Categories -----------------------------------------------------------------------------------------------------


class CategoriesPage(Page):
    def __init__(self, window, parent=None):
        super().__init__(window, "Categories", parent=parent)
        self.body.addWidget(c.WrapLabel("Each category picks videos for one TikTok account. Pause one to stop new "
                                        "videos for it (posting what's queued carries on); Find videos asks "
                                        "automation to check its sources now. Change them on the PC.", muted=True))
        self.list = QVBoxLayout()
        self.list.setSpacing(8)
        self.body.addLayout(self.list)
        self.body.addStretch(1)

    def load(self) -> None:
        self.call(lambda cl: cl.get("/api/categories"), self._show)

    def _show(self, got: dict) -> None:
        c.clear(self.list)
        items = got.get("items") or []
        if not items:
            self.list.addWidget(c.WrapLabel("No categories yet: automation clips for the Automation folder. Add "
                                            "categories in the Categories tab on the PC.", muted=True))
            return
        for x in items:
            f, v = c.panel(14, 6)
            top = QHBoxLayout()
            name = c.WrapLabel(f"<b>{c.esc(x['name'])}</b>" + (f"  <span style='color:{theme.MUTED}'>"
                                                               f"{c.esc(x['account'])}</span>" if x.get("account") else ""))
            if x.get("on"):
                chip = c.Chip("● Picking", theme.GOOD)
            elif x.get("paused"):
                chip = c.Chip("❚❚ Paused", theme.WARN)
            else:
                chip = c.Chip("Off", theme.MUTED)
            top.addWidget(name, 1)
            top.addWidget(chip)
            v.addLayout(top)
            bits = [f"{x.get('daily', 0)} videos a day"]
            if x.get("creator_daily"):
                bits.append(f"{x['creator_daily']} Creator videos a day")
            if x.get("min_len") and x.get("max_len"):
                bits.append(f"clips {x['min_len']:g}–{x['max_len']:g} s")
            bits.append(f"{x.get('waiting', 0)} waiting to clip")
            bits.append(f"{x.get('queued', 0)} queued to post")
            v.addWidget(c.WrapLabel(c.esc(" · ".join(bits)), muted=True))
            if x.get("state") and not x.get("paused"):
                v.addWidget(c.WrapLabel(f"<span style='color:{theme.WARN}'>{c.esc(x['state'])}</span>"))
            pause = QPushButton("▶  Resume" if x.get("paused") else "❚❚  Pause")
            pause.clicked.connect(lambda _=False, x=x: self._pause(x))
            find = QPushButton("🔎  Find videos now")
            find.setEnabled(bool(x.get("on")))
            find.clicked.connect(lambda _=False, x=x: self.call(
                lambda cl: cl.post("/api/scan", {"category": x["id"]}),
                lambda out: self.window_.notice(out.get("message", ""), good=True)))
            v.addLayout(c.row(pause, find))
            self.list.addWidget(f)

    def _pause(self, x: dict) -> None:
        self.call(lambda cl: cl.post(f"/api/categories/{x['id']}", {"paused": not x.get("paused")}),
                  lambda out: (self.window_.notice(out.get("message", ""), good=True), self.load()))


# --- This week ------------------------------------------------------------------------------------------------------


class WeekPage(Page):
    def __init__(self, window, parent=None):
        super().__init__(window, "This week", scroll=False, parent=parent)
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(True)
        self.text.setFrameShape(QFrame.NoFrame)
        self.text.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        self.body.addWidget(self.text, 1)

    def load(self) -> None:
        self.text.setHtml(f"<p style='color:{theme.MUTED}'>Reading the week from the PC…</p>")
        self.call(lambda cl: cl.get("/api/week"), self._show)

    def _show(self, w: dict) -> None:
        out = [f"<h2 style='margin:0'>Your week, {c.esc(w.get('span'))}</h2>",
               f"<p style='font-size:12pt'>{c.esc(w.get('headline'))}</p>"]
        for s in w.get("sections") or []:
            out.append(f"<h3 style='margin-bottom:4px'>{c.esc(s.get('heading'))}</h3><ul style='margin-top:0'>")
            for line in s.get("lines") or []:
                text = c.esc(line.get("text"))
                if line.get("link"):
                    text = f"<a href='{c.esc(line['link'])}' style='color:{theme.ACCENT_2}'>{text}</a>"
                out.append(f"<li style='margin-bottom:3px'>{text}</li>")
            out.append("</ul>")
        self.text.setHtml("".join(out))


# --- Log -----------------------------------------------------------------------------------------------------------


class LogPage(Page):
    def __init__(self, window, parent=None):
        super().__init__(window, "Log", scroll=False, parent=parent)
        self.follow = QCheckBox("Keep up to date")
        self.follow.setChecked(True)
        self.head.addWidget(self.follow)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSizeF(max(9.0, mono.pointSizeF()))
        self.text.setFont(mono)
        self.body.addWidget(c.WrapLabel("Automation's log on the PC, newest at the bottom.", muted=True))
        self.body.addWidget(self.text, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self._tick)
        self._last = None

    def load(self) -> None:
        self.timer.start()
        self.call(lambda cl: cl.get("/api/log", lines=600), self._show)

    def _tick(self) -> None:
        if not self.isVisible():
            self.timer.stop()
            return
        if self.follow.isChecked():
            self.call(lambda cl: cl.get("/api/log", lines=600), self._show)

    def _show(self, got: dict) -> None:
        lines = got.get("lines") or []
        if lines == self._last:
            return
        self._last = lines
        bar = self.text.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self.text.setPlainText("\n".join(lines) or "The log is empty.")
        if at_end or self.follow.isChecked():
            bar.setValue(bar.maximum())

"""The Sports page: pick the games to clip, see each game's best highlights ranked, and make the ones you want.

Three columns: on the left the filter (sport, league, teams, when, what footage, which category the clips are
for) and Find games; in the middle the games found, each with a tick to pick it; on the right the game you're
looking at, with its plays ranked best first (each with a picture, the play, a title, why, and its AI and
crowd scores), ticks for the ones to make, and Make clips.

The page is the same on the PC and in ClipMonster for Mac. It talks to a backend: on the PC, LocalBackend asks
this PC's desk (sportsdesk/desk.py) directly; on a Mac, mac/sports_page.py's backend asks the linked PC's, over
the link. Either way the work (searching, downloading, listening, the AI's ranking, exporting) is done on the PC,
and the page asks how it's going every second or two while it's on screen.
"""

from __future__ import annotations

import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
                               QStackedWidget, QVBoxLayout, QWidget)

from .. import sports
from ..sportsdesk import games as G
from . import theme

EVENT_HELP = ("Describe a moment and the game you're looking at (else every picked game) is searched for it:\n"
              "“red card or player ejection”, “missed penalty”, “technical foul on the bench”,\n"
              "“game-tying shot in the final minute”, “controversial pass interference in the 4th quarter”.\n"
              "It reads the commentary and, when there is one, the game's official play-by-play.\n"
              "What it finds joins the game's plays, ticked, ready for Make clips or Make reel.")
POLL_MS = 1500  # how often the page asks how things are going while it's showing
SLOW_POLL_MS = 6000  # ... while nothing is being worked on
EDIT_HOLD = 4.0  # s after you change the filter before the PC's copy of it is shown in its place
PICTURES_KEPT = 400
PLAYED_STALE = 180.0  # s before the games played are read from the scoreboards again while they're showing

SINCE_SHORT = {"day": "24 h", "3days": "3 days", "week": "Week", "month": "Month", "any": "Any"}
STATUS_WORDS = {"found": "", "queued": "Waiting", "analysing": "Finding highlights", "making": "Making",
                "ready": "Highlights ready", "failed": "Failed"}


def _plural(n: int, word: str, many: str = "") -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def events_text(g: dict) -> str:
    """What the last event search of this game found, and its Clip-Ability index, in a line or two."""
    lines = []
    ev = g.get("events") or {}
    if ev.get("query"):
        n = int(ev.get("found") or 0)
        how = " + ".join(x for x in (ev.get("feed") or "", "the commentary") if x)
        line = f"🔎 “{ev['query']}”: {_plural(n, 'moment')} found" if n else f"🔎 “{ev['query']}”: nothing found"
        line += f" (read as {ev.get('describe')}; from {how}; {ev.get('reader') or 'words'})"
        notes = " ".join(ev.get("notes") or [])
        lines.append(line + (f". {notes}" if notes else "."))
    ix = g.get("index") or {}
    if ix.get("parts"):
        lines.append(f"Clip-Ability {ix.get('score', 0):.0f}/100" + (": " + " ".join(ix.get("reasons") or [])
                                                                       if ix.get("reasons") else ""))
    return "\n".join(lines)


def index_tip(g: dict) -> str:
    from ..clipability import SPORTS_LABELS, SPORTS_WEIGHTS

    ix = g.get("index") or {}
    if not ix.get("parts"):
        return ""
    rows = [f"{SPORTS_LABELS[k]} ({SPORTS_WEIGHTS[k]:.0%}): " + (f"{ix['parts'][k] * 100:.0f}" if k in ix["parts"]
                                                                  else "not measured") for k in SPORTS_WEIGHTS]
    return "Clip-Ability index\n" + "\n".join(rows)


def clock(seconds: float | None) -> str:
    s = int(round(seconds or 0))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return ""
    d = max(0.0, (now or time.time()) - ts)
    if d < 3600:
        return "just now" if d < 120 else f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    days = int(d // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def views_text(n: int | None) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M views".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.0f}K views"
    return f"{n} views" if n else ""


def watch_url(game: dict, at: float = 0.0) -> str:
    url = game.get("url") or f"https://www.youtube.com/watch?v={game.get('video')}"
    return f"{url}&t={int(max(0, at))}s" if at else url


# ---------------------------------------------------------------- off the window's thread


class Calls(QObject):
    """Run a function on a worker thread; its result (or error) comes back on the window's thread."""

    _done = Signal(object, object, object, object)

    def __init__(self, workers: int = 4, parent=None):
        super().__init__(parent)
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sports")
        self._done.connect(self._deliver)

    def run(self, fn: Callable, ok: Callable | None = None, fail: Callable | None = None) -> None:
        def work():
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - handed to `fail`
                self._done.emit(ok, fail, None, exc)
                return
            self._done.emit(ok, fail, result, None)

        self.pool.submit(work)

    @staticmethod
    def _deliver(ok, fail, result, error) -> None:
        if error is not None:
            if fail is not None:
                fail(error)
        elif ok is not None:
            ok(result)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


class Backend(QObject):
    """What the page asks. `call(name, *args)` runs one of the desk's methods (sportsdesk/desk.py names them:
    state, set_filter, find_games, pick, analyse, choose, make, version, remove, clear, cancel) and hands its
    answer to `ok`, or the reason it failed to `fail`."""

    where = "this PC"  # where the work is done, for the page's words
    local = True  # the PC's own window: its folders can be opened here

    def call(self, name: str, *args, ok: Callable | None = None, fail: Callable | None = None) -> None:
        raise NotImplementedError

    def picture(self, game_id: str, highlight_id: str) -> bytes | None:
        """A highlight's picture (blocking: called off the window's thread)."""
        return None

    def available(self) -> bool:
        return True


class LocalBackend(Backend):
    """This PC's desk, asked directly."""

    def __init__(self, desk, parent=None):
        super().__init__(parent)
        self.desk = desk
        self.calls = Calls(parent=self)

    def call(self, name: str, *args, ok=None, fail=None) -> None:
        fn = getattr(self.desk, name)
        self.calls.run(lambda: fn(*args), ok, fail)

    def picture(self, game_id: str, highlight_id: str) -> bytes | None:
        try:
            return self.desk.thumb(game_id, highlight_id).read_bytes()
        except Exception:  # noqa: BLE001
            return None


def error_text(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc) or exc.__class__.__name__


# ---------------------------------------------------------------- pictures


class Pictures(QObject):
    """Game thumbnails (from YouTube) and highlight pictures (from the PC), loaded off the window's thread and kept
    for the page's life. `ready(key)` says one arrived."""

    ready = Signal(str)

    def __init__(self, backend: Backend, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.calls = Calls(workers=4, parent=self)
        self.cache: dict[str, QPixmap] = {}
        self.asked: set[str] = set()

    def get(self, key: str) -> QPixmap | None:
        return self.cache.get(key)

    def want_url(self, url: str) -> None:
        if not url or url in self.cache or url in self.asked or not url.startswith("https://"):
            return
        self.asked.add(url)

        def fetch() -> bytes:
            req = urllib.request.Request(url, headers={"User-Agent": "ClipMonster"})
            with urllib.request.urlopen(req, timeout=12) as r:
                return r.read(2_000_000)

        self.calls.run(fetch, lambda data, k=url: self._got(k, data), lambda _e: None)

    def want_highlight(self, game_id: str, highlight_id: str, version: str = "") -> str:
        key = f"hl:{game_id}:{highlight_id}:{version}"
        if key not in self.cache and key not in self.asked:
            self.asked.add(key)
            self.calls.run(lambda: self.backend.picture(game_id, highlight_id),
                           lambda data, k=key: self._got(k, data), lambda _e: None)
        return key

    def _got(self, key: str, data: bytes | None) -> None:
        if not data:
            return
        pm = QPixmap()
        if pm.loadFromData(data):
            if len(self.cache) > PICTURES_KEPT:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = pm
            self.ready.emit(key)

    def shutdown(self) -> None:
        self.calls.shutdown()


class Picture(QWidget):
    """A rounded picture that fills its box (cropped, never stretched), or a dim box with a mark until it loads."""

    def __init__(self, w: int, h: int, mark: str = "▶", parent=None):
        super().__init__(parent)
        self.setFixedSize(w, h)
        self.pm: QPixmap | None = None
        self.mark = mark
        self.badge = ""

    def set(self, pm: QPixmap | None, badge: str = "") -> None:
        self.pm = pm
        self.badge = badge
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(r, 7, 7)
        p.setClipPath(path)
        p.fillRect(self.rect(), QColor(theme.PANEL_2))
        if self.pm is not None and not self.pm.isNull():
            ratio = self.devicePixelRatioF()
            scaled = self.pm.scaled(self.size() * ratio, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio)
            sw, sh = scaled.width() / ratio, scaled.height() / ratio
            p.drawPixmap(int((self.width() - sw) / 2), int((self.height() - sh) / 2), scaled)
        else:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, self.mark)
        if self.badge:
            f = p.font()
            f.setPointSizeF(7.5)
            f.setBold(True)
            p.setFont(f)
            w = p.fontMetrics().horizontalAdvance(self.badge) + 10
            box = QRectF(self.width() - w - 4, self.height() - 18, w, 15)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 175))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(box, Qt.AlignCenter, self.badge)
        p.end()


def chip(text: str = "", colour: str = "") -> QLabel:
    lab = QLabel()
    set_chip(lab, text, colour)
    return lab


class Elided(QLabel):
    """One line that ends in "…" when it doesn't fit, its whole text in its tooltip, never widening its row."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.full = ""
        self.tip = ""
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(30)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self.full = text or ""
        self._fit()

    def text(self) -> str:
        return self.full

    def setToolTip(self, tip: str) -> None:  # noqa: N802
        self.tip = tip or ""
        self._fit()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._fit()

    def _fit(self) -> None:
        shown = self.fontMetrics().elidedText(self.full, Qt.ElideRight, max(10, self.width()))
        QLabel.setText(self, shown)
        whole = self.full if shown != self.full and self.full != self.tip else ""
        QLabel.setToolTip(self, "\n\n".join(t for t in (whole, self.tip) if t))


def set_chip(lab: QLabel, text: str, colour: str = "") -> None:
    c = QColor(colour or theme.MUTED)
    lab.setText(text)
    lab.setStyleSheet(f"QLabel {{ color: {c.name()}; background: rgba({c.red()},{c.green()},{c.blue()},0.16);"
                      " border-radius: 8px; padding: 1px 8px; font-weight: 600; font-size: 8.5pt; }")
    lab.ensurePolished()  # measured in its own (bold) font, not the plain one
    lab.setVisible(bool(text))
    lab.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)  # never squeezed into "Finding hig…"


def muted(text: str = "", wrap: bool = True) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("muted", True)
    lab.setWordWrap(wrap)
    return lab


def h2(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("h2")
    return lab


def status_colour(status: str) -> str:
    return {"ready": theme.GOOD, "failed": theme.BAD, "analysing": theme.ACCENT, "making": theme.ACCENT,
            "queued": theme.WARN}.get(status, theme.MUTED)


# ---------------------------------------------------------------- the filter


class FilterPanel(QFrame):
    """Which games: changes are saved a moment after you make them (`changed`), Find games looks (`find`)."""

    changed = Signal(dict)
    find = Signal(dict)
    stop = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self._filling = False
        self.edited_at = 0.0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)
        area.setWidget(body)
        outer.addWidget(area)

        v.addWidget(h2("Which games"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.sport = QComboBox()
        for sid, label in sports.choices():
            self.sport.addItem(label, sid)
        self.sport.setToolTip("Each sport has its own big calls and play lengths: a dunk is shorter than a goal's "
                              "build-up")
        form.addRow("Sport", self.sport)
        self.league = QComboBox()
        self.league.setEditable(True)
        self.league.setInsertPolicy(QComboBox.NoInsert)
        self.league.lineEdit().setPlaceholderText("Any league")
        self.league.setToolTip("The competition: pick one or type any (\"Champions League\", \"March Madness\")")
        form.addRow("League", self.league)
        self.teams = QLineEdit()
        self.teams.setPlaceholderText("Every team")
        self.teams.setToolTip("Games with any of these teams or players, separated by commas: Lakers, Celtics")
        form.addRow("Teams", self.teams)
        v.addLayout(form)

        v.addWidget(muted("Played in the last"))
        seg = QFrame()
        seg.setObjectName("segbar")
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(3, 3, 3, 3)
        sh.setSpacing(2)
        self.since = QButtonGroup(self)
        self.since_buttons: dict[str, QPushButton] = {}
        for sid, label, _days in G.SINCE:
            b = QPushButton(SINCE_SHORT.get(sid, label))
            b.setToolTip("Any time" if sid == "any" else f"Played (uploaded) in the last {label.lower()}")
            b.setMinimumWidth(10)
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            b.setStyleSheet("QPushButton { padding: 4px 2px; }")
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setProperty("since", sid)
            self.since.addButton(b)
            self.since_buttons[sid] = b
            sh.addWidget(b, len(b.text()) + 3)  # "3 days" gets more room than "Any"
        v.addWidget(seg)

        v.addWidget(muted("Footage"))
        self.kinds: dict[str, QCheckBox] = {}
        tips = {"full": "Whole games (40 min and up): every play, but long to download and type out",
                "extended": "Extended or condensed highlights (7-45 min): the plays without the slow bits. The best "
                            "place to start",
                "highlights": "Short recaps (2-16 min): the biggest plays, already picked by the channel"}
        for kid, label, *_ in G.KINDS:
            box = QCheckBox(label)
            box.setToolTip(tips[kid])
            self.kinds[kid] = box
            v.addWidget(box)

        more = QFormLayout()
        more.setContentsMargins(0, 4, 0, 0)
        more.setHorizontalSpacing(10)
        more.setVerticalSpacing(8)
        more.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        more.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.channels = QLineEdit()
        self.channels.setPlaceholderText("Any channel")
        self.channels.setToolTip("Only uploads from these channels, separated by commas (a league's own channel, "
                                 "say)")
        more.addRow("Channels", self.channels)
        self.category = QComboBox()
        self.category.setToolTip("The category the clips are made for: its folder, its look and its TikTok account "
                                 "(a sports category also sends them to your Macs)")
        more.addRow("Clips for", self.category)
        self.per_game = QSpinBox()
        self.per_game.setRange(1, 20)
        self.per_game.setSuffix(" per game")
        self.per_game.setToolTip("How many of each game's best highlights are ticked to make, to start with")
        more.addRow("Best", self.per_game)
        v.addLayout(more)

        v.addWidget(muted("Recap reel"))
        reel = QFormLayout()
        reel.setContentsMargins(0, 0, 0, 0)
        reel.setHorizontalSpacing(10)
        reel.setVerticalSpacing(8)
        reel.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.reel_voice = QComboBox()
        self.reel_voice.addItem("No narrator", "off")
        from ..creator.voice_engine import VOICES

        for key, prof in VOICES.items():
            self.reel_voice.addItem(prof.label, key)
            self.reel_voice.setItemData(self.reel_voice.count() - 1, prof.blurb, Qt.ToolTipRole)
        self.reel_voice.setToolTip("The voice that opens a reel with two sentences on the game, written by the AI "
                                   "(the Video Creator's voices). A narrated reel is labelled AI-generated when it "
                                   "posts")
        reel.addRow("Narrator", self.reel_voice)
        self.reel_framing = QComboBox()
        for key, label in G.REEL_FRAMING.items():
            self.reel_framing.addItem(label, key)
        self.reel_framing.setToolTip("Follow the action: a 9:16 window that moves with the play, filling the "
                                     "screen.\nWhole picture: all of the 16:9 broadcast, over a blurred fill of "
                                     "itself")
        reel.addRow("Framing", self.reel_framing)
        self.reel_order = QComboBox()
        for key, label in G.REEL_ORDER.items():
            self.reel_order.addItem(label, key)
        self.reel_order.setToolTip("Countdown: the plays build to the best one, labelled PLAY 4, PLAY 3... "
                                   "NUMBER 1.\nAs it happened: in the order of the game.\nAs it happened, the "
                                   "best last: the game's story, with its biggest play held back for the end")
        reel.addRow("Order", self.reel_order)
        v.addLayout(reel)

        row = QHBoxLayout()
        self.find_btn = QPushButton("Find games")
        self.find_btn.setObjectName("primary")
        self.find_btn.clicked.connect(lambda: self.find.emit(self.values()))
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop.emit)
        self.stop_btn.hide()
        row.addWidget(self.find_btn, 1)
        row.addWidget(self.stop_btn)
        v.addLayout(row)
        self.search_note = muted("")
        v.addWidget(self.search_note)
        self.youtube_note = muted("")
        v.addWidget(self.youtube_note)
        v.addStretch(1)
        warn = muted("League footage is often claimed for copyright on TikTok. Clips that only use a few seconds and "
                     "add something of your own fare best.")
        warn.setStyleSheet("font-size: 8.5pt;")
        v.addWidget(warn)

        self._timer = QTimer(self, singleShot=True, interval=600)
        self._timer.timeout.connect(lambda: self.changed.emit(self.values()))
        self.sport.currentIndexChanged.connect(self._sport_changed)
        self.league.currentTextChanged.connect(self._edited)
        self.teams.textEdited.connect(self._edited)
        self.channels.textEdited.connect(self._edited)
        self.since.buttonClicked.connect(self._edited)
        for box in self.kinds.values():
            box.toggled.connect(self._kind_toggled)
        self.category.currentIndexChanged.connect(self._edited)
        self.per_game.valueChanged.connect(self._edited)
        for box in (self.reel_voice, self.reel_framing, self.reel_order):
            box.currentIndexChanged.connect(self._edited)
        self._leagues(self.sport.currentData())

    def _leagues(self, sport_id: str) -> None:
        text = self.league.currentText()
        self.league.blockSignals(True)
        self.league.clear()
        self.league.addItem("")
        for name in G.LEAGUES.get(sport_id or "any", ()):
            self.league.addItem(name)
        self.league.setEditText(text)
        self.league.blockSignals(False)

    def _sport_changed(self) -> None:
        if self._filling:
            return
        old = self.league.currentText()
        self._leagues(self.sport.currentData())
        if old and old not in G.LEAGUES.get(self.sport.currentData() or "any", ()):  # last sport's league
            if any(old in names for names in G.LEAGUES.values()):
                self.league.setEditText("")
        self._edited()

    def _kind_toggled(self, _on: bool) -> None:
        if not any(b.isChecked() for b in self.kinds.values()):  # at least one kind of footage
            sender = self.sender()
            if isinstance(sender, QCheckBox):
                sender.blockSignals(True)
                sender.setChecked(True)
                sender.blockSignals(False)
            return
        self._edited()

    def _edited(self, *_a) -> None:
        if self._filling:
            return
        self.edited_at = time.monotonic()
        self._timer.start()

    def values(self) -> dict:
        since = next((sid for sid, b in self.since_buttons.items() if b.isChecked()), "3days")
        return {"sport": self.sport.currentData() or "any", "league": self.league.currentText().strip(),
                "teams": self.teams.text(), "since": since,
                "kinds": [k for k, b in self.kinds.items() if b.isChecked()], "channels": self.channels.text(),
                "category_id": self.category.currentData() or "", "per_game": self.per_game.value(),
                "reel_voice": self.reel_voice.currentData() or "off",
                "reel_framing": self.reel_framing.currentData() or "follow",
                "reel_order": self.reel_order.currentData() or "countdown"}

    def recently_edited(self) -> bool:
        return self._timer.isActive() or time.monotonic() - self.edited_at < EDIT_HOLD

    def fill(self, f: dict, categories: list[dict]) -> None:
        """Show `f` (unless you changed it a moment ago) and the categories to pick from."""
        self._filling = True
        try:
            self._fill_categories(categories, f.get("category_id", ""))
            if self.recently_edited():
                return
            i = self.sport.findData(f.get("sport", "any"))
            self.sport.setCurrentIndex(max(0, i))
            self._leagues(self.sport.currentData())
            self.league.setEditText(f.get("league", ""))
            if self.teams.text() != f.get("teams", "") and not self.teams.hasFocus():
                self.teams.setText(f.get("teams", ""))
            if self.channels.text() != f.get("channels", "") and not self.channels.hasFocus():
                self.channels.setText(f.get("channels", ""))
            b = self.since_buttons.get(f.get("since", "3days")) or self.since_buttons["3days"]
            b.setChecked(True)
            for k, box in self.kinds.items():
                box.blockSignals(True)
                box.setChecked(k in (f.get("kinds") or []))
                box.blockSignals(False)
            self.per_game.setValue(int(f.get("per_game") or 5))
            i = self.category.findData(f.get("category_id", ""))
            self.category.setCurrentIndex(max(0, i))
            for box, key, default in ((self.reel_voice, "reel_voice", "en_storyteller"),
                                      (self.reel_framing, "reel_framing", "follow"),
                                      (self.reel_order, "reel_order", "countdown")):
                i = box.findData(f.get(key) or default)
                box.setCurrentIndex(max(0, i))
        finally:
            self._filling = False

    def _fill_categories(self, categories: list[dict], want: str) -> None:
        cur = self.category.currentData() if self.recently_edited() else want
        ids = [c["id"] for c in categories]
        if self.category.count() and [self.category.itemData(i) for i in range(1, self.category.count())] == ids:
            return
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItem("None: just save them", "")
        for c in categories:
            label = (f"{c['emoji']}  " if c.get("emoji") else "") + c["name"]
            self.category.addItem(label, c["id"])
            tip = f"Posts to {c['account']}" if c.get("account") else ""
            if c.get("macs"):
                tip = (tip + "\n" if tip else "") + "Its clips also go to your Macs' Sports clips"
            if c.get("state"):
                tip = (tip + "\n" if tip else "") + c["state"]
            self.category.setItemData(self.category.count() - 1, tip, Qt.ToolTipRole)
        i = self.category.findData(cur)
        self.category.setCurrentIndex(max(0, i))
        self.category.blockSignals(False)

    def searching(self, search: dict) -> None:
        busy = search.get("status") in ("queued", "working")
        self.find_btn.setEnabled(not busy)
        self.find_btn.setText("Looking…" if busy else "Find games")
        self.stop_btn.setVisible(busy)
        if busy:
            self.search_note.setText(search.get("stage") or "Waiting to start")
            self.search_note.setStyleSheet("")
            return
        if search.get("error"):
            self.search_note.setText(f"Couldn't search: {search['error']}")
            self.search_note.setStyleSheet(f"color: {theme.BAD};")
            return
        self.search_note.setStyleSheet("")
        if not search.get("at"):
            self.search_note.setText("Set the filter, then Find games. Pick the games you want, and their best "
                                     "highlights are found and ranked.")
            return
        bits = [f"{_plural(search.get('added', 0), 'new game')} {ago(search.get('at'))}",
                f"{_plural(search.get('searched', 0), 'video')} looked at"]
        left = search.get("left_out") or {}
        if left:
            top = sorted(left.items(), key=lambda kv: -kv[1])[:3]
            bits.append("left out: " + ", ".join(f"{n} {why}" for why, n in top))
        if search.get("by"):
            bits.append(f"titles read by {search['by']}")
        if search.get("quota"):
            bits.append(f"{search['quota']} YouTube units")
        text = " · ".join(bits)
        if search.get("note"):
            text += f"\n{search['note']}"
        self.search_note.setText(text)


# ---------------------------------------------------------------- a game in the list


class GameRow(QFrame):
    ROW_H = 84
    picked = Signal(str, bool)

    def __init__(self, game_id: str, pictures: Pictures, parent=None):
        super().__init__(parent)
        self.setObjectName("vrow")
        self.game_id = game_id
        self.pictures = pictures
        self.thumb_url = ""
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 10, 6)
        h.setSpacing(10)
        self.tick = QCheckBox()
        self.tick.setToolTip("Pick this game: its highlights are found when you press Find highlights")
        self.tick.toggled.connect(lambda on: self.picked.emit(self.game_id, on))
        h.addWidget(self.tick)
        self.pic = Picture(112, 63)
        h.addWidget(self.pic)
        col = QVBoxLayout()
        col.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.title = Elided()
        self.title.setStyleSheet("font-weight: 600;")
        self.new = chip("")
        top.addWidget(self.title, 1)
        top.addWidget(self.new)
        col.addLayout(top)
        self.sub = Elided()
        self.sub.setObjectName("muted")
        col.addWidget(self.sub)
        state = QHBoxLayout()
        state.setSpacing(8)
        self.state = chip("")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(6)
        self.bar.setTextVisible(False)
        self.bar.hide()
        self.stage = Elided()
        self.stage.setObjectName("muted")
        self.stage.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # the chip keeps its room, not this
        self.bar.setMinimumWidth(40)
        state.addWidget(self.state)
        state.addWidget(self.bar, 1)
        state.addWidget(self.stage, 2)
        col.addLayout(state)
        h.addLayout(col, 1)

    def set(self, g: dict, now: float | None = None) -> None:
        self.tick.blockSignals(True)
        self.tick.setChecked(bool(g.get("picked")))
        self.tick.blockSignals(False)
        title = f"{g.get('emoji', '')} {g.get('matchup') or g.get('title')}".strip()
        if g.get("score"):
            title += f"  {g['score']}"
        self.title.setText(title)
        self.title.setToolTip(g.get("title", ""))
        bits = [g.get("league") or "", g.get("kind_label") or "", clock(g.get("duration")) if g.get("duration") else "",
                ago(g.get("published"), now) or "", g.get("channel") or ""]
        if len(g.get("versions") or []) > 1:
            bits.append(f"{len(g['versions'])} versions")
        self.sub.setText(" · ".join(dict.fromkeys(b for b in bits if b)))
        self.sub.setToolTip(views_text(g.get("views")))
        set_chip(self.new, "NEW" if g.get("new") else "", theme.ACCENT)
        status = g.get("status", "found")
        hl = g.get("highlights") or []
        made = sum(1 for h in hl if h.get("made"))
        words = STATUS_WORDS.get(status, "")
        if status == "ready":
            words = f"{_plural(len(hl), 'highlight')}" + (f" · {made} made" if made else "")
        set_chip(self.state, words, status_colour(status))
        busy = status in ("analysing", "making")
        self.bar.setVisible(busy)
        if busy:
            frac = g.get("frac")
            if frac is None:
                self.bar.setRange(0, 0)
            else:
                self.bar.setRange(0, 1000)
                self.bar.setValue(int(frac * 1000))
        stage = g.get("error") if status == "failed" else g.get("stage") if status in ("queued", "analysing", "making") \
            else ""
        self.stage.setText(stage or "")
        self.stage.setToolTip(stage or "")
        self.stage.setStyleSheet(f"color: {theme.BAD};" if status == "failed" else "")
        url = g.get("thumbnail") or ""
        if url != self.thumb_url:
            self.thumb_url = url
            self.pic.set(self.pictures.get(url))
            self.pictures.want_url(url)

    def picture_ready(self, key: str) -> None:
        if key == self.thumb_url:
            self.pic.set(self.pictures.get(key))


# ---------------------------------------------------------------- a highlight in the game's list


class HighlightRow(QFrame):
    ROW_H = 100
    chosen = Signal(str, bool)
    watch = Signal(float)

    def __init__(self, highlight_id: str, pictures: Pictures, parent=None):
        super().__init__(parent)
        self.setObjectName("vrow")
        self.hid = highlight_id
        self.pictures = pictures
        self.key = ""
        self.start = 0.0
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 10, 6)
        h.setSpacing(10)
        self.tick = QCheckBox()
        self.tick.setToolTip("Make a clip of this play")
        self.tick.toggled.connect(lambda on: self.chosen.emit(self.hid, on))
        h.addWidget(self.tick)
        self.pic = Picture(136, 77, "🏟")
        self.pic.setCursor(Qt.PointingHandCursor)
        self.pic.setToolTip("Watch this play on YouTube")
        self.pic.mousePressEvent = lambda _e: self.watch.emit(self.start)  # type: ignore[method-assign]
        h.addWidget(self.pic)
        col = QVBoxLayout()
        col.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.rank = QLabel()
        self.rank.setStyleSheet(f"font-weight: 700; color: {theme.MUTED};")
        self.play = chip("")
        self.title = Elided()
        self.title.setStyleSheet("font-weight: 600;")
        self.made = chip("")
        top.addWidget(self.rank)
        top.addWidget(self.play)
        top.addWidget(self.title, 1)
        top.addWidget(self.made)
        col.addLayout(top)
        self.why = muted("")
        self.why.setTextFormat(Qt.PlainText)
        self.why.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.why.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.why.setFixedHeight(self.why.fontMetrics().lineSpacing() * 2 + 2)  # two lines; the rest in its tooltip
        col.addWidget(self.why)
        self.meta = muted("", wrap=False)
        self.meta.setStyleSheet("font-size: 8.5pt;")
        col.addWidget(self.meta)
        h.addLayout(col, 1)
        self.score = QLabel()
        self.score.setAlignment(Qt.AlignCenter)
        self.score.setFixedWidth(46)
        h.addWidget(self.score)

    def set(self, game: dict, hl: dict) -> None:
        self.tick.blockSignals(True)
        self.tick.setChecked(bool(hl.get("chosen")))
        self.tick.blockSignals(False)
        self.start = float(hl.get("start") or 0)
        found = hl.get("origin") == "event"
        self.rank.setText("🔎" if found else f"#{hl.get('rank') or '?'}")
        self.rank.setToolTip("Found by your event search" + ("" if hl.get("certain", True) else
                             ": it's talked about here and may not have happened, so watch it first") if found else "")
        set_chip(self.play, hl.get("play") or "", theme.ACCENT)
        self.title.setText(hl.get("title") or "")
        self.title.setToolTip(hl.get("title") or "")
        players = ", ".join(hl.get("players") or [])
        self.why.setText((hl.get("why") or "") + (f"  ({players})" if players else ""))
        self.why.setToolTip(self.why.text())
        parts = hl.get("parts") or {}
        bits = [f"at {clock(hl.get('start'))}", f"{hl.get('length') or 0:.0f} s"]
        if found:
            bits.append(" + ".join(hl.get("sources") or []) or "meaning")
            if hl.get("badge"):
                bits.append(hl["badge"])
        if hl.get("ai") is not None:
            bits.append(f"AI {hl['ai']}")
        if parts.get("crowd") is not None:
            bits.append(f"crowd {parts['crowd']}")
        if parts.get("calls"):
            bits.append(f"calls {parts['calls']}")
        self.meta.setText(" · ".join(bits))
        score = int(round(hl.get("score") or 0))
        colour = theme.GOOD if score >= 75 else theme.ACCENT if score >= 50 else theme.MUTED
        self.score.setText(f"<div style='font-size:15pt; font-weight:700; color:{colour}'>{score}</div>"
                           f"<div style='font-size:7.5pt; color:{theme.MUTED}'>score</div>")
        self.score.setToolTip("How sure the search is that this is the moment you asked for" if found else
                              "The AI's rating and the crowd and commentary's, together" if hl.get("ai") is not None
                              else "How the crowd and the commentary rose over this play")
        set_chip(self.made, "Made" if hl.get("made") else "", theme.GOOD)
        self.made.setToolTip(hl.get("file") or "")
        key = self.pictures.want_highlight(game["id"], self.hid, f"{game.get('analysed') or ''}")
        self.key = key
        self.pic.set(self.pictures.get(key), f"{hl.get('length') or 0:.0f} s" if hl.get("length") else "")

    def picture_ready(self, key: str) -> None:
        if key == self.key:
            self.pic.set(self.pictures.get(key), self.pic.badge)


# ---------------------------------------------------------------- the game you're looking at


class GamePanel(QFrame):
    """One game: its versions of footage, where it's at, its ranked highlights, and Make clips."""

    ask = Signal(str, tuple)  # a desk method's name and its arguments
    studio = Signal(dict, list)  # the game and the highlights to open in the Studio

    def __init__(self, backend: Backend, pictures: Pictures, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.backend = backend
        self.pictures = pictures
        self.game: dict | None = None
        self.rows: dict[str, HighlightRow] = {}
        self.order: list[str] = []
        self.categories: list[dict] = []
        self.category_id = ""
        self.ai: dict = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        empty = QWidget()
        ev = QVBoxLayout(empty)
        ev.addStretch(1)
        self.empty_text = muted("Pick a game on the left to see its best highlights here.")
        self.empty_text.setAlignment(Qt.AlignCenter)
        ev.addWidget(self.empty_text)
        ev.addStretch(2)
        self.stack.addWidget(empty)

        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(12)
        self.pic = Picture(160, 90)
        head.addWidget(self.pic)
        hv = QVBoxLayout()
        hv.setSpacing(3)
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.PlainText)
        self.sub = Elided()
        self.sub.setObjectName("muted")
        self.yt_title = Elided()
        self.yt_title.setObjectName("muted")
        hv.addStretch(1)
        hv.addWidget(self.title)
        hv.addWidget(self.sub)
        hv.addWidget(self.yt_title)
        hv.addStretch(1)
        head.addLayout(hv, 1)
        v.addLayout(head)
        acts = QHBoxLayout()
        acts.setSpacing(6)
        self.version = QComboBox()
        self.version.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.version.setMinimumContentsLength(22)  # "Extended highlights · 22:14", whole
        self.version.setToolTip("This game's uploads: highlights are found in the one picked here")
        self.version.activated.connect(self._version_picked)
        self.watch_btn = QPushButton("▶  YouTube")
        self.watch_btn.setToolTip("Watch the game on YouTube")
        self.watch_btn.clicked.connect(lambda: self._open(0.0))
        self.find_btn = QPushButton("Find highlights")
        self.find_btn.clicked.connect(self._find)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(lambda: self.game and self.ask.emit("cancel", (self.game["id"],)))
        acts.addWidget(self.version)
        acts.addWidget(self.watch_btn)
        acts.addWidget(self.find_btn)
        acts.addWidget(self.stop_btn)
        acts.addStretch(1)
        v.addLayout(acts)

        self.progress = QWidget()
        pv = QHBoxLayout(self.progress)
        pv.setContentsMargins(0, 2, 0, 2)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(8)
        self.bar.setTextVisible(False)
        self.stage = muted("", wrap=False)
        pv.addWidget(self.bar, 1)
        pv.addWidget(self.stage, 2)
        v.addWidget(self.progress)
        self.problem = QLabel()
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet(f"color: {theme.BAD};")
        v.addWidget(self.problem)
        self.ranked = muted("")
        v.addWidget(self.ranked)
        self.events_note = muted("")
        self.events_note.setVisible(False)
        v.addWidget(self.events_note)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setFrameShape(QFrame.NoFrame)
        v.addWidget(self.list, 1)
        self.list_empty = muted("")
        self.list_empty.setAlignment(Qt.AlignCenter)
        v.addWidget(self.list_empty, 1)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.all_btn = QPushButton("Tick all")
        self.all_btn.setObjectName("flat")
        self.all_btn.clicked.connect(self._tick_all)
        self.where = Elided()
        self.where.setObjectName("muted")
        self.where.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.studio_btn = QPushButton("Open in Studio")
        self.studio_btn.setToolTip("Edit the ticked plays in the Studio first (they're downloaded there)")
        self.studio_btn.clicked.connect(self._studio)
        self.folder_btn = QPushButton("Show clips")
        self.folder_btn.setToolTip("Open the folder of the clips made last")
        self.folder_btn.clicked.connect(self._show_folder)
        self.make_btn = QPushButton("Make clips")
        self.make_btn.setObjectName("primary")
        self.make_btn.clicked.connect(self._make)
        self.reel_btn = QPushButton("Make reel")
        self.reel_btn.setToolTip("One 65-85 s video of the game: an opening that teases the best play, then the "
                                 "ticked plays (at least 4, at most 7) counted down, with the score, player badges "
                                 "and the narrator set under Recap reel. Long enough for Creator Rewards")
        self.reel_btn.clicked.connect(self._reel)
        foot.addWidget(self.all_btn)
        foot.addWidget(self.where, 1)
        foot.addWidget(self.folder_btn)
        foot.addWidget(self.studio_btn)
        foot.addWidget(self.reel_btn)
        foot.addWidget(self.make_btn)
        self.reel_note = Elided()
        self.reel_note.setObjectName("muted")
        v.addWidget(self.reel_note)
        v.addLayout(foot)
        self.stack.addWidget(page)
        self.folder_btn.setVisible(False)
        pictures.ready.connect(self._picture_ready)

    # --- showing ------------------------------------------------------------------------------------------

    def show_game(self, g: dict | None, categories: list[dict], category_id: str, ai_state: dict) -> None:
        self.categories, self.category_id, self.ai = categories, category_id, ai_state
        if g is None:
            self.game = None
            self.stack.setCurrentIndex(0)
            return
        same = self.game is not None and self.game.get("id") == g["id"]
        self.game = g
        self.stack.setCurrentIndex(1)
        self.title.setText(f"{g.get('emoji', '')} {g.get('matchup') or g.get('title')}".strip()
                           + (f"  {g['score']}" if g.get("score") else ""))
        bits = [g.get("league") or "", g.get("channel") or "", ago(g.get("published")) or "", views_text(g.get("views"))]
        self.sub.setText(" · ".join(dict.fromkeys(b for b in bits if b)))
        self.yt_title.setText(g.get("title", ""))
        url = g.get("thumbnail") or ""
        self.pic.set(self.pictures.get(url))
        self.pictures.want_url(url)
        self._versions(g)
        status = g.get("status", "found")
        busy = status in ("queued", "analysing", "making")
        hl = g.get("highlights") or []
        self.find_btn.setVisible(not busy)
        self.find_btn.setText("Find again" if hl else "Find highlights")
        self.find_btn.setToolTip("Download it again if needed, listen again and rank the plays again" if hl else
                                 "Download the game, listen for the crowd and the big calls, and rank its plays")
        self.find_btn.setObjectName("" if hl else "primary")
        self.find_btn.style().unpolish(self.find_btn)
        self.find_btn.style().polish(self.find_btn)
        self.stop_btn.setVisible(busy)
        self.version.setEnabled(not busy)
        self.progress.setVisible(busy)
        if busy:
            frac = g.get("frac")
            if frac is None or status == "queued":
                self.bar.setRange(0, 0 if status != "queued" else 1000)
                self.bar.setValue(0)
            else:
                self.bar.setRange(0, 1000)
                self.bar.setValue(int(frac * 1000))
            self.stage.setText(g.get("stage") or STATUS_WORDS.get(status, ""))
        self.problem.setVisible(status == "failed" and bool(g.get("error")))
        self.problem.setText(f"Couldn't finish: {g.get('error')}" if status == "failed" else "")
        if hl:
            if g.get("ranked_by"):
                text = f"Ranked by {g['ranked_by']}, with the crowd and the commentary"
            else:
                text = "Ranked by the crowd and the commentary"
                if g.get("ai_error"):
                    text += f" (the AI couldn't: {g['ai_error']})"
            if not self.backend.local:
                text += f", on {self.backend.where}"
            self.ranked.setText(text + ".")
        else:
            self.ranked.setText("")
        self.events_note.setText(events_text(g))
        self.events_note.setToolTip(index_tip(g))
        self.events_note.setVisible(bool(self.events_note.text()))
        self._rows(g, hl, same)
        if not hl:
            if busy:
                self.list_empty.setText("Its plays show here, best first, once they're found.")
            elif status == "failed":
                self.list_empty.setText("")
            else:
                who = (f"{self.ai.get('name')} ranks them" if self.ai.get("ready") else
                       "The crowd and the commentary rank them (no AI is set up)")
                self.list_empty.setText(f"Find highlights downloads this game on {self.backend.where}, listens for "
                                        f"the crowd erupting and the commentator's big calls, and cuts each play whole. "
                                        f"{who}.")
        self.list.setVisible(bool(hl))
        self.list_empty.setVisible(not hl)
        self._footer()

    def _versions(self, g: dict) -> None:
        vs = g.get("versions") or []
        self.version.blockSignals(True)
        self.version.clear()
        for ver in vs:
            label = f"{G.KIND_LABELS.get(ver.get('kind'), 'Footage')} · {clock(ver.get('duration'))}"
            self.version.addItem(label, ver.get("video"))
            self.version.setItemData(self.version.count() - 1, f"{ver.get('title') or ''}\n{ver.get('channel') or ''}",
                                     Qt.ToolTipRole)
        self.version.setCurrentIndex(max(0, self.version.findData(g.get("video"))))
        self.version.blockSignals(False)
        self.version.setVisible(len(vs) > 1)

    def _rows(self, g: dict, hl: list[dict], same: bool) -> None:
        ids = [h["id"] for h in hl]
        if not same or ids != self.order:
            self.list.clear()
            self.rows = {}
            for h in hl:
                row = HighlightRow(h["id"], self.pictures)
                row.chosen.connect(self._chosen)
                row.watch.connect(self._open)
                it = QListWidgetItem()
                it.setSizeHint(QSize(100, HighlightRow.ROW_H))
                self.list.addItem(it)
                self.list.setItemWidget(it, row)
                self.rows[h["id"]] = row
            self.order = ids
        for h in hl:
            self.rows[h["id"]].set(g, h)

    def _footer(self) -> None:
        g = self.game or {}
        hl = g.get("highlights") or []
        ticked = [h for h in hl if h.get("chosen")]
        busy = g.get("status") in ("queued", "analysing", "making")
        self.make_btn.setEnabled(bool(ticked) and not busy)
        self.make_btn.setText(f"Make {_plural(len(ticked), 'clip')}" if ticked else "Make clips")
        self.studio_btn.setEnabled(bool(ticked) and not busy)
        self.studio_btn.setVisible(bool(hl))
        self.all_btn.setVisible(bool(hl))
        self.all_btn.setText("Untick all" if hl and len(ticked) == len(hl) else "Tick all")
        cat = next((c for c in self.categories if c["id"] == self.category_id), None)
        if not hl:
            self.where.setText("")
        elif cat:
            self.where.setText(f"For {cat['name']}" + (" · your Macs too" if cat.get("macs") else ""))
            self.where.setToolTip(f"The clips are made with {cat['name']}'s look, saved in its folder and queued for "
                                  f"its account" + (", and sent to your Macs' Sports clips" if cat.get("macs") else "")
                                  + ". Change it under Clips for.")
        else:
            self.where.setText("No category: just saved")
            self.where.setToolTip("The clips are saved in the Automation folder's Sports folder. Pick a category under "
                                  "Clips for to queue them for its account.")
        only_found = bool(ticked) and all(h.get("origin") == "event" for h in ticked)
        self.reel_btn.setEnabled((len(hl) >= 2 or only_found) and not busy)
        self.reel_btn.setText("Make reel of found" if only_found else "Make reel")
        self.reel_btn.setVisible(bool(hl))
        made = g.get("made") or []
        reels = g.get("reels") or []
        latest = max(made + reels, key=lambda x: x.get("at") or 0) if made or reels else None
        self.folder_btn.setVisible(self.backend.local and latest is not None)
        if latest is not None:
            is_reel = latest in reels
            self.folder_btn.setText("Show reel" if is_reel else "Show clips")
            self.folder_btn.setToolTip(f"{latest.get('folder', '')}\n" + (
                f"A {latest.get('length', 0):.0f} s reel made {ago(latest.get('at'))}" if is_reel else
                f"{_plural(int(latest.get('clips') or 0), 'clip')} made {ago(latest.get('at'))}"))
        if reels:
            r = reels[-1]
            bits = [f"Reel made {ago(r.get('at'))}: {r.get('length', 0):.0f} s, {_plural(int(r.get('plays') or 0), 'play')}",
                    "for Creator Rewards" if r.get("rewards") else "under a minute (not for Creator Rewards)"]
            if r.get("category"):
                bits.append(f"for {r['category']}" + (", sent to your Macs" if r.get("sent") else ""))
            self.reel_note.setText(" · ".join(bits))
            self.reel_note.setToolTip("\n".join(r.get("warnings") or []))
        else:
            self.reel_note.setText("")
        self.reel_note.setVisible(bool(reels))

    def _picture_ready(self, key: str) -> None:
        if self.game and key == (self.game.get("thumbnail") or ""):
            self.pic.set(self.pictures.get(key))
        for row in self.rows.values():
            row.picture_ready(key)

    # --- asking -------------------------------------------------------------------------------------------

    def _chosen(self, hid: str, on: bool) -> None:
        if not self.game:
            return
        for h in self.game.get("highlights") or []:
            if h["id"] == hid:
                h["chosen"] = on
        self._footer()
        self.ask.emit("choose", (self.game["id"], [hid], on))

    def _tick_all(self) -> None:
        if not self.game:
            return
        hl = self.game.get("highlights") or []
        on = not all(h.get("chosen") for h in hl)
        for h in hl:
            h["chosen"] = on
        for row in self.rows.values():
            row.tick.blockSignals(True)
            row.tick.setChecked(on)
            row.tick.blockSignals(False)
        self._footer()
        self.ask.emit("choose", (self.game["id"], [h["id"] for h in hl], on))

    def _find(self) -> None:
        if self.game:
            self.ask.emit("analyse", ([self.game["id"]], bool(self.game.get("highlights"))))

    def _make(self) -> None:
        if not self.game:
            return
        ids = [h["id"] for h in self.game.get("highlights") or [] if h.get("chosen")]
        if ids:
            self.ask.emit("make", (self.game["id"], ids, self.category_id))

    def _reel(self) -> None:
        if not self.game:
            return
        ids = [h["id"] for h in self.game.get("highlights") or [] if h.get("chosen")]
        self.ask.emit("reel", (self.game["id"], ids, self.category_id))

    def _studio(self) -> None:
        if self.game:
            self.studio.emit(self.game, [h for h in self.game.get("highlights") or [] if h.get("chosen")])

    def _version_picked(self, _i: int) -> None:
        video = self.version.currentData()
        if self.game and video and video != self.game.get("video"):
            if self.game.get("highlights"):
                box = QMessageBox(QMessageBox.Question, "Use other footage",
                                  "Use this footage instead? The highlights found so far are cleared, and Find "
                                  "highlights looks in this upload.", parent=self)
                box.addButton("Use it", QMessageBox.AcceptRole)
                no = box.addButton("Keep these", QMessageBox.RejectRole)
                box.exec()
                if box.clickedButton() is no:
                    self.version.setCurrentIndex(max(0, self.version.findData(self.game.get("video"))))
                    return
            self.ask.emit("version", (self.game["id"], video))

    def _open(self, at: float) -> None:
        if self.game:
            QDesktopServices.openUrl(QUrl(watch_url(self.game, at)))

    def _show_folder(self) -> None:
        g = self.game or {}
        done = (g.get("made") or []) + (g.get("reels") or [])
        if done:
            latest = max(done, key=lambda x: x.get("at") or 0)
            QDesktopServices.openUrl(QUrl.fromLocalFile(latest.get("folder") or ""))


# ---------------------------------------------------------------- the page


# ---------------------------------------------------------------- the games played (the last 24 hours)


class PlayedRow(QFrame):
    """One game from the scoreboards: who played, the score, its league, and whether it's over."""

    ROW_H = 50

    def __init__(self, g: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("vrow")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 5, 10, 5)
        h.setSpacing(10)
        mark = QLabel(g.get("emoji") or "🏆")
        mark.setFixedWidth(22)
        mark.setAlignment(Qt.AlignCenter)
        h.addWidget(mark)
        col = QVBoxLayout()
        col.setSpacing(1)
        line = Elided(g.get("line") or g.get("label") or "")
        line.setStyleSheet("font-weight: 600;")
        sub = Elided(g.get("league") or "")
        sub.setObjectName("muted")
        col.addWidget(line)
        col.addWidget(sub)
        h.addLayout(col, 1)
        state = g.get("state")
        h.addWidget(chip(g.get("when") or "", theme.BAD if state == "live" else theme.GOOD if state == "final"
                         else theme.MUTED))
        if not g.get("ready"):
            line.setStyleSheet(f"font-weight: 600; color: {theme.MUTED};")


class PlayedPanel(QWidget):
    """Every game of the last 24 hours (the scoreboards), to pick by drop-down (sport, league, game) or from the
    list under them. `find` looks for the picked game's footage, so its plays can be gone through by hand;
    `order` asks for its recap reel, hands-off."""

    find = Signal(dict)
    order = Signal(str, str)  # the game's id on the list, what to make

    def __init__(self, backend: Backend, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.data: dict | None = None
        self.loaded_at = 0.0
        self.chosen = ""
        self.error = ""
        self._asking = False
        self._filling = False
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        pair = QHBoxLayout()
        pair.setSpacing(8)
        self.sport = QComboBox()
        self.sport.setToolTip("Only this sport's games")
        self.league = QComboBox()
        self.league.setToolTip("Only this league's games")
        pair.addWidget(self.sport, 1)
        pair.addWidget(self.league, 1)
        form.addRow("Sport", pair)
        self.game = QComboBox()
        self.game.setToolTip("Every game of the last 24 hours. One still being played can be picked once it's over.")
        self.game.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.game.setMinimumContentsLength(18)
        form.addRow("Game", self.game)
        v.addLayout(form)
        self.note = muted("", wrap=True)
        v.addWidget(self.note)
        self.list = QListWidget()
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setFrameShape(QFrame.NoFrame)
        v.addWidget(self.list, 1)
        self.empty = muted("Reading the scoreboards…")
        self.empty.setAlignment(Qt.AlignCenter)
        v.addWidget(self.empty, 1)
        self.hint = muted("", wrap=True)
        v.addWidget(self.hint)
        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setObjectName("flat")
        self.refresh_btn.setToolTip("Read the scoreboards again")
        self.refresh_btn.clicked.connect(lambda: self.load(force=True))
        self.find_btn = QPushButton("Find its footage")
        self.find_btn.setToolTip("Look for this game's highlights and full game on YouTube: they join the Games "
                                 "list, where you tick the plays yourself")
        self.find_btn.clicked.connect(self._find)
        self.reel_btn = QPushButton("Make its reel")
        self.reel_btn.setObjectName("primary")
        self.reel_btn.setToolTip(f"Hands-off: its highlights are found, its best plays ranked and a recap reel "
                                 f"made on {backend.where} (65-85 s, over a minute)")
        self.reel_btn.clicked.connect(lambda: self.chosen and self.order.emit(self.chosen, "reel"))
        foot.addWidget(self.refresh_btn)
        foot.addStretch(1)
        foot.addWidget(self.find_btn)
        foot.addWidget(self.reel_btn)
        v.addLayout(foot)
        self.sport.currentIndexChanged.connect(lambda _i: self._narrowed("sport"))
        self.league.currentIndexChanged.connect(lambda _i: self._narrowed("league"))
        self.game.currentIndexChanged.connect(lambda _i: self._choose(self.game.currentData() or "", "game"))
        self.list.currentItemChanged.connect(
            lambda cur, _prev=None: self._choose(cur.data(Qt.UserRole) if cur is not None else "", "list"))
        self._fill()

    # --- reading the scoreboards ----------------------------------------------------------------------------

    def load(self, force: bool = False) -> None:
        """Ask for the games (on the backend's threads), unless they were read a moment ago."""
        if self._asking or not self.backend.available():
            return
        if not force and self.data is not None and time.monotonic() - self.loaded_at < PLAYED_STALE:
            return
        self._asking = True
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Reading…")

        def done() -> None:
            self._asking = False
            self.loaded_at = time.monotonic()
            self.refresh_btn.setEnabled(True)
            self.refresh_btn.setText("Refresh")

        def ok(d: dict) -> None:
            done()
            self.error = ""
            self.data = d if isinstance(d, dict) else {}
            self._fill()

        def fail(exc: Exception) -> None:
            done()
            self.error = error_text(exc)
            if self.data is None:
                self.data = {}
            self._fill()

        self.backend.call("played", "any", "", ok=ok, fail=fail)

    def games(self) -> list[dict]:
        """The games the two narrowing drop-downs leave."""
        sport, league = self.sport.currentData() or "", self.league.currentData() or ""
        return [g for g in (self.data or {}).get("games") or []
                if (not sport or g.get("sport") == sport) and (not league or g.get("league") == league)]

    def picked(self) -> dict | None:
        return next((g for g in (self.data or {}).get("games") or [] if g.get("id") == self.chosen), None)

    # --- showing them ---------------------------------------------------------------------------------------

    def _fill(self) -> None:
        d = self.data or {}
        every = d.get("games") or []
        self._filling = True
        try:
            sport = self.sport.currentData() or ""
            self.sport.clear()
            self.sport.addItem("All sports", "")
            for sid in dict.fromkeys(g.get("sport") for g in every):
                info = sports.get(sid or "")
                self.sport.addItem(f"{info.emoji}  {info.label}" if info else str(sid), sid)
            self.sport.setCurrentIndex(max(0, self.sport.findData(sport)))
            self._fill_leagues()
            self._fill_games()
        finally:
            self._filling = False
        self._sync()

    def _fill_leagues(self) -> None:
        sport, league = self.sport.currentData() or "", self.league.currentData() or ""
        self.league.clear()
        self.league.addItem("All leagues", "")
        for r in (self.data or {}).get("leagues") or []:
            if r.get("games") and (not sport or r.get("sport") == sport):
                self.league.addItem(f"{r['league']} ({r['games']})", r["league"])
        self.league.setCurrentIndex(max(0, self.league.findData(league)))

    def _fill_games(self) -> None:
        games = self.games()
        if not any(g["id"] == self.chosen for g in games):
            self.chosen = ""
        one_league = bool(self.league.currentData())
        self.game.clear()
        self.game.addItem("Pick a game…" if games else "No games here in the last 24 hours", "")
        for g in games:
            self.game.addItem(("" if one_league else f"{g['league']}: ") + f"{g['line']}  ·  {g['when']}", g["id"])
            if not g.get("ready"):  # still being played, or yet to start: listed, not to pick
                item = self.game.model().item(self.game.count() - 1)
                if item is not None:
                    item.setEnabled(False)
        self.game.setCurrentIndex(max(0, self.game.findData(self.chosen)))
        self.list.clear()
        for g in games:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, g["id"])
            it.setSizeHint(QSize(100, PlayedRow.ROW_H))
            self.list.addItem(it)
            self.list.setItemWidget(it, PlayedRow(g))
            if g["id"] == self.chosen:
                self.list.setCurrentItem(it)

    def _narrowed(self, which: str) -> None:
        if self._filling:
            return
        self._filling = True
        try:
            if which == "sport":
                self._fill_leagues()
            self._fill_games()
        finally:
            self._filling = False
        self._sync()

    def _choose(self, game_id: str, source: str) -> None:
        if self._filling:
            return
        self.chosen = game_id
        self._filling = True
        try:
            if source != "game":
                self.game.setCurrentIndex(max(0, self.game.findData(game_id)))
            if source != "list":
                row = next((i for i in range(self.list.count())
                            if self.list.item(i).data(Qt.UserRole) == game_id), -1)
                self.list.setCurrentRow(row)
        finally:
            self._filling = False
        self._sync()

    def _find(self) -> None:
        g = self.picked()
        if g is not None:
            self.find.emit(g)

    def said(self, text: str) -> None:
        """What came of the last thing asked, under the list."""
        self.hint.setText(text)
        self.hint.setVisible(bool(text))

    def _sync(self) -> None:
        d = self.data
        games = self.games() if d is not None else []
        g = self.picked()
        ready = bool(g and g.get("ready"))
        self.find_btn.setEnabled(bool(g))
        self.reel_btn.setEnabled(ready)
        self.list.setVisible(bool(games))
        self.empty.setVisible(not games)
        every = (d or {}).get("games") or []
        if d is None:
            self.empty.setText("Reading the scoreboards…")
        elif self.error and not every:
            self.empty.setText(f"The games couldn't be read: {self.error}")
        elif not every:
            self.empty.setText("No games on the scoreboards in the last 24 hours.\nThe filter on the left looks for "
                               "any game, of any sport, by name.")
        else:
            self.empty.setText("None for this sport and league.")
        over = sum(1 for x in games if x.get("ready"))
        live = sum(1 for x in games if x.get("state") == "live")
        bits = [_plural(len(games), "game"), f"{over} finished"] + ([f"{live} being played"] if live else [])
        note = " · ".join(bits) if games else ""
        problems = list((d or {}).get("problems") or []) + ([self.error] if self.error and every else [])
        if problems:
            note += ("\n" if note else "") + "Not read: " + " ".join(problems[:3])
        self.note.setText(note)
        self.note.setVisible(bool(note))
        if g and not ready:
            self.said("It's still being played: its highlights go up after it ends." if g.get("state") == "live"
                      else "It hasn't been played yet.")
        elif self.hint.text().startswith(("It's still being played", "It hasn't been played")):
            self.said("")
        self.hint.setVisible(bool(self.hint.text()))


class SportsView(QWidget):
    notice = Signal(str)  # a line for the window's status
    open_studio = Signal(str, str, list)  # a video's link, its title and the plays to open in the Studio
    open_settings = Signal(str)  # a Settings tab to open

    def __init__(self, backend: Backend, parent=None, heading: bool = True):
        super().__init__(parent)
        self.backend = backend
        self.pictures = Pictures(backend, self)
        self.state: dict = {}
        self.rev = ""
        self.games: dict[str, dict] = {}
        self.rows: dict[str, GameRow] = {}
        self.order: list[str] = []
        self.current = ""
        self.view = "all"
        self._asking = False
        self._loaded = False
        self.offline = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(10)
        if heading:
            t = QLabel("Sports")
            t.setObjectName("title")
            top.addWidget(t)
        self.describe = muted("", wrap=False)
        self.describe.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        top.addWidget(self.describe, 1)
        self.event_box = QLineEdit()
        self.event_box.setPlaceholderText("Search by in-game event: red card after a VAR check…")
        self.event_box.setClearButtonEnabled(True)
        self.event_box.setMinimumWidth(300)
        self.event_box.setMaximumWidth(460)
        self.event_box.setToolTip(EVENT_HELP)
        self.event_box.returnPressed.connect(self._find_events)
        self.event_btn = QPushButton("Search")
        self.event_btn.setToolTip(EVENT_HELP)
        self.event_btn.clicked.connect(self._find_events)
        top.addWidget(self.event_box)
        top.addWidget(self.event_btn)
        self.ai_chip = chip("")
        self.ai_chip.setCursor(Qt.PointingHandCursor)
        self.ai_chip.mousePressEvent = lambda _e: self.open_settings.emit("AI")  # type: ignore[method-assign]
        self.worker_chip = chip("")
        top.addWidget(self.worker_chip)
        top.addWidget(self.ai_chip)
        outer.addLayout(top)

        self.now_line = Elided()  # what's being worked on right now: the game, the step and how far
        self.now_line.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 600;")
        self.now_line.setIndent(2)
        self.now_line.hide()
        outer.addWidget(self.now_line)

        self.banner = QFrame()
        self.banner.setObjectName("banner")
        bh = QHBoxLayout(self.banner)
        bh.setContentsMargins(12, 7, 12, 7)
        self.banner_text = QLabel()
        self.banner_text.setWordWrap(True)
        bh.addWidget(self.banner_text, 1)
        self.banner.hide()
        outer.addWidget(self.banner)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.filter = FilterPanel()
        self.filter.setMinimumWidth(260)
        self.filter.setMaximumWidth(340)
        self.filter.changed.connect(lambda f: self.ask("set_filter", (f,)))
        self.filter.find.connect(lambda f: self.ask("find_games", (f,)))
        self.filter.stop.connect(lambda: self.ask("cancel", ("search",)))
        split.addWidget(self.filter)

        mid = QFrame()
        mid.setObjectName("panel")
        mv = QVBoxLayout(mid)
        mv.setContentsMargins(12, 12, 12, 10)
        mv.setSpacing(8)
        mh = QHBoxLayout()
        mh.setSpacing(8)
        mh.addWidget(h2("Games"))
        self.count = muted("", wrap=False)
        mh.addWidget(self.count, 1)
        seg = QFrame()
        seg.setObjectName("segbar")
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(3, 3, 3, 3)
        sh.setSpacing(2)
        self.view_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        for key, label in (("all", "All"), ("picked", "Picked"), ("ready", "Ready"), ("played", "Last 24 hours")):
            b = QPushButton(label)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.clicked.connect(lambda _c=False, k=key: self._view(k))
            group.addButton(b)
            sh.addWidget(b)
            self.view_buttons[key] = b
        self.view_buttons["played"].setToolTip("Every game played in the last 24 hours, from the scoreboards: pick "
                                               "one by sport, league and game, and make its reel")
        self.view_buttons["all"].setChecked(True)
        mh.addWidget(seg)
        mv.addLayout(mh)
        self.list = QListWidget()
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.currentItemChanged.connect(self._selected)
        mv.addWidget(self.list, 1)
        self.list_empty = muted("")
        self.list_empty.setAlignment(Qt.AlignCenter)
        mv.addWidget(self.list_empty, 1)
        self.played = PlayedPanel(backend)
        self.played.find.connect(self._find_footage)
        self.played.order.connect(self._order_game)
        self.played.hide()
        mv.addWidget(self.played, 1)
        self.list_foot = QWidget()
        mf = QHBoxLayout(self.list_foot)
        mf.setContentsMargins(0, 0, 0, 0)
        mf.setSpacing(6)
        self.clear_btn = QPushButton("Clear the rest")
        self.clear_btn.setObjectName("flat")
        self.clear_btn.setToolTip("Take every game that isn't picked off the list")
        self.clear_btn.clicked.connect(lambda: self.ask("clear", ()))
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setToolTip("Take the game you're looking at off the list")
        self.remove_btn.clicked.connect(self._remove)
        self.analyse_btn = QPushButton("Find highlights")
        self.analyse_btn.setObjectName("primary")
        self.analyse_btn.clicked.connect(self._analyse_picked)
        mf.addWidget(self.clear_btn)
        mf.addWidget(self.remove_btn)
        mf.addStretch(1)
        mf.addWidget(self.analyse_btn)
        mv.addWidget(self.list_foot)
        split.addWidget(mid)

        self.panel = GamePanel(backend, self.pictures)
        self.panel.ask.connect(lambda name, args: self.ask(name, args))
        self.panel.studio.connect(self._studio)
        split.addWidget(self.panel)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 2)
        split.setStretchFactor(2, 3)
        split.setSizes([280, 380, 720])  # the plays get the most room
        self.split = split
        outer.addWidget(split, 1)
        self.pictures.ready.connect(self._picture_ready)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.render({})

    # --- keeping up to date ---------------------------------------------------------------------------------

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self.refresh(force=not self._loaded)
        self.timer.start(POLL_MS)

    def hideEvent(self, e) -> None:  # noqa: N802
        super().hideEvent(e)
        self.timer.stop()

    def refresh(self, force: bool = False) -> None:
        if self._asking or not self.backend.available():
            if not self.backend.available():
                self.timer.setInterval(SLOW_POLL_MS)
            return
        self._asking = True

        def ok(st: dict) -> None:
            self._asking = False
            self.offline = ""
            if st.get("same"):
                return
            self.render(st)

        def fail(exc: Exception) -> None:
            self._asking = False
            self.offline = error_text(exc)
            self._banner()

        self.backend.call("state", "" if force else self.rev, ok=ok, fail=fail)

    def ask(self, name: str, args: tuple) -> None:
        """One of the desk's methods; then the page asks how things are now."""
        def ok(result) -> None:
            msg = result.get("message") if isinstance(result, dict) else ""
            if msg:
                self.notice.emit(msg)
            self.refresh(force=True)

        def fail(exc: Exception) -> None:
            text = error_text(exc)
            self.notice.emit(text)
            self._banner(text)
            self.refresh(force=True)

        self.backend.call(name, *args, ok=ok, fail=fail)

    def render(self, st: dict) -> None:
        if st:
            self._loaded = True
            self.state = st
            self.rev = st.get("rev", "")
        st = self.state
        gs = st.get("games") or []
        self.games = {g["id"]: g for g in gs}
        busy = any(g.get("status") in ("queued", "analysing", "making") for g in gs) or \
            (st.get("search") or {}).get("status") in ("queued", "working")
        self.timer.setInterval(POLL_MS if busy else SLOW_POLL_MS)
        if st:
            self.filter.fill(st.get("filter") or {}, st.get("categories") or [])
            self.filter.searching(st.get("search") or {})
            self.describe.setText(st.get("describe") or "")
            self._chips(st)
        self._banner()
        shown = [g for g in gs if self._shows(g)]
        ids = [g["id"] for g in shown]
        if ids != self.order:
            keep = self.current
            self.list.blockSignals(True)
            self.list.clear()
            self.rows = {}
            for g in shown:
                row = GameRow(g["id"], self.pictures)
                row.picked.connect(self._picked)
                it = QListWidgetItem()
                it.setData(Qt.UserRole, g["id"])
                it.setSizeHint(QSize(100, GameRow.ROW_H))
                self.list.addItem(it)
                self.list.setItemWidget(it, row)
                self.rows[g["id"]] = row
            self.order = ids
            if keep in ids:
                self.list.setCurrentRow(ids.index(keep))
            self.list.blockSignals(False)
        now = time.time()
        for g in shown:
            self.rows[g["id"]].set(g, now)
        if self.current not in self.games:
            self.current = ""
            if ids:
                first = next((g["id"] for g in shown if g.get("highlights") or g.get("picked")), ids[0])
                self.list.setCurrentRow(ids.index(first))  # (shows it)
        self.panel.show_game(self.games.get(self.current), st.get("categories") or [],
                             (st.get("filter") or {}).get("category_id", ""), st.get("ai") or {})
        picked = [g for g in gs if g.get("picked") and not g.get("highlights")
                  and g.get("status") not in ("queued", "analysing", "making")]
        self.analyse_btn.setEnabled(bool(picked))
        self.analyse_btn.setText(f"Find highlights in {_plural(len(picked), 'game')}" if picked else "Find highlights")
        self.analyse_btn.setToolTip("Pick games (tick them), then find their best highlights, one game after "
                                    "another" if not picked else
                                    f"Download them on {self.backend.where}, listen to each, and rank their plays")
        self.remove_btn.setEnabled(bool(self.current) and self.games.get(self.current, {}).get("status")
                                   not in ("analysing", "making"))
        self.clear_btn.setEnabled(any(not g.get("picked") and not g.get("highlights") for g in gs))
        n_ready = sum(1 for g in gs if g.get("highlights"))
        self.count.setText(f"{_plural(len(gs), 'game')}" + (f" · {st.get('picked', 0)} picked" if st.get("picked")
                                                            else "") + (f" · {n_ready} ready" if n_ready else ""))
        played = self.view == "played"
        self.played.setVisible(played)
        self.list_foot.setVisible(not played)
        self.list.setVisible(bool(shown) and not played)
        self.list_empty.setVisible(not shown and not played)
        if played:
            self.played.load()  # (again once what it has is a few minutes old)
        if not gs:
            self.list_empty.setText("No games yet.\nSet the filter on the left and press Find games.")
        elif not shown:
            self.list_empty.setText({"picked": "No games picked. Tick the games you want to clip.",
                                     "ready": "No game's highlights are ready yet."}.get(self.view, ""))

    def _shows(self, g: dict) -> bool:
        if self.view == "played":
            return False  # the middle shows the scoreboards' games instead
        if self.view == "picked":
            return bool(g.get("picked"))
        if self.view == "ready":
            return bool(g.get("highlights"))
        return True

    def _chips(self, st: dict) -> None:
        a = st.get("ai") or {}
        if a.get("ready"):
            set_chip(self.ai_chip, f"AI: {a.get('name')}" + (f" · {a['model']}" if a.get("model") and
                                                              a.get("model") != a.get("name") else ""), theme.GOOD)
            self.ai_chip.setToolTip("Reads the titles and ranks each game's plays (Settings → AI"
                                    + (" on your PC" if not self.backend.local else "") + ")")
        else:
            set_chip(self.ai_chip, "No AI: crowd & commentary rank plays", theme.WARN)
            self.ai_chip.setToolTip((a.get("why") or "") + "\nClaude or Gemini (with an API key) or the local AI "
                                    "names each play, writes its title and ranks them better.")
        worker = st.get("worker") or ""
        waiting = int(st.get("waiting") or 0)
        if worker:
            where = "automation" if worker == "automation" else "this window" if self.backend.local else "your PC"
            set_chip(self.worker_chip, f"Working in {where}" + (f" · {waiting} waiting" if waiting else ""),
                     theme.ACCENT)
        elif waiting:
            set_chip(self.worker_chip, f"{waiting} waiting", theme.WARN)
        else:
            set_chip(self.worker_chip, "")
        doing = st.get("doing") or ""
        self.now_line.setText(("Now: " + doing) if doing else "")
        self.now_line.setVisible(bool(doing))
        yt = "" if st.get("youtube") == "api" else (
            "No YouTube Data API key: searching YouTube's own results (free; upload dates are less exact). A key "
            "in Settings → Research makes searches by date exact.")
        self.filter.youtube_note.setText(yt)
        self.filter.youtube_note.setVisible(bool(yt))

    def _banner(self, text: str = "") -> None:
        text = text or (f"Can't reach {self.backend.where}: {self.offline}" if self.offline else "")
        self.banner_text.setText(text)
        self.banner.setVisible(bool(text))
        if text and not self.offline:
            QTimer.singleShot(8000, lambda t=text: self.banner_text.text() == t and self.banner.hide())

    # --- what you do ----------------------------------------------------------------------------------------

    def _view(self, key: str) -> None:
        self.view = key
        self.order = []
        self.render({})

    def _selected(self, cur: QListWidgetItem | None, _prev=None) -> None:
        self.current = cur.data(Qt.UserRole) if cur is not None else ""
        st = self.state
        self.panel.show_game(self.games.get(self.current), st.get("categories") or [],
                             (st.get("filter") or {}).get("category_id", ""), st.get("ai") or {})
        g = self.games.get(self.current)
        self.remove_btn.setEnabled(bool(g) and g.get("status") not in ("analysing", "making"))

    def _picked(self, game_id: str, on: bool) -> None:
        g = self.games.get(game_id)
        if g is not None:
            g["picked"] = on
            g["new"] = False
        self.ask("pick", ([game_id], on))

    def _analyse_picked(self) -> None:
        ids = [g["id"] for g in self.state.get("games") or [] if g.get("picked") and not g.get("highlights")
               and g.get("status") not in ("queued", "analysing", "making")]
        if ids:
            self.ask("analyse", (ids, False))

    def _find_events(self) -> None:
        """Search the game you're looking at (else every picked game) for the moment typed in the header."""
        query = " ".join(self.event_box.text().split())
        if len(query) < 3:
            self._banner("Type the moment to look for, such as “red card” or “technical foul on the bench”.")
            return
        self.ask("find_events", (query, [self.current] if self.current else None))

    def _remove(self) -> None:
        if self.current:
            self.ask("remove", ([self.current],))

    def _find_footage(self, g: dict) -> None:
        """A game picked from the last 24 hours: look for its highlights and full game, into the Games list."""
        f = {**self.filter.values(), "sport": g.get("sport") or "any", "league": g.get("league") or "",
             "teams": f"{g.get('away', '')}, {g.get('home', '')}".strip(", "), "since": "3days"}
        self.ask("find_games", (f,))
        self.view_buttons["all"].click()

    def _order_game(self, game_id: str, want: str) -> None:
        """Its recap reel, hands-off: the desk finds the footage, ranks the plays and makes it."""
        def ok(result) -> None:
            msg = result.get("message") if isinstance(result, dict) else ""
            notes = result.get("notes") if isinstance(result, dict) else None
            self.played.said(" ".join([msg or "Asked for."] + [str(n) for n in notes or []]))
            if msg:
                self.notice.emit(msg)
            self.refresh(force=True)

        def fail(exc: Exception) -> None:
            text = error_text(exc)
            self.played.said(text)
            self.notice.emit(text)

        self.backend.call("order_game", game_id, want, ok=ok, fail=fail)

    def _studio(self, game: dict, hl: list[dict]) -> None:
        clips = [{"start": h["start"], "end": h["end"], "score": h.get("score") or 0, "title": h.get("title") or "",
                  "reason": h.get("why") or "", "origin": "manual", "edited": True} for h in hl]
        self.open_studio.emit(game.get("url") or "", game.get("title") or "", clips)

    def _picture_ready(self, key: str) -> None:
        for row in self.rows.values():
            row.picture_ready(key)

    def shutdown(self) -> None:
        self.timer.stop()
        self.pictures.shutdown()
        calls = getattr(self.backend, "calls", None)
        if calls is not None:
            calls.shutdown()

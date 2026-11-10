"""Research tab: what's trending by region right now, scored for how well it will clip.

A command center in three views over the same filters: the opportunity matrix (a momentum heatmap over a card
per cluster, research_dashboard.py), the table of every video, and Live arbitrage (taking off right now and not
clipped by you yet). The panel on the right inspects the video picked in any of them: its Most replayed graph
with the likely clips, its viral potential and its clip-ability. The numbers are analytics.py's.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu,
                               QPushButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import analytics, clipability, fresh, learn, lookup, categories, steps
from ..analytics import VIRAL_HELP, VIRAL_LABELS, VIRAL_WEIGHTS
from ..clipability import FIT, PART_LABELS, WEIGHTS, age_text, compact, verdict
from ..organize import stamp
from ..policy import Decision, PickLog, Policy, decide, language_codes, picks_path
from ..research import ScanCancelled, ScanOptions, ScanResult, history_path, last_result, scan, youtube_key
from ..settings import Settings
from ..trending import CATEGORIES, KEY_HELP_URL, REGIONS, History, TrendingVideo, deep_check, parse_regions
from . import theme
from .research_dashboard import (CategoryPills, OpportunityMatrix, ReplayGraph, ViewSwitch, delta_colour,
                                 longevity_colour, replay_legend, score_colour)
from .status_bars import LabeledBar

# The order the cells are filled in. The header shows them in another (_build moves Match, Viral, Momentum and
# Lifespan beside what they belong with), so a column's number never changes when one is added.
COLUMNS = ("Score", "Video", "Channel", "Found in", "Views", "Views / h", "Engagement", "Length", "Posted", "Category",
           "Pick", "Seen", "Match", "Viral", "Momentum", "Lifespan")
TITLE_COLUMN = COLUMNS.index("Video")
VIEWS_COLUMN = COLUMNS.index("Views")
PICK_COLUMN = COLUMNS.index("Pick")
SEEN_COLUMN = COLUMNS.index("Seen")
CATEGORY_COLUMN = COLUMNS.index("Category")
MATCH_COLUMN = COLUMNS.index("Match")  # only for a request's results
VIRAL_COLUMN = COLUMNS.index("Viral")
MOMENTUM_COLUMN = COLUMNS.index("Momentum")
LIFESPAN_COLUMN = COLUMNS.index("Lifespan")
NUMBER_COLUMNS = range(VIEWS_COLUMN, CATEGORY_COLUMN)  # right-aligned
# Columns its header's menu can hide (Score and Video always show; Category and Match show when they apply)
CHOOSABLE = tuple(c for c in range(len(COLUMNS)) if c not in (0, TITLE_COLUMN, CATEGORY_COLUMN, MATCH_COLUMN))
TITLE_MIN = 230  # the Video column never gets narrower than this: the table scrolls sideways instead
VIEWS = ("matrix", "table", "arbitrage")
ORIGIN_LABELS = {"watchlist": "Your list", "cc": "CC search", "request": "Your request"}
SINCE_CHOICES = (("any", "Any time"), ("year", "This year"), ("month", "This month"), ("week", "This week"))
FIND_EXAMPLE = "Call of Duty MW4 with good comedy"
MIN_LENGTHS = ((0.0, "Any length"), (3.0, "3 min +"), (8.0, "8 min +"), (20.0, "20 min +"), (45.0, "45 min +"))
SORT_ROLE = Qt.UserRole + 1
NEW_HOURS = 24  # a trending video first seen this recently counts as new


class _Job(QThread):
    """Runs one function off the GUI thread (research is network-bound, so it doesn't
    queue behind downloads and encodes in the Studio's task queue)."""

    progress = Signal(str, float)
    log = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[["_Job"], Any]):
        super().__init__()
        self.fn = fn
        self.cancel_requested = False

    def run(self) -> None:
        try:
            self.succeeded.emit(self.fn(self))
        except ScanCancelled:
            self.failed.emit("Cancelled")
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)


class _SortItem(QTableWidgetItem):
    """Sorts by the number stored under SORT_ROLE instead of the display text."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        a, b = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if a is None or b is None:  # (QTableWidgetItem's own __lt__ comes back here: compare the text)
            return self.text() < other.text()
        return a < b


class _Bar(QWidget):
    """A thin horizontal meter for one score part."""

    def __init__(self, value: float | None, colour: str):
        super().__init__()
        self.value, self.colour = value, colour
        self.setFixedHeight(8)
        self.setMinimumWidth(80)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        r = QRectF(self.rect())
        p.setBrush(QColor(theme.BORDER))
        p.drawRoundedRect(r, 4, 4)
        if self.value:
            p.setBrush(QColor(self.colour))
            p.drawRoundedRect(QRectF(r.x(), r.y(), r.width() * min(1.0, self.value), r.height()), 4, 4)


def market_lines(v: TrendingVideo) -> list[str]:
    """The market side of a video (research/velocity.py) as lines of rich text for the detail panel."""
    p = v.pace or {}
    out: list[str] = []
    if v.badge:
        mark = CategoryPills.BADGE_MARKS.get(v.badge, "")
        out.append(f"<b>{mark} {_esc(v.badge)}</b> · opportunity {v.opportunity:.0f}/100. {_esc(p.get('why') or '')}")
    if p.get("delta"):
        basis = {"channel": "its channel's 7-day median", "category": "its category in this scan",
                 "scan": "this scan's median"}.get(p.get("basis", ""), "the usual")
        out.append(f"Velocity delta <b>{p['delta']:g}x</b>: {compact(p.get('velocity', 0))} views/hour against "
                   f"{compact(p.get('baseline', 0))} ({_esc(basis)}, {p.get('peers', 0)} videos).")
    if p.get("saturation") is not None:
        out.append(f"Saturation {p['saturation'] * 100:.0f}%: {p.get('shorts', 0)} YouTube Shorts of it by other "
                   "channels (TikTok and Reels can't be checked).")
    elif p.get("delta"):
        out.append("Saturation not checked (Settings → Research → Saturation checks per scan).")
    return out


class Research(QWidget):
    send = Signal(list)  # video URLs for the Studio
    event_search = Signal(dict, str)  # a video and the moment to search it for (the header's event bar)
    queue = Signal(list)  # (url, title, category) for Automation's queue
    notice = Signal(str)  # a line for the main status bar
    log = Signal(str)
    hits_changed = Signal(int)  # new high scorers the user hasn't looked at yet
    open_settings = Signal(str)  # a Settings tab to show (the picking rules)

    def __init__(self, settings: Callable[[], Settings], change: Callable[[str, Any], None], parent=None):
        super().__init__(parent)
        self._settings = settings
        self._change = change
        self.history = History(history_path())
        self.picks = PickLog(picks_path())
        self.result: ScanResult | None = None  # the last trending check
        self.found: lookup.LookupResult | None = None  # the last request's results
        self.mode = "trending"  # what the table shows: "trending" or "find" (a request's results)
        self.videos: list[TrendingVideo] = []
        self._used: set[str] = set()  # videos already picked, queued, clipped or sent (fresh.used_ids)
        self._category_names: dict[str, str] = {}
        self.decisions: dict[str, Decision] = {}
        self.analysis: dict[str, analytics.Insight] = {}  # video id -> its dashboard numbers (analytics.py)
        self.cluster_filter: str | None = None  # a cluster picked from the matrix, shown on its own
        self.view = "matrix"  # matrix | table | arbitrage
        self._matrix_due = False  # the matrix needs redrawing next time it shows
        self._matrix_timer = QTimer(self, singleShot=True, interval=150)  # (typing in the filter box)
        self._matrix_timer.timeout.connect(self.refresh_matrix)
        self.job: _Job | None = None
        self.side_job: _Job | None = None
        self.hits = 0
        self._again = False
        self._error = ""
        self._shown: TrendingVideo | None = None
        self._thumbs: dict[str, QPixmap] = {}
        self._thumb_ids: dict[str, str] = {}  # thumbnail URL -> video id, while downloading
        self._net = QNetworkAccessManager(self)
        self._net.finished.connect(self._thumb_loaded)

        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(lambda: self.check(auto=True))
        self._debounce = QTimer(self, singleShot=True, interval=1200)
        self._debounce.timeout.connect(lambda: self.check())
        self._ticker = QTimer(self, interval=30_000)
        self._ticker.timeout.connect(self._update_summary)
        self._ticker.start()

        self._build()
        self.sync_from_settings()
        self.found = lookup.last_result()
        self._recent.model().setStringList(lookup.recent_requests())
        self.result = last_result(self.history)
        if self.result:
            self._show_result(self.result)
        self._update_find_bar()
        self._update_summary()
        self._schedule(first=True)

    # ================================================================== UI
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("Research")
        title.setObjectName("title")
        self.summary = QLabel()
        self.summary.setProperty("muted", True)
        self.summary.setTextFormat(Qt.RichText)
        self.check_btn = QPushButton("⟳  Check now")
        self.check_btn.setObjectName("primary")
        self.check_btn.clicked.connect(lambda: self.check())
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.cancel)
        top.addWidget(title)
        top.addWidget(self.summary, 1)
        top.addWidget(self.cancel_btn)
        top.addWidget(self.check_btn)
        layout.addLayout(top)

        finder = QHBoxLayout()
        ask = QLabel("🔎  Find videos")
        ask.setToolTip("Describe the videos you want and ClipMonster searches YouTube for them")
        self.find_box = QLineEdit()
        self.find_box.setPlaceholderText(f"Describe what you want, like: {FIND_EXAMPLE}")
        self.find_box.setClearButtonEnabled(True)
        self.find_box.setMaxLength(lookup.MAX_REQUEST)
        self.find_box.setToolTip("What the videos are about and what kind you want. With a Claude or Gemini key "
                                 "(Settings → AI), the AI writes the searches and rates how well each video matches; without one, the words do. "
                                 "Each request uses about 200 of the YouTube API's 10,000 free daily units")
        self.find_box.returnPressed.connect(self.find)
        self._recent = QCompleter([], self)
        self._recent.setCaseSensitivity(Qt.CaseInsensitive)
        self._recent.setFilterMode(Qt.MatchContains)
        self.find_box.setCompleter(self._recent)
        self.find_since = QComboBox()
        for key, name in SINCE_CHOICES:
            self.find_since.addItem(name, key)
        self.find_since.setToolTip("Only videos posted in this time")
        self.find_btn = QPushButton("Find")
        self.find_btn.setObjectName("primary")
        self.find_btn.clicked.connect(self.find)
        finder.addWidget(ask)
        finder.addWidget(self.find_box, 1)
        finder.addWidget(self.find_since)
        finder.addWidget(self.find_btn)
        layout.addLayout(finder)

        self.find_bar = QFrame()
        self.find_bar.setObjectName("panel")
        fb = QHBoxLayout(self.find_bar)
        fb.setContentsMargins(12, 6, 8, 6)
        self.find_text = QLabel()
        self.find_text.setWordWrap(True)
        self.find_text.setTextFormat(Qt.RichText)
        self.back_btn = QPushButton("← Back to trending")
        self.back_btn.clicked.connect(self.show_trending)
        self.last_find_btn = QPushButton()
        self.last_find_btn.clicked.connect(self.show_found)
        self.more_btn = QPushButton("➕  Find more")
        self.more_btn.setToolTip("Search again with the request's next searches and another sort order, leaving out "
                                 "every video already listed or used. About 200 API units")
        self.more_btn.clicked.connect(lambda: self.find(more=True))
        fb.addWidget(self.find_text, 1)
        fb.addWidget(self.more_btn)
        fb.addWidget(self.last_find_btn)
        fb.addWidget(self.back_btn)
        layout.addWidget(self.find_bar)

        filters = QHBoxLayout()
        self.regions_btn = QPushButton()
        self.regions_btn.setToolTip("Which countries' trending charts to check")
        self.regions_menu = QMenu(self)
        self.regions_menu.aboutToShow.connect(self._fill_regions_menu)
        self.regions_btn.setMenu(self.regions_menu)
        self.category = QComboBox()
        for cid, name in CATEGORIES.items():
            self.category.addItem(name, cid)
        self.category.activated.connect(lambda _i: self._filter_changed("research_category",
                                                                        self.category.currentData()))
        self.min_len = QComboBox()
        for minutes, name in MIN_LENGTHS:
            self.min_len.addItem(name, minutes)
        self.min_len.setToolTip("Short videos have little to cut into clips")
        self.min_len.activated.connect(lambda _i: self._filter_changed("research_min_minutes",
                                                                       self.min_len.currentData()))
        self.auto = QCheckBox("Keep checking every")
        self.auto.toggled.connect(self._auto_toggled)
        self.interval = QSpinBox()
        self.interval.setRange(10, 720)
        self.interval.setSingleStep(5)
        self.interval.setSuffix(" min")
        self.interval.valueChanged.connect(self._interval_changed)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by title or channel")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        self.hide_used = QCheckBox("Hide used")
        self.hide_used.setToolTip("Hide videos you've already used: picked, queued or clipped by automation, or sent "
                                  "to the Studio")
        self.hide_used.toggled.connect(self._hide_used_toggled)
        self.hide_sent = self.hide_used  # its name before 2.9
        self.new_only = QCheckBox("New only")
        self.new_only.setToolTip(f"Only videos first seen in the last {NEW_HOURS} hours (for a request: the ones it "
                                 "hadn't shown before)")
        self.new_only.toggled.connect(self._apply_filter)
        self.category_filter = QComboBox()  # hidden: the category pills choose from it
        self.category_filter.setToolTip("Show the videos that fit one category")
        self.category_filter.activated.connect(lambda _i: self._apply_filter())
        self._has_categories = False
        self.picks_only = QCheckBox("Picks only")
        self.picks_only.setToolTip("Show only the videos your picking rules would clip")
        self.picks_only.toggled.connect(self._apply_filter)
        self.rules_btn = QPushButton("Picking rules…")
        self.rules_btn.setToolTip("Which videos may be clipped without asking: your channel list, Creative Commons "
                                  "searches, limits and blocks")
        self.rules_btn.clicked.connect(lambda: self.open_settings.emit("Picking"))
        filters.addWidget(QLabel("Regions"))
        filters.addWidget(self.regions_btn)
        filters.addWidget(self.category)
        filters.addWidget(self.min_len)
        filters.addSpacing(10)
        filters.addWidget(self.auto)
        filters.addWidget(self.interval)
        filters.addStretch(1)
        filters.addWidget(self.category_filter)
        filters.addWidget(self.hide_used)
        filters.addWidget(self.new_only)
        filters.addWidget(self.picks_only)
        filters.addWidget(self.rules_btn)
        layout.addLayout(filters)

        layout.addWidget(self._build_key_panel())

        self.bar = LabeledBar(height=14)
        self.bar.setVisible(False)
        self._tracker = steps.Tracker()
        layout.addWidget(self.bar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("panel")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(False)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeToContents)
        head.setSectionResizeMode(TITLE_COLUMN, QHeaderView.Fixed)  # _fit_title gives it the room left
        head.setStyleSheet("QHeaderView::section { padding: 6px 5px; }")
        head.setSectionsClickable(True)
        head.setContextMenuPolicy(Qt.CustomContextMenu)
        head.customContextMenuRequested.connect(self._column_menu)
        head.setToolTip("Right-click to choose the columns")
        self._fit_timer = QTimer(self, singleShot=True, interval=0)
        self._fit_timer.timeout.connect(self._fit_title)
        head.sectionResized.connect(lambda i, _o, _n: i != TITLE_COLUMN and self._fit_timer.start())
        self.table.installEventFilter(self)
        head.moveSection(head.visualIndex(MATCH_COLUMN), 1)  # a request's match goes beside the score
        head.moveSection(head.visualIndex(VIRAL_COLUMN), head.visualIndex(MATCH_COLUMN) + 1)  # then viral potential
        head.moveSection(head.visualIndex(MOMENTUM_COLUMN), head.visualIndex(COLUMNS.index("Views / h")) + 1)
        head.moveSection(head.visualIndex(LIFESPAN_COLUMN), head.visualIndex(COLUMNS.index("Posted")) + 1)
        for col, tip in ((VIRAL_COLUMN, "Viral potential out of 100: 35% velocity, 25% Most replayed peakiness, 20% "
                                        "engagement, 20% how uncrowded its cluster is"),
                         (MOMENTUM_COLUMN, "Views an hour lately against its last day (or its average since upload)"),
                         (LIFESPAN_COLUMN, "Ephemeral: stale within about 48 hours. Evergreen: still worth a clip "
                                           "next week"),
                         (0, "Clip-ability out of 100: how well it will cut into clips")):
            head_item = self.table.horizontalHeaderItem(col)
            if head_item:
                head_item.setToolTip(tip)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.itemSelectionChanged.connect(self._selected)
        self.table.itemDoubleClicked.connect(lambda _item: self._send_selected())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)

        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setProperty("muted", True)

        # The views: the opportunity matrix, and the table (which Live arbitrage shows filtered)
        self.matrix = OpportunityMatrix()
        self.matrix.open_cluster.connect(self.show_cluster)
        self.matrix.open_video.connect(self.select_video)
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(12, 8, 12, 8)
        self.banner_text = QLabel()
        self.banner_text.setWordWrap(True)
        self.banner_text.setTextFormat(Qt.RichText)
        bl.addWidget(self.banner_text, 1)
        table_page = QWidget()
        tv = QVBoxLayout(table_page)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(8)
        tv.addWidget(self.banner)
        tv.addWidget(self.table, 1)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.matrix)
        self.stack.addWidget(table_page)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.stack, 1)
        lv.addWidget(self.empty, 1)
        row = QHBoxLayout()
        self.send_btn = QPushButton("➜  Send selected to Studio")
        self.send_btn.clicked.connect(self._send_selected)
        self.queue_btn = QPushButton("⏵  Queue in Automation")
        self.queue_btn.setToolTip("Automation clips these next, in their category's style, with your clip length")
        self.queue_btn.clicked.connect(lambda: self._queue(self._selected_videos()))
        self.send_picks_btn = QPushButton()
        self.send_picks_btn.setToolTip("Send every video your picking rules picked to the Studio. They count "
                                       "towards your daily and per-channel limits")
        self.send_picks_btn.clicked.connect(self._send_picks)
        self.count = QLabel()
        self.count.setProperty("muted", True)
        row.addWidget(self.count, 1)
        row.addWidget(self.send_picks_btn)
        row.addWidget(self.queue_btn)
        row.addWidget(self.send_btn)
        lv.addLayout(row)

        # Category pills and the view switch, above everything they scope
        dash = QHBoxLayout()
        dash.setSpacing(10)
        self.pills = CategoryPills()
        self.pills.picked.connect(self._pill_picked)
        self.pills.cluster_cleared.connect(lambda: self.show_cluster(None))
        self.views = ViewSwitch()
        self.views.changed.connect(self.set_view)
        self.search.setFixedWidth(250)
        self.event_box = QLineEdit()
        self.event_box.setPlaceholderText("Search by in-game event: red card, buzzer beater…")
        self.event_box.setClearButtonEnabled(True)
        self.event_box.setFixedWidth(280)
        self.event_box.setToolTip("Describe a moment (“red card after a VAR check”, “technical foul on the bench”) and "
                                  "the video selected below is sent to Sports and searched for it: its commentary, "
                                  "and its official play-by-play when there is one. What's found shows in Sports, "
                                  "ready to make clips or a reel of.")
        self.event_box.returnPressed.connect(self._event_search)
        dash.addWidget(self.pills, 1)
        dash.addWidget(self.event_box)
        dash.addWidget(self.search)
        dash.addWidget(self.views)
        layout.addLayout(dash)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self._build_detail())
        split.setStretchFactor(0, 1)
        split.setSizes([980, 420])
        layout.addWidget(split, 1)

        self.hint = QLabel()
        self.hint.setProperty("muted", True)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

    def _build_key_panel(self) -> QFrame:
        self.key_panel = QFrame()
        self.key_panel.setObjectName("panel")
        lay = QHBoxLayout(self.key_panel)
        lay.setContentsMargins(12, 10, 12, 10)
        text = QLabel("<b>Add a YouTube Data API key to start.</b> It's free: "
                      f"<a href='{KEY_HELP_URL}' style='color:{theme.ACCENT_2}'>enable YouTube Data API v3</a> "
                      "in a Google Cloud project, create an API key under Credentials, and paste it here. "
                      "Checking four regions every 30 minutes uses about 2% of the free daily quota.")
        text.setWordWrap(True)
        text.setOpenExternalLinks(True)
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.Password)
        self.key_input.setPlaceholderText("AIza…")
        self.key_input.setMinimumWidth(260)
        self.key_input.returnPressed.connect(self._save_key)
        save = QPushButton("Save and check")
        save.setObjectName("primary")
        save.clicked.connect(self._save_key)
        lay.addWidget(text, 1)
        lay.addWidget(self.key_input)
        lay.addWidget(save)
        return self.key_panel

    def _build_detail(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)
        self.thumb = QLabel()
        self.thumb.setFixedSize(QSize(352, 198))
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet(f"background:{theme.BG}; border-radius:8px; color:{theme.MUTED};")
        self.d_title = QLabel()
        self.d_title.setObjectName("h2")
        self.d_title.setWordWrap(True)
        self.d_title.setTextFormat(Qt.PlainText)
        self.d_meta = QLabel()
        self.d_meta.setProperty("muted", True)
        self.d_meta.setWordWrap(True)
        self.d_meta.setTextFormat(Qt.RichText)

        self.d_pick = QLabel()
        self.d_pick.setWordWrap(True)
        self.d_pick.setTextFormat(Qt.RichText)

        score_row = QHBoxLayout()
        self.d_score = QLabel()
        self.d_verdict = QLabel()
        self.d_verdict.setWordWrap(True)
        self.d_viral = QLabel()
        self.d_viral.setToolTip("Viral potential out of 100: how likely a clip of it is to take off right now")
        self.d_viral_caption = QLabel()
        self.d_viral_caption.setWordWrap(True)
        self.d_viral_caption.setTextFormat(Qt.RichText)
        for number, caption in ((self.d_score, self.d_verdict), (self.d_viral, self.d_viral_caption)):
            block = QVBoxLayout()
            block.setSpacing(0)
            block.addWidget(number)
            block.addWidget(caption)
            block.addStretch(1)
            score_row.addLayout(block, 1)

        self.parts_box = QWidget()
        self.parts_grid = QGridLayout(self.parts_box)
        self.parts_grid.setContentsMargins(0, 0, 0, 0)
        self.parts_grid.setVerticalSpacing(6)
        self.parts_grid.setColumnStretch(1, 1)

        self.d_reasons = QLabel()
        self.d_reasons.setWordWrap(True)
        self.d_reasons.setTextFormat(Qt.RichText)

        buttons = QGridLayout()
        self.d_send = QPushButton("➜  Send to Studio")
        self.d_send.setObjectName("primary")
        self.d_send.clicked.connect(lambda: self._send([self._shown] if self._shown else []))
        self.d_queue = QPushButton("⏵  Queue in Automation")
        self.d_queue.setToolTip("Automation clips it next, in its category's style, with your clip length")
        self.d_queue.clicked.connect(lambda: self._queue([self._shown] if self._shown else []))
        self.d_open = QPushButton("Open on YouTube")
        self.d_open.clicked.connect(lambda: self._shown and QDesktopServices.openUrl(QUrl(self._shown.url)))
        self.d_copy = QPushButton("Copy link")
        self.d_copy.clicked.connect(lambda: self._shown and QApplication.clipboard().setText(self._shown.url))
        self.d_deep = QPushButton("Check replays")
        self.d_deep.setToolTip("Read this video's Most replayed graph now (no quota used)")
        self.d_deep.clicked.connect(self._check_one)
        buttons.addWidget(self.d_send, 0, 0)
        buttons.addWidget(self.d_queue, 0, 1)
        buttons.addWidget(self.d_open, 1, 0)
        buttons.addWidget(self.d_copy, 1, 1)
        buttons.addWidget(self.d_deep, 2, 0, 1, 2)

        # The replay graph inspector
        replay_head = QHBoxLayout()
        replay_title = QLabel("Most replayed")
        replay_title.setObjectName("h2")
        replay_head.addWidget(replay_title)
        replay_head.addStretch(1)
        replay_head.addWidget(replay_legend())
        self.replay = ReplayGraph()
        self.d_windows = QLabel()
        self.d_windows.setWordWrap(True)
        self.d_windows.setTextFormat(Qt.RichText)
        self.d_windows.setProperty("muted", True)
        self.d_windows.setTextInteractionFlags(Qt.TextSelectableByMouse)

        # Viral potential
        viral_title = QLabel("Viral potential")
        viral_title.setObjectName("h2")
        self.d_momentum = QLabel()
        self.d_momentum.setWordWrap(True)
        self.d_momentum.setTextFormat(Qt.RichText)
        self.d_life = QLabel()
        self.d_life.setWordWrap(True)
        self.d_life.setTextFormat(Qt.RichText)
        self.d_flags = QLabel()
        self.d_flags.setWordWrap(True)
        self.d_flags.setTextFormat(Qt.RichText)
        self.viral_box = QWidget()
        self.viral_grid = QGridLayout(self.viral_box)
        self.viral_grid.setContentsMargins(0, 0, 0, 0)
        self.viral_grid.setVerticalSpacing(6)
        self.viral_grid.setColumnStretch(1, 1)
        clip_title = QLabel("Clip-ability")
        clip_title.setObjectName("h2")

        v.addWidget(self.thumb, 0, Qt.AlignHCenter)
        v.addWidget(self.d_title)
        v.addWidget(self.d_meta)
        v.addLayout(score_row)
        v.addWidget(self.d_pick)
        v.addLayout(buttons)
        v.addSpacing(6)
        v.addLayout(replay_head)
        v.addWidget(self.replay)
        v.addWidget(self.d_windows)
        v.addSpacing(6)
        v.addWidget(viral_title)
        v.addWidget(self.d_momentum)
        v.addWidget(self.d_life)
        v.addWidget(self.d_flags)
        v.addWidget(self.viral_box)
        v.addSpacing(6)
        v.addWidget(clip_title)
        v.addWidget(self.parts_box)
        v.addWidget(self.d_reasons)
        v.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        scroll.setMinimumWidth(400)
        frame = QFrame()
        frame.setObjectName("panel")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(scroll)
        self._show_detail(None)
        return frame

    # ================================================================== settings
    def sync_from_settings(self) -> None:
        """Show the current settings in the filter controls (after the Settings dialog, too)."""
        s = self._settings()
        for w in (self.category, self.min_len, self.auto, self.interval, self.hide_used):
            w.blockSignals(True)
        self.hide_used.setChecked(s.research_hide_used)
        i = self.category.findData(s.research_category)
        self.category.setCurrentIndex(max(0, i))
        i = self.min_len.findData(float(s.research_min_minutes))
        if i < 0:
            self.min_len.addItem(f"{s.research_min_minutes:g} min +", float(s.research_min_minutes))
            i = self.min_len.count() - 1
        self.min_len.setCurrentIndex(i)
        self.auto.setChecked(s.research_auto)
        self.interval.setValue(s.research_interval)
        self.interval.setEnabled(s.research_auto)
        for w in (self.category, self.min_len, self.auto, self.interval, self.hide_used):
            w.blockSignals(False)
        regions = parse_regions(s.research_regions)
        self.regions_btn.setText(", ".join(regions) if regions else "Choose…")
        self._fill_category_filter(s)
        self.key_panel.setVisible(not youtube_key(s))
        self._apply_columns()
        self.set_view(s.research_view if s.research_view in VIEWS else "matrix", save=False)
        self._update_summary()
        self._apply_filter()

    def _fill_category_filter(self, s: Settings) -> None:
        all_categories = categories.load(s)
        self._category_names = {n.id: n.name for n in all_categories}
        keep = self.category_filter.currentData()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("All categories", None)
        for n in all_categories:
            self.category_filter.addItem(n.name, n.id)
        self.category_filter.addItem("Fits no category", "")
        self.category_filter.setCurrentIndex(max(0, self.category_filter.findData(keep)))
        self.category_filter.blockSignals(False)
        self.category_filter.setVisible(False)  # the pills above the views pick from it
        self._has_categories = bool(all_categories)
        self.table.setColumnHidden(CATEGORY_COLUMN, not all_categories)
        self._update_pills()

    def settings_changed(self) -> None:
        self.sync_from_settings()
        self.refresh_picks()
        self._schedule()

    def _filter_changed(self, name: str, value: Any) -> None:
        if getattr(self._settings(), name) == value:
            return
        self._change(name, value)
        self.sync_from_settings()
        if youtube_key(self._settings()):
            self._debounce.start()  # wait for the user to finish clicking around

    def _hide_used_toggled(self, on: bool) -> None:
        if self._settings().research_hide_used != on:
            self._change("research_hide_used", on)
        self._apply_filter()

    def _fill_regions_menu(self) -> None:
        self.regions_menu.clear()
        chosen = parse_regions(self._settings().research_regions)
        for code in chosen + [c for c in REGIONS if c not in chosen]:
            act = self.regions_menu.addAction(f"{code}   {REGIONS.get(code, '')}")
            act.setCheckable(True)
            act.setChecked(code in chosen)
            act.toggled.connect(lambda on, c=code: self._toggle_region(c, on))
        self.regions_menu.addSeparator()
        self.regions_menu.addAction("Type country codes…", self._type_regions)

    def _toggle_region(self, code: str, on: bool) -> None:
        regions = parse_regions(self._settings().research_regions)
        if on and code not in regions:
            regions.append(code)
        elif not on and code in regions:
            regions.remove(code)
        self._filter_changed("research_regions", ", ".join(regions))

    def _type_regions(self) -> None:
        text, ok = QInputDialog.getText(self, "Regions", "Two-letter country codes, comma separated:",
                                        text=self._settings().research_regions)
        if ok:
            self._filter_changed("research_regions", ", ".join(parse_regions(text)))

    def _auto_toggled(self, on: bool) -> None:
        self._change("research_auto", on)
        self.interval.setEnabled(on)
        self._schedule()
        self._update_summary()

    def _interval_changed(self, minutes: int) -> None:
        self._change("research_interval", minutes)
        self._schedule()
        self._update_summary()

    def _save_key(self) -> None:
        key = self.key_input.text().strip()
        if not key:
            return
        self._change("youtube_api_key", key)
        self.key_input.clear()
        self.sync_from_settings()
        self.check()

    # ================================================================== scanning
    def _schedule(self, first: bool = False) -> None:
        """Arm the auto-check timer: due now (shortly after startup) or interval after the last scan."""
        self.timer.stop()
        s = self._settings()
        if not (s.research_auto and youtube_key(s)):
            return
        last = self.result.time if self.result else 0.0
        due = last + s.research_interval * 60 - time.time()
        self.timer.start(int(max(4.0 if first else 1.0, due) * 1000))

    def check(self, auto: bool = False) -> None:
        if self.job:
            self._again = not auto  # filters changed mid-scan: check again when it finishes
            return
        s = self._settings()
        if not youtube_key(s):
            self.key_panel.setVisible(True)
            self.key_input.setFocus()
            return
        self._debounce.stop()
        opts = ScanOptions.from_settings(s)
        # Most-replayed checks load YouTube pages. While YouTube has asked this PC to sign in there
        # are none, and checks that run by themselves are spaced out like automation's.
        from ..jobs import JobDB
        from ..pipeline import DEEP_PAUSE, paused_until

        db = JobDB()
        try:
            if paused_until(db, time.time()):
                opts.deep_count = 0
        finally:
            db.close()
        if auto:
            opts.deep_workers, opts.deep_pause = 1, DEEP_PAUSE
        history = self.history
        job = _Job(lambda j: scan(opts, history, j.log.emit,
                                  lambda m, f: j.progress.emit(m, -1.0 if f is None else float(f)),
                                  lambda: j.cancel_requested))
        job.progress.connect(self._progress)
        job.log.connect(self.log.emit)
        job.succeeded.connect(lambda r: self._scan_done(r, auto))
        job.failed.connect(self._scan_failed)
        job.finished.connect(self._job_finished)
        self._start(job, "Checking…")

    def _start(self, job: "_Job", text: str) -> None:
        self.job = job
        self.check_btn.setEnabled(False)
        self.find_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.bar.setVisible(True)
        self.bar.set_fraction(None)
        self._tracker = steps.Tracker()  # for the time left
        self.summary.setText(text)
        job.start()

    # ================================================================== finding videos for a request
    def find(self, more: bool = False) -> None:
        """Search YouTube for what's typed in the Find box (lookup.py). Each search for the same request
        takes its next angles and sort order (fresh.py); more=True adds to the request's results,
        leaving out every video they already list."""
        if more and not self.found:
            return
        request = self.found.request if more else lookup.tidy(self.find_box.text())
        if not request:
            self.find_box.setFocus()
            self.notice.emit(f"Type what you're looking for, like: {FIND_EXAMPLE}")
            return
        if self.job:
            self.notice.emit("Research is busy checking. Find again when it's done.")
            return
        s = self._settings()
        key = youtube_key(s)
        if not key:
            self.key_panel.setVisible(True)
            self.key_input.setFocus()
            return
        since = self.find_since.currentData() or "any"
        language = next(iter(language_codes(s.pick_languages)), "")
        memory = fresh.load()
        skip = set(fresh.used_now(self.history))
        if more:
            skip |= {v.id for v in self.found.videos}

        def run(j: "_Job") -> "lookup.LookupResult":
            result = lookup.find(request, s, key, since, s.research_min_minutes, language, j.log.emit,
                                 lambda m, f: j.progress.emit(m, -1.0 if f is None else float(f)),
                                 lambda: j.cancel_requested, memory=memory,
                                 request_key=fresh.request_key(request), skip=skip)
            memory.save()
            return result

        job = _Job(run)
        job.progress.connect(self._progress)
        job.log.connect(self.log.emit)
        job.succeeded.connect(lambda r: self._find_done(r, more))
        job.failed.connect(self._find_failed)
        job.finished.connect(self._job_finished)
        self._start(job, f"Finding {'more ' if more else ''}videos for “{_esc(request[:60])}”…")

    def _find_done(self, result: "lookup.LookupResult", more: bool = False) -> None:
        added = len(result.videos)
        if more and self.found is not None and self.found.request == result.request:
            before = self.found
            have = {v.id for v in before.videos}
            result.videos = before.videos + [v for v in result.videos if v.id not in have]
            result.videos.sort(key=lambda v: (lookup.rank(v), v.match), reverse=True)
            result.quota += before.quota
            result.weak += before.weak
            result.skipped = max(0, result.skipped - len(have))  # (it left out the listed ones on purpose)
        try:
            lookup.remember(result)
        except OSError:
            pass  # next time it just isn't suggested
        self._recent.model().setStringList(lookup.recent_requests())
        for v in result.videos:
            v.sent = bool((self.history.videos.get(v.id) or {}).get("sent"))
        self.found = result
        for p in result.problems:
            self.log.emit(f"Find: {p}")
        self.show_found()
        n, new = len(result.videos), len(result.fresh)
        request = _esc(result.request[:60])
        if more:
            self.notice.emit(f"Found {added} more video{'s' if added != 1 else ''} for “{request}”."
                             if added else f"Nothing new for “{request}” this time. Find more again tries the "
                             "next searches")
        else:
            self.notice.emit(f"Found {n} video{'s' if n != 1 else ''} for “{request}”"
                             + (f" ({new} new)" if new != n else "") + "."
                             + (f" Best: {_esc(result.videos[0].title[:60])}" if n else " Try other words."))

    def _find_failed(self, msg: str) -> None:
        if msg != "Cancelled":
            self.notice.emit(f"<span style='color:{theme.WARN}'>⚠ Find: {_esc(msg)}</span>")
            self.log.emit(f"⚠ Find: {msg}")

    def show_found(self) -> None:
        if not self.found:
            return
        self.mode = "find"
        self.find_box.setText(self.found.request)
        self._display(self.found.videos, {})
        self._update_find_bar()

    def show_trending(self) -> None:
        self.mode = "trending"
        self._display(self.result.videos if self.result else [], self.result.notes if self.result else {})
        self._update_find_bar()

    def _update_find_bar(self) -> None:
        finding = self.mode == "find" and self.found is not None
        self.table.setColumnHidden(MATCH_COLUMN, not finding)
        self.back_btn.setVisible(finding)
        self.more_btn.setVisible(finding)
        self.last_find_btn.setVisible(not finding and self.found is not None)
        if self.found is not None:
            self.last_find_btn.setText(f"Show “{self.found.request[:40]}” again")
        self.find_bar.setVisible(self.found is not None)
        if not finding:
            if self.found is not None:
                self.find_text.setText(f"<span style='color:{theme.MUTED}'>Showing what's trending. Your last "
                                       f"request found {len(self.found.videos)} videos.</span>")
            return
        r = self.found
        bits = [f"<b>{len(r.videos)} video{'s' if len(r.videos) != 1 else ''}</b> for “{_esc(r.request)}”"]
        new = len(r.fresh)
        if new and new != len(r.videos):
            bits.append(f"<span style='color:{theme.ACCENT_2}'>{new} new</span>")
        if r.weak:
            bits.append(f"{r.weak} that didn't match left out")
        if r.skipped:
            bits.append(f"{r.skipped} you've already used left out")
        bits.append(f"rated by {_esc(r.rated_by)}" if r.rated_by else "matched by their words (add a Claude or Gemini "
                    "key in Settings → AI to have them rated)")
        bits.append(f"found {age_text((time.time() - r.time) / 3600)} ago" if time.time() - r.time >= 60
                    else "found just now")
        searched = ", ".join(f"“{_esc(q)}”" for q in r.plan.queries)
        if r.plan.order != "relevance":
            searched += f" ({fresh.ORDER_WORDS.get(r.plan.order, r.plan.order)} first)"
        about = f"<br><span style='color:{theme.MUTED}'>{_esc(r.plan.about)} · " if r.plan.about else \
            f"<br><span style='color:{theme.MUTED}'>"
        self.find_text.setText(" · ".join(bits) + about + f"searched {searched}, {r.quota} API units</span>")

    def cancel(self) -> None:
        if self.job:
            self.job.cancel_requested = True

    def _progress(self, msg: str, frac: float) -> None:
        self.summary.setText(msg)
        self.bar.set_status(self._tracker.update(msg, None if frac < 0 else frac))

    def _job_finished(self) -> None:
        if self.job:
            self.job.deleteLater()
        self.job = None
        self.check_btn.setEnabled(True)
        self.find_btn.setEnabled(True)
        self.cancel_btn.setVisible(False)
        self.bar.setVisible(False)
        self._schedule()
        self._update_summary()
        if self._again:
            self._again = False
            self._debounce.start()

    def _scan_done(self, result: ScanResult, auto: bool) -> None:
        self.result = result
        self._error = ""
        if result.blocked:
            from ..jobs import JobDB
            from ..pipeline import record_block

            db = JobDB()
            try:
                self.log.emit(record_block(db, result.blocked, time.time(), self._settings().cookies_browser))
            finally:
                db.close()
        self._show_result(result)
        self._update_summary()
        for p in result.problems:
            self.log.emit(f"Research: {p}")
        alert = self._settings().research_alert
        hot = [v for v in result.new if v.score >= alert]
        if result.first:
            good = [v for v in result.videos if v.score >= alert]
            self.notice.emit(f"Research: scored {len(result.videos)} trending videos, {len(good)} at {alert}+."
                             + (f" Best: {good[0].title[:60]} ({good[0].score:.0f})" if good else ""))
        elif hot:
            if not self.isVisible():
                self.hits += len(hot)
                self.hits_changed.emit(self.hits)
            best = max(hot, key=lambda v: v.score)
            self.notice.emit(f"🔥 Research: {len(hot)} new video{'s' if len(hot) != 1 else ''} worth clipping, "
                             f"best: {best.title[:60]} ({best.score:.0f})")
        elif not auto:
            self.notice.emit(f"Research: checked {len(result.regions)} region"
                             f"{'s' if len(result.regions) != 1 else ''}, {len(result.videos)} videos scored.")

    def _scan_failed(self, msg: str) -> None:
        if msg != "Cancelled":
            self._error = msg
            self.notice.emit(f"<span style='color:{theme.WARN}'>⚠ Research: {_esc(msg)}</span>")
            self.log.emit(f"⚠ Research: {msg}")

    def shutdown(self) -> None:
        self.timer.stop()
        self._debounce.stop()
        for job in (self.job, self.side_job):
            if job:
                job.cancel_requested = True
                if not job.wait(5000):  # stuck in a network call: don't hold up closing the app
                    job.terminate()
                    job.wait(1000)

    # ================================================================== results
    def _update_summary(self) -> None:
        if self.job:
            return
        s = self._settings()
        if not youtube_key(s):
            self.summary.setText("   Needs a YouTube Data API key")
            return
        parts = []
        if self.result:
            ago = time.time() - self.result.time
            parts.append(f"{len(self.result.videos)} videos from {', '.join(self.result.regions)}")
            parts.append(f"checked {age_text(ago / 3600)} ago" if ago >= 60 else "checked just now")
        else:
            parts.append("Not checked yet")
        if s.research_auto and self.timer.isActive():
            mins = max(0, round(self.timer.remainingTime() / 60000))
            parts.append(f"next check in {mins} min" if mins else "next check in under a minute")
        if self._error:
            parts.insert(0, f"<span style='color:{theme.WARN}'>⚠ {_esc(self._error)}</span>")
        self.summary.setText("   " + " · ".join(parts))
        self.summary.setToolTip(self._error)

    def _show_result(self, result: ScanResult) -> None:
        """A trending check finished (or the last one, at startup): show it, unless a request's
        results are showing, which stay until Back to trending."""
        self.result = result
        if self.mode == "trending":
            self._display(result.videos, result.notes)
        else:
            self._update_find_bar()

    def _display(self, videos: list[TrendingVideo], notes: dict) -> None:
        keep = self._shown.id if self._shown else None
        self.videos = videos
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.videos))
        s = self._settings()
        categories.assign_from_settings(self.videos, s, self.history)
        self._mark_used()
        self.decisions = {d.video.id: d for d in decide(self.videos, Policy.from_settings(s), self.picks,
                                                        notes=notes, lessons=learn.for_settings(s))}
        self._analyse()
        if self.cluster_filter and not any(i.cluster == self.cluster_filter for i in self.analysis.values()):
            self.cluster_filter = None  # that cluster isn't in these results
            self.pills.set_cluster(None)
        for r, v in enumerate(self.videos):
            self._fill_row(r, v)
        self.table.setSortingEnabled(True)
        self._sort_for_view()
        self._update_pills()
        self._apply_filter()
        rows = [r for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)]
        target = next((r for r in rows if self._video_at(r) and self._video_at(r).id == keep), None)
        if target is None and rows:
            target = rows[0]
        if target is not None:
            self.table.selectRow(target)
        self._show_detail(self._video_at(target) if target is not None else None)  # also when the row stays 0

    def _mark_used(self) -> None:
        """Mark the videos already used (picked, queued, clipped or sent), for Hide used and the rules."""
        self.picks.reload()
        self._used = fresh.used_now(self.history)
        for v in self.videos:
            v.used = v.id in self._used

    def is_new(self, v: TrendingVideo, now: float | None = None) -> bool:
        """First seen in the last NEW_HOURS (for a request's results: not shown by it before)."""
        if self.mode == "find" or v.match >= 0:
            return v.new
        return v.new or (bool(v.first_seen) and (now or time.time()) - v.first_seen < NEW_HOURS * 3600)

    def _seen_item(self, v: TrendingVideo) -> QTableWidgetItem:
        now = time.time()
        if v.used or v.sent:
            it = _SortItem("Used")
            it.setData(SORT_ROLE, -1.0)
            it.setToolTip("Already picked, queued or clipped by automation, or sent to the Studio")
            it.setForeground(QColor(theme.MUTED))
        elif self.is_new(v, now):
            it = _SortItem("New")
            it.setData(SORT_ROLE, now + (1 if v.new else 0))
            it.setToolTip("Not shown before by this request" if v.match >= 0 else
                          "First seen " + ("in this check" if v.new else
                                           f"{age_text((now - v.first_seen) / 3600)} ago"))
            it.setForeground(QColor(theme.ACCENT_2))
        else:
            first = v.first_seen or now
            it = _SortItem(age_text((now - first) / 3600) if v.first_seen else "Before")
            it.setData(SORT_ROLE, first)
            it.setToolTip("Shown by this request before" if v.match >= 0 else
                          f"Showing up in Research for {age_text((now - first) / 3600)}")
            it.setForeground(QColor(theme.MUTED))
        it.setTextAlignment(Qt.AlignCenter)
        return it

    def _fill_row(self, r: int, v: TrendingVideo) -> None:
        def item(text: str, sort=None, tip: str = "") -> QTableWidgetItem:
            it = _SortItem(text)
            if sort is not None:
                it.setData(SORT_ROLE, sort)
            if tip:
                it.setToolTip(tip)
            return it

        why = "\n".join(f"• {x}" for x in v.reasons)
        score = item(f"{v.score:.0f}", v.score, why)
        score.setForeground(QColor(score_colour(v.score)))
        f = score.font()
        f.setBold(True)
        score.setFont(f)
        score.setTextAlignment(Qt.AlignCenter)
        score.setData(Qt.UserRole, v.id)
        used = v.sent or v.used
        mark = "✓ " if used else ("● " if v.new else "")
        title = item(mark + v.title, v.title.lower(), v.title + (f"\n\n{why}" if why else ""))
        if v.new and not used:
            title.setForeground(QColor(theme.ACCENT_2))
        elif used:
            title.setForeground(QColor(theme.MUTED))
        regions = sorted(v.regions.items(), key=lambda kv: kv[1])
        found = [f"{code} #{rank}" for code, rank in regions[:3]] + (["…"] if len(regions) > 3 else [])
        found += [ORIGIN_LABELS[o] for o in v.origins if o in ORIGIN_LABELS]
        where = item("  ".join(found), v.best_rank or 999,
                     ", ".join([f"{REGIONS.get(c, c)}: #{n}" for c, n in regions]
                               + [ORIGIN_LABELS[o] for o in v.origins if o in ORIGIN_LABELS]))
        eng = (v.parts.get("engagement") or 0)
        rate = ((v.likes or 0) + (v.comments or 0)) / v.views * 100 if v.views and v.likes is not None else None
        cells = [
            score, title, item(v.channel, v.channel.lower()), where,
            item(compact(v.views), v.views),
            item(("↑ " if v.recent else "") + compact(v.velocity), v.velocity,
                 "Measured since the last check" if v.recent else "Average since upload; becomes live momentum "
                 "after the next check"),
            item(f"{rate:.1f}%" if rate is not None else "hidden", eng,
                 "Likes and comments per view" if rate is not None else "This channel hides its like count"),
            item(stamp(v.duration) if v.live == "none" else v.live.upper(), v.duration),
            item(age_text(v.age_hours()), -v.published),
            self._category_item(v),
            self._pick_item(v),
            self._seen_item(v),
            self._match_item(v),
            self._viral_item(v),
            self._momentum_item(v),
            self._lifespan_item(v),
        ]
        for c, cell in enumerate(cells):
            if c in NUMBER_COLUMNS:
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, c, cell)

    def _category_item(self, v: TrendingVideo) -> QTableWidgetItem:
        name = self._category_names.get(v.category_id, "")
        it = _SortItem(name or "–")
        it.setData(SORT_ROLE, name.lower() or "~")  # videos without a category sort last
        it.setToolTip(f"Fits the {name} category" if name else "Fits none of your categories")
        if not name:
            it.setForeground(QColor(theme.MUTED))
        return it

    def _match_item(self, v: TrendingVideo) -> QTableWidgetItem:
        if v.match < 0:
            it = _SortItem("")
            it.setData(SORT_ROLE, -1.0)
            return it
        it = _SortItem(f"{v.match:.0f}")
        it.setData(SORT_ROLE, lookup.rank(v))  # sorts by the match, then by how well it will clip
        it.setToolTip(f"How well it's what you asked for, out of 100: {v.match_why}\n"
                      f"Listed by the match first, then by clip-ability")
        it.setForeground(QColor(score_colour(v.match)))
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        it.setTextAlignment(Qt.AlignCenter)
        return it

    def _pick_item(self, v: TrendingVideo) -> QTableWidgetItem:
        d = self.decisions.get(v.id)
        it = _SortItem("✓ Pick" if d and d.picked else "–" if d else "")
        it.setData(SORT_ROLE, v.score + (1000 if d and d.picked else 0))  # picks sort above the rest
        if d:
            it.setToolTip(("Picked: " if d.picked else "Not picked: ") + d.reason + (f"\n{d.learned}" if d.learned else ""))
            it.setForeground(QColor(theme.GOOD if d.picked else theme.MUTED))
        it.setTextAlignment(Qt.AlignCenter)
        return it

    def _viral_item(self, v: TrendingVideo) -> QTableWidgetItem:
        ins = self.analysis.get(v.id)
        if ins is None:
            it = _SortItem("")
            it.setData(SORT_ROLE, -1.0)
            return it
        it = _SortItem(f"{ins.viral:.0f}")
        it.setData(SORT_ROLE, ins.viral)
        lines = [f"Viral potential {ins.viral:.0f} out of 100"]
        for key in VIRAL_WEIGHTS:
            part = ins.parts.get(key)
            lines.append(f"• {VIRAL_LABELS[key]} ({VIRAL_WEIGHTS[key]:.0%}): "
                         + ("not measured yet" if part is None else f"{part * 100:.0f}"))
        it.setToolTip("\n".join(lines))
        it.setForeground(QColor(score_colour(ins.viral)))
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        it.setTextAlignment(Qt.AlignCenter)
        return it

    def _momentum_item(self, v: TrendingVideo) -> QTableWidgetItem:
        ins = self.analysis.get(v.id)
        m = ins.momentum if ins else None
        text = (m.short() if m else "") + ("  ⚑" if ins and ins.outlier else "") + ("  ⚡" if ins and ins.arbitrage else "")
        it = _SortItem(text.strip())
        it.setData(SORT_ROLE, m.delta if m and m.known else -99.0)  # measuring sorts last
        tip = [m.text()] if m else []
        if ins and ins.outlier:
            tip.append(f"⚑ Outlier: {ins.outlier:g}× the median views an hour of {ins.outlier_of}")
        if ins and ins.arbitrage:
            tip.append(f"⚡ Live arbitrage: {ins.arbitrage_why}")
        it.setToolTip("\n".join(tip))
        it.setForeground(QColor(delta_colour(m.delta if m and m.known else None)))
        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return it

    def _lifespan_item(self, v: TrendingVideo) -> QTableWidgetItem:
        ins = self.analysis.get(v.id)
        it = _SortItem({"ephemeral": "Ephemeral", "evergreen": "Evergreen"}.get(ins.longevity, "") if ins else "")
        it.setData(SORT_ROLE, 0 if ins and ins.longevity == "ephemeral" else 1)
        if ins:
            it.setToolTip(f"{ins.longevity_label}: {ins.longevity_why}")
        return it

    def _analyse(self) -> None:
        """The dashboard's numbers for every video shown (analytics.py), scored against all of them."""
        self.analysis = analytics.insights(self.videos, self._category_names, self.history)
        self._matrix_due = True

    def refresh_picks(self) -> None:
        """Run the picking rules over the current results (after a scan or a rules change)."""
        s = self._settings()
        notes = self.result.notes if self.result else {}
        categories.assign_from_settings(self.videos, s, self.history)  # a category may have changed
        self._mark_used()  # the automation runner may have picked or clipped videos since
        self.decisions = {d.video.id: d for d in decide(self.videos, Policy.from_settings(s), self.picks,
                                                        notes=notes, lessons=learn.for_settings(s))}
        self._analyse()  # clusters follow the categories, and used videos aren't arbitrage
        self.table.setSortingEnabled(False)
        for r in range(self.table.rowCount()):
            v = self._video_at(r)
            if v:
                self.table.setItem(r, CATEGORY_COLUMN, self._category_item(v))
                self.table.setItem(r, PICK_COLUMN, self._pick_item(v))
                self.table.setItem(r, SEEN_COLUMN, self._seen_item(v))
                self.table.setItem(r, VIRAL_COLUMN, self._viral_item(v))
                self.table.setItem(r, MOMENTUM_COLUMN, self._momentum_item(v))
                self.table.setItem(r, LIFESPAN_COLUMN, self._lifespan_item(v))
        self.table.setSortingEnabled(True)
        if self._shown:
            self._show_detail(self._shown)
        self._update_pills()
        self._apply_filter()

    def picked_videos(self) -> list[TrendingVideo]:
        return [v for v in self.videos if (d := self.decisions.get(v.id)) and d.picked]

    def passes(self, v: TrendingVideo, now: float | None = None) -> bool:
        """Whether the filters above the views (search, category pill or cluster, Hide used, New only, Picks
        only) let this video through. Live arbitrage adds its own on top."""
        text = self.search.text().strip().lower()
        d = self.decisions.get(v.id)
        category = self.category_filter.currentData() if self._has_categories else None
        ins = self.analysis.get(v.id)
        return not ((self.hide_used.isChecked() and (v.sent or v.used))
                    or (self.picks_only.isChecked() and not (d and d.picked))
                    or bool(text and text not in v.title.lower() and text not in v.channel.lower())
                    or (category is not None and v.category_id != category)
                    or (self.cluster_filter is not None and (ins is None or ins.cluster != self.cluster_filter))
                    or (self.new_only.isChecked() and not self.is_new(v, now)))

    def _apply_filter(self) -> None:
        hide_used = self.hide_used.isChecked()
        arbitrage = self.view == "arbitrage"
        shown = used = 0
        now = time.time()
        for r in range(self.table.rowCount()):
            v = self._video_at(r)
            is_used = v is not None and (v.sent or v.used)
            used += is_used
            ins = self.analysis.get(v.id) if v else None
            hidden = v is None or not self.passes(v, now) or (arbitrage and not (ins and ins.arbitrage))
            self.table.setRowHidden(r, hidden)
            shown += not hidden
        total = len(self.videos)
        n_picked = len(self.picked_videos())
        racing = sum(1 for i in self.analysis.values() if i.arbitrage and self.passes(i.video, now))
        self.views.set_arbitrage_count(racing)
        self._update_banner(racing)
        if arbitrage:
            self.count.setText(f"{shown} to race for, of {total} videos")
        else:
            self.count.setText((f"{shown} of {total} videos" if shown != total else f"{total} videos")
                               + (f" · {n_picked} picked" if n_picked else "")
                               + (f" · {used} already used{', hidden' if hide_used else ''}" if used else ""))
        has_rows = total > 0
        self.table.setVisible(has_rows)
        self.stack.setVisible(has_rows)
        self.empty.setVisible(not has_rows)
        self._matrix_due = True
        if self.view == "matrix":
            self._matrix_timer.start()
        self.count.setVisible(has_rows)
        # In the matrix, the video being inspected has its own Send and Queue buttons on the right
        self.send_btn.setVisible(has_rows and self.view != "matrix")
        self.queue_btn.setVisible(has_rows and self.view != "matrix")
        self.send_picks_btn.setVisible(has_rows and n_picked > 0)
        self.send_picks_btn.setText(f"➜  Send {n_picked} pick{'s' if n_picked != 1 else ''} to Studio")
        if not has_rows and self.mode == "find" and self.found:
            self.empty.setText(f"Nothing matched “{_esc(self.found.request)}”. Try other words, a longer time, "
                               "or a shorter Skip videos shorter than.")
        elif not has_rows:
            self.empty.setText("Nothing yet. Press Check now to see what's trending."
                               if youtube_key(self._settings()) else
                               "Add your YouTube Data API key above, then ClipMonster checks the trending charts "
                               "for you and scores every video for how well it will clip.")

    # ================================================================== the command center
    def set_view(self, key: str, save: bool = True) -> None:
        """Show the matrix, the table, or the table's Live arbitrage videos."""
        key = key if key in VIEWS else "matrix"
        changed = key != self.view
        self.view = key
        self.views.set_view(key)
        self.stack.setCurrentIndex(0 if key == "matrix" else 1)
        self.banner.setVisible(key == "arbitrage")
        if save and self._settings().research_view != key:
            self._change("research_view", key)
        if changed:
            self._sort_for_view()
        self._apply_filter()
        if key == "matrix":
            self.refresh_matrix()
        if changed:
            self._follow_filter()
        self._update_hint()

    def _sort_for_view(self) -> None:
        if self.view == "arbitrage":
            self.table.sortItems(VIRAL_COLUMN, Qt.DescendingOrder)
        else:
            self.table.sortItems(MATCH_COLUMN if self.mode == "find" else 0, Qt.DescendingOrder)

    def refresh_matrix(self) -> None:
        """Redraw the opportunity matrix from the videos the filters let through."""
        self._matrix_timer.stop()
        self._matrix_due = False
        now = time.time()
        shown = {k: i for k, i in self.analysis.items() if self.passes(i.video, now)}
        found = analytics.clusters(shown, self.history, now)
        self.matrix.show_clusters(found, now, self._matrix_note(found), self._has_categories)
        ins = self.analysis.get(self._shown.id) if self._shown else None
        self.matrix.mark(ins.cluster if ins else "")

    def _matrix_note(self, found: list) -> str:
        if not found:
            return ""
        order = ("your categories first, then YouTube's category for videos that fit none of them, each best first "
                 "by median viral potential" if self._has_categories else "best first by median viral potential")
        return (f"{len(found)} cluster{'s' if len(found) != 1 else ''}: {order}. Click a card for its videos, or a "
                "video to inspect it.")

    def _update_pills(self) -> None:
        """A pill per category, with how many of these videos fit it."""
        if not self._has_categories:
            self.pills.set_items([], None)
            self.pills.setVisible(self.cluster_filter is not None)
            return
        counts: dict[str, int] = {}
        for v in self.videos:
            counts[v.category_id] = counts.get(v.category_id, 0) + 1
        items: list[tuple[str, Any, int]] = []
        for i in range(self.category_filter.count()):
            data = self.category_filter.itemData(i)
            if data is None:
                items.append(("All", None, len(self.videos)))
            else:
                items.append(("Fits none" if data == "" else self.category_filter.itemText(i), data,
                              counts.get(data, 0)))
        from ..research import velocity

        names = {data: label for label, data, _n in items if isinstance(data, str) and data}
        roll = velocity.by_category(self.videos, names)
        self.pills.set_items(items, self.category_filter.currentData(),
                             {cid: (n.badge, n.why) for cid, n in roll.items() if n.badge})
        self.pills.setVisible(True)

    def _pill_picked(self, data: Any) -> None:
        self.category_filter.setCurrentIndex(max(0, self.category_filter.findData(data)) if data is not None else 0)
        self.cluster_filter = None
        self.pills.set_cluster(None)
        self._apply_filter()
        self._follow_filter()

    def show_cluster(self, name: str | None) -> None:
        """Show one cluster's videos in the table (picked from the matrix), or everything again (None)."""
        category = next((cid for cid, n in self._category_names.items() if n == name), None) if name else None
        if category is not None:  # one of your categories: its pill
            self.cluster_filter = None
            self.pills.set_cluster(None)
            self.category_filter.setCurrentIndex(max(0, self.category_filter.findData(category)))
            self._update_pills()
        else:
            self.cluster_filter = name
            if name:
                self.category_filter.setCurrentIndex(0)  # all categories, narrowed to this cluster
                self._update_pills()
            self.pills.set_cluster(name)
            self.pills.setVisible(self._has_categories or name is not None)
        if name and self.view == "matrix":
            self.set_view("table")
        else:
            self._apply_filter()
        self._follow_filter()

    def _follow_filter(self) -> None:
        """After the view or the filters change: keep inspecting the video shown if it's still listed, else the
        first one listed."""
        if self.view == "matrix":
            now = time.time()
            if self._shown is not None and self.passes(self._shown, now):
                return
            listed = [i for i in self.analysis.values() if self.passes(i.video, now)]
            best = max(listed, key=lambda i: (i.yours, i.viral), default=None)
            self.select_video(best.video.id) if best else self._show_detail(None)
            return
        rows = [r for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)]
        if self._shown is not None and any(self._video_at(r) is self._shown for r in rows):
            return
        if rows:
            self.table.selectRow(rows[0])
        else:
            self.table.clearSelection()
            self._show_detail(None)

    # ================================================================== the table's columns
    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt's name)
        if obj is self.table and event.type() == QEvent.Resize:
            self._fit_timer.start()
        return super().eventFilter(obj, event)

    def _fit_title(self) -> None:
        """Give the Video column the room the others leave, but never less than TITLE_MIN."""
        head = self.table.horizontalHeader()
        others = sum(head.sectionSize(c) for c in range(len(COLUMNS)) if c != TITLE_COLUMN and not head.isSectionHidden(c))
        width = max(TITLE_MIN, self.table.viewport().width() - others)
        if head.sectionSize(TITLE_COLUMN) != width:
            head.resizeSection(TITLE_COLUMN, width)

    def _hidden_columns(self) -> set[str]:
        return {x.strip() for x in self._settings().research_hidden_columns.split(",") if x.strip()}

    def _apply_columns(self) -> None:
        hidden = self._hidden_columns()
        for c in CHOOSABLE:
            self.table.setColumnHidden(c, COLUMNS[c] in hidden)
        self._fit_timer.start()

    def _column_menu(self, pos) -> None:
        head = self.table.horizontalHeader()
        menu = QMenu(self)
        hidden = self._hidden_columns()
        for c in sorted(CHOOSABLE, key=head.visualIndex):
            act = menu.addAction(COLUMNS[c])
            act.setCheckable(True)
            act.setChecked(COLUMNS[c] not in hidden)
            act.toggled.connect(lambda on, name=COLUMNS[c]: self.show_column(name, on))
        menu.addSeparator()
        menu.addAction("Show every column", lambda: self._set_hidden_columns(set()))
        menu.exec(head.mapToGlobal(pos))

    def show_column(self, name: str, on: bool) -> None:
        hidden = self._hidden_columns()
        if on:
            hidden.discard(name)
        else:
            hidden.add(name)
        self._set_hidden_columns(hidden)

    def _set_hidden_columns(self, hidden: set[str]) -> None:
        value = ", ".join(n for n in COLUMNS if n in hidden)
        if value != self._settings().research_hidden_columns:
            self._change("research_hidden_columns", value)
        self._apply_columns()

    def select_video(self, video_id: str) -> None:
        """Inspect a video picked from the matrix, selecting its row too (for the buttons under the table)."""
        v = next((x for x in self.videos if x.id == video_id), None)
        if v is None:
            return
        for r in range(self.table.rowCount()):
            if self._video_at(r) is v and not self.table.isRowHidden(r):
                self.table.selectRow(r)
                break
        if self._shown is not v:
            self._show_detail(v)

    def _update_banner(self, racing: int) -> None:
        if racing:
            head = f"<b>⚡ {racing} video{'s' if racing != 1 else ''} to race for:</b> "
        else:
            head = "<b>⚡ Nothing to race for right now:</b> "
        self.banner_text.setText(
            head + "taking off on YouTube right now and not used by you yet, best first by viral potential."
            f"<br><span style='color:{theme.MUTED}'>ClipMonster can't search TikTok, so “not used by you yet” stands "
            "in for “not on TikTok yet”.</span>")
        self.banner.setToolTip("Live arbitrage: in this check's fastest fifth, or 30% faster lately than over its day "
                               "(that needs two checks at least 20 minutes apart, so Keep checking fills it in); "
                               "under 3 days old; longer than a minute; not picked, queued, clipped or sent by you")

    def _update_hint(self) -> None:
        if self.view == "matrix":
            text = ("Viral potential blends velocity (35%), Most replayed peakiness (25%), engagement (20%) and how "
                    "uncrowded its cluster is (20%). Momentum compares views an hour lately with each video's last "
                    "day. ⚑ marks an outlier, far faster than the rest of its cluster; ⚡ one to race for.")
        elif self.view == "arbitrage":
            text = ("Queue them in Automation or send them to the Studio while they're rising. Hover Momentum for how "
                    "fast each is going, and the Lifespan for how long a clip of it will stay worth posting.")
        else:
            text = ("Clip-ability blends momentum, most-replayed peaks, engagement and freshness, then scales it by "
                    "how much material there is to cut (length and category). Viral is how likely a clip is to take "
                    "off right now. The Pick column shows what your picking rules would clip, and why the rest were "
                    "skipped. Double-click a video to send it to the Studio.")
        self.hint.setText(text)

    def _video_at(self, row: int) -> TrendingVideo | None:
        it = self.table.item(row, 0)
        vid = it.data(Qt.UserRole) if it else None
        return next((v for v in self.videos if v.id == vid), None)

    def _selected_videos(self) -> list[TrendingVideo]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [v for v in (self._video_at(r) for r in rows if not self.table.isRowHidden(r)) if v]

    def _selected(self) -> None:
        sel = self._selected_videos()
        self.send_btn.setEnabled(bool(sel))
        self.queue_btn.setEnabled(bool(sel))
        self.queue_btn.setText(f"⏵  Queue {len(sel)} in Automation" if len(sel) > 1 else "⏵  Queue in Automation")
        self.send_btn.setText(f"➜  Send {len(sel)} to Studio" if len(sel) > 1 else "➜  Send selected to Studio")
        self._show_detail(sel[0] if sel else None)

    # ================================================================== detail
    def _show_detail(self, v: TrendingVideo | None) -> None:
        self._shown = v
        for w in (self.d_send, self.d_queue, self.d_open, self.d_copy, self.d_deep):
            w.setEnabled(v is not None)
        for grid in (self.parts_grid, self.viral_grid):
            while grid.count():
                w = grid.takeAt(0).widget()
                if w:
                    w.hide()
                    w.deleteLater()
        ins = self.analysis.get(v.id) if v else None
        self.matrix.mark(ins.cluster if ins else "")
        self.replay.set_video(v)
        self.d_windows.setText(self.replay.summary())
        self.d_windows.setVisible(bool(self.d_windows.text()))
        self._show_viral(ins)
        if v is None:
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("Select a video")
            for lab in (self.d_title, self.d_meta, self.d_score, self.d_verdict, self.d_reasons, self.d_pick):
                lab.setText("")
            return

        self.d_title.setText(v.title)
        where = ", ".join([f"#{rank} in {REGIONS.get(code, code)}" for code, rank in
                           sorted(v.regions.items(), key=lambda kv: kv[1])]
                          + [{"watchlist": "on your channel list", "cc": "from a Creative Commons search",
                              "request": "found for your request"}[o]
                             for o in v.origins if o in ORIGIN_LABELS])
        if v.license == "creativeCommon":
            where += " · Creative Commons licence"
        if v.match >= 0:
            where += (f"<br><span style='color:{score_colour(v.match)}'><b>Match {v.match:.0f}</b></span>"
                      f" · {_esc(v.match_why)}")
        if self._category_names.get(v.category_id):
            where += f" · fits the <b>{_esc(self._category_names[v.category_id])}</b> category"
        length = stamp(v.duration) if v.live == "none" else v.live
        self.d_meta.setText(f"{_esc(v.channel)} · {compact(v.views)} views · {length} · posted {age_text(v.age_hours())} ago"
                            f"<br>{where}")
        colour = score_colour(v.score)
        self.d_score.setText(f"{v.score:.0f}")
        self.d_score.setStyleSheet(f"color:{colour}; font-size:26pt; font-weight:800;")
        self.d_verdict.setText(f"<b>{verdict(v.score)}</b><br><span style='color:{theme.MUTED}'>clip-ability "
                               f"out of 100</span>")
        d = self.decisions.get(v.id)
        learned = f"<br><span style='color:{theme.MUTED}'>📈 {_esc(d.learned)}</span>" if d and d.learned else ""
        if d is None:
            self.d_pick.setText("")
        elif d.picked:
            self.d_pick.setText(f"<span style='color:{theme.GOOD}'><b>✓ Picked.</b></span> {_esc(d.reason)}{learned}")
        else:
            self.d_pick.setText(f"<span style='color:{theme.MUTED}'><b>Not picked:</b> {_esc(d.reason)}</span>{learned}")

        keys = sorted(WEIGHTS, key=WEIGHTS.get, reverse=True) + list(FIT)
        for row, key in enumerate(keys):
            value = v.parts.get(key)
            label = QLabel(PART_LABELS[key])
            label.setToolTip(f"{WEIGHTS[key]:.0%} of the blend" if key in WEIGHTS else
                             "Multiplies the blend: how much usable material there is to cut")
            pct = QLabel("—" if value is None else f"{value * 100:.0f}")
            pct.setFixedWidth(30)
            pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if value is None:
                pct.setToolTip("Not measured yet: press Check replays" if key == "replays" else
                               "Not available for this video")
                label.setProperty("muted", True)
            if key == FIT[0]:
                gap = QLabel("Material")
                gap.setProperty("muted", True)
                self.parts_grid.addWidget(gap, row * 2, 0)
            r = row * 2 + 1
            self.parts_grid.addWidget(label, r, 0)
            self.parts_grid.addWidget(_Bar(value, score_colour((value or 0) * 100)), r, 1)
            self.parts_grid.addWidget(pct, r, 2)

        market = market_lines(v)
        bullets = "".join(f"<li style='margin-bottom:4px'>{r}</li>" for r in market)
        bullets += "".join(f"<li style='margin-bottom:4px'>{_esc(r)}</li>" for r in v.reasons)
        self.d_reasons.setText(f"<ul style='margin-left:-20px'>{bullets}</ul>" if bullets else "")
        self.d_deep.setText("Re-check replays" if v.deep else "Check replays")
        self.d_send.setText("✓  Sent, send again" if v.sent else "➜  Send to Studio")
        self._load_thumb(v)

    def _show_viral(self, ins: "analytics.Insight | None") -> None:
        """The inspector's viral potential: its number, momentum, lifespan, flags and the four parts."""
        if ins is None:
            for lab in (self.d_viral, self.d_viral_caption, self.d_momentum, self.d_life, self.d_flags):
                lab.setText("")
            self.d_flags.setVisible(False)
            return
        colour = score_colour(ins.viral)
        self.d_viral.setText(f"{ins.viral:.0f}")
        self.d_viral.setStyleSheet(f"color:{colour}; font-size:26pt; font-weight:800;")
        self.d_viral_caption.setText(f"<b>{viral_word(ins.viral)}</b><br><span style='color:{theme.MUTED}'>viral "
                                     "potential out of 100</span>")
        self.d_momentum.setText(_momentum_html(ins.momentum))
        self.d_life.setText(f"<span style='color:{longevity_colour(ins.longevity)}'>●</span> "
                            f"<b>{_esc(ins.longevity_label)}</b> <span style='color:{theme.MUTED}'>· "
                            f"{_esc(ins.longevity_why)}</span>")
        flags = []
        if ins.outlier:
            flags.append(f"⚑ <b>Outlier</b> <span style='color:{theme.MUTED}'>· {ins.outlier:g}× the median views an "
                         f"hour of {_esc(ins.outlier_of)}</span>")
        if ins.arbitrage:
            flags.append(f"⚡ <b>Live arbitrage</b> <span style='color:{theme.MUTED}'>· {_esc(ins.arbitrage_why)}</span>")
        self.d_flags.setText("<br>".join(flags))
        self.d_flags.setVisible(bool(flags))
        for row, key in enumerate(VIRAL_WEIGHTS):
            value = ins.parts.get(key)
            label = QLabel(VIRAL_LABELS[key])
            label.setToolTip(f"{VIRAL_WEIGHTS[key]:.0%} of viral potential. {VIRAL_HELP[key]}")
            pct = QLabel("—" if value is None else f"{value * 100:.0f}")
            pct.setFixedWidth(30)
            pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if value is None:
                pct.setToolTip("Not measured yet: press Check replays" if key == "peakiness" else
                               "This channel hides its likes" if key == "engagement" else "Not available")
                label.setProperty("muted", True)
            self.viral_grid.addWidget(label, row, 0)
            self.viral_grid.addWidget(_Bar(value, score_colour((value or 0) * 100)), row, 1)
            self.viral_grid.addWidget(pct, row, 2)

    def _load_thumb(self, v: TrendingVideo) -> None:
        if v.id in self._thumbs:
            self.thumb.setPixmap(self._thumbs[v.id])
            return
        self.thumb.setPixmap(QPixmap())
        self.thumb.setText("Loading thumbnail…")
        if v.thumbnail:
            self._thumb_ids[v.thumbnail] = v.id
            self._net.get(QNetworkRequest(QUrl(v.thumbnail)))

    def _thumb_loaded(self, reply: QNetworkReply) -> None:
        vid = self._thumb_ids.pop(reply.request().url().toString(), None)
        data = reply.readAll()
        reply.deleteLater()
        pix = QPixmap()
        if not vid or reply.error() != QNetworkReply.NoError or not pix.loadFromData(bytes(data)):
            if self._shown and self._shown.id == vid:
                self.thumb.setText("No thumbnail")
            return
        pix = pix.scaled(self.thumb.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        self._thumbs[vid] = pix
        if self._shown and self._shown.id == vid:
            self.thumb.setPixmap(pix)

    def _check_one(self) -> None:
        v = self._shown
        if not v or self.side_job:
            return
        cookies = self._settings().cookies_browser or None
        job = _Job(lambda j: deep_check(v.url, cookies, j.log.emit))
        job.log.connect(self.log.emit)

        def done(deep) -> None:
            v.deep = deep
            self.history.store_deep(v.id, deep)
            clipability.score(v, [x.velocity for x in self.videos])
            if v.match >= 0:  # a request's result: its match stays first
                v.reasons.insert(0, f"Match {v.match:.0f}: {v.match_why}")
            self.history.save()
            self._analyse()  # the replay graph's peakiness is part of viral potential
            self._refresh_video(v)
            self.refresh_picks()  # a new score can change what the rules pick
            if deep.error:
                self.notice.emit(f"<span style='color:{theme.WARN}'>⚠ Couldn't read replays: {_esc(deep.error)}</span>")

        job.succeeded.connect(done)
        job.failed.connect(lambda msg: self.notice.emit(f"⚠ {msg}"))
        job.finished.connect(self._side_finished)
        self.side_job = job
        self.d_deep.setEnabled(False)
        self.d_deep.setText("Checking replays…")
        job.start()

    def _side_finished(self) -> None:
        if self.side_job:
            self.side_job.deleteLater()
        self.side_job = None
        if self._shown:
            self.d_deep.setEnabled(True)
            self.d_deep.setText("Re-check replays" if self._shown.deep else "Check replays")

    def _refresh_video(self, v: TrendingVideo) -> None:
        for r in range(self.table.rowCount()):
            if self._video_at(r) is v:
                self.table.setSortingEnabled(False)
                self._fill_row(r, v)
                self.table.setSortingEnabled(True)
                break
        if self._shown is v:
            self._show_detail(v)

    # ================================================================== actions
    def _send(self, videos: list[TrendingVideo]) -> None:
        if not videos:
            return
        for v in videos:
            d = self.decisions.get(v.id)
            if d and d.picked:
                self.picks.record(d, "studio")  # counts towards the daily and per-channel limits
            v.sent = True
            self.history.mark_sent(v.id)
        self.history.save()
        self.picks.save()
        self.send.emit([v.url for v in videos])
        for v in videos:
            self._refresh_video(v)
        self.refresh_picks()

    def _queue(self, videos: list[TrendingVideo]) -> None:
        """To Automation's queue, each in the category it fits (or none)."""
        if videos:
            self.queue.emit([(v.url, v.title, v.category_id) for v in videos])

    def _send_selected(self) -> None:
        self._send(self._selected_videos())

    def _send_picks(self) -> None:
        self._send(self.picked_videos())

    def _event_search(self) -> None:
        """The header's "Search by in-game event": send the selected video to Sports with the question."""
        query = " ".join(self.event_box.text().split())
        v = self._shown or next(iter(self._selected_videos()), None)
        if len(query) < 3:
            self.hint.setText("Type the moment to look for, such as “red card” or “buzzer beater”.")
            return
        if v is None:
            self.hint.setText("Select the video to search first: the event search looks inside one video.")
            return
        self.event_search.emit({"id": v.id, "title": v.title, "channel": v.channel, "published": v.published,
                                "duration": v.duration, "views": v.views, "thumbnail": v.thumbnail}, query)

    def _menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if not item:
            return
        v = self._video_at(item.row())
        sel = self._selected_videos()
        menu = QMenu(self)
        menu.addAction(f"Send {len(sel)} to Studio" if len(sel) > 1 else "Send to Studio", self._send_selected)
        menu.addAction(f"Queue {len(sel)} in Automation" if len(sel) > 1 else "Queue in Automation",
                       lambda: self._queue(sel))
        if v:
            menu.addAction("Open on YouTube", lambda: QDesktopServices.openUrl(QUrl(v.url)))
            menu.addAction("Copy link", lambda: QApplication.clipboard().setText(v.url))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if self.hits:
            self.hits = 0
            self.hits_changed.emit(0)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def viral_word(value: float) -> str:
    return "Hot" if value >= 75 else "Promising" if value >= 55 else "Middling" if value >= 40 else "Quiet"


def _momentum_html(m: "analytics.Momentum") -> str:
    muted = theme.MUTED
    if not m.known:
        return (f"<b>· Momentum: measuring</b><br><span style='color:{muted}'>It needs two checks 20 minutes to 6 "
                f"hours apart. So far: {compact(m.baseline)} views an hour since upload.</span>")
    span = (f"{m.span_minutes / 60:.1f}".rstrip("0").rstrip(".") + " h") if m.span_minutes >= 90 else \
        f"{m.span_minutes:.0f} min"
    base = "its average since upload" if m.since_upload else "its last day"
    return (f"<span style='color:{delta_colour(m.delta)}'><b>{m.arrow} {m.label} {m.delta:+.0%}</b></span>"
            f"<br><span style='color:{muted}'>{compact(m.hourly)} views an hour over the last {span}, against "
            f"{compact(m.baseline)} an hour for {base}</span>")

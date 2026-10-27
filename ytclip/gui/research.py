"""Research tab: what's trending by region right now, scored for how well it will clip."""

from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QProgressBar,
                               QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .. import clipability, learn
from ..clipability import FIT, PART_LABELS, WEIGHTS, age_text, compact, verdict
from ..organize import stamp
from ..policy import Decision, PickLog, Policy, decide, picks_path
from ..research import ScanCancelled, ScanOptions, ScanResult, history_path, last_result, scan, youtube_key
from ..settings import Settings
from ..trending import CATEGORIES, KEY_HELP_URL, REGIONS, History, TrendingVideo, deep_check, parse_regions
from . import theme

COLUMNS = ("Score", "Video", "Channel", "Found in", "Views", "Views / h", "Engagement", "Length", "Posted", "Pick")
PICK_COLUMN = COLUMNS.index("Pick")
ORIGIN_LABELS = {"watchlist": "Your list", "cc": "CC search"}
MIN_LENGTHS = ((0.0, "Any length"), (3.0, "3 min +"), (8.0, "8 min +"), (20.0, "20 min +"), (45.0, "45 min +"))
SORT_ROLE = Qt.UserRole + 1


def score_colour(value: float) -> str:
    if value >= 75:
        return theme.GOOD
    if value >= 55:
        return theme.ACCENT_2
    if value >= 40:
        return theme.WARN
    return theme.MUTED


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
        if a is None or b is None:
            return super().__lt__(other)
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


class Research(QWidget):
    send = Signal(list)  # video URLs for the Studio
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
        self.result: ScanResult | None = None
        self.videos: list[TrendingVideo] = []
        self.decisions: dict[str, Decision] = {}
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
        self.result = last_result(self.history)
        if self.result:
            self._show_result(self.result)
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
        self.hide_sent = QCheckBox("Hide sent")
        self.hide_sent.setToolTip("Hide videos you've already sent to the Studio")
        self.hide_sent.toggled.connect(self._apply_filter)
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
        filters.addWidget(self.search, 1)
        filters.addWidget(self.hide_sent)
        filters.addWidget(self.picks_only)
        filters.addWidget(self.rules_btn)
        layout.addLayout(filters)

        layout.addWidget(self._build_key_panel())

        self.bar = QProgressBar()
        self.bar.setFixedHeight(5)
        self.bar.setRange(0, 1000)
        self.bar.setVisible(False)
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
        head.setSectionResizeMode(1, QHeaderView.Stretch)
        head.setSectionsClickable(True)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.itemSelectionChanged.connect(self._selected)
        self.table.itemDoubleClicked.connect(lambda _item: self._send_selected())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)

        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setProperty("muted", True)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.table, 1)
        lv.addWidget(self.empty, 1)
        row = QHBoxLayout()
        self.send_btn = QPushButton("➜  Send selected to Studio")
        self.send_btn.clicked.connect(self._send_selected)
        self.send_picks_btn = QPushButton()
        self.send_picks_btn.setToolTip("Send every video your picking rules picked to the Studio. They count "
                                       "towards your daily and per-channel limits")
        self.send_picks_btn.clicked.connect(self._send_picks)
        self.count = QLabel()
        self.count.setProperty("muted", True)
        row.addWidget(self.count, 1)
        row.addWidget(self.send_picks_btn)
        row.addWidget(self.send_btn)
        lv.addLayout(row)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self._build_detail())
        split.setStretchFactor(0, 1)
        split.setSizes([980, 420])
        layout.addWidget(split, 1)

        hint = QLabel("Clip-ability blends momentum, most-replayed peaks, engagement and freshness, then scales it by "
                      "how much material there is to cut (length and category). The Pick column shows what your "
                      "picking rules would clip, and why the rest were skipped. Double-click a video to send it "
                      "to the Studio.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

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
        score_row.addWidget(self.d_score)
        score_row.addWidget(self.d_verdict, 1)

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
        self.d_open = QPushButton("Open on YouTube")
        self.d_open.clicked.connect(lambda: self._shown and QDesktopServices.openUrl(QUrl(self._shown.url)))
        self.d_copy = QPushButton("Copy link")
        self.d_copy.clicked.connect(lambda: self._shown and QApplication.clipboard().setText(self._shown.url))
        self.d_deep = QPushButton("Check replays")
        self.d_deep.setToolTip("Read this video's Most replayed graph now (no quota used)")
        self.d_deep.clicked.connect(self._check_one)
        buttons.addWidget(self.d_send, 0, 0, 1, 2)
        buttons.addWidget(self.d_open, 1, 0)
        buttons.addWidget(self.d_copy, 1, 1)
        buttons.addWidget(self.d_deep, 2, 0, 1, 2)

        v.addWidget(self.thumb, 0, Qt.AlignHCenter)
        v.addWidget(self.d_title)
        v.addWidget(self.d_meta)
        v.addLayout(score_row)
        v.addWidget(self.d_pick)
        v.addLayout(buttons)
        v.addSpacing(4)
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
        for w in (self.category, self.min_len, self.auto, self.interval):
            w.blockSignals(True)
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
        for w in (self.category, self.min_len, self.auto, self.interval):
            w.blockSignals(False)
        regions = parse_regions(s.research_regions)
        self.regions_btn.setText(", ".join(regions) if regions else "Choose…")
        self.key_panel.setVisible(not youtube_key(s))
        self._update_summary()
        self._apply_filter()

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
        self.job = job
        self.check_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self.summary.setText("Checking…")
        job.start()

    def cancel(self) -> None:
        if self.job:
            self.job.cancel_requested = True

    def _progress(self, msg: str, frac: float) -> None:
        self.summary.setText(msg)
        if frac < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))

    def _job_finished(self) -> None:
        if self.job:
            self.job.deleteLater()
        self.job = None
        self.check_btn.setEnabled(True)
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
                self.log.emit(record_block(db, result.blocked, time.time()))
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
        keep = self._shown.id if self._shown else None
        self.videos = result.videos
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.videos))
        s = self._settings()
        self.decisions = {d.video.id: d for d in decide(self.videos, Policy.from_settings(s), self.picks,
                                                        notes=result.notes, lessons=learn.for_settings(s))}
        for r, v in enumerate(self.videos):
            self._fill_row(r, v)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.DescendingOrder)
        self._apply_filter()
        rows = [r for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)]
        target = next((r for r in rows if self._video_at(r) and self._video_at(r).id == keep), None)
        if target is None and rows:
            target = rows[0]
        if target is not None:
            self.table.selectRow(target)
        else:
            self._show_detail(None)

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
        mark = "✓ " if v.sent else ("● " if v.new else "")
        title = item(mark + v.title, v.title.lower(), v.title + (f"\n\n{why}" if why else ""))
        if v.new and not v.sent:
            title.setForeground(QColor(theme.ACCENT_2))
        elif v.sent:
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
            self._pick_item(v),
        ]
        for c, cell in enumerate(cells):
            if c >= 4:
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(r, c, cell)

    def _pick_item(self, v: TrendingVideo) -> QTableWidgetItem:
        d = self.decisions.get(v.id)
        it = _SortItem("✓ Pick" if d and d.picked else "–" if d else "")
        it.setData(SORT_ROLE, v.score + (1000 if d and d.picked else 0))  # picks sort above the rest
        if d:
            it.setToolTip(("Picked: " if d.picked else "Not picked: ") + d.reason + (f"\n{d.learned}" if d.learned else ""))
            it.setForeground(QColor(theme.GOOD if d.picked else theme.MUTED))
        it.setTextAlignment(Qt.AlignCenter)
        return it

    def refresh_picks(self) -> None:
        """Run the picking rules over the current results (after a scan or a rules change)."""
        s = self._settings()
        self.picks.reload()  # the automation runner may have picked videos since
        notes = self.result.notes if self.result else {}
        self.decisions = {d.video.id: d for d in decide(self.videos, Policy.from_settings(s), self.picks,
                                                        notes=notes, lessons=learn.for_settings(s))}
        self.table.setSortingEnabled(False)
        for r in range(self.table.rowCount()):
            v = self._video_at(r)
            if v:
                self.table.setItem(r, PICK_COLUMN, self._pick_item(v))
        self.table.setSortingEnabled(True)
        if self._shown:
            self._show_detail(self._shown)
        self._apply_filter()

    def picked_videos(self) -> list[TrendingVideo]:
        return [v for v in self.videos if (d := self.decisions.get(v.id)) and d.picked]

    def _apply_filter(self) -> None:
        text = self.search.text().strip().lower()
        hide_sent = self.hide_sent.isChecked()
        only_picks = self.picks_only.isChecked()
        shown = 0
        for r in range(self.table.rowCount()):
            v = self._video_at(r)
            d = self.decisions.get(v.id) if v else None
            hidden = v is None or (hide_sent and v.sent) or (only_picks and not (d and d.picked)) or bool(
                text and text not in v.title.lower() and text not in v.channel.lower())
            self.table.setRowHidden(r, hidden)
            shown += not hidden
        total = len(self.videos)
        n_picked = len(self.picked_videos())
        self.count.setText((f"{shown} of {total} videos" if shown != total else f"{total} videos")
                           + (f" · {n_picked} picked" if n_picked else ""))
        has_rows = total > 0
        self.table.setVisible(has_rows)
        self.empty.setVisible(not has_rows)
        self.count.setVisible(has_rows)
        self.send_btn.setVisible(has_rows)
        self.send_picks_btn.setVisible(has_rows and n_picked > 0)
        self.send_picks_btn.setText(f"➜  Send {n_picked} pick{'s' if n_picked != 1 else ''} to Studio")
        if not has_rows:
            self.empty.setText("Nothing yet. Press Check now to see what's trending."
                               if youtube_key(self._settings()) else
                               "Add your YouTube Data API key above, then ClipMonster checks the trending charts "
                               "for you and scores every video for how well it will clip.")

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
        self.send_btn.setText(f"➜  Send {len(sel)} to Studio" if len(sel) > 1 else "➜  Send selected to Studio")
        self._show_detail(sel[0] if sel else None)

    # ================================================================== detail
    def _show_detail(self, v: TrendingVideo | None) -> None:
        self._shown = v
        for w in (self.d_send, self.d_open, self.d_copy, self.d_deep):
            w.setEnabled(v is not None)
        while self.parts_grid.count():
            w = self.parts_grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        if v is None:
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("Select a video")
            for lab in (self.d_title, self.d_meta, self.d_score, self.d_verdict, self.d_reasons, self.d_pick):
                lab.setText("")
            return

        self.d_title.setText(v.title)
        where = ", ".join([f"#{rank} in {REGIONS.get(code, code)}" for code, rank in
                           sorted(v.regions.items(), key=lambda kv: kv[1])]
                          + [{"watchlist": "on your channel list", "cc": "from a Creative Commons search"}[o]
                             for o in v.origins if o in ORIGIN_LABELS])
        if v.license == "creativeCommon":
            where += " · Creative Commons licence"
        length = stamp(v.duration) if v.live == "none" else v.live
        self.d_meta.setText(f"{_esc(v.channel)} · {compact(v.views)} views · {length} · posted {age_text(v.age_hours())} ago"
                            f"<br>{where}")
        colour = score_colour(v.score)
        self.d_score.setText(f"{v.score:.0f}")
        self.d_score.setStyleSheet(f"color:{colour}; font-size:28pt; font-weight:800; padding-right:6px;")
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

        bullets = "".join(f"<li style='margin-bottom:4px'>{_esc(r)}</li>" for r in v.reasons)
        self.d_reasons.setText(f"<ul style='margin-left:-20px'>{bullets}</ul>" if bullets else "")
        self.d_deep.setText("Re-check replays" if v.deep else "Check replays")
        self.d_send.setText("✓  Sent, send again" if v.sent else "➜  Send to Studio")
        self._load_thumb(v)

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
            self.history.save()
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

    def _send_selected(self) -> None:
        self._send(self._selected_videos())

    def _send_picks(self) -> None:
        self._send(self.picked_videos())

    def _menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if not item:
            return
        v = self._video_at(item.row())
        sel = self._selected_videos()
        menu = QMenu(self)
        menu.addAction(f"Send {len(sel)} to Studio" if len(sel) > 1 else "Send to Studio", self._send_selected)
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

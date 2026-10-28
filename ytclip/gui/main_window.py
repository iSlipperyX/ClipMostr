"""ClipMonster main window: queue, preview, timeline, clip editor, tuning and export."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, fields, replace
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QScrollArea, QSlider, QSplitter, QTableWidget, QTableWidgetItem,
                               QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from .. import __version__, compose, layout, pipeline, series, updater, uploads, youtube_login
from ..analyze import SIGNAL_LABELS, Clip, clip_score
from ..download import VIDEO_EXTS, expand_sources, is_url, needs_expansion
from ..engine import Engine, Project
from ..export import export_clip
from ..export import frame as export_frame
from ..ffmpeg import MediaInfo, probe
from ..organize import stamp
from ..settings import APP_NAME, SCHEMA, Settings, all_presets, app_dir, save_user_preset, work_dir
from ..polish import reapply_fixes
from ..stackpool import preview_video
from . import forms, theme
from .layout_tab import LayoutTab
from .library import Library, reveal
from .player import Preview
from .automation import Automation
from .cleanup_dialog import CleanupDialog
from .niches import Niches
from .research import Research
from .settings_dialog import SettingsDialog
from .tasks import Task, TaskQueue
from .timeline import Timeline
from .update_banner import UpdateBanner

STATUS_TEXT = {
    "saved": ("Saved — click to load", theme.MUTED),
    "queued": ("Waiting…", theme.MUTED),
    "working": ("Working…", theme.ACCENT_2),
    "ready": ("Ready to export", theme.GOOD),
    "exporting": ("Exporting…", theme.ACCENT_2),
    "done": ("Exported ✓", theme.GOOD),
    "error": ("Failed", theme.BAD),
}
REDETECT_FIELDS = {"clip_count", "min_len", "max_len", "min_gap", "quality_floor", "snap_to_pauses",
                   "pad_start", "pad_end", "keywords", "mode", "w_heatmap", "w_audio", "w_reactions",
                   "w_scenes", "w_keywords"}
CLIP_FIELDS = {f.name for f in fields(Clip)}  # a session saved by another version may differ
RESCORE_FIELDS = {"keywords", "w_heatmap", "w_audio", "w_reactions", "w_scenes", "w_keywords"}
RESEARCH_TAB = "🔥  Research"
SHORTCUTS = [
    ("Space", "Play / pause"), ("← / →", "Seek 5 s (Shift: 1 s)"), ("J / L", "Previous / next clip"),
    ("P", "Play selected clip"), ("I / O", "Set clip start / end at playhead"), ("N", "New clip at playhead"),
    ("Delete", "Delete selected clip"), ("Ctrl+E", "Export clips"), ("Ctrl+O", "Open video files"),
    ("Ctrl+V", "Add links from clipboard"), ("Ctrl+,", "Settings"), ("Scroll on timeline", "Zoom (Shift: pan)"),
]


def _panel(widget: QWidget | None = None) -> QFrame:
    frame = QFrame()
    frame.setObjectName("panel")
    if widget is not None:
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.addWidget(widget)
    return frame


def _label(text: str, name: str = "", muted: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    if muted:
        lab.setProperty("muted", True)
    return lab


class QueueItem(QWidget):
    def __init__(self, project: Project):
        super().__init__()
        self.title = QLabel(project.source)
        self.title.setStyleSheet("font-weight:600;")
        self.sub = QLabel()
        self.sub.setStyleSheet(f"color:{theme.MUTED}; font-size:9pt;")
        self.bar = QProgressBar()
        self.bar.setFixedHeight(5)
        self.bar.setRange(0, 1000)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(3)
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        lay.addWidget(self.bar)

    def show_state(self, p: Project, msg: str = "", frac: float | None = None) -> None:
        saved_title = (getattr(p, "_saved", None) or {}).get("title")
        name = p.video.title if p.video else saved_title or (p.source if is_url(p.source) else Path(p.source).name)
        self.title.setText(self.title.fontMetrics().elidedText(name, Qt.ElideRight, 250))
        self.title.setToolTip(name)
        text, colour = STATUS_TEXT.get(p.status, (p.status, theme.MUTED))
        if p.status == "error":
            text = f"Failed: {p.error[:80]}"
        elif msg and p.status in ("working", "exporting"):
            text = msg
        elif p.status in ("ready", "done") and p.clips:
            text = f"{text} · {sum(c.enabled for c in p.clips)} clips"
        if p.ai_error and p.status in ("ready", "done"):
            text, colour = f"{text} · AI failed, used signals", theme.WARN
        self.sub.setText(text)
        self.sub.setToolTip(f"AI clip finder: {p.ai_error}" if p.ai_error else "")
        self.sub.setStyleSheet(f"color:{colour}; font-size:9pt;")
        busy = p.status in ("working", "exporting", "queued")
        self.bar.setVisible(busy)
        if frac is None or frac < 0:
            self.bar.setRange(0, 0 if p.status != "queued" else 1000)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))


class _Relay(QObject):
    """Hands lines from a worker thread to the window's log, on the window's own thread."""

    line = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  ·  v{__version__}")
        self.setWindowIcon(theme.app_icon())
        self.resize(1560, 940)
        self.setAcceptDrops(True)

        self.settings = Settings.load()
        self.projects: list[Project] = []
        self.items: dict[int, tuple[QListWidgetItem, QueueItem]] = {}
        self.current: Project | None = None
        self.sel = -1
        self._filling = False
        self._play_until: float | None = None
        self.qs = QSettings(APP_NAME, APP_NAME)

        self.tasks = TaskQueue(self)
        self.tasks.started.connect(self._task_started)
        self._status_is_progress = False
        self._closing = False
        self.tasks.idle.connect(self._idle)

        self._save_timer = QTimer(self, singleShot=True, interval=600)
        self._save_timer.timeout.connect(lambda: self.settings.save())

        self._build()
        self._shortcuts()
        self._restore()
        self.library.refresh()
        QTimer.singleShot(2500, self._startup_updates)
        # Finished clips go up to the storage server (Settings → Upload), here while automation isn't running.
        self._upload_log = _Relay(self)
        self._upload_log.line.connect(self._log)
        self.uploader = uploads.Uploader(lambda: self.settings, self._upload_log.line.emit,
                                         idle_when=pipeline.runner_alive)
        self.uploader.start()

    def _startup_updates(self) -> None:
        finished = updater.finished_update_message()
        if finished:
            msg, ok = finished
            if ok:
                self._notice(f"✓ {msg}")
            else:
                self._warn(msg)
        self._offer_old_copies()
        if self._offer_install():
            return
        # Only packaged builds update themselves; from source you'd use git.
        if self.settings.check_updates and updater.install_dir() and \
                updater.due_for_check(updater.CHECK_HOURS[updater.channel_setting(self.settings)]):
            self.banner.check(manual=False)

    def _offer_old_copies(self) -> None:
        """Copies of ClipMonster unzipped by hand before the installer: offer to remove each (once)."""
        for folder in getattr(self, "old_copies", []):
            box = QMessageBox(QMessageBox.Question, APP_NAME,
                              f"There's another copy of ClipMonster in {folder}, from before it was installed. "
                              f"The one you're using now is in {updater.install_dir()}, and it has all your "
                              "settings, niches and history (they're kept separately from the program).\n\n"
                              "Remove the old copy? Only ClipMonster's own program files are removed: anything "
                              "else you keep in that folder stays.", parent=self)
            remove = box.addButton("Remove old copy", QMessageBox.AcceptRole)
            box.addButton("Keep it", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is remove:
                why = updater.remove_copy(folder)
                if why:
                    self._warn(f"The old copy in {folder} is still there: {why}")
                    continue
                self._notice(f"✓ Removed the old copy of ClipMonster in {folder}.")
            else:
                updater.keep_copy(folder)
        self.old_copies = []

    INSTALL_ASK_AGAIN = 7 * 86400

    def _offer_install(self) -> bool:
        """A copy unzipped by hand, from before the installer: offer to install ClipMonster properly
        (again a week after "Not now"). True when it's installing and this window is closing."""
        asked = updater.load_state().get("install_offer")
        if not updater.needs_installing() or asked == "never" or \
                isinstance(asked, (int, float)) and time.time() - asked < self.INSTALL_ASK_AGAIN:
            return False
        box = QMessageBox(QMessageBox.Question, APP_NAME,
                          "ClipMonster now installs like other Windows apps: into your user folder "
                          "(%LOCALAPPDATA%\\Programs\\ClipMonster), out of OneDrive and the Desktop, with a Start "
                          "menu entry and its own Uninstall. Updates are smoother there.\n\n"
                          f"This copy, in {updater.install_dir()}, was unzipped by hand. Install ClipMonster "
                          "now? Your settings, niches, history and clips stay exactly as they are, and "
                          "automation carries on. This window closes, and the installed ClipMonster opens by "
                          "itself (in about a minute) and offers to remove this copy.", parent=self)
        install = box.addButton("Install now", QMessageBox.AcceptRole)
        later = box.addButton("Not now", QMessageBox.RejectRole)
        box.addButton("Keep using this copy", QMessageBox.DestructiveRole)
        box.setDefaultButton(install)
        box.exec()
        if box.clickedButton() is not install:
            updater.save_state(install_offer=time.time() if box.clickedButton() is later else "never")
            return False
        updater.save_state(install_offer=time.time())  # if it fails, ask again next week
        setup = self._download_setup()
        if setup is None:
            return False
        try:
            updater.run_setup(setup, stop_from=updater.install_dir())
        except updater.UpdateError as exc:
            self._warn(str(exc))
            return False
        self.banner.ready = None  # the installer brings the newest version: no update on closing
        QTimer.singleShot(0, self.close)
        return True

    def _download_setup(self) -> Path | None:
        from PySide6.QtWidgets import QProgressDialog

        s = self.settings
        wait = QProgressDialog("Downloading ClipMonster-Setup.exe…", "Cancel", 0, 100, self)
        wait.setWindowTitle(f"Install {APP_NAME}")
        wait.setMinimumDuration(0)

        def fetch(t: Task) -> Path:
            rel = updater.setup_release(updater.configured_repo(s.update_repo), updater.channel_setting(s))
            return updater.download(rel, progress=lambda f: t.progress.emit("", f), setup=True,
                                    cancelled=lambda: t.cancel_requested)

        task = Task("Downloading the installer", fetch)
        done: dict = {}
        wait.canceled.connect(lambda: setattr(task, "cancel_requested", True))
        task.progress.connect(lambda _m, f: wait.setValue(int(f * 100)) if f >= 0 else None)
        task.succeeded.connect(lambda path: done.update(path=path))
        task.failed.connect(lambda msg: done.update(error=msg))
        task.finished.connect(wait.reset)
        self._setup_task = task  # kept alive while it runs
        task.start()
        wait.exec()
        task.wait(5000)
        if done.get("error") and not isinstance(done.get("path"), Path):
            if "Cancel" not in done["error"]:
                self._warn(f"Couldn't download the installer: {done['error']}")
            return None
        return done.get("path")

    # ================================================================== UI
    def _build(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 10, 12, 8)
        outer.setSpacing(8)
        outer.addLayout(self._build_header())
        self.banner = UpdateBanner(self)
        outer.addWidget(self.banner)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_studio(), "🎬  Studio")
        self.library = Library(lambda: self.settings)
        self.library.reopen.connect(lambda src: (self.add_sources([src]), self.tabs.setCurrentIndex(0)))
        self.tabs.addTab(self.library, "📚  Library")
        self.tabs.currentChanged.connect(lambda i: self.library.refresh() if i == 1 else None)
        self.research = Research(lambda: self.settings, self._setting_changed)
        self.research.send.connect(self._research_send)
        self.research.queue.connect(self._research_queue)
        self.research.notice.connect(self._notice)
        self.research.log.connect(self._log)
        self.research.hits_changed.connect(self._research_hits)
        self.research.open_settings.connect(self._open_settings)
        self.research_tab = self.tabs.addTab(self.research, RESEARCH_TAB)
        self.niches = Niches(lambda: self.settings, self._setting_changed)
        self.niches.notice.connect(self._notice)
        self.niches.changed.connect(self._niches_changed)
        self.tabs.addTab(self.niches, "🎯  Niches")
        self.automation = Automation(lambda: self.settings, change=self._setting_changed)
        self.automation.niches_edited.connect(lambda: (self.niches.reload(), self.research.settings_changed()))
        self.automation.notice.connect(self._notice)
        self.automation.open_settings.connect(self._open_settings)
        self.automation.clean_up.connect(self._clean_up)
        self.library.clean_up.connect(self._clean_up)
        self.tabs.addTab(self.automation, "🤖  Automation")
        outer.addWidget(self.tabs, 1)
        outer.addWidget(self._build_footer())
        self.setCentralWidget(root)

    def _niches_changed(self) -> None:
        """A niche was added, edited or removed: everything that shows niches follows."""
        self.research.settings_changed()
        self.automation.niches_changed()
        if self.tabs.currentWidget() is self.library:  # otherwise it refreshes when it's opened
            self.library.refresh()

    def _clean_up(self) -> None:
        def busy() -> set[str]:
            from ..cleanup import busy_ids, source_id

            # Every video in the Studio's list, opened yet or not (restored ones load when clicked).
            studio = {p.video.id if p.video is not None else source_id(p.source) for p in self.projects}
            return (studio - {""}) | busy_ids(self.automation.db)

        dlg = CleanupDialog(lambda: self.settings, self._setting_changed, busy, self)
        dlg.cleaned.connect(self._notice)
        dlg.exec()
        dlg.dispose()
        self.library.refresh()

    def _research_send(self, urls: list[str]) -> None:
        self.add_sources(urls)
        self.tabs.setCurrentIndex(0)
        self._notice(f"Sent {len(urls)} video{'s' if len(urls) != 1 else ''} from Research to the Studio.")

    def _research_queue(self, videos: list) -> None:
        from .. import niches as niche_list
        from .. import pipeline

        runner = pipeline.Runner(self.automation.db)
        added = sum(runner.add_manual(url, title, niche if niche_list.find(self.settings, niche) else "")
                    for url, title, niche in videos)
        self.automation.refresh()
        again = len(videos) - added
        self._notice(f"Queued {added} video{'s' if added != 1 else ''} from Research in Automation."
                     + (f" {again} {'was' if again == 1 else 'were'} queued before." if again else ""))

    def _research_hits(self, n: int) -> None:
        self.tabs.setTabText(self.research_tab, f"{RESEARCH_TAB}  ({n} new)" if n else RESEARCH_TAB)

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(theme.app_icon_pixmap(34))
        brand = _label(APP_NAME, "brand")
        self.input = QLineEdit()
        self.input.setObjectName("bigInput")
        self.input.setPlaceholderText("Paste YouTube links, playlists or channels (several at once is fine) — "
                                      "or drop video files anywhere")
        self.input.returnPressed.connect(self._add_from_input)
        add = QPushButton("＋  Add")
        add.setObjectName("primary")
        add.clicked.connect(self._add_from_input)
        open_files = QPushButton("📂  Open files")
        open_files.clicked.connect(self._open_files)

        self.preset = QComboBox()
        self.preset.setMinimumWidth(230)
        self.preset.setToolTip("Presets change clip finding and export settings in one click")
        self._fill_presets()
        self.preset.activated.connect(self._preset_chosen)

        settings_btn = QPushButton("⚙  Settings")
        settings_btn.clicked.connect(lambda: self._open_settings())
        help_btn = QPushButton("?")
        help_btn.setObjectName("icon")
        help_btn.clicked.connect(self._show_help)

        row.addWidget(logo)
        row.addWidget(brand)
        row.addSpacing(12)
        row.addWidget(self.input, 1)
        row.addWidget(add)
        row.addWidget(open_files)
        row.addSpacing(12)
        row.addWidget(_label("Preset", muted=True))
        row.addWidget(self.preset)
        row.addWidget(settings_btn)
        row.addWidget(help_btn)
        return row

    # ---------------------------------------------------------------- studio
    def _build_studio(self) -> QWidget:
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self._build_queue())
        self.split.addWidget(self._build_center())
        self.split.addWidget(self._build_right())
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([280, 860, 460])
        return self.split

    def _build_queue(self) -> QWidget:
        box = _panel()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 10, 8, 8)
        head = QHBoxLayout()
        head.addWidget(_label("Videos", "h2"))
        head.addStretch()
        export_all = QPushButton("Export all")
        export_all.setToolTip("Export every video that's ready")
        export_all.clicked.connect(self.export_all)
        head.addWidget(export_all)
        lay.addLayout(head)

        self.queue = QListWidget()
        self.queue.setSpacing(2)
        self.queue.currentRowChanged.connect(self._queue_selected)
        self.queue.setContextMenuPolicy(Qt.CustomContextMenu)
        self.queue.customContextMenuRequested.connect(self._queue_menu)
        lay.addWidget(self.queue, 1)

        row = QHBoxLayout()
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_selected)
        clear = QPushButton("Clear finished")
        clear.clicked.connect(self._clear_finished)
        row.addWidget(remove)
        row.addWidget(clear)
        lay.addLayout(row)
        box.setMinimumWidth(240)
        return box

    def _build_center(self) -> QWidget:
        # Preview + transport
        prev_box = _panel()
        pl = QVBoxLayout(prev_box)
        pl.setContentsMargins(10, 8, 10, 8)
        head = QHBoxLayout()
        self.video_title = _label("No video loaded", "h2")
        self.video_meta = _label("", muted=True)
        head.addWidget(self.video_title)
        head.addWidget(self.video_meta, 1)
        self.export_view_btn = QPushButton("Export view")
        self.export_view_btn.setCheckable(True)
        self.export_view_btn.setToolTip("Show the clip exactly as it will be exported, playing: format, layout, "
                                        "banners, stacked video and captions (V)")
        self.safe_btn = QPushButton("Safe zone")
        self.safe_btn.setCheckable(True)
        self.safe_btn.setToolTip("Shade where TikTok's buttons, tabs and caption cover the video")
        head.addWidget(self.export_view_btn)
        head.addWidget(self.safe_btn)
        pl.addLayout(head)

        self.preview = Preview()
        self._second_info: dict[str, MediaInfo] = {}
        self.export_view_btn.setChecked(self.qs.value("export_view", True, type=bool))
        self.safe_btn.setChecked(self.qs.value("safe_zone", False, type=bool))
        self.preview.set_export_view(self.export_view_btn.isChecked())
        self.preview.set_safe_zone(self.safe_btn.isChecked())
        self.export_view_btn.toggled.connect(self._export_view_toggled)
        self.safe_btn.toggled.connect(self._safe_zone_toggled)
        self._update_preview_frame()
        self.preview.cropChanged.connect(self._crop_dragged)
        self.preview.player.positionChanged.connect(self._position_changed)
        self.preview.player.playbackStateChanged.connect(self._playback_changed)
        pl.addWidget(self.preview, 1)

        tr = QHBoxLayout()
        self.btn_prev = self._tbtn("⏮", "Previous clip (J)", lambda: self.step_clip(-1))
        self.btn_back = self._tbtn("−5s", "Back 5 s (←)", lambda: self.seek_rel(-5))
        self.btn_play = self._tbtn("▶", "Play / pause (Space)", self.toggle_play)
        self.btn_play.setFixedWidth(44)
        self.btn_fwd = self._tbtn("+5s", "Forward 5 s (→)", lambda: self.seek_rel(5))
        self.btn_next = self._tbtn("⏭", "Next clip (L)", lambda: self.step_clip(1))
        self.btn_play_clip = QPushButton("▶ Play clip")
        self.btn_play_clip.setToolTip("Play the selected clip from its start (P)")
        self.btn_play_clip.clicked.connect(lambda: self.play_clip(self.sel))
        self.loop = QCheckBox("Loop clip")
        self.time_label = _label("0:00 / 0:00", muted=True)
        vol = QSlider(Qt.Horizontal)
        vol.setRange(0, 100)
        vol.setValue(80)
        vol.setFixedWidth(100)
        vol.valueChanged.connect(lambda v: self.preview.set_volume(v / 100))
        for w in (self.btn_prev, self.btn_back, self.btn_play, self.btn_fwd, self.btn_next):
            tr.addWidget(w)
        tr.addSpacing(8)
        tr.addWidget(self.btn_play_clip)
        tr.addWidget(self.loop)
        tr.addStretch()
        tr.addWidget(self.time_label)
        tr.addSpacing(10)
        tr.addWidget(_label("🔊", muted=True))
        tr.addWidget(vol)
        pl.addLayout(tr)

        # Timeline + legend
        tl_box = _panel()
        tl = QVBoxLayout(tl_box)
        tl.setContentsMargins(10, 8, 10, 8)
        legend = QHBoxLayout()
        legend.addWidget(_label("Interest", "h2"))
        legend.addSpacing(6)
        self.legend_boxes: dict[str, QCheckBox] = {}
        self.timeline_defaults = {"heatmap", "keywords"}  # the rest are one click away
        for key, colour in theme.SIGNAL_COLOURS.items():
            cb = QCheckBox(SIGNAL_LABELS[key])
            cb.setChecked(key in self.timeline_defaults)
            cb.setStyleSheet(f"QCheckBox {{ color: {colour}; }}")
            cb.toggled.connect(self._legend_toggled)
            self.legend_boxes[key] = cb
            legend.addWidget(cb)
        legend.addStretch()
        legend.addWidget(_label("scroll = zoom · drag clip edges to trim · right-click for more", muted=True))
        fit = QPushButton("Fit")
        fit.setObjectName("icon")
        fit.clicked.connect(lambda: self.timeline.zoom_all())
        legend.addWidget(fit)
        tl.addLayout(legend)

        self.timeline = Timeline()
        self.timeline.visible = set(self.timeline_defaults)
        self.timeline.seekRequested.connect(self.seek)
        self.timeline.clipSelected.connect(lambda i: self.select_clip(i, seek=False))
        self.timeline.clipLive.connect(self._clip_dragging)
        self.timeline.clipChanged.connect(self._clip_edited)
        self.timeline.addClipRequested.connect(self.add_clip)
        self.timeline.deleteClipRequested.connect(self.delete_clip)
        self.timeline.playClipRequested.connect(self.play_clip)
        tl.addWidget(self.timeline, 1)

        center = QSplitter(Qt.Vertical)
        center.addWidget(prev_box)
        center.addWidget(tl_box)
        center.setSizes([600, 260])
        self.center_split = center
        return center

    def _tbtn(self, text: str, tip: str, fn) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("icon")
        b.setToolTip(tip)
        b.clicked.connect(fn)
        return b

    def _build_right(self) -> QWidget:
        self.side = QTabWidget()
        self.side.addTab(self._build_clips_tab(), "Clips")
        self.side.addTab(self._build_tune_tab(), "Find clips")
        self.layout_tab = LayoutTab(self.settings, self._setting_changed)
        self.layout_tab.clipChanged.connect(self._save_session)
        self.layout_tab.clipChanged.connect(self._refresh_export_view)
        self.layout_tab.presetChosen.connect(self._apply_layout_preset)
        self.side.addTab(self.layout_tab, "Layout")
        self.side.addTab(self._build_export_tab(), "Export")
        box = _panel(self.side)
        box.setMinimumWidth(380)
        return box

    def _build_clips_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 8, 4, 4)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", "#", "Start", "Len", "Score", "Title"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        hh = self.table.horizontalHeader()
        for col, w in ((0, 28), (1, 44), (2, 58), (3, 44), (4, 48)):
            hh.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, w)
        hh.setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._table_edited)
        self.table.itemSelectionChanged.connect(self._table_selected)
        self.table.cellDoubleClicked.connect(lambda r, c: self.play_clip(r) if c != 5 else None)
        lay.addWidget(self.table, 3)

        row = QHBoxLayout()
        for text, tip, fn in (
            ("＋ Add", "New clip at the playhead (N)", lambda: self.add_clip(self.preview.player.position() / 1000)),
            ("Delete", "Delete selected clip (Del)", lambda: self.delete_clip(self.sel)),
            ("▲", "Move up", lambda: self.move_clip(-1)),
            ("▼", "Move down", lambda: self.move_clip(1)),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        sort = QComboBox()
        sort.addItems(["Sort…", "By score", "By time", "By length"])
        sort.activated.connect(lambda i: (self.sort_clips(i), sort.setCurrentIndex(0)))
        row.addWidget(sort)
        lay.addLayout(row)

        # Inspector
        insp = QWidget()
        il = QVBoxLayout(insp)
        il.setContentsMargins(0, 6, 0, 0)
        il.addWidget(_label("Selected clip", "h2"))
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Clip title (used in the file name and title overlay)")
        self.title_edit.textEdited.connect(self._title_typed)
        il.addWidget(self.title_edit)

        times = QHBoxLayout()
        self.start_spin = self._time_spin()
        self.end_spin = self._time_spin()
        self.start_spin.valueChanged.connect(lambda v: self._spin_changed("start", v))
        self.end_spin.valueChanged.connect(lambda v: self._spin_changed("end", v))
        set_in = QPushButton("⇤ I")
        set_in.setToolTip("Set start to playhead (I)")
        set_in.clicked.connect(lambda: self.set_edge("start"))
        set_out = QPushButton("O ⇥")
        set_out.setToolTip("Set end to playhead (O)")
        set_out.clicked.connect(lambda: self.set_edge("end"))
        times.addWidget(_label("Start", muted=True))
        times.addWidget(self.start_spin)
        times.addWidget(set_in)
        times.addSpacing(6)
        times.addWidget(_label("End", muted=True))
        times.addWidget(self.end_spin)
        times.addWidget(set_out)
        il.addLayout(times)

        crop = QHBoxLayout()
        crop.addWidget(_label("Reframe", muted=True))
        self.crop_slider = QSlider(Qt.Horizontal)
        self.crop_slider.setRange(0, 100)
        self.crop_slider.setToolTip("Horizontal position of the vertical/square crop. You can also drag the frame "
                                    "on the preview.")
        self.crop_slider.valueChanged.connect(self._crop_slid)
        crop.addWidget(self.crop_slider, 1)
        self.preview_btn = QPushButton("Render preview")
        self.preview_btn.setToolTip("Render just this clip with the current export settings and open it")
        self.preview_btn.clicked.connect(self.render_preview)
        crop.addWidget(self.preview_btn)
        il.addLayout(crop)

        self.reason = _label("", muted=True)
        self.reason.setWordWrap(True)
        il.addWidget(self.reason)
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("Transcript of the selected clip appears here")
        il.addWidget(self.transcript, 1)
        lay.addWidget(insp, 2)
        return page

    def _time_spin(self) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setDecimals(1)
        s.setSingleStep(0.5)
        s.setSuffix(" s")
        s.setRange(0, 1e6)
        s.setKeyboardTracking(False)
        return s

    def _build_tune_tab(self) -> QWidget:
        fields = [f for f in SCHEMA if f.group == "Clip finding"]
        form, self.tune_editors = forms.build_form(fields, self.settings, self._setting_changed, compact=True)
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 8, 4, 4)
        intro = _label("Every second of the video is scored by the signals below. Move the sliders to change "
                       "what counts as interesting. Clips update live.", muted=True)
        intro.setWordWrap(True)
        lay.addWidget(intro)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form)
        lay.addWidget(scroll, 1)
        row = QHBoxLayout()
        self.live = QCheckBox("Live update")
        self.live.setChecked(True)
        self.live.setToolTip("Re-pick clips instantly as you change settings (replaces unedited clips)")
        redetect = QPushButton("⟳ Re-detect clips")
        redetect.setObjectName("primary")
        redetect.setToolTip("Pick clips again with the current settings. In AI mode this asks the AI (Settings → AI).")
        redetect.clicked.connect(lambda: self.redetect(force_ai=True))
        row.addWidget(self.live)
        row.addStretch()
        row.addWidget(redetect)
        lay.addLayout(row)
        return page

    def _build_export_tab(self) -> QWidget:
        fields = [f for f in SCHEMA if f.group in ("Export", "Captions")]
        form, self.export_editors = forms.build_form(fields, self.settings, self._setting_changed, compact=True)
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 8, 4, 4)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form)
        lay.addWidget(scroll, 1)
        self.out_label = _label("", muted=True)
        self.out_label.setWordWrap(True)
        lay.addWidget(self.out_label)
        row = QHBoxLayout()
        open_out = QPushButton("Open folder")
        open_out.clicked.connect(self._open_output)
        self.export_btn = QPushButton("Export clips")
        self.export_btn.setObjectName("primary")
        self.export_btn.setMinimumHeight(38)
        self.export_btn.clicked.connect(self.export_current)
        row.addWidget(open_out)
        row.addWidget(self.export_btn, 1)
        lay.addLayout(row)
        self._update_out_label()
        return page

    def _build_footer(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(130)
        self.log_view.setVisible(False)
        lay.addWidget(self.log_view)
        row = QHBoxLayout()
        self.status = QLabel("Ready")
        self.status.setOpenExternalLinks(False)
        self.status.linkActivated.connect(lambda link: QDesktopServices.openUrl(QUrl(link)))
        self.progress = QProgressBar()
        self.progress.setFixedWidth(280)
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.tasks.cancel_current)
        log_btn = QPushButton("Log")
        log_btn.setCheckable(True)
        log_btn.toggled.connect(self.log_view.setVisible)
        row.addWidget(self.status, 1)
        row.addWidget(self.progress)
        row.addWidget(self.cancel_btn)
        row.addWidget(log_btn)
        lay.addLayout(row)
        return box

    def _shortcuts(self) -> None:
        def sc(keys: str, fn) -> None:
            QShortcut(QKeySequence(keys), self, activated=fn)

        sc("Space", self.toggle_play)
        sc("V", self.export_view_btn.toggle)
        sc("Left", lambda: self.seek_rel(-5))
        sc("Right", lambda: self.seek_rel(5))
        sc("Shift+Left", lambda: self.seek_rel(-1))
        sc("Shift+Right", lambda: self.seek_rel(1))
        sc("J", lambda: self.step_clip(-1))
        sc("L", lambda: self.step_clip(1))
        sc("P", lambda: self.play_clip(self.sel))
        sc("I", lambda: self.set_edge("start"))
        sc("O", lambda: self.set_edge("end"))
        sc("N", lambda: self.add_clip(self.preview.player.position() / 1000))
        sc("Delete", lambda: self.delete_clip(self.sel))
        sc("Ctrl+E", self.export_current)
        sc("Ctrl+O", self._open_files)
        sc("Ctrl+V", self._paste)
        sc("Ctrl+,", lambda: self._open_settings())

    # ================================================================== sources & queue
    def _add_from_input(self) -> None:
        text = self.input.text()
        self.input.clear()
        self.add_sources(text.replace(",", " ").split())

    def _paste(self) -> None:
        from PySide6.QtWidgets import QApplication

        self.add_sources(QApplication.clipboard().text().split())

    def _open_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
        files, _ = QFileDialog.getOpenFileNames(self, "Open videos", "", f"Videos ({exts});;All files (*)")
        self.add_sources(files)

    def add_sources(self, sources: list[str]) -> None:
        sources = [s.strip().strip('"') for s in sources if s.strip()]
        sources = [s for s in sources if is_url(s) or Path(s).exists()]
        if not sources:
            return
        expand = [s for s in sources if needs_expansion(s)]
        for s in sources:
            if s not in expand:
                self._add_project(s)
        if expand:
            s = self.settings.copy()
            task = Task("Reading playlists", lambda t: expand_sources(expand, s.playlist_limit or None,
                                                                      s.cookies_browser or None, t.log.emit))
            task.succeeded.connect(lambda found: [self._add_project(f) for f in found])
            self._wire(task)
            self.tasks.add(task)

    def _add_project(self, source: str, saved: dict | None = None) -> Project:
        p = Project(source)
        if saved:
            p.status = "saved"
            p._saved = saved  # type: ignore[attr-defined]
        self.projects.append(p)
        item = QListWidgetItem()
        widget = QueueItem(p)
        item.setSizeHint(QSize(0, 64))
        self.queue.addItem(item)
        self.queue.setItemWidget(item, widget)
        self.items[id(p)] = (item, widget)
        widget.show_state(p)
        if not saved:
            self._queue_prepare(p)
            if self.queue.currentRow() < 0:
                self.queue.setCurrentRow(self.queue.count() - 1)
        self._save_session()
        return p

    def _queue_prepare(self, p: Project) -> None:
        settings = self.settings.copy()
        saved = getattr(p, "_saved", None)
        p.status, p.error = "queued", ""
        self._refresh_item(p)

        def run(t: Task):
            eng = t.engine(settings)
            p.status = "working"
            eng.open(p)
            eng.measure(p)
            if saved and saved.get("clips"):
                p.clips = [Clip(**{k: v for k, v in c.items() if k in CLIP_FIELDS}) for c in saved["clips"]]
                p.mode_used = saved.get("mode", "auto")
                eng.rescore(p)
                eng.retype(p, p.clips)  # their own words, as they had; edges stay where they were
                if saved.get("polish"):
                    p.meta["polish"] = saved["polish"]
                    p.polished, p.captions_off = True, bool(saved["polish"].get("captions_off"))
                    reapply_fixes(saved["polish"], p.words)
            else:
                eng.find(p)
                eng.retype(p)
                if settings.ai_polish:
                    eng.polish(p)
            p.status = "ready"
            if settings.auto_export:
                p.status = "exporting"
                eng.export(p, [replace(c) for c in p.clips])
                p.status = "done"
            return p

        task = Task(f"Analysing {p.source}", run, owner=p)
        task.succeeded.connect(lambda _p: self._project_ready(p))
        task.failed.connect(lambda msg: self._project_failed(p, msg))
        self._wire(task)
        self.tasks.add(task)

    def _project_ready(self, p: Project) -> None:
        if hasattr(p, "_saved"):
            del p._saved  # type: ignore[attr-defined]
        self._refresh_item(p)
        self._log(f"✓ {p.name}: {len(p.clips)} clips found"
                  f"{' and exported' if p.status == 'done' else ''}")
        if p.ai_error:
            self._warn(f"AI clip finder failed for “{p.name[:40]}”: {p.ai_error} Smart signals were used instead.")
        if p.polish_error:
            self._log(f"AI polish skipped for {p.name}: {p.polish_error}")
        if p.captions_off:
            self._notice(f"“{p.name[:40]}” already has subtitles in the picture, so your captions are off for it")
        if self.current is p or self.current is None:
            self._select_project(p)
        if p.status == "done":
            self.library.refresh()
        self._save_session()

    def _project_failed(self, p: Project, msg: str) -> None:
        p.status, p.error = "error", msg
        self._refresh_item(p)
        self._log(f"✗ {p.source}: {msg}")
        if self.current is p:
            self.load_project(p)

    def _refresh_item(self, p: Project, msg: str = "", frac: float | None = None) -> None:
        if id(p) in self.items:
            self.items[id(p)][1].show_state(p, msg, frac)

    def _select_project(self, p: Project) -> None:
        if p in self.projects:
            row = self.projects.index(p)
            if self.queue.currentRow() != row:
                self.queue.setCurrentRow(row)
            else:
                self.load_project(p)

    def _queue_selected(self, row: int) -> None:
        p = self.projects[row] if 0 <= row < len(self.projects) else None
        if p and p.status == "saved":
            self._queue_prepare(p)
        self.load_project(p)

    def _queue_menu(self, pos) -> None:
        item = self.queue.itemAt(pos)
        if not item:
            return
        p = self.projects[self.queue.row(item)]
        menu = QMenu(self)
        if p.export_dir:
            menu.addAction("Open exported folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.export_dir))))
        if p.video and p.video.path.exists():
            menu.addAction("Show source video", lambda: reveal(p.video.path))
        if is_url(p.source):
            menu.addAction("Open on YouTube", lambda: QDesktopServices.openUrl(QUrl(p.source)))
        if p.status in ("ready", "done", "error"):
            menu.addAction("Re-analyse (new clips)", lambda: self._reanalyse(p))
        if p.status in ("ready", "done"):
            menu.addAction("Export", lambda: self._queue_export(p))
        menu.addSeparator()
        menu.addAction("Remove", lambda: self._remove(p))
        menu.exec(self.queue.mapToGlobal(pos))

    def _reanalyse(self, p: Project) -> None:
        p.clips = []
        if hasattr(p, "_saved"):
            del p._saved  # type: ignore[attr-defined]
        self._queue_prepare(p)

    def _remove(self, p: Project) -> None:
        if p.status in ("working", "exporting"):
            QMessageBox.information(self, APP_NAME, "This video is busy. Cancel the task first.")
            return
        for t in list(self.tasks.pending):
            if t.owner is p:
                self.tasks.pending.remove(t)
        row = self.projects.index(p)
        self.projects.pop(row)
        self.queue.takeItem(row)
        self.items.pop(id(p), None)
        if self.current is p:
            self.load_project(None)
        self._save_session()

    def _remove_selected(self) -> None:
        row = self.queue.currentRow()
        if 0 <= row < len(self.projects):
            self._remove(self.projects[row])

    def _clear_finished(self) -> None:
        for p in [p for p in self.projects if p.status in ("done", "error")]:
            self._remove(p)

    # ================================================================== tasks
    def _wire(self, task: Task) -> None:
        task.log.connect(self._log)
        task.progress.connect(lambda msg, f, t=task: self._task_progress(t, msg, f))
        task.failed.connect(lambda msg, t=task: self._log(f"✗ {t.label}: {msg}") if t.owner is None else None)

    def _task_started(self, task: Task) -> None:
        self._set_status(task.label, None, busy=True)
        if isinstance(task.owner, Project):
            self._refresh_item(task.owner)

    def _task_progress(self, task: Task, msg: str, frac: float) -> None:
        f = None if frac < 0 else frac
        name = task.owner.name if isinstance(task.owner, Project) else ""
        self._set_status(f"{msg}{' — ' + name if name else ''}", f, busy=True)
        if isinstance(task.owner, Project):
            self._refresh_item(task.owner, msg, f)

    def _set_status(self, text: str, frac: float | None, busy: bool) -> None:
        self.status.setText(text)
        self._status_is_progress = busy
        self.progress.setVisible(busy)
        self.cancel_btn.setVisible(busy)
        if frac is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(frac * 1000))

    def _notice(self, text: str) -> None:
        """A result message for the user; unlike progress text it stays after the queue goes idle."""
        self.status.setText(text)
        self._status_is_progress = False

    def _idle(self) -> None:
        self.uploader.nudge()  # an export may have just finished
        self.progress.setVisible(False)
        self.cancel_btn.setVisible(False)
        if self._status_is_progress:
            self.status.setText("Ready")

    def _log(self, msg: str) -> None:
        self.log_view.appendPlainText(msg)

    def _warn(self, msg: str) -> None:
        """A problem the user should notice: amber status line plus the log."""
        self._log(f"⚠ {msg}")
        self._notice(f"<span style='color:{theme.WARN}'>⚠ {msg}</span>")

    # ================================================================== editor
    def load_project(self, p: Project | None) -> None:
        self.current = p
        self._update_preview_frame()  # an 'original' frame follows each video's own size
        self._update_out_label()
        self.sel = -1
        self._play_until = None
        ready = p is not None and p.status in ("ready", "done", "exporting") and p.measurements is not None
        if not ready:
            self.preview.load(None)
            self.timeline.clear()
            self.video_title.setText(p.name if p else "No video loaded")
            self.video_meta.setText(STATUS_TEXT.get(p.status, ("", ""))[0] if p else "")
            if p and p.status == "error":
                self.video_meta.setText(f"Failed: {p.error}")
            self._fill_table()
            self._fill_inspector()
            self._update_export_btn()
            return
        v = p.video
        self.video_title.setText(self.video_title.fontMetrics().elidedText(v.title, Qt.ElideRight, 520))
        self._show_meta(p)
        self.preview.load(v.path)
        self.time_label.setText(f"0:00 / {stamp(v.duration)}")
        self.timeline.set_data(v.duration, p.signals, p.score, p.clips)
        self._fill_table()
        if p.clips:
            self.select_clip(0)
        else:
            self._fill_inspector()
        self._update_export_btn()

    def _show_meta(self, p: Project) -> None:
        v = p.video
        bits = [v.channel, stamp(v.duration)]
        if p.transcript_source:
            bits.append(f"transcript: {p.transcript_source}")
        if p.mode_used == "ai":
            bits.append(f"picked by: ✨ {p.ai_model or 'AI'}")
        else:
            bits.append(f"picked by: {p.mode_used or 'signals'}" + (" (AI failed)" if p.ai_error else ""))
        self.video_meta.setText("   " + " · ".join(bits))
        self.video_meta.setToolTip(f"AI clip finder: {p.ai_error}" if p.ai_error else "")

    def _ready(self) -> Project | None:
        p = self.current
        return p if p is not None and p.measurements is not None and p.status in ("ready", "done") else None

    def _fill_table(self) -> None:
        self._filling = True
        clips = self.current.clips if self._ready() else []
        self.table.setRowCount(len(clips))
        for r, c in enumerate(clips):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if c.enabled else Qt.Unchecked)
            chk.setToolTip("Include in export")
            self.table.setItem(r, 0, chk)
            self._fill_row(r, c)
        self._filling = False
        if 0 <= self.sel < len(clips):
            self.table.selectRow(self.sel)

    def _fill_rows(self) -> None:
        """Every row's text again: moving or switching off one clip can change its neighbours' part numbers."""
        clips = self.current.clips if self._ready() else []
        for r, c in enumerate(clips[:self.table.rowCount()]):
            self._fill_row(r, c)

    def _fill_row(self, r: int, c: Clip) -> None:
        was = self._filling
        self._filling = True
        origin = {"ai": "✨ ", "manual": "✎ ", "chapter": "§ "}.get(c.origin, "")
        clips = self.current.clips if self.current else []
        _, part, parts = series.labels(clips)[r] if r < len(clips) else ("", 0, 0)
        cells = [f"{r + 1}" + (f" P{part}" if part else ""), stamp(c.start), f"{c.length:.0f}s", f"{c.score:.0f}",
                 f"{origin}{c.title}"]
        tips = {1: f"Part {part} of {parts}: " + ("the next part starts where this one stops" if part < parts
                                                   else "the last part") if part else "", 5: c.reason}
        for col, text in enumerate(cells, 1):
            item = self.table.item(r, col) or QTableWidgetItem()
            item.setText(text)
            flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | (Qt.ItemIsEditable if col == 5 else Qt.NoItemFlags)
            item.setFlags(flags)
            item.setToolTip(tips.get(col, ""))  # cells are reused, so always reset it
            self.table.setItem(r, col, item)
        self._filling = was

    def _fill_inspector(self) -> None:
        p = self._ready()
        c = p.clips[self.sel] if p and 0 <= self.sel < len(p.clips) else None
        for w in (self.title_edit, self.start_spin, self.end_spin, self.crop_slider, self.preview_btn):
            w.setEnabled(c is not None)
        self._filling = True
        if c is None:
            self.title_edit.clear()
            self.start_spin.setValue(0)
            self.end_spin.setValue(0)
            self.reason.clear()
            self.transcript.clear()
        else:
            self.title_edit.setText(c.title)
            self.start_spin.setMaximum(p.video.duration)
            self.end_spin.setMaximum(p.video.duration)
            self.start_spin.setValue(c.start)
            self.end_spin.setValue(c.end)
            self.crop_slider.setValue(int(c.crop_x * 100))
            self.reason.setText(f"✨ {c.reason}" if c.reason else "")
            self.transcript.setPlainText(p.transcript(c) or "(no transcript for this part)")
            self.preview.set_crop(c.crop_x)
        self._filling = False
        if c is None:
            self.layout_tab.set_clip(None)
            self._refresh_export_view()
        else:
            self._clip_shown()

    def select_clip(self, i: int, seek: bool = True) -> None:
        p = self._ready()
        if not p or not (0 <= i < len(p.clips)):
            return
        self.sel = i
        self._filling = True
        self.table.selectRow(i)
        self._filling = False
        self.timeline.set_selected(i)
        self._fill_inspector()
        if seek:
            self.seek(p.clips[i].start)

    def _table_selected(self) -> None:
        if self._filling:
            return
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.select_clip(rows[0].row())

    def _table_edited(self, item: QTableWidgetItem) -> None:
        p = self._ready()
        if self._filling or not p or item.row() >= len(p.clips):
            return
        c = p.clips[item.row()]
        if item.column() == 0:
            c.enabled = item.checkState() == Qt.Checked
            self._fill_rows()  # a switched-off part breaks its series
            self.timeline.update()
            self._update_export_btn()
        elif item.column() == 5:
            c.title = item.text().lstrip("✨✎§ ").strip()
            c.edited = True
            if item.row() == self.sel:
                self._filling = True
                self.title_edit.setText(c.title)
                self._filling = False
            self.timeline.update()
        self._save_session()

    def _title_typed(self, text: str) -> None:
        p = self._ready()
        if self._filling or not p or not (0 <= self.sel < len(p.clips)):
            return
        p.clips[self.sel].title = text
        p.clips[self.sel].edited = True
        self._fill_row(self.sel, p.clips[self.sel])
        self.timeline.update()
        self._clip_shown()

    def _spin_changed(self, edge: str, value: float) -> None:
        p = self._ready()
        if self._filling or not p or not (0 <= self.sel < len(p.clips)):
            return
        c = p.clips[self.sel]
        if edge == "start":
            c.start = min(value, c.end - 1)
        else:
            c.end = max(value, c.start + 1)
        self._clip_edited(self.sel, seek_to=c.start if edge == "start" else max(c.start, c.end - 3))

    def set_edge(self, edge: str) -> None:
        p = self._ready()
        if not p or not (0 <= self.sel < len(p.clips)):
            return
        t = self.preview.player.position() / 1000
        c = p.clips[self.sel]
        if edge == "start":
            c.start = min(t, c.end - 1)
        else:
            c.end = max(t, c.start + 1)
        self._clip_edited(self.sel)

    def _clip_shown(self) -> None:
        """The selected clip changed (picked, retimed, reframed, retitled): refresh what shows it."""
        p = self._ready()
        if p and 0 <= self.sel < len(p.clips):
            self.layout_tab.set_clip(p.clips[self.sel], p.video.path, p.media, p.words,
                                     next_part=layout.has_next_part(p.clips[self.sel], p.clips, self.settings))
        self._refresh_export_view()

    def _second_media(self, path: str) -> MediaInfo | None:
        """Size and length of the stacked video (probed once per file)."""
        f = Path(path).expanduser() if path else None
        if f is None or not f.is_file():
            return None
        key = f"{f}|{f.stat().st_mtime_ns}"
        if key not in self._second_info:
            try:
                self._second_info[key] = probe(f)
            except Exception:  # unreadable file: the export view shows the layout without it
                return None
        return self._second_info[key]

    def _refresh_export_view(self) -> None:
        """Rebuild the export view's frame for the selected clip from the current settings."""
        if not hasattr(self, "preview"):
            return  # still building the window
        s = self.settings
        p = self._ready()
        if p and p.captions_off:
            s = s.copy(burn_captions=False)  # matches the export: the video has subtitles of its own
        clip = p.clips[self.sel] if p and 0 <= self.sel < len(p.clips) else None
        media = p.media if p and p.media else MediaInfo(width=1920, height=1080)
        stack_video = preview_video(s)
        second = self._second_media(stack_video) if s.stack_enabled else None
        size = (second.width, second.height) if second and second.width else None
        comp = compose.compose(s, media, clip, p.words if p else None, second=size,
                               next_part=layout.has_next_part(clip, p.clips, s) if p and clip else True)
        self.preview.set_composition(comp, clip.start if clip else 0.0, clip.end if clip else 0.0)
        self.preview.set_second(str(Path(stack_video).expanduser()) if size else "", clip.start if clip else 0.0,
                                clip.length if clip else 0.0, s.stack_start, s.stack_offset, s.stack_volume)
        self.safe_btn.setEnabled(comp.safe is not None)

    def _export_view_toggled(self, on: bool) -> None:
        self.qs.setValue("export_view", on)
        self.preview.set_export_view(on)

    def _safe_zone_toggled(self, on: bool) -> None:
        self.qs.setValue("safe_zone", on)
        self.preview.set_safe_zone(on)

    def _clip_dragging(self, i: int) -> None:
        p = self._ready()
        if p and 0 <= i < len(p.clips):
            self._fill_rows()
            if i == self.sel:
                self._refresh_export_view()

    def _clip_edited(self, i: int, seek_to: float | None = None) -> None:
        p = self._ready()
        if not p or not (0 <= i < len(p.clips)):
            return
        c = p.clips[i]
        c.edited = True  # hand-tuned clips (including AI picks) survive re-detection
        if c.origin in ("auto", "part"):
            c.origin = "manual"
        if c.origin != "ai":
            c.score = clip_score(p.score, c.start, c.end)
        self._fill_rows()
        if i == self.sel:
            self._fill_inspector()
        self.timeline.update()
        if seek_to is not None:
            self.seek(seek_to)
        self._save_session()

    def _crop_slid(self, v: int) -> None:
        p = self._ready()
        if self._filling or not p or not (0 <= self.sel < len(p.clips)):
            return
        p.clips[self.sel].crop_x = v / 100
        p.clips[self.sel].edited = True
        self.preview.set_crop(v / 100)
        self._clip_shown()

    def _crop_dragged(self, x: float) -> None:
        p = self._ready()
        if not p or not (0 <= self.sel < len(p.clips)):
            return
        p.clips[self.sel].crop_x = x
        p.clips[self.sel].edited = True
        self._filling = True
        self.crop_slider.setValue(int(x * 100))
        self._filling = False
        self._clip_shown()

    def add_clip(self, t: float) -> None:
        p = self._ready()
        if not p:
            return
        length = min(p.video.duration, (self.settings.min_len + self.settings.max_len) / 2)
        start = max(0.0, min(t, p.video.duration - length))
        c = Clip(start, start + length, clip_score(p.score, start, start + length), "", origin="manual")
        c.title = " ".join(p.transcript(c).split()[:8])
        p.clips.append(c)
        self._fill_table()
        self.timeline.update_data(p.signals, p.score, p.clips)
        self.select_clip(len(p.clips) - 1)
        self._update_export_btn()
        self._save_session()

    def delete_clip(self, i: int) -> None:
        p = self._ready()
        if not p or not (0 <= i < len(p.clips)):
            return
        p.clips.pop(i)
        self.sel = min(i, len(p.clips) - 1)
        self._fill_table()
        self.timeline.update_data(p.signals, p.score, p.clips)
        if self.sel >= 0:
            self.select_clip(self.sel, seek=False)
        else:
            self._fill_inspector()
        self._update_export_btn()
        self._save_session()

    def move_clip(self, d: int) -> None:
        p = self._ready()
        if not p or not (0 <= self.sel < len(p.clips)) or not (0 <= self.sel + d < len(p.clips)):
            return
        i, j = self.sel, self.sel + d
        p.clips[i], p.clips[j] = p.clips[j], p.clips[i]
        self.sel = j
        self._fill_table()
        self.timeline.update_data(p.signals, p.score, p.clips)
        self.select_clip(j, seek=False)

    def sort_clips(self, how: int) -> None:
        p = self._ready()
        if not p or how == 0:
            return
        key = {1: lambda c: -c.score, 2: lambda c: c.start, 3: lambda c: -c.length}[how]
        p.clips.sort(key=key)
        self.sel = 0 if p.clips else -1
        self._fill_table()
        self.timeline.update_data(p.signals, p.score, p.clips)
        if p.clips:
            self.select_clip(0, seek=False)

    def step_clip(self, d: int) -> None:
        p = self._ready()
        if p and p.clips:
            self.select_clip((self.sel + d) % len(p.clips))

    # ================================================================== playback
    def seek(self, t: float) -> None:
        self.preview.player.setPosition(int(max(0.0, t) * 1000))
        self.timeline.set_playhead(t)

    def seek_rel(self, dt: float) -> None:
        self.seek(self.preview.player.position() / 1000 + dt)

    def toggle_play(self) -> None:
        pl = self.preview.player
        if pl.playbackState() == QMediaPlayer.PlayingState:
            pl.pause()
        else:
            self._play_until = None
            pl.play()

    def play_clip(self, i: int) -> None:
        p = self._ready()
        if not p or not (0 <= i < len(p.clips)):
            return
        if i != self.sel:
            self.select_clip(i, seek=False)
        c = p.clips[i]
        self.seek(c.start)
        self._play_until = c.end
        self.preview.player.play()

    def _position_changed(self, ms: int) -> None:
        t = ms / 1000
        self.timeline.set_playhead(t)
        dur = self.current.video.duration if self._ready() else 0
        self.time_label.setText(f"{stamp(t)} / {stamp(dur)}")
        if self._play_until is not None and t >= self._play_until:
            p = self._ready()
            if self.loop.isChecked() and p and 0 <= self.sel < len(p.clips):
                self.seek(p.clips[self.sel].start)
            else:
                self.preview.player.pause()
                self._play_until = None

    def _playback_changed(self, state) -> None:
        self.btn_play.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def _legend_toggled(self) -> None:
        self.timeline.visible = {k for k, cb in self.legend_boxes.items() if cb.isChecked()}
        self.timeline.update()

    # ================================================================== settings & re-detect
    def _setting_changed(self, name: str, value) -> None:
        self.settings = self.settings.apply({name: value})
        if name in ("min_len", "max_len") and self.settings.min_len > self.settings.max_len:
            other = "max_len" if name == "min_len" else "min_len"  # the other end follows along
            self.settings = self.settings.apply({other: value})
            if other in self.tune_editors:
                forms.refresh({other: self.tune_editors[other]}, self.settings)
        self._save_timer.start()
        self.layout_tab.set_settings(self.settings)
        self._update_preview_frame()
        self._update_out_label()
        p = self._ready()
        if not p:
            return
        if name in RESCORE_FIELDS:
            Engine(self.settings).rescore(p)
        if name in REDETECT_FIELDS and self.live.isChecked() and self.settings.mode != "ai":
            self.redetect()
        elif name in RESCORE_FIELDS:
            self.timeline.update_data(p.signals, p.score, p.clips)
            self._fill_table()

    def _update_preview_frame(self) -> None:
        """Match the preview's reframe box to what the export keeps, including a layout's clip area."""
        s = self.settings
        media = self.current.media if self.current and self.current.media else MediaInfo(width=1920, height=1080)
        aspect, w, h = export_frame(s, media)
        if not layout.active(s):
            self.preview.set_aspect(aspect)
        else:
            ratio = layout.crop_ratio(s, w, h)
            self.preview.set_aspect(aspect if ratio else "original", ratio)
        self._refresh_export_view()

    @staticmethod
    def _kept(p: Project) -> list[Clip]:
        """Clips the user made or touched; re-detection never replaces these."""
        return [c for c in p.clips if c.origin == "manual" or c.edited]

    def redetect(self, force_ai: bool = False) -> None:
        p = self._ready()
        if not p:
            return
        if self.settings.mode == "ai" and force_ai:
            self._ask_ai(p)
            return
        keep = self._kept(p)
        Engine(self.settings.copy(mode="auto" if self.settings.mode == "ai" else self.settings.mode)).find(p, use_ai=False)
        self._after_redetect(p, keep)

    def _ask_ai(self, p: Project) -> None:
        from ..ai import has_key, name

        if not has_key(self.settings):
            QMessageBox.information(self, APP_NAME, f"Add your {name(self.settings)} API key in Settings → AI to "
                                                    "use the AI clip finder.")
            return
        if getattr(p, "_ai_pending", False):
            self._notice(f"{name(self.settings)} is already picking clips for this video…")
            return
        p._ai_pending = True  # type: ignore[attr-defined]
        settings = self.settings.copy()
        # Work on a snapshot so the editor stays usable and nothing is overwritten mid-edit;
        # the result is merged on the GUI thread once it arrives.
        snapshot = replace(p, clips=[])

        def run(t: Task):
            t.engine(settings).find(snapshot, strict=True)
            return snapshot

        task = Task("AI is picking clips", run, owner=p)
        task.succeeded.connect(lambda q: self._ai_done(p, q))
        task.failed.connect(lambda msg: self._ai_failed(p, msg))
        self._wire(task)
        self.tasks.add(task)

    def _ai_done(self, p: Project, q: Project) -> None:
        p._ai_pending = False  # type: ignore[attr-defined]
        if self.settings.mode != "ai" or p not in self.projects:
            self._log("Ignored the AI result: the clip finder was switched away from AI while it ran.")
            return
        keep = self._kept(p)  # recomputed now, so edits made while waiting are respected
        p.clips, p.mode_used, p.ai_model, p.ai_error = q.clips, "ai", q.ai_model, ""
        self._after_redetect(p, keep)
        self._notice(f"✨ {q.ai_model or 'The AI'} picked {len(q.clips)} clips"
                            + (f" · kept {len(keep)} you edited" if keep else ""))

    def _ai_failed(self, p: Project, msg: str) -> None:
        p._ai_pending = False  # type: ignore[attr-defined]
        if msg == "Cancelled" or self._closing:
            if not self._closing:
                self._notice("AI clip picking cancelled. Your clips are unchanged.")
            return
        self._warn(f"AI clip finder: {msg}")
        QMessageBox.warning(self, APP_NAME, f"The AI clip finder couldn't pick clips:\n\n{msg}\n\n"
                                            "Your current clips were left unchanged.")

    def _after_redetect(self, p: Project, keep: list[Clip]) -> None:
        clips = [c for c in p.clips if c not in keep and not any(c.start < k.end and c.end > k.start for k in keep)]
        p.clips = keep + clips
        if self.current is p:
            self._show_meta(p)
            self.timeline.update_data(p.signals, p.score, p.clips)
            self.sel = 0 if p.clips else -1
            self._fill_table()
            if p.clips:
                self.select_clip(0, seek=False)
            else:
                self._fill_inspector()
            self._update_export_btn()
        self._refresh_item(p)
        self._save_session()

    def _fill_presets(self) -> None:
        self.preset.clear()
        self.preset.addItem("Choose a preset…")
        for name in all_presets():
            self.preset.addItem(name)
        self.preset.insertSeparator(self.preset.count())
        self.preset.addItem("💾  Save current settings as preset…")

    def _preset_chosen(self, index: int) -> None:
        text = self.preset.itemText(index)
        self.preset.setCurrentIndex(0)
        if index == 0:
            return
        if text.startswith("💾"):
            name, ok = QInputDialog.getText(self, "Save preset", "Preset name:")
            if ok and name.strip():
                save_user_preset(name.strip(), self.settings)
                self._fill_presets()
                self._log(f"Saved preset '{name.strip()}'")
            return
        values = all_presets().get(text)
        if values is None:
            return
        self._apply_settings(self.settings.apply(values))
        self._log(f"Applied preset '{text}'")
        self._notice(f"Preset applied: {text}")

    def _apply_settings(self, new: Settings) -> None:
        old = self.settings
        self.settings = new
        self.settings.save()
        self.uploader.nudge()
        forms.refresh(self.tune_editors, new)
        forms.refresh(self.export_editors, new)
        self.research.settings_changed()
        self.niches.reload()
        self.automation.niches_changed()
        self.layout_tab.set_settings(new, refresh_editors=True)
        self._update_preview_frame()
        self._update_out_label()
        p = self._ready()
        if p and any(getattr(old, k) != getattr(new, k) for k in REDETECT_FIELDS):
            Engine(new).rescore(p)
            if new.mode != "ai":
                self.redetect()
            else:
                self.timeline.update_data(p.signals, p.score, p.clips)

    def _apply_layout_preset(self, name: str, values: dict) -> None:
        """Swap in a top/bottom view. A stacked view needs a second video, so ask for one if there isn't one yet."""
        stack_video = preview_video(self.settings.apply(values))
        if values.get("stack_enabled") and not (stack_video and Path(stack_video).is_file()):
            exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
            path, _ = QFileDialog.getOpenFileName(self, "Choose the video to stack (gameplay, satisfying clips…)",
                                                  "", f"Videos ({exts})")
            if path:
                values["stack_video"] = path
        self._apply_settings(self.settings.apply(values))
        self._log(f"Applied layout '{name}'")
        self._notice(f"Layout applied: {name}")

    def _open_settings(self, tab: str = "") -> None:
        self.niches.commit()  # a niche name still being typed would be lost when the dialog's copy comes back
        dlg = SettingsDialog(self.settings, self, tab)
        channel = self.settings.update_channel
        if dlg.exec():
            self._apply_settings(dlg.result_settings)
            if dlg.go_back:
                self.banner.go_back()
            elif self.settings.update_channel != channel and self.settings.check_updates:
                self.banner.check(manual=False)  # a new channel: see what it has straight away
        elif dlg.signed_in:  # cancelled, but the sign-in just made still counts
            self._apply_settings(self.settings.copy(cookies_browser=youtube_login.CHOICE))
        if dlg.drive_changed:
            self.uploader.nudge()
            pipeline.request_uploads(self.automation.db)
        if dlg.signed_in and self.settings.cookies_browser == youtube_login.CHOICE:
            self.automation.end_pause_after_sign_in()

    # ================================================================== export
    def _update_export_btn(self) -> None:
        p = self._ready()
        n = sum(c.enabled for c in p.clips) if p else 0
        self.export_btn.setEnabled(n > 0)
        self.export_btn.setText(f"Export {n} clip{'s' if n != 1 else ''}" if p else "Export clips")

    def _update_out_label(self) -> None:
        s = self.settings
        media = self.current.media if self.current and self.current.media else MediaInfo(width=1920, height=1080)
        _, w, h = export_frame(s, media)
        size = f"{w}×{h}" + (" · TikTok-ready" if s.tiktok_ready else "")
        self.out_label.setText(f"Output: {size}\nSaving to: {s.output_path}")

    def _open_output(self) -> None:
        p = self.current
        target = p.export_dir if p and p.export_dir else self.settings.output_path
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def export_current(self) -> None:
        p = self._ready()
        if p:
            self._queue_export(p)

    def export_all(self) -> None:
        for p in self.projects:
            if p.status == "ready" and p.measurements is not None:
                self._queue_export(p)

    def _queue_export(self, p: Project) -> None:
        settings = self.settings.copy()
        clips = [replace(c) for c in p.clips if c.enabled]
        if not clips:
            return
        p.status = "exporting"
        self._refresh_item(p)

        def run(t: Task):
            folder = t.engine(settings).export(p, clips)
            return folder

        def done(folder: Path) -> None:
            p.status = "done"
            self._refresh_item(p)
            self.library.refresh()
            url = QUrl.fromLocalFile(str(folder)).toString()
            self._notice(f"✓ Exported {len(clips)} clips from “{p.name[:50]}” — "
                                f"<a style='color:{theme.ACCENT_2}' href='{url}'>open folder</a>")
            self._save_session()

        def failed(msg: str) -> None:
            p.status = "ready"
            self._refresh_item(p)
            self._log(f"✗ Export failed for {p.name}: {msg}")
            # A dialog after the window has closed would keep the old process alive
            # (and block an update from replacing it), so only report real failures while open.
            if msg != "Cancelled" and not self._closing:
                QMessageBox.warning(self, APP_NAME, f"Export failed:\n\n{msg}")

        task = Task(f"Exporting {p.name}", run, owner=p)
        task.succeeded.connect(done)
        task.failed.connect(failed)
        self._wire(task)
        self.tasks.add(task)
        if self.current is p:
            self._update_export_btn()

    def render_preview(self) -> None:
        p = self._ready()
        if not p or not (0 <= self.sel < len(p.clips)):
            return
        clip = replace(p.clips[self.sel])
        settings = self.settings.copy(fast_copy=False)
        out_dir = work_dir() / "previews"
        out_dir.mkdir(exist_ok=True)
        dest = out_dir / f"preview_{p.video.id}_{int(clip.start)}.mp4"

        next_part = layout.has_next_part(p.clips[self.sel], p.clips, settings)

        def run(t: Task):
            export_clip(p.video.path, p.media, dest, clip, p.words, settings,
                        lambda f: t.progress.emit("Rendering preview", f), next_part=next_part)
            return dest

        task = Task("Rendering preview", run, owner=None)
        task.succeeded.connect(lambda path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
        self._wire(task)
        self.tasks.add(task)

    # ================================================================== misc
    def _show_help(self) -> None:
        rows = "".join(f"<tr><td style='padding:3px 14px 3px 0'><b>{k}</b></td><td>{v}</td></tr>" for k, v in SHORTCUTS)
        repo = updater.configured_repo(self.settings.update_repo)
        source = f"Updates come from github.com/{repo}." if repo else "No update source is configured yet."
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        box.setText(f"""
            <h3>{APP_NAME} {updater.build_label()}</h3>
            <p>Paste links or drop videos, let the analysis find the best moments, fine-tune them on the
            timeline, then export clips with captions in any format.</p>
            <p><b>Clip colours:</b> purple = signals, cyan = AI, green = edited by you.</p>
            <table>{rows}</table>
            <p style='color:{theme.MUTED}'>{source}<br>Settings, presets and your session live in {app_dir()}</p>""")
        check = box.addButton("Check for updates", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is check:
            self.banner.check(manual=True)

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        md = e.mimeData()
        if md.hasUrls():
            self.add_sources([u.toLocalFile() if u.isLocalFile() else u.toString() for u in md.urls()])
        elif md.hasText():
            self.add_sources(md.text().split())

    # ---------------------------------------------------------------- session
    def _session_path(self) -> Path:
        return app_dir() / "session.json"

    def _save_session(self) -> None:
        data = []
        for p in self.projects:
            if p.status == "error":
                continue
            saved = getattr(p, "_saved", None)
            clips = [asdict(c) for c in p.clips] if p.clips else (saved or {}).get("clips", [])
            polish = p.meta.get("polish") or (saved or {}).get("polish")
            data.append({"source": p.source, "id": p.video.id if p.video else (saved or {}).get("id", ""),
                         "clips": clips, "mode": p.mode_used or (saved or {}).get("mode", ""),
                         "title": p.video.title if p.video else (saved or {}).get("title", ""),
                         **({"polish": polish} if polish else {})})
        try:
            self._session_path().write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass

    def _restore(self) -> None:
        geo = self.qs.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        for key, split in (("split", self.split), ("center", self.center_split)):
            state = self.qs.value(key)
            if state:
                split.restoreState(state)
        try:
            data = json.loads(self._session_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        for entry in data:
            if entry.get("source"):
                self._add_project(entry["source"], saved=entry)
        if data:
            self._log(f"Restored {len(data)} videos from your last session — click one to load it.")

    def closeEvent(self, e) -> None:
        self._closing = True
        self.uploader.stop(wait=3)  # an upload cut off carries on next time, from where it stopped
        self.banner.shutdown()
        self.research.shutdown()
        self.niches.shutdown()
        self.automation.shutdown()
        self._save_session()
        self.settings.save()
        self.qs.setValue("geometry", self.saveGeometry())
        self.qs.setValue("split", self.split.saveState())
        self.qs.setValue("center", self.center_split.saveState())
        self.preview.player.stop()
        self.tasks.cancel_all()
        if self.tasks.current:
            self.tasks.current.wait(4000)
            if self.tasks.current and self.tasks.current.isRunning():
                self.tasks.current.terminate()
        super().closeEvent(e)


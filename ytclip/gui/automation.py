"""Automation tab: watch and steer the unattended runner (pipeline.py) through jobs.db.

The runner is its own process, so it keeps going when this window closes. This tab starts it,
stops or pauses it, queues links by hand, and shows the queue, each niche, clip requests and the
runner's log.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import learn, niches, pipeline
from ..clipability import age_text
from ..jobs import Job, JobDB
from ..research import youtube_key
from ..settings import Settings
from . import theme
from .library import reveal
from .overnight import OvernightPanel
from .requests_panel import NichePanel, RequestDialog, RequestPanel

REPORT_EVERY = 30.0  # seconds between refreshes of the Overnight page while it's showing

COLUMNS = ("Video", "Channel", "Niche", "Status", "Clips", "Updated", "Why it was picked")
NICHE_COLUMN = COLUMNS.index("Niche")
STATUS_COLUMN = COLUMNS.index("Status")
STATUS_COLOURS = {"working": theme.ACCENT_2, "queued": theme.TEXT, "done": theme.GOOD, "failed": theme.BAD}
STATUS_WORDS = {"working": "Working", "queued": "Waiting", "done": "Done", "failed": "Failed"}
LOG_LINES = 300


class Automation(QWidget):
    notice = Signal(str)
    open_settings = Signal(str)
    clean_up = Signal()  # open the Clean up window
    niches_edited = Signal()  # a niche was paused or resumed here

    def __init__(self, settings: Callable[[], Settings], db: JobDB | None = None,
                 launch: Callable[[], None] = pipeline.launch_background,
                 change: Callable[[str, Any], None] | None = None, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._change = change
        self.db = db or JobDB()
        self._launch = launch
        self._starting_until = 0.0  # just launched: show "Starting…" until its first heartbeat
        self._sig = None
        self._log_size = -1
        self._windows_checked = False
        self._lessons_sig: tuple | None = None
        self._niche_names: dict[str, str] = {}
        self._build()
        self.niches_changed()
        self.refresh()
        self.timer = QTimer(self, interval=3000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        QTimer.singleShot(2000, self._while_away)
        if self.db.meta("resume_after_update") == "1":  # stopped so the app could update itself
            self.db.set_meta("resume_after_update", "0")
            self.start()

    # ================================================================== UI
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("Automation")
        title.setObjectName("title")
        self.status = QLabel()
        self.status.setTextFormat(Qt.RichText)
        self.start_btn = QPushButton("▶  Start")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setToolTip("Stops after a few seconds. A video in progress goes back in the queue")
        self.stop_btn.clicked.connect(self.stop)
        self.pause_btn = QPushButton()
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.try_btn = QPushButton("↻  Try now")
        self.try_btn.setToolTip("End the sign-in pause and try YouTube again now. If it asks again, the next pause "
                                "is longer, so sign in to YouTube in a browser and pick it in Settings → Download → "
                                "Use browser login first")
        self.try_btn.clicked.connect(self.try_now)
        top.addWidget(title)
        top.addWidget(self.status, 1)
        top.addWidget(self.try_btn)
        top.addWidget(self.pause_btn)
        top.addWidget(self.stop_btn)
        top.addWidget(self.start_btn)
        layout.addLayout(top)

        row = QHBoxLayout()
        self.link = QLineEdit()
        self.link.setPlaceholderText("Paste a YouTube link to queue it yourself")
        self.link.returnPressed.connect(self.add_link)
        self.niche = QComboBox()
        self.niche.setToolTip("The niche a video you queue is for: its style, and its folder")
        add = QPushButton("Queue")
        add.clicked.connect(self.add_link)
        self.with_windows = QCheckBox("Start with Windows")
        self.with_windows.setToolTip("Run automation in the background whenever you sign in, even with this "
                                     "window closed")
        self.with_windows.toggled.connect(self._toggle_windows)
        self.with_windows.setVisible(os.name == "nt")
        rules = QPushButton("Picking rules…")
        rules.clicked.connect(lambda: self.open_settings.emit("Picking"))
        setup = QPushButton("Automation settings…")
        setup.clicked.connect(lambda: self.open_settings.emit("Automation"))
        open_out = QPushButton("Open clips folder")
        open_out.clicked.connect(self._open_output)
        clean = QPushButton("🧹  Clean up…")
        clean.setToolTip("Clear out downloads, rejected clips and clips TikTok Manager is done with. Shows what "
                         "would go first")
        clean.clicked.connect(self.clean_up.emit)
        row.addWidget(self.link, 1)
        row.addWidget(self.niche)
        row.addWidget(add)
        row.addSpacing(10)
        row.addWidget(self.with_windows)
        row.addWidget(rules)
        row.addWidget(setup)
        row.addWidget(open_out)
        row.addWidget(clean)
        layout.addLayout(row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("panel")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeToContents)
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        head.setSectionResizeMode(STATUS_COLUMN, QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.itemDoubleClicked.connect(lambda item: self._open_folder(self._job_at(item.row())))

        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setProperty("muted", True)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("panel")
        self.log_view.setMaximumBlockCount(LOG_LINES)

        queue = QWidget()
        uv = QVBoxLayout(queue)
        uv.setContentsMargins(0, 6, 0, 0)
        uv.addWidget(self.table, 1)
        uv.addWidget(self.empty, 1)
        self.counts = QLabel()
        self.counts.setProperty("muted", True)
        uv.addWidget(self.counts)
        self.learned = QLabel()
        self.learned.setTextFormat(Qt.RichText)
        self.learned.setWordWrap(True)
        self.learned.setToolTip("From the views TikTok Manager reads for your posted clips (Settings → Picking → "
                                "Learn from how my clips do). Details: ytclip results")
        uv.addWidget(self.learned)

        self.niche_panel = NichePanel(self._settings, self._niche_change, self.db)
        self.niche_panel.notice.connect(self.notice.emit)
        self.niche_panel.request.connect(self.new_request)
        self.request_panel = RequestPanel(self._settings, self.db)
        self.request_panel.notice.connect(self.notice.emit)
        self.request_panel.new.connect(lambda: self.new_request(""))
        self.pages = QTabWidget()
        self.pages.setDocumentMode(True)
        self.pages.addTab(queue, "Queue")
        self.pages.addTab(self.niche_panel, "Niches")
        self.pages.addTab(self.request_panel, "Requests")
        self.overnight = OvernightPanel(self._settings, self.db)
        self.pages.addTab(self.overnight, "Overnight report")
        self._report_at = 0.0
        self.pages.currentChanged.connect(lambda _i: self._refresh_report(force=True))
        upper = self.pages

        split = QSplitter(Qt.Vertical)
        split.addWidget(upper)
        split.addWidget(self.log_view)
        split.setStretchFactor(0, 3)
        split.setSizes([520, 180])
        layout.addWidget(split, 1)

        hint = QLabel("Automation checks Research on your schedule, queues what your picking rules pick, and clips "
                      "one video at a time with the style in Automation settings. It runs on its own, so it keeps "
                      "going after you close ClipMonster. Clips go to the Automation folder; right-click a video "
                      "to open its clips, try it again or remove it. Requests ask for a number of clips of a "
                      "length, and go ahead of everything else.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

    # ================================================================== state
    def niches_changed(self) -> None:
        """Niches were added, renamed or removed: the queue's Niche column and the niche picker follow."""
        all_niches = niches.load(self._settings())
        self._niche_names = {n.id: n.name for n in all_niches}
        keep = self.niche.currentData()
        self.niche.blockSignals(True)
        self.niche.clear()
        for n in all_niches:
            self.niche.addItem(n.name, n.id)
        self.niche.addItem("No niche", "")
        self.niche.setCurrentIndex(max(0, self.niche.findData(keep)) if keep is not None else 0)
        self.niche.blockSignals(False)
        self.niche.setVisible(bool(all_niches))
        self._sig = None  # redraw the table with the new names
        self.niche_panel._sig = self.request_panel._sig = None
        self.niche_panel.refresh()
        self.request_panel.refresh()

    def running(self) -> bool:
        return pipeline.runner_alive(self.db)

    def refresh(self) -> None:
        alive = self.running()
        starting = not alive and time.time() < self._starting_until
        paused = self.db.meta("paused") == "1"
        state = self.db.meta("state")
        if alive:
            dot = theme.WARN if paused else theme.GOOD
            text = f"<span style='color:{dot}'>●</span> Running" + (" · paused" if paused else "")
            if state and not paused:
                text += f" · <span style='color:{theme.MUTED}'>{_esc(state)}</span>"
        elif starting:
            text = f"<span style='color:{theme.ACCENT_2}'>●</span> Starting…"
        elif hung := pipeline.runner_hung(self.db):
            text = (f"<span style='color:{theme.WARN}'>●</span> Not responding (process {hung}). "
                    f"<span style='color:{theme.MUTED}'>If it stays like this, end it in Task Manager, then press "
                    "Start</span>")
        else:
            text = f"<span style='color:{theme.MUTED}'>○ Not running</span>"
        blocked = pipeline.paused_until(self.db, time.time())
        if blocked and not (alive and state.startswith("Paused until")):
            until = time.strftime("%H:%M", time.localtime(blocked))
            text += (f"   <span style='color:{theme.WARN}'>YouTube asked this PC to sign in: no downloads until "
                     f"{until}</span>")
        self.try_btn.setVisible(bool(blocked))
        if not youtube_key(self._settings()):
            text += (f"   <span style='color:{theme.WARN}'>No YouTube API key, so nothing new is picked "
                     f"(Settings → Research)</span>")
        self.status.setText("   " + text)
        self.start_btn.setVisible(not alive and not starting)
        self.stop_btn.setVisible(alive)
        self.pause_btn.setVisible(alive or paused)
        self.pause_btn.setText("▶  Resume" if paused else "❚❚  Pause")
        self.pause_btn.setToolTip("Carry on" if paused else "Finish nothing new until you resume; a video in "
                                                            "progress carries on")
        self._fill_table()
        self.niche_panel.refresh()
        self.request_panel.refresh()
        self._refresh_report()
        self._tail_log()
        self._show_lessons()

    def _show_lessons(self) -> None:
        """What's working, from TikTok Manager's results (re-read only when the file changes)."""
        path = learn.results_path()
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = None
        on = self._settings().learn_from_results
        if (stamp, on) == self._lessons_sig:
            return
        self._lessons_sig = (stamp, on)
        lines = learn.Lessons.load(path).summary_lines() if stamp is not None else []
        if not lines:
            self.learned.setVisible(False)
            return
        head = "📈 What's working" + ("" if on else " (not used: learning is off in Settings → Picking)")
        self.learned.setText(f"<b>{head}</b><br>" + "<br>".join(
            f"<span style='color:{theme.MUTED}'>{_esc(line)}</span>" for line in lines))
        self.learned.setVisible(True)

    def _fill_table(self) -> None:
        jobs = self.db.jobs()
        sig = [(j.id, j.status, j.stage, j.updated, j.clips, j.rejected, j.niche) for j in jobs]
        counts = self.db.counts()
        self.counts.setText(f"{counts['queued']} waiting · {counts['working']} working · {counts['done']} done · "
                            f"{counts['failed']} failed")
        if sig == self._sig:
            return
        self._sig = sig
        keep = {self._job_at(i.row()).id for i in self.table.selectedItems() if self._job_at(i.row())}
        self.table.setRowCount(len(jobs))
        now = time.time()
        for r, j in enumerate(jobs):
            word = STATUS_WORDS.get(j.status, j.status)
            status = word + (f": {j.stage}" if j.stage and j.stage not in (word, "Done") else "")
            tip = j.error if j.error and j.status != "done" else ""
            niche = self._niche_names.get(j.niche, "(deleted niche)" if j.niche else "")
            cells = [
                _item(j.title or j.url, j.url),
                _item(j.channel),
                _item(niche),
                _item(status, "\n".join(x for x in (status, tip, j.recipe and f"Style: {j.recipe}") if x)),
                _item(f"{j.clips} ✓ {j.rejected} ✗" if j.rejected else str(j.clips) if j.clips else "",
                      f"{j.clips} ready to upload, {j.rejected} rejected by the quality check (in the _rejected "
                      "folder)" if j.rejected else ""),
                _item(age_text((now - j.updated) / 3600) + " ago" if j.updated else ""),
                _item(j.permission),
            ]
            cells[0].setData(Qt.UserRole, j.id)
            cells[STATUS_COLUMN].setForeground(QColor(STATUS_COLOURS.get(j.status, theme.TEXT)))
            cells[STATUS_COLUMN + 1].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for c, cell in enumerate(cells):
                self.table.setItem(r, c, cell)
            if j.id in keep:
                self.table.selectRow(r)
        self.table.setColumnHidden(NICHE_COLUMN, not self._niche_names and not any(j.niche for j in jobs))
        has = bool(jobs)
        self.table.setVisible(has)
        self.empty.setVisible(not has)
        if not has:
            self.empty.setText("Nothing queued yet. Press Start: automation checks Research and queues what your "
                               "picking rules pick. You can also paste a link above.")

    def _tail_log(self) -> None:
        path = pipeline.log_path()
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size == self._log_size:
            return
        self._log_size = size
        try:
            with open(path, "rb") as f:
                f.seek(max(0, size - 60_000))
                lines = f.read().decode("utf-8", "replace").splitlines()[-LOG_LINES:]
        except OSError:
            lines = []
        self.log_view.setPlainText("\n".join(lines))
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def _job_at(self, row: int) -> Job | None:
        it = self.table.item(row, 0)
        return self.db.get(it.data(Qt.UserRole)) if it else None

    def _selected_jobs(self) -> list[Job]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [j for j in (self._job_at(r) for r in rows) if j]

    # ================================================================== actions
    def start(self) -> None:
        if self.running():
            return
        try:
            self._launch()
        except OSError as exc:
            self.notice.emit(f"<span style='color:{theme.WARN}'>⚠ Couldn't start automation: {_esc(str(exc))}</span>")
            return
        self._starting_until = time.time() + 20
        self.notice.emit("Automation started. It keeps running when you close ClipMonster.")
        self.refresh()

    def stop(self) -> None:
        self.db.set_meta("stop", "1")
        self.notice.emit("Automation is stopping. A video in progress goes back in the queue.")

    def try_now(self) -> None:
        n = pipeline.end_pause(self.db, time.time())
        self.notice.emit("Trying YouTube again now" + (f" with {n} waiting video{'s' if n != 1 else ''}." if n else ".")
                         + ("" if self.running() else " Press Start to run automation."))
        self.refresh()

    def toggle_pause(self) -> None:
        paused = self.db.meta("paused") == "1"
        self.db.set_meta("paused", "0" if paused else "1")
        self.refresh()

    def add_link(self) -> None:
        text = self.link.text().strip()
        if not text:
            return
        added = 0
        niche = self.niche.currentData() if not self.niche.isHidden() else ""
        for url in text.split():
            try:
                added += pipeline.Runner(self.db).add_manual(url, niche=niche or "")
            except ValueError as exc:
                self.notice.emit(f"<span style='color:{theme.WARN}'>⚠ {_esc(str(exc))}</span>")
                return
        self.link.clear()
        self.notice.emit(f"Queued {added} video{'s' if added != 1 else ''}." if added else
                         "That video was queued before. Right-click it to try again.")
        self.refresh()

    def _while_away(self) -> None:
        """Opening the app after automation worked on its own: one line on what it did."""
        from .. import report

        now = time.time()
        seen = self.db.meta_float("report_seen")
        self.db.set_meta("report_seen", now)
        if not seen:
            return
        try:
            r = report.build(self.db, self._settings(), hours=min(24.0, max(0.0, now - seen) / 3600), now=now)
        except Exception:
            return
        if r.videos or r.failed:
            self.notice.emit(f"While you were away: {r.headline().split(': ', 1)[-1]} "
                             "Automation → Overnight report has the details.")

    def _refresh_report(self, force: bool = False) -> None:
        if self.pages.currentWidget() is not self.overnight:
            return
        if force or time.time() - self._report_at >= REPORT_EVERY:
            self._report_at = time.time()
            self.overnight.refresh(force=force)

    def _niche_change(self, name: str, value) -> None:
        if self._change is not None:
            self._change(name, value)
            self.niches_edited.emit()

    def new_request(self, niche_id: str = "") -> None:
        dlg = RequestDialog(self._settings(), niche_id or "", self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.order is None:
            return
        order = pipeline.Runner(self.db, load=self._settings, log=pipeline.file_logger()).place(dlg.order)
        self.notice.emit(f"Requested {order.count} clips of {order.length_text()}. {order.message}."
                         + ("" if self.running() else " Press Start to begin."))
        self.pages.setCurrentWidget(self.request_panel)
        self.refresh()

    def _toggle_windows(self, on: bool) -> None:
        err = pipeline.set_start_with_windows(on)
        if err:
            self.with_windows.blockSignals(True)
            self.with_windows.setChecked(not on)
            self.with_windows.blockSignals(False)
            self.notice.emit(f"<span style='color:{theme.WARN}'>⚠ {_esc(err)}</span>")
        else:
            self.notice.emit("Automation starts when you sign in to Windows." if on else
                             "Automation no longer starts with Windows.")

    def _open_output(self) -> None:
        path = self._settings().auto_output_path
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_folder(self, job: Job | None) -> None:
        if job and job.folder and Path(job.folder).exists():
            index = Path(job.folder) / "clips.json"
            reveal(index if index.exists() else Path(job.folder))

    def _menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        job = self._job_at(item.row()) if item else None
        if not job:
            return
        jobs = self._selected_jobs() or [job]
        menu = QMenu(self)
        if job.folder:
            menu.addAction("Show clips", lambda: self._open_folder(job))
        if any(j.status in ("failed", "done") for j in jobs):
            menu.addAction("Clip again" if all(j.status == "done" for j in jobs) else "Try again",
                           lambda: self._each(jobs, self.db.retry))
        if job.error:
            menu.addAction("Copy error", lambda: QApplication.clipboard().setText(job.error))
        menu.addAction("Open on YouTube", lambda: QDesktopServices.openUrl(QUrl(job.url)))
        menu.addSeparator()
        menu.addAction("Remove from the list", lambda: self._each([j for j in jobs if j.status != "working"],
                                                                  self.db.remove))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _each(self, jobs: list[Job], fn) -> None:
        for j in jobs:
            fn(j.id)
        self._sig = None
        self.refresh()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not self._windows_checked and os.name == "nt":
            self._windows_checked = True
            self.with_windows.blockSignals(True)
            self.with_windows.setChecked(pipeline.starts_with_windows())
            self.with_windows.blockSignals(False)
        self.refresh()

    def shutdown(self) -> None:
        self.timer.stop()


def _item(text: str, tip: str = "") -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    if tip:
        it.setToolTip(tip)
    return it


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

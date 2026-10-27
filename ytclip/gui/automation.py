"""Automation tab: watch and steer the unattended runner (pipeline.py) through jobs.db.

The runner is its own process, so it keeps going when this window closes. This tab starts it,
stops or pauses it, queues links by hand, and shows the queue and the runner's log.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMenu, QPlainTextEdit, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import pipeline
from ..clipability import age_text
from ..jobs import Job, JobDB
from ..research import youtube_key
from ..settings import Settings
from . import theme
from .library import reveal

COLUMNS = ("Video", "Channel", "Status", "Clips", "Updated", "Why it was picked")
STATUS_COLOURS = {"working": theme.ACCENT_2, "queued": theme.TEXT, "done": theme.GOOD, "failed": theme.BAD}
STATUS_WORDS = {"working": "Working", "queued": "Waiting", "done": "Done", "failed": "Failed"}
LOG_LINES = 300


class Automation(QWidget):
    notice = Signal(str)
    open_settings = Signal(str)

    def __init__(self, settings: Callable[[], Settings], db: JobDB | None = None,
                 launch: Callable[[], None] = pipeline.launch_background, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.db = db or JobDB()
        self._launch = launch
        self._starting_until = 0.0  # just launched: show "Starting…" until its first heartbeat
        self._sig = None
        self._log_size = -1
        self._windows_checked = False
        self._build()
        self.refresh()
        self.timer = QTimer(self, interval=3000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
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
        top.addWidget(title)
        top.addWidget(self.status, 1)
        top.addWidget(self.pause_btn)
        top.addWidget(self.stop_btn)
        top.addWidget(self.start_btn)
        layout.addLayout(top)

        row = QHBoxLayout()
        self.link = QLineEdit()
        self.link.setPlaceholderText("Paste a YouTube link to queue it yourself")
        self.link.returnPressed.connect(self.add_link)
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
        row.addWidget(self.link, 1)
        row.addWidget(add)
        row.addSpacing(10)
        row.addWidget(self.with_windows)
        row.addWidget(rules)
        row.addWidget(setup)
        row.addWidget(open_out)
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
        head.setSectionResizeMode(2, QHeaderView.Stretch)
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

        upper = QWidget()
        uv = QVBoxLayout(upper)
        uv.setContentsMargins(0, 0, 0, 0)
        uv.addWidget(self.table, 1)
        uv.addWidget(self.empty, 1)
        self.counts = QLabel()
        self.counts.setProperty("muted", True)
        uv.addWidget(self.counts)

        split = QSplitter(Qt.Vertical)
        split.addWidget(upper)
        split.addWidget(self.log_view)
        split.setStretchFactor(0, 3)
        split.setSizes([520, 180])
        layout.addWidget(split, 1)

        hint = QLabel("Automation checks Research on your schedule, queues what your picking rules pick, and clips "
                      "one video at a time with the style in Automation settings. It runs on its own, so it keeps "
                      "going after you close ClipMonster. Clips go to the Automation folder; right-click a video "
                      "to open its clips, try it again or remove it.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

    # ================================================================== state
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
        else:
            text = f"<span style='color:{theme.MUTED}'>○ Not running</span>"
        if not youtube_key(self._settings()):
            text += (f"   <span style='color:{theme.WARN}'>No YouTube API key, so nothing new is picked "
                     f"(Settings → Research)</span>")
        self.status.setText("   " + text)
        self.start_btn.setVisible(not alive and not starting)
        self.stop_btn.setVisible(alive)
        self.pause_btn.setText("▶  Resume" if paused else "❚❚  Pause")
        self.pause_btn.setToolTip("Carry on" if paused else "Finish nothing new until you resume; a video in "
                                                            "progress carries on")
        self._fill_table()
        self._tail_log()

    def _fill_table(self) -> None:
        jobs = self.db.jobs()
        sig = [(j.id, j.status, j.stage, j.updated, j.clips) for j in jobs]
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
            cells = [
                _item(j.title or j.url, j.url),
                _item(j.channel),
                _item(status, "\n".join(x for x in (status, tip, j.recipe and f"Style: {j.recipe}") if x)),
                _item(str(j.clips) if j.clips else ""),
                _item(age_text((now - j.updated) / 3600) + " ago" if j.updated else ""),
                _item(j.permission),
            ]
            cells[0].setData(Qt.UserRole, j.id)
            cells[2].setForeground(QColor(STATUS_COLOURS.get(j.status, theme.TEXT)))
            cells[3].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for c, cell in enumerate(cells):
                self.table.setItem(r, c, cell)
            if j.id in keep:
                self.table.selectRow(r)
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

    def toggle_pause(self) -> None:
        paused = self.db.meta("paused") == "1"
        self.db.set_meta("paused", "0" if paused else "1")
        self.refresh()

    def add_link(self) -> None:
        text = self.link.text().strip()
        if not text:
            return
        added = 0
        for url in text.split():
            try:
                added += pipeline.Runner(self.db).add_manual(url)
            except ValueError as exc:
                self.notice.emit(f"<span style='color:{theme.WARN}'>⚠ {_esc(str(exc))}</span>")
                return
        self.link.clear()
        self.notice.emit(f"Queued {added} video{'s' if added != 1 else ''}." if added else
                         "That video was queued before. Right-click it to try again.")
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

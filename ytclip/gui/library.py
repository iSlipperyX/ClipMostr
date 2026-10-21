"""Library tab: every exported video and its clips, with thumbnails."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu,
                               QPushButton, QSplitter, QVBoxLayout, QWidget)

from ..organize import stamp
from ..settings import Settings


def reveal(path: Path) -> None:
    """Open Explorer with the file selected."""
    try:
        subprocess.Popen(["explorer", "/select,", str(path)])
    except OSError:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


class Library(QWidget):
    reopen = Signal(str)

    def __init__(self, settings: Callable[[], Settings], parent=None):
        super().__init__(parent)
        self._settings = settings
        self._videos: list[tuple[Path, dict]] = []

        top = QHBoxLayout()
        title = QLabel("Library")
        title.setObjectName("title")
        self.summary = QLabel()
        self.summary.setProperty("muted", True)
        refresh = QPushButton("⟳ Refresh")
        refresh.clicked.connect(self.refresh)
        open_dir = QPushButton("Open output folder")
        open_dir.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._settings().output_path))))
        self.reopen_btn = QPushButton("Re-open in Studio")
        self.reopen_btn.clicked.connect(self._reopen)
        top.addWidget(title)
        top.addWidget(self.summary, 1)
        top.addWidget(self.reopen_btn)
        top.addWidget(open_dir)
        top.addWidget(refresh)

        self.videos = QListWidget()
        self.videos.setObjectName("panel")
        self.videos.setSpacing(2)
        self.videos.currentRowChanged.connect(self._show_video)

        self.clips = QListWidget()
        self.clips.setObjectName("panel")
        self.clips.setViewMode(QListWidget.IconMode)
        self.clips.setIconSize(QSize(256, 144))
        self.clips.setGridSize(QSize(280, 220))
        self.clips.setResizeMode(QListWidget.Adjust)
        self.clips.setMovement(QListWidget.Static)
        self.clips.setWordWrap(True)
        self.clips.setSpacing(8)
        self.clips.itemDoubleClicked.connect(self._play)
        self.clips.setContextMenuPolicy(Qt.CustomContextMenu)
        self.clips.customContextMenuRequested.connect(self._menu)

        split = QSplitter()
        split.addWidget(self.videos)
        split.addWidget(self.clips)
        split.setSizes([340, 900])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addLayout(top)
        hint = QLabel("Double-click a clip to play it · right-click for more")
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        layout.addWidget(split, 1)

    def refresh(self) -> None:
        root = self._settings().output_path
        found: list[tuple[Path, dict]] = []
        if root.exists():
            for index in root.glob("**/clips.json"):
                if ".downloads" in index.parts:
                    continue
                try:
                    found.append((index.parent, json.loads(index.read_text(encoding="utf-8"))))
                except (OSError, ValueError):
                    continue
        found.sort(key=lambda fv: (fv[0] / "clips.json").stat().st_mtime, reverse=True)
        self._videos = found
        self.videos.clear()
        total = 0
        for folder, meta in found:
            v = meta.get("video", {})
            n = len(meta.get("clips", []))
            total += n
            item = QListWidgetItem(f"{v.get('title', folder.name)}\n{v.get('channel', '')} · {n} clips")
            thumb = folder / "thumbnails" / "01.jpg"
            if thumb.exists():
                item.setIcon(QIcon(QPixmap(str(thumb)).scaled(96, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            item.setSizeHint(QSize(0, 62))
            self.videos.addItem(item)
        self.videos.setIconSize(QSize(96, 54))
        self.summary.setText(f"   {len(found)} videos · {total} clips in {root}")
        if found:
            self.videos.setCurrentRow(0)
        else:
            self.clips.clear()

    def _show_video(self, row: int) -> None:
        self.clips.clear()
        if not (0 <= row < len(self._videos)):
            return
        folder, meta = self._videos[row]
        for rec in meta.get("clips", []):
            path = folder / rec["file"]
            text = (f"{rec['rank']:02d} · {rec.get('title') or 'clip'}\n"
                    f"{stamp(rec['start'])} · {rec['length']:.0f}s · score {rec.get('score', 0):.0f}")
            item = QListWidgetItem(text)
            thumb = folder / "thumbnails" / f"{rec['rank']:02d}.jpg"
            if thumb.exists():
                item.setIcon(QIcon(QPixmap(str(thumb))))
            item.setData(Qt.UserRole, str(path))
            item.setToolTip(rec.get("reason") or str(path))
            if not path.exists():
                item.setForeground(Qt.gray)
            self.clips.addItem(item)

    def _play(self, item: QListWidgetItem) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(item.data(Qt.UserRole)))

    def _reopen(self) -> None:
        row = self.videos.currentRow()
        if 0 <= row < len(self._videos):
            src = self._videos[row][1].get("source") or self._videos[row][1].get("video", {}).get("url")
            if src:
                self.reopen.emit(src)

    def _menu(self, pos) -> None:
        item = self.clips.itemAt(pos)
        if not item:
            return
        path = Path(item.data(Qt.UserRole))
        menu = QMenu(self)
        menu.addAction("Play", lambda: self._play(item))
        menu.addAction("Show in Explorer", lambda: reveal(path))
        menu.addAction("Copy file path", lambda: QApplication.clipboard().setText(str(path)))
        menu.exec(self.clips.mapToGlobal(pos))

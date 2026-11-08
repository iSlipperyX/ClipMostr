"""Library tab: every exported video and its clips, with thumbnails."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu,
                               QPushButton, QSplitter, QVBoxLayout, QWidget)

from .. import categories
from ..organize import stamp
from ..settings import Settings


def reveal(path: Path) -> None:
    """Open Explorer with the file selected."""
    try:
        subprocess.Popen(["explorer", "/select,", str(path)])
    except OSError:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


ALL = "*"  # the category filter's "every video"
STUDIO = "studio"  # videos exported from the Studio (not by automation)


class Library(QWidget):
    reopen = Signal(str)
    clean_up = Signal()  # open the Clean up window

    def __init__(self, settings: Callable[[], Settings], parent=None):
        super().__init__(parent)
        self._settings = settings
        self._all: list[tuple[Path, dict]] = []
        self._videos: list[tuple[Path, dict]] = []  # what the category filter lets through
        self._category_names: dict[str, str] = {}

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
        self.category_id = QComboBox()
        self.category_id.setToolTip("Show one category's videos")
        self.category_id.activated.connect(lambda _i: self._fill())
        clean = QPushButton("🧹  Clean up…")
        clean.setToolTip("Clear out downloads, rejected clips and clips that won't be posted. Shows what "
                         "would go first")
        clean.clicked.connect(self.clean_up.emit)
        top.addWidget(title)
        top.addWidget(self.summary, 1)
        top.addWidget(self.category_id)
        top.addWidget(self.reopen_btn)
        top.addWidget(clean)
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
        s = self._settings()
        root = s.output_path
        roots = [root] + ([s.auto_output_path] if not _inside(s.auto_output_path, root) else [])
        found: list[tuple[Path, dict, float]] = []
        for r in roots:
            if not r.exists():
                continue
            for index in r.glob("**/clips.json"):
                if ".downloads" in index.parts:
                    continue
                try:
                    meta = json.loads(index.read_text(encoding="utf-8"))
                    found.append((index.parent, meta if isinstance(meta, dict) else {}, index.stat().st_mtime))
                except (OSError, ValueError):
                    continue
        found.sort(key=lambda fv: fv[2], reverse=True)
        self._all = [(folder, meta) for folder, meta, _ in found]
        self._fill_categories(s)
        self._fill()

    def _fill_categories(self, s: Settings) -> None:
        all_categories = categories.load(s)
        self._category_names = {n.id: n.name for n in all_categories}
        keep = self.category_id.currentData()
        self.category_id.blockSignals(True)
        self.category_id.clear()
        self.category_id.addItem("Every video", ALL)
        for n in all_categories:
            self.category_id.addItem(n.name, n.id)
        self.category_id.addItem("Studio exports", STUDIO)
        self.category_id.setCurrentIndex(max(0, self.category_id.findData(keep)))
        self.category_id.blockSignals(False)
        self.category_id.setVisible(bool(all_categories))

    def _fill(self) -> None:
        root = self._settings().output_path
        want = self.category_id.currentData() if not self.category_id.isHidden() else ALL
        self._videos = [(f, m) for f, m in self._all if want in (ALL, None) or _category_key(m) == want]
        self.videos.clear()
        total = 0
        for folder, meta in self._videos:
            v = meta.get("video", {})
            n = len(meta.get("clips", []))
            total += n
            check = meta.get("qc") or {}
            count = (f"{check.get('passed', 0)} ready · {check['rejected']} rejected" if check.get("rejected")
                     else f"{n} clips")
            auto = meta.get("automation") if isinstance(meta.get("automation"), dict) else {}
            category = self._category_names.get(_category_key(meta)) or str(auto.get("category") or "")  # a deleted one: its name
            account = str(auto.get("account") or "").strip()  # the TikTok account the clips are for
            category = f"{category} ({account})" if category and account else category or account
            item = QListWidgetItem(f"{v.get('title', folder.name)}\n{v.get('channel', '')} · "
                                   + (f"{category} · " if category else "") + count)
            thumb = folder / "thumbnails" / "01.jpg"
            if thumb.exists():
                item.setIcon(QIcon(QPixmap(str(thumb)).scaled(96, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            item.setSizeHint(QSize(0, 62))
            self.videos.addItem(item)
        self.videos.setIconSize(QSize(96, 54))
        self.summary.setText(f"   {len(self._videos)} videos · {total} clips in {root}")
        if self._videos:
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
            check = rec.get("qc") or {}
            rejected = check.get("ok") is False
            text = (f"{rec['rank']:02d} · {rec.get('title') or 'clip'}\n"
                    f"{stamp(rec['start'])} · {rec['length']:.0f}s · score {rec.get('score', 0):.0f}"
                    + (f" · AI {rec['rating']}/10" if rec.get("rating") else "")
                    + (" · ✗ rejected" if rejected else " · ⚠ check" if check.get("warnings") else ""))
            item = QListWidgetItem(text)
            thumb = folder / "thumbnails" / f"{rec['rank']:02d}.jpg"
            if thumb.exists():
                item.setIcon(QIcon(QPixmap(str(thumb))))
            item.setData(Qt.UserRole, str(path))
            tip = [f"Hook: {rec['hook']}" if rec.get("hook") else "", rec.get("reason") or "",
                   "Rejected: " + "; ".join(check.get("problems", [])) if rejected else "",
                   "Warnings: " + "; ".join(check.get("warnings", [])) if check.get("warnings") else ""]
            item.setToolTip("\n".join(x for x in tip if x) or str(path))
            if rejected or not path.exists():
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


def _category_key(meta: dict) -> str:
    """The category id of an exported video, STUDIO for one exported by hand, "" for automation without a category."""
    auto = meta.get("automation")
    if not isinstance(auto, dict):
        return STUDIO
    return str(auto.get("category_id") or "")


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False

"""Sports clips: the clips the PC sends to this Mac (Categories → Send new clips to my Macs on the PC; every sports
category has it on). Each new one shows here with a notification, ready to watch (streamed from the PC, or from
the Mac once it's saved), save, open, or remove. With "Save new clips to this Mac" on (it is, to start with),
each one is downloaded by itself into a folder (~/Movies/ClipMonster/<category>), and the PC is told this Mac
has it, so it can let go of its copy once every Mac has it.

inbox.json (in the app's support folder) remembers where each clip was saved, per PC.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSplitter, QStyle,
                               QStyledItemDelegate, QVBoxLayout, QWidget)

from ..gui import theme
from . import common as c
from . import prefs
from .paths import support_dir
from .videos import Player, Thumbs, _css, _rounded

ITEM = Qt.UserRole + 1
ROW_H = 86
THUMB_W, THUMB_H = 42, 74
FILTERS = (("", "All"), ("new", "New"), ("saved", "On this Mac"))


# --- where clips are saved ----------------------------------------------------------------------------------------


def default_folder() -> Path:
    movies = Path.home() / "Movies"
    return (movies if movies.is_dir() else Path.home()) / "ClipMonster"


def save_folder() -> Path:
    chosen = str(prefs.load().get("inbox_folder") or "").strip()
    return Path(chosen).expanduser() if chosen else default_folder()


def auto_save() -> bool:
    return bool(prefs.load().get("inbox_auto_save", True))


def _safe(text: str, n: int = 80) -> str:
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(text or ""))
    t = re.sub(r"[^\w\s.,'!&()#@+-]", "", t)  # no emoji in file names: Finder copes, other apps don't
    t = " ".join(t.split()).strip(" .")
    return t[:n].rstrip(" .") or "clip"


def dest_for(item: dict, folder: Path | None = None) -> Path:
    """Where a clip goes: <folder>/<category>/<date time> <title>.mp4, numbered if that's taken."""
    base = (folder or save_folder()) / _safe(item.get("category") or "Clips", 40)
    when = time.strftime("%Y-%m-%d %H.%M", time.localtime(float(item.get("at") or time.time())))
    stem = f"{when} {_safe(item.get('title'))}"
    ext = str(item.get("ext") or "")
    ext = ext.lower() if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", ext) else ".mp4"  # (only ever an extension)
    p = base / f"{stem}{ext}"
    n = 2
    while p.exists():
        p = base / f"{stem} ({n}){ext}"
        n += 1
    return p


class Saved:
    """Which sent clips are on this Mac, and where, per PC (inbox.json)."""

    def __init__(self, path: Path | None = None):
        self.file = path or support_dir() / "inbox.json"
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.data: dict[str, dict[str, str]] = {k: v for k, v in data.items() if isinstance(v, dict)} \
            if isinstance(data, dict) else {}

    def path(self, host_id: str, item_id: str) -> Path | None:
        p = (self.data.get(host_id) or {}).get(item_id)
        return Path(p) if p and Path(p).is_file() else None

    def remember(self, host_id: str, item_id: str, path: Path) -> None:
        self.data.setdefault(host_id, {})[item_id] = str(path)
        try:
            tmp = self.file.with_name(self.file.name + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError:
            pass


def reveal(path: Path) -> None:
    """Show the file in Finder (its folder elsewhere)."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])  # noqa: S603,S607
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


# --- downloading --------------------------------------------------------------------------------------------------


class Saver(QObject):
    """Saves clips to this Mac one at a time, and tells the PC this Mac has each one."""

    progress = Signal(str, float)  # clip id, 0-1
    done = Signal(str, str)  # clip id, where it went
    failed = Signal(str, str)  # clip id, what went wrong

    def __init__(self, session_getter, saved: Saved, parent=None):
        super().__init__(parent)
        self._session = session_getter
        self.saved = saved
        self.queue: list[tuple[dict, Path | None]] = []
        self.busy = ""  # the clip being saved
        self.frac: dict[str, float] = {}
        self._stop = False
        self._told: set[str] = set()

    def host_id(self) -> str:
        s = self._session()
        return s.host.id if s is not None else ""

    def has(self, item_id: str) -> Path | None:
        return self.saved.path(self.host_id(), item_id)

    def waiting(self, item_id: str) -> bool:
        return item_id == self.busy or any(i["id"] == item_id for i, _ in self.queue)

    def want(self, item: dict, dest: Path | None = None) -> None:
        if self.has(item["id"]) and dest is None or self.waiting(item["id"]):
            return
        self.queue.append((item, dest))
        self._next()

    def stop(self) -> None:
        self._stop = True
        self.queue.clear()

    def start(self) -> None:
        self._stop = False

    def tell_got(self, item_id: str) -> None:
        """The PC hears that this Mac has it (once per run)."""
        s = self._session()
        if s is None or item_id in self._told:
            return
        self._told.add(item_id)
        s.call(lambda cl: cl.post(cl.delivery_path(item_id, "got")), None, lambda _e: self._told.discard(item_id))

    def _next(self) -> None:
        s = self._session()
        if self.busy or not self.queue or s is None or self._stop:
            return
        item, dest = self.queue.pop(0)
        target = dest or dest_for(item)
        did, host = item["id"], s.host.id
        self.busy = did
        self.frac[did] = 0.0
        self.progress.emit(did, 0.0)

        def work(cl):
            target.parent.mkdir(parents=True, exist_ok=True)
            return cl.download(cl.delivery_path(did), target,
                               lambda got, total: self.progress.emit(did, got / total if total else 0.0),
                               cancelled=lambda: self._stop)

        def ok(path):
            self.busy = ""
            self.frac.pop(did, None)
            self.saved.remember(host, did, Path(path))
            self.tell_got(did)
            self.done.emit(did, str(path))
            self._next()

        def fail(exc):
            self.busy = ""
            self.frac.pop(did, None)
            try:
                target.with_name(target.name + ".part").unlink()
            except OSError:
                pass
            self.failed.emit(did, getattr(exc, "message", str(exc)))
            self._next()

        s.call(work, ok, fail)


# --- the list -----------------------------------------------------------------------------------------------------


class _Delegate(QStyledItemDelegate):
    def __init__(self, page: "InboxPage", parent=None):
        super().__init__(parent)
        self.page = page

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), ROW_H)

    def paint(self, p: QPainter, option, index) -> None:
        v = index.data(ITEM) or {}
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(6, 3, -6, -3)
        if option.state & QStyle.State_Selected:
            p.setPen(Qt.NoPen)
            p.setBrush(_css(theme.soft(alpha=0.24 if theme.is_dark() else 0.14)))
            p.drawRoundedRect(QRectF(r), 8, 8)
        elif option.state & QStyle.State_MouseOver:
            col = QColor(theme.TEXT)
            col.setAlphaF(0.05)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(r), 8, 8)
        thumb = QRect(r.x() + 8, r.y() + (r.height() - THUMB_H) // 2, THUMB_W, THUMB_H)
        _rounded(p, self.page.thumbs.get(v), thumb)
        x = thumb.right() + 12
        right = r.right() - 10
        label, colour = self.page.chip_for(v)
        f = QFont(option.font)
        chip = QRect(right, r.y() + 12, 0, 18)
        if label:
            cf = QFont(f)
            cf.setPointSizeF(max(7.0, f.pointSizeF() * 0.82))
            cf.setWeight(QFont.DemiBold)
            p.setFont(cf)
            cw = p.fontMetrics().horizontalAdvance(label) + 16
            chip = QRect(right - cw, r.y() + 12, cw, 18)
            col = QColor(colour)
            bg = QColor(col)
            bg.setAlphaF(0.16)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(QRectF(chip), 9, 9)
            p.setPen(col)
            p.drawText(chip, Qt.AlignCenter, label)
        hf = QFont(f)
        hf.setWeight(QFont.DemiBold if v.get("new") else QFont.Normal)
        p.setFont(hf)
        p.setPen(QColor(theme.TEXT))
        hr = QRect(x, r.y() + 10, chip.left() - 8 - x, 22)
        head = f"{v.get('emoji')}  {v.get('title', '')}" if v.get("emoji") else v.get("title", "")
        p.drawText(hr, Qt.AlignLeft | Qt.AlignVCenter, p.fontMetrics().elidedText(head, Qt.ElideRight, hr.width()))
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        meta = " · ".join(b for b in (v.get("category"), c.ago(v.get("at")), v.get("source_title")) if b)
        mr = QRect(x, r.y() + 33, right - x, 20)
        p.drawText(mr, Qt.AlignLeft | Qt.AlignVCenter, p.fontMetrics().elidedText(meta, Qt.ElideRight, mr.width()))
        why = re.sub(r"^\W+\s*", "", str(v.get("reason") or ""))
        extra = " · ".join(b for b in (c.length(v.get("length")), c.size(v.get("size")), v.get("part"), why) if b)
        er = QRect(x, r.y() + 53, right - x, 18)
        p.drawText(er, Qt.AlignLeft | Qt.AlignVCenter, p.fontMetrics().elidedText(extra, Qt.ElideRight, er.width()))
        p.restore()


# --- one clip -----------------------------------------------------------------------------------------------------


class InboxDetail(QScrollArea):
    def __init__(self, page: "InboxPage", parent=None):
        super().__init__(parent)
        self.page = page
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumWidth(340)
        body = QWidget()
        body.setObjectName("page")
        self.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(16, 14, 20, 18)
        v.setSpacing(10)
        self.empty = c.WrapLabel("Pick a clip to watch it or save it.", muted=True)
        v.addWidget(self.empty)
        self.box = QWidget()
        b = QVBoxLayout(self.box)
        b.setContentsMargins(0, 0, 0, 0)
        b.setSpacing(10)
        self.player = Player()
        b.addWidget(self.player)
        top = QHBoxLayout()
        self.headline = c.WrapLabel("")
        self.headline.setStyleSheet("font-size: 13pt; font-weight: 700;")
        self.chip = c.Chip()
        top.addWidget(self.headline, 1)
        top.addWidget(self.chip, 0, Qt.AlignTop)
        b.addLayout(top)
        self.meta = c.WrapLabel("", muted=True)
        b.addWidget(self.meta)
        self.why = c.WrapLabel("")
        b.addWidget(self.why)
        self.source = c.WrapLabel("", muted=True)
        self.source.setOpenExternalLinks(True)
        b.addWidget(self.source)
        self.where = c.WrapLabel("", muted=True)
        b.addWidget(self.where)
        acts = QHBoxLayout()
        acts.setSpacing(8)
        self.save_btn = QPushButton("⬇  Save to Mac")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(lambda: self.v and self.page.save(self.v))
        self.save_as_btn = QPushButton("Save as…")
        self.save_as_btn.clicked.connect(lambda: self.v and self.page.save_as(self.v))
        self.show_btn = QPushButton("Show in Finder")
        self.show_btn.clicked.connect(lambda: self.v and self.page.reveal(self.v))
        self.open_btn = QPushButton("Open")
        self.open_btn.setToolTip("Open it in the Mac's video player (QuickTime), to trim or share it")
        self.open_btn.clicked.connect(lambda: self.v and self.page.open_local(self.v))
        for btn in (self.save_btn, self.show_btn, self.open_btn, self.save_as_btn):
            acts.addWidget(btn)
        acts.addStretch(1)
        b.addLayout(acts)
        more = QHBoxLayout()
        more.setSpacing(8)
        self.copy_btn = QPushButton("Copy link to the moment")
        self.copy_btn.setToolTip("The source video on YouTube, at this clip's start")
        self.copy_btn.clicked.connect(lambda: self.v and self.page.copy_link(self.v))
        self.remove_btn = QPushButton("Remove from inbox")
        self.remove_btn.setToolTip("Off this Mac's list (a copy you saved stays where it is)")
        self.remove_btn.clicked.connect(lambda: self.v and self.page.dismiss(self.v))
        more.addWidget(self.copy_btn)
        more.addWidget(self.remove_btn)
        more.addStretch(1)
        b.addLayout(more)
        b.addStretch(1)
        v.addWidget(self.box)
        v.addStretch(1)
        self.box.hide()
        self.v: dict | None = None

    def show_clip(self, v: dict | None, url: str = "", keep_player: bool = False) -> None:
        same = self.v is not None and v is not None and v["id"] == self.v["id"]
        self.v = v
        self.empty.setVisible(v is None)
        self.box.setVisible(v is not None)
        if v is None:
            self.player.stop()
            return
        if not (same and keep_player):
            self.player.load(url, self.page.thumbs.get(v))
        head = f"{v.get('emoji')}  {v.get('title')}" if v.get("emoji") else v.get("title")
        self.headline.setText(c.esc(head))
        label, colour = self.page.chip_for(v)
        self.chip.set(label, colour)
        self.meta.setText(c.esc(" · ".join(b for b in (v.get("sport_label") or "", v.get("category"),
                                                       c.length(v.get("length")), c.size(v.get("size")),
                                                       v.get("part"), f"sent {c.ago(v.get('at'))}") if b)))
        why = re.sub(r"^\W+\s*", "", str(v.get("reason") or ""))
        self.why.setText(f"<i>Why: {c.esc(why)}</i>" if why else "")
        self.why.setVisible(bool(why))
        src = v.get("source_title") or ""
        bits = []
        if src:
            bits.append(f"From “{c.esc(c.trim(src, 120))}”" + (f" by {c.esc(v['source_channel'])}"
                                                               if v.get("source_channel") else ""))
        if v.get("watch"):
            bits.append(f"<a href='{c.esc(v['watch'])}' style='color:{theme.ACCENT_2}'>watch it on YouTube</a>")
        self.source.setText(" · ".join(bits))
        self.source.setVisible(bool(bits))
        local = self.page.saver.has(v["id"])
        busy = self.page.saver.waiting(v["id"])
        self.where.setText(f"On this Mac: {c.esc(str(local))}" if local else "")
        self.where.setVisible(bool(local))
        self.save_btn.setVisible(not local)
        self.save_btn.setEnabled(not busy)
        self.save_btn.setText("Saving…" if busy else "⬇  Save to Mac")
        self.show_btn.setVisible(bool(local))
        self.open_btn.setVisible(bool(local))
        self.copy_btn.setVisible(bool(v.get("watch")))


# --- the page -----------------------------------------------------------------------------------------------------


class InboxPage(QWidget):
    counts = Signal(int)  # clips this Mac hasn't seen yet (the sidebar's badge)

    def __init__(self, window, parent=None, saved: Saved | None = None):
        super().__init__(parent)
        self.window_ = window
        self.thumbs = Thumbs(lambda: window.session, kind="d")
        self.thumbs.ready.connect(lambda _id: (self.list.viewport().update(), self._repicture()))
        self.saver = Saver(lambda: window.session, saved or Saved(), self)
        self.saver.progress.connect(self._progress)
        self.saver.done.connect(self._saved)
        self.saver.failed.connect(self._save_failed)
        self.items: list[dict] = []
        self.sending: list[dict] = []
        self.unsupported = False
        self.filter = ""
        self._known: set[str] | None = None  # ids already shown, to say only what's new
        self._seen_here: set[str] = set()  # watched here (a list asked for before the PC heard may still say new)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(24, 16, 24, 6)
        self.heading = c.title("Sports clips")
        head.addWidget(self.heading)
        head.addSpacing(16)
        seg = QFrame()
        seg.setObjectName("segbar")
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(3, 3, 3, 3)
        sh.setSpacing(2)
        self.seg_btns: dict[str, QPushButton] = {}
        for key, label in FILTERS:
            btn = QPushButton(label)
            btn.setObjectName("seg")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=key: self.set_filter(k))
            sh.addWidget(btn)
            self.seg_btns[key] = btn
        head.addWidget(seg)
        head.addStretch(1)
        self.count = QLabel("")
        self.count.setProperty("muted", True)
        head.addWidget(self.count)
        v.addLayout(head)
        bar = QHBoxLayout()
        bar.setContentsMargins(24, 0, 24, 8)
        bar.setSpacing(8)
        self.auto = QCheckBox("Save new clips to this Mac by themselves, in")
        self.auto.setChecked(auto_save())
        self.auto.toggled.connect(self._auto_toggled)
        self.folder_btn = QPushButton("")
        self.folder_btn.setObjectName("flat")
        self.folder_btn.setToolTip("Choose the folder (each category gets a folder in it)")
        self.folder_btn.clicked.connect(self.choose_folder)
        self.open_folder = QPushButton("Open folder")
        self.open_folder.clicked.connect(self._open_folder)
        bar.addWidget(self.auto)
        bar.addWidget(self.folder_btn)
        bar.addWidget(self.open_folder)
        bar.addStretch(1)
        self.sending_label = QLabel("")
        self.sending_label.setProperty("muted", True)
        bar.addWidget(self.sending_label)
        v.addLayout(bar)
        self._show_folder()
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(18, 0, 6, 12)
        self.list = QListWidget()
        self.list.setItemDelegate(_Delegate(self, self.list))
        self.list.setMouseTracking(True)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setUniformItemSizes(True)
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setStyleSheet("QListWidget { background: transparent; border: none; } "
                                "QListWidget::item { border: none; background: transparent; }")
        self.list.currentItemChanged.connect(lambda li, _old=None: li is not None and self._show(li.data(ITEM)))
        lv.addWidget(self.list)
        self.empty = c.WrapLabel("", muted=True)
        self.empty.setAlignment(Qt.AlignCenter)
        lv.addWidget(self.empty)
        self.detail = InboxDetail(self)
        split.addWidget(left)
        split.addWidget(self.detail)
        split.setSizes([460, 560])
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 5)
        v.addWidget(split, 1)
        self.set_filter("")

    # --- what's shown -----------------------------------------------------------------------------------------

    def chip_for(self, v: dict) -> tuple[str, str]:
        did = v.get("id", "")
        if did in self.saver.frac:
            return f"Saving {int(self.saver.frac[did] * 100)}%", theme.ACCENT_2
        if self.saver.waiting(did):
            return "Waiting to save", theme.MUTED
        if self.saver.has(did):
            return "On this Mac", theme.GOOD
        if v.get("new"):
            return "New", theme.ACCENT
        return "", theme.MUTED

    def _show_folder(self) -> None:
        folder = save_folder()
        home = str(Path.home())
        text = str(folder)
        self.folder_btn.setText("~" + text[len(home):] if text.startswith(home) else text)

    def show_deliveries(self, data: dict) -> None:
        """The PC's list for this Mac (session.deliveries)."""
        if data.get("unsupported"):
            self.unsupported, self.items, self.sending = True, [], []
            self._fill()
            return
        self.unsupported = False
        self.items = list(data.get("items") or [])
        for v in self.items:
            if v["id"] in self._seen_here:
                v["new"] = False
        self.sending = list(data.get("sending") or [])
        sporty = not self.sending or all(s.get("sport") for s in self.sending)
        self.heading.setText("Sports clips" if sporty else "Clips sent to this Mac")
        names = [f"{s.get('emoji')} {s['name']}".strip() for s in self.sending]
        self.sending_label.setText(("From " + ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3
                                                                       else "")) if names else "")
        self._announce()
        for v in self.items:
            if self.saver.has(v["id"]):
                if not v.get("got"):
                    self.saver.tell_got(v["id"])  # saved, but the PC didn't hear it
                v["new"] = False
            elif self.auto.isChecked() and not v.get("got"):
                self.saver.want(v)
        self.counts.emit(sum(1 for v in self.items if v.get("new")))
        self._fill()

    def _announce(self) -> None:
        news = [v for v in self.items if v.get("new") and not self.saver.has(v["id"])]
        if self._known is None:  # the first list since connecting: what came while the app was closed
            if news:
                self.window_.notify("Sports clips" if len(news) > 1 else self._title(news[0]),
                                    f"{len(news)} new clips are waiting." if len(news) > 1 else c.trim(
                                        news[0].get("title"), 120))
        else:
            fresh = [v for v in news if v["id"] not in self._known]
            if len(fresh) == 1:
                self.window_.notify(self._title(fresh[0]), c.trim(fresh[0].get("title"), 120))
            elif fresh:
                kinds = {v.get("sport_label") for v in fresh}
                what = f"{kinds.pop()} clips" if len(kinds) == 1 and None not in kinds and "" not in kinds else "clips"
                self.window_.notify(f"{len(fresh)} new {what}", c.trim(fresh[0].get("source_title") or "", 120))
        self._known = {v["id"] for v in self.items}

    @staticmethod
    def _title(v: dict) -> str:
        what = f"New {v['sport_label']} clip" if v.get("sport_label") else "New clip"
        return f"{v.get('emoji')} {what}".strip()

    def set_filter(self, key: str) -> None:
        self.filter = key
        for k, btn in self.seg_btns.items():
            btn.setChecked(k == key)
        self._fill()

    def _matches(self, v: dict) -> bool:
        if self.filter == "new":
            return bool(v.get("new"))
        if self.filter == "saved":
            return self.saver.has(v["id"]) is not None
        return True

    def _fill(self) -> None:
        cur = self.list.currentItem()
        cur_id = (cur.data(ITEM) or {}).get("id") if cur is not None else (self.detail.v or {}).get("id")
        shown = [v for v in self.items if self._matches(v)]
        n_new = sum(1 for v in self.items if v.get("new"))
        n_saved = sum(1 for v in self.items if self.saver.has(v["id"]))
        for k, label in FILTERS:
            num = {"new": n_new, "saved": n_saved}.get(k, 0)
            self.seg_btns[k].setText(f"{label}  {num}" if num else label)
        self.count.setText(c.plural(len(self.items), "clip"))
        self.list.blockSignals(True)
        self.list.clear()
        pick = None
        for v in shown:
            li = QListWidgetItem()
            li.setData(ITEM, v)
            li.setSizeHint(QSize(100, ROW_H))
            self.list.addItem(li)
            if v["id"] == cur_id:
                pick = li
        self.list.blockSignals(False)
        self.empty.setVisible(not shown)
        self.list.setVisible(bool(shown))
        if not shown:
            self.empty.setText(self._empty_text())
        if pick is not None:
            self.list.blockSignals(True)
            self.list.setCurrentItem(pick)
            self.list.blockSignals(False)
            self._show(pick.data(ITEM), keep_player=True)
        elif shown:
            self.list.setCurrentRow(0)
        else:
            self.detail.show_clip(None)

    def _empty_text(self) -> str:
        if self.unsupported:
            return "The PC runs an older ClipMonster that can't send clips to the Mac yet: update it to 3.6 or newer."
        if self.filter:
            return "No clips like that right now."
        if not self.sending:
            return ("No category sends its clips here yet. On the PC: Categories → pick one → Sport (a sports "
                    "category sends them by itself), or tick Send new clips to my Macs.")
        return ("No clips yet. Each new clip from " + ", ".join(s["name"] for s in self.sending[:3])
                + " lands here as soon as it's made, with a notification.")

    def _show(self, v: dict, keep_player: bool = False) -> None:
        local = self.saver.has(v["id"])
        proxy = self.window_.proxy
        if local is not None:
            url = QUrl.fromLocalFile(str(local)).toString()
        else:
            url = proxy.url(v["id"], kind="d") if proxy is not None else ""
        self.detail.show_clip(v, url, keep_player=keep_player)
        if self.isVisible():
            self._mark_seen(v)

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        if self.detail.v is not None:
            self._mark_seen(self.detail.v)

    def _update(self, did: str, **changes) -> None:
        """Change a clip in the list (each row keeps its own copy of the clip)."""
        for v in self.items:
            if v["id"] == did:
                v.update(changes)
        for row in range(self.list.count()):
            li = self.list.item(row)
            d = li.data(ITEM) or {}
            if d.get("id") == did:
                li.setData(ITEM, {**d, **changes})
        if self.detail.v is not None and self.detail.v["id"] == did:
            self.detail.v.update(changes)

    def _mark_seen(self, v: dict) -> None:
        if v.get("new"):  # seen now
            self._seen_here.add(v["id"])
            self._update(v["id"], new=False)
            v["new"] = False
            s = self.window_.session
            if s is not None:
                s.call(lambda cl, did=v["id"]: cl.post(cl.delivery_path(did, "seen")), None, lambda _e: None)
            self.counts.emit(sum(1 for i in self.items if i.get("new")))
            self.list.viewport().update()

    def _repicture(self) -> None:
        v = self.detail.v
        if v is not None and self.detail.player.stack.currentWidget() is self.detail.player.picture:
            self.detail.player.show_picture(self.thumbs.get(v))

    def _refresh_row(self, did: str) -> None:
        self.list.viewport().update()
        if self.detail.v is not None and self.detail.v["id"] == did:
            self._show(self.detail.v, keep_player=True)

    # --- saving -----------------------------------------------------------------------------------------------

    def _progress(self, did: str, frac: float) -> None:
        self.saver.frac[did] = frac
        self.list.viewport().update()
        if self.detail.v is not None and self.detail.v["id"] == did:
            self.detail.chip.set(*self.chip_for(self.detail.v))

    def _saved(self, did: str, path: str) -> None:
        self._update(did, new=False, got=True)
        self.window_.notice(f"Saved {Path(path).name}", good=True)
        self.counts.emit(sum(1 for v in self.items if v.get("new")))
        self._fill()

    def _save_failed(self, did: str, message: str) -> None:
        self.window_.notice(f"Couldn't save the clip: {message}", bad=True)
        self._refresh_row(did)

    def save(self, v: dict) -> None:
        self.saver.want(v)
        self._refresh_row(v["id"])

    def save_as(self, v: dict) -> None:
        start = str(dest_for(v))
        path, _ = QFileDialog.getSaveFileName(self, "Save the clip", start, "Videos (*.mp4 *.mov)")
        if path:
            self.saver.want(v, Path(path))
            self._refresh_row(v["id"])

    def reveal(self, v: dict) -> None:
        p = self.saver.has(v["id"])
        if p is not None:
            reveal(p)

    def open_local(self, v: dict) -> None:
        p = self.saver.has(v["id"])
        if p is not None:
            self.detail.player.stop()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def copy_link(self, v: dict) -> None:
        QGuiApplication.clipboard().setText(v.get("watch") or "")
        self.window_.notice("Copied the link to the moment.", good=True)

    def dismiss(self, v: dict) -> None:
        s = self.window_.session
        if s is None:
            return
        if self.saver.has(v["id"]) is None:
            ask = QMessageBox(QMessageBox.Question, "Remove it from the inbox?",
                              "It isn't saved on this Mac. Once it's off the list, it can't be watched or saved "
                              "here any more.", QMessageBox.Cancel, self)
            go = ask.addButton("Remove", QMessageBox.DestructiveRole)
            ask.exec()
            if ask.clickedButton() is not go:
                return
        did = v["id"]

        def ok(_out):
            self.items = [i for i in self.items if i["id"] != did]
            if self.detail.v is not None and self.detail.v["id"] == did:
                self.detail.show_clip(None)
            self.counts.emit(sum(1 for i in self.items if i.get("new")))
            self._fill()
            self.window_.notice("Removed from the inbox.", good=True)

        s.call(lambda cl: cl.post(cl.delivery_path(did, "dismiss")), ok,
               lambda e: self.window_.notice(getattr(e, "message", str(e)), bad=True))

    def _auto_toggled(self, on: bool) -> None:
        prefs.set("inbox_auto_save", bool(on))
        if on:
            for v in self.items:
                if not v.get("got") and self.saver.has(v["id"]) is None:
                    self.saver.want(v)
            self._fill()

    def choose_folder(self) -> None:
        folder = save_folder()
        got = QFileDialog.getExistingDirectory(self, "Save clips in", str(folder if folder.is_dir() else Path.home()))
        if got:
            prefs.set("inbox_folder", got)
            self._show_folder()

    def _open_folder(self) -> None:
        folder = save_folder()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # --- the window -------------------------------------------------------------------------------------------

    def connected(self) -> None:
        """A new connection (another PC, or the same one again): say only what's new from here."""
        self._known = None
        self._seen_here = set()
        self.items = []
        self.saver.start()
        self._fill()

    def stop(self) -> None:
        self.detail.player.stop()

    def disconnect_(self) -> None:
        self.stop()
        self.saver.stop()

    def load(self) -> None:
        s = self.window_.session
        if s is not None:
            s.refresh_deliveries(force=True)

"""Clean up window: shows what would go and how much space it frees, then clears what you tick.

The look happens on a worker thread (a big output folder takes a moment to read). Every kind is a
row you can open to see, tick and untick single files; a file you tick or untick keeps that when the
window looks again. Nothing is removed until Clean up is pressed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .. import cleanup
from ..settings import APP_NAME, Settings
from . import theme

HELP = {
    "partial": "Downloads that stopped part way and haven't been touched for half an hour",
    "previews": "Clips made by Render preview and the Layout tab's preview, in ClipMonster's work folder",
    "downloads": "Full videos kept after clipping. Clipping one again downloads it again",
    "analysis": "Transcripts, captions and measurements. A video opened again is measured again without them",
    "rejected": "Clips the quality check moved to a _rejected folder. TikTok Manager never takes these",
    "posted": "Clips TikTok Manager says you've posted. Its post history and stats stay",
    "removed": "Clips you removed from TikTok Manager's library. It never brings them back",
    "uploaded": "Clips that are safely on your storage server (Settings → Upload), unless TikTok Manager still "
                "needs them. The uploaded copies stay",
}
ON_BY_DEFAULT = ("partial", "previews", "rejected", "posted", "removed", "uploaded")
ITEM_ROLE = Qt.UserRole + 1  # a file row's index in the plan


class _Look:
    """One look, on a plain background thread that never touches Qt (the window checks on it), so
    closing the window or quitting while it runs just leaves it behind. A QThread still running
    when the app closes would bring the whole app down."""

    def __init__(self, fn: Callable[[], cleanup.Plan]):
        self.plan: cleanup.Plan | None = None
        self.error = ""
        self._thread = threading.Thread(target=self._run, args=(fn,), name="Clean up look", daemon=True)
        self._thread.start()

    def _run(self, fn: Callable[[], cleanup.Plan]) -> None:
        try:
            self.plan = fn()
        except Exception as exc:  # shown in the window
            self.error = str(exc) or exc.__class__.__name__

    @property
    def running(self) -> bool:
        return self._thread.is_alive()


def move_to_trash(path: Path) -> None:
    from PySide6.QtCore import QFile

    ok = QFile.moveToTrash(str(path))
    if isinstance(ok, tuple):  # some bindings also return the new path
        ok = ok[0]
    if not ok:
        raise OSError("couldn't move it to the Recycle Bin")


class CleanupDialog(QDialog):
    cleaned = Signal(str)  # a line for the status bar after a clean-up

    def __init__(self, settings: Callable[[], Settings], change: Callable[[str, Any], None],
                 busy: Callable[[], set[str]], parent=None, look: Callable[..., cleanup.Plan] = cleanup.plan):
        super().__init__(parent)
        self.setWindowTitle("Clean up")
        self.resize(760, 600)
        self._settings = settings
        self._change = change
        self._busy = busy
        self._look_fn = look
        self.plan: cleanup.Plan | None = None
        self._job: _Look | None = None
        self._again = False
        self._closing = False
        self.qs = QSettings(APP_NAME, APP_NAME)
        s = settings()
        chosen = set(cleanup.parse_kinds(s.cleanup_kinds)) if s.cleanup_auto else set(ON_BY_DEFAULT)
        self._chosen = chosen or set(ON_BY_DEFAULT)
        self._picked: dict[str, bool] = {}  # files you ticked or unticked, by path: a new look keeps that
        self._recount = QTimer(self, singleShot=True, interval=0)  # one recount for a whole kind's ticks
        self._recount.timeout.connect(self._update_button)
        self._poll = QTimer(self, interval=50)  # is the look done?
        self._poll.timeout.connect(self._check_look)

        layout = QVBoxLayout(self)
        intro = QLabel("Clears out what nothing needs any more. Clips TikTok Manager still has waiting, and "
                       "anything for a video that's queued, being clipped or open in the Studio, always stay. "
                       "Nothing goes until you press Clean up.")
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        layout.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Only things older than"))
        self.days = QSpinBox()
        self.days.setRange(0, 365)
        self.days.setSuffix(" days")
        self.days.setSpecialValueText("any age")
        self.days.setValue(s.cleanup_days if s.cleanup_auto else 0)
        self.days.setToolTip("Posted clips count from when you posted them; everything else from when it was made")
        self._debounce = QTimer(self, singleShot=True, interval=500)
        self._debounce.timeout.connect(self.look)
        self.days.valueChanged.connect(lambda _v: self._debounce.start())
        row.addWidget(self.days)
        row.addStretch(1)
        self.status = QLabel()
        self.status.setProperty("muted", True)
        row.addWidget(self.status)
        layout.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setObjectName("panel")
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(("What", "How many", "Space"))
        head = self.tree.header()
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        head.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        head.setStretchLastSection(False)
        self.tree.itemChanged.connect(self._ticked)
        layout.addWidget(self.tree, 1)

        self.notes = QLabel()
        self.notes.setWordWrap(True)
        self.notes.setTextFormat(Qt.RichText)
        layout.addWidget(self.notes)

        self.trash = QCheckBox("Move to the Recycle Bin instead of deleting for good")
        self.trash.setToolTip("You can get things back from the Recycle Bin, but the space is only freed once "
                              "it's emptied")
        self.trash.setChecked(self.qs.value("cleanup/trash", False, type=bool))
        self.auto = QCheckBox("Do this by itself once a day while automation runs (the ticked kinds, at this age)")
        self.auto.setChecked(s.cleanup_auto)
        self.auto.setToolTip("Settings → Automation → Clean up by itself once a day. It clears whole kinds: single "
                             "files you untick here aren't remembered for it. It never clears anything under a "
                             "day old.")
        self.auto.toggled.connect(self._auto_toggled)
        layout.addWidget(self.trash)
        layout.addWidget(self.auto)

        buttons = QHBoxLayout()
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.RichText)
        self.clean_btn = QPushButton("Clean up")
        self.clean_btn.setObjectName("primary")
        self.clean_btn.clicked.connect(self.clean)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(self.result, 1)
        buttons.addWidget(close)
        buttons.addWidget(self.clean_btn)
        layout.addLayout(buttons)
        self.look()

    # ================================================================== looking
    def look(self) -> None:
        if self._closing:
            return
        if self._job:
            self._again = True
            return
        s, days, busy = self._settings(), self.days.value(), set(self._busy())
        now = time.time()
        self.status.setText("Looking…")
        self.clean_btn.setEnabled(False)
        self._job = _Look(lambda: self._look_fn(s, now, busy=busy, older_than_days=days))
        self._poll.start()

    def _check_look(self) -> None:
        job = self._job
        if job is None or job.running:
            return
        self._poll.stop()
        self._job = None
        if job.error:
            self.status.setText(f"Couldn't look: {job.error}")
        elif job.plan is not None:
            self._show(job.plan)
        if self._again:
            self._again = False
            self.look()
        else:
            self._update_button()

    def _show(self, plan: cleanup.Plan) -> None:
        self.plan = plan
        self.tree.blockSignals(True)
        self.tree.clear()
        totals = plan.totals()
        for kind in cleanup.KINDS:
            n, size = totals[kind]
            if kind in ("posted", "removed") and not plan.tm_found:
                continue
            top = QTreeWidgetItem([cleanup.LABELS[kind], str(n) if n else "–", cleanup.size_text(size) if n else ""])
            top.setData(0, Qt.UserRole, kind)
            top.setToolTip(0, HELP.get(kind, ""))
            top.setFlags(top.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            top.setCheckState(0, Qt.Checked if kind in self._chosen else Qt.Unchecked)
            top.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            top.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
            if not n:
                for c in range(3):
                    top.setForeground(c, QColor(theme.MUTED))
            for index, it in ((i, x) for i, x in enumerate(plan.items) if x.kind == kind):
                child = QTreeWidgetItem([_describe(it), "", cleanup.size_text(it.size)])
                child.setData(0, ITEM_ROLE, index)
                child.setToolTip(0, str(it.path))
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                ticked = self._picked.get(str(it.path), kind in self._chosen)
                child.setCheckState(0, Qt.Checked if ticked else Qt.Unchecked)
                child.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
                top.addChild(child)
            self.tree.addTopLevelItem(top)
        self.tree.blockSignals(False)
        notes = [f"<span style='color:{theme.WARN}'>⚠ {_esc(x)}</span>" for x in plan.notes]
        if plan.kept_for_tm:
            notes.append(f"<span style='color:{theme.MUTED}'>{plan.kept_for_tm} clip"
                         f"{'s stay' if plan.kept_for_tm != 1 else ' stays'} because TikTok Manager still has "
                         f"{'them' if plan.kept_for_tm != 1 else 'it'}.</span>")
        self.notes.setText("<br>".join(notes))
        self.notes.setVisible(bool(notes))
        self.status.setText("")
        self._update_button()

    def _ticked(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        if item.parent() is None:  # a whole kind; ticking some of its files doesn't tick the files to come
            kind, state = item.data(0, Qt.UserRole), item.checkState(0)
            if state == Qt.Checked:
                self._chosen.add(kind)
            elif state == Qt.Unchecked:
                self._chosen.discard(kind)
        else:
            index = item.data(0, ITEM_ROLE)
            if self.plan and isinstance(index, int) and 0 <= index < len(self.plan.items):
                self._picked[str(self.plan.items[index].path)] = item.checkState(0) == Qt.Checked
        self._recount.start()

    def selected(self) -> list[cleanup.Item]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                index = child.data(0, ITEM_ROLE)
                if child.checkState(0) == Qt.Checked and isinstance(index, int) and self.plan:
                    out.append(self.plan.items[index])
        return out

    def _update_button(self) -> None:
        chosen = self.selected()
        size = sum(it.size for it in chosen)
        self.clean_btn.setEnabled(bool(chosen) and self._job is None)
        self.clean_btn.setText(f"Clean up {len(chosen)} item{'s' if len(chosen) != 1 else ''} "
                               f"({cleanup.size_text(size)})" if chosen else "Clean up")

    # ================================================================== cleaning
    def clean(self) -> None:
        if not self.plan:
            return
        chosen = self.selected()
        if not chosen:
            return
        kinds = sorted({it.kind for it in chosen})
        self.qs.setValue("cleanup/trash", self.trash.isChecked())
        self._save_auto()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            plan = cleanup.Plan(items=chosen, roots=self.plan.roots)
            result = cleanup.apply(plan, kinds, remove=move_to_trash if self.trash.isChecked() else None)
        finally:
            QApplication.restoreOverrideCursor()
        summary = result.summary(trashed=self.trash.isChecked())
        text = f"✓ Cleared {_esc(summary)}."
        if result.failed:
            first = result.failed[0]
            text += (f"<br><span style='color:{theme.WARN}'>⚠ {len(result.failed)} couldn't be removed, e.g. "
                     f"{_esc(Path(first[0]).name)}: {_esc(first[1])}. A file that's open somewhere (a player, "
                     f"TikTok Manager) stays until it's closed.</span>")
        self.result.setText(text)
        self.cleaned.emit(f"Cleared {summary}.")
        self.look()

    def _auto_toggled(self, on: bool) -> None:
        if on and self.days.value() == 0:  # the daily run keeps at least a day's grace
            self.days.setValue(max(1, self._settings().cleanup_days))

    def _save_auto(self) -> None:
        s = self._settings()
        on = self.auto.isChecked()
        if on != s.cleanup_auto:
            self._change("cleanup_auto", on)
        if on:
            kinds = ", ".join(k for k in cleanup.KINDS if self._kind_ticked(k))
            if kinds != s.cleanup_kinds:
                self._change("cleanup_kinds", kinds)
            days = max(1, self.days.value())
            if days != s.cleanup_days:
                self._change("cleanup_days", days)

    def _kind_ticked(self, kind: str) -> bool:
        """Whether a kind's row shows ticked (all or some of its files), as the daily run will take it."""
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.data(0, Qt.UserRole) == kind:
                return top.checkState(0) != Qt.Unchecked
        return kind in self._chosen  # not shown (TikTok Manager isn't on this PC)

    def done(self, r: int) -> None:
        self._save_auto()
        super().done(r)

    def dispose(self) -> None:
        """Delete the window. A look still running finishes on its own and is dropped."""
        self._closing = True
        self._debounce.stop()
        self._poll.stop()
        self._again = False
        self._job = None
        self.deleteLater()


def _describe(it: cleanup.Item) -> str:
    if it.folder is not None:
        return f"{it.title}  ·  {it.folder.name}"
    return it.path.name


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

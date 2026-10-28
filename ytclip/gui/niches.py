"""Niches tab: one TikTok account's topic each, with its own channels, searches, look and folder.

The list on the left shows every niche and how it's doing today; the form on the right edits the
selected one. Every change is saved straight away (Settings.niches), and automation and the
Research tab pick it up on their next look.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from .. import niches
from ..jobs import JobDB
from ..niches import MAX_CLIPS, MAX_DAILY, MAX_LEN, Niche
from ..policy import PickLog, picks_path
from ..research import youtube_key
from ..settings import PRESETS, Settings, all_layouts, all_presets
from ..trending import CATEGORIES
from . import theme

STATS_EVERY = 5000  # ms between refreshes of the counts while the tab is showing
CHANNELS_HINT = "@somepodcast  # clipping programme, credit in caption\nhttps://www.youtube.com/@streamer"


class Niches(QWidget):
    notice = Signal(str)
    changed = Signal()  # niches were edited (other tabs re-sort their videos)
    open_settings = Signal(str)

    def __init__(self, settings: Callable[[], Settings], change: Callable[[str, Any], None],
                 db: JobDB | None = None, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._change = change
        self.db = db or JobDB()
        self.picks = PickLog(picks_path())
        self._niches: list[Niche] = niches.load(settings())
        self._current = ""  # id of the niche in the form
        self._filling = False
        self._changed_timer = QTimer(self, singleShot=True, interval=700)
        self._changed_timer.timeout.connect(self.changed.emit)
        self._build()
        self.timer = QTimer(self, interval=STATS_EVERY)
        self.timer.timeout.connect(self._refresh_stats)
        self.reload()

    # ================================================================== UI
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("Niches")
        title.setObjectName("title")
        self.summary = QLabel()
        self.summary.setProperty("muted", True)
        self.summary.setTextFormat(Qt.RichText)
        self.add_btn = QPushButton("＋  New niche")
        self.add_btn.setObjectName("primary")
        self.add_btn.clicked.connect(self.add)
        open_dir = QPushButton("Open niche folders")
        open_dir.setToolTip("The Automation folder, with a folder for each niche")
        open_dir.clicked.connect(lambda: self._open_dir(self._settings().auto_output_path))
        top.addWidget(title)
        top.addWidget(self.summary, 1)
        top.addWidget(open_dir)
        top.addWidget(self.add_btn)
        layout.addLayout(top)

        self.list = QListWidget()
        self.list.setObjectName("panel")
        self.list.setSpacing(2)
        self.list.currentRowChanged.connect(self._select)

        self.form_area = QScrollArea()
        self.form_area.setWidgetResizable(True)
        self.form_area.setWidget(self._build_form())

        self.split = QSplitter()
        self.split.addWidget(self.list)
        self.split.addWidget(self.form_area)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([360, 820])
        layout.addWidget(self.split, 1)

        self.empty = QFrame()
        self.empty.setObjectName("panel")
        ev = QVBoxLayout(self.empty)
        ev.setContentsMargins(30, 30, 30, 30)
        ev.addStretch(1)
        self.empty_text = QLabel()
        self.empty_text.setWordWrap(True)
        self.empty_text.setTextFormat(Qt.RichText)
        self.empty_text.setAlignment(Qt.AlignCenter)
        first = QPushButton("＋  Create my first niche")
        first.setObjectName("primary")
        first.clicked.connect(self.add)
        ev.addWidget(self.empty_text)
        ev.addSpacing(12)
        ev.addWidget(first, 0, Qt.AlignCenter)
        ev.addStretch(1)
        layout.addWidget(self.empty, 1)

        self.warnings = QLabel()
        self.warnings.setWordWrap(True)
        self.warnings.setTextFormat(Qt.RichText)
        self.warnings.linkActivated.connect(self._link)
        layout.addWidget(self.warnings)

        self.hint = QLabel()
        self.hint.setProperty("muted", True)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

    def _build_form(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(9)
        form.setContentsMargins(12, 6, 12, 6)

        def section(text: str) -> None:
            label = QLabel(text)
            label.setObjectName("h2")
            form.addRow(label)

        def note(text: str) -> QLabel:
            label = QLabel(text)
            label.setProperty("muted", True)
            label.setWordWrap(True)
            form.addRow(label)
            return label

        section("The account")
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Gaming, Podcasts, Science")
        self.name.editingFinished.connect(self._rename)
        form.addRow("Name", self.name)
        self.account = QLineEdit()
        self.account.setPlaceholderText("@youraccount")
        self.account.setToolTip("The TikTok account these clips are for. It's saved with every clip (clips.json) "
                                "and shown in the Library. TikTok Manager doesn't queue clips by it yet: check "
                                "which account it queues them for")
        self.account.textChanged.connect(lambda v: self._set("account", v.strip()))
        form.addRow("TikTok account", self.account)
        self.enabled = QCheckBox("Pick videos for this niche")
        self.enabled.setToolTip("Off: nothing new is picked for it. Videos already waiting are still clipped")
        self.enabled.toggled.connect(lambda v: self._set("enabled", bool(v)))
        form.addRow("", self.enabled)
        self.daily = QSpinBox()
        self.daily.setRange(1, MAX_DAILY)
        self.daily.setSuffix(" a day")
        self.daily.setToolTip("Videos picked for this niche in 24 hours. Each video makes several clips")
        self.daily.valueChanged.connect(lambda v: self._set("daily", int(v)))
        form.addRow("Videos", self.daily)

        section("Where its videos come from")
        self.channels = QPlainTextEdit()
        self.channels.setFixedHeight(110)
        self.channels.setPlaceholderText(CHANNELS_HINT)
        self.channels.setToolTip("One channel per line: @handle, channel link or channel id. Add a note after #. "
                                 "Each channel costs about 1 quota unit an hour")
        self.channels.textChanged.connect(lambda: self._set("channels", self.channels.toPlainText()))
        form.addRow("Channels", self.channels)
        self.searches = QPlainTextEdit()
        self.searches.setFixedHeight(70)
        self.searches.setPlaceholderText("gaming highlights\nminecraft funny moments")
        self.searches.setToolTip("Creative Commons videos for these searches. Each search costs 100 quota units "
                                 "and runs at most every 6 hours")
        self.searches.textChanged.connect(lambda: self._set("searches", self.searches.toPlainText()))
        form.addRow("Creative Commons searches", self.searches)
        self.keywords = QLineEdit()
        self.keywords.setPlaceholderText("minecraft, fortnite, speedrun")
        self.keywords.setToolTip("Comma separated. A trending or Creative Commons video with one of these in its "
                                 "title or channel name fits this niche")
        self.keywords.textChanged.connect(lambda v: self._set("keywords", v))
        form.addRow("Title words", self.keywords)
        self.categories = QListWidget()
        self.categories.setFixedHeight(120)
        for cid, name in CATEGORIES.items():
            if cid:
                it = QListWidgetItem(name)
                it.setData(Qt.UserRole, cid)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Unchecked)
                self.categories.addItem(it)
        self.categories.setToolTip("Trending or Creative Commons videos in these YouTube categories fit this niche")
        self.categories.itemChanged.connect(lambda _it: self._set("categories", self._checked_categories()))
        form.addRow("Categories", self.categories)
        self.sources_note = note("")

        section("How its clips look")
        self.preset = QComboBox()
        self.preset.setToolTip("How clips are found and exported for this niche")
        self.preset.activated.connect(lambda _i: self._set("preset", self.preset.currentData()))
        form.addRow("Style (preset)", self.preset)
        self.view = QComboBox()
        self.view.activated.connect(lambda _i: self._set("view", self.view.currentData()))
        form.addRow("Top/bottom view", self.view)
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        self.stack_folder = QLineEdit()
        self.stack_folder.setPlaceholderText("As in Settings → Layout")
        self.stack_folder.setToolTip("A folder of second videos (gameplay, satisfying clips…) for stacked views. "
                                     "Each clip gets a different one")
        self.stack_folder.editingFinished.connect(lambda: self._set("stack_folder", self.stack_folder.text().strip()))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_stack)
        row.addWidget(self.stack_folder, 1)
        row.addWidget(browse)
        form.addRow("Second videos", box)
        self.per_video = QSpinBox()
        self.per_video.setRange(0, MAX_CLIPS)
        self.per_video.setSpecialValueText("As the style says")
        self.per_video.setSuffix(" clips")
        self.per_video.setToolTip("How many clips each of this niche's videos gives")
        self.per_video.valueChanged.connect(lambda v: self._set("clips_per_video", int(v)))
        form.addRow("Clips per video", self.per_video)
        lengths = QWidget()
        lrow = QHBoxLayout(lengths)
        lrow.setContentsMargins(0, 0, 0, 0)
        self.min_len = QDoubleSpinBox()
        self.max_len = QDoubleSpinBox()
        for box_, key in ((self.min_len, "min_len"), (self.max_len, "max_len")):
            box_.setRange(0, MAX_LEN)
            box_.setDecimals(0)
            box_.setSingleStep(5)
            box_.setSuffix(" s")
            box_.setSpecialValueText("Style's")
            box_.valueChanged.connect(lambda v, key=key: self._set(key, float(v)))
        lrow.addWidget(self.min_len)
        lrow.addWidget(QLabel("to"))
        lrow.addWidget(self.max_len)
        lrow.addStretch(1)
        lengths.setToolTip("How long this niche's clips are, shortest to longest. Leave at Style's to use the style's")
        form.addRow("Clip length", lengths)
        self.instructions = QPlainTextEdit()
        self.instructions.setFixedHeight(70)
        self.instructions.setPlaceholderText("e.g. clutch plays and funny fails, skip menus and loading screens, "
                                             "no swearing in the hook")
        self.instructions.setToolTip("Plain words for the AI. With an Anthropic key, the AI picks this niche's "
                                     "clips so it can follow them, and it writes the hooks and bottom text to "
                                     "suit. Each video then counts towards AI calls per day")
        self.instructions.textChanged.connect(lambda: self._set("instructions", self.instructions.toPlainText()))
        form.addRow("Special requests", self.instructions)
        note("A recipe in Settings → Automation for a channel or category still wins over the niche's style.")

        buttons = QHBoxLayout()
        self.folder_btn = QPushButton("Open its folder")
        self.folder_btn.clicked.connect(lambda: self._open_dir(self._folder_of(self._niche())))
        self.delete_btn = QPushButton("Delete niche")
        self.delete_btn.clicked.connect(self.delete)
        buttons.addWidget(self.folder_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.delete_btn)
        form.addRow(buttons)
        return page

    # ================================================================== state
    def reload(self) -> None:
        """Show the niches as they are in the settings (after the Settings dialog, too)."""
        self._niches = niches.load(self._settings())
        if not any(n.id == self._current for n in self._niches):
            self._current = self._niches[0].id if self._niches else ""
        self._fill_choices()
        self._fill_list()
        self._fill_form()
        self._update_notes()

    def _niche(self) -> Niche | None:
        return next((n for n in self._niches if n.id == self._current), None)

    def _folder_of(self, n: Niche | None) -> Path:
        root = self._settings().auto_output_path
        return root / n.folder if n else root

    def _fill_choices(self) -> None:
        self.preset.clear()
        self.preset.addItem("Automation's style", "")
        for name in all_presets():
            self.preset.addItem(name + ("" if name in PRESETS else "  (yours)"), name)
        self.view.clear()
        self.view.addItem("Automation's view", "")
        for name in all_layouts():
            self.view.addItem(name, name)

    def _fill_list(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for n in self._niches:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, n.id)
            it.setSizeHint(QSize(0, 66))
            self.list.addItem(it)
        row = next((i for i, n in enumerate(self._niches) if n.id == self._current), -1)
        self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        self._refresh_stats()
        has = bool(self._niches)
        self.split.setVisible(has)
        self.empty.setVisible(not has)
        s = self._settings()
        moving = bool(s.pick_channels.strip() or s.pick_searches.strip())
        self.empty_text.setText(
            "<p style='font-size:13pt'><b>Sort your clips by niche, one TikTok account each</b></p>"
            "<p>A niche is the topic of one TikTok account: the channels and Creative Commons searches its "
            "videos come from, how many videos a day it gets, and how its clips look. Automation picks videos "
            "for every niche in turn and puts each niche's clips in a folder of its own.</p>"
            + ("<p>Your channel list and Creative Commons searches (Settings → Picking) move into the first "
               "niche, so nothing is lost.</p>" if moving else ""))

    def _refresh_stats(self) -> None:
        """Each niche's line in the list: account, sources, and how today is going."""
        if not self._niches:
            self.summary.setText("")
            return
        now = time.time()
        self.picks.reload()
        try:
            counts = self.db.niche_counts(now - 7 * 86400)
        except Exception:  # the runner has jobs.db busy; try again next time
            counts = {}
        total_today = 0
        for i, n in enumerate(self._niches):
            it = self.list.item(i)
            if it is None:
                continue
            today = self.picks.count_since(now - 86400, niche=n.id)
            total_today += today
            c = counts.get(n.id, {})
            refs, _ = n.channel_refs()
            sources = [f"{len(refs)} channel{'s' if len(refs) != 1 else ''}"] if refs else []
            if n.search_list():
                sources.append(f"{len(n.search_list())} search{'es' if len(n.search_list()) != 1 else ''}")
            if n.keyword_list() or n.category_ids():
                sources.append("trending")
            line2 = " · ".join([n.account or "no account set"] + (sources or ["no sources yet"]))
            line3 = (f"Today {today}/{n.daily} · {c.get('waiting', 0)} waiting · {c.get('clips', 0)} clips this week"
                     if n.on else "Off: nothing new is picked" if not n.enabled else
                     "Paused in Automation: nothing is picked or clipped")
            it.setText(f"{n.name}\n{line2}\n{line3}")
            it.setForeground(QColor(theme.TEXT if n.on else theme.MUTED))
            it.setToolTip(str(self._folder_of(n)))
        on = [n for n in self._niches if n.enabled]
        self.summary.setText(f"   {len(self._niches)} niche{'s' if len(self._niches) != 1 else ''}"
                             + (f" · {len(on)} on" if len(on) != len(self._niches) else "")
                             + f" · {total_today} picked today · up to {sum(n.daily for n in on)} a day")

    def _fill_form(self) -> None:
        n = self._niche()
        self.form_area.setEnabled(n is not None)
        if n is None:
            return
        self._filling = True
        try:
            self.name.setText(n.name)
            self.account.setText(n.account)
            self.enabled.setChecked(n.enabled)
            self.daily.setValue(n.daily)
            self.channels.setPlainText(n.channels)
            self.searches.setPlainText(n.searches)
            self.keywords.setText(n.keywords)
            chosen = n.category_ids()
            for i in range(self.categories.count()):
                it = self.categories.item(i)
                it.setCheckState(Qt.Checked if it.data(Qt.UserRole) in chosen else Qt.Unchecked)
            for combo, value in ((self.preset, n.preset), (self.view, n.view)):
                i = combo.findData(value)
                if i < 0:  # a preset or view that has since been deleted
                    combo.addItem(f"{value} (missing)", value)
                    i = combo.count() - 1
                combo.setCurrentIndex(i)
            self.stack_folder.setText(n.stack_folder)
            self.per_video.setValue(n.clips_per_video)
            self.min_len.setValue(n.min_len)
            self.max_len.setValue(n.max_len)
            self.instructions.setPlainText(n.instructions)
        finally:
            self._filling = False

    def _checked_categories(self) -> str:
        return ", ".join(str(self.categories.item(i).data(Qt.UserRole)) for i in range(self.categories.count())
                         if self.categories.item(i).checkState() == Qt.Checked)

    def _update_notes(self) -> None:
        """Problems worth fixing (in the warning colour), and the line under the list."""
        s = self._settings()
        on = [n for n in self._niches if n.enabled]
        lines: list[str] = []
        total = sum(n.daily for n in on)
        if on and total > s.pick_daily:
            lines.append(f"Your niches add up to {total} videos a day, but at most {s.pick_daily} are picked a day in "
                         f"all (Settings → Picking), shared out between them. "
                         f"<a href='raise:{total}' style='color:{theme.ACCENT_2}'>Raise it to {total}</a>")
        if on and not youtube_key(s):
            lines.append("Add a YouTube Data API key (Settings → Research) so niches can find videos.")
        owner: dict[str, str] = {}
        searched: dict[str, str] = {}
        for n in on:
            refs, bad = n.channel_refs()
            if not refs and not n.search_list() and not n.keyword_list() and not n.category_ids():
                lines.append(f"{_esc(n.name)} has nothing to find videos with yet: add a channel or a Creative "
                             "Commons search.")
            for x in bad:
                lines.append(f"{_esc(n.name)}: \"{_esc(x)}\" isn't a channel link, @handle or id.")
            for ref in refs:
                if ref.key in owner and owner[ref.key] != n.name:
                    lines.append(f"{_esc(ref.label)} is in {_esc(owner[ref.key])} and {_esc(n.name)}; its videos go "
                                 f"to {_esc(owner[ref.key])}, the first.")
                owner.setdefault(ref.key, n.name)
            for q in n.search_list():
                if q.lower() in searched and searched[q.lower()] != n.name:
                    lines.append(f"The search \"{_esc(q)}\" is in {_esc(searched[q.lower()])} and {_esc(n.name)}; what "
                                 f"it finds goes to {_esc(searched[q.lower()])}, the first.")
                searched.setdefault(q.lower(), n.name)
        self.warnings.setText("<br>".join(f"<span style='color:{theme.WARN}'>⚠ {x}</span>" for x in lines))
        self.warnings.setVisible(bool(lines))
        n = self._niche()
        if n:
            self.sources_note.setText(
                "A video goes to the first niche that lists its channel, then the first whose search found it, "
                "then title words, then categories. Title words and categories matter for Creative Commons videos "
                "on the charts" + (", and for trending ones, since you pick from anything"
                                   if s.pick_from == "any" else "") + ".")
        self.hint.setText(f"Each niche's clips go to their own folder in {s.auto_output_path}, and every clip records "
                          "its niche's TikTok account. TikTok Manager doesn't queue clips by account yet, so check "
                          "which account it puts them under. Picking rules (Settings → Picking) still apply to "
                          "every niche.")

    # ================================================================== editing
    def _select(self, row: int) -> None:
        if 0 <= row < len(self._niches):
            self._rename()  # a name still being typed is kept
            self._current = self._niches[row].id
            self._fill_form()
            self._update_notes()

    def _set(self, name: str, value) -> None:
        if self._filling:
            return
        n = self._niche()
        if n is None or getattr(n, name) == value:
            return
        self._replace(replace(n, **{name: value}))

    def _replace(self, new: Niche) -> None:
        self._niches = [new if n.id == new.id else n for n in self._niches]
        self._save()
        self._refresh_stats()
        self._update_notes()

    def _save(self) -> None:
        self._change("niches", niches.to_settings(self._niches))
        self._changed_timer.start()

    def _rename(self) -> None:
        n = self._niche()
        if n is None or self._filling:
            return
        name = " ".join(self.name.text().split())
        if name == n.name:
            return
        if not name:
            self.name.setText(n.name)
            self.notice.emit("A niche needs a name.")
            return
        if niches.name_taken(self._niches, name, n.id):
            self.name.setText(n.name)
            self.notice.emit(f"<span style='color:{theme.WARN}'>⚠ There's already a niche called "
                             f"{_esc(name)}.</span>")
            return
        old = self._folder_of(n)
        self._replace(replace(n, name=name))
        if old.is_dir() and any(old.iterdir()):
            self.notice.emit(f"Renamed to {_esc(name)}. New clips go to its new folder; clips already made stay in "
                             f"{_esc(str(old))}.")

    def _browse_stack(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder of second videos", self.stack_folder.text())
        if d:
            self.stack_folder.setText(d)
            self._set("stack_folder", d)

    def add(self) -> None:
        s = self._settings()
        first = not self._niches
        n = Niche(id=niches.new_id(), name=niches.unique_name(self._niches, "My first niche" if first else "New niche"))
        if first and (s.pick_channels.strip() or s.pick_searches.strip()):
            n.channels, n.searches = s.pick_channels, s.pick_searches
            self._change("pick_channels", "")
            self._change("pick_searches", "")
            self.notice.emit("Made your first niche. Your channel list and Creative Commons searches are in it now.")
        self._niches.append(n)
        self._current = n.id
        self._save()
        self._fill_list()
        self._fill_form()
        self._update_notes()
        self.name.setFocus()
        self.name.selectAll()

    def delete(self) -> None:
        n = self._niche()
        if n is None:
            return
        waiting = sum(1 for j in self.db.jobs(limit=5000) if j.niche == n.id and j.status == "queued")
        last = len(self._niches) == 1
        text = (f"Delete the niche “{n.name}”?\n\n"
                + (f"Its {waiting} video{'s' if waiting != 1 else ''} waiting to be clipped come"
                   f"{'s' if waiting == 1 else ''} out of the queue. " if waiting else "")
                + f"Clips already made stay in {self._folder_of(n)}."
                + ("\n\nIt's your last niche, so its channels and searches go back to the channel list in "
                   "Settings → Picking, and automation picks for everything again." if last else ""))
        if QMessageBox.question(self, "Delete niche", text) != QMessageBox.Yes:
            return
        self.db.drop_waiting(n.id)
        if last and (n.channels.strip() or n.searches.strip()):
            s = self._settings()
            self._change("pick_channels", "\n".join(x for x in (s.pick_channels.strip(), n.channels.strip()) if x))
            self._change("pick_searches", "\n".join(x for x in (s.pick_searches.strip(), n.searches.strip()) if x))
        i = next(i for i, x in enumerate(self._niches) if x.id == n.id)
        self._niches.pop(i)
        self._current = self._niches[min(i, len(self._niches) - 1)].id if self._niches else ""
        self._save()
        self._fill_list()
        self._fill_form()
        self._update_notes()
        self.notice.emit(f"Deleted the niche {_esc(n.name)}.")

    def _link(self, href: str) -> None:
        if href.startswith("raise:"):
            self._change("pick_daily", int(href.split(":", 1)[1]))
            self._update_notes()
            self.notice.emit("Raised the daily limit (Settings → Picking).")

    def _open_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # ================================================================== Qt
    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not e.spontaneous():  # opened (not a window restored): styles and views saved elsewhere, a new key…
            self._fill_choices()
            self._fill_form()
            self._update_notes()
        self._refresh_stats()
        self.timer.start()

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self.timer.stop()

    def commit(self) -> None:
        """Save what's still being typed in the boxes that save when you leave them (the name and the
        second-video folder), before the settings are copied or saved."""
        self._rename()
        n = self._niche()
        folder = self.stack_folder.text().strip()
        if n is not None and folder != n.stack_folder:
            self._set("stack_folder", folder)

    def shutdown(self) -> None:
        self.timer.stop()
        self.commit()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

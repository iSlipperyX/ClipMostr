"""Categories tab: the topic of a TikTok account each, with its own channels, searches, look and folder.

The list on the left shows every category and how it's doing today; the form on the right edits the
selected one. Every change is saved straight away (Settings.categories), and automation and the
Research tab pick it up on their next look.

Every category posts to a TikTok account: a new one is made with its account (picked, or added there
and then with the account wizard), and one without (from before 3.2, or whose account was removed) is
held, nothing picked or clipped, and shown in the warning colour until it has one.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter,
                               QVBoxLayout, QWidget)

from .. import categories
from ..jobs import JobDB
from ..categories import MAX_CLIPS, MAX_CREATOR_DAILY, MAX_DAILY, MAX_LEN, Category
from ..policy import PickLog, picks_path
from ..research import youtube_key
from ..settings import PRESETS, Settings, all_layouts, all_presets
from ..trending import CATEGORIES as YT_CATEGORIES
from . import theme
from .forms import WrapLabel

STATS_EVERY = 5000  # ms between refreshes of the counts while the tab is showing
CHANNELS_HINT = "@somepodcast  # clipping programme, credit in caption\nhttps://www.youtube.com/@streamer"


def _saved_accounts() -> list:
    from ..posting.accounts import Accounts

    try:
        return list(Accounts.load())
    except OSError:
        return []


class Categories(QWidget):
    notice = Signal(str)
    changed = Signal()  # categories were edited (other tabs re-sort their videos)
    open_settings = Signal(str)
    add_account = Signal()  # "Add a new account…" was picked and there's no wizard (`onboard`) to run

    def __init__(self, settings: Callable[[], Settings], change: Callable[[str, Any], None],
                 db: JobDB | None = None, parent=None, accounts: Callable[[], list] | None = None,
                 onboard: Callable[[QWidget, str], str] | None = None):
        super().__init__(parent)
        self._accounts = accounts or _saved_accounts
        # (parent, category id) -> the id of an account added in the account wizard, "" if none was
        self.onboard = onboard
        self._settings = settings
        self._change = change
        self.db = db or JobDB()
        self.picks = PickLog(picks_path())
        self._categories: list[Category] = categories.load(settings())
        self._current = ""  # id of the category in the form
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
        title = QLabel("Categories")
        title.setObjectName("title")
        self.summary = QLabel()
        self.summary.setProperty("muted", True)
        self.summary.setTextFormat(Qt.RichText)
        self.add_btn = QPushButton("＋  New category")
        self.add_btn.setObjectName("primary")
        self.add_btn.clicked.connect(self.add)
        open_dir = QPushButton("Open category folders")
        open_dir.setToolTip("The Automation folder, with a folder for each category")
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
        first = QPushButton("＋  Create my first category")
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
            label = WrapLabel(text)
            label.setProperty("muted", True)
            form.addRow(label)
            return label

        section("The category")
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Gaming, Podcasts, Science")
        self.name.editingFinished.connect(self._rename)
        form.addRow("Name", self.name)
        self.account = QComboBox()
        self.account.setToolTip("The TikTok account this category's clips post to. Each clip goes to that account's "
                                "queue as soon as it's made, with its schedule. One account can take several "
                                "categories")
        self.account.activated.connect(self._pick_account)
        form.addRow("Posts to", self.account)
        self.account_note = WrapLabel()
        self.account_note.setTextFormat(Qt.RichText)
        self.account_note.setAlignment(Qt.AlignLeft | Qt.AlignTop)  # right under the account it's about
        form.addRow("", self.account_note)
        self.enabled = QCheckBox("Pick videos for this category")
        self.enabled.setToolTip("Off: nothing new is picked for it. Videos already waiting are still clipped")
        self.enabled.toggled.connect(lambda v: self._set("enabled", bool(v)))
        form.addRow("", self.enabled)
        self.daily = QSpinBox()
        self.daily.setRange(1, MAX_DAILY)
        self.daily.setSuffix(" a day")
        self.daily.setToolTip("Videos picked for this category in 24 hours. Each video makes several clips")
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
        self.finds = QPlainTextEdit()
        self.finds.setFixedHeight(70)
        self.finds.setPlaceholderText("Call of Duty MW4 with good comedy\nWarzone funny moments")
        self.finds.setToolTip("Videos you want for this category, one request per line, in your own words. Automation "
                              "searches YouTube for each one every few hours (Settings → Picking) and picks the "
                              "best new matches, within the category's videos a day. Each run costs about 200 quota "
                              "units")
        self.finds.textChanged.connect(lambda: self._set("finds", self.finds.toPlainText()))
        form.addRow("Standing requests", self.finds)
        self.keywords = QLineEdit()
        self.keywords.setPlaceholderText("minecraft, fortnite, speedrun")
        self.keywords.setToolTip("Comma separated. A trending or Creative Commons video with one of these in its "
                                 "title or channel name fits this category")
        self.keywords.textChanged.connect(lambda v: self._set("keywords", v))
        form.addRow("Title words", self.keywords)
        self.yt_categories = QListWidget()
        self.yt_categories.setFixedHeight(120)
        for cid, name in YT_CATEGORIES.items():
            if cid:
                it = QListWidgetItem(name)
                it.setData(Qt.UserRole, cid)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Unchecked)
                self.yt_categories.addItem(it)
        self.yt_categories.setToolTip("Trending or Creative Commons videos in these YouTube categories fit this category")
        self.yt_categories.itemChanged.connect(lambda _it: self._set("yt_categories", self._checked_yt_categories()))
        form.addRow("YouTube categories", self.yt_categories)
        self.sources_note = note("")

        section("How its clips look")
        self.preset = QComboBox()
        self.preset.setToolTip("How clips are found and exported for this category")
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
        self.per_video.setToolTip("How many clips each of this category's videos gives")
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
            box_.setKeyboardTracking(False)  # typing 45 doesn't save 4 on the way
            box_.valueChanged.connect(lambda v, key=key: self._set(key, float(v)))
        lrow.addWidget(self.min_len)
        lrow.addWidget(QLabel("to"))
        lrow.addWidget(self.max_len)
        lrow.addStretch(1)
        lengths.setToolTip("How long this category's clips are, shortest to longest. Leave at Style's to use the style's")
        form.addRow("Clip length", lengths)
        self.instructions = QPlainTextEdit()
        self.instructions.setFixedHeight(70)
        self.instructions.setPlaceholderText("e.g. clutch plays and funny fails, skip menus and loading screens, "
                                             "no swearing in the hook")
        self.instructions.setToolTip("Plain words for the AI. With a Claude or Gemini key, the AI picks this category's "
                                     "clips so it can follow them, and it writes the hooks and bottom text to "
                                     "suit. Each video then counts towards AI calls per day")
        self.instructions.textChanged.connect(lambda: self._set("instructions", self.instructions.toPlainText()))
        form.addRow("Special requests", self.instructions)
        note("A recipe in Settings → Automation for a channel or category still wins over the category's style.")

        section("Video Creator")
        self.creator_daily = QSpinBox()
        self.creator_daily.setRange(0, MAX_CREATOR_DAILY)
        self.creator_daily.setSpecialValueText("None")
        self.creator_daily.setSuffix(" a day")
        self.creator_daily.setToolTip("Original videos the Video Creator makes for this category every day, planned "
                                      "by automation and made overnight (Settings → Creator), then queued for its "
                                      "account like the clips, labelled AI-generated")
        self.creator_daily.valueChanged.connect(lambda v: self._set("creator_daily", int(v)))
        form.addRow("Creator videos", self.creator_daily)
        self.creator_topics = QPlainTextEdit()
        self.creator_topics.setFixedHeight(90)
        self.creator_topics.setPlaceholderText("3 stoic habits that will change your life\nThe psychology of "
                                               "first impressions\n# one idea per line")
        self.creator_topics.setToolTip("Ideas for this category's Creator videos, one per line; each is used at most "
                                       "once a month. They take turns with what's trending in the category on "
                                       "YouTube; with neither, the title words and the name are used")
        self.creator_topics.textChanged.connect(lambda: self._set("creator_topics",
                                                                  self.creator_topics.toPlainText()))
        form.addRow("Topics", self.creator_topics)
        self.creator_note = note("")

        buttons = QHBoxLayout()
        self.folder_btn = QPushButton("Open its folder")
        self.folder_btn.clicked.connect(lambda: self._open_dir(self._folder_of(self._category())))
        self.delete_btn = QPushButton("Delete category")
        self.delete_btn.clicked.connect(self.delete)
        buttons.addWidget(self.folder_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.delete_btn)
        form.addRow(buttons)
        return page

    # ================================================================== state
    def reload(self) -> None:
        """Show the categories as they are in the settings (after the Settings dialog, too)."""
        self._categories = categories.load(self._settings())
        if not any(n.id == self._current for n in self._categories):
            self._current = self._categories[0].id if self._categories else ""
        self._fill_choices()
        self._fill_list()
        self._fill_form()
        self._update_notes()

    def _category(self) -> Category | None:
        return next((n for n in self._categories if n.id == self._current), None)

    def _folder_of(self, n: Category | None) -> Path:
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
        for n in self._categories:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, n.id)
            it.setSizeHint(QSize(0, 66))
            self.list.addItem(it)
        row = next((i for i, n in enumerate(self._categories) if n.id == self._current), -1)
        self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        self._refresh_stats()
        has = bool(self._categories)
        self.split.setVisible(has)
        self.empty.setVisible(not has)
        s = self._settings()
        moving = bool(s.pick_channels.strip() or s.pick_searches.strip() or s.pick_finds.strip())
        self.empty_text.setText(
            "<p style='font-size:13pt'><b>Sort your clips by category, one TikTok account each</b></p>"
            "<p>A category is the topic of one TikTok account: the channels and Creative Commons searches its "
            "videos come from, how many videos a day it gets, and how its clips look. Automation picks videos "
            "for every category in turn and puts each category's clips in a folder of its own.</p>"
            + ("<p>Your channel list, searches and standing requests (Settings → Picking) move into the first "
               "category, so nothing is lost.</p>" if moving else ""))

    def _refresh_stats(self) -> None:
        """Each category's line in the list: account, sources, and how today is going."""
        if not self._categories:
            self.summary.setText("")
            return
        now = time.time()
        self.picks.reload()
        try:
            counts = self.db.category_counts(now - 7 * 86400)
        except Exception:  # the runner has jobs.db busy; try again next time
            counts = {}
        total_today = 0
        try:
            handles = {a.id: a.handle for a in self._accounts()}
        except Exception:  # noqa: BLE001 - the accounts file is being written: the stored names do
            handles = {}
        for i, n in enumerate(self._categories):
            it = self.list.item(i)
            if it is None:
                continue
            today = self.picks.count_since(now - 86400, category_id=n.id)
            total_today += today
            c = counts.get(n.id, {})
            refs, _ = n.channel_refs()
            sources = [f"{len(refs)} channel{'s' if len(refs) != 1 else ''}"] if refs else []
            if n.search_list():
                sources.append(f"{len(n.search_list())} search{'es' if len(n.search_list()) != 1 else ''}")
            if n.keyword_list() or n.yt_category_ids():
                sources.append("trending")
            who = "⚠ needs an account" if n.needs_account else (handles.get(n.account_id) or n.account or "its account")
            line2 = " · ".join([who] + (sources or ["no sources yet"]))
            line3 = (f"Today {today}/{n.daily} · {c.get('waiting', 0)} waiting · {c.get('clips', 0)} clips this week"
                     if n.on else n.state.split(":")[0] + ": nothing is picked")
            it.setText(f"{n.name}\n{line2}\n{line3}")
            it.setForeground(QColor(theme.WARN if n.enabled and n.needs_account else
                                    theme.TEXT if n.on else theme.MUTED))
            it.setToolTip(str(self._folder_of(n)))
        on = [n for n in self._categories if n.enabled]
        self.summary.setText(f"   {len(self._categories)} {'category' if len(self._categories) == 1 else 'categories'}"
                             + (f" · {len(on)} on" if len(on) != len(self._categories) else "")
                             + f" · {total_today} picked today · up to {sum(n.daily for n in on)} a day")

    def _fill_form(self) -> None:
        n = self._category()
        self.form_area.setEnabled(n is not None)
        if n is None:
            return
        self._filling = True
        try:
            self.name.setText(n.name)
            self._fill_accounts(n)
            self.enabled.setChecked(n.enabled)
            self.daily.setValue(n.daily)
            self.channels.setPlainText(n.channels)
            self.searches.setPlainText(n.searches)
            self.finds.setPlainText(n.finds)
            self.keywords.setText(n.keywords)
            chosen = n.yt_category_ids()
            for i in range(self.yt_categories.count()):
                it = self.yt_categories.item(i)
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
            self.creator_daily.setValue(n.creator_daily)
            self.creator_topics.setPlainText(n.creator_topics)
            self._creator_note(n)
        finally:
            self._filling = False

    def _creator_note(self, n) -> None:
        from ..creator import schedule

        if not n.creator_daily:
            self.creator_note.setText("Set a number of Creator videos a day and automation makes them for this "
                                      "category from its topics and what's trending in it.")
            return
        ideas = schedule.topic_lines(n.creator_topics)
        when = "overnight" if getattr(self._settings(), "creator_schedule_when", "night") == "night" else \
            "as soon as they're planned"
        self.creator_note.setText(
            f"{n.creator_daily} a day, made {when}"
            + (f", from your {len(ideas)} topic{'s' if len(ideas) != 1 else ''} and what's trending" if ideas else
               ", from what's trending in it, then its title words")
            + (". It has no account yet, so nothing is planned." if n.needs_account else ".")
            + " Each one waits for your OK in the Queue tab at first (Settings → Creator).")

    def _checked_yt_categories(self) -> str:
        return ", ".join(str(self.yt_categories.item(i).data(Qt.UserRole)) for i in range(self.yt_categories.count())
                         if self.yt_categories.item(i).checkState() == Qt.Checked)

    def _update_notes(self) -> None:
        """Problems worth fixing (in the warning colour), and the line under the list."""
        if self._category() is not None and hasattr(self, "creator_note"):
            self._creator_note(self._category())
        s = self._settings()
        on = [n for n in self._categories if n.enabled]
        lines: list[str] = []
        total = sum(n.daily for n in on)
        if on and total > s.pick_daily:
            lines.append(f"Your categories add up to {total} videos a day, but at most {s.pick_daily} are picked a day in "
                         f"all (Settings → Picking), shared out between them. "
                         f"<a href='raise:{total}' style='color:{theme.ACCENT_2}'>Raise it to {total}</a>")
        loose = [n for n in on if n.needs_account]
        if loose:
            lines.append(f"{', '.join(_esc(n.name) for n in loose)} need{'s' if len(loose) == 1 else ''} a TikTok "
                         "account: nothing is picked or clipped for "
                         f"{'it' if len(loose) == 1 else 'them'} until "
                         f"{'it has' if len(loose) == 1 else 'they have'} one. "
                         f"<a href='account:{loose[0].id}' style='color:{theme.ACCENT_2}'>Pick one</a>")
        if on and not youtube_key(s):
            lines.append("Add a YouTube Data API key (Settings → Research) so categories can find videos.")
        owner: dict[str, str] = {}
        searched: dict[str, str] = {}
        for n in on:
            refs, bad = n.channel_refs()
            if not refs and not n.search_list() and not n.keyword_list() and not n.yt_category_ids():
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
        n = self._category()
        if n:
            self.sources_note.setText(
                "A video goes to the first category that lists its channel, then the first whose search found it, "
                "then title words, then categories. Title words and categories matter for Creative Commons videos "
                "on the charts" + (", and for trending ones, since you pick from anything"
                                   if s.pick_from == "any" else "") + ".")
        self.hint.setText(f"Each category's clips go to their own folder in {s.auto_output_path} and straight into "
                          "its TikTok account's posting queue. Picking rules (Settings → Picking) still apply to "
                          "every category.")

    # ================================================================== editing
    def _select(self, row: int) -> None:
        if 0 <= row < len(self._categories):
            self._rename()  # a name still being typed is kept
            self._current = self._categories[row].id
            self._fill_form()
            self._update_notes()

    # ------------------------------------------------------------ the account it posts to
    ADD_ACCOUNT = "+add"

    def _fill_accounts(self, n: Category) -> None:
        fill_account_choices(self.account, self._accounts(), self._categories, n.account_id, n.id)
        self._show_account_note(n)

    def _show_account_note(self, n: Category) -> None:
        if n.needs_account:
            self.account_note.setText(f"<span style='color:{theme.WARN}'>⚠ Every category needs a TikTok account. "
                                      "Nothing is picked or clipped for this one until it has one.</span>")
        else:
            a = next((a for a in self._accounts() if a.id == n.account_id), None)
            others = [x.name for x in self._categories if x.account_id == n.account_id and x.id != n.id]
            bits = []
            if a is not None and not a.logged_in:
                bits.append(f"<span style='color:{theme.WARN}'>⚠ {_esc(a.handle)} isn't signed in to TikTok, so "
                            "nothing posts yet: sign it in on the Accounts tab.</span>")
            if others:
                bits.append(f"Also posts {_esc(', '.join(others))}. Its schedule (Accounts tab) is shared out "
                            "between them.")
            elif a is not None:
                bits.append("Clips post on that account's schedule (Accounts tab).")
            self.account_note.setText(" ".join(bits))
        self.account_note.setVisible(bool(self.account_note.text()))

    def _pick_account(self, index: int) -> None:
        n = self._category()
        if n is None or self._filling:
            return
        aid = self.account.itemData(index) or ""
        if aid == self.ADD_ACCOUNT:
            aid = self._onboard(n.id)
            if not aid:
                self._fill_accounts(n)
                return
            n = self._category() or n  # the wizard may have picked it already
        if not aid:
            self._fill_accounts(n)
            return
        a = next((a for a in self._accounts() if a.id == aid), None)
        self._replace(replace(n, account_id=aid, account=a.handle if a else n.account))
        self._fill_accounts(self._category() or n)
        self.notice.emit(f"{n.name}'s clips now post to {a.handle if a else 'that account'}.")

    def _onboard(self, category_id: str = "") -> str:
        """Add an account with the wizard; its id, or "" when none was added."""
        if self.onboard is None:
            self.add_account.emit()
            return ""
        aid = self.onboard(self, category_id) or ""
        self._categories = categories.load(self._settings())  # the wizard may have given categories this account
        return aid

    def accounts_changed(self) -> None:
        """Accounts were added, renamed or removed in the Accounts tab."""
        n = self._category()
        if n is not None:
            self._fill_accounts(n)
        self._refresh_stats()

    def _set(self, name: str, value) -> None:
        if self._filling:
            return
        n = self._category()
        if n is None or getattr(n, name) == value:
            return
        self._replace(replace(n, **{name: value}))

    def _replace(self, new: Category) -> None:
        self._categories = [new if n.id == new.id else n for n in self._categories]
        self._save()
        self._refresh_stats()
        self._update_notes()

    def _save(self) -> None:
        self._change("categories", categories.to_settings(self._categories))
        self._changed_timer.start()

    def _rename(self) -> None:
        n = self._category()
        if n is None or self._filling:
            return
        name = " ".join(self.name.text().split())
        if name == n.name:
            return
        if not name:
            self.name.setText(n.name)
            self.notice.emit("A category needs a name.")
            return
        if categories.name_taken(self._categories, name, n.id):
            self.name.setText(n.name)
            self.notice.emit(f"<span style='color:{theme.WARN}'>⚠ There's already a category called "
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
        """A new category, made with its name and its account (both asked for first)."""
        s = self._settings()
        first = not self._categories
        dlg = NewCategoryDialog(categories.unique_name(self._categories, "My first category" if first else
                                                       "New category"),
                                self._accounts, self._categories, self._onboard, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._categories = categories.load(self._settings())
        if categories.name_taken(self._categories, dlg.name()):
            self.notice.emit(f"<span style='color:{theme.WARN}'>⚠ There's already a category called "
                             f"{_esc(dlg.name())}.</span>")
            return
        a = next((a for a in self._accounts() if a.id == dlg.account_id()), None)
        n = Category(id=categories.new_id(), name=dlg.name(), account_id=dlg.account_id(),
                     account=a.handle if a else "")
        if first and (s.pick_channels.strip() or s.pick_searches.strip() or s.pick_finds.strip()):
            n.channels, n.searches, n.finds = s.pick_channels, s.pick_searches, s.pick_finds
            self._change("pick_channels", "")
            self._change("pick_searches", "")
            self._change("pick_finds", "")
            self.notice.emit("Made your first category. Your channel list, searches and standing requests are in it now.")
        self._categories.append(n)
        self._current = n.id
        self._save()
        self._fill_list()
        self._fill_form()
        self._update_notes()
        self.channels.setFocus()

    def delete(self) -> None:
        n = self._category()
        if n is None:
            return
        waiting = sum(1 for j in self.db.jobs(limit=5000) if j.category_id == n.id and j.status == "queued")
        last = len(self._categories) == 1
        text = (f"Delete the category “{n.name}”?\n\n"
                + (f"Its {waiting} video{'s' if waiting != 1 else ''} waiting to be clipped come"
                   f"{'s' if waiting == 1 else ''} out of the queue. " if waiting else "")
                + f"Clips already made stay in {self._folder_of(n)}."
                + ("\n\nIt's your last category, so its channels, searches and standing requests go back to "
                   "Settings → Picking, and automation picks for everything again." if last else ""))
        if QMessageBox.question(self, "Delete category", text) != QMessageBox.Yes:
            return
        self.db.drop_waiting(n.id)
        if last and (n.channels.strip() or n.searches.strip() or n.finds.strip()):
            s = self._settings()
            self._change("pick_channels", "\n".join(x for x in (s.pick_channels.strip(), n.channels.strip()) if x))
            self._change("pick_searches", "\n".join(x for x in (s.pick_searches.strip(), n.searches.strip()) if x))
            self._change("pick_finds", "\n".join(x for x in (s.pick_finds.strip(), n.finds.strip()) if x))
        i = next(i for i, x in enumerate(self._categories) if x.id == n.id)
        self._categories.pop(i)
        self._current = self._categories[min(i, len(self._categories) - 1)].id if self._categories else ""
        self._save()
        self._fill_list()
        self._fill_form()
        self._update_notes()
        self.notice.emit(f"Deleted the category {_esc(n.name)}.")

    def _link(self, href: str) -> None:
        if href.startswith("account:"):
            cid = href.split(":", 1)[1]
            row = next((i for i, n in enumerate(self._categories) if n.id == cid), -1)
            if row >= 0:
                self.list.setCurrentRow(row)
                self.account.setFocus()
                self.account.showPopup()
            return
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
        n = self._category()
        folder = self.stack_folder.text().strip()
        if n is not None and folder != n.stack_folder:
            self._set("stack_folder", folder)

    def shutdown(self) -> None:
        self.timer.stop()
        self.commit()


def fill_account_choices(combo: QComboBox, accounts: list, all_categories: list[Category], account_id: str,
                         category_id: str = "") -> None:
    """The account picker: every account (with the other categories it posts), then "Add a new account…".
    With no account picked yet, a first line asks for one."""
    combo.blockSignals(True)
    combo.clear()
    if not account_id:
        combo.addItem("⚠ Pick an account" if accounts else "No accounts yet: add one", "")
    known = False
    for a in accounts:
        others = [n.name for n in all_categories if n.account_id == a.id and n.id != category_id]
        label = a.label if a.label == a.handle else f"{a.label} ({a.handle})"
        if not a.logged_in:
            label += " · not signed in"
        if others:
            label += f" · also {', '.join(others)}"
        combo.addItem(label, a.id)
        known = known or a.id == account_id
    if account_id and not known:  # removed since
        combo.addItem("Removed account", account_id)
    combo.insertSeparator(combo.count())
    combo.addItem("＋ Add a new account…", Categories.ADD_ACCOUNT)
    i = combo.findData(account_id) if account_id else 0
    combo.setCurrentIndex(max(0, i))
    combo.blockSignals(False)


class NewCategoryDialog(QDialog):
    """A new category's name and the TikTok account it posts to: both are needed before it's made."""

    def __init__(self, name: str, accounts: Callable[[], list], all_categories: list[Category],
                 onboard: Callable[[str], str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("New category")
        self.setMinimumWidth(460)
        self._accounts, self._all, self._onboard = accounts, all_categories, onboard
        lay = QVBoxLayout(self)
        intro = QLabel("A category is what one TikTok account posts: its clips go to that account's queue. "
                       "One account can take several categories.")
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        lay.addWidget(intro)
        form = QFormLayout()
        self.name_edit = QLineEdit(name)
        self.name_edit.selectAll()
        self.name_edit.textChanged.connect(self._check)
        form.addRow("Name", self.name_edit)
        self.account = QComboBox()
        self.account.activated.connect(self._picked)
        form.addRow("Posts to", self.account)
        lay.addLayout(form)
        self.problem = QLabel()
        self.problem.setStyleSheet(f"color: {theme.WARN}")
        lay.addWidget(self.problem)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Create category")
        self.buttons.button(QDialogButtonBox.Ok).setObjectName("primary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._fill("")

    def _fill(self, account_id: str) -> None:
        fill_account_choices(self.account, self._accounts(), self._all, account_id)
        self._check()

    def _picked(self, index: int) -> None:
        aid = self.account.itemData(index) or ""
        if aid == Categories.ADD_ACCOUNT:
            aid = self._onboard("") or ""
        self._fill(aid if aid != Categories.ADD_ACCOUNT else "")

    def name(self) -> str:
        return " ".join(self.name_edit.text().split())

    def account_id(self) -> str:
        aid = self.account.currentData() or ""
        return "" if aid == Categories.ADD_ACCOUNT else aid

    def _check(self) -> None:
        why = ("Give it a name." if not self.name() else
               "There's already a category called that." if categories.name_taken(self._all, self.name()) else
               "Pick the TikTok account it posts to, or add one." if not self.account_id() else "")
        self.problem.setText(why)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(not why)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

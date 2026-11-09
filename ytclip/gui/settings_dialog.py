"""The full Settings dialog: one page per settings group, picked from a sidebar."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from .. import categories, updater, youtube_login
from ..settings import SCHEMA, Settings, app_dir
from ..gemini import KEY_URL as GEMINI_KEY_URL
from ..trending import KEY_HELP_URL
from . import forms, theme
from .navigation import NavTabs
from .sign_in_dialog import SignInDialog

ANTHROPIC_KEY_URL = "https://console.anthropic.com/settings/keys"
GROUPS = ("General", "Download", "Clip finding", "Transcription", "Speed", "AI", "Export", "Captions", "Editing", "Creator", "Layout",
          "Research", "Picking", "Automation", "Posting", "Telegram", "Updates")



def _scrolling(page: QWidget) -> QScrollArea:
    """A settings page in a scroll area at its own height. In a window taller than the page, the space is
    left under it: a form hands spare height to rows that can grow, which pulled a file picker and the
    Test keys row apart from their labels."""
    holder = QWidget()
    column = QVBoxLayout(holder)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(0)
    column.addWidget(page)
    column.addStretch(1)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(holder)
    return scroll

class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None, tab: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(880, 640)
        self.result_settings = settings.copy()
        self._editors: dict[str, forms.Editor] = {}
        self.signed_in = False  # signed in to YouTube from here: the caller ends a sign-in pause
        self.go_back = False  # Go back to the previous version was pressed: the caller does it after saving
        self.run_setup = False  # Set up again was pressed: the caller opens the setup after saving
        # Posting has its own settings file (posting/settings.json): saved with Save, like the rest
        from ..posting import config as posting_config

        self.posting_settings = posting_config.Settings.load()
        self._posting_editors: dict[str, forms.Editor] = {}
        self._telegram = None  # Settings → Telegram's linking panel (telegram_page.py)

        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        # Sections down the left, like System Settings; the page on the right under its own title.
        tabs = NavTabs()
        tabs.sidebar.setFixedWidth(196)
        shell.addWidget(tabs.sidebar)
        right = QWidget()
        layout = QVBoxLayout(right)
        layout.setContentsMargins(22, 16, 18, 14)
        layout.setSpacing(10)
        shell.addWidget(right, 1)
        heading = QLabel()
        heading.setObjectName("title")
        layout.addWidget(heading)
        tabs.currentChanged.connect(lambda i: heading.setText(tabs.tabText(i)))
        # With categories, each category has its own channels and searches (Categories tab).
        per_category = {"pick_channels", "pick_searches", "pick_finds"} if categories.load(settings) else set()
        for group in GROUPS:
            if group == "Posting":
                page, self._posting_editors = forms.build_form(posting_config.SCHEMA, self.posting_settings,
                                                               self._posting_changed)
                note = QLabel("Each TikTok account's schedule, hashtags and login are set in the Accounts tab. "
                              "These apply to every account. Posting runs in automation's background process, so "
                              "start automation (Automation tab) and it posts, reads stats and clears posted clips "
                              "by itself, with the window closed too.")
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
                tabs.addTab(_scrolling(page), group)
                continue
            fields = [f for f in SCHEMA if f.group == group and f.name not in per_category]
            page, editors = forms.build_form(fields, self.result_settings, self._changed)
            self._editors |= editors
            if group == "AI":
                note = QLabel("The AI clip finder gives the video's transcript to the AI you choose and asks it "
                              "to pick self-contained moments. Local AI runs a model on this PC: free, private, "
                              "no key (the model that suits this PC downloads once, 2 to 20 GB; Test keys says "
                              "how it would run here). Claude and Gemini run online and "
                              "need a key; AI polish sends them each clip's words and one frame, and gets back "
                              "hooks, caption fixes and where the speaker is. "
                              f"Get a Claude key at <a href='{ANTHROPIC_KEY_URL}' style='color:{theme.ACCENT_2}'>"
                              "console.anthropic.com</a> or a Gemini key at "
                              f"<a href='{GEMINI_KEY_URL}' style='color:{theme.ACCENT_2}'>aistudio.google.com"
                              "</a> (Google may use what free keys send to improve its products; keys with "
                              "billing on aren't used that way). You can also set ANTHROPIC_API_KEY or "
                              "GEMINI_API_KEY as environment variables instead.")
                note.setWordWrap(True)
                note.setOpenExternalLinks(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
                row = QHBoxLayout()
                test = QPushButton("Test keys")
                test.setToolTip("Checks each key you've added, and that it can use the models picked above. "
                                "Uses no tokens.")
                self._key_result = forms.WrapLabel("")
                test.clicked.connect(self._test_key)
                row.addWidget(test)
                row.addWidget(self._key_result, 1)
                page.layout().addRow("", row)
            if group == "Download":
                row = QHBoxLayout()
                sign_in = QPushButton("🔑  Sign in to YouTube…")
                sign_in.setToolTip("Opens Firefox, just for ClipMonster, to sign in to YouTube once. Downloads then "
                                   "use that sign-in")
                sign_in.clicked.connect(self._sign_in)
                row.addWidget(sign_in)
                row.addStretch(1)
                page.layout().addRow("", row)
            if group == "Speed":
                from .. import whisper_cpp

                note = QLabel(f"This PC has {os.cpu_count() or '?'} CPU threads. Quiet and Balanced let automation "
                              "step aside for whatever else you're doing; Full power doesn't. The Automation log "
                              "says how long each step of each video took, so you can see the difference.")
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
                why = whisper_cpp.unavailable()
                rest = "" if why else whisper_cpp.rest_note()
                self._gpu_state = forms.WrapLabel("Graphics card: " + (
                    f"not used: {why}" if why else f"{rest}. Whisper uses the CPU until then." if rest else
                    "ready (it's tried on the next video)"))  # on a row of its own, so a long reason wraps in full
                page.layout().addRow("", self._gpu_state)
                if (why or rest) and whisper_cpp.binary() is not None:
                    again = QPushButton("Try the graphics card again")
                    again.setToolTip("After a graphics driver update, say: Whisper tries the card on the next video")
                    again.clicked.connect(lambda: (whisper_cpp.forget_gpu_check(), again.hide(), self._gpu_state.setText(
                        "Graphics card: tried again on the next video")))
                    row = QHBoxLayout()
                    row.addWidget(again)
                    row.addStretch(1)
                    page.layout().addRow("", row)
            if group == "General":
                row = QHBoxLayout()
                again = QPushButton("Set up again…")
                again.setToolTip("The first-run setup: tools, keys, YouTube sign-in, TikTok accounts, a category, the "
                                 "posting schedule. What you've set up already is filled in")
                again.clicked.connect(self._run_setup)
                row.addWidget(again)
                row.addStretch(1)
                page.layout().addRow("Setup", row)
            if group == "Updates":
                repo = updater.configured_repo(self.result_settings.update_repo)
                where = updater.install_dir()
                note = QLabel(f"You have ClipMonster {updater.build_label()}"
                              + (f", installed in {where}. " if where else ", running from source. ")
                              + (f"New versions are published at github.com/{repo}/releases. "
                                 if repo else "This build doesn't know its GitHub repository yet. Builds made by "
                                 "the GitHub release workflow set it automatically. ")
                              + "Updates are checked against their SHA-256 checksum and kept out of the install "
                                f"folder, in {updater.updates_dir()}, until they're installed. Your settings, "
                                "session and clips are never touched by an update.")
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
                previous = updater.previous_version()
                row = QHBoxLayout()
                back = QPushButton(f"Go back to {previous}" if previous else "Go back to the previous version")
                back.setEnabled(bool(previous) and where is not None)
                back.setToolTip("Installs the version you had before the last update again. The version you have "
                                "now is kept, so you can come back to it the same way" if previous else
                                "The version before each update is kept from 2.8.0 on, so this is possible after "
                                "your next update")
                back.clicked.connect(self._go_back)
                row.addWidget(back)
                row.addStretch(1)
                page.layout().addRow("", row)
            if group == "Research":
                note = QLabel("The Research tab reads YouTube's most-popular chart for each region and scores "
                              "every video on how well it will cut into clips. It needs a free YouTube Data API "
                              f"key: <a href='{KEY_HELP_URL}' style='color:{theme.ACCENT_2}'>enable YouTube Data "
                              "API v3</a> in a Google Cloud project, then create an API key under Credentials. "
                              "You can also set the YOUTUBE_API_KEY environment variable instead.")
                note.setWordWrap(True)
                note.setOpenExternalLinks(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
            if group == "Picking":
                note = QLabel("These rules decide which researched videos ClipMonster may clip without asking you. "
                              "Reposting other people's videos without permission can get clips removed and "
                              "accounts struck, so by default only channels on your list (creators who say clips "
                              "are welcome) and Creative Commons videos are picked. The Research tab's Pick column "
                              "shows what the rules would pick, and why the rest were skipped."
                              + (" You have categories, so each one's channels and Creative Commons searches are set "
                                 "in the Categories tab; these rules apply to all of them." if per_category else ""))
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
            if group == "Automation":
                note = QLabel("Automation clips what your picking rules pick, one video at a time, with no one "
                              "clicking. Start it from the Automation tab. These settings choose the style of its "
                              "clips, when it works, and how hard it pushes YouTube and the AI.")
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
            if group == "Telegram":
                from .telegram_page import TelegramLink

                note = QLabel("A Telegram bot of your own, run by automation: from your phone you can ask for "
                              "videos (a Creator video about an idea, clips of a link, a request), watch the videos "
                              "waiting to post, OK them or delete bad ones from this PC, pause automation and read "
                              "the week's report. It tells you when a video waits for your OK and when something "
                              "needs you. Only phones you link here can use it.")
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
                self._telegram = TelegramLink(lambda: self.result_settings.telegram_token,
                                              lambda: self.result_settings.telegram_on)
                page.layout().addRow(self._telegram)
            if group == "Transcription":
                note = QLabel("When a video has no captions (or for local files) ClipMonster can transcribe "
                              "speech on your computer with Whisper. Transcripts power clip titles, "
                              "burned-in captions, keyword boosts and the AI clip finder.")
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
            tabs.addTab(_scrolling(page), group)
        if tab in GROUPS:
            tabs.setCurrentIndex(GROUPS.index(tab))
        heading.setText(tabs.tabText(tabs.currentIndex()))
        self.tabs = tabs
        layout.addWidget(tabs, 1)

        footer = QLabel(f"Settings are stored in {app_dir()}")
        footer.setProperty("muted", True)
        layout.addWidget(footer)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        reset = QPushButton("Reset to defaults")
        buttons.addButton(reset, QDialogButtonBox.ResetRole)
        reset.clicked.connect(self._reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _changed(self, name: str, value) -> None:
        self.result_settings = self.result_settings.apply({name: value})
        s = self.result_settings
        if name in ("min_len", "max_len") and s.min_len > s.max_len:  # the other end follows along
            other = "max_len" if name == "min_len" else "min_len"
            self.result_settings = s.apply({other: value})
            forms.refresh({other: self._editors[other]}, self.result_settings)

    def _test_key(self) -> None:
        from .. import ai

        s = self.result_settings
        self._key_result.setText("Checking…")
        self._key_result.setStyleSheet(f"color:{theme.MUTED};")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        lines, colour = [], theme.GOOD
        try:
            # The chosen AI first, then the other one if it has a key too.
            providers = [ai.chosen(s)] + [p for p in ai.NAMES if p != ai.chosen(s) and ai.key_for(s, p)]
            for provider in providers:
                try:
                    lines.append(f"✓ {ai.check_key(s, provider)}")
                except Exception as exc:
                    lines.append(f"✗ {ai.name(provider)}: {exc}")
                    colour = theme.BAD if provider == providers[0] else theme.WARN
        finally:
            QApplication.restoreOverrideCursor()
        self._key_result.setText("\n".join(lines))
        self._key_result.setStyleSheet(f"color:{colour};")

    def _sign_in(self) -> None:
        dlg = SignInDialog(self)
        dlg.signed_in.connect(lambda: self._use_sign_in(True))
        dlg.forgotten.connect(lambda: self._use_sign_in(False))
        dlg.exec()

    def _use_sign_in(self, on: bool) -> None:
        self.signed_in = on
        mine = self.result_settings.cookies_browser == youtube_login.CHOICE
        if on != mine and (on or mine):
            self.result_settings = self.result_settings.apply({"cookies_browser": youtube_login.CHOICE if on else ""})
            forms.refresh(self._editors, self.result_settings)

    def _posting_changed(self, name: str, value) -> None:
        setattr(self.posting_settings, name, value)

    def accept(self) -> None:
        try:
            self.posting_settings.save()
        except OSError:
            pass
        super().accept()

    def done(self, result: int) -> None:
        if self._telegram is not None:
            self._telegram.shutdown()  # the window stops listening to Telegram, so automation's bot can
        super().done(result)

    def _run_setup(self) -> None:
        self.run_setup = True
        self.accept()

    def _go_back(self) -> None:
        self.go_back = True
        self.accept()

    def _reset(self) -> None:
        # Categories are your content, edited in their own tab, not a setting to reset.
        self.result_settings = Settings(categories=self.result_settings.categories,
                                        setup_done=self.result_settings.setup_done)
        forms.refresh(self._editors, self.result_settings)
        self.posting_settings = type(self.posting_settings)()
        forms.refresh(self._posting_editors, self.posting_settings)

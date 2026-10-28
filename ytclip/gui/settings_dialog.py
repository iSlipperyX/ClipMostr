"""The full Settings dialog: one tab per settings group."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QTabWidget, QVBoxLayout)

from .. import __version__, niches, updater, youtube_login
from ..settings import SCHEMA, Settings, app_dir
from ..trending import KEY_HELP_URL
from . import forms, theme
from .sign_in_dialog import SignInDialog

GROUPS = ("General", "Download", "Clip finding", "Transcription", "Speed", "AI", "Export", "Captions", "Layout",
          "Research", "Picking", "Automation", "Updates")


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None, tab: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(640, 620)
        self.result_settings = settings.copy()
        self._editors: dict[str, forms.Editor] = {}
        self.signed_in = False  # signed in to YouTube from here: the caller ends a sign-in pause

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        # With niches, each niche has its own channels and searches (Niches tab).
        per_niche = {"pick_channels", "pick_searches"} if niches.load(settings) else set()
        for group in GROUPS:
            fields = [f for f in SCHEMA if f.group == group and f.name not in per_niche]
            page, editors = forms.build_form(fields, self.result_settings, self._changed)
            self._editors |= editors
            if group == "AI":
                note = QLabel("The AI clip finder sends the video's transcript to Claude and asks it to pick "
                              "self-contained moments. AI polish sends it each clip's words and one frame, "
                              "and gets back hooks, caption fixes and where the speaker is. "
                              "Get an API key at console.anthropic.com. "
                              "You can also set the ANTHROPIC_API_KEY environment variable instead.")
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
                row = QHBoxLayout()
                test = QPushButton("Test key")
                test.setToolTip("Checks the key and model with Anthropic. Uses no tokens.")
                self._key_result = QLabel("")
                self._key_result.setWordWrap(True)
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
                self._gpu_state = QLabel("Graphics card: " + ("ready (it's tried on the next video)" if not why
                                                              else f"not used: {why}"))
                self._gpu_state.setWordWrap(True)
                row = QHBoxLayout()
                again = QPushButton("Try the graphics card again")
                again.setToolTip("After a graphics driver update, say: Whisper tries the card on the next video")
                again.setVisible(bool(why) and whisper_cpp.binary() is not None)
                again.clicked.connect(lambda: (whisper_cpp.forget_gpu_check(), again.hide(),
                                               self._gpu_state.setText("Graphics card: tried again on the next video")))
                row.addWidget(self._gpu_state, 1)
                row.addWidget(again)
                page.layout().addRow("", row)
            if group == "Updates":
                repo = updater.configured_repo(self.result_settings.update_repo)
                note = QLabel(f"You have ClipMonster {__version__}. "
                              + (f"New versions are published at github.com/{repo}/releases. "
                                 if repo else "This build doesn't know its GitHub repository yet. Builds made by "
                                 "the GitHub release workflow set it automatically. ")
                              + "Updates download in the background, are checked against their SHA-256 checksum, "
                                "and install when ClipMonster restarts. Your settings and session are kept.")
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
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
                              + (" You have niches, so each one's channels and Creative Commons searches are set "
                                 "in the Niches tab; these rules apply to all of them." if per_niche else ""))
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
            if group == "Automation":
                note = QLabel("Automation clips what your picking rules pick, one video at a time, with no one "
                              "clicking. Start it from the Automation tab. These settings choose the style of its "
                              "clips, when it works, and how hard it pushes YouTube and the Claude API.")
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
            if group == "Transcription":
                note = QLabel("When a video has no captions (or for local files) ClipMonster can transcribe "
                              "speech on your computer with Whisper. Transcripts power clip titles, "
                              "burned-in captions, keyword boosts and the AI clip finder.")
                note.setWordWrap(True)
                note.setProperty("muted", True)
                page.layout().insertRow(0, note)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            tabs.addTab(scroll, group)
        if tab in GROUPS:
            tabs.setCurrentIndex(GROUPS.index(tab))
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

    def _test_key(self) -> None:
        from ..ai import check_key

        s = self.result_settings
        self._key_result.setText("Checking…")
        self._key_result.setStyleSheet(f"color:{theme.MUTED};")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            msg, colour = check_key(s.ai_api_key or os.environ.get("ANTHROPIC_API_KEY", ""), s.ai_model), theme.GOOD
        except Exception as exc:
            msg, colour = f"✗ {exc}", theme.BAD
        finally:
            QApplication.restoreOverrideCursor()
        self._key_result.setText(msg if colour == theme.BAD else f"✓ {msg}")
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

    def _reset(self) -> None:
        # Niches are your content, edited in their own tab, not a setting to reset.
        self.result_settings = Settings(niches=self.result_settings.niches)
        forms.refresh(self._editors, self.result_settings)

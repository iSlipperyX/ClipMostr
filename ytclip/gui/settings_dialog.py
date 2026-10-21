"""The full Settings dialog: one tab per settings group."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QTabWidget, QVBoxLayout)

from .. import __version__, updater
from ..settings import SCHEMA, Settings, app_dir
from ..trending import KEY_HELP_URL
from . import forms, theme

GROUPS = ("General", "Download", "Clip finding", "Transcription", "AI", "Export", "Captions", "Layout",
          "Research", "Updates")


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(640, 620)
        self.result_settings = settings.copy()
        self._editors: dict[str, forms.Editor] = {}

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        for group in GROUPS:
            fields = [f for f in SCHEMA if f.group == group]
            page, editors = forms.build_form(fields, self.result_settings, self._changed)
            self._editors |= editors
            if group == "AI":
                note = QLabel("The AI clip finder sends the video's transcript to Claude and asks it to pick "
                              "self-contained moments. Get an API key at console.anthropic.com. "
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

    def _reset(self) -> None:
        self.result_settings = Settings()
        forms.refresh(self._editors, self.result_settings)

"""ClipMonster for Mac's first start: the AI that picks the clips, and where they're saved. Everything here is in
Settings too (AI, General), so it's one window, never a wizard to get stuck in."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLayout,
                               QLineEdit, QPushButton, QRadioButton, QSizePolicy, QVBoxLayout, QWidget)

from ..gui import theme
from . import APP_NAME, common as c

KEY_SITES = {"claude": "console.anthropic.com", "gemini": "aistudio.google.com"}


def _local_line() -> tuple[bool, str]:
    """(whether this copy can run a local model, what it would download for this Mac)."""
    import platform

    on = "the Mac's graphics" if platform.machine() == "arm64" else "this Mac's processor"  # (Intel: no Metal build)
    try:
        from ..intelligence import hardware, llama_engine, models

        if not llama_engine.backend_name():
            return False, "This copy of ClipMonster doesn't include the local AI."
        m = models.choose(hardware.detect())
        return True, (f"Free, private and offline: {m.name} runs on {on}. It downloads once "
                      f"({m.size_gb:.1f} GB), the first time it's asked.")
    except Exception:  # noqa: BLE001 - offered anyway; Settings → AI says more
        return True, f"Free, private and offline: a model runs on {on}. It downloads once."


class Welcome(QDialog):
    def __init__(self, settings, link, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Welcome to {APP_NAME}")
        self.setModal(True)
        self.setMinimumWidth(620)
        self.settings = settings
        self.result_settings = settings
        self.link_pc = False
        self.output = settings.output_dir

        v = QVBoxLayout(self)
        v.setContentsMargins(26, 22, 26, 20)
        v.setSpacing(10)
        v.setSizeConstraint(QLayout.SetNoConstraint)  # sized to its wrapped text below, not to a narrow guess
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(theme.app_icon_pixmap(128).scaled(56, 56, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        head.addWidget(logo)
        words = QVBoxLayout()
        words.addWidget(c.title(f"Welcome to {APP_NAME}"))
        words.addWidget(c.WrapLabel("It finds the best moments in YouTube videos and your own, and makes them into "
                                    "clips with captions, ready for TikTok, Shorts and Reels.", muted=True))
        head.addLayout(words, 1)
        v.addLayout(head)
        v.addSpacing(8)
        v.addWidget(c.h2("Which AI should pick the clips?"))

        local_ok, local_text = _local_line()
        pc_text = ("Free and private, and the Mac stays cool: the model on your PC answers. "
                   + ("Your PC is linked." if link.linked else "Link your PC after this, under Your PC in the sidebar."))
        choices = [
            ("claude", "Claude", "The best at picking clips and writing their titles. Paid per use (about a cent "
                                 "a video) with an API key."),
            ("gemini", "Gemini", "Google's AI: very good, with a free allowance. Needs an API key."),
            ("local", "Local AI on this Mac", local_text),
            ("pc", "Your PC's local AI", pc_text),
            ("none", "No AI for now", "Clips are picked from the video itself: its most-replayed moments, loud parts, "
                                      "laughter and scene changes."),
        ]
        self.group = QButtonGroup(self)
        self.radios: dict[str, QRadioButton] = {}
        self.keys: dict[str, QLineEdit] = {}
        for key, label, text in choices:
            box = QFrame()
            box.setObjectName("panel")
            box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            bv = QVBoxLayout(box)
            bv.setContentsMargins(14, 10, 14, 10)
            bv.setSpacing(4)
            r = QRadioButton(label)
            r.setStyleSheet("font-weight: 600;")
            self.group.addButton(r)
            self.radios[key] = r
            bv.addWidget(r)
            line = c.WrapLabel(text, muted=True)
            line.setContentsMargins(24, 0, 0, 0)
            bv.addWidget(line)
            if key in KEY_SITES:
                field = QLineEdit(getattr(settings, "ai_api_key" if key == "claude" else "gemini_api_key", ""))
                field.setEchoMode(QLineEdit.Password)
                field.setPlaceholderText(f"Paste your {label} API key (from {KEY_SITES[key]})")
                field.textEdited.connect(lambda _t, r=r: r.setChecked(True))
                wrap = QWidget()
                wh = QHBoxLayout(wrap)
                wh.setContentsMargins(24, 2, 0, 0)
                wh.addWidget(field)
                bv.addWidget(wrap)
                self.keys[key] = field
            if key == "local" and not local_ok:
                r.setEnabled(False)
            v.addWidget(box)
        start = "pc" if link.linked else settings.ai_provider if settings.mode == "ai" else "none"
        (self.radios.get(start) or self.radios["none"]).setChecked(True)
        self.group.buttonToggled.connect(lambda *_: self._update())

        row = QHBoxLayout()
        self.out_label = c.WrapLabel("", muted=True)
        change = QPushButton("Change…")
        change.clicked.connect(self._pick_folder)
        row.addWidget(self.out_label, 1)
        row.addWidget(change)
        v.addLayout(row)
        self.msg = c.WrapLabel("")
        self.msg.hide()
        v.addWidget(self.msg)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        later = QPushButton("Later")
        later.clicked.connect(self.reject)
        self.go = QPushButton("Start")
        self.go.setObjectName("primary")
        self.go.setDefault(True)
        self.go.clicked.connect(self.accept)
        buttons.addWidget(later)
        buttons.addWidget(self.go)
        v.addStretch(1)
        v.addLayout(buttons)
        self._update()
        self.resize(660, self.layout().totalHeightForWidth(660))

    def chosen(self) -> str:
        return next((k for k, r in self.radios.items() if r.isChecked()), "none")

    def _update(self) -> None:
        home = str(Path.home())
        shown = self.output.replace(home, "~", 1) if self.output.startswith(home) else self.output
        self.out_label.setText(f"Clips are saved in <b>{c.esc(shown)}</b>")
        self.go.setText("Start and link my PC" if self.chosen() == "pc" and not self.parent_linked() else "Start")
        self.msg.hide()

    def parent_linked(self) -> bool:
        win = self.parent()
        return bool(getattr(getattr(win, "link", None), "linked", False))

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Save clips in", self.output)
        if folder:
            self.output = folder
            self._update()

    def accept(self) -> None:
        pick = self.chosen()
        changes = {"setup_done": True, "output_dir": self.output}
        if pick in KEY_SITES:
            key = self.keys[pick].text().strip()
            if not key:
                self.msg.setText(f"<span style='color:{theme.WARN}'>Paste the {self.radios[pick].text()} API key, "
                                 "or pick another way.</span>")
                self.msg.show()
                self.keys[pick].setFocus()
                return
            changes["ai_api_key" if pick == "claude" else "gemini_api_key"] = key
        if pick == "none":
            changes["mode"] = "auto"
        else:
            changes.update(ai_provider=pick, mode="ai")
        self.link_pc = pick == "pc" and not self.parent_linked()
        self.result_settings = self.settings.apply(changes)
        super().accept()

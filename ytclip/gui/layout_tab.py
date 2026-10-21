"""The Layout side tab: text banners and a stacked second video, with a live sketch and a real-frame preview."""

from __future__ import annotations

import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .. import layout
from ..analyze import Clip
from ..export import export_clip
from ..export import frame as export_frame
from ..ffmpeg import MediaInfo
from ..settings import (APP_NAME, LAYOUT_PRESETS, SCHEMA, Settings, all_layouts, delete_user_layout,
                        load_user_layouts, save_user_layout)
from . import forms, qt_fonts, theme


class LayoutSketch(QWidget):
    """A to-scale drawing of the export frame. Shows a rendered frame instead once one is ready."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.plan: layout.Plan | None = None
        self.settings: Settings | None = None
        self.top_text = self.bottom_text = ""
        self.still: QPixmap | None = None
        self.show_safe = True

    def show_plan(self, plan: layout.Plan | None, s: Settings, top: str, bottom: str) -> None:
        self.plan, self.settings, self.top_text, self.bottom_text = plan, s, top, bottom
        self.still = None
        self.update()

    def show_still(self, pix: QPixmap) -> None:
        self.still = pix
        self.update()

    def paintEvent(self, _e) -> None:
        g = QPainter(self)
        g.setRenderHint(QPainter.Antialiasing)
        g.fillRect(self.rect(), QColor("#07080C"))
        p, s = self.plan, self.settings
        if p is None or s is None:
            g.setPen(QColor(theme.MUTED))
            g.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap,
                       "Turn on a banner or a stacked video below to design the frame")
            return
        k = min((self.width() - 16) / p.width, (self.height() - 16) / p.height)
        fw, fh = p.width * k, p.height * k
        ox, oy = (self.width() - fw) / 2, (self.height() - fh) / 2

        def rect(b: layout.Box) -> QRectF:
            return QRectF(ox + b.x * k, oy + b.y * k, b.w * k, b.h * k)

        frame = QRectF(ox, oy, fw, fh)
        if self.still is not None:
            g.drawPixmap(frame, self.still, QRectF(self.still.rect()))
            if p.safe is not None and self.show_safe:
                self._safe_zone(g, frame, rect(p.safe))
        else:
            g.fillRect(frame, QColor(s.banner_bg))
            g.fillRect(rect(p.main), QColor("#2B2350"))
            self._label(g, rect(p.main), "YOUR CLIP", QColor("#B9A8FF"))
            if p.second is not None:
                g.fillRect(rect(p.second), QColor("#173A33"))
                name = Path(s.stack_video).name if s.stack_video else "Choose a second video"
                self._label(g, rect(p.second), name, QColor("#7FE0C4"))
            for box, text_box, text in ((p.top, p.top_text, self.top_text),
                                        (p.bottom, p.bottom_text, self.bottom_text)):
                if box is None or text_box is None:
                    continue
                if p.bars:
                    g.fillRect(rect(box), QColor(s.banner_bg))
                else:
                    g.setPen(QPen(QColor(s.banner_color), 1, Qt.DashLine))
                    g.drawRect(rect(text_box).adjusted(1, 1, -1, -1))
                shown = (text.upper() if s.banner_uppercase else text) or "(no text)"
                # Same line breaks and size as the export, so the sketch is a fair preview.
                lines, size = layout.wrap(shown, int(s.banner_font_size * min(p.width, p.height) / 1080),
                                          text_box, s.banner_uppercase, s.banner_font)
                g.setFont(qt_fonts.qfont(s.banner_font, max(6, int(size * k))))
                g.setPen(QColor(s.banner_color))
                g.drawText(rect(text_box), Qt.AlignCenter, "\n".join(lines))
        if p.safe is not None and self.show_safe:
            self._safe_zone(g, frame, rect(p.safe))
        g.setPen(QPen(QColor(theme.ACCENT_2), 2))
        g.setBrush(Qt.NoBrush)
        g.drawRect(frame)

    @staticmethod
    def _safe_zone(g: QPainter, frame: QRectF, safe: QRectF) -> None:
        """Shade where TikTok draws its tabs, buttons and caption, and outline the safe area."""
        shade = QColor(255, 80, 80, 46)
        g.fillRect(QRectF(frame.left(), frame.top(), frame.width(), safe.top() - frame.top()), shade)
        g.fillRect(QRectF(frame.left(), safe.bottom(), frame.width(), frame.bottom() - safe.bottom()), shade)
        g.fillRect(QRectF(frame.left(), safe.top(), safe.left() - frame.left(), safe.height()), shade)
        g.fillRect(QRectF(safe.right(), safe.top(), frame.right() - safe.right(), safe.height()), shade)
        g.setPen(QPen(QColor(255, 120, 120, 200), 1, Qt.DashLine))
        g.setBrush(Qt.NoBrush)
        g.drawRect(safe)

    @staticmethod
    def _label(g: QPainter, r: QRectF, text: str, colour: QColor) -> None:
        f = QFont("Segoe UI")
        f.setPixelSize(max(9, int(min(r.height(), r.width()) / 9)))
        f.setBold(True)
        g.setFont(f)
        g.setPen(colour)
        g.drawText(r, Qt.AlignCenter | Qt.TextWordWrap, text)


class _StillBridge(QObject):
    done = Signal(int, str, str)  # generation, path, error


class LayoutTab(QWidget):
    """Edits the Layout settings (via on_change, like the other side tabs) and the selected clip's banner text."""

    clipChanged = Signal()  # the selected clip's banner text was edited
    presetChosen = Signal(str, dict)  # a top/bottom view preset to apply: name, layout values

    def __init__(self, settings: Settings, on_change, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.clip: Clip | None = None
        self.src: Path | None = None
        self.media: MediaInfo | None = None
        self.words: list = []
        self._gen = 0
        self._bridge = _StillBridge()
        self._bridge.done.connect(self._still_done)
        self._still_timer = QTimer(self)
        self._still_timer.setSingleShot(True)
        self._still_timer.setInterval(700)
        self._still_timer.timeout.connect(self.render_still)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)

        presets = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.setToolTip("Top and bottom views: banners and stacked videos in one click")
        self.preset.activated.connect(self._preset_picked)
        save = QPushButton("Save…")
        save.setToolTip("Save the current layout (banners, second video, fonts, colours) as your own preset")
        save.clicked.connect(self._save_preset)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setToolTip("Delete one of your saved layouts")
        self.delete_btn.clicked.connect(self._delete_preset)
        presets.addWidget(QLabel("View"))
        presets.addWidget(self.preset, 1)
        presets.addWidget(save)
        presets.addWidget(self.delete_btn)
        lay.addLayout(presets)
        self.fill_presets()

        self.sketch = LayoutSketch()
        lay.addWidget(self.sketch, 3)

        row = QHBoxLayout()
        self.auto_still = QCheckBox("Live frame")
        self.auto_still.setChecked(True)
        self.auto_still.setToolTip("Render a real frame of the selected clip whenever the layout changes")
        self.still_btn = QPushButton("Render frame")
        self.still_btn.setToolTip("Draw one frame of the selected clip exactly as it will export")
        self.still_btn.clicked.connect(self.render_still)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color:{theme.MUTED}")
        self.safe_box = QCheckBox("TikTok UI")
        self.safe_box.setChecked(True)
        self.safe_box.setToolTip("Shade the parts of the frame that TikTok's tabs, buttons and caption cover")
        self.safe_box.toggled.connect(self._toggle_safe)
        row.addWidget(self.auto_still)
        row.addWidget(self.safe_box)
        row.addWidget(self.status, 1)
        row.addWidget(self.still_btn)
        lay.addLayout(row)

        clip_box = QWidget()
        cl = QVBoxLayout(clip_box)
        cl.setContentsMargins(0, 4, 0, 4)
        head = QLabel("This clip's banner text")
        head.setObjectName("h2")
        cl.addWidget(head)
        self.top_edit = QLineEdit()
        self.bottom_edit = QLineEdit()
        for edit, which in ((self.top_edit, "top"), (self.bottom_edit, "bottom")):
            edit.setClearButtonEnabled(True)
            edit.textEdited.connect(lambda text, w=which: self._clip_text(w, text))
            cl.addWidget(edit)
        hint = QLabel("Leave blank to use the default text below. {title} = the clip title.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.MUTED}")
        cl.addWidget(hint)
        lay.addWidget(clip_box)

        fields = [f for f in SCHEMA if f.group == "Layout"]
        form, self.editors = forms.build_form(fields, settings, on_change, compact=True)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form)
        lay.addWidget(scroll, 4)
        self._refresh_clip_fields()
        self._redraw()

    # ------------------------------------------------------------ presets
    def fill_presets(self) -> None:
        self.preset.clear()
        self.preset.addItem("Pick a view…")
        for name in LAYOUT_PRESETS:
            self.preset.addItem(name, name)
        mine = load_user_layouts()
        if mine:
            self.preset.insertSeparator(self.preset.count())
            for name in mine:
                self.preset.addItem(f"★ {name}", name)
        self.delete_btn.setEnabled(bool(mine))

    def _preset_picked(self, index: int) -> None:
        name = self.preset.itemData(index)
        self.preset.setCurrentIndex(0)
        values = all_layouts().get(name) if name else None
        if values is not None:
            self.presetChosen.emit(name, dict(values))

    def _save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Save layout", "Name for this top/bottom view:")
        name = name.strip()
        if not ok or not name:
            return
        if name in LAYOUT_PRESETS:
            QMessageBox.information(self, APP_NAME, f"“{name}” is a built-in layout. Pick another name.")
            return
        if name in load_user_layouts() and QMessageBox.question(
                self, APP_NAME, f"Replace your saved layout “{name}”?") != QMessageBox.Yes:
            return
        save_user_layout(name, self.settings)
        self.fill_presets()
        self.status.setText(f"Saved layout “{name}”")

    def _delete_preset(self) -> None:
        mine = list(load_user_layouts())
        if not mine:
            return
        name, ok = QInputDialog.getItem(self, "Delete layout", "Delete which saved layout?", mine, 0, False)
        if ok and name:
            delete_user_layout(name)
            self.fill_presets()

    # ------------------------------------------------------------ inputs from the main window
    def set_settings(self, s: Settings, refresh_editors: bool = False) -> None:
        self.settings = s
        if refresh_editors:
            forms.refresh(self.editors, s)
        self._refresh_clip_fields()
        self._redraw()

    def set_clip(self, clip: Clip | None, src: Path | None = None, media: MediaInfo | None = None,
                 words: list | None = None) -> None:
        self.clip, self.src, self.media, self.words = clip, src, media, words or []
        self._refresh_clip_fields()
        self._redraw()

    # ------------------------------------------------------------ internals
    def _refresh_clip_fields(self) -> None:
        s, c = self.settings, self.clip
        title = c.title if c else ""
        for edit, on, template, value in (
            (self.top_edit, s.banner_top, s.banner_top_text, c.top_text if c else None),
            (self.bottom_edit, s.banner_bottom, s.banner_bottom_text, c.bottom_text if c else None),
        ):
            edit.setEnabled(c is not None and on)
            default = layout.banner_text(template, title)
            where = "Top" if edit is self.top_edit else "Bottom"
            if not on:
                edit.setPlaceholderText(f"{where} banner is off")
            else:
                edit.setPlaceholderText(f"{where}: {default or '(blank - this clip has no title yet)'}")
            if edit.text() != (value or ""):
                edit.setText(value or "")

    def _clip_text(self, which: str, text: str) -> None:
        if self.clip is None:
            return
        setattr(self.clip, f"{which}_text", text if text.strip() else None)
        self.clip.edited = True
        self.clipChanged.emit()
        self._redraw()

    def _toggle_safe(self, on: bool) -> None:
        self.sketch.show_safe = on
        self.sketch.update()

    def _frame_size(self) -> tuple[int, int]:
        media = self.media or MediaInfo(width=1920, height=1080)
        _, w, h = export_frame(self.settings, media)
        return w, h

    def _redraw(self) -> None:
        s = self.settings
        self._gen += 1  # any in-flight still is now out of date
        w, h = self._frame_size()
        plan = layout.plan(s, w, h)
        title = self.clip.title if self.clip else "Your clip title"
        top = layout.banner_text(s.banner_top_text, title, self.clip.top_text if self.clip else None)
        bottom = layout.banner_text(s.banner_bottom_text, title, self.clip.bottom_text if self.clip else None)
        self.sketch.show_plan(plan, s, top, bottom)
        can_render = self.clip is not None and self.src is not None and self.media is not None
        self.still_btn.setEnabled(can_render)
        self.status.setText(f"{w}×{h}" + ("" if can_render else " · select a clip to see a real frame"))
        if can_render and self.auto_still.isChecked() and self.isVisible():
            self._still_timer.start()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._redraw()

    def render_still(self) -> None:
        if self.clip is None or self.src is None or self.media is None:
            return
        self._gen += 1
        gen = self._gen
        clip, s = replace(self.clip), self.settings.copy()
        src, media, words = self.src, self.media, list(self.words)
        dest = Path(tempfile.gettempdir()) / f"{APP_NAME}_layout_{gen}.jpg"
        self.status.setText("Rendering frame…")

        def work() -> None:
            try:
                export_clip(src, media, dest, clip, words, s, still_at=min(1.5, clip.length / 2))
                self._bridge.done.emit(gen, str(dest), "")
            except Exception as exc:  # shown in the tab; a still is never worth a dialog
                self._bridge.done.emit(gen, "", str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _still_done(self, gen: int, path: str, error: str) -> None:
        if gen != self._gen:  # the layout changed while this frame was rendering
            if path:
                Path(path).unlink(missing_ok=True)
            return
        if error:
            self.status.setText(error.splitlines()[0][:120])
            self.status.setToolTip(error)
            return
        pix = QPixmap(path)
        Path(path).unlink(missing_ok=True)
        if not pix.isNull():
            self.sketch.show_still(pix)
            w, h = self._frame_size()
            self.status.setText(f"{w}×{h}")
            self.status.setToolTip("")

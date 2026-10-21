"""The Layout side tab: text banners and a stacked second video, with a live sketch and a real-frame preview."""

from __future__ import annotations

import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from .. import layout
from ..analyze import Clip
from ..export import export_clip, output_size
from ..ffmpeg import MediaInfo
from ..settings import APP_NAME, SCHEMA, Settings
from . import forms, theme


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
        else:
            g.fillRect(frame, QColor(s.banner_bg))
            g.fillRect(rect(p.main), QColor("#2B2350"))
            self._label(g, rect(p.main), "YOUR CLIP", QColor("#B9A8FF"))
            if p.second is not None:
                g.fillRect(rect(p.second), QColor("#173A33"))
                name = Path(s.stack_video).name if s.stack_video else "Choose a second video"
                self._label(g, rect(p.second), name, QColor("#7FE0C4"))
            for box, text in ((p.top, self.top_text), (p.bottom, self.bottom_text)):
                if box is None:
                    continue
                r = rect(box)
                if p.bars:
                    g.fillRect(r, QColor(s.banner_bg))
                else:
                    g.setPen(QPen(QColor(s.banner_color), 1, Qt.DashLine))
                    g.drawRect(r.adjusted(1, 1, -1, -1))
                shown = text.upper() if s.banner_uppercase else text
                f = QFont(s.banner_font)
                f.setBold(True)
                f.setPixelSize(max(7, int(s.banner_font_size * min(p.width, p.height) / 1080 * k)))
                g.setFont(f)
                g.setPen(QColor(s.banner_color))
                g.drawText(r.adjusted(4, 2, -4, -2), Qt.AlignCenter | Qt.TextWordWrap, shown or "(no text)")
        g.setPen(QPen(QColor(theme.ACCENT_2), 2))
        g.setBrush(Qt.NoBrush)
        g.drawRect(frame)

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
        row.addWidget(self.auto_still)
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

    def _frame_size(self) -> tuple[int, int]:
        media = self.media or MediaInfo(width=1920, height=1080)
        return output_size(layout.frame_aspect(self.settings), self.settings.resolution, media)

    def _redraw(self) -> None:
        s = self.settings
        self._gen += 1  # any in-flight still is now out of date
        if not layout.active(s):
            self.sketch.show_plan(None, s, "", "")
            self.status.setText("")
            self.still_btn.setEnabled(False)
            return
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
        if self.clip is None or self.src is None or self.media is None or not layout.active(self.settings):
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
            self.status.setText(f"{w}×{h} · real frame")
            self.status.setToolTip("")

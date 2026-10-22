"""Video preview. Two ways to look at a clip:

- Source view: the whole video, with the part a vertical/square export keeps outlined; drag to reframe.
- Export view: the finished frame as it will be exported, playing live: the reframed clip, blurred fill,
  stacked second video, bars, banner words, captions and title, with TikTok's safe zone on request.
  It's drawn from the same numbers the export uses (ytclip.compose), so what you see is what renders."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QRectF, QSizeF, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import QGraphicsItem, QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView

from .. import layout
from ..compose import Composition
from . import theme
from .compositor import FramePainter

FRAME_RATIOS = {"vertical_crop": 9 / 16, "square": 1.0}


class _FrameSource:
    """The latest decoded frame of one player, turned into a QImage only when it's drawn."""

    def __init__(self):
        self.frame: QVideoFrame | None = None
        self._image: QImage | None = None

    def push(self, frame: QVideoFrame) -> None:
        if frame.isValid():
            self.frame, self._image = frame, None

    def clear(self) -> None:
        self.frame, self._image = None, None

    @property
    def time(self) -> float | None:
        """Media time of the frame, in seconds."""
        if self.frame is None or self.frame.startTime() < 0:
            return None
        return self.frame.startTime() / 1_000_000

    def image(self) -> QImage | None:
        if self._image is None and self.frame is not None:
            img = self.frame.toImage()
            self._image = img if not img.isNull() else None
        return self._image


class _ExportItem(QGraphicsItem):
    """The export frame, in output pixels (the view scales it to fit)."""

    def __init__(self, preview: "Preview"):
        super().__init__()
        self.preview = preview
        self.setZValue(1)

    def boundingRect(self) -> QRectF:
        c = self.preview.comp
        return QRectF(0, 0, c.width, c.height) if c else QRectF(0, 0, 1080, 1920)

    def paint(self, g: QPainter, option, widget=None) -> None:
        self.preview._paint_export(g)


class Preview(QGraphicsView):
    cropChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setBackgroundBrush(QColor("#07080C"))
        self.setMinimumHeight(220)

        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        self.video = QGraphicsVideoItem()
        self.video.setSize(QSizeF(1280, 720))
        self.scene_.addItem(self.video)
        self.video.nativeSizeChanged.connect(self._native_size)

        dim = QBrush(QColor(0, 0, 0, 160))
        self.mask_l = self._rect(dim, Qt.NoPen)
        self.mask_r = self._rect(dim, Qt.NoPen)
        self.frame = self._rect(Qt.NoBrush, QPen(QColor(theme.ACCENT_2), 3))
        self.hint = QGraphicsSimpleTextItem("")
        self.hint.setBrush(QColor("white"))
        self.hint.setFont(QFont("Segoe UI", 14, QFont.DemiBold))
        self.hint.setZValue(5)
        self.scene_.addItem(self.hint)

        self.placeholder = QGraphicsSimpleTextItem("Paste a YouTube link above or drop video files here")
        self.placeholder.setBrush(QColor(theme.MUTED))
        self.placeholder.setFont(QFont("Segoe UI", 22))
        self.scene_.addItem(self.placeholder)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.mediaStatusChanged.connect(self._media_status)

        # Export view: frames come from the video item's sink, the stacked video from its own player.
        self.main_src = _FrameSource()
        self.video.videoSink().videoFrameChanged.connect(self._main_frame)
        self.second_src = _FrameSource()
        self.second_sink = QVideoSink(self)
        self.second_sink.videoFrameChanged.connect(self._second_frame)
        self.second_player = QMediaPlayer(self)
        self.second_audio = QAudioOutput(self)
        self.second_player.setAudioOutput(self.second_audio)
        self.second_player.setVideoSink(self.second_sink)
        self.second_player.setLoops(QMediaPlayer.Infinite)
        self.second_player.durationChanged.connect(lambda _: self._sync_second(force=True))
        self.player.playbackStateChanged.connect(lambda _: self._sync_second(force=True))
        self.player.positionChanged.connect(lambda _: self._sync_second())
        self._second_path = ""
        self._second_timing = (0.0, 0.0, "random", 0.0)  # clip start, clip length, start mode, fixed offset
        self._volume = 0.8
        self._stack_volume = 0.0

        self.export_item = _ExportItem(self)
        self.export_item.hide()
        self.scene_.addItem(self.export_item)
        self.export_view = False
        self.comp: Composition | None = None
        self.clip_span = (0.0, 0.0)
        self.show_safe = False
        self.painter = FramePainter()
        self._drag_from: tuple[float, float] | None = None  # (scene x, crop) when a drag started

        self.aspect = "original"
        self.ratio: float | None = None
        self.crop_x = 0.5
        self._dragging = False
        self._layout_overlay()

    def _rect(self, brush, pen) -> QGraphicsRectItem:
        item = QGraphicsRectItem()
        item.setBrush(brush)
        item.setPen(pen)
        item.setZValue(2)
        self.scene_.addItem(item)
        return item

    # ------------------------------------------------------------ media
    def load(self, path: Path | None) -> None:
        self.player.stop()
        self.main_src.clear()
        self.painter.reset()
        if path is None:
            self.player.setSource(QUrl())
            self.placeholder.show()
        else:
            self.player.setSource(QUrl.fromLocalFile(str(path)))
            self.placeholder.hide()
            self.player.pause()  # shows the first frame
        self._layout_overlay()

    def _media_status(self, status) -> None:
        # A freshly loaded file shows nothing until something is decoded; re-seeking
        # (to wherever the editor already asked for) paints that frame.
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self.player.setPosition(max(1, self.player.position()))

    def _native_size(self, size: QSizeF) -> None:
        if size.width() > 0 and size.height() > 0:
            self.video.setSize(size)
            self._layout_overlay()

    def set_volume(self, v: float) -> None:
        self._volume = v
        self.audio.setVolume(v)
        self.second_audio.setVolume(v * self._stack_volume)

    # ------------------------------------------------------------ export view
    def set_export_view(self, on: bool) -> None:
        self.export_view = on
        self._layout_overlay()
        self._sync_second(force=True)

    def set_composition(self, comp: Composition | None, clip_start: float = 0.0, clip_end: float = 0.0) -> None:
        """What the export view draws: the selected clip's frame layout, banner words and captions."""
        self.comp = comp
        self.clip_span = (clip_start, clip_end)
        self.export_item.prepareGeometryChange()
        self.painter.reset()
        self._layout_overlay()

    def set_safe_zone(self, on: bool) -> None:
        self.show_safe = on
        self.export_item.update()

    def set_second(self, path: str, clip_start: float, clip_len: float, mode: str, fixed: float,
                   volume: int) -> None:
        """The stacked video, timed the way the export reads it: from layout.second_offset at the clip's
        start, looping. `volume` is the layout's 0-100 mix level for its sound."""
        timing = (clip_start, clip_len, mode, fixed)
        changed = timing != self._second_timing or path != self._second_path
        self._second_timing = timing
        self._stack_volume = max(0, volume) / 100
        self.second_audio.setVolume(self._volume * self._stack_volume)
        if path != self._second_path:
            self._second_path = path
            self.second_src.clear()
            self.second_player.stop()
            self.second_player.setSource(QUrl.fromLocalFile(path) if path else QUrl())
            if path:
                self.second_player.pause()
        self._sync_second(force=changed)

    def _second_wanted(self) -> bool:
        return bool(self.exporting and self._second_path
                    and any(p.source == "second" for p in self.comp.layers))

    def _second_position(self) -> float | None:
        dur = self.second_player.duration() / 1000
        if dur <= 0:
            return None
        start, length, mode, fixed = self._second_timing
        off = layout.second_offset(start, length, dur, mode, fixed)
        return (off + max(0.0, self.player.position() / 1000 - start)) % dur

    def _sync_second(self, force: bool = False) -> None:
        """Keep the stacked video in step with the clip: same play state, position within a few frames."""
        sp = self.second_player
        if not self._second_wanted():
            if sp.playbackState() == QMediaPlayer.PlayingState:
                sp.pause()
            return
        want = self._second_position()
        if want is None:
            return
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        drift = abs(sp.position() / 1000 - want)
        if force or drift > (0.25 if playing else 0.03):
            sp.setPosition(int(want * 1000))
        if playing and sp.playbackState() != QMediaPlayer.PlayingState:
            sp.play()
        elif not playing and sp.playbackState() == QMediaPlayer.PlayingState:
            sp.pause()

    def _main_frame(self, frame: QVideoFrame) -> None:
        self.main_src.push(frame)  # kept even when hidden, so switching views shows the frame at once
        if self.exporting:
            self.export_item.update()

    def _second_frame(self, frame: QVideoFrame) -> None:
        self.second_src.push(frame)  # kept even when hidden, so switching views shows the frame at once
        if self.exporting:
            self.export_item.update()

    def _clip_time(self) -> float | None:
        """Seconds into the selected clip of the frame on screen, or None outside the clip."""
        t = self.main_src.time
        if t is None:
            t = self.player.position() / 1000
        start, end = self.clip_span
        return t - start if start <= t < end else None

    def _paint_export(self, g: QPainter) -> None:
        c = self.comp
        if c is None:
            return
        frames = {"main": self.main_src.image(), "second": self.second_src.image()}
        sizes = {}
        native = self.video.nativeSize()
        if native.width() > 0:
            sizes["main"] = (int(native.width()), int(native.height()))
        if self.second_src.frame is not None:
            sizes["second"] = (self.second_src.frame.width(), self.second_src.frame.height())
        self.painter.paint(g, c, frames, self._clip_time(), sizes, clock=time.monotonic(), show_safe=self.show_safe)

    # ------------------------------------------------------------ source view overlay
    def set_aspect(self, aspect: str, ratio: float | None = None) -> None:
        """`ratio` overrides the kept width/height, e.g. for the clip's half of a stacked layout."""
        self.aspect = aspect
        self.ratio = ratio
        self._layout_overlay()

    @property
    def _ratio(self) -> float | None:
        return self.ratio if self.ratio is not None else FRAME_RATIOS.get(self.aspect)

    def set_crop(self, x: float) -> None:
        self.crop_x = min(1.0, max(0.0, x))
        self._layout_overlay()

    def _video_rect(self) -> QRectF:
        return QRectF(self.video.pos(), self.video.size())

    @property
    def exporting(self) -> bool:
        """True when the export view is on and there's a clip to show in it."""
        return self.export_view and self.comp is not None and self.player.source().isValid()

    def _view_rect(self) -> QRectF:
        if self.exporting:
            return QRectF(0, 0, self.comp.width, self.comp.height)
        return self._video_rect()

    def _layout_overlay(self) -> None:
        export = self.exporting
        self.export_item.setVisible(export)
        self.video.setVisible(not export)
        if export:
            for item in (self.mask_l, self.mask_r, self.frame):
                item.hide()
            fr = self._view_rect()
            self.scene_.setSceneRect(fr)
            self.placeholder.hide()
            self.hint.setVisible(self.comp.reframe is not None and self.underMouse())  # keep the frame clean
            if self.hint.isVisible():
                k = fr.height() / 720
                self.hint.setText("⇆ drag to reframe")
                self.hint.setScale(k)
                self.hint.setPos(12 * k, 10 * k)
            self.fitInView(fr, Qt.KeepAspectRatio)
            self.export_item.update()
            return
        self.placeholder.setVisible(not self.player.source().isValid())
        vr = self._video_rect()
        self.scene_.setSceneRect(vr)
        pr = self.placeholder.boundingRect()
        self.placeholder.setPos(vr.center().x() - pr.width() / 2, vr.center().y() - pr.height() / 2)
        ratio = self._ratio
        show = ratio is not None and not self.placeholder.isVisible()
        for item in (self.mask_l, self.mask_r, self.frame):
            item.setVisible(show)
        self.hint.setVisible(show)
        if show:
            w = min(vr.width(), vr.height() * ratio)
            x = vr.left() + (vr.width() - w) * self.crop_x
            self.mask_l.setRect(QRectF(vr.left(), vr.top(), x - vr.left(), vr.height()))
            self.mask_r.setRect(QRectF(x + w, vr.top(), vr.right() - x - w, vr.height()))
            self.frame.setRect(QRectF(x, vr.top(), w, vr.height()))
            self.hint.setText("⇆ drag to reframe")
            self.hint.setScale(vr.height() / 720)
            self.hint.setPos(x + 12 * vr.height() / 720, vr.top() + 10 * vr.height() / 720)
        self.fitInView(vr, Qt.KeepAspectRatio)

    def enterEvent(self, e) -> None:
        super().enterEvent(e)
        if self.exporting:
            self._layout_overlay()

    def leaveEvent(self, e) -> None:
        super().leaveEvent(e)
        if self.exporting:
            self._layout_overlay()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.fitInView(self._view_rect(), Qt.KeepAspectRatio)

    def _crop_from(self, view_x: float) -> None:
        vr = self._video_rect()
        ratio = self._ratio
        if ratio is None:
            return
        w = min(vr.width(), vr.height() * ratio)
        free = vr.width() - w
        if free <= 0:
            return
        sx = self.mapToScene(int(view_x), 0).x()
        self.set_crop((sx - w / 2 - vr.left()) / free)
        self.cropChanged.emit(self.crop_x)

    def _drag_export(self, view_x: float) -> None:
        """Slide the picture under the frame: dragging right shows more of the left of the video."""
        r = self.comp.reframe if self.comp else None
        if r is None or self._drag_from is None:
            return
        x0, crop0 = self._drag_from
        dx = self.mapToScene(int(view_x), 0).x() - x0
        self.crop_x = min(1.0, max(0.0, crop0 - dx / r.slide))
        self.cropChanged.emit(self.crop_x)

    def mousePressEvent(self, e) -> None:
        if self.exporting and self.comp.reframe is not None and e.button() == Qt.LeftButton:
            self._drag_from = (self.mapToScene(int(e.position().x()), 0).x(), self.crop_x)
            self.setCursor(Qt.ClosedHandCursor)
        elif self.frame.isVisible() and e.button() == Qt.LeftButton:
            self._dragging = True
            self._crop_from(e.position().x())
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._drag_from is not None:
            self._drag_export(e.position().x())
        elif self._dragging:
            self._crop_from(e.position().x())
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        self._dragging = False
        if self._drag_from is not None:
            self._drag_from = None
            self.unsetCursor()
        super().mouseReleaseEvent(e)

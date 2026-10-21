"""Video preview with an export-frame overlay you can drag to reposition vertical/square crops."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSizeF, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView

from . import theme

FRAME_RATIOS = {"vertical_crop": 9 / 16, "square": 1.0}


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

        self.aspect = "original"
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

    # ------------------------------------------------------------ overlay
    def set_aspect(self, aspect: str) -> None:
        self.aspect = aspect
        self._layout_overlay()

    def set_crop(self, x: float) -> None:
        self.crop_x = min(1.0, max(0.0, x))
        self._layout_overlay()

    def _video_rect(self) -> QRectF:
        return QRectF(self.video.pos(), self.video.size())

    def _layout_overlay(self) -> None:
        vr = self._video_rect()
        self.scene_.setSceneRect(vr)
        pr = self.placeholder.boundingRect()
        self.placeholder.setPos(vr.center().x() - pr.width() / 2, vr.center().y() - pr.height() / 2)
        ratio = FRAME_RATIOS.get(self.aspect)
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

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.fitInView(self._video_rect(), Qt.KeepAspectRatio)

    def _crop_from(self, view_x: float) -> None:
        vr = self._video_rect()
        ratio = FRAME_RATIOS.get(self.aspect)
        if ratio is None:
            return
        w = min(vr.width(), vr.height() * ratio)
        free = vr.width() - w
        if free <= 0:
            return
        sx = self.mapToScene(int(view_x), 0).x()
        self.set_crop((sx - w / 2 - vr.left()) / free)
        self.cropChanged.emit(self.crop_x)

    def mousePressEvent(self, e) -> None:
        if self.frame.isVisible() and e.button() == Qt.LeftButton:
            self._dragging = True
            self._crop_from(e.position().x())
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._dragging:
            self._crop_from(e.position().x())
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        self._dragging = False
        super().mouseReleaseEvent(e)

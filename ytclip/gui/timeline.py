"""Interactive timeline: interest curve, per-signal lines, draggable clips, playhead, zoom."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu, QWidget

from ..analyze import Clip
from ..organize import stamp
from . import theme

L_PAD, R_PAD, RULER, LANE = 10, 10, 22, 30
EDGE_GRAB = 6
TICK_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)


class Timeline(QWidget):
    seekRequested = Signal(float)
    clipSelected = Signal(int)
    clipLive = Signal(int)  # while dragging
    clipChanged = Signal(int)  # drag finished
    addClipRequested = Signal(float)
    deleteClipRequested = Signal(int)
    playClipRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.duration = 0.0
        self.signals: dict[str, np.ndarray] = {}
        self.score = np.zeros(0)
        self.clips: list[Clip] = []
        self.selected = -1
        self.playhead = 0.0
        self.visible = set(theme.SIGNAL_COLOURS)
        self.v0, self.v1 = 0.0, 1.0
        self._drag: tuple | None = None
        self._hover: float | None = None

    # ------------------------------------------------------------ data
    def set_data(self, duration: float, signals: dict, score: np.ndarray, clips: list[Clip]) -> None:
        self.duration = max(1.0, duration)
        self.v0, self.v1 = 0.0, self.duration
        self.update_data(signals, score, clips)

    def update_data(self, signals: dict, score: np.ndarray, clips: list[Clip]) -> None:
        self.signals, self.score, self.clips = signals, score, clips
        if self.selected >= len(clips):
            self.selected = -1
        self.update()

    def clear(self) -> None:
        self.set_data(1.0, {}, np.zeros(0), [])
        self.duration = 0.0

    def set_playhead(self, t: float) -> None:
        self.playhead = t
        # Keep the playhead in view when zoomed in.
        span = self.v1 - self.v0
        if span < self.duration and not (self.v0 <= t <= self.v1):
            self.v0 = max(0.0, min(self.duration - span, t - span * 0.1))
            self.v1 = self.v0 + span
        self.update()

    def set_selected(self, i: int) -> None:
        self.selected = i
        self.update()

    def zoom_to(self, start: float, end: float, margin: float = 0.25) -> None:
        pad = (end - start) * margin
        self.v0, self.v1 = max(0.0, start - pad), min(self.duration, end + pad)
        self.update()

    def zoom_all(self) -> None:
        self.v0, self.v1 = 0.0, self.duration
        self.update()

    # ------------------------------------------------------------ geometry
    def _plot(self) -> QRectF:
        return QRectF(L_PAD, RULER + 4, self.width() - L_PAD - R_PAD, self.height() - RULER - LANE - 10)

    def _lane(self) -> QRectF:
        return QRectF(L_PAD, self.height() - LANE - 2, self.width() - L_PAD - R_PAD, LANE - 4)

    def x_of(self, t: float) -> float:
        plot = self._plot()
        return plot.left() + (t - self.v0) / max(1e-6, self.v1 - self.v0) * plot.width()

    def t_of(self, x: float) -> float:
        plot = self._plot()
        t = self.v0 + (x - plot.left()) / max(1.0, plot.width()) * (self.v1 - self.v0)
        return min(self.duration, max(0.0, t))

    # ------------------------------------------------------------ painting
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.PANEL))
        if self.duration <= 0:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "The interest timeline appears here once a video is analysed")
            return
        plot, lane = self._plot(), self._lane()
        self._paint_ruler(p, plot)
        self._paint_clip_spans(p, plot)
        self._paint_curves(p, plot)
        self._paint_lane(p, lane)
        # Playhead
        x = self.x_of(self.playhead)
        p.setPen(QPen(QColor("#FFFFFF"), 1.5))
        p.drawLine(QPointF(x, RULER - 2), QPointF(x, self.height() - 2))
        tri = QPainterPath()
        tri.moveTo(x - 5, RULER - 8)
        tri.lineTo(x + 5, RULER - 8)
        tri.lineTo(x, RULER - 2)
        p.fillPath(tri, QColor("#FFFFFF"))
        # Hover readout
        if self._hover is not None:
            hx = self.x_of(self._hover)
            p.setPen(QPen(QColor(255, 255, 255, 60), 1, Qt.DashLine))
            p.drawLine(QPointF(hx, plot.top()), QPointF(hx, plot.bottom()))
            idx = min(len(self.score) - 1, int(self._hover))
            val = f"  ·  interest {self.score[idx] * 100:.0f}" if idx >= 0 and len(self.score) else ""
            p.setPen(QColor(theme.TEXT))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(QPointF(min(hx + 6, self.width() - 150), plot.top() + 12), f"{stamp(self._hover)}{val}")

    def _paint_ruler(self, p: QPainter, plot: QRectF) -> None:
        span = self.v1 - self.v0
        step = next((s for s in TICK_STEPS if s / span * plot.width() >= 70), TICK_STEPS[-1])
        p.setFont(QFont("Segoe UI", 8))
        t = (self.v0 // step) * step
        while t <= self.v1:
            if t >= self.v0:
                x = self.x_of(t)
                p.setPen(QPen(QColor(theme.BORDER), 1))
                p.drawLine(QPointF(x, RULER), QPointF(x, plot.bottom()))
                p.setPen(QColor(theme.MUTED))
                p.drawText(QPointF(x + 3, RULER - 8), stamp(t))
            t += step

    def _samples(self, arr: np.ndarray, plot: QRectF) -> tuple[np.ndarray, np.ndarray]:
        """Max-pool a per-second array into one value per pixel column."""
        n_px = max(2, int(plot.width()))
        edges = np.linspace(self.v0, self.v1, n_px + 1)
        idx = np.clip(edges.astype(int), 0, len(arr) - 1)
        starts, ends = idx[:-1], np.maximum(idx[1:], idx[:-1] + 1)
        csum = np.maximum.reduceat(arr, starts) if len(arr) else np.zeros(n_px)
        vals = np.where(ends - starts > 1, csum, arr[starts])
        vals[-1] = arr[starts[-1] : ends[-1]].max()  # reduceat's last bin runs to the array end
        xs = plot.left() + np.arange(n_px) * plot.width() / n_px
        return xs, vals

    def _paint_curves(self, p: QPainter, plot: QRectF) -> None:
        if len(self.score):
            xs, vals = self._samples(self.score, plot)
            path = QPainterPath(QPointF(xs[0], plot.bottom()))
            for x, v in zip(xs, vals):
                path.lineTo(QPointF(x, plot.bottom() - v * plot.height()))
            path.lineTo(QPointF(xs[-1], plot.bottom()))
            path.closeSubpath()
            grad = QLinearGradient(0, plot.top(), 0, plot.bottom())
            grad.setColorAt(0, QColor(139, 92, 246, 170))
            grad.setColorAt(1, QColor(139, 92, 246, 20))
            p.fillPath(path, QBrush(grad))
            p.setPen(QPen(QColor(theme.ACCENT), 1.5))
            p.drawPath(path)
        for key, arr in self.signals.items():
            if key not in self.visible or not len(arr):
                continue
            xs, vals = self._samples(arr, plot)
            path = QPainterPath(QPointF(xs[0], plot.bottom() - vals[0] * plot.height()))
            for x, v in zip(xs[1:], vals[1:]):
                path.lineTo(QPointF(x, plot.bottom() - v * plot.height()))
            col = QColor(theme.SIGNAL_COLOURS.get(key, theme.MUTED))
            col.setAlpha(150)
            p.setPen(QPen(col, 1))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

    def _clip_colour(self, i: int, c: Clip, alpha: int) -> QColor:
        if not c.enabled:
            return QColor(120, 120, 140, alpha // 2)
        col = QColor(theme.ACCENT_2 if c.origin == "ai" else theme.GOOD if c.origin == "manual" else theme.ACCENT)
        col.setAlpha(min(255, alpha * 2) if i == self.selected else alpha)
        return col

    def _paint_clip_spans(self, p: QPainter, plot: QRectF) -> None:
        for i, c in enumerate(self.clips):
            x0, x1 = self.x_of(c.start), self.x_of(c.end)
            if x1 < plot.left() or x0 > plot.right():
                continue
            p.fillRect(QRectF(x0, plot.top(), x1 - x0, plot.height()), self._clip_colour(i, c, 38))

    def _paint_lane(self, p: QPainter, lane: QRectF) -> None:
        p.fillRect(lane, QColor(theme.BG))
        p.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        for i, c in enumerate(self.clips):
            x0, x1 = self.x_of(c.start), self.x_of(c.end)
            if x1 < lane.left() or x0 > lane.right():
                continue
            r = QRectF(x0, lane.top(), max(3.0, x1 - x0), lane.height())
            p.setPen(QPen(QColor("white"), 1.5) if i == self.selected else Qt.NoPen)
            p.setBrush(self._clip_colour(i, c, 200))
            p.drawRoundedRect(r, 5, 5)
            if r.width() > 22:
                p.setPen(QColor("white"))
                label = f"{i + 1}  {c.title}" if r.width() > 70 else str(i + 1)
                p.drawText(r.adjusted(6, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft,
                           p.fontMetrics().elidedText(label, Qt.ElideRight, int(r.width() - 10)))

    # ------------------------------------------------------------ interaction
    def _hit(self, x: float, y: float) -> tuple[str, int]:
        """('start'|'end'|'body'|'', clip index) under the cursor."""
        in_lane = self._lane().adjusted(0, -2, 0, 2).contains(QPointF(x, y))
        for i in ([self.selected] if self.selected >= 0 else []) + list(range(len(self.clips))):
            if i >= len(self.clips):
                continue
            c = self.clips[i]
            x0, x1 = self.x_of(c.start), self.x_of(c.end)
            if abs(x - x0) <= EDGE_GRAB and y > RULER:
                return "start", i
            if abs(x - x1) <= EDGE_GRAB and y > RULER:
                return "end", i
            if in_lane and x0 <= x <= x1:
                return "body", i
        return "", -1

    def mousePressEvent(self, e) -> None:
        if self.duration <= 0:
            return
        x, y = e.position().x(), e.position().y()
        if e.button() == Qt.LeftButton:
            kind, i = self._hit(x, y)
            if kind:
                c = self.clips[i]
                self.selected = i
                self.clipSelected.emit(i)
                self._drag = (kind, i, self.t_of(x), c.start, c.end, False)
                if kind == "body":
                    self.seekRequested.emit(c.start)
            else:
                t = self.t_of(x)
                hit = next((j for j, c in enumerate(self.clips) if c.start <= t <= c.end), -1)
                if hit >= 0 and hit != self.selected:
                    self.selected = hit
                    self.clipSelected.emit(hit)
                self.seekRequested.emit(t)
                self._drag = ("seek", -1, t, 0, 0, False)
            self.update()
        elif e.button() == Qt.MiddleButton:
            self._drag = ("pan", -1, x, self.v0, self.v1, False)

    def mouseMoveEvent(self, e) -> None:
        x = e.position().x()
        if self.duration <= 0:
            return
        self._hover = self.t_of(x)
        if self._drag is None:
            kind, _ = self._hit(x, e.position().y())
            self.setCursor(Qt.SizeHorCursor if kind in ("start", "end") else
                           Qt.OpenHandCursor if kind == "body" else Qt.ArrowCursor)
            self.update()
            return
        kind, i, t0, a, b, _moved = self._drag
        t = self.t_of(x)
        if kind == "seek":
            self.seekRequested.emit(t)
        elif kind == "pan":
            dt = (t0 - x) / max(1.0, self._plot().width()) * (b - a)
            dt = max(-a, min(self.duration - b, dt))
            self.v0, self.v1 = a + dt, b + dt
        else:
            c = self.clips[i]
            if kind == "start":
                c.start = max(0.0, min(t, c.end - 1.0))
            elif kind == "end":
                c.end = min(self.duration, max(t, c.start + 1.0))
            else:
                length = b - a
                start = max(0.0, min(self.duration - length, a + (t - t0)))
                c.start, c.end = start, start + length
            self._drag = (kind, i, t0, a, b, True)
            self.clipLive.emit(i)
        self.update()

    def mouseReleaseEvent(self, _e) -> None:
        if self._drag and self._drag[0] in ("start", "end", "body") and self._drag[5]:
            self.clipChanged.emit(self._drag[1])
        self._drag = None

    def leaveEvent(self, _e) -> None:
        self._hover = None
        self.update()

    def mouseDoubleClickEvent(self, e) -> None:
        kind, i = self._hit(e.position().x(), e.position().y())
        if i >= 0:
            self.playClipRequested.emit(i)

    def wheelEvent(self, e) -> None:
        if self.duration <= 0:
            return
        steps = e.angleDelta().y() / 120
        span = self.v1 - self.v0
        if e.modifiers() & Qt.ShiftModifier:
            dt = -steps * span * 0.1
            dt = max(-self.v0, min(self.duration - self.v1, dt))
            self.v0, self.v1 = self.v0 + dt, self.v1 + dt
        else:
            anchor = self.t_of(e.position().x())
            new_span = min(self.duration, max(5.0, span * (0.8 ** steps)))
            frac = (anchor - self.v0) / span if span else 0.5
            self.v0 = max(0.0, min(self.duration - new_span, anchor - frac * new_span))
            self.v1 = self.v0 + new_span
        self.update()

    def contextMenuEvent(self, e) -> None:
        if self.duration <= 0:
            return
        t = self.t_of(e.pos().x())
        menu = QMenu(self)
        add = QAction(f"Add clip at {stamp(t)}", menu)
        add.triggered.connect(lambda: self.addClipRequested.emit(t))
        menu.addAction(add)
        hit = next((j for j, c in enumerate(self.clips) if c.start <= t <= c.end), -1)
        if hit >= 0:
            play = QAction(f"Play clip {hit + 1}", menu)
            play.triggered.connect(lambda: self.playClipRequested.emit(hit))
            delete = QAction(f"Delete clip {hit + 1}", menu)
            delete.triggered.connect(lambda: self.deleteClipRequested.emit(hit))
            menu.addAction(play)
            menu.addAction(delete)
        menu.addSeparator()
        fit = QAction("Zoom to fit", menu)
        fit.triggered.connect(self.zoom_all)
        menu.addAction(fit)
        menu.exec(e.globalPos())

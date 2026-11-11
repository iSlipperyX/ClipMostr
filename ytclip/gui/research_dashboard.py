"""The Research tab's command-center pieces: the opportunity matrix (a momentum heatmap over a card per
cluster), the category pills, the view switch and the Most replayed graph inspector.

The numbers all come from analytics.py; research.py puts the pieces together. Charts follow one look:
2 px lines with a light wash under them, hairline axes, a 2 px surface gap between touching marks, values
in text colours (never the series colour), and every value also readable without hovering (labels beside
the charts, or the table view). Hover any chart for its exact figures.
"""

from __future__ import annotations

import datetime as dt
import statistics

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSizePolicy, QToolTip, QVBoxLayout, QWidget)

from .. import analytics
from ..analytics import compact
from ..organize import stamp
from . import theme

# Validated with the dataviz palette check against the app's panels (#262628 dark, #FFFFFF light): adjacent CVD
# separation 9.4 / 9.2, normal vision 26.5 / 27.6. Light aqua sits under 3:1, so the counts are always
# written beside the bar.
LONGEVITY_COLOURS = {"ephemeral": ("#d95926", "#eb6834"), "evergreen": ("#199e70", "#1baf7a")}  # (dark, light)
CARD_WIDTH = 300  # the matrix fits as many columns of cards this wide as there's room for


def replay_colour() -> str:
    """The Most replayed curve: the Studio's heatmap orange, darkened in light mode to stay over 3:1."""
    return "#FF9F0A" if theme.is_dark() else "#C93400"


def drop_colour() -> QColor:
    """Drop-off marks: neutral ink, so they're never confused with the replay curve; the ▼ says what they are."""
    c = QColor(theme.TEXT)
    c.setAlphaF(0.75)
    return c


def longevity_colour(kind: str) -> str:
    dark, light = LONGEVITY_COLOURS.get(kind, LONGEVITY_COLOURS["evergreen"])
    return dark if theme.is_dark() else light


def heat_colour(fraction: float) -> QColor:
    """One hue, from just off the panel (a little) to the full accent (a lot): darker for more in light
    mode, brighter for more in dark mode."""
    t = 0.12 + 0.88 * max(0.0, min(1.0, fraction))
    a, b = QColor(theme.PANEL), QColor(theme.ACCENT)
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def score_colour(value: float) -> str:
    """The colour Research gives a score out of 100 (the same steps as clip-ability)."""
    if value >= 75:
        return theme.GOOD
    if value >= 55:
        return theme.ACCENT_2
    if value >= 40:
        return theme.WARN
    return theme.MUTED


def delta_colour(delta: float | None) -> str:
    if delta is None:
        return theme.MUTED
    return theme.GOOD if delta >= 0.1 else theme.BAD if delta <= -0.15 else theme.MUTED


def delta_arrow(delta: float | None) -> str:
    if delta is None:
        return "·"
    return "⇈" if delta >= 1.0 else "↑" if delta >= 0.1 else "→" if delta > -0.15 else "↘" if delta > -0.5 else "↓"


def hour_span(end: float, hours: int, index: int) -> str:
    """'13:00–14:00' for the index-th hour of the `hours` before `end`."""
    start = dt.datetime.fromtimestamp(end - (hours - index) * 3600)
    return f"{start:%H:%M}–{start + dt.timedelta(hours=1):%H:%M}"


def _small(font: QFont, delta: float = -1.5) -> QFont:
    f = QFont(font)
    f.setPointSizeF(max(6.5, f.pointSizeF() + delta))
    return f


class ElidedLabel(QLabel):
    """A one-line label that ends in … when it doesn't fit (the full text is its tooltip)."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setMinimumWidth(30)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt's name)
        self._full = text
        self.setToolTip(text)
        super().setText(text)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        p.setFont(self.font())
        p.drawText(self.rect(), int(self.alignment() | Qt.AlignVCenter),
                   self.fontMetrics().elidedText(self._full, Qt.ElideRight, self.width()))


# ---------------------------------------------------------------- the Most replayed graph inspector


class ReplayGraph(QWidget):
    """The Most replayed graph of the video being inspected: likely clips shaded with their edges drawn,
    each one's punchline as a dot, and the steepest drop-offs as ▼. Hover for the time and what's there."""

    PAD_L, PAD_R, PAD_T, PAD_B = 10, 10, 16, 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFixedHeight(168)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.segs: list[tuple[float, float, float]] = []
        self.windows: list[analytics.ClipWindow] = []
        self.drops: list[tuple[float, float]] = []
        self.duration = 0.0
        self.usual = 0.0
        self.message = "Select a video"
        self._hover: float | None = None

    def sizeHint(self) -> QSize:
        return QSize(360, 168)

    def set_video(self, v) -> None:
        self.segs, self.windows, self.drops, self._hover = [], [], [], None
        if v is None:
            self.message = "Select a video"
        elif v.live != "none":
            self.message = "A live video has no Most replayed graph yet"
        elif v.deep is None:
            self.message = "Not read yet: press Check replays (it uses no quota)"
        elif v.deep.error:
            self.message = f"Couldn't read it: {v.deep.error}"
        else:
            self.segs = analytics.curve(v.deep.heatmap)
            self.message = "" if self.segs else ("YouTube shows no Most replayed graph for this video yet: it "
                                                 "needs more views")
        if self.segs:
            self.duration = max(float(v.duration or 0.0), self.segs[-1][1])
            self.windows = analytics.clip_windows(v.deep.heatmap, self.duration)
            self.drops = analytics.drop_offs(v.deep.heatmap)
            self.usual = statistics.median(s[2] for s in self.segs)
        self.update()

    # geometry
    def _plot(self) -> QRectF:
        return QRectF(self.PAD_L, self.PAD_T, max(1.0, self.width() - self.PAD_L - self.PAD_R),
                      max(1.0, self.height() - self.PAD_T - self.PAD_B))

    def _x(self, t: float, r: QRectF) -> float:
        return r.left() + r.width() * (t / self.duration if self.duration else 0.0)

    def _y(self, v: float, r: QRectF) -> float:
        return r.bottom() - r.height() * v

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.FIELD))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        if not self.segs:
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(self.rect()).adjusted(16, 0, -16, 0), Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        r = self._plot()
        hair = QColor(theme.HAIRLINE)
        p.setPen(QPen(hair, 1))
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))
        # The video's usual level, so a peak reads as "above normal"
        usual = QColor(theme.MUTED)
        usual.setAlphaF(0.45)
        p.setPen(QPen(usual, 1))
        y = self._y(self.usual, r)
        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

        accent = QColor(theme.ACCENT)
        wash = QColor(accent)
        wash.setAlphaF(0.16 if theme.is_dark() else 0.10)
        edge = QColor(accent)
        edge.setAlphaF(0.9)
        for w in self.windows:
            x0, x1 = self._x(w.start, r), self._x(w.end, r)
            p.fillRect(QRectF(x0, r.top(), max(1.0, x1 - x0), r.height()), wash)
            p.setPen(QPen(edge, 1))
            p.drawLine(QPointF(x0, r.top()), QPointF(x0, r.bottom()))
            p.drawLine(QPointF(x1, r.top()), QPointF(x1, r.bottom()))

        drop = drop_colour()
        faint = QColor(drop)
        faint.setAlphaF(0.3)
        for t, _fall in self.drops:
            x = self._x(t, r)
            p.setPen(QPen(faint, 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))

        colour = QColor(replay_colour())
        line = QPainterPath(QPointF(self._x(self.segs[0][0], r), self._y(self.segs[0][2], r)))
        for a, b, v in self.segs:
            line.lineTo(QPointF(self._x((a + b) / 2, r), self._y(v, r)))
        line.lineTo(QPointF(self._x(self.segs[-1][1], r), self._y(self.segs[-1][2], r)))
        area = QPainterPath(line)
        area.lineTo(QPointF(self._x(self.segs[-1][1], r), r.bottom()))
        area.lineTo(QPointF(self._x(self.segs[0][0], r), r.bottom()))
        area.closeSubpath()
        fill = QColor(colour)
        fill.setAlphaF(0.12)
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawPath(area)
        pen = QPen(colour, 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(line)

        # Drop-offs: ▼ above the plot
        p.setPen(Qt.NoPen)
        p.setBrush(drop)
        for t, _fall in self.drops:
            x = self._x(t, r)
            p.drawPolygon(QPolygonF([QPointF(x - 4.5, 3), QPointF(x + 4.5, 3), QPointF(x, 11)]))
        # Punchlines: a dot on the curve, ringed in the surface colour
        for w in self.windows:
            p.setPen(QPen(QColor(theme.FIELD), 2))
            p.setBrush(accent)
            p.drawEllipse(QPointF(self._x(w.peak, r), self._y(w.height, r)), 5, 5)

        if self._hover is not None:
            x = self._x(self._hover, r)
            cross = QColor(theme.TEXT)
            cross.setAlphaF(0.4)
            p.setPen(QPen(cross, 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))

        p.setFont(_small(self.font()))
        p.setPen(QColor(theme.MUTED))
        axis = QRectF(r.left(), r.bottom() + 3, r.width(), self.PAD_B - 4)
        p.drawText(axis, Qt.AlignLeft | Qt.AlignTop, "0:00")
        p.drawText(axis, Qt.AlignHCenter | Qt.AlignTop, stamp(self.duration / 2))
        p.drawText(axis, Qt.AlignRight | Qt.AlignTop, stamp(self.duration))

    def describe(self, t: float) -> str:
        """What's at t seconds, for the hover readout."""
        seg = next((s for s in self.segs if s[0] <= t < s[1]), self.segs[-1])
        lines = [f"<b>{seg[2] * 100:.0f}% of the top</b> · {stamp(t)}"]
        w = next((w for w in self.windows if w.start <= t <= w.end), None)
        if w:
            lines.append(f"Likely clip {stamp(w.start)}–{stamp(w.end)} ({w.end - w.start:.0f} s), punchline at "
                         f"{stamp(w.peak)}")
        near = max(3.0, self.duration / 80)
        d = next((d for d in self.drops if abs(d[0] - t) <= near), None)
        if d:
            lines.append(f"Drop-off at {stamp(d[0])}: replays fall {d[1] * 100:.0f} points")
        return "<br>".join(lines)

    def mouseMoveEvent(self, e) -> None:
        if not self.segs:
            return
        r = self._plot()
        t = max(0.0, min(self.duration, (e.position().x() - r.left()) / r.width() * self.duration))
        self._hover = t
        self.update()
        QToolTip.showText(e.globalPosition().toPoint(), self.describe(t), self)

    def leaveEvent(self, e) -> None:
        self._hover = None
        self.update()
        QToolTip.hideText()
        super().leaveEvent(e)

    def summary(self) -> str:
        """The windows and drop-offs as text, under the graph (so no value needs a hover)."""
        if not self.segs:
            return ""
        if not self.windows:
            clips = "No standout peak: the replays are flat, so there's no obvious clip."
        else:
            clips = "Likely clips: " + ", ".join(f"<b>{stamp(w.start)}–{stamp(w.end)}</b> (punchline {stamp(w.peak)})"
                                                 for w in self.windows)
        if self.drops:
            clips += "<br>Drop-offs: " + ", ".join(stamp(t) for t, _ in self.drops)
        return clips


class Legend(QWidget):
    """Keys for a chart: a short line, a wash, a dot or a ▼, each with its name in muted text."""

    def __init__(self, items: list[tuple[str, str, str]], parent=None):
        super().__init__(parent)
        self.items = items  # (kind, colour, name)
        self.setFixedHeight(18)

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(sum(22 + fm.horizontalAdvance(name) + 12 for _k, _c, name in self.items), 18)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(_small(self.font(), -1))
        fm = p.fontMetrics()
        x, mid = 0.0, self.height() / 2
        for kind, colour, name in self.items:
            c = QColor(colour)
            if kind == "line":
                pen = QPen(c, 2)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawLine(QPointF(x + 1, mid), QPointF(x + 15, mid))
            elif kind == "wash":
                wash = QColor(c)
                wash.setAlphaF(0.22)
                p.setPen(QPen(c, 1))
                p.setBrush(wash)
                p.drawRect(QRectF(x + 2, mid - 5, 12, 10))
            elif kind == "dot":
                p.setPen(Qt.NoPen)
                p.setBrush(c)
                p.drawEllipse(QPointF(x + 8, mid), 4, 4)
            elif kind == "tri":
                p.setPen(Qt.NoPen)
                p.setBrush(c)
                p.drawPolygon(QPolygonF([QPointF(x + 3.5, mid - 4), QPointF(x + 12.5, mid - 4), QPointF(x + 8, mid + 4)]))
            p.setPen(QColor(theme.MUTED))
            p.drawText(QPointF(x + 20, mid + fm.ascent() / 2 - 1), name)
            x += 22 + fm.horizontalAdvance(name) + 12


def replay_legend() -> Legend:
    return Legend([("line", replay_colour(), "Replays"), ("wash", theme.ACCENT, "Likely clip"),
                   ("dot", theme.ACCENT, "Punchline"), ("tri", drop_colour().name(QColor.HexArgb), "Drop-off")])


# ---------------------------------------------------------------- category velocity


class Sparkline(QWidget):
    """A cluster's views per hour over the last day: a 2 px line with a light wash under it, a gap where no
    checks covered an hour, and a dot on the latest hour. Hover for each hour's number."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.values: list[float | None] = []
        self.end = 0.0
        self._hover: int | None = None
        self.setMouseTracking(True)
        self.setFixedHeight(36)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_values(self, values, end: float) -> None:
        self.values, self.end = list(values or []), end
        self.update()

    def _known(self) -> list[float]:
        return [v for v in self.values if v is not None]

    def _geometry(self) -> tuple[QRectF, float]:
        r = QRectF(self.rect()).adjusted(5, 6, -5, -4)
        return r, max(self._known(), default=0.0) or 1.0

    def _point(self, i: int, v: float, r: QRectF, top: float) -> QPointF:
        n = max(1, len(self.values))
        return QPointF(r.left() + r.width() * (i + 0.5) / n, r.bottom() - r.height() * (v / top))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r, top = self._geometry()
        p.setPen(QPen(QColor(theme.HAIRLINE), 1))
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))
        if not self._known():
            p.setPen(QColor(theme.MUTED))
            p.setFont(_small(self.font()))
            p.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, "Views an hour build up here as Research checks again")
            return
        colour = QColor(theme.ACCENT)
        fill = QColor(colour)
        fill.setAlphaF(0.12)
        runs: list[list[tuple[int, float]]] = [[]]
        for i, v in enumerate(self.values):
            if v is None:
                if runs[-1]:
                    runs.append([])
            else:
                runs[-1].append((i, v))
        pen = QPen(colour, 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        for run in (x for x in runs if x):
            pts = [self._point(i, v, r, top) for i, v in run]
            if len(pts) == 1:
                p.setPen(Qt.NoPen)
                p.setBrush(colour)
                p.drawEllipse(pts[0], 2.5, 2.5)
                continue
            line = QPainterPath(pts[0])
            for q in pts[1:]:
                line.lineTo(q)
            area = QPainterPath(line)
            area.lineTo(QPointF(pts[-1].x(), r.bottom()))
            area.lineTo(QPointF(pts[0].x(), r.bottom()))
            area.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawPath(area)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(line)
        last = max(i for i, v in enumerate(self.values) if v is not None)
        p.setPen(QPen(QColor(theme.PANEL), 2))
        p.setBrush(colour)
        p.drawEllipse(self._point(last, self.values[last], r, top), 4, 4)
        if self._hover is not None:
            x = self._point(self._hover, 0, r, top).x()
            cross = QColor(theme.TEXT)
            cross.setAlphaF(0.35)
            p.setPen(QPen(cross, 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))

    def describe(self, i: int) -> str:
        v = self.values[i]
        span = hour_span(self.end, len(self.values), i)
        return f"<b>{compact(v)} views an hour</b> · {span}" if v is not None else f"No checks covered {span}"

    def mouseMoveEvent(self, e) -> None:
        if not self.values or not self._known():
            return
        r, _top = self._geometry()
        i = int((e.position().x() - r.left()) / max(1.0, r.width()) * len(self.values))
        self._hover = max(0, min(len(self.values) - 1, i))
        self.update()
        QToolTip.showText(e.globalPosition().toPoint(), self.describe(self._hover), self)

    def leaveEvent(self, e) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(e)


class MomentumHeatmap(QWidget):
    """Clusters against the last 24 hours: each cell is a cluster's views per hour in that hour, deeper for
    more, each row against its own busiest hour so its shape shows when it heated up. The number at the end
    of a row is its latest hour's. Hover a cell for its figures; click a row for that cluster's videos."""

    picked = Signal(str)
    LABEL_W, VALUE_W, ROW_H, GAP, AXIS_H = 128, 70, 18, 2, 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[tuple[str, list[float | None]]] = []
        self.end = 0.0
        self._hover: tuple[int, int] | None = None
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(self._height())

    def _height(self) -> int:
        return max(1, len(self.rows)) * (self.ROW_H + self.GAP) + self.AXIS_H + 2

    def set_rows(self, rows: list[tuple[str, list[float | None]]], end: float) -> None:
        self.rows, self.end, self._hover = rows, end, None
        self.setFixedHeight(self._height())
        self.update()

    def has_data(self) -> bool:
        return any(v is not None for _name, vals in self.rows for v in vals)

    def _cells(self) -> tuple[float, float, int]:
        """(x where the cells start, a cell's width, hours)."""
        n = max((len(v) for _k, v in self.rows), default=analytics.HOURS) or analytics.HOURS
        x0 = self.LABEL_W
        width = max(1.0, self.width() - self.LABEL_W - self.VALUE_W)
        return x0, (width - self.GAP * (n - 1)) / n, n

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        x0, cell, n = self._cells()
        small = _small(self.font(), -1)
        p.setFont(small)
        fm = p.fontMetrics()
        for k, (name, vals) in enumerate(self.rows):
            y = k * (self.ROW_H + self.GAP)
            p.setPen(QColor(theme.TEXT))
            p.drawText(QRectF(0, y, self.LABEL_W - 10, self.ROW_H), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(name, Qt.ElideRight, self.LABEL_W - 10))
            known = [v for v in vals if v is not None]
            top = max(known, default=0.0)
            for i in range(n):
                v = vals[i] if i < len(vals) else None
                rect = QRectF(x0 + i * (cell + self.GAP), y, cell, self.ROW_H)
                if v is None:
                    p.setPen(QPen(QColor(theme.HAIRLINE), 1))
                    p.setBrush(Qt.NoBrush)
                    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
                else:
                    p.setPen(Qt.NoPen)
                    p.setBrush(heat_colour((v / top) ** 0.75 if top > 0 else 0.0))
                    p.drawRoundedRect(rect, 3, 3)
                if self._hover == (k, i):
                    p.setPen(QPen(QColor(theme.TEXT), 1.5))
                    p.setBrush(Qt.NoBrush)
                    p.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), 3, 3)
            latest = next((v for v in reversed(vals) if v is not None), None)
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(self.width() - self.VALUE_W + 6, y, self.VALUE_W - 6, self.ROW_H),
                       Qt.AlignRight | Qt.AlignVCenter, f"{compact(latest)}/h" if latest is not None else "–")
        axis_y = len(self.rows or [None]) * (self.ROW_H + self.GAP)
        axis = QRectF(x0, axis_y, self.width() - x0 - self.VALUE_W, self.AXIS_H)
        p.setPen(QColor(theme.MUTED))
        p.drawText(axis, Qt.AlignLeft | Qt.AlignVCenter, f"{n} h ago")
        p.drawText(axis, Qt.AlignHCenter | Qt.AlignVCenter, f"{n // 2} h ago")
        p.drawText(axis, Qt.AlignRight | Qt.AlignVCenter, "now")
        p.drawText(QRectF(self.width() - self.VALUE_W + 6, axis_y, self.VALUE_W - 6, self.AXIS_H),
                   Qt.AlignRight | Qt.AlignVCenter, "latest")
        if not self.rows:
            p.drawText(QRectF(x0, 0, axis.width(), self.ROW_H), Qt.AlignCenter, "No videos to show")

    def cell_at(self, x: float, y: float) -> tuple[int, int] | None:
        x0, cell, n = self._cells()
        k = int(y // (self.ROW_H + self.GAP))
        if not (0 <= k < len(self.rows)) or x < x0:
            return None
        i = int((x - x0) // (cell + self.GAP))
        return (k, i) if 0 <= i < n else None

    def describe(self, k: int, i: int) -> str:
        name, vals = self.rows[k]
        v = vals[i] if i < len(vals) else None
        span = hour_span(self.end, len(vals) or analytics.HOURS, i)
        if v is None:
            return f"{name} · {span}<br>No two checks covered this hour"
        return f"<b>{compact(v)} views an hour</b><br>{name} · {span}"

    def mouseMoveEvent(self, e) -> None:
        hit = self.cell_at(e.position().x(), e.position().y())
        if hit != self._hover:
            self._hover = hit
            self.update()
        if hit:
            QToolTip.showText(e.globalPosition().toPoint(), self.describe(*hit), self)
        else:
            QToolTip.hideText()
        self.setCursor(Qt.PointingHandCursor if hit or e.position().x() < self.LABEL_W else Qt.ArrowCursor)

    def mousePressEvent(self, e) -> None:
        k = int(e.position().y() // (self.ROW_H + self.GAP))
        if 0 <= k < len(self.rows) and e.button() == Qt.LeftButton:
            self.picked.emit(self.rows[k][0])

    def leaveEvent(self, e) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(e)


class HeatScale(QWidget):
    """The heatmap's key: a few cells from a little to a lot, and the empty cell for no checks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(16)

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance("Fewer  More views an hour   No checks") + 5 * 14 + 40, 16)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(_small(self.font(), -1))
        fm = p.fontMetrics()
        y = self.height() / 2
        x = 0.0
        p.setPen(QColor(theme.MUTED))
        p.drawText(QPointF(x, y + fm.ascent() / 2 - 1), "Fewer")
        x += fm.horizontalAdvance("Fewer") + 6
        for k in range(5):
            p.setPen(Qt.NoPen)
            p.setBrush(heat_colour(k / 4))
            p.drawRoundedRect(QRectF(x, y - 6, 12, 12), 3, 3)
            x += 14
        p.setPen(QColor(theme.MUTED))
        p.drawText(QPointF(x + 4, y + fm.ascent() / 2 - 1), "More views an hour")
        x += fm.horizontalAdvance("More views an hour") + 18
        p.setPen(QPen(QColor(theme.HAIRLINE), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(x + 0.5, y - 5.5, 11, 11), 3, 3)
        p.setPen(QColor(theme.MUTED))
        p.drawText(QPointF(x + 18, y + fm.ascent() / 2 - 1), "No checks")


# ---------------------------------------------------------------- cluster cards


class LongevityBar(QWidget):
    """Ephemeral against evergreen in a cluster: a thin two-part bar with a 2 px gap between the parts, and
    the counts written under it, so it never relies on colour."""

    def __init__(self, mix: dict[str, int], parent=None):
        super().__init__(parent)
        self.mix = mix
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        total = sum(mix.values())
        self.setToolTip("How long a clip of these videos stays worth posting: ephemeral ones (news, drama, live "
                        "events, results) date within about 48 hours, evergreen ones still work next week"
                        + (f"\n{mix.get('ephemeral', 0)} of {total} are ephemeral" if total else ""))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        total = sum(self.mix.values())
        bar = QRectF(0, 1, self.width(), 6)
        if total:
            x = bar.left()
            parts = [(k, n) for k, n in (("ephemeral", self.mix.get("ephemeral", 0)),
                                         ("evergreen", self.mix.get("evergreen", 0))) if n]
            gap = 2.0 if len(parts) > 1 else 0.0
            usable = bar.width() - gap * (len(parts) - 1)
            for k, n in parts:
                w = usable * n / total
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(longevity_colour(k)))
                p.drawRoundedRect(QRectF(x, bar.top(), w, bar.height()), 3, 3)
                x += w + gap
        p.setFont(_small(self.font(), -1))
        fm = p.fontMetrics()
        x, base = 0.0, bar.bottom() + 6 + fm.ascent()
        for k, label in (("ephemeral", "ephemeral"), ("evergreen", "evergreen")):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(longevity_colour(k)))
            p.drawEllipse(QPointF(x + 4, base - fm.ascent() / 2 + 1), 3.5, 3.5)
            text = f"{self.mix.get(k, 0)} {label}"
            p.setPen(QColor(theme.MUTED))
            p.drawText(QPointF(x + 11, base), text)
            x += 11 + fm.horizontalAdvance(text) + 14


class _VideoRow(QFrame):
    clicked = Signal(str)

    def __init__(self, ins: analytics.Insight, parent=None):
        super().__init__(parent)
        self.setObjectName("vrow")
        self.setCursor(Qt.PointingHandCursor)
        self.video_id = ins.video.id
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(8)
        score = QLabel(f"{ins.viral:.0f}")
        score.setFixedWidth(24)
        score.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        score.setStyleSheet(f"color:{score_colour(ins.viral)}; font-weight:700;")
        title = ElidedLabel(ins.video.title)
        flags = ("⚡ " if ins.arbitrage else "") + ("⚑ " if ins.outlier else "")
        arrow = QLabel(flags + ins.momentum.arrow)
        arrow.setStyleSheet(f"color:{delta_colour(ins.momentum.delta if ins.momentum.known else None)};")
        lay.addWidget(score)
        lay.addWidget(title, 1)
        lay.addWidget(arrow)
        tip = [ins.video.title, f"Viral potential {ins.viral:.0f}, clip-ability {ins.video.score:.0f}",
               ins.momentum.text()]
        if ins.outlier:
            tip.append(f"Outlier: {ins.outlier:g}× the median views an hour of {ins.outlier_of}")
        if ins.arbitrage:
            tip.append(f"Live arbitrage: {ins.arbitrage_why}")
        self.setToolTip("\n".join(tip))
        title.setToolTip(self.toolTip())

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.video_id)


class ClusterCard(QFrame):
    """One cluster in the opportunity matrix: its median viral potential, momentum, velocity over the last
    day, how long its videos will last, and its three best videos."""

    open_cluster = Signal(str)
    open_video = Signal(str)

    def __init__(self, c: analytics.Cluster, end: float, outside: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.name = c.name
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)  # a row's cards share its height
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(6)

        head = QHBoxLayout()
        names = QVBoxLayout()
        names.setSpacing(1)
        title = ElidedLabel(c.name)
        title.setObjectName("h2")
        sub = QLabel(f"{c.count} video{'s' if c.count != 1 else ''} · {compact(c.speed)} views an hour"
                     + (" · not one of your categories" if outside else ""))
        sub.setProperty("muted", True)
        sub.setWordWrap(True)
        if outside:
            sub.setToolTip("YouTube's category for trending videos that fit none of your categories. Your picking "
                           "rules skip these: add a category for them to post them")
        names.addWidget(title)
        names.addWidget(sub)
        figure = QVBoxLayout()
        figure.setSpacing(0)
        self.viral = QLabel(f"{c.viral:.0f}")
        self.viral.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.viral.setStyleSheet(f"color:{score_colour(c.viral)}; font-size:20pt; font-weight:700;")
        self.viral.setToolTip("The median viral potential of its videos, out of 100")
        caption = QLabel("viral potential")
        caption.setProperty("muted", True)
        caption.setAlignment(Qt.AlignRight)
        caption.setStyleSheet("font-size:8pt;")
        figure.addWidget(self.viral)
        figure.addWidget(caption)
        head.addLayout(names, 1)
        head.addLayout(figure)
        lay.addLayout(head)

        m = c.momentum
        bits = [f"<span style='color:{delta_colour(m)}'><b>{delta_arrow(m)} {m:+.0%}</b></span> lately"
                if m is not None else "Momentum: measuring"]
        if c.hot:
            bits.append(f"{c.hot} accelerating")
        if c.outliers:
            bits.append(f"⚑ {c.outliers} outlier{'s' if c.outliers != 1 else ''}")
        if c.arbitrage:
            bits.append(f"⚡ {c.arbitrage} to race for")
        self.stats = QLabel(" · ".join(bits))
        self.stats.setTextFormat(Qt.RichText)
        self.stats.setProperty("muted", True)
        self.stats.setWordWrap(True)
        self.stats.setToolTip("Momentum: the median change in views an hour lately against each video's last day. "
                              "Accelerating: 30% faster or more. Outliers: far faster than the rest of the cluster. "
                              "To race for: taking off now and not clipped by you yet (Live arbitrage)")
        lay.addWidget(self.stats)

        self.spark = Sparkline()
        self.spark.set_values(c.velocity, end)
        self.spark.setToolTip(f"{c.name}: views an hour, all its videos together, over the last day")
        lay.addWidget(self.spark)
        lay.addWidget(LongevityBar(c.longevity_mix()))

        self.rows = []
        for ins in c.top(3):
            row = _VideoRow(ins)
            row.clicked.connect(self.open_video.emit)
            lay.addWidget(row)
            self.rows.append(row)
        lay.addStretch(1)
        more = QPushButton(f"Show {'all ' if c.count > 1 else ''}{c.count} in the table  →")
        more.setObjectName("flat")
        more.setCursor(Qt.PointingHandCursor)
        more.clicked.connect(lambda: self.open_cluster.emit(self.name))
        lay.addWidget(more, 0, Qt.AlignRight)
        self.more_btn = more

    def set_selected(self, on: bool) -> None:
        if self.property("selected") != on:
            self.setProperty("selected", on)
            self.style().unpolish(self)
            self.style().polish(self)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.open_cluster.emit(self.name)


class OpportunityMatrix(QScrollArea):
    """The matrix view: the momentum heatmap, then a card per cluster, most promising first."""

    open_cluster = Signal(str)
    open_video = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 4, 0)
        v.setSpacing(10)

        self.heat_panel = QFrame()
        self.heat_panel.setObjectName("panel")
        hp = QVBoxLayout(self.heat_panel)
        hp.setContentsMargins(14, 10, 14, 10)
        hp.setSpacing(8)
        top = QHBoxLayout()
        heading = QLabel("Momentum heatmap")
        heading.setObjectName("h2")
        note = QLabel("views an hour by cluster, last 24 hours")
        note.setProperty("muted", True)
        top.addWidget(heading)
        top.addWidget(note)
        top.addStretch(1)
        top.addWidget(HeatScale())
        hp.addLayout(top)
        self.heatmap = MomentumHeatmap()
        self.heatmap.picked.connect(self.open_cluster.emit)
        hp.addWidget(self.heatmap)
        self.heat_note = QLabel("Each check adds to this: with Keep checking on, Research fills a day of it. "
                                "Each row is shaded against its own busiest hour.")
        self.heat_note.setProperty("muted", True)
        self.heat_note.setWordWrap(True)
        hp.addWidget(self.heat_note)
        v.addWidget(self.heat_panel)

        head = QHBoxLayout()
        title = QLabel("Market opportunity matrix")
        title.setObjectName("h2")
        self.matrix_note = QLabel()
        self.matrix_note.setProperty("muted", True)
        self.matrix_note.setWordWrap(True)
        head.addWidget(title)
        head.addWidget(self.matrix_note, 1)
        v.addLayout(head)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        v.addWidget(self.grid_host)
        self.empty = QLabel("No videos match the filters")
        self.empty.setProperty("muted", True)
        self.empty.setAlignment(Qt.AlignCenter)
        v.addWidget(self.empty)
        v.addStretch(1)
        self.setWidget(body)
        self.cards: list[ClusterCard] = []
        self._columns = 0
        self._selected = ""

    def show_clusters(self, clusters: list[analytics.Cluster], end: float, note: str = "",
                      has_categories: bool = False) -> None:
        for card in self.cards:
            self.grid.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []
        for c in clusters:
            card = ClusterCard(c, end, outside=has_categories and not c.yours)
            card.open_cluster.connect(self.open_cluster.emit)
            card.open_video.connect(self.open_video.emit)
            card.set_selected(c.name == self._selected)
            self.cards.append(card)
        self._columns = 0
        self._layout_cards()
        self.heatmap.set_rows([(c.name, c.velocity) for c in clusters], end)
        self.heat_note.setVisible(not self.heatmap.has_data() or len(clusters) <= 1)
        self.empty.setVisible(not clusters)
        self.matrix_note.setText(note)

    def mark(self, name: str) -> None:
        """Outline the card of the cluster whose video is being inspected."""
        self._selected = name
        for card in self.cards:
            card.set_selected(card.name == name)

    def columns(self) -> int:
        return max(1, (self.viewport().width() - 4) // CARD_WIDTH)

    def _layout_cards(self) -> None:
        cols = self.columns()
        if cols == self._columns and all(self.grid.indexOf(c) >= 0 for c in self.cards):
            return
        self._columns = cols
        for card in self.cards:
            self.grid.removeWidget(card)
        for k, card in enumerate(self.cards):
            self.grid.addWidget(card, k // cols, k % cols)
        for c in range(max(cols, self.grid.columnCount())):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._layout_cards()


# ---------------------------------------------------------------- filter pills and the view switch


class CategoryPills(QWidget):
    """One pill per category (and All, and Fits none), with how many videos each has; one is picked. A
    cluster picked from the matrix shows as a pill with ✕ to clear it."""

    picked = Signal(object)  # the category id: None for all, "" for the ones that fit none
    cluster_cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: list[QPushButton] = []
        self.chip = QPushButton()
        self.chip.setObjectName("pill")
        self.chip.setCheckable(True)
        self.chip.setChecked(True)
        self.chip.setVisible(False)
        self.chip.setToolTip("Showing one cluster from the matrix. Click to show everything again")
        self.chip.clicked.connect(self._clear_cluster)
        self.lay.addWidget(self.chip)
        self.lay.addStretch(1)

    BADGE_MARKS = {"Hot Arbitrage": "🔥", "High Retention": "🔁", "Oversaturated": "🧊", "Rising": "📈"}

    def set_items(self, items: list[tuple[str, object, int]], current: object,
                  badges: dict | None = None) -> None:
        """`badges`: category id -> (badge, why), the opportunity badge shown on that category's pill
        (research/velocity.py)."""
        badges = badges or {}
        for b in self.buttons:
            self.group.removeButton(b)
            self.lay.removeWidget(b)
            b.hide()
            b.deleteLater()
        self.buttons = []
        for k, (label, data, count) in enumerate(items):
            badge, why = badges.get(data, ("", "")) if isinstance(data, str) else ("", "")
            b = QPushButton(f"{label}  {count}" + (f"  {self.BADGE_MARKS.get(badge, '')} {badge}" if badge else ""))
            b.setObjectName("pill")
            if badge:
                b.setToolTip(f"{badge}: {why}")
                b.setProperty("badge", badge)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setProperty("category", data)
            b.setChecked(data == current)
            b.clicked.connect(lambda _on=False, d=data: self.picked.emit(d))
            self.group.addButton(b)
            self.lay.insertWidget(k, b)
            self.buttons.append(b)

    def current(self) -> object:
        b = self.group.checkedButton()
        return b.property("category") if b else None

    def labels(self) -> list[str]:
        return [b.text() for b in self.buttons]

    def set_cluster(self, name: str | None) -> None:
        self.chip.setVisible(bool(name))
        if name:
            self.chip.setText(f"{name}  ✕")
            self.chip.setChecked(True)

    def _clear_cluster(self) -> None:
        self.chip.setVisible(False)
        self.cluster_cleared.emit()


class ViewSwitch(QFrame):
    """Matrix | Table | Live arbitrage."""

    changed = Signal(str)
    VIEWS = (("matrix", "▦  Matrix", "A card per cluster with its momentum heatmap: where the opportunities are"),
             ("table", "☰  Table", "Every video, sortable, with its scores"),
             ("arbitrage", "⚡  Live arbitrage", "Taking off on YouTube right now and not clipped by you yet"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("segbar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        for key, text, tip in self.VIEWS:
            b = QPushButton(text)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _on=False, k=key: self.changed.emit(k))
            self.group.addButton(b)
            lay.addWidget(b)
            self.buttons[key] = b

    def set_view(self, key: str) -> None:
        if key in self.buttons:
            self.buttons[key].setChecked(True)

    def view(self) -> str:
        return next((k for k, b in self.buttons.items() if b.isChecked()), "matrix")

    def set_arbitrage_count(self, n: int) -> None:
        self.buttons["arbitrage"].setText(f"⚡  Live arbitrage  {n}" if n else "⚡  Live arbitrage")

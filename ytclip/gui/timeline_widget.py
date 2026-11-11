"""A picture of a timeline (studio/timeline_schema.py): its tracks as rows, its clips as blocks along a ruler.

    view = TimelineWidget()
    view.set_timeline(Timeline.load(folder / "timeline.json"))

Read-only: it shows what the Studio laid out (and what you changed in timeline.json), it doesn't edit. Each
row is a track: pictures, overlays, the voice, the music, the effects, the captions and titles. A clip's
block carries its label; a transition is the lighter wedge at a clip's start; markers (the hook, the twist,
the payoff, chapters) are flags on the ruler. Hovering a block says what it is: its file, where it came
from and who to credit, its time and length.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..studio.timeline_schema import Timeline
from . import theme

HEADER = 112  # the track names' column
RULER = 26
ROW = 30
GAP = 4
PAD = 8
# A colour for each kind of row (the same in dark and light: they sit on a panel either way).
KIND_COLOURS = {"video": "#4F7CFF", "overlay": "#8E6BFF", "voice": "#19B37D", "music": "#D9892B", "sfx": "#D4556B",
                "subtitle": "#C9A227"}
MARKER_COLOURS = {"hook": "#FF6A3D", "retention": "#D4556B", "payoff": "#19B37D", "cta": "#4F7CFF",
                  "chapter": "#C9A227", "note": "#8A8F98"}
ROW_NAMES = {"video": "Pictures", "overlay": "Overlays", "voice": "Voice", "music": "Music", "sfx": "Effects",
             "subtitle": "Captions"}


@dataclass
class Block:
    """One thing drawn on a row."""
    row: int
    start: float
    end: float
    label: str
    tip: str
    kind: str
    transition: float = 0.0  # seconds of transition at its start
    rect: QRectF | None = None  # where it was last painted


def _clock(t: float) -> str:
    t = max(0.0, float(t))
    return f"{int(t // 60)}:{t % 60:04.1f}" if t < 600 else f"{int(t // 60)}:{int(t % 60):02d}"


def _cue_name(clip_id: str) -> str:
    """"auto_bass_drop_1" and "cue_whoosh_3" as "bass drop" and "whoosh"."""
    name = clip_id.removeprefix("auto_").removeprefix("cue_")
    head, _, tail = name.rpartition("_")
    return (head if head and tail.isdigit() else name).replace("_", " ")


def tick_step(duration: float, width: float) -> float:
    """Seconds between ruler ticks so their labels don't crowd (about one every 70 pixels)."""
    if duration <= 0 or width <= 0:
        return 1.0
    want = duration / max(1.0, width / 70.0)
    for step in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
        if step >= want:
            return float(step)
    return 1200.0


def rows_of(tl: Timeline) -> tuple[list[tuple[str, str]], list[Block]]:
    """([(row name, kind)], blocks) for a timeline: a row a track, in the order they're stacked."""
    rows: list[tuple[str, str]] = []
    blocks: list[Block] = []
    videos = [t for t in tl.tracks if t.kind == "video"]
    for track in tl.tracks:
        if track.kind == "video":
            kind = "video" if track is videos[0] else "overlay"
            name = track.label or ROW_NAMES[kind]
        elif track.kind == "audio":
            kind = track.role if track.role in ("voice", "music", "sfx") else "sfx"
            name = track.label or ROW_NAMES[kind]
        else:
            kind = "subtitle"
            name = track.label or ("Titles" if track.id != "subtitle_track" else ROW_NAMES[kind])
        row = len(rows)
        rows.append((name + (" (muted)" if track.muted else ""), kind))
        if track.kind == "subtitle":
            for ev in track.events:
                blocks.append(Block(row, ev.start, ev.end, ev.text,
                                    f"“{ev.text}”\n{_clock(ev.start)} to {_clock(ev.end)}", kind))
            continue
        for clip in track.clips:
            asset = tl.assets.get(clip.asset)
            name = clip.label or clip.id
            if kind == "sfx":  # an effect is known by its sound; its label says why it's there
                name = _cue_name(clip.id)
            lines = [name + (f" ({clip.label})" if kind == "sfx" and clip.label else "")]
            if asset is not None:
                what = asset.path or asset.color or asset.kind
                lines.append(f"{asset.kind}: {what}")
                if asset.source:
                    lines.append(f"from {asset.source}")
                if asset.credit:
                    lines.append(asset.credit)
            lines.append(f"{_clock(clip.start)} to {_clock(clip.end)} ({clip.duration:.1f} s)")
            fade = 0.0
            if track.kind == "video":
                if not clip.transition_in.cut:
                    fade = clip.transition_in.duration
                    lines.append(f"arrives by {clip.transition_in.type.replace('_', ' ')} ({fade:.2f} s)")
                if clip.keyframes:
                    lines.append(f"{len(clip.keyframes)} keyframes")
            elif clip.gain_db:
                lines.append(f"{clip.gain_db:+.0f} dB")
            blocks.append(Block(row, clip.start, clip.end, name, "\n".join(lines), kind, fade))
    return rows, blocks


class TimelineWidget(QWidget):
    """Draws a timeline to fit its width."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tl: Timeline | None = None
        self.rows: list[tuple[str, str]] = []
        self.blocks: list[Block] = []
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(320)

    def set_timeline(self, tl: Timeline | None) -> None:
        self.tl = tl
        self.rows, self.blocks = rows_of(tl) if tl is not None else ([], [])
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt's name
        return QSize(640, RULER + PAD + max(1, len(self.rows)) * (ROW + GAP) + PAD)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, self.sizeHint().height())

    # ------------------------------------------------------------ geometry
    def _span(self) -> tuple[float, float]:
        """(left edge of the time axis, its width) in pixels."""
        return float(HEADER), max(1.0, self.width() - HEADER - PAD)

    def x_of(self, t: float) -> float:
        left, width = self._span()
        total = self.tl.duration if self.tl and self.tl.duration > 0 else 1.0
        return left + max(0.0, min(1.0, t / total)) * width

    def time_at(self, x: float) -> float:
        left, width = self._span()
        total = self.tl.duration if self.tl and self.tl.duration > 0 else 0.0
        return max(0.0, min(1.0, (x - left) / width)) * total

    def _row_y(self, row: int) -> float:
        return RULER + PAD + row * (ROW + GAP)

    def block_rect(self, b: Block) -> QRectF:
        x0, x1 = self.x_of(b.start), self.x_of(b.end)
        return QRectF(x0, self._row_y(b.row), max(2.0, x1 - x0 - 1.0), ROW)

    def block_at(self, x: float, y: float) -> Block | None:
        """The block under a point (the shortest one, when captions sit close together)."""
        hits = [b for b in self.blocks if self.block_rect(b).contains(QPointF(x, y))]
        return min(hits, key=lambda b: b.end - b.start) if hits else None

    def marker_at(self, x: float, y: float):
        if self.tl is None or y > RULER:
            return None
        near = [m for m in self.tl.markers if abs(self.x_of(m.t) - x) <= 6]
        return min(near, key=lambda m: abs(self.x_of(m.t) - x)) if near else None

    # ------------------------------------------------------------ painting
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(theme.PANEL or "#1b1d23"))
        text, muted = QColor(theme.TEXT or "#e8e8ea"), QColor(theme.MUTED or "#8a8f98")
        line = QColor(theme.HAIRLINE or theme.BORDER or "#30333b")
        font = QFont(self.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1))
        p.setFont(font)
        fm = QFontMetrics(font)
        if self.tl is None or not self.rows:
            p.setPen(muted)
            p.drawText(self.rect(), Qt.AlignCenter, "A video made by the Studio shows its timeline here.")
            p.end()
            return
        left, width = self._span()
        total = self.tl.duration
        # The ruler
        step = tick_step(total, width)
        p.setPen(QPen(line, 1))
        p.drawLine(QPointF(left, RULER - 0.5), QPointF(left + width, RULER - 0.5))
        t = 0.0
        while t <= total + 1e-6:
            x = self.x_of(t)
            p.setPen(QPen(line, 1))
            p.drawLine(QPointF(x, RULER - 6), QPointF(x, RULER))
            p.setPen(muted)
            label = _clock(t) if step < 1 or total < 600 else f"{int(t // 60)}:{int(t % 60):02d}"
            if x + fm.horizontalAdvance(label) + 4 < left + width or t == 0:
                p.drawText(QPointF(x + 3, RULER - 9), label)
            t += step
        # Rows
        for i, (name, kind) in enumerate(self.rows):
            y = self._row_y(i)
            p.fillRect(QRectF(left, y, width, ROW), QColor(theme.PANEL_2 or "#22252c"))
            p.setPen(muted)
            p.drawText(QRectF(PAD, y, HEADER - PAD * 2, ROW), Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(name, Qt.ElideRight, HEADER - PAD * 2))
        # Blocks
        for b in self.blocks:
            r = self.block_rect(b)
            b.rect = r
            colour = QColor(KIND_COLOURS.get(b.kind, "#4F7CFF"))
            p.setPen(Qt.NoPen)
            p.setBrush(colour)
            p.drawRoundedRect(r, 3, 3)
            if b.transition > 0:
                w = min(r.width(), self.x_of(b.start + b.transition) - self.x_of(b.start))
                if w >= 2:
                    wedge = QPolygonF([QPointF(r.left(), r.top()), QPointF(r.left() + w, r.top()),
                                       QPointF(r.left(), r.bottom())])
                    p.setBrush(QColor(255, 255, 255, 90))
                    p.drawPolygon(wedge)
            if r.width() > 26:
                p.setPen(QColor("#FFFFFF") if colour.lightness() < 150 else QColor("#111111"))
                p.drawText(r.adjusted(5, 0, -3, 0), Qt.AlignVCenter | Qt.AlignLeft,
                           fm.elidedText(b.label, Qt.ElideRight, int(r.width() - 8)))
        # Markers
        bottom = self._row_y(len(self.rows) - 1) + ROW
        for m in self.tl.markers:
            x = self.x_of(m.t)
            colour = QColor(MARKER_COLOURS.get(m.kind, MARKER_COLOURS["note"]))
            p.setPen(QPen(colour, 1, Qt.DashLine))
            p.drawLine(QPointF(x, RULER), QPointF(x, bottom))
            p.setPen(Qt.NoPen)
            p.setBrush(colour)
            p.drawPolygon(QPolygonF([QPointF(x, RULER - 1), QPointF(x - 5, RULER - 9), QPointF(x + 5, RULER - 9)]))
        p.setPen(text)
        p.end()

    # ------------------------------------------------------------ hovering
    def tip_at(self, x: float, y: float) -> str:
        m = self.marker_at(x, y)
        if m is not None:
            return f"{m.kind.capitalize()} at {_clock(m.t)}" + (f": {m.label}" if m.label else "")
        b = self.block_at(x, y)
        return b.tip if b is not None else ""

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        tip = self.tip_at(pos.x(), pos.y())
        if tip:
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

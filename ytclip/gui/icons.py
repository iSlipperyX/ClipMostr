"""Line icons drawn in code: one consistent set for the sidebar, the toolbar and the check mark.

Thin rounded strokes on a 24-unit grid, in the style of the system symbol sets on macOS and Windows 11, so
they sit with the text instead of shouting the way emoji do. Each icon is a function that draws into a
QPainter scaled to 24 x 24; `icon()` turns one into a QIcon in any colour, crisp at any screen scale.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

STROKE = 1.7


def _studio(p: QPainter) -> None:  # a clapperboard-ish frame with a play mark
    p.drawRoundedRect(QRectF(3, 5, 18, 14), 3, 3)
    tri = QPainterPath(QPointF(10, 9))
    tri.lineTo(QPointF(15.5, 12))
    tri.lineTo(QPointF(10, 15))
    tri.closeSubpath()
    p.drawPath(tri)


def _library(p: QPainter) -> None:  # stacked rectangles
    p.drawRoundedRect(QRectF(4, 8, 16, 12), 2.5, 2.5)
    p.drawLine(QPointF(6, 5), QPointF(18, 5))
    p.drawLine(QPointF(8, 2.5), QPointF(16, 2.5))


def _research(p: QPainter) -> None:  # magnifier with a spark
    p.drawEllipse(QRectF(3.5, 3.5, 12, 12))
    p.drawLine(QPointF(14.3, 14.3), QPointF(20.5, 20.5))
    p.drawLine(QPointF(9.5, 7), QPointF(9.5, 12))
    p.drawLine(QPointF(7, 9.5), QPointF(12, 9.5))


def _niches(p: QPainter) -> None:  # target
    p.drawEllipse(QRectF(3, 3, 18, 18))
    p.drawEllipse(QRectF(7.5, 7.5, 9, 9))
    p.drawPoint(QPointF(12, 12))


def _automation(p: QPainter) -> None:  # lightning bolt
    bolt = QPainterPath(QPointF(13.5, 2.5))
    for x, y in ((5, 13.5), (11.5, 13.5), (10.5, 21.5), (19, 10.5), (12.5, 10.5)):
        bolt.lineTo(QPointF(x, y))
    bolt.closeSubpath()
    p.drawPath(bolt)


def _accounts(p: QPainter) -> None:  # person
    p.drawEllipse(QRectF(8, 3.5, 8, 8))
    body = QPainterPath(QPointF(4, 20.5))
    body.cubicTo(QPointF(4.5, 15.5), QPointF(8, 14), QPointF(12, 14))
    body.cubicTo(QPointF(16, 14), QPointF(19.5, 15.5), QPointF(20, 20.5))
    p.drawPath(body)


def _queue(p: QPainter) -> None:  # tray with an up arrow
    tray = QPainterPath(QPointF(3.5, 14))
    tray.lineTo(QPointF(3.5, 18.5))
    tray.quadTo(QPointF(3.5, 20.5), QPointF(5.5, 20.5))
    tray.lineTo(QPointF(18.5, 20.5))
    tray.quadTo(QPointF(20.5, 20.5), QPointF(20.5, 18.5))
    tray.lineTo(QPointF(20.5, 14))
    p.drawPath(tray)
    p.drawLine(QPointF(12, 3.5), QPointF(12, 15))
    p.drawLine(QPointF(7.5, 8), QPointF(12, 3.5))
    p.drawLine(QPointF(16.5, 8), QPointF(12, 3.5))


def _performance(p: QPainter) -> None:  # rising line chart
    p.drawLine(QPointF(3.5, 20.5), QPointF(20.5, 20.5))
    line = QPainterPath(QPointF(4, 16))
    for x, y in ((9, 11), (13, 14), (20, 5.5)):
        line.lineTo(QPointF(x, y))
    p.drawPath(line)
    p.drawLine(QPointF(15.5, 5.5), QPointF(20, 5.5))
    p.drawLine(QPointF(20, 5.5), QPointF(20, 10))


def _settings(p: QPainter) -> None:  # three sliders
    for y, x in ((6, 15), (12, 8), (18, 13)):
        p.drawLine(QPointF(3.5, y), QPointF(20.5, y))
        p.save()
        p.setBrush(p.pen().color())
        p.drawEllipse(QPointF(x, y), 2.2, 2.2)
        p.restore()


def _help(p: QPainter) -> None:  # question mark in a circle
    p.drawEllipse(QRectF(3, 3, 18, 18))
    q = QPainterPath(QPointF(9.3, 9.5))
    q.cubicTo(QPointF(9.3, 6.8), QPointF(14.7, 6.8), QPointF(14.7, 9.5))
    q.cubicTo(QPointF(14.7, 11.6), QPointF(12, 11.6), QPointF(12, 13.8))
    p.drawPath(q)
    p.save()
    p.setBrush(p.pen().color())
    p.drawEllipse(QPointF(12, 16.8), 0.6, 0.6)
    p.restore()


def _add(p: QPainter) -> None:
    p.drawLine(QPointF(12, 5), QPointF(12, 19))
    p.drawLine(QPointF(5, 12), QPointF(19, 12))


def _folder(p: QPainter) -> None:
    f = QPainterPath(QPointF(3.5, 7))
    f.quadTo(QPointF(3.5, 5), QPointF(5.5, 5))
    f.lineTo(QPointF(9.5, 5))
    f.lineTo(QPointF(11.5, 7.5))
    f.lineTo(QPointF(18.5, 7.5))
    f.quadTo(QPointF(20.5, 7.5), QPointF(20.5, 9.5))
    f.lineTo(QPointF(20.5, 17.5))
    f.quadTo(QPointF(20.5, 19.5), QPointF(18.5, 19.5))
    f.lineTo(QPointF(5.5, 19.5))
    f.quadTo(QPointF(3.5, 19.5), QPointF(3.5, 17.5))
    f.closeSubpath()
    p.drawPath(f)


def _link(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(2.5, 8.5, 11, 7), 3.5, 3.5)
    p.drawRoundedRect(QRectF(10.5, 8.5, 11, 7), 3.5, 3.5)


def _check(p: QPainter) -> None:
    c = QPainterPath(QPointF(5.5, 12.5))
    c.lineTo(QPointF(10, 17))
    c.lineTo(QPointF(18.5, 7.5))
    p.drawPath(c)


def _down(p: QPainter) -> None:
    c = QPainterPath(QPointF(6.5, 9.5))
    c.lineTo(QPointF(12, 15))
    c.lineTo(QPointF(17.5, 9.5))
    p.drawPath(c)


def _up(p: QPainter) -> None:
    c = QPainterPath(QPointF(6.5, 14.5))
    c.lineTo(QPointF(12, 9))
    c.lineTo(QPointF(17.5, 14.5))
    p.drawPath(c)


ICONS = {
    "studio": _studio, "library": _library, "research": _research, "niches": _niches,
    "automation": _automation, "accounts": _accounts, "queue": _queue, "performance": _performance,
    "settings": _settings, "help": _help, "add": _add, "folder": _folder, "link": _link, "check": _check,
    "down": _down, "up": _up,
}


def pixmap(name: str, colour: str, size: int = 18, ratio: float = 2.0, stroke: float = STROKE) -> QPixmap:
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 24, size / 24)
    pen = QPen(QColor(colour), stroke * 24 / size, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    ICONS[name](p)
    p.end()
    return pm


def icon(name: str, colour: str | None = None, selected: str | None = None, size: int = 18) -> QIcon:
    """`name` as a QIcon in `colour` (the theme's text colour by default), drawn in `selected` when its
    row or button is selected."""
    from . import theme

    ic = QIcon()
    for ratio in (1.0, 1.5, 2.0):
        ic.addPixmap(pixmap(name, colour or theme.TEXT, size, ratio), QIcon.Normal, QIcon.Off)
        if selected:
            ic.addPixmap(pixmap(name, selected, size, ratio), QIcon.Selected, QIcon.Off)
            ic.addPixmap(pixmap(name, selected, size, ratio), QIcon.Normal, QIcon.On)
    return ic


_FOLDER: Path | None = None


def resource_dir() -> Path:
    global _FOLDER
    if _FOLDER is None:
        _FOLDER = Path(tempfile.gettempdir()) / "clipmonster-ui"
    return _FOLDER


def image(name: str) -> str:
    """A style-sheet image saved by register_resources(), as a path Qt's style sheets load (forward
    slashes)."""
    from . import theme

    return (resource_dir() / f"{name}-{theme.MODE}.png").as_posix()


def check_image() -> str:
    return image("check")


def register_resources() -> None:
    """Save the images the style sheet uses, at 1x and 2x: the check-box tick and the arrows of drop-downs
    and number boxes, in the current mode's colours."""
    from . import theme

    folder = resource_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name, colour, size, stroke in (("check", "#FFFFFF", 14, 2.4), ("down", theme.MUTED, 14, 1.8),
                                           ("up", theme.MUTED, 14, 1.8)):
            for suffix, ratio in (("", 1.0), ("@2x", 2.0)):
                pm = pixmap(name, colour, size, ratio, stroke=stroke)
                pm.setDevicePixelRatio(1.0)
                pm.save(str(folder / f"{name}-{theme.MODE}{suffix}.png"))
    except OSError:
        pass

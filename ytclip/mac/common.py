"""Small pieces the Mac app's pages share: words for times and sizes, labels, cards and state chips."""

from __future__ import annotations

import datetime as dt
import html
import time

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..gui import theme

GB = 1024 ** 3


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def trim(text: str, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip(" ,.;:-") + "…"


def plural(n: int, word: str, many: str = "") -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def when(ts: float | None, now: float | None = None) -> str:
    """'Today 14:30', 'Tomorrow 09:00', 'Wed 18:00', '12 Nov 18:00'."""
    if not ts:
        return "—"
    now = time.time() if now is None else now
    d, today = dt.datetime.fromtimestamp(ts), dt.datetime.fromtimestamp(now).date()
    hm = d.strftime("%H:%M")
    days = (d.date() - today).days
    if days == 0:
        return f"Today {hm}"
    if days == 1:
        return f"Tomorrow {hm}"
    if days == -1:
        return f"Yesterday {hm}"
    if 1 < days < 7:
        return f"{d.strftime('%a')} {hm}"
    return f"{d.day} {d.strftime('%b')} {hm}"


def ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return ""
    s = max(0, int((time.time() if now is None else now) - ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def length(seconds: float | None) -> str:
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}" if s else ""


def size(n: int | None) -> str:
    n = int(n or 0)
    if n >= GB:
        return f"{n / GB:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MB"
    return f"{max(1, n // 1024)} KB" if n else ""


def number(n: int | None) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1000:.0f}K"
    if n >= 1000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


# What a video's state is called, and its colour (theme names, read when drawn so light/dark both work)
STATES = {
    "review": ("Needs your OK", "WARN"),
    "queued": ("Queued", "ACCENT"),
    "posting": ("Posting now", "ACCENT_2"),
    "failed": ("Didn't post", "BAD"),
    "library": ("Not queued", "MUTED"),
    "posted": ("Posted", "GOOD"),
}


def state_colour(state: str) -> str:
    return getattr(theme, STATES.get(state, ("", "MUTED"))[1])


class WrapLabel(QLabel):
    """A word-wrapped label that's as tall as its text at the width it has."""

    def __init__(self, text: str = "", parent=None, muted: bool = False):
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.RichText)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        if muted:
            self.setProperty("muted", True)


def title(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("title")
    return lab


def h2(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("h2")
    return lab


def panel(margins: int = 14, spacing: int = 8) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("panel")
    v = QVBoxLayout(f)
    v.setContentsMargins(margins, margins - 2, margins, margins)
    v.setSpacing(spacing)
    return f, v


class Chip(QLabel):
    """A small rounded label in a state's colour."""

    def __init__(self, text: str = "", colour: str = "", parent=None):
        super().__init__(text, parent)
        self.set(text, colour)

    def set(self, text: str, colour: str = "") -> None:
        c = QColor(colour or theme.MUTED)
        self.setText(text)
        self.setStyleSheet(f"QLabel {{ color: {c.name()}; background: rgba({c.red()},{c.green()},{c.blue()},0.16);"
                           f" border-radius: 9px; padding: 2px 9px; font-weight: 600; font-size: 8.5pt; }}")
        self.setVisible(bool(text))


class StatCard(QFrame):
    """A number with a label under it; clickable when `clicked` is connected."""

    clicked = Signal()

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setCursor(Qt.PointingHandCursor)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 11)
        v.setSpacing(2)
        self.value = QLabel("–")
        self.value.setStyleSheet("font-size: 20pt; font-weight: 700;")
        self.label = QLabel(label)
        self.label.setProperty("muted", True)
        self.note = QLabel("")
        self.note.setProperty("muted", True)
        self.note.setStyleSheet("font-size: 8.5pt;")
        v.addWidget(self.value)
        v.addWidget(self.label)
        v.addWidget(self.note)
        self.setMinimumWidth(130)

    def set(self, value, note: str = "", colour: str = "") -> None:
        self.value.setText(str(value))
        self.value.setStyleSheet(f"font-size: 20pt; font-weight: 700; color: {colour or theme.TEXT};")
        self.note.setText(note)
        self.note.setVisible(bool(note))

    def mouseReleaseEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class Dot(QWidget):
    """A coloured connection dot."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.colour = theme.MUTED
        self.setFixedSize(QSize(10, 10))

    def set(self, colour: str) -> None:
        self.colour = colour
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.colour))
        p.drawEllipse(1, 1, 8, 8)
        p.end()


def clear(layout) -> None:
    """Remove (and delete) everything in a layout."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()  # gone from the screen now, not only when the event loop gets to deleting it
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            clear(item.layout())


def row(*widgets, stretch_after: bool = True, spacing: int = 8) -> QHBoxLayout:
    h = QHBoxLayout()
    h.setSpacing(spacing)
    for w in widgets:
        if isinstance(w, int):
            h.addSpacing(w)
        else:
            h.addWidget(w)
    if stretch_after:
        h.addStretch(1)
    return h

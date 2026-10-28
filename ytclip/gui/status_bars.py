"""Status bars: a labelled progress bar, a video's steps, and the Automation tab's "Now" panel.

They show a steps.Status: the step a job is on, how far along the whole job is, and about how long
is left.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QStyledItemDelegate, QVBoxLayout, QWidget

from .. import steps
from ..steps import Status
from . import theme

BAR_ROLE = Qt.UserRole + 7  # a table cell's progress (0-1) for ProgressDelegate; None draws no bar


class LabeledBar(QProgressBar):
    """A progress bar tall enough to read, with its percentage and time left written inside."""

    def __init__(self, parent=None, height: int = 18):
        super().__init__(parent)
        self.setObjectName("labeled")
        self.setFixedHeight(height)
        self.setRange(0, 1000)
        self.setTextVisible(True)
        self.setAlignment(Qt.AlignCenter)

    def set_fraction(self, frac: float | None, text: str = "") -> None:
        """frac 0-1, or None for "busy, can't tell how far" (a moving bar)."""
        if frac is None:
            self.setRange(0, 0)
            self.setFormat(text)
            return
        self.setRange(0, 1000)
        self.setValue(int(max(0.0, min(1.0, frac)) * 1000))
        self.setFormat(text or f"{frac:.0%}")

    def set_status(self, status: Status | None) -> None:
        if status is None:
            self.set_fraction(None)
            return
        self.set_fraction(status.overall, status.bar_text())


class StepChips(QWidget):
    """A video's steps in a row: done ✓, the current one highlighted, the rest still to come."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(4)
        self.chips: list[QLabel] = []

    def texts(self) -> list[str]:
        return [c.text() for c in self.chips]

    def show_status(self, status: Status | None) -> None:
        plan = list(status.plan) if status and status.plan else []
        while len(self.chips) < len(plan):
            chip = QLabel()
            chip.setAlignment(Qt.AlignCenter)
            self.row.insertWidget(len(self.chips), chip)
            self.chips.append(chip)
        for i, chip in enumerate(self.chips):
            if i >= len(plan):
                chip.setVisible(False)
                continue
            key = plan[i]
            label = steps.LABELS.get(key, key)
            if key in status.done:
                text, fg, bg, border = f"✓ {label}", theme.GOOD, theme.PANEL, theme.BORDER
            elif key == status.step:
                pct = f" {status.step_frac:.0%}" if status.step_frac is not None else ""
                text, fg, bg, border = f"● {label}{pct}", theme.TEXT, theme.PANEL_2, theme.ACCENT
            else:
                text, fg, bg, border = label, theme.MUTED, theme.PANEL, theme.BORDER
            chip.setText(text)
            chip.setStyleSheet(f"color:{fg}; background:{bg}; border:1px solid {border}; border-radius:9px; "
                               "padding:2px 9px; font-size:9pt;")
            chip.setVisible(True)
        if self.row.count() == len(self.chips):
            self.row.addStretch(1)


class NowPanel(QFrame):
    """What automation is doing right now: the video, its steps, a bar for the whole video with the
    time left, and the step's own message. Or, between videos, what it's checking."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 9)
        lay.setSpacing(5)
        top = QHBoxLayout()
        self.title = QLabel()
        self.title.setStyleSheet("font-weight:600;")
        self.title.setTextFormat(Qt.RichText)
        self.right = QLabel()
        self.right.setProperty("muted", True)
        top.addWidget(self.title, 1)
        top.addWidget(self.right)
        lay.addLayout(top)
        self.chips = StepChips()
        lay.addWidget(self.chips)
        self.bar = LabeledBar()
        lay.addWidget(self.bar)
        self.detail = QLabel()
        self.detail.setProperty("muted", True)
        lay.addWidget(self.detail)

    def show_job(self, title: str, status: Status | None, stage: str = "") -> None:
        self.title.setText(f"✂ Clipping <span style='color:{theme.TEXT}'>{_esc(title)}</span>")
        self.title.setToolTip(title)
        if status is None:  # started by an older runner, or just starting
            self.right.setText("")
            self.chips.show_status(None)
            self.chips.setVisible(False)
            self.bar.set_fraction(None)
            self.detail.setText(stage or "Starting")
        else:
            self.right.setText(status.headline())
            self.chips.show_status(status)
            self.chips.setVisible(bool(status.plan))
            self.bar.set_status(status)
            self.detail.setText(status.detail() or stage)
        self.setVisible(True)

    def show_activity(self, text: str, frac: float | None) -> None:
        head, _, rest = text.partition(": ")
        self.title.setText(f"🔎 {_esc(head)}")
        self.title.setToolTip(text)
        self.right.setText("")
        self.chips.setVisible(False)
        self.bar.set_fraction(frac, f"{frac:.0%}" if frac is not None else "")
        self.detail.setText(rest)
        self.setVisible(True)

    def idle(self) -> None:
        self.setVisible(False)


class ProgressDelegate(QStyledItemDelegate):
    """Draws a thin progress bar along the bottom of a table cell whose BAR_ROLE holds 0-1."""

    def paint(self, painter: QPainter, option, index) -> None:
        super().paint(painter, option, index)
        frac = index.data(BAR_ROLE)
        if frac is None:
            return
        try:
            frac = max(0.0, min(1.0, float(frac)))
        except (TypeError, ValueError):
            return
        r = option.rect
        track = QRect(r.left() + 4, r.bottom() - 5, r.width() - 8, 3)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.BORDER))
        painter.drawRoundedRect(track, 1.5, 1.5)
        if frac > 0:
            done = QRect(track.left(), track.top(), max(3, int(track.width() * frac)), track.height())
            painter.setBrush(QColor(theme.ACCENT_2))
            painter.drawRoundedRect(done, 1.5, 1.5)
        painter.restore()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

"""A layout that lines its widgets up in a row and wraps them onto the next line when the row runs out of
room (Qt's own Flow Layout example): the Studio's timeline legend, so a narrower window, such as a 13-inch
MacBook's, wraps the signals instead of cutting their names off."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QWidget


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 - Qt's name
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._lay_out(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._lay_out(rect, move=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        """Everything on one row."""
        shown = [i for i in self._items if not i.isEmpty()]
        if not shown:
            return QSize(0, 0)
        m = self.contentsMargins()
        width = sum(i.sizeHint().width() for i in shown) + self._spacing * (len(shown) - 1)
        height = max(i.sizeHint().height() for i in shown)
        return QSize(width + m.left() + m.right(), height + m.top() + m.bottom())

    def minimumSize(self) -> QSize:  # noqa: N802
        """The widest one: the rest can wrap."""
        size = QSize()
        for i in self._items:
            if not i.isEmpty():
                size = size.expandedTo(i.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _lay_out(self, rect: QRect, move: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line = area.x(), area.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:  # no room left on this row: the next one
                x, y, line = area.x(), y + line + self._spacing, 0
            if move:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line = max(line, hint.height())
        return y + line - rect.y() + m.bottom()


def flow_widget(widgets, spacing: int = 6) -> QWidget:
    """`widgets` in a FlowLayout, as a widget that takes the room it's given and grows down to wrap."""
    box = QWidget()
    lay = FlowLayout(box, spacing)
    for w in widgets:
        lay.addWidget(w)
    policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
    policy.setHeightForWidth(True)
    box.setSizePolicy(policy)
    return box

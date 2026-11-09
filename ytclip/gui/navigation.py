"""Sidebar navigation: the app's sections in a left rail, the way pro desktop apps lay them out.

`NavTabs` is a QTabWidget whose own tab strip is hidden; its `sidebar` widget (put on the window's left)
lists the same pages under section headings, each with a line icon and an optional count badge. Everything
that worked with the old top tabs (addTab, setCurrentWidget, setTabText, currentChanged) still works, and
the sidebar follows.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QTabWidget, QVBoxLayout, QWidget)

from . import icons, theme

PAGE = Qt.UserRole + 1   # the tab index a row opens (None for a heading)
BADGE = Qt.UserRole + 2  # a count shown at the row's right
ICON = Qt.UserRole + 3   # the icon name
ROW_H = 34
HEAD_H = 30


class _Delegate(QStyledItemDelegate):
    """Rows like a macOS / Windows 11 sidebar: a rounded highlight, icon, label and a pill badge."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), ROW_H if index.data(PAGE) is not None else HEAD_H)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index) -> None:
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(8, 1, -8, -1)
        text = index.data(Qt.DisplayRole) or ""
        if index.data(PAGE) is None:  # section heading
            f = QFont(option.font)
            f.setPointSizeF(max(7.0, f.pointSizeF() * 0.8))
            f.setWeight(QFont.DemiBold)
            f.setLetterSpacing(QFont.PercentageSpacing, 106)
            p.setFont(f)
            p.setPen(QColor(theme.MUTED))
            p.drawText(r.adjusted(10, 0, 0, -4), Qt.AlignLeft | Qt.AlignBottom, text.upper())
            p.restore()
            return
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        if selected:
            p.setPen(Qt.NoPen)
            p.setBrush(_qcolor(theme.soft(alpha=0.26 if theme.is_dark() else 0.16)))
            p.drawRoundedRect(QRectF(r), 7, 7)
        elif hover:
            p.setPen(Qt.NoPen)
            c = QColor(theme.TEXT)
            c.setAlphaF(0.06)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(r), 7, 7)
        name = index.data(ICON)
        x = r.x() + 10
        if name:
            colour = theme.ACCENT if selected else theme.MUTED
            pm = icons.pixmap(name, colour, 18, p.device().devicePixelRatioF() if p.device() else 2.0)
            p.drawPixmap(x, r.y() + (r.height() - 18) // 2, pm)
            x += 18 + 10
        f = QFont(option.font)
        if selected:
            f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT))
        badge = index.data(BADGE) or 0
        right = r.right() - 8
        if badge:
            label = str(badge) if badge < 100 else "99+"
            bf = QFont(option.font)
            bf.setPointSizeF(max(7.0, bf.pointSizeF() * 0.82))
            bf.setWeight(QFont.DemiBold)
            p.setFont(bf)
            w = max(20, p.fontMetrics().horizontalAdvance(label) + 12)
            pill = QRect(right - w, r.y() + (r.height() - 18) // 2, w, 18)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.ACCENT))
            p.drawRoundedRect(QRectF(pill), 9, 9)
            p.setPen(QColor(theme.ON_ACCENT))
            p.drawText(pill, Qt.AlignCenter, label)
            right = pill.left() - 6
            p.setFont(f)
            p.setPen(QColor(theme.TEXT))
        p.drawText(QRect(x, r.y(), right - x, r.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(text, Qt.ElideRight, right - x))
        p.restore()


def _qcolor(css: str) -> QColor:
    """A QColor from the style sheet's rgba(r, g, b, a) or #hex."""
    if css.startswith("rgba("):
        r, g, b, a = (v.strip() for v in css[5:-1].split(","))
        c = QColor(int(r), int(g), int(b))
        c.setAlphaF(float(a))
        return c
    return QColor(css)


class Sidebar(QWidget):
    """The rail: the app name at the top, the sections, and a footer (the version) at the bottom."""

    picked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(212)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 12, 0, 12)
        lay.setSpacing(6)
        self.top = QVBoxLayout()
        self.top.setContentsMargins(16, 2, 16, 8)
        lay.addLayout(self.top)
        self.list = QListWidget()
        self.list.setObjectName("nav")
        self.list.setItemDelegate(_Delegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setUniformItemSizes(False)
        self.list.currentItemChanged.connect(self._changed)
        lay.addWidget(self.list, 1)
        self.bottom = QVBoxLayout()
        self.bottom.setContentsMargins(16, 0, 16, 0)
        lay.addLayout(self.bottom)

    def heading(self, text: str) -> None:
        it = QListWidgetItem(text)
        it.setFlags(Qt.NoItemFlags)
        it.setData(PAGE, None)
        self.list.addItem(it)

    def add(self, page: int, text: str, icon: str = "") -> QListWidgetItem:
        it = QListWidgetItem(text)
        it.setData(PAGE, page)
        it.setData(ICON, icon)
        it.setToolTip(text)
        self.list.addItem(it)
        return it

    def item_for(self, page: int) -> QListWidgetItem | None:
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(PAGE) == page:
                return it
        return None

    def select(self, page: int) -> None:
        it = self.item_for(page)
        if it is not None and self.list.currentItem() is not it:
            self.list.setCurrentItem(it)

    def _changed(self, it: QListWidgetItem | None, _old=None) -> None:
        if it is not None and it.data(PAGE) is not None:
            self.picked.emit(int(it.data(PAGE)))


class NavTabs(QTabWidget):
    """A QTabWidget driven by a sidebar instead of its tab strip."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tabBar().hide()
        self.setDocumentMode(True)
        self.sidebar = Sidebar()
        self.sidebar.picked.connect(self.setCurrentIndex)
        self.currentChanged.connect(self.sidebar.select)

    def section(self, title: str) -> None:
        """Start a heading in the sidebar; the next pages added go under it."""
        self.sidebar.heading(title)

    def addTab(self, widget: QWidget, text: str, icon: str = "") -> int:  # type: ignore[override]
        i = super().addTab(widget, text)
        self.sidebar.add(i, text, icon)
        if self.count() == 1:
            self.sidebar.select(0)
        return i

    def setTabText(self, index: int, text: str) -> None:  # type: ignore[override]
        super().setTabText(index, text)
        it = self.sidebar.item_for(index)
        if it is not None:
            it.setText(text)

    def set_hidden(self, index: int, hidden: bool) -> None:
        """Hide a page's row in the sidebar (or show it again); the page stays where it is."""
        it = self.sidebar.item_for(index)
        if it is not None:
            it.setHidden(hidden)

    def is_hidden(self, index: int) -> bool:
        it = self.sidebar.item_for(index)
        return it is None or it.isHidden()

    def set_badge(self, index: int, count: int) -> None:
        """A count at the right of the page's row (0 hides it)."""
        it = self.sidebar.item_for(index)
        if it is not None:
            it.setData(BADGE, max(0, int(count)))
            tip = super().tabText(index)
            it.setToolTip(f"{tip}: {count} new" if count else tip)

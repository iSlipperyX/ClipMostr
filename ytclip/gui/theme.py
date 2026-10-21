"""Dark theme, colours and the app icon."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

BG = "#0F1117"
PANEL = "#161922"
PANEL_2 = "#1D2130"
BORDER = "#2A2F40"
TEXT = "#E6E8EF"
MUTED = "#8A90A6"
ACCENT = "#8B5CF6"
ACCENT_2 = "#22D3EE"
GOOD = "#34D399"
WARN = "#FBBF24"
BAD = "#F87171"

SIGNAL_COLOURS = {
    "heatmap": "#F59E0B",
    "audio": "#22D3EE",
    "reactions": "#F472B6",
    "scenes": "#A78BFA",
    "keywords": "#34D399",
}

QSS = f"""
* {{ font-family: "Segoe UI", "Inter", sans-serif; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#panel, QFrame#panel {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; }}
QLabel#title {{ font-size: 15pt; font-weight: 700; }}
QLabel#h2 {{ font-size: 11pt; font-weight: 600; color: {TEXT}; }}
QLabel#muted, QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel#brand {{ font-size: 14pt; font-weight: 800; color: {TEXT}; letter-spacing: 0.5px; }}
QToolTip {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}

QPushButton {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 7px; padding: 6px 12px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {MUTED}; border-color: {PANEL_2}; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: white; font-weight: 600; padding: 8px 16px; }}
QPushButton#primary:hover {{ background: #9D74FF; }}
QPushButton#primary:disabled {{ background: #4B3A80; color: #B9B3CC; }}
QPushButton#icon {{ padding: 5px 8px; min-width: 26px; }}
QPushButton:checked {{ background: #2C2350; border-color: {ACCENT}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 7px; padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit#bigInput {{ font-size: 11pt; padding: 9px 12px; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {PANEL_2}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; padding: 8px 14px; color: {MUTED}; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

QListWidget, QTableWidget, QTreeWidget {{ background: {PANEL}; border: none; outline: none; }}
QListWidget::item {{ border-radius: 8px; }}
QListWidget::item:selected {{ background: #2C2350; }}
QTableWidget {{ gridline-color: {BORDER}; }}
QTableWidget::item:selected {{ background: #2C2350; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER}; padding: 5px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}

QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: white; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}

QProgressBar {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 5px; height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2}); border-radius: 4px; }}

QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: {BG}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QSplitter::handle {{ background: {BG}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 8px; margin-top: 14px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {MUTED}; }}
QScrollArea {{ border: none; background: transparent; }}
QMenu {{ background: {PANEL_2}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
"""


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    for role, colour in (
        (QPalette.Window, BG), (QPalette.WindowText, TEXT), (QPalette.Base, BG), (QPalette.AlternateBase, PANEL),
        (QPalette.Text, TEXT), (QPalette.Button, PANEL_2), (QPalette.ButtonText, TEXT),
        (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, "#FFFFFF"), (QPalette.ToolTipBase, PANEL_2),
        (QPalette.ToolTipText, TEXT), (QPalette.PlaceholderText, MUTED),
    ):
        pal.setColor(role, QColor(colour))
    app.setPalette(pal)
    app.setStyleSheet(QSS)


def app_icon_pixmap(size: int = 256) -> QPixmap:
    """A purple gradient tile with a play triangle being sliced by a cut line."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size
    grad = QLinearGradient(0, 0, s, s)
    grad.setColorAt(0, QColor("#A855F7"))
    grad.setColorAt(1, QColor("#4F46E5"))
    p.setBrush(QBrush(grad))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92), s * 0.22, s * 0.22)

    tri = QPainterPath()
    tri.moveTo(QPointF(s * 0.36, s * 0.27))
    tri.lineTo(QPointF(s * 0.36, s * 0.73))
    tri.lineTo(QPointF(s * 0.75, s * 0.50))
    tri.closeSubpath()
    p.setBrush(QColor("white"))
    p.drawPath(tri)

    pen = QPen(QColor("#22D3EE"), s * 0.07, Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(s * 0.22, s * 0.80), QPointF(s * 0.80, s * 0.20))
    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_icon_pixmap(size))
    return icon

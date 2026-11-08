"""The look: colours (dark and light), the style sheet and the app icon.

A pro-app look in the spirit of Apple's and Adobe's desktop apps: neutral graphite (or soft white) surfaces
instead of tinted ones, a single blue accent for what's selected or primary, generous rounded corners,
quiet borders, and a sidebar for the app's sections (navigation.py).

The colours are module constants (theme.TEXT, theme.ACCENT, ...) that the rest of the GUI reads when it
builds or paints. `use()` picks dark or light before any window is built; the appearance setting
(Settings → General → Appearance) applies the next time ClipMonster starts, so every widget agrees.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

DARK = {
    "BG": "#1C1C1E",        # window
    "SIDEBAR": "#232325",   # navigation rail
    "PANEL": "#262628",     # cards and lists
    "PANEL_2": "#313134",   # buttons, raised controls
    "FIELD": "#1A1A1C",     # text fields
    "BORDER": "#3A3A3D",
    "HAIRLINE": "#2E2E31",  # separators inside panels
    "TEXT": "#F2F2F7",
    "MUTED": "#9A9AA1",
    "ACCENT": "#0A84FF",
    "ACCENT_HOVER": "#3395FF",
    "ACCENT_2": "#64D2FF",
    "GOOD": "#30D158",
    "WARN": "#FFD60A",
    "BAD": "#FF453A",
    "ON_ACCENT": "#FFFFFF",
}
LIGHT = {
    "BG": "#F5F5F7",
    "SIDEBAR": "#EBEBEF",
    "PANEL": "#FFFFFF",
    "PANEL_2": "#FFFFFF",
    "FIELD": "#FFFFFF",
    "BORDER": "#D2D2D7",
    "HAIRLINE": "#E5E5EA",
    "TEXT": "#1D1D1F",
    "MUTED": "#6E6E73",
    "ACCENT": "#007AFF",
    "ACCENT_HOVER": "#1A88FF",
    "ACCENT_2": "#0071A4",
    "GOOD": "#248A3D",
    "WARN": "#B25000",
    "BAD": "#D70015",
    "ON_ACCENT": "#FFFFFF",
}

MODE = "dark"
BG = SIDEBAR = PANEL = PANEL_2 = FIELD = BORDER = HAIRLINE = TEXT = MUTED = ""
ACCENT = ACCENT_HOVER = ACCENT_2 = GOOD = WARN = BAD = ON_ACCENT = ""

SIGNAL_COLOURS = {
    "heatmap": "#FF9F0A",
    "audio": "#64D2FF",
    "reactions": "#FF6482",
    "scenes": "#BF5AF2",
    "keywords": "#30D158",
    "voice": "#5E5CE6",
    "emotion": "#FF375F",
}

RADIUS = 10       # panels
RADIUS_CTRL = 7   # buttons and fields
FONT = '"Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Inter", "Helvetica Neue", sans-serif'


def _rgba(hex_rgb: str, alpha: float) -> str:
    c = QColor(hex_rgb)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha:.2f})"


def system_is_dark() -> bool:
    """Whether the OS is set to dark mode (Windows' "app mode"; Qt's colour scheme elsewhere). Dark when
    it can't tell."""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        except OSError:
            return True
    try:
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is not None:
            return app.styleHints().colorScheme() != Qt.ColorScheme.Light
    except Exception:
        pass
    return True


def use(appearance: str = "dark") -> str:
    """Switch the colour constants to `appearance` ("dark", "light" or "system"). Returns the mode used.
    Call before building windows: widgets read the constants as they're made."""
    global MODE
    mode = appearance if appearance in ("dark", "light") else ("dark" if system_is_dark() else "light")
    MODE = mode
    globals().update(DARK if mode == "dark" else LIGHT)
    return mode


use("dark")


def is_dark() -> bool:
    return MODE == "dark"


def soft(colour: str | None = None, alpha: float | None = None) -> str:
    """A see-through tint of `colour` (the accent by default) for selected rows and chips."""
    return _rgba(colour or ACCENT, alpha if alpha is not None else (0.22 if is_dark() else 0.14))


def stylesheet() -> str:
    from . import icons

    sel = soft()
    sel_strong = soft(alpha=0.32 if is_dark() else 0.22)
    hover = _rgba(TEXT, 0.06)
    pressed = _rgba(TEXT, 0.10)
    scroll = _rgba(TEXT, 0.22)
    scroll_hover = _rgba(TEXT, 0.36)
    disabled = _rgba(TEXT, 0.32)
    focus = _rgba(ACCENT, 0.55)
    raised = "#3A3A3D" if is_dark() else "#FFFFFF"
    seg_bg = _rgba(TEXT, 0.07)
    return f"""
* {{ font-family: {FONT}; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#page {{ background: {BG}; }}
QWidget#panel, QFrame#panel {{ background: {PANEL}; border: 1px solid {HAIRLINE}; border-radius: {RADIUS}px; }}
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 17pt; font-weight: 700; }}
QLabel#h2 {{ font-size: 11pt; font-weight: 600; color: {TEXT}; }}
QLabel#muted, QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel#brand {{ font-size: 13pt; font-weight: 700; color: {TEXT}; }}
QLabel#version {{ color: {MUTED}; font-size: 8.5pt; }}
QToolTip {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 8px; }}

QPushButton, QToolButton {{
    background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: {RADIUS_CTRL}px; padding: 6px 14px;
}}
QPushButton:hover, QToolButton:hover {{ background: {raised if not is_dark() else '#38383B'}; border-color: {_rgba(TEXT, 0.22)}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {pressed}; }}
QPushButton:focus {{ border-color: {focus}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {disabled}; background: {seg_bg}; border-color: transparent; }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: {ON_ACCENT}; font-weight: 600; padding: 7px 18px; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background: {ACCENT}; }}
QPushButton#primary:disabled {{ background: {_rgba(ACCENT, 0.35)}; border-color: transparent; color: {_rgba(ON_ACCENT, 0.7)}; }}
QPushButton#icon {{ padding: 5px 8px; min-width: 26px; }}
QPushButton#flat, QToolButton#flat {{ background: transparent; border: 1px solid transparent; }}
QPushButton#flat:hover, QToolButton#flat:hover {{ background: {hover}; }}
QPushButton:checked {{ background: {sel}; border-color: {ACCENT}; color: {TEXT}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit, QTimeEdit, QDateTimeEdit {{
    background: {FIELD}; border: 1px solid {BORDER}; border-radius: {RADIUS_CTRL}px; padding: 5px 9px;
    selection-background-color: {ACCENT}; selection-color: {ON_ACCENT};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover, QTimeEdit:hover {{ border-color: {_rgba(TEXT, 0.25)}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus,
QTimeEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {disabled}; }}
QLineEdit#bigInput {{ font-size: 10.5pt; padding: 8px 14px; border-radius: 9px; }}
QLineEdit#search {{ padding-left: 30px; }}
QComboBox {{ padding-right: 24px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({icons.image('down')}); width: 14px; height: 14px; margin-right: 8px; }}
QComboBox QAbstractItemView {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px;
    selection-background-color: {ACCENT}; selection-color: {ON_ACCENT}; outline: none; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
QTimeEdit::up-button, QTimeEdit::down-button {{ border: none; width: 16px; background: transparent; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QTimeEdit::up-arrow {{ image: url({icons.image('up')});
    width: 11px; height: 11px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QTimeEdit::down-arrow {{ image: url({icons.image('down')});
    width: 11px; height: 11px; }}

QTabWidget::pane {{ border: none; }}
QTabWidget::tab-bar {{ alignment: left; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 5px 13px; margin: 2px 2px 8px 0;
    border: 1px solid transparent; border-radius: 7px; }}
QTabBar::tab:hover {{ color: {TEXT}; background: {hover}; }}
QTabBar::tab:selected {{ color: {TEXT}; background: {raised}; border-color: {BORDER}; font-weight: 600; }}

QListWidget, QTableWidget, QTreeWidget, QListView, QTableView, QTreeView {{
    background: {PANEL}; border: none; outline: none; alternate-background-color: {_rgba(TEXT, 0.025)};
}}
QListWidget::item, QListView::item {{ border-radius: 7px; padding: 2px; }}
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {hover}; }}
QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected, QTableView::item:selected {{
    background: {sel_strong}; color: {TEXT};
}}
QTableWidget, QTableView {{ gridline-color: {HAIRLINE}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: none; border-bottom: 1px solid {HAIRLINE};
    padding: 6px 8px; font-size: 9pt; font-weight: 600; }}
QTableCornerButton::section {{ background: {PANEL}; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {scroll}; border-radius: 3px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {scroll_hover}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {scroll}; border-radius: 3px; min-width: 32px; }}
QScrollBar::handle:horizontal:hover {{ background: {scroll_hover}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSlider::groove:horizontal {{ height: 4px; background: {_rgba(TEXT, 0.16)}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #FFFFFF; border: 1px solid {_rgba('#000000', 0.18)}; width: 16px; height: 16px;
    margin: -7px 0; border-radius: 8px; }}

QProgressBar {{ background: {_rgba(TEXT, 0.10)}; border: none; border-radius: 4px; height: 8px; text-align: center;
    color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QProgressBar[done="true"]::chunk {{ background: {GOOD}; }}
QProgressBar#labeled {{ height: 18px; border-radius: 9px; color: {TEXT}; font-size: 8pt; font-weight: 600; }}
QProgressBar#labeled::chunk {{ border-radius: 9px; }}

QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {_rgba(TEXT, 0.30)};
    background: {FIELD}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT};
    image: url({icons.check_image()}); }}
QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 8px; border: 1px solid {_rgba(TEXT, 0.30)};
    background: {FIELD}; }}
QRadioButton::indicator:checked {{ background: {FIELD}; border: 5px solid {ACCENT}; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:vertical {{ height: 8px; }}
QGroupBox {{ border: 1px solid {HAIRLINE}; border-radius: {RADIUS}px; margin-top: 16px; padding-top: 10px;
    background: {PANEL}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {MUTED}; font-weight: 600; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QMenu {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 9px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {ACCENT}; color: {ON_ACCENT}; }}
QMenu::separator {{ height: 1px; background: {HAIRLINE}; margin: 4px 6px; }}
QMessageBox {{ background: {BG}; }}

/* Research's command center (research_dashboard.py): filter pills, the view switch, cluster cards */
QPushButton#pill {{ background: {seg_bg}; border: 1px solid transparent; border-radius: 12px; padding: 3px 11px;
    color: {MUTED}; }}
QPushButton#pill:hover {{ background: {hover}; color: {TEXT}; }}
QPushButton#pill:checked {{ background: {sel}; border-color: {ACCENT}; color: {TEXT}; font-weight: 600; }}
QFrame#segbar {{ background: {seg_bg}; border: none; border-radius: 8px; }}
QPushButton#seg {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 4px 12px;
    color: {MUTED}; }}
QPushButton#seg:hover {{ color: {TEXT}; background: transparent; }}
QPushButton#seg:checked {{ background: {raised}; border-color: {BORDER}; color: {TEXT}; font-weight: 600; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {HAIRLINE}; border-radius: {RADIUS}px; }}
QFrame#card:hover {{ border-color: {_rgba(ACCENT, 0.55)}; }}
QFrame#card[selected="true"] {{ border-color: {ACCENT}; }}
QFrame#vrow {{ background: transparent; border: none; border-radius: 6px; }}
QFrame#vrow:hover {{ background: {hover}; }}
QFrame#banner {{ background: {soft(ACCENT, 0.12 if is_dark() else 0.08)}; border: 1px solid {_rgba(ACCENT, 0.35)};
    border-radius: {RADIUS_CTRL}px; }}

/* Sidebar navigation (navigation.py) */
QWidget#sidebar {{ background: {SIDEBAR}; border-right: 1px solid {HAIRLINE}; }}
QListWidget#nav {{ background: transparent; border: none; }}
QWidget#toolbar {{ background: {BG}; }}
QFrame#hairline {{ background: {HAIRLINE}; border: none; max-height: 1px; min-height: 1px; }}
QWidget#statusbar {{ background: {BG}; }}
"""


# Kept for code that read the old constant: the style sheet for the current mode.
QSS = stylesheet()


def palette() -> QPalette:
    pal = QPalette()
    for role, colour in (
        (QPalette.Window, BG), (QPalette.WindowText, TEXT), (QPalette.Base, PANEL),
        (QPalette.AlternateBase, PANEL_2), (QPalette.Text, TEXT), (QPalette.Button, PANEL_2),
        (QPalette.ButtonText, TEXT), (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, ON_ACCENT),
        (QPalette.ToolTipBase, PANEL_2), (QPalette.ToolTipText, TEXT), (QPalette.PlaceholderText, MUTED),
        (QPalette.Link, ACCENT), (QPalette.Mid, BORDER), (QPalette.Midlight, HAIRLINE),
    ):
        pal.setColor(role, QColor(colour))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(MUTED))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(MUTED))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(MUTED))
    return pal


def apply(app: QApplication, appearance: str | None = None) -> None:
    """Style the app. `appearance` ("dark", "light", "system") switches the colours first; leave it out to
    keep what `use()` last chose."""
    global QSS
    if appearance is not None:
        use(appearance)
    from . import icons

    icons.register_resources()
    app.setStyle("Fusion")
    app.setPalette(palette())
    QSS = stylesheet()
    app.setStyleSheet(QSS)


def title_bar(hwnd: int) -> None:
    """Match the Windows title bar to the window: dark or light, and on Windows 11 the window's own colour,
    so the title bar and the sidebar read as one surface."""
    if os.name != "nt":
        return
    try:
        import ctypes

        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1 if is_dark() else 0)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(on), ctypes.sizeof(on))
        c = QColor(SIDEBAR)
        colorref = ctypes.c_uint(c.red() | (c.green() << 8) | (c.blue() << 16))
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 35, ctypes.byref(colorref), ctypes.sizeof(colorref))
    except Exception:
        pass


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

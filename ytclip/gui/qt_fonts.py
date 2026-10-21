"""Make the bundled fonts available to Qt, for the font picker and the layout sketch."""

from __future__ import annotations

from PySide6.QtGui import QFont, QFontDatabase

from ..fonts import BUNDLED, FONTS_DIR

_registered = False


def register() -> None:
    global _registered
    if _registered:
        return
    _registered = True
    for f in BUNDLED.values():
        QFontDatabase.addApplicationFont(str(FONTS_DIR / f.file))


def qfont(name: str, pixel_size: int = 14) -> QFont:
    """A QFont for a font name as the settings store it (bundled or installed)."""
    register()
    b = BUNDLED.get(name.strip())
    font = QFont(b.qt_family if b else name)
    if b:
        font.setWeight(QFont.Weight(b.qt_weight))
    else:
        font.setBold(True)
    font.setPixelSize(max(1, pixel_size))
    return font

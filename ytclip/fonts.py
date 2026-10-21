"""Fonts that ship with ClipMonster, so captions and banners look the same on every PC.

libass is pointed at just the files a render uses (copied next to the .ass file), and the GUI registers
them with Qt so the font picker and layout sketch can draw them too. Any installed font still works by name."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

FONTS_DIR = Path(__file__).with_name("fonts")


@dataclass(frozen=True)
class Font:
    name: str  # what the user picks and what the .ass file asks libass for
    file: str
    qt_family: str  # how Qt names the same face
    qt_weight: int = 400
    caps: float = 0.68  # average advance of A-Z / a-z as a fraction of the font size, for line fitting
    lower: float = 0.58


# Advances measured from each file's hmtx table.
BUNDLED: dict[str, Font] = {f.name: f for f in (
    Font("TikTok Sans Black", "TikTokSansBlack.ttf", "TikTok Sans Black", 400, 0.69, 0.55),
    Font("Montserrat Black", "Montserrat-Black.ttf", "Montserrat", 900, 0.75, 0.63),
    Font("Montserrat ExtraBold", "Montserrat-ExtraBold.ttf", "Montserrat", 800, 0.74, 0.62),
    Font("Poppins Black", "Poppins-Black.ttf", "Poppins", 900, 0.71, 0.62),
    Font("Poppins ExtraBold", "Poppins-ExtraBold.ttf", "Poppins", 800, 0.69, 0.61),
    Font("Archivo Black", "ArchivoBlack-Regular.ttf", "Archivo Black", 400, 0.77, 0.61),
    Font("Anton", "Anton-Regular.ttf", "Anton", 400, 0.47, 0.46),
    Font("Bebas Neue", "BebasNeue-Regular.ttf", "Bebas Neue", 400, 0.39, 0.39),
    Font("Bangers", "Bangers-Regular.ttf", "Bangers", 400, 0.42, 0.42),
    Font("Luckiest Guy", "LuckiestGuy-Regular.ttf", "Luckiest Guy", 400, 0.59, 0.60),
    Font("Lilita One", "LilitaOne-Regular.ttf", "Lilita One", 400, 0.61, 0.49),
    Font("Titan One", "TitanOne-Regular.ttf", "Titan One", 400, 0.69, 0.63),
    Font("Permanent Marker", "PermanentMarker-Regular.ttf", "Permanent Marker", 400, 0.68, 0.55),
)}

# Widths for popular installed fonts people type in by name.
_SYSTEM_WIDTHS = {"arial black": (0.78, 0.66), "impact": (0.50, 0.46), "arial": (0.67, 0.53)}


def bundled(name: str) -> Font | None:
    return BUNDLED.get(name.strip())


def ass_bold(name: str) -> int:
    """ASS 'Bold' flag: bundled faces are already as heavy as intended, so never let libass embolden them."""
    return 0 if bundled(name) else -1


def glyph_width(name: str, uppercase: bool) -> float:
    f = bundled(name)
    if f:
        return f.caps if uppercase else f.lower
    caps, lower = _SYSTEM_WIDTHS.get(name.strip().lower(), (0.70, 0.58))
    return caps if uppercase else lower


def stage(names: list[str], folder: Path) -> bool:
    """Copy the bundled files these fonts need into `folder`/fonts. True if libass should look there."""
    files = {f.file for n in names if (f := bundled(n))}
    if not files:
        return False
    dest = folder / "fonts"
    dest.mkdir(exist_ok=True)
    for file in files:
        shutil.copyfile(FONTS_DIR / file, dest / file)
    return True

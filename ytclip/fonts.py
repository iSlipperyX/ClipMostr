"""Fonts that ship with ClipMonster, so captions and banners look the same on every PC, plus the font
metrics both renderers share.

libass is pointed at just the files a render uses (copied next to the .ass file), and the GUI registers
them with Qt so the font picker, layout sketch and export view draw them too. Any installed font still works
by name. Text is measured from the font file itself (advance widths and the Windows ascent/descent libass
sizes fonts by), so line breaks, text fitting and the editor's export view match what libass burns in."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

FONTS_DIR = Path(__file__).with_name("fonts")


@dataclass(frozen=True)
class Font:
    name: str  # what the user picks and what the .ass file asks libass for
    file: str
    qt_family: str  # how Qt names the same face
    qt_weight: int = 400


BUNDLED: dict[str, Font] = {f.name: f for f in (
    Font("TikTok Sans Black", "TikTokSansBlack.ttf", "TikTok Sans Black", 400),
    Font("Montserrat Black", "Montserrat-Black.ttf", "Montserrat", 900),
    Font("Montserrat ExtraBold", "Montserrat-ExtraBold.ttf", "Montserrat", 800),
    Font("Poppins Black", "Poppins-Black.ttf", "Poppins", 900),
    Font("Poppins ExtraBold", "Poppins-ExtraBold.ttf", "Poppins", 800),
    Font("Archivo Black", "ArchivoBlack-Regular.ttf", "Archivo Black", 400),
    Font("Anton", "Anton-Regular.ttf", "Anton", 400),
    Font("Bebas Neue", "BebasNeue-Regular.ttf", "Bebas Neue", 400),
    Font("Bangers", "Bangers-Regular.ttf", "Bangers", 400),
    Font("Luckiest Guy", "LuckiestGuy-Regular.ttf", "Luckiest Guy", 400),
    Font("Lilita One", "LilitaOne-Regular.ttf", "Lilita One", 400),
    Font("Titan One", "TitanOne-Regular.ttf", "Titan One", 400),
    Font("Permanent Marker", "PermanentMarker-Regular.ttf", "Permanent Marker", 400),
)}


def bundled(name: str) -> Font | None:
    return BUNDLED.get(name.strip())


def ass_bold(name: str) -> int:
    """ASS 'Bold' flag: bundled faces are already as heavy as intended, so never let libass embolden them."""
    return 0 if bundled(name) else -1


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


# ---------------------------------------------------------------- metrics


@dataclass
class Metrics:
    """What libass needs to place text: it sizes a font so that winAscent + winDescent equals the ASS font
    size, so one line is exactly `size` pixels tall and the em is size * em_ratio."""

    upm: int = 1000
    win_ascent: int = 900
    win_descent: int = 250
    advances: dict[int, int] = field(default_factory=dict)  # code point -> advance in font units
    default_advance: int = 600
    found: bool = False  # False = the font file couldn't be read, so these are typical values

    @property
    def em_ratio(self) -> float:
        return self.upm / max(1, self.win_ascent + self.win_descent)

    @property
    def ascent_ratio(self) -> float:
        return self.win_ascent / max(1, self.win_ascent + self.win_descent)

    def width(self, text: str, size: float) -> float:
        """Advance width of `text` in pixels at ASS font `size`."""
        units = sum(self.advances.get(ord(c), self.default_advance) for c in text)
        return units * size * self.em_ratio / self.upm


def _parse(path: Path, index: int = 0) -> Metrics:
    data = path.read_bytes()
    base = 0
    if data[:4] == b"ttcf":  # a collection: use the first (or requested) font in it
        base = struct.unpack_from(">I", data, 12 + 4 * index)[0]
    num = struct.unpack_from(">H", data, base + 4)[0]
    tables = {}
    for i in range(num):
        tag, _, offset, length = struct.unpack_from(">4sIII", data, base + 12 + 16 * i)
        tables[tag.decode("latin-1")] = (offset, length)
    upm = struct.unpack_from(">H", data, tables["head"][0] + 18)[0]
    os2 = tables["OS/2"][0]
    win_ascent, win_descent = struct.unpack_from(">HH", data, os2 + 74)
    hhea = tables["hhea"][0]
    num_hmetrics = struct.unpack_from(">H", data, hhea + 34)[0]
    hmtx = tables["hmtx"][0]
    widths = [struct.unpack_from(">H", data, hmtx + 4 * i)[0] for i in range(num_hmetrics)]

    def advance(glyph: int) -> int:
        return widths[min(glyph, num_hmetrics - 1)]

    advances: dict[int, int] = {}
    cmap = tables["cmap"][0]
    n_sub = struct.unpack_from(">H", data, cmap + 2)[0]
    subtables = [struct.unpack_from(">HHI", data, cmap + 4 + 8 * i) for i in range(n_sub)]
    # Prefer the Windows Unicode BMP table (3,1), else any Unicode one; format 4 covers what captions use.
    subtables.sort(key=lambda t: (t[0] != 3 or t[1] != 1, t[0] not in (0, 3)))
    for _, _, off in subtables:
        sub = cmap + off
        if struct.unpack_from(">H", data, sub)[0] != 4:
            continue
        seg2 = struct.unpack_from(">H", data, sub + 6)[0]
        ends = sub + 14
        starts = ends + seg2 + 2
        deltas = starts + seg2
        ranges = deltas + seg2
        for s in range(seg2 // 2):
            end, start = struct.unpack_from(">H", data, ends + 2 * s)[0], struct.unpack_from(">H", data, starts + 2 * s)[0]
            delta = struct.unpack_from(">h", data, deltas + 2 * s)[0]
            roff = struct.unpack_from(">H", data, ranges + 2 * s)[0]
            for cp in range(start, min(end, 0xFFFE) + 1):
                if roff == 0:
                    glyph = (cp + delta) & 0xFFFF
                else:
                    at = ranges + 2 * s + roff + 2 * (cp - start)
                    glyph = struct.unpack_from(">H", data, at)[0]
                    glyph = (glyph + delta) & 0xFFFF if glyph else 0
                if glyph:
                    advances[cp] = advance(glyph)
        break
    default = advances.get(ord("n"), advance(0))
    return Metrics(upm, win_ascent, win_descent, advances, default, True)


def _system_font_file(name: str) -> Path | None:
    """The file an installed font lives in, looked up the way Windows (or fontconfig) knows it."""
    want = name.strip().lower()
    if os.name == "nt":
        try:
            import winreg
        except ImportError:  # pragma: no cover
            return None
        fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates: dict[str, Path] = {}
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        label, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    i += 1
                    label = label.lower().replace(" (truetype)", "").replace(" (opentype)", "").strip()
                    path = Path(value) if os.path.isabs(value) else fonts_dir / value
                    for part in label.split(" & "):
                        candidates.setdefault(part.strip(), path)
        # libass asks for the bold face of an installed font (ASS Bold = -1), so measure that one.
        for key in (f"{want} bold", want):
            if key in candidates and candidates[key].is_file():
                return candidates[key]
        return None
    try:
        out = subprocess.run(["fc-match", "-f", "%{family}\n%{file}", f"{name}:bold"], capture_output=True,
                             text=True, timeout=5).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if len(out) == 2 and want in out[0].lower().split(","):
        return Path(out[1])
    return None


@lru_cache(maxsize=64)
def metrics(name: str) -> Metrics:
    f = bundled(name)
    path = FONTS_DIR / f.file if f else _system_font_file(name)
    if path is not None:
        try:
            return _parse(path)
        except (OSError, KeyError, struct.error, IndexError):
            pass
    return Metrics()


def ass_size(name: str, em_px: float) -> int:
    """The ASS font size that gives `em_px` pixel letters in this font. Settings sizes mean the visible
    letter size, so the same number looks the same in every font."""
    return max(1, round(em_px / metrics(name).em_ratio))

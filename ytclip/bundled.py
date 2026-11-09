"""Where a packaged ClipMonster keeps the programs it brings with it (whisper.cpp, llama.cpp).

On Windows they sit beside ClipMonster.exe. In the Mac app (ClipMonster.app) PyInstaller puts the program in
Contents/MacOS and everything it brings in Contents/Frameworks (sys._MEIPASS), with the data in
Contents/Resources, so each of those is looked in.
"""

from __future__ import annotations

import sys
from pathlib import Path


def folders() -> list[Path]:
    """The packaged app's own folders, most likely first; none when running from source."""
    if not getattr(sys, "frozen", False):
        return []
    exe = Path(sys.executable).resolve().parent
    out = [exe]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        out.append(Path(meipass))
    if exe.name == "MacOS" and exe.parent.name == "Contents":  # inside a Mac .app
        out += [exe.parent / "Frameworks", exe.parent / "Resources"]
    seen: list[Path] = []
    for p in out:
        if p not in seen:
            seen.append(p)
    return seen


def find(*parts: str) -> Path | None:
    """A file the packaged app brought (`find("whisper-cpp", "whisper-cli")`), or None."""
    for base in folders():
        p = base.joinpath(*parts)
        if p.is_file():
            return p
    return None

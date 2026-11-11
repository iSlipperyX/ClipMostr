"""File paths between the PC (Windows or Linux) and the Mac: what may travel, and how it lands.

Only paths *relative to a folder both sides know* ever travel (a clip's place under the output folder, say
"Gaming/2026-11-10 Big play/01 - Big play.mp4"), always written with "/" (to_wire). A PC's own absolute paths
("C:\\Users\\…") never leave it: the Mac has its own output folder, and its own folders are its own business.

Whatever comes in is checked before it touches the disk (from_wire): it must stay inside the folder it's for
(no "..", no absolute path, drive letter or network share), "\\" counts as a separator too (an older PC, or
one on Windows, may send it), and each part is made safe for the computer it lands on: on Windows no
<>:"|?* or control characters, no trailing dots or spaces and no reserved names (CON, NUL, COM1 ...); on a
Mac no ":" (Finder shows it as "/") and nothing starting with "._" (the Mac's own resource-fork files). A
path that can't be made safe raises UnsafePath, and nothing is written.

translate() maps an absolute path under one computer's folder to the same place under another's, for the
few places a whole path has to be shown on the other side.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_PART = 120  # characters of one file or folder name
MAX_PARTS = 12  # folders deep
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}
_WIN_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')
_MAC_BAD = re.compile(r"[:\x00-\x1f]")


class UnsafePath(ValueError):
    """A path from the other computer that would land outside its folder, or can't be made safe."""


def platform_name() -> str:
    return "windows" if os.name == "nt" else "mac" if sys.platform == "darwin" else "linux"


def safe_part(name: str, platform: str = "") -> str:
    """One file or folder name made safe on `platform` ("windows", "mac", "linux"; "" = this computer).
    The same name comes out the same every time, so a file fetched twice lands in the same place."""
    platform = platform or platform_name()
    text = unicodedata.normalize("NFC", str(name or ""))
    text = (_WIN_BAD if platform == "windows" else _MAC_BAD if platform == "mac" else re.compile(r"[\x00]")).sub(
        "-", text)
    text = text.replace("/", "-").replace("\\", "-").strip()
    if platform == "windows":
        text = text.rstrip(" .")
        stem = text.split(".", 1)[0].upper().strip()
        if stem in WINDOWS_RESERVED:
            text = "_" + text
    if platform == "mac" and text.startswith("._"):
        text = "_" + text[2:]
    if len(text) > MAX_PART:
        stem, dot, ext = text.rpartition(".")
        if dot and 0 < len(ext) <= 8:
            text = stem[:MAX_PART - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            text = text[:MAX_PART].rstrip(" .")
    if text in ("", ".", ".."):
        raise UnsafePath(f"“{name}” can't be a file name")
    return text


def parts_of(rel: str) -> list[str]:
    """The names in a relative path from the other computer, "/" or "\\" between them. UnsafePath for anything
    that isn't plainly relative: absolute ("/x", "\\x"), a drive ("C:"), a share ("\\\\server"), or "..". """
    text = str(rel or "").strip()
    if not text:
        raise UnsafePath("no path")
    if "\x00" in text:
        raise UnsafePath("a path with a NUL in it")
    norm = text.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or PureWindowsPath(text).anchor:
        raise UnsafePath(f"“{rel}” isn't relative")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts:
        raise UnsafePath("no path")
    if any(p == ".." for p in parts):
        raise UnsafePath(f"“{rel}” climbs out of its folder")
    if len(parts) > MAX_PARTS:
        raise UnsafePath(f"“{rel}” is too deep")
    return parts


def from_wire(rel: str, root: Path, platform: str = "") -> Path:
    """Where a relative path from the other computer lands under `root` here, every part made safe; always
    inside `root` (checked after following any links already there)."""
    parts = [safe_part(p, platform) for p in parts_of(rel)]
    root = Path(root)
    dest = root.joinpath(*parts)
    try:
        inside = dest.resolve().is_relative_to(root.resolve())
    except OSError as exc:
        raise UnsafePath(f"“{rel}” can't be checked ({exc})") from exc
    if not inside:
        raise UnsafePath(f"“{rel}” would land outside {root}")
    return dest


def to_wire(path: Path, root: Path) -> str:
    """`path` (under `root` on this computer) as it travels: relative, with "/". UnsafePath when it isn't under
    `root`, so an absolute path of this computer can't be sent by mistake."""
    try:
        rel = Path(path).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError) as exc:
        raise UnsafePath(f"{path} isn't inside {root}") from exc
    text = rel.as_posix()
    if text in ("", "."):
        raise UnsafePath(f"{path} is {root} itself")
    return text


def _pure(path: str):
    """The path as the computer that wrote it reads it: Windows when it has a drive or backslashes."""
    return PureWindowsPath(path) if (re.match(r"^[A-Za-z]:", path) or "\\" in path) else PurePosixPath(path)


def translate(path: str, from_root: str, to_root: Path, platform: str = "") -> Path | None:
    """An absolute path on the other computer ("C:\\Users\\me\\Videos\\ClipMonster\\Gaming\\a.mp4"), under its
    folder `from_root` ("C:\\Users\\me\\Videos\\ClipMonster"), as the same place under `to_root` here. None
    when it isn't under `from_root` (Windows paths compare without regard to case, as Windows does)."""
    src, base = _pure(str(path or "")), _pure(str(from_root or ""))
    if not str(base) or not str(src) or type(src) is not type(base):
        return None
    windows = isinstance(src, PureWindowsPath)
    a = [p.lower() for p in src.parts] if windows else list(src.parts)
    b = [p.lower() for p in base.parts] if windows else list(base.parts)
    if len(a) <= len(b) or a[:len(b)] != b:
        return None
    rest = "/".join(src.parts[len(base.parts):])
    try:
        return from_wire(rest, Path(to_root), platform)
    except UnsafePath:
        return None

"""Model files the Video Creator downloads once (voices, image models), into %LOCALAPPDATA%\\ClipMonster\\models.

Each file arrives whole or not at all (a .part file renamed at the end), so a cut-off download is never
mistaken for a model. Settings → Creator → Download models when needed can turn this off; then a missing
model is reported with the file to put in place instead.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Callable

from ..settings import models_dir

Progress = Callable[[str, "float | None"], None]


class MissingModel(RuntimeError):
    """A model file isn't here and may not be downloaded."""


def folder(*parts: str) -> Path:
    path = models_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch(url: str, dest: Path, label: str, progress: Progress | None = None, *, allowed: bool = True,
          min_bytes: int = 1000, opener=urllib.request.urlopen) -> Path:
    """`dest`, downloaded from `url` first when it isn't here yet."""
    if dest.is_file() and dest.stat().st_size >= min_bytes:
        return dest
    if not allowed:
        raise MissingModel(f"{dest.name} isn't downloaded yet and downloads are off (Settings → Creator). Put it "
                           f"in {dest.parent} or turn downloads on.")
    progress = progress or (lambda m, f: None)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    progress(label, None)
    try:
        with opener(url, timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                progress(label, done / total if total else None)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise OSError(f"Couldn't download {dest.name}: {exc}") from exc
    if total and part.stat().st_size != total:
        part.unlink(missing_ok=True)
        raise OSError(f"The download of {dest.name} was cut short. It'll try again next time.")
    part.replace(dest)
    return dest

"""Scene-cut detection: lots of hard cuts in a short span usually means action."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .ffmpeg import ffmpeg_exe, run_with_progress

_PTS = re.compile(r"pts_time:([\d.]+)")


def detect_scenes(path: Path, duration: float, progress: Callable[[float], None] | None = None,
                  threshold: float = 0.35) -> list[float]:
    cuts: list[float] = []

    def on_stderr(line: str) -> None:
        if "Parsed_showinfo" in line and (m := _PTS.search(line)):
            cuts.append(float(m[1]))

    # Low frame rate + tiny frames keeps this fast; scene changes survive both.
    cmd = [ffmpeg_exe(), "-hide_banner", "-i", str(path), "-an", "-sn", "-dn",
           "-vf", f"fps=5,scale=160:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]
    run_with_progress(cmd, duration, progress, on_stderr=on_stderr)
    return cuts

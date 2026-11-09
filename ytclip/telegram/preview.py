"""The video as the bot sends it: the file itself when Telegram takes it, else a smaller copy.

A bot can send files up to 50 MB. A clip under LIMIT goes as it is (what you watch is what posts). A
bigger one, like a long 1080p Creator video, is sent as a copy made to fit: 720p, or smaller for a long
video, at the bitrate that lands it under the limit. The copy is made once and kept in
%LOCALAPPDATA%\\ClipMonster\\telegram for a few days (prune()), and ffmpeg makes it at low priority so
clipping doesn't slow down.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..ffmpeg import NO_WINDOW, ffmpeg_exe, probe
from ..settings import local_dir

LIMIT = 48_000_000  # under the Bot API's 50 MB, with room for the form around the file
KEEP_DAYS = 3
AUDIO_BPS = 96_000
MAX_VIDEO_BPS = 4_000_000
MIN_VIDEO_BPS = 250_000
TIMEOUT = 20 * 60  # an encode taking longer than this has gone wrong
BELOW_NORMAL = 0x00004000  # Windows' BELOW_NORMAL_PRIORITY_CLASS


@dataclass
class Sendable:
    path: Path
    width: int = 0
    height: int = 0
    duration: float = 0.0
    copy: bool = False  # a smaller copy, not the file itself


class TooBig(RuntimeError):
    pass


def folder() -> Path:
    return local_dir("telegram")


def plan(duration: float, size_limit: int = LIMIT) -> tuple[int, int]:
    """(video bitrate, the long side in pixels) for a copy of a `duration`-second video under the limit."""
    budget = size_limit * 8 * 0.9 / max(1.0, duration) - AUDIO_BPS  # 10% for the MP4's own bytes
    bps = int(max(MIN_VIDEO_BPS, min(MAX_VIDEO_BPS, budget)))
    side = 1280 if bps >= 1_200_000 else 960 if bps >= 700_000 else 640
    return bps, side


def command(video: Path, out: Path, bps: int, side: int) -> list[str]:
    # The long side becomes `side` (never larger than it was), the other follows, kept even.
    scale = (f"scale=w='if(gte(iw,ih),min(iw,{side}),-2)':h='if(gte(iw,ih),-2,min(ih,{side}))'")
    return [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(video), "-map", "0:v:0",
            "-map", "0:a:0?", "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(bps),
            "-maxrate", str(int(bps * 1.4)), "-bufsize", str(bps * 2), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", str(AUDIO_BPS), "-ac", "2", "-movflags", "+faststart", str(out)]


def for_telegram(video: Path, key: str, where: Path | None = None, run=subprocess.run) -> Sendable:
    """What to send for `video` (`key` names its copy, e.g. the library entry's id)."""
    video = Path(video)
    size = video.stat().st_size
    info = probe(video)
    if size <= LIMIT:
        return Sendable(video, info.width, info.height, info.duration)
    out = (where or folder()) / f"{key}.mp4"
    try:
        if out.stat().st_mtime >= video.stat().st_mtime and 0 < out.stat().st_size <= LIMIT:
            got = probe(out)
            return Sendable(out, got.width, got.height, got.duration, copy=True)
    except OSError:
        pass
    out.parent.mkdir(parents=True, exist_ok=True)
    limit = LIMIT
    for _attempt in range(2):
        bps, side = plan(info.duration, limit)
        part = out.with_name(out.stem + ".part.mp4")
        flags = NO_WINDOW | (BELOW_NORMAL if os.name == "nt" else 0)
        done = run(command(video, part, bps, side), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   timeout=TIMEOUT, creationflags=flags)
        if done.returncode != 0 or not part.is_file():
            part.unlink(missing_ok=True)
            tail = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
            raise RuntimeError("Couldn't make a copy small enough for Telegram" + (f": {tail[0]}" if tail else ""))
        if part.stat().st_size <= LIMIT:
            os.replace(part, out)
            got = probe(out)
            return Sendable(out, got.width, got.height, got.duration, copy=True)
        part.unlink(missing_ok=True)
        limit = int(limit * 0.75)  # a busy video came out bigger than planned: once more, smaller
    raise TooBig("Even a small copy of this video is over Telegram's 50 MB. Watch it on the PC.")


def drop(key: str, where: Path | None = None) -> None:
    for f in (where or folder()).glob(f"{key}.*"):
        try:
            f.unlink()
        except OSError:
            pass


def prune(keep: set[str], where: Path | None = None, now: float | None = None) -> int:
    """Remove copies of videos no longer in the library (not in `keep`), and any older than KEEP_DAYS."""
    now = time.time() if now is None else now
    gone = 0
    d = where or folder()
    for f in d.glob("*.mp4") if d.is_dir() else []:
        key = f.name.split(".", 1)[0]
        try:
            age = now - f.stat().st_mtime
        except OSError:
            continue
        if ".part" in f.name:
            stale = age > 3600  # a copy being made right now is left alone
        else:
            stale = key not in keep or age > KEEP_DAYS * 86400
        if stale:
            try:
                f.unlink()
                gone += 1
            except OSError:
                pass
    return gone

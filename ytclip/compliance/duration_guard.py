"""Creator Rewards length: TikTok's Creator Rewards Program only pays for videos longer than one minute.

With Settings → Editing → Creator Rewards length on, clips are found at 62 to 88 seconds (Engine.lengths),
built from whole sentences. Cutting pauses and fillers makes a clip shorter than the stretch of video it
came from, so before each clip is exported its finished length is worked out, and a clip that would come
out under 61 seconds runs on to the end of the next sentence (or the one after), staying clear of the
next clip and the end of the video. A clip that still can't get there is kept, and the log says so.
"""

from __future__ import annotations

from typing import Callable

MIN_SECONDS = 61.0  # strictly over a minute, with a second's margin for players that round
TARGET = (62.0, 88.0)
MAX_SECONDS = 90.0


def lengths(lo: float, hi: float) -> tuple[float, float]:
    """Clip lengths in Creator Rewards mode."""
    return max(lo, TARGET[0]), max(TARGET[0], min(max(hi, TARGET[1]), MAX_SECONDS))


def stretch(clip, sents, finished: Callable[[float, float], float], limit: float,
            want: float = MIN_SECONDS + 0.5) -> bool:
    """Move `clip.end` on, a sentence at a time, until `finished(start, end)` (the clip's length after
    its cuts) reaches `want`, never past `limit` (the next clip or the video's end) or MAX_SECONDS of
    source. True when it got there."""
    if finished(clip.start, clip.end) >= MIN_SECONDS:
        return True
    ends = sorted(c.end for c in sents if c.end > clip.end + 0.05)
    for e in ends:
        if e > limit or e - clip.start > MAX_SECONDS * 1.4:
            break
        if finished(clip.start, e) >= want:
            clip.end = round(min(limit, e + 0.25), 3)
            return True
    return False

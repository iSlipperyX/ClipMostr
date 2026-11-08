"""Cutaways to B-roll from a folder of your own clips, matched to what's being said.

Only clips you have the right to use belong in the folder: your own footage, licensed stock, gameplay
you recorded, public-domain or Creative Commons clips that allow it. ClipMonster never fetches B-roll
from anywhere else.

Every clip's tags come from its file and folder names: "B-roll/money/counting cash.mp4" is tagged
money, counting, cash. About every 15-20 seconds of the edited clip (Settings → Editing), at the start
of a sentence, the picture cuts away for a couple of seconds to the clip whose tags best match the words
around that moment. With no match, the least used clip is taken, so the same one doesn't keep coming
back. The sound carries on underneath: it's the speaker's words over new pictures.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "with", "clip", "video", "broll",
        "b", "roll", "stock", "footage", "final", "copy", "hd", "4k", "1080p", "720p", "mp4"}
EDGE = 3.0  # no cutaway in the first or last few seconds (the hook and the payoff are the speaker's)


@dataclass
class Insert:
    start: float  # output seconds
    end: float
    path: Path
    offset: float  # where in the B-roll clip to start
    tags: tuple[str, ...] = ()
    matched: str = ""  # the word that chose it, if one did


def _stem(word: str) -> str:
    w = re.sub(r"[^a-z0-9]", "", word.lower())
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(w) > len(suffix) + 2 and w.endswith(suffix):
            return w[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return w


def tags_for(path: Path, root: Path) -> tuple[str, ...]:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(path.name)
    parts = [*rel.parent.parts, rel.stem]
    words = re.split(r"[\s_\-.,()\[\]]+", " ".join(parts))
    return tuple(dict.fromkeys(_stem(w) for w in words if w and w.lower() not in STOP and not w.isdigit()))


def library(folder: str | Path) -> list[tuple[Path, tuple[str, ...]]]:
    root = Path(folder).expanduser()
    if not str(folder).strip() or not root.is_dir():
        return []
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
    return [(p, tags_for(p, root)) for p in files]


def plan(words, duration: float, folder: str | Path, every: float = 18.0, length: float = 2.5,
         used: dict[str, int] | None = None, durations: dict[Path, float] | None = None,
         seed: str = "") -> list[Insert]:
    """Where the cutaways go in an edited clip (`words` in output time) and which clip each shows.

    `used` counts how often each clip was shown (shared across a video's clips, so they vary).
    `durations` gives clip lengths when known, so the offset stays inside the clip."""
    lib = library(folder)
    if not lib or duration < 2 * EDGE + length or every <= 0:
        return []
    used = used if used is not None else {}
    starts = sentence_starts(words)
    inserts: list[Insert] = []
    want = every
    while want < duration - EDGE - length:
        # the sentence start nearest the wanted time, within a few seconds of it
        near = [s for s in starts if abs(s - want) <= 4.0 and EDGE <= s <= duration - EDGE - length
                and all(abs(s - i.start) >= every * 0.6 for i in inserts)]
        at = min(near, key=lambda s: abs(s - want)) if near else want
        said = {_stem(w.text) for w in words if at - 2.0 <= w.start <= at + length + 2.0}
        said.discard("")
        best, best_hits, matched = None, 0, ""
        for path, tags in lib:
            hits = [t for t in tags if t in said]
            key = (len(hits), -used.get(str(path), 0))
            if best is None or key > (best_hits, -used.get(str(best[0]), 0)):
                best, best_hits, matched = (path, tags), len(hits), hits[0] if hits else ""
        if best is None:
            break
        if not best_hits:  # no word matched: the least used clip, steadily varied
            best = min(lib, key=lambda item: (used.get(str(item[0]), 0),
                                              hashlib.sha1(f"{seed}|{item[0]}|{at:.0f}".encode()).hexdigest()))
            matched = ""
        path, tags = best
        used[str(path)] = used.get(str(path), 0) + 1
        clip_len = (durations or {}).get(path, 0.0)
        span = max(0.0, clip_len - length - 0.5)
        h = int(hashlib.sha1(f"{seed}|{path}|{at:.2f}".encode()).hexdigest()[:8], 16)
        offset = (h % 1000) / 1000 * span if span > 0 else 0.0
        inserts.append(Insert(round(at, 3), round(min(duration - EDGE, at + length), 3), path, round(offset, 2),
                              tags, matched))
        want = at + every
    return inserts


def sentence_starts(words) -> list[float]:
    from .camera import sentence_starts as starts

    return starts(words)

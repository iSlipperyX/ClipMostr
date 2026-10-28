"""Clean cut points: clips start and end in a pause, never part-way through a word.

Clip edges go on sentence boundaries (analyze.fit_to_sentences), which is only as good as the
transcript's timing. YouTube's auto-captions have no punctuation and time each word by its start,
so for a fast talker the "sentences" run together and an edge can land mid-word. This module
listens instead: the fine loudness (analyze.FINE, 20 ms frames) shows the real pauses.

* refine_words() gives auto-caption words the pauses the audio shows between them, so
  subtitles.sentences() can break sentences there (a breath is a natural place to cut).
* clean_edges() is a last check on every clip: an edge that sits in speech, or in a gap between
  words with a sentence's pause close by, moves into the pause (up to a second or so, keeping the
  clip inside the clip lengths). Edges already in a pause, and clips you edited, stay.

Pauses are judged against the sound around them, so background music or room noise doesn't
count as speech; where there's no clear pause nearby (a crowd shouting over the talk), nothing moves.
"""

from __future__ import annotations

import bisect

import numpy as np

from .analyze import FINE, Clip
from .layout import CARRIES_ON

FLOOR = -80.0  # dBFS: quieter than this counts as silence (codec noise can sit far below it)
CONTRAST = 9.0  # dB between speech and pause, at least, for a pause to count
QUIET = 0.25  # share of the way from the quietest to the loud sound around: under it is a pause
BREATH = 0.1  # seconds: a pause this long ends a sentence in captions without punctuation
GAP = 0.06  # seconds: the shortest pause an edge may sit in (between two words)
REACH = 0.5  # seconds an edge may move
LONG, REACH_LONG = 0.18, 1.2  # a pause this long is most likely between sentences: an edge may move further to it
CONTEXT = 3.0  # seconds of sound either side that judge what's loud
BLOCK = 0.5  # seconds judged together
BLUR = 0.06  # seconds: an edge this close to a pause is in it
LEAD, TAIL = 0.12, 0.2  # seconds of a pause kept before a clip's first word and after its last


class Pauses:
    """Where the sound dips into a pause, from the fine loudness. What counts as quiet is judged
    against the few seconds around (CONTEXT), worked out once, so every question gets the same answer."""

    def __init__(self, fine: np.ndarray):
        f = np.maximum(np.asarray(fine, dtype=np.float32), FLOOR)
        level = np.convolve(f, np.ones(3) / 3, mode="same") if len(f) >= 3 else f  # 60 ms average
        self.level = level
        n, block, half = len(level), int(round(BLOCK / FINE)), int(round(CONTEXT / FINE))
        quiet = np.zeros(n, bool)
        for k in range(0, n, block):
            around = level[max(0, k - half):k + block + half]
            quietest, loud = float(np.percentile(around, 3)), float(np.percentile(around, 85))
            if loud - quietest >= CONTRAST:  # else there are no clear pauses here
                quiet[k:k + block] = level[k:k + block] <= quietest + QUIET * (loud - quietest)
        edges = np.flatnonzero(np.diff(np.concatenate(([0], quiet.astype(np.int8), [0]))))
        self._runs = [(round(float(s) * FINE, 3), round(float(e) * FINE, 3)) for s, e in zip(edges[::2], edges[1::2])]
        self._starts = [s for s, _ in self._runs]

    def __bool__(self) -> bool:
        return bool(len(self.level))

    def runs(self, a: float, b: float, shortest: float = GAP) -> list[tuple[float, float]]:
        """Pauses (start, end) of at least `shortest` seconds that overlap [a, b]."""
        i = max(0, bisect.bisect_left(self._starts, a) - 1)
        out = []
        for s, e in self._runs[i:]:
            if s >= b:
                break
            if e > a and e - s >= shortest - 1e-9:
                out.append((s, e))
        return out

    def quiet_at(self, t: float) -> bool:
        return bool(self.runs(t - 1e-6, t + 1e-6, 0.0))


def punctuated(words: list) -> bool:
    """Whether a transcript ends its sentences with . ! ? (Whisper, hand-made captions) or not (auto-captions)."""
    if not words:
        return True
    ends = sum(1 for w in words if w.text.rstrip("\"'”’)]»").endswith((".", "!", "?", "…")))
    return ends >= max(1, len(words) // 60)


def refine_words(words: list, pauses: Pauses) -> int:
    """Give captions' words the pauses the audio shows between them (in place): a word ends where
    the pause starts and the next begins where it ends. Returns how many pauses were found."""
    if not pauses:
        return 0
    found = 0
    for w, nxt in zip(words, words[1:]):
        lo = max(w.start + 0.05, nxt.start - 0.4)  # a caption stamp can come a little early or late
        hi = nxt.start + 0.15
        runs = [r for r in pauses.runs(lo, hi, BREATH) if r[0] >= lo - 0.02 and r[1] <= hi + 0.3]
        if not runs:
            continue
        s, e = min(runs, key=lambda r: abs(r[1] - nxt.start))
        if s <= w.start or e >= nxt.end + 0.3:
            continue
        w.end, nxt.start = s, e
        if nxt.end < nxt.start:
            nxt.end = nxt.start + 0.05
        found += 1
    return found


def _move(t: float, pauses: Pauses, start: bool, lo: float, hi: float) -> float:
    """t, or a better place for it in a pause between `lo` and `hi`: a long pause (most likely
    between sentences) within REACH_LONG, else the nearest pause within REACH. Kept close to the
    speech it borders."""

    def spot(r: tuple[float, float]) -> float:
        s, e = r
        return max(s, e - LEAD) if start else min(e, s + TAIL)

    def nearest(shortest: float, reach: float) -> float | None:
        runs = [r for r in pauses.runs(t - reach, t + reach, shortest) if lo <= spot(r) <= hi
                and abs(spot(r) - t) <= reach]
        # earlier is safer for a start (it keeps the first word), later for an end (it keeps the last)
        best = min(runs, key=lambda r: (abs(spot(r) - t), spot(r) if start else -spot(r)), default=None)
        return round(float(spot(best)), 2) if best else None

    def inside(shortest: float) -> bool:  # the 60 ms average blurs a pause's ends by a frame or two
        return any(s - BLUR <= t <= e + BLUR for s, e in pauses.runs(t - BLUR, t + BLUR, shortest))

    if inside(BREATH):
        return t
    # between two words already, it moves only for a pause close by; in a word, it reaches further
    tries = ((LONG, REACH_LONG), (BREATH, REACH)) if inside(GAP) else ((LONG, REACH_LONG), (BREATH, REACH_LONG),
                                                                        (GAP, REACH))
    for shortest, reach in tries:
        got = nearest(shortest, reach)
        if got is not None:
            return got
    return t


def clean_edges(clips: list[Clip], pauses: Pauses, duration: float, min_len: float = 0.0,
                max_len: float = 0.0) -> int:
    """Move each clip edge that sits in speech (or between words, with a sentence's pause close
    by) into a pause, keeping the clip between min_len and max_len, off its neighbours, and a part
    still right after the part before (in place). Clips you edited stay. Returns how many changed."""
    if not pauses:
        return 0
    on = sorted((c for c in clips if c.enabled), key=lambda c: c.start)
    chained = {(id(a), id(b)) for a in on for b in on
               if b.start > a.start and b.end > a.end and abs(b.start - a.end) <= CARRIES_ON}
    was = {id(c): (c.start, c.end) for c in on}
    for i, c in enumerate(on):
        if c.edited:
            continue
        shortest = min(min_len, c.length) * 0.9
        longest = max(max_len, c.length) + 0.5 if max_len else float("inf")
        before = max((o.end for o in on[:i] if o.end <= c.start + 0.01), default=0.0)
        after = min((o.start for o in on[i + 1:] if o.start >= c.end - 0.01), default=duration or float("inf"))
        c.start = _move(c.start, pauses, True, max(before, c.end - longest), c.end - shortest)
        c.end = _move(c.end, pauses, False, c.start + shortest, min(after, c.start + longest))
    for a in on:  # a part must still carry on right after the part before it
        for b in on:
            if (id(a), id(b)) in chained and abs(b.start - a.end) > CARRIES_ON:
                a.end, b.start = was[id(a)][1], was[id(b)][0]
    return sum(1 for c in on if (c.start, c.end) != was[id(c)])

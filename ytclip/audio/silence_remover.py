"""Tighter pacing: long pauses shrink to a breath and filler words go, using Whisper's word timings.

The result is a list of the stretches of the clip to keep (source seconds). The export plays them back to
back: the picture jumps (a jump cut, which short-form viewers expect) and the sound gets a 5 ms fade at
each join, so no join clicks.

* A pause between words longer than `longest` (0.40 s) shrinks to `keep` (0.12 s), split around the
  middle of the gap, so the words either side keep their natural attack and release.
* Fillers: "um", "uh", "erm" and the like always go. Softer ones ("like", "you know", "literally") only go
  when they stand apart from the sentence: set off by commas or by a pause, never "I like it".
* A cut never leaves a piece shorter than MIN_PIECE: those are merged back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..subtitles import Word

HARD_FILLERS = {"um", "umm", "uh", "uhh", "uhm", "er", "erm", "err", "hmm", "mm", "mhm", "ah"}
SOFT_FILLERS = ("you know", "i mean", "like", "literally", "basically", "actually", "sort of", "kind of")
DEFAULT_FILLERS = "um, uh, erm, hmm, you know, i mean, like, literally, basically"
GAP_FOUND = 0.35  # gaps longer than this are measured as pauses (Whisper's own word gaps run up to here)
MIN_PIECE = 0.20  # the shortest stretch worth keeping between two cuts
ISOLATED = 0.15  # a soft filler set apart by a pause at least this long counts as a filler


@dataclass
class Cut:
    start: float
    end: float
    why: str  # "pause" | "filler"


def _bare(text: str) -> str:
    return re.sub(r"[^\w' ]", "", text.lower()).strip()


def filler_list(text: str) -> list[str]:
    return [w for w in (_bare(x) for x in re.split(r"[,\n]", text or "")) if w]


def find_fillers(words: list[Word], fillers: list[str] | None = None) -> list[tuple[int, int]]:
    """(first, last) word indexes of each filler. Soft fillers count only when set apart."""
    fillers = filler_list(DEFAULT_FILLERS) if fillers is None else fillers
    hard = [f for f in fillers if f in HARD_FILLERS]
    soft = sorted((f.split() for f in fillers if f not in HARD_FILLERS), key=len, reverse=True)
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(words):
        if _bare(words[i].text) in hard:
            out.append((i, i))
            i += 1
            continue
        hit = None
        for phrase in soft:
            n = len(phrase)
            if i + n <= len(words) and [_bare(w.text) for w in words[i:i + n]] == phrase:
                hit = n
                break
        if hit:
            j = i + hit - 1
            before = words[i - 1] if i else None
            after = words[j + 1] if j + 1 < len(words) else None
            comma_before = before is not None and before.text.rstrip().endswith((",", "…", "-", "—"))
            comma_after = words[j].text.rstrip().endswith((",", "…", "-", "—"))
            gap_before = before is None or words[i].start - before.end >= ISOLATED
            gap_after = after is None or after.start - words[j].end >= ISOLATED
            # "like" in "I like it" runs straight on; "it was, like, huge" is set apart on both sides.
            if (comma_before or gap_before) and (comma_after or gap_after) and not (before is None and after is None):
                out.append((i, j))
                i = j + 1
                continue
        i += 1
    return out


def cuts_for(words: list[Word], start: float, end: float, *, pauses: bool = True, fillers: bool = True,
             longest: float = 0.40, keep: float = 0.12, filler_words: str = DEFAULT_FILLERS) -> list[Cut]:
    """What to take out of [start, end): long pauses and filler words."""
    inside = [w for w in words if w.end > start and w.start < end]
    out: list[Cut] = []
    if fillers and inside:
        for a, b in find_fillers(inside, filler_list(filler_words)):
            # Take the filler and the air around it, up to the words either side, leaving one breath.
            lo = inside[a - 1].end + keep / 2 if a else max(start, inside[a].start - 0.02)
            hi = inside[b + 1].start - keep / 2 if b + 1 < len(inside) else min(end, inside[b].end + 0.02)
            if hi - lo > 0.05:
                out.append(Cut(round(lo, 3), round(hi, 3), "filler"))
    if pauses and inside:
        bounds = [(start, start)] + [(w.start, w.end) for w in inside] + [(end, end)]
        for (_, a), (b, _) in zip(bounds, bounds[1:]):
            a, b = max(a, start), min(b, end)
            if b - a > max(longest, GAP_FOUND):
                out.append(Cut(round(a + keep / 2, 3), round(b - keep / 2, 3), "pause"))
    return merge(out)


def merge(cuts: list[Cut]) -> list[Cut]:
    cuts = sorted((c for c in cuts if c.end > c.start), key=lambda c: c.start)
    out: list[Cut] = []
    for c in cuts:
        if out and c.start <= out[-1].end + MIN_PIECE:
            out[-1] = Cut(out[-1].start, max(out[-1].end, c.end), out[-1].why if out[-1].why == c.why else "both")
        else:
            out.append(c)
    return out


def keep_spans(start: float, end: float, cuts: list[Cut], fps: float = 0.0) -> list[tuple[float, float]]:
    """The stretches of [start, end) left after the cuts. With `fps`, every edge lands on a frame, so
    picture and sound stay in step however many cuts there are."""
    spans: list[tuple[float, float]] = []
    at = start
    for c in cuts:
        a, b = max(start, c.start), min(end, c.end)
        if b <= at:
            continue
        if a - at >= MIN_PIECE:
            spans.append((at, a))
        at = max(at, b)
    if end - at >= MIN_PIECE or not spans:
        spans.append((at, end))
    if fps > 0:
        snapped = []
        for a, b in spans:
            fa = start + round((a - start) * fps) / fps
            fb = start + round((b - start) * fps) / fps
            if fb - fa >= 1 / fps:
                snapped.append((round(fa, 6), round(fb, 6)))
        spans = snapped or [(start, end)]
    return spans


def length(spans: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in spans)


def to_output(t: float, spans: list[tuple[float, float]]) -> float | None:
    """Where source time `t` lands in the edited clip, or None when it was cut."""
    at = 0.0
    for a, b in spans:
        if a <= t < b:
            return at + t - a
        at += b - a
    return None


def to_source(t: float, spans: list[tuple[float, float]]) -> float:
    """The source time shown `t` seconds into the edited clip."""
    at = 0.0
    for a, b in spans:
        if t < at + (b - a):
            return a + (t - at)
        at += b - a
    return spans[-1][1] if spans else t


def remap_words(words: list[Word], spans: list[tuple[float, float]]) -> list[Word]:
    """The words that survive the cuts, timed in the edited clip. A word survives when its middle does."""
    out: list[Word] = []
    for w in words:
        mid = to_output((w.start + w.end) / 2, spans)
        if mid is None:
            continue
        a = to_output(w.start, spans)
        b = to_output(max(w.start, w.end - 1e-4), spans)
        a = mid - (w.end - w.start) / 2 if a is None else a
        b = mid + (w.end - w.start) / 2 if b is None else b
        out.append(Word(round(max(0.0, a), 3), round(max(a, b), 3), w.text))
    return out

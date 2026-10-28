"""Part series: a great moment longer than one clip becomes Part 1, Part 2, Part 3.

extend() looks at each clip the finder chose. One that ran into the longest clip length while
the video was still at its best gets a next part: it starts on the next sentence after the clip
ends and runs up to the longest length again, ending on a sentence. That repeats while the moment
stays as good, up to the most parts allowed (Settings → Clip finding → Part series). Parts come out
of the clip count: a series replaces the weakest standalone clips rather than adding to them, and
when it covers clips the finder chose, spare picks (the next best moments) fill their places.

A next part starts within layout.CARRIES_ON seconds of the part before, so layout.has_next_part
already says every part but the last has a next part, and the bottom banner promises it. When the
quality check rejects a part, the clip before it is made again without the promise (see
Engine._unpromise), and the parts are numbered again from what survived (labels(), written to
clips.json as "series", "part" and "parts" for TikTok Manager).
"""

from __future__ import annotations

import numpy as np

from .analyze import Clip, clip_score
from .layout import CARRIES_ON

FULL = 0.9  # a clip at least this share of the longest length ran out of room
KEEP_UP = 0.75  # the next stretch must be at least this interesting, relative to the clip
MIN_PART = 0.75  # a next part shorter than this share of the shortest length isn't worth posting
LEAD, TAIL = 0.15, 0.3  # seconds of room before a part's first sentence and after its last
OVERLAP = 0.05  # seconds two clips may share (rounding) without overlapping
WINDOW = 5  # seconds of dull video that end a moment
PAUSE = CARRIES_ON - 2 * (LEAD + TAIL)  # a longer silence ends the moment (and trimming could break the chain)

Label = tuple[str, int, int]  # (series id, part number, parts in the series); ("", 0, 0) = not a part


def _mean(score: np.ndarray, a: float, b: float) -> float:
    seg = score[max(0, int(a)): max(int(a) + 1, int(b))]
    return float(seg.mean()) if len(seg) else 0.0


def _hot_until(score: np.ndarray, start: float, stop: float, bar: float) -> float:
    """The first second after `start` where the next few seconds fall under `bar` (or `stop`)."""
    t = int(start)
    while t < int(stop):
        if _mean(score, t, t + WINDOW) < bar - 1e-6:
            return float(t)
        t += 1
    return stop


def _next_part(part: Clip, sents: list, score: np.ndarray, bar: float, min_len: float, max_len: float,
               duration: float) -> tuple[float, float] | None:
    """Where the part after `part` goes, (start, end), or None when the moment doesn't carry on.
    It starts on the next sentence and ends on a sentence once the moment is over, or as long as allowed."""
    if sents:
        first = next((s for s in sents if s.start >= part.end - 0.05), None)
        if first is None or first.start - part.end > PAUSE:
            return None  # a long silence: the moment is over
        start = max(part.end, first.start - LEAD)
    else:
        first, start = None, part.end
    if _mean(score, start, start + min_len) < bar - 1e-6:
        return None  # the moment is over
    limit = min(duration, start + max_len)
    over = min(limit, _hot_until(score, start + min_len, limit, bar) + WINDOW)
    if first is None:
        end = over
    else:
        ends = [s.end for s in sents if s.start >= first.start and s.end + TAIL <= limit + 0.01]
        fits = [e for e in ends if e <= over] or [e for e in ends if e - start >= min_len][:1]
        if not fits:
            return None
        last = fits[-1]
        after = next((s.start for s in sents if s.start >= last - 0.01), duration)
        end = min(duration, last + TAIL, max(last, after - 0.05))
    if end - start < min_len * MIN_PART:
        return None
    return round(float(start), 2), round(float(end), 2)


def extend(clips: list[Clip], score: np.ndarray, sents: list, min_len: float, max_len: float, count: int,
           max_parts: int, duration: float, spares: list[Clip] = ()) -> int:
    """Add next parts to the clips a great moment outlasted, then keep `count` clips (in place).
    A weaker clip the finder put further into the same moment gives way to the parts, and `spares`
    (the finder's next picks, as good as it gets after `clips`) may take the room that leaves.
    Returns how many parts were added and kept."""
    if max_parts < 2 or not len(score) or not clips:
        return 0
    floor = float(np.median(score))  # a moment has to stay better than most of the video
    added: list[Clip] = []

    def signal(c: Clip) -> float:  # an AI pick's score mixes in the AI's rating; compare like with like
        return clip_score(score, c.start, c.end) if c.origin == "ai" else c.score

    for first in sorted([c for c in clips if c.enabled and not c.edited], key=lambda c: c.score, reverse=True):
        if first not in clips:
            continue  # gave way to an earlier series
        offset = first.score - clip_score(score, first.start, first.end)  # a part scores on its clip's scale
        part, chain = first, 1
        bar = max(floor, _mean(score, first.start, first.end) * KEEP_UP)
        while chain < max_parts and part.length >= max_len * FULL:
            where = _next_part(part, sents, score, bar, min_len, max_len, duration)
            if where is None:
                break
            start, end = where
            if any(o.enabled and abs(o.start - part.end) <= CARRIES_ON and o.end > part.end for o in clips + added):
                break  # a clip already carries on from this one: it is the next part
            nxt = Clip(start, end, round(clip_score(score, start, end) + offset, 1), origin="part",
                       reason="Carries on from the clip before: the moment was longer than one clip")
            inside = [o for o in clips + added if o.enabled and o is not part
                      and start < o.end - OVERLAP and end > o.start + OVERLAP]
            if any(o.edited or signal(o) > signal(first) or o in added for o in inside):
                break  # a clip you placed, a better one, or another series is there
            for o in inside:
                clips.remove(o)
            added.append(nxt)
            part, chain = nxt, chain + 1
    if not added:
        return 0
    clips.extend(added)
    on = [c for c in clips if c.enabled]
    for s in sorted(spares, key=lambda c: c.score, reverse=True):  # well clear of every clip, so no false series
        if all(s.end + CARRIES_ON < o.start or s.start > o.end + CARRIES_ON for o in on):
            clips.append(s)
            on.append(s)
    keep(clips, count)
    return sum(1 for c in added if c in clips)


def keep(clips: list[Clip], count: int) -> None:
    """At most `count` clips (in place), best first. A series' next part only counts once the part
    before it is in, and then competes on its own score, so a weak last part never pushes out a
    better standalone clip. Parts end up next to each other, in order, so their files are numbered
    in order too. Hand-edited and switched-off clips stay."""
    chains: dict[str, list[Clip]] = {}
    for c, (sid, _, _) in zip(clips, labels(clips)):
        if c.enabled and not c.edited:
            chains.setdefault(sid or f"one:{id(c)}", []).append(c)
    for chain in chains.values():
        chain.sort(key=lambda c: c.start)
    fixed = [c for c in clips if not c.enabled or c.edited]
    room = max(0, count - sum(1 for c in fixed if c.enabled))
    taken = {key: 0 for key in chains}
    for _ in range(min(room, sum(len(ch) for ch in chains.values()))):
        key = max((k for k, ch in chains.items() if taken[k] < len(ch)), key=lambda k: chains[k][taken[k]].score)
        taken[key] += 1
    order = sorted((k for k in chains if taken[k]), key=lambda k: max(c.score for c in chains[k][:taken[k]]),
                   reverse=True)
    clips[:] = [c for k in order for c in chains[k][:taken[k]]] + fixed


def labels(clips: list[Clip], kept: list[bool] | None = None) -> list[Label]:
    """Number each run of clips that carry on from one another, one label per clip. `kept` (one
    per clip) leaves out the ones the quality check rejected: they won't be posted, so they aren't
    parts, and the run breaks there."""
    alive = [i for i, c in enumerate(clips) if c.enabled and (kept is None or kept[i])]
    order = sorted(alive, key=lambda i: clips[i].start)
    nxt: dict[int, int] = {}
    taken: set[int] = set()
    for i in order:
        c = clips[i]
        j = next((j for j in order if j != i and j not in taken and clips[j].start > c.start
                  and clips[j].end > c.end and abs(clips[j].start - c.end) <= CARRIES_ON), None)
        if j is not None:
            nxt[i] = j
            taken.add(j)
    out: list[Label] = [("", 0, 0)] * len(clips)
    n = 0
    for i in order:
        if i in taken or i not in nxt:
            continue
        n += 1
        chain = [i]
        while chain[-1] in nxt:
            chain.append(nxt[chain[-1]])
        for k, j in enumerate(chain, 1):
            out[j] = (f"series-{n}", k, len(chain))
    return out


def details(label: Label) -> dict:
    """What clips.json says about a clip's place in its series (nothing when it isn't a part)."""
    sid, part, parts = label
    return {"series": sid, "part": part, "parts": parts} if sid else {}

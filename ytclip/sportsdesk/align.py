"""Where in the video a feed's event is: from the game clock to the video's own seconds.

A feed (feeds.py) says a red card came in the 67th minute, or a technical foul with 4:12 left in the third. The
video says nothing of the kind: it has a pre-game show in front, a half-time in the middle, ads, or it's a
ten-minute highlights cut where the 67th minute is at 6:40. So each event is placed in up to three steps:

1. **Anchors**: moments where the video's second and the game's clock are both known.
   - the commentary ("and we're under way", "the second half is under way", "start of the fourth quarter");
   - the broadcast's own scoreboard, read off a handful of frames by Claude or Gemini (scoreboard.py) when one
     has a key: the period and the clock at that frame, the most exact anchor there is;
   - yours (`--anchor 2=58:40`: the second half starts at 58:40 of the video).
2. **The clock**, for a whole game: an event's clock against the nearest anchor of its period. Soccer's clock
   runs with the world, so that's good to a few seconds. The others stop their clocks, so the estimate
   stretches the gap by the sport's usual ratio (a 12-minute quarter takes about 27) or, between two anchors,
   by what the video shows, and comes with a wide margin. When the feed gives the time of day of each play, the
   gaps between plays are real seconds and the margin is small.
3. **The commentary**: inside that margin (or, for a highlights cut, anywhere, keeping the events in order),
   the stretch of commentary that best matches the event's own words, when there is one. That's the final
   position; without a match the clock's estimate stands, with its margin, and without either the event is left
   out rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import numpy as np

from ..subtitles import Cue
from .feeds import CLOCKS, Feed, FeedEvent

FULL_SHARE = 0.7  # a video at least this much of a game's clock time is a whole game, not a cut
MATCH_MIN = 0.42  # a commentary match weaker than this isn't one (0-1, semantic_search.event_match)
ORDER_MIN = 0.5  # for a highlights cut, where there's no clock to agree with

ORDINALS = {"first": 1, "1st": 1, "opening": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
_UNDERWAY = r"(?:under ?way|kicks? off|kicked off|begins|starts|is go)"
_NOT_YET = re.compile(r"\b(?:before|until|ahead of|prior to|minutes? (?:to|from|until|before)|waiting for|build[- ]up to|"
                      r"countdown to|when we|as soon as|once we)\b[^.?!]{0,40}$", re.I)
ANCHOR_WORDS: dict[str, tuple[tuple[int, re.Pattern], ...]] = {
    "soccer": (
        (2, re.compile(rf"\bsecond half (?:is |gets |now |has )?{_UNDERWAY}|\bback under ?way\b|"
                       r"\bunder ?way (?:again|in the second|for the second)|\bgets? the second half (?:started|going|"
                       r"under ?way)|\bwe(?:'re| are) (?:back )?under ?way (?:again|for the second)", re.I)),
        (1, re.compile(r"\bwe(?:'re| are) (?:off and running|under ?way)\b|\band we(?:'re| are) off\b|"
                       r"\bgets? (?:us|the (?:game|match)|this (?:game|match|one)) (?:started|going|under ?way)|"
                       r"\b(?:kicks?|kicked) (?:us )?off\b|\b(?:the )?(?:game|match) is under ?way", re.I)),
    ),
}
_PERIOD_START = re.compile(
    r"\b(?:start|beginning) of the (first|1st|second|2nd|third|3rd|fourth|4th) (?:quarter|period|half)\b|"
    r"\b(first|1st|second|2nd|third|3rd|fourth|4th) (?:quarter|period|half) (?:is |gets |now )?(?:under ?way|begins|starts)\b|"
    r"\b(?:begin|start|open)s? the (second|2nd|third|3rd|fourth|4th) (?:quarter|period|half)\b|"
    r"\bunder ?way in the (first|1st|second|2nd|third|3rd|fourth|4th)\b", re.I)
_FIRST_START = re.compile(r"\b(?:opening tip|tip(?:ped)?[- ]?off\b|jump ball to (?:start|get us)|opening kick ?off|"
                          r"puck (?:is )?(?:dropped|down)|opening face[- ]?off|we(?:'re| are) under ?way)\b", re.I)


@dataclass
class Anchor:
    video: float  # seconds into the video
    period: int
    elapsed: float  # seconds of game clock since the period began
    source: str  # commentary | scoreboard | you
    wallclock: float | None = None


@dataclass
class Placed:
    event: FeedEvent
    video: float
    slack: float  # the event is within this many seconds of `video`, as far as we know
    how: str  # file | commentary | clock | time of day | order


# ---------------------------------------------------------------- anchors


def transcript_anchors(cues: list[Cue], sport: str, duration: float) -> list[Anchor]:
    """Period starts the commentary announces."""
    out: dict[int, Anchor] = {}
    for cue in cues:
        text = cue.text
        for period, rx in ANCHOR_WORDS.get(sport, ()):
            m = rx.search(text)
            if not m or _NOT_YET.search(text[:m.start()]):
                continue
            # A first half can't start in the video's last stretch, nor a second in its first.
            if period == 1 and cue.start > 0.45 * duration:
                continue
            if period == 2 and (cue.start < 0.2 * duration or (1 in out and cue.start - out[1].video < 40 * 60
                                                                 and duration > 80 * 60)):
                continue
            out.setdefault(period, Anchor(cue.start, period, 0.0, "commentary"))
            break
        if sport != "soccer":
            m = _PERIOD_START.search(text)
            if m:
                word = next(g for g in m.groups() if g)
                p = ORDINALS.get(word.lower(), 0)
                if p and p not in out and not _NOT_YET.search(text[:m.start()]):
                    out[p] = Anchor(cue.start, p, 0.0, "commentary")
            elif 1 not in out and cue.start < 0.3 * duration and _FIRST_START.search(text):
                out[1] = Anchor(cue.start, 1, 0.0, "commentary")
    return sorted(out.values(), key=lambda a: a.video)


def parse_anchor(spec: str) -> Anchor:
    """`--anchor 2=58:40` (period 2 starts at 58:40 of the video) or `2@12:00=1:02:10` (period 2, 12:00 on the
    clock, is at 1:02:10)."""
    m = re.fullmatch(r"\s*(\d)\s*(?:@\s*([\d:.]+))?\s*=\s*([\d:.]+)\s*", spec or "")
    if not m:
        raise ValueError(f"An anchor looks like 2=58:40 or 2@12:00=1:02:10, not {spec!r}.")

    def secs(text: str) -> float:
        total = 0.0
        for part in text.split(":"):
            total = total * 60 + float(part)
        return total

    return Anchor(secs(m[3]), int(m[1]), secs(m[2]) if m[2] else 0.0, "you")


def scoreboard_anchor(period_text: str, clock_text: str, sport: str, video: float) -> Anchor | None:
    """An anchor from what a scoreboard shows ("2ND", "4:12"; soccer: "", "67:12")."""
    periods, length, down, _ratio = CLOCKS.get(sport, (0, 0, True, 0.0))
    m = re.fullmatch(r"\s*(\d{1,3}):(\d{2})(?:\.\d)?\s*", clock_text or "")
    if not m or not length:
        return None
    shown = int(m[1]) * 60 + int(m[2])
    if sport == "soccer":
        extra = "+" in (clock_text or "") or "+" in (period_text or "")
        half = 2 if (shown > 45 * 60 + 30 and not extra) or re.search(r"2|second", period_text or "", re.I) else 1
        if re.search(r"1|first", period_text or "", re.I):
            half = 1
        return Anchor(video, half, max(0.0, shown - (half - 1) * length), "scoreboard")
    pm = re.search(r"([1-4])|(first|second|third|fourth)", period_text or "", re.I)
    if not pm or shown > length:
        return None
    period = int(pm[1]) if pm[1] else ORDINALS[pm[2].lower()]
    return Anchor(video, period, float(length - shown) if down else float(shown), "scoreboard")


def clock_anchors(src, duration: float, sport: str, s, *, frames: int = 8, log=None, check=None) -> list[Anchor]:
    """Anchors read off the broadcast's scoreboard at `frames` moments spread through the video (one request to
    Claude or Gemini; [] when neither has a key or nothing could be read)."""
    from . import scoreboard

    if sport not in CLOCKS or not CLOCKS[sport][1] or not scoreboard.can_read(s) or duration < 600:
        return []
    times = [duration * (i + 0.5) / frames for i in range(frames)]
    read, _by = scoreboard.read(src, times, s, log=log, check=check)
    out = []
    for t, sc in zip(times, read):
        if sc is None:
            continue
        a = scoreboard_anchor(sc.period, sc.clock, sport, t)
        if a is not None:
            out.append(a)
    return out


# ---------------------------------------------------------------- placing


def is_full_game(duration: float, sport: str, kind: str = "") -> bool:
    if kind == "full":
        return True
    if kind in ("highlights", "extended"):
        return False
    periods, length, _down, _ratio = CLOCKS.get(sport, (0, 0, True, 0.0))
    return bool(length) and duration >= FULL_SHARE * periods * length


def by_clock(ev: FeedEvent, anchors: list[Anchor], sport: str) -> tuple[float, float] | None:
    """(video seconds, margin) of `ev` from its game clock and the anchors of its period."""
    if ev.elapsed is None or not ev.period:
        return None
    mine = sorted((a for a in anchors if a.period == ev.period), key=lambda a: a.elapsed)
    if not mine:
        return None
    ratio = CLOCKS.get(sport, (0, 0, True, 1.0))[3] or 1.0
    before = [a for a in mine if a.elapsed <= ev.elapsed]
    after = [a for a in mine if a.elapsed > ev.elapsed]
    if before and after and after[0].video > before[-1].video:  # between two: the video's own pace
        a, b = before[-1], after[0]
        share = (ev.elapsed - a.elapsed) / max(1e-6, b.elapsed - a.elapsed)
        gap = min(ev.elapsed - a.elapsed, b.elapsed - ev.elapsed)
        return a.video + share * (b.video - a.video), 15.0 + (0.0 if ratio == 1.0 else 0.35 * gap * ratio)
    a = before[-1] if before else after[0]
    gap = ev.elapsed - a.elapsed
    if ratio == 1.0:
        return a.video + gap, 20.0 + 0.01 * abs(gap)  # a running clock: good to seconds (stoppages are on it)
    return a.video + gap * ratio, 30.0 + 0.4 * abs(gap) * ratio


def by_time_of_day(ev: FeedEvent, known: list[tuple[float, float]]) -> tuple[float, float] | None:
    """(video seconds, margin) from the event's time of day and events whose place is already known
    ((time of day, video seconds) pairs): the nearest one in time carries its offset over."""
    if ev.wallclock is None or not known:
        return None
    wall, video = min(known, key=lambda k: abs(k[0] - ev.wallclock))
    gap = abs(ev.wallclock - wall)
    return video + (ev.wallclock - wall), 20.0 + 0.02 * gap  # ad breaks cut from the upload stretch the margin


def place(feed: Feed, events: list[FeedEvent], duration: float, cues: list[Cue], *, anchors: list[Anchor] | None = None,
          kind: str = "", match: Callable[[FeedEvent, float, float], tuple[float, float] | None] | None = None,
          order_scores: Callable[[list[FeedEvent]], tuple[np.ndarray, np.ndarray]] | None = None
          ) -> tuple[list[Placed], str]:
    """Each of `events` (some of the feed's) in the video, and a sentence saying how well that went.

    `match(event, lo, hi)` gives the best commentary match between `lo` and `hi` seconds as (video seconds,
    strength 0-1) or None. `order_scores(events)` gives, for a highlights cut, (strength of every event against
    every stretch of commentary, each stretch's seconds)."""
    anchors = list(anchors or []) + transcript_anchors(cues, feed.sport, duration)
    full = is_full_game(duration, feed.sport, kind)
    placed: list[Placed] = []
    left: list[FeedEvent] = []
    for ev in events:
        if ev.video is not None and 0 <= ev.video <= duration:
            placed.append(Placed(ev, float(ev.video), 2.0, "file"))
        else:
            left.append(ev)
    if not left:
        return sorted(placed, key=lambda p: p.video), ""
    if not full:
        got = _in_order(left, duration, order_scores)
        note = "" if got else ("This looks like a highlights cut, and its commentary doesn't name the feed's "
                               "events clearly enough to place them.")
        return sorted(placed + got, key=lambda p: p.video), note

    # A whole game: the clock, then the time of day from the events the clock placed, then the commentary.
    known_wall: list[tuple[float, float]] = []
    for a in anchors:  # a period's start is in the feed too, with its time of day
        start = next((e for e in feed.events if e.kind == "period_start" and e.period == a.period
                      and e.wallclock is not None), None)
        if start is not None and a.elapsed == 0.0:
            known_wall.append((start.wallclock, a.video))
    unplaced = 0
    for ev in left:
        est = by_time_of_day(ev, known_wall) or by_clock(ev, anchors, feed.sport)
        how = "time of day" if (ev.wallclock is not None and known_wall) else "clock"
        if est is None:
            unplaced += 1
            continue
        t, slack = est
        if not -slack <= t <= duration + slack:
            unplaced += 1
            continue
        t = float(min(max(t, 0.0), duration))
        if match is not None:
            found = match(ev, max(0.0, t - slack - 8.0), min(duration, t + slack + 12.0))
            if found is not None and found[1] >= MATCH_MIN:
                placed.append(Placed(ev, found[0], 6.0, "commentary"))
                if ev.wallclock is not None:
                    known_wall.append((ev.wallclock, found[0]))  # a firmer footing for the events around it
                continue
        placed.append(Placed(ev, t, slack, how))
    note = ""
    if unplaced and not anchors:
        note = ("The feed's events couldn't be placed in the video: nothing says where a period starts. Add an "
                "anchor (the video second a half or quarter begins), or use a Claude or Gemini key so the "
                "scoreboard's clock can be read.")
    elif unplaced:
        note = f"{unplaced} of the feed's events fall in a period with no anchor, and were left out."
    return sorted(placed, key=lambda p: p.video), note


def _in_order(events: list[FeedEvent], duration: float,
              order_scores: Callable[[list[FeedEvent]], tuple[np.ndarray, np.ndarray]] | None) -> list[Placed]:
    """A highlights cut: there's no clock, but the events come in the game's order. Each event takes the
    stretch of commentary that names it, with no two crossing (the best set of non-crossing matches)."""
    if order_scores is None or not events:
        return []
    events = sorted(events, key=lambda e: (e.period, e.elapsed if e.elapsed is not None else 0.0))
    scores, times = order_scores(events)
    n, m = scores.shape if scores.ndim == 2 else (0, 0)
    if not n or not m:
        return []
    gain = np.where(scores >= ORDER_MIN, scores - ORDER_MIN + 0.05, -1.0)
    best = np.zeros((n + 1, m + 1))
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            take = best[i - 1, j - 1] + gain[i - 1, j - 1] if gain[i - 1, j - 1] > 0 else -1.0
            best[i, j] = max(best[i - 1, j], best[i, j - 1], take)
    out: list[Placed] = []
    i, j = n, m
    while i > 0 and j > 0:
        if gain[i - 1, j - 1] > 0 and abs(best[i, j] - (best[i - 1, j - 1] + gain[i - 1, j - 1])) < 1e-9:
            out.append(Placed(events[i - 1], float(times[j - 1]), 8.0, "order"))
            i, j = i - 1, j - 1
        elif best[i, j] == best[i - 1, j]:
            i -= 1
        else:
            j -= 1
    return out[::-1]

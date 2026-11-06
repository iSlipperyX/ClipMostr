"""Posting plan: turn the queue order into post times, and helpers to reorder the queue."""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Iterator

from .config import Settings
from .store import VideoItem


# A new account posts less for its first two weeks (Account.warm_up_start): (up to day, at most
# this many posts a day). From then on its own posts_per_day applies.
WARM_UP = ((3, 0), (7, 1), (14, 2))
WARM_UP_DAYS = WARM_UP[-1][0]
# More than this many posts a day is past what posting-frequency studies support; the Queue page warns.
SUGGESTED_MAX = 3


def _hm(text: str, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = text.strip().split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except (ValueError, AttributeError):
        return fallback


def _window(s: Settings) -> tuple[int, int]:
    """First and last post time, in minutes after midnight."""
    h0, m0 = _hm(s.first_post_time, (11, 0))
    h1, m1 = _hm(s.last_post_time, (21, 0))
    start, end = h0 * 60 + m0, h1 * 60 + m1
    return start, max(start, end)


def fitting(s: Settings) -> int:
    """How many of `posts_per_day` fit between the first and last post time, `min_gap_minutes` apart."""
    n = max(1, int(s.posts_per_day))
    gap = max(0, int(s.min_gap_minutes))
    if gap == 0:
        return n
    start, end = _window(s)
    return min(n, (end - start) // gap + 1)


def warm_up_cap(s: Settings, day: dt.date) -> int | None:
    """The most posts a warming-up account makes on `day`, or None once warm-up is over (or off)."""
    if not s.warm_up_start:
        return None
    n = (day - dt.date.fromtimestamp(s.warm_up_start)).days
    if n < 0:
        return 0
    return next((cap for upto, cap in WARM_UP if n < upto), None)


def warm_up_day(s: Settings, today: dt.date) -> int | None:
    """Which day of the warm-up `today` is (1-based), or None when the account isn't warming up."""
    if warm_up_cap(s, today) is None:
        return None
    return (today - dt.date.fromtimestamp(s.warm_up_start)).days + 1


def daily_slots(s: Settings, posts: int | None = None) -> list[dt.time]:
    """The day's post times before they're varied: as many of `posts_per_day` (or `posts`) as fit,
    spread evenly from first_post_time to last_post_time."""
    n = fitting(s) if posts is None else max(0, min(posts, fitting(s)))
    if n == 0:
        return []
    start, end = _window(s)
    if n == 1:
        mins = [start]
    else:
        step = (end - start) / (n - 1)
        mins = [round(start + i * step) for i in range(n)]
    return [dt.time(m // 60 % 24, m % 60) for m in sorted(set(mins))]


def _offset(s: Settings, day: dt.date, i: int) -> int:
    """A steady pseudo-random shift in minutes for slot `i` on `day`, within ± vary_minutes.

    Hashed from the account, day and slot, so replanning gives the same times and accounts differ.
    """
    j = max(0, int(s.vary_minutes))
    if j == 0:
        return 0
    h = hashlib.sha256(f"{s.schedule_seed}|{day.isoformat()}|{i}".encode()).digest()
    return int.from_bytes(h[:4], "big") % (2 * j + 1) - j


def slots_on(s: Settings, day: dt.date) -> list[dt.datetime]:
    """The post times on `day`: the daily slots (fewer while warming up), each shifted a little,
    still at least `min_gap_minutes` apart."""
    gap = max(0, int(s.min_gap_minutes))
    out: list[int] = []
    for i, t in enumerate(daily_slots(s, warm_up_cap(s, day))):
        m = t.hour * 60 + t.minute + _offset(s, day, i)
        if out:
            m = max(m, out[-1] + gap)
        m = max(0, min(24 * 60 - 1, m))
        if out and m - out[-1] < gap:  # pushed past midnight by the gap
            break
        out.append(m)
    midnight = dt.datetime.combine(day, dt.time())
    return [midnight + dt.timedelta(minutes=m) for m in out]


def describe(s: Settings, today: dt.date) -> list[str]:
    """Plain-language notes about the schedule for the Queue page: slots that don't fit,
    warm-up, and a count past what's recommended."""
    notes = []
    n, fit = max(1, int(s.posts_per_day)), fitting(s)
    if fit < n:
        notes.append(f"Only {fit} of {n} fit between {s.first_post_time} and {s.last_post_time} at "
                     f"{s.min_gap_minutes} min apart. Widen the hours or shorten the gap for more.")
    day = warm_up_day(s, today)
    if day is not None:
        today_n = min(warm_up_cap(s, today) or 0, fit)
        what = "no posts yet, just watch your niche on TikTok" if today_n == 0 else f"{today_n} a day for now"
        full = dt.date.fromtimestamp(s.warm_up_start) + dt.timedelta(days=WARM_UP_DAYS)
        notes.append(f"Warm-up day {day} of {WARM_UP_DAYS}: {what}. {fit} a day from {full.strftime('%a %d %b')}.")
    if fit > SUGGESTED_MAX:
        notes.append(f"{fit} a day is more than the {SUGGESTED_MAX} that posting studies support. Extra posts "
                     "rarely add views, and they look more automated to TikTok.")
    return notes


def _slot_stream(s: Settings, after: dt.datetime) -> Iterator[dt.datetime]:
    day = after.date()
    while True:
        for when in slots_on(s, day):
            if when >= after:
                yield when
        day += dt.timedelta(days=1)


def plan(queue: list[VideoItem], s: Settings, now: float, last_post: float | None) -> dict[str, float]:
    """Post time for every queued item, in queue order.

    Pinned items keep their time. Others take the next free daily slot that is at least
    `min_gap_minutes` after the previous post and not within the gap of a pinned post.
    """
    gap = dt.timedelta(minutes=max(0, s.min_gap_minutes))
    now_dt = dt.datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    pinned = sorted(dt.datetime.fromtimestamp(i.pinned_at) for i in queue if i.pinned_at)
    earliest = now_dt
    if last_post:
        earliest = max(earliest, dt.datetime.fromtimestamp(last_post) + gap)

    out: dict[str, float] = {}
    slots = _slot_stream(s, earliest)
    prev: dt.datetime | None = None
    for item in queue:
        if item.pinned_at:
            out[item.id] = item.pinned_at
            continue
        for when in slots:
            if prev and when < prev + gap:
                continue
            if any(abs((when - p).total_seconds()) < gap.total_seconds() for p in pinned):
                continue
            out[item.id] = when.timestamp()
            prev = when
            break
    return out


def due(queue: list[VideoItem], now: float) -> VideoItem | None:
    """The first queued item whose time has come (failed items are retried only if re-queued)."""
    for item in queue:
        if item.status == "queued" and item.planned_at and item.planned_at <= now:
            return item
    return None


def by_score(items: list[VideoItem]) -> list[VideoItem]:
    return sorted(items, key=lambda i: (i.total is None, -(i.total or 0)))


def spread(items: list[VideoItem]) -> list[VideoItem]:
    """Best-first, but never two clips from the same source video back to back when avoidable.

    The parts of a series stay together, in part order, placed by their best part's score: each
    part ends with "follow for part 2", so they're meant to go up one after another.
    """
    blocks: dict[str, list[VideoItem]] = {}
    for it in items:
        blocks.setdefault(it.series or f"\0{it.id}", []).append(it)
    for b in blocks.values():
        b.sort(key=lambda i: i.part or 0)

    def best(b: list[VideoItem]) -> tuple[bool, float]:
        scores = [i.total for i in b if i.total is not None]
        return (not scores, -max(scores) if scores else 0.0)

    pool = sorted(blocks.values(), key=best)
    out: list[VideoItem] = []
    while pool:
        last = out[-1].source_group if out else None
        pick = next((b for b in pool if not last or b[0].source_group != last), pool[0])
        pool.remove(pick)
        out += pick
    return out


def fmt_when(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "—"
    when = dt.datetime.fromtimestamp(ts)
    today = dt.datetime.fromtimestamp(now).date() if now else dt.date.today()
    if when.date() == today:
        day = "Today"
    elif when.date() == today + dt.timedelta(days=1):
        day = "Tomorrow"
    elif when.date() == today - dt.timedelta(days=1):
        day = "Yesterday"
    else:
        day = when.strftime("%a %d %b")
    return f"{day} {when.strftime('%H:%M')}"

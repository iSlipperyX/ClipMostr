"""How posted clips did, for picking and the AI polish to learn what works (learn.py).

After each stats reading, every posted clip that has been up for two days gets a row: its views and
its "lift", which is its views divided by the usual (median) views of that account's videos of the
same age or older. Lift lets a small account and a big one be compared. The rows go to results.json
in ClipMonster's settings folder, where learn.py ranks channels when picking and shows the AI polish
which hooks worked. Nothing here posts anything; it only reads the stats posting already has.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .store import Store, VideoItem

VERSION = 1
MIN_AGE_HOURS = 48.0  # younger posts are still growing; comparing them would mislead
MIN_BASELINE = 3  # videos needed to say what "usual" views are on an account
VIDEO_ID = re.compile(r"^[\w-]{11}$")


@dataclass
class Outcome:
    item_id: str
    account: str  # account id in this app
    channel: str  # the YouTube channel ClipMonster clipped
    video_id: str  # the YouTube video id, when known
    clip: str  # the clip's title
    hook: str  # ClipMonster's hook (its AI polish), "" if none
    caption: str
    length: float  # seconds
    rating: int | None
    score: float | None  # ClipMonster's clip score
    posted_at: float
    views: int
    likes: int
    comments: int
    shares: int
    lift: float  # views / the account's usual views
    niche: str = ""  # ClipMonster's niche id for the clip (clips.json "automation" → "niche_id"), "" if none


def from_clipmonster(item: VideoItem) -> bool:
    return bool(item.source_group or item.source_channel)


def baseline(store: Store, account: str, now: float, min_age_hours: float = MIN_AGE_HOURS) -> float | None:
    """The account's usual views: the median over its videos at least min_age_hours old."""
    cutoff = now - min_age_hours * 3600
    views = [v.views for v in store.videos(account).values() if v.create_time and v.create_time <= cutoff]
    return float(statistics.median(views)) if len(views) >= MIN_BASELINE else None


def outcomes(store: Store, now: float | None = None, min_age_hours: float = MIN_AGE_HOURS) -> list[Outcome]:
    """Every ClipMonster clip posted long enough ago, linked to its TikTok video, with its lift."""
    now = time.time() if now is None else now
    out: list[Outcome] = []
    with store.lock:
        accounts = list(store.stats)
        for account in accounts:
            usual = baseline(store, account, now, min_age_hours)
            if not usual:
                continue
            for it in store.items.values():
                if it.account != account or it.status != "posted" or not from_clipmonster(it):
                    continue
                v = store.video_for(it)
                if v is None:
                    continue
                posted = v.create_time or it.posted_at or 0
                if not posted or now - posted < min_age_hours * 3600:
                    continue
                group = it.source_group if VIDEO_ID.match(it.source_group or "") else ""
                out.append(Outcome(
                    item_id=it.id, account=account, channel=it.source_channel, video_id=group, clip=it.name,
                    hook=it.hook, caption=it.caption, length=round(it.duration or v.duration, 1),
                    rating=it.source_rating, score=it.source_score, posted_at=posted, views=v.views,
                    likes=v.likes, comments=v.comments, shares=v.shares, lift=round(v.views / usual, 3),
                    niche=it.niche_id))
    out.sort(key=lambda o: o.posted_at, reverse=True)
    return out


def results_path() -> Path:
    """results.json in ClipMonster's settings folder, where learn.py reads it."""
    from ..settings import app_dir

    return app_dir() / "results.json"


def share(store: Store, path: Path | None = None, now: float | None = None) -> int:
    """Write the outcomes for learn.py; returns how many clips were written."""
    now = time.time() if now is None else now
    path = path or results_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = outcomes(store, now)
    data = {"version": VERSION, "written_at": now, "from": "posting", "min_age_hours": MIN_AGE_HOURS,
            "outcomes": [asdict(o) for o in rows]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return len(rows)


def _times(lift: float) -> str:
    return f"{lift:.1f}×"


def insights(rows: list[Outcome], top: int = 2) -> list[str]:
    """Plain lines for the Performance tab: which source channels and hooks beat the usual views."""
    if len(rows) < 3:
        return []
    lines = [f"{len(rows)} ClipMonster clips are 2+ days old: median {_times(statistics.median(o.lift for o in rows))} "
             "your usual views."]
    by_channel: dict[str, list[float]] = {}
    for o in rows:
        if o.channel:
            by_channel.setdefault(o.channel, []).append(o.lift)
    ranked = sorted(((statistics.median(v), len(v), c) for c, v in by_channel.items() if len(v) >= 2), reverse=True)
    for lift, n, channel in ranked[:top]:
        if lift >= 1.25:
            lines.append(f"Clips from {channel} do best: {_times(lift)} your usual views ({n} posts).")
    for lift, n, channel in list(reversed(ranked))[:1]:
        if lift <= 0.6:
            lines.append(f"Clips from {channel} lag behind: {_times(lift)} your usual views ({n} posts).")
    best = max((o for o in rows if o.hook), key=lambda o: o.lift, default=None)
    if best and best.lift >= 1.5:
        lines.append(f"Best hook so far: “{best.hook}” ({_times(best.lift)}).")
    return lines

"""Which videos are worth clipping *now*: how fast each is rising against what's normal for its channel, and
whether short clips of it are already everywhere.

A scan (research/__init__.py) scores how well a video would cut (clipability.py). This adds the market side:

* **Velocity Delta**: the video's views per hour against the median views per hour of the same channel's
  other videos seen over the last 7 days (the history keeps a week). 1.0x is a normal upload for that
  channel; 3x is breaking out. A channel seen fewer than MIN_PEERS times is measured against its category
  in this scan, then against the whole scan, and the result says which.
* **Saturation**: how many short clips of it other channels have already posted, counted on YouTube Shorts
  with the YouTube Data API's own search (100 quota units a check, so only the best few get one, once per 12
  hours). YouTube Shorts is the one short-video platform with a search API open to everyone; TikTok and
  Reels have none ClipMonster may use, so Shorts stands in for them and the result says so.
* **Opportunity score** 0-100: demand (the delta) and clip-ability, cut down by saturation.
* **Badges**: Hot Arbitrage (breaking out, long-form, few short clips yet), High Retention (viewers rewatch
  clear moments), Oversaturated (short clips of it are already everywhere), Rising (breaking out; saturation
  not checked).
* **Roll-ups**: the same numbers per category (the pills' badges), and for a set of ready-made topics matched
  by title (topic_report).
"""

from __future__ import annotations

import math
import re
import statistics
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Callable

from ..trending import API_URL, History, ResearchError, TrendingVideo, _get

WEEK = 7 * 86400
MIN_PEERS = 3  # a channel's median needs this many other videos
SATURATION_FRESH = 12 * 3600
SATURATED_AT = 12  # this many other channels' Shorts of it counts as full
SEARCH_COST = 100  # YouTube API quota units per search.list call
DAILY_CHECKS = 20  # at most this many a day (2,000 of the key's 10,000 daily units)
LONG_FORM = 8 * 60

HOT, RETENTION, SATURATED, RISING = "Hot Arbitrage", "High Retention", "Oversaturated", "Rising"
BADGES = (HOT, RETENTION, SATURATED, RISING)
BADGE_HELP = {
    HOT: "Rising well above its channel's normal, long enough to cut, and few short clips of it exist yet.",
    RETENTION: "Viewers rewatch clear moments: the Most replayed graph has sharp peaks.",
    SATURATED: "Other channels have already posted many Shorts of it.",
    RISING: "Rising well above its channel's normal. Saturation wasn't checked.",
}

# Ready-made topics: (name, words any of which in the title puts a video in it)
TOPICS: dict[str, tuple[str, tuple[str, ...]]] = {
    "soccer-refs": ("Soccer referee calls", ("var", "red card", "penalty", "offside", "referee", "sent off", "handball")),
    "soccer-goals": ("Soccer goals & late winners", ("goal", "winner", "stoppage", "hat-trick", "hat trick", "free kick")),
    "nba-drama": ("NBA drama", ("ejected", "technical", "flagrant", "scuffle", "trash talk", "heated", "altercation")),
    "nba-clutch": ("NBA clutch shots", ("buzzer", "game-winner", "game winner", "clutch", "overtime", "game 7")),
    "nfl-calls": ("NFL controversial calls", ("pass interference", "flag", "review", "controversial", "roughing", "targeting")),
    "fight-finishes": ("Fight finishes", ("knockout", "ko", "submission", "tko", "finish", "stoppage")),
    "podcast-debates": ("Podcast debates", ("debate", "podcast", "argues", "heated", "calls out", "responds")),
    "streamer-moments": ("Streamer moments", ("rage", "stream", "reacts", "clip", "banned", "live")),
}

Get = Callable[[str], dict]


@dataclass
class Pace:
    velocity: float = 0.0  # views per hour
    baseline: float = 0.0  # what's normal: the median it's measured against
    delta: float = 0.0  # velocity / baseline (0 = couldn't be measured)
    basis: str = ""  # channel | category | scan
    peers: int = 0  # how many videos the baseline is the median of
    saturation: float | None = None  # 0-1; None = not checked
    shorts: int | None = None  # other channels' Shorts of it found
    checked: float = 0.0
    opportunity: float = 0.0  # 0-100
    badge: str = ""
    why: str = ""
    topics: list[str] = field(default_factory=list)  # TOPICS keys its title matches

    def to_json(self) -> dict:
        return asdict(self)


def baselines(history: History, now: float) -> dict[str, list[float]]:
    """channel id -> views per hour of each of its videos seen in the last 7 days (their latest pace)."""
    out: dict[str, list[float]] = {}
    with history.lock:
        for vid, rec in history.videos.items():
            ch, pace = rec.get("channel"), rec.get("pace")
            if ch and isinstance(pace, (int, float)) and pace > 0 and now - rec.get("last_seen", 0) <= WEEK:
                out.setdefault(ch, []).append((vid, float(pace)))
    return out


def measure(v: TrendingVideo, by_channel: dict, videos: list[TrendingVideo]) -> Pace:
    """The video's pace against its channel's 7-day median; else its category's in this scan; else the scan's."""
    p = Pace(velocity=round(v.velocity, 1))
    peers = [pace for vid, pace in by_channel.get(v.channel_id, []) if vid != v.id]
    basis = "channel"
    if len(peers) < MIN_PEERS:
        peers = [o.velocity for o in videos if o.id != v.id and o.velocity > 0 and v.category_id
                 and o.category_id == v.category_id]
        basis = "category"
    if len(peers) < MIN_PEERS:
        peers = [o.velocity for o in videos if o.id != v.id and o.velocity > 0]
        basis = "scan"
    if len(peers) < MIN_PEERS:
        return p
    p.baseline = round(statistics.median(peers), 1)
    p.peers, p.basis = len(peers), basis
    p.delta = round(v.velocity / p.baseline, 2) if p.baseline > 0 else 0.0
    return p


def demand(delta: float) -> float:
    """0-1 from a velocity delta: 1x (normal) is 0.3, 2x 0.55, 4x 0.8, 8x and up 1."""
    if delta <= 0:
        return 0.0
    return float(min(1.0, max(0.0, 0.3 + 0.25 * math.log2(delta))))


_WORD = re.compile(r"[a-z0-9']{3,}")
_FILLER = frozenset("the and for with from this that full game match highlights highlight official video hd extended "
                    "live stream watch new best all vs versus episode part".split())


def key_words(title: str, limit: int = 6) -> list[str]:
    seen: list[str] = []
    for w in _WORD.findall(title.lower()):
        if w not in _FILLER and w not in seen:
            seen.append(w)
    return seen[:limit]


def shorts_of(v: TrendingVideo, api_key: str, get: Get | None = None) -> int:
    """How many Shorts by *other* channels, posted since the video, share its title's key words: one
    search.list call (SEARCH_COST units)."""
    words = key_words(v.title)
    if not words:
        return 0
    params = {"part": "snippet", "type": "video", "videoDuration": "short", "maxResults": "50", "order": "date",
              "q": " ".join(words), "key": api_key}
    if v.published:
        params["publishedAfter"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v.published))
    data = (get or _get)(f"{API_URL}/search?{urllib.parse.urlencode(params)}")
    count = 0
    need = max(2, math.ceil(len(words) * 0.5))
    for item in data.get("items") or []:
        sn = item.get("snippet") or {}
        if sn.get("channelId") == v.channel_id:
            continue
        title = set(_WORD.findall(str(sn.get("title") or "").lower()))
        if sum(w in title for w in words) >= need:
            count += 1
    return count


def badge(v: TrendingVideo, p: Pace) -> tuple[str, str]:
    """(badge, a sentence saying why) for a video; ("", "") when none fits."""
    x = f"{p.delta:g}x its {p.basis}'s usual pace" if p.delta else ""
    if p.saturation is not None and p.saturation >= 0.6:
        return SATURATED, f"{p.shorts} other channels have already posted Shorts of it."
    if p.delta >= 2.0 and v.duration >= LONG_FORM and p.saturation is not None and p.saturation <= 0.25:
        few = "no Shorts of it yet" if not p.shorts else f"only {p.shorts} Short{'s' if p.shorts != 1 else ''} of it so far"
        return HOT, f"Rising at {x}, with {few} (YouTube Shorts; TikTok can't be checked)."
    if v.parts.get("replays", 0) >= 0.6:
        return RETENTION, "Viewers rewatch clear moments in it."
    if p.delta >= 2.0 and p.saturation is None:
        return RISING, f"Rising at {x}. How many short clips of it exist wasn't checked."
    return "", ""


def opportunity(v: TrendingVideo, p: Pace) -> float:
    """0-100: demand and clip-ability, cut down by how saturated it already is."""
    base = 0.5 * demand(p.delta) + 0.3 * (v.score / 100.0) + 0.2 * v.parts.get("replays", 0.4)
    if p.saturation is not None:
        base *= 1.0 - 0.7 * p.saturation
    else:
        base *= 0.85  # unknown isn't the same as empty
    return round(100 * min(1.0, base), 1)


def topics_of(title: str) -> list[str]:
    low = " " + re.sub(r"[^a-z0-9' -]", " ", title.lower()) + " "
    return [key for key, (_name, words) in TOPICS.items() if any(f" {w} " in low or f" {w}s " in low for w in words)]


def annotate(videos: list[TrendingVideo], history: History, now: float, *, api_key: str = "", checks: int = 0,
             get: Get | None = None, log: Callable[[str], None] | None = None) -> int:
    """Fill every video's `pace` (a Pace as a dict), `opportunity` and `badge`. Up to `checks` of the most
    promising get a saturation check. Returns the YouTube quota units spent."""
    by_channel = baselines(history, now)
    paces: dict[str, Pace] = {}
    for v in videos:
        p = measure(v, by_channel, videos)
        p.topics = topics_of(v.title)
        with history.lock:
            old = (history.videos.get(v.id) or {}).get("saturation")
        if isinstance(old, dict) and now - old.get("checked", 0) <= SATURATION_FRESH:
            p.saturation, p.shorts, p.checked = old.get("value"), old.get("shorts"), old.get("checked", 0.0)
        paces[v.id] = p
    spent = 0
    day = time.strftime("%Y-%m-%d", time.gmtime(now))  # the quota's own day is Pacific; close enough for a cap
    with history.lock:
        tally = history.market
        used = int(tally.get("count", 0)) if tally.get("day") == day else 0
    checks = min(checks, max(0, DAILY_CHECKS - used))
    if api_key and checks > 0:
        todo = [v for v in sorted(videos, key=lambda v: demand(paces[v.id].delta) * v.score, reverse=True)
                if paces[v.id].saturation is None and paces[v.id].delta >= 1.5 and v.duration >= LONG_FORM
                and v.live == "none"][:checks]
        for v in todo:
            try:
                n = shorts_of(v, api_key, get)
            except ResearchError as exc:
                if log:
                    log(f"Saturation checks stopped: {exc}")
                break
            spent += SEARCH_COST
            used += 1
            with history.lock:
                history.market = {"day": day, "count": used}
            p = paces[v.id]
            p.shorts, p.saturation, p.checked = n, round(min(1.0, n / SATURATED_AT), 2), now
            with history.lock:
                history.videos.setdefault(v.id, {"first_seen": now, "snapshots": []})["saturation"] = {
                    "value": p.saturation, "shorts": n, "checked": now}
    for v in videos:
        p = paces[v.id]
        p.opportunity = opportunity(v, p)
        p.badge, p.why = badge(v, p)
        v.pace, v.opportunity, v.badge = p.to_json(), p.opportunity, p.badge
    return spent


@dataclass
class Rollup:
    """A category's or a topic's videos in this scan, summed up for its pill."""

    key: str
    name: str
    videos: int = 0
    delta: float = 0.0  # median velocity delta of its videos
    opportunity: float = 0.0  # its best video's
    badge: str = ""  # the badge the pill shows
    best: str = ""  # its best video's id
    why: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def _roll_up(key: str, name: str, vids: list[TrendingVideo]) -> Rollup:
    n = Rollup(key, name, len(vids))
    if not vids:
        return n
    deltas = [(v.pace or {}).get("delta", 0.0) for v in vids if (v.pace or {}).get("delta")]
    n.delta = round(statistics.median(deltas), 2) if deltas else 0.0
    top = max(vids, key=lambda v: v.opportunity)
    n.opportunity, n.best = top.opportunity, top.id
    counts = {b: sum(1 for v in vids if v.badge == b) for b in BADGES}
    checked = [v for v in vids if (v.pace or {}).get("saturation") is not None]
    if counts[HOT]:
        n.badge, n.why = HOT, f"{counts[HOT]} video{'s' if counts[HOT] != 1 else ''} rising with few short clips yet."
    elif checked and counts[SATURATED] >= max(1, math.ceil(len(checked) / 2)):
        n.badge, n.why = SATURATED, f"{counts[SATURATED]} of the {len(checked)} checked already have many Shorts."
    elif counts[RETENTION] >= max(1, math.ceil(len(vids) / 3)):
        n.badge, n.why = RETENTION, f"{counts[RETENTION]} of {len(vids)} have clear rewatched moments."
    elif counts[RISING]:
        n.badge, n.why = RISING, f"{counts[RISING]} rising above their channel's normal."
    return n


def by_category(videos: list[TrendingVideo], names: dict[str, str]) -> dict[str, Rollup]:
    """category id -> its roll-up, for the category pills' opportunity badges (`names`: id -> name)."""
    return {cid: _roll_up(cid, name, [v for v in videos if v.category_id == cid]) for cid, name in names.items()}


def _topics(v: TrendingVideo) -> list[str]:
    pace = v.pace or {}
    return pace.get("topics") or pace.get("niches") or []  # ("niches" in scans saved by 3.7.0 dev builds)


def topic_report(videos: list[TrendingVideo]) -> list[Rollup]:
    """The ready-made topics that have videos in this scan, best opportunity first."""
    out = []
    for key, (name, _words) in TOPICS.items():
        vids = [v for v in videos if key in _topics(v)]
        if vids:
            out.append(_roll_up(key, name, vids))
    return sorted(out, key=lambda n: n.opportunity, reverse=True)

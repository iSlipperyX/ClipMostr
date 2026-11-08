"""The Research dashboard's numbers: momentum, viral potential, longevity, clusters, outliers and clip windows.

Everything here works on what a research check already has (the view counts it saw on each check, the
Most replayed graph, likes and comments), so none of it costs YouTube quota:

* Momentum delta (ΔV/Δt): views per hour lately (between the two newest checks, 20 minutes to 6 hours
  apart) against the video's baseline: its views per hour over the last day, or since upload until
  ClipMonster has watched it that long. Before there are two checks it says "Measuring" instead of guessing.
* Viral potential, 0-100: 35% velocity, 25% how peaky its Most replayed graph is, 20% engagement
  ((likes + comments) / views) and 20% how uncrowded its cluster is in this check. A part that can't be
  measured (no replay graph read yet, likes hidden) is left out and the rest re-weighted, as clip-ability does.
* Longevity: "ephemeral" (stale within about 48 hours: news, drama, a live event, or already fading fast)
  or "evergreen" (a clip of it should still work next week). An estimate from the title, YouTube's
  category, age and momentum, and it says why.
* Clusters: the videos grouped by your category, or by YouTube's category for ones that fit none of yours.
* Outliers: a video far faster than the rest of its cluster (3 times its median views per hour or more,
  and a clear statistical outlier on a log scale, so a cluster that's all over the place has none).
* Category velocity: each cluster's views per hour, hour by hour over the last day, from the checks.
* Clip windows: from the Most replayed graph, where each standout peak starts and ends (likely clip
  boundaries), its top (the punchline) and the steepest falls (where viewers drop off).
* Live arbitrage: taking off on YouTube right now and not used by you yet. ClipMonster doesn't search
  TikTok, so "few matches on TikTok" can't be checked: the stand-in is "nothing from you yet".
"""

from __future__ import annotations

import math
import re
import statistics
import time
from dataclasses import dataclass, field

from .clipability import ENGAGEMENT_CAP, VELOCITY_CAP, replay_peaks
from .trending import CATEGORIES, TrendingVideo

VIRAL_WEIGHTS = {"velocity": 0.35, "peakiness": 0.25, "engagement": 0.20, "saturation": 0.20}
VIRAL_LABELS = {"velocity": "Velocity", "peakiness": "Most replayed peakiness", "engagement": "Engagement ratio",
                "saturation": "Cluster not crowded"}
VIRAL_HELP = {
    "velocity": "Views per hour lately, on a log scale: 100 an hour is next to nothing, 250K an hour is the top",
    "peakiness": "How sharply its Most replayed graph peaks: clear standout moments make clear clips",
    "engagement": "(Likes + comments) / views: 6% or more is the top",
    "saturation": "How few other videos in this check share its cluster: less competition for the same viewers",
}
MIN_HOURLY_GAP = 20 * 60  # the two checks "lately" is measured between are at least this far apart…
MAX_HOURLY_SPAN = 6 * 3600  # …and at most this far
MIN_BASELINE_GAP = 3600  # the day's baseline needs at least an hour between its ends
DAY = 86400.0
HOURS = 24  # the category velocity chart's span
MAX_PAIR_GAP = 6 * 3600  # two checks further apart than this say too little about any one hour
OUTLIER_RATIO = 3.0  # an outlier is at least this many times its cluster's median views per hour…
OUTLIER_Z = 3.5  # …and this far out on a log scale (a modified z-score, Iglewicz and Hoaglin's cut-off)
MIN_CLUSTER_FOR_OUTLIERS = 4  # smaller clusters are compared with the whole check instead
ARBITRAGE_MAX_HOURS = 72
# Title words of moments that date fast (news, drama, live events, announcements, results)…
_EPHEMERAL = re.compile(r"\b(breaking|live|leak(s|ed)?|drama|responds?|exposed|arrested|announce(s|d|ment)?|"
                        r"trailer|today|tonight|just happened|update|news|highlights|results?|reveal(ed)?)\b", re.I)
# …and of ones that keep getting found
_EVERGREEN = re.compile(r"\b(how to|guide|tutorial|explained|tips|best of|compilation|ranking|every|history of|"
                        r"documentary|beginners?|top \d+|funniest)\b", re.I)
EPHEMERAL_CATEGORIES = {"25", "17"}  # News & Politics, Sports: headlines and results date within days
EVERGREEN_CATEGORIES = {"26", "27", "28"}  # Howto & Style, Education, Science & Technology
OTHER = "Other"
LONGEVITY_LABELS = {"ephemeral": "Ephemeral (< 48 h)", "evergreen": "Evergreen"}


# ---------------------------------------------------------------- momentum


@dataclass
class Momentum:
    hourly: float = 0.0  # views per hour lately (the baseline until it's known)
    baseline: float = 0.0  # views per hour over the last day, or since upload
    known: bool = False  # two checks far enough apart to measure "lately"
    span_minutes: float = 0.0  # what "lately" covered
    since_upload: bool = True  # the baseline is the average since upload (not a day of checks yet)

    @property
    def delta(self) -> float:
        """Lately against the baseline, as a fraction: +0.5 is half as fast again, -0.3 is 30% slower."""
        if not self.known or self.baseline <= 0:
            return 0.0
        return self.hourly / self.baseline - 1.0

    @property
    def label(self) -> str:
        if not self.known:
            return "Measuring"
        d = self.delta
        return ("Exploding" if d >= 1.0 else "Accelerating" if d >= 0.3 else "Rising" if d >= 0.1 else
                "Steady" if d > -0.15 else "Cooling" if d > -0.5 else "Fading")

    @property
    def arrow(self) -> str:
        if not self.known:
            return "·"
        d = self.delta
        return "⇈" if d >= 1.0 else "↑" if d >= 0.1 else "→" if d > -0.15 else "↘" if d > -0.5 else "↓"

    def short(self) -> str:
        """For a table cell: "↑ +34%", or "Measuring"."""
        return f"{self.arrow} {self.delta:+.0%}" if self.known else "Measuring"

    def text(self) -> str:
        if not self.known:
            return ("Measuring: momentum needs two checks 20 minutes to 6 hours apart. So far it has averaged "
                    f"{compact(self.baseline)} views an hour since upload")
        span = (f"{self.span_minutes / 60:.1f}".rstrip("0").rstrip(".") + " h") if self.span_minutes >= 90 else \
            f"{self.span_minutes:.0f} min"
        base = "its average since upload" if self.since_upload else "its last day"
        return (f"{self.label}: {self.delta:+.0%}. {compact(self.hourly)} views an hour over the last {span}, "
                f"against {compact(self.baseline)} an hour for {base}")


def _snapshots(snapshots) -> list[tuple[float, float]]:
    out = []
    for s in snapshots or []:
        try:
            out.append((float(s[0]), float(s[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(out)


def momentum(v: TrendingVideo, snapshots, now: float | None = None) -> Momentum:
    """The video's momentum delta from its view snapshots ([[time, views], …] as research.json keeps them)."""
    now = time.time() if now is None else now
    snaps = _snapshots(snapshots)
    m = Momentum()
    if not snaps:
        m.baseline = m.hourly = v.views / max(1.0, v.age_hours(now))
        return m
    end_t, end_v = snaps[-1]
    prev = next((s for s in reversed(snaps[:-1])
                 if MIN_HOURLY_GAP <= end_t - s[0] <= MAX_HOURLY_SPAN and s[1] <= end_v), None)
    # The baseline: from the oldest check in the day before the newest, when that goes further back than
    # "lately" does; else the average since upload
    base = next((s for s in snaps if end_t - DAY <= s[0] <= end_t - MIN_BASELINE_GAP and s[1] <= end_v), None)
    if base is not None and (prev is None or base[0] < prev[0]):
        m.baseline = (end_v - base[1]) / ((end_t - base[0]) / 3600)
        m.since_upload = False
    else:
        m.baseline = end_v / max(1.0, v.age_hours(end_t))
    if prev is not None:
        m.hourly = (end_v - prev[1]) / ((end_t - prev[0]) / 3600)
        m.known = True
        m.span_minutes = (end_t - prev[0]) / 60
    else:
        m.hourly = m.baseline
    return m


# ---------------------------------------------------------------- viral potential parts


def peakiness(v: TrendingVideo) -> float | None:
    """How sharply the Most replayed graph stands out (0-1), None when it hasn't been read."""
    deep = v.deep
    if deep is None or deep.error or not deep.heatmap:
        return None
    signal, _peaks = replay_peaks(deep.heatmap, v.duration)
    return max(0.0, min(1.0, signal))


def engagement_ratio(v: TrendingVideo) -> float | None:
    """(likes + comments) / views, None when the likes are hidden."""
    likes = v.likes if v.likes is not None else (v.deep.likes if v.deep and v.deep.likes is not None else None)
    if likes is None or not v.views:
        return None
    return (likes + (v.comments or 0)) / v.views


def velocity_part(views_per_hour: float) -> float:
    """0-1 on a log scale: 100 views an hour is nothing, VELOCITY_CAP is everything."""
    if views_per_hour <= 0:
        return 0.0
    return max(0.0, min(1.0, (math.log10(views_per_hour + 1) - 2) / (math.log10(VELOCITY_CAP) - 2)))


def saturation_part(size: int, biggest: int) -> float:
    """1.0 for a cluster of one, down to 0.2 for the most crowded cluster in the check."""
    if biggest <= 1:
        return 1.0
    return 1.0 - 0.8 * (max(1, size) - 1) / (biggest - 1)


def viral_score(parts: dict[str, float]) -> float:
    """The 0-100 blend of the parts there are, re-weighted over the ones measured."""
    total = sum(VIRAL_WEIGHTS[k] for k in parts if k in VIRAL_WEIGHTS)
    if not total:
        return 0.0
    return round(100 * sum(VIRAL_WEIGHTS[k] * x for k, x in parts.items() if k in VIRAL_WEIGHTS) / total, 1)


def cluster_key(v: TrendingVideo, names: dict[str, str], taken: set[str] | None = None) -> str:
    """The cluster a video is shown in: your category's name, else YouTube's category (marked "(YouTube)" when
    one of your categories has the same name, so the two are never mixed), else Other."""
    if v.category_id and names.get(v.category_id):
        return names[v.category_id]
    yt = (CATEGORIES.get(v.category) if v.category else "") or OTHER
    taken = {n.lower() for n in names.values()} if taken is None else taken
    return f"{yt} (YouTube)" if yt.lower() in taken else yt


def longevity(v: TrendingVideo, m: Momentum, now: float | None = None) -> tuple[str, str]:
    """('ephemeral' or 'evergreen', why) for a video."""
    age = v.age_hours(now)
    keeps = bool(_EVERGREEN.search(v.title))
    if v.live != "none":
        return "ephemeral", "a live broadcast: its moment is now"
    if _EPHEMERAL.search(v.title) and not keeps:
        return "ephemeral", "news, drama, a result or an announcement: clip it within a day or two or not at all"
    if v.category in EPHEMERAL_CATEGORIES and not keeps:
        return "ephemeral", f"{CATEGORIES[v.category]}: headlines and results date within days"
    if m.known and m.delta <= -0.5 and age < 72:
        return "ephemeral", "already fading fast in its first days"
    if age >= 7 * 24:
        return "evergreen", f"{age / 24:.0f} days old and still on the charts"
    if keeps or v.category in EVERGREEN_CATEGORIES:
        return "evergreen", "a guide, explainer or best-of: these keep getting found for months"
    return "evergreen", "nothing ties it to this week: a clip of it should still work later"


def outliers(speeds: dict[str, float]) -> dict[str, float]:
    """The ids far faster than the rest: OUTLIER_RATIO times the median or more, and a modified z-score of
    OUTLIER_Z or more on a log scale. id -> how many times the median it is."""
    vals = {k: max(0.0, float(x)) for k, x in speeds.items()}
    if len(vals) < MIN_CLUSTER_FOR_OUTLIERS:
        return {}
    med = statistics.median(vals.values())
    if med <= 0:
        return {}
    logs = {k: math.log10(x + 1) for k, x in vals.items()}
    lmed = statistics.median(logs.values())
    mad = statistics.median(abs(x - lmed) for x in logs.values())
    out = {}
    for k, x in vals.items():
        if x / med < OUTLIER_RATIO:
            continue
        if mad > 0 and 0.6745 * (logs[k] - lmed) / mad < OUTLIER_Z:
            continue
        out[k] = round(x / med, 1)
    return out


# ---------------------------------------------------------------- per video and per cluster


@dataclass
class Insight:
    """Everything the dashboard shows about one video."""

    video: TrendingVideo
    cluster: str
    momentum: Momentum
    viral: float = 0.0  # 0-100
    parts: dict[str, float] = field(default_factory=dict)  # each 0-1; one that can't be measured is left out
    longevity: str = "evergreen"  # ephemeral | evergreen
    longevity_why: str = ""
    arbitrage: bool = False
    arbitrage_why: str = ""
    outlier: float = 0.0  # times its cluster's median views per hour, 0 when it isn't an outlier
    outlier_of: str = ""  # what it was compared with: the cluster's name, or "this check"
    yours: bool = False  # it fits one of your categories (its cluster is that category)

    @property
    def speed(self) -> float:
        """Views per hour: lately when measured, else the check's own figure."""
        return self.momentum.hourly if self.momentum.known else (self.video.velocity or self.momentum.baseline)

    @property
    def longevity_label(self) -> str:
        return LONGEVITY_LABELS.get(self.longevity, self.longevity)


def snapshots_by_id(history, ids) -> dict[str, list]:
    if history is None:
        return {}
    with history.lock:
        return {i: [list(s) for s in (history.videos.get(i) or {}).get("snapshots") or []] for i in ids}


def insights(videos: list[TrendingVideo], category_names: dict[str, str], history=None,
             now: float | None = None) -> dict[str, Insight]:
    """An Insight per video id, scored against the rest of the check."""
    now = time.time() if now is None else now
    snaps = snapshots_by_id(history, [v.id for v in videos])
    taken = {n.lower() for n in category_names.values()}
    keys = {v.id: cluster_key(v, category_names, taken) for v in videos}
    sizes: dict[str, int] = {}
    for k in keys.values():
        sizes[k] = sizes.get(k, 0) + 1
    biggest = max(sizes.values(), default=1)
    out: dict[str, Insight] = {}
    for v in videos:
        m = momentum(v, snaps.get(v.id), now)
        i = Insight(v, keys[v.id], m, yours=bool(v.category_id and category_names.get(v.category_id)))
        i.parts["velocity"] = velocity_part(i.speed)
        p = peakiness(v)
        if p is not None:
            i.parts["peakiness"] = p
        e = engagement_ratio(v)
        if e is not None:
            i.parts["engagement"] = min(1.0, e / ENGAGEMENT_CAP)
        i.parts["saturation"] = saturation_part(sizes[keys[v.id]], biggest)
        i.viral = viral_score(i.parts)
        i.longevity, i.longevity_why = longevity(v, m, now)
        out[v.id] = i
    _mark_outliers(out)
    _mark_arbitrage(out, now)
    return out


def _mark_outliers(found: dict[str, Insight]) -> None:
    speeds = {k: i.speed for k, i in found.items()}
    whole = outliers(speeds)
    groups: dict[str, list[str]] = {}
    for k, i in found.items():
        groups.setdefault(i.cluster, []).append(k)
    for name, ids in groups.items():
        if len(ids) >= MIN_CLUSTER_FOR_OUTLIERS:
            hits, of = outliers({k: speeds[k] for k in ids}), name
        else:
            hits, of = {k: whole[k] for k in ids if k in whole}, "this check"
        for k, ratio in hits.items():
            found[k].outlier, found[k].outlier_of = ratio, of


def _mark_arbitrage(found: dict[str, Insight], now: float) -> None:
    """Live arbitrage: taking off right now (the check's fastest fifth, or accelerating), recent, long enough
    to clip, and not used by you yet."""
    if not found:
        return
    speeds = sorted(i.speed for i in found.values())
    cut = speeds[int(len(speeds) * 0.8)] if len(speeds) >= 5 else speeds[-1]
    for i in found.values():
        v = i.video
        age = v.age_hours(now)
        if v.used or v.sent or v.live != "none" or age > ARBITRAGE_MAX_HOURS or (v.duration and v.duration <= 60):
            continue
        fast = i.speed >= cut and i.speed >= 1000
        rising = i.momentum.known and i.momentum.delta >= 0.3 and i.speed >= 500
        if fast or rising:
            i.arbitrage = True
            how = i.momentum.label.lower() if i.momentum.known and rising else "among the fastest in this check"
            i.arbitrage_why = f"{how} at {compact(i.speed)} views an hour, {age_text(age)} old, nothing from you yet"


@dataclass
class Cluster:
    name: str
    insights: list[Insight]
    velocity: list[float | None] = field(default_factory=list)  # views per hour, hour by hour (cluster_velocity)

    @property
    def count(self) -> int:
        return len(self.insights)

    @property
    def yours(self) -> bool:
        """One of your categories (not YouTube's category for videos that fit none of yours)."""
        return any(i.yours for i in self.insights)

    @property
    def viral(self) -> float:
        """The median viral potential."""
        return statistics.median(i.viral for i in self.insights) if self.insights else 0.0

    @property
    def momentum(self) -> float | None:
        """The median momentum delta of the videos it could measure, None when none yet."""
        known = [i.momentum.delta for i in self.insights if i.momentum.known]
        return statistics.median(known) if known else None

    @property
    def speed(self) -> float:
        """Views per hour, all its videos together."""
        return sum(i.speed for i in self.insights)

    @property
    def hot(self) -> int:
        return sum(i.momentum.known and i.momentum.delta >= 0.3 for i in self.insights)

    @property
    def arbitrage(self) -> int:
        return sum(i.arbitrage for i in self.insights)

    @property
    def outliers(self) -> int:
        return sum(i.outlier > 0 for i in self.insights)

    def longevity_mix(self) -> dict[str, int]:
        out = {k: 0 for k in LONGEVITY_LABELS}
        for i in self.insights:
            out[i.longevity] = out.get(i.longevity, 0) + 1
        return out

    def top(self, n: int = 3) -> list[Insight]:
        return sorted(self.insights, key=lambda i: (i.viral, i.video.score), reverse=True)[:n]


def clusters(found: dict[str, Insight], history=None, now: float | None = None) -> list[Cluster]:
    """The clusters: your categories first, then YouTube's (Other last), each most promising first (median viral
    potential, then size)."""
    groups: dict[str, list[Insight]] = {}
    for i in found.values():
        groups.setdefault(i.cluster, []).append(i)
    velocity = cluster_velocity(found, history, now) if history is not None else {}
    out = [Cluster(k, v, velocity.get(k, [])) for k, v in groups.items()]
    return sorted(out, key=lambda c: (not c.yours, c.name == OTHER, -c.viral, -c.count, c.name))


def cluster_velocity(found: dict[str, Insight], history, now: float | None = None,
                     hours: int = HOURS) -> dict[str, list[float | None]]:
    """Each cluster's views per hour, hour by hour over the last `hours` (oldest first), from the checks: the
    sum over its videos of their views per hour in that hour (between the checks either side of it). None for
    an hour no two checks of any of its videos cover."""
    now = time.time() if now is None else now
    start = now - hours * 3600
    snaps = snapshots_by_id(history, list(found))
    out: dict[str, list[float | None]] = {}
    for vid, ins in found.items():
        row = out.setdefault(ins.cluster, [None] * hours)
        s = _snapshots(snaps.get(vid))
        num, den = [0.0] * hours, [0.0] * hours
        for (t0, v0), (t1, v1) in zip(s, s[1:]):
            if not (60 <= t1 - t0 <= MAX_PAIR_GAP) or v1 < v0 or t1 <= start or t0 >= now:
                continue
            rate = (v1 - v0) / ((t1 - t0) / 3600)
            for h in range(max(0, int((t0 - start) // 3600)), min(hours - 1, int((t1 - start) // 3600)) + 1):
                b0 = start + h * 3600
                overlap = min(b0 + 3600, t1) - max(b0, t0)
                if overlap > 0:
                    num[h] += rate * overlap
                    den[h] += overlap
        for h in range(hours):
            if den[h] > 0:
                row[h] = (row[h] or 0.0) + num[h] / den[h]
    return out


# ---------------------------------------------------------------- the Most replayed graph


@dataclass
class ClipWindow:
    start: float  # seconds: where replays climb above the video's usual (a likely clip's start)…
    end: float  # …and where they fall back (its end)
    peak: float  # the top of the peak: the punchline
    height: float  # the peak's height, 0-1 of the graph's top
    drop: float  # how far replays fall right after it, 0-1 of the peak (the retention drop-off)


def curve(heatmap: list[dict]) -> list[tuple[float, float, float]]:
    """(start, end, value) per segment of the Most replayed graph, in time order, scaled so the top is 1."""
    segs = []
    for h in heatmap or []:
        try:
            a, b, x = float(h["start_time"]), float(h["end_time"]), float(h.get("value") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if b > a:
            segs.append((a, b, max(0.0, x)))
    segs.sort()
    top = max((s[2] for s in segs), default=0.0)
    return [(a, b, x / top) for a, b, x in segs] if top > 0 else []


def edges(duration: float) -> tuple[float, float]:
    """The start and end of a video's body: its first max(20 s, 3%) is people skipping in and rewatching the
    intro, and its last max(30 s, 5%) the end screen, so neither says anything about a moment (as in
    clipability.replay_peaks)."""
    return max(20.0, duration * 0.03), duration - max(30.0, duration * 0.05)


def clip_windows(heatmap: list[dict], duration: float = 0.0, max_windows: int = 4, min_len: float = 15.0,
                 max_len: float = 90.0) -> list[ClipWindow]:
    """Likely clips from the Most replayed graph: each standout peak in the video's body (edges()), from where
    replays climb to halfway up it to where they fall back below that, kept between min_len and max_len seconds
    (more set-up before the peak than after it), and never overlapping."""
    segs = curve(heatmap)
    if len(segs) < 5:
        return []
    total = duration or segs[-1][1]
    lo_edge, hi_edge = edges(total)
    body = [k for k, (a, b, _x) in enumerate(segs) if lo_edge <= (a + b) / 2 <= hi_edge] or list(range(len(segs)))
    values = [s[2] for s in segs]
    top = max(values[k] for k in body)
    if top <= 0:
        return []
    values = [x / top for x in values]  # the body's top is 1: an intro spike doesn't flatten the rest
    base = statistics.median(values[k] for k in body)
    taken: list[ClipWindow] = []
    for i in sorted(body, key=lambda k: values[k], reverse=True):
        if len(taken) >= max_windows or values[i] < base + 0.25 * (1 - base) or values[i] - base < 0.15:
            break  # no more standout peaks (a flat graph has none)
        mid = (segs[i][0] + segs[i][1]) / 2
        if any(w.start - 5 <= mid <= w.end + 5 for w in taken):
            continue  # part of a peak already taken
        if (i > 0 and values[i - 1] > values[i]) or (i < len(segs) - 1 and values[i + 1] > values[i]):
            continue  # a slope, not a peak (the tail of the intro spike, say)
        level = base + 0.5 * (values[i] - base)
        lo = hi = i
        while lo > 0 and values[lo - 1] >= level:
            lo -= 1
        while hi < len(segs) - 1 and values[hi + 1] >= level:
            hi += 1
        start, end = segs[lo][0], segs[hi][1]
        if end - start < min_len:
            grow = min_len - (end - start)
            start, end = start - grow * 0.65, end + grow * 0.35
        if end - start > max_len:
            start, end = max(start, mid - max_len * 0.6), min(end, mid + max_len * 0.4)
        start, end = max(0.0, start), min(total, end)
        for w in taken:  # trim it off any window already taken
            if start < w.end and end > w.start:
                start, end = (max(start, w.end), end) if mid > w.peak else (start, min(end, w.start))
        if end - start < min(min_len, 8.0):
            continue
        after = values[hi + 1] if hi + 1 < len(segs) else 0.0
        drop = max(0.0, (values[i] - after) / values[i])
        taken.append(ClipWindow(round(start, 1), round(end, 1), round(mid, 1), round(min(1.0, values[i]), 3),
                                round(min(1.0, drop), 3)))
    # Two windows with a short lull between them are one moment, when together they still fit max_len
    merged: list[ClipWindow] = []
    for w in sorted(taken, key=lambda w: w.start):
        last = merged[-1] if merged else None
        if last and w.start - last.end <= 15 and w.end - last.start <= max_len:
            best = last if last.height >= w.height else w
            merged[-1] = ClipWindow(last.start, w.end, best.peak, best.height, w.drop)
        else:
            merged.append(w)
    return merged


def drop_offs(heatmap: list[dict], n: int = 3, min_fall: float = 0.15) -> list[tuple[float, float]]:
    """The steepest falls in replays inside the video's body (edges()): (time in seconds, how far it falls within
    three segments, 0-1 of the graph's top), in time order. Where viewers lose interest: a clip should end
    before one."""
    segs = curve(heatmap)
    if len(segs) < 4:
        return []
    lo_edge, hi_edge = edges(segs[-1][1])
    values = [s[2] for s in segs]
    falls = []
    for i in range(len(segs) - 1):
        if not lo_edge <= segs[i][1] <= hi_edge:
            continue  # the fall after the intro, and into the end screen, are always there
        fall = values[i] - min(values[i + 1:i + 4])
        if fall >= min_fall:
            falls.append((fall, i))
    picked: list[tuple[int, float]] = []
    for fall, i in sorted(falls, key=lambda f: (-f[0], f[1])):
        if len(picked) >= n:
            break
        if all(abs(i - j) > 3 for j, _ in picked):
            picked.append((i, fall))
    return sorted((segs[i][1], round(fall, 3)) for i, fall in picked)


# ---------------------------------------------------------------- text


def compact(n: float) -> str:
    n = float(n)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{n:.0f}"


def age_text(hours: float) -> str:
    if hours < 1:
        return f"{max(1, round(hours * 60))} min"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} days"

"""Clip-ability: how well a trending video is likely to cut into short clips, 0-100.

Scored before anything is downloaded. Interest parts, each 0-1, are blended by WEIGHTS:

* momentum   - views per hour (between checks when we have two, else since upload).
* replays    - YouTube's "Most replayed" heatmap, read with the same signal the Studio's
               clip finder uses: sharp standout peaks mean obvious clips; flat means none.
* engagement - likes and comments per view.
* freshness  - clips of a video posted today ride its wave; a fortnight later they don't.
* transcript - captions make titles, burned-in captions and the AI clip finder better.

Then the blend is multiplied by how much usable material there is (FIT), because a hot
video still can't be clipped if it's a 3-minute music video:

* length     - long-form (8-90 min) has the most material; Shorts have nothing to cut.
* category   - gaming, comedy and entertainment clip well; music and films get claimed.

Parts that can't be measured (hidden likes, no heatmap yet) are left out and the rest
re-weighted, so a missing number never drags a video down by itself.
"""

from __future__ import annotations

import math
import time

import numpy as np

from .analyze import heatmap_signal
from .organize import stamp
from .trending import CATEGORIES, TrendingVideo

WEIGHTS = {
    "momentum": 0.40,
    "replays": 0.30,
    "engagement": 0.12,
    "freshness": 0.12,
    "transcript": 0.06,
}
FIT = ("length", "category")  # multiply the blend instead of joining it
FIT_FLOOR = 0.25  # the multiplier when there's nothing usable to cut

PART_LABELS = {
    "momentum": "Momentum",
    "replays": "Most replayed peaks",
    "length": "Length to cut from",
    "engagement": "Engagement",
    "category": "Category",
    "freshness": "Freshness",
    "transcript": "Captions",
}

CATEGORY_FIT = {
    "20": 1.0,  # Gaming
    "23": 1.0,  # Comedy
    "24": 0.95,  # Entertainment
    "22": 0.9,  # People & Blogs (podcasts, vlogs, streamers)
    "17": 0.85,  # Sports
    "15": 0.8,  # Pets & Animals
    "27": 0.75,  # Education
    "28": 0.75,  # Science & Technology
    "26": 0.7,  # Howto & Style
    "2": 0.65,  # Autos & Vehicles
    "25": 0.6,  # News & Politics
    "19": 0.6,  # Travel & Events
    "29": 0.5,  # Nonprofits & Activism
    "1": 0.4,  # Film & Animation: trailers and studio content get claimed
    "10": 0.15,  # Music: almost always claimed, little to cut
}

# Views per hour at which momentum saturates (roughly a top-5 global trending video).
VELOCITY_CAP = 250_000
# A strong like + comment rate for a trending video.
ENGAGEMENT_CAP = 0.06


def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y1
    t = min(1.0, max(0.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def length_fit(seconds: float) -> float:
    m = seconds / 60
    if m <= 1:
        return 0.0  # already a Short
    if m <= 3:
        return _lerp(m, 1, 3, 0.15, 0.45)
    if m <= 8:
        return _lerp(m, 3, 8, 0.45, 0.9)
    if m <= 20:
        return _lerp(m, 8, 20, 0.9, 1.0)
    if m <= 90:
        return 1.0
    return _lerp(m, 90, 240, 1.0, 0.75)  # long streams: lots to cut, slow to process


def freshness(age_hours: float) -> float:
    if age_hours <= 36:
        return 1.0
    return _lerp(age_hours, 36, 14 * 24, 1.0, 0.25)


def momentum(velocity: float, all_velocities: list[float]) -> float:
    """Half absolute (log scale up to VELOCITY_CAP), half rank among this scan's videos,
    so a quiet region's best video still stands out without matching the US chart."""
    absolute = min(1.0, math.log10(1 + max(0.0, velocity)) / math.log10(1 + VELOCITY_CAP))
    if len(all_velocities) < 2:
        return absolute
    below = sum(v < velocity for v in all_velocities)
    equal = sum(v == velocity for v in all_velocities) - 1
    rank = (below + equal / 2) / (len(all_velocities) - 1)
    return 0.5 * absolute + 0.5 * rank


def engagement(v: TrendingVideo, likes: int | None = None) -> float | None:
    likes = v.likes if v.likes is not None else likes
    if not v.views or (likes is None and v.comments is None):
        return None
    rate = ((likes or 0) + 3 * (v.comments or 0)) / v.views
    return min(1.0, rate / ENGAGEMENT_CAP)


def replay_peaks(heatmap: list[dict], duration: float, max_peaks: int = 5) -> tuple[float, list[float]]:
    """(0-1 strength, peak times in seconds) from a Most-replayed heatmap.

    Strength mixes contrast - how far the best moments rise above the typical level - with
    how many separate standout moments there are (more peaks = more clips)."""
    n = max(1, int(math.ceil(duration or max((h["end_time"] for h in heatmap), default=0))))
    sig = heatmap_signal(heatmap, n) if heatmap else None
    if sig is None:
        return 0.0, []
    raw = np.zeros(n)
    for seg in heatmap:
        raw[int(seg["start_time"]):int(math.ceil(seg["end_time"]))] = seg["value"]
    edge_in, edge_out = max(20, int(n * 0.03)), max(30, int(n * 0.05))
    body = raw[edge_in:n - edge_out] if n > edge_in + edge_out + 10 else raw
    contrast = float(np.clip((np.percentile(body, 97) - np.median(body)) / 0.6, 0, 1))

    peaks: list[float] = []
    work = sig.copy()
    spacing = max(30, int(n * 0.04))
    while len(peaks) < max_peaks:
        i = int(np.argmax(work))
        if work[i] < 0.55:
            break
        # Heatmap segments are flat steps; report the middle of the top plateau.
        a, b = i, i
        while a > 0 and work[a - 1] >= work[i] - 1e-6:
            a -= 1
        while b < n - 1 and work[b + 1] >= work[i] - 1e-6:
            b += 1
        i = (a + b + 1) // 2
        peaks.append(float(i))
        work[max(0, i - spacing):i + spacing] = -1
    strength = 0.65 * contrast + 0.35 * min(1.0, len(peaks) / 3)
    return round(strength, 3), sorted(peaks)


def material(length: float, category: float) -> float:
    """The FIT multiplier: FIT_FLOOR with nothing to cut, 1.0 for long gaming/comedy videos."""
    return FIT_FLOOR + (1 - FIT_FLOOR) * math.sqrt(max(0.0, length) * max(0.0, category))


def score(v: TrendingVideo, all_velocities: list[float], now: float | None = None) -> None:
    """Fill v.score, v.parts, v.reasons and v.moments."""
    now = now or time.time()
    deep = v.deep if v.deep and not v.deep.error else None
    parts: dict[str, float] = {
        "momentum": momentum(v.velocity, all_velocities),
        "freshness": freshness(v.age_hours(now)),
        "transcript": 1.0 if v.captions or (deep and deep.captions) else 0.6,
        "length": length_fit(v.duration),
        "category": CATEGORY_FIT.get(v.category, 0.6),
    }
    eng = engagement(v, deep.likes if deep else None)
    if eng is not None:
        parts["engagement"] = eng
    v.moments = []
    if deep and deep.heatmap:
        parts["replays"], v.moments = replay_peaks(deep.heatmap, v.duration)

    blend = sum(WEIGHTS[k] * p for k, p in parts.items() if k in WEIGHTS) / sum(
        WEIGHTS[k] for k in parts if k in WEIGHTS)
    value = blend * material(parts["length"], parts["category"]) * 100
    reasons = _reasons(v, parts, deep, now)
    if v.live == "live":
        value = min(value, 15)
        reasons.insert(0, "Live right now: clip it once the stream has finished.")
    elif v.live == "upcoming":
        value = min(value, 5)
        reasons.insert(0, "Hasn't premiered yet.")
    elif v.duration and v.duration <= 60:
        value = min(value, 20)
        reasons.insert(0, "Already a Short, so there's nothing to cut.")
    v.parts = {k: round(p, 3) for k, p in parts.items()}
    v.score = round(value, 1)
    v.reasons = reasons


def _reasons(v: TrendingVideo, parts: dict[str, float], deep, now: float) -> list[str]:
    out: list[str] = []
    per_hour = f"{compact(v.velocity)} views/hour"
    if parts["momentum"] >= 0.75:
        out.append(f"Taking off: {per_hour}{' since the last check' if v.recent else ''}.")
    elif parts["momentum"] <= 0.3:
        out.append(f"Slowing down: {per_hour}.")
    if "replays" in parts:
        if v.moments and parts["replays"] >= 0.6:
            out.append(f"Clear standout moments viewers rewatch: {', '.join(stamp(t) for t in v.moments[:3])}.")
        elif v.moments:
            out.append(f"Some rewatched moments ({', '.join(stamp(t) for t in v.moments[:3])}), "
                       "but no single obvious highlight.")
        else:
            out.append("Viewers watch it evenly, with no standout moment.")
    elif deep is not None:
        out.append("YouTube shows no Most replayed graph yet (common for brand-new videos).")
    if parts["length"] >= 0.9:
        out.append(f"{stamp(v.duration)} long: plenty of material for several clips.")
    elif 0 < parts["length"] < 0.5:
        out.append(f"Only {stamp(v.duration)} long, so there's room for one clip at most.")
    if parts.get("engagement", 0) >= 0.7:
        out.append("Very high likes and comments for its views.")
    if parts["category"] <= 0.4:
        name = CATEGORIES.get(v.category, "This category")
        out.append(f"{name} uploads are often copyright-claimed when reposted.")
    if parts["freshness"] <= 0.5:
        out.append(f"Posted {age_text(v.age_hours(now))} ago; the wave may have passed.")
    if deep and deep.chapters >= 3:
        out.append(f"Has {deep.chapters} chapters, which the Studio can split on.")
    return out


def compact(n: float) -> str:
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{n:.0f}"


def age_text(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(hours * 60))} min"
    if hours < 48:
        return f"{int(hours)} h"
    return f"{int(hours / 24)} days"


def verdict(value: float) -> str:
    if value >= 80:
        return "Excellent clip potential"
    if value >= 65:
        return "Good clip potential"
    if value >= 45:
        return "Worth a look"
    return "Weak clip potential"


"""The virtual camera: where the crop window sits and how far it's zoomed in, frame by frame.

**Following the speaker.** The camera eases after the speaker's eye line with an exponential moving average,

    x̂(t) = α·x(t) + (1 − α)·x̂(t−1),   α = 0.08 per frame at 30 fps

which moves like a camera operator rather than a tracker: steady, then a smooth glide. A deadband keeps it
still while the speaker only shifts a little (the most common cause of seasick reframing); it moves once
they've drifted further than that, and keeps going until it has caught up. A scene cut, or the speaker
changing to someone across the frame, is a cut: the camera jumps straight there instead of panning.

**Zooms**, as a multiplier of the widest framing:

* punch-ins: each new sentence alternates between 1.00x and 1.08x, a cut-like change of shot size that
  makes a single static camera feel edited;
* slow push-in: for stories and curiosity clips, a creep from 1.00x to 1.15x over ten seconds, starting
  again at each change of topic (a long pause or a scene cut).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .face_tracker import Target

HOLD_LOST = 1.5  # seconds the camera stays on a face that left the picture before drifting home
MAX_ZOOM = 1.3
SLOW_SECONDS = 10.0
MIN_PUNCH_GAP = 1.5  # seconds a punch-in shot size holds at least
MIN_TOPIC = 6.0  # seconds between push-in restarts at least
TOPIC_PAUSE = 0.7  # a pause this long in the source between sentences is a change of topic
SLOW_CATEGORIES = ("Story", "Curiosity")


def alpha_for(alpha: float, fps: float) -> float:
    """The per-frame smoothing for `fps`, so the camera moves at the same speed whatever the frame rate
    (`alpha` is stated per frame at 30 fps)."""
    alpha = min(1.0, max(0.001, alpha))
    return 1 - (1 - alpha) ** (30.0 / max(1.0, fps))


def raw_targets(targets: list[Target], times: np.ndarray, home: tuple[float, float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Target x, y and a jump flag at each of `times`: the latest sample before each time, held for a
    while when the face is lost, then home."""
    xs = np.full(len(times), home[0], float)
    ys = np.full(len(times), home[1], float)
    jumps = np.zeros(len(times), bool)
    if not targets:
        return xs, ys, jumps
    sample_t = np.array([t.t for t in targets])
    last_seen: Target | None = None
    j = -1
    for i, t in enumerate(times):
        while j + 1 < len(targets) and sample_t[j + 1] <= t + 1e-6:
            j += 1
            if targets[j].x is not None:
                if targets[j].jump:
                    jumps[i] = True
                last_seen = targets[j]
        if last_seen is not None and t - last_seen.t <= HOLD_LOST:
            xs[i], ys[i] = last_seen.x, last_seen.y
        elif last_seen is None and j < 0:  # before the first sample: the first face found, if it's soon
            first = next((x for x in targets if x.x is not None), None)
            if first is not None and first.t - t <= HOLD_LOST:
                xs[i], ys[i] = first.x, first.y
    return xs, ys, jumps


def follow(xs: np.ndarray, ys: np.ndarray, jumps: np.ndarray, times: np.ndarray, cuts: list[float], fps: float,
           alpha: float = 0.08, deadband: float = 0.04) -> tuple[np.ndarray, np.ndarray]:
    """The camera's eased path toward the targets: EMA plus a deadband with hysteresis, cutting on jumps
    and at scene cuts."""
    a = alpha_for(alpha, fps)
    out_x, out_y = np.empty(len(xs)), np.empty(len(ys))
    cut_set = sorted(cuts)
    ci = 0
    cam_x = cam_y = None
    moving = False
    prev_t = -1e9
    for i in range(len(xs)):
        t = times[i]
        crossed = False
        while ci < len(cut_set) and cut_set[ci] <= t:
            crossed = crossed or cut_set[ci] > prev_t
            ci += 1
        tx, ty = xs[i], ys[i]
        if cam_x is None or jumps[i] or crossed:
            cam_x, cam_y, moving = tx, ty, False
        else:
            err = math.hypot(tx - cam_x, (ty - cam_y) * 0.6)
            if not moving and err > deadband:
                moving = True
            if moving:
                cam_x += a * (tx - cam_x)
                cam_y += a * (ty - cam_y)
                if math.hypot(tx - cam_x, (ty - cam_y) * 0.6) < deadband / 4:
                    moving = False
        out_x[i], out_y[i] = cam_x, cam_y
        prev_t = t
    return out_x, out_y


def sentence_starts(words, gap: float = 0.6) -> list[float]:
    """Output times where a new sentence starts: after . ! ? or a pause."""
    starts: list[float] = []
    for i, w in enumerate(words):
        if i == 0:
            starts.append(w.start)
            continue
        prev = words[i - 1]
        if prev.text.rstrip("\"'”’)").endswith((".", "!", "?", "…")) or w.start - prev.end >= gap:
            starts.append(w.start)
    return starts


def punch_path(times: np.ndarray, starts: list[float], scale: float) -> np.ndarray:
    """1.0 / scale, switching at each sentence start (no sooner than MIN_PUNCH_GAP after the last switch)."""
    z = np.ones(len(times))
    switches: list[float] = []
    for s in starts:
        if s <= 0.05:
            continue
        if not switches or s - switches[-1] >= MIN_PUNCH_GAP:
            switches.append(s)
    level = 1.0
    k = 0
    for i, t in enumerate(times):
        while k < len(switches) and switches[k] <= t:
            level = scale if level == 1.0 else 1.0
            k += 1
        z[i] = level
    return z


def topic_starts(words, source_gaps: list[float], cuts: list[float]) -> list[float]:
    """Output times where the push-in starts again: a long pause in the source, or a scene cut."""
    marks = sorted(set([0.0] + [t for t in source_gaps] + [t for t in cuts]))
    out: list[float] = []
    for m in marks:
        if not out or m - out[-1] >= MIN_TOPIC:
            out.append(m)
    return out


def slow_path(times: np.ndarray, starts: list[float], scale: float) -> np.ndarray:
    """A smooth push-in from 1.0 to `scale` over SLOW_SECONDS, starting again at each of `starts`."""
    z = np.ones(len(times))
    if not starts:
        starts = [0.0]
    k = 0
    for i, t in enumerate(times):
        while k + 1 < len(starts) and starts[k + 1] <= t:
            k += 1
        p = min(1.0, max(0.0, (t - starts[k]) / SLOW_SECONDS))
        ease = p * p * (3 - 2 * p)  # smoothstep: no lurch at either end
        z[i] = 1 + (scale - 1) * ease
    return z


@dataclass
class Window:
    """The crop, in source pixels, for one output frame."""

    x: float
    y: float
    w: float
    h: float


def base_window(src_w: int, src_h: int, aspect: float | None) -> tuple[float, float]:
    """The widest window of `aspect` (width/height) that fits the source; None = the whole frame."""
    if aspect is None:
        return float(src_w), float(src_h)
    if src_w / src_h > aspect:
        return src_h * aspect, float(src_h)
    return float(src_w), src_w / aspect


def window(cx: float, cy: float, zoom: float, src_w: int, src_h: int, aspect: float | None,
           eye_line: float = 0.38) -> Window:
    """The crop window centred across on `cx` (a fraction of the frame) with the eyes `eye_line` of the way
    down it at `cy`, kept inside the frame."""
    bw, bh = base_window(src_w, src_h, aspect)
    z = min(MAX_ZOOM, max(1.0, zoom))
    w, h = bw / z, bh / z
    x = cx * src_w - w / 2
    y = cy * src_h - h * eye_line
    return Window(min(max(0.0, x), src_w - w), min(max(0.0, y), src_h - h), w, h)

"""Movement on the timeline: keyframes worked out as numbers (for the timeline view and the checks) and
written as ffmpeg expressions (for the render), plus the moves the Studio puts on its clips.

A clip's keyframes say where it is at certain moments; between two of them each value travels by the later
keyframe's ease. Before the first and after the last, the value holds.

    value_at(keys, "scale", 1.3)          the number at 1.3 s into the clip
    expr(keys, "scale", "(t-4.2)")        the same, as an ffmpeg expression of time

The moves, each a list of keyframes ready to put on a clip:

* ken_burns(name, seconds): the Video Creator's camera moves over a still (creator/motion.py: slow push-ins,
  pull-outs and pans with a hint of roll), as keyframes. The 2.5D feel is the roll and the eased pan together.
* punch_in(at, seconds): a hard cut-in on a word, the "camera punch" of talking videos, easing back out.
* zoom(seconds, a, b): a steady zoom between two sizes.
* shake(at, length): a short camera shake, dying away, the same every time for the same seed.
* whip_in(seconds, direction): the clip arrives from one side fast and settles (an overlay's entrance; between
  two clips on the picture track a whip is a transition, see TRANSITION_XFADE).

combine(a, b) lays one move over another (a shake on top of a push-in).
"""

from __future__ import annotations

import hashlib
import math

from ..creator import motion
from ..studio.timeline_schema import Keyframe

PROPS = ("x", "y", "scale", "rotation", "opacity")
DEFAULTS = {"x": 0.5, "y": 0.5, "scale": 1.0, "rotation": 0.0, "opacity": 1.0}

# A timeline transition as ffmpeg's xfade names it.
TRANSITION_XFADE = {"crossfade": "fade", "dissolve": "dissolve", "dip_black": "fadeblack", "dip_white": "fadewhite",
                    "whip_left": "slideleft", "whip_right": "slideright", "whip_up": "slideup",
                    "whip_down": "slidedown", "zoom_in": "zoomin", "circle_open": "circleopen",
                    "smooth_up": "smoothup"}
# The Creator's xfade names (Settings → Creator → Scene changes, "mixed") as timeline transitions.
XFADE_TRANSITION = {v: k for k, v in TRANSITION_XFADE.items()} | {"fade": "crossfade", "slideleft": "whip_left"}


# ---------------------------------------------------------------- numbers

def ease(kind: str, p: float) -> float:
    p = max(0.0, min(1.0, p))
    if kind == "ease_in":
        return p * p
    if kind == "ease_out":
        return p * (2 - p)
    if kind == "ease_in_out":
        return 0.5 - 0.5 * math.cos(math.pi * p)
    if kind == "hold":
        return 1.0 if p >= 1.0 else 0.0
    return p


def value_at(keys: list[Keyframe], prop: str, t: float) -> float:
    """`prop` at `t` seconds into the clip."""
    if not keys:
        return DEFAULTS[prop]
    if t <= keys[0].t:
        return getattr(keys[0], prop)
    for a, b in zip(keys, keys[1:]):
        if t < b.t:
            va, vb = getattr(a, prop), getattr(b, prop)
            return va + (vb - va) * ease(b.ease, (t - a.t) / (b.t - a.t))
    return getattr(keys[-1], prop)


def constant(keys: list[Keyframe], prop: str) -> float | None:
    """The value when it never changes, else None."""
    if not keys:
        return DEFAULTS[prop]
    first = getattr(keys[0], prop)
    return first if all(abs(getattr(k, prop) - first) < 1e-9 for k in keys) else None


def moving(keys: list[Keyframe]) -> bool:
    return any(constant(keys, p) is None for p in PROPS)


def extremes(keys: list[Keyframe], prop: str) -> tuple[float, float]:
    """The least and most `prop` gets (every ease stays between its two keyframes, so the keyframes say)."""
    values = [getattr(k, prop) for k in keys] or [DEFAULTS[prop]]
    return min(values), max(values)


def samples(keys: list[Keyframe], seconds: float, step: float = 1 / 15) -> list[float]:
    """Times worth checking a move at: every keyframe, and steps between."""
    times = {0.0, max(0.0, seconds)} | {k.t for k in keys if 0 <= k.t <= seconds}
    n = int(seconds / step)
    times |= {i * step for i in range(1, n + 1)}
    return sorted(times)


# ---------------------------------------------------------------- ffmpeg expressions

def _num(v: float) -> str:
    text = f"{v:.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def expr(keys: list[Keyframe], prop: str, time: str = "t") -> str:
    """`prop` as an ffmpeg expression of `time` (an expression giving seconds into the clip). A value that
    never changes comes back as a plain number."""
    fixed = constant(keys, prop)
    if fixed is not None:
        return _num(fixed)
    T = f"({time})"

    def segment(a: Keyframe, b: Keyframe) -> str:
        va, vb = getattr(a, prop), getattr(b, prop)
        if abs(vb - va) < 1e-9 or b.ease == "hold":
            return _num(va)
        p = f"(({T}-{_num(a.t)})/{_num(b.t - a.t)})"
        if b.ease == "ease_in":
            p = f"({p}*{p})"
        elif b.ease == "ease_out":
            p = f"({p}*(2-{p}))"
        elif b.ease == "ease_in_out":
            p = f"(0.5-0.5*cos(PI*{p}))"
        return f"({_num(va)}+({_num(vb - va)})*{p})"

    out = _num(getattr(keys[-1], prop))
    for a, b in reversed(list(zip(keys, keys[1:]))):
        out = f"if(lt({T},{_num(b.t)}),{segment(a, b)},{out})"
    first = keys[0]
    return f"if(lt({T},{_num(first.t)}),{_num(getattr(first, prop))},{out})"


# ---------------------------------------------------------------- staying on the picture

def rotation_cover(degrees: float, width: float, height: float) -> float:
    """How much bigger a width x height picture must be so that, turned `degrees`, it still covers the frame."""
    a = math.radians(abs(degrees) % 180)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    return max((width * c + height * s) / width, (width * s + height * c) / height)


def reach(scale: float, overflow: float = 1.0) -> float:
    """How far a clip's centre may sit from the canvas's (in canvas widths or heights) before its edge shows:
    a clip `scale` times its fit, whose fit already runs `overflow` times the canvas on that axis."""
    return max(0.0, (scale * overflow - 1.0) / 2)


def covers(keys: list[Keyframe], seconds: float, overflow: tuple[float, float] = (1.0, 1.0),
           size: tuple[float, float] = (9, 16), slack: float = 0.002) -> bool:
    """True when a cover-fitted clip fills a `size` canvas the whole way through its move: big enough for its
    turn, and never slid so far that an edge shows."""
    for t in samples(keys, seconds):
        s = value_at(keys, "scale", t) / rotation_cover(value_at(keys, "rotation", t), *size)
        if s < 1.0 - 5e-4 or value_at(keys, "opacity", t) < 1.0 - 1e-6:
            return False
        if abs(value_at(keys, "x", t) - 0.5) > reach(s, overflow[0]) + slack:
            return False
        if abs(value_at(keys, "y", t) - 0.5) > reach(s, overflow[1]) + slack:
            return False
    return True


# ---------------------------------------------------------------- the moves

def ken_burns(name: str, seconds: float, strength: float = 1.0, overflow: tuple[float, float] = (1.0, 1.0),
              size: tuple[float, float] = (9, 16)) -> list[Keyframe]:
    """One of the Video Creator's camera moves (creator/motion.MOVES) as two keyframes. The camera's view of
    the picture becomes the picture's place on the canvas: looking left moves the picture right. The move is
    kept on the picture (its pan shortened when the zoom leaves less room than it asks for)."""
    move = motion.get(name, strength)
    out = []
    for p, t in ((0.0, 0.0), (1.0, max(0.05, seconds))):
        zoom, cx, cy, roll = motion.at(move, p)
        need = rotation_cover(roll, *size)
        rx, ry = reach(zoom, overflow[0]), reach(zoom, overflow[1])
        zoom *= need  # a rolled picture is that much bigger, so its corners stay off the canvas
        x = 0.5 + max(-rx, min(rx, (0.5 - cx) * zoom * overflow[0]))
        y = 0.5 + max(-ry, min(ry, (0.5 - cy) * zoom * overflow[1]))
        out.append(Keyframe(round(t, 3), round(x, 5), round(y, 5), round(zoom, 5), round(roll, 3), 1.0,
                            "ease_in_out" if move.ease == "sine" and p else "linear"))
    return out


def zoom(seconds: float, start: float = 1.0, end: float = 1.15, ease_kind: str = "linear") -> list[Keyframe]:
    return [Keyframe(0.0, scale=start), Keyframe(round(max(0.05, seconds), 3), scale=end, ease=ease_kind)]


def punch_in(at: float, seconds: float, amount: float = 1.12, snap: float = 0.08, hold: float = 0.0,
             base: float = 1.0) -> list[Keyframe]:
    """A cut-in at `at`: the picture jumps to `amount` within `snap` seconds, holds, then eases back by the
    clip's end (`hold` 0 = holds for half of what's left)."""
    at = max(0.0, min(at, max(0.0, seconds - snap * 2)))
    left = max(0.0, seconds - at - snap)
    hold = hold or left * 0.5
    keys = [Keyframe(0.0, scale=base)]
    if at > 0.001:
        keys.append(Keyframe(round(at, 3), scale=base, ease="hold"))
    keys.append(Keyframe(round(at + snap, 3), scale=base * amount, ease="ease_out"))
    back = min(seconds, at + snap + hold)
    if back > at + snap + 0.01:
        keys.append(Keyframe(round(back, 3), scale=base * amount, ease="hold"))
    if seconds > back + 0.05:
        keys.append(Keyframe(round(seconds, 3), scale=base, ease="ease_in_out"))
    return keys


def shake(at: float, length: float = 0.45, amount: float = 0.012, seed: str = "", fps: int = 30,
          scale: float = 1.06) -> list[Keyframe]:
    """A camera shake from `at` for `length` seconds: the picture jolts up to `amount` of the canvas each way,
    less each time, zoomed in `scale` so its edges never show. The same seed always shakes the same way."""
    step = 2.0 / fps  # a new position every other frame
    n = max(2, int(length / step))
    digest = hashlib.sha256(f"shake:{seed}:{at:.3f}".encode()).digest()
    room = reach(scale)
    amount = min(amount, room * 0.95) if room else 0.0
    keys = [Keyframe(0.0)] if at > 0.001 else []
    keys.append(Keyframe(round(at, 3), scale=1.0, ease="hold"))
    for i in range(1, n):
        fade = 1.0 - i / n
        dx = (digest[(2 * i) % 32] / 127.5 - 1.0) * amount * fade
        dy = (digest[(2 * i + 1) % 32] / 127.5 - 1.0) * amount * fade
        roll = (digest[(i + 7) % 32] / 127.5 - 1.0) * 0.6 * fade
        keys.append(Keyframe(round(at + i * step, 3), round(0.5 + dx, 5), round(0.5 + dy, 5), scale, round(roll, 3)))
    keys.append(Keyframe(round(at + n * step, 3), 0.5, 0.5, 1.0, 0.0, ease="ease_out"))
    return keys


def whip_in(seconds: float = 0.28, direction: str = "left", x: float = 0.5, y: float = 0.5,
            scale: float = 1.0) -> list[Keyframe]:
    """An overlay's entrance: it flies in from off the canvas on one side and settles at (x, y)."""
    dx, dy = {"left": (1.2, 0.0), "right": (-1.2, 0.0), "up": (0.0, 1.2), "down": (0.0, -1.2)}.get(direction, (1.2, 0.0))
    return [Keyframe(0.0, x + dx, y + dy, scale, opacity=1.0),
            Keyframe(round(seconds, 3), x, y, scale, ease="ease_out")]


def fade(seconds: float, fade_in: float = 0.3, fade_out: float = 0.3) -> list[Keyframe]:
    """Opacity from nothing, held, back to nothing."""
    keys = [Keyframe(0.0, opacity=0.0 if fade_in > 0 else 1.0)]
    if fade_in > 0:
        keys.append(Keyframe(round(min(fade_in, seconds / 2), 3), opacity=1.0))
    if fade_out > 0 and seconds - fade_out > keys[-1].t:
        keys.append(Keyframe(round(seconds - fade_out, 3), opacity=1.0, ease="hold"))
        keys.append(Keyframe(round(seconds, 3), opacity=0.0))
    return keys


def combine(base: list[Keyframe], extra: list[Keyframe]) -> list[Keyframe]:
    """`extra` laid over `base`: its offsets from rest (centre, scale 1, no turn, opaque) are added to the
    base move's values (scales and opacities multiply). The result has a keyframe wherever either had one."""
    if not base:
        return list(extra)
    if not extra:
        return list(base)
    times = sorted({round(k.t, 3) for k in base} | {round(k.t, 3) for k in extra})
    eases = {round(k.t, 3): k.ease for k in base} | {round(k.t, 3): k.ease for k in extra}
    out = []
    for t in times:
        out.append(Keyframe(
            t,
            round(value_at(base, "x", t) + value_at(extra, "x", t) - 0.5, 5),
            round(value_at(base, "y", t) + value_at(extra, "y", t) - 0.5, 5),
            round(value_at(base, "scale", t) * value_at(extra, "scale", t), 5),
            round(value_at(base, "rotation", t) + value_at(extra, "rotation", t), 3),
            round(value_at(base, "opacity", t) * value_at(extra, "opacity", t), 4),
            eases.get(t, "linear") if eases.get(t) != "hold" else "linear"))
    return out


def shift(keys: list[Keyframe], seconds: float) -> list[Keyframe]:
    """The same move, `seconds` later."""
    return [Keyframe(round(k.t + seconds, 3), k.x, k.y, k.scale, k.rotation, k.opacity, k.ease) for k in keys]

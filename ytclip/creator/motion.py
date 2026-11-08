"""Camera moves over still pictures: the 2.5D "Ken Burns" push-ins, pull-outs and pans, framed for 9:16.

A move is where the camera looks over the scene's length: its zoom (1 = the picture just covers the
1080x1920 frame) and the point of the picture at the centre of the frame (0-1 across and down), with a
slight roll for depth. From the spec:

    slow_push_in   z(t) = 1.0 + 0.15 * t/T
    slow_pull_out  z(t) = 1.15 - 0.15 * t/T
    pans           the centre moves in a straight line across the picture at a steady zoom

Zooms are linear, as written; pans ease in and out (a sine curve), which reads as a camera operator
rather than a slide. `strength` (Settings → Creator → Camera movement) scales every move.

Two renderers use this: compositor.render_still draws each frame with OpenCV's sub-pixel warp (smooth,
no stair-stepping), and zoompan() writes the same move as an ffmpeg zoompan filter for when OpenCV can't
be used. The picture is first scaled to cover the frame (cover_size), so a square or landscape picture is
cropped, never squashed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Move:
    zoom0: float
    zoom1: float
    x0: float = 0.5  # centre of view, share of the picture's width
    y0: float = 0.5
    x1: float = 0.5
    y1: float = 0.5
    roll: float = 0.0  # degrees of roll over the move (a hint of depth)
    ease: str = "linear"  # linear | sine


PUSH = 0.15
PAN_ZOOM = 1.14  # pans happen zoomed in a little, so there's picture to move across
MOVES: dict[str, Move] = {
    "slow_push_in": Move(1.0, 1.0 + PUSH, roll=0.6),
    "slow_pull_out": Move(1.0 + PUSH, 1.0, roll=-0.6),
    "pan_left": Move(PAN_ZOOM, PAN_ZOOM, 0.62, 0.5, 0.38, 0.5, ease="sine"),
    "pan_right": Move(PAN_ZOOM, PAN_ZOOM, 0.38, 0.5, 0.62, 0.5, ease="sine"),
    "pan_up": Move(PAN_ZOOM, PAN_ZOOM, 0.5, 0.6, 0.5, 0.4, ease="sine"),
    "pan_down": Move(PAN_ZOOM, PAN_ZOOM, 0.5, 0.4, 0.5, 0.6, ease="sine"),
    "pan_down_right": Move(PAN_ZOOM + 0.02, PAN_ZOOM + 0.02, 0.4, 0.42, 0.6, 0.58, ease="sine"),
    "pan_up_left": Move(PAN_ZOOM + 0.02, PAN_ZOOM + 0.02, 0.6, 0.58, 0.4, 0.42, ease="sine"),
    "push_in_left": Move(1.0, 1.0 + PUSH + 0.02, 0.5, 0.5, 0.42, 0.48, roll=0.5),
    "push_in_right": Move(1.0, 1.0 + PUSH + 0.02, 0.5, 0.5, 0.58, 0.48, roll=-0.5),
    "static": Move(1.04, 1.04),
}
DEFAULT = "slow_push_in"


def get(name: str, strength: float = 1.0) -> Move:
    """The move called `name`, scaled by `strength` (0 = still, 1 = as above, up to 2)."""
    m = MOVES.get(name, MOVES[DEFAULT])
    k = max(0.0, min(2.0, float(strength)))
    if k == 1.0:
        return m

    def mix(a: float, b: float, centre: float) -> tuple[float, float]:
        return centre + (a - centre) * k, centre + (b - centre) * k

    base = min(m.zoom0, m.zoom1)
    z0 = 1.0 + (m.zoom0 - 1.0) * k if m.zoom0 != m.zoom1 else 1.0 + (base - 1.0) * max(k, 0.3)
    z1 = 1.0 + (m.zoom1 - 1.0) * k if m.zoom0 != m.zoom1 else z0
    x0, x1 = mix(m.x0, m.x1, 0.5)
    y0, y1 = mix(m.y0, m.y1, 0.5)
    return Move(z0, z1, x0, y0, x1, y1, m.roll * k, m.ease)


def ease(kind: str, p: float) -> float:
    p = max(0.0, min(1.0, p))
    if kind == "sine":
        return 0.5 - 0.5 * math.cos(math.pi * p)
    return p


def at(move: Move, p: float) -> tuple[float, float, float, float]:
    """(zoom, centre x, centre y, roll degrees) a share `p` (0-1) of the way through the move."""
    zp = p if move.zoom0 != move.zoom1 else 0.0
    cp = ease(move.ease, p)
    zoom = move.zoom0 + (move.zoom1 - move.zoom0) * zp
    return (zoom, move.x0 + (move.x1 - move.x0) * cp, move.y0 + (move.y1 - move.y0) * cp,
            move.roll * (p - 0.5))


def cover_size(src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[float, float]:
    """The picture's size once scaled to just cover out_w x out_h, keeping its shape."""
    k = max(out_w / max(1, src_w), out_h / max(1, src_h))
    return src_w * k, src_h * k


def viewport(move: Move, p: float, src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[float, float, float, float]:
    """The part of the picture in view (x, y, width, height in its pixels), kept inside it."""
    zoom, cx, cy, _ = at(move, p)
    cw, ch = cover_size(src_w, src_h, out_w, out_h)
    k = src_w / cw  # picture pixels per covered pixel
    w, h = out_w * k / zoom, out_h * k / zoom
    x = min(max(cx * src_w - w / 2, 0.0), max(0.0, src_w - w))
    y = min(max(cy * src_h - h / 2, 0.0), max(0.0, src_h - h))
    return x, y, w, h


def affine(move: Move, p: float, src_w: int, src_h: int, out_w: int, out_h: int) -> list[list[float]]:
    """The 2x3 matrix taking the picture to the frame at `p` (for cv2.warpAffine), roll included. A rolled
    frame needs a touch more zoom so its corners stay on the picture."""
    x, y, w, h = viewport(move, p, src_w, src_h, out_w, out_h)
    roll = math.radians(at(move, p)[3])
    s = out_w / w
    if roll:
        # Zoom in just enough that the rotated frame's corners stay inside the view.
        c, si = abs(math.cos(roll)), abs(math.sin(roll))
        need = max((out_w * c + out_h * si) / out_w, (out_w * si + out_h * c) / out_h)
        s *= need
    cx, cy = x + w / 2, y + h / 2
    cos, sin = math.cos(roll) * s, math.sin(roll) * s
    # frame = R*S*(src - centre) + frame centre
    return [[cos, -sin, out_w / 2 - (cos * cx - sin * cy)],
            [sin, cos, out_h / 2 - (sin * cx + cos * cy)]]


def zoompan(move: Move, frames: int, fps: float, out_w: int, out_h: int, upscale: int = 3) -> str:
    """The move as ffmpeg filters for a single picture input: scale to cover (times `upscale`, which keeps
    zoompan's whole-pixel steps small), then zoompan for `frames` frames. Roll is left out."""
    n = max(1, frames - 1)
    big_w, big_h = out_w * upscale, out_h * upscale
    p = f"(on/{n})"
    if move.ease == "sine":
        q = f"(0.5-0.5*cos(PI*{p}))"
    else:
        q = p
    zp = p if move.zoom0 != move.zoom1 else "0"
    z = f"{move.zoom0:.5f}+({move.zoom1 - move.zoom0:.5f})*{zp}"
    cx = f"({move.x0:.5f}+({move.x1 - move.x0:.5f})*{q})"
    cy = f"({move.y0:.5f}+({move.y1 - move.y0:.5f})*{q})"
    x = f"max(0,min(iw-iw/zoom,{cx}*iw-iw/zoom/2))"
    y = f"max(0,min(ih-ih/zoom,{cy}*ih-ih/zoom/2))"
    return (f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase:flags=lanczos,crop={big_w}:{big_h},"
            f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={out_w}x{out_h}:fps={fps:g}")


# ---------------------------------------------------------------- scene timing with transitions

def clip_spans(bounds: list[float], total: float, overlap: float) -> list[tuple[float, float]]:
    """Each scene's stretch of the timeline, widened by half a transition at each inner join so
    neighbouring scenes overlap for the cross-fade. `bounds` are the joins between scenes (n - 1 of them)."""
    edges = [0.0, *bounds, total]
    half = overlap / 2
    out = []
    for i in range(len(edges) - 1):
        a = edges[i] - (half if i > 0 else 0.0)
        b = edges[i + 1] + (half if i < len(edges) - 2 else 0.0)
        out.append((max(0.0, a), min(total, b)))
    return out


def scene_bounds(starts: list[float]) -> list[float]:
    """Joins between scenes from when each scene's voice starts: the cut lands just before the words, so a
    new picture arrives with them (a quarter of a second ahead)."""
    return [max(0.05, t - 0.25) for t in starts[1:]]

"""Where the play is: a 9:16 window over 16:9 broadcast footage that follows the action, not faces.

Face tracking (vision/face_tracker.py) is no use on a wide shot of a pitch: the players are tiny and the
broadcast camera is already panning with the ball. So the window follows *movement*:

1. The play's frames are read small (256 px wide, 10 a second).
2. The broadcast camera's own pan between two frames is measured (phase correlation) and taken out, so the
   stands sliding past don't count as action.
3. What still moves (players running, the ball, a goalmouth scramble) is summed down each column of the
   picture, the score bug's and the ad boards' rows left out, and its centre of mass across is where the play
   is. A frame with too little movement to go by (a pause, a repeated frame) holds the window where it was;
   one where the whole picture changed (a cut, a flash) does too.
4. The window eases after that point like a camera operator, as vision/camera.py's does for speakers:

       x̂(t) = α·x(t) + (1 − α)·x̂(t−1)

   holding still while the action moves less than 5% of the picture across (no jitter), and speeding up
   (a larger α) when it runs away fast (a breakaway, a counter-attack), so the ball doesn't leave the frame.
   As the whole stretch is known before it's framed, the window looks a third of a second ahead, so it moves
   with the play instead of after it.

This finds where the action is, not the ball itself: a ball detector (a YOLO model) would need a model file
the app doesn't carry. On a broadcast, where the camera follows the ball, the two are nearly always the same.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import numpy as np

from ..ffmpeg import NO_WINDOW, ffmpeg_exe, tracked

SAMPLE_W, SAMPLE_H = 256, 144
SAMPLE_FPS = 10.0
TOP_IGNORE, BOTTOM_IGNORE = 0.14, 0.12  # the score bug, the ticker and the ad boards
DEADBAND = 0.05  # of the picture's width: the window holds still for less
ALPHA = 0.07  # per frame at 30 fps, as the camera eases toward the action
FAST_ALPHA = 0.22  # when the action has run more than FAST of the picture away from the window
FAST = 0.55  # of half the window's width: the action this far off the window's centre speeds it up
LEAD = 0.35  # seconds the window looks ahead: the footage is all known, so it moves as the play does, not after
QUIET = 0.15  # below this share of a frame's usual movement, the window holds where it was


@dataclass
class ActionPath:
    """The window's centre across the picture (0-1) at each of `times` (seconds from the stretch's start)."""

    times: np.ndarray
    xs: np.ndarray
    found: float  # share of frames where movement placed the window (the rest held or went to the middle)

    def at(self, t: float) -> float:
        if not len(self.times):
            return 0.5
        return float(np.interp(t, self.times, self.xs))


def read_frames(src, start: float, length: float) -> np.ndarray:
    """The stretch's frames, small and grey: (n, SAMPLE_H, SAMPLE_W) uint8."""
    cmd = [ffmpeg_exe(), "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", str(src), "-t", f"{length:.3f}",
           "-an", "-sn", "-vf", f"fps={SAMPLE_FPS},scale={SAMPLE_W}:{SAMPLE_H}:flags=area,format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    with tracked(proc):
        data, _ = proc.communicate()
    n = len(data) // (SAMPLE_W * SAMPLE_H)
    return np.frombuffer(data[: n * SAMPLE_W * SAMPLE_H], np.uint8).reshape(n, SAMPLE_H, SAMPLE_W)


def action_points(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For each frame (after the first), where the movement is across (0-1) and how much there is."""
    import cv2

    n = len(frames)
    xs = np.full(n, 0.5)
    energy = np.zeros(n)
    if n < 2:
        return xs, energy
    top, bottom = int(SAMPLE_H * TOP_IGNORE), int(SAMPLE_H * (1 - BOTTOM_IGNORE))
    window = cv2.createHanningWindow((SAMPLE_W, SAMPLE_H), cv2.CV_32F)
    cols = np.arange(SAMPLE_W, dtype=float) + 0.5
    prev = frames[0].astype(np.float32)
    for i in range(1, n):
        cur = frames[i].astype(np.float32)
        # (phaseCorrelate windows its inputs in place: it gets copies)
        (dx, dy), _resp = cv2.phaseCorrelate(prev.copy(), cur.copy(), window)
        if abs(dx) > SAMPLE_W * 0.25 or abs(dy) > SAMPLE_H * 0.25:
            dx = dy = 0.0  # a cut, not a pan: the difference below is everything; it's discounted by its size
        m = np.float32([[1, 0, dx], [0, 1, dy]])
        moved = cv2.warpAffine(prev, m, (SAMPLE_W, SAMPLE_H), borderMode=cv2.BORDER_REPLICATE)
        diff = np.abs(cur - moved)[top:bottom]
        still = np.abs(cur - prev)[top:bottom]
        if float(still.mean()) <= float(diff.mean()):
            # The camera didn't really move (a repeating pattern, the pitch's stripes, can fool the
            # correlation): the plain difference is the truer one.
            diff, dx = still, 0.0
        diff = cv2.GaussianBlur(diff, (5, 5), 0)
        diff[diff < 12] = 0  # sensor noise and compression shimmer
        # Edges where the shift brought in repeated pixels aren't movement.
        pad = int(min(SAMPLE_W * 0.2, abs(dx) + 2))
        if dx > 0:
            diff[:, :pad] = 0
        elif dx < 0:
            diff[:, SAMPLE_W - pad:] = 0
        col = diff.sum(axis=0) ** 1.5  # clusters of movement count for more than a scatter
        total = float(col.sum())
        if total > 0:
            xs[i] = float((col * cols).sum() / total) / SAMPLE_W
        energy[i] = float(diff.mean())
        prev = cur
    xs[0], energy[0] = xs[1], energy[1]
    return xs, energy


def targets(xs: np.ndarray, energy: np.ndarray) -> tuple[np.ndarray, float]:
    """The point to follow per frame: the action where there's enough of it; the last point where there isn't,
    or where the whole picture changed (a cut, a flash)."""
    if not len(xs):
        return xs, 0.0
    usual = float(np.median(energy[energy > 0])) if np.any(energy > 0) else 0.0
    got: list[float | None] = []
    found = 0
    for x, e in zip(xs, energy):
        if usual <= 0 or e <= 0 or e > usual * 6:
            # No change (a repeated frame), or the whole picture changed (a cut, a flash): no reading.
            got.append(None)
            continue
        if e < usual * QUIET:  # barely anything moved: too little to place the window by; it holds
            got.append(None)
            continue
        got.append(float(x))
        found += 1
    # Frames without a reading take the one before them (the first ones, the first reading).
    first = next((g for g in got if g is not None), 0.5)
    out = np.empty(len(xs))
    last = first
    for i, g in enumerate(got):
        last = g if g is not None else last
        out[i] = last
    # A short median takes out single-frame spikes before the camera eases after it.
    if len(out) >= 5:
        k = 2
        padded = np.pad(out, k, mode="edge")
        out = np.median(np.lib.stride_tricks.sliding_window_view(padded, 2 * k + 1), axis=-1)
    return out, found / len(xs)


def ease(points: np.ndarray, fps: float, half: float) -> np.ndarray:
    """The window's centre after the points: EMA with a deadband, quicker when the action runs away. `half` is
    half the window's width as a share of the picture (the centre can't go nearer the edge than that)."""
    a_slow = 1 - (1 - ALPHA) ** (30.0 / fps)
    a_fast = 1 - (1 - FAST_ALPHA) ** (30.0 / fps)
    lo, hi = half, 1 - half
    out = np.empty(len(points))
    # It starts where the action is over the first half second (a cut into the play is a cut, not a pan).
    cam = min(hi, max(lo, float(np.median(points[: max(1, int(fps / 2))])))) if len(points) else 0.5
    moving = False
    for i, p in enumerate(points):
        p = min(hi, max(lo, float(p)))
        err = p - cam
        if not moving and abs(err) > DEADBAND:
            moving = True
        if moving:
            cam += (a_fast if abs(err) > FAST * half else a_slow) * err
            if abs(p - cam) < DEADBAND / 4:
                moving = False
        out[i] = cam
    return out


def follow(src, start: float, length: float, src_w: int, src_h: int) -> ActionPath:
    """The window's path over source seconds [start, start + length)."""
    frames = read_frames(src, start, length)
    if len(frames) < 2:
        return ActionPath(np.array([0.0]), np.array([0.5]), 0.0)
    xs, energy = action_points(frames)
    pts, found = targets(xs, energy)
    half = (src_h * 9 / 16) / max(1, src_w) / 2
    if half >= 0.5:  # already as narrow as 9:16: nothing to follow
        return ActionPath(np.array([0.0]), np.array([0.5]), 0.0)
    lead = int(round(LEAD * SAMPLE_FPS))
    ahead = np.concatenate([pts[lead:], np.repeat(pts[-1:], lead)]) if 0 < lead < len(pts) else pts
    eased = ease(ahead, SAMPLE_FPS, half)
    times = np.arange(len(eased)) / SAMPLE_FPS
    return ActionPath(times, eased, found)

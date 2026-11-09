"""The TikTok cover: the picture a post shows on the profile grid and in search, instead of TikTok's
default (the first frame, which for a clip with a hook card is a dark frame with half a headline).

Which moment of the clip (Settings → Posting → TikTok cover):

* **the hook card** ("auto"): when the clip opens with ClipMonster's headline card (its edit, or a Video
  Creator video's hook in big letters), the moment the whole headline is up. clips.json says when
  ("hook_card"); that's a designed thumbnail: the headline over the picture;
* **the best face** (else, or always with "face"): frames from the clip's first minute, a couple a
  second, scored on a face (big, near the middle, a confident detection), how sharp the picture is, its
  exposure and its contrast. Without a face, the clearest frame wins. The hook card's seconds are
  skipped: a dimmed frame behind half a headline is never the face cover;
* **off**: TikTok's own pick.

The frame is saved full size (covers/<item id>.jpg in posting's cache) and put in TikTok Studio's cover
editor (Edit cover → Upload cover) just before Post. If the editor isn't there or doesn't take it, the post
goes up with TikTok's own pick: a cover never stops a post.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..ffmpeg import NO_WINDOW, ffmpeg_exe

MODES = ("auto", "face", "off")
SAMPLE_FPS = 2.0
SAMPLE_WIDTH = 360
MAX_SECONDS = 60.0  # from the clip's first minute: the cover should show what it opens with
SKIP_START = 0.3  # the first frames are often mid-fade
SKIP_END = 0.8
FACE_MIN = 0.06  # a face smaller than this (share of the frame's height) is a crowd, not a cover
FACE_WIN = 0.25  # a face part this strong makes it "a face"


@dataclass
class Pick:
    at: float  # seconds into the clip
    why: str  # "the hook card" | "a face" | "the clearest frame"
    score: float = 0.0


def covers_dir() -> Path:
    from .config import cache_dir

    path = cache_dir() / "covers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def hook_card(record: dict | None) -> tuple[float, float] | None:
    """(fully up at, shows until) from a clip's clips.json record, None when it has no hook card."""
    for d in (record or {}, (record or {}).get("edit") or {}):
        card = d.get("hook_card") if isinstance(d, dict) else None
        if isinstance(card, dict) and isinstance(card.get("at"), (int, float)) and 0 < card["at"] < 10:
            until = card.get("until")
            return float(card["at"]), float(until) if isinstance(until, (int, float)) else float(card["at"]) + 1.0
    return None


def frame_score(frame, faces) -> tuple[float, float]:
    """(score, its face part) for one small BGR frame and the faces found in it."""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    sharp = min(1.0, math.log10(float(cv2.Laplacian(gray, cv2.CV_64F).var()) + 1.0) / 3.0)
    g = gray.astype(np.float32) / 255.0
    exposure = max(0.0, 1.0 - abs(float(g.mean()) - 0.48) / 0.38)
    contrast = min(1.0, float(g.std()) / 0.22)
    face = 0.0
    for f in faces:
        if f.h < FACE_MIN:
            continue
        size = min(1.0, f.h / 0.22)
        centred = 1.0 - min(1.0, abs(f.cx - 0.5) * 2)
        placed = 1.0 if 0.12 <= f.y + f.h / 2 <= 0.72 else 0.6  # a face at the very top or bottom gets cut off
        face = max(face, min(1.0, f.score) * size * (0.6 + 0.4 * centred) * placed)
    return 0.45 * face + 0.25 * sharp + 0.15 * exposure + 0.15 * contrast, face


def best_frame(path: Path, duration: float, size: tuple[int, int] = (1080, 1920), *, skip_until: float = 0.0,
               detector=None, frames=None) -> Pick | None:
    """The clip's strongest frame for a cover. `frames` ((t, BGR frame), from the window's start) and `detector`
    stand in for ffmpeg and OpenCV's face detector in tests; detector False = no face finding."""
    from ..vision import face_tracker

    start = max(SKIP_START, skip_until)
    end = min(duration - SKIP_END, MAX_SECONDS)
    if end - start < 0.5:  # a very short clip, or all hook card: anywhere in it
        start, end = 0.0, max(0.1, duration)
    if detector is None:
        try:
            detector = face_tracker.detector()
        except Exception:  # noqa: BLE001 - no OpenCV model: the clearest frame, still
            detector = False
    if frames is None:
        frames = face_tracker.sample_frames(path, start, end - start, SAMPLE_FPS, SAMPLE_WIDTH, size)
    best: Pick | None = None
    for t, frame in frames:
        try:
            faces = detector.detect(frame) if detector else []
        except Exception:  # noqa: BLE001 - one bad frame
            faces = []
        score, face = frame_score(frame, faces)
        if best is None or score > best.score:
            best = Pick(round(start + t, 2), "a face" if face >= FACE_WIN else "the clearest frame", round(score, 3))
    return best


def choose(item, mode: str = "auto", record: dict | None = None, **kw) -> Pick | None:
    """The moment of `item`'s clip for its cover (None with "off")."""
    if mode not in MODES or mode == "off":
        return None
    if record is None:
        from .jobs import clip_context

        record = clip_context(item)
    duration = item.duration
    width, height = int(item.info.get("width") or 0), int(item.info.get("height") or 0)
    if not duration or not width:
        from .media import probe

        info = probe(Path(item.path))
        duration, width, height = info.duration, info.width, info.height
    card = hook_card(record)
    if mode == "auto" and card and card[0] < duration:
        return Pick(card[0], "the hook card", 1.0)
    pick = best_frame(Path(item.path), duration, (width or 1080, height or 1920),
                      skip_until=card[1] if card else 0.0, **kw)
    if pick is None:
        raise RuntimeError("no frames could be read from the clip")
    return pick


def extract(src: Path, at: float, dest: Path) -> Path:
    """The frame at `at`, full size, as a JPEG TikTok's cover editor takes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".part.jpg")
    out = subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", str(src),
                          "-frames:v", "1", "-q:v", "2", str(tmp)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NO_WINDOW, timeout=120)
    if out.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1000:
        tmp.unlink(missing_ok=True)
        why = (out.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"ffmpeg couldn't take the frame at {at:.1f} s" + (f": {why[-1]}" if why else ""))
    tmp.replace(dest)
    return dest


def make(item, mode: str = "auto", dest_dir: Path | None = None, **kw) -> tuple[Pick, Path] | None:
    """Choose the cover and save it (covers/<id>.jpg): (pick, picture), or None with "off"."""
    pick = choose(item, mode, **kw)
    if pick is None:
        return None
    return pick, extract(Path(item.path), pick.at, (dest_dir or covers_dir()) / f"{item.id}.jpg")


def describe(item) -> str:
    """'the hook card (1.2 s)' for the record and the Queue tab; "" when there's no cover."""
    if not item.cover or item.cover_at is None:
        return ""
    return f"{item.cover_why} ({item.cover_at:.1f} s)"

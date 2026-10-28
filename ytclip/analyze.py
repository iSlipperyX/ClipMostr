"""Decide which parts of a video are interesting.

Analysis is split in two:

* **measure** (slow, cached): loudness of every half second, scene cuts, captions.
* **score** (instant): turn measurements into per-second signals, blend them with
  the user's weights, and pick clips. The GUI re-runs this live as sliders move.

Signals, each normalised to 0-1 per second:

* heatmap   - YouTube "Most replayed": where viewers rewind and rewatch.
* audio     - loud relative to the surroundings (shouting, laughter, drops).
* reactions - [Laughter] / [Applause] tags in captions.
* scenes    - density of hard cuts (fast editing = action).
* keywords  - mentions of user-supplied words or phrases.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .ffmpeg import NO_WINDOW, ffmpeg_exe, tracked
from .subtitles import REACTION, Cue

HOP = 0.5  # seconds per loudness frame
FINE = 0.02  # seconds per fine loudness frame, for cut points (cuts.py)
SAMPLE_RATE = 8000

SIGNAL_LABELS = {
    "heatmap": "Most replayed",
    "audio": "Audio excitement",
    "reactions": "Laughter / applause",
    "scenes": "Scene cuts",
    "keywords": "Keywords",
}


@dataclass
class Clip:
    start: float
    end: float
    score: float  # 0-100
    title: str = ""
    enabled: bool = True
    crop_x: float = 0.5  # horizontal crop centre for vertical/square exports
    origin: str = "auto"  # auto | ai | chapter | manual
    reason: str = ""
    edited: bool = False  # touched by the user, so re-detection keeps it
    top_text: str | None = None  # banner text for this clip; None = the template in settings, "" = no words
    bottom_text: str | None = None
    bottom_ai: str = ""  # the AI polish's bottom line, for when the clip has no part 2 (layout.bottom_banner)
    hook: str = ""  # a short line written by the AI polish (polish.py) for the banner and the upload caption
    rating: int = 0  # the AI polish's 1-10 "does it stand alone?" (0 = not rated); qc.py rejects low ones
    rating_note: str = ""  # what holds it back, when rated low

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class Measurements:
    duration: float
    db: np.ndarray  # loudness (dBFS) per HOP seconds
    heatmap: list[dict] = field(default_factory=list)
    scene_cuts: list[float] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)
    fine: np.ndarray = field(default_factory=lambda: np.zeros(0))  # loudness (dBFS) per FINE seconds

    @property
    def seconds(self) -> int:
        return max(1, int(np.ceil(self.duration)))


# ---------------------------------------------------------------- helpers


def _rolling(x: np.ndarray, win: int, fn) -> np.ndarray:
    win = max(1, min(win, len(x)))
    pad = win // 2
    padded = np.pad(x, (pad, win - 1 - pad), mode="edge")
    return fn(np.lib.stride_tricks.sliding_window_view(padded, win), axis=-1)


def _smooth(x: np.ndarray, win: int) -> np.ndarray:
    return _rolling(x, win, np.mean) if len(x) else x


def _rank01(x: np.ndarray) -> np.ndarray:
    """Percentile-rank normalisation to [0, 1]; robust to outliers."""
    if len(x) < 2:
        return np.zeros_like(x, dtype=float)
    return np.argsort(np.argsort(x, kind="stable"), kind="stable") / (len(x) - 1)


def _minmax(x: np.ndarray) -> np.ndarray | None:
    lo, hi = float(x.min()), float(x.max())
    return (x - lo) / (hi - lo) if hi > lo else None


# ---------------------------------------------------------------- measurement


def loudness(path: Path, duration: float = 0,
             progress: Callable[[float], None] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """RMS loudness in dBFS for every HOP-second frame, and for every FINE-second frame (the short
    pauses between sentences and words, see cuts.py), in one pass through ffmpeg."""
    cmd = [ffmpeg_exe(), "-v", "error", "-i", str(path), "-vn", "-ac", "1",
           "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    frame_bytes = int(SAMPLE_RATE * HOP) * 2
    per = int(round(HOP / FINE))
    chunks: list[np.ndarray] = []
    fine: list[np.ndarray] = []
    leftover = b""
    done = 0
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          creationflags=NO_WINDOW) as proc, tracked(proc):
        assert proc.stdout is not None
        while data := proc.stdout.read(frame_bytes * 240):
            buf = leftover + data
            usable = len(buf) // frame_bytes * frame_bytes
            frames = np.frombuffer(buf[:usable], dtype=np.int16).astype(np.float32).reshape(-1, frame_bytes // 2)
            rms = np.sqrt(np.mean(frames**2, axis=1)) + 1e-6
            chunks.append(20 * np.log10(rms / 32768))
            small = frames.reshape(-1, frame_bytes // 2 // per)
            fine.append((20 * np.log10(np.sqrt(np.mean(small**2, axis=1)) / 32768 + 1e-9)).astype(np.float32))
            leftover = buf[usable:]
            done += len(frames)
            if progress and duration:
                progress(min(1.0, done * HOP / duration))
    return (np.concatenate(chunks) if chunks else np.zeros(0),
            np.concatenate(fine) if fine else np.zeros(0, np.float32))


# ---------------------------------------------------------------- signals


def _per_second(frames: np.ndarray, n_seconds: int) -> np.ndarray:
    per = int(round(1 / HOP))
    usable = len(frames) // per * per
    sec = frames[:usable].reshape(-1, per).mean(axis=1) if usable else np.zeros(0)
    if len(sec) < n_seconds:
        sec = np.pad(sec, (0, n_seconds - len(sec)), mode="edge" if len(sec) else "constant")
    return sec[:n_seconds]


def audio_signal(db: np.ndarray, n: int) -> np.ndarray | None:
    if not len(db):
        return None
    sec = _per_second(db, n)
    baseline = _rolling(sec, 90, np.median)  # what "normal" sounds like around here
    return _smooth(0.65 * _rank01(sec - baseline) + 0.35 * _rank01(sec), 4)


def heatmap_signal(heatmap: list[dict], n: int) -> np.ndarray | None:
    if not heatmap:
        return None
    sig = np.zeros(n)
    for seg in heatmap:
        a, b = int(seg["start_time"]), int(np.ceil(seg["end_time"]))
        sig[a:b] = seg["value"]
    # Everyone watches the first few seconds and lots of people skip to the
    # ending/credits, so the heatmap is inflated at both edges - flatten those
    # so the intro and outro don't win by default.
    median = np.median(sig)
    intro = max(20, int(n * 0.03))
    outro = max(30, int(n * 0.05))
    sig[:intro] = np.minimum(sig[:intro], median)
    sig[-outro:] = np.minimum(sig[-outro:], median)
    return _minmax(sig)


def _mark(cues: list[Cue], n: int, match: Callable[[str], bool], before: float, after: float) -> np.ndarray | None:
    sig = np.zeros(n)
    for cue in cues:
        if match(cue.text):
            a, b = max(0, int(cue.start - before)), min(n, int(cue.end + after))
            sig[a:b] += 1
    if not sig.any():
        return None
    return np.clip(_smooth(sig, 3), 0, 1)


def reaction_signal(cues: list[Cue], n: int) -> np.ndarray | None:
    # The funny/impressive bit happens just *before* the laugh.
    return _mark(cues, n, lambda t: bool(REACTION.search(t)), 6, 1)


def keyword_signal(cues: list[Cue], n: int, keywords: list[str]) -> np.ndarray | None:
    if not keywords:
        return None
    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b", re.I)
    return _mark(cues, n, lambda t: bool(pattern.search(t)), 2, 4)


def scene_signal(cuts: list[float], n: int) -> np.ndarray | None:
    if len(cuts) < 3:
        return None
    counts = np.zeros(n)
    for t in cuts:
        if 0 <= t < n:
            counts[int(t)] += 1
    density = _rolling(counts, 10, np.sum)
    return _minmax(_smooth(density, 3))


def build_signals(m: Measurements, keywords: list[str]) -> dict[str, np.ndarray]:
    n = m.seconds
    raw = {
        "heatmap": heatmap_signal(m.heatmap, n),
        "audio": audio_signal(m.db, n),
        "reactions": reaction_signal(m.cues, n),
        "scenes": scene_signal(m.scene_cuts, n),
        "keywords": keyword_signal(m.cues, n, keywords),
    }
    return {k: v for k, v in raw.items() if v is not None}


def combine(signals: dict[str, np.ndarray], weights: dict[str, float], n: int) -> np.ndarray:
    parts = [(weights.get(k, 0.0), s) for k, s in signals.items() if weights.get(k, 0.0) > 0]
    total = sum(w for w, _ in parts)
    if not total:
        return np.zeros(n)
    score = sum(w * s for w, s in parts) / total
    # The last few seconds are usually end screens, credits or "subscribe!" outros.
    tail = max(15, int(n * 0.02))
    if n > tail * 4:
        score[-tail:] *= 0.5
    return score


# ---------------------------------------------------------------- clip picking


def quiet_frames(db: np.ndarray) -> np.ndarray:
    return db < np.percentile(db, 20) if len(db) else np.zeros(0, bool)


def snap(t: float, quiet: np.ndarray, lo: float, hi: float) -> float:
    """Move t to the nearest quiet frame within [t+lo, t+hi], if any."""
    if not len(quiet):
        return t
    a, b = max(0, int((t + lo) / HOP)), min(len(quiet), int((t + hi) / HOP) + 1)
    idx = np.nonzero(quiet[a:b])[0]
    if not len(idx):
        return t
    frames = a + idx
    return float(frames[np.argmin(np.abs(frames * HOP - t))] * HOP)


def fit_to_sentences(start: float, end: float, sents: list[Cue], min_len: float, max_len: float,
                     reach: float = 4.0) -> tuple[float, float]:
    """Move clip edges onto sentence boundaries so nobody is cut off mid-sentence.

    The start goes back to the beginning of the sentence it falls in (or forward to the
    next sentence if that is closer); the end goes forward to the end of its sentence.
    Too-short results grow a whole sentence at a time, never past max_len."""
    if not sents:
        return start, end
    starts = np.array([s.start for s in sents])
    ends = np.array([s.end for s in sents])

    def nearest(arr: np.ndarray, t: float, back: float, fwd: float) -> float:
        ok = np.nonzero((arr >= t - back) & (arr <= t + fwd))[0]
        return float(arr[ok[np.argmin(np.abs(arr[ok] - t))]]) if len(ok) else t

    i = int(np.searchsorted(starts, start, side="right")) - 1  # sentence containing start
    if 0 <= i and start - starts[i] <= reach and start < ends[i]:
        new_start = float(starts[i])
    else:
        new_start = nearest(starts, start, reach / 2, reach)
    j = int(np.searchsorted(ends, end, side="left"))  # first sentence ending at/after end
    if j < len(ends) and starts[j] < end and ends[j] - end <= reach:
        new_end = float(ends[j])
    else:
        new_end = nearest(ends, end, reach, reach / 2)
    if new_end <= new_start:
        return start, end
    k = int(np.searchsorted(ends, new_end, side="left")) + 1
    while new_end - new_start < min_len and k < len(ends) and ends[k] - new_start <= max_len:
        new_end = float(ends[k])
        k += 1
    return new_start, new_end


def breathing_room(start: float, end: float, sents: list[Cue], lead: float, tail: float) -> tuple[float, float]:
    """Pad a sentence-aligned clip without catching a syllable of the neighbouring sentences."""
    prev_end = max((s.end for s in sents if s.end <= start + 0.01), default=start - lead)
    next_start = min((s.start for s in sents if s.start >= end - 0.01), default=end + tail)
    return max(start - lead, min(start, prev_end + 0.05)), min(end + tail, max(end, next_start - 0.05))


def trim_dead_air(clip: Clip, words: list, lead: float = 0.25, tail: float = 0.5, slack: float = 0.6,
                  min_len: float = 0.0) -> bool:
    """Pull a clip's edges in to its speech, so it doesn't open on a second of silence or trail off
    after the last word. `words` have .start/.end. Gaps up to `slack` are left alone; a trim that
    would leave the clip shorter than `min_len` is skipped. True if the clip changed."""
    inside = [w for w in words if w.end > clip.start and w.start < clip.end]
    if not inside:
        return False
    start, end = clip.start, clip.end
    if inside[0].start - start > lead + slack:
        start = inside[0].start - lead
    if end - inside[-1].end > tail + slack:
        end = inside[-1].end + tail
    if (start, end) == (clip.start, clip.end) or end - start < min_len:
        return False
    clip.start, clip.end = round(start, 2), round(end, 2)
    return True


def clip_score(score: np.ndarray, start: float, end: float) -> float:
    seg = score[int(start) : max(int(start) + 1, int(end))]
    return round(float(seg.mean()) * 100, 1) if len(seg) else 0.0


def pick_clips(score: np.ndarray, db: np.ndarray, count: int, min_len: float, max_len: float,
               gap: float = 5.0, floor: float = 0.45, snap_edges: bool = True,
               pad_start: float = 0.0, pad_end: float = 0.0) -> list[Clip]:
    n = len(score)
    if n <= min_len:
        return [Clip(0.0, float(n), clip_score(score, 0, n))]

    min_len_i, max_len_i = max(1, int(min_len)), max(1, int(max_len))
    local = _smooth(score, 4)
    centre = _smooth(score, min_len_i)  # favours sustained interest, not blips
    quiet = quiet_frames(db) if snap_edges else np.zeros(0, bool)
    taken = np.zeros(n, bool)
    clips: list[Clip] = []

    while len(clips) < count:
        cand = np.where(taken, -np.inf, centre)
        peak = int(np.argmax(cand))
        if not np.isfinite(cand[peak]):
            break

        # Grow outward from the peak, always toward the more interesting side,
        # until the clip is long enough and interest has dropped off.
        keep_going = max(local[peak] * 0.75, float(np.percentile(score, 55)))
        a = b = peak
        while b - a + 1 < max_len_i:
            left = local[a - 1] if a > 0 and not taken[a - 1] else -1.0
            right = local[b + 1] if b < n - 1 and not taken[b + 1] else -1.0
            if max(left, right) < 0:
                break
            if b - a + 1 >= min_len_i and max(left, right) < keep_going:
                break
            if right >= left:
                b += 1
            else:
                a -= 1

        if b - a + 1 < min_len_i * 0.6:  # squeezed between earlier clips
            taken[a : b + 1] = True
            continue

        start = snap(float(a), quiet, -3, 1) - pad_start
        end = snap(float(b + 1), quiet, -1, 3) + pad_end
        start, end = max(0.0, start), min(float(n), end)
        if end - start > max_len + 3 + pad_start + pad_end:
            end = start + max_len + pad_start + pad_end
        clips.append(Clip(start, end, clip_score(score, a, b + 1)))
        taken[max(0, int(start - gap)) : min(n, int(end + gap))] = True

    # Don't pad the list with filler: drop picks far weaker than the best one.
    best = max((c.score for c in clips), default=0)
    clips = [c for c in clips if c.score >= best * floor]
    clips.sort(key=lambda c: c.score, reverse=True)
    return clips


def chapter_clips(chapters: list[dict], score: np.ndarray) -> list[Clip]:
    return [Clip(float(ch["start_time"]), float(ch["end_time"]),
                 clip_score(score, ch["start_time"], ch["end_time"]), ch.get("title") or "", origin="chapter")
            for ch in chapters]

"""Offline signals for finding clips without any AI: how animated the voice is and how emotional the words.

They join the smart signals (analyze.build_signals) as two more sliders, so videos are scored well with no
API key and no model, on any PC:

* **voice** - emotional velocity of the voice: the moving variance of the spectral centroid (pitch and
  brightness rising and falling as someone gets animated) times the RMS energy envelope, each judged
  against what's normal for that stretch of the video.
* **emotion** - the transcript's sentiment: how strong each line is, and how sharply it swings from the
  line before (a calm setup and an outraged payoff), smoothed over a few seconds.

The spectral centroid is measured in the same pass as loudness (analyze.loudness), from the 8 kHz mono
audio already being read, so it costs nothing extra.
"""

from __future__ import annotations

import numpy as np

from ..subtitles import Cue
from . import sentiment

SAMPLE_RATE = 8000


def spectral_centroid(frames: np.ndarray, rate: int = SAMPLE_RATE) -> np.ndarray:
    """Spectral centroid in Hz of each row of `frames` (int16 or float samples). Silent rows give 0."""
    if not len(frames):
        return np.zeros(0, np.float32)
    x = frames.astype(np.float32)
    x = x * np.hanning(x.shape[1]).astype(np.float32)
    mag = np.abs(np.fft.rfft(x, axis=1))
    freqs = np.fft.rfftfreq(x.shape[1], 1 / rate).astype(np.float32)
    total = mag.sum(axis=1)
    out = np.where(total > 1e-3, (mag * freqs).sum(axis=1) / np.maximum(total, 1e-9), 0.0)
    return out.astype(np.float32)


def _rolling(x: np.ndarray, win: int, fn) -> np.ndarray:
    win = max(1, min(win, len(x)))
    pad = win // 2
    padded = np.pad(x, (pad, win - 1 - pad), mode="edge")
    return fn(np.lib.stride_tricks.sliding_window_view(padded, win), axis=-1)


def _per_second(frames: np.ndarray, n: int, hop: float) -> np.ndarray:
    per = max(1, int(round(1 / hop)))
    usable = len(frames) // per * per
    sec = frames[:usable].reshape(-1, per).mean(axis=1) if usable else np.zeros(0)
    if len(sec) < n:
        sec = np.pad(sec, (0, n - len(sec)), mode="edge" if len(sec) else "constant")
    return sec[:n]


def _rank01(x: np.ndarray) -> np.ndarray:
    if len(x) < 2:
        return np.zeros_like(x, dtype=float)
    return np.argsort(np.argsort(x, kind="stable"), kind="stable") / (len(x) - 1)


def voice_signal(centroid: np.ndarray, db: np.ndarray, n: int, hop: float = 0.5) -> np.ndarray | None:
    """Emotional velocity of the voice per second, 0-1, or None without the measurements."""
    if not len(centroid) or not len(db) or n < 4:
        return None
    m = min(len(centroid), len(db))
    c, d = centroid[:m].astype(float), db[:m].astype(float)
    voiced = d > np.percentile(d, 30)  # pitch only means something where someone's talking
    if voiced.sum() < 4:
        return None
    c = np.where(voiced, c, np.median(c[voiced]))
    win = max(3, int(round(4 / hop)))  # 4-second window
    variance = _rolling(c, win, np.std)
    energy = d - _rolling(d, int(round(90 / hop)), np.median)  # loud for this part of the video
    velocity = _rank01(variance) * 0.6 + _rank01(energy) * 0.4
    sec = _per_second(velocity, n, hop)
    return _rolling(sec, 3, np.mean)


def emotion_signal(cues: list[Cue], n: int) -> np.ndarray | None:
    """Emotional intensity and swing of the transcript per second, 0-1, or None without one."""
    lines = [c for c in cues if c.text.strip()]
    if len(lines) < 3 or n < 4:
        return None
    scores = np.array([sentiment.score(c.text) for c in lines])
    if not np.any(scores):
        return None
    swing = np.abs(np.diff(scores, prepend=scores[0]))
    raw = np.abs(scores) * 0.55 + swing * 0.45
    sig = np.zeros(n)
    for c, v in zip(lines, raw):
        a, b = max(0, int(c.start)), min(n, int(np.ceil(c.end)) + 1)
        if b > a:
            sig[a:b] = np.maximum(sig[a:b], v)
    if not sig.any():
        return None
    sig = _rolling(sig, 5, np.mean)
    hi = float(sig.max())
    return sig / hi if hi > 0 else None


_STORY = ("so then", "one day", "i was", "we were", "he was", "she was", "they were", "when i", "years ago",
          "the night", "turns out", "and then", "that's when", "story")
_CURIOUS = ("why", "how", "what if", "secret", "nobody knows", "the truth", "did you know", "the reason",
            "here's the thing", "mystery", "guess what", "you won't believe")
_HUMOR = ("haha", "lol", "laugh", "laughter", "funny", "joke", "hilarious")
_SHOCK = ("oh my god", "no way", "what the", "insane", "unbelievable", "died", "dead", "crazy")
_FIGHT = ("wrong", "disagree", "ridiculous", "lie", "liar", "scam", "fraud", "worst", "hate", "exposed")


def guess_category(text: str) -> str:
    """A viral category from the words alone, for clips no AI looked at: Story | Curiosity | Humor |
    Shock | Controversy, or "" when nothing stands out."""
    low = " " + " ".join((text or "").lower().split()) + " "
    counts = {
        "Story": sum(low.count(f" {w}") for w in _STORY),
        "Curiosity": sum(low.count(f" {w}") for w in _CURIOUS) + low.count("?"),
        "Humor": sum(low.count(w) for w in _HUMOR),
        "Shock": sum(low.count(f" {w}") for w in _SHOCK),
        "Controversy": sum(low.count(f" {w}") for w in _FIGHT),
    }
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] >= 2 else ""

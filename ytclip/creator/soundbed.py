"""Background music: a track from your music folder, else a calm bed composed here for the mood.

Your own royalty-free or licensed tracks come first (Settings → Editing → Music folder, the same folder the
clips use, sorted into mood folders such as Suspense, Dark, Upbeat or Chill; audio/ducking.py). Without
one, a bed is made on this PC for the storyboard's mood: slow chords (soft additive pads, detuned and in a
hall), a sub-bass root, and for the brighter moods a quiet arpeggio. It's plain by design, music to sit
under a voice, and nobody else's, so it never needs a licence or trips a copyright check.
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import numpy as np

from ..audio import ducking

RATE = 44100
VERSION = 1

# mood -> (chords as semitones above A2 = 110 Hz, seconds per chord, arpeggio notes per second, brightness 0-1)
A2 = 110.0
MOODS: dict[str, tuple[list[list[int]], float, float, float]] = {
    "Suspense": ([[0, 3, 7], [-4, 0, 3], [-7, -4, 0], [-5, -1, 2]], 8.0, 0.0, 0.35),  # Am F Dm E
    "Dark": ([[3, 6, 10], [-1, 3, 6], [-4, -1, 3], [-2, 2, 5]], 10.0, 0.0, 0.2),  # Cm Ab Fm G (low)
    "Mysterious": ([[5, 8, 12, 15], [1, 5, 8], [-2, 1, 5], [0, 4, 7]], 8.0, 1.0, 0.4),  # Dm(add9) Bb Gm A
    "Epic": ([[5, 8, 12], [1, 5, 8], [-4, 0, 3, 8], [-2, 3, 7]], 6.0, 0.0, 0.55),  # Dm Bb F C
    "Emotional": ([[3, 7, 10], [-2, 2, 5, 10], [0, 3, 7], [-4, 0, 3, 8]], 7.0, 2.0, 0.5),  # C G Am F
    "Upbeat": ([[3, 7, 10], [-2, 2, 5], [0, 3, 7], [-4, 0, 3]], 4.0, 4.0, 0.7),  # C G Am F, quicker
    "Chill": ([[-4, 0, 3, 7], [-5, -2, 2, 5], [-7, -4, 0, 3], [3, 7, 10, 14]], 6.0, 2.0, 0.5),  # Fmaj7 Em7 Dm7 Cmaj7
}
VIBE = {"Suspense": "Suspense", "Mysterious": "Suspense", "Dark": "Dark", "Epic": "Dark", "Emotional": "Chill",
        "Upbeat": "Upbeat", "Chill": "Chill"}  # mood -> audio/ducking.py's music folders


def from_folder(folder: str, mood: str, seed: str) -> Path | None:
    """One of your tracks for the mood (its folder when there is one), else None."""
    root = Path(str(folder or "")).expanduser()
    found = ducking.tracks(folder)
    if not found:
        return None
    want = VIBE.get(mood, "Chill")
    words = (mood.lower(),) + ducking.VIBES.get(want, ())
    matching = [p for p in found if any(w in " ".join(x.lower() for x in p.relative_to(root).parts) for w in words)]
    pool = matching or found
    h = int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16)
    return pool[h % len(pool)]


def _pad_note(freq: float, t: np.ndarray, brightness: float, rng: np.random.Generator) -> np.ndarray:
    out = np.zeros_like(t)
    for detune in (-0.004, 0.0, 0.0045):
        f = freq * (1 + detune)
        phase = rng.uniform(0, 2 * np.pi)
        for k in range(1, 7):
            amp = (1 / k) * np.exp(-k * (1.1 - brightness))
            out += amp * np.sin(2 * np.pi * f * k * t + phase * k)
    return out / 3


def compose(mood: str, seconds: float, seed: str = "") -> np.ndarray:
    """A bed of `seconds` for the mood, stereo float32 at RATE."""
    from .sfx_library import reverb

    chords, per, arp_rate, bright = MOODS.get(mood, MOODS["Suspense"])
    rng = np.random.default_rng(int(hashlib.sha1(f"{mood}:{seed}:{VERSION}".encode()).hexdigest()[:8], 16))
    n = int(seconds * RATE)
    left = np.zeros(n)
    right = np.zeros(n)
    fade = min(2.5, per / 3)
    k = 0
    start = 0.0
    while start < seconds:
        chord = chords[k % len(chords)]
        a, b = int(start * RATE), min(n, int((start + per + fade) * RATE))
        t = np.arange(b - a) / RATE
        env = np.minimum(1, t / fade) * np.minimum(1, np.maximum(0, (per + fade - t) / fade))
        for j, semi in enumerate(chord):
            note = _pad_note(A2 * 2 ** (semi / 12), t, bright, rng) * env * 0.22
            pan = (j / max(1, len(chord) - 1) - 0.5) * 0.6
            left[a:b] += note * (1 - pan)
            right[a:b] += note * (1 + pan)
        root = A2 * 2 ** (min(chord) / 12)
        while root > 70:
            root /= 2
        sub = np.sin(2 * np.pi * root * t) * env * 0.25
        left[a:b] += sub
        right[a:b] += sub
        if arp_rate:
            step = 1 / arp_rate
            notes = sorted(chord) + [sorted(chord)[0] + 12]
            m = 0
            while m * step < per:
                i0 = a + int(m * step * RATE)
                length = min(int(0.9 * RATE), n - i0)
                if length <= 0:
                    break
                tt = np.arange(length) / RATE
                f = A2 * 2 ** ((notes[m % len(notes)] + 12) / 12)
                pluck = np.sin(2 * np.pi * f * tt) * np.exp(-tt / 0.35) * 0.06
                pluck += 0.4 * np.sin(4 * np.pi * f * tt) * np.exp(-tt / 0.18) * 0.06
                pan = 0.3 if m % 2 else -0.3
                left[i0:i0 + length] += pluck * (1 - pan)
                right[i0:i0 + length] += pluck * (1 + pan)
                m += 1
        start += per
        k += 1
    # A slow swell, so the bed breathes.
    t = np.arange(n) / RATE
    swell = 0.85 + 0.15 * np.sin(2 * np.pi * t / 23.0)
    out = np.stack([reverb(left * swell, 2.8, 0.4, f"bed-l{mood}")[:n],
                    reverb(right * swell, 2.8, 0.4, f"bed-r{mood}")[:n]], axis=1)
    edge = min(n // 4, int(1.5 * RATE))
    if edge:
        ramp = np.linspace(0, 1, edge)[:, None]
        out[:edge] *= ramp
        out[-edge:] *= ramp[::-1]
    peak = np.max(np.abs(out)) or 1.0
    return (out / peak * 0.7).astype(np.float32)


def bed(mood: str, seconds: float, folder: Path, seed: str = "") -> Path:
    """The composed bed as a WAV in `folder`."""
    path = folder / f"music-{mood.lower()}.wav"
    data = compose(mood, seconds + 1.0, seed)
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return path


def music_for(s, mood: str, seconds: float, work: Path, seed: str = "") -> tuple[Path | None, str]:
    """(file, where it's from) for the video's music: your folder's track, a composed bed, or none
    (Settings → Creator → Music: off)."""
    choice = getattr(s, "creator_music", "auto")
    if choice == "off":
        return None, "none"
    mine = from_folder(getattr(s, "music_folder", ""), mood, seed) if choice in ("auto", "folder") else None
    if mine is not None:
        return mine, f"your music: {mine.name}"
    if choice == "folder":
        return None, "none (the music folder is empty)"
    return bed(mood, seconds, work, seed), f"composed here ({mood.lower()} bed)"

"""Sound effects: whooshes, risers, bass drops, booms, hits, ticks, matched to the storyboard's cues.

Every cue has a built-in sound, synthesised here from noise and sine waves (so it's ours, with nothing to
license) and saved once to %LOCALAPPDATA%\\ClipMonster\\creator\\sfx. A folder of your own effects
(Settings → Creator → Sound effects folder) comes first: files or folders named for a cue ("whoosh",
"bass drop", "riser", "glass") are used for it, rotated so the same file doesn't play every time.

plan() turns the storyboard into timed events: each scene's cue at its start plus its offset (a riser is
placed to finish there instead, so it builds into the moment), and with "Whoosh on scene changes" on, a
whoosh on cuts that have no other effect near them, never closer together than AUTO_GAP seconds.
"""

from __future__ import annotations

import hashlib
import re
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

RATE = 48000
VERSION = 2  # bump to make the built-in sounds again after changing them
AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus"}

# cue -> (words that name it in your files, level in dB, plays ending at the cue instead of starting there)
CUES: dict[str, tuple[tuple[str, ...], float, bool]] = {
    "whoosh": (("whoosh", "swoosh", "swish", "woosh", "transition", "swipe"), -9.0, False),
    "riser": (("riser", "rise", "uplifter", "build", "tension"), -11.0, True),
    "bass_drop": (("bass drop", "bassdrop", "drop", "808"), -5.0, False),
    "sub_boom": (("boom", "sub", "braam", "braaam", "thud"), -5.0, False),
    "impact": (("impact", "hit", "slam", "punch", "smash"), -7.0, False),
    "glass_hit": (("glass", "ting", "ding", "chime", "bell"), -12.0, False),
    "vinyl_click": (("vinyl", "record", "scratch", "crackle", "click"), -13.0, False),
    "shimmer": (("shimmer", "sparkle", "magic", "twinkle", "glitter"), -13.0, False),
    "tick": (("tick", "clock", "tock", "timer"), -11.0, False),
    "heartbeat": (("heartbeat", "heart", "pulse"), -7.0, False),
}
AUTO_GAP = 6.0  # seconds between automatic whooshes
NEAR = 1.2  # an automatic whoosh isn't added this close to another effect


@dataclass
class SfxEvent:
    time: float  # seconds into the video it starts
    cue: str
    path: Path
    gain_db: float
    length: float = 0.0


# ---------------------------------------------------------------- synthesis

def _rng(name: str) -> np.random.Generator:
    return np.random.default_rng(int(hashlib.sha1(f"{name}:{VERSION}".encode()).hexdigest()[:8], 16))


def _t(seconds: float) -> np.ndarray:
    return np.arange(int(seconds * RATE)) / RATE


def _sweep(f0: float, f1: float, seconds: float, curve: str = "exp") -> np.ndarray:
    """A sine whose pitch moves from f0 to f1 (phase-continuous)."""
    t = _t(seconds)
    p = t / seconds
    f = f0 * (f1 / f0) ** p if curve == "exp" else f0 + (f1 - f0) * p
    return np.sin(2 * np.pi * np.cumsum(f) / RATE)


def _band_noise(seconds: float, centre: np.ndarray | float, width: float, rng: np.random.Generator) -> np.ndarray:
    """Noise kept to a band around `centre` Hz (which may move over time), by short overlapping FFT frames."""
    n = int(seconds * RATE)
    noise = rng.standard_normal(n + 4096)
    frame, hop = 2048, 512
    win = np.hanning(frame)
    out = np.zeros(n + 4096)
    freqs = np.fft.rfftfreq(frame, 1 / RATE)
    centres = np.full(n, centre) if np.isscalar(centre) else np.asarray(centre)
    for start in range(0, n, hop):
        c = float(centres[min(start + frame // 2, n - 1)])
        spec = np.fft.rfft(noise[start:start + frame] * win)
        gain = np.exp(-0.5 * ((np.log2(np.maximum(freqs, 1)) - np.log2(max(c, 20))) / width) ** 2)
        out[start:start + frame] += np.fft.irfft(spec * gain, frame) * win
    out = out[:n]
    return out / (np.max(np.abs(out)) or 1)


def _lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / RATE)
    return np.fft.irfft(spec / np.sqrt(1 + (f / cutoff) ** 4), len(x))


def _env(seconds: float, attack: float, decay: float, shape: float = 1.0) -> np.ndarray:
    t = _t(seconds)
    a = np.clip(t / max(attack, 1e-4), 0, 1) ** shape
    d = np.exp(-np.maximum(t - attack, 0) / max(decay, 1e-4))
    return a * d


def reverb(x: np.ndarray, seconds: float = 0.8, mix: float = 0.25, seed: str = "verb") -> np.ndarray:
    """A plain hall: the sound convolved with fading noise."""
    rng = _rng(seed)
    ir = rng.standard_normal(int(seconds * RATE)) * np.exp(-_t(seconds) / (seconds / 5))
    ir = _lowpass(ir, 6000)
    ir /= np.sqrt(np.sum(ir ** 2)) or 1
    n = len(x) + len(ir) - 1
    size = 1 << (n - 1).bit_length()
    wet = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[:n]
    dry = np.concatenate([x, np.zeros(len(ir) - 1)])
    return dry * (1 - mix) + wet * mix


def _norm(x: np.ndarray, peak: float = 0.9) -> np.ndarray:
    m = np.max(np.abs(x)) or 1.0
    return (x / m * peak).astype(np.float32)


def _stereo(mono: np.ndarray, pan: np.ndarray | float = 0.0) -> np.ndarray:
    """(n, 2), panned -1 (left) to 1 (right), constant power."""
    pan = np.clip(pan, -1, 1)
    a = (pan + 1) * np.pi / 4
    return np.stack([mono * np.cos(a), mono * np.sin(a)], axis=1)


def synth(cue: str) -> np.ndarray:
    """The built-in sound for `cue`, stereo float32 at RATE."""
    rng = _rng(cue)
    if cue == "whoosh":
        d = 0.9
        t = _t(d)
        centre = 300 * (3500 / 300) ** np.sin(np.pi * t / d * 0.5) ** 2
        x = _band_noise(d, centre, 0.55, rng) * _env(d, 0.42, 0.12, 2.0)
        return _norm(_stereo(reverb(x, 0.4, 0.2, cue)[: len(t) + RATE // 4],
                             np.linspace(-0.7, 0.7, len(t) + RATE // 4)))
    if cue == "riser":
        d = 2.0
        t = _t(d)
        centre = 250 * (5000 / 250) ** (t / d)
        x = 0.6 * _band_noise(d, centre, 0.4, rng) + 0.4 * _sweep(180, 1400, d) * (t / d)
        x *= (t / d) ** 2.2
        x[-int(0.02 * RATE):] *= np.linspace(1, 0, int(0.02 * RATE))
        return _norm(_stereo(reverb(x, 0.6, 0.25, cue)[: len(t)]))
    if cue == "bass_drop":
        d = 1.8
        body = _sweep(120, 34, d) * _env(d, 0.004, 0.55)
        click = _band_noise(0.05, 2500, 0.8, rng) * _env(0.05, 0.001, 0.01)
        x = np.tanh(2.2 * body)
        x[: len(click)] += 0.35 * click
        return _norm(_stereo(x))
    if cue == "sub_boom":
        d = 2.6
        body = _sweep(62, 28, d) * _env(d, 0.01, 0.9)
        thump = _lowpass(rng.standard_normal(int(d * RATE)), 180) * _env(d, 0.002, 0.08)
        x = np.tanh(1.6 * (body + 0.6 * thump / (np.max(np.abs(thump)) or 1)))
        return _norm(_stereo(reverb(x, 1.5, 0.3, cue)[: int(3.2 * RATE)]))
    if cue == "impact":
        d = 1.2
        crack = _band_noise(d, 1800, 1.4, rng) * _env(d, 0.001, 0.06)
        thump = _sweep(90, 40, d) * _env(d, 0.002, 0.22)
        x = np.tanh(1.8 * (0.7 * crack + thump))
        return _norm(_stereo(reverb(x, 1.0, 0.35, cue)[: int(1.8 * RATE)]))
    if cue == "glass_hit":
        d = 1.6
        t = _t(d)
        x = np.zeros_like(t)
        for f, a, dec in ((2217, 1.0, 0.5), (3391, 0.7, 0.35), (5127, 0.5, 0.25), (6911, 0.35, 0.18), (8750, 0.2, 0.1)):
            x += a * np.sin(2 * np.pi * f * t * (1 + 0.0015 * rng.standard_normal())) * np.exp(-t / dec)
        x[: int(0.01 * RATE)] += _band_noise(0.01, 6000, 0.8, rng) * 0.6
        return _norm(_stereo(reverb(x, 1.2, 0.3, cue)[: int(2.2 * RATE)], 0.15))
    if cue == "vinyl_click":
        d = 0.7
        n = int(d * RATE)
        x = np.zeros(n)
        for at in rng.choice(n - 200, 26, replace=False):
            x[at:at + 40] += rng.standard_normal(40) * np.exp(-np.arange(40) / 6) * rng.uniform(0.2, 1)
        x = _lowpass(x, 7000) + 0.6 * _sweep(70, 45, d) * _env(d, 0.002, 0.06)
        return _norm(_stereo(x), 0.7)
    if cue == "shimmer":
        d = 2.2
        t = _t(d)
        x = np.zeros_like(t)
        for f in (2093, 2637, 3136, 4186, 5274):
            x += np.sin(2 * np.pi * f * t) * (0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(5, 9) * t + rng.uniform(0, 6)))
        x *= np.sin(np.pi * t / d) ** 2
        return _norm(_stereo(reverb(x, 1.6, 0.45, cue)[: int(3.0 * RATE)], np.sin(2 * np.pi * 0.5 * _t(3.0)) * 0.5))
    if cue == "tick":
        d = 1.1
        n = int(d * RATE)
        x = np.zeros(n)
        for k, at in enumerate((0.0, 0.5)):
            click = _band_noise(0.025, 3200 if k == 0 else 2600, 0.5, rng) * _env(0.025, 0.0005, 0.004)
            i = int(at * RATE)
            x[i:i + len(click)] += click
        return _norm(_stereo(reverb(x, 0.3, 0.15, cue)[:n]), 0.8)
    if cue == "heartbeat":
        d = 1.7
        n = int(d * RATE)
        x = np.zeros(n)
        for at, f in ((0.0, 62), (0.24, 52), (0.85, 62), (1.09, 52)):
            beat = _sweep(f, f * 0.7, 0.25) * _env(0.25, 0.004, 0.06)
            i = int(at * RATE)
            x[i:i + len(beat)] += beat[: n - i]
        return _norm(_stereo(np.tanh(1.5 * x)))
    raise KeyError(cue)


def _write(path: Path, data: np.ndarray) -> None:
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    tmp.replace(path)


def builtin_folder() -> Path:
    from ..settings import local_dir

    return local_dir("creator", "sfx")


def builtin(cue: str) -> Path:
    """The built-in sound's WAV (made the first time)."""
    path = builtin_folder() / f"{cue}-v{VERSION}.wav"
    if not path.is_file():
        _write(path, synth(cue))
    return path


# ---------------------------------------------------------------- your own files

def _yours(folder: str | Path) -> list[Path]:
    root = Path(str(folder or "")).expanduser()
    if not str(folder or "").strip() or not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def cue_of(path: Path, root: Path) -> str:
    try:
        text = " ".join(path.relative_to(root).with_suffix("").parts).lower()
    except ValueError:
        text = path.stem.lower()
    text = re.sub(r"[_\-.]+", " ", text)
    best, best_len = "", 0
    for cue, (words, _, _) in CUES.items():
        for w in words:
            if re.search(rf"\b{re.escape(w)}", text) and len(w) > best_len:
                best, best_len = cue, len(w)
    return best


def library(folder: str | Path) -> dict[str, list[Path]]:
    """Your effects by cue."""
    root = Path(str(folder or "")).expanduser()
    out: dict[str, list[Path]] = {}
    for p in _yours(folder):
        cue = cue_of(p, root)
        if cue:
            out.setdefault(cue, []).append(p)
    return out


def sound_for(cue: str, folder: str | Path = "", seed: str = "", yours: dict[str, list[Path]] | None = None) -> Path:
    """The file for a cue: one of yours when there are any, varied by `seed`, else the built-in one."""
    yours = library(folder) if yours is None else yours
    pool = yours.get(cue) or []
    if pool:
        h = int(hashlib.sha1(f"{cue}:{seed}".encode()).hexdigest()[:8], 16)
        return pool[h % len(pool)]
    return builtin(cue)


def length_of(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, OSError, EOFError):
        from ..ffmpeg import probe

        return probe(path).duration


# ---------------------------------------------------------------- the plan

def plan(scenes, starts: list[float], cuts: list[float], *, folder: str | Path = "", auto_whoosh: bool = True,
         level_db: float = 0.0, seed: str = "", duration: float = 0.0) -> list[SfxEvent]:
    """Timed effects for the video. `scenes` are director.Scenes, `starts` when each scene's voice starts,
    `cuts` when the picture changes (scene joins)."""
    yours = library(folder)
    events: list[SfxEvent] = []
    for sc, start in zip(scenes, starts):
        cue = getattr(sc, "sfx_cue", "none")
        if cue not in CUES:
            continue
        path = sound_for(cue, folder, f"{seed}:{sc.scene_id}", yours)
        length = length_of(path)
        at = start + max(0, int(getattr(sc, "sfx_offset_ms", 0) or 0)) / 1000
        if CUES[cue][2]:  # a riser builds up to the moment
            at = max(0.0, at - length)
        events.append(SfxEvent(round(at, 3), cue, path, CUES[cue][1] + level_db, length))
    if auto_whoosh:
        last = -1e9
        for t in cuts:
            if any(abs(e.time - t) < NEAR or e.time <= t <= e.time + e.length * 0.5 for e in events):
                continue
            if t - last < AUTO_GAP:
                continue
            path = sound_for("whoosh", folder, f"{seed}:cut:{t:.1f}", yours)
            length = length_of(path)
            # The whoosh peaks on the cut.
            events.append(SfxEvent(round(max(0.0, t - length * 0.45), 3), "whoosh", path,
                                   CUES["whoosh"][1] - 2 + level_db, length))
            last = t
    if duration:
        events = [e for e in events if e.time < duration - 0.3]
    return sorted(events, key=lambda e: e.time)

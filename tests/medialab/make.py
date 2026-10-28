"""The media lab's test videos: real spoken audio, matching captions and known good moments.

Each video is a moving test picture with a hard colour change every few seconds (scene cuts),
spoken sentences from ffmpeg's flite voice with short silences between them, and "good moments":
a few seconds of shouting and laughter noise under a [Laughter] caption, or one long stretch of
excitement for part series. The captions are written the way YouTube writes them, one timed tag
per word, so ClipMonster reads exact word timings.

Everything ClipMonster should find is written to truth.json next to the videos: where each
sentence's speech is (no clip may start or end inside one), and where the good moments are.

    python tests/medialab/make.py OUT_DIR [--quick]
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

RATE = 16000
GAP = (0.45, 0.9)  # silence between sentences, seconds

WORDS = ("so the thing about this is that nobody ever tells you how hard it gets when you start out "
         "we tried it three times and every single time something new went wrong which honestly made it "
         "better because you learn more from the mistakes than from anything that works the first time "
         "my friend thought the whole plan was crazy but then the numbers came in and suddenly everyone "
         "wanted to know how we did it the answer is simple you keep going after everyone else stops "
         "and you write down what happened every night so tomorrow is a little less of a surprise").split()
HYPE = ["No way, that actually worked!", "Are you kidding me right now?", "That is the craziest thing I have ever seen!",
        "Everybody stop, look at this!", "I cannot believe that just happened!"]
FOREIGN = ["Hari ini kita akan mencoba sesuatu yang baru.", "Saya tidak percaya ini berhasil.",
           "Semua orang terkejut melihat hasilnya."]


@dataclass
class Spec:
    name: str
    minutes: float
    size: tuple[int, int] = (640, 360)
    moments: int = 3  # short good moments
    series: float = 0.0  # seconds of one long good stretch (0 = none), for part series
    language: str = "en"
    burned_text: bool = False
    scene_every: float = 8.0
    gap: tuple[float, float] = GAP  # silence between sentences
    auto_captions: bool = False  # like YouTube's: no punctuation, lines that run across sentences


SPECS = [
    Spec("talk-short", 2, moments=1),
    Spec("talk-10", 10),
    Spec("talk-20", 20, moments=4),
    Spec("podcast-40", 40, moments=6, scene_every=30),
    Spec("vertical-8", 8, size=(360, 640)),
    Spec("square-ish-6", 6, size=(480, 360)),
    Spec("series-12", 12, moments=1, series=150),
    Spec("series-long-15", 15, moments=2, series=200),
    Spec("burned-subs-6", 6, burned_text=True),
    Spec("indonesian-6", 6, language="id"),
    Spec("quiet-8", 8, moments=0),
    Spec("gaming-10", 10, moments=5, scene_every=3),
    Spec("fast-auto-8", 8, gap=(0.12, 0.3), auto_captions=True),  # a fast talker with auto-captions: the hard case
]
QUICK = [s for s in SPECS if s.name in ("talk-short", "talk-10", "series-12", "vertical-8")]


@dataclass
class Truth:
    name: str
    duration: float
    language: str
    sentences: list[tuple[float, float]] = field(default_factory=list)  # (speech start, speech end)
    moments: list[tuple[float, float]] = field(default_factory=list)
    series: list[tuple[float, float]] = field(default_factory=list)
    gaps: list[tuple[float, float]] = field(default_factory=list)  # silences between words inside sentences


def speak(text: str, tmp: Path) -> np.ndarray:
    out = tmp / "say.wav"
    safe = text.replace("\\", "").replace("'", "").replace(":", "").replace(",", "")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"flite=text='{safe}':voice=slt",
                    "-ar", str(RATE), "-ac", "1", str(out)], check=True)
    with wave.open(str(out)) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    # flite pads each line with a little silence: keep only the speech, so the truth is exact
    loud = np.nonzero(np.abs(data) > 0.01)[0]
    return data[loud[0]:loud[-1] + 1] if len(loud) else data


def silences(voice: np.ndarray, frame: float = 0.01, shortest: float = 0.03) -> list[tuple[float, float]]:
    """The silent stretches inside one spoken line (between its words), in seconds from its start."""
    step = int(RATE * frame)
    n = len(voice) // step
    quiet = np.sqrt(np.mean(voice[:n * step].reshape(n, step) ** 2, axis=1)) < 0.004
    out, i = [], 0
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            if (j - i) * frame >= shortest:
                out.append((i * frame, j * frame))
            i = j
        else:
            i += 1
    return out


def sentence(rng: random.Random) -> str:
    n = rng.randint(6, 14)
    i = rng.randrange(len(WORDS) - n)
    words = WORDS[i:i + n]
    return " ".join(words).capitalize() + rng.choice([".", ".", ".", "?"])


def stamp(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def build(spec: Spec, out: Path, tmp: Path, seed: int = 7) -> Truth:
    rng = random.Random(f"{spec.name}{seed}")
    total = spec.minutes * 60
    audio = np.zeros(int(total * RATE) + RATE, np.float32)
    truth = Truth(spec.name, total, spec.language)
    cues: list[tuple[float, float, str, list[tuple[float, str]]]] = []  # start, end, text, word stamps

    # Where the good moments go: spread out, never near the very start or end
    slots: list[tuple[float, float, str]] = []
    if spec.series:
        a = rng.uniform(total * 0.3, total * 0.6 - spec.series)
        slots.append((a, a + spec.series, "series"))
    for k in range(spec.moments):
        for _ in range(50):
            a = rng.uniform(30, total - 40)
            if all(a + 14 < s or a > e + 20 for s, e, _ in slots):
                slots.append((a, a + rng.uniform(8, 14), "moment"))
                break
    slots.sort()

    t = 1.0
    cache: dict[str, np.ndarray] = {}
    while t < total - 4:
        slot = next(((s, e, kind) for s, e, kind in slots if s <= t < e), None)
        if slot:
            line = rng.choice(HYPE)
        elif spec.language == "id":
            line = rng.choice(FOREIGN)
        else:
            line = sentence(rng)
        voice = cache.get(line)
        if voice is None:
            voice = cache[line] = speak(line, tmp)
        n = len(voice)
        if t + n / RATE > total - 2:
            break
        gain = 0.9 if slot else 0.35
        i = int(t * RATE)
        audio[i:i + n] += voice * gain
        start, end = t, t + n / RATE
        words = line.split()
        # word stamps spread by letters: flite speaks at an even pace
        letters = sum(len(w) for w in words) or 1
        at, stamps = start, []
        for w in words:
            stamps.append((at, w))
            at += (end - start) * len(w) / letters
        text = line
        if slot and rng.random() < 0.6:
            text = "[Laughter] " + line
        cues.append((start, end, text, stamps))
        truth.sentences.append((round(start, 3), round(end, 3)))
        truth.gaps += [(round(start + a, 3), round(start + b, 3)) for a, b in silences(voice)]
        t = end + rng.uniform(*spec.gap)
    # Excitement under the good moments: crowd-like noise between the words
    for s, e, kind in slots:
        a, b = int(s * RATE), int(e * RATE)
        noise = np.random.default_rng(int(s)).normal(0, 0.18, b - a).astype(np.float32)
        audio[a:b] += noise
        (truth.series if kind == "series" else truth.moments).append((round(s, 2), round(e, 2)))
    audio = np.clip(audio, -1, 1)
    wav = tmp / f"{spec.name}.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((audio[:int(total * RATE)] * 32767).astype(np.int16).tobytes())

    vtt = out / f"{spec.name}.{spec.language}.vtt"
    lines = ["WEBVTT", "Kind: captions", f"Language: {spec.language}", ""]
    if spec.auto_captions:  # every word timed, no punctuation, 7 words a line whatever the sentences
        said = [(at, w.strip(".,?!").lower()) for _, _, _, stamps in cues for at, w in stamps]
        for k in range(0, len(said), 7):
            line = said[k:k + 7]
            until = said[k + 7][0] if k + 7 < len(said) else line[-1][0] + 1.0
            tagged = "".join(f"<{stamp(at)}><c> {w}</c>" if j else w for j, (at, w) in enumerate(line))
            lines += [f"{stamp(line[0][0])} --> {stamp(until)}", tagged, ""]
    for start, end, text, stamps in [] if spec.auto_captions else cues:
        tagged = "".join(f"<{stamp(at)}><c> {w}</c>" if k else w for k, (at, w) in enumerate(stamps))
        if text.startswith("[Laughter]"):
            tagged = "[Laughter] " + tagged
        lines += [f"{stamp(start)} --> {stamp(end)}", tagged, ""]
    vtt.write_text("\n".join(lines), encoding="utf-8")

    w, h = spec.size
    vf = [f"hue=H=floor(t/{spec.scene_every})*1.7"]
    if spec.burned_text:
        vf.append(f"subtitles={vtt.as_posix()}:force_style='FontSize=22'")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=15",
                    "-i", str(wav), "-t", f"{total:.3f}", "-vf", ",".join(vf), "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "34", "-g", "60", "-c:a", "aac", "-b:a", "64k",
                    "-movflags", "+faststart", "-shortest", str(out / f"{spec.name}.mp4")], check=True)
    (out / f"{spec.name}.html").write_text(
        f"<!doctype html><html><head><title>{spec.name}</title></head><body>"
        f"<video controls><source src='{spec.name}.mp4' type='video/mp4'>"
        f"<track kind='captions' src='{spec.name}.{spec.language}.vtt' srclang='{spec.language}'></video>"
        f"</body></html>", encoding="utf-8")
    return truth


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--quick", action="store_true", help="4 short videos instead of 12")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    tmp = args.out / "_tmp"
    tmp.mkdir(exist_ok=True)
    truths = {}
    for spec in QUICK if args.quick else SPECS:
        if (args.out / f"{spec.name}.mp4").exists() and (args.out / "truth.json").exists():
            old = json.loads((args.out / "truth.json").read_text())
            if spec.name in old:
                truths[spec.name] = old[spec.name]
                continue
        print(f"making {spec.name} ({spec.minutes:g} min)", flush=True)
        truths[spec.name] = asdict(build(spec, args.out, tmp))
    (args.out / "truth.json").write_text(json.dumps(truths, indent=1))
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    return 0


if __name__ == "__main__":
    sys.exit(main())

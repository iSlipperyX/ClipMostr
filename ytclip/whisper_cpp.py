"""Whisper on the graphics card: whisper.cpp built with Vulkan, so it runs on AMD, NVIDIA and Intel
graphics alike (faster-whisper only uses NVIDIA cards). The release build ships it in the app
folder as whisper-cpp/whisper-cli.exe. Its models are a different format from faster-whisper's,
so each is downloaded once, on first use, to the models folder.

All the stretches to type out go through one run, joined with a little silence between them, so the
model loads once. A PC whose graphics card turns out not to be usable is remembered, and Whisper
goes straight to the CPU there next time (transcribe.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from .ffmpeg import NO_WINDOW, Cancelled, tracked
from .settings import models_dir
from .subtitles import Cue, Word

Cancel = Cancelled
RATE = 16000
JOIN = 0.6  # seconds of silence between joined stretches
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
# faster-whisper's model names -> whisper.cpp's files. The bigger ones are quantised: much smaller to
# download, quicker on the graphics card, and hardly any less accurate.
MODELS = {
    "tiny": "ggml-tiny.bin",
    "base": "ggml-base.bin",
    "small": "ggml-small.bin",
    "medium": "ggml-medium-q5_0.bin",
    "large-v3-turbo": "ggml-large-v3-turbo-q5_0.bin",
    "large-v3": "ggml-large-v3-q5_0.bin",
}
NOT_WORDS = re.compile(r"^\s*(\[[^\]]*\]|\([^)]*\)|\*[^*]*\*)\s*$")  # [BLANK_AUDIO], (music), *laughs*
SENTENCE_END = (".", "!", "?", "…")
GPU_FOUND = re.compile(r"ggml_vulkan: Found [1-9]|using (Vulkan|CUDA|Metal)\d* backend", re.I)
NO_GPU = re.compile(r"no GPU found|ggml_vulkan: Found 0|ggml_vulkan: No devices", re.I)
PROGRESS = re.compile(r"progress\s*=\s*(\d+)%")
LANGUAGE = re.compile(r"auto-detected language:\s*([a-z]{2,3})")

Progress = Callable[[str, "float | None"], None]
Log = Callable[[str], None]


def binary() -> Path | None:
    """whisper-cli: next to the app, or wherever CLIPMONSTER_WHISPER_CPP points (tests and CI)."""
    env = os.environ.get("CLIPMONSTER_WHISPER_CPP")
    if env:
        return Path(env) if Path(env).is_file() else None
    name = "whisper-cli.exe" if sys.platform == "win32" else "whisper-cli"
    if getattr(sys, "frozen", False):
        found = Path(sys.executable).resolve().parent / "whisper-cpp" / name
        return found if found.is_file() else None
    found = shutil.which("whisper-cli")
    return Path(found) if found else None


def _state_path() -> Path:
    return models_dir() / "whisper.cpp" / "gpu.json"


def _remembered(exe: Path) -> dict:
    """What the last run found out about the graphics card, while it's the same whisper-cli."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if data.get("binary") == f"{exe}|{exe.stat().st_mtime_ns}" else {}
    except (OSError, ValueError):
        return {}


def _remember(exe: Path, ok: bool, why: str = "") -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"binary": f"{exe}|{exe.stat().st_mtime_ns}", "ok": ok, "why": why,
                                "checked": time.time()}), encoding="utf-8")


def unavailable() -> str:
    """Why Whisper can't use the graphics card, or "" when it can (or hasn't been tried yet)."""
    exe = binary()
    if exe is None:
        return "this copy of ClipMonster doesn't include it"
    known = _remembered(exe)
    if known and not known.get("ok"):
        return known.get("why") or "no usable graphics card was found last time"
    return ""


def forget_gpu_check() -> None:
    """Try the graphics card again next time (after a driver update, say)."""
    _state_path().unlink(missing_ok=True)


def model_path(name: str) -> Path:
    return models_dir() / "whisper.cpp" / MODELS.get(name, MODELS["base"])


def fetch_model(name: str, progress: Progress, opener=urllib.request.urlopen) -> Path:
    """The model file, downloaded first if it isn't here yet (whole or not at all)."""
    path = model_path(name)
    if path.is_file() and path.stat().st_size > 1_000_000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    label = f"Downloading Whisper '{name}' for the graphics card (once)"
    progress(label, None)
    with opener(MODEL_URL + path.name, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            progress(label, done / total if total else None)
    if total and part.stat().st_size != total:
        part.unlink(missing_ok=True)
        raise OSError("the Whisper model download was cut short")
    part.replace(path)
    return path


def join_spans(audio: np.ndarray, spans: list[tuple[float, float]]) -> tuple[np.ndarray, list[tuple[float, float, float]]]:
    """The stretches as one track with JOIN seconds of silence between, and where each one went:
    (start in the joined track, end in it, start in the video)."""
    gap = np.zeros(int(JOIN * RATE), np.float32)
    parts: list[np.ndarray] = []
    where: list[tuple[float, float, float]] = []
    at = 0.0
    for s, e in spans:
        piece = audio[int(s * RATE): int(e * RATE)]
        if parts:
            parts.append(gap)
            at += JOIN
        parts.append(piece)
        where.append((at, at + len(piece) / RATE, s))
        at += len(piece) / RATE
    return (np.concatenate(parts) if parts else np.zeros(0, np.float32)), where


def write_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def command(exe: Path, model: Path, wav: Path, out_base: Path, language: str, threads: int) -> list[str]:
    # -ml 1 -sow: one segment per word, each with its own times. -pp: progress on stderr.
    return [str(exe), "-m", str(model), "-f", str(wav), "-l", language or "auto", "-t", str(max(1, threads)),
            "-oj", "-of", str(out_base), "-ml", "1", "-sow", "-pp"]


def parse(data: dict, where: list[tuple[float, float, float]] | None = None) -> list[Word]:
    """Words from whisper-cli's JSON, moved back to their place in the video."""
    words: list[Word] = []
    for seg in data.get("transcription") or []:
        text = str(seg.get("text") or "").strip()
        offsets = seg.get("offsets") or {}
        if not text or NOT_WORDS.match(text) or "from" not in offsets:
            continue
        start, end = offsets["from"] / 1000, offsets["to"] / 1000
        if where is not None:
            mid = (start + end) / 2
            spot = next(((a, b, real) for a, b, real in where if a - JOIN / 2 <= mid <= b + JOIN / 2), None)
            if spot is None:
                continue
            a, b, real = spot
            start, end = real + max(0.0, start - a), real + min(b, end) - a
            if end < start:
                end = start
        if words and not text[:1].isalnum() and text[:1] not in "\"'([":  # a lone "," or "." joins its word
            words[-1].text += text
            words[-1].end = max(words[-1].end, end)
            continue
        words.append(Word(start, end, text))
    return words


def cues_from(words: list[Word], longest: float = 8.0, pause: float = 0.8) -> list[Cue]:
    """Caption lines from words: a line ends at the end of a sentence, a pause, or after `longest` s."""
    cues: list[Cue] = []
    line: list[Word] = []
    for w in words:
        if line and (w.start - line[-1].end > pause or w.end - line[0].start > longest):
            cues.append(Cue(line[0].start, line[-1].end, " ".join(x.text for x in line)))
            line = []
        line.append(w)
        if w.text.endswith(SENTENCE_END):
            cues.append(Cue(line[0].start, line[-1].end, " ".join(x.text for x in line)))
            line = []
    if line:
        cues.append(Cue(line[0].start, line[-1].end, " ".join(x.text for x in line)))
    return cues


def run(cmd: list[str], progress: Progress, label: str) -> tuple[int, list[str]]:
    """Run whisper-cli, passing on its progress. Returns its exit code and what it said."""
    said: list[str] = []
    frac: float | None = None
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    try:
        with tracked(proc):
            assert proc.stderr is not None
            for line in proc.stderr:
                said.append(line.rstrip())
                del said[:-400]
                m = PROGRESS.search(line)
                frac = int(m[1]) / 100 if m else frac
                progress(label, frac)  # also where a cancel stops it
            proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    return proc.returncode, said


def transcribe(audio: np.ndarray, model_name: str, language: str, spans: list[tuple[float, float]] | None,
               threads: int, progress: Progress, log: Log, opener=urllib.request.urlopen) -> tuple[list[Cue], list[Word]]:
    from .transcribe import merge_spans

    exe = binary()
    if exe is None:
        raise RuntimeError(unavailable())
    model = fetch_model(model_name, progress, opener)
    duration = len(audio) / RATE
    where = None
    if spans is not None:
        spans = merge_spans(spans, duration)
        if not spans:
            return [], []
        audio, where = join_spans(audio, spans)
    label = "Transcribing speech on the graphics card" if spans is None else "Transcribing the clips on the graphics card"
    with tempfile.TemporaryDirectory(prefix="cm-whisper-") as tmp:
        wav, out = Path(tmp) / "audio.wav", Path(tmp) / "words"
        write_wav(wav, audio)
        progress(label, 0.0)
        code, said = run(command(exe, model, wav, out, language, threads), progress, label)
        text = "\n".join(said)
        if code != 0 or not out.with_suffix(".json").is_file():
            tail = " ".join(line for line in said[-3:] if line) or f"exit code {code}"
            why = f"whisper.cpp failed: {tail[:300]}"
            _remember(exe, False, why)  # not again on every video: Speed's GPU check tries it afresh
            raise RuntimeError(why)
        data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8", errors="replace"))
    on_gpu = bool(GPU_FOUND.search(text)) and not NO_GPU.search(text)
    if not on_gpu:
        _remember(exe, False, "no graphics card that supports Vulkan was found")
        log("Whisper found no graphics card it can use, so it uses the CPU from now on. (Settings → Speed)")
    elif not _remembered(exe).get("ok"):
        _remember(exe, True)
        found = re.search(r"ggml_vulkan: \d+ = ([^|(]+)", text)
        log("Whisper is using the graphics card" + (f" ({found[1].strip()})" if found else "") + ".")
    words = parse(data, where)
    return cues_from(words), words

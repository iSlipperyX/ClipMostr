"""Local speech-to-text with faster-whisper (or whisper.cpp on the graphics card, whisper_cpp.py),
for videos without captions and for the clips' own captions.

It can type out the whole video, or only some stretches of it (`spans`: the clips, when YouTube's
captions found them). A whole video keeps a checkpoint as it goes, so a run that's stopped or
killed carries on where it got to instead of starting again.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .ffmpeg import NO_WINDOW, ffmpeg_exe
from .settings import models_dir
from .subtitles import Cue, Word, to_dicts

RATE = 16000
CHECKPOINT_EVERY = 30.0  # seconds between checkpoint writes while typing out a whole video

Progress = Callable[[str, "float | None"], None]
Log = Callable[[str], None]


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _load_audio(path: Path) -> np.ndarray:
    """16 kHz mono float32, decoded by our ffmpeg rather than faster-whisper's PyAV path."""
    cmd = [ffmpeg_exe(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True, creationflags=NO_WINDOW).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


def _pick_language(model, audio: np.ndarray) -> str:
    """Whisper's detector is easily fooled (British accents often come back as Welsh).
    When it isn't sure, transcribe a sample in each of the top two candidates and keep
    the one the model is more confident about."""
    lang, prob, ranked = model.detect_language(audio, language_detection_segments=4)
    if prob >= 0.9:
        return lang
    candidates = [code for code, p in ranked[:2] if p >= 0.1] or [lang]
    mid = len(audio) // 2
    sample = audio[max(0, mid - RATE * 30) : mid + RATE * 30]

    def confidence(code: str) -> float:
        segs, _ = model.transcribe(sample, language=code, vad_filter=True)
        probs = [s.avg_logprob for s in segs]
        return float(np.mean(probs)) if probs else -9.0

    return max(candidates, key=confidence)


def merge_spans(spans: list[tuple[float, float]], duration: float, gap: float = 1.0) -> list[tuple[float, float]]:
    """Spans inside the video, sorted, with ones that overlap or nearly touch joined."""
    out: list[list[float]] = []
    for s, e in sorted((max(0.0, s), min(duration, e) if duration else e) for s, e in spans):
        if e <= s:
            continue
        if out and s <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def inside(items: list, start: float, end: float) -> list:
    """The cues or words whose middle falls in [start, end)."""
    return [w for w in items if start <= (w.start + w.end) / 2 < end]


def read_checkpoint(path: Path | None, model_name: str) -> tuple[float, str, list[Cue], list[Word]]:
    """(typed out up to, language, cues, words) from a checkpoint, or nothing to carry on from."""
    if path is None:
        return 0.0, "", [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("model") != model_name:
            return 0.0, "", [], []
        return (float(data["upto"]), str(data.get("language") or ""), [Cue(**c) for c in data["cues"]],
                [Word(**w) for w in data["words"]])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0, "", [], []


def write_checkpoint(path: Path, model_name: str, upto: float, language: str, cues: list[Cue],
                     words: list[Word]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"model": model_name, "upto": upto, "language": language, "cues": to_dicts(cues),
                               "words": to_dicts(words)}), encoding="utf-8")
    tmp.replace(path)


def transcribe(path: Path, model_name: str, language: str, duration: float, progress: Progress,
               threads: int = 0, spans: list[tuple[float, float]] | None = None, checkpoint: Path | None = None,
               gpu: bool = False, log: Log | None = None) -> tuple[list[Cue], list[Word]]:
    """Type out the video, or only `spans` of it. With `gpu`, whisper.cpp on the graphics card is
    tried first; anything going wrong there falls back to faster-whisper on the CPU."""
    log = log or (lambda m: None)
    progress("Preparing audio for transcription", None)
    audio = _load_audio(path)
    duration = duration or len(audio) / RATE
    if gpu:
        from . import whisper_cpp

        why = whisper_cpp.unavailable()
        if not why:
            try:
                return whisper_cpp.transcribe(audio, model_name, language, spans, threads, progress, log)
            except whisper_cpp.Cancel:
                raise
            except Exception as exc:
                why = str(exc) or exc.__class__.__name__
        log(f"Whisper on the graphics card isn't working ({why}), so it's using the CPU.")
    return _faster_whisper(audio, model_name, language, duration, progress, threads, spans, checkpoint)


def _faster_whisper(audio: np.ndarray, model_name: str, language: str, duration: float, progress: Progress,
                    threads: int, spans: list[tuple[float, float]] | None,
                    checkpoint: Path | None) -> tuple[list[Cue], list[Word]]:
    from faster_whisper import WhisperModel

    progress(f"Loading Whisper '{model_name}' model (first use downloads it)", None)
    model = WhisperModel(model_name, device="auto", compute_type="int8", cpu_threads=threads,
                         download_root=str(models_dir()))
    cues: list[Cue] = []
    words: list[Word] = []
    if spans is None:  # the whole video, carrying on from a checkpoint
        upto, saved_language, cues, words = read_checkpoint(checkpoint, model_name)
        language = language or saved_language
        todo = [(upto, duration)] if upto < duration - 0.5 else []
    else:
        todo = merge_spans(spans, duration)
    if not language and todo:
        progress("Detecting spoken language", None)
        language = _pick_language(model, audio)
    total = sum(e - s for s, e in todo) or 1.0
    done = 0.0
    saved_at = time.monotonic()
    for start, end in todo:
        piece = audio[int(start * RATE): int(end * RATE)]
        # condition_on_previous_text off: each sentence on its own is faster, and a misheard
        # sentence can't send the rest of the video into repeating itself.
        segments, _info = model.transcribe(piece, language=language, word_timestamps=True, vad_filter=True,
                                           condition_on_previous_text=False)
        for seg in segments:
            cues.append(Cue(start + seg.start, start + seg.end, seg.text.strip()))
            for w in seg.words or []:
                text = w.word.strip()
                if text:
                    words.append(Word(start + w.start, start + w.end, text))
            label = "Transcribing speech" if spans is None else "Transcribing the clips"
            progress(label, min(1.0, (done + seg.end) / total))
            if checkpoint is not None and spans is None and time.monotonic() - saved_at >= CHECKPOINT_EVERY:
                write_checkpoint(checkpoint, model_name, start + seg.end, language, cues, words)
                saved_at = time.monotonic()
        done += end - start
    return cues, words

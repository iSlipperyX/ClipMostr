"""Local speech-to-text with faster-whisper, for videos without captions."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

import numpy as np

from .ffmpeg import NO_WINDOW, ffmpeg_exe
from .settings import models_dir
from .subtitles import Cue, Word


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _load_audio(path: Path) -> np.ndarray:
    """16 kHz mono float32, decoded by our ffmpeg rather than faster-whisper's PyAV path."""
    cmd = [ffmpeg_exe(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
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
    sample = audio[max(0, mid - 16000 * 30) : mid + 16000 * 30]

    def confidence(code: str) -> float:
        segs, _ = model.transcribe(sample, language=code, vad_filter=True)
        probs = [s.avg_logprob for s in segs]
        return float(np.mean(probs)) if probs else -9.0

    return max(candidates, key=confidence)


def transcribe(path: Path, model_name: str, language: str, duration: float,
               progress: Callable[[str, float | None], None]) -> tuple[list[Cue], list[Word]]:
    from faster_whisper import WhisperModel

    progress(f"Loading Whisper '{model_name}' model (first use downloads it)", None)
    model = WhisperModel(model_name, device="auto", compute_type="int8", download_root=str(models_dir()))
    progress("Preparing audio for transcription", None)
    audio = _load_audio(path)
    if not language:
        progress("Detecting spoken language", None)
        language = _pick_language(model, audio)
    segments, _info = model.transcribe(audio, language=language, word_timestamps=True, vad_filter=True)
    cues: list[Cue] = []
    words: list[Word] = []
    for seg in segments:
        cues.append(Cue(seg.start, seg.end, seg.text.strip()))
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(w.start, w.end, text))
        if duration:
            progress("Transcribing speech", min(1.0, seg.end / duration))
    return cues, words

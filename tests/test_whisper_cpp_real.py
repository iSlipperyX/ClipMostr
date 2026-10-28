"""whisper.cpp for real, through ClipMonster's wrapper. Runs in the Tests workflow's whisper-cpp job,
which builds whisper-cli and points these at it; skipped everywhere else.

    CLIPMONSTER_WHISPER_CPP=.../whisper-cli  CLIPMONSTER_WHISPER_SAMPLE=.../samples/jfk.wav  pytest tests/test_whisper_cpp_real.py
"""

import os
import urllib.request
from pathlib import Path

import pytest

from ytclip import whisper_cpp
from ytclip.transcribe import _load_audio

EXE = os.environ.get("CLIPMONSTER_WHISPER_CPP", "")
SAMPLE = os.environ.get("CLIPMONSTER_WHISPER_SAMPLE", "")
pytestmark = pytest.mark.skipif(not (EXE and SAMPLE), reason="needs a whisper.cpp build (the whisper-cpp CI job)")


@pytest.fixture
def models(tmp_path, monkeypatch):
    # One download of the tiny model for the whole run, outside the per-test temp profile.
    shared = Path(os.environ.get("RUNNER_TEMP") or tmp_path) / "cm-models"
    shared.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(whisper_cpp, "models_dir", lambda: shared)
    monkeypatch.setenv("CLIPMONSTER_WHISPER_CPP", str(Path(EXE).resolve()))
    return shared


def _words(words):
    return " ".join(w.text for w in words).lower()


def test_it_types_out_the_whole_sample(models):
    audio = _load_audio(Path(SAMPLE))
    notes = []
    cues, words = whisper_cpp.transcribe(audio, "tiny", "en", None, 2, lambda m, f: None, notes.append)
    text = _words(words)
    assert "country" in text and "fellow americans" in text, text
    assert all(0 <= w.start <= w.end <= len(audio) / 16000 + 0.5 for w in words)
    assert [w.start for w in words] == sorted(w.start for w in words)
    assert cues and cues[0].start <= words[0].start + 0.01
    # A CPU build: the PC is remembered as having no usable card, so Whisper goes to the CPU next time.
    assert whisper_cpp.unavailable() and any("no graphics card" in n for n in notes)


def test_only_the_stretches_asked_for_land_in_place(models):
    audio = _load_audio(Path(SAMPLE))
    # The sample is 11 s: "And so my fellow Americans, ask not what your country can do for you,
    # ask what you can do for your country." Type out only the second half, then the start too.
    cues, words = whisper_cpp.transcribe(audio, "tiny", "en", [(5.5, 11.0)], 2, lambda m, f: None, lambda m: None)
    assert words and all(5.0 <= w.start and w.end <= 11.5 for w in words), [(w.start, w.text) for w in words]
    assert "country" in _words(words)
    _, both = whisper_cpp.transcribe(audio, "tiny", "en", [(0.0, 3.5), (5.5, 11.0)], 2, lambda m, f: None,
                                     lambda m: None)
    first = _words([w for w in both if w.start < 4.0])
    assert "fellow" in first or "americans" in first, first
    assert all(not 3.9 < w.start < 5.4 for w in both)  # nothing from the gap between them


@pytest.mark.skipif(not os.environ.get("CLIPMONSTER_CHECK_MODEL_URLS"), reason="checks the model downloads exist")
def test_every_model_it_may_download_exists():
    for name, file in whisper_cpp.MODELS.items():
        req = urllib.request.Request(whisper_cpp.MODEL_URL + file, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            status, got = r.status, r.headers.get("Content-Range") or ""
            r.read(1)
        assert status in (200, 206), (name, file, status)
        size = int(got.rsplit("/", 1)[-1]) if "/" in got else 0
        assert size == 0 or size > 10_000_000, (name, file, size)

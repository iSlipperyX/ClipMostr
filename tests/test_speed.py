import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from ytclip import doctor, speed, whisper_cpp
from ytclip import transcribe as whisper
from ytclip.settings import Settings, app_dir
from ytclip.subtitles import Cue, Word


# ---------------------------------------------------------------- Speed setting

def test_threads_for_each_speed():
    assert [speed.threads(x, 16) for x in speed.LEVELS] == [4, 8, 16]  # a Ryzen 7 8700F
    assert [speed.threads(x, 4) for x in speed.LEVELS] == [2, 4, 4]
    assert speed.threads("nonsense", 16) == 8  # anything unknown is Balanced


def test_only_full_power_uses_the_graphics_card():
    assert speed.use_gpu(Settings(speed="full"))
    assert not speed.use_gpu(Settings(speed="full", gpu_whisper=False))
    assert not speed.use_gpu(Settings(speed="balanced"))
    assert speed.hwaccel("balanced") and speed.hwaccel("full") and not speed.hwaccel("quiet")


@pytest.mark.skipif(sys.platform == "win32", reason="nice values are for Linux and macOS")
def test_priority_only_ever_goes_down_off_windows(monkeypatch):
    now = {"nice": 0}
    monkeypatch.setattr(speed.os, "getpriority", lambda which, who: now["nice"])
    monkeypatch.setattr(speed.os, "setpriority", lambda which, who, value: now.update(nice=value))
    assert speed.set_priority("balanced") == "Speed: Balanced" and now["nice"] == 10
    assert speed.set_priority("balanced") == ""
    assert speed.set_priority("full") == "" and now["nice"] == 10  # can't go back up without rights
    assert speed.set_priority("quiet") == "Speed: Quiet" and now["nice"] == 19


@pytest.mark.skipif(sys.platform != "win32", reason="Windows priority classes")
def test_priority_on_windows():
    speed.set_priority("full")  # normal, as the test run already is
    assert speed.set_priority("full") == ""


def test_old_always_settings_now_type_out_just_the_clips(tmp_path):
    path = app_dir() / "settings.json"
    path.write_text(json.dumps({"transcribe": "always", "whisper_model": "medium"}), encoding="utf-8")
    assert Settings.load().transcribe == "clips"
    path.write_text(json.dumps({"transcribe": "always", "speed": "full"}), encoding="utf-8")
    assert Settings.load().transcribe == "always"  # chosen after 2.5.0: kept
    Settings(transcribe="always").save()
    assert Settings.load().transcribe == "always"


def test_doctor_says_when_full_power_cant_use_the_card(monkeypatch):
    assert doctor._whisper_speed(Settings()) == []
    monkeypatch.setattr(whisper_cpp, "unavailable", lambda: "this copy of ClipMonster doesn't include it")
    got = doctor._whisper_speed(Settings(speed="full"))
    assert got[0].level == doctor.WARN and "doesn't include it" in got[0].detail
    monkeypatch.setattr(whisper_cpp, "unavailable", lambda: "")
    assert doctor._whisper_speed(Settings(speed="full"))[0].level == doctor.GOOD


# ---------------------------------------------------------------- faster-whisper

class FakeSegment:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text
        self.words = [types.SimpleNamespace(start=start, end=end, word=f" {text}")]
        self.avg_logprob = -0.2


class FakeModel:
    made: list = []
    calls: list = []

    def __init__(self, name, device, compute_type, cpu_threads, download_root):
        FakeModel.made.append({"name": name, "threads": cpu_threads})

    def detect_language(self, audio, language_detection_segments):
        return "en", 0.99, [("en", 0.99)]

    def transcribe(self, audio, language, word_timestamps, vad_filter, condition_on_previous_text=True):
        FakeModel.calls.append({"seconds": len(audio) / 16000, "language": language,
                                "condition": condition_on_previous_text})
        n = int(len(audio) / 16000)
        return iter([FakeSegment(t + 0.1, t + 0.9, f"w{t}") for t in range(0, n, 2)]), None


@pytest.fixture
def fake_whisper(monkeypatch):
    FakeModel.made, FakeModel.calls = [], []
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(whisper, "_load_audio", lambda path: np.zeros(16000 * 60, np.float32))
    return FakeModel


def test_whisper_uses_the_threads_and_doesnt_chain_sentences(fake_whisper):
    cues, words = whisper.transcribe(Path("v.mp4"), "medium", "", 60, lambda m, f: None, threads=8)
    assert fake_whisper.made == [{"name": "medium", "threads": 8}]
    assert fake_whisper.calls == [{"seconds": 60, "language": "en", "condition": False}]
    assert len(words) == 30 and words[0].text == "w0" and cues[-1].start == pytest.approx(58.1)


def test_whisper_types_out_only_the_spans_and_puts_them_in_place(fake_whisper):
    cues, words = whisper.transcribe(Path("v.mp4"), "base", "en", 60, lambda m, f: None,
                                     spans=[(30, 40), (10, 20), (19.5, 22)])
    assert [c["seconds"] for c in fake_whisper.calls] == [12, 10]  # 10-22 joined, then 30-40
    assert words[0].start == pytest.approx(10.1) and all(10 <= w.start < 22 or 30 <= w.start < 40 for w in words)


def test_a_whole_video_keeps_a_checkpoint_and_carries_on_from_it(fake_whisper, tmp_path, monkeypatch):
    point = tmp_path / "v.whisper-base.partial.json"
    monkeypatch.setattr(whisper, "CHECKPOINT_EVERY", 0.0)
    whisper.transcribe(Path("v.mp4"), "base", "en", 60, lambda m, f: None, checkpoint=point)
    saved = json.loads(point.read_text(encoding="utf-8"))
    assert saved["upto"] == pytest.approx(58.9) and saved["model"] == "base" and len(saved["words"]) == 30
    # Stopped at 40 s last time: only the rest is typed out now, after what was saved.
    whisper.write_checkpoint(point, "base", 40.0, "en", [Cue(0, 1, "a")], [Word(0, 1, "a")])
    fake_whisper.calls.clear()
    cues, words = whisper.transcribe(Path("v.mp4"), "base", "", 60, lambda m, f: None, checkpoint=point)
    assert [c["seconds"] for c in fake_whisper.calls] == [20] and fake_whisper.calls[0]["language"] == "en"
    assert words[0].text == "a" and words[1].start == pytest.approx(40.1)
    # A checkpoint from another model doesn't count.
    assert whisper.read_checkpoint(point, "medium")[0] == 0.0


def test_spans_are_merged_and_kept_inside_the_video():
    assert whisper.merge_spans([(50, 70), (-3, 5), (5.5, 9), (65, 80)], 75) == [(0.0, 9), (50, 75)]
    assert whisper.merge_spans([(10, 5)], 60) == []
    words = [Word(1, 2, "a"), Word(4.8, 5.4, "b"), Word(6, 7, "c")]
    assert [w.text for w in whisper.inside(words, 5, 10)] == ["b", "c"]  # by its middle


def test_the_graphics_card_is_tried_first_and_the_cpu_takes_over(fake_whisper, monkeypatch):
    notes = []
    monkeypatch.setattr(whisper_cpp, "unavailable", lambda: "")

    def broken(*a, **k):
        raise RuntimeError("whisper.cpp failed: no device")

    monkeypatch.setattr(whisper_cpp, "transcribe", broken)
    _, words = whisper.transcribe(Path("v.mp4"), "base", "en", 60, lambda m, f: None, gpu=True, log=notes.append)
    assert words and "isn't working (whisper.cpp failed: no device)" in notes[0]
    monkeypatch.setattr(whisper_cpp, "transcribe", lambda *a, **k: ([Cue(0, 1, "gpu")], [Word(0, 1, "gpu")]))
    _, words = whisper.transcribe(Path("v.mp4"), "base", "en", 60, lambda m, f: None, gpu=True)
    assert [w.text for w in words] == ["gpu"]


def test_a_cancel_on_the_graphics_card_isnt_a_reason_to_use_the_cpu(fake_whisper, monkeypatch):
    from ytclip.engine import Cancelled

    monkeypatch.setattr(whisper_cpp, "unavailable", lambda: "")

    def cancelled(*a, **k):
        raise Cancelled()

    monkeypatch.setattr(whisper_cpp, "transcribe", cancelled)
    with pytest.raises(Cancelled):
        whisper.transcribe(Path("v.mp4"), "base", "en", 60, lambda m, f: None, gpu=True)
    assert not fake_whisper.calls


def test_whisper_test_times_each_model(tmp_path, monkeypatch, capsys):
    from ytclip import cli, ffmpeg

    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg, "probe", lambda p: type("M", (), {"duration": 600.0})())
    monkeypatch.setattr(whisper, "available", lambda: True)
    asked = []

    def fake(path, model, language, duration, progress, threads=0, spans=None, gpu=False, log=None, checkpoint=None):
        asked.append((model, spans, threads, gpu))
        if gpu:
            log("Whisper on the graphics card isn't working (x), so it's using the CPU.")
        return [], [Word(0, 1, "hello"), Word(1, 2, "there")]

    monkeypatch.setattr(whisper, "transcribe", fake)
    monkeypatch.setattr(whisper_cpp, "unavailable", lambda: "")
    assert cli.main(["whisper-test", str(video), "--models", "small,medium", "--speed", "full"]) == 0
    out = capsys.readouterr().out
    assert [a[0] for a in asked] == ["small", "small", "medium", "medium"]
    assert asked[0][1] == [(270.0, 330.0)] and asked[0][2] == speed.threads("full")  # a minute from the middle
    assert "hello there" in out and "card failed" in out
    assert cli.main(["whisper-test", str(tmp_path / "missing.mp4")]) == 2

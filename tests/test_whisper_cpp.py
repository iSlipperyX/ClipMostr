import io
import json
from pathlib import Path

import numpy as np
import pytest

from ytclip import whisper_cpp
from ytclip.engine import Cancelled
from ytclip.subtitles import Word


def _seg(start, end, text):
    return {"offsets": {"from": int(start * 1000), "to": int(end * 1000)}, "text": text}


def test_words_come_from_one_segment_each_and_punctuation_joins_its_word():
    data = {"transcription": [_seg(0.0, 0.3, " And"), _seg(0.3, 0.6, " so"), _seg(0.6, 0.65, ","),
                              _seg(1.0, 1.4, " [BLANK_AUDIO]"), _seg(1.5, 2.0, " Americans."), _seg(2, 2, " ")]}
    words = whisper_cpp.parse(data)
    assert [(w.text, w.start, w.end) for w in words] == [("And", 0.0, 0.3), ("so,", 0.3, 0.65),
                                                         ("Americans.", 1.5, 2.0)]


def test_joined_stretches_go_back_to_their_place_in_the_video():
    audio = np.ones(16000 * 100, np.float32)
    joined, where = whisper_cpp.join_spans(audio, [(10.0, 15.0), (50.0, 52.0)])
    assert len(joined) == 16000 * 5 + int(16000 * whisper_cpp.JOIN) + 16000 * 2
    assert where == [(0.0, 5.0, 10.0), (5.0 + whisper_cpp.JOIN, 7.0 + whisper_cpp.JOIN, 50.0)]
    gap_start = 5.0 + whisper_cpp.JOIN
    data = {"transcription": [_seg(1.0, 1.5, " first"), _seg(gap_start + 0.5, gap_start + 1.0, " second"),
                              _seg(30.0, 31.0, " nowhere")]}
    words = whisper_cpp.parse(data, where)
    assert [(w.text, round(w.start, 2), round(w.end, 2)) for w in words] == [("first", 11.0, 11.5),
                                                                             ("second", 50.5, 51.0)]


def test_caption_lines_end_at_sentences_pauses_and_length():
    words = [Word(0, 0.5, "Hi"), Word(0.5, 1, "there."), Word(1.1, 1.5, "So"), Word(3.0, 3.4, "anyway"),
             Word(3.5, 4, "long")] + [Word(4 + i, 4.9 + i, f"w{i}") for i in range(10)]
    cues = whisper_cpp.cues_from(words)
    assert [c.text for c in cues[:3]] == ["Hi there.", "So", "anyway long w0 w1 w2 w3 w4 w5 w6"]
    assert all(c.end - c.start <= 8.0 for c in cues)


def test_the_command_asks_for_word_times_and_progress(tmp_path):
    cmd = whisper_cpp.command(Path("cli"), Path("m.bin"), Path("a.wav"), Path("out"), "", 8)
    assert cmd[:7] == ["cli", "-m", "m.bin", "-f", "a.wav", "-l", "auto"]
    for flag in ("-oj", "-ml", "-sow", "-pp"):
        assert flag in cmd
    assert cmd[cmd.index("-t") + 1] == "8"
    assert whisper_cpp.command(Path("cli"), Path("m"), Path("a"), Path("o"), "en", 0)[6] == "en"


class Reply(io.BytesIO):
    def __init__(self, data: bytes, length: int | None = None):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_models_download_once_whole_or_not_at_all(tmp_path):
    urls = []

    def opener(url, timeout):
        urls.append(url)
        return Reply(b"x" * 2_000_000)

    path = whisper_cpp.fetch_model("medium", lambda m, f: None, opener)
    assert path.name == "ggml-medium-q5_0.bin" and path.stat().st_size == 2_000_000
    assert urls == [whisper_cpp.MODEL_URL + "ggml-medium-q5_0.bin"]
    whisper_cpp.fetch_model("medium", lambda m, f: None, opener)
    assert len(urls) == 1  # already here
    with pytest.raises(OSError):
        whisper_cpp.fetch_model("small", lambda m, f: None, lambda url, timeout: Reply(b"x" * 10, 2_000_000))
    assert not whisper_cpp.model_path("small").exists() and not list(whisper_cpp.model_path("small").parent.glob("*.part"))


def test_where_whisper_cli_is(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPMONSTER_WHISPER_CPP", str(tmp_path / "missing"))
    assert whisper_cpp.binary() is None and "doesn't include it" in whisper_cpp.unavailable()
    exe = tmp_path / "whisper-cli"
    exe.write_bytes(b"")
    monkeypatch.setenv("CLIPMONSTER_WHISPER_CPP", str(exe))
    assert whisper_cpp.binary() == exe and whisper_cpp.unavailable() == ""


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """whisper-cli that answers with `said` on stderr and these segments, without running anything."""
    exe = tmp_path / "whisper-cli"
    exe.write_bytes(b"")
    monkeypatch.setenv("CLIPMONSTER_WHISPER_CPP", str(exe))
    whisper_cpp.model_path("base").parent.mkdir(parents=True, exist_ok=True)
    whisper_cpp.model_path("base").write_bytes(b"m" * 2_000_000)
    state = {"said": ["ggml_vulkan: Found 1 Vulkan devices:", "ggml_vulkan: 0 = AMD Radeon RX 9060 XT (AMD) | uma: 0",
                      "progress = 50%", "progress = 100%"],
             "segments": [_seg(0.2, 0.6, " hello"), _seg(0.6, 1.0, " world.")], "code": 0, "cmds": []}

    def run(cmd, progress, label):
        state["cmds"].append(cmd)
        if state["code"] == 0:
            out = Path(cmd[cmd.index("-of") + 1]).with_suffix(".json")
            out.write_text(json.dumps({"transcription": state["segments"]}), encoding="utf-8")
        for line in state["said"]:
            if "progress" in line:
                progress(label, int(line.split("=")[1].strip(" %")) / 100)
        return state["code"], state["said"]

    monkeypatch.setattr(whisper_cpp, "run", run)
    return state


def test_typed_out_on_the_graphics_card(fake_cli):
    notes, seen = [], []
    cues, words = whisper_cpp.transcribe(np.zeros(16000 * 5, np.float32), "base", "en", None, 8,
                                         lambda m, f: seen.append(f), notes.append)
    assert [w.text for w in words] == ["hello", "world."] and cues[0].text == "hello world."
    assert notes == ["Whisper is using the graphics card (AMD Radeon RX 9060 XT)."]
    assert 0.5 in seen and 1.0 in seen
    whisper_cpp.transcribe(np.zeros(16000 * 5, np.float32), "base", "en", None, 8, lambda m, f: None, notes.append)
    assert len(notes) == 1  # said once
    assert whisper_cpp.unavailable() == ""


def test_no_usable_card_is_remembered(fake_cli):
    fake_cli["said"] = ["whisper_backend_init_gpu: no GPU found"]
    notes = []
    _, words = whisper_cpp.transcribe(np.zeros(16000 * 5, np.float32), "base", "en", None, 8, lambda m, f: None,
                                      notes.append)
    assert words  # this run's words still count (whisper.cpp did it on the CPU)
    assert "no graphics card" in whisper_cpp.unavailable() and "uses the CPU from now on" in notes[0]
    whisper_cpp.forget_gpu_check()
    assert whisper_cpp.unavailable() == ""


def test_a_failed_run_is_remembered_and_raises(fake_cli):
    fake_cli["code"], fake_cli["said"] = 3, ["error: failed to initialize whisper context"]
    with pytest.raises(RuntimeError, match="failed to initialize"):
        whisper_cpp.transcribe(np.zeros(16000 * 5, np.float32), "base", "en", None, 8, lambda m, f: None,
                               lambda m: None)
    assert "failed to initialize" in whisper_cpp.unavailable()


def test_only_the_stretches_go_through_in_one_run(fake_cli):
    fake_cli["segments"] = [_seg(0.5, 1.0, " one"), _seg(2.0 + whisper_cpp.JOIN + 0.2, 2.0 + whisper_cpp.JOIN + 0.6,
                                                         " two")]
    cues, words = whisper_cpp.transcribe(np.zeros(16000 * 60, np.float32), "base", "en", [(30, 32), (10, 12)], 4,
                                         lambda m, f: None, lambda m: None)
    assert len(fake_cli["cmds"]) == 1
    assert [(w.text, round(w.start, 1)) for w in words] == [("one", 10.5), ("two", 30.2)]


def test_a_cancel_stops_it(fake_cli):
    def progress(msg, frac):
        if frac:
            raise Cancelled()

    with pytest.raises(Cancelled):
        whisper_cpp.transcribe(np.zeros(16000 * 5, np.float32), "base", "en", None, 8, progress, lambda m: None)

import io
import json
import sys
import textwrap
import time
from pathlib import Path

import numpy as np
import pytest

from ytclip import diagnostics, whisper_cpp
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
             "segments": [_seg(0.2, 0.6, " hello"), _seg(0.6, 1.0, " world.")], "code": 0, "cmds": [], "runs": [],
             "card": None}  # card: (exit code, what it says) for runs on the graphics card, when they fail

    def run(cmd, progress, label, env=None, silence=None):
        on_card = "-ng" not in cmd
        state["cmds"].append(cmd)
        state["runs"].append({"card": on_card, "env": env, "silence": silence, "label": label})
        code, said = state["code"], state["said"]
        if on_card and state["card"]:
            code, said = state["card"]
        elif not on_card:
            said = [line for line in said if "vulkan" not in line.lower()]  # the card is hidden from it
        if code == 0:
            out = Path(cmd[cmd.index("-of") + 1]).with_suffix(".json")
            out.write_text(json.dumps({"transcription": state["segments"]}), encoding="utf-8")
        for line in said:
            if "progress" in line:
                progress(label, int(line.split("=")[1].strip(" %")) / 100)
        return code, said

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


def test_typed_out_on_a_macs_graphics_card(fake_cli):
    """whisper.cpp calls a Mac's card MTL0 ("using MTL0 backend"). Read as no card at all, every video after the
    first was typed out on the CPU."""
    fake_cli["said"] = ["whisper_backend_init_gpu: using MTL0 backend", "ggml_metal_device_init: GPU name:   MTL0",
                        "ggml_metal_init: found device: Apple M2", "progress = 100%"]
    notes = []
    _, words = whisper_cpp.transcribe(np.zeros(16000 * 5, np.float32), "base", "en", None, 8, lambda m, f: None,
                                      notes.append)
    assert words
    assert whisper_cpp.unavailable() == ""
    assert notes == ["Whisper is using the graphics card (Apple M2)."]


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
    """whisper.cpp failing on the CPU as well isn't the graphics card: faster-whisper takes over (transcribe.py)."""
    fake_cli["code"], fake_cli["said"] = 3, ["error: failed to initialize whisper context"]
    with pytest.raises(RuntimeError, match="failed to initialize"):
        whisper_cpp.transcribe(np.zeros(16000 * 5, np.float32), "base", "en", None, 8, lambda m, f: None,
                               lambda m: None)
    assert "failed to initialize" in whisper_cpp.unavailable()
    assert [r["card"] for r in fake_cli["runs"]] == [True, False]  # the card, then the CPU to tell them apart
    written = diagnostics.log_path().read_text(encoding="utf-8")
    assert "whisper.cpp stopped on the graphics card (exit code 3)" in written
    assert "failed on the CPU too (exit code 3), so it isn't the graphics card" in written
    assert not whisper_cpp.resting()


# --- the graphics card failing part-way: the CPU carries on ------------------------------------------

DEVICE_LOST = (3, ["ggml_vulkan: Found 1 Vulkan devices:", "ggml_vulkan: 0 = AMD Radeon RX 9060 XT (AMD) | uma: 0",
                   "progress = 35%", "ggml_vulkan: device lost on AMD Radeon RX 9060 XT",
                   "terminate called after throwing an instance of 'vk::DeviceLostError'",
                   "  what():  vk::Device::waitForFences: ErrorDeviceLost"])


def _five_seconds(notes, threads=8):
    return whisper_cpp.transcribe(np.zeros(16000 * 5, np.float32), "base", "en", None, threads, lambda m, f: None,
                                  notes.append)


def test_a_card_that_fails_part_way_hands_the_same_run_to_the_cpu(fake_cli):
    fake_cli["card"] = DEVICE_LOST
    notes = []
    cues, words = _five_seconds(notes, threads=12)
    assert [w.text for w in words] == ["hello", "world."]  # the video is still typed out
    card, cpu = fake_cli["runs"]
    assert card["card"] and card["silence"] == whisper_cpp.GPU_SILENCE_SECONDS and card["env"] is None
    assert not cpu["card"] and cpu["env"]["GGML_VK_VISIBLE_DEVICES"] == "" and cpu["silence"] is None
    assert cpu["label"] == "Transcribing the speech on the CPU"
    assert fake_cli["cmds"][1][fake_cli["cmds"][1].index("-t") + 1] == "12"  # every thread Speed allows
    written = diagnostics.log_path().read_text(encoding="utf-8")
    assert ("[GPU] Whisper: the graphics card failed while typing out the speech. Typing out the speech again on "
            "the CPU (12 threads).") in written
    assert "ggml_vulkan: device lost on AMD Radeon RX 9060 XT" in written  # what whisper.cpp itself said
    assert notes[0].startswith("Whisper: the graphics card failed while typing out the speech.")
    assert notes[0].endswith("(details in automation.log)")
    assert notes[1].startswith("Whisper: typed out on the CPU; the graphics card is tried again after ")
    assert whisper_cpp.resting() == pytest.approx(time.time() + whisper_cpp.GPU_REST_SECONDS, abs=5)
    assert whisper_cpp.unavailable() == ""  # resting, not given up on
    assert whisper_cpp.rest_note().startswith("resting until ")


def test_while_the_card_rests_the_cpu_is_used_then_the_card_is_tried_again(fake_cli):
    fake_cli["card"] = DEVICE_LOST
    _five_seconds([])
    fake_cli["runs"].clear()
    notes = []
    _, words = _five_seconds(notes)
    assert words and [r["card"] for r in fake_cli["runs"]] == [False]  # straight to the CPU
    assert notes[0].startswith("Whisper is using the CPU until ") and "failed a little while ago" in notes[0]
    # Half an hour later the card is tried again, and it works.
    state = whisper_cpp._state_path()
    data = json.loads(state.read_text(encoding="utf-8"))
    state.write_text(json.dumps({**data, "rest_until": time.time() - 1}), encoding="utf-8")
    fake_cli["card"], notes = None, []
    fake_cli["runs"].clear()
    _five_seconds(notes)
    assert [r["card"] for r in fake_cli["runs"]] == [True]
    assert notes == ["Whisper is using the graphics card (AMD Radeon RX 9060 XT)."]
    assert not whisper_cpp.resting() and not json.loads(state.read_text(encoding="utf-8")).get("fails")


def test_a_card_that_keeps_failing_is_left_alone_until_speed_says_try_again(fake_cli):
    fake_cli["card"] = DEVICE_LOST
    state = whisper_cpp._state_path()
    notes = []
    for _ in range(whisper_cpp.GPU_FAILS_TO_STOP):
        _five_seconds(notes)
        data = json.loads(state.read_text(encoding="utf-8"))
        state.write_text(json.dumps({**data, "rest_until": time.time() - 1}), encoding="utf-8")
    assert "uses the CPU from now on" in notes[-1]
    assert "failed 3 times in a row (the graphics card failed while typing out the speech)" in whisper_cpp.unavailable()
    whisper_cpp.forget_gpu_check()
    assert whisper_cpp.unavailable() == "" and not whisper_cpp.resting()


def test_running_out_of_graphics_memory_with_the_local_ai_loaded_makes_room_first(fake_cli, monkeypatch):
    from ytclip.intelligence import llama_engine

    freed = []
    monkeypatch.setattr(llama_engine, "free_graphics_memory", lambda: freed.append(1) or True)
    fake_cli["card"] = (3, ["ggml_vulkan: Found 1 Vulkan devices:",
                            "ggml_vulkan: Device memory allocation of size 1625000000 failed.",
                            "ggml_backend_alloc_ctx_tensors_from_buft: failed to allocate Vulkan0 buffer of size 1625000000",
                            "error: failed to initialize whisper context"])
    fake_run = whisper_cpp.run

    def run(cmd, progress, label, env=None, silence=None):
        out = fake_run(cmd, progress, label, env, silence)
        fake_cli["card"] = None  # with the local AI stopped, the next try fits
        return out

    monkeypatch.setattr(whisper_cpp, "run", run)
    notes = []
    _, words = _five_seconds(notes)
    assert words and freed == [1]
    assert [r["card"] for r in fake_cli["runs"]] == [True, True]  # the card again, not the CPU
    assert "Stopped the local AI to make room" in notes[0]
    assert notes[1] == "Whisper is using the graphics card (AMD Radeon RX 9060 XT)."
    assert not whisper_cpp.resting()


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


# A whisper-cli stand-in, run for real: on the graphics card it goes quiet after 5% (a card that stopped
# answering), on the CPU (-ng) it types out one word.
HANGS_ON_THE_CARD = textwrap.dedent("""
    import json, sys, time
    args = sys.argv[1:]
    out = args[args.index("-of") + 1] + ".json"
    print("ggml_vulkan: Found 1 Vulkan devices:", file=sys.stderr, flush=True)
    if "-ng" not in args:
        print("progress = 5%", file=sys.stderr, flush=True)
        time.sleep(120)
        sys.exit(0)
    print("progress = 100%", file=sys.stderr, flush=True)
    with open(out, "w") as f:
        json.dump({"transcription": [{"offsets": {"from": 200, "to": 600}, "text": " hello"}]}, f)
""")


def test_a_card_that_stops_answering_is_stopped_and_the_cpu_takes_over(tmp_path, monkeypatch):
    exe = tmp_path / "whisper-cli"
    exe.write_bytes(b"")
    monkeypatch.setenv("CLIPMONSTER_WHISPER_CPP", str(exe))
    whisper_cpp.model_path("base").parent.mkdir(parents=True, exist_ok=True)
    whisper_cpp.model_path("base").write_bytes(b"m" * 2_000_000)
    script = tmp_path / "fake_whisper_cli.py"
    script.write_text(HANGS_ON_THE_CARD, encoding="utf-8")
    real = whisper_cpp.command
    monkeypatch.setattr(whisper_cpp, "command", lambda *a, **k: [sys.executable, str(script), *real(*a, **k)[1:]])
    monkeypatch.setattr(whisper_cpp, "GPU_SILENCE_SECONDS", 1.0)
    notes = []
    began = time.monotonic()
    _, words = _five_seconds(notes)
    assert [w.text for w in words] == ["hello"] and time.monotonic() - began < 30  # not the 120 s it'd sit there
    assert "the graphics card stopped responding while typing out the speech" in notes[0]
    assert "whisper-cli said nothing for" in diagnostics.log_path().read_text(encoding="utf-8")
    assert whisper_cpp.resting()


def test_a_long_video_on_a_slow_card_gets_longer_to_report_progress():
    assert whisper_cpp.silence_for(60) == whisper_cpp.GPU_SILENCE_SECONDS  # 10 minutes for a short one
    assert whisper_cpp.silence_for(3 * 3600) == pytest.approx(0.05 * 3 * 3600 / 0.4)  # 5% of 3 hours at 0.4x


def test_exit_codes_in_plain_words():
    assert whisper_cpp._exit(3) == "exit code 3"
    assert whisper_cpp._exit(3221225477) == "exit code 0xc0000005"  # Windows: the program crashed
    assert whisper_cpp._exit(-6) == "stopped by signal 6"
    assert whisper_cpp._exit(whisper_cpp.HUNG) == "it stopped responding"

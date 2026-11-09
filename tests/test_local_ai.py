"""The local AI: hardware detection, the model catalogue and download, llama-server over HTTP (a stand-in
server here; tests/test_local_ai_real.py runs the real one), its fallbacks when the graphics card fails,
the transcript parser and how ai.py routes to it. Sizing a model to the PC (the GGUF header, free memory,
the layer split) is tests/test_local_ai_sizing.py."""

from __future__ import annotations

import io
import json
import sys
import textwrap
import types
import urllib.error

import numpy as np
import pytest
from conftest import write_gguf

from ytclip import ai, diagnostics
from ytclip.intelligence import hardware, llama_engine, models, transcript_parser
from ytclip.intelligence.hardware import Hardware, Memory
from ytclip.rendering import plan as edit_plan
from ytclip.settings import Settings
from ytclip.subtitles import Cue

CARD = Hardware("Windows", 16, 32, "amd", "AMD Radeon RX 9060 XT", 16)


# ---------------------------------------------------------------- hardware


def test_backend_suits_the_card():
    assert CARD.backend == "vulkan"
    assert Hardware("Windows", 16, 32, "nvidia", "RTX 4070", 12).backend == "cuda"
    assert Hardware("Darwin", 10, 16, "apple", "Apple Silicon GPU", 10).backend == "metal"
    assert Hardware("Windows", 8, 16, "intel", "Intel UHD", 0.5).backend == "cpu"  # too little memory
    assert Hardware("Linux", 4, 8).backend == "cpu" and not Hardware("Linux", 4, 8).has_gpu
    assert CARD.describe() == "AMD Radeon RX 9060 XT (16 GB) · 32 GB RAM · 16 threads"


def test_linux_cards_read_from_sysfs(tmp_path):
    gb = hardware.GB
    for n, vendor, total, used in (("card0", "0x8086", None, None), ("card1", "0x1002", 16 * gb, 3 * gb)):
        dev = tmp_path / n / "device"
        dev.mkdir(parents=True)
        (dev / "vendor").write_text(vendor + "\n")
        if total:
            (dev / "mem_info_vram_total").write_text(str(total))
            (dev / "mem_info_vram_used").write_text(str(used))
    (tmp_path / "card1-DP-1").mkdir()  # a connector, not a card
    cards = hardware.linux_cards(str(tmp_path))
    assert [(c.vendor, round(c.total_gb), c.free_gb, c.source) for c in cards] == [
        ("intel", 0, None, "sysfs"), ("amd", 16, 13.0, "sysfs")]
    assert hardware.best_card(cards).vendor == "amd"
    assert hardware.best_card([]) is None


# ---------------------------------------------------------------- the catalogue


def test_model_choice_follows_graphics_memory():
    def card(gb):
        return Hardware("Windows", 16, 32, "nvidia", "Card", gb)

    assert models.choose(Hardware("Windows", 8, 16, "intel", "UHD", 0)).key == "llama-3.2-3b"  # quick on a CPU
    # The biggest that runs entirely on the card; Command R (non-commercial) is never picked by itself.
    assert [models.choose(card(gb)).key for gb in (4, 6, 8, 12, 16, 20, 24, 48)] == [
        "llama-3.2-3b", "llama-3.2-3b", "qwen2.5-7b", "mistral-nemo-12b", "mistral-nemo-12b", "mistral-nemo-12b",
        "qwen2.5-32b", "qwen2.5-32b"]
    assert models.choose(CARD).key == "mistral-nemo-12b"  # Justyn's RX 9060 XT
    model, path = models.resolve(Settings(local_model="llama-3.2-3b"), CARD)
    assert model.key == "llama-3.2-3b" and path.name == "Llama-3.2-3B-Instruct-Q4_K_M.gguf"
    assert path.parent.name == "llm" and "ClipMonster" in str(path) and "models" in str(path)
    model, path = models.resolve(Settings(local_model="custom", local_model_path="~/my.gguf"), CARD)
    assert model is None and path.name == "my.gguf"
    for m in models.MODELS.values():
        assert m.url.startswith("https://huggingface.co/") and m.url.endswith(m.file) and m.file.endswith(".gguf")


def test_pick_for_this_pc_keeps_a_downloaded_model_until_the_better_one_is_fetched(monkeypatch):
    have = {"Mistral-7B-Instruct-v0.3-Q4_K_M.gguf"}  # ClipMonster 3.1's pick, downloaded already
    monkeypatch.setattr(models, "installed", lambda path: path.name in have)
    auto = Settings(local_model="auto")
    assert models.resolve(auto, CARD)[0].key == "mistral-7b"  # never a surprise 7.5 GB download mid-run
    assert models.better_choice(auto, CARD).key == "mistral-nemo-12b"
    have.add("Mistral-Nemo-Instruct-2407-Q4_K_M.gguf")
    assert models.resolve(auto, CARD)[0].key == "mistral-nemo-12b" and models.better_choice(auto, CARD) is None
    # A downloaded model too big for the card is no stand-in.
    have.clear()
    have.add("Mistral-Nemo-Instruct-2407-Q4_K_M.gguf")
    small = Hardware("Windows", 16, 32, "nvidia", "RTX 3070", 8)
    assert models.resolve(auto, small)[0].key == "qwen2.5-7b"
    assert models.better_choice(Settings(local_model="qwen2.5-32b"), CARD) is None  # chosen by hand


def test_settings_offer_every_model_with_its_tier_and_licence():
    from ytclip import settings

    assert settings.LOCAL_MODELS == models.CHOICES  # the Settings list is written out; this keeps it the same
    assert settings.MAC_LOCAL_MODELS == models.MAC_CHOICES  # (and ClipMonster for Mac's, in a Mac's words)
    assert [k for k, _ in models.MAC_CHOICES] == [k for k, _ in models.CHOICES]
    assert dict(models.MAC_CHOICES)["qwen2.5-32b"].endswith("36 GB+ Mac")
    labels = dict(models.CHOICES)
    assert labels["qwen2.5-7b"] == "Qwen2.5 7B · Standard · 4.7 GB · 8 GB+ graphics card"
    assert labels["mistral-nemo-12b"] == "Mistral Nemo 12B · High-capacity · 7.5 GB · 12 GB+ graphics card"
    assert labels["command-r-32b"].endswith("non-commercial licence")
    assert "3.1" in labels["mistral-7b"]
    command_r = models.MODELS["command-r-32b"]
    assert not command_r.commercial and not command_r.auto
    assert {m.tier for m in models.MODELS.values()} == set(models.TIERS)


# ---------------------------------------------------------------- download


class _Reply(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Length": str(len(data))}


def test_download_resumes_and_only_names_a_whole_gguf_file(tmp_path):
    model = models.MODELS["llama-3.2-3b"]
    body = b"GGUF" + bytes(range(256)) * 40
    dest = tmp_path / model.file
    asked = []

    def stopped(req, timeout):  # the first try ends part-way
        asked.append(req.get_header("Range"))
        r = _Reply(body[:500])
        r.headers = {"Content-Length": str(len(body))}
        return r

    with pytest.raises(models.DownloadError, match="cut short"):
        models.download(model, lambda m, f: None, opener=stopped, dest=dest)
    assert not dest.exists() and (tmp_path / (model.file + ".part")).stat().st_size == 500

    def rest(req, timeout):
        asked.append(req.get_header("Range"))
        return _Reply(body[500:], status=206)

    seen = []
    assert models.download(model, lambda m, f: seen.append(f), opener=rest, dest=dest) == dest
    assert dest.read_bytes() == body and asked == [None, "bytes=500-"]
    assert seen[0] is None and seen[-1] == pytest.approx(1.0)


def test_a_download_that_drops_part_way_carries_on_next_time(tmp_path):
    model = models.MODELS["llama-3.2-3b"]
    dest = tmp_path / model.file

    class Dropped(_Reply):
        def read(self, n=-1):
            if self.tell() >= 300:
                raise ConnectionResetError("the connection was reset")
            return super().read(min(n, 100) if n and n > 0 else 100)

    with pytest.raises(models.DownloadError, match="carries on"):
        models.download(model, lambda m, f: None, opener=lambda req, timeout: Dropped(b"GGUF" + b"x" * 996),
                        dest=dest)
    assert (tmp_path / (model.file + ".part")).stat().st_size == 300


def test_a_download_that_isnt_gguf_is_thrown_away(tmp_path):
    model = models.MODELS["llama-3.2-3b"]
    dest = tmp_path / model.file
    with pytest.raises(models.DownloadError, match="isn't a GGUF"):
        models.download(model, lambda m, f: None, opener=lambda req, timeout: _Reply(b"<html>login</html>"),
                        dest=dest)
    assert not dest.exists()

    def refused(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with pytest.raises(models.DownloadError, match="401"):
        models.download(model, lambda m, f: None, opener=refused, dest=dest)


# ---------------------------------------------------------------- llama-server (a stand-in)


# Behaves like llama-server b12194 as far as ClipMonster sees it: the options it was started with go to
# --log-to, it prints where the layers went, checks the API key, streams reading progress, the answer and
# its timings. FAKE_OOM_ABOVE=N: more than N layers on the card runs it out of memory loading the model.
# FAKE_GPU_FAILS=1: on the card, the first answer loses the device and the server dies.
FAKE_SERVER = textwrap.dedent('''
    import json, os, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    args = sys.argv[1:]
    def arg(name, default=None):
        return args[args.index(name) + 1] if name in args else default

    port = int(arg("--port"))
    log = open(arg("--log-to"), "a")
    log.write(" ".join(args) + "\\n"); log.flush()
    say = lambda line: print(line, file=sys.stderr, flush=True)
    total = 29
    layers = total if arg("--fit") == "on" or arg("-ngl") is None else min(total, int(arg("-ngl")))
    on_card = layers > 0 and os.environ.get("GGML_VK_VISIBLE_DEVICES") != ""
    say("ggml_vulkan: 0 = AMD Radeon RX 9060 XT (AMD proprietary driver) | uma: 0 | fp16: 1 | warp size: 64")
    if on_card and layers > int(os.environ.get("FAKE_OOM_ABOVE", "999")):
        say("ggml_backend_alloc_ctx_tensors_from_buft: failed to allocate Vulkan0 buffer of size 4831838208")
        say("llama_model_load: error loading model: unable to allocate Vulkan0 buffer")
        say("srv    load_model: failed to load model")
        sys.exit(1)
    say(f"load_tensors: offloaded {layers if on_card else 0}/{total} layers to GPU")
    key = os.environ.get("LLAMA_API_KEY", "")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404); self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.headers.get("Authorization") != "Bearer " + key:
                self.send_response(401); self.end_headers(); return
            log.write(json.dumps(body["response_format"]["type"]) + " " + str(body["max_tokens"]) + "\\n"); log.flush()
            if on_card and os.environ.get("FAKE_GPU_FAILS"):
                say("ggml_vulkan: device lost on vkQueueSubmit")
                os._exit(134)
            answer = json.dumps({"clips": [{"start_time": 10.2, "end_time": 41.0, "hook_headline": "Forty dollars",
                "hook_rating": 9.4, "category": "Money", "pacing_style": "Dramatic Push-in",
                "viral_trigger": "Shock/Disbelief", "target_duration_tier": "Standard",
                "reason": "A twist nobody expects."}]})
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            def send(chunk):
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\\n\\n")
            send({"choices": [], "prompt_progress": {"total": 900, "processed": 900, "cache": 0, "time_ms": 40}})
            for i in range(0, len(answer), 17):
                send({"choices": [{"delta": {"content": answer[i:i + 17]}, "finish_reason": None}]})
            send({"choices": [{"delta": {}, "finish_reason": "stop"}],
                  "timings": {"prompt_n": 900, "prompt_per_second": 1500.0, "predicted_n": 120,
                              "predicted_per_second": 45.5}})
            self.wfile.write(b"data: [DONE]\\n\\n")

    HTTPServer(("127.0.0.1", port), H).serve_forever()
''')


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    """llama-server's stand-in, run as llama-server is, on a small GGUF model with 28 layers."""
    script = tmp_path / "llama-server.py"
    script.write_text(FAKE_SERVER)
    calls = tmp_path / "calls.log"
    model = write_gguf(tmp_path / "model.gguf", n_layer=28, ctx=8192)
    real = llama_engine.ServerBackend.command
    monkeypatch.setattr(llama_engine.ServerBackend, "command",
                        lambda self: [sys.executable, str(script), *real(self)[1:], "--log-to", str(calls)])
    monkeypatch.setenv("CLIPMONSTER_LLAMA_SERVER", str(script))
    monkeypatch.setattr(llama_engine, "server_flags", lambda exe: frozenset(llama_engine.FLAGS))
    monkeypatch.setattr(llama_engine, "_gpu_failed_at", 0.0)
    monkeypatch.setattr(hardware, "_cached", CARD)
    monkeypatch.setattr(hardware, "memory_now", lambda hw=None, exe=None, refresh=False:
                        Memory(16.0, 15.2, 32.0, 20.0, "Vulkan", "Vulkan0"))
    llama_engine.shutdown()
    yield Settings(ai_provider="local", local_model="custom", local_model_path=str(model)), calls
    llama_engine.shutdown()


def _starts(calls) -> list[str]:
    return [line for line in calls.read_text().splitlines() if "--port" in line]


def test_server_backend_answers_through_llama_server(fake_server):
    s, calls = fake_server
    assert ai.key_for(s, "local") == "local" and llama_engine.backend_name() == "llama-server"
    said, shown = [], []
    reply = llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=500,
                                  log=said.append, progress=lambda m, f: shown.append((m, f)))
    assert json.loads(reply.text)["clips"][0]["hook_headline"] == "Forty dollars"
    assert reply.backend == "llama-server" and reply.model == "model.gguf"
    assert reply.device == "AMD Radeon RX 9060 XT" and (reply.gpu_layers, reply.total_layers) == (29, 29)
    started = _starts(calls)[0]
    # The context is ClipMonster's; llama.cpp's --fit places the layers, keeping 7% of the card free.
    assert "-c 8192" in started and "--fit on --fit-target 1146" in started and "-ngl" not in started
    assert "--host 127.0.0.1" in started and "-lv 4" in started and "--cache-ram 0" in started
    assert "--no-webui" in started and "-ctk" not in started  # an 8,192-token memory stays at 16 bits
    assert calls.read_text().splitlines()[1] == '"json_schema" 500'  # the key was right: it answered
    assert any("running on AMD Radeon RX 9060 XT: model.gguf, 8,192-token memory" in m for m in said)
    assert any("read 900 tokens at 1,500 a second and wrote 120 at 45.5 a second" in m for m in said)
    assert ("The local AI is reading the transcript", 1.0) in shown
    # The second question reuses the running server.
    llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=500)
    assert len(_starts(calls)) == 1


def test_an_answer_gets_the_room_that_is_left(fake_server):
    s, calls = fake_server
    with pytest.raises(llama_engine.TooLong, match="too long"):  # all 9,000 tokens or nothing
        llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=9000)
    llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=9000,
                          min_tokens=1000)
    room = int(calls.read_text().splitlines()[-1].split()[-1])
    assert 6000 < room < 8192  # the memory less the question


def test_a_long_memory_is_stored_at_8_bits(fake_server):
    s, calls = fake_server
    write_gguf(s.local_model_path, n_layer=28, ctx=32768)
    reply = llama_engine.ask_json(s.copy(local_context=16384), "Pick clips.", "transcript",
                                  transcript_parser.schema(1), max_tokens=500)
    assert reply.context == 16384
    assert "-c 16384" in _starts(calls)[0] and "-ctk q8_0 -ctv q8_0" in _starts(calls)[0]


def test_layers_fixed_in_settings_are_passed_as_they_are(fake_server):
    s, calls = fake_server
    reply = llama_engine.ask_json(s.copy(local_gpu_layers=12), "Pick clips.", "transcript",
                                  transcript_parser.schema(1), max_tokens=500)
    assert "-ngl 12 --fit off" in _starts(calls)[0]
    assert reply.device == "AMD Radeon RX 9060 XT (12 of 29 layers) and the CPU"


def test_an_older_llama_server_gets_the_planned_layer_count(fake_server, monkeypatch):
    s, calls = fake_server
    monkeypatch.setattr(llama_engine, "server_flags", lambda exe: frozenset())  # no --fit, no -lv
    llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=500)
    started = _starts(calls)[0]
    assert "-ngl 29" in started and "--fit" not in started and "-lv" not in started


def test_running_out_of_graphics_memory_loading_tries_fewer_layers(fake_server, monkeypatch):
    s, calls = fake_server
    monkeypatch.setenv("FAKE_OOM_ABOVE", "21")
    said = []
    reply = llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=500,
                                  log=said.append)
    first, second = _starts(calls)
    assert "--fit on" in first and "-ngl 21 --fit off" in second  # a quarter fewer
    assert reply.device == "AMD Radeon RX 9060 XT (21 of 29 layers) and the CPU"
    assert any("ran out of memory loading the model" in m and "21 layers" in m for m in said)
    written = diagnostics.log_path().read_text()
    assert "[GPU] Local AI: the graphics card ran out of memory loading the model (model.gguf on AMD Radeon" \
           in written and "unable to allocate Vulkan0 buffer" in written
    assert not llama_engine.gpu_resting()  # out of memory isn't a broken card


def test_when_the_card_cant_hold_it_at_all_it_runs_on_the_cpu(fake_server, monkeypatch):
    s, calls = fake_server
    monkeypatch.setenv("FAKE_OOM_ABOVE", "0")
    reply = llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=500)
    starts = _starts(calls)
    assert len(starts) == 3 and "-ngl 0 -dev none --fit off" in starts[2]
    assert reply.device == "the CPU" and json.loads(reply.text)["clips"]
    assert "Running it on the CPU instead." in diagnostics.log_path().read_text()


def test_a_card_that_fails_part_way_through_an_answer_hands_over_to_the_cpu(fake_server, monkeypatch):
    s, calls = fake_server
    monkeypatch.setenv("FAKE_GPU_FAILS", "1")
    said = []
    reply = llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=500,
                                  log=said.append)
    assert json.loads(reply.text)["clips"][0]["hook_headline"] == "Forty dollars"  # the run carries on
    assert reply.device == "the CPU"
    first, second = _starts(calls)
    assert "-ngl" not in first and "-ngl 0 -dev none" in second
    assert any("the graphics card failed while answering" in m and "(details in automation.log)" in m
               for m in said)
    written = diagnostics.log_path().read_text()
    assert "[GPU] Local AI: the graphics card failed while answering" in written
    assert "Asking again on the CPU; the card is tried again in half an hour." in written
    assert "device lost on vkQueueSubmit" in written
    assert llama_engine.gpu_resting()  # the card is left alone for half an hour
    status = llama_engine.status(s)
    assert "on the CPU (16 threads)" in status and "graphics card failed earlier" in status


def test_whisper_short_of_graphics_memory_can_have_the_local_ais_back(fake_server, monkeypatch):
    s, calls = fake_server
    assert not llama_engine.free_graphics_memory()  # nothing running: nothing to free
    llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=500)
    eng = llama_engine.running_engine(s)
    assert eng.running() and eng.layers_on_gpu == 29
    with eng._lock:  # answering a question right now: never cut short
        assert not llama_engine.free_graphics_memory() and eng.running()
    assert llama_engine.free_graphics_memory() and not eng.running()
    reply = llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=500)
    assert json.loads(reply.text)["clips"] and len(_starts(calls)) == 2  # it starts again when it's asked
    eng.mode = "cpu"  # on the CPU it holds no graphics memory to give back
    assert not llama_engine.free_graphics_memory() and eng.running()


def test_find_clips_ai_routes_to_the_local_model(fake_server):
    s, _ = fake_server
    sents = [Cue(i * 5.0, i * 5.0 + 4.8, f"Sentence number {i}.") for i in range(12)]
    log = []
    clips, model = ai.find_clips_ai("A video", 60.0, sents, np.zeros(60), 3, 15, 60, "the best bits", s, log=log.append)
    assert model == "model.gguf" and len(clips) == 1
    c = clips[0]
    assert (c.start, c.end) == (10.0, 39.8)  # snapped to sentence edges
    assert c.category == "Shock" and c.trigger == "Shock/Disbelief" and c.pacing == "Dramatic Push-in"
    assert c.title == c.hook == "Forty dollars" and c.topic == "Money" and c.origin == "ai"
    assert any("local AI" in m for m in log)


def test_local_is_a_fallback_only_once_its_model_is_here(monkeypatch, tmp_path):
    monkeypatch.setattr(llama_engine, "backend_name", lambda: "llama-server")
    monkeypatch.setattr(hardware, "_cached", Hardware("Windows", 8, 16))
    s = Settings(ai_provider="claude", ai_api_key="sk-test", ai_fallback=True)
    assert ai.order(s) == ["claude"]  # not downloaded yet: never a surprise multi-GB download
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    assert ai.order(s.copy(local_model="custom", local_model_path=str(gguf))) == ["claude", "local"]
    assert ai.order(Settings(ai_provider="local")) == ["local"]
    monkeypatch.setattr(llama_engine, "backend_name", lambda: "")
    assert ai.order(Settings(ai_provider="local")) == []
    assert "local AI" in ai.missing_key(Settings(ai_provider="local"))


# ---------------------------------------------------------------- llama-cpp-python (a stand-in)


def test_llama_cpp_python_steps_down_until_the_model_loads(tmp_path, monkeypatch):
    loads = []

    class Llama:
        def __init__(self, model_path, n_ctx, n_threads, n_gpu_layers, seed, verbose):
            loads.append(n_gpu_layers)
            if n_gpu_layers > 10:
                raise ValueError("Failed to create llama_context: unable to allocate CUDA0 buffer")
            self.n_ctx = n_ctx

        def create_chat_completion(self, messages, grammar, temperature, max_tokens, stream):
            yield {"choices": [{"delta": {"content": '{"clips": []}'}, "finish_reason": "stop"}]}

    fake = types.SimpleNamespace(Llama=Llama, llama_supports_gpu_offload=lambda: True,
                                 LlamaGrammar=types.SimpleNamespace(from_json_schema=lambda text, verbose: text))
    monkeypatch.setitem(sys.modules, "llama_cpp", fake)
    monkeypatch.setattr(llama_engine, "server_binary", lambda: None)
    monkeypatch.setattr(llama_engine, "has_python_binding", lambda: True)
    monkeypatch.setattr(hardware, "_cached", Hardware("Windows", 16, 32, "nvidia", "RTX 4070", 12))
    monkeypatch.setattr(hardware, "memory_now", lambda hw=None, exe=None, refresh=False:
                        Memory(12.0, 11.0, 32.0, 20.0, "NVML"))
    model = write_gguf(tmp_path / "model.gguf", n_layer=28, ctx=8192)
    s = Settings(ai_provider="local", local_model="custom", local_model_path=str(model))
    llama_engine.shutdown()
    try:
        said = []
        reply = llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=500,
                                      log=said.append)
    finally:
        llama_engine.shutdown()
    assert loads == [29, 21, 0]  # all of it, a quarter fewer, then the CPU
    assert reply.backend == "llama-cpp-python" and reply.device == "the CPU" and reply.text == '{"clips": []}'
    assert "unable to allocate CUDA0 buffer" in diagnostics.log_path().read_text()


# ---------------------------------------------------------------- what llama.cpp says


def test_llama_cpp_failures_are_told_apart():
    start = ["ggml_vulkan: 0 = AMD Radeon RX 9060 XT (AMD proprietary driver) | uma: 0 | fp16: 1",
             "load_tensors: offloaded 38/65 layers to GPU"]
    assert llama_engine.classify(start + ["main: exiting due to HTTP server error"]) == ""  # not the card's fault
    assert llama_engine.classify(start + ["llama_model_load: error loading model: unable to allocate Vulkan0 buffer"]
                                 ) == "oom"
    assert llama_engine.classify(["what():  vk::Device::allocateMemory: ErrorOutOfDeviceMemory"]) == "oom"
    assert llama_engine.classify(["CUDA error: out of memory"]) == "oom"
    assert llama_engine.classify(start + ["ggml_vulkan: device lost on vkQueueSubmit"]) == "gpu"
    assert llama_engine.classify(["CUDA error: the launch timed out and was terminated"]) == "gpu"
    assert llama_engine.classify(["llama_model_load: error loading model: unknown model architecture: 'x'"]) == "model"
    assert llama_engine.offloaded(start) == (38, 65) and llama_engine.offloaded([]) == (-1, 0)
    assert llama_engine.device_from_log(start, True) == "AMD Radeon RX 9060 XT"
    assert llama_engine.device_from_log(start, False) == "the CPU"


# ---------------------------------------------------------------- the transcript parser


def test_long_transcripts_are_read_in_overlapping_windows():
    sents = [Cue(i, i + 1, "word " * 30) for i in range(40)]
    parts = transcript_parser.windows(sents, budget_tokens=400)
    assert len(parts) > 3
    assert parts[0][0] is sents[0] and parts[-1][-1] is sents[-1]
    for a, b in zip(parts, parts[1:]):
        assert a[-transcript_parser.OVERLAP:] == b[:transcript_parser.OVERLAP]


def test_model_answers_become_clips_on_sentence_edges():
    sents = [Cue(i * 10.0, i * 10.0 + 9.5, f"Line {i}.") for i in range(10)]
    items = [
        {"start_time": 21, "end_time": 58, "hook_headline": " The Exact Moment ", "hook_rating": 9.4,
         "category": "  Gaming ", "pacing_style": "Dramatic Push-in", "viral_trigger": "Curiosity Gap",
         "target_duration_tier": "Standard", "reason": "It lands."},
        {"start_time": 30, "end_time": 31},  # too short once snapped
        {"start_time": "x", "end_time": 2},  # unreadable
        "not a clip",
        {"start_time": 0, "end_time": 99, "viral_trigger": "Made up", "pacing_style": "Wobbly"},  # cut to max_len
        # 3.1's field names still work.
        {"start_time": 61, "end_time": 79, "hook_rating_1_to_10": 12, "viral_category": "Story", "title": "Old"},
    ]
    clips = transcript_parser.to_clips(items, sents, 100.0, max_len=40)
    assert [(c.start, c.end) for c in clips] == [(20.0, 59.5), (0.0, 39.5), (60.0, 79.5)]
    first, cut, old = clips
    assert first.score == 94.0 and first.title == first.hook == "The Exact Moment" and first.reason == "It lands."
    assert (first.category, first.trigger, first.pacing, first.topic) == ("Curiosity", "Curiosity Gap",
                                                                          "Dramatic Push-in", "Gaming")
    assert (cut.category, cut.trigger, cut.pacing, cut.score) == ("", "", "", 50.0)
    assert old.score == 100.0 and old.category == "Story" and old.title == "Old"  # a 12 is a 10
    assert transcript_parser._rating({"hook_rating": 85}) == 8.5  # rated out of 100
    assert transcript_parser._rating({"hook_rating": "nan"}) == 5.0


def test_a_creator_rewards_moment_is_lengthened_past_a_minute():
    sents = [Cue(i * 10.0, i * 10.0 + 9.5, f"Line {i}.") for i in range(12)]
    item = {"start_time": 0, "end_time": 39.5, "target_duration_tier": "Creator_Rewards_Eligible"}
    (clip,) = transcript_parser.to_clips([item], sents, 120.0, max_len=90)
    assert (clip.start, clip.end) == (0.0, 69.5)  # the first sentence end past 62 seconds
    (clip,) = transcript_parser.to_clips([item], sents, 120.0, max_len=60)
    assert clip.end == 39.5  # clips are kept under a minute: left as it is


def test_the_schema_holds_every_field_to_what_editing_understands():
    full = transcript_parser.schema(3, 15, 90, topics=["Gaming", "Money"])
    clips = full["properties"]["clips"]
    item = clips["items"]
    assert clips["maxItems"] == 3 and item["additionalProperties"] is False
    assert item["required"] == ["start_time", "end_time", "hook_headline", "hook_rating", "category",
                                "pacing_style", "viral_trigger", "target_duration_tier", "reason"]
    for name, prop in item["properties"].items():
        if prop["type"] == "string":
            assert "maxLength" in prop or "enum" in prop, name  # a small model can't ramble
    rating = item["properties"]["hook_rating"]["enum"]
    assert rating[0] == 1.0 and rating[-1] == 10.0 and 9.4 in rating and len(rating) == 91
    assert set(item["properties"]["viral_trigger"]["enum"]) == set(transcript_parser.VIRAL_TRIGGERS)
    assert set(transcript_parser.VIRAL_TRIGGERS.values()) == set(transcript_parser.CATEGORIES)
    assert item["properties"]["target_duration_tier"]["enum"] == ["Quick_Hook", "Standard", "Creator_Rewards_Eligible"]
    assert "Gaming, Money" in item["properties"]["category"]["description"]
    assert transcript_parser.tiers_for(15, 60) == ["Quick_Hook", "Standard"]
    assert transcript_parser.tiers_for(61, 90) == ["Creator_Rewards_Eligible"]
    loose = transcript_parser.schema(3, strict=False)["properties"]["clips"]["items"]["properties"]["hook_rating"]
    assert "enum" not in loose and (loose["minimum"], loose["maximum"]) == (1, 10)


def test_a_part_too_long_for_the_models_memory_is_read_in_halves(monkeypatch):
    sents = [Cue(i * 6.0, i * 6.0 + 5.5, f"Sentence {i} with a few more words in it.") for i in range(40)]
    asked = []

    def ask_json(s, system, prompt, schema, **kw):
        lines = [line for line in prompt.splitlines() if line.startswith("[")]
        asked.append(len(lines))
        if len(lines) > 25:
            raise llama_engine.TooLong("too long")
        start = float(lines[0][1:].split("-")[0])
        answer = {"clips": [{"start_time": start, "end_time": start + 23.5, "hook_headline": "Moment",
                             "hook_rating": 8.0, "category": "", "pacing_style": "Steady",
                             "viral_trigger": "Humor", "target_duration_tier": "Standard", "reason": ""}]}
        return llama_engine.Reply(json.dumps(answer), "model.gguf", "llama-server", "the CPU")

    monkeypatch.setattr(llama_engine, "room", lambda s: 100_000)  # one part, as far as the plan knows
    monkeypatch.setattr(llama_engine, "ask_json", ask_json)
    log = []
    clips, model = transcript_parser.find_clips("A video", 240.0, sents, np.zeros(0), 4, 15, 60, "", Settings(),
                                                log=log.append)
    assert asked[0] == 40 and all(n <= 25 for n in asked[1:]) and len(asked) == 3
    assert model == "model.gguf" and {c.start for c in clips} == {0.0, 108.0}  # one from each half
    assert any("reading it in two halves" in m for m in log)


def test_the_ais_pacing_decides_the_slow_push_in():
    def plan(category, pacing):
        return edit_plan.EditPlan(0, 10, [(0, 10)], 10, [], 30, None, category=category, pacing=pacing)

    assert edit_plan.PUSH_IN == transcript_parser.PUSH_IN
    assert edit_plan.pushes_in(plan("Shock", "Dramatic Push-in"))
    assert not edit_plan.pushes_in(plan("Story", "Punchy Cuts"))  # the AI said fast cuts
    assert edit_plan.pushes_in(plan("Story", "")) and not edit_plan.pushes_in(plan("Shock", ""))  # no pacing given

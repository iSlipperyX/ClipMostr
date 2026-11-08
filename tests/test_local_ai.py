"""The local AI: hardware detection, the model catalogue and download, llama-server over HTTP (a stand-in
server here; tests/test_local_ai_real.py runs the real one), the transcript parser and how ai.py routes
to it."""

from __future__ import annotations

import io
import json
import sys
import textwrap
import urllib.error

import numpy as np
import pytest

from ytclip import ai
from ytclip.intelligence import hardware, llama_engine, models, transcript_parser
from ytclip.intelligence.hardware import Hardware
from ytclip.settings import Settings
from ytclip.subtitles import Cue


# ---------------------------------------------------------------- hardware


def test_backend_suits_the_card():
    assert Hardware("Windows", 16, 32, "amd", "AMD Radeon RX 9060 XT", 16).backend == "vulkan"
    assert Hardware("Windows", 16, 32, "nvidia", "RTX 4070", 12).backend == "cuda"
    assert Hardware("Darwin", 10, 16, "apple", "Apple Silicon GPU", 10).backend == "metal"
    assert Hardware("Windows", 8, 16, "intel", "Intel UHD", 0.5).backend == "cpu"  # too little memory
    assert Hardware("Linux", 4, 8).backend == "cpu" and not Hardware("Linux", 4, 8).has_gpu
    assert Hardware("Windows", 16, 32, "amd", "AMD Radeon RX 9060 XT", 16).describe() == \
        "AMD Radeon RX 9060 XT (16 GB) · 32 GB RAM · 16 threads"


def test_linux_cards_read_from_sysfs(tmp_path):
    for n, vendor, vram in (("card0", "0x8086", None), ("card1", "0x1002", str(16 * hardware.GB))):
        dev = tmp_path / n / "device"
        dev.mkdir(parents=True)
        (dev / "vendor").write_text(vendor + "\n")
        if vram:
            (dev / "mem_info_vram_total").write_text(vram)
    (tmp_path / "card1-DP-1").mkdir()  # a connector, not a card
    cards = hardware.linux_cards(str(tmp_path))
    assert [(v, round(g)) for v, _, g in cards] == [("intel", 0), ("amd", 16)]
    assert hardware.best_card(cards)[0] == "amd"
    assert hardware.best_card([]) == ("", "", 0.0)


def test_model_choice_follows_graphics_memory():
    big = Hardware("Windows", 16, 32, "amd", "RX 9060 XT", 16)
    small = Hardware("Windows", 8, 16, "intel", "UHD", 0)
    assert models.choose(big).key == "mistral-7b"
    assert models.choose(small).key == "llama-3.2-3b"
    model, path = models.resolve(Settings(local_model="llama-3.2-3b"), big)
    assert model.key == "llama-3.2-3b" and path.name == "Llama-3.2-3B-Instruct-Q4_K_M.gguf"
    assert path.parent.name == "llm" and "ClipMonster" in str(path) and "models" in str(path)
    model, path = models.resolve(Settings(local_model="custom", local_model_path="~/my.gguf"), big)
    assert model is None and path.name == "my.gguf"
    for m in models.MODELS.values():
        assert m.url.startswith("https://huggingface.co/") and m.url.endswith(m.file)


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


FAKE_SERVER = textwrap.dedent('''
    import json, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    port = int(sys.argv[sys.argv.index("--port") + 1])
    log = open(sys.argv[sys.argv.index("--log-to") + 1], "a")
    log.write(" ".join(sys.argv[1:]) + "\\n"); log.flush()
    print("load_tensors: offloaded 29/29 layers to GPU", file=sys.stderr, flush=True)
    print("ggml_vulkan: 0 = AMD Radeon RX 9060 XT (RADV) | uma: 0", file=sys.stderr, flush=True)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404); self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            log.write(json.dumps(body["response_format"]["type"]) + " " + str(body["max_tokens"]) + "\\n"); log.flush()
            answer = json.dumps({"clips": [{"start_time": 10.2, "end_time": 41.0, "title": "Forty dollars",
                "hook_text": "The money was forty dollars", "hook_rating_1_to_10": 9, "payoff_type": "Twist",
                "viral_category": "Shock", "reason": "A twist nobody expects."}]})
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for i in range(0, len(answer), 17):
                chunk = {"choices": [{"delta": {"content": answer[i:i + 17]}, "finish_reason": None}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\\n\\n")
            done = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(b"data: " + json.dumps(done).encode() + b"\\n\\ndata: [DONE]\\n\\n")

    HTTPServer(("127.0.0.1", port), H).serve_forever()
''')


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    script = tmp_path / "llama-server.py"
    script.write_text(FAKE_SERVER)
    calls = tmp_path / "calls.log"
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF" + b"\0" * 64)
    real = llama_engine.ServerBackend.command
    monkeypatch.setattr(llama_engine.ServerBackend, "command",
                        lambda self: [sys.executable, str(script), *real(self)[1:], "--log-to", str(calls)])
    monkeypatch.setenv("CLIPMONSTER_LLAMA_SERVER", str(script))
    monkeypatch.setattr(hardware, "_cached", Hardware("Windows", 8, 16, "amd", "AMD Radeon RX 9060 XT", 16))
    yield Settings(ai_provider="local", local_model="custom", local_model_path=str(model)), calls
    llama_engine.shutdown()


def test_server_backend_answers_through_llama_server(fake_server):
    s, calls = fake_server
    assert ai.key_for(s, "local") == "local" and llama_engine.backend_name() == "llama-server"
    said = []
    reply = llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=500,
                                  log=said.append)
    assert json.loads(reply.text)["clips"][0]["title"] == "Forty dollars"
    assert reply.backend == "llama-server" and reply.model == "model.gguf"
    assert "AMD Radeon RX 9060 XT" in reply.device
    started = calls.read_text().splitlines()
    assert "-ngl 999" in started[0] and "--host 127.0.0.1" in started[0]
    assert started[1] == '"json_schema" 500'
    assert any("running on" in m for m in said)
    # The second question reuses the running server.
    llama_engine.ask_json(s, "Pick clips.", "transcript", transcript_parser.schema(1), max_tokens=500)
    assert sum(1 for line in calls.read_text().splitlines() if "--port" in line) == 1


def test_find_clips_ai_routes_to_the_local_model(fake_server):
    s, _ = fake_server
    sents = [Cue(i * 5.0, i * 5.0 + 4.8, f"Sentence number {i}.") for i in range(12)]
    log = []
    clips, model = ai.find_clips_ai("A video", 60.0, sents, np.zeros(60), 3, 15, 60, "the best bits", s, log=log.append)
    assert model == "model.gguf" and len(clips) == 1
    c = clips[0]
    assert (c.start, c.end) == (10.0, 39.8)  # snapped to sentence edges
    assert c.category == "Shock" and c.hook == "The money was forty dollars" and c.origin == "ai"
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
        {"start_time": 21, "end_time": 58, "hook_rating_1_to_10": 12, "viral_category": "Story", "title": " A "},
        {"start_time": 30, "end_time": 31},  # too short once snapped
        {"start_time": "x", "end_time": 2},  # unreadable
        "not a clip",
        {"start_time": 0, "end_time": 99, "viral_category": "Made up"},  # too long: cut to max_len
    ]
    clips = transcript_parser.to_clips(items, sents, 100.0, max_len=40)
    assert [(c.start, c.end) for c in clips] == [(20.0, 59.5), (0.0, 39.5)]
    assert clips[0].score == 100.0 and clips[0].category == "Story" and clips[0].title == "A"
    assert clips[1].category == ""


def test_schema_bounds_every_string_so_a_small_model_cant_ramble():
    item = transcript_parser.schema(3)["properties"]["clips"]["items"]
    assert transcript_parser.schema(3)["properties"]["clips"]["maxItems"] == 3
    for name, prop in item["properties"].items():
        if prop["type"] == "string":
            assert "maxLength" in prop or "enum" in prop, name
    assert set(item["required"]) >= {"start_time", "end_time", "hook_text", "hook_rating_1_to_10", "payoff_type",
                                      "viral_category"}

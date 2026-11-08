"""The local AI for real: a llama-server build loading a GGUF model and answering through ClipMonster's
wrapper, held to the clip schema, and llama.cpp's own memory figures held against ClipMonster's sizing.

Skipped unless CLIPMONSTER_LLAMA_SERVER (a llama-server) and CLIPMONSTER_TEST_GGUF (a model) are set;
the tests workflow builds a CPU llama-server and a tiny random model (tests/local_ai/make_tiny_gguf.py).
A random model's words are nonsense, so this checks the plumbing and the JSON grammar, not the picks.

CLIPMONSTER_CHECK_MODEL_URLS=1 also reads the header of every model in the catalogue straight from
Hugging Face (a few MB each, never the weights) and checks the catalogue's shapes and sizes against it.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

import pytest

from ytclip.intelligence import gguf, llama_engine, models, planner, transcript_parser
from ytclip.settings import Settings

SERVER = os.environ.get("CLIPMONSTER_LLAMA_SERVER", "")
MODEL = os.environ.get("CLIPMONSTER_TEST_GGUF", "")
needs_llama = pytest.mark.skipif(not (SERVER and Path(SERVER).is_file() and MODEL and Path(MODEL).is_file()),
                                 reason="needs CLIPMONSTER_LLAMA_SERVER and CLIPMONSTER_TEST_GGUF")


def _buffer_mib(lines: list[str], what: str) -> float:
    """llama.cpp's 'llama_kv_cache:        CPU KV buffer size =     2.00 MiB' (the sum of every buffer)."""
    found = [float(m[1]) for line in lines for m in [re.search(what + r" buffer size =\s*([\d.]+) MiB", line)] if m]
    assert found, f"llama-server didn't print its {what} buffer size"
    return sum(found)


@needs_llama
def test_llama_server_answers_in_the_clip_schema():
    s = Settings(ai_provider="local", local_model="custom", local_model_path=MODEL, local_gpu=False,
                 local_context=4096)
    try:
        said = []
        reply = llama_engine.ask_json(s, "Pick the best moment.", "[0.0-4.0] So I quit my job.\n[4.0-9.0] It paid.",
                                      transcript_parser.schema(1), max_tokens=1500, log=said.append,
                                      prompt_schema=transcript_parser.schema(1, strict=False))
        eng = llama_engine.running_engine(s)
        lines = list(eng.said)
        started = eng.command()
    finally:
        llama_engine.shutdown()
    item = transcript_parser.schema(1)["properties"]["clips"]["items"]
    data = json.loads(reply.text)
    assert isinstance(data["clips"], list) and len(data["clips"]) <= 1
    for clip in data["clips"]:
        assert set(clip) == set(item["properties"])  # the grammar leaves nothing out and adds nothing
        assert clip["viral_trigger"] in transcript_parser.VIRAL_TRIGGERS
        assert clip["pacing_style"] in transcript_parser.PACING_STYLES
        assert clip["target_duration_tier"] in item["properties"]["target_duration_tier"]["enum"]
        assert clip["hook_rating"] in transcript_parser.RATINGS  # tenths from 1.0 to 10.0, nothing else
        assert len(clip["hook_headline"]) <= 80 and len(clip["reason"]) <= 160
    assert reply.backend == "llama-server" and reply.device == "the CPU" and reply.gpu_layers == 0
    assert any("The local AI is running on the CPU" in m for m in said)
    assert "-dev" in started and "--fit" in started and "-lv" in started  # this build knows them all
    # llama.cpp's own figures, held against ClipMonster's sizing of the same model.
    shape = gguf.read(MODEL)
    kv = planner.kv_bytes(shape, list(range(shape.n_layer)), reply.context, "f16")
    assert _buffer_mib(lines, "KV") == pytest.approx(kv / planner.MIB, rel=0.02)
    assert _buffer_mib(lines, "model") == pytest.approx(shape.weights_bytes / planner.MIB, rel=0.02)


@needs_llama
def test_the_header_of_a_model_llama_cpp_made():
    """tests/local_ai/make_tiny_gguf.py's model: 2 layers, 64 wide, 32-bit weights."""
    shape = gguf.read(MODEL)
    assert (shape.arch, shape.n_layer, shape.n_embd, shape.n_ff, shape.n_head) == ("llama", 2, 64, 128, 4)
    assert shape.kv_heads == [4, 4] and shape.head_dim_k == shape.head_dim_v == 16 and shape.n_ctx_train == 4096
    per_layer = 4 * (2 * 64 + 4 * 64 * 64 + 3 * 128 * 64)  # two norms, attention q k v o, feed-forward x3
    assert shape.layer_bytes == [per_layer, per_layer]
    assert shape.embed_bytes == shape.n_vocab * 64 * 4 and shape.output_bytes == shape.n_vocab * 64 * 4 + 64 * 4
    assert not shape.tied_output and shape.n_vocab > 100_000  # Llama 3's tokenizer


class _Remote:
    """A file on a web server, read from the start through Range requests: only what's asked for."""

    CHUNK = 4 << 20

    def __init__(self, url: str):
        self.url, self.buf, self.at, self.size = url, b"", 0, 0

    def _fetch(self) -> bytes:
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={self.at}-{self.at + self.CHUNK - 1}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            assert r.status == 206, f"{self.url} ignored the Range request ({r.status})"
            total = (r.headers.get("Content-Range") or "").rsplit("/", 1)[-1]
            self.size = int(total) if total.isdigit() else self.size
            data = r.read()
        self.at += len(data)
        return data

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            more = self._fetch()
            if not more:
                break
            self.buf += more
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


@pytest.mark.skipif(not os.environ.get("CLIPMONSTER_CHECK_MODEL_URLS"), reason="reads the catalogue's models")
@pytest.mark.parametrize("key", list(models.MODELS))
def test_every_catalogue_model_is_what_the_catalogue_says(key):
    m = models.MODELS[key]
    remote = _Remote(m.url)
    head = remote.read(8)  # the file's size comes with the first answer
    assert head[:4] == b"GGUF", m.url

    class Stream:
        first = head

        def read(self, n):
            out, Stream.first = Stream.first[:n], Stream.first[n:]
            return out + (remote.read(n - len(out)) if len(out) < n else b"")

    shape = gguf.read_stream(Stream(), remote.size)
    a = m.arch
    assert remote.size == pytest.approx(m.size_gb * 1e9, rel=0.005), (key, remote.size)
    assert (shape.arch, shape.n_layer, shape.n_embd, shape.n_ff, shape.n_head) == (a.arch, a.n_layer, a.n_embd,
                                                                                   a.n_ff, a.n_head), key
    assert shape.kv_heads == [a.n_head_kv] * a.n_layer and shape.head_dim_k == a.head_dim, key
    assert (shape.n_vocab, shape.n_ctx_train, shape.tied_output) == (a.n_vocab, a.n_ctx_train, a.tied), key
    assert shape.file_type == 15, key  # Q4_K_M
    estimate = models.estimate_shape(m)  # what sizing uses before the file is downloaded
    assert sum(estimate.layer_bytes) == pytest.approx(sum(shape.layer_bytes), rel=0.05), key
    assert estimate.output_bytes == pytest.approx(shape.output_bytes, rel=0.25), key

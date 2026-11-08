"""The local AI for real: a llama-server build loading a GGUF model and answering through ClipMonster's
wrapper, held to the clip schema.

Skipped unless CLIPMONSTER_LLAMA_SERVER (a llama-server) and CLIPMONSTER_TEST_GGUF (a model) are set;
the tests workflow builds a CPU llama-server and a tiny random model (tests/local_ai/make_tiny_gguf.py).
A random model's words are nonsense, so this checks the plumbing and the JSON grammar, not the picks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ytclip.intelligence import llama_engine, transcript_parser
from ytclip.settings import Settings

SERVER = os.environ.get("CLIPMONSTER_LLAMA_SERVER", "")
MODEL = os.environ.get("CLIPMONSTER_TEST_GGUF", "")

pytestmark = pytest.mark.skipif(not (SERVER and Path(SERVER).is_file() and MODEL and Path(MODEL).is_file()),
                                reason="needs CLIPMONSTER_LLAMA_SERVER and CLIPMONSTER_TEST_GGUF")


def test_llama_server_answers_in_the_clip_schema():
    s = Settings(ai_provider="local", local_model="custom", local_model_path=MODEL, local_gpu=False,
                 local_context=4096)
    try:
        said = []
        reply = llama_engine.ask_json(s, "Pick the best moment.", "[0.0-4.0] So I quit my job.\n[4.0-9.0] It paid.",
                                      transcript_parser.schema(1), max_tokens=1500, log=said.append)
    finally:
        llama_engine.shutdown()
    data = json.loads(reply.text)
    assert isinstance(data["clips"], list) and len(data["clips"]) <= 1
    for clip in data["clips"]:
        assert set(clip) == set(transcript_parser.schema(1)["properties"]["clips"]["items"]["properties"])
        assert clip["viral_category"] in transcript_parser.CATEGORIES
        assert 1 <= clip["hook_rating_1_to_10"] <= 10
    assert reply.backend == "llama-server" and "CPU" in reply.device

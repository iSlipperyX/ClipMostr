"""The AI router (intelligence/agent_router.py) and the usage count and spending limits (usage.py)."""

import json

import pytest

from ytclip import ai
from ytclip.intelligence import agent_router as R, hardware, usage
from ytclip.settings import Settings

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}, "kind": {"type": "string", "enum": ["a", "b"]}},
          "required": ["n", "kind"], "additionalProperties": False}


@pytest.fixture
def ais(monkeypatch):
    """Claude, Gemini and a local model that's downloaded; each answers what `say` holds and is logged in
    `asked`."""
    asked: list[tuple] = []
    say = {"claude": '{"n": 1, "kind": "a"}', "gemini": '{"n": 2, "kind": "b"}', "local": '{"n": 3, "kind": "a"}'}

    def answer(provider):
        v = say[provider]
        if isinstance(v, Exception):
            raise v
        return v

    def claude(key, model, system, content, schema, effort, max_tokens, check):
        asked.append(("claude", model, effort))
        usage.note("claude", model, 1000, 500)
        return answer("claude"), model

    def gemini(key, model, system, content, schema, effort, max_tokens, check):
        asked.append(("gemini", model, effort))
        usage.note("gemini", model, 2000, 100)
        return answer("gemini"), model

    def local(s, system, content, schema, max_tokens, log, check, progress):
        asked.append(("local", "Nemo", ""))
        if check:
            check()
        return answer("local"), "Nemo"

    monkeypatch.setattr(ai, "_ask_claude", claude)
    monkeypatch.setattr(ai, "_ask_gemini", gemini)
    monkeypatch.setattr(ai, "_ask_local", local)
    monkeypatch.setattr(ai, "local_ready", lambda s: True)
    monkeypatch.setattr(ai, "local_model_name", lambda s: "Nemo")
    real = ai.key_for
    monkeypatch.setattr(ai, "key_for", lambda s, p="": "local" if (p or ai.chosen(s)) == "local" else real(s, p))
    return asked, say


def settings(**kw):
    return Settings(ai_api_key="sk-ant-x", gemini_api_key="g-key", **kw)


def test_local_first_answers_locally_and_costs_nothing(ais):
    asked, _ = ais
    got = R.query_intelligence("q", SCHEMA, settings=settings())
    assert (got.provider, got.tier, got.data) == ("local", "local", {"n": 3, "kind": "a"})
    assert [a[0] for a in asked] == ["local"] and not got.fell_back
    assert got.usage["usd"] == 0 and got.usage["tokens_out"] > 0
    assert usage.spent("day") == 0


def test_local_failure_falls_to_the_chosen_cloud_ai_then_the_other(ais):
    asked, say = ais
    say["local"] = ai.AIError("the graphics card ran out of memory")
    got = R.query_intelligence("q", SCHEMA, settings=settings(ai_provider="gemini"))
    assert got.provider == "gemini" and got.tier == "cloud" and got.tried[0]["provider"] == "local"
    assert "out of memory" in got.tried[0]["error"]
    say["gemini"] = ai.AIError("busy")
    got = R.query_intelligence("q", SCHEMA, settings=settings(ai_provider="gemini"))
    assert got.provider == "claude" and [t["provider"] for t in got.tried] == ["local", "gemini"]
    assert got.usage == {"provider": "claude", "model": "claude-sonnet-5-5", "tokens_in": 1000, "tokens_out": 500,
                         "usd": pytest.approx(0.007), "estimated": False}


def test_a_local_answer_that_misses_the_schema_is_not_trusted(ais):
    asked, say = ais
    say["local"] = '{"n": "three", "kind": "a"}'
    got = R.query_intelligence("q", SCHEMA, settings=settings())
    assert got.provider == "claude" and "whole number" in got.tried[0]["error"]
    say["local"] = '{"n": 3, "kind": "z"}'
    assert "one of a, b" in R.query_intelligence("q", SCHEMA, settings=settings()).tried[0]["error"]


def test_a_slow_local_model_hands_over_to_the_cloud(ais, monkeypatch):
    asked, _ = ais
    stopped = []
    from ytclip.intelligence import llama_engine

    monkeypatch.setattr(llama_engine, "shutdown", lambda: stopped.append(1))
    clock = iter([0.0, 0.0, 999.0, 999.0, 999.0, 999.0])
    monkeypatch.setattr(R.time, "monotonic", lambda: next(clock, 999.0))
    got = R.query_intelligence("q", SCHEMA, settings=settings(), local_timeout=5)
    assert got.provider == "claude" and "longer than 5 s" in got.tried[0]["error"] and stopped == [1]


def test_priorities(ais):
    asked, say = ais
    s = settings()
    got = R.query_intelligence("q", SCHEMA, "cloud_deep", settings=s)
    assert asked[-1] == ("claude", "claude-opus-5-5", "high") and got.tier == "cloud"
    R.query_intelligence("q", SCHEMA, "cloud_first", settings=s)
    assert asked[-1] == ("claude", "claude-sonnet-5-5", "medium")
    say["local"] = ai.AIError("no")
    with pytest.raises(R.RouterError) as e:
        R.query_intelligence("q", SCHEMA, "local_only", settings=s)
    assert [t["provider"] for t in e.value.tried] == ["local"] and "local AI only" in str(e.value)
    say["claude"] = say["gemini"] = ai.AIError("down")
    with pytest.raises(R.RouterError) as e:
        R.query_intelligence("q", SCHEMA, "cloud_only", settings=s)
    assert [t["provider"] for t in e.value.tried] == ["claude", "gemini"]
    with pytest.raises(ValueError):
        R.route(s, "fastest")


def test_plain_text_without_a_schema(ais):
    _, say = ais
    say["local"] = '{"answer": "A red card."}'
    got = R.query_intelligence("What is it?", settings=settings())
    assert got.text == "A red card." and got.data == {"answer": "A red card."}


def test_a_cancel_passes_straight_through(ais):
    class Stop(Exception):
        pass

    def check():
        raise Stop()

    with pytest.raises(Stop):
        R.query_intelligence("q", SCHEMA, settings=settings(), check=check)


def test_the_route_says_what_is_missing(monkeypatch):
    monkeypatch.setattr(ai, "local_ready", lambda s: False)
    steps = R.route(Settings(), "local_first")
    assert [st.provider for st in steps] == ["local", "claude", "gemini"] and not any(st.ready for st in steps)
    assert "API key" in steps[1].why
    assert not R.available(Settings())
    with pytest.raises(R.RouterError, match="No AI could answer"):
        R.query_intelligence("q", SCHEMA, settings=Settings())


def test_usage_file_and_prices(tmp_path):
    f = tmp_path / "ai_usage.json"
    now = 1_794_316_800.0  # 2026-11-10
    usage.note("claude", "claude-opus-5-5", 1_000_000, 100_000, now=now, file=f)
    usage.note("claude", "claude-opus-5-5", 0, 100_000, now=now, file=f)
    usage.note("local", "Nemo", 5000, 500, now=now, file=f)
    data = json.loads(f.read_text())
    day = usage.today(now)
    assert data["days"][day]["claude"]["claude-opus-5-5"] == {"calls": 2, "in": 1_000_000, "out": 200_000, "usd": 8.0}
    assert data["days"][day]["local"]["Nemo"]["usd"] == 0
    assert usage.spent("day", now=now, file=f) == 8.0 and usage.spent("month", now=now + 86400, file=f) == 8.0
    assert usage.spent("day", now=now + 86400, file=f) == 0
    assert usage.price("claude", "claude-opus-5") == (5.0, 25.0, False)
    assert usage.price("claude", "claude-haiku-5-5") == (0.10, 0.50, False)
    assert usage.price("gemini", "models/gemini-3.8-flash", "2026-11-10") == (0.75, 3.75, False)
    assert usage.price("gemini", "gemini-3.8-flash", "2027-01-01") == (1.50, 7.50, False)
    assert usage.price("gemini", "gemini-2.5-flash-lite")[:2] == (0.10, 0.40)
    assert usage.price("gemini", "gemini-9-ultra") == (2.0, 12.0, True)
    assert usage.price("local", "anything") == (0.0, 0.0, False)
    rows = usage.summary(now=now, file=f)
    assert [r["provider"] for r in rows] == ["claude", "local"] and rows[0]["calls"] == 2


def test_your_own_prices_win(isolated_profile):
    from ytclip.settings import app_dir

    (app_dir() / usage.PRICES_FILE).write_text('{"gemini-9-ultra": [3, 9], "claude-opus-5-5": [1, 2], "bad": "x"}')
    assert usage.price("gemini", "gemini-9-ultra") == (3.0, 9.0, False)
    assert usage.price("claude", "claude-opus-5-5") == (1.0, 2.0, False)


def test_the_spending_limit_stops_paid_ais_but_not_the_local_one(ais):
    asked, say = ais
    s = settings(ai_budget_day=0.01, ai_provider="claude")
    assert R.query_intelligence("q", SCHEMA, "cloud_first", settings=s).provider == "claude"  # $0.007 so far
    assert R.query_intelligence("q", SCHEMA, "cloud_first", settings=s).provider == "claude"  # now $0.014
    got = R.query_intelligence("q", SCHEMA, "cloud_first", settings=s)
    assert got.provider == "local" and "spending limit is reached" in got.tried[0]["error"]
    assert "$0.01" in got.tried[0]["error"]
    with pytest.raises(R.RouterError, match="spending limit"):
        R.query_intelligence("q", SCHEMA, "cloud_only", settings=s)
    # the rest of ClipMonster (ai.ask_json) is held to the same limit
    with pytest.raises(ai.AIBudget):
        ai.ask_json(s, "polish", "sys", "q", SCHEMA, only=["claude", "gemini"])
    assert ai.ask_json(s, "polish", "sys", "q", SCHEMA).provider == "local"
    assert usage.status(s).over == "day" and usage.status(settings()).over == ""
    assert usage.status(settings(ai_budget_month=0.01)).over == "month"


def test_an_ai_that_does_not_count_its_tokens_is_estimated(monkeypatch):
    monkeypatch.setattr(ai, "_ask_gemini", lambda *a: ('{"n": 1, "kind": "a"}', "gemini-3.8-flash"))
    ans = ai.ask_json(Settings(gemini_api_key="k", ai_provider="gemini"), "polish", "s" * 400, "q" * 4000, SCHEMA)
    assert ans.usage["tokens_in"] > 800 and ans.usage["usd"] > 0


def test_hardware_stack():
    amd = hardware.Hardware("Windows", 16, 32, "amd", "AMD Radeon RX 9060 XT", 15.9)
    st = hardware.stack(amd, ["DmlExecutionProvider", "CPUExecutionProvider"])
    assert st["backend"] == "vulkan" and st["onnx_providers"] == ["DmlExecutionProvider", "CPUExecutionProvider"]
    assert st["reserve_gb"] == 1.11 and st["memory_for_models_gb"] == 14.8 and "Vulkan" in st["llm"]
    assert "onnxruntime-directml" in hardware.stack(amd, ["CPUExecutionProvider"])["onnx"]
    mac = hardware.Hardware("Darwin", 12, 36, "apple", "Apple M3 Pro GPU", 23.8, chip="Apple M3 Pro")
    st = hardware.stack(mac, ["CoreMLExecutionProvider", "CPUExecutionProvider"])
    assert st["backend"] == "metal" and st["unified_memory"] and st["onnx_providers"][0] == "CoreMLExecutionProvider"
    assert "unified memory" in st["llm"] and mac.describe().startswith("Apple M3 Pro (unified memory)")
    nv = hardware.Hardware("Windows", 24, 64, "nvidia", "NVIDIA GeForce RTX 4090", 24)
    assert hardware.onnx_providers(nv, ["CUDAExecutionProvider", "CPUExecutionProvider"])[0] == "CUDAExecutionProvider"
    assert hardware.onnx_providers(nv, ["DmlExecutionProvider", "CPUExecutionProvider"])[0] == "DmlExecutionProvider"
    cpu = hardware.Hardware("Linux", 8, 16)
    assert hardware.stack(cpu, [])["onnx_providers"] == ["CPUExecutionProvider"] and "CPU" in hardware.stack(cpu, [])["llm"]

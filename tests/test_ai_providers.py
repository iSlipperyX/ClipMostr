"""Claude or Gemini (Settings → AI): the Gemini connection, choosing an AI, falling back to the other one.

No real keys: Gemini is a local HTTP server that answers like Google's API, and Claude a fake client.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from ytclip import ai, doctor, gemini, polish
from ytclip.analyze import Clip
from ytclip.engine import Engine
from ytclip.settings import Settings
from ytclip.subtitles import Cue

ENV = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


@pytest.fixture(autouse=True)
def no_env_keys(monkeypatch):
    for v in ENV:
        monkeypatch.delenv(v, raising=False)


def sse(*chunks: dict) -> list[bytes]:
    out = []
    for c in chunks:
        out += [b"data: " + json.dumps(c).encode() + b"\r\n", b"\r\n"]
    return out


def chunk(text: str = "", finish: str = "", thought: bool = False, model: str = "gemini-3.8-flash-001") -> dict:
    cand: dict = {"index": 0, "content": {"role": "model", "parts": [{"text": text, "thought": thought}]}}
    if finish:
        cand["finishReason"] = finish
    return {"candidates": [cand], "modelVersion": model}


class FakeResp(io.BytesIO):
    def __init__(self, lines: list[bytes]):
        super().__init__(b"".join(lines))


def http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(json.dumps(body).encode()))


class Calls:
    """A stand-in for urlopen: answers in turn, remembering each request."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].data)


SCHEMA = {"type": "object", "properties": {"clips": {"type": "array", "items": {
    "type": "object", "properties": {"start": {"type": "number"}, "title": {"type": "string", "description": "d"}},
    "required": ["start", "title"], "additionalProperties": False}}}, "required": ["clips"],
    "additionalProperties": False}


# ---------------------------------------------------------------- the Gemini request

def test_schema_becomes_gemini_response_schema():
    out = gemini.to_schema(SCHEMA)
    assert out["type"] == "OBJECT" and "additionalProperties" not in out
    item = out["properties"]["clips"]["items"]
    assert item["type"] == "OBJECT" and item["properties"]["start"] == {"type": "NUMBER"}
    assert item["properties"]["title"] == {"type": "STRING", "description": "d"}
    assert item["propertyOrdering"] == ["start", "title"] and item["required"] == ["start", "title"]
    assert "additionalProperties" not in item
    # the real schemas convert too
    assert gemini.to_schema(polish.SCHEMA)["properties"]["clips"]["items"]["properties"]["fixes"]["type"] == "ARRAY"


def test_images_go_as_inline_data():
    parts = gemini.to_parts([{"type": "text", "text": "hi"},
                             {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                          "data": "QUJD"}}])
    assert parts == [{"text": "hi"}, {"inlineData": {"mimeType": "image/jpeg", "data": "QUJD"}}]
    assert gemini.to_parts("plain") == [{"text": "plain"}]


def test_thinking_setting_suits_the_model():
    assert gemini.thinking("gemini-3.8-flash", "low") == {"thinkingLevel": "low"}
    assert gemini.thinking("models/gemini-4-argon", "medium") == {"thinkingLevel": "medium"}
    assert gemini.thinking("gemini-2.5-flash", "low") == {"thinkingBudget": 1024}


def test_generate_streams_the_answer_and_skips_thoughts():
    calls = Calls(FakeResp(sse(chunk("thinking about it", thought=True), chunk('{"clips": '),
                               chunk('[]}', finish="STOP"))))
    reply = gemini.generate(" AIzaKEY ", "gemini-3.8-flash", "sys", "prompt", SCHEMA, effort="low",
                            max_tokens=500, urlopen=calls)
    assert reply.text == '{"clips": []}' and reply.model == "gemini-3.8-flash-001"
    req = calls.requests[0]
    assert req.full_url == (f"{gemini.API_URL}/models/gemini-3.8-flash:streamGenerateContent?alt=sse")
    assert req.get_header("X-goog-api-key") == "AIzaKEY"  # trimmed, and never in the URL
    body = calls.body()
    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "prompt"}]}]
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json" and cfg["responseSchema"]["type"] == "OBJECT"
    assert cfg["thinkingConfig"] == {"thinkingLevel": "low"} and cfg["maxOutputTokens"] == 500


def test_generate_retries_busy_servers_and_rate_limits():
    naps = []
    calls = Calls(http_error(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "overloaded"}}),
                  http_error(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "slow down"}}),
                  urllib.error.URLError("reset"),
                  FakeResp(sse(chunk("{}", finish="STOP"))))
    assert gemini.generate("k", "m", "s", "p", SCHEMA, urlopen=calls, sleep=naps.append).text == "{}"
    assert len(calls.requests) == 4 and sum(naps) == pytest.approx(14)


def test_generate_gives_up_after_three_retries():
    calls = Calls(*[urllib.error.URLError("down")] * 4)
    with pytest.raises(gemini.GeminiError, match="Couldn't reach the Gemini API"):
        gemini.generate("k", "m", "s", "p", SCHEMA, urlopen=calls, sleep=lambda t: None)
    assert len(calls.requests) == 4


def test_generate_asks_again_without_thinking_when_a_model_refuses_it():
    calls = Calls(http_error(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                             "message": "Thinking level is not supported for this model."}}),
                  FakeResp(sse(chunk("{}", finish="STOP"))))
    gemini.generate("k", "gemini-x", "s", "p", SCHEMA, urlopen=calls, sleep=lambda t: None)
    assert "thinkingConfig" in calls.body(0)["generationConfig"]
    assert "thinkingConfig" not in calls.body(1)["generationConfig"]


@pytest.mark.parametrize("code, err, match, kind", [
    (400, {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key.",
           "details": [{"reason": "API_KEY_INVALID"}]}, "key was rejected", "key"),
    (403, {"code": 403, "status": "PERMISSION_DENIED", "message": "denied"}, "isn't allowed to use gm", "key"),
    (404, {"code": 404, "status": "NOT_FOUND", "message": "models/gm is not found"}, "isn't available", ""),
    (429, {"code": 429, "status": "RESOURCE_EXHAUSTED",
           "message": "You exceeded your current quota for the free tier, per day"}, "quota is used up", ""),
    (400, {"code": 400, "status": "FAILED_PRECONDITION", "message": "User location is not supported for the API "
                                                                     "use."}, "free tier", ""),
])
def test_errors_say_what_to_do(code, err, match, kind):
    calls = Calls(http_error(code, {"error": err}))
    with pytest.raises(gemini.GeminiError, match=match) as info:
        gemini.generate("k", "gm", "s", "p", SCHEMA, urlopen=calls, sleep=lambda t: None)
    assert info.value.kind == kind and len(calls.requests) == 1  # none of these is worth retrying


def test_cut_off_and_declined_answers():
    with pytest.raises(gemini.GeminiError, match="cut off") as info:
        gemini.generate("k", "m", "s", "p", SCHEMA, urlopen=Calls(FakeResp(sse(chunk('{"cl', finish="MAX_TOKENS")))))
    assert info.value.kind == "cut_off"
    with pytest.raises(gemini.GeminiError, match="safety filter") as info:
        gemini.generate("k", "m", "s", "p", SCHEMA, urlopen=Calls(FakeResp(sse(chunk("", finish="SAFETY")))))
    assert info.value.kind == "refused"
    blocked = FakeResp(sse({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}))
    with pytest.raises(gemini.GeminiError, match="declined to read this") as info:
        gemini.generate("k", "m", "s", "p", SCHEMA, urlopen=Calls(blocked))
    assert info.value.kind == "refused"


def test_an_error_inside_the_stream_is_reported():
    resp = FakeResp(sse(chunk('{"a"'), {"error": {"code": 503, "status": "UNAVAILABLE", "message": "x"}},
                        ))
    calls = Calls(resp, FakeResp(sse(chunk("{}", finish="STOP"))))
    assert gemini.generate("k", "m", "s", "p", SCHEMA, urlopen=calls, sleep=lambda t: None).text == "{}"


def test_cancel_stops_the_stream():
    class Stop(Exception):
        pass

    def check():
        raise Stop

    with pytest.raises(Stop):
        gemini.generate("k", "m", "s", "p", SCHEMA, check=check,
                        urlopen=Calls(FakeResp(sse(chunk("{}", finish="STOP")))))


def test_no_key_never_calls_google():
    calls = Calls()
    with pytest.raises(gemini.GeminiError, match="No Gemini API key"):
        gemini.generate("  ", "m", "s", "p", SCHEMA, urlopen=calls)
    assert not calls.requests


def test_check_model_looks_the_model_up():
    calls = Calls(FakeResp([json.dumps({"name": "models/gemini-3.8-flash",
                                        "displayName": "Gemini 3.8 Flash"}).encode()]))
    assert gemini.check_model("k", "gemini-3.8-flash", urlopen=calls) == "Gemini 3.8 Flash"
    req = calls.requests[0]
    assert req.get_method() == "GET" and req.full_url.endswith("/v1beta/models/gemini-3.8-flash")


# ---------------------------------------------------------------- a real HTTP round trip

class FakeGoogle(BaseHTTPRequestHandler):
    """Answers like generativelanguage.googleapis.com: GET models/<m>, POST <m>:streamGenerateContent."""
    seen: list = []
    reply: str = '{"clips": []}'

    def log_message(self, *a):
        pass

    def _key_ok(self) -> bool:
        if self.headers.get("x-goog-api-key") == "AIza-good":
            return True
        body = json.dumps({"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid.",
                                     "details": [{"reason": "API_KEY_INVALID"}]}}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self):
        if not self._key_ok():
            return
        name = self.path.rsplit("/", 1)[1]
        body = json.dumps({"name": f"models/{name}", "displayName": name.replace("-", " ").title()}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGoogle.seen.append((self.path, data))
        if not self._key_ok():
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        text = FakeGoogle.reply
        half = len(text) // 2
        for c in (chunk(text[:half]), chunk(text[half:], finish="STOP", model="gemini-3.8-flash-002")):
            self.wfile.write(b"data: " + json.dumps(c).encode() + b"\r\n\r\n")
            self.wfile.flush()


@pytest.fixture
def google(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(gemini, "API_URL", f"http://127.0.0.1:{server.server_address[1]}/v1beta")
    monkeypatch.setattr(gemini, "RETRY_WAITS", ())
    FakeGoogle.seen = []
    yield FakeGoogle
    server.shutdown()


def sents() -> list[Cue]:
    return [Cue(0, 10, "So here's the thing about cats."), Cue(10, 30, "They knock everything off the table."),
            Cue(30, 60, "And then they stare at you like you did it.")]


def test_clip_finder_with_gemini_over_http(google):
    google.reply = json.dumps({"clips": [{"start": 0, "end": 30, "title": "Cats", "reason": "funny", "score": 80},
                                         {"start": 5, "end": 25, "title": "Overlaps", "reason": "x", "score": 90}]})
    s = Settings(ai_provider="gemini", gemini_api_key="AIza-good", ai_api_key="")
    logs = []
    clips, model = ai.find_clips_ai("Cats", 60, sents(), np.zeros(60), 3, 10, 40, "be funny", s, log=logs.append)
    assert [(c.start, c.end, c.title) for c in clips] == [(0, 30, "Cats")] and model == "gemini-3.8-flash-002"
    assert "Asking gemini-3.8-flash to pick clips…" in logs
    path, body = google.seen[0]
    assert path == "/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse"
    prompt = body["contents"][0]["parts"][0]["text"]
    assert "[10.0-30.0] They knock everything off the table." in prompt and "be funny" in prompt
    assert body["systemInstruction"]["parts"][0]["text"] == ai.SYSTEM


def test_check_key_over_http(google):
    s = Settings(ai_provider="gemini", gemini_api_key="AIza-good", gemini_model="gemini-4-argon")
    assert ai.check_key(s) == "Gemini key works. Gemini 4 Argon and Gemini 3.8 Flash are ready."
    with pytest.raises(ai.AIError, match="Gemini API key was rejected"):
        ai.check_key(s.copy(gemini_api_key="AIza-bad"))


def test_polish_with_gemini_sends_the_frames(google, tmp_path, monkeypatch):
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(polish, "grab_frames", lambda src, clips, folder: [frame])
    google.reply = json.dumps({"burned_in_subtitles": False, "clips": [
        {"clip": 1, "hook": "Cats are chaos", "bottom_text": "Agree?", "rating": 8, "rating_note": "",
         "subject_x": 0.3, "fixes": []}]})
    s = Settings(ai_provider="gemini", gemini_api_key="AIza-good", gemini_polish_model="gemini-3.5-flash-lite")
    result = polish.ask("Cats", [Clip(0, 30, 80)], [], Path("x.mp4"), s)
    assert result.clips[0].hook == "Cats are chaos" and result.model == "gemini-3.8-flash-002"
    path, body = google.seen[0]
    assert "gemini-3.5-flash-lite:" in path
    parts = body["contents"][0]["parts"]
    assert {"inlineData": {"mimeType": "image/jpeg", "data": "/9hqcGVn"}} in parts
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}


# ---------------------------------------------------------------- choosing the AI

def test_keys_and_order(monkeypatch):
    s = Settings()
    assert ai.chosen(s) == "claude" and not ai.has_key(s) and ai.order(s) == []
    assert ai.order(s.copy(ai_api_key="sk")) == ["claude"]
    assert ai.order(s.copy(ai_api_key="sk", gemini_api_key="g")) == ["claude", "gemini"]
    assert ai.order(s.copy(ai_api_key="sk", gemini_api_key="g", ai_provider="gemini")) == ["gemini", "claude"]
    assert ai.order(s.copy(ai_api_key="sk", gemini_api_key="g", ai_fallback=False)) == ["claude"]
    # the chosen AI has no key: the other one stands in, unless falling back is off
    assert ai.order(s.copy(ai_provider="gemini", ai_api_key="sk")) == ["claude"]
    assert ai.order(s.copy(ai_provider="gemini", ai_api_key="sk", ai_fallback=False)) == []
    assert ai.chosen(s.copy(ai_provider="nonsense")) == "claude"
    monkeypatch.setenv("GOOGLE_API_KEY", " from-env ")
    assert ai.key_for(s, "gemini") == "from-env" and ai.order(s.copy(ai_provider="gemini")) == ["gemini"]
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-env")
    assert ai.key_for(s, "gemini") == "gemini-env"
    assert ai.key_for(s.copy(gemini_api_key="mine"), "gemini") == "mine"
    assert ai.model_for(s, "find", "gemini") == "gemini-3.8-flash" and ai.model_for(s, "polish") == "claude-sonnet-5-5"


def test_falls_back_to_the_other_ai(monkeypatch):
    def claude_fails(*a):
        raise ai.AIError("Anthropic is rate-limiting this key. Wait a minute and try again.")

    asked = []
    monkeypatch.setattr(ai, "_ask_claude", claude_fails)
    monkeypatch.setattr(ai, "_ask_gemini", lambda key, model, *a: asked.append((key, model)) or ('{"ok": 1}', model))
    logs = []
    s = Settings(ai_api_key="sk", gemini_api_key="g")
    answer = ai.ask_json(s, "polish", "sys", "hi", SCHEMA, log=logs.append, what="polish 1 clip")
    assert answer.data == {"ok": 1} and answer.provider == "gemini" and asked == [("g", "gemini-3.8-flash")]
    assert "Claude didn't work: Anthropic is rate-limiting this key. Wait a minute and try again. Trying Gemini " \
           "instead." in logs
    with pytest.raises(ai.AIError, match="rate-limiting"):
        ai.ask_json(s.copy(ai_fallback=False), "find", "sys", "hi", SCHEMA)

    def gemini_fails(*a):
        raise ai.AIError("Google's servers are busy (error 503). Try again shortly.")

    monkeypatch.setattr(ai, "_ask_gemini", gemini_fails)
    with pytest.raises(ai.AIError, match=r"^Claude: Anthropic is .* Gemini: Google's servers are busy"):
        ai.ask_json(s, "find", "sys", "hi", SCHEMA)


def test_a_cancel_is_not_a_failure(monkeypatch):
    class Cancelled(Exception):
        pass

    def cancel(*a):
        raise Cancelled

    monkeypatch.setattr(ai, "_ask_claude", cancel)
    monkeypatch.setattr(ai, "_ask_gemini", lambda *a: pytest.fail("a cancel must not move on to the other AI"))
    with pytest.raises(Cancelled):
        ai.ask_json(Settings(ai_api_key="sk", gemini_api_key="g"), "find", "s", "p", SCHEMA)


def test_unreadable_answers(monkeypatch):
    monkeypatch.setattr(ai, "_ask_gemini", lambda *a: ("not json", "m"))
    with pytest.raises(ai.AIError, match="Gemini's answer couldn't be read"):
        ai.ask_json(Settings(ai_provider="gemini", gemini_api_key="g"), "find", "s", "p", SCHEMA)
    monkeypatch.setattr(ai, "_ask_gemini", lambda *a: ('{"clips": "nope"}', "m"))
    with pytest.raises(ai.AIError, match="couldn't be read"):
        ai.find_clips_ai("t", 60, sents(), np.zeros(60), 3, 10, 40, "", Settings(ai_provider="gemini",
                                                                                gemini_api_key="g"))


def test_cut_off_finder_answer_suggests_fewer_clips(monkeypatch):
    def cut(*a):
        raise ai.AICutOff("Gemini's answer was cut off before it finished.")

    monkeypatch.setattr(ai, "_ask_gemini", cut)
    with pytest.raises(ai.AICutOff, match="cut off before it finished. Try asking for fewer clips."):
        ai.find_clips_ai("t", 60, sents(), np.zeros(60), 3, 10, 40, "", Settings(ai_provider="gemini",
                                                                                gemini_api_key="g"))


def test_no_key_message_names_the_chosen_ai():
    with pytest.raises(polish.PolishError, match="No Gemini API key set"):
        polish.ask("t", [Clip(0, 10, 80)], [], Path("x.mp4"), Settings(ai_provider="gemini"))


# ---------------------------------------------------------------- Claude still works the same way

class FakeStream:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(range(3))

    def get_final_message(self):
        return self.response


class FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeResponse:
    def __init__(self, text, stop="end_turn"):
        self.content, self.stop_reason, self.model = [FakeBlock(text)], stop, "claude-opus-5-5"


def fake_claude(monkeypatch, response, seen):
    try:
        import anthropic  # noqa: F401
    except ImportError:  # CI installs no anthropic package: errors only need its APIError to exist
        import types

        monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(APIError=type("APIError", (Exception,),
                                                                                           {})))
    class Messages:
        def stream(self, **kw):
            seen.update(kw)
            return FakeStream(response)

    class Client:
        def __init__(self):
            self.beta = type("B", (), {"messages": Messages()})()

    monkeypatch.setattr(ai, "_client", lambda key, **kw: seen.update(key=key) or Client())


def test_claude_request_is_unchanged(monkeypatch):
    seen = {}
    fake_claude(monkeypatch, FakeResponse(json.dumps({"clips": [{"start": 0, "end": 30, "title": "A", "reason": "r",
                                                                 "score": 70}]})), seen)
    clips, model = ai.find_clips_ai("t", 60, sents(), np.zeros(60), 3, 10, 40, "x",
                                    Settings(ai_api_key="sk-ant-1", ai_model="claude-opus-5-5"))
    assert len(clips) == 1 and model == "claude-opus-5-5" and seen["key"] == "sk-ant-1"
    assert seen["model"] == "claude-opus-5-5" and seen["system"] == ai.SYSTEM
    assert seen["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": ai.SCHEMA}}
    assert seen["max_tokens"] == 16000 and seen["extra_body"] == {"fallbacks": "default"}


def test_claude_refusal_and_cut_off(monkeypatch):
    fake_claude(monkeypatch, FakeResponse("", stop="refusal"), {})
    with pytest.raises(ai.AIRefused, match="Claude declined"):
        ai.ask_json(Settings(ai_api_key="sk"), "find", "s", "p", SCHEMA)
    fake_claude(monkeypatch, FakeResponse("{", stop="max_tokens"), {})
    with pytest.raises(ai.AICutOff):
        ai.ask_json(Settings(ai_api_key="sk"), "find", "s", "p", SCHEMA)


# ---------------------------------------------------------------- settings, engine, health check, CLI

def test_old_settings_keep_using_claude(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"ai_api_key": "sk-ant-old", "ai_model": "claude-sonnet-5-5"}), encoding="utf-8")
    s = Settings.load(path)
    assert s.ai_provider == "claude" and ai.order(s) == ["claude"] and s.gemini_model == "gemini-3.8-flash"
    assert Settings().apply({"gemini_api_key": "  AIza-pasted \n"}).gemini_api_key == "AIza-pasted"


def test_engine_names_the_gemini_model():
    p = type("P", (), {"signals": [], "mode_used": "ai", "ai_model": ""})()
    assert Engine(Settings(ai_provider="gemini")).signal_names(p) == ["AI (gemini-3.8-flash)"]
    p.ai_model = "gemini-3.8-flash-002"
    assert Engine(Settings(ai_provider="gemini")).signal_names(p) == ["AI (gemini-3.8-flash-002)"]


def test_health_check_names_the_ai(monkeypatch):
    s = Settings(ai_provider="gemini", ai_api_key="sk", auto_polish=True)
    checks = {c.name: c for c in doctor._keys(s, online=False)}
    assert checks["Gemini key"].level == doctor.WARN and "Claude is used instead" in checks["Gemini key"].detail
    assert checks["Claude key"].level == doctor.GOOD
    checks = {c.name: c for c in doctor._keys(Settings(ai_provider="gemini", auto_polish=True), online=False)}
    assert checks["Gemini key"].level == doctor.WARN and "Not set" in checks["Gemini key"].detail
    monkeypatch.setattr(ai, "check_key", lambda s, p: f"{ai.name(p)} key works.")
    both = Settings(ai_provider="gemini", ai_api_key="sk", gemini_api_key="g")
    names = [c.name for c in doctor._keys(both, online=True) if "key" in c.name and "YouTube" not in c.name]
    assert names == ["Gemini key", "Claude key (if the other fails)"]


def test_cli_ai_flag(monkeypatch):
    from ytclip import cli

    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: Settings()))
    s = cli.settings_from_args(cli.build_parser().parse_args(["x", "--ai", "gemini", "--mode", "ai"]))
    assert s.ai_provider == "gemini" and s.mode == "ai"
    assert cli.settings_from_args(cli.build_parser().parse_args(["x"])).ai_provider == "claude"


def test_engine_finds_clips_with_gemini_when_claude_fails(google, tmp_path, monkeypatch):
    from test_series import _find_project, _score

    from ytclip import engine as engine_mod

    def claude_busy(*a):
        raise ai.AIError("Anthropic's servers are busy (error 529). Try again shortly.")

    monkeypatch.setattr(ai, "_ask_claude", claude_busy)
    monkeypatch.setattr(engine_mod.Engine, "rescore", lambda self, p: setattr(p, "score", _score()))
    google.reply = json.dumps({"clips": [{"start": 40, "end": 74.5, "title": "The story", "reason": "r",
                                          "score": 90}]})
    s = Settings(mode="ai", ai_api_key="sk-ant", gemini_api_key="AIza-good", min_len=20, max_len=60, clip_count=1,
                 trim_silence=False, series_parts=1)
    logs = []
    p = _find_project(tmp_path)
    Engine(s, log=logs.append).find(p, strict=True)
    assert p.mode_used == "ai" and p.ai_model == "gemini-3.8-flash-002" and not p.ai_error
    assert [c.title for c in p.clips] == ["The story"] and 39 < p.clips[0].start < 41
    assert any("Claude didn't work: Anthropic's servers are busy" in m and "Trying Gemini" in m for m in logs)
    # without falling back, the unattended run uses smart signals and says why
    p = _find_project(tmp_path)
    Engine(s.copy(ai_fallback=False), log=logs.append).find(p)
    assert p.mode_used != "ai" and "Anthropic's servers are busy" in p.ai_error

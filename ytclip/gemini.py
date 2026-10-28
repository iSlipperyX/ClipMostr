"""Google's Gemini API, for the AI clip finder and AI polish when Settings → AI uses Gemini.

Plain HTTPS to generativelanguage.googleapis.com (no extra package): one streamed generateContent
call that asks for JSON matching a schema. Requests aren't stored by Google's API between calls.
ai.py turns the prompts into requests and picks Claude or Gemini; this module only talks to Google.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import __version__

API_URL = "https://generativelanguage.googleapis.com/v1beta"
KEY_URL = "https://aistudio.google.com/apikey"
ENV_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")  # the names Google's own tools read

MODELS = (("gemini-4-argon", "Gemini 4 Argon (best)"), ("gemini-3.8-flash", "Gemini 3.8 Flash (fast, recommended)"),
          ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite (cheapest)"))

# Gemini 3 and later take a thinking level; 2.x models take a token budget instead.
BUDGETS = {"low": 1024, "medium": 8192, "high": 24576}
RETRY_WAITS = (2.0, 4.0, 8.0)  # rate limits, busy servers and dropped connections are tried again
BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY", "LANGUAGE"}


class GeminiError(RuntimeError):
    """A plain-English reason the call failed. `retry`: worth trying again in a moment."""

    def __init__(self, message: str, retry: bool = False, kind: str = ""):
        super().__init__(message)
        self.retry, self.kind = retry, kind  # kind: "" | "key" | "refused" | "cut_off" | "thinking"


@dataclass
class Reply:
    text: str
    model: str  # the model version that answered
    finish: str = ""


def model_path(model: str) -> str:
    model = (model or "").strip()
    return model if model.startswith("models/") else f"models/{model}"


def to_schema(schema: dict) -> dict:
    """Our JSON Schemas as the OpenAPI subset generateContent's responseSchema reads: upper-case
    types, no additionalProperties, properties kept in the order we wrote them."""
    out: dict = {}
    for key, value in schema.items():
        if key == "type":
            out["type"] = str(value).upper()
        elif key == "properties":
            out["properties"] = {k: to_schema(v) for k, v in value.items()}
            out["propertyOrdering"] = list(value)
        elif key == "items":
            out["items"] = to_schema(value)
        elif key in ("required", "description", "enum", "minimum", "maximum", "minItems", "maxItems", "format",
                     "nullable"):
            out[key] = value
    return out


def to_parts(content: str | list[dict]) -> list[dict]:
    """A prompt (text, or Anthropic-style text and base64 image blocks) as Gemini parts."""
    if isinstance(content, str):
        return [{"text": content}]
    parts: list[dict] = []
    for block in content:
        if block.get("type") == "text":
            parts.append({"text": block["text"]})
        elif block.get("type") == "image":
            src = block.get("source") or {}
            parts.append({"inlineData": {"mimeType": src.get("media_type", "image/jpeg"), "data": src["data"]}})
    return parts


def thinking(model: str, effort: str) -> dict:
    name = model_path(model).split("/", 1)[1]
    if name.startswith(("gemini-1", "gemini-2")):
        return {"thinkingBudget": BUDGETS.get(effort, BUDGETS["medium"])}
    return {"thinkingLevel": effort if effort in BUDGETS else "medium"}


def build_request(system: str, content: str | list[dict], schema: dict, model: str, effort: str,
                  max_tokens: int, think: bool = True) -> dict:
    config: dict = {"responseMimeType": "application/json", "responseSchema": to_schema(schema),
                    "maxOutputTokens": int(max_tokens)}
    if think:
        config["thinkingConfig"] = thinking(model, effort)
    return {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": to_parts(content)}],
            "generationConfig": config}


def friendly(status: int, body: bytes, model: str) -> GeminiError:
    """Google's error answer (most specific first) as something a user can act on."""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        data = {}
    if isinstance(data, list):  # streamed calls send the error inside a list
        data = data[0] if data else {}
    err = data.get("error") if isinstance(data, dict) else None
    err = err if isinstance(err, dict) else {}
    message = str(err.get("message") or f"HTTP {status}")
    state = str(err.get("status") or "")
    reasons = {str(d.get("reason")) for d in err.get("details") or [] if isinstance(d, dict) and d.get("reason")}
    low = message.lower()
    if "API_KEY_INVALID" in reasons or "api key not valid" in low or "api key expired" in low or (
            status in (400, 401) and "api key" in low):
        return GeminiError("The Gemini API key was rejected. Check it was copied completely, or make a new one "
                           "at aistudio.google.com/apikey.", kind="key")
    if status == 400 and "thinking" in low:  # a model that doesn't take this thinking setting
        return GeminiError(message, kind="thinking")
    if "location is not supported" in low:
        return GeminiError("Google doesn't offer the Gemini API's free tier where you are. Turn on billing for "
                           "the key's project in Google AI Studio, or use Claude.")
    if status == 403 or state == "PERMISSION_DENIED":
        return GeminiError(f"This key isn't allowed to use {model}. Check the key, or pick another model in "
                           "Settings → AI.", kind="key")
    if status == 404 or state == "NOT_FOUND":
        return GeminiError(f"{model} isn't available to this key. Pick another model in Settings → AI.")
    if status == 429 or state == "RESOURCE_EXHAUSTED":
        quota = "quota" in low and ("day" in low or "free" in low)
        return GeminiError("This Gemini key's quota is used up for now (free keys have a daily limit). Wait, or "
                           "turn on billing in Google AI Studio." if quota
                           else "Google is rate-limiting this key. Wait a minute and try again.", retry=not quota)
    if status >= 500:
        return GeminiError(f"Google's servers are busy (error {status}). Try again shortly.", retry=True)
    return GeminiError(f"The Gemini API returned an error ({status}): {message}")


def _open(url: str, key: str, body: dict | None, timeout: float, urlopen: Callable | None = None):
    headers = {"x-goog-api-key": key, "Accept": "application/json", "User-Agent": f"ClipMonster/{__version__}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    return (urlopen or urllib.request.urlopen)(req, timeout=timeout)


def _unreachable(exc: Exception) -> GeminiError:
    if isinstance(exc, (TimeoutError,)) or "timed out" in str(exc).lower():
        return GeminiError("Gemini stopped answering partway through.", retry=True)
    return GeminiError("Couldn't reach the Gemini API. Check your internet connection.", retry=True)


def check_model(key: str, model: str, timeout: float = 20, urlopen: Callable | None = None) -> str:
    """Look the model up with this key (free: no tokens, no quota). Returns its display name."""
    if not (key or "").strip():
        raise GeminiError("No Gemini API key set. Add one in Settings → AI.", kind="key")
    path = model_path(model)
    try:
        with _open(f"{API_URL}/{urllib.parse.quote(path)}", key.strip(), None, timeout, urlopen) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise friendly(exc.code, exc.read() or b"", model) from exc
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise _unreachable(exc) from exc
    except ValueError as exc:
        raise GeminiError("Google sent an answer that couldn't be read.") from exc
    return str(info.get("displayName") or model)


def _stream(resp, check: Callable[[], None] | None) -> Reply:
    """Read the server-sent events of streamGenerateContent: each `data:` line is a partial answer."""
    text: list[str] = []
    model = finish = blocked = ""
    for i, raw in enumerate(resp):
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        if check and i % 10 == 0:
            check()  # lets Cancel stop a request mid-stream
        if not line.startswith("data:"):
            continue
        try:
            chunk = json.loads(line[5:].strip())
        except ValueError:
            continue
        if chunk.get("error"):
            err = chunk["error"]
            raise friendly(int(err.get("code") or 500), json.dumps({"error": err}).encode(), model)
        model = chunk.get("modelVersion") or model
        blocked = (chunk.get("promptFeedback") or {}).get("blockReason") or blocked
        for cand in chunk.get("candidates") or []:
            if cand.get("index", 0) != 0:
                continue
            finish = cand.get("finishReason") or finish
            for part in (cand.get("content") or {}).get("parts") or []:
                if part.get("text") and not part.get("thought"):
                    text.append(part["text"])
    if blocked:
        finish = finish or "SAFETY"
        if not text:
            raise GeminiError(f"Gemini declined to read this ({str(blocked).lower().replace('_', ' ')}).",
                              kind="refused")
    return Reply("".join(text), model, finish)


def generate(key: str, model: str, system: str, content: str | list[dict], schema: dict, *,
             effort: str = "medium", max_tokens: int = 16000, timeout: float = 120.0,
             check: Callable[[], None] | None = None, urlopen: Callable | None = None,
             sleep: Callable[[float], None] = time.sleep) -> Reply:
    """One streamed JSON answer from `model`. `timeout` is how long a quiet stream may stall.
    Raises GeminiError (kind "refused" or "cut_off" for answers that came back unusable)."""
    if not (key or "").strip():
        raise GeminiError("No Gemini API key set. Add one in Settings → AI.", kind="key")
    url = f"{API_URL}/{urllib.parse.quote(model_path(model))}:streamGenerateContent?alt=sse"
    think, waits = True, list(RETRY_WAITS)
    while True:
        try:
            with _open(url, key.strip(), build_request(system, content, schema, model, effort, max_tokens, think),
                       timeout, urlopen) as resp:
                reply = _stream(resp, check)
            break
        except urllib.error.HTTPError as exc:
            err = friendly(exc.code, exc.read() or b"", model)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            err = _unreachable(exc)
        except GeminiError as exc:
            err = exc
        if err.kind == "thinking" and think:  # this model takes no thinking setting: ask without one
            think = False
            continue
        if not err.retry or not waits:
            raise err
        wait = waits.pop(0)
        while wait > 0:  # short naps so Cancel still works while waiting to retry
            if check:
                check()
            sleep(min(0.5, wait))
            wait -= 0.5
    reply.model = reply.model or model_path(model).split("/", 1)[1]
    if reply.finish == "MAX_TOKENS":
        raise GeminiError("Gemini's answer was cut off before it finished.", kind="cut_off")
    if reply.finish in BLOCKED:
        raise GeminiError(f"Gemini declined to answer (its {reply.finish.lower().replace('_', ' ')} filter "
                          "triggered).", kind="refused")
    if reply.finish not in ("", "STOP", "FINISH_REASON_UNSPECIFIED"):
        raise GeminiError(f"Gemini stopped early ({reply.finish.lower().replace('_', ' ')}). Try again.")
    return reply

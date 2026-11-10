"""One way to ask an AI, whichever is there: the local model first (free), a cloud one when it can't answer.

    from ytclip.intelligence.agent_router import query_intelligence

    got = query_intelligence("Which of these plays is a red card? …", schema=SCHEMA)       # local, then cloud
    got = query_intelligence(prompt, schema=SCHEMA, priority="cloud_deep")                   # the best cloud model
    got.data, got.provider, got.model, got.tier, got.usage["usd"], got.tried

The router decides the order; ai.ask_json (the one place every AI is called from) does the asking, so the
models, keys, JSON schemas, the Mac's linked PC and the usage count are the ones the rest of ClipMonster uses.

**Tier 1, local (free).** The GGUF model llama.cpp runs on this computer (llama_engine.py: Metal on Apple
silicon, CUDA or Vulkan on a PC's card, else the CPU; hardware.py picks, planner.py sizes it to the memory
that's free), and on a Mac a linked PC's local model before the Mac's own. Its answer is held to the JSON
schema by llama.cpp's grammar, so it can't come back malformed. The local tier is used when its model is
already downloaded (a question never starts a download of several GB) or when Local AI is the AI you chose.

**Tier 2, cloud.** Claude or Gemini with your key: the one Settings → AI names first, then the other. Asked
when the local tier fails (no model, out of memory, an answer that doesn't fit the schema), takes longer than
`local_timeout`, or when the caller asks for the cloud outright.

Priorities:

    local_first   local, then cloud (the default)
    cloud_first   cloud (the quicker model), then local
    cloud_deep    cloud with the best model at high effort, then local: for analysis worth paying for
    local_only    never leaves this computer
    cloud_only    never the local model
    settings      the order Settings → AI gives (what the Studio and automation use)

**Budget.** Every answer's tokens and cost go to ai_usage.json (usage.py). With a daily or monthly limit set
in Settings → AI, a paid AI over the limit isn't asked: the router goes on to the local tier, or raises
RouterError saying the limit is reached.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .. import ai
from . import usage

PRIORITIES = ("local_first", "cloud_first", "cloud_deep", "local_only", "cloud_only", "settings")
PRIORITY_WORDS = {
    "local_first": "the local AI first, a cloud AI if it can't answer",
    "cloud_first": "a cloud AI first, the local AI if it can't answer",
    "cloud_deep": "the best cloud model, thinking hard",
    "local_only": "the local AI only",
    "cloud_only": "a cloud AI only",
    "settings": "the order Settings → AI gives",
}
LOCAL_TIMEOUT = 240.0  # seconds the local tier gets before the cloud is asked instead
TEXT_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
               "additionalProperties": False}
SYSTEM = "You are a careful assistant inside a video clipping app. Answer exactly what is asked, briefly."

Log = Callable[[str], None]


class RouterError(ai.AIError):
    """No tier could answer. `tried` says what each one said."""

    def __init__(self, message: str, tried: list[dict] | None = None):
        super().__init__(message)
        self.tried = tried or []


class _LocalTimeout(Exception):
    """The local tier ran out of time (raised from the `check` hook, so the stream stops)."""


@dataclass
class Step:
    """One AI the router would ask."""

    provider: str  # claude | gemini | local | pc
    tier: str  # local | cloud
    model: str
    ready: bool
    why: str = ""  # when not ready: what's missing

    def to_json(self) -> dict:
        return {"provider": self.provider, "tier": self.tier, "model": self.model, "ready": self.ready,
                "why": self.why}


@dataclass
class Intelligence:
    """An answer, and how it was got."""

    data: dict  # the JSON answer (with no schema: {"answer": "<text>"})
    text: str  # with no schema: the answer's words; else ""
    provider: str
    model: str
    tier: str  # local | cloud
    usage: dict = field(default_factory=dict)  # tokens_in, tokens_out, usd, estimated
    tried: list[dict] = field(default_factory=list)  # the AIs that failed first: {"provider", "error"}
    seconds: float = 0.0

    @property
    def fell_back(self) -> bool:
        return bool(self.tried)

    def to_json(self) -> dict:
        return {"data": self.data, "text": self.text, "provider": self.provider, "model": self.model,
                "tier": self.tier, "usage": self.usage, "tried": self.tried, "seconds": round(self.seconds, 2)}


# ---------------------------------------------------------------- the route


def _settings(settings):
    if settings is not None:
        return settings
    from ..settings import Settings

    return Settings.load()


def _local_steps(s) -> list[Step]:
    out = []
    # A linked PC's local AI before this Mac's own (it's the faster of the two, and it's already loaded).
    if ai.remote() is not None or ai.chosen(s) == "pc":
        ready = bool(ai.key_for(s, "pc"))
        out.append(Step("pc", "local", ai.model_for(s, "polish", "pc"), ready,
                        "" if ready else "your PC isn't linked or isn't answering"))
    if ai.key_for(s, "local"):
        ready = ai.local_ready(s) or ai.chosen(s) == "local"
        out.append(Step("local", "local", ai.local_model_name(s), ready,
                        "" if ready else "its model isn't downloaded yet (ytclip local-ai download)"))
    else:
        from . import llama_engine

        out.append(Step("local", "local", "", False, llama_engine.missing_backend()))
    return out


def _cloud_steps(s, task: str) -> list[Step]:
    first = ai.chosen(s)
    order = ([first] if first in ai.CLOUD else []) + [p for p in ai.CLOUD if p != first]
    out = []
    for p in order:
        has = bool(ai.key_for(s, p))
        why = "" if has else f"no {ai.name(p)} API key (Settings → AI)"
        stopped = usage.guard(s, p) if has else ""
        out.append(Step(p, "cloud", ai.model_for(s, task, p), has and not stopped, stopped or why))
    return out


def route(settings=None, priority: str = "local_first", task: str = "") -> list[Step]:
    """The AIs `priority` would ask, in order, each marked ready or not (and why not)."""
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
    s = _settings(settings)
    task = task or ("find" if priority == "cloud_deep" else "polish")
    local, cloud = _local_steps(s), _cloud_steps(s, task)
    if priority == "local_only":
        return local
    if priority == "cloud_only":
        return cloud
    if priority in ("cloud_first", "cloud_deep"):
        return cloud + local
    if priority == "settings":
        by = {st.provider: st for st in local + cloud}
        wanted = ai.order(s)
        return [by[p] for p in wanted if p in by] + [st for st in local + cloud if st.provider not in wanted]
    return local + cloud


def describe(settings=None, priority: str = "local_first") -> str:
    """'Mistral Nemo 12B (local), then claude-sonnet-5-5 (Claude)', for a log line or the CLI."""
    steps = [st for st in route(settings, priority) if st.ready]
    if not steps:
        return "no AI is set up"
    return ", then ".join(f"{st.model or ai.name(st.provider)} ({'local' if st.tier == 'local' else ai.name(st.provider)})"
                          for st in steps)


# ---------------------------------------------------------------- checking an answer


def conforms(data, schema: dict, where: str = "answer") -> str:
    """"" when `data` has the shape `schema` asks for (types, required keys, enums), else what's wrong. The
    cloud AIs and llama.cpp's grammar enforce the schema themselves; this catches the cases they can't (the
    Python binding without a grammar, a PC running an older version)."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(data, dict):
            return f"{where} should be an object"
        for key in schema.get("required") or []:
            if key not in data:
                return f"{where} has no \"{key}\""
        for key, sub in (schema.get("properties") or {}).items():
            if key in data and isinstance(sub, dict):
                bad = conforms(data[key], sub, f"{where}.{key}")
                if bad:
                    return bad
    elif kind == "array":
        if not isinstance(data, list):
            return f"{where} should be a list"
        sub = schema.get("items")
        if isinstance(sub, dict):
            for i, item in enumerate(data):
                bad = conforms(item, sub, f"{where}[{i}]")
                if bad:
                    return bad
    elif kind == "string":
        if not isinstance(data, str):
            return f"{where} should be text"
    elif kind == "integer":
        if isinstance(data, bool) or not isinstance(data, int):
            if not (isinstance(data, float) and data.is_integer()):
                return f"{where} should be a whole number"
    elif kind == "number":
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            return f"{where} should be a number"
    elif kind == "boolean":
        if not isinstance(data, bool):
            return f"{where} should be true or false"
    allowed = schema.get("enum")
    if allowed is not None and data not in allowed:
        return f"{where} should be one of {', '.join(map(str, allowed))}"
    return ""


# ---------------------------------------------------------------- asking


def query_intelligence(prompt: str | list[dict], schema: dict | None = None, priority: str = "local_first", *,
                       system: str = "", settings=None, task: str = "", effort: str = "", max_tokens: int = 4000,
                       local_timeout: float | None = None, log: Log | None = None,
                       check: Callable[[], None] | None = None, what: str = "",
                       progress: Callable[[str, "float | None"], None] | None = None) -> Intelligence:
    """Ask for JSON matching `schema` (or, with no schema, for a plain answer in `.text`).

    `prompt` is text, or Anthropic-style content blocks (text and base64 JPEG images; the local tier reads the
    text only). `priority` is one of PRIORITIES. `task` picks each cloud AI's model: "polish" (the quicker one,
    the default) or "find" (the best; what cloud_deep uses). `local_timeout` is how long the local tier may take
    before the cloud is asked (default: Settings' ai_local_timeout, 240 s; with local_only there's no limit).
    `check` may raise to cancel; that passes straight through. Raises RouterError when no tier could answer."""
    s = _settings(settings)
    steps = route(s, priority, task)
    task = task or ("find" if priority == "cloud_deep" else "polish")
    effort = effort or ("high" if priority == "cloud_deep" else "medium")
    want = schema or TEXT_SCHEMA
    limit = float(local_timeout if local_timeout is not None else getattr(s, "ai_local_timeout", LOCAL_TIMEOUT)
                  or LOCAL_TIMEOUT)
    more_after_local = any(st.ready and st.tier == "cloud" for st in steps)
    tried: list[dict] = []
    started = time.monotonic()
    for i, st in enumerate(steps):
        if not st.ready:
            tried.append({"provider": st.provider, "error": st.why})
            continue
        if tried and log:
            last = tried[-1]
            log(f"{ai.name(last['provider'])} can't answer ({last['error']}); asking {ai.name(st.provider)}.")
        guard = check
        if st.tier == "local" and limit > 0 and (more_after_local or priority != "local_only"):
            guard = _deadline(check, time.monotonic() + limit)
        try:
            ans = ai.ask_json(s, task, system or SYSTEM, prompt, want, effort=effort, max_tokens=max_tokens,
                              log=log, check=guard, what=what, only=[st.provider], progress=progress)
        except _LocalTimeout:
            tried.append({"provider": st.provider, "error": f"took longer than {limit:.0f} s"})
            _stop_local(st.provider)
            continue
        except ai.AIError as exc:
            tried.append({"provider": st.provider, "error": str(exc)})
            continue
        bad = conforms(ans.data, want)
        if bad:
            tried.append({"provider": st.provider, "error": f"its answer didn't fit what was asked ({bad})"})
            continue
        text = str(ans.data.get("answer", "")) if schema is None else ""
        return Intelligence(ans.data, text, ans.provider, ans.model, st.tier, dict(ans.usage), tried,
                            time.monotonic() - started)
    raise RouterError(_why(tried, priority), tried)


def _deadline(check: Callable[[], None] | None, until: float) -> Callable[[], None]:
    def guard() -> None:
        if check is not None:
            check()
        if time.monotonic() > until:
            raise _LocalTimeout()

    return guard


def _stop_local(provider: str) -> None:
    """A local model that ran out of time is still writing: stop it so the next question starts clean."""
    if provider != "local":
        return
    try:
        from . import llama_engine

        llama_engine.shutdown()
    except Exception:  # noqa: BLE001 - stopping is a courtesy; the next start replaces it anyway
        pass


def _why(tried: list[dict], priority: str) -> str:
    if not tried:
        return "No AI is set up. Add a Claude or Gemini key, or download a local model, in Settings → AI."
    said = " ".join(f"{ai.name(t['provider'])}: {t['error'].rstrip('.')}." for t in tried)
    return f"No AI could answer ({PRIORITY_WORDS[priority]}). {said}"


def available(settings=None, priority: str = "local_first") -> bool:
    """Whether any AI on this route could be asked right now."""
    try:
        return any(st.ready for st in route(settings, priority))
    except Exception:  # noqa: BLE001 - settings unreadable: treat as none
        return False

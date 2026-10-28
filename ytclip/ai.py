"""AI clip finder: the AI (Claude or Gemini, Settings → AI) reads the timestamped transcript and picks the
best moments. Also the one place both AIs are called from: ask_json, check_key, has_key."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .analyze import Clip, clip_score
from .organize import stamp
from .subtitles import Cue


class AIError(RuntimeError):
    """A plain-English reason the AI clip finder couldn't produce clips."""


SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "description": "Start time in seconds"},
                    "end": {"type": "number", "description": "End time in seconds"},
                    "title": {"type": "string", "description": "Short, catchy clip title (max 8 words)"},
                    "reason": {"type": "string", "description": "One sentence on why this moment works"},
                    "score": {"type": "integer", "description": "Predicted appeal, 1-100"},
                },
                "required": ["start", "end", "title", "reason", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are an expert short-form video editor. You receive a video transcript with one sentence per "
    "line, each prefixed with its [start-end] time in seconds, plus moments where viewers rewatched or "
    "the audio spiked. Choose clips that stand on their own: they open at the beginning of a thought "
    "(including any setup the payoff depends on), contain the complete payoff, and end right after it "
    "lands, before the speaker moves on to something else. Prefer moments with a strong hook in the "
    "first few seconds. A clip's start must be the start time of a transcript line and its end must be "
    "the end time of a transcript line. Clips must not overlap."
)


# A stream that goes this long without a byte has stalled (the API sends keep-alive pings much more
# often): give up on that try instead of the SDK's default 10 minutes, which held automation up.
STREAM_TIMEOUT = 120.0

# Settings → AI chooses which AI answers; each has its own key and models.
PROVIDERS = (("claude", "Claude (Anthropic)"), ("gemini", "Gemini (Google)"))
NAMES = {"claude": "Claude", "gemini": "Gemini"}
ENV_KEYS = {"claude": ("ANTHROPIC_API_KEY",), "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY")}
KEY_FIELDS = {"claude": "ai_api_key", "gemini": "gemini_api_key"}
# task -> the setting holding each AI's model for it: "find" wants the best model, "polish" a quicker one
MODEL_FIELDS = {"find": {"claude": "ai_model", "gemini": "gemini_model"},
                "polish": {"claude": "ai_polish_model", "gemini": "gemini_polish_model"}}


class AIRefused(AIError):
    """The AI declined to answer (a safety filter)."""


class AICutOff(AIError):
    """The answer ran out of room before it finished."""


@dataclass
class Answer:
    data: dict
    model: str  # the model that actually answered
    provider: str  # "claude" | "gemini"


def chosen(s) -> str:
    """The AI Settings → AI picked."""
    p = str(getattr(s, "ai_provider", "claude") or "claude").lower()
    return p if p in NAMES else "claude"


def name(s_or_provider) -> str:
    """'Claude' or 'Gemini', for messages."""
    p = s_or_provider if isinstance(s_or_provider, str) else chosen(s_or_provider)
    return NAMES.get(p, "AI")


def key_for(s, provider: str = "") -> str:
    """That AI's key from settings, else from its environment variable."""
    provider = provider or chosen(s)
    own = str(getattr(s, KEY_FIELDS[provider], "") or "").strip()
    return own or next((os.environ[v].strip() for v in ENV_KEYS[provider] if os.environ.get(v, "").strip()), "")


def model_for(s, task: str = "find", provider: str = "") -> str:
    return str(getattr(s, MODEL_FIELDS[task][provider or chosen(s)]))


def order(s) -> list[str]:
    """The AIs to ask, in turn: the chosen one when it has a key, then (with "If it fails, try the other
    AI" on) the other one when it has a key."""
    first = chosen(s)
    out = [first] if key_for(s, first) else []
    if getattr(s, "ai_fallback", True):
        out += [p for p in NAMES if p != first and key_for(s, p)]
    return out


def has_key(s) -> bool:
    """Whether any AI can be asked: the AI clip finder, polish and special requests need one."""
    return bool(order(s))


def missing_key(s) -> str:
    """What to tell someone with no usable key."""
    return f"No {name(s)} API key set. Add one in Settings → AI."


def ask_json(s, task: str, system: str, content: str | list[dict], schema: dict, *, effort: str = "medium",
             max_tokens: int = 16000, log: Callable[[str], None] | None = None,
             check: Callable[[], None] | None = None, what: str = "") -> Answer:
    """Ask the AI Settings → AI chose (`s` is a Settings) for JSON matching `schema`.

    `task` is "find" or "polish" and picks the model setting (the best model, or the quicker one).
    `content` is text, or Anthropic-style blocks (text, and base64 JPEG images), which work for both AIs.
    `what` finishes the log line "Asking <model> to …". `check` is called while the answer streams in
    and may raise to cancel; that exception passes straight through. If the chosen AI fails and the
    other one has a key (and ai_fallback is on), the other one is asked. Raises AIError."""
    providers = order(s)
    if not providers:
        raise AIError(missing_key(s))
    failures: list[tuple[str, AIError]] = []
    for provider in providers:
        model = model_for(s, task, provider)
        if failures and log:
            log(f"{name(failures[-1][0])} didn't work: {failures[-1][1]} Trying {name(provider)} instead.")
        if log and what:
            log(f"Asking {model} to {what}…")
        try:
            if provider == "gemini":
                text, used = _ask_gemini(key_for(s, provider), model, system, content, schema, effort, max_tokens,
                                         check)
            else:
                text, used = _ask_claude(key_for(s, provider), model, system, content, schema, effort, max_tokens,
                                         check)
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise AIError(f"{name(provider)}'s answer couldn't be read. Try again.") from exc
            if not isinstance(data, dict):
                raise AIError(f"{name(provider)}'s answer couldn't be read. Try again.")
            return Answer(data, used or model, provider)
        except AIError as exc:
            failures.append((provider, exc))
    if len(failures) == 1:
        raise failures[0][1]
    kind = type(failures[0][1]) if len({type(e) for _, e in failures}) == 1 else AIError
    raise kind(" ".join(f"{name(p)}: {e}" for p, e in failures))


def _ask_claude(key: str, model: str, system: str, content, schema: dict, effort: str, max_tokens: int,
                check) -> tuple[str, str]:
    try:
        import anthropic
    except ImportError as exc:  # a source install without the "ai" extra
        raise AIError("Claude needs the anthropic package (pip install anthropic).") from exc

    messages = [{"role": "user", "content": content}]
    try:
        # Streaming keeps long transcripts clear of HTTP timeouts. `fallbacks: "default"`
        # lets the API re-run a safety-declined request on its recommended fallback model.
        with _client(key, max_retries=3, timeout=STREAM_TIMEOUT).beta.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
        ) as stream:
            for i, _event in enumerate(stream):
                if check and i % 25 == 0:
                    check()  # lets Cancel stop a request mid-stream
            response = stream.get_final_message()
    except (anthropic.APIError, TypeError) as exc:
        raise _friendly(exc, model) from exc

    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        raise AIRefused("Claude declined to answer" + (f" (its {category} safety filter triggered)" if category
                                                       else "") + ".")
    if response.stop_reason == "max_tokens":
        raise AICutOff("Claude's answer was cut off before it finished.")
    # After a mid-stream fallback the answer continues in a second text block, so join them all.
    return "".join(b.text for b in response.content if b.type == "text"), getattr(response, "model", None) or model


def _ask_gemini(key: str, model: str, system: str, content, schema: dict, effort: str, max_tokens: int,
                check) -> tuple[str, str]:
    from . import gemini

    try:
        # Gemini counts its thinking inside the output limit, so it gets more room.
        reply = gemini.generate(key, model, system, content, schema, effort=effort, max_tokens=max_tokens * 2,
                                timeout=STREAM_TIMEOUT, check=check)
    except gemini.GeminiError as exc:
        kind = {"refused": AIRefused, "cut_off": AICutOff}.get(exc.kind, AIError)
        raise kind(str(exc)) from exc
    return reply.text, reply.model


def _client(api_key: str, **kwargs):
    import anthropic

    # A stray space from copy-paste would otherwise fail as a confusing "connection error".
    return anthropic.Anthropic(api_key=(api_key or "").strip() or None, **kwargs)


def _friendly(exc: Exception, model: str) -> AIError:
    """Translate SDK errors (most specific first) into messages a user can act on."""
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return AIError("The Anthropic API key was rejected. Check it was copied completely (it starts with "
                       "sk-ant-).")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return AIError(f"This key isn't allowed to use {model}.")
    if isinstance(exc, anthropic.NotFoundError):
        return AIError(f"{model} isn't available to this key. Pick another model in Settings → AI.")
    if isinstance(exc, anthropic.RateLimitError):
        return AIError("Anthropic is rate-limiting this key. Wait a minute and try again.")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return AIError(f"Anthropic's servers are busy (error {exc.status_code}). Try again shortly.")
        return AIError(f"The API returned an error ({exc.status_code}): {getattr(exc, 'message', exc)}")
    if isinstance(exc, anthropic.APIConnectionError):
        return AIError("Couldn't reach the Anthropic API. Check your internet connection.")
    if isinstance(exc, TypeError) and "authentication" in str(exc).lower():
        return AIError("No Anthropic API key set. Add one in Settings → AI.")
    return AIError(str(exc) or exc.__class__.__name__)


def check_key(s, provider: str = "") -> str:
    """Confirm that AI's key works and can use both its models (finding and polishing) by looking
    them up. Costs no tokens. Raises AIError."""
    provider = provider or chosen(s)
    key = key_for(s, provider)
    if not key:
        raise AIError(f"No {name(provider)} API key set.")
    models = list(dict.fromkeys(model_for(s, task, provider) for task in MODEL_FIELDS))
    found = [_lookup(provider, key, m) for m in models]
    return f"{name(provider)} key works. {' and '.join(found)} {'is' if len(found) == 1 else 'are'} ready."


def _lookup(provider: str, key: str, model: str) -> str:
    if provider == "gemini":
        from . import gemini

        try:
            return gemini.check_model(key, model)
        except gemini.GeminiError as exc:
            raise AIError(str(exc)) from exc
    import anthropic

    try:
        return _client(key, max_retries=1, timeout=20).models.retrieve(model).display_name
    except (anthropic.APIError, TypeError) as exc:
        raise _friendly(exc, model) from exc


def _transcript(sents: list[Cue]) -> str:
    """One sentence per line with its exact start and end, so clips can begin and end
    on real sentence boundaries instead of somewhere inside a chunk."""
    return "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in sents if s.text.strip())


def _hotspots(score: np.ndarray, n: int = 12) -> str:
    if not len(score):
        return "none"
    order = np.argsort(score)[::-1]
    picked: list[int] = []
    for t in order:
        if all(abs(int(t) - p) > 20 for p in picked):
            picked.append(int(t))
        if len(picked) >= n:
            break
    return ", ".join(f"{stamp(t)} ({t}s, {score[t] * 100:.0f})" for t in sorted(picked))


def find_clips_ai(title: str, duration: float, sents: list[Cue], score: np.ndarray, count: int,
                  min_len: float, max_len: float, instructions: str, s,
                  log: Callable[[str], None] | None = None,
                  check: Callable[[], None] | None = None) -> tuple[list[Clip], str]:
    """Ask the AI for clips. `sents` is the transcript split into sentences (see
    subtitles.sentences); `s` is the Settings, which choose the AI, its key and model. `check` is
    called while the answer streams in and may raise to cancel. Returns the clips and the model
    that actually answered."""
    if not sents:
        raise AIError("AI mode needs a transcript (captions or Whisper transcription).")
    prompt = (
        f"Video: {title}\nLength: {stamp(duration)} ({duration:.0f}s)\n\n"
        f"Pick up to {count} clips. Each clip must be between {min_len:.0f} and {max_len:.0f} seconds long "
        f"(end minus start).\n"
        f"What the editor wants: {instructions}\n\n"
        f"Signal hotspots (time, seconds, interest 0-100): {_hotspots(score)}\n\n"
        f"Transcript:\n{_transcript(sents)}"
    )
    try:
        answer = ask_json(s, "find", SYSTEM, prompt, SCHEMA, effort="medium", max_tokens=16000, log=log,
                          check=check, what="pick clips")
    except AICutOff as exc:
        raise AICutOff(f"{exc} Try asking for fewer clips.") from exc
    who = name(answer.provider)
    items = answer.data.get("clips") or []
    if not isinstance(items, list):
        raise AIError(f"{who}'s answer couldn't be read. Try again.")

    clips: list[Clip] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            start = max(0.0, float(item["start"]))
            end = min(duration, float(item["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end - start < 3:
            continue
        if end - start > max_len * 1.25:
            end = start + max_len
        if any(start < c.end and end > c.start for c in clips):
            continue
        try:
            rating = float(item.get("score", 0))
        except (TypeError, ValueError):
            rating = 0.0
        clips.append(Clip(start, end, rating, str(item.get("title") or ""), origin="ai",
                          reason=str(item.get("reason") or "")))
    if not clips:
        raise AIError(f"{who} suggested {len(items)} clip{'s' if len(items) != 1 else ''} but none fit inside "
                      "the video." if items
                      else f"{who} didn't find any moments worth clipping in this video.")
    if log and len(clips) < len(items):
        log(f"Skipped {len(items) - len(clips)} AI suggestion(s) that overlapped or fell outside the video.")
    # Report the signal score alongside the AI's own rating so both are comparable.
    for c in clips:
        sig = clip_score(score, c.start, c.end) if len(score) else c.score
        c.score = round((c.score + sig) / 2, 1)
    clips.sort(key=lambda c: c.score, reverse=True)
    return clips[:count], answer.model

"""AI clip finder: Claude reads the timestamped transcript and picks the best moments."""

from __future__ import annotations

import json
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


def _client(api_key: str, **kwargs):
    import anthropic

    # A stray space from copy-paste would otherwise fail as a confusing "connection error".
    return anthropic.Anthropic(api_key=(api_key or "").strip() or None, **kwargs)


def _friendly(exc: Exception, model: str) -> AIError:
    """Translate SDK errors (most specific first) into messages a user can act on."""
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return AIError("The API key was rejected. Check it was copied completely (it starts with sk-ant-).")
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
        return AIError("No API key set. Add one in Settings → AI.")
    return AIError(str(exc) or exc.__class__.__name__)


def check_key(api_key: str, model: str) -> str:
    """Confirm the key works and can use `model` by looking the model up (costs no tokens)."""
    import anthropic

    try:
        info = _client(api_key, max_retries=1, timeout=20).models.retrieve(model)
    except (anthropic.APIError, TypeError) as exc:
        raise _friendly(exc, model) from exc
    return f"Key works. {info.display_name} is ready."


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
                  min_len: float, max_len: float, instructions: str, api_key: str, model: str,
                  log: Callable[[str], None] | None = None,
                  check: Callable[[], None] | None = None) -> tuple[list[Clip], str]:
    """Ask Claude for clips. `sents` is the transcript split into sentences (see
    subtitles.sentences). `check` is called while the answer streams in and may raise
    to cancel. Returns the clips and the model that actually answered."""
    import anthropic

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
    if log:
        log(f"Asking {model} to pick clips…")

    try:
        # Streaming keeps long transcripts clear of HTTP timeouts. `fallbacks: "default"`
        # lets the API re-run a safety-declined request on its recommended fallback model.
        with _client(api_key, max_retries=3, timeout=STREAM_TIMEOUT).beta.messages.stream(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
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
        raise AIError("Claude declined to analyse this transcript"
                      + (f" (its {category} safety filter triggered)" if category else "") + ".")
    if response.stop_reason == "max_tokens":
        raise AIError("Claude's answer was cut off before it finished. Try asking for fewer clips.")
    # After a mid-stream fallback the answer continues in a second text block, so join them all.
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        items = json.loads(text).get("clips", [])
    except (ValueError, AttributeError) as exc:
        raise AIError("Claude's answer couldn't be read. Try again.") from exc

    clips: list[Clip] = []
    for item in items:
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
        clips.append(Clip(start, end, float(item.get("score", 0)), item.get("title", ""), origin="ai",
                          reason=item.get("reason", "")))
    if not clips:
        raise AIError(f"Claude suggested {len(items)} clip{'s' if len(items) != 1 else ''} but none fit inside "
                      "the video." if items
                      else "Claude didn't find any moments worth clipping in this video.")
    if log and len(clips) < len(items):
        log(f"Skipped {len(items) - len(clips)} AI suggestion(s) that overlapped or fell outside the video.")
    # Report the signal score alongside Claude's own rating so both are comparable.
    for c in clips:
        sig = clip_score(score, c.start, c.end) if len(score) else c.score
        c.score = round((c.score + sig) / 2, 1)
    clips.sort(key=lambda c: c.score, reverse=True)
    return clips[:count], getattr(response, "model", None) or model

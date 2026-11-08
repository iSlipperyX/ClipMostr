"""Hook and clip discovery with the local model.

A local model has far less room than Claude or Gemini (16,000 tokens here, against hundreds of thousands),
so a long transcript is read in overlapping windows: each window is asked for its best few moments,
and the answers are pooled, deduplicated and ranked by the model's hook rating blended with the video's
own signals (where viewers rewatched, where the audio spiked). Whisper's word timings give every
sentence an exact start and end; the model has to answer with those times, and anything it gets a
little wrong is moved onto the nearest real sentence edge.

Each pick carries a viral category (Controversy, Curiosity, Story, Humor, Shock), which the editing
uses: stories and curiosity get a slow push-in, the others punchier cuts.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from ..analyze import Clip, clip_score
from ..organize import stamp
from ..subtitles import Cue
from . import llama_engine

CATEGORIES = ("Controversy", "Curiosity", "Story", "Humor", "Shock")
PAYOFFS = ("punchline", "reveal", "lesson", "twist", "reaction", "argument", "answer")
PER_WINDOW = 4  # moments asked of each window
OVERLAP = 4  # sentences each window shares with the one before, so no moment is split between two

SYSTEM = (
    "You are an expert short-form video editor finding moments in a long video that work as standalone "
    "vertical clips. You get part of a transcript, one sentence per line, each with its [start-end] time "
    "in seconds. Pick moments that open at the beginning of a thought, include any setup the payoff "
    "needs, contain the complete payoff and end right after it lands. A strong hook in the first three "
    "seconds matters most. start_time must be the start time of a transcript line and end_time the end "
    "time of a line. Moments must not overlap. Rate hooks honestly: 9-10 only for moments that would stop "
    "almost anyone scrolling."
)


def schema(count: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "minItems": 0,
                "maxItems": max(1, count),
                "items": {
                    "type": "object",
                    "properties": {
                        "start_time": {"type": "number", "description": "Start, in seconds: a line's start time"},
                        "end_time": {"type": "number", "description": "End, in seconds: a line's end time"},
                        "title": {"type": "string", "maxLength": 80,
                                  "description": "A short, catchy title, at most 8 words"},
                        "hook_text": {"type": "string", "maxLength": 160,
                                      "description": "The opening words that grab attention, as said"},
                        "hook_rating_1_to_10": {"type": "integer", "minimum": 1, "maximum": 10,
                                                "description": "How strongly the opening stops someone scrolling"},
                        "payoff_type": {"type": "string", "enum": list(PAYOFFS),
                                        "description": "What the clip pays off with"},
                        "viral_category": {"type": "string", "enum": list(CATEGORIES),
                                           "description": "What kind of moment it is"},
                        "reason": {"type": "string", "maxLength": 200, "description": "One sentence on why it works"},
                    },
                    "required": ["start_time", "end_time", "title", "hook_text", "hook_rating_1_to_10",
                                 "payoff_type", "viral_category", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["clips"],
        "additionalProperties": False,
    }


def lines(sents: list[Cue]) -> list[str]:
    return [f"[{c.start:.1f}-{c.end:.1f}] {c.text}" for c in sents if c.text.strip()]


def windows(sents: list[Cue], budget_tokens: int, overlap: int = OVERLAP) -> list[list[Cue]]:
    """The transcript in pieces that each fit `budget_tokens`, sharing `overlap` sentences with the one
    before."""
    sents = [c for c in sents if c.text.strip()]
    out: list[list[Cue]] = []
    i = 0
    while i < len(sents):
        used, j = 0, i
        while j < len(sents):
            cost = llama_engine.estimate_tokens(f"[{sents[j].start:.1f}-{sents[j].end:.1f}] {sents[j].text}\n")
            if used + cost > budget_tokens and j > i:
                break
            used += cost
            j += 1
        out.append(sents[i:j])
        if j >= len(sents):
            break
        i = max(i + 1, j - overlap)
    return out


def _nearest(values: np.ndarray, t: float) -> float:
    return float(values[np.argmin(np.abs(values - t))]) if len(values) else t


def to_clips(items: list, sents: list[Cue], duration: float, max_len: float) -> list[Clip]:
    """The model's moments as clips, with edges moved onto real sentence edges."""
    starts = np.array([c.start for c in sents])
    ends = np.array([c.end for c in sents])
    out: list[Clip] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            a, b = float(item["start_time"]), float(item["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        a, b = _nearest(starts, a), _nearest(ends, b)
        a, b = max(0.0, a), min(duration, b)
        if b - a < 3:
            continue
        if b - a > max_len * 1.25:
            b = _nearest(ends[ends <= a + max_len], a + max_len) if np.any(ends <= a + max_len) else a + max_len
        try:
            rating = min(10, max(1, int(item.get("hook_rating_1_to_10") or 5)))
        except (TypeError, ValueError):
            rating = 5
        category = str(item.get("viral_category") or "")
        clip = Clip(a, b, float(rating * 10), str(item.get("title") or "").strip(), origin="ai",
                    reason=str(item.get("reason") or "").strip(), category=category if category in CATEGORIES else "")
        clip.hook = str(item.get("hook_text") or "").strip()[:80]
        out.append(clip)
    return out


def find_clips(title: str, duration: float, sents: list[Cue], score: np.ndarray, count: int, min_len: float,
               max_len: float, instructions: str, s, log: Callable[[str], None] | None = None,
               check: Callable[[], None] | None = None,
               progress: Callable[[str, "float | None"], None] | None = None) -> tuple[list[Clip], str]:
    """Clips picked by the local model. Returns the clips (best first) and the model's name.
    Raises llama_engine.LocalAIError."""
    log = log or (lambda m: None)
    if not sents:
        raise llama_engine.LocalAIError("The local AI needs a transcript (captions or Whisper).")
    budget = llama_engine.room(s)
    parts = windows(sents, budget)
    if log and len(parts) > 1:
        log(f"The local AI reads this video in {len(parts)} parts.")
    pool: list[Clip] = []
    model = ""
    per = min(PER_WINDOW, max(1, count))
    for n, part in enumerate(parts, 1):
        prompt = (f"Video: {title}\nLength: {stamp(duration)} ({duration:.0f}s)\n"
                  + (f"Part {n} of {len(parts)} of the transcript.\n" if len(parts) > 1 else "")
                  + f"Pick up to {per} moments, each {min_len:.0f} to {max_len:.0f} seconds long "
                    f"(end_time minus start_time).\nWhat the editor wants: {instructions}\n\n"
                  + "Transcript:\n" + "\n".join(lines(part)))
        if progress:
            progress("The local AI is picking clips", (n - 1) / len(parts))
        try:
            reply = llama_engine.ask_json(s, SYSTEM, prompt, schema(per), max_tokens=320 * per + 200, log=log,
                                          check=check, progress=progress)
        except llama_engine.LocalCutOff:
            log(f"The local AI's answer for part {n} was cut off; skipped that part.")
            continue
        model = reply.model
        import json

        try:
            data = json.loads(reply.text)
        except ValueError:
            log(f"The local AI's answer for part {n} couldn't be read; skipped that part.")
            continue
        pool += to_clips((data or {}).get("clips") or [], part, duration, max_len)
    if not pool:
        raise llama_engine.LocalAIError("The local AI didn't find any moments worth clipping in this video.")
    # Blend its rating with the signals, then keep the best that don't overlap.
    for c in pool:
        sig = clip_score(score, c.start, c.end) if len(score) else c.score
        c.score = round(0.6 * c.score + 0.4 * sig, 1)
    kept: list[Clip] = []
    for c in sorted(pool, key=lambda c: c.score, reverse=True):
        if any(c.start < k.end and c.end > k.start for k in kept):
            continue
        kept.append(c)
        if len(kept) >= count:
            break
    return kept, model

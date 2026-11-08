"""Hook and clip discovery with the local model.

With a long context (up to 32,768 tokens for Qwen2.5, Mistral Nemo and Command R) a whole video's
transcript is usually read in one go. A longer one, or a smaller model's shorter memory, is read in
overlapping windows: each window is asked for its best few moments, and the answers are pooled,
deduplicated and ranked by the model's hook rating blended with the video's own signals (where viewers
rewatched, where the audio spiked). A window that turns out too long for the model (the card failed and
the CPU took over with a shorter memory) is split in two and asked again.

Whisper's word timings give every sentence an exact start and end; the model has to answer with those
times, and anything it gets a little wrong is moved onto the nearest real sentence edge.

The answer is held by a grammar to this shape (ClipMonster 3.2's clip schema):

    {"clips": [{"start_time": 142.5, "end_time": 218.0,
                "hook_headline": "The Exact Moment Everything Went Wrong", "hook_rating": 9.4,
                "category": "Gaming", "pacing_style": "Dramatic Push-in", "viral_trigger": "Shock/Disbelief",
                "target_duration_tier": "Creator_Rewards_Eligible", "reason": "..."}]}

hook_headline becomes the clip's title and hook, hook_rating (1.0 to 10.0 in tenths: the grammar allows
nothing else) its score, viral_trigger its editing category (Shock, Curiosity, Story, Humor or
Controversy, which picks its music and cut style), pacing_style whether it gets the slow push-in, category
the topic on its lower third when the video has no category of yours, and a clip meant for Creator Rewards
is lengthened to over a minute when the transcript allows it.
"""

from __future__ import annotations

import json
import math
from typing import Callable

import numpy as np

from ..analyze import Clip, clip_score
from ..organize import stamp
from ..subtitles import Cue
from . import llama_engine

CATEGORIES = ("Controversy", "Curiosity", "Story", "Humor", "Shock")  # the editing categories (rendering/plan.py)
# What made the moment work, as the model names it, and the editing category each one gets.
VIRAL_TRIGGERS = {
    "Shock/Disbelief": "Shock",
    "Curiosity Gap": "Curiosity",
    "Suspense/Reveal": "Curiosity",
    "Humor": "Humor",
    "Controversy/Debate": "Controversy",
    "Hot Take": "Controversy",
    "Storytelling": "Story",
    "Relatable": "Story",
    "Inspiration": "Story",
}
# How the clip should be cut; "Dramatic Push-in" gets the slow push-in (Settings → Editing → Slow zoom).
PACING_STYLES = ("Dramatic Push-in", "Punchy Cuts", "Steady")
PUSH_IN = "Dramatic Push-in"
# Length tiers: (shortest, longest) seconds. Creator Rewards pays for videos over a minute only.
DURATION_TIERS = {"Quick_Hook": (0.0, 30.0), "Standard": (30.0, 60.0), "Creator_Rewards_Eligible": (60.0, math.inf)}
CREATOR_REWARDS = "Creator_Rewards_Eligible"
CR_SECONDS = 62.0  # what a Creator Rewards clip is lengthened to before its cuts (compliance/duration_guard.py)
RATINGS = [round(1 + i / 10, 1) for i in range(91)]  # 1.0, 1.1 ... 10.0: the only ratings the grammar lets through
PER_WINDOW = 4  # moments asked of each window when the transcript is read in parts
MOST_AT_ONCE = 10  # moments asked of a transcript read in one go
OVERLAP = 4  # sentences each window shares with the one before, so no moment is split between two
MAX_SPLITS = 3  # a window too long for the model is halved at most this many times

SYSTEM = (
    "You are an expert short-form video editor finding moments in a long video that work as standalone "
    "vertical clips for TikTok. You get a transcript, one sentence per line, each with its [start-end] time "
    "in seconds. Pick moments that open at the beginning of a thought, include any setup the payoff needs, "
    "contain the complete payoff and end right after it lands. A strong hook in the first three seconds "
    "matters most. start_time must be the start time of a transcript line and end_time the end time of a "
    "line. Moments must not overlap. Rate hooks honestly: 9 to 10 only for moments that would stop almost "
    "anyone scrolling."
)


def tiers_for(min_len: float, max_len: float) -> list[str]:
    """The length tiers a clip between min_len and max_len seconds can be."""
    return [name for name, (lo, hi) in DURATION_TIERS.items() if lo < max_len and hi > min_len] or ["Standard"]


def schema(count: int, min_len: float = 15, max_len: float = 90, topics: list[str] | tuple = (),
           strict: bool = True) -> dict:
    """The clip schema. `strict` holds hook_rating to tenths from 1.0 to 10.0 (an enum, the one way a
    grammar can bound a decimal); the copy written into the instructions says it in words instead."""
    rating = ({"type": "number", "enum": RATINGS} if strict else {"type": "number", "minimum": 1, "maximum": 10})
    rating["description"] = "How strongly the opening stops someone scrolling, 1.0 to 10.0 with one decimal"
    subject = "The video's subject in one to three words, such as Gaming or Personal finance"
    if topics:
        subject += " (one of these when it fits: " + ", ".join(list(topics)[:12]) + ")"
    tiers = tiers_for(min_len, max_len)
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
                        "hook_headline": {"type": "string", "maxLength": 80,
                                          "description": "A short, punchy headline for the clip, at most 8 words"},
                        "hook_rating": rating,
                        "category": {"type": "string", "maxLength": 40, "description": subject},
                        "pacing_style": {"type": "string", "enum": list(PACING_STYLES),
                                         "description": "How to cut it: Dramatic Push-in for stories and slow "
                                                        "reveals, Punchy Cuts for fast energy, Steady for calm talk"},
                        "viral_trigger": {"type": "string", "enum": list(VIRAL_TRIGGERS),
                                          "description": "What makes people watch to the end"},
                        "target_duration_tier": {
                            "type": "string", "enum": tiers,
                            "description": "Quick_Hook: under 30 s; Standard: 30 to 60 s; Creator_Rewards_Eligible: "
                                           "over 60 s, the length TikTok's Creator Rewards pays for"},
                        "reason": {"type": "string", "maxLength": 160, "description": "One sentence on why it works"},
                    },
                    "required": ["start_time", "end_time", "hook_headline", "hook_rating", "category", "pacing_style",
                                 "viral_trigger", "target_duration_tier", "reason"],
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


def _rating(item: dict) -> float:
    """The hook rating, 1 to 10. The grammar allows nothing else; an answer from elsewhere may overshoot
    (a 12 is a 10) or rate out of 100 (an 85 is an 8.5)."""
    try:
        r = float(item.get("hook_rating", item.get("hook_rating_1_to_10")) or 5)
    except (TypeError, ValueError):
        return 5.0
    if not math.isfinite(r):
        return 5.0
    if 20 < r <= 100:
        r /= 10
    return min(10.0, max(1.0, r))


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
        tier = str(item.get("target_duration_tier") or "")
        if tier == CREATOR_REWARDS and b - a < CR_SECONDS and max_len >= CR_SECONDS:
            longer = ends[(ends >= a + CR_SECONDS) & (ends <= min(duration, a + max(max_len, CR_SECONDS) * 1.25))]
            if len(longer):
                b = float(longer.min())
        trigger = str(item.get("viral_trigger") or "")
        category = VIRAL_TRIGGERS.get(trigger) or str(item.get("viral_category") or "")
        title = str(item.get("hook_headline") or item.get("title") or "").strip()
        clip = Clip(a, b, round(_rating(item) * 10, 1), title, origin="ai",
                    reason=str(item.get("reason") or "").strip(), category=category if category in CATEGORIES else "")
        clip.hook = str(item.get("hook_text") or title).strip()[:80]
        pacing = str(item.get("pacing_style") or "")
        clip.pacing = pacing if pacing in PACING_STYLES else ""
        clip.trigger = trigger if trigger in VIRAL_TRIGGERS else ""
        clip.topic = " ".join(str(item.get("category") or "").split())[:40]
        out.append(clip)
    return out


def _ask(s, title: str, duration: float, part: list[Cue], n: int, parts: int, per: int, min_len: float,
         max_len: float, instructions: str, topics: list[str], log, check, progress, depth: int = 0):
    """One window's answer, as (clips, model). A window too long for the model is split in two."""
    prompt = (f"Video: {title}\nLength: {stamp(duration)} ({duration:.0f}s)\n"
              + (f"Part {n} of {parts} of the transcript.\n" if parts > 1 else "")
              + f"Pick up to {per} moments, each {min_len:.0f} to {max_len:.0f} seconds long "
                f"(end_time minus start_time).\nWhat the editor wants: {instructions}\n\n"
              + "Transcript:\n" + "\n".join(lines(part)))
    try:
        reply = llama_engine.ask_json(s, SYSTEM, prompt, schema(per, min_len, max_len, topics),
                                      max_tokens=220 * per + 300, log=log, check=check, progress=progress,
                                      prompt_schema=schema(per, min_len, max_len, topics, strict=False))
    except llama_engine.TooLong:
        if depth >= MAX_SPLITS or len(part) < 2 * OVERLAP + 2:
            raise
        half = len(part) // 2
        log(f"Part {n} was too long for the local AI's memory; reading it in two halves.")
        clips: list[Clip] = []
        model = ""
        for piece in (part[:half + OVERLAP // 2], part[half - OVERLAP // 2:]):
            got, model = _ask(s, title, duration, piece, n, parts, max(1, per // 2 + 1), min_len, max_len,
                              instructions, topics, log, check, progress, depth + 1)
            clips += got
        return clips, model
    except llama_engine.LocalCutOff:
        log(f"The local AI's answer for part {n} was cut off; skipped that part.")
        return [], ""
    try:
        data = json.loads(reply.text)
    except ValueError:
        log(f"The local AI's answer for part {n} couldn't be read; skipped that part.")
        return [], reply.model
    return to_clips((data or {}).get("clips") or [], part, duration, max_len), reply.model


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
    if len(parts) > 1:
        log(f"The local AI reads this video in {len(parts)} parts.")
    try:
        from ..categories import load as load_categories

        topics = [c.name for c in load_categories(s)]
    except Exception:  # no categories, or settings from a test
        topics = []
    pool: list[Clip] = []
    model = ""
    per = min(MOST_AT_ONCE, max(1, count)) if len(parts) == 1 else min(PER_WINDOW, max(1, count))
    for n, part in enumerate(parts, 1):
        if progress:
            progress("The local AI is picking clips", (n - 1) / len(parts))
        got, used = _ask(s, title, duration, part, n, len(parts), per, min_len, max_len, instructions, topics, log,
                         check, progress)
        pool += got
        model = used or model
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

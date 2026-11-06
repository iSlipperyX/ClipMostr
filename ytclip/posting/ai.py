"""The AI for posting: rates a clip from its frames and writes a caption and hashtags (rate), writes
just hashtags from what's known about a clip (suggest_hashtags, text only, so quick and cheap) and
suggests bios. It goes through ytclip.ai, so Settings → AI (Claude or Gemini, and its keys) decides."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from .config import Settings
from .media import sample_frames
from .store import VideoItem

FRAME_COUNT = 6

SCHEMA = {
    "type": "object",
    "properties": {
        "appeal": {"type": "integer", "description": "Predicted TikTok performance, 1-100"},
        "hook": {"type": "integer", "description": "Strength of the opening frame/first seconds, 1-100"},
        "verdict": {"type": "string", "description": "One or two sentences on why"},
        "caption": {"type": "string", "description": "Ready-to-post caption without hashtags, max 150 characters"},
        "hashtags": {"type": "array", "items": {"type": "string"}, "description": "Hashtags without the # sign"},
        "ideas": {"type": "array", "items": {"type": "string"}, "description": "Up to 3 concrete edits that would raise performance"},
    },
    "required": ["appeal", "hook", "verdict", "caption", "hashtags", "ideas"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a TikTok growth strategist who has reviewed thousands of short-form videos. You get frames "
    "sampled across one video (the first frame is from the opening half-second) plus technical measurements. "
    "Judge it the way the For You feed does: would a stranger stop scrolling in the first second, and stay "
    "to the end? Be calibrated, not flattering: an average upload is about 50, a likely viral hit 85+. "
    "Write captions in a natural creator voice that add curiosity rather than describing the video, "
    "and pick a mix of broad and niche hashtags that fit the content."
)


NO_KEY = "Add a Claude or Gemini API key in Settings → AI"


def main_settings():
    """ClipMonster's settings, whose Settings → AI chooses the AI and holds its keys."""
    from ..settings import Settings as MainSettings

    return MainSettings.load()


def has_key(s: Settings | None = None) -> bool:
    from .. import ai

    return ai.has_key(main_settings())


def ask_json(s: Settings, system: str, content, schema: dict, *, max_tokens: int = 2000, effort: str = "low",
             log=None, what: str = "") -> tuple[dict, str]:
    """Ask the AI Settings → AI chose (falling back to the other one as it says) for JSON matching
    `schema`, with its quicker "polish" model. Returns (answer, model). Raises ytclip.ai.AIError."""
    from .. import ai

    answer = ai.ask_json(main_settings(), "polish", system, content, schema, effort=effort or "low",
                         max_tokens=max_tokens, log=log, what=what)
    return answer.data, answer.model


def rate(item: VideoItem, s: Settings, log=None) -> dict:
    if not has_key(s):
        raise RuntimeError(f"{NO_KEY} to use AI scoring.")
    frames = sample_frames(Path(item.path), item.duration, FRAME_COUNT)
    if not frames:
        raise RuntimeError("Couldn't read frames from the video.")

    score = item.score or {}
    facts = [
        f"File: {Path(item.path).name}",
        f"Title: {item.name}",
        f"Length: {item.duration:.1f}s, {item.info.get('width')}x{item.info.get('height')}",
        f"Technical score: {score.get('total', '?')}/100, parts: {json.dumps(score.get('parts', {}))}",
    ]
    if item.source_reason:
        facts.append(f"Why the clip finder picked it: {item.source_reason}")
    if s.ai_niche:
        facts.append(f"The creator's niche / audience: {s.ai_niche}")
    facts.append(f"Give {s.ai_hashtag_count} hashtags.")

    content: list[dict] = []
    for i, jpg in enumerate(frames):
        content.append({"type": "text", "text": "Opening frame:" if i == 0 else f"Frame {i + 1}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(jpg).decode("ascii")}})
    content.append({"type": "text", "text": "\n".join(facts)})

    data, _model = ask_json(s, SYSTEM, content, SCHEMA, max_tokens=16000, effort="medium", log=log,
                            what=f"rate “{item.name}”")
    data["appeal"] = max(1, min(100, int(data.get("appeal", 50))))
    data["hook"] = max(1, min(100, int(data.get("hook", 50))))
    data["hashtags"] = [h.lstrip("#").strip() for h in data.get("hashtags", []) if isinstance(h, str) and h.strip()]
    return data


BIO_SCHEMA = {
    "type": "object",
    "properties": {
        "bios": {"type": "array", "items": {"type": "string"},
                 "description": "Three different bios, each at most 80 characters"},
    },
    "required": ["bios"],
    "additionalProperties": False,
}


def suggest_bios(s: Settings, niche: str, username: str = "", name: str = "", current: str = "",
                 examples: list[str] | None = None) -> list[str]:
    """Three short TikTok bios for an account, from its niche (and its best captions, if any)."""
    from .profile import BIO_MAX

    if not has_key(s):
        raise RuntimeError(f"{NO_KEY} to get bio suggestions.")
    if not (niche or examples or current):
        raise RuntimeError("Fill in the account's niche / audience first, so the suggestions fit it.")
    facts = []
    if username:
        facts.append(f"Account: @{username}" + (f" (shown as “{name}”)" if name else ""))
    if niche:
        facts.append(f"What it posts and for whom: {niche}")
    if current:
        facts.append(f"Current bio, for reference: {current}")
    if examples:
        facts.append("Captions of some of its posts:\n" + "\n".join(f"- {c}" for c in examples[:8]))
    facts.append(f"Write three different bios, each at most {BIO_MAX} characters including emoji and spaces.")
    data, _model = ask_json(
        s, ("You write TikTok bios for clip accounts. A good bio says in a few words what the viewer gets "
            "and why they should follow, in the account's own voice. At most one or two emoji, no hashtags, "
            "no links, no claims the account can't back up, and nothing that sounds like spam."),
        "\n".join(facts), BIO_SCHEMA, max_tokens=2000)
    bios = [" ".join(b.split()) for b in data.get("bios", []) if isinstance(b, str) and b.strip()]
    return [b if len(b) <= BIO_MAX else b[:BIO_MAX - 1].rstrip() + "…" for b in bios][:3]


HASHTAG_SCHEMA = {
    "type": "object",
    "properties": {
        "hashtags": {"type": "array", "items": {"type": "string"},
                     "description": "Hashtags without the # sign, most important first"},
    },
    "required": ["hashtags"],
    "additionalProperties": False,
}

HASHTAG_SYSTEM = (
    "You pick hashtags for TikTok clips. TikTok uses hashtags to work out who to show a video to, so "
    "they must say what the clip is actually about: the people in it (streamer, creator, guest), the game, "
    "show or topic, and the kind of moment (funny, rage, fail, clutch, story). Mix one or two broad tags "
    "for the category with specific ones for this clip. Prefer tags people really follow over invented "
    "ones. Never use spam tags (#fyp, #foryou, #foryoupage, #viral, #xyzbca, #trending), tags for things "
    "that aren't in the clip, or tags about other platforms. Write each tag as one word without the #: "
    "lowercase, or CamelCase when it joins words (e.g. MinecraftFunny)."
)

SPAM_TAGS = {"fyp", "foryou", "foryoupage", "fy", "viral", "xyzbca", "trending", "fypシ", "explore", "explorepage"}


def suggest_hashtags(s: Settings, item: VideoItem, niche: str = "", always: str = "",
                     proven: list[tuple[str, int]] | None = None, context: dict | None = None,
                     count: int = 0) -> list[str]:
    """Hashtags (without #) for one clip, from what's known about it; text only, one quick call.

    `always` are the account's tags added to every post (not repeated), `proven` the account's
    hashtags that did best so far with their median views, `context` ClipMonster's facts about
    the clip (source_title, channel, reason) when the item doesn't carry them.
    """
    from .tiktok import normalize_hashtags

    if not has_key(s):
        raise RuntimeError(f"{NO_KEY} to get AI hashtags.")
    count = max(1, min(10, count or s.ai_hashtag_count or 4))
    ctx = context or {}
    facts = [f"Clip title: {item.name}"]
    for label, value in (("On-screen hook / caption", item.caption or item.hook),
                         ("From the video", ctx.get("source_title", "")),
                         ("Channel / creator", item.source_channel or ctx.get("channel", "")),
                         ("Why it was clipped", item.source_reason or ctx.get("reason", ""))):
        if value and value != item.name:
            facts.append(f"{label}: {value}")
    if item.duration:
        facts.append(f"Length: {item.duration:.0f} s")
    if niche:
        facts.append(f"The account posts: {niche}")
    always_tags = normalize_hashtags(always).split()
    if always_tags:
        facts.append(f"Already added to every post (don't repeat): {' '.join(always_tags)}")
    if proven:
        facts.append("Hashtags that did best on this account so far (median views): " +
                     ", ".join(f"#{t} ({v:,})" for t, v in proven) + ". Reuse them only where they fit this clip.")
    facts.append(f"Give exactly {count} hashtags.")
    data, _model = ask_json(s, HASHTAG_SYSTEM, "\n".join(facts), HASHTAG_SCHEMA, max_tokens=1000)
    return clean_hashtags(data.get("hashtags") or [], always_tags, count)


def clean_hashtags(raw: list, always: list[str], count: int) -> list[str]:
    """One word each, no spam tags, none the account already adds, no repeats, at most `count`."""
    import re

    skip = {t.lstrip("#").lower() for t in always} | SPAM_TAGS
    out: list[str] = []
    for tag in raw:
        word = re.sub(r"[^\w]", "", tag) if isinstance(tag, str) else ""  # "#r6 siege" -> "r6siege"
        if not word or word.lower() in skip or len(word) > 40 or word.isdigit():
            continue
        skip.add(word.lower())
        out.append(word)
    return out[:count]


"""A game's best highlights, ranked.

The plays come from sports.py, as for any sports category: the crowd erupting, the commentator's big calls, the
replays and the close-ups after, each cut as a whole play (build-up, the moment, the reaction, stopping before
the replay). More are found than will be made (CANDIDATES), and each gets a signal score.

Then the AI rates them: it reads what the commentator said over each play and how the crowd and the commentary
rose, and gives each a 1-100 rating, the play in a word or two ("Dunk", "Penalty save"), a short title (naming a
player only when the commentary does), a line on why, and spots a candidate that's a replay of another play.
It's asked with ai.ask_json, so it's whichever AI Settings → AI chose: Claude or Gemini with an API key, the
local AI on this PC, or (on a Mac) the linked PC's local AI, with the others after it if that one fails. The
local model gets shorter excerpts and fewer plays, so the question fits it and it answers in good time.

The final score is mostly the AI's rating with the signals' score for the rest (they hear the crowd, which the
AI can't). Without an AI, or if it fails, the signals rank them alone and the reason says what they heard.
"""

from __future__ import annotations

import re
from typing import Callable

import numpy as np

from .. import ai, sports
from ..analyze import Clip
from ..organize import stamp
from ..subtitles import Cue, text_between

Log = Callable[[str], None]

CANDIDATES = 14  # plays found per game, for the AI to rank
LOCAL_CANDIDATES = 10  # fewer for a local model
AI_WEIGHT = 0.65  # the final score: this much the AI's rating, the rest the signals
DROP_BELOW = 22  # an AI rating under this isn't a highlight at all (a timeout, an ad, studio talk)
EXCERPT = 420  # characters of commentary per play
LOCAL_EXCERPT = 240
SIGNALS = ("crowd", "calls", "voice", "replay", "heatmap", "reactions")
SIGNAL_WORDS = {"crowd": "crowd", "calls": "big calls", "voice": "excited commentary", "replay": "replayed",
                "heatmap": "most rewatched", "reactions": "cheering"}

SYSTEM = (
    "You are the highlights editor for a sports TikTok account. You get candidate moments from one {what}, found by "
    "listening for the crowd erupting and the commentator's big calls. For each: when it is, how long, how loud the "
    "crowd and the commentary got (0-100), and what the commentator said over it. Rate each as a standalone "
    "short-form highlight, 1-100: 90+ a game-defining or jaw-dropping play (a winner at the death, a spectacular "
    "goal, dunk or knockout), 70-89 a great play anyone would share, 45-69 a decent play, under 30 not a highlight "
    "(a foul, a stoppage, an injury, a timeout, an ad, studio or crowd shots with nothing happening). Prefer {words}. "
    "play is what happens, in one to three words ('Dunk', 'Penalty save', 'Walk-off homer'). title is a short "
    "catchy title, at most 8 words, that builds curiosity without spoiling the result; name a player or team only "
    "when the commentary names them, never invent a name. why is one sentence. same_as is the number of an "
    "earlier candidate when this one is a replay or another angle of the same play, else 0.")

SCHEMA = {
    "type": "object",
    "properties": {
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "The candidate's number"},
                    "rating": {"type": "integer", "description": "1-100"},
                    "play": {"type": "string"},
                    "title": {"type": "string"},
                    "players": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                    "same_as": {"type": "integer"},
                },
                "required": ["n", "rating", "play", "title", "players", "why", "same_as"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["highlights"],
    "additionalProperties": False,
}


def is_local(s) -> bool:
    """Whether the AI that will answer first is a local model (this PC's, or a Mac's linked PC's)."""
    first = (ai.order(s) or [ai.chosen(s)])[0]
    return first in ai.LOCALS


def signal_parts(signals: dict[str, np.ndarray], clip: Clip) -> dict[str, int]:
    """How strong each signal got over the play, 0-100."""
    a, b = int(clip.start), max(int(clip.start) + 1, int(clip.end))
    out = {}
    for k in SIGNALS:
        sig = signals.get(k)
        if sig is not None and len(sig[a:b]):
            out[k] = int(round(float(np.max(sig[a:b])) * 100))
    return out


def excerpt(cues: list[Cue], clip: Clip, limit: int = EXCERPT) -> str:
    """What the commentator said over the play (and the few seconds after), trimmed to `limit` characters."""
    text = text_between(cues, clip.start, clip.end + 4)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def prompt(game: dict, clips: list[Clip], parts: list[dict[str, int]], cues: list[Cue], local: bool) -> str:
    limit = LOCAL_EXCERPT if local else EXCERPT
    sport = sports.get(game.get("sport", "")) or sports.SPORTS["any"]
    head = [f"Game: {game.get('matchup') or game.get('title')}",
            f"Video title: {game.get('title', '')}",
            f"Sport: {sport.label}" + (f", {game['league']}" if game.get("league") else ""),
            f"Footage: {game.get('kind', 'other')}, {stamp(game.get('duration') or 0)} long", "", "Candidates:"]
    lines = []
    for n, (c, p) in enumerate(zip(clips, parts), 1):
        loud = ", ".join(f"{SIGNAL_WORDS[k]} {v}" for k, v in p.items() if v >= 15) or "quiet"
        said = excerpt(cues, c, limit) or "(no commentary)"
        lines.append(f"{n}. at {stamp(c.start)} ({c.length:.0f} s) · {loud}\n   Commentary: “{said}”")
    return "\n".join(head + lines)


def signal_reason(clip: Clip, parts: dict[str, int], cues: list[Cue], sport: sports.Sport) -> str:
    """What the signals heard, for a play the AI didn't rate: 'Crowd 92 · commentator: “what a goal”'."""
    strong = sorted(((v, k) for k, v in parts.items() if v >= 55), reverse=True)[:2]
    said = ""
    pattern = sports.calls_pattern(sport)
    for c in cues:
        if clip.start - 1 <= c.start <= clip.end:
            m = pattern.search(c.text)
            if m:
                said = m.group(0).lower()
                break
    bits = [f"{SIGNAL_WORDS[k].capitalize()} {v}" for v, k in strong]
    if said:
        bits.append(f"commentator: “{said}”")
    return " · ".join(bits) or "Picked by the crowd and the commentary"


def play_guess(clip: Clip, cues: list[Cue], sport: sports.Sport) -> str:
    """The play by the commentator's call, without an AI: 'Dunk', 'Goal', 'Touchdown'; '' when nothing was called."""
    pattern = sports.calls_pattern(sport)
    for c in cues:
        if clip.start - 1 <= c.start <= clip.end + 3:
            m = pattern.search(c.text)
            if m:
                word = re.sub(r"(\w)\1{2,}", r"\1", m.group(0).lower())  # "gooooal" → "goal"
                word = re.sub(r"^what an? ", "", word)
                return word[:24].capitalize()
    return ""


def highlight(n: int, c: Clip, parts: dict[str, int], **extra) -> dict:
    return {"id": f"h{n}", "start": round(c.start, 2), "end": round(c.end, 2), "length": round(c.length, 1),
            "signal": round(float(c.score), 1), "parts": parts, "ai": None, "score": round(float(c.score), 1),
            "rank": 0, "title": "", "play": "", "players": [], "why": "", "chosen": False, "made": False,
            "file": "", **extra}


def rank(game: dict, clips: list[Clip], signals: dict[str, np.ndarray], cues: list[Cue], s, per_game: int,
         log: Log | None = None, check: Callable[[], None] | None = None, use_ai: bool = True
         ) -> tuple[list[dict], str, str]:
    """Rank `clips` (a game's candidate plays, best by the signals first). Returns the highlights (best first, the
    top `per_game` ticked), who ranked them ("" for the signals, else the AI's model) and why the AI didn't, if it
    didn't."""
    sport = sports.get(game.get("sport", "")) or sports.SPORTS["any"]
    local = is_local(s)
    clips = clips[:LOCAL_CANDIDATES if local else CANDIDATES]
    parts = [signal_parts(signals, c) for c in clips]
    out = [highlight(n, c, p) for n, (c, p) in enumerate(zip(clips, parts), 1)]
    by_ai, why_not = "", ""
    if use_ai and clips and ai.has_key(s):
        what = "game" if sport.id == "any" else {"soccer": "soccer match", "tennis": "tennis match",
                                                   "combat": "fight", "motorsport": "race", "golf": "round of golf",
                                                   "cricket": "cricket match"}.get(sport.id, f"{sport.label.lower()} game")
        system = SYSTEM.format(what=what, words=sport.words)
        try:
            answer = ai.ask_json(s, "find", system, prompt(game, clips, parts, cues, local), SCHEMA, effort="low",
                                 max_tokens=6000, log=log, check=check,
                                 what=f"rank {len(clips)} plays from {game.get('matchup') or 'the game'}")
            got = apply(out, answer.data)
            if got:
                by_ai = answer.model
                if log:
                    log(f"{ai.name(answer.provider)} ({answer.model}) ranked {got} plays.")
            else:
                why_not = f"{ai.name(answer.provider)}'s answer had no ratings in it."
        except ai.AIError as exc:
            why_not = str(exc)
            if log:
                log(f"The AI couldn't rank the plays ({exc}), so the crowd and the commentary rank them.")
    elif use_ai and clips:
        why_not = ai.missing_key(s)
    for h, c, p in zip(out, clips, parts):
        if not h["why"]:
            h["why"] = signal_reason(c, p, cues, sport)
        if not h["play"]:
            h["play"] = play_guess(c, cues, sport)
        if not h["title"]:
            h["title"] = c.title or h["play"] or f"Play at {stamp(c.start)}"
    out = [h for h in out if not h.get("dropped")]
    out.sort(key=lambda h: h["score"], reverse=True)
    for i, h in enumerate(out, 1):
        h["rank"] = i
        h["chosen"] = i <= per_game
    return out, by_ai, why_not


def apply(out: list[dict], data: dict) -> int:
    """Put the AI's ratings on the highlights; a replay of an earlier play, or one rated under DROP_BELOW, is
    dropped (marked). Returns how many were rated."""
    rated = 0
    items = data.get("highlights") if isinstance(data, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("n"))
            rating = max(1, min(100, int(round(float(item.get("rating"))))))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(out):
            continue
        h = out[n - 1]
        if h["ai"] is not None:  # the same number twice: the first answer stands
            continue
        rated += 1
        h["ai"] = rating
        h["score"] = round(AI_WEIGHT * rating + (1 - AI_WEIGHT) * h["signal"], 1)
        h["play"] = " ".join(str(item.get("play") or "").split())[:30]
        h["title"] = " ".join(str(item.get("title") or "").split()).strip("\"“” ")[:80]
        h["why"] = " ".join(str(item.get("why") or "").split())[:200]
        players = item.get("players") if isinstance(item.get("players"), list) else []
        h["players"] = [" ".join(str(p).split())[:40] for p in players if str(p).strip()][:4]
        try:
            same = int(item.get("same_as") or 0)
        except (TypeError, ValueError):
            same = 0
        h["_same"] = same if 0 < same < n else 0
    for h in out:  # after every rating is in: a replay goes when the play it repeats stays
        same = h.pop("_same", 0)
        if h["ai"] is not None and h["ai"] < DROP_BELOW:
            h["dropped"] = True
        elif same and not out[same - 1].get("dropped"):
            h["dropped"] = True
    return rated

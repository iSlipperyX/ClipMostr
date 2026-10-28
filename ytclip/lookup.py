"""Find videos for a request you type, like "a Call of Duty MW4 video with good comedy".

The request becomes one or two YouTube searches (with an AI key, the AI writes them, knowing that
MW4 is Modern Warfare 4 and that comedy means funny moments). Every video found is scored twice:
how well it matches the request (the AI rates each one, or the words in its title, tags and
description are counted) and how well it will clip (clipability.py). Best first.

A search costs 100 of the YouTube API's 10,000 free daily units, so a request uses about 200.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import clipability
from .settings import Settings, app_dir
from .trending import API_URL, VIDEO_PARTS, ResearchBlocked, ResearchError, TrendingVideo, _get, video_from_api

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]

SEARCH_COST = 100  # quota units per search.list call
PER_SEARCH = 50  # results asked of each search (the most one call gives)
MAX_SEARCHES = 3
MAX_REQUEST = 300  # characters
RECENT = 12  # requests remembered for the box's suggestions
RATE_AT_ONCE = 60  # videos the AI rates in one call
MATCH_WEIGHT = 0.6  # the rest is clip-ability
WEAK = 35.0  # a match under this isn't what you asked for: hidden
SINCE = {"any": 0, "week": 7, "month": 31, "year": 365}

# Words that ask for a kind of video, the search phrase that finds it, and words that show a video is one.
VIBES: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = [
    (("comedy", "comedic", "funny", "funniest", "hilarious", "humor", "humour", "laugh", "laughs", "lol", "jokes"),
     "funny moments", ("funny", "funniest", "hilarious", "comedy", "lol", "laugh", "laughing", "jokes", "troll",
                       "trolling")),
    (("rage", "raging", "angry"), "rage moments", ("rage", "raging", "angry", "mad", "tilted")),
    (("clutch", "clutches"), "clutch moments", ("clutch", "clutches", "1v4", "1v5", "1v3", "comeback")),
    (("fail", "fails"), "fails", ("fail", "fails", "failed")),
    (("highlight", "highlights", "best"), "highlights", ("highlights", "highlight", "best", "top")),
    (("scary", "horror", "jumpscare", "jumpscares", "creepy"), "scary moments",
     ("scary", "horror", "jumpscare", "jumpscares", "creepy", "terrifying")),
    (("wholesome", "heartwarming"), "wholesome moments", ("wholesome", "heartwarming", "cute")),
    (("reaction", "reactions", "react", "reacts"), "reaction", ("reaction", "reacts", "reacting", "react")),
    (("drama", "beef", "exposed"), "drama", ("drama", "beef", "exposed", "responds")),
    (("insane", "crazy", "epic", "wild", "intense"), "insane moments", ("insane", "crazy", "epic", "wild",
                                                                       "intense", "unbelievable")),
    (("tips", "tutorial", "guide", "howto"), "tips", ("tips", "tutorial", "guide", "how")),
]
FILLER = {"a", "an", "the", "some", "any", "find", "me", "show", "get", "i", "want", "need", "looking", "look",
          "for", "please", "video", "videos", "clip", "clips", "youtube", "good", "great", "really", "very",
          "lots", "lot", "of", "with", "that", "has", "have", "having", "and", "or", "in", "on", "to", "is", "are",
          "it", "its", "where", "which", "who", "something", "stuff", "content", "full", "type", "kind", "like",
          "nice", "solid", "decent", "plenty", "bit", "by", "from", "about", "my", "our", "your", "be", "been"}
BRIDGE = {"of", "the", "and", "&", "vs", "n"}  # kept inside a name: "Call of Duty", "Dungeons & Dragons"
NOT_SUBJECT = {"moments", "moment", "compilation", "compilations", "gameplay", "stream", "streams", "vod", "vods",
               "episode", "episodes", "montage"}
LEAD = re.compile(r"^(?:(?:for example|example|e\.g\.|please|can you|could you|find|show|get|search|look for|"
                  r"looking for|i want|i need|give)"
                  r"(?:\s+me)?\s+)+", re.I)


class FindError(ResearchError):
    """A plain-English reason the request couldn't be searched."""


@dataclass
class Plan:
    """What to search YouTube for, and what a matching video looks like."""

    request: str
    queries: list[str]
    subject: list[str] = field(default_factory=list)  # words the video should be about (lowercase)
    vibe: list[str] = field(default_factory=list)  # words that show it's the kind asked for
    about: str = ""  # one line: what the AI understood
    by_ai: bool = False


@dataclass
class LookupResult:
    request: str
    videos: list[TrendingVideo]
    time: float
    plan: Plan
    quota: int = 0
    weak: int = 0  # found but hidden: they don't match the request
    problems: list[str] = field(default_factory=list)
    rated_by: str = ""  # the model that rated the matches, "" = by their words

    def to_json(self) -> dict:
        return {"request": self.request, "time": self.time, "quota": self.quota, "weak": self.weak,
                "problems": self.problems, "rated_by": self.rated_by,
                "plan": {"queries": self.plan.queries, "subject": self.plan.subject, "vibe": self.plan.vibe,
                         "about": self.plan.about, "by_ai": self.plan.by_ai},
                "videos": [v.to_json() for v in self.videos]}

    @classmethod
    def from_json(cls, d: dict) -> "LookupResult":
        p = d.get("plan") or {}
        plan = Plan(d.get("request", ""), list(p.get("queries") or []), list(p.get("subject") or []),
                    list(p.get("vibe") or []), p.get("about", ""), bool(p.get("by_ai")))
        return cls(d.get("request", ""), [TrendingVideo.from_json(v) for v in d.get("videos") or []],
                   float(d.get("time") or 0), plan, int(d.get("quota") or 0), int(d.get("weak") or 0),
                   list(d.get("problems") or []), d.get("rated_by", ""))


# ---------------------------------------------------------------- the request → searches


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())


def tidy(request: str) -> str:
    text = " ".join((request or "").split())[:MAX_REQUEST]
    return LEAD.sub("", text).strip(" .?!")


def plain_plan(request: str) -> Plan:
    """Searches from the request's own words: what it's about, then what kind of video.

    "a Call of Duty MW4 video with good comedy" is about "Call of Duty MW4" and asks for comedy,
    so it searches "Call of Duty MW4 funny moments", then "Call of Duty MW4"."""
    text = tidy(request)
    if not text:
        raise FindError("Type what you're looking for, like \"Call of Duty MW4 with good comedy\".")
    asked = set(_words(text))
    vibes = [(phrase, shows) for words, phrase, shows in VIBES if asked & set(words)]
    kind = {w for words, _, _ in VIBES if asked & set(words) for w in words}
    tokens = [t for t in re.findall(r"[^\s,.;:!?]+", text)]
    plain = [re.sub(r"[^a-z0-9']", "", t.lower()) for t in tokens]
    content = [i for i, w in enumerate(plain) if w and w not in FILLER and w not in kind and w not in NOT_SUBJECT]
    kept = [tokens[i] for i in range(content[0], content[-1] + 1)
            if i in content or (plain[i] in BRIDGE and i - 1 in content and i + 1 in content)] if content else []
    subject_text = " ".join(kept)
    subject = [w for w in _words(subject_text) if w not in FILLER]
    if not subject and not vibes:
        raise FindError("Type what you're looking for, like \"Call of Duty MW4 with good comedy\".")
    if not subject:  # the request is only the kind of video ("funny fails"): search it as typed
        return Plan(text, [text], [], sorted({w for _, shows in vibes for w in shows}))
    queries = [f"{subject_text} {phrase}" for phrase, _ in vibes[:2]]
    queries.append(subject_text if vibes else text)
    seen: set[str] = set()
    queries = [q for q in queries if not (q.lower() in seen or seen.add(q.lower()))][:2]
    return Plan(text, queries, subject, sorted({w for _, shows in vibes for w in shows}))


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"},
                    "description": "1-3 YouTube searches, most likely to find it first"},
        "subject": {"type": "array", "items": {"type": "string"},
                    "description": "Words a matching video's title would use for what it's about, lowercase, "
                                   "including other names for it"},
        "vibe": {"type": "array", "items": {"type": "string"},
                 "description": "Words a title would use for the kind of video asked for, lowercase"},
        "about": {"type": "string", "description": "One short sentence: what you understood they want"},
    },
    "required": ["queries", "subject", "vibe", "about"],
    "additionalProperties": False,
}

RATE_SCHEMA = {
    "type": "object",
    "properties": {
        "videos": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "The video's number in the list"},
                "match": {"type": "integer", "description": "0-10: how well it is what they asked for"},
                "why": {"type": "string", "description": "At most 12 words"},
            },
            "required": ["n", "match", "why"],
            "additionalProperties": False,
        }}
    },
    "required": ["videos"],
    "additionalProperties": False,
}

PLAN_SYSTEM = (
    "You help a TikTok clipper find long YouTube videos to cut into clips. Turn their request into "
    "YouTube searches. Use the names people really search for (spell out abbreviations when titles use "
    "both, like 'MW4' and 'Modern Warfare 4'), and add the words that kind of video uses in its title "
    "(for comedy in a game: 'funny moments'). Keep each search short, like a person would type it."
)
RATE_SYSTEM = (
    "You help a TikTok clipper find long YouTube videos to cut into clips. Rate how well each video is "
    "what they asked for, from its title, channel, length, tags and description: 10 is exactly it, 5 is "
    "about the right thing but not the kind asked for, 0 is something else (another game, a different "
    "show, a trailer or news when they wanted gameplay). Be strict about the subject: a video about a "
    "different game or person is 0-2 however popular it is."
)


def _ask(prompt: str, system: str, schema: dict, s: Settings, check: Callable[[], None] | None = None,
         max_tokens: int = 4000) -> tuple[dict, str]:
    """One AI call (Claude or Gemini, per Settings → AI) with a JSON answer, and the model that gave it.
    The quicker polish model is plenty for writing searches and rating titles."""
    from .ai import ask_json

    answer = ask_json(s, "polish", system, prompt, schema, effort="low", max_tokens=max_tokens, check=check)
    return answer.data, answer.model


def ai_plan(request: str, s: Settings, check: Callable[[], None] | None = None) -> Plan:
    text = tidy(request)
    if not text:
        return plain_plan(request)
    answer, _ = _ask(f"Request: {text}", PLAN_SYSTEM, PLAN_SCHEMA, s, check, max_tokens=1500)
    queries = [" ".join(str(q).split())[:100] for q in answer.get("queries") or [] if str(q).strip()]
    if not queries:
        return plain_plan(request)
    words = lambda key: sorted({w for x in answer.get(key) or [] for w in _words(str(x)) if w not in FILLER})  # noqa: E731
    return Plan(text, queries[:MAX_SEARCHES], words("subject"), words("vibe"),
                " ".join(str(answer.get("about") or "").split())[:200], by_ai=True)


# ---------------------------------------------------------------- YouTube


def search_ids(api_key: str, query: str, published_after: float = 0.0, language: str = "",
               limit: int = PER_SEARCH, get: Callable[[str], dict] = _get) -> list[str]:
    """Video ids for a search, most relevant first; 100 quota units."""
    params = {"part": "id", "type": "video", "q": query, "order": "relevance",
              "maxResults": str(max(1, min(50, limit))), "key": api_key.strip()}
    if published_after:
        from datetime import datetime, timezone

        params["publishedAfter"] = datetime.fromtimestamp(published_after, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    if language:
        params["relevanceLanguage"] = language
    data = get(f"{API_URL}/search?{urllib.parse.urlencode(params)}")
    ids = [((it.get("id") or {}).get("videoId") or "") for it in data.get("items") or []]
    return [i for i in ids if i]


def details(api_key: str, ids: list[str], get: Callable[[str], dict] = _get
            ) -> tuple[list[TrendingVideo], dict[str, str]]:
    """The videos, and each one's tags and the start of its description (for matching); 1 unit per 50."""
    videos: list[TrendingVideo] = []
    text: dict[str, str] = {}
    for i in range(0, len(ids), 50):
        params = {"part": VIDEO_PARTS, "id": ",".join(ids[i:i + 50]), "key": api_key.strip()}
        for item in get(f"{API_URL}/videos?{urllib.parse.urlencode(params)}").get("items") or []:
            if not item.get("id"):
                continue
            v = video_from_api(item)
            sn = item.get("snippet") or {}
            text[v.id] = " ".join([" ".join(sn.get("tags") or [])[:300], (sn.get("description") or "")[:600]])
            videos.append(v)
    return videos, text


# ---------------------------------------------------------------- how well each one matches


def word_match(v: TrendingVideo, extra: str, plan: Plan) -> tuple[float, str]:
    """0-100 from the request's words in the title (most), tags and description; and why."""
    title, rest = set(_words(v.title)), set(_words(f"{v.channel} {extra}"))
    subject = [w for w in plan.subject if w]
    if subject:
        in_title = sum(w in title for w in subject) / len(subject)
        anywhere = sum(w in title or w in rest for w in subject) / len(subject)
        about = 0.65 * in_title + 0.35 * anywhere
    else:
        about = 1.0
    if plan.vibe:
        kind = 1.0 if title & set(plan.vibe) else 0.6 if rest & set(plan.vibe) else 0.0
        value = 0.7 * about + 0.3 * kind
    else:
        kind, value = 1.0, about
    hits = [w for w in subject if w in title or w in rest] + sorted((title | rest) & set(plan.vibe))[:2]
    why = ("Has " + ", ".join(f"“{w}”" for w in hits[:5])) if hits else "None of the request's words"
    if plan.vibe and not kind:
        why += "; nothing says it's the kind of video you asked for"
    return round(value * 100, 1), why


def ai_rate(videos: list[TrendingVideo], text: dict[str, str], plan: Plan, s: Settings,
            check: Callable[[], None] | None = None) -> tuple[dict[str, tuple[float, str]], str]:
    """The AI's 0-100 match for each video (by id), and the model that rated them."""
    out: dict[str, tuple[float, str]] = {}
    model = ""
    for k in range(0, len(videos), RATE_AT_ONCE):
        batch = videos[k:k + RATE_AT_ONCE]
        lines = [f"{n}. \"{v.title}\" by {v.channel}, {v.duration / 60:.0f} min, {v.views:,} views. "
                 f"{' '.join(text.get(v.id, '').split())[:350]}" for n, v in enumerate(batch, 1)]
        prompt = (f"Request: {plan.request}\n" + (f"(Understood as: {plan.about})\n" if plan.about else "")
                  + "\nVideos:\n" + "\n".join(lines))
        answer, model = _ask(prompt, RATE_SYSTEM, RATE_SCHEMA, s, check, max_tokens=6000)
        for item in answer.get("videos") or []:
            try:
                n, value = int(item["n"]), max(0, min(10, int(item["match"])))
            except (KeyError, TypeError, ValueError):
                continue
            if 1 <= n <= len(batch):
                out[batch[n - 1].id] = (value * 10.0, " ".join(str(item.get("why") or "").split())[:120])
    return out, model


# ---------------------------------------------------------------- one request


def find(request: str, s: Settings, api_key: str, since: str = "any", min_minutes: float | None = None,
         language: str = "", log: Log | None = None, progress: Progress | None = None,
         cancelled: Callable[[], bool] | None = None, get: Callable[[str], dict] = _get,
         use_ai: bool | None = None, now: float | None = None) -> LookupResult:
    """Search YouTube for the request and score what comes back, best match first."""
    from . import ai
    from .ai import AIError

    log = log or (lambda m: None)
    cancelled = cancelled or (lambda: False)
    now = now or time.time()

    def step(msg: str, frac: float | None) -> None:
        if cancelled():
            from .research import ScanCancelled

            raise ScanCancelled()
        if progress:
            progress(msg, frac)

    if not api_key:
        raise FindError("Add a YouTube Data API key (Settings → Research) to search YouTube.")
    problems: list[str] = []
    with_ai = ai.has_key(s) if use_ai is None else use_ai
    plan = None
    if with_ai:
        step("The AI is working out what to search for", None)
        try:
            plan = ai_plan(request, s, check=lambda: step("The AI is working out what to search for", None))
        except AIError as exc:
            problems.append(f"The AI couldn't plan the search ({exc}), so it searched your words.")
    plan = plan or plain_plan(request)
    log(f"Find: searching YouTube for {', '.join(repr(q) for q in plan.queries)}")
    after = now - SINCE.get(since, 0) * 86400 if SINCE.get(since) else 0.0
    ids: list[str] = []
    quota = 0
    for i, query in enumerate(plan.queries):
        step(f"Searching YouTube: {query}", i / (len(plan.queries) + 2))
        try:
            got = search_ids(api_key, query, after, language, PER_SEARCH, get)
        except ResearchBlocked:  # a bad key or used-up quota fails every search the same way
            raise
        except ResearchError as exc:
            if not ids and i == len(plan.queries) - 1:
                raise
            problems.append(f"Search \"{query}\": {exc}")
            continue
        finally:
            quota += SEARCH_COST
        ids += [x for x in got if x not in ids]
    if not ids:
        return LookupResult(plan.request, [], now, plan, quota, 0, problems)
    step(f"Reading {len(ids)} videos", len(plan.queries) / (len(plan.queries) + 2))
    videos, text = details(api_key, ids, get)
    quota += math.ceil(len(ids) / 50)
    shortest = 60.0 if min_minutes is None else max(60.0, min_minutes * 60)
    too_short = [v for v in videos if v.live != "live" and v.duration < shortest]
    videos = [v for v in videos if v not in too_short and v.live != "upcoming"]
    if too_short:
        log(f"Find: skipped {len(too_short)} video{'s' if len(too_short) != 1 else ''} shorter than "
            f"{shortest / 60:g} min.")
    for v in videos:
        v.origins = ["request"]
        v.velocity = v.views / max(1.0, v.age_hours(now))
    velocities = [v.velocity for v in videos]
    for v in videos:
        clipability.score(v, velocities, now)
    rated, rated_by = {}, ""
    if with_ai and videos:
        step(f"The AI is rating how well {len(videos)} videos match", (len(plan.queries) + 1) / (len(plan.queries) + 2))
        try:
            rated, rated_by = ai_rate(videos, text, plan, s,
                                      check=lambda: step("The AI is rating the videos", None))
        except AIError as exc:
            problems.append(f"The AI couldn't rate the videos ({exc}), so they're matched by their words.")
    for v in videos:
        v.match, v.match_why = rated.get(v.id) or word_match(v, text.get(v.id, ""), plan)
        v.reasons = [f"Match {v.match:.0f}: {v.match_why}"] + [r for r in v.reasons if not r.startswith("Match ")]
    weak = [v for v in videos if v.match < WEAK]
    videos = [v for v in videos if v.match >= WEAK]
    videos.sort(key=lambda v: (rank(v), v.match), reverse=True)
    step("Done", 1.0)
    return LookupResult(plan.request, videos, now, plan, quota, len(weak), problems, rated_by)


def rank(v: TrendingVideo) -> float:
    """Where a found video goes in the list: mostly how well it matches, then how well it will clip."""
    return round(MATCH_WEIGHT * max(0.0, v.match) + (1 - MATCH_WEIGHT) * v.score, 1)


# ---------------------------------------------------------------- remembered requests


def store_path() -> Path:
    return app_dir() / "finds.json"


def load_store(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or store_path()).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember(result: LookupResult, path: Path | None = None) -> None:
    """Keep the request for the box's suggestions, and its results for next time the tab opens."""
    path = path or store_path()
    data = load_store(path)
    recent = [r for r in data.get("recent") or [] if isinstance(r, str) and r.lower() != result.request.lower()]
    data["recent"] = [result.request] + recent[:RECENT - 1]
    data["last"] = result.to_json()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def recent_requests(path: Path | None = None) -> list[str]:
    return [r for r in load_store(path).get("recent") or [] if isinstance(r, str)]


def last_result(path: Path | None = None) -> LookupResult | None:
    last = load_store(path).get("last")
    try:
        return LookupResult.from_json(last) if isinstance(last, dict) and last.get("request") else None
    except (TypeError, ValueError):
        return None

"""The Creator's schedule: each category's "Creator videos a day", planned by automation once a day and made
overnight (or straight away), in turn with the clips.

What each video is about comes, in turn, from:

1. the category's own topic list (Categories tab → Video Creator → Topics), one idea per line, each used
   once a month at most;
2. what's trending in the category on YouTube right now: phrases that several of the last check's videos
   share in their titles ("GTA 6", "dopamine detox"), looked up online as they are, so the script is about
   the topic and today's news on it, never a retelling of someone's video;
3. the category's title words, then its name, with a rotating angle ("The strange history of ...").

The ideas from 1 and 2 alternate when there are both. Nothing is planned for a category that's switched off,
paused or without an account, and a day's plan is only made once: a video that fails isn't replaced.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import categories
from ..settings import app_dir

Log = Callable[[str], None]
DAY = 86400.0
REUSE_DAYS = 30  # an idea isn't used again for a month
KEEP_DAYS = 90  # used ideas older than this are forgotten
MAX_DAILY = 6
MAKE_HOUR = 1  # "overnight": the day's videos are due from 1 am
MIN_TITLES = 2  # a phrase must be in this many of the category's trending titles

TRENDING_PROMPT = "{topic}: what's happening right now and why people care"
ANGLES = ("A surprising fact about {topic} that most people get wrong",
          "The strange history of {topic}",
          "3 things about {topic} that sound made up but are true",
          "The biggest myth about {topic}, explained",
          "What experts on {topic} wish everyone knew",
          "The darkest chapter in the story of {topic}",
          "Why {topic} works the way it does",
          "A mistake almost everyone makes with {topic}",
          "The person who changed {topic} forever",
          "One question about {topic} nobody can answer yet")

GENERIC = set("""official video videos trailer full episode ep part new best top live stream highlights reaction
reacts react review reviews shorts short vs versus ft feat featuring music audio lyric lyrics hd 4k 1080p
update news today day week month year time things thing way ways everyone people guy guys man men woman women
watch watching try trying tried first last ever never most more how why what when who which made make makes
making get got gets getting go going goes know need want world life real really actually finally just""".split())


@dataclass
class Idea:
    prompt: str
    why: str  # where it came from, for the log
    search: str = ""  # what the web lookup searches ("" = from the prompt)


def key(text: str) -> str:
    return re.sub(r"[^\w]+", " ", (text or "").lower()).strip()


def topic_lines(text: str) -> list[str]:
    """The topic list: one idea per line, # starts a comment."""
    out = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and key(line) not in {key(x) for x in out}:
            out.append(line)
    return out


# ---------------------------------------------------------------- what's trending in a category

def _tokens(title: str) -> list[tuple[str, str]]:
    """(lower-case word, word as written) for each word of a title."""
    words = re.findall(r"[A-Za-z0-9][\w'’.+-]*", title or "")
    return [(w.lower().strip(".'’"), w.strip(".'’")) for w in words]


def trending_topics(videos: list, category_id: str, *, exclude: tuple[str, ...] = (), limit: int = 6,
                    min_titles: int = MIN_TITLES) -> list[tuple[str, int]]:
    """Phrases (1-3 words) that `min_titles`+ of the category's videos share in their titles, most shared and
    longest first: [(phrase as written, how many titles)]."""
    from .director import STOPWORDS

    skip = {key(x) for x in exclude if x}
    titles = [v.title for v in videos if getattr(v, "category_id", "") == category_id and getattr(v, "title", "")]
    seen: dict[str, set[int]] = {}
    shown: dict[str, str] = {}
    for i, title in enumerate(titles):
        toks = _tokens(title)
        for n in (1, 2, 3):
            for j in range(len(toks) - n + 1):
                gram = toks[j:j + n]
                low = [w for w, _ in gram]
                if low[0] in STOPWORDS or low[-1] in STOPWORDS or low[0] in GENERIC or low[-1] in GENERIC:
                    continue
                if all(w.isdigit() for w in low) or (n == 1 and (len(low[0]) < 4 or low[0] in GENERIC)):
                    continue
                k = " ".join(low)
                if k in skip or any(len(w) < 2 for w in low if not w.isdigit()):
                    continue
                seen.setdefault(k, set()).add(i)
                shown.setdefault(k, " ".join(w for _, w in gram))
    common = {k: len(ids) for k, ids in seen.items() if len(ids) >= min_titles}
    # A shorter phrase that's only ever part of a longer common one says less: keep the longer one.
    for k in list(common):
        if any(k != other and f" {k} " in f" {other} " and common[other] >= common[k] for other in common):
            del common[k]
    ranked = sorted(common.items(), key=lambda kv: (-kv[1], -len(kv[0].split()), kv[0]))
    return [(shown[k], n) for k, n in ranked[:limit]]


# ---------------------------------------------------------------- the day's ideas

class Used:
    """Ideas already used, so none comes back within a month (creator_schedule.json)."""

    def __init__(self, path: Path | None = None):
        self.path = path or (app_dir() / "creator_schedule.json")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        used = data.get("used") if isinstance(data, dict) else None
        self.used: dict[str, float] = {str(k): float(v) for k, v in (used or {}).items()
                                       if isinstance(v, (int, float))}

    def fresh(self, text: str, now: float) -> bool:
        return now - self.used.get(key(text), 0.0) >= REUSE_DAYS * DAY

    def mark(self, text: str, now: float) -> None:
        self.used[key(text)] = now

    def save(self, now: float) -> None:
        keep = {k: t for k, t in self.used.items() if now - t < KEEP_DAYS * DAY}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "used": keep}, indent=1), encoding="utf-8")
        tmp.replace(self.path)


def ideas_for(c: categories.Category, videos: list, used: Used, now: float, n: int) -> list[Idea]:
    """`n` ideas for a category's Creator videos today (fewer only if it has nothing at all to go on)."""
    out: list[Idea] = []
    taken: set[str] = set()

    def add(idea: Idea, *marks: str) -> bool:
        if len(out) >= n or any(key(m) in taken or not used.fresh(m, now) for m in marks):
            return False
        out.append(idea)
        taken.update(key(m) for m in marks)
        return True

    mine = [Idea(t, "from its topic list") for t in topic_lines(c.creator_topics)]
    trending = [Idea(TRENDING_PROMPT.format(topic=t), f"trending in {c.name} on YouTube: \"{t}\" in {k} videos", t)
                for t, k in trending_topics(videos, c.id, exclude=(c.name,))]
    while len(out) < n and (mine or trending):  # the two take turns when there are both
        for pool in (mine, trending):
            while pool and len(out) < n:
                idea = pool.pop(0)
                if add(idea, idea.search or idea.prompt):
                    break
    words = [w.strip() for w in (c.keywords or "").split(",") if w.strip()] + [c.name]
    day = int(now // DAY)
    for i in range(len(ANGLES) * len(words)):
        if len(out) >= n:
            break
        word = words[(day + i) % len(words)]
        angle = ANGLES[(day + i // len(words)) % len(ANGLES)]
        add(Idea(angle.format(topic=word), f"{c.name}'s {'name' if word == c.name else 'title words'}", word),
            angle.format(topic=word))
    if len(out) < n:  # every angle used this month: the least recently used again (the script still differs)
        again = sorted(((used.used.get(key(a.format(topic=w)), 0.0), a.format(topic=w), w)
                        for w in words for a in ANGLES), key=lambda x: x[0])
        for _, prompt, word in again:
            if len(out) >= n:
                break
            if key(prompt) not in taken:
                out.append(Idea(prompt, f"{c.name}'s {'name' if word == c.name else 'title words'}, again", word))
                taken.add(key(prompt))
    return out


def day_start(now: float) -> float:
    """Local midnight at the start of `now`'s day."""
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def made_today(db, now: float) -> dict[str, int]:
    """Category id: Creator videos the schedule has planned since midnight."""
    out: dict[str, int] = {}
    for job in db.created_since(day_start(now)):
        if job.is_creation and "schedule" in (job.origins or []):
            out[job.category_id] = out.get(job.category_id, 0) + 1
    return out


def due_at(s, now: float) -> float:
    """When today's videos are due: 1 am ("overnight"), or now when that's passed or for "straight away"."""
    if getattr(s, "creator_schedule_when", "night") != "night":
        return now
    return max(now, day_start(now) + MAKE_HOUR * 3600)


def plan(s, db, videos: list | None = None, now: float | None = None, log: Log | None = None,
         used: Used | None = None) -> int:
    """Queue what's missing from today's Creator videos, every category with some a day. Returns how many."""
    from . import orchestrator

    now = time.time() if now is None else now
    log = log or (lambda m: None)
    todo = [c for c in categories.active(s) if c.creator_daily > 0]
    if not todo:
        return 0
    have = made_today(db, now)
    used = used or Used()
    videos = videos or []
    when = due_at(s, now)
    added = 0
    for c in todo:
        need = min(MAX_DAILY, c.creator_daily) - have.get(c.id, 0)
        if need <= 0:
            continue
        for idea in ideas_for(c, videos, used, now, need):
            req = orchestrator.CreateRequest(prompt=idea.prompt, category=c.id, search=idea.search)
            orchestrator.queue(db, req, c.id, now, origins=["creator", "schedule"], due=when)
            used.mark(idea.search or idea.prompt, now)
            used.mark(idea.prompt, now)
            added += 1
            log(f"Planned a Creator video for {c.name}: “{idea.prompt}” ({idea.why})"
                + (f", made from {time.strftime('%H:%M', time.localtime(when))}." if when > now + 60 else "."))
    if added:
        used.save(now)
    return added

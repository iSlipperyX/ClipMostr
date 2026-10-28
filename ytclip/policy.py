"""Picking rules: which researched videos may be clipped without anyone looking first.

decide() goes through a scan's videos best first and gives each one a Decision: picked, or
skipped with the rule that stopped it. It changes nothing; PickLog.record() is called only
when a pick is actually acted on (sent to the Studio, or clipped by the command line or the
runner), so the daily and per-channel limits count real work, not previews.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import niches
from .learn import Lessons
from .organize import stamp
from .settings import PICK_FROM, Settings, app_dir
from .sources import parse_channel, split_entries
from .trending import CATEGORIES, TrendingVideo

LANGUAGES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
             "it": "Italian", "nl": "Dutch", "id": "Indonesian", "hi": "Hindi", "ja": "Japanese", "ko": "Korean",
             "zh": "Chinese", "ru": "Russian", "ar": "Arabic", "tr": "Turkish", "pl": "Polish", "vi": "Vietnamese",
             "th": "Thai", "tl": "Filipino", "ms": "Malay"}
KEEP_DAYS = 120  # picks are remembered this long, so nothing is picked twice
DAY = 86400.0


@dataclass
class Policy:
    pick_from: str = "watchlist"
    min_score: float = 65.0
    min_minutes: float = 8.0
    max_minutes: float = 180.0  # 0 = no limit
    max_age_days: float = 3.0  # 0 = no limit
    max_age_allowed_days: float = 14.0  # for videos you have permission to clip (channel list, CC); 0 = no limit
    daily: int = 3
    per_channel_week: int = 2  # 0 = no limit
    block_channels: list[str] = field(default_factory=list)  # names, @handles or channel ids, lowercased
    block_categories: set[str] = field(default_factory=set)
    block_words: list[str] = field(default_factory=list)  # lowercased
    languages: set[str] = field(default_factory=set)  # two-letter codes; empty = any
    use_niches: bool = False  # niches exist, so every pick must fit a switched-on one
    niche_daily: dict[str, int] = field(default_factory=dict)  # niche id -> its picks per day
    niche_names: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, s: Settings) -> "Policy":
        on = niches.active(s)
        return cls(
            pick_from=s.pick_from if s.pick_from in dict(PICK_FROM) else "watchlist",
            min_score=float(s.pick_min_score), min_minutes=float(s.pick_min_minutes),
            max_minutes=float(s.pick_max_minutes), max_age_days=float(s.pick_max_age_days),
            max_age_allowed_days=float(s.pick_max_age_allowed_days),
            daily=int(s.pick_daily), per_channel_week=int(s.pick_channel_weekly),
            block_channels=_blocked_channels(s.pick_block_channels),
            block_categories={c for c in re.split(r"[\s,;]+", s.pick_block_categories) if c in CATEGORIES and c},
            block_words=[w.strip().lower() for w in s.pick_block_words.split(",") if w.strip()],
            languages=parse_languages(s.pick_languages),
            use_niches=bool(niches.load(s)),
            niche_daily={n.id: n.daily for n in on},
            niche_names={n.id: n.name for n in on},
        )


def language_codes(text: str) -> list[str]:
    """"en, es-MX" -> ["en", "es"], in the order given."""
    out: list[str] = []
    for c in re.split(r"[\s,;]+", text or ""):
        if re.fullmatch(r"[A-Za-z]{2,3}(-\w+)?", c) and c.split("-")[0].lower() not in out:
            out.append(c.split("-")[0].lower())
    return out


def parse_languages(text: str) -> set[str]:
    return set(language_codes(text))


def _blocked_channels(text: str) -> list[str]:
    """Channel ids stay as they are; handles and plain names ("Some Channel") are lowercased."""
    out = []
    for item, _note in split_entries(text):
        ref = parse_channel(item)
        out.append(ref.value if ref and ref.kind == "id" else (ref.value if ref else item).lower())
    return out


def _squash(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower())


@dataclass
class Decision:
    video: TrendingVideo
    picked: bool
    reason: str  # why it was skipped, or why it's allowed when picked
    rule: str = ""  # short id of the deciding rule, for tests and the CLI's JSON
    learned: str = ""  # how results from earlier posts moved it up or down, if they did
    bonus: float = 0.0  # those clip-ability points

    @property
    def permission(self) -> str:
        return self.reason if self.picked else ""


# ---------------------------------------------------------------- the log of real picks


class PickLog:
    """picks.json: every video the rules picked and something then acted on."""

    def __init__(self, path: Path | None):
        self.path = path
        self.picks: list[dict] = []
        self.lock = threading.RLock()
        self.reload()

    def _read(self) -> list[dict]:
        if not self.path:
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [p for p in data.get("picks") or [] if isinstance(p, dict) and p.get("id")]
        except (OSError, ValueError, AttributeError):
            return []

    def reload(self) -> None:
        """Pick up picks another process (the runner, or the app) saved since."""
        with self.lock:
            mine = {p["id"]: p for p in self.picks}
            self.picks = list(({p["id"]: p for p in self._read()} | mine).values())

    def get(self, video_id: str) -> dict | None:
        with self.lock:
            return next((p for p in self.picks if p["id"] == video_id), None)

    def count_since(self, since: float, channel_id: str | None = None, niche: str | None = None) -> int:
        with self.lock:
            return sum(p.get("time", 0) >= since and (channel_id is None or p.get("channel_id") == channel_id)
                       and (niche is None or p.get("niche", "") == niche) for p in self.picks)

    def record(self, d: Decision, how: str, now: float | None = None) -> dict:
        """Remember an acted-on pick. how: "studio" (sent by hand) | "cli" | "auto"."""
        now = now or time.time()
        v = d.video
        rec = {"id": v.id, "url": v.url, "title": v.title, "channel": v.channel, "channel_id": v.channel_id,
               "score": v.score, "license": v.license, "origins": list(v.origins), "permission": d.reason,
               "time": now, "how": how, "niche": v.niche}
        with self.lock:
            self.picks = [p for p in self.picks if p["id"] != v.id]
            self.picks.append(rec)
            self._prune(now)
        return rec

    def _prune(self, now: float) -> None:
        self.picks = [p for p in self.picks if now - p.get("time", 0) < KEEP_DAYS * DAY]

    def save(self) -> None:
        """Write, keeping picks another process saved meanwhile (the app and the runner share the file)."""
        if not self.path:
            return
        with self.lock:
            self.reload()
            self._prune(max([time.time()] + [p.get("time", 0) for p in self.picks]))
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps({"picks": self.picks}, indent=1, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass


def picks_path() -> Path:
    return app_dir() / "picks.json"


# ---------------------------------------------------------------- deciding


def _ago(seconds: float) -> str:
    days = seconds / DAY
    if days < 1:
        return "today" if seconds < 12 * 3600 else "yesterday"
    return f"{days:.0f} days ago"


def _permission(v: TrendingVideo, notes: dict[str, str]) -> str | None:
    """Why this video may be clipped under the source policy, or None if nothing says so."""
    if "watchlist" in v.origins:
        note = notes.get(v.channel_id, "")
        return f"On your channel list{': ' + note if note else ''}"
    if v.license == "creativeCommon":
        return "Creative Commons licence (credit the channel)"
    return None


def decide(videos: list[TrendingVideo], policy: Policy, log: PickLog, now: float | None = None,
           notes: dict[str, str] | None = None, lessons: Lessons | None = None) -> list[Decision]:
    """One decision per video, best first. Limits count earlier picks in the log plus the
    picks made higher up this list. With lessons, a channel whose clips did well (or badly)
    once posted gets clip-ability points added (or taken off) for ranking and the minimum score.

    With niches, every pick must fit one (video.niche), each niche has a daily limit of its own,
    and the overall daily limit is handed out one pick at a time to each niche in turn (its best
    video first), the niche with the fewest picks in the last day going first, so one busy niche
    can't take every pick of the day."""
    now = now or time.time()
    notes = notes or {}
    learned = {v.id: lessons.bonus(v.channel) if lessons else (0.0, "") for v in videos}
    ranked = sorted(videos, key=lambda v: (v.score + learned[v.id][0], v.velocity), reverse=True)
    out: dict[str, Decision] = {}
    queues: dict[str, list[tuple[TrendingVideo, str]]] = {}  # niche -> (video, permission), best first
    for v in ranked:
        d = _check(v, policy, log, now, notes, learned[v.id])
        if isinstance(d, Decision):
            out[v.id] = d
        else:  # passed every rule but the limits
            queues.setdefault(v.niche if policy.use_niches else "", []).append((v, d))

    picked_today = log.count_since(now - DAY)
    per_channel: dict[str, int] = {}
    per_niche: dict[str, int] = {}
    earlier = {k: log.count_since(now - DAY, niche=k) for k in queues} if policy.use_niches else {}
    turns = list(queues.items())  # in the order of each niche's best video
    while any(q for _, q in turns):
        # Each round the niche with the fewest picks so far goes first; on a tie, the better video.
        turns.sort(key=lambda kq: earlier.get(kq[0], 0) + per_niche.get(kq[0], 0))
        for _, q in turns:
            while q:  # this niche's next video that the limits allow, if any
                v, permission = q.pop(0)
                d = _limit(v, permission, policy, log, now, picked_today, per_channel, per_niche,
                           learned[v.id])
                out[v.id] = d
                if d.picked:
                    picked_today += 1
                    per_channel[v.channel_id] = per_channel.get(v.channel_id, 0) + 1
                    per_niche[v.niche] = per_niche.get(v.niche, 0) + 1
                    break
    return [out[v.id] for v in ranked]


def _check(v: TrendingVideo, p: Policy, log: PickLog, now: float, notes: dict[str, str],
           learned: tuple[float, str] = (0.0, "")) -> Decision | str:
    """Every rule but the limits: a Decision when one stops the video, else why it may be clipped."""
    bonus, note = learned

    def skip(rule: str, reason: str) -> Decision:
        return Decision(v, False, reason, rule, note, bonus)

    earlier = log.get(v.id)
    if earlier:
        return skip("done", f"Already picked {_ago(now - earlier.get('time', now))}.")
    if v.sent:
        return skip("done", "Already sent to the Studio.")
    if v.live == "live":
        return skip("live", "Live right now; it can be picked once the stream ends.")
    if v.live == "upcoming":
        return skip("live", "Hasn't premiered yet.")

    name, cid = v.channel.lower(), v.channel_id
    for b in p.block_channels:
        if b == cid or b == name or (len(_squash(b)) >= 3 and _squash(b) == _squash(v.channel)):
            return skip("channel", f"{v.channel} is on your blocked channels.")
    if v.category in p.block_categories:
        return skip("category", f"{CATEGORIES.get(v.category, 'This category')} is blocked.")
    title = v.title.lower()
    word = next((w for w in p.block_words if w in title), None)
    if word:
        return skip("word", f"The title contains \"{word}\".")

    if p.languages and v.language and v.language not in p.languages:
        return skip("language", f"In {LANGUAGES.get(v.language, v.language)}; you only pick "
                                f"{', '.join(LANGUAGES.get(c, c) for c in sorted(p.languages))} videos.")

    permission = _permission(v, notes)
    if p.pick_from == "cc" and v.license != "creativeCommon":
        return skip("source", "Not Creative Commons, and you only pick Creative Commons videos.")
    if p.pick_from == "watchlist" and permission is None:
        return skip("source", "Not on your channel list and not Creative Commons.")
    if p.use_niches and v.niche not in p.niche_daily:
        return skip("niche", "Fits none of your niches. Add its channel, a word from its title or its category "
                             "to a niche (Niches tab)." if not v.niche else
                             "Its niche is switched off.")

    minutes = v.duration / 60
    if p.min_minutes and minutes < p.min_minutes:
        return skip("length", f"Only {stamp(v.duration)} long; your minimum is {p.min_minutes:g} min.")
    if p.max_minutes and minutes > p.max_minutes:
        return skip("length", f"{stamp(v.duration)} long; your maximum is {p.max_minutes:g} min.")
    age_days = v.age_hours(now) / 24
    # Trending videos fade within days; videos you have permission to clip stay worth it for longer.
    limit = p.max_age_allowed_days if permission is not None else p.max_age_days
    if limit and age_days > limit:
        return skip("age", f"Posted {age_days:.0f} days ago; your limit is {limit:g}.")
    if v.score + bonus < p.min_score:
        if bonus:
            return skip("score", f"Clip-ability {v.score:.0f}, {v.score + bonus:.0f} after your results, is under "
                                 f"your minimum of {p.min_score:g}.")
        return skip("score", f"Clip-ability {v.score:.0f} is under your minimum of {p.min_score:g}.")
    return permission or "Passed your rules (no permission on record)"


def _limit(v: TrendingVideo, permission: str, p: Policy, log: PickLog, now: float, picked_today: int,
           per_channel: dict[str, int], per_niche: dict[str, int],
           learned: tuple[float, str] = (0.0, "")) -> Decision:
    """The limits: per channel per week, per niche per day, and in all per day."""
    bonus, note = learned
    cid = v.channel_id

    def skip(rule: str, reason: str) -> Decision:
        return Decision(v, False, reason, rule, note, bonus)

    if p.per_channel_week:
        this_week = log.count_since(now - 7 * DAY, cid) + per_channel.get(cid, 0)
        if this_week >= p.per_channel_week:
            return skip("channel_cap", f"Already {this_week} pick{'s' if this_week != 1 else ''} from {v.channel} "
                                       f"in the last 7 days (your limit is {p.per_channel_week}).")
    if p.use_niches:
        cap = p.niche_daily.get(v.niche, 0)
        today = log.count_since(now - DAY, niche=v.niche) + per_niche.get(v.niche, 0)
        if today >= cap:
            return skip("niche_cap", f"{p.niche_names.get(v.niche, 'This niche')} already has its {cap} "
                                     f"pick{'s' if cap != 1 else ''} for today.")
    if picked_today >= p.daily:
        return skip("daily_cap", f"The limit of {p.daily} pick{'s' if p.daily != 1 else ''} a day is reached.")
    return Decision(v, True, permission, "picked", note, bonus)


def picked(decisions: list[Decision]) -> list[Decision]:
    return [d for d in decisions if d.picked]

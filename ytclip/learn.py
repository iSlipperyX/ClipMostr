"""Learning from results: how the clips did once they were posted, fed back into picking and polish.

Posting (posting/results.py) writes results.json into ClipMonster's settings folder as it reads stats:
one record per posted clip that has been up long enough (48 hours), with its views and its
"lift", which is its views divided by the usual (median) views on that TikTok account, so clips
from a big account and a small one can be compared.

Lessons turns those records into:

* a bonus or penalty per YouTube channel, used to rank and filter picks (bounded, and shrunk
  toward zero until a channel has a few posts, so one lucky clip doesn't decide anything),
* examples of hooks that worked and hooks that flopped, for the AI polish prompt,
* plain-English notes on clip length and on whether the AI rating predicts views.

It never changes a setting by itself; notes that suggest a change say so.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from .settings import app_dir

KEEP_DAYS = 90  # older results say little about what works now
MAX_BONUS = 12.0  # clip-ability points, either way
BONUS_PER_DOUBLING = 8.0  # points for clips that get twice the usual views
SHRINK = 2  # a channel's lift counts n / (n + SHRINK): 1 post a third, 4 posts two thirds
MIN_EXAMPLES = 6  # posts with hooks before the polish gets examples
CREATOR_MIN = 4  # Creator videos with results before their own hooks and titles are examples
LENGTHS = ((0, 20, "under 20 s"), (20, 35, "20–35 s"), (35, 50, "35–50 s"), (50, 10_000, "over 50 s"))


def results_path() -> Path:
    return app_dir() / "results.json"


def _squash(text: str) -> str:
    return re.sub(r"[^\w]", "", (text or "").lower())


def _times(lift: float) -> str:
    return f"{lift:.1f}×" if lift < 10 else f"{lift:.0f}×"


@dataclass
class Outcome:
    """One posted clip, as posting saw it on TikTok."""

    channel: str = ""
    video_id: str = ""
    views: int = 0
    lift: float = 1.0  # views / the account's usual views
    length: float = 0.0  # seconds
    hook: str = ""
    rating: int | None = None  # the AI polish rating, 1-10
    score: float | None = None  # ClipMonster's clip score
    posted_at: float = 0.0
    account: str = ""
    category_id: str = ""  # id of the ClipMonster category the clip was made for (see for_settings)
    origin: str = ""  # "creator" (made by the Video Creator) or "clip"; "" from before posting said
    title: str = ""
    words: int = 0  # a Creator video's script, in words (0 = not known): its pace (intelligence/learner.py)
    scenes: int = 0  # and its scenes

    @property
    def creator(self) -> bool:
        return self.origin == "creator"

    @classmethod
    def from_json(cls, d: dict) -> "Outcome | None":
        try:
            lift = float(d.get("lift"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(lift) or lift < 0:
            return None
        rating, score = d.get("rating"), d.get("score")
        return cls(channel=str(d.get("channel") or ""), video_id=str(d.get("video_id") or ""),
                   views=int(d.get("views") or 0), lift=lift, length=float(d.get("length") or 0),
                   hook=str(d.get("hook") or "").strip(),
                   rating=int(rating) if isinstance(rating, (int, float)) else None,
                   score=float(score) if isinstance(score, (int, float)) else None,
                   posted_at=float(d.get("posted_at") or 0), account=str(d.get("account") or ""),
                   category_id=str(d.get("category_id") or d.get("niche") or ""),
                   origin=str(d.get("origin") or ""), title=str(d.get("clip") or d.get("title") or "").strip(),
                   words=_count(d.get("words")), scenes=_count(d.get("scenes")))


def _count(value) -> int:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


@dataclass
class ChannelLesson:
    name: str
    posts: int
    lift: float  # typical lift (geometric mean), before shrinking
    bonus: float  # clip-ability points to add when ranking this channel's videos

    def note(self) -> str:
        posts = f"{self.posts} post{'s' if self.posts != 1 else ''}"
        if self.bonus >= 1:
            return f"Your clips from {self.name} get {_times(self.lift)} your usual views ({posts}): +{self.bonus:.0f}."
        if self.bonus <= -1:
            return f"Your clips from {self.name} get {_times(self.lift)} your usual views ({posts}): {self.bonus:.0f}."
        return f"Your clips from {self.name} get about your usual views ({posts})."


def _log2(lift: float) -> float:
    return math.log2(max(lift, 0.05))


@dataclass
class Lessons:
    outcomes: list[Outcome] = field(default_factory=list)
    channels: dict[str, ChannelLesson] = field(default_factory=dict)  # by squashed channel name
    written_at: float = 0.0

    @classmethod
    def load(cls, path: Path | None = None, now: float | None = None) -> "Lessons":
        """Lessons from the results file posting writes (empty when there is none yet)."""
        now = time.time() if now is None else now
        try:
            data = json.loads((path or results_path()).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        rows = [Outcome.from_json(d) for d in data.get("outcomes", []) if isinstance(d, dict)]
        keep = [o for o in rows if o and (not o.posted_at or now - o.posted_at <= KEEP_DAYS * 86400)]
        lessons = cls.from_outcomes(keep)
        lessons.written_at = float(data.get("written_at") or 0)
        return lessons

    @classmethod
    def from_outcomes(cls, outcomes: list[Outcome]) -> "Lessons":
        groups: dict[str, list[Outcome]] = {}
        for o in outcomes:
            if _squash(o.channel) and not o.creator:  # the Creator's videos have no YouTube channel to rank
                groups.setdefault(_squash(o.channel), []).append(o)
        channels = {}
        for key, rows in groups.items():
            logs = [_log2(o.lift) for o in rows]
            mean = sum(logs) / len(logs)
            shrunk = mean * len(rows) / (len(rows) + SHRINK)
            bonus = max(-MAX_BONUS, min(MAX_BONUS, BONUS_PER_DOUBLING * shrunk))
            name = max(rows, key=lambda o: o.posted_at).channel
            channels[key] = ChannelLesson(name, len(rows), 2 ** mean, round(bonus, 1))
        return cls(list(outcomes), channels)

    @property
    def empty(self) -> bool:
        return not self.outcomes

    def channel(self, name: str) -> ChannelLesson | None:
        return self.channels.get(_squash(name))

    def bonus(self, channel: str) -> tuple[float, str]:
        """(points, note) for ranking a video from this channel; (0, "") when nothing is known."""
        c = self.channel(channel)
        if not c or abs(c.bonus) < 1:
            return 0.0, ""
        return c.bonus, c.note()

    # --------------------------------------------------------------- for the AI polish

    def hook_examples(self, count: int = 3,
                      category_id: str = "") -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
        """(hooks that worked, hooks that flopped) as (hook, lift), best and worst first. For a category,
        its own posts once there are enough of them (what works differs between accounts), else all."""
        hooked = self._hooked(category_id)
        if len(hooked) < MIN_EXAMPLES:
            return [], []
        ranked = sorted(hooked, key=lambda o: o.lift, reverse=True)
        good = [(o.hook, o.lift) for o in ranked if o.lift >= 1.5][:count]
        bad = [(o.hook, o.lift) for o in reversed(ranked) if o.lift <= 0.6][:count]
        return good, bad

    def _hooked(self, category_id: str = "") -> list[Outcome]:
        hooked = [o for o in self.outcomes if o.hook and not o.creator]  # clips' hooks (the Creator has its own)
        own = [o for o in hooked if category_id and o.category_id == category_id]
        return own if len(own) >= MIN_EXAMPLES else hooked

    def prompt_text(self, category_id: str = "") -> str:
        """A paragraph for the polish prompt, or "" when there isn't enough to go on."""
        good, bad = self.hook_examples(category_id=category_id)
        if not good and not bad:
            return ""
        if category_id and any(o.category_id != category_id for o in self._hooked(category_id)):  # too few of its own yet
            lines = ["How earlier hooks did on your TikTok accounts (views compared with each account's usual):"]
        else:
            lines = ["How earlier hooks did on this TikTok account (views compared with the account's usual):"]
        lines += [f'- worked: "{h}" ({_times(lift)})' for h, lift in good]
        lines += [f'- flopped: "{h}" ({_times(lift)})' for h, lift in bad]
        lines.append("Write hooks more like the ones that worked, but always about what happens in this clip.")
        return "\n".join(lines)

    # --------------------------------------------------------------- for the Video Creator

    def creator_examples(self, category_id: str = "", count: int = 3) -> tuple[list[Outcome], list[Outcome], bool]:
        """(Creator videos that worked, that flopped, whether they're this category's own), best and worst
        first; empty until there are CREATOR_MIN of them."""
        made = [o for o in self.outcomes if o.creator and (o.hook or o.title)]
        own = [o for o in made if category_id and o.category_id == category_id]
        pool = own if len(own) >= CREATOR_MIN else made
        if len(pool) < CREATOR_MIN:
            return [], [], False
        ranked = sorted(pool, key=lambda o: o.lift, reverse=True)
        good = [o for o in ranked if o.lift >= 1.5][:count]
        bad = [o for o in reversed(ranked) if o.lift <= 0.6][:count]
        return good, bad, pool is own and bool(own)

    def creator_text(self, category_id: str = "") -> str:
        """A paragraph for the Creator's script writer: the hooks and titles of earlier videos that worked and
        flopped (its own once there are a few, else your clips' hooks), or "" when there's too little yet."""
        good, bad, own = self.creator_examples(category_id)

        def line(kind: str, o: Outcome) -> str:
            what = f'"{o.title}"' if o.title else ""
            hook = f'hook "{o.hook}"' if o.hook and o.hook != o.title else ""
            return f"- {kind}: " + ", ".join(x for x in (what, hook) if x) + f" ({_times(o.lift)})"

        lines: list[str] = []
        if good or bad:
            where = "this account" if own else "your accounts"
            lines.append(f"How your earlier videos from this creator did on {where} (views compared with the "
                         "account's usual):")
            lines += [line("worked", o) for o in good] + [line("flopped", o) for o in bad]
        else:
            hooks_good, hooks_bad = self.hook_examples(category_id=category_id)
            if hooks_good or hooks_bad:
                lines.append("Hooks that worked and flopped on your TikTok clips (views compared with the "
                             "account's usual):")
                lines += [f'- worked: "{h}" ({_times(x)})' for h, x in hooks_good]
                lines += [f'- flopped: "{h}" ({_times(x)})' for h, x in hooks_bad]
        if lines:
            lines.append("Learn from how the ones that worked open, how long they are and what they promise; "
                         "don't copy them, and keep to this video's topic and the facts.")
        return "\n".join(lines)

    def origin_note(self) -> str:
        """Creator videos against clips, once both have 3+ results."""
        made = [o.lift for o in self.outcomes if o.creator]
        clips = [o.lift for o in self.outcomes if not o.creator]  # older rows say nothing: all clips
        if len(made) < 3 or len(clips) < 3:
            return ""
        return (f"Creator videos get {_times(statistics.median(made))} your usual views ({len(made)} posts); "
                f"clips {_times(statistics.median(clips))} ({len(clips)} posts).")

    # --------------------------------------------------------------- notes for people

    def length_note(self) -> str:
        """Which clip length does best, when one clearly does (3+ posts in it and 1.3× the rest)."""
        buckets = []
        for lo, hi, name in LENGTHS:
            rows = [o.lift for o in self.outcomes if o.length and lo <= o.length < hi]
            if len(rows) >= 3:
                buckets.append((statistics.median(rows), len(rows), name, lo, hi))
        if len(buckets) < 2:
            return ""
        best = max(buckets)
        rest = [o.lift for o in self.outcomes if o.length and not best[3] <= o.length < best[4]]
        if not rest or best[0] < 1.3 * statistics.median(rest):
            return ""
        return (f"Clips {best[2]} do best ({_times(best[0])} your usual views over {best[1]} posts). "
                f"Consider setting the clip length to match (Settings → Clip finding).")

    def rating_note(self) -> str:
        """Does the AI rating predict views? Compares clips rated 7+ with the rest."""
        high = [o.lift for o in self.outcomes if o.rating is not None and o.rating >= 7]
        low = [o.lift for o in self.outcomes if o.rating is not None and o.rating < 7]
        if len(high) < 3 or len(low) < 3:
            return ""
        h, lo = statistics.median(high), statistics.median(low)
        if h >= 1.3 * lo:
            return (f"Clips the AI rated 7+ get {_times(h)} your usual views, against {_times(lo)} for the rest: "
                    f"raising the quality check's minimum rating would keep more flops out.")
        if lo >= 1.3 * h:
            return ("Clips the AI rated lower are doing better than the high-rated ones, so the AI rating isn't "
                    "a good guide for your accounts yet. Keep the quality check's minimum rating low.")
        return ""

    def summary_lines(self, top: int = 3) -> list[str]:
        """What's working, for the Automation tab and `ytclip results`."""
        if self.empty:
            return []
        n = len(self.outcomes)
        lines = [f"Learning from {n} posted clip{'s' if n != 1 else ''}, median {_times(statistics.median(o.lift for o in self.outcomes))} "
                 "your usual views."]
        ranked = sorted((c for c in self.channels.values() if abs(c.bonus) >= 1), key=lambda c: c.bonus, reverse=True)
        lines += [c.note() for c in ranked if c.bonus > 0][:top]
        lines += [c.note() for c in reversed(ranked) if c.bonus < 0][:top]
        lines += [t for t in (self.length_note(), self.rating_note(), self.origin_note()) if t]
        return lines


def for_settings(s) -> Lessons | None:
    """The lessons to use with these settings: None when learning from results is off. A post
    without its category (from before posting carried it) gets it from picks.json (the category each video
    was picked for)."""
    if not getattr(s, "learn_from_results", False):
        return None
    lessons = Lessons.load()
    if any(not o.category_id for o in lessons.outcomes):
        from .policy import PickLog, pick_category, picks_path

        picked = {p["id"]: pick_category(p) for p in PickLog(picks_path()).picks}
        for o in lessons.outcomes:
            o.category_id = o.category_id or picked.get(o.video_id, "")
    return lessons

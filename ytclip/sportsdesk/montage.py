"""A game's recap reel, planned: which plays, in what order, cut where, and what's said and shown over them.

A reel turns a game's best plays (the Sports tab's ranked highlights, rank.py) into one vertical video of 65 to
85 seconds, long enough for TikTok's Creator Rewards (over a minute, with room for a viewer who leaves early):

    0:00  the opening: the best play, stopped just before its moment, under a hook banner, with the narrator's
          two sentences on what the game was (when a narrator is on)
    0:03  the plays, 4 to 7 of them, each cut to 8-14 s: the build-up, the moment (where the crowd peaks), the
          reaction; never the replay after it. Counted down to the best ("PLAY 4", "PLAY 3"... "THE GAME
          WINNER"), or in the order they happened
    1:10  the last play ends on the crowd, with the final score when it's known

plan() is pure arithmetic over the plays and the game's signals (no video is read), so it's quick to test and
to show before anything is rendered. story() asks the AI (any of them: Claude, Gemini or the local one) for the
words: the narrator's opening, the hook banner, a badge for each play, the caption and hashtags. It's told to
say only what the plays and the commentary say, so it never makes up a score or a comeback; without an AI the
words come from the plays' own titles.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable

import numpy as np

from .. import sports

Log = Callable[[str], None]

REEL_MIN = 65.0  # seconds: Creator Rewards counts "over a minute"; a few seconds' room for the encoder
REEL_MAX = 85.0
REEL_AIM = 72.0  # what the plays are sized to: the middle of the range, leaning short (people leave)
MIN_PLAYS, MAX_PLAYS = 4, 7
PLAY_MIN, PLAY_MAX = 8.0, 14.0  # seconds each play gets
PLAY_STRETCH = 17.0  # at most, when a short game has too few plays to reach a minute otherwise
PLAY_LONG = 24.0  # and with only three plays to show: each gets its whole build-up and its reaction
TEASER = 3.2  # the opening: the best play up to just before its moment
TEASER_STOP = 0.35  # seconds before the moment the opening cuts away (the payoff is kept for the end)
TRANSITION = 0.25  # each change of play: a quick whip, flash or slide (and a whoosh)
SETUP_SHARE = 0.64  # of a play's seconds, how many come before its moment (the build-up matters most)
SNAP = 0.7  # a shot change this close to a cut point becomes the cut point (cleaner cuts)
REPLAY_GUARD = 1.0  # a play stops this long before a replay starts

WINNER = re.compile(r"\b(?:game[- ]?winn(?:er|ing)|buzzer[- ]?beater|walk[- ]?off|winner|winning (?:goal|shot|"
                    r"basket|run|touchdown|try)|golden goal|clinch(?:er|es|ed)?|decider|knock ?out|ko\b|tko|"
                    r"submission|match point|championship point)", re.I)


@dataclass
class Segment:
    """One stretch of the game's video in the reel."""

    kind: str  # "teaser" | "play"
    hid: str  # the highlight it's from
    start: float  # source seconds
    end: float
    moment: float  # source seconds of the play's moment (the crowd's peak)
    rank: int = 0  # the play's rank in the game (1 = best)
    label: str = ""  # "PLAY 4", "THE GAME WINNER"
    play: str = ""  # "Poster dunk"
    title: str = ""
    players: list[str] = field(default_factory=list)
    badge: str = ""  # the lower-third: "LEBRON JAMES · POSTER DUNK"

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class Story:
    """The reel's words."""

    opening: str = ""  # the narrator's two sentences ("" = no narrator)
    hook: str = ""  # the banner over the opening
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    badges: dict[str, str] = field(default_factory=dict)  # highlight id -> badge text
    written_by: str = ""  # the AI model, or "" (from the plays' own titles)
    note: str = ""  # why the AI didn't write it, if it didn't


@dataclass
class Reel:
    segments: list[Segment]
    transition: float = TRANSITION
    order: str = "countdown"
    notes: list[str] = field(default_factory=list)  # what the viewer of the plan should know
    story: Story = field(default_factory=Story)
    final: str = ""  # "Lakers 108-106 Warriors", when the score is known
    style: str = "mix"  # how one play hands over to the next: mix | dissolve | whip | zoom (reel.STYLES)
    kind: str = "plays"  # "plays" (a game's best) | "events" (the moments an event search found)
    title: str = ""  # an event reel's subject: "Red card"

    @property
    def duration(self) -> float:
        n = len(self.segments)
        return sum(s.length for s in self.segments) - max(0, n - 1) * self.transition

    def starts(self) -> list[float]:
        """Each segment's start in the reel (they overlap by the transition)."""
        out, at = [], 0.0
        for s in self.segments:
            out.append(round(at, 3))
            at += s.length - self.transition
        return out

    @property
    def plays(self) -> list[Segment]:
        return [s for s in self.segments if s.kind == "play"]

    @property
    def eligible(self) -> bool:
        return self.duration > REEL_MIN - 0.01

    def to_json(self) -> dict:
        return {"duration": round(self.duration, 2), "transition": self.transition, "order": self.order,
                "style": self.style, "kind": self.kind, "title": self.title,
                "eligible": self.eligible, "notes": self.notes, "final": self.final,
                "starts": self.starts(), "segments": [asdict(s) for s in self.segments], "story": asdict(self.story)}


class ReelError(RuntimeError):
    """A plain-English reason a reel can't be made."""


# ---------------------------------------------------------------- the plan


def moment_of(h: dict, crowd: np.ndarray | None) -> float:
    """Source seconds of a play's moment: where the crowd is loudest inside it (else 60% of the way in)."""
    a, b = float(h["start"]), float(h["end"])
    if crowd is not None and len(crowd):
        i0, i1 = max(0, int(a)), min(len(crowd), int(np.ceil(b)))
        if i1 - i0 >= 2:
            seg = np.asarray(crowd[i0:i1], float)
            if np.ptp(seg) > 1e-6:
                # The roar lags the play by a beat: the moment is a second before the loudest second.
                return max(a, min(b, i0 + float(np.argmax(seg)) + 0.5 - 1.0))
    return a + 0.6 * (b - a)


def cut_around(moment: float, length: float, lo: float, hi: float, replays: list[float]) -> tuple[float, float]:
    """A span of `length` around `moment` (SETUP_SHARE before it), inside [lo, hi] and ending before a replay."""
    for r in sorted(replays):
        if moment + 1.5 < r < hi + REPLAY_GUARD:
            hi = min(hi, r - REPLAY_GUARD)
            break
    length = max(1.0, min(length, hi - lo))
    start = moment - SETUP_SHARE * length
    start = max(lo, min(start, hi - length))
    return start, start + length


def snap(start: float, end: float, cuts: list[float], moment: float) -> tuple[float, float]:
    """Move the ends onto nearby shot changes: a play that starts on a new shot looks cut, not chopped."""
    after = [c for c in cuts if start <= c <= start + SNAP and c < moment - 2.0]
    if after:
        start = after[0]
    before = [c for c in cuts if end - SNAP <= c <= end and c > moment + 1.5]
    if before:
        end = before[-1]
    return start, end


def pick_plays(highlights: list[dict], ids: list[str] | None = None) -> list[dict]:
    """The plays for the reel, best first: the ones asked for (or ticked), topped up from the best of the rest
    to MIN_PLAYS, at most MAX_PLAYS."""
    ranked = sorted(highlights, key=lambda h: (h.get("rank") or 99, -(h.get("score") or 0)))
    want = set(ids) if ids else {h["id"] for h in ranked if h.get("chosen")}
    first = [h for h in ranked if h["id"] in want]
    rest = [h for h in ranked if h["id"] not in want]
    out = first[:MAX_PLAYS]
    while len(out) < MIN_PLAYS and rest:
        out.append(rest.pop(0))
    return out, rest


def is_winner(h: dict) -> bool:
    return bool(WINNER.search(f"{h.get('play', '')} {h.get('title', '')}"))


def labels_for(order: list[dict], best: dict, how: str) -> list[str]:
    """Each play's on-screen label, in the reel's order."""
    if how in ("timeline", "climax"):
        return [("THE GAME WINNER" if h is best and is_winner(h) else f"#{h.get('rank') or i + 1} PLAY")
                for i, h in enumerate(order)]
    n = len(order)
    out = []
    for i, h in enumerate(order):
        k = n - i
        out.append(("THE GAME WINNER" if is_winner(h) else "NUMBER 1") if k == 1 else f"PLAY {k}")
    return out


def plan(game: dict, highlights: list[dict], *, crowd: np.ndarray | None, cues=(), scene_cuts=(),
         duration: float = 0.0, ids: list[str] | None = None, order: str = "countdown",
         teaser: bool = True, transition: float = TRANSITION, style: str = "mix") -> Reel:
    """The reel's cut: which plays, in what order, from where to where, sized to 65-85 s.

    `order`: "countdown" (worst to best), "timeline" (as they happened) or "climax" (as they happened, with
    the best play held back for the end: the game's story, then its biggest moment)."""
    TRANSITION = max(0.1, min(0.6, float(transition)))  # noqa: N806 - this plan's own, used as the constant below
    if not highlights:
        raise ReelError("This game has no highlights yet: find its highlights first.")
    plays, spare = pick_plays(highlights, ids)
    if len(plays) < 2:
        raise ReelError("A reel needs at least two plays; this game has only one. Find its highlights again, or "
                        "try a longer version of its footage.")
    duration = duration or max(float(h["end"]) for h in highlights) + 5
    replays = sports.replay_times(list(cues)) if cues else []
    cuts = sorted(float(c) for c in scene_cuts)
    notes: list[str] = []

    def room(h: dict) -> tuple[float, float]:
        # A play may reach a little beyond its own highlight for build-up and reaction, not into another one.
        lo, hi = max(0.0, float(h["start"]) - 4.0), min(duration, float(h["end"]) + 5.0)
        for o in highlights:
            if o is h:
                continue
            if float(o["end"]) <= float(h["start"]) and float(o["end"]) > lo:
                lo = float(o["end"])
            if float(o["start"]) >= float(h["end"]) and float(o["start"]) < hi:
                hi = float(o["start"])
        return lo, hi

    def fixed() -> float:
        return (TEASER - TRANSITION if teaser else 0.0) - TRANSITION * (len(plays) - 1)

    def each(cap: float) -> float:
        return max(PLAY_MIN, min(cap, (REEL_AIM - fixed()) / max(1, len(plays))))

    # Enough plays to reach a minute at their longest; then not so many that they're cut too short.
    while len(plays) < MAX_PLAYS and spare and fixed() + len(plays) * PLAY_MAX < REEL_MIN + 1:
        plays.append(spare.pop(0))
    while len(plays) > MIN_PLAYS and fixed() + len(plays) * PLAY_MIN > REEL_MAX:
        plays.pop()

    want, longest = each(PLAY_MAX), PLAY_STRETCH
    if fixed() + want * len(plays) < REEL_MIN:
        want = each(PLAY_STRETCH)
    if fixed() + want * len(plays) < REEL_MIN:  # three plays (or two): longer still, so the reel passes a minute
        want, longest = each(PLAY_LONG), PLAY_LONG

    spans: dict[str, tuple[float, float, float]] = {}
    for h in plays:
        m = moment_of(h, crowd)
        lo, hi = room(h)
        a, b = cut_around(m, want, lo, hi, replays)
        a, b = snap(a, b, cuts, m)
        spans[h["id"]] = (a, b, m)

    # Plays that couldn't take their share (the footage started or a replay came) hand it to the others.
    def total() -> float:
        return fixed() + sum(b - a for a, b, _ in spans.values())

    for _ in range(3):
        short = REEL_AIM - total()
        if short <= 0.3:
            break
        growable = [h for h in plays if spans[h["id"]][1] - spans[h["id"]][0] < longest - 0.2]
        if not growable:
            break
        extra = short / len(growable)
        for h in growable:
            a, b, m = spans[h["id"]]
            lo, hi = room(h)
            a2, b2 = cut_around(m, min(longest, b - a + extra), lo, hi, replays)
            spans[h["id"]] = (a2, b2, m)
    while total() > REEL_MAX and len(plays) > MIN_PLAYS:
        spans.pop(plays.pop()["id"])
    if total() > REEL_MAX:  # four long plays: trim each toward PLAY_MIN
        over = total() - REEL_AIM
        for h in plays:
            a, b, m = spans[h["id"]]
            cut = min(over / len(plays), (b - a) - PLAY_MIN)
            if cut > 0:
                spans[h["id"]] = cut_around(m, (b - a) - cut, a, b, []) + (m,)

    best = plays[0]
    if order == "timeline":
        ordered = sorted(plays, key=lambda h: spans[h["id"]][0])
    elif order == "climax":
        ordered = sorted((h for h in plays if h is not best), key=lambda h: spans[h["id"]][0]) + [best]
    else:
        ordered = list(reversed(plays))  # the best last
    labels = labels_for(ordered, best, order)
    segments: list[Segment] = []
    if teaser:
        _a, _b, m = spans[best["id"]]
        lo, _hi = room(best)
        stop = m - TEASER_STOP
        segments.append(Segment("teaser", best["id"], max(lo, stop - TEASER), stop, m, best.get("rank") or 1,
                                play=best.get("play") or "", title=best.get("title") or ""))
        if segments[0].length < 1.5:  # the footage starts right at the moment: no room for an opening
            segments.pop()
    for h, label in zip(ordered, labels):
        a, b, m = spans[h["id"]]
        segments.append(Segment("play", h["id"], round(a, 3), round(b, 3), round(m, 3), h.get("rank") or 0, label,
                                h.get("play") or "", h.get("title") or "", list(h.get("players") or [])[:3]))
    reel = Reel(segments, TRANSITION, order, notes, final=final_score(game), style=style)
    if not reel.eligible:
        notes.append(f"It comes to {reel.duration:.0f} s, under a minute: this game hasn't enough strong plays for "
                     "Creator Rewards. Find the highlights in a longer version of its footage (Extended highlights or "
                     "the Full game) for more.")
    return reel


# ---------------------------------------------------------------- a reel of found moments

EVENT_MIN, EVENT_MAX = 9.0, 22.0  # seconds each found moment gets
EVENT_SETUP = 0.42  # of a moment's seconds, how many come before it (the aftermath is the point of these)
MAX_EVENTS = 6


def plan_events(game: dict, hits: list[dict], *, duration: float = 0.0, scene_cuts=(), order: str = "timeline",
                title: str = "", teaser: bool = True, transition: float = TRANSITION, style: str = "mix") -> Reel:
    """A reel of the moments an event search found (semantic_search.EventHit as dicts): every red card, each
    argued call, the flashpoints of a game. Each keeps the search's own cut when it fits, else is trimmed
    around its moment; they run as they happened, or (climax, countdown) with the surest match last."""
    hits = [h for h in hits if h.get("end", 0) - h.get("start", 0) >= 3.0]
    if not hits:
        raise ReelError("There are no found moments to make a reel from: search the game for an event first.")
    best_first = sorted(hits, key=lambda h: -(h.get("score") or 0))[:MAX_EVENTS]
    n = len(best_first)
    tr = max(0.1, min(0.6, float(transition)))
    fixed = (TEASER - tr if teaser and n > 1 else 0.0) - tr * (n - 1)
    want = max(EVENT_MIN, min(EVENT_MAX, (REEL_AIM - fixed) / n))
    cuts = sorted(float(c) for c in scene_cuts)
    spans: dict[str, tuple[float, float, float]] = {}
    for h in best_first:
        a, b, m = float(h["start"]), float(h["end"]), float(h.get("t_event", h["start"]))
        if b - a > want + 0.5:  # trim around the moment, keeping the search's own edges where they're inside
            start = max(a, m - EVENT_SETUP * want)
            end = min(b, start + want)
            start = max(a, end - want)
            a, b = snap(start, end, cuts, m)
        spans[h["id"]] = (a, b, m)
    best = best_first[0]
    by_time = sorted(best_first, key=lambda h: spans[h["id"]][0])
    if order == "countdown":
        ordered = list(reversed(best_first))
    elif order == "climax":
        ordered = [h for h in by_time if h is not best] + [best]
    else:
        ordered = by_time
    segments: list[Segment] = []
    if teaser and n > 1:
        a, _b, m = spans[best["id"]]
        stop = max(a + 1.5, m - TEASER_STOP)
        seg = Segment("teaser", best["id"], max(a, stop - TEASER), stop, m, 1, play=best.get("label") or "")
        if seg.length >= 1.5:
            segments.append(seg)
    for i, h in enumerate(ordered, 1):
        a, b, m = spans[h["id"]]
        ev = h.get("feed") or {}
        when = ev.get("when") or ""
        label = (h.get("label") or "MOMENT").upper() if n == 1 else f"{(h.get('label') or 'Moment').upper()} {i}/{n}"
        segments.append(Segment("play", h["id"], round(a, 3), round(b, 3), round(m, 3), i, label,
                                h.get("label") or "", h.get("quote") or "", list(ev.get("players") or [])[:3],
                                badge=h.get("badge") or (f"{when} {h.get('label', '')}".strip().upper())))
    notes: list[str] = []
    reel = Reel(segments, tr, order, notes, final=final_score(game), style=style, kind="events", title=title)
    if not reel.eligible:
        notes.append(f"It comes to {reel.duration:.0f} s, under a minute, so it isn't for Creator Rewards: the "
                     f"search found {n} moment{'s' if n != 1 else ''} here. It's still a clip worth posting.")
    if any(not h.get("certain", True) for h in ordered):
        notes.append("Some of these are only talked about in the commentary and may not show the event: watch "
                     "the reel before posting.")
    return reel


def event_story(game: dict, reel: Reel, narrator: bool) -> Story:
    """An event reel's words, from what the search and the feed found (nothing here is made up by an AI: the
    badges are the feed's own minute and player, the hook is the subject)."""
    matchup = game.get("matchup") or game.get("title") or "this game"
    n = len(reel.plays)
    what = (reel.title or "moment").strip()
    plural = what if n == 1 else (what + "s" if not what.lower().endswith(("s", "review", "call")) else what)
    words = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six"}.get(n, str(n))
    opening = (f"{matchup}. {words} {plural.lower()} that changed the game, starting here." if narrator and n > 1
               else f"{matchup}. The {what.lower()} everyone is talking about." if narrator else "")
    hook = tidy(f"EVERY {plural}" if n > 1 else f"THE {what}", 48).upper()
    sp = sports.get(game.get("sport", "")) or sports.SPORTS["any"]
    tags = [t for t in (re.sub(r"\W", "", game.get("league") or ""), re.sub(r"\W", "", game.get("home") or ""),
                        re.sub(r"\W", "", game.get("away") or ""), sp.id if sp.id != "any" else "sports",
                        re.sub(r"\W", "", what.title())) if t]
    badges = {seg.hid: tidy(seg.badge, 34).upper() for seg in reel.plays if seg.badge}
    return Story(opening, hook, tidy(f"{matchup}: {'every ' + plural.lower() if n > 1 else 'the ' + what.lower()}", 150),
                 list(dict.fromkeys(tags))[:5], badges)


def feed_badges(reel: Reel, timeline: list[dict]) -> dict[str, str]:
    """Lower-thirds from the game's official timeline: for each play, the scoring event (or card) the feed
    places inside it, as "90+4' WINNER · E. HAALAND". `timeline` is [{"video": seconds, "badge": text,
    "scoring": bool, "kind": str}] (the desk keeps it after an event search with a feed)."""
    out: dict[str, str] = {}
    for seg in reel.plays:
        inside = [e for e in timeline if seg.start - 2.0 <= float(e.get("video", -1)) <= seg.end + 2.0 and e.get("badge")]
        if not inside:
            continue
        pick = min(inside, key=lambda e: (not e.get("scoring"), abs(float(e["video"]) - seg.moment)))
        text = str(pick["badge"])
        if seg.label == "THE GAME WINNER" and pick.get("scoring"):
            text = re.sub(r"\b(GOAL|TOUCHDOWN|BASKET|SCORE)\b", "WINNER", text, count=1)
        out[seg.hid] = tidy(text, 34).upper()
    return out


def final_score(game: dict) -> str:
    """'Real Madrid 3-1 Barcelona' when the title gave the score."""
    score, home, away = game.get("score") or "", game.get("home") or "", game.get("away") or ""
    if score and home and away:
        return f"{home} {score} {away}"
    return ""


# ---------------------------------------------------------------- the words

STORY_SYSTEM = """You write the words for a 70-second vertical recap of one {sport} game, cut from its broadcast \
footage: an opening, then its best plays one after another.

Write:
- opening: what a narrator says over the first 4 to 6 seconds, at most two short sentences and 26 words. It sets \
up the game's story so a viewer stays: who played, what was at stake or how it went, and that the best is coming. \
Plain spoken English, present or past tense, no hashtags, no emoji.
- hook: a banner over the opening, at most 7 words, in capitals-friendly wording. It makes people stay for the \
end without lying ("WAIT FOR NUMBER 1", "THE FINISH WAS UNREAL").
- badges: for each play, the words on its lower-third: the player (when known) and what they did, at most 30 \
characters ("STEPH CURRY · 34-FOOT DAGGER", "SAKA · CURLED FINISH").
- caption: the post's caption, at most 150 characters, no hashtags.
- hashtags: 3 to 5, without #.

Say only what the information below says or the commentary makes plain. Never invent a score, a deficit, a \
comeback, a record, a quarter, a minute or a player's name. If you don't know who scored, leave the name out. \
Don't claim to be the league or the broadcaster."""

STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "opening": {"type": "string"},
        "hook": {"type": "string"},
        "badges": {"type": "array", "items": {"type": "object", "properties": {
            "n": {"type": "integer"}, "text": {"type": "string"}}, "required": ["n", "text"],
            "additionalProperties": False}},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["opening", "hook", "badges", "caption", "hashtags"],
    "additionalProperties": False,
}


def story_prompt(game: dict, reel: Reel, commentary: dict[str, str]) -> str:
    sp = sports.get(game.get("sport", "")) or sports.SPORTS["any"]
    lines = [f"Game: {game.get('matchup') or game.get('title')}",
             f"Sport: {sp.label}" + (f" · {game['league']}" if game.get("league") else ""),
             f"Video title: {game.get('title', '')}"]
    if reel.final:
        lines.append(f"Final score (from the title): {reel.final}")
    lines.append(f"Order: {'counting down to the best play' if reel.order == 'countdown' else 'as they happened'}")
    lines.append("")
    lines.append("Plays, in the reel's order:")
    for i, seg in enumerate(reel.plays, 1):
        bits = [f"{i}. [{seg.label}]", seg.play or "", f"\"{seg.title}\"" if seg.title else ""]
        if seg.players:
            bits.append("players heard: " + ", ".join(seg.players))
        lines.append(" ".join(b for b in bits if b))
        said = commentary.get(seg.hid, "")
        if said:
            lines.append(f"   Commentary: {said}")
    return "\n".join(lines)


def tidy(text: str, limit: int) -> str:
    text = " ".join(str(text or "").replace("#", "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:·-") if cut else text[:limit]


def plain_story(game: dict, reel: Reel, narrator: bool) -> Story:
    """The words without an AI, from the plays' own titles."""
    matchup = game.get("matchup") or game.get("title") or "this game"
    n = len(reel.plays)
    best = reel.plays[-1] if reel.order == "countdown" else max(reel.plays, key=lambda s: -s.rank)
    words = {2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven"}.get(n, str(n))
    score = (game.get("score") or "").strip()
    then = "the best is saved for last" if reel.order == "countdown" else "here they are as they happened"
    opening = (f"{matchup}{', ' + score if score else ''}. {words} plays told the story, and {then}."
               if narrator else "")
    hook = "WAIT FOR THE GAME WINNER" if best.label == "THE GAME WINNER" else f"THE TOP {n} PLAYS"
    badges = {}
    for seg in reel.plays:
        who = seg.players[0] if seg.players else ""
        what = seg.play or ""  # (a title like "Play at 1:29" says nothing on screen)
        text = tidy(" · ".join(x for x in (who, what) if x), 30).upper()
        if text:
            badges[seg.hid] = text
    sp = sports.get(game.get("sport", "")) or sports.SPORTS["any"]
    tags = [t for t in (re.sub(r"\W", "", game.get("league") or ""), re.sub(r"\W", "", game.get("home") or ""),
                        re.sub(r"\W", "", game.get("away") or ""), sp.id if sp.id != "any" else "sports",
                        "highlights") if t]
    return Story(opening, hook, tidy(f"{matchup}: the top {n} plays", 150), list(dict.fromkeys(tags))[:5], badges)


def story(game: dict, reel: Reel, s, commentary: dict[str, str], *, narrator: bool, log: Log | None = None,
          check: Callable[[], None] | None = None) -> Story:
    """The reel's words, by the AI chosen in Settings → AI, else from the plays' own titles."""
    from .. import ai

    plain = plain_story(game, reel, narrator)
    if not ai.order(s):
        plain.note = "No AI is set up, so the words come from the plays' own titles."
        return plain
    sp = sports.get(game.get("sport", "")) or sports.SPORTS["any"]
    prompt = story_prompt(game, reel, commentary)
    if getattr(s, "learn_from_results", True):  # the reel hooks that did best for this sport (learner.py)
        from ..intelligence import learner

        few = learner.guide(s, f"{game.get('matchup') or ''} {game.get('title') or ''}",
                            sport=sp.id if sp.id != "any" else "", purpose="reel")
        if few:
            prompt += "\n\n" + few
    try:
        ans = ai.ask_json(s, "polish", STORY_SYSTEM.format(sport=sp.label.split(" (")[0].lower()),
                          prompt, STORY_SCHEMA, effort="low", max_tokens=2500,
                          log=log, check=check, what="write the reel's opening and badges")
    except ai.AIError as exc:
        plain.note = f"The AI couldn't write the words ({exc}), so they come from the plays' own titles."
        return plain
    d = ans.data
    out = Story(written_by=ans.model)
    out.opening = tidy(d.get("opening", ""), 190) if narrator else ""
    if narrator and len(out.opening.split()) > 32:
        out.opening = " ".join(out.opening.split()[:30]).rstrip(",;") + "."
    out.hook = tidy(d.get("hook", ""), 48).upper() or plain.hook
    out.caption = tidy(d.get("caption", ""), 150) or plain.caption
    tags = [re.sub(r"[^\w]", "", str(t)) for t in d.get("hashtags") or []]
    out.hashtags = [t for t in dict.fromkeys(tags) if t][:5] or plain.hashtags
    by_n = {int(b.get("n") or 0): b.get("text", "") for b in d.get("badges") or [] if isinstance(b, dict)}
    for i, seg in enumerate(reel.plays, 1):
        text = tidy(by_n.get(i, ""), 32).upper()
        ok = text if text and _names_ok(text, seg, game, commentary) else plain.badges.get(seg.hid, "")
        if ok:
            out.badges[seg.hid] = ok
    if narrator and not out.opening:
        out.opening = plain.opening
    return out


def _names_ok(badge: str, seg: Segment, game: dict, commentary: dict[str, str]) -> bool:
    """A badge may only name someone the plays, the title or the commentary named (no invented scorers)."""
    head = badge.split("·")[0].strip()
    words = [w for w in re.findall(r"[A-Z][A-Z'’.-]{2,}", head)]
    if not words:
        return True
    known = " ".join([*seg.players, seg.title, seg.play, game.get("title", ""), game.get("home", ""),
                      game.get("away", ""), commentary.get(seg.hid, "")]).upper()
    return all(w in known for w in words if w not in GENERIC_WORDS)


GENERIC_WORDS = {"THE", "AND", "WITH", "FROM", "DUNK", "GOAL", "SHOT", "THREE", "POSTER", "BLOCK", "SAVE",
                 "STEAL", "PASS", "FINISH", "WINNER", "GAME", "BUZZER", "BEATER", "DAGGER", "CURLER", "HEADER",
                 "STRIKE", "TOUCHDOWN", "CATCH", "HOME", "RUN", "KNOCKOUT", "ACE", "PLAY", "FOOT", "POINTER",
                 "LAYUP", "ALLEY", "OOP", "CROSSOVER", "TRY", "SIX", "WICKET", "OVERTAKE", "PUTT", "EAGLE",
                 "BIRDIE", "PENALTY", "FREE", "KICK", "VOLLEY", "BICYCLE", "SCREAMER", "STOP", "SACK", "PICK",
                 "INTERCEPTION", "HIT", "SLAM", "BANG", "EQUALIZER", "EQUALISER", "LATE", "DEEP", "LONG", "FAST",
                 "BREAK", "CHASE", "DOWN", "WIN", "CLUTCH", "BIG", "HUGE", "WILD", "NASTY", "INSANE", "UNREAL"}

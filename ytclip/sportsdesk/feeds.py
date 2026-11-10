"""A game's play-by-play from a public feed: the cards, goals, fouls and scores, each with its game clock.

The commentary doesn't name everything (a quiet booking, a technical foul under crowd noise, a game with no
commentary at all), and it never says the minute. A feed does. The event search (semantic_search.py) reads
both, and align.py works out where in the video each feed event is.

Sources, all of them public data asked for politely (one or two requests a game, kept on disk afterwards):

* **ESPN** - the JSON behind espn.com's scoreboards (site.api.espn.com), no key. It isn't a documented
  developer API, so ESPN may change it; everything here reads it loosely and a game it can't read is simply a
  game without a feed. Soccer gives goals, cards, substitutions and its text commentary with the minute; the
  US sports give every play with the period, the game clock and usually the time of day it happened.
* **API-Football** (api-football.com, `x-apisports-key`) - a documented API with a free tier (100 requests a
  day). Soccer only: goals, cards, substitutions and VAR decisions for many more leagues than ESPN lists.
* **A file** - your own timeline as JSON or CSV (`--feed timeline.json`): rows of minute or period+clock (or
  `video`, the second in the video itself), kind, text, team and player.

Not used: SofaScore and FotMob. Their apps' data sits behind request signing and bot checks, and their terms
don't allow reading it this way, so there's nothing here that gets around that.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .games import team_key

ESPN_URL = os.environ.get("CLIPMONSTER_ESPN_API", "https://site.api.espn.com/apis/site/v2/sports").rstrip("/")
APIF_URL = os.environ.get("CLIPMONSTER_API_FOOTBALL", "https://v3.football.api-sports.io").rstrip("/")
TIMEOUT = 20.0
DAY = 86400.0
CACHE_FINAL = 30 * DAY  # a finished game's feed doesn't change
CACHE_LIVE = 600.0

Log = Callable[[str], None]
Get = Callable[[str, dict], dict]


class FeedError(RuntimeError):
    """A plain-English reason a feed couldn't be read."""


# What a sport's clock does: (periods, seconds each, counts down, real seconds per clock second in a broadcast).
# Soccer's clock runs with the world (1.0); the others stop, so a 12-minute quarter takes about 27.
CLOCKS = {
    "soccer": (2, 45 * 60, False, 1.0),
    "basketball": (4, 12 * 60, True, 2.35),
    "football": (4, 15 * 60, True, 3.1),
    "hockey": (3, 20 * 60, True, 1.95),
    "baseball": (9, 0, False, 0.0),  # innings: no clock
}

# ESPN's path for each sport, and for the league names a game's title or the Sports filter gives.
ESPN_SPORT = {"soccer": "soccer", "basketball": "basketball", "football": "football", "hockey": "hockey",
              "baseball": "baseball"}
ESPN_LEAGUES = {
    "soccer": (("eng.1", ("premier league", "epl", "english premier")), ("esp.1", ("la liga", "laliga")),
               ("ger.1", ("bundesliga",)), ("ita.1", ("serie a",)), ("fra.1", ("ligue 1",)),
               ("uefa.champions", ("champions league", "ucl")), ("uefa.europa", ("europa league",)),
               ("usa.1", ("mls", "major league soccer")), ("eng.fa", ("fa cup",)),
               ("eng.league_cup", ("carabao", "league cup", "efl cup")), ("eng.2", ("championship",)),
               ("fifa.world", ("world cup",)), ("uefa.euro", ("euros", "euro 20", "european championship")),
               ("usa.nwsl", ("nwsl",)), ("mex.1", ("liga mx",))),
    "basketball": (("nba", ("nba",)), ("wnba", ("wnba",)),
                   ("mens-college-basketball", ("ncaa", "college", "march madness"))),
    "football": (("nfl", ("nfl", "super bowl")), ("college-football", ("ncaa", "college", "cfb"))),
    "hockey": (("nhl", ("nhl", "stanley cup")),),
    "baseball": (("mlb", ("mlb", "world series")),),
}

# A feed's own words for an event -> the kind the event search knows it by. First match wins.
KINDS: tuple[tuple[str, str], ...] = (
    ("second_yellow", r"second yellow|2nd yellow|yellow[- ]red"),
    ("red_card", r"red card|sent off|sending off|dismiss"),
    ("yellow_card", r"yellow card|booked|caution"),
    ("penalty_missed", r"penalty (?:kick )?(?:missed|saved)|missed penalty|penalty - (?:missed|saved)|"
                       r"(?:misses|saves) (?:the |a )?penalty"),
    ("own_goal", r"own goal"),
    ("var", r"\bvar\b|video (?:assistant|review)|goal (?:cancelled|disallowed)|penalty (?:confirmed|cancelled)|"
            r"card (?:upgrade|reviewed)|replay review|instant replay|coach'?s challenge|challenge\b|"
            r"play (?:under )?review|overturned|call stands|call (?:is )?confirmed"),
    ("penalty_goal", r"penalty (?:kick )?(?:goal|scored)|goal - penalty|penalty - scored|converts the penalty"),
    ("offside", r"offside"),
    ("goal", r"\bgoal\b|scores!"),
    ("ejection", r"eject|disqualif|game misconduct|match penalty|thrown out"),
    ("technical_foul", r"technical foul|\btechnical\b|double technical|delay of game technical"),
    ("flagrant_foul", r"flagrant"),
    ("fight", r"fight(?:ing)?\b|roughing|brawl|unsportsmanlike|unnecessary roughness|altercation"),
    ("flag", r"pass interference|\bpenalty\b|holding|roughing the passer|targeting|face ?mask|personal foul|"
             r"false start|offside[s]? on"),
    ("turnover", r"intercept|fumble|turnover|\bsteal\b|pick[- ]six"),
    ("touchdown", r"touchdown"),
    ("field_goal_missed", r"field goal (?:missed|no good|blocked)|missed field goal"),
    ("three_pointer", r"three point|3-?pt|three-point"),
    ("dunk", r"dunk"),
    ("block", r"\bblocks?\b|blocked shot"),
    ("home_run", r"home run|homer"),
    ("strikeout", r"strike(?:s)? ?out|struck out"),
    ("substitution", r"substitution|\bsubst\b|enters the game|\bsub\b"),
    ("timeout", r"time ?out"),
    ("period_start", r"kick ?off|start (?:of )?(?:the )?(?:1st|2nd|3rd|4th|first|second|third|fourth)? ?(?:half|quarter|period)|"
                     r"start 2nd half|jump ?ball|tip ?off|face ?off to start"),
    ("period_end", r"half ?time|end (?:of )?(?:the )?(?:regular time|game|match|1st|2nd|3rd|4th|first|second|third|fourth|period|"
                   r"quarter|half)|full ?time|final whistle|end of regulation"),
    ("foul", r"\bfoul\b|shooting foul|personal foul|loose ball foul|offensive foul"),
    ("save", r"\bsave[sd]?\b"),
    ("shot", r"\bshot\b|attempt|makes|misses"),
)
_KIND_RE = [(k, re.compile(p, re.I)) for k, p in KINDS]
SCORING = ("goal", "own_goal", "penalty_goal", "touchdown", "home_run", "three_pointer", "dunk")
KIND_WORDS = {
    "red_card": "RED CARD", "second_yellow": "SECOND YELLOW", "yellow_card": "YELLOW CARD", "goal": "GOAL",
    "own_goal": "OWN GOAL", "penalty_goal": "PENALTY", "penalty_missed": "PENALTY MISSED", "var": "VAR",
    "offside": "OFFSIDE", "ejection": "EJECTED", "technical_foul": "TECHNICAL FOUL", "flagrant_foul": "FLAGRANT FOUL",
    "fight": "ALTERCATION", "flag": "FLAG", "turnover": "TURNOVER", "touchdown": "TOUCHDOWN", "dunk": "DUNK",
    "three_pointer": "THREE", "block": "BLOCK", "home_run": "HOME RUN", "strikeout": "STRIKEOUT", "foul": "FOUL",
    "save": "SAVE", "field_goal_missed": "MISSED FIELD GOAL",
}


def kind_of(*words: str) -> str:
    """The event kind a feed's type and text describe ("other" when nothing fits)."""
    text = " ".join(w for w in words if w)
    for kind, rx in _KIND_RE:
        if rx.search(text):
            return kind
    return "other"


@dataclass
class FeedEvent:
    kind: str  # one of KINDS' names, or "other"
    text: str  # the feed's own sentence
    period: int = 0  # 1-based; 0 = unknown
    elapsed: float | None = None  # seconds of game clock since the period began
    minute: str = ""  # soccer: "90+4'"
    clock: str = ""  # a countdown clock as shown: "11:42"
    wallclock: float | None = None  # when it happened (epoch seconds), when the feed says
    video: float | None = None  # where it is in the video, when a file feed says outright
    team: str = ""
    players: list[str] = field(default_factory=list)
    home_score: int | None = None
    away_score: int | None = None
    scoring: bool = False
    raw: str = ""  # the feed's type name

    @property
    def when(self) -> str:
        """'90+4\\'' or 'Q4 0:58' or ''."""
        if self.minute:
            return self.minute
        if self.clock:
            return f"P{self.period} {self.clock}" if self.period else self.clock
        return ""

    def sentence(self) -> str:
        """One line the event search can compare a question with."""
        bits = [KIND_WORDS.get(self.kind, "").capitalize(), self.text]
        if self.team and self.team.lower() not in self.text.lower():
            bits.append(f"({self.team})")
        who = [p for p in self.players if p.lower() not in self.text.lower()]
        if who:
            bits.append(", ".join(who))
        return ". ".join(b for b in bits if b)

    def to_json(self) -> dict:
        return asdict(self) | {"when": self.when}


@dataclass
class Feed:
    source: str  # espn | api-football | file
    sport: str
    events: list[FeedEvent]
    event_id: str = ""
    league: str = ""
    home: str = ""
    away: str = ""
    home_score: int | None = None
    away_score: int | None = None
    note: str = ""

    @property
    def name(self) -> str:
        return {"espn": "ESPN", "api-football": "API-Football", "file": "your timeline file"}.get(self.source,
                                                                                                    self.source)

    def to_json(self) -> dict:
        return {"source": self.source, "sport": self.sport, "event_id": self.event_id, "league": self.league,
                "home": self.home, "away": self.away, "home_score": self.home_score, "away_score": self.away_score,
                "note": self.note, "events": [e.to_json() for e in self.events]}


# ---------------------------------------------------------------- asking, politely


def cache_dir() -> Path:
    from ..settings import local_dir

    return local_dir("cache", "feeds")


def http_get(url: str, headers: dict | None = None, *, max_age: float = CACHE_LIVE, opener=None,
             now: Callable[[], float] = time.time) -> dict:
    """The JSON at `url`. An answer is kept on disk for `max_age` seconds, so a game is asked for once."""
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    path = cache_dir() / f"{key}.json"
    try:
        if now() - path.stat().st_mtime < max_age:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    req = urllib.request.Request(url, headers={"User-Agent": "ClipMonster", "Accept": "application/json",
                                               **(headers or {})})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT) as r:
            body = r.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise FeedError(f"The feed refused the request ({exc.code}): check its key in Settings → Sports.") from exc
        if exc.code == 429:
            raise FeedError("The feed's daily request limit is used up. It starts again tomorrow.") from exc
        raise FeedError(f"The feed answered {exc.code}.") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FeedError(f"The feed couldn't be reached ({getattr(exc, 'reason', exc)}).") from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise FeedError("The feed's answer couldn't be read.") from exc
    if not isinstance(data, dict):
        raise FeedError("The feed's answer couldn't be read.")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data


def _int(v) -> int | None:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _clock_seconds(text: str) -> float | None:
    """'11:42' or '42.5' as seconds."""
    m = re.fullmatch(r"\s*(?:(\d{1,3}):)?(\d{1,2})(?:\.(\d))?\s*", str(text or ""))
    if not m:
        return None
    return int(m[1] or 0) * 60 + int(m[2]) + (int(m[3]) / 10 if m[3] else 0.0)


def soccer_minute(display: str, seconds: float | None = None) -> tuple[int, float | None, str]:
    """(period, seconds since the period began, "45+2'") from a soccer clock ("45'+2'", "67'", "90+4"); from
    the running seconds when there's no text."""
    m = re.search(r"(\d{1,3})\s*'?\s*(?:\+\s*(\d{1,2}))?", str(display or ""))
    if not m:
        if seconds is None:
            return 0, None, ""
        minute, extra = int(seconds // 60), 0
        shown = f"{minute + 1}'"
        base = minute * 60 + seconds % 60
    else:
        minute, extra = int(m[1]), int(m[2] or 0)
        shown = f"{minute}+{extra}'" if extra else f"{minute}'"
        # "67'" is the 67th minute: 66:00-66:59 on the clock, so its middle is the best guess
        base = (max(0, minute - 1) * 60 + 30.0) if not extra else minute * 60 + (extra - 1) * 60 + 30.0
        if seconds is not None and not extra and abs(seconds - base) < 90:
            base = float(seconds)
    if extra:
        period = 1 if minute <= 45 else 2 if minute <= 90 else 3 if minute <= 105 else 4
    else:
        period = 1 if minute <= 45 else 2 if minute <= 90 else 3 if minute <= 105 else 4
    start = {1: 0, 2: 45 * 60, 3: 90 * 60, 4: 105 * 60}[period]
    return period, max(0.0, base - start), shown


# ---------------------------------------------------------------- ESPN


def espn_league(sport: str, league_hint: str = "") -> list[str]:
    """ESPN's league paths to look in for `sport`, the hinted one first."""
    known = ESPN_LEAGUES.get(sport, ())
    hint = (league_hint or "").lower()
    first = [code for code, names in known if hint and any(n in hint for n in names)]
    return first + [code for code, _ in known if code not in first]


def _wall(text) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _competitors(comp: dict) -> tuple[dict, dict]:
    home = away = {}
    for c in comp.get("competitors") or []:
        if isinstance(c, dict):
            if c.get("homeAway") == "home":
                home = c
            elif c.get("homeAway") == "away":
                away = c
    return home, away


def _team_names(c: dict) -> list[str]:
    t = c.get("team") or {}
    return [str(t.get(k)) for k in ("displayName", "shortDisplayName", "name", "location", "abbreviation") if t.get(k)]


def same_team(wanted: str, names: list[str]) -> bool:
    w = team_key(wanted)
    if not w:
        return False
    for n in names:
        k = team_key(n)
        if k and (k == w or (len(k) >= 4 and k in w) or (len(w) >= 4 and w in k)):
            return True
    return False


def espn_find(sport: str, home: str, away: str, when: float, league_hint: str = "", *, get: Get | None = None,
              days: tuple[int, ...] = (0, -1, -2, 1, -3)) -> tuple[str, str, dict] | None:
    """(league path, event id, the scoreboard's own entry) of the game between `home` and `away` played on or
    just before `when` (a video goes up after its game), or None. One scoreboard request per league and day
    tried."""
    get = get or (lambda url, headers: http_get(url, headers, max_age=CACHE_LIVE))
    path = ESPN_SPORT.get(sport)
    if not path or not (home or away):
        return None
    for league in espn_league(sport, league_hint)[:3 if league_hint else 2]:
        for shift in days:
            day = time.strftime("%Y%m%d", time.gmtime(when + shift * DAY))
            try:
                board = get(f"{ESPN_URL}/{path}/{league}/scoreboard?dates={day}", {})
            except FeedError:
                continue
            for ev in board.get("events") or []:
                comp = ((ev.get("competitions") or [{}])[0]) if isinstance(ev, dict) else {}
                h, a = _competitors(comp)
                names = _team_names(h) + _team_names(a)
                if (not home or same_team(home, names)) and (not away or same_team(away, names)) and (home or away):
                    if home and away and not (
                            (same_team(home, _team_names(h)) and same_team(away, _team_names(a))) or
                            (same_team(home, _team_names(a)) and same_team(away, _team_names(h)))):
                        continue
                    return league, str(ev.get("id") or ""), ev
    return None


def _espn_play(p: dict, sport: str, teams: dict[str, str]) -> FeedEvent | None:
    if not isinstance(p, dict):
        return None
    kind_text = str((p.get("type") or {}).get("text") or "")
    text = " ".join(str(p.get("text") or p.get("shortText") or kind_text).split())
    if not text and not kind_text:
        return None
    clock = p.get("clock") or {}
    shown = str(clock.get("displayValue") or "")
    period = _int((p.get("period") or {}).get("number")) or 0
    ev = FeedEvent(kind_of(kind_text, text), text, raw=kind_text, wallclock=_wall(p.get("wallclock")),
                   scoring=bool(p.get("scoringPlay")), home_score=_int(p.get("homeScore")),
                   away_score=_int(p.get("awayScore")))
    if sport == "soccer":
        per, elapsed, minute = soccer_minute(shown, clock.get("value") if isinstance(clock.get("value"), (int, float))
                                             else None)
        ev.period, ev.elapsed, ev.minute = period or per, elapsed, minute
    else:
        ev.period, ev.clock = period, shown
        left = _clock_seconds(shown)
        length = CLOCKS.get(sport, (0, 0, True, 0))[1]
        if left is not None and length:
            ev.elapsed = max(0.0, (length if period <= CLOCKS[sport][0] else 300) - left) if CLOCKS[sport][2] else left
    team = p.get("team") or {}
    ev.team = str(team.get("displayName") or teams.get(str(team.get("id") or ""), "") or "")
    for part in (p.get("participants") or p.get("athletesInvolved") or [])[:3]:
        a = part.get("athlete") if isinstance(part, dict) and "athlete" in part else part
        name = (a or {}).get("displayName") or (a or {}).get("fullName") if isinstance(a, dict) else ""
        if name:
            ev.players.append(str(name))
    if ev.kind in SCORING:
        ev.scoring = True
    return ev


def _espn_detail(d: dict, teams: dict[str, str]) -> FeedEvent | None:
    """One entry of a soccer scoreboard's `details`: goals and cards with the minute."""
    if not isinstance(d, dict):
        return None
    kind_text = str((d.get("type") or {}).get("text") or "")
    kind = ("red_card" if d.get("redCard") else "yellow_card" if d.get("yellowCard") else
            "own_goal" if d.get("ownGoal") else "penalty_goal" if d.get("penaltyKick") and d.get("scoringPlay") else
            "goal" if d.get("scoringPlay") else kind_of(kind_text))
    clock = d.get("clock") or {}
    period, elapsed, minute = soccer_minute(str(clock.get("displayValue") or ""),
                                            clock.get("value") if isinstance(clock.get("value"), (int, float)) else None)
    players = [str(a.get("displayName") or a.get("fullName")) for a in d.get("athletesInvolved") or []
               if isinstance(a, dict) and (a.get("displayName") or a.get("fullName"))]
    team = teams.get(str((d.get("team") or {}).get("id") or ""), "")
    text = f"{kind_text or KIND_WORDS.get(kind, kind).title()}" + (f": {players[0]}" if players else "") + (
        f" ({team})" if team else "")
    return FeedEvent(kind, text, period, elapsed, minute, team=team, players=players[:3],
                     scoring=bool(d.get("scoringPlay")), raw=kind_text)


def espn_feed(sport: str, league: str, event_id: str, *, get: Get | None = None, board_entry: dict | None = None) -> Feed:
    """A game's events from ESPN: its summary's plays, key events and commentary, else the scoreboard's
    goals and cards."""
    get = get or (lambda url, headers: http_get(url, headers, max_age=CACHE_LIVE))
    path = ESPN_SPORT.get(sport)
    if not path:
        raise FeedError(f"ESPN has no play-by-play ClipMonster reads for {sport}.")
    data = get(f"{ESPN_URL}/{path}/{league}/summary?event={urllib.parse.quote(str(event_id))}", {})
    comp = ((data.get("header") or {}).get("competitions") or [{}])[0]
    if not comp.get("competitors") and board_entry:
        comp = (board_entry.get("competitions") or [{}])[0]
    h, a = _competitors(comp)
    teams = {str((c.get("team") or {}).get("id") or c.get("id") or ""): (_team_names(c) or [""])[0] for c in (h, a) if c}
    feed = Feed("espn", sport, [], str(event_id), league, (_team_names(h) or [""])[0], (_team_names(a) or [""])[0],
                _int(h.get("score")), _int(a.get("score")))
    plays: list[dict] = []
    if isinstance(data.get("plays"), list):
        plays = data["plays"]
    elif isinstance(data.get("drives"), dict):  # American football: plays grouped by drive
        for drive in (data["drives"].get("previous") or []) + ([data["drives"].get("current")] if
                                                                isinstance(data["drives"].get("current"), dict) else []):
            plays += [p for p in (drive or {}).get("plays") or [] if isinstance(p, dict)]
    seen: set[tuple] = set()

    def add(ev: FeedEvent | None) -> None:
        if ev is None:
            return
        key = (ev.kind, ev.period, round((ev.elapsed or 0) / 20), ev.text[:60])
        if key not in seen:
            seen.add(key)
            feed.events.append(ev)

    for p in plays:
        add(_espn_play(p, sport, teams))
    for p in data.get("keyEvents") or []:
        add(_espn_play(p, sport, teams))
    for c in data.get("commentary") or []:  # soccer's text commentary: [{"time": {"displayValue": "67'"}, "text"}]
        if not isinstance(c, dict) or not c.get("text"):
            continue
        play = c.get("play") if isinstance(c.get("play"), dict) else {}
        ev = _espn_play({**play, "text": c["text"], "clock": c.get("time") or play.get("clock") or {}}, sport, teams)
        if ev is not None and ev.kind != "other":
            # the key events already carry the same card or goal: keep the one with the players
            same = next((e for e in feed.events if e.kind == ev.kind and e.period == ev.period and
                         e.elapsed is not None and ev.elapsed is not None and abs(e.elapsed - ev.elapsed) < 120), None)
            if same is None:
                add(ev)
            elif len(ev.text) > len(same.text):
                same.text = ev.text
    if not feed.events:
        details = comp.get("details") or ((board_entry or {}).get("competitions") or [{}])[0].get("details") or []
        for d in details:
            add(_espn_detail(d, teams))
        if feed.events:
            feed.note = "ESPN gave this game's goals and cards only."
    if not feed.events:
        raise FeedError("ESPN has no play-by-play for this game.")
    feed.events.sort(key=lambda e: (e.period, e.elapsed if e.elapsed is not None else 0.0))
    _carry_scores(feed)
    return feed


def _carry_scores(feed: Feed) -> None:
    """Every event gets the score as it stood (the feeds only put it on scoring plays, or nowhere)."""
    if all(e.home_score is None for e in feed.events) and feed.sport == "soccer":
        h = a = 0
        for e in feed.events:
            if e.kind in ("goal", "penalty_goal", "own_goal") or (e.scoring and e.kind not in ("penalty_missed",)):
                mine = same_team(e.team, [feed.home]) if e.team else None
                if mine is None:
                    e.home_score, e.away_score = None, None
                    continue
                scored_home = mine != (e.kind == "own_goal")
                h, a = (h + 1, a) if scored_home else (h, a + 1)
            e.home_score, e.away_score = h, a
        if feed.home_score is not None and (h, a) != (feed.home_score, feed.away_score):
            for e in feed.events:  # the tally doesn't end on the real score: don't show a wrong one
                e.home_score = e.away_score = None
        return
    h = a = None
    for e in feed.events:
        if e.home_score is None:
            e.home_score, e.away_score = h, a
        else:
            h, a = e.home_score, e.away_score


# ---------------------------------------------------------------- API-Football


def apif_feed(key: str, home: str, away: str, when: float, *, fixture: str = "", get: Get | None = None) -> Feed:
    """A soccer game's events from API-Football: the day's fixtures (to find it), then its events. Two requests."""
    if not (key or "").strip():
        raise FeedError("API-Football needs its key (Settings → Sports).")
    headers = {"x-apisports-key": key.strip()}
    get = get or (lambda url, hd: http_get(url, hd, max_age=CACHE_LIVE))
    info: dict = {}
    if not fixture:
        for shift in (0, -1, -2, 1):
            day = time.strftime("%Y-%m-%d", time.gmtime(when + shift * DAY))
            data = get(f"{APIF_URL}/fixtures?date={day}", headers)
            _apif_errors(data)
            for fx in data.get("response") or []:
                t = (fx.get("teams") or {}) if isinstance(fx, dict) else {}
                names_h, names_a = [str((t.get("home") or {}).get("name") or "")], [str((t.get("away") or {}).get("name") or "")]
                if ((same_team(home, names_h) and same_team(away, names_a)) or
                        (same_team(home, names_a) and same_team(away, names_h))):
                    fixture, info = str((fx.get("fixture") or {}).get("id") or ""), fx
                    break
            if fixture:
                break
        if not fixture:
            raise FeedError(f"API-Football has no {home} v {away} around that date.")
    data = get(f"{APIF_URL}/fixtures/events?fixture={urllib.parse.quote(fixture)}", headers)
    _apif_errors(data)
    t = info.get("teams") or {}
    g = info.get("goals") or {}
    feed = Feed("api-football", "soccer", [], fixture, str((info.get("league") or {}).get("name") or ""),
                str((t.get("home") or {}).get("name") or home), str((t.get("away") or {}).get("name") or away),
                _int(g.get("home")), _int(g.get("away")))
    for e in data.get("response") or []:
        if not isinstance(e, dict):
            continue
        tm = e.get("time") or {}
        minute, extra = _int(tm.get("elapsed")), _int(tm.get("extra")) or 0
        if minute is None:
            continue
        kind_word, detail = str(e.get("type") or ""), str(e.get("detail") or "")
        kind = {"Normal Goal": "goal", "Own Goal": "own_goal", "Penalty": "penalty_goal",
                "Missed Penalty": "penalty_missed", "Yellow Card": "yellow_card", "Red Card": "red_card",
                "Second Yellow card": "second_yellow"}.get(detail) or (
            "var" if kind_word.lower() == "var" else "substitution" if kind_word.lower() == "subst" else
            kind_of(kind_word, detail))
        period, elapsed, shown = soccer_minute(f"{minute}'" + (f"+{extra}" if extra else ""))
        player = str((e.get("player") or {}).get("name") or "")
        team = str((e.get("team") or {}).get("name") or "")
        words = detail if kind == "var" else KIND_WORDS.get(kind, detail or kind_word).title()
        text = f"{words}" + (f": {player}" if player else "") + (f" ({team})" if team else "") + (
            f". {e['comments']}" if e.get("comments") else "")
        assist = str((e.get("assist") or {}).get("name") or "")
        feed.events.append(FeedEvent(kind, text, period, elapsed, shown, team=team,
                                     players=[p for p in (player, assist if kind != "substitution" else "") if p],
                                     scoring=kind in ("goal", "own_goal", "penalty_goal"), raw=f"{kind_word}: {detail}"))
    if not feed.events:
        raise FeedError("API-Football has no events for this game.")
    feed.events.sort(key=lambda e: (e.period, e.elapsed or 0.0))
    _carry_scores(feed)
    return feed


def _apif_errors(data: dict) -> None:
    errs = data.get("errors")
    if errs:
        said = "; ".join(map(str, errs.values())) if isinstance(errs, dict) else "; ".join(map(str, errs))
        raise FeedError(f"API-Football: {said[:200]}")


# ---------------------------------------------------------------- your own file


def file_feed(path: Path, sport: str = "") -> Feed:
    """A timeline you wrote: JSON (a list, or {"events": [...]} with optional sport/home/away) or CSV. Each row
    has `kind` and/or `text`, and when it happened as one of: `video` (seconds or h:mm:ss into the video),
    `minute` ("67" or "90+4", soccer), or `period` + `clock` ("11:42" left) for the other sports."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise FeedError(f"{path.name} couldn't be read ({exc}).") from exc
    meta: dict = {}
    if path.suffix.lower() == ".csv":
        rows = list(csv.DictReader(io.StringIO(raw)))
    else:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise FeedError(f"{path.name} isn't valid JSON ({exc}).") from exc
        if isinstance(data, dict):
            meta, rows = data, data.get("events") or []
        else:
            rows = data
    if not isinstance(rows, list):
        raise FeedError(f"{path.name} should hold a list of events.")
    sport = sport or str(meta.get("sport") or "") or "any"
    feed = Feed("file", sport, [], home=str(meta.get("home") or ""), away=str(meta.get("away") or ""))
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
        text = " ".join(str(row.get("text") or "").split())
        kind = str(row.get("kind") or "").strip().lower().replace(" ", "_") or kind_of(text)
        if kind not in {k for k, _ in KINDS}:
            kind = kind_of(kind.replace("_", " "), text)
        ev = FeedEvent(kind, text or KIND_WORDS.get(kind, kind).title(), team=str(row.get("team") or ""),
                       players=[p.strip() for p in str(row.get("player") or row.get("players") or "").split(",")
                                if p.strip()], raw="file")
        if str(row.get("video") or "").strip():
            ev.video = _video_seconds(row["video"])
        if str(row.get("minute") or "").strip():
            ev.period, ev.elapsed, ev.minute = soccer_minute(str(row["minute"]))
        elif str(row.get("clock") or "").strip():
            ev.period, ev.clock = _int(row.get("period")) or 0, str(row["clock"]).strip()
            left = _clock_seconds(ev.clock)
            periods, length, down, _ = CLOCKS.get(sport, (0, 0, True, 0))
            if left is not None and length:
                ev.elapsed = max(0.0, length - left) if down else left
        elif _int(row.get("period")):
            ev.period = _int(row.get("period")) or 0
        ev.home_score, ev.away_score = _int(row.get("home_score")), _int(row.get("away_score"))
        ev.scoring = ev.kind in SCORING
        if ev.video is None and ev.elapsed is None and not ev.period:
            continue  # nothing says when
        feed.events.append(ev)
    if not feed.events:
        raise FeedError(f"{path.name} has no events with a time (video, minute, or period and clock).")
    feed.events.sort(key=lambda e: (e.video if e.video is not None else 1e9, e.period, e.elapsed or 0.0))
    return feed


def _video_seconds(v) -> float | None:
    text = str(v).strip()
    try:
        return float(text)
    except ValueError:
        pass
    parts = text.split(":")
    try:
        total = 0.0
        for p in parts:
            total = total * 60 + float(p)
        return total
    except ValueError:
        return None


# ---------------------------------------------------------------- the feed for a game


def parse_spec(spec: str) -> tuple[str, str, str]:
    """`--feed` as (source, league, id): "espn:eng.1:401879244", "espn:401879244", "api-football:1208021",
    "auto", "off", or a file path ("file", "", path)."""
    spec = (spec or "").strip()
    low = spec.lower()
    if low in ("", "auto", "off", "espn", "api-football"):
        return low or "auto", "", ""
    if low.startswith("espn:"):
        bits = spec.split(":")[1:]
        return ("espn", bits[0], bits[1]) if len(bits) >= 2 else ("espn", "", bits[0])
    if low.startswith(("api-football:", "apif:")):
        return "api-football", "", spec.split(":", 1)[1]
    return "file", "", spec


def for_game(game: dict, s, *, spec: str = "", log: Log | None = None, get: Get | None = None) -> Feed | None:
    """The best feed there is for `game` (a Sports game: sport, league, home, away, published), or None with
    the reason logged. `spec` (parse_spec) overrides Settings → Sports → Play-by-play feed."""
    log = log or (lambda m: None)
    source, league, ident = parse_spec(spec or str(getattr(s, "event_feed", "auto") or "auto"))
    sport = str(game.get("sport") or "")
    if source == "off":
        return None
    try:
        if source == "file":
            return file_feed(Path(ident).expanduser(), sport)
        home, away = str(game.get("home") or ""), str(game.get("away") or "")
        when = float(game.get("played") or game.get("published") or time.time())
        key = str(getattr(s, "api_football_key", "") or "").strip()
        problems: list[str] = []
        if source in ("api-football", "auto") and sport == "soccer" and (key or source == "api-football"):
            try:
                if not ident and not (home and away):
                    raise FeedError("the game's two teams aren't known")
                return apif_feed(key, home, away, when, fixture=ident if source == "api-football" else "", get=get)
            except FeedError as exc:
                if source == "api-football":
                    raise
                problems.append(str(exc))
        if source in ("espn", "auto"):
            if ident:
                leagues = [league] if league else espn_league(sport, str(game.get("league") or ""))
                last: FeedError | None = None
                for lg in leagues:
                    try:
                        return espn_feed(sport, lg, ident, get=get)
                    except FeedError as exc:
                        last = exc
                raise last or FeedError("ESPN has no such game.")
            if sport not in ESPN_SPORT:
                raise FeedError(f"there's no public play-by-play for {sport or 'this sport'}")
            if not (home and away):
                raise FeedError("the game's two teams aren't known, so its play-by-play can't be looked up")
            found = espn_find(sport, home, away, when, str(game.get("league") or ""), get=get)
            if found is None:
                raise FeedError(f"ESPN lists no {home} v {away} around that date" +
                                (f" ({'; '.join(problems)})" if problems else ""))
            return espn_feed(sport, found[0], found[1], get=get, board_entry=found[2])
    except FeedError as exc:
        log(f"No play-by-play feed: {exc}. Searching the commentary only.")
    return None

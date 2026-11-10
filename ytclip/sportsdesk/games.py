"""Which games to clip: the Sports section's filter, the searches it makes, and reading results into games.

A filter says the sport, the league (optional), the teams (optional, any of them), how recently the game was
played and what footage to look for: full games, extended highlights, highlights. "Find games" turns it into a
few YouTube searches (2-3; with the YouTube Data API key each costs 100 of the 10,000 free daily units, without
one yt-dlp searches YouTube's own page instead, which costs nothing but can't limit by date as exactly), reads
every result's title into a game, drops what isn't a game (reaction videos, podcasts, previews, video-game
footage...), keeps the ones that match the filter, and groups the uploads of one game (a full game and its
highlights from the same night) into one game with several versions of footage.

Reading titles: with an AI set up (Settings → AI: Claude, Gemini or the local AI), the AI reads them, which copes
with any way a channel words its titles; otherwise (or if the AI fails) read_title() does it by the words.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .. import sports

Log = Callable[[str], None]

DAY = 86400.0
MAX_SEARCHES = 3  # searches one "Find games" makes
PER_SEARCH = 25  # results asked of each
MAX_GAMES = 120  # games the list keeps (the oldest that aren't picked go first)
SAME_GAME = 1.6 * DAY  # two uploads of one matchup this close together are one game

# What footage to look for: (id, label, shortest minutes, longest minutes, the words a search adds)
KINDS = (("full", "Full games", 40, 400, "full game"),
         ("extended", "Extended highlights", 7, 45, "extended highlights"),
         ("highlights", "Highlights", 2, 16, "highlights"))
KIND_LABELS = {k: label for k, label, *_ in KINDS} | {"other": "Other footage"}
# Which version of a game is shown first: extended highlights have the plays without three hours to download.
PREFER = ("extended", "highlights", "full", "other")

SINCE = (("day", "24 hours", 1), ("3days", "3 days", 3), ("week", "Week", 7), ("month", "Month", 31),
         ("any", "Any time", 0))
SINCE_DAYS = {k: d for k, _, d in SINCE}

# The recap reel's choices: how the picture is framed, and the order of its plays.
REEL_FRAMING = {"follow": "Follow the action", "fit": "Whole picture"}
REEL_ORDER = {"countdown": "Countdown to the best", "timeline": "As it happened",
              "climax": "As it happened, the best last"}

# What a game is called in each sport, for searches ("full match", "full fight").
GAME_WORD = {"soccer": "match", "tennis": "match", "cricket": "match", "combat": "fight", "motorsport": "race",
             "golf": "round"}

LEAGUES: dict[str, tuple[str, ...]] = {
    "soccer": ("Premier League", "Champions League", "La Liga", "Serie A", "Bundesliga", "Ligue 1", "MLS",
               "Europa League", "Liga MX", "World Cup"),
    "basketball": ("NBA", "WNBA", "NCAA", "EuroLeague"),
    "football": ("NFL", "College football"),
    "baseball": ("MLB", "NPB", "KBO"),
    "hockey": ("NHL", "PWHL"),
    "tennis": ("ATP", "WTA", "Wimbledon", "US Open", "Australian Open", "French Open"),
    "combat": ("UFC", "Boxing", "PFL", "ONE Championship"),
    "golf": ("PGA Tour", "LIV Golf", "LPGA"),
    "cricket": ("IPL", "Test cricket", "T20", "Big Bash"),
    "motorsport": ("Formula 1", "NASCAR", "MotoGP", "IndyCar"),
    "any": (),
}

# Titles of videos that aren't the game itself.
NOT_GAME = re.compile(
    r"\b(?:reacts?|reaction|reacting|podcast|preview|predictions?|prediction show|press conference|presser|"
    r"interview|post[- ]?game show|pre[- ]?game|breakdown|analysis|film (?:study|room)|watch ?party|live ?stream|"
    r"(?:fifa|madden|nba ?2k|mlb the show|ea (?:sports )?fc|nhl ?\d\d|efootball|pes) ?\d*|simulation|simulated|"
    r"\bsim\b|mock draft|draft (?:grades|picks?)|trade (?:rumou?rs?|deadline)|rumou?rs?|fantasy|betting|best bets|"
    r"odds|tier list|ranking (?:every|the)|top \d+ (?!plays\b)|#shorts|shorts\b|news|recap show|explained|"
    r"what (?:went wrong|happened)|mic'?d up|behind the scenes|training|practice|warm[- ]?ups?)\b", re.I)
LIVE_WORDS = re.compile(r"\b(?:live|stream|streaming|watch along)\b", re.I)

FULL_WORDS = re.compile(r"\bfull (?:game|match|fight|race|replay|broadcast|round|episode)\b(?!\s*(?:highlights?|recap))|"
                        r"\bfull[- ]length\b|\b(?:entire|whole) (?:game|match)\b", re.I)
EXTENDED_WORDS = re.compile(r"\bextended (?:highlights?|cut)\b|\bcondensed\b|\bmini[- ]?movie\b|"
                            r"\ball (?:the )?(?:plays|goals|touchdowns|points)\b", re.I)
HIGHLIGHT_WORDS = re.compile(r"\bhighlights?\b|\brecap\b|\bbest (?:plays|moments)\b|\bgoals? and highlights\b", re.I)

# "Lakers vs Celtics", "LAKERS at CELTICS", "Arsenal v Chelsea", "Real Madrid 3-1 Barcelona", "Chiefs @ Bills"
_SIDE = r"[A-Za-z0-9][\w.&'’ ]{0,40}?"
_MATCHUP = re.compile(
    rf"(?P<a>{_SIDE})\s+(?:(?P<score>\d{{1,3}}\s*[-–:]\s*\d{{1,3}})|vs\.?|v\.?|versus|@|at)\s+(?P<b>{_SIDE})"
    r"(?=\s*(?:$|[|\-–—:(\[,•·!]|\d{1,3}\s*[-–:]|full\b|game\b|match\b|highlights?\b|extended\b|recap\b|"
    r"week\b|round\b|nba\b|nfl\b|nhl\b|mlb\b|ufc\b|20\d\d\b))", re.I)
_SCORE = re.compile(r"\b(\d{1,3})\s*[-–:]\s*(\d{1,3})\b")
_LEAD_JUNK = re.compile(r"^(?:\W+|(?:full|extended|game|match)\s+(?:highlights?\s*)?[|:\-–—]\s*)+", re.I)
_TEAM_FILLER = re.compile(r"\b(?:the|fc|cf|sc|afc|club|team)\b", re.I)


@dataclass
class GameFilter:
    """Which games the Sports section looks for."""

    sport: str = "any"  # sports.SPORTS id
    league: str = ""  # "NBA", "Premier League" or any words; "" = any league
    teams: str = ""  # comma separated: games with any of them; "" = every team
    since: str = "3days"  # SINCE id: played (uploaded) within this
    kinds: list[str] = field(default_factory=lambda: ["extended", "highlights"])  # KINDS ids
    channels: str = ""  # comma separated channel names: only their uploads; "" = any channel
    category_id: str = ""  # the category the clips are made for (its folder, account and look); "" = none
    per_game: int = 5  # highlights ticked to make, per game
    # The game recap reel (reel.py): the narrator's voice ("off" = none), how the 16:9 footage becomes 9:16
    # (REEL_FRAMING) and the order of its plays (REEL_ORDER).
    reel_voice: str = "en_storyteller"
    reel_framing: str = "follow"
    reel_order: str = "countdown"

    @classmethod
    def from_json(cls, d) -> "GameFilter":
        f = cls()
        if not isinstance(d, dict):
            return f
        sport = sports.get(str(d.get("sport") or ""))
        f.sport = sport.id if sport else "any"
        f.league = " ".join(str(d.get("league") or "").split())[:60]
        f.teams = ", ".join(split_list(str(d.get("teams") or "")))[:300]
        f.since = str(d.get("since") or "3days") if str(d.get("since") or "") in SINCE_DAYS else "3days"
        kinds = d.get("kinds")
        if isinstance(kinds, str):
            kinds = kinds.split(",")
        known = [k for k, *_ in KINDS]
        picked = [k for k in known if isinstance(kinds, list) and k in [str(x).strip() for x in kinds]]
        f.kinds = picked or ["extended", "highlights"]
        f.channels = ", ".join(split_list(str(d.get("channels") or "")))[:300]
        f.category_id = re.sub(r"[^\w-]", "", str(d.get("category_id") or ""))[:40]
        try:
            f.per_game = max(1, min(20, int(d.get("per_game") or 5)))
        except (TypeError, ValueError):
            f.per_game = 5
        from ..creator.voice_engine import VOICES

        voice = str(d.get("reel_voice") or f.reel_voice)
        f.reel_voice = voice if voice == "off" or voice in VOICES else "en_storyteller"
        f.reel_framing = str(d.get("reel_framing") or "") if str(d.get("reel_framing") or "") in REEL_FRAMING \
            else "follow"
        f.reel_order = str(d.get("reel_order") or "") if str(d.get("reel_order") or "") in REEL_ORDER else "countdown"
        return f

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def sport_info(self) -> sports.Sport:
        return sports.get(self.sport) or sports.SPORTS["any"]

    @property
    def team_list(self) -> list[str]:
        return split_list(self.teams)

    @property
    def channel_list(self) -> list[str]:
        return split_list(self.channels)

    def published_after(self, now: float) -> float:
        days = SINCE_DAYS.get(self.since, 3)
        return now - days * DAY if days else 0.0

    def describe(self) -> str:
        """'NBA basketball games with Lakers or Celtics, last 3 days, extended highlights or highlights'"""
        sp = self.sport_info
        what = (f"{self.league} " if self.league else "") + (sp.label.split(" (")[0].lower() if sp.id != "any" else "sports")
        word = GAME_WORD.get(sp.id, "game")
        parts = [f"{what} {word}es" if word.endswith("h") else f"{what} {word}s"]
        if self.team_list:
            parts.append("with " + " or ".join(self.team_list[:4]) + (" or others" if len(self.team_list) > 4 else ""))
        days = dict((k, label) for k, label, _ in SINCE)[self.since]
        parts.append("any time" if self.since == "any" else f"from the last {days.lower()}")
        parts.append(" or ".join(KIND_LABELS[k].lower() for k in self.kinds))
        return ", ".join(parts)


def split_list(text: str) -> list[str]:
    """'Lakers, Celtics;  Heat' → ['Lakers', 'Celtics', 'Heat'] (no repeats, any case)."""
    out: list[str] = []
    for part in re.split(r"[,;\n]+", text or ""):
        part = " ".join(part.split()).strip(" .")
        if part and part.lower() not in (o.lower() for o in out):
            out.append(part[:40])
    return out[:12]


# ---------------------------------------------------------------- searches


def queries(f: GameFilter) -> list[str]:
    """The searches for a filter, at most MAX_SEARCHES: one per team (with the footage asked for most), else the
    league (or the sport) with each kind of footage."""
    sp = f.sport_info
    word = GAME_WORD.get(sp.id, "game")
    kind_words = {"full": f"full {word}", "extended": "extended highlights", "highlights": "highlights"}
    where = f.league or (sp.label.split(" (")[0] if sp.id != "any" else "")
    teams = f.team_list
    out: list[str] = []
    if teams:
        main = kind_words[f.kinds[0]]
        for team in teams[:MAX_SEARCHES]:
            out.append(" ".join(x for x in (team, where if f.league else "", main) if x))
    else:
        for k in f.kinds:
            out.append(" ".join(x for x in (where or "sports", kind_words[k]) if x))
        if len(out) < 2 and f.kinds == ["full"]:
            out.append(" ".join(x for x in (where or "sports", f"{word} replay") if x))
    if f.channel_list and len(out) < MAX_SEARCHES:  # a channel named in the filter: search its name too
        out.append(" ".join(x for x in (f.channel_list[0], where, kind_words[f.kinds[0]]) if x))
    seen: list[str] = []
    for q in out:
        q = " ".join(q.split())[:100]
        if q and q.lower() not in (s.lower() for s in seen):
            seen.append(q)
    return seen[:MAX_SEARCHES]


@dataclass
class Found:
    """One search result, from the YouTube Data API or yt-dlp."""

    id: str
    title: str
    channel: str = ""
    published: float = 0.0  # unix time; 0 when yt-dlp's search didn't say
    duration: float = 0.0
    views: int = 0
    thumbnail: str = ""
    live: bool = False

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"


def search_api(api_key: str, query: str, published_after: float, limit: int = PER_SEARCH, get=None) -> list[Found]:
    """A YouTube Data API search and the videos' details (101 units)."""
    from .. import lookup
    from ..trending import _get

    get = get or _get
    ids, _ = lookup.search_page(api_key, query, published_after, limit=limit, get=get, order="relevance")
    if not ids:
        return []
    videos, _text = lookup.details(api_key, ids, get=get)
    by_id = {v.id: v for v in videos}
    return [Found(v.id, v.title, v.channel, v.published, v.duration, v.views, v.thumbnail, v.live != "none")
            for i in ids if (v := by_id.get(i))]


def search_free(query: str, limit: int = PER_SEARCH, newest: bool = True, cookies: str | None = None,
                log: Log | None = None) -> list[Found]:
    """yt-dlp's search of YouTube's own results page: no key, no quota. It gives titles, channels, lengths and
    views; upload dates only sometimes."""
    import yt_dlp

    from ..download import _base_opts

    opts = _base_opts(log, cookies) | {"extract_flat": "in_playlist", "skip_download": True}
    prefix = "ytsearchdate" if newest else "ytsearch"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{prefix}{max(1, min(50, limit))}:{query}", download=False)
    out: list[Found] = []
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        thumbs = e.get("thumbnails") or []
        thumb = (thumbs[-1].get("url") if thumbs and isinstance(thumbs[-1], dict) else "") or \
            f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg"
        stamp = e.get("timestamp") or e.get("release_timestamp") or 0
        if not stamp and e.get("upload_date"):
            try:
                stamp = time.mktime(time.strptime(str(e["upload_date"]), "%Y%m%d"))
            except ValueError:
                stamp = 0
        out.append(Found(str(e["id"]), str(e.get("title") or e["id"]), str(e.get("channel") or e.get("uploader") or ""),
                         float(stamp or 0), float(e.get("duration") or 0), int(e.get("view_count") or 0), thumb,
                         str(e.get("live_status") or "") in ("is_live", "is_upcoming")))
    return out


# ---------------------------------------------------------------- reading a title


def kind_of(title: str, duration: float = 0.0) -> str:
    """full | extended | highlights | other, by the title's words and then the length."""
    if FULL_WORDS.search(title):
        return "full"
    if EXTENDED_WORDS.search(title):
        return "extended"
    if HIGHLIGHT_WORDS.search(title):
        if duration >= 50 * 60:
            return "full"
        return "extended" if duration >= 14 * 60 else "highlights"
    if duration >= 80 * 60:
        return "full"
    return "other"


def _side(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip(" -–—|:·•,.!")
    text = _LEAD_JUNK.sub("", text).strip()
    words = text.split()
    # "NBA 2026 Lakers" → "Lakers": leading league or year words aren't the team
    while words and (re.fullmatch(r"(?:19|20)\d\d|week|\d+", words[0], re.I) or words[0].upper() in _LEAGUE_WORDS):
        words.pop(0)
    while words and re.fullmatch(r"(?:19|20)\d\d|game|\d+|full|highlights?", words[-1], re.I):
        words.pop()
    side = " ".join(words)
    if side.isupper() and len(side) > 4:  # "LOS ANGELES LAKERS" → "Los Angeles Lakers"
        side = " ".join(w if len(w) <= 3 else w.capitalize() for w in side.split())
    return side[:40]


_LEAGUE_WORDS = {w.upper() for names in LEAGUES.values() for n in names for w in [n] if " " not in n} | {
    "HIGHLIGHTS", "FULL", "EXTENDED", "GAME", "MATCH"}


def read_title(title: str, duration: float = 0.0) -> dict:
    """What a title says about the game: {home, away, score, kind, game}; `game` is False for a video that isn't
    the game itself (a reaction, a podcast, a preview, video-game footage...)."""
    clean = " ".join((title or "").split())
    out = {"home": "", "away": "", "score": "", "kind": kind_of(clean, duration), "game": True}
    if NOT_GAME.search(clean) or (LIVE_WORDS.search(clean) and not FULL_WORDS.search(clean)
                                  and not HIGHLIGHT_WORDS.search(clean)):
        out["game"] = False
    # The matchup is usually before the first separator: "Lakers vs Celtics | Full Game Highlights"
    for chunk in [clean] + [c for c in re.split(r"\s[|•·]\s|\s[–—]\s", clean) if c.strip()]:
        m = _MATCHUP.search(chunk)
        if m:
            a, b = _side(m.group("a")), _side(m.group("b"))
            if a and b and a.lower() != b.lower():
                # "A at B" / "A @ B": B is at home; "A vs B": the first named is (by convention) home
                sep = chunk[m.end("a"):m.start("b")].strip().lower()
                away_first = sep in ("at", "@")
                out["away"], out["home"] = (a, b) if away_first else (b, a)
                if m.group("score"):
                    out["score"] = re.sub(r"\s+", "", m.group("score")).replace("–", "-").replace(":", "-")
                break
    if not out["score"]:
        sm = _SCORE.search(clean)
        if sm and not re.search(r"(?:19|20)\d\d", sm.group(0)) and int(sm.group(1)) < 300 and int(sm.group(2)) < 300:
            out["score"] = f"{sm.group(1)}-{sm.group(2)}"
    if out["kind"] == "other" and out["home"] and duration >= 7 * 60:
        out["kind"] = "extended" if duration < 45 * 60 else "full"
    return out


def matchup(home: str, away: str, sport_id: str = "") -> str:
    """'Celtics vs Lakers' (home first), or 'Lakers at Celtics' in the US sports, where that's how it's said."""
    if not home or not away:
        return home or away
    if sport_id in ("basketball", "football", "baseball", "hockey"):
        return f"{away} at {home}"
    return f"{home} vs {away}"


def team_key(name: str) -> str:
    return " ".join(_TEAM_FILLER.sub(" ", re.sub(r"[^\w ]", " ", (name or "").lower())).split())


def game_key(home: str, away: str) -> str:
    """The same two teams, whichever way round and however spelt ('Man City' doesn't meet 'Manchester City')."""
    sides = sorted(k for k in (team_key(home), team_key(away)) if k)
    return " | ".join(sides) if len(sides) == 2 else ""


def mentions(text: str, names: list[str]) -> bool:
    """Whether `text` names any of `names` as whole words, any case."""
    low = " " + re.sub(r"[^\w]+", " ", (text or "").lower()) + " "
    return any(f" {' '.join(re.sub(r'[^\w]+', ' ', n.lower()).split())} " in low for n in names if n.strip())


# ---------------------------------------------------------------- the AI reads the titles

AI_SYSTEM = (
    "You sort YouTube search results for someone clipping sports highlights. For each video, from its title, "
    "channel and length, say whether it is footage of a real game, match, fight or race being played (game=true) "
    "or something else (game=false): a reaction, podcast, preview, analysis or talk show, news, an interview, a "
    "press conference, a live stream that hasn't happened, a video game (FIFA, Madden, 2K...), a fan film or a "
    "compilation of many games. For a real game: kind is full (the whole game or most of it), extended (long or "
    "condensed highlights, 7-40 minutes), highlights (a short recap) or other; home and away are the two teams or "
    "players exactly as the title names them (home is the one playing at home: in 'A at B' or 'A @ B' it is B; "
    "when the title doesn't say, the first named); league is the competition if the title or channel shows it, "
    "else empty. Never invent a team that isn't in the title.")

AI_SCHEMA = {
    "type": "object",
    "properties": {
        "videos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "The video's number in the list"},
                    "game": {"type": "boolean"},
                    "kind": {"type": "string", "enum": ["full", "extended", "highlights", "other"]},
                    "home": {"type": "string"},
                    "away": {"type": "string"},
                    "league": {"type": "string"},
                },
                "required": ["n", "game", "kind", "home", "away", "league"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["videos"],
    "additionalProperties": False,
}


def ai_read(found: list[Found], f: GameFilter, s, log: Log | None = None, check=None) -> tuple[dict[str, dict], str]:
    """The AI's reading of each title: {video id: {game, kind, home, away, league}}, and the model that answered.
    A local model reads them 15 at a time (a small model loses its place in a long list). Raises ai.AIError."""
    from .. import ai

    if not found:
        return {}, ""
    local = ai.chosen(s) in ai.LOCALS or not any(ai.key_for(s, p) for p in ai.CLOUD)
    batch = 15 if local else 50
    out: dict[str, dict] = {}
    model = ""
    sp = f.sport_info
    for i in range(0, len(found), batch):
        part = found[i:i + batch]
        lines = [f"{n}. {v.title} — {v.channel or 'unknown channel'} — "
                 f"{int(v.duration // 60)} min" for n, v in enumerate(part, 1)]
        prompt = (f"Sport wanted: {sp.label if sp.id != 'any' else 'any sport'}"
                  + (f", {f.league}" if f.league else "") + "\n\n" + "\n".join(lines))
        answer = ai.ask_json(s, "polish", AI_SYSTEM, prompt, AI_SCHEMA, effort="low", max_tokens=3000, log=log,
                             check=check, what=f"read {len(part)} video titles" if i == 0 else "")
        model = answer.model
        for item in answer.data.get("videos") or []:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("n"))
            except (TypeError, ValueError):
                continue
            if not 1 <= n <= len(part):
                continue
            v = part[n - 1]
            title = v.title.lower()
            home, away = (" ".join(str(item.get(k) or "").split())[:40] for k in ("home", "away"))
            # a team the title doesn't name is the AI guessing: leave it out
            home = home if home and team_key(home) and team_key(home).split()[0] in title.lower() else ""
            away = away if away and team_key(away) and team_key(away).split()[0] in title.lower() else ""
            kind = str(item.get("kind") or "other")
            out[v.id] = {"game": bool(item.get("game")), "kind": kind if kind in KIND_LABELS else "other",
                         "home": home, "away": away, "league": " ".join(str(item.get("league") or "").split())[:40]}
    return out, model


# ---------------------------------------------------------------- from results to games


def make_game(v: Found, read: dict, f: GameFilter, now: float) -> dict:
    """A game (as the desk keeps it) from one search result."""
    sp = f.sport_info
    home, away = read.get("home", ""), read.get("away", "")
    league = read.get("league") or f.league
    return {
        "id": v.id, "video": v.id, "url": v.url, "title": v.title, "channel": v.channel,
        "published": v.published, "duration": v.duration, "views": v.views, "thumbnail": v.thumbnail
        or f"https://i.ytimg.com/vi/{v.id}/hqdefault.jpg",
        "sport": sp.id, "league": league, "home": home, "away": away,
        "matchup": matchup(home, away, sp.id) or _short(v.title), "score": read.get("score", ""),
        "kind": read.get("kind", "other"), "key": game_key(home, away),
        "found": now, "new": True, "picked": False,
        "versions": [version_of(v, read.get("kind", "other"))],
        "status": "found", "stage": "", "frac": None, "error": "", "highlights": [], "ranked_by": "",
        "ai_error": "", "analysed": 0.0, "made": [],
    }


def version_of(v: Found, kind: str) -> dict:
    return {"video": v.id, "url": v.url, "title": v.title, "channel": v.channel, "kind": kind,
            "duration": v.duration, "views": v.views, "published": v.published,
            "thumbnail": v.thumbnail or f"https://i.ytimg.com/vi/{v.id}/hqdefault.jpg"}


def _short(title: str) -> str:
    t = re.split(r"\s[|•·]\s|\s[–—]\s", title or "")[0].strip()
    return t[:70] + ("…" if len(t) > 70 else "")


def fits(v: Found, read: dict, f: GameFilter, now: float) -> str:
    """Why a result is left out ("" when it fits the filter)."""
    if not read.get("game", True):
        return "not a game"
    if v.live:
        return "live or not out yet"
    kind = read.get("kind", "other")
    lo, hi = 2.0, 400.0
    if kind in f.kinds:
        lo, hi = next((a, b) for k, _, a, b, _w in KINDS if k == kind)
    elif kind == "other":
        lo = min(a for k, _, a, b, _w in KINDS if k in f.kinds)
        if not (read.get("home") and read.get("away")):
            return "not a game"
    else:
        return f"{KIND_LABELS[kind].lower()}, not what the filter asks for"
    minutes = v.duration / 60
    if v.duration and not lo <= minutes <= hi * 1.15:
        return "too short" if minutes < lo else "too long"
    after = f.published_after(now)
    if after and v.published and v.published < after:
        return "too old"
    if f.team_list and not mentions(f"{v.title} {read.get('home', '')} {read.get('away', '')}", f.team_list):
        return "another team"
    if f.channel_list and not mentions(v.channel, f.channel_list):
        return "another channel"
    if f.league and read.get("league") and not mentions(f"{read.get('league')} {v.title} {v.channel}",
                                                         [f.league]):
        # the AI said which competition and it isn't this one (a title without a league is given the benefit)
        if team_key(read.get("league", "")) and team_key(f.league) not in team_key(read.get("league", "")):
            return "another league"
    return ""


def merge(games: list[dict], new: list[dict]) -> tuple[list[dict], int]:
    """Add `new` games to `games`: a result already there (by video) is updated; another upload of a game already
    there (the same two teams within SAME_GAME) becomes one more version of it. Returns (games, how many new)."""
    by_video = {ver["video"]: g for g in games for ver in g.get("versions") or [{"video": g["video"]}]}
    added = 0
    for n in new:
        old = by_video.get(n["video"])
        if old is not None:
            for k in ("views", "thumbnail", "title"):
                old[k] = n[k] if old["video"] == n["video"] else old[k]
            for ver in old.get("versions") or []:
                if ver["video"] == n["video"]:
                    ver["views"] = n["views"]
            continue
        same = next((g for g in games if n["key"] and g.get("key") == n["key"]
                     and abs((g.get("published") or n["published"]) - (n["published"] or g.get("published") or 0))
                     <= SAME_GAME), None)
        if same is not None:
            same.setdefault("versions", []).append(n["versions"][0])
            same["versions"].sort(key=lambda x: PREFER.index(x["kind"]) if x["kind"] in PREFER else 9)
            if not same.get("highlights") and same.get("status") == "found":  # not started on: show the best version
                use(same, same["versions"][0]["video"])
            if not same.get("league") and n.get("league"):
                same["league"] = n["league"]
            by_video[n["video"]] = same
            continue
        games.append(n)
        by_video[n["video"]] = n
        added += 1
    return games, added


def use(game: dict, video_id: str) -> bool:
    """Show (and clip) the version of `game` that's the upload `video_id`. False when it has no such version."""
    ver = next((v for v in game.get("versions") or [] if v["video"] == video_id), None)
    if ver is None:
        return False
    for k in ("video", "url", "title", "channel", "kind", "duration", "views", "published", "thumbnail"):
        game[k] = ver[k]
    return True


def order(games: list[dict]) -> list[dict]:
    """Picked first, then the newest games, then the most watched."""
    return sorted(games, key=lambda g: (not g.get("picked"), -(g.get("published") or g.get("found") or 0),
                                        -(g.get("views") or 0)))


def trim(games: list[dict], keep: int = MAX_GAMES) -> list[dict]:
    """At most `keep` games: those picked, being worked on or with highlights stay; the oldest others go."""
    if len(games) <= keep:
        return games
    busy = [g for g in games if g.get("picked") or g.get("highlights") or g.get("status") not in ("found", "failed")]
    rest = sorted((g for g in games if g not in busy), key=lambda g: -(g.get("found") or 0))
    return busy + rest[:max(0, keep - len(busy))]

"""The games just played: every game of the last 24 hours, league by league, with its score.

The Sports section's filter finds footage by searching YouTube, which is the right way to find a video and the
wrong way to answer "what was on last night?". This asks the scoreboards instead (ESPN's public JSON, the same
one feeds.py reads for a game's play-by-play): one request per league and day, kept on disk for a few minutes
while games are being played and for hours once a day's games are all over. The list is what the Sports tab
shows under Games played, what its drop-downs choose from, and what the Telegram bot and its Mini App offer, so
a game is picked, not typed.

A game here is a row to pick, not footage yet: picking one asks the desk (desk.py) for it by its two teams,
and the search for its highlights goes on from there as for any other request.

The scoreboards cover soccer, basketball, American football, hockey and baseball (the leagues in LEAGUES).
Tennis, golf, fights, cricket and races have no scoreboard of two teams to read, so those are still asked for
by name.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Callable

from .. import sports
from . import feeds

HOUR = 3600.0
HOURS = 24.0  # "the last 24 hours"
GAME_LENGTH = 3.5 * HOUR  # a game that started this long before the 24 hours began may have ended inside them
SOON = 12 * HOUR  # games starting within this (and still today, US Eastern) are listed too: "Today 7:30 PM"
EASTERN = -5 * HOUR  # ESPN files a game under its US Eastern day; an hour of summer time changes nothing here
POOL = 6  # scoreboards asked for at once
DEADLINE = 14.0  # s the whole list waits for the scoreboards; what hasn't answered by then is left out and said
FRESH_LIVE, FRESH_TODAY, FRESH_PAST = 120.0, 300.0, 6 * HOUR

Get = Callable[[str, dict], dict]

# (sport, ESPN's league path, the league as the Sports filter names it)
LEAGUES: tuple[tuple[str, str, str], ...] = (
    ("basketball", "nba", "NBA"), ("basketball", "wnba", "WNBA"),
    ("basketball", "mens-college-basketball", "NCAA"),
    ("football", "nfl", "NFL"), ("football", "college-football", "College football"),
    ("hockey", "nhl", "NHL"),
    ("baseball", "mlb", "MLB"),
    ("soccer", "eng.1", "Premier League"), ("soccer", "uefa.champions", "Champions League"),
    ("soccer", "esp.1", "La Liga"), ("soccer", "ita.1", "Serie A"), ("soccer", "ger.1", "Bundesliga"),
    ("soccer", "fra.1", "Ligue 1"), ("soccer", "usa.1", "MLS"), ("soccer", "uefa.europa", "Europa League"),
    ("soccer", "mex.1", "Liga MX"), ("soccer", "eng.fa", "FA Cup"), ("soccer", "eng.league_cup", "League Cup"),
    ("soccer", "eng.2", "Championship"), ("soccer", "usa.nwsl", "NWSL"), ("soccer", "fifa.world", "World Cup"),
    ("soccer", "uefa.euro", "Euros"),
)
SPORTS = tuple(dict.fromkeys(sp for sp, _c, _l in LEAGUES))
STATES = {"post": "final", "in": "live", "pre": "soon"}

_lock = threading.Lock()
_last: dict[str, dict] = {}  # the last list made for each (sport, league), for a caller that can't wait


def covers(sport: str) -> bool:
    """Whether the scoreboards list this sport's games."""
    return sport in SPORTS


def leagues_for(sport: str = "any", league: str = "") -> list[tuple[str, str, str]]:
    """The scoreboards to read for a sport (every one for "any") and a league (its own; every one for "")."""
    want = " ".join(str(league or "").lower().split())
    out = [row for row in LEAGUES if sport in ("", "any", row[0])]
    if want:
        named = [row for row in out if row[2].lower() == want]
        out = named or [row for row in out if want in row[2].lower() or row[2].lower() in want]
    return out


def days(now: float, hours: float = HOURS) -> list[str]:
    """The Eastern days (YYYYMMDD) that the last `hours` touch, oldest first (tonight's games yet to start are
    on today's board with the rest)."""
    stamps = (now - hours * HOUR - GAME_LENGTH, now - hours * HOUR / 2, now)
    return list(dict.fromkeys(time.strftime("%Y%m%d", time.gmtime(t + EASTERN)) for t in stamps))


def _names(c: dict) -> list[str]:
    """What a team is called, the name a highlights title most likely uses first."""
    t = c.get("team") if isinstance(c.get("team"), dict) else {}
    # "Knicks" before "New York Knicks": a title says "KNICKS at CELTICS". A soccer club's short name is its
    # nickname in the same way ("Man City"), and its full one comes right after.
    order = ("shortDisplayName", "name", "displayName", "location")
    out: list[str] = []
    for k in order:
        v = " ".join(str(t.get(k) or "").split())
        if v and v.lower() not in (o.lower() for o in out):
            out.append(v[:40])
    return out


def _score(c: dict) -> int | None:
    v = c.get("score")
    if isinstance(v, dict):  # some boards give {"value": 3.0, "displayValue": "3"}
        v = v.get("displayValue", v.get("value"))
    return feeds._int(v)


def read_event(ev: dict, sport: str, code: str, league: str) -> dict | None:
    """One scoreboard entry as a game to pick, or None when it isn't two sides playing."""
    if not isinstance(ev, dict):
        return None
    comp = (ev.get("competitions") or [{}])[0]
    comp = comp if isinstance(comp, dict) else {}
    home, away = feeds._competitors(comp)
    hn, an = _names(home), _names(away)
    if not hn or not an:
        return None
    status = ev.get("status") if isinstance(ev.get("status"), dict) else comp.get("status") or {}
    kind = status.get("type") if isinstance(status.get("type"), dict) else {}
    state = STATES.get(str(kind.get("state") or ""), "soon")
    if kind.get("completed") is True:
        state = "final"
    words = " ".join(str(kind.get("shortDetail") or kind.get("detail") or kind.get("description") or "").split())
    if any(w in f"{kind.get('name', '')} {words}".upper() for w in ("POSTPONED", "CANCELED", "CANCELLED", "SUSPENDED",
                                                                    "ABANDONED", "DELAYED")):
        state = "off"
    start = feeds._wall(ev.get("date") or comp.get("date")) or 0.0
    hs, as_ = _score(home), _score(away)
    if state == "soon":
        hs = as_ = None
    sp = sports.get(sport) or sports.SPORTS["any"]
    soccer = sport == "soccer"
    # Soccer names the home side first ("Arsenal v Chelsea"); the US sports name the visitors first ("at").
    label = f"{hn[0]} v {an[0]}" if soccer else f"{an[0]} at {hn[0]}"
    if hs is not None and as_ is not None:
        line = f"{hn[0]} {hs}-{as_} {an[0]}" if soccer else f"{an[0]} {as_}, {hn[0]} {hs}"
    else:
        line = label
    winner = ""
    if state == "final" and hs is not None and as_ is not None and hs != as_:
        winner = "home" if hs > as_ else "away"
    return {"id": f"{code}-{ev.get('id') or ''}"[:60], "event": str(ev.get("id") or ""), "sport": sport,
            "emoji": sp.emoji, "league": league, "code": code, "home": hn[0], "away": an[0], "home_names": hn,
            "away_names": an, "home_score": hs, "away_score": as_, "state": state,
            "detail": "Final" if state == "final" and not words else words[:40], "start": start, "label": label,
            "line": line, "winner": winner}


def fresh_for(day: str, now: float) -> float:
    """How long a day's scoreboard is kept before it's asked for again."""
    today = time.strftime("%Y%m%d", time.gmtime(now + EASTERN))
    yesterday = time.strftime("%Y%m%d", time.gmtime(now + EASTERN - 24 * HOUR))
    if day >= today:
        return FRESH_LIVE
    return FRESH_TODAY if day == yesterday else FRESH_PAST


def board(sport: str, code: str, league: str, day: str, now: float, get: Get | None = None) -> list[dict]:
    """One league's games on one day. Raises feeds.FeedError."""
    path = feeds.ESPN_SPORT.get(sport, sport)
    url = f"{feeds.ESPN_URL}/{path}/{code}/scoreboard?dates={day}"
    data = get(url, {}) if get else feeds.http_get(url, max_age=fresh_for(day, now), now=lambda: now)
    events = data.get("events") if isinstance(data, dict) else None
    if events is not None and not isinstance(events, list):
        raise feeds.FeedError("Its scoreboard couldn't be read.")
    out = []
    for ev in events or []:
        g = read_event(ev, sport, code, league)
        if g is not None and g["event"]:
            out.append(g)
    return out


def played(sport: str = "any", league: str = "", *, hours: float = HOURS, now: float | None = None,
           get: Get | None = None, deadline: float = DEADLINE, soon: bool = True) -> dict:
    """Every game of the last `hours` (finished or being played), newest first, then the ones about to start.

    Returns {"at", "hours", "games": [...], "leagues": [{"sport", "league", "emoji", "games", "final"}],
    "problems": [...], "covered": bool}. A scoreboard that can't be read is a line in "problems", never an
    error: the other leagues' games are still there."""
    now = time.time() if now is None else now
    rows = leagues_for(sport, league)
    out: dict = {"at": now, "hours": hours, "games": [], "leagues": [], "problems": [],
                 "covered": bool(rows), "sport": sport or "any", "league": league or ""}
    if not rows:
        return out
    jobs = [(sp, code, name, day) for sp, code, name in rows for day in days(now, hours)]
    found: dict[str, dict] = {}
    failed: dict[str, str] = {}
    pool = ThreadPoolExecutor(max_workers=POOL, thread_name_prefix="slate")
    try:
        futures = {pool.submit(board, sp, code, name, day, now, get): (name, day) for sp, code, name, day in jobs}
        done, late = wait(futures, timeout=max(0.5, deadline))
        for fut in done:
            name, _day = futures[fut]
            try:
                for g in fut.result():
                    found[g["id"]] = g
            except feeds.FeedError as exc:
                failed.setdefault(name, str(exc))
            except Exception as exc:  # noqa: BLE001 - a board in a shape nobody has seen: that league is left out
                failed.setdefault(name, f"Its scoreboard couldn't be read ({type(exc).__name__}).")
        for fut in late:
            fut.cancel()
            failed.setdefault(futures[fut][0], "Its scoreboard didn't answer in time.")
    finally:
        pool.shutdown(wait=False)
    since = now - hours * HOUR
    games = []
    for g in found.values():
        start = g["start"]
        if g["state"] == "off" or not start:
            continue
        if g["state"] == "soon":
            if soon and now - HOUR <= start <= now + SOON:
                games.append(g)
        elif g["state"] == "live":
            if now - 8 * HOUR <= start <= now + HOUR:
                games.append(g)
        elif since - GAME_LENGTH <= start <= now:
            games.append(g)
    rank = {"live": 0, "final": 1, "soon": 2}
    games.sort(key=lambda g: (rank[g["state"]], -g["start"] if g["state"] != "soon" else g["start"], g["label"]))
    out["games"] = games
    order = {name: i for i, (_sp, _code, name) in enumerate(LEAGUES)}
    seen: dict[str, dict] = {}
    for g in games:
        row = seen.setdefault(g["league"], {"sport": g["sport"], "league": g["league"], "emoji": g["emoji"],
                                            "games": 0, "final": 0, "live": 0})
        if g["state"] != "soon":
            row["games"] += 1
        row["final"] += g["state"] == "final"
        row["live"] += g["state"] == "live"
    out["leagues"] = sorted(seen.values(), key=lambda r: order.get(r["league"], 99))
    # A league that failed on one day but answered on another still shows its games: say only what's missing.
    out["problems"] = [f"{name}: {why}" for name, why in failed.items()][:6]
    with _lock:
        _last[f"{sport or 'any'}|{league or ''}"] = out
    return out


def last(sport: str = "any", league: str = "") -> dict | None:
    """The list `played` made last for this sport and league (in this process), without asking again."""
    with _lock:
        return _last.get(f"{sport or 'any'}|{league or ''}")


def find(game_id: str, *, now: float | None = None, get: Get | None = None) -> dict | None:
    """A listed game by its id (the league's scoreboards are read again, from what's kept on disk)."""
    code = str(game_id or "").rsplit("-", 1)[0]
    row = next((r for r in LEAGUES if r[1] == code), None)
    if row is None:
        return None
    for g in played(row[0], row[2], now=now, get=get)["games"]:
        if g["id"] == game_id:
            return g
    return None


def when(g: dict, now: float | None = None) -> str:
    """'Final', 'Live: 6:00 - 2nd', 'Today 7:30 PM' for a game's row."""
    now = time.time() if now is None else now
    if g["state"] == "final":
        return g.get("detail") or "Final"
    if g["state"] == "live":
        return "Live" + (f": {g['detail']}" if g.get("detail") else "")
    lt = time.localtime(g["start"])
    day = "Today" if lt[:3] == time.localtime(now)[:3] else time.strftime("%a", lt)
    return f"{day} {time.strftime('%I:%M %p', lt).lstrip('0')}"


PUBLIC = ("id", "sport", "emoji", "league", "home", "away", "home_score", "away_score", "state", "label", "line",
          "winner", "start")


def public(out: dict, now: float | None = None) -> dict:
    """`played`'s answer as the pages take it (the Sports tab, the Mini App, a linked Mac): each game with what
    its row says ("when") and whether its highlights can be asked for yet ("ready": it's over)."""
    now = time.time() if now is None else now
    return {"at": out["at"], "hours": out.get("hours", HOURS), "covered": out["covered"],
            "problems": list(out["problems"]), "leagues": out["leagues"],
            "games": [{**{k: g[k] for k in PUBLIC}, "when": when(g, now), "ready": g["state"] == "final"}
                      for g in out["games"]]}


def order_values(g: dict, want: str = "reel", plays: int | None = None, category: str = "") -> dict:
    """What Desk.order takes to make a listed game's highlights: its league, its two teams (with every name a
    title may use for them) and the days its footage went up in."""
    values = {"sport": g["sport"], "league": g["league"], "teams": f"{g['away']}, {g['home']}", "since": "3days",
              "want": want, "games": 1,
              "match": {"id": g["id"], "label": g["label"], "line": g["line"], "state": g["state"],
                        "sides": [list(g.get("away_names") or [g["away"]]),
                                  list(g.get("home_names") or [g["home"]])]}}
    if plays:
        values["plays"] = plays
    if category:
        values["category"] = category
    return values

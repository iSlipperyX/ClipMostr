"""The games played (ytclip/sportsdesk/slate.py): every game of the last 24 hours from the scoreboards, the rows
the Sports tab, the Telegram bot and the Mini App pick from. The scoreboards here are stand-ins in the shape
ESPN's really has (read from the live NBA and NHL boards on 2026-11-11): scores are strings, the state is
pre / in / post, a game is filed under its US Eastern day and its date is UTC."""

from __future__ import annotations

import time

import pytest

from ytclip.sportsdesk import feeds, slate

NOW = 1_794_366_000.0  # 2026-11-11 03:00 UTC = Tuesday 10 PM Eastern
HOUR = 3600.0


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(t))


def team(abbr, city, name, short=None):
    return {"id": "1", "abbreviation": abbr, "displayName": f"{city} {name}".strip(), "shortDisplayName": short or name,
            "name": name, "location": city, "logo": f"https://a.espncdn.com/i/teamlogos/x/{abbr.lower()}.png"}


def event(eid, away, home, start, state="post", score=("108", "112"), detail="Final", name="STATUS_FINAL"):
    status = {"clock": 0.0, "displayClock": "0.0", "period": 4,
              "type": {"id": "3", "name": name, "state": state, "completed": state == "post",
                       "description": detail, "detail": detail, "shortDetail": detail}}
    return {"id": eid, "date": iso(start), "name": f"{away['displayName']} at {home['displayName']}",
            "shortName": f"{away['abbreviation']} @ {home['abbreviation']}", "status": status,
            "competitions": [{"date": iso(start), "venue": {"fullName": "The Garden"}, "status": status, "competitors": [
                {"homeAway": "home", "score": score[1], "team": home},
                {"homeAway": "away", "score": score[0], "team": away}]}]}


BUCKS, KNICKS = team("MIL", "Milwaukee", "Bucks"), team("NY", "New York", "Knicks")
CELTICS, SIXERS = team("BOS", "Boston", "Celtics"), team("PHI", "Philadelphia", "76ers")
LAKERS, SUNS = team("LAL", "Los Angeles", "Lakers"), team("PHX", "Phoenix", "Suns")
HEAT, BULLS = team("MIA", "Miami", "Heat"), team("CHI", "Chicago", "Bulls")
AVS, LEAFS = team("COL", "Colorado", "Avalanche"), team("TOR", "Toronto", "Maple Leafs")
CITY, POOL = team("MNC", "", "Manchester City", "Man City"), team("LIV", "", "Liverpool")

BOARDS = {
    ("basketball/nba", "20261110"): [
        event("401", BUCKS, KNICKS, NOW - 2.5 * HOUR),  # tonight's early game: over
        event("402", CELTICS, SIXERS, NOW - 1.5 * HOUR, "in", ("88", "91"), "6:00 - 4th", "STATUS_IN_PROGRESS"),
        event("403", LAKERS, SUNS, NOW + 0.5 * HOUR, "pre", ("0", "0"), "11/10 - 10:30 PM EST", "STATUS_SCHEDULED"),
    ],
    ("basketball/nba", "20261109"): [
        event("390", HEAT, BULLS, NOW - 26 * HOUR, score=("99", "97")),  # last night: it ended inside the 24 hours
        event("380", LAKERS, BUCKS, NOW - 29 * HOUR),  # and this one didn't
        event("391", SUNS, HEAT, NOW - 25 * HOUR, "post", ("0", "0"), "Postponed", "STATUS_POSTPONED"),
    ],
    ("hockey/nhl", "20261110"): [
        event("501", AVS, LEAFS, NOW - 3 * HOUR, "in", ("3", "2"), "End of 2nd", "STATUS_END_PERIOD")],
    ("soccer/eng.1", "20261110"): [event("601", POOL, CITY, NOW - 8 * HOUR, score=("1", "2"), detail="FT")],
}


@pytest.fixture
def boards():
    asked = []

    def get(url, headers):
        path, day = url.split("/sports/")[1].split("/scoreboard?dates=")
        asked.append((path, day))
        if path == "football/nfl":
            raise feeds.FeedError("The feed answered 503.")
        return {"leagues": [{}], "events": list(BOARDS.get((path, day), []))}

    get.asked = asked
    return get


def test_the_last_24_hours_of_one_league(boards):
    out = slate.played("basketball", "NBA", now=NOW, get=boards)
    assert [(g["event"], g["state"]) for g in out["games"]] == [("402", "live"), ("401", "final"), ("390", "final"),
                                                                ("403", "soon")]
    assert sorted(set(boards.asked)) == [("basketball/nba", "20261109"), ("basketball/nba", "20261110")]
    final = out["games"][1]
    assert final["id"] == "nba-401" and final["label"] == "Bucks at Knicks" and final["line"] == "Bucks 108, Knicks 112"
    assert (final["home"], final["away"], final["home_score"], final["away_score"]) == ("Knicks", "Bucks", 112, 108)
    assert final["winner"] == "home" and final["league"] == "NBA" and final["emoji"] == "🏀"
    assert final["home_names"] == ["Knicks", "New York Knicks", "New York"]
    live = out["games"][0]
    assert live["line"] == "Celtics 88, 76ers 91" and slate.when(live, NOW) == "Live: 6:00 - 4th"
    soon = out["games"][-1]
    assert soon["line"] == "Lakers at Suns" and soon["home_score"] is None  # no "0-0" before it starts
    assert slate.when(final, NOW) == "Final"
    assert out["leagues"] == [{"sport": "basketball", "league": "NBA", "emoji": "🏀", "games": 3, "final": 2, "live": 1}]
    assert out["problems"] == [] and out["covered"]
    assert slate.last("basketball", "NBA") is out


def test_every_league_at_once_and_a_board_that_fails_is_only_a_note(boards):
    out = slate.played(now=NOW, get=boards)
    assert [r["league"] for r in out["leagues"]] == ["NBA", "NHL", "Premier League"]
    assert {(g["league"], g["event"]) for g in out["games"]} >= {("NHL", "501"), ("Premier League", "601")}
    city = next(g for g in out["games"] if g["event"] == "601")
    assert city["label"] == "Man City v Liverpool" and city["line"] == "Man City 2-1 Liverpool"  # home first, as soccer does
    assert city["home_names"][:2] == ["Man City", "Manchester City"]
    assert out["problems"] == ["NFL: The feed answered 503."]
    assert len(set(boards.asked)) == len(slate.LEAGUES) * 2  # one request a league and day, no more


def test_no_soon_and_a_sport_the_scoreboards_dont_cover(boards):
    out = slate.played("basketball", "NBA", now=NOW, get=boards, soon=False)
    assert all(g["state"] != "soon" for g in out["games"])
    golf = slate.played("golf", now=NOW, get=boards)
    assert golf["games"] == [] and golf["covered"] is False
    assert slate.covers("soccer") and not slate.covers("tennis")
    assert [r[2] for r in slate.leagues_for("football")] == ["NFL", "College football"]
    assert [r[2] for r in slate.leagues_for("any", "premier league")] == ["Premier League"]


def test_the_days_asked_for_are_eastern_days():
    # 03:00 UTC on the 11th is still the evening of the 10th in the US: tonight's games are on the 10th's board
    assert slate.days(NOW) == ["20261109", "20261110"]
    assert slate.days(NOW + 4 * HOUR) == ["20261109", "20261110", "20261111"]  # 2 AM Eastern: three days touch it
    assert slate.fresh_for("20261110", NOW) == slate.FRESH_LIVE
    assert slate.fresh_for("20261109", NOW) == slate.FRESH_TODAY
    assert slate.fresh_for("20261108", NOW) == slate.FRESH_PAST


def test_a_game_is_found_again_by_its_id_and_becomes_a_request(boards):
    g = slate.find("nba-401", now=NOW, get=boards)
    assert g["label"] == "Bucks at Knicks"
    assert slate.find("nba-999", now=NOW, get=boards) is None and slate.find("curling-1", now=NOW, get=boards) is None
    values = slate.order_values(g, "reel", category="h1")
    assert values == {"sport": "basketball", "league": "NBA", "teams": "Bucks, Knicks", "since": "3days",
                      "want": "reel", "games": 1, "category": "h1",
                      "match": {"id": "nba-401", "label": "Bucks at Knicks", "line": "Bucks 108, Knicks 112",
                                "state": "final", "sides": [["Bucks", "Milwaukee Bucks", "Milwaukee"],
                                                            ["Knicks", "New York Knicks", "New York"]]}}


def test_boards_in_shapes_nobody_expected(boards):
    assert slate.read_event({"id": "1", "competitions": [{"competitors": [{"homeAway": "home", "team": {}}]}]},
                            "basketball", "nba", "NBA") is None
    assert slate.read_event("nope", "basketball", "nba", "NBA") is None
    ev = event("7", BUCKS, KNICKS, NOW - HOUR)
    ev["competitions"][0]["competitors"][0]["score"] = {"value": 112.0, "displayValue": "112"}
    del ev["status"]  # only the competition carries it
    g = slate.read_event(ev, "basketball", "nba", "NBA")
    assert g["home_score"] == 112 and g["state"] == "final"

    def broken(url, headers):
        return {"events": "soon"}

    with pytest.raises(feeds.FeedError):
        slate.board("basketball", "nba", "NBA", "20261110", NOW, broken)
    out = slate.played("hockey", now=NOW, get=broken)
    assert out["games"] == [] and out["problems"] == ["NHL: Its scoreboard couldn't be read."]
    out = slate.played("hockey", now=NOW, get=lambda url, headers: 1 / 0)
    assert out["problems"] == ["NHL: Its scoreboard couldn't be read (ZeroDivisionError)."]


def test_a_scoreboard_is_asked_for_once_and_kept(monkeypatch, tmp_path):
    calls = []

    class Reply:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def opener(req, timeout=0):
        import json

        calls.append(req.full_url)
        day = req.full_url.rsplit("=", 1)[1]
        return Reply(json.dumps({"events": list(BOARDS.get(("hockey/nhl", day), []))}).encode())

    monkeypatch.setattr(feeds.urllib.request, "urlopen", opener)
    now = time.time()
    first = slate.played("hockey", now=now)
    again = slate.played("hockey", now=now)
    assert len(calls) == len(slate.days(now)) and first["problems"] == again["problems"] == []
    assert all("site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates=" in u for u in calls)

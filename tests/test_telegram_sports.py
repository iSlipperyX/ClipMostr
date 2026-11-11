"""Asking for sports highlights from Telegram (the chat's /sports and the Mini App's Make → Sports highlights):
the request becomes a sports order in the asker's name, automation's sports worker does it, and both people are
told how it went. Telegram is telegram_fake, the search and the engine are stand-ins, the server is the real one."""

import calendar
import json
import time
from pathlib import Path

import pytest

from telegram_fake import JUSTYN, buttons
import test_sports_slate as SL
from test_sports_desk import FakeEngine, found, play_sized
from test_sports_orders import reels  # noqa: F401 - a fixture: reel.make without rendering
from test_telegram import make_video, world  # noqa: F401 - `world` is a fixture
from test_telegram_app import SAM, link, served, to  # noqa: F401 - `served` is a fixture
from ytclip import ai
from ytclip.sportsdesk import feeds, slate
from ytclip.sportsdesk import games as G
from ytclip.sportsdesk.desk import DeskWorker
from ytclip.telegram import bot as botmod
from ytclip.telegram import state, webapp_page


class SportsEngine(FakeEngine):
    """FakeEngine, exporting real little videos with the clips.json posting reads."""

    def export(self, p):
        folder = Path(self.s.output_dir) / "Lakers at Celtics [a1]"
        folder.mkdir(parents=True, exist_ok=True)
        recs = []
        for i, c in enumerate(p.clips, 1):
            name = f"{i:02d} - {c.title or 'play'}.mp4"
            make_video(folder / name)
            recs.append({"rank": i, "file": name, "title": c.title or f"Play {i}", "score": 90 - i, "start": c.start,
                         "end": c.end, "length": round(c.end - c.start, 2), "qc": {"ok": True}})
        (folder / "clips.json").write_text(json.dumps({
            "video": {"id": "a1", "title": "Lakers at Celtics | Extended Highlights", "channel": "NBA"},
            "automation": p.meta["automation"], "clips": recs}), encoding="utf-8")
        self.exported = p
        return folder


@pytest.fixture
def sports(world, monkeypatch):  # noqa: F811
    """The world with a basketball category, the second person linked, and a sports worker to run by hand."""
    link(world)
    world.s.categories = world.s.categories + [{"id": "h1", "name": "Hoops", "sport": "basketball",
                                                "account_id": world.account.id}]
    monkeypatch.setattr("ytclip.export.thumbnail", lambda src, dest, at: Path(dest).write_bytes(b"jpg"))
    monkeypatch.setattr(ai, "order", lambda s: [])
    monkeypatch.setattr(DeskWorker, "_youtube_pause", lambda self: None)
    monkeypatch.setattr(G, "search_free", lambda q, **k: [
        found("a1", "Lakers at Celtics | Extended Highlights", 20, 3),
        found("c3", "Heat at Knicks | Extended Highlights", 20, 4)])
    world.worker = DeskWorker(world.bot.desk, lambda: world.s, "test:1", engine_factory=SportsEngine)
    world.worker.probe = play_sized

    def work(most=10):
        n = 0
        while world.worker.step():
            n += 1
            assert n < most
        return n

    world.work = work
    yield world
    world.bot.stop()


@pytest.mark.parametrize("text, want", [
    ("NBA Lakers vs Celtics", {"league": "NBA", "sport": "basketball", "teams": "Lakers, Celtics", "want": "reel",
                               "since": "week"}),
    ("Premier League 2 games, reel only", {"league": "Premier League", "sport": "soccer", "games": 2, "want": "reel",
                                           "since": "week"}),
    ("UFC 3 clips", {"league": "UFC", "sport": "combat", "plays": 3, "want": "clips", "since": "week"}),
    ("lakers at celtics last night", {"teams": "lakers, celtics", "want": "reel", "since": "3days"}),
    ("soccer Arsenal v Chelsea highlights and a reel", {"sport": "soccer", "teams": "Arsenal, Chelsea", "want": "reel",
                                                        "since": "week"}),
    ("soccer Arsenal v Chelsea 4 clips and a reel", {"sport": "soccer", "teams": "Arsenal, Chelsea", "want": "both",
                                                     "plays": 4, "since": "week"}),
    ("NHL both", {"league": "NHL", "sport": "hockey", "want": "both", "since": "week"}),
    ("top 5 plays NFL Chiefs, Bills this month", {"league": "NFL", "sport": "football", "plays": 5, "want": "clips",
                                                  "teams": "Chiefs, Bills", "since": "month"}),
    ("f1 recap", {"league": "Formula 1", "sport": "motorsport", "want": "reel", "since": "week"}),
    ("hockey", {"sport": "hockey", "want": "reel", "since": "week"}),  # a reel unless single plays are asked for
])
def test_what_was_said_becomes_a_sports_request(text, want):
    assert botmod.parse_sports(text) == want


def test_nothing_to_look_for_says_how_to_ask():
    for text in ("", "the best highlights please", "3 clips"):
        with pytest.raises(ValueError, match="Which game"):
            botmod.parse_sports(text)


def test_asking_in_the_chat_makes_the_highlights_and_tells_both(sports):
    w = sports
    sent = w.say("/sports NBA Lakers vs Celtics 2 clips", SAM)
    assert "Got it: Lakers vs Celtics (NBA): the top 2 plays as clips for Hoops" in sent["text"]
    assert "they show up under Videos" in sent["text"]
    assert to(w, JUSTYN)[-1]["text"] == ("🏆 <b>Sam</b> asked for sports highlights: Lakers vs Celtics (NBA): the top 2 "
                                         "plays as clips.")
    o = w.bot.desk.orders()[0]
    assert (o["by"], o["by_id"], o["status"], o["category"]) == ("Sam", SAM["id"], "queued", "Hoops")
    data = state.load(w.state_file)
    assert data["asked"][f"sports:{o['id']}"]["chat"] == SAM["id"]
    assert data["activity"][-1]["what"] == "sports" and data["activity"][-1]["name"] == "Sam"
    assert any("Sam asked for sports highlights" in line for line in w.logs)
    w.fake.clear()
    w.bot.check(w.clock[0])
    assert not w.fake.sent("sendMessage")  # nothing to say while it's under way
    assert w.work() == 3  # the search, the highlights, the clips
    o = w.bot.desk.orders()[0]
    assert o["status"] == "done" and o["steps"][-1]["clips"] == 2
    items = w.take()
    assert len(items) == 2 and all(i.category_id == "h1" for i in items)
    assert items[0].facts["permission"] == "Asked for by Sam from Telegram"
    w.bot.check(w.clock[0])
    mine, theirs = to(w, SAM)[-1], to(w, JUSTYN)[-1]
    assert mine["text"].startswith("🏀 <b>Your sports request is ready</b>: Lakers vs Celtics (NBA): the top 2 plays "
                                   "as clips\n• Made 2 clips from Lakers at Celtics, for Hoops")
    assert theirs["text"].startswith("🏀 <b>Sam's sports request is ready</b>: Lakers vs Celtics")
    assert len(buttons(mine["reply_markup"])) == 3 and buttons(mine["reply_markup"])[0][1] == f"v:{items[0].id}"
    assert state.load(w.state_file)["asked"] == {}
    w.fake.clear()
    w.bot.check(w.clock[0])
    assert not w.fake.sent("sendMessage")  # told once


def test_the_report_waits_for_posting_to_bring_the_clips_in(sports):
    w = sports
    w.say("/sports NBA Lakers vs Celtics 2 clips")
    w.work()
    w.fake.clear()
    w.bot.check(w.clock[0])
    assert not w.fake.sent("sendMessage")  # made a moment ago, not in the library yet
    w.clock[0] += 16 * 60  # and if they never come in (no account for the category), that's said
    w.bot.check(w.clock[0])
    assert "They're in the Library on the PC" in to(w, JUSTYN)[-1]["text"]


def test_a_request_that_finds_no_game_says_so_to_both(sports):
    w = sports
    w.say("/sports NBA Bulls reel", SAM)
    w.work()
    w.fake.clear()
    w.bot.check(w.clock[0])
    assert to(w, SAM)[-1]["text"] == ("✖ <b>Your sports request couldn't be done</b> (Bulls (NBA): a recap reel): No "
                                      "game matched (2 videos looked at: 2 another team).")
    assert to(w, JUSTYN)[-1]["text"].startswith("✖ <b>Sam's sports request couldn't be done</b>")
    assert state.load(w.state_file)["asked"] == {}


def test_sports_by_itself_asks_which_game_and_can_cancel(sports):
    w = sports
    sent = w.say("/sports")
    assert "Which game?" in sent["text"] and "copyright claims" in sent["text"]
    sent = w.say("basketball, reel only")  # the next message is the answer
    assert "Got it: Basketball: a recap reel for Hoops" in sent["text"]
    sent = w.say("/sports", SAM)
    assert "<b>Under way</b>" in sent["text"] and "Basketball: a recap reel (Justyn)" in sent["text"]
    sent = w.say("/sports the best highlights please")
    assert "Which game?" in sent["text"]
    w.fake.clear()
    sent = w.say("/sports cancel", SAM)  # Sam has none of their own: the newest one goes
    assert sent["text"] == "✖ Stopped: Basketball: a recap reel."
    assert "<b>Sam</b> cancelled the request for Basketball: a recap reel." in to(w, JUSTYN)[-1]["text"]
    assert w.bot.desk.orders()[0]["status"] == "cancelled" and state.load(w.state_file)["asked"] == {}
    assert w.say("/sports cancel")["text"] == "No sports request is waiting or under way."
    assert "/sports NBA Lakers vs Celtics" in botmod.help_html() and "recap reel" in botmod.help_html(True)
    assert any(c == "sports" for c, _ in botmod.COMMANDS)


def test_asking_from_the_app(served, monkeypatch):
    w = served
    w.s.categories = w.s.categories + [{"id": "h1", "name": "Hoops", "sport": "basketball",
                                        "account_id": w.account.id}]
    d = w.session(SAM)
    assert w.call("GET", "/api/sports")[0] == 401 and w.call("POST", "/api/sports/order", {"sport": "golf"})[0] == 401
    status, form, _ = w.call("GET", "/api/sports", token=d["token"])
    assert status == 200 and form["orders"] == [] and form["most"] == {"games": 3, "plays": 7, "open": 4}
    ball = next(s for s in form["sports"] if s["id"] == "basketball")
    assert ball["emoji"] == "🏀" and "NBA" in ball["leagues"]
    assert {"id": "h1", "name": "Hoops", "sport": "basketball"}.items() <= form["categories"][0].items()
    assert [s["id"] for s in form["since"]][:3] == ["day", "3days", "week"]
    status, err, _ = w.call("POST", "/api/sports/order", {"want": "both"}, token=d["token"])
    assert status == 400 and "which sport, league or teams" in err["error"]
    w.fake.clear()
    status, r, _ = w.call("POST", "/api/sports/order", {"sport": "basketball", "league": "NBA",
                                                         "teams": "Lakers vs Celtics", "want": "both", "plays": 3,
                                                         "games": 1, "since": "3days"}, token=d["token"])
    assert status == 200 and r["message"].startswith("Got it: Lakers vs Celtics (NBA): the top 3 plays as clips and a "
                                                     "recap reel for Hoops.")
    oid = r["order"]["id"]
    assert "<b>Sam</b> asked for sports highlights" in to(w, JUSTYN)[-1]["text"]
    _, form, _ = w.call("GET", "/api/sports", token=d["token"])
    assert [(o["id"], o["mine"], o["by"], o["status"]) for o in form["orders"]] == [(oid, True, "Sam", "queued")]
    mine = w.session(JUSTYN)
    _, theirs, _ = w.call("GET", "/api/sports", token=mine["token"])
    assert theirs["orders"][0]["mine"] is False  # both see it; it's Sam's
    _, acts, _ = w.call("GET", "/api/activity", token=mine["token"])
    assert (acts["items"][0]["what"], acts["items"][0]["name"]) == ("sports", "Sam")
    # either of them can stop it; once stopped it can't be stopped again, and a made-up id isn't one
    status, r, _ = w.call("POST", f"/api/sports/orders/{oid}/cancel", token=mine["token"])
    assert status == 200 and r["order"]["status"] == "cancelled"
    assert w.call("POST", f"/api/sports/orders/{oid}/cancel", token=mine["token"])[0] == 409
    assert w.call("POST", "/api/sports/orders/nope/cancel", token=mine["token"])[0] == 404
    assert w.call("POST", "/api/sports/orders/..%2Fx/cancel", token=mine["token"])[0] == 404
    assert "<b>Justyn</b> cancelled the request for Lakers vs Celtics" in to(w, SAM)[-1]["text"]
    w.bot.stop()


def test_the_page_has_the_sports_form():
    js = webapp_page.JS
    for bit in ("api/sports/order", "Sports highlights", "Make its reel", "/cancel", "copyright claims",
                "api/sports/played", "Pick a game", "Describe it", "All games, last 24 hours", "under the minimums",
                "On the PC now"):
        assert bit in js
    assert "want: 'reel'" in js  # a reel is what's asked for unless something else is picked
    assert "innerHTML" not in js  # what people type and what YouTube titles say is only ever set as text


# --- the games played: tap one, don't type it ---------------------------------------------------------------------


def scoreboards(now, extra=()):
    """Stand-in scoreboards in ESPN's shape around `now`: two NBA games over, one being played, one to come, an
    NHL game over, and baseball's board down."""
    H = 3600.0
    t, ev = SL.team, SL.event
    lakers, celtics = t("LAL", "Los Angeles", "Lakers"), t("BOS", "Boston", "Celtics")
    boards = {
        "basketball/nba": [
            ev("401", lakers, celtics, now - 4 * H, score=("108", "112")),
            ev("404", t("MIA", "Miami", "Heat"), t("NY", "New York", "Knicks"), now - 5 * H, score=("99", "97")),
            ev("402", t("GS", "Golden State", "Warriors"), t("DEN", "Denver", "Nuggets"), now - 1.5 * H, "in",
               ("88", "91"), "6:00 - 4th", "STATUS_IN_PROGRESS"),
            ev("403", t("PHX", "Phoenix", "Suns"), t("DAL", "Dallas", "Mavericks"), now + 0.5 * H, "pre", ("0", "0"),
               "10:30 PM EST", "STATUS_SCHEDULED")],
        "hockey/nhl": [ev("501", t("COL", "Colorado", "Avalanche"), t("TOR", "Toronto", "Maple Leafs"), now - 3 * H,
                          score=("3", "2"))],
    }
    for path, events in extra:
        boards.setdefault(path, []).extend(events)

    def get(url, headers):
        path, day = url.split("/sports/")[1].split("/scoreboard?dates=")
        if path == "baseball/mlb":
            raise feeds.FeedError("The feed answered 503.")
        return {"leagues": [{}], "events": [
            e for e in boards.get(path, [])
            if time.strftime("%Y%m%d", time.gmtime(calendar.timegm(time.strptime(e["date"], "%Y-%m-%dT%H:%MZ"))
                                                   + slate.EASTERN)) == day]}

    return get


def test_games_lists_the_last_24_hours_and_a_tap_asks_for_the_reel(sports, reels):  # noqa: F811
    w = sports
    w.bot.slate_get = scoreboards(w.clock[0])
    sent = w.say("/games", SAM)
    text = sent["text"]
    assert text.startswith("🏆 <b>Games played, last 24 hours</b>")
    assert "🏀 <b>NBA</b>" in text and "🏒 <b>NHL</b>" in text
    assert "• Lakers 108, Celtics 112 · Final" in text and "• Warriors 88, Nuggets 91 · Live: 6:00 - 4th" in text
    assert "Still to come: Suns at Mavericks" in text and "Tap a game for its recap reel" in text
    assert "⚠ MLB" in text  # a scoreboard that couldn't be read is said, the rest still listed
    taps = buttons(sent["reply_markup"])
    assert ("🎬 Lakers at Celtics", "gg:nba-401") in taps and ("🎬 Avalanche at Maple Leafs", "gg:nhl-501") in taps
    assert not any(data == "gg:nba-402" for _, data in taps)  # still being played: nothing to make yet
    w.fake.clear()
    w.press("gg:nba-401", 1, SAM)
    said = to(w, SAM)[-1]
    assert "Got it: Lakers at Celtics (NBA): a recap reel for Hoops" in said["text"]
    assert "checked before it's handed over (over a minute, upright, with sound)" in said["text"]
    o = w.bot.desk.orders()[0]
    assert (o["by"], o["want"], o["line"], o["games"]) == ("Sam", "reel", "Lakers 108, Celtics 112", 1)
    assert o["requires"]["min_seconds"] == 61.0
    assert (buttons(said["reply_markup"])[-1]) == ("✖ Cancel it", f"so:{o['id']}")
    assert "<b>Sam</b> asked for sports highlights: Lakers at Celtics (NBA): a recap reel." in to(w, JUSTYN)[-1]["text"]
    # the search looks for that game, and its reel is made and reviewed
    assert w.work() == 3
    o = w.bot.desk.orders()[0]
    assert o["status"] == "done" and [s["kind"] for s in o["steps"]] == ["highlights", "reel"]
    assert o["review"] == {"checked": 1, "passed": 1, "held": 0, "why": []} and reels[0]["asked_by"] == "Sam"
    # the Cancel button under the reply stops a request that's still open
    w.fake.clear()
    w.press("gg:nhl-501", 1, SAM)
    oid = next(o["id"] for o in w.bot.desk.orders() if o["status"] == "queued")
    w.press(f"so:{oid}", 1, SAM)
    assert to(w, SAM)[-1]["text"].startswith("✖ Stopped: Avalanche at Maple Leafs (NHL): a recap reel")
    assert w.bot.desk.find_order(oid)["status"] == "cancelled"
    # a game that has left the list says so instead of guessing
    w.bot.slate_get = scoreboards(w.clock[0] + 3 * 86400)
    w.fake.clear()
    w.press("gg:nba-404", 1, SAM)
    assert not to(w, SAM) and len(w.bot.desk.orders()) == 2


def test_games_of_one_league_and_too_many_to_list(sports):
    w = sports
    H = 3600.0
    more = [SL.event(str(600 + i), SL.team(f"A{i}", "", f"Away {i}"), SL.team(f"H{i}", "", f"Home {i}"),
                     w.clock[0] - (6 + i) * H, score=("1", "2"), detail="FT") for i in range(9)]
    w.bot.slate_get = scoreboards(w.clock[0], [("soccer/eng.1", more)])
    sent = w.say("/games")  # 13 games in three leagues: the leagues first
    assert "Pick a league." in sent["text"] and "🏀 NBA: 3 games, 1 being played" in sent["text"]
    taps = buttons(sent["reply_markup"])
    assert ("⚽ Premier League (9)", "gl:eng.1") in taps and ("🏀 NBA (3)", "gl:nba") in taps
    w.fake.clear()
    w.press("gl:nba", 1)
    listed = to(w, JUSTYN)[-1]
    assert listed["text"].startswith("🏆 <b>Games played, last 24 hours</b>: NBA")
    assert [d for _, d in buttons(listed["reply_markup"])] == ["gg:nba-401", "gg:nba-404"]
    sent = w.say("/games NHL")
    assert "Avalanche 3, Maple Leafs 2" in sent["text"] and "Lakers" not in sent["text"]
    sent = w.say("/games UFC")  # not a sport the scoreboards list
    assert "The scoreboards I read list soccer" in sent["text"] and sent.get("reply_markup") is None
    w.bot.slate_get = scoreboards(w.clock[0] + 5 * 86400)
    assert "No NHL games in the last 24 hours" in w.say("/games NHL")["text"]
    assert any(c == "games" for c, _ in botmod.COMMANDS) and "/games" in botmod.help_html()


def test_reel_in_words_and_reel_alone(sports):
    w = sports
    w.bot.slate_get = scoreboards(w.clock[0])
    sent = w.say("/reel NBA Lakers vs Celtics 3 clips")  # /reel means a reel, whatever else is said
    assert "Got it: Lakers vs Celtics (NBA): a recap reel for Hoops" in sent["text"]
    assert w.bot.desk.orders()[0]["want"] == "reel"
    assert w.say("/reel")["text"].startswith("🏆 <b>Games played, last 24 hours</b>")
    sent = w.say("/sports NBA Heat vs Knicks 3 clips")  # single plays: the reply says they'll be held
    assert "ℹ Single plays run 15 to 40 seconds" in sent["text"] and "will wait for your OK" in sent["text"]


def test_the_report_says_what_the_review_found(sports):
    w = sports
    w.say("/sports NBA Lakers vs Celtics 2 clips", SAM)
    w.work()
    items = w.take()
    assert all(i.review == "waiting" for i in items)  # 24-second plays: under the minimum, held for an OK
    assert "Under the minimums for a Telegram request: 20 s (needs over 60 s)" in items[0].warnings
    w.bot.check(w.clock[0])
    text = next(m["text"] for m in to(w, SAM) if "sports request is ready" in m["text"])
    assert "⏸ 2 videos are under the minimums and wait for an OK" in text and "24 s (needs over 60 s)" in text  # as measured


def test_status_says_what_the_sports_worker_is_on(sports):
    w = sports
    w.say("/sports NBA Lakers vs Celtics reel", SAM)
    assert "🏆 1 sports job waiting to start" in w.say("/status")["text"]
    task = w.bot.desk.claim("test:1")
    text = w.say("/status")["text"]
    assert "🏀 Looking for the games of “Lakers vs Celtics (NBA)” (Sam asked)" in text and "Waiting" not in text
    w.bot.desk.finish(task, "queued")
    assert w.worker.step()  # the search: the game is found, its highlights are next
    task = w.bot.desk.claim("test:1")
    w.bot.desk.stage(task["game"], "Downloading the game", 0.4, "analysing")
    text = w.say("/status")["text"]
    assert "🏀 Finding the best plays of “Lakers at Celtics” (Sam asked)\n    Downloading the game · 40%" in text
    assert "1 more sports job waiting" not in text


def test_the_app_lists_the_games_and_orders_one(served, monkeypatch):
    w = served
    w.s.categories = w.s.categories + [{"id": "h1", "name": "Hoops", "sport": "basketball",
                                        "account_id": w.account.id}]
    w.bot.slate_get = scoreboards(w.clock[0])
    d = w.session(SAM)
    assert w.call("GET", "/api/sports/played")[0] == 401
    status, p, _ = w.call("GET", "/api/sports/played", token=d["token"])
    assert status == 200 and p["covered"] is True and len(p["problems"]) == 1
    rows = {g["id"]: g for g in p["games"]}
    assert list(rows)[0] == "nba-402" and list(rows)[-1] == "nba-403"  # being played first, to come last
    assert rows["nba-401"] == {"id": "nba-401", "sport": "basketball", "emoji": "🏀", "league": "NBA",
                               "home": "Celtics", "away": "Lakers", "home_score": 112, "away_score": 108,
                               "state": "final", "label": "Lakers at Celtics", "line": "Lakers 108, Celtics 112",
                               "winner": "home", "start": rows["nba-401"]["start"], "when": "Final", "ready": True}
    assert rows["nba-402"]["ready"] is False and rows["nba-402"]["when"] == "Live: 6:00 - 4th"
    assert [(r["league"], r["games"], r["final"], r["live"]) for r in p["leagues"]] == [("NBA", 3, 2, 1),
                                                                                         ("NHL", 1, 1, 0)]
    status, one, _ = w.call("GET", "/api/sports/played?sport=hockey", token=d["token"])
    assert status == 200 and [g["id"] for g in one["games"]] == ["nhl-501"]
    _, form, _ = w.call("GET", "/api/sports", token=d["token"])
    assert form["want"] == "reel" and form["requires"]["min_seconds"] == 61.0 and "soccer" in form["scoreboards"]
    # one tap: the game's reel
    status, r, _ = w.call("POST", "/api/sports/order", {"game": "nba-401"}, token=d["token"])
    assert status == 200 and r["message"].startswith("Got it: Lakers at Celtics (NBA): a recap reel for Hoops.")
    assert r["order"]["want"] == "reel" and r["order"]["line"] == "Lakers 108, Celtics 112" and r["notes"] == []
    # single plays are allowed, and the answer says they'll wait for an OK
    status, r, _ = w.call("POST", "/api/sports/order", {"game": "nhl-501", "want": "clips", "plays": 3},
                          token=d["token"])
    assert status == 200 and r["order"]["want"] == "clips" and any("wait for your OK" in n for n in r["notes"])
    # a game still being played, one to come, and one that isn't listed
    status, err, _ = w.call("POST", "/api/sports/order", {"game": "nba-402"}, token=d["token"])
    assert status == 409 and "still being played" in err["error"]
    status, err, _ = w.call("POST", "/api/sports/order", {"game": "nba-403"}, token=d["token"])
    assert status == 409 and "hasn't been played yet" in err["error"]
    status, err, _ = w.call("POST", "/api/sports/order", {"game": "nba-999"}, token=d["token"])
    assert status == 404 and "Load the games again" in err["error"]
    assert w.call("POST", "/api/sports/order", {"game": "../../x"}, token=d["token"])[0] == 404
    # the status the app polls says what the sports worker is on
    _, st, _ = w.call("GET", "/api/status", token=d["token"])
    assert st["sports"] is None and st["sports_waiting"] == 2
    task = w.bot.desk.claim("test:1")
    _, st, _ = w.call("GET", "/api/status", token=d["token"])
    assert st["sports"]["what"] == "Looking for the games of" and st["sports"]["by"] == "Sam" and st["sports_waiting"] == 1
    assert "game" not in st["sports"] and "order" not in st["sports"]
    w.bot.desk.finish(task, "queued")
    w.bot.stop()

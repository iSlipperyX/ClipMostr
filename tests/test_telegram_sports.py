"""Asking for sports highlights from Telegram (the chat's /sports and the Mini App's Make → Sports highlights):
the request becomes a sports order in the asker's name, automation's sports worker does it, and both people are
told how it went. Telegram is telegram_fake, the search and the engine are stand-ins, the server is the real one."""

import json
from pathlib import Path

import pytest

from telegram_fake import JUSTYN, buttons
from test_sports_desk import FakeEngine, found
from test_telegram import make_video, world  # noqa: F401 - `world` is a fixture
from test_telegram_app import SAM, link, served, to  # noqa: F401 - `served` is a fixture
from ytclip import ai
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
                         "end": c.end, "qc": {"ok": True}})
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
    ("NBA Lakers vs Celtics", {"league": "NBA", "sport": "basketball", "teams": "Lakers, Celtics", "want": "both",
                               "since": "week"}),
    ("Premier League 2 games, reel only", {"league": "Premier League", "sport": "soccer", "games": 2, "want": "reel",
                                           "since": "week"}),
    ("UFC 3 clips", {"league": "UFC", "sport": "combat", "plays": 3, "want": "clips", "since": "week"}),
    ("lakers at celtics last night", {"teams": "lakers, celtics", "want": "both", "since": "3days"}),
    ("soccer Arsenal v Chelsea highlights and a reel", {"sport": "soccer", "teams": "Arsenal, Chelsea", "want": "both",
                                                        "since": "week"}),
    ("top 5 plays NFL Chiefs, Bills this month", {"league": "NFL", "sport": "football", "plays": 5, "want": "clips",
                                                  "teams": "Chiefs, Bills", "since": "month"}),
    ("f1 recap", {"league": "Formula 1", "sport": "motorsport", "want": "reel", "since": "week"}),
    ("hockey", {"sport": "hockey", "want": "both", "since": "week"}),
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
    for bit in ("api/sports/order", "Sports highlights", "Make these highlights", "/cancel", "copyright claims"):
        assert bit in js
    assert "innerHTML" not in js  # what people type and what YouTube titles say is only ever set as text

"""The event search in the Sports section: the desk's queue and worker, the Mac's API, the page's search bar, the
Research tab's badges and bar, and the command line."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_sports_desk import NOW, FakeEngine, desk, seed, worker  # noqa: F401 - fixtures and helpers
from ytclip import cli
from ytclip.settings import Settings
from ytclip.sportsdesk.desk import DeskError, public
from ytclip.subtitles import Cue

LINES = [(30, "Tip-off and we're under way."), (300, "What a dunk by number 23."),
         (410, "And that's a technical foul on the bench! The coach is furious with the official."),
         (417, "He cannot believe it, and the crowd lets the referee know."),
         (600, "Timeout on the floor.")]


class TalkEngine(FakeEngine):
    """A game with the commentary above."""

    def measure(self, p):
        super().measure(p)
        p.measurements = SimpleNamespace(cues=[Cue(t, t + 6.0, text) for t, text in LINES])


@pytest.fixture
def searcher(worker, desk):  # noqa: F811
    worker.s.event_search_model = "words"
    worker.s.event_feed = "off"
    worker.engine_factory = TalkEngine
    return worker


def test_asking_needs_a_question_a_game_and_a_readable_anchor(desk):  # noqa: F811
    seed(desk, 2)
    with pytest.raises(DeskError, match="Type the moment"):
        desk.find_events("  ")
    with pytest.raises(DeskError, match="Pick a game"):
        desk.find_events("red card")
    with pytest.raises(DeskError, match="isn't an anchor"):
        desk.find_events("red card", ["v0"], anchors=["soon"])
    out = desk.find_events("technical foul on bench", ["v0"], anchors=["2=58:40"])
    assert out["queued"] == 1 and "technical foul on bench" in out["message"]
    assert desk.game("v0")["status"] == "queued" and "Waiting to search" in desk.game("v0")["stage"]
    with pytest.raises(DeskError, match="being worked on"):
        desk.find_events("red card", ["v0"])
    desk.pick(["v1"])
    assert desk.find_events("red card")["queued"] == 1  # with no game named: the picked ones


def test_what_it_finds_joins_the_games_plays(searcher, desk):  # noqa: F811
    seed(desk, 1, highlights=True)
    desk.find_events("Technical foul on bench during game 7", ["v0"])
    assert searcher.step()
    g = desk.game("v0")
    assert g["status"] == "ready" and g["events"]["found"] == 1 and g["events"]["reader"] == "word matching"
    first = g["highlights"][0]
    assert first["origin"] == "event" and first["id"] == "e1" and first["chosen"] and first["play"] == "Technical foul"
    assert first["start"] < 410 < first["end"] and "technical foul" in first["why"].lower()
    assert len([h for h in g["highlights"] if h.get("origin") != "event"]) == 4  # its plays are still there
    assert (desk.media / "v0" / "e1.jpg").is_file() and desk.thumb("v0", "e1").name == "e1.jpg"
    shown = public(g)
    assert shown["events"]["query"] == "Technical foul on bench during game 7"
    assert shown["highlights"][0]["origin"] == "event" and "sources" in shown["highlights"][0]
    assert any("1 moment found" in t for _, t in searcher.news)
    # finding highlights again keeps what the search found
    desk.analyse(["v0"], again=True)
    assert searcher.step()
    ids = [h["id"] for h in desk.game("v0")["highlights"]]
    assert "e1" in ids and "h1" in ids and (desk.media / "v0" / "e1.jpg").is_file()
    # a new search replaces the last one's
    desk.find_events("dunk", ["v0"])
    assert searcher.step()
    found_now = [h for h in desk.game("v0")["highlights"] if h.get("origin") == "event"]
    assert found_now and all(h["play"] == "Dunk" for h in found_now)


def test_nothing_found_is_said_and_the_game_is_left_alone(searcher, desk):  # noqa: F811
    seed(desk, 1)
    desk.find_events("red card", ["v0"])
    assert searcher.step()
    g = desk.game("v0")
    assert g["status"] == "found" and g["events"]["found"] == 0 and "Nothing in this video" in g["events"]["notes"][0]
    assert any("Nothing found" in t for _, t in searcher.news)


def test_a_timeline_file_places_what_nobody_said(searcher, desk, tmp_path):  # noqa: F811
    seed(desk, 1)
    feed = tmp_path / "game.json"
    feed.write_text(json.dumps({"sport": "basketball", "events": [
        {"kind": "ejection", "text": "Draymond Green ejected", "video": "8:20", "player": "Draymond Green"}]}))
    desk.find_events("player ejection", ["v0"], feed=str(feed))
    assert searcher.step()
    g = desk.game("v0")
    hit = g["highlights"][0]
    assert hit["t_event"] == 500.0 and hit["badge"].endswith("D. GREEN") and "feed" in hit["sources"]
    assert g["events"]["feed"] == "your timeline file" and g["timeline"][0]["video"] == 500.0


def test_a_reel_of_the_found_moments(searcher, desk, monkeypatch):  # noqa: F811
    from ytclip.sportsdesk import reel as R

    seed(desk, 1, highlights=True)
    desk.find_events("technical foul", ["v0"])
    searcher.step()
    desk.choose("v0", [h["id"] for h in desk.game("v0")["highlights"] if h.get("origin") != "event"], False)
    out = desk.reel("v0")
    assert "1 found moment" in out["message"]
    got = {}

    def make(p, game, hl, s, root, **kw):
        got.update(kw, highlights=hl)
        reel = R.montage.plan_events(game, kw["events"], duration=800, title=kw["events_title"])
        return R.ReelResult(Path(root), Path(root) / "reel.mp4", reel, {"length": reel.duration, "rewards": False})

    monkeypatch.setattr(R, "make", make)
    assert searcher.step()
    assert [e["id"] for e in got["events"]] == ["e1"] and got["events_title"] == "Technical foul"
    assert got["ids"] is None and all(h.get("origin") != "event" for h in got["highlights"])
    assert any("1 found moment" in t for _, t in searcher.news)
    # with plays ticked too it's the usual recap, of the plays
    desk.choose("v0", ["h1", "h2"], True)
    assert "top" in desk.reel("v0")["message"]


def test_a_video_from_research_becomes_a_game(desk):  # noqa: F811
    out = desk.add_video({"id": "abc123", "title": "Arsenal vs Chelsea | Extended Highlights", "duration": 900,
                          "channel": "NBC Sports", "views": 5000, "published": NOW - 3600})
    g = desk.game(out["id"])
    assert (g["home"], g["away"], g["picked"], g["new"]) == ("Arsenal", "Chelsea", True, False)
    assert desk.add_video({"id": "abc123", "title": "Something else"}) == {"id": "abc123"}
    assert desk.game("abc123")["title"].startswith("Arsenal")
    with pytest.raises(DeskError):
        desk.add_video({"id": "../.."[:0]})


def test_the_mac_asks_the_pc_to_search(world):  # noqa: F811
    from ytclip.mac.client import ApiError

    c, _ = world.linked()
    d = world.host.desk
    seed(d, 2)
    assert "red card" in c.post("/api/sports/games/v0/events", {"query": "red card"})["message"]
    assert d.game("v0")["status"] == "queued"
    d.pick(["v1"])
    assert c.post("/api/sports/events", {"query": "buzzer beater", "ids": ["v1"]})["queued"] == 1
    with pytest.raises(ApiError) as e:
        c.post("/api/sports/events", {"query": "x"})
    assert e.value.status == 400
    (d.media / "v0").mkdir(parents=True, exist_ok=True)
    (d.media / "v0" / "e1.jpg").write_bytes(b"\xff\xd8jpg")
    assert c.fetch("/api/sports/thumbs/v0/e1") == b"\xff\xd8jpg"


# ---------------------------------------------------------------- the page


@pytest.fixture
def page(app, tmp_path):
    from ytclip.gui.sports_view import LocalBackend, SportsView
    from ytclip.sportsdesk.desk import Desk

    d = Desk(tmp_path / "sports.db", settings=lambda: Settings())
    seed(d, 2, highlights=True)
    view = SportsView(LocalBackend(d))
    view.resize(1280, 760)
    view.show()
    view.desk = d
    yield view
    view.shutdown()
    view.close()
    view.deleteLater()
    d.close()


def test_the_header_bar_searches_the_game_being_looked_at(app, page):
    assert wait(app, lambda: page.current == "v0")
    assert "in-game event" in page.event_box.placeholderText()
    page.event_box.setText("x")
    page.event_btn.click()
    assert "Type the moment" in page.banner_text.text()
    page.event_box.setText("red card after a VAR check")
    page.event_btn.click()
    assert wait(app, lambda: page.desk.game("v0")["status"] == "queued")
    task = page.desk.claim("test:1")
    assert task["kind"] == "events" and task["payload"]["query"] == "red card after a VAR check"


def test_found_moments_show_with_the_plays(app, page):
    from ytclip.gui import sports_view as V

    def fn(g):
        g["highlights"].insert(0, {"id": "e1", "start": 60.0, "end": 97.0, "length": 37.0, "score": 91.0, "rank": 0,
                                   "title": "He's sent off!", "play": "Red card", "why": "The commentary names it.",
                                   "chosen": True, "origin": "event", "badge": "59' RED CARD · M. CAICEDO",
                                   "certain": True, "sources": ["feed", "commentary"], "players": []})
        for h in g["highlights"][1:]:
            h["chosen"] = False
        g["events"] = {"query": "red card", "describe": "Red card", "found": 1, "feed": "ESPN", "reader": "words",
                       "notes": []}
        g["index"] = {"score": 72.0, "parts": {"emotion": 0.8}, "reasons": ["Big moments."], "missing": ["momentum"]}

    page.desk.change("v0", fn)
    panel = page.panel
    assert wait(app, lambda: panel.list.count() == 5)
    row = panel.list.itemWidget(panel.list.item(0))
    assert row.rank.text() == "🔎" and "feed + commentary" in row.meta.text() and "M. CAICEDO" in row.meta.text()
    assert "“red card”: 1 moment found" in panel.events_note.text() and "Clip-Ability 72/100" in panel.events_note.text()
    assert "not measured" in panel.events_note.toolTip()
    assert panel.reel_btn.text() == "Make reel of found" and panel.reel_btn.isEnabled()
    assert V.events_text({}) == ""


def test_research_pills_carry_opportunity_badges_and_the_bar_sends_a_video(app, tmp_path):
    from ytclip.gui.research import Research, market_lines
    from ytclip.gui.research_dashboard import CategoryPills
    from ytclip.trending import TrendingVideo

    pills = CategoryPills()
    pills.set_items([("All", None, 3), ("Soccer", "soccer", 2)], None,
                    {"soccer": ("Hot Arbitrage", "2 videos rising with few short clips yet.")})
    assert pills.labels() == ["All  3", "Soccer  2  🔥 Hot Arbitrage"]
    assert "rising" in pills.buttons[1].toolTip()
    v = TrendingVideo("v1", "Arsenal vs Chelsea", "NBC", NOW, 900, 1000)
    v.pace = {"delta": 3.2, "velocity": 6400, "baseline": 2000, "basis": "channel", "peers": 5, "saturation": 0.08,
              "shorts": 1, "why": "Rising."}
    v.badge, v.opportunity = "Hot Arbitrage", 81.0
    text = " ".join(market_lines(v))
    assert "Hot Arbitrage" in text and "3.2x" in text and "7-day median" in text and "1 YouTube Shorts" in text
    assert market_lines(TrendingVideo("v2", "t", "c", NOW, 900, 10)) == []

    s = Settings()
    r = Research(lambda: s, lambda k, val: None)
    got = []
    r.event_search.connect(lambda video, query: got.append((video["id"], query)))
    r.event_box.setText("buzzer beater")
    r._event_search()
    assert got == [] and "Select the video" in r.hint.text()
    r._shown = v
    r._event_search()
    assert got == [("v1", "buzzer beater")]
    r.shutdown()
    r.deleteLater()


# ---------------------------------------------------------------- the command line


def test_ytclip_ai_shows_the_route_the_usage_and_the_hardware(capsys, monkeypatch):
    from ytclip.intelligence import agent_router, usage

    assert cli.main(["ai", "usage"]) == 0
    assert "No AI calls counted yet" in capsys.readouterr().out
    usage.note("claude", "claude-sonnet-5-5", 1000, 500)
    assert cli.main(["ai", "usage", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["rows"][0]["model"] == "claude-sonnet-5-5" and data["rows"][0]["usd"] == pytest.approx(0.007)
    assert cli.main(["ai", "hardware", "--json"]) == 0
    assert "onnx_providers" in json.loads(capsys.readouterr().out)
    assert cli.main(["ai", "route"]) == 1  # nothing set up in a fresh install
    assert "No AI is set up" in capsys.readouterr().out
    monkeypatch.setattr(agent_router, "query_intelligence", lambda *a, **k: agent_router.Intelligence(
        {"answer": "Hello"}, "Hello", "local", "qwen2.5-7b", "local", {"tokens_in": 12, "tokens_out": 3}, [], 0.4))
    assert cli.main(["ai", "ask", "Say hello"]) == 0
    out = capsys.readouterr().out
    assert "Hello" in out and "12 in / 3 out" in out and "free" in out
    with pytest.raises(SystemExit):
        cli.main(["ai", "ask", "x", "--priority", "nonsense"])


def test_ytclip_sports_find(capsys, monkeypatch, tmp_path):
    class Eng(TalkEngine):
        def open(self, p):
            p.video = SimpleNamespace(path=tmp_path / "game.mp4", duration=800.0, title="Lakers at Celtics", id="v0",
                                      channel="NBA")

    monkeypatch.setattr(cli, "Engine", Eng)
    assert cli.main(["sports-find", "technical foul on bench", "game.mp4", "--sport", "basketball", "--words",
                     "--feed", "off", "--no-ai", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["hits"][0]["concept"] == "technical_foul" and data["query"]["concepts"] == ["technical_foul"]
    assert data["hits"][0]["start"] < 410 < data["hits"][0]["end"] and data["reader"] == "word matching"
    assert cli.main(["sports-find", "red card", "game.mp4", "--sport", "basketball", "--words", "--feed", "off",
                     "--no-ai"]) == 1
    assert "Nothing in this video matches" in capsys.readouterr().out
    assert cli.main(["sports-find", "red card", "game.mp4", "--anchor", "later"]) == 2
    with pytest.raises(SystemExit):
        cli.main(["sports-find", "--help"])
    assert "--feed" in capsys.readouterr().out


from test_mac_app import wait  # noqa: E402,F401
from test_remote import mac_home, world  # noqa: E402,F401 - fixtures

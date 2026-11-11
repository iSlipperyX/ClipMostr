"""A sports order (ytclip/sportsdesk/desk.py): one request that looks for the games, finds each one's best plays
and makes clips of them, a recap reel, or both, with nobody picking along the way. The Telegram app asks for
them; here the desk and its worker do one from start to finish with a stand-in search and engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ytclip.sportsdesk import desk as D
from ytclip.sportsdesk import games as G
from ytclip.sportsdesk import montage
from ytclip.sportsdesk.desk import DeskError

from test_sports_desk import FakeEngine, desk, found, seed, worker  # noqa: F401 - fixtures


def results(*extra):
    return [found("a1", "Lakers at Celtics | Extended Highlights", 20, 3),
            found("b2", "Lakers vs Celtics reaction", 20, 2),
            found("c3", "Heat at Knicks | Extended Highlights", 20, 4),
            found("d4", "Lakers at Suns | Extended Highlights", 20, 30), *extra]


@pytest.fixture
def search(monkeypatch):
    asked = []
    monkeypatch.setattr(G, "search_free", lambda q, **k: asked.append(q) or results())
    return asked


@pytest.fixture
def reels(monkeypatch):
    """reel.make, without rendering: a file in the folder it was asked for."""
    from ytclip.sportsdesk import reel as R

    made = []

    def fake_make(p, game, highlights, s, out_root, **k):
        made.append({"game": game["id"], "root": out_root, **k})
        folder = Path(out_root) / f"reel {game['id']}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "01 - reel.mp4").write_bytes(b"mp4")
        (folder / "clips.json").write_text(json.dumps({"clips": [{"rank": 1, "file": "01 - reel.mp4"}]}))
        plan = montage.plan(game, highlights, crowd=None, duration=800)
        plan.story = montage.Story(written_by="")
        return R.ReelResult(folder, folder / "01 - reel.mp4", plan, {"length": 71.4, "rewards": True}, {}, [])

    monkeypatch.setattr(R, "make", fake_make)
    return made


def run(worker, most=12):  # noqa: F811
    n = 0
    while worker.step():
        n += 1
        assert n < most, "the order never finished"
    return n


# ---------------------------------------------------------------- asking


def test_an_order_says_what_it_is_and_keeps_the_pages_filter(desk):  # noqa: F811
    desk.set_filter({"sport": "soccer", "league": "Serie A", "reel_voice": "off", "reel_framing": "fit"})
    r = desk.order({"sport": "basketball", "teams": "Lakers vs Celtics", "want": "both", "plays": 3}, by="Sam",
                   by_id=5002)
    o = r["order"]
    assert o["title"] == "Lakers vs Celtics: the top 3 plays as clips and a recap reel"
    assert r["message"] == "Looking for Lakers vs Celtics: the top 3 plays as clips and a recap reel."
    assert (o["by"], o["by_id"], o["status"], o["sport"], o["emoji"]) == ("Sam", 5002, "queued", "basketball", "🏀")
    kept = desk.find_order(o["id"])["filter"]
    assert kept["teams"] == "Lakers, Celtics" and kept["per_game"] == 3 and kept["since"] == "week"
    assert kept["kinds"] == ["extended", "highlights"]
    assert (kept["reel_voice"], kept["reel_framing"]) == ("off", "fit")  # the reel is made the Sports page's way
    assert desk.filter().sport == "soccer" and desk.filter().league == "Serie A"  # and the page's filter is its own
    assert desk.state()["search"]["status"] == "idle"  # nor is it the page's search
    task = desk.claim("test:1")
    assert task["kind"] == "order" and task["payload"] == {"order": o["id"]}


@pytest.mark.parametrize("values, title", [
    ({"sport": "soccer", "league": "Premier League", "want": "reel", "games": 2},
     "Premier League: a recap reel, of the 2 newest games"),
    ({"sport": "hockey", "want": "clips", "plays": 1}, "Ice hockey: the best play as a clip"),
    ({"teams": "Arsenal at Chelsea", "league": "Premier League", "want": "clips", "plays": 99, "games": 99},
     "Arsenal vs Chelsea (Premier League): the top 7 plays as clips, of the 3 newest games"),
    ({"teams": "Heat, Knicks; Bulls", "want": "reel"}, "Heat, Knicks, Bulls: a recap reel"),
])
def test_what_an_order_is_called(desk, values, title):  # noqa: F811
    assert desk.order(values)["order"]["title"] == title


def test_an_order_that_cant_be_taken_says_why(desk, worker):  # noqa: F811
    for values, why in [({}, "which sport, league or teams"), ({"sport": "curling"}, "which sport"),
                        ({"sport": "soccer", "want": "everything"}, "clips, a reel, or both"),
                        ({"sport": "soccer", "category": "nope"}, "category isn't there"), ("x", "couldn't be read")]:
        with pytest.raises(DeskError, match=why):
            desk.order(values)
    assert desk.orders() == []
    # the one category for the sport is used without asking; another sport's isn't
    assert desk.order({"sport": "basketball"})["order"]["category"] == "Hoops"
    assert desk.order({"sport": "soccer"})["order"]["category"] == ""
    for _ in range(D.MAX_OPEN_ORDERS - 2):
        desk.order({"sport": "golf"})
    with pytest.raises(DeskError, match="waiting or under way already") as exc:
        desk.order({"sport": "golf"})
    assert exc.value.status == 409


# ---------------------------------------------------------------- doing


def test_an_order_is_done_from_the_search_to_the_clips_and_the_reel(desk, worker, search, reels):  # noqa: F811
    oid = desk.order({"sport": "basketball", "teams": "Lakers vs Celtics", "want": "both", "plays": 3},
                     by="Sam", by_id=5002)["id"]
    assert worker.step()  # the search: the game with both teams is picked, not the reaction video or another game
    o = desk.orders()[0]
    assert search == ["Lakers extended highlights", "Celtics extended highlights"]
    assert o["status"] == "working" and o["doing"] == "Waiting its turn on the PC"
    assert [(st["game"], st["kind"], st["status"]) for st in o["steps"]] == [
        ("a1", "highlights", "queued"), ("a1", "clips", "queued"), ("a1", "reel", "queued")]
    assert desk.game("a1")["picked"] and desk.game("a1")["status"] == "queued"
    assert desk.state()["search"]["status"] == "idle" and "added" not in desk.state()["search"]
    assert worker.step()  # its highlights
    g = desk.game("a1")
    assert len(g["highlights"]) == 5 and [h["chosen"] for h in g["highlights"]] == [True] * 3 + [False] * 2
    assert g["status"] == "queued" and g["stage"] == "Waiting to make 3 clips and the reel"
    assert worker.step()  # its clips
    g = desk.game("a1")
    assert g["status"] == "queued" and g["stage"] == "Waiting to make the reel"  # not free to be asked of meanwhile
    with pytest.raises(DeskError, match="being worked on already"):
        desk.reel("a1")
    assert run(worker) == 1  # then its reel
    o = desk.orders()[0]
    assert o["status"] == "done" and o["error"] == "" and o["finished"]
    assert o["message"] == ("Made 3 clips from Lakers at Celtics, for Hoops (all held for your OK: 24 s (needs over "
                            "60 s)); Made a 71 s reel of the top 5 plays of Lakers at Celtics, for Hoops")
    clips, reel = o["steps"][1], o["steps"][2]
    # a request from Telegram is checked against the minimums: the single plays fall short, the reel passes
    assert clips["review"] == {"checked": 3, "passed": 0, "held": 3, "why": ["24 s (needs over 60 s)"]}
    assert reel["review"] == {"checked": 1, "passed": 1, "held": 0, "why": [], "passed_as": "71 s"}
    assert o["review"] == {"checked": 4, "passed": 1, "held": 3, "why": ["24 s (needs over 60 s)"]}
    assert o["requires"]["min_seconds"] == 61.0
    assert clips["status"] == "done" and clips["clips"] == 3 and "Highlights" in Path(clips["folder"]).parts
    assert reel["status"] == "done" and reel["file"].endswith("01 - reel.mp4") and Path(reel["folder"]).parent.name == "Reels"
    p = next(e.exported for e in FakeEngine.made[::-1] if hasattr(e, "exported"))
    assert [c.start for c in p.clips] == [100.0, 200.0, 300.0]  # its three best, in rank order
    auto = p.meta["automation"]
    assert auto["permission"] == "Asked for by Sam from Telegram" and auto["origins"] == ["sports", "telegram"]
    assert auto["category_id"] == "h1"
    assert auto["requires"]["min_seconds"] == 61.0  # written into clips.json, so posting holds what falls short
    assert reels[0]["category"].name == "Hoops" and reels[0]["voice"] == "en_storyteller"
    assert reels[0]["requires"]["min_seconds"] == 61.0 and reels[0]["asked_by"] == "Sam"
    assert any("Sports request done (Lakers vs Celtics" in t for _, t in worker.news)
    assert desk.game("a1")["status"] == "ready"
    assert not worker.step()


def test_several_games_newest_first_and_only_whats_asked(desk, worker, search, reels):  # noqa: F811
    oid = desk.order({"sport": "basketball", "since": "week", "want": "reel", "games": 2})["id"]
    run(worker)
    o = desk.find_order(oid)
    assert o["status"] == "done"
    assert [(st["game"], st["kind"], st["status"]) for st in o["steps"]] == [
        ("a1", "highlights", "done"), ("a1", "reel", "done"), ("c3", "highlights", "done"), ("c3", "reel", "done")]
    assert [m["game"] for m in reels] == ["a1", "c3"]
    assert not any(hasattr(e, "exported") for e in FakeEngine.made[-4:])  # no clips were asked for


def test_an_orders_search_goes_before_a_long_game_and_isnt_done_twice(desk, worker, search):  # noqa: F811
    seed(desk, 1)
    desk.analyse(["v0"])  # a game picked on the PC, waiting
    oid = desk.order({"sport": "basketball", "teams": "Heat", "want": "clips"})["id"]
    task = desk.claim("dead:1")
    assert task["kind"] == "order"  # the search first: it takes seconds, and says at once when nothing matched
    worker.run(task | {"owner": "dead:1"})
    steps = desk.find_order(oid)["steps"]
    assert [st["kind"] for st in steps] == ["highlights", "clips"]
    # that worker died before it could say so, and another takes the task over: the games aren't picked again
    with desk._tx() as con:
        con.execute("UPDATE tasks SET status='working', lease=0 WHERE kind='order'")
    again = desk.claim("test:1")
    assert again["kind"] == "order" and worker.do_order(again) == ""
    assert desk.find_order(oid)["steps"] == steps
    with desk.lock:
        n = desk.con.execute("SELECT count(*) AS n FROM tasks WHERE kind='highlights' AND game='c3'").fetchone()["n"]
    assert n == 1


def test_no_game_matched(desk, worker, search):  # noqa: F811
    oid = desk.order({"sport": "basketball", "teams": "Bulls", "want": "clips"})["id"]
    assert worker.step() and not worker.step()
    o = desk.orders()[0]
    assert o["id"] == oid and o["status"] == "failed" and o["steps"] == []
    assert o["error"] == "No game matched (4 videos looked at: 3 another team, 1 not a game)."
    assert any("couldn't be done" in t for _, t in worker.news)


def test_a_search_that_fails_fails_the_order_not_the_pages_search(desk, worker, monkeypatch):  # noqa: F811
    monkeypatch.setattr(G, "search_free", lambda q, **k: (_ for _ in ()).throw(RuntimeError("no internet")))
    oid = desk.order({"sport": "basketball", "want": "clips"})["id"]
    worker.step()
    o = desk.find_order(oid)
    assert o["status"] == "failed" and "YouTube couldn't be searched" in o["error"] and "no internet" in o["error"]
    assert "error" not in desk.state()["search"]


def test_a_game_that_fails_leaves_the_others(desk, worker, search, reels, monkeypatch):  # noqa: F811
    oid = desk.order({"sport": "basketball", "want": "both", "games": 2, "plays": 2})["id"]
    worker.step()
    real_open = FakeEngine.open
    calls = []

    def flaky(self, p):
        calls.append(p.source if hasattr(p, "source") else 1)
        if len(calls) == 1:
            raise RuntimeError("Video unavailable")
        real_open(self, p)

    monkeypatch.setattr(FakeEngine, "open", flaky)
    run(worker)
    o = desk.find_order(oid)
    assert [(st["game"], st["kind"], st["status"]) for st in o["steps"]] == [
        ("a1", "highlights", "failed"), ("a1", "clips", "skipped"), ("a1", "reel", "skipped"),
        ("c3", "highlights", "done"), ("c3", "clips", "done"), ("c3", "reel", "done")]
    assert o["status"] == "done" and o["error"] == "Lakers at Celtics: Video unavailable"
    assert o["message"].startswith("Made 2 clips from Heat at Knicks")
    assert desk.game("a1")["status"] == "failed"


def test_every_game_failing_fails_the_order(desk, worker, search, monkeypatch):  # noqa: F811
    monkeypatch.setattr(FakeEngine, "open", lambda self, p: (_ for _ in ()).throw(RuntimeError("Video unavailable")))
    oid = desk.order({"sport": "basketball", "teams": "Lakers, Celtics", "want": "clips"})["id"]
    run(worker)
    o = desk.find_order(oid)
    assert o["status"] == "failed" and o["error"] == "Video unavailable"


def test_a_game_looked_at_already_isnt_looked_at_again_or_made_twice(desk, worker, search, reels):  # noqa: F811
    first = desk.order({"sport": "basketball", "teams": "Lakers vs Celtics", "want": "clips", "plays": 2})["id"]
    run(worker)
    assert desk.find_order(first)["status"] == "done"
    again = desk.order({"sport": "basketball", "teams": "Lakers vs Celtics", "want": "both", "plays": 2})["id"]
    assert worker.step()
    o = desk.find_order(again)
    assert [(st["kind"], st["status"]) for st in o["steps"]] == [("clips", "queued"), ("reel", "queued")]
    run(worker)
    p = next(e.exported for e in FakeEngine.made[::-1] if hasattr(e, "exported"))
    assert [c.start for c in p.clips] == [300.0, 400.0]  # the next two: the first two were made already
    third = desk.order({"sport": "basketball", "teams": "Lakers vs Celtics", "want": "reel"})["id"]
    run(worker)
    o = desk.find_order(third)
    assert o["status"] == "done" and o["message"] == "Nothing new to make: It has a recap reel already."
    assert len(reels) == 1


def test_cancelling_an_order(desk, worker, search):  # noqa: F811
    oid = desk.order({"sport": "basketball", "teams": "Lakers vs Celtics", "want": "both"})["id"]
    worker.step()
    r = desk.cancel_order(oid)
    assert r["order"]["status"] == "cancelled" and "Stopped" in r["message"]
    assert desk.game("a1")["status"] == "found" and not worker.step()
    with pytest.raises(DeskError, match="finished already"):
        desk.cancel_order(oid)
    with pytest.raises(DeskError) as exc:
        desk.cancel_order("nope")
    assert exc.value.status == 404
    # cancelled while a task of it is under way: the task stops, and the order stays cancelled
    oid = desk.order({"sport": "basketball", "teams": "Heat", "want": "clips"})["id"]
    worker.step()
    task = desk.claim("test:1")
    assert task["kind"] == "highlights" and task["game"] == "c3"
    desk.cancel_order(oid)
    assert desk.renew(task) == "cancel"
    assert desk.order_step(oid, "c3", "highlights", "done") is None
    assert desk.find_order(oid)["status"] == "cancelled"


def test_a_game_taken_off_the_list_closes_its_order(desk, worker, search):  # noqa: F811
    oid = desk.order({"sport": "basketball", "teams": "Lakers vs Celtics", "want": "clips"})["id"]
    worker.step()
    desk.remove(["a1"])  # on the PC's Sports page
    assert desk.settle_orders() == []  # not yet: a step may be between two tasks
    desk.clock["t"] += D.ORDER_ORPHAN + 1
    closed = desk.settle_orders()
    assert [o["id"] for o in closed] == [oid]
    o = desk.find_order(oid)
    assert o["status"] == "failed" and "taken off the list on the PC" in o["error"]
    assert desk.settle_orders() == []


def test_stopping_automation_leaves_the_order_to_carry_on(desk, worker, search, monkeypatch):  # noqa: F811
    oid = desk.order({"sport": "basketball", "teams": "Lakers vs Celtics", "want": "clips", "plays": 2})["id"]
    worker.step()
    real = FakeEngine.measure

    def stop_once(self, p):
        monkeypatch.setattr(FakeEngine, "measure", real)
        worker._stop.set()
        self.progress("Measuring audio", 0.5)

    monkeypatch.setattr(FakeEngine, "measure", stop_once)
    worker.step()
    o = desk.orders()[0]
    assert o["status"] == "working" and "starts again" in desk.find_order(oid)["stage"]
    worker._stop.clear()
    run(worker)
    assert desk.find_order(oid)["status"] == "done"


def test_the_order_shows_what_the_pc_is_doing(desk, worker, search):  # noqa: F811
    oid = desk.order({"sport": "basketball", "teams": "Lakers vs Celtics", "want": "clips"})["id"]
    assert desk.orders()[0]["doing"] == "Waiting to look for the games"
    worker.step()
    desk.stage("a1", "Downloading", 0.43, "analysing")
    o = desk.orders()[0]
    assert o["id"] == oid and o["doing"] == "Lakers at Celtics: Downloading" and o["frac"] == 0.43

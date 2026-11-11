"""The minimums a video made for a Telegram request must meet (ytclip/compliance/requirements.py): over a minute,
upright 9:16, with sound. What falls short is kept and held for an OK, at the desk and again at posting's intake."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ytclip.compliance import requirements as R
from ytclip.ffmpeg import MediaInfo
from ytclip.posting import clipsjson
from ytclip.posting.store import Store
from ytclip.settings import Settings
from ytclip.sportsdesk import desk as D
from ytclip.sportsdesk import montage, slate

from test_sports_desk import FakeEngine, desk, found, plays, worker  # noqa: F401 - fixtures
from test_sports_orders import reels, run, search  # noqa: F401 - fixtures

NEED = R.rules(Settings())


def test_the_rules_come_from_settings():
    assert NEED == {"min_seconds": 61.0, "max_seconds": 600.0, "min_width": 720, "min_height": 1280, "by": "telegram"}
    assert R.rules(Settings(telegram_min_seconds=0)) == {}  # off
    assert R.rules(Settings(telegram_min_seconds=30))["min_seconds"] == 30.0
    assert R.rules(None)["min_seconds"] == 61.0
    assert R.over(61) == "over 60 s" and R.over(30.5) == "30.5 s or more"


@pytest.mark.parametrize("measured, ok, why", [
    (dict(length=72.4, width=1080, height=1920, sound=True, lufs=-14.1), True, ""),
    (dict(length=26.0, width=1080, height=1920, sound=True), False, "26 s (needs over 60 s)"),
    (dict(length=19.0), False, "19 s (needs over 60 s)"),
    (dict(length=60.4, width=1080, height=1920, sound=True), False, "60.4 s (needs over 60 s)"),
    (dict(length=61.0, width=720, height=1280, sound=True), True, ""),
    (dict(length=70.0, width=1920, height=1080, sound=True), False, "1920 x 1080 (needs upright 9:16)"),
    (dict(length=70.0, width=540, height=960, sound=True), False, "540 x 960 (needs at least 720 x 1280)"),
    (dict(length=70.0, width=1080, height=1920, sound=False), False, "no sound track (needs sound)"),
    (dict(length=70.0, width=1080, height=1920, sound=True, lufs=-70.0), False, "silent (needs sound)"),
    (dict(length=700.0, width=1080, height=1920, sound=True), False, "700 s (needs 10 minutes at most)"),
    (dict(length=0.0), False, "it can't be read (needs a video that plays)"),
    (dict(length=None), False, "it can't be read (needs a video that plays)"),
    (dict(length=70.0, width=1080, height=1920, sound=True, fps=15.0), False, "15 frames a second (needs 24 or more)"),
    (dict(length=20.0, width=1920, height=1080, sound=False), False,
     "20 s (needs over 60 s); 1920 x 1080 (needs upright 9:16); no sound track (needs sound)"),
])
def test_a_video_against_the_minimums(measured, ok, why):
    r = R.review(NEED, **measured)
    assert (r.ok, r.why()) == (ok, why)
    assert r.to_json()["ok"] is ok


def test_what_passed_is_said_too_and_no_rules_means_no_check():
    r = R.review(NEED, length=72.4, width=1080, height=1920, sound=True)
    assert r.passed() == "72 s, 1080 x 1920, with sound"
    assert [c.id for c in r.checks] == ["length", "shape", "sound"]
    assert R.review({}, length=3.0).ok and R.review({}, length=3.0).checks == []


def test_a_file_is_measured(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    r = R.review_file(f, NEED, probe=lambda p: MediaInfo(duration=66.0, width=1080, height=1920, has_audio=True),
                      loudness=lambda p: -14.0)
    assert r.ok and r.name == "clip.mp4"
    assert not R.review_file(f, NEED, probe=lambda p: 1 / 0).ok  # a file that can't be opened doesn't play
    assert R.review_file(f, {}, probe=lambda p: 1 / 0).ok


def test_posting_holds_what_falls_short(tmp_path):
    folder = tmp_path / "Automation" / "Hoops" / "Highlights" / "game"
    folder.mkdir(parents=True)
    for name in ("01 - dunk.mp4", "02 - block.mp4", "03 - reel.mp4"):
        (folder / name).write_bytes(b"mp4")
    (folder / "clips.json").write_text(json.dumps({
        "video": {"id": "a1", "title": "Lakers at Celtics"},
        "automation": {"permission": "Asked for by Sam from Telegram", "requires": NEED, "category": "Hoops"},
        "clips": [{"rank": 1, "file": "01 - dunk.mp4", "title": "Dunk", "length": 19.0, "qc": {"ok": True}},
                  {"rank": 2, "file": "02 - block.mp4", "title": "Block", "length": 26.0,
                   "requirements": {"ok": True}},  # the desk's own check wins when it's there
                  {"rank": 3, "file": "03 - reel.mp4", "title": "Reel",
                   "qc": {"length": 71.2, "width": 1080, "height": 1920, "sound": True, "lufs": -14.0}}]}))
    index = clipsjson.read_folder_index(folder)
    assert index["01 - dunk.mp4"]["hold"] == "Under the minimums for a Telegram request: 19 s (needs over 60 s)"
    assert index["02 - block.mp4"]["hold"] == "" and index["03 - reel.mp4"]["hold"] == ""
    store = Store(tmp_path / "lib.json")
    items = {i.title: i for i in store.add_paths([tmp_path / "Automation"], watched=True)}
    assert items["Dunk"].review == "waiting"
    assert items["Dunk"].warnings == ["Under the minimums for a Telegram request: 19 s (needs over 60 s)"]
    assert items["Block"].review == "" and items["Reel"].review == ""
    # a folder made without minimums (the PC's own Sports tab) holds nothing
    plain = tmp_path / "Automation" / "plain"
    plain.mkdir()
    (plain / "01 - x.mp4").write_bytes(b"mp4")
    (plain / "clips.json").write_text(json.dumps({"clips": [{"rank": 1, "file": "01 - x.mp4", "length": 12.0}]}))
    assert clipsjson.read_folder_index(plain)["01 - x.mp4"]["hold"] == ""
    assert R.hold_reason({"min_seconds": 61}, {"requirements": {"ok": False}}) == \
        "Under the minimums for a Telegram request: it doesn't meet the minimums"


# ---------------------------------------------------------------- the desk: a request is reviewed, before and after


def test_a_request_is_told_what_it_will_get(desk, worker):  # noqa: F811
    r = desk.order({"deliver": "post", "sport": "basketball", "teams": "Lakers vs Celtics", "want": "both"}, by="Sam", by_id=5002)
    assert r["notes"] == [
        "Single plays run 15 to 40 seconds, and a video from Telegram has to be over 60 s: the clips will wait for "
        "your OK, not post by themselves. The reel is the one that's over a minute.",
        "No AI is set up, so the plays are ranked by the crowd's noise alone."]
    assert r["order"]["notes"] == r["notes"] and r["order"]["requires"]["min_seconds"] == 61.0
    r = desk.order({"deliver": "post", "sport": "basketball", "want": "reel"}, by="Sam", by_id=5002)
    assert r["notes"] == ["No AI is set up, so the plays are ranked by the crowd's noise alone."]
    r = desk.order({"deliver": "post", "sport": "soccer", "want": "clips"}, by="Sam", by_id=5002)
    assert "Ask for a reel to get one that's over a minute." in r["notes"][0]
    assert r["notes"][1] == ("No category is set for this sport: what's made goes in the Sports folder and isn't "
                             "queued to post.")
    for o in desk.orders():
        desk.cancel_order(o["id"])
    worker.s.telegram_min_seconds = 0  # the check turned off: nothing is said or held
    r = desk.order({"deliver": "post", "sport": "soccer", "want": "clips"}, by="Sam", by_id=5002)
    assert r["order"]["requires"] == {} and not any("Single plays" in n for n in r["notes"])
    # the PC's own requests aren't Telegram's
    assert desk.order({"sport": "hockey", "want": "clips"})["order"]["requires"] == {}
    # asked for from Telegram without "post it": a download, which has nothing to meet
    r = desk.order({"sport": "hockey", "want": "reel"}, by="Sam", by_id=5002)
    assert r["order"]["requires"] == {} and r["order"]["deliver"] == "download"
    assert r["notes"][0] == ("It's made to download: it isn't queued to post and no TikTok account is tied to it. "
                             "You can still queue it to post afterwards.")


def test_a_category_without_an_account_is_said(desk, worker):  # noqa: F811
    worker.s.categories = [{"id": "h1", "name": "Hoops", "sport": "basketball"}]
    r = desk.order({"deliver": "post", "sport": "basketball", "want": "reel"}, by="Sam", by_id=5002)
    assert "Hoops has no TikTok account: what's made stays in the Library until it has one." in r["notes"]


def test_a_reel_under_a_minute_is_held_and_said(desk, worker, search, monkeypatch):  # noqa: F811
    from ytclip.sportsdesk import reel as RR

    def short_reel(p, game, highlights, s, out_root, **k):
        folder = Path(out_root) / "reel"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "01 - reel.mp4").write_bytes(b"mp4")
        plan = montage.plan(game, highlights, crowd=None, duration=800)
        plan.story = montage.Story(written_by="")
        return RR.ReelResult(folder, folder / "01 - reel.mp4", plan,
                             {"length": 52.3, "width": 1080, "height": 1920, "sound": True, "rewards": False}, {}, [])

    monkeypatch.setattr(RR, "make", short_reel)
    desk.order({"deliver": "post", "sport": "basketball", "teams": "Lakers vs Celtics", "want": "reel"}, by="Sam", by_id=5002)
    run(worker)
    o = desk.orders()[0]
    assert o["status"] == "done"
    assert o["message"] == ("Made a 52 s reel of the top 5 plays of Lakers at Celtics, for Hoops (held for your OK: 52 s "
                            "(needs over 60 s))")
    assert o["review"] == {"checked": 1, "passed": 0, "held": 1, "why": ["52 s (needs over 60 s)"]}


def test_a_reel_that_meets_them_says_so(desk, worker, search, reels):  # noqa: F811
    desk.order({"deliver": "post", "sport": "basketball", "teams": "Lakers vs Celtics", "want": "reel"}, by="Sam", by_id=5002)
    run(worker)
    o = desk.orders()[0]
    assert o["message"] == "Made a 71 s reel of the top 5 plays of Lakers at Celtics, for Hoops"
    assert o["review"] == {"checked": 1, "passed": 1, "held": 0, "why": []}
    assert o["steps"][-1]["review"]["passed_as"] == "71 s"


def test_three_plays_still_make_a_reel_over_a_minute():
    hl = [{"id": f"h{i}", "start": 100.0 * i, "end": 100.0 * i + 22, "rank": i, "score": 90 - i, "play": "dunk",
           "title": f"Play {i}"} for i in (1, 2, 3)]
    reel = montage.plan({"matchup": "A at B"}, hl, crowd=None, duration=800)
    assert len(reel.plays) == 3 and reel.eligible and 61 < reel.duration <= montage.REEL_MAX
    assert max(seg.length for seg in reel.plays) <= montage.PLAY_LONG + 0.01
    two = montage.plan({"matchup": "A at B"}, hl[:2], crowd=None, duration=800)
    assert not two.eligible and "under a minute" in two.notes[0]  # two plays aren't a recap: said, and then held
    many = montage.plan({"matchup": "A at B"}, [dict(h, id=f"x{i}", start=60.0 * i, end=60.0 * i + 20, rank=i)
                                                 for i, h in enumerate(hl * 2, 1)], crowd=None, duration=800)
    assert max(seg.length for seg in many.plays) <= montage.PLAY_STRETCH + 0.01  # enough plays: none is stretched


# ---------------------------------------------------------------- a game picked from the games played


GAME = {"id": "nba-401", "sport": "basketball", "league": "NBA", "home": "Celtics", "away": "Lakers",
        "home_names": ["Celtics", "Boston Celtics", "Boston"], "away_names": ["Lakers", "Los Angeles Lakers"],
        "state": "final", "label": "Lakers at Celtics", "line": "Lakers 108, Celtics 112"}


def test_a_picked_game_becomes_a_request_for_its_reel(desk, worker, search, reels):  # noqa: F811
    r = desk.order(slate.order_values(GAME, "reel") | {"deliver": "post"}, by="Sam", by_id=5002)
    o = r["order"]
    assert o["title"] == "Lakers at Celtics (NBA): a recap reel" and o["line"] == "Lakers 108, Celtics 112"
    kept = desk.find_order(o["id"])
    assert kept["filter"]["teams"] == "Lakers, Celtics" and kept["filter"]["since"] == "3days"
    assert kept["match"]["sides"] == [["Lakers", "Los Angeles Lakers"], ["Celtics", "Boston Celtics", "Boston"]]
    run(worker)
    o = desk.orders()[0]
    assert o["status"] == "done" and [st["game"] for st in o["steps"]] == ["a1", "a1"]  # the game with both of them
    assert search == ["Lakers NBA extended highlights", "Celtics NBA extended highlights"]


def test_a_game_is_matched_by_any_name_its_teams_go_by(desk, worker, monkeypatch, reels):  # noqa: F811
    from ytclip.sportsdesk import games as G

    monkeypatch.setattr(G, "search_free", lambda q, **k: [
        found("z9", "Liverpool v Everton | Extended Highlights", 20, 5, channel="Premier League"),
        found("m1", "Manchester City v Liverpool | Extended Highlights", 20, 4, channel="Premier League")])
    game = {"id": "eng.1-601", "sport": "soccer", "league": "Premier League", "home": "Man City", "away": "Liverpool",
            "home_names": ["Man City", "Manchester City"], "away_names": ["Liverpool"], "state": "final",
            "label": "Man City v Liverpool", "line": "Man City 2-1 Liverpool"}
    desk.order(slate.order_values(game, "reel") | {"deliver": "post"}, by="Sam", by_id=5002)
    run(worker)
    o = desk.orders()[0]
    assert o["status"] == "done" and o["steps"][0]["game"] == "m1"  # "Manchester City" is "Man City"


def test_a_game_still_being_played_cant_be_asked_for_yet(desk):  # noqa: F811
    with pytest.raises(D.DeskError, match="still being played") as exc:
        desk.order(slate.order_values(dict(GAME, state="live"), "reel") | {"deliver": "post"}, by="Sam", by_id=5002)
    assert exc.value.status == 409
    with pytest.raises(D.DeskError, match="hasn't been played yet"):
        desk.order(slate.order_values(dict(GAME, state="soon"), "reel"))
    assert desk.orders() == []


@pytest.mark.parametrize("sport", ["soccer", "basketball", "football", "baseball", "hockey", "tennis", "combat",
                                   "golf", "cricket", "motorsport"])
def test_every_sport_can_be_asked_for_as_a_reel(desk, sport):  # noqa: F811
    o = desk.order({"deliver": "post", "sport": sport, "want": "reel"}, by="Sam", by_id=5002)["order"]
    assert o["status"] == "queued" and o["want"] == "reel" and o["sport"] == sport
    assert o["title"].endswith(": a recap reel")


# ---------------------------------------------------------------- what the PC is doing right now


def test_what_is_being_worked_on_in_words(desk, worker, search, reels):  # noqa: F811
    assert desk.doing() is None
    desk.order({"deliver": "post", "sport": "basketball", "teams": "Lakers vs Celtics", "want": "reel"}, by="Sam", by_id=5002)
    assert desk.doing() is None  # waiting isn't doing
    task = desk.claim("test:1")
    d = desk.doing()
    assert d["kind"] == "order" and d["by"] == "Sam" and d["emoji"] == "🏀"
    assert d["text"] == "Looking for the games of Lakers vs Celtics"  # (not what it said while it queued)
    desk.finish(task, "queued")
    assert worker.step()  # the search
    task = desk.claim("test:1")
    assert task["kind"] == "highlights"
    desk.stage("a1", "Downloading", 0.42, "analysing")
    d = desk.doing()
    assert d["text"] == "Finding the best plays of Lakers at Celtics: Downloading (42%)"
    assert (d["what"], d["title"], d["stage"], d["frac"], d["game"]) == (
        "Finding the best plays of", "Lakers at Celtics", "Downloading", 0.42, "a1")
    assert d["waiting"] == 0 and d["order"]
    desk.clock["t"] += D.LEASE + 1  # its worker went quiet: nothing is being done
    assert desk.doing() is None

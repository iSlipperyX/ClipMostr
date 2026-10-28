"""Standing requests: automation runs your saved requests every few hours and picks the best new matches."""

import pytest
from conftest import NOW, api_item
from test_pipeline import Clock, _runner, _scan_result, _settings

from ytclip import lookup, niches
from ytclip.trending import ResearchBlocked, video_from_api

GAMING = {"id": "game01", "name": "Gaming", "daily": 2, "finds": "MW4 with good comedy\n# not this\n\nmw4 WITH good comedy"}
COOKING = {"id": "cook01", "name": "Cooking", "daily": 2, "finds": "easy pasta recipes"}


def test_requests_are_read_one_per_line():
    assert niches.parse_finds("a\n# note\n\n  b   c \nA") == ["a", "b c"]
    s = _settings(niches=[GAMING, COOKING])
    assert niches.standing_requests(s) == [("game01", "MW4 with good comedy"), ("cook01", "easy pasta recipes")]
    s = _settings(niches=[GAMING | {"enabled": False}, COOKING])
    assert niches.standing_requests(s) == [("cook01", "easy pasta recipes")]  # a switched-off niche's don't run
    assert niches.standing_requests(_settings(pick_finds="funny fails\n")) == [("", "funny fails")]


class FakeFind:
    """lookup.find: answers each request with videos and their matches; records the calls."""

    def __init__(self, monkeypatch, answers=None):
        self.calls = []
        self.answers = answers or {}
        self.fail = None
        monkeypatch.setattr(lookup, "find", self)

    def __call__(self, request, s, key, since="any", min_minutes=None, language="", log=None, progress=None,
                 cancelled=None, use_ai=None, **kw):
        self.calls.append({"request": request, "since": since, "use_ai": use_ai})
        if self.fail:
            raise self.fail
        videos = []
        for i, (vid, match) in enumerate(self.answers.get(request, [])):
            v = video_from_api(api_item(vid, title=f"Found {vid}", minutes=25, hours_old=48, channel=f"F{vid}"))
            v.origins, v.match, v.match_why, v.score = ["request"], match, "test", 70
            videos.append(v)
        plan = lookup.Plan(request, [request])
        return lookup.LookupResult(request, videos, NOW, plan, quota=201, weak=1, ai_calls=2 if use_ai else 0,
                                   rated_by="claude-test" if use_ai else "")


def _empty_scan(opts, history, log=None, progress=None, cancelled=None, now=None):
    return _scan_result()


def test_good_matches_are_queued_for_the_requests_niche(tmp_path, monkeypatch):
    find = FakeFind(monkeypatch, {"MW4 with good comedy": [("m1", 90), ("m2", 75), ("m3", 40)]})
    s = _settings(niches=[GAMING])
    r = _runner(tmp_path, s, scan=_empty_scan)
    assert r.research(s) == 2
    jobs = {j.id: j for j in r.db.jobs()}
    assert set(jobs) == {"m1", "m2"}  # m3 doesn't match well enough
    assert jobs["m1"].niche == "game01" and jobs["m1"].permission == "Found for your request (90 match)"
    assert find.calls == [{"request": "MW4 with good comedy", "since": "month", "use_ai": False}]
    assert any("Standing request “MW4 with good comedy” for Gaming: 2 good matches of 4 found (201 quota units)"
               in m for m in r.logs)


def test_a_request_runs_every_few_hours_and_never_picks_a_video_twice(tmp_path, monkeypatch):
    find = FakeFind(monkeypatch, {"MW4 with good comedy": [("m1", 90)]})
    clock = Clock()
    s = _settings(niches=[GAMING], pick_find_hours=6)
    r = _runner(tmp_path, s, scan=_empty_scan, clock=clock)
    assert r.research(s) == 1
    clock.t += 3600
    assert r.research(s) == 0 and len(find.calls) == 1  # not due yet
    clock.t += 6 * 3600
    find.answers["MW4 with good comedy"] = [("m1", 90), ("m4", 80)]
    assert r.research(s) == 1 and len(find.calls) == 2  # due again: only the new one
    assert {j.id for j in r.db.jobs()} == {"m1", "m4"}


def test_the_niches_videos_a_day_still_count(tmp_path, monkeypatch):
    FakeFind(monkeypatch, {"MW4 with good comedy": [(f"m{i}", 90 - i) for i in range(5)]})
    s = _settings(niches=[GAMING | {"daily": 2}])
    r = _runner(tmp_path, s, scan=_empty_scan)
    assert r.research(s) == 2
    assert any("good matches not picked: the niche's videos a day are reached (3)" in m for m in r.logs)


def test_find_videos_now_runs_one_niches_requests(tmp_path, monkeypatch):
    find = FakeFind(monkeypatch, {"easy pasta recipes": [("p1", 88)]})
    s = _settings(niches=[GAMING, COOKING])
    r = _runner(tmp_path, s, scan=_empty_scan)
    r.research(s)
    find.calls.clear()
    r.db.set_meta("scan_now", "cook01")
    r.research(s)
    assert [c["request"] for c in find.calls] == ["easy pasta recipes"]  # due or not, and only that niche's


def test_claude_rates_them_within_ai_calls_per_day(tmp_path, monkeypatch):
    find = FakeFind(monkeypatch, {"MW4 with good comedy": [("m1", 90)]})
    s = _settings(niches=[GAMING], ai_api_key="sk-ant-x", auto_ai_daily=3)
    r = _runner(tmp_path, s, scan=_empty_scan)
    r.research(s)
    assert find.calls[-1]["use_ai"] is True and r.db.count("ai", NOW - 60) == 2
    r.db.set_meta("scan_now", "1")
    r.research(s)
    assert find.calls[-1]["use_ai"] is False  # 2 used, 2 more would pass the 3 a day


def test_a_blocked_key_stops_the_rest_and_a_failure_waits_its_turn(tmp_path, monkeypatch):
    find = FakeFind(monkeypatch)
    find.fail = ResearchBlocked("Today's YouTube API quota is used up.")
    s = _settings(niches=[GAMING, COOKING])
    r = _runner(tmp_path, s, scan=_empty_scan)
    assert r.research(s) == 0 and len(find.calls) == 1
    assert any("Standing requests stopped: Today's YouTube API quota" in m for m in r.logs)
    find.fail = ValueError("odd answer")
    r.db.set_meta("scan_now", "1")
    r.research(s)
    assert len(find.calls) == 3 and any("Standing request “easy pasta recipes”: odd answer" in m for m in r.logs)


def test_without_niches_the_requests_in_picking_run(tmp_path, monkeypatch):
    FakeFind(monkeypatch, {"funny fails": [("f1", 95)]})
    s = _settings(pick_finds="funny fails", pick_from="watchlist")
    r = _runner(tmp_path, s, scan=_empty_scan)
    assert r.research(s) == 1  # found for your request counts as permission, like a link you add
    assert r.db.jobs()[0].permission == "Found for your request (95 match)"


def test_creative_commons_only_still_means_only(tmp_path, monkeypatch):
    FakeFind(monkeypatch, {"funny fails": [("f1", 95)]})
    s = _settings(pick_finds="funny fails", pick_from="cc")
    r = _runner(tmp_path, s, scan=_empty_scan)
    assert r.research(s) == 0
    assert any("your picking rules allow Creative Commons only" in m for m in r.logs)


def test_a_video_on_the_charts_too_goes_to_the_requests_niche(tmp_path, monkeypatch):
    FakeFind(monkeypatch, {"MW4 with good comedy": [("a", 90)]})
    s = _settings(niches=[GAMING], pick_from="watchlist")

    def scan(opts, history, log=None, progress=None, cancelled=None, now=None):
        return _scan_result("a")

    r = _runner(tmp_path, s, scan=scan)
    assert r.research(s) == 1
    job = r.db.jobs()[0]
    assert job.niche == "game01" and "request" in job.origins


@pytest.mark.parametrize("key", ["", "K"])
def test_nothing_runs_without_requests_or_a_key(tmp_path, monkeypatch, key):
    find = FakeFind(monkeypatch)
    s = _settings(youtube_api_key=key, niches=[GAMING | {"finds": ""}] if key else [GAMING])
    r = _runner(tmp_path, s, scan=_empty_scan)
    r.standing(s, r.hist)
    assert find.calls == []

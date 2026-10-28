"""Fresh recommendations (fresh.py): running a request again finds new videos instead of the same ones,
videos already used are never recommended again, and Research checks more than one chart."""

import json
import urllib.parse

import pytest
from conftest import NOW, api_item
from test_research import FakeDeep, fake_fetch
from test_research import opts as scan_opts
from test_sources import FakeAPI

from ytclip import fresh, lookup, sources
from ytclip.jobs import Job, JobDB
from ytclip.policy import Decision, PickLog
from ytclip.research import ScanOptions, niche_charts, scan
from ytclip.settings import Settings
from ytclip.trending import ChartUnavailable, History, video_from_api

DAY = 86400

# ---------------------------------------------------------------- whose turn


def test_runs_take_turns_with_the_angles_then_the_sort_orders():
    angles = ["a", "b", "c", "d"]
    turns = [fresh.turn_for(angles, n) for n in range(7)]
    assert [(t.queries, t.order) for t in turns] == [
        (["a", "b"], "relevance"), (["c", "d"], "relevance"), (["a", "b"], "date"), (["c", "d"], "date"),
        (["a", "b"], "viewCount"), (["c", "d"], "viewCount"), (["a", "b"], "relevance")]
    one = [fresh.turn_for(["mw4"], n) for n in range(3)]  # one angle: still something different each run
    assert [(t.queries, t.order) for t in one] == [(["mw4"], "relevance"), (["mw4"], "date"), (["mw4"], "viewCount")]
    odd = [fresh.turn_for(["a", "b", "c"], n).queries for n in range(3)]
    assert odd == [["a", "b"], ["c", "a"], ["a", "b"]]


def test_a_request_without_ai_still_has_other_angles():
    plan = lookup.plain_plan("a Call of Duty MW4 video with good comedy")
    assert plan.queries == ["Call of Duty MW4 funny moments", "Call of Duty MW4"]  # the first run, as before
    assert plan.angles[:2] == plan.queries and "Call of Duty MW4 funniest moments" in plan.angles
    assert len(plan.angles) <= lookup.MAX_SEARCHES and len({a.lower() for a in plan.angles}) == len(plan.angles)
    assert "MrBeast challenge highlights" in lookup.plain_plan("MrBeast challenge videos").angles
    assert "fail compilation" in lookup.plain_plan("funny fails").angles


# ---------------------------------------------------------------- memory


def _video(vid, match=80.0, why="w"):
    v = video_from_api(api_item(vid))
    v.match, v.match_why = match, why
    return v


def test_memory_remembers_turns_shown_videos_and_survives_a_restart(tmp_path):
    path = tmp_path / "fresh.json"
    m = fresh.Memory(path)
    key = fresh.request_key("MW4  Funny", "game01")
    assert key == fresh.request_key("mw4 funny", "game01") != fresh.request_key("mw4 funny")
    assert m.next_turn(key, ["a", "b", "c"]).number == 0 and m.next_turn(key, ["a", "b", "c"]).number == 1
    m.record(key, [_video("x", 90, "great"), _video("y", 20)], NOW)
    m.save()
    again = fresh.Memory(path)
    assert again.shown(key, NOW + 60) == {"x": (NOW, 90.0, "great"), "y": (NOW, 20.0, "w")}
    assert again.next_turn(key, ["a", "b", "c"]).number == 2
    assert again.shown(key, NOW + (fresh.SHOWN_DAYS + 1) * DAY) == {}  # a month later it's not a repeat any more
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    assert fresh.Memory(tmp_path / "bad.json").requests == {}


def test_saving_keeps_what_the_other_process_saved(tmp_path):
    path = tmp_path / "fresh.json"
    app, runner = fresh.Memory(path), fresh.Memory(path)
    app.record("|a", [_video("x")], NOW)
    app.save()
    runner.record("|b", [_video("y")], NOW + 5)
    runner.save()  # the runner never saw "|a"; it must not drop it
    back = fresh.Memory(path)
    assert set(back.requests) == {"|a", "|b"}
    app.record("|b", [_video("z")], NOW + 10)  # newer run of the same request: both runs' videos kept
    app.save()
    assert set(fresh.Memory(path).shown("|b", NOW + 20)) == {"y", "z"}


def test_an_ai_plan_is_reused_for_a_week_then_planned_again(tmp_path):
    m = fresh.Memory(tmp_path / "fresh.json")
    m.store_plan("|r", {"angles": ["a", "b"], "by_ai": True}, NOW)
    assert m.plan("|r", NOW + 6 * DAY)["angles"] == ["a", "b"]
    assert m.plan("|r", NOW + 8 * DAY) is None
    m.next_turn("|r", ["a", "b"])
    m.store_plan("|r", {"angles": ["c", "d"], "by_ai": True}, NOW + 8 * DAY)
    assert m.tried("|r") == ["a", "b", "c", "d"] and m.get("|r")["turn"] == 0  # new angles start from the first
    m.store_plan("|w", {"angles": ["x"], "by_ai": False}, NOW)
    assert m.plan("|w", NOW + 99 * DAY)["angles"] == ["x"]  # a plan from the words never goes stale


# ---------------------------------------------------------------- what's been used


def test_used_ids_come_from_picks_the_queue_and_the_studio(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    db.add(Job(id="queued", url="u"))
    db.add(Job(id="vid~order1", url="u"))  # a request's own copy of a video
    picks = PickLog(tmp_path / "picks.json")
    picks.record(Decision(video_from_api(api_item("picked")), True, "ok"), "auto", NOW)
    history = History(None)
    history.mark_sent("sent")
    assert fresh.used_ids(db, picks, history) == {"queued", "vid", "picked", "sent"}
    assert fresh.used_ids() == set()
    db.close()


# ---------------------------------------------------------------- a request run again


class PagedYouTube:
    """search.list with pages and sort orders, and videos.list; records what was asked."""

    def __init__(self, pages, items):
        self.pages = pages  # (query, order) -> [[ids on page 1], [page 2], ...]
        self.items = {i["id"]: i for i in items}
        self.asked = []

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        endpoint = parsed.path.rsplit("/", 1)[-1]
        self.asked.append((endpoint, q))
        if endpoint == "search":
            pages = self.pages.get((q["q"], q["order"]), [[]])
            n = int(q.get("pageToken", "p0")[1:])
            out = {"items": [{"id": {"videoId": i}} for i in pages[n]]}
            if n + 1 < len(pages):
                out["nextPageToken"] = f"p{n + 1}"
            return out
        return {"items": [self.items[i] for i in q["id"].split(",") if i in self.items]}

    def searches(self):
        return [(q["q"], q["order"], q.get("pageToken", "")) for e, q in self.asked if e == "search"]


def _items(ids, title="MW4 funny moments"):
    return [api_item(i, title=f"{title} {i}", minutes=20) for i in ids]


def test_running_a_request_again_searches_differently_and_puts_new_videos_first(tmp_path):
    first = [f"a{i}" for i in range(12)]
    second = [f"b{i}" for i in range(12)]
    yt = PagedYouTube({("MW4 funny moments", "relevance"): [first], ("MW4 funny moments", "date"): [first[:2] + second]},
                      _items(first + second))
    memory = fresh.Memory(tmp_path / "fresh.json")
    r1 = lookup.find("MW4 funny", Settings(), "KEY", get=yt, now=NOW, memory=memory)
    assert all(v.new for v in r1.videos) and len(r1.fresh) == 12
    assert yt.searches() == [("MW4 funny moments", "relevance", ""), ("MW4", "relevance", "")]
    yt.asked.clear()
    r2 = lookup.find("MW4 funny", Settings(), "KEY", get=yt, now=NOW + 3600, memory=memory)
    searched = yt.searches()
    assert searched[0][0] != "MW4 funny moments" or searched[0][1] != "relevance"  # not the same search again
    plan = lookup.plain_plan("MW4 funny")
    assert [q for q, _, _ in searched] == fresh.turn_for(plan.angles, 1).queries
    r3 = lookup.find("MW4 funny", Settings(), "KEY", get=yt, now=NOW + 7200, memory=memory)
    turn = fresh.turn_for(plan.angles, 2)
    assert r3.plan.queries == turn.queries and r3.plan.order == turn.order and r2.plan.order == "relevance"


def test_repeats_keep_their_match_go_below_new_ones_and_used_ones_are_left_out(tmp_path, monkeypatch):
    rated = []

    def ask(prompt, system, schema, s, check=None, max_tokens=0):
        if "queries" in schema["properties"]:
            return {"queries": ["q one", "q two"], "subject": ["mw4"], "vibe": ["funny"], "about": "x"}, "ai"
        lines = [ln for ln in prompt.splitlines() if ln[:1].isdigit()]
        rated.extend(ln.split('"')[1] for ln in lines)
        return {"videos": [{"n": int(ln.split(".")[0]), "match": 8, "why": "ai says"} for ln in lines]}, "ai"

    monkeypatch.setattr(lookup, "_ask", ask)
    yt = PagedYouTube({("q one", "relevance"): [["old", "used"]], ("q two", "relevance"): [["old"]],
                       ("q one", "date"): [["old", "new", "used"]], ("q two", "date"): [["new"]]},
                      _items(["old", "new", "used"]))
    memory = fresh.Memory(tmp_path / "fresh.json")
    s = Settings(ai_api_key="sk-ant-x")
    lookup.find("MW4 funny", s, "KEY", get=yt, now=NOW, memory=memory, skip={"used"})
    assert rated == ["MW4 funny moments old"]  # the used video was never rated
    rated.clear()
    r = lookup.find("MW4 funny", s, "KEY", get=yt, now=NOW + 60, memory=memory, skip={"used"})
    assert rated == ["MW4 funny moments new"]  # only the video it hadn't seen: no AI call for the repeat
    assert r.ai_calls == 1  # the plan was reused, so just the one rating call
    assert [v.id for v in r.videos] == ["new", "old"] and [v.new for v in r.videos] == [True, False]
    assert r.videos[1].match == 80 and r.videos[1].match_why == "ai says"
    assert lookup.rank(r.videos[0]) - lookup.rank(r.videos[1]) == pytest.approx(lookup.REPEAT, abs=0.2)
    assert r.skipped == 1 and "used" not in [v.id for v in r.videos]
    videos_asked = [q["id"] for e, q in yt.asked if e == "videos"]
    assert "used" not in ",".join(videos_asked)  # not even its details: that's quota too


def test_a_page_of_repeats_is_followed_by_the_next_page(tmp_path):
    seen = [f"s{i}" for i in range(20)]
    fresh_ids = [f"n{i}" for i in range(lookup.DIG_BELOW)]
    yt = PagedYouTube({("MW4", "relevance"): [seen, fresh_ids, ["never"]]}, _items(seen + fresh_ids + ["never"]))
    r = lookup.find("MW4", Settings(), "KEY", get=yt, now=NOW, skip=set(seen))
    assert yt.searches() == [("MW4", "relevance", ""), ("MW4", "relevance", "p1"),  # one page deeper, no more
                             ("MW4 highlights", "relevance", "")]
    assert {v.id for v in r.videos} == set(fresh_ids) and r.pages == 3 and r.skipped == 20
    assert r.quota == 3 * lookup.SEARCH_COST + 1
    yt.asked.clear()
    lookup.find("MW4", Settings(), "KEY", get=yt, now=NOW)  # nothing to skip: the first page is plenty new
    assert yt.searches() == [("MW4", "relevance", ""), ("MW4 highlights", "relevance", "")]


def test_the_ai_writes_new_angles_once_its_plan_is_a_week_old(tmp_path, monkeypatch):
    prompts = []

    def ask(prompt, system, schema, s, check=None, max_tokens=0):
        if "queries" in schema["properties"]:
            prompts.append(prompt)
            n = len(prompts)
            return {"queries": [f"angle {n}a", f"angle {n}b", f"angle {n}c"], "subject": ["mw4"], "vibe": [],
                    "about": "x"}, "ai"
        return {"videos": []}, "ai"

    monkeypatch.setattr(lookup, "_ask", ask)
    memory = fresh.Memory(tmp_path / "fresh.json")
    s = Settings(ai_api_key="sk-ant-x")
    yt = PagedYouTube({}, [])
    r = lookup.find("MW4", s, "KEY", get=yt, now=NOW, memory=memory)
    assert r.plan.angles == ["angle 1a", "angle 1b", "angle 1c"] and r.plan.queries == ["angle 1a", "angle 1b"]
    r = lookup.find("MW4", s, "KEY", get=yt, now=NOW + 3 * DAY, memory=memory)
    assert len(prompts) == 1 and r.plan.queries == ["angle 1c", "angle 1a"]
    r = lookup.find("MW4", s, "KEY", get=yt, now=NOW + 8 * DAY, memory=memory)
    assert len(prompts) == 2 and "angle 1a" in prompts[1] and "different" in prompts[1]
    assert r.plan.queries == ["angle 2a", "angle 2b"]


def test_a_results_new_videos_and_settings_survive_being_remembered(tmp_path):
    yt = PagedYouTube({("MW4", "relevance"): [["a1"]]}, _items(["a1"]))
    r = lookup.find("MW4", Settings(), "KEY", get=yt, now=NOW, memory=fresh.Memory(None), skip={"zzz"})
    lookup.remember(r, tmp_path / "finds.json")
    back = lookup.last_result(tmp_path / "finds.json")
    assert back.plan.order == "relevance" and back.plan.angles == r.plan.angles and back.videos[0].new


# ---------------------------------------------------------------- automation


def test_a_video_already_in_the_queue_isnt_picked_and_doesnt_use_up_a_pick(tmp_path, monkeypatch):
    from test_pipeline import _runner, _settings
    from test_standing import GAMING, FakeFind, _empty_scan

    FakeFind(monkeypatch, {"MW4 with good comedy": [("m1", 90), ("m2", 85)]})
    s = _settings(niches=[GAMING | {"daily": 1}])
    r = _runner(tmp_path, s, scan=_empty_scan)
    r.db.add(Job(id="m1", url="https://www.youtube.com/watch?v=m1", how="manual"), NOW)  # you queued it by hand
    assert r.research(s) == 1
    assert {j.id for j in r.db.jobs()} == {"m1", "m2"} and r.picks_log.get("m1") is None


def test_standing_requests_remember_their_turns_and_skip_used_videos(tmp_path, monkeypatch):
    from test_pipeline import _runner, _settings
    from test_standing import GAMING, FakeFind, _empty_scan

    find = FakeFind(monkeypatch, {"MW4 with good comedy": [("m1", 90)]})
    seen = []
    real = find.__call__

    def spy(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(lookup, "find", spy)
    s = _settings(niches=[GAMING])
    r = _runner(tmp_path, s, scan=_empty_scan)
    r.db.add(Job(id="old1", url="u"), NOW)
    r.research(s)
    kw = seen[0]
    assert kw["request_key"] == fresh.request_key("MW4 with good comedy", "game01")
    assert isinstance(kw["memory"], fresh.Memory) and {"old1"} <= kw["skip"]


# ---------------------------------------------------------------- Creative Commons searches and charts


def test_creative_commons_searches_take_turns_with_sort_orders_and_keep_what_they_found():
    items = [api_item(i, license="creativeCommon", hours_old=10) for i in ("c1", "c2", "c3")]
    api = FakeAPI(videos=items, search={"gaming highlights": ["c1", "c2"]})
    history = History(None)
    sources.gather("KEY", [], ["gaming highlights"], history, NOW, get=api)
    api.search["gaming highlights"] = ["c3"]
    logs = []
    g = sources.gather("KEY", [], ["gaming highlights"], history, NOW + sources.SEARCH_EVERY + 1, get=api,
                       log=logs.append)
    orders = [q["order"] for e, q in api.calls if e == "search"]
    assert orders == ["viewCount", "date"]
    assert {v.id for v in g.videos} == {"c1", "c2", "c3"}  # the earlier finds stay candidates
    assert any("newest first (1 new" in m for m in logs)


def test_niches_categories_get_their_own_charts():
    s = Settings().apply({"niches": [{"id": "g", "name": "Gaming", "categories": "20, 24"},
                                     {"id": "c", "name": "Comedy", "categories": "23, 24"},
                                     {"id": "x", "name": "Off", "categories": "17", "enabled": False}]})
    assert niche_charts(s) == ["24", "20", "23"]  # the most used first; switched-off niches don't count
    assert ScanOptions.from_settings(s).niche_charts == ["24", "20", "23"]
    assert ScanOptions.from_settings(s.apply({"research_niche_charts": False})).niche_charts == []

    asked = []
    base = fake_fetch()

    def fetch(key, region, category, limit):
        asked.append((region, category))
        if category == "23":
            raise ChartUnavailable("no comedy chart here")
        if category:
            v = video_from_api(api_item(f"{region}-{category}", category=category, minutes=30))
            v.regions[region] = 1
            return [v, *base(key, region, "", limit)[:1]]
        return base(key, region, category, limit)

    o = scan_opts(niche_charts=["20", "23"], deep_count=0)
    assert o.charts == ["", "20", "23"] and o.quota == 2 * 3
    result = scan(o, History(None), fetch=fetch, deep=FakeDeep(), now=NOW)
    assert asked == [("US", ""), ("US", "20"), ("US", "23"), ("GB", ""), ("GB", "20"), ("GB", "23")]
    ids = {v.id for v in result.videos}
    assert {"US-20", "GB-20", "shared", "us1", "gb1"} <= ids
    assert not result.problems  # a category with no chart in a region is nothing to report
    shared = next(v for v in result.videos if v.id == "shared")
    assert shared.regions == {"US": 1, "GB": 2}  # its place on the main chart, not the category chart
    assert scan_opts(category="20", niche_charts=["24"]).charts == ["20"]  # a chosen category: just that one


def test_fresh_json_is_in_the_app_folder():
    assert fresh.memory_path().name == "fresh.json"
    m = fresh.load()
    m.record("|x", [_video("a")], NOW)
    m.save()
    assert "a" in json.loads(fresh.memory_path().read_text(encoding="utf-8"))["requests"]["|x"]["shown"]

import sqlite3

import pytest
from conftest import NOW
from test_pipeline import GAMING, Clock, FakeEngine, _runner, _scan_result, _settings, fresh_engine  # noqa: F401

from ytclip import niches, orders
from ytclip.jobs import Job, JobDB
from ytclip.pipeline import settings_for


# ---------------------------------------------------------------- the request itself


@pytest.mark.parametrize("text,want", [
    ("30-45", (30, 45)), ("30 to 45", (30, 45)), ("30–45s", (30, 45)), ("45 - 30", (30, 45)),
    ("45", (27, 45)), ("60 seconds", (36, 60)), ("4-12", (5, 12)), ("40-41", (38, 41)),
])
def test_lengths_read_the_way_people_write_them(text, want):
    assert orders.parse_length(text) == want


@pytest.mark.parametrize("text", ["", "long", "1-2-3", "700-900", "2-3"])
def test_unreadable_lengths_say_how_to_write_them(text):
    with pytest.raises(ValueError):
        orders.parse_length(text)


def test_a_request_is_checked_and_its_links_tidied():
    o = orders.make(10, 30, 45, niche="game01", links=[" https://youtu.be/AAAAAAAAAAA ",
                                                       "https://www.youtube.com/watch?v=AAAAAAAAAAA", ""],
                    notes="  only   the funniest  bits ", now=NOW)
    assert o.links == ["https://youtu.be/AAAAAAAAAAA"]  # the same video twice is one link
    assert o.notes == "only the funniest bits" and o.status == "open" and o.remaining == 10
    assert o.title("Gaming") == "10 clips of 30–45 s for Gaming from 1 link"
    for bad in (dict(count=0), dict(count=500), dict(min_len=50, max_len=40), dict(links=["not a link"])):
        args = dict(count=5, min_len=20, max_len=40) | bad
        with pytest.raises(ValueError):
            orders.make(args.pop("count"), args.pop("min_len"), args.pop("max_len"), **args)


def test_a_hand_edited_request_is_read_safely():
    assert orders.Order.from_json({"id": "x", "count": "7", "min_len": 1, "max_len": 9000, "links": "u",
                                   "status": "odd"}).to_json() | {} == orders.Order(
        id="x", count=7, min_len=5, max_len=600, links=[], status="open").to_json()
    assert orders.Order.from_json({"id": "x", "count": "lots"}) is None
    assert orders.Order.from_json({"count": 3}) is None and orders.Order.from_json([1]) is None


def test_clips_are_shared_out_between_videos():
    links = orders.Order(id="a", count=10, links=["u1", "u2", "u3"])
    assert orders.clips_for_video(links, per_video=4, links_left=3) == 4  # 10 over 3 links
    links.made = 9
    assert orders.clips_for_video(links, per_video=4, links_left=1) == 1
    picks = orders.Order(id="b", count=10)
    assert orders.clips_for_video(picks, per_video=4, links_left=0) == 4
    assert orders.videos_needed(picks, per_video=4, waiting=1) == 2
    picks.made = 10
    assert orders.clips_for_video(picks, 4, 0) == 0 and orders.videos_needed(picks, 4, 0) == 0


def test_requests_are_kept_in_jobs_db(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    o = orders.make(5, 20, 40, now=NOW)
    db.add_order(o)
    assert db.get_order(o.id) == o
    db.update_order(o.id, NOW + 5, made=3, message="3 of 5")
    again = JobDB(tmp_path / "jobs.db")  # another process (the app) sees it
    assert again.get_order(o.id).made == 3 and again.orders(open_only=True)[0].message == "3 of 5"
    assert db.update_order("nope", made=1) is None
    db.remove_order(o.id)
    assert db.orders() == []


def test_a_jobs_db_from_before_requests_gains_the_column(tmp_path):
    path = tmp_path / "jobs.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, url TEXT, status TEXT, created REAL, next_try REAL, "
                "niche TEXT)")
    con.execute("INSERT INTO jobs VALUES ('old', 'u', 'queued', 1, 0, '')")
    con.commit()
    con.close()
    db = JobDB(path)
    assert db.get("old").order == "" and db.next_due(NOW).id == "old"
    db.add(Job("new", "u2", order="r1"), NOW)
    assert db.next_due(NOW).id == "new"  # a request's video goes first
    assert [j.id for j in db.order_jobs("r1")] == ["new"]


def test_paused_niches_videos_wait(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    db.add(Job("a", "u", niche="game01"), NOW - 10)
    db.add(Job("b", "u", niche="pod"), NOW)
    assert db.next_due(NOW).id == "a"
    assert db.next_due(NOW, {"game01"}).id == "b"
    assert db.next_due(NOW, {"game01", "pod"}) is None


# ---------------------------------------------------------------- niches


def test_a_niche_can_set_its_own_count_and_length(tmp_path):
    s = _settings(niches=[GAMING | {"clips_per_video": 3, "min_len": 25, "max_len": 40}],
                  auto_output_dir=str(tmp_path / "out"))
    game, _ = settings_for(Job("a", "u", niche="game01"), s)
    assert (game.clip_count, game.min_len, game.max_len) == (3, 25, 40)
    n = niches.Niche.from_json(GAMING | {"clips_per_video": "x", "min_len": 50, "max_len": 30})
    assert n.clips_per_video == 0 and (n.min_len, n.max_len) == (30, 50)


def test_a_niches_one_length_never_passes_the_styles_other(tmp_path):
    s = _settings(niches=[GAMING | {"min_len": 80}], auto_output_dir=str(tmp_path / "out"), max_len=60)
    game, _ = settings_for(Job("a", "u", niche="game01"), s)
    assert (game.min_len, game.max_len) == (80, 80)  # the niche's shortest wins over the style's longest
    s = _settings(niches=[GAMING | {"max_len": 15}], auto_output_dir=str(tmp_path / "out"), min_len=20)
    game, _ = settings_for(Job("a", "u", niche="game01"), s)
    assert (game.min_len, game.max_len) == (15, 15)


def test_a_paused_niche_picks_nothing_and_holds_its_queue(tmp_path):
    history = __import__("ytclip.trending", fromlist=["History"]).History(None)
    history.sources["channels"]["@c0"] = {"id": "UCC0"}
    s = _settings(niches=[GAMING | {"paused": True}])
    assert niches.active(s) == [] and niches.paused_ids(s) == {"game01"}
    r = _runner(tmp_path, s, scan=lambda *a, **k: _scan_result("a"), history=history)
    assert r.research(s) == 0
    r.db.add(Job("q", "https://www.youtube.com/watch?v=q", niche="game01"), NOW)
    r.db.set_meta("last_scan", NOW)
    r.tick()
    assert r.db.get("q").status == "queued" and "paused" in r.db.meta("state")


def test_special_requests_make_the_ai_pick_when_there_is_a_key(tmp_path, monkeypatch):
    s = _settings(niches=[GAMING], ai_api_key="sk-test")
    game, label = settings_for(Job("a", "u", niche="game01"), s)
    assert game.mode == "ai" and "AI picks, for the special requests" in label
    no_notes, _ = settings_for(Job("a", "u", niche="game01"), _settings(niches=[GAMING | {"instructions": ""}],
                                                                       ai_api_key="sk-test"))
    assert no_notes.mode != "ai"
    no_key, _ = settings_for(Job("a", "u", niche="game01"), _settings(niches=[GAMING]))
    assert no_key.mode != "ai"


# ---------------------------------------------------------------- automation fills requests


def _url(vid):
    return f"https://www.youtube.com/watch?v={vid:_<11}"


def test_a_request_with_links_is_clipped_to_its_count_and_length(tmp_path):
    s = _settings(auto_output_dir=str(tmp_path / "out"), ai_api_key="sk-test", auto_ai_daily=50)
    r = _runner(tmp_path, s)
    o = r.place(orders.make(5, 30, 45, links=[_url("v1"), _url("v2")], notes="only the funny bits"))
    assert o.message == "2 videos waiting"
    r.db.set_meta("last_scan", NOW)
    FakeEngine.plan = {_url("v1"): 3, _url("v2"): 2}
    r.tick()
    first = FakeEngine.made[-1].s
    assert (first.clip_count, first.min_len, first.max_len) == (3, 30, 45)  # 5 shared over 2 links
    assert first.mode == "ai" and first.ai_instructions.endswith("Special requests for these clips: only the funny bits")
    assert first.output_path.parent == tmp_path / "out" / "Requests"
    meta = FakeEngine.made[-1].exported.meta["automation"]["request"]
    assert meta == {"id": o.id, "count": 5, "min_len": 30, "max_len": 45, "notes": "only the funny bits"}
    assert r.db.get_order(o.id).made == 3 and r.db.get_order(o.id).is_open
    r.tick()
    assert FakeEngine.made[-1].s.clip_count == 2  # what's left
    done = r.db.get_order(o.id)
    assert done.status == "done" and done.made == 5 and done.message == "5 clips ready"


def test_a_request_whose_videos_run_dry_finishes_short(tmp_path):
    r = _runner(tmp_path, _settings())
    o = r.place(orders.make(6, 20, 40, links=[_url("v1")]))
    r.db.set_meta("last_scan", NOW)
    FakeEngine.plan = {_url("v1"): 2}
    r.tick()
    short = r.db.get_order(o.id)
    assert short.status == "short" and short.made == 2 and "Made 2 of 6" in short.message


def test_a_video_clipped_before_gets_its_own_copy_for_the_request(tmp_path):
    r = _runner(tmp_path, _settings(auto_output_dir=str(tmp_path / "out")))
    r.db.add(Job("v1_________", _url("v1"), status="done", clips=4), NOW - 100)
    o = r.place(orders.make(2, 20, 40, links=[_url("v1")]))
    copy = r.db.get(orders.job_id("v1_________", o.id))
    assert copy.status == "queued" and copy.order == o.id and copy.video_id == "v1_________"
    assert r.db.get("v1_________").clips == 4  # the earlier clips are left alone


def test_cancelling_a_request_empties_its_queue(tmp_path):
    r = _runner(tmp_path, _settings())
    o = r.place(orders.make(4, 20, 40, links=[_url("v1"), _url("v2")]))
    assert r.cancel(o.id) == 2
    assert r.db.order_jobs(o.id) == [] and r.db.get_order(o.id).status == "cancelled"


def test_a_job_whose_request_was_cancelled_while_it_waited_is_dropped(tmp_path):
    r = _runner(tmp_path, _settings())
    o = r.place(orders.make(4, 20, 40, links=[_url("v1")]))
    r.db.update_order(o.id, status="cancelled")
    r.db.set_meta("last_scan", NOW)
    r.tick()
    assert r.db.order_jobs(o.id) == [] and not FakeEngine.made
    assert any("its request is cancelled" in line for line in r.logs)


def test_a_request_that_picks_gets_videos_beyond_the_daily_limit(tmp_path):
    s = _settings(pick_daily=1, clip_count=4)
    r = _runner(tmp_path, s, scan=lambda *a, **k: _scan_result("a", "b", "c", "d"))
    o = r.place(orders.make(8, 20, 40))
    assert r.db.meta("scan_now") == "1"  # it asks for a check now
    assert r.research(s) == 3  # the usual 1 a day, plus 2 videos for 8 clips at 4 each
    tagged = [j for j in r.db.jobs() if j.order == o.id]
    assert len(tagged) == 2 and r.db.meta("scan_now") == ""
    assert sum(1 for line in r.logs if "(for a request)" in line) == 2
    # with those 2 waiting, the next check picks no more for it
    assert r.videos_wanted(s) == {}


def test_find_videos_now_for_one_niche_checks_only_its_sources(tmp_path):
    seen = {}

    def scan(opts, history, log, **kw):
        seen["searches"] = list(opts.searches)
        return _scan_result("a")

    pod = {"id": "pod01", "name": "Podcasts", "searches": "podcast clips"}
    s = _settings(niches=[GAMING | {"searches": "gaming clips"}, pod])
    r = _runner(tmp_path, s, scan=scan)
    r.db.set_meta("scan_now", "pod01")
    r.db.set_meta("last_scan", NOW)  # not due by the clock
    r.tick()
    assert seen["searches"] == ["podcast clips"]
    assert any("Finding videos now for Podcasts" in line for line in r.logs)


# ---------------------------------------------------------------- found in review (2.4.0)


def test_once_ends_when_only_a_paused_niches_videos_wait(tmp_path):
    r = _runner(tmp_path, _settings(youtube_api_key="", disk_min_free_gb=0,
                                    niches=[{"id": "g", "name": "G", "searches": "x", "paused": True}]))
    r.db.add(Job("aaaaaaaaaaa", "https://youtu.be/aaaaaaaaaaa", niche="g"), 0)
    ticks = []
    real = r.tick
    r.tick = lambda: ticks.append(1) or (real() if len(ticks) < 20 else pytest.fail("never ended"))
    r.run(once=True, sleep=lambda x: None)
    assert len(ticks) == 1 and r.db.meta("state") == "Stopped"


def test_the_runner_never_waits_less_than_a_second(tmp_path):
    r = _runner(tmp_path, _settings(youtube_api_key="", auto_gap_minutes=60, disk_min_free_gb=0))
    r.db.add(Job("aaaaaaaaaaa", "https://youtu.be/aaaaaaaaaaa"), 0)
    r.db.set_meta("last_download", r.now())
    r.db.set_meta("scan_now", "1")  # "Find videos now" with no key to find them with
    assert r.tick() >= 60


def test_a_requests_own_picks_dont_use_up_its_daily_room(tmp_path):
    s = _settings(pick_daily=1, clip_count=4)
    r = _runner(tmp_path, s, scan=lambda *a, **k: _scan_result("a", "b", "c", "d", "e", "f"))
    o = r.place(orders.make(8, 20, 40))
    r.research(s)
    for j in r.db.order_jobs(o.id):
        r.db.update(j.id, status="done", clips=1)  # each made one clip: it needs another video
    r.order_progress(o.id)
    r.research(s)
    assert len(r.db.order_jobs(o.id)) == 3


def test_a_request_for_no_niche_takes_videos_when_niches_exist(tmp_path):
    s = _settings(pick_daily=1, clip_count=4, niches=[GAMING])
    r = _runner(tmp_path, s, scan=lambda *a, **k: _scan_result("a", "b", "c", "d"))
    o = r.place(orders.make(8, 20, 40))
    assert r.research(s) == 2 and len(r.db.order_jobs(o.id)) == 2


def test_a_request_whose_videos_were_removed_finishes(tmp_path):
    r = _runner(tmp_path, _settings(youtube_api_key="", disk_min_free_gb=0))
    o = r.place(orders.make(3, 20, 40, links=["https://youtu.be/aaaaaaaaaaa"]))
    r.db.remove(r.db.order_jobs(o.id)[0].id)  # Remove in the queue
    r.tick()
    assert r.db.get_order(o.id).status == "short"


def test_a_cancel_isnt_undone_by_the_runners_count(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    o = orders.make(3, 20, 40, links=["https://youtu.be/aaaaaaaaaaa"])
    db.add_order(o)
    db.update_order(o.id, status="cancelled")
    assert db.update_order(o.id, status="open", made=2).status == "cancelled"


def test_find_now_for_two_niches_checks_them_all(tmp_path):
    from ytclip import pipeline

    db = JobDB(tmp_path / "jobs.db")
    pipeline.request_scan(db, "g")
    pipeline.request_scan(db, "g")
    assert db.meta("scan_now") == "g" and db.meta("nudge") == "1"
    pipeline.request_scan(db, "p")
    assert db.meta("scan_now") == "1"

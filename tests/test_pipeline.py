import json
from datetime import datetime
from pathlib import Path

import pytest
from conftest import NOW, api_item

from ytclip import pipeline
from ytclip.analyze import Clip
from ytclip.download import Video
from ytclip.engine import Cancelled
from ytclip.jobs import Job, JobDB
from ytclip.pipeline import Runner, Stopped, classify, hours_wait, parse_recipes, settings_for
from ytclip.policy import PickLog
from ytclip.research import ScanResult
from ytclip.settings import Settings
from ytclip.trending import History, video_from_api


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class FakeEngine:
    """Stands in for Engine: no downloads, no ffmpeg. `plan` maps a url to clips or an exception."""

    plan: dict = {}
    made: list = []
    reject = 0  # clips the quality check rejects

    def __init__(self, settings, log=None, progress=None, cancelled=None):
        self.s, self.progress, self.cancelled = settings, progress, cancelled
        FakeEngine.made.append(self)

    def prepare(self, p):
        what = self.plan.get(p.source, 3)
        self.progress("Downloading", 0.5)
        if isinstance(what, BaseException):
            raise what
        vid = p.source.rsplit("=", 1)[-1]
        p.video = Video(id=vid, title=f"Title {vid}", channel="Chan", upload_date="", duration=600, url=p.source,
                        path=Path("x.mp4"))
        p.clips = [Clip(i * 60, i * 60 + 30, 80) for i in range(what)]
        p.mode_used = self.s.mode
        p.polished = self.s.ai_polish

    def export(self, p):
        self.exported = p
        if self.s.qc_enabled and self.reject:
            p.qc = {"passed": len(p.clips) - self.reject, "rejected": self.reject, "warned": 0}
        return self.s.auto_output_path / p.video.id


@pytest.fixture(autouse=True)
def fresh_engine():
    FakeEngine.plan, FakeEngine.made, FakeEngine.reject = {}, [], 0


def _settings(**kw):
    base = {"youtube_api_key": "K", "auto_gap_minutes": 0, "pick_from": "any", "pick_min_score": 50,
            "research_interval": 30}
    return Settings().apply(base | kw)


def _scan_result(*ids, score=80):
    videos = []
    for i, vid in enumerate(ids):
        v = video_from_api(api_item(vid, title=f"Video {vid}", minutes=30, hours_old=2, channel=f"C{i}"))
        v.score, v.origins = score, ["chart"]
        videos.append(v)
    return ScanResult(videos, NOW, ["US"])


def _runner(tmp_path, settings=None, scan=None, clock=None, history=None):
    db = JobDB(tmp_path / "jobs.db")
    s = settings or _settings()
    logs = []
    hist = history or History(tmp_path / "research.json")
    picks = PickLog(tmp_path / "picks.json")
    r = Runner(db, load=lambda: s, log=logs.append, now=clock or Clock(), scan=scan, engine=FakeEngine,
               history=lambda: hist, picks=lambda: picks)
    r.logs, r.picks_log, r.hist = logs, picks, hist
    return r


# ---------------------------------------------------------------- helpers


def test_classify_errors():
    assert classify("ERROR: [youtube] abc: Sign in to confirm you’re not a bot") == "blocked"
    assert classify("HTTP Error 429: Too Many Requests") == "blocked"
    assert classify("ERROR: [youtube] abc: Private video") == "permanent"
    assert classify("Video unavailable. This video has been removed by the uploader") == "permanent"
    assert classify("No clips found in this video.") == "permanent"
    assert classify("ffmpeg exited with code 1") == "retry"


def _at(hour, minute=0):
    return datetime(2027, 1, 15, hour, minute).timestamp()


@pytest.mark.parametrize("start,end,hour,wait_hours", [
    (0, 0, 13, 0),  # same hour twice: any time
    (1, 7, 3, 0),
    (1, 7, 9, 16),  # until 1:00 tomorrow
    (22, 6, 23, 0),  # overnight window
    (22, 6, 2, 0),
    (22, 6, 12, 10),
])
def test_hours_wait(start, end, hour, wait_hours):
    assert hours_wait(_at(hour), start, end) == pytest.approx(wait_hours * 3600)


def test_recipes_parse_and_match():
    recipes, bad = parse_recipes("Gaming = Gaming / action | Gameplay below\n"
                                 "@Some Pod = Podcast moments  # talky\n"
                                 "* = | title on top\n"
                                 "nonsense line\n"
                                 "Comedy = No such preset")
    assert [(r.match, r.preset, r.view) for r in recipes] == [
        ("Gaming", "Gaming / action", "Gameplay below"), ("@Some Pod", "Podcast moments", ""),
        ("*", "", "Title on top")]
    assert bad == ["nonsense line", "Comedy = No such preset"]
    game, pod, other = (Job("a", "u", category="20"), Job("b", "u", channel="Some Pod"), Job("c", "u"))
    assert recipes[0].matches(game) and not recipes[0].matches(pod)
    assert recipes[1].matches(pod) and recipes[2].matches(other)


def test_settings_for_a_job(tmp_path):
    s = _settings(auto_recipes="Gaming = Gaming / action | Gameplay below", auto_output_dir=str(tmp_path / "out"),
                  keep_source=True, burn_captions=False)
    game, label = settings_for(Job("a", "u", category="20"), s)
    assert game.stack_enabled and game.burn_captions and not game.keep_source
    assert game.output_path == tmp_path / "out"
    assert label == "Gaming / action · Gameplay below (recipe: Gaming)"
    other, label = settings_for(Job("b", "u", category="22"), s)
    assert label == "Shorts / TikTok / Reels" and other.cache_path == s.cache_path
    plain, label = settings_for(Job("c", "u"), s.apply({"auto_preset": "", "auto_captions": False,
                                                        "auto_delete_sources": False}))
    assert label == "My settings" and not plain.burn_captions and plain.keep_source


# ---------------------------------------------------------------- research → queue


def test_research_queues_picks_once(tmp_path):
    r = _runner(tmp_path, scan=lambda opts, history, log=None, now=None: _scan_result("a", "b"))
    assert r.research(_settings()) == 2
    jobs = {j.id: j for j in r.db.jobs()}
    assert set(jobs) == {"a", "b"} and jobs["a"].how == "auto"
    assert jobs["a"].permission == "Passed your rules (no permission on record)"
    assert {p["id"]: p["how"] for p in PickLog(tmp_path / "picks.json").picks} == {"a": "auto", "b": "auto"}
    assert r.hist.videos["a"]["sent"]
    assert r.research(_settings()) == 0  # already picked: never queued twice


def test_research_respects_the_rules(tmp_path):
    r = _runner(tmp_path, scan=lambda *a, **k: _scan_result("a", "b"))
    assert r.research(_settings(pick_from="watchlist")) == 0
    assert r.db.jobs() == []


def test_research_reuses_the_apps_recent_scan(tmp_path):
    clock = Clock()
    history = History(None)
    history.remember(_scan_result("a").videos, {"regions": ["US"]}, clock.t - 60)
    called = []
    r = _runner(tmp_path, scan=lambda *a, **k: called.append(1), clock=clock, history=history)
    assert r.research(_settings()) == 1 and not called
    assert "Using the Research tab's last check." in r.logs[0]


def test_research_failure_is_logged_not_raised(tmp_path):
    def scan(*a, **k):
        raise RuntimeError("quota is used up")

    r = _runner(tmp_path, scan=scan)
    assert r.research(_settings()) == 0
    assert "quota is used up" in r.db.meta("last_error")


# ---------------------------------------------------------------- one video


def test_process_a_video(tmp_path):
    r = _runner(tmp_path, _settings(auto_output_dir=str(tmp_path / "out")))
    r.add_manual("https://www.youtube.com/watch?v=abcdefghijk")
    job = r.db.next_due(NOW)
    assert job.id == "abcdefghijk" and job.permission == "Added by you"
    r.process(job, r.load())
    done = r.db.get(job.id)
    assert (done.status, done.clips, done.title) == ("done", 3, "Title abcdefghijk")
    assert done.folder == str(tmp_path / "out" / "abcdefghijk")
    eng = FakeEngine.made[0]
    assert eng.exported.meta["automation"]["permission"] == "Added by you"
    assert eng.exported.meta["automation"]["recipe"].startswith("Shorts / TikTok / Reels")
    assert r.db.meta_float("last_download") == NOW


def test_retry_then_give_up(tmp_path):
    clock = Clock()
    r = _runner(tmp_path, _settings(auto_retries=2), clock=clock)
    r.add_manual("https://youtu.be/aaaaaaaaaaa")
    FakeEngine.plan["https://youtu.be/aaaaaaaaaaa"] = RuntimeError("ffmpeg exited with code 1")
    r.process(r.db.next_due(clock.t), r.load())
    job = r.db.get("aaaaaaaaaaa")
    assert (job.status, job.attempts, job.next_try) == ("queued", 1, NOW + 15 * 60)
    assert job.stage == "Try 2 in 15 min" and "ffmpeg" in job.error
    assert r.db.next_due(clock.t) is None
    clock.t += 15 * 60
    r.process(r.db.next_due(clock.t), r.load())
    job = r.db.get("aaaaaaaaaaa")
    assert (job.status, job.attempts, job.stage) == ("failed", 2, "Gave up")


def test_permanent_errors_fail_at_once(tmp_path):
    r = _runner(tmp_path)
    r.add_manual("https://youtu.be/bbbbbbbbbbb")
    FakeEngine.plan["https://youtu.be/bbbbbbbbbbb"] = RuntimeError("ERROR: Private video")
    r.process(r.db.next_due(NOW), r.load())
    assert r.db.get("bbbbbbbbbbb").stage == "Can't be clipped"


def test_no_clips_is_a_failure(tmp_path):
    r = _runner(tmp_path)
    r.add_manual("https://youtu.be/ccccccccccc")
    FakeEngine.plan["https://youtu.be/ccccccccccc"] = 0
    r.process(r.db.next_due(NOW), r.load())
    job = r.db.get("ccccccccccc")
    assert job.status == "failed" and job.error == "No clips found in this video."


def test_a_bot_check_pauses_downloads(tmp_path):
    r = _runner(tmp_path)
    r.add_manual("https://youtu.be/ddddddddddd")
    FakeEngine.plan["https://youtu.be/ddddddddddd"] = RuntimeError("Sign in to confirm you're not a bot")
    r.process(r.db.next_due(NOW), r.load())
    job = r.db.get("ddddddddddd")
    assert job.status == "queued" and job.attempts == 0  # not the video's fault
    assert r.db.meta_float("blocked_until") == NOW + pipeline.BLOCK_PAUSE
    wait, why = r.wait_reason(r.load(), NOW + 60)
    assert wait == pipeline.BLOCK_PAUSE - 60 and "sign in" in why


def test_stopping_mid_video_puts_it_back(tmp_path):
    r = _runner(tmp_path)
    r.add_manual("https://youtu.be/eeeeeeeeeee")
    FakeEngine.plan["https://youtu.be/eeeeeeeeeee"] = Cancelled()
    with pytest.raises(Stopped):
        r.process(r.db.next_due(NOW), r.load())
    assert r.db.get("eeeeeeeeeee").status == "queued"


def test_ai_budget(tmp_path):
    r = _runner(tmp_path, _settings(auto_preset="AI picks (Claude)", auto_ai_daily=1))
    for vid in ("fffffffffff", "ggggggggggg"):
        r.add_manual(f"https://youtu.be/{vid}")
    r.process(r.db.next_due(NOW), r.load())
    r.process(r.db.next_due(NOW), r.load())
    assert [e.s.mode for e in FakeEngine.made] == ["ai", "auto"]
    assert "today's AI limit is used up" in r.db.get("ggggggggggg").recipe


def test_polish_shares_the_ai_budget(tmp_path):
    r = _runner(tmp_path, _settings(ai_api_key="sk-ant-x", auto_ai_daily=1))
    for vid in ("fffffffffff", "ggggggggggg"):
        r.add_manual(f"https://youtu.be/{vid}")
    r.process(r.db.next_due(NOW), r.load())
    r.process(r.db.next_due(NOW), r.load())
    assert [e.s.ai_polish for e in FakeEngine.made] == [True, False]
    assert r.db.get("fffffffffff").recipe.endswith("· AI polish")
    assert "no polish (today's AI limit is used up)" in r.db.get("ggggggggggg").recipe


def test_no_polish_without_a_key_or_when_turned_off(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not settings_for(Job("a", "u"), _settings())[0].ai_polish
    assert settings_for(Job("a", "u"), _settings(ai_api_key="k"))[0].ai_polish
    assert not settings_for(Job("a", "u"), _settings(ai_api_key="k", auto_polish=False))[0].ai_polish


def test_quality_check_counts_reach_the_queue(tmp_path):
    r = _runner(tmp_path)
    FakeEngine.reject = 1
    r.add_manual("https://youtu.be/fffffffffff")
    r.process(r.db.next_due(NOW), r.load())
    job = r.db.get("fffffffffff")
    assert FakeEngine.made[0].s.qc_enabled
    assert (job.clips, job.rejected) == (2, 1)
    assert any("1 rejected by the quality check" in m for m in r.logs)


def test_spacing_between_downloads(tmp_path):
    r = _runner(tmp_path, _settings(auto_gap_minutes=10))
    r.db.set_meta("last_download", NOW - 4 * 60)
    wait, why = r.wait_reason(r.load(), NOW)
    assert wait == 6 * 60 and "next in 6 min" in why


def test_prune_removes_cache_files_of_old_finished_videos(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for name in ("old.analysis.npz", "old.en.vtt", "new.analysis.npz", "other.mp4"):
        (cache / name).write_text("x")
    s = _settings(cache_dir=str(cache), auto_keep_days=14)
    r = _runner(tmp_path, s)
    r.db.add(Job("old", "u", status="done"), NOW - 20 * 86400)
    r.db.add(Job("new", "u", status="done"), NOW - 86400)
    assert r.prune(s) == 2
    assert sorted(f.name for f in cache.iterdir()) == ["new.analysis.npz", "other.mp4"]
    assert r.prune(s) == 0  # each video is cleaned once


# ---------------------------------------------------------------- the loop


def test_tick_scans_then_clips_then_waits(tmp_path):
    clock = Clock()
    r = _runner(tmp_path, scan=lambda *a, **k: _scan_result("a"), clock=clock)
    assert r.tick() == 1.0  # scanned, queued "a" and clipped it
    assert r.db.get("a").status == "done"
    assert r.tick() == pipeline.IDLE  # next scan is 30 min away; it checks in at least every 5
    assert r.db.meta("state") == "Waiting for the next check"


def test_paused_runner_does_nothing(tmp_path):
    r = _runner(tmp_path, scan=lambda *a, **k: pytest.fail("scanned while paused"))
    r.db.set_meta("paused", "1")
    assert r.tick() == 30.0 and r.db.meta("state") == "Paused"


def test_without_a_key_queued_videos_still_run(tmp_path):
    r = _runner(tmp_path, _settings(youtube_api_key=""))
    r.add_manual("https://youtu.be/hhhhhhhhhhh")
    r.tick()
    assert r.db.get("hhhhhhhhhhh").status == "done"
    assert any("No YouTube Data API key" in line for line in r.logs)


def test_only_one_runner_at_a_time(tmp_path):
    clock = Clock()
    r = _runner(tmp_path, clock=clock)
    r.db.set_meta("runner_pid", "99999")
    r.db.set_meta("heartbeat", clock.t - 30)
    assert "already running" in r.claim()
    r.db.set_meta("heartbeat", clock.t - pipeline.HEARTBEAT_STALE - 1)  # that one died
    assert r.claim() == ""


def test_run_resumes_interrupted_work_and_stops_cleanly(tmp_path):
    r = _runner(tmp_path, _settings(youtube_api_key=""))
    r.db.add(Job("iiiiiiiiiii", "https://youtu.be/iiiiiiiiiii", status="working"), NOW)
    assert r.run(once=True, sleep=lambda s: None) == 0
    assert r.db.get("iiiiiiiiiii").status == "done"
    assert any("interrupted last time" in line for line in r.logs)
    assert r.db.meta("runner_pid") == "" and r.db.meta("state") == "Stopped"


def test_stop_flag_ends_the_loop(tmp_path):
    r = _runner(tmp_path, _settings(youtube_api_key=""))
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        r.db.set_meta("stop", "1")

    assert r.run(sleep=sleep) == 0
    assert len(sleeps) == 1


def test_schedule_command():
    cmd = pipeline.schedule_command(True)
    assert cmd[:4] == ["schtasks", "/Create", "/TN", pipeline.TASK_NAME]
    assert cmd[cmd.index("/SC") + 1] == "ONLOGON"
    assert "--automation" in cmd[cmd.index("/TR") + 1]
    assert pipeline.schedule_command(False) == ["schtasks", "/Delete", "/TN", pipeline.TASK_NAME, "/F"]


def test_file_logger_rolls_over(tmp_path, monkeypatch):
    path = tmp_path / "automation.log"
    monkeypatch.setattr(pipeline, "log_path", lambda: path)
    path.write_text("x" * 2_000_001)
    echoed = []
    pipeline.file_logger(echoed.append)("hello")
    assert path.read_text() == "hello\n" and path.with_suffix(".log.1").exists() and echoed == ["hello"]


def test_clips_json_carries_why_a_video_was_picked(tmp_path):
    from ytclip.organize import write_index

    v = Video(id="abc", title="T", channel="C", upload_date="", duration=60, url="https://youtu.be/abc",
              path=tmp_path / "abc.mp4", license="Creative Commons Attribution license (reuse allowed)")
    write_index(tmp_path, v, [Clip(0, 30, 80)], ["01.mp4"], [], "auto", {"automation": {"permission": "ok"}})
    data = json.loads((tmp_path / "clips.json").read_text(encoding="utf-8"))
    assert data["automation"] == {"permission": "ok"} and data["clips"][0]["file"] == "01.mp4"
    # TikTok Manager reads the licence to add a credit line for videos added by hand
    assert data["video"]["license"].startswith("Creative Commons")


# ---------------------------------------------------------------- jobs.db


def test_jobs_db_basics(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    assert db.add(Job("a", "u", origins=["cc"]), NOW)
    assert not db.add(Job("a", "u2"), NOW)
    assert db.get("a").origins == ["cc"]
    db.add(Job("b", "u", status="failed"), NOW + 1)
    assert db.counts() == {"queued": 1, "working": 0, "done": 0, "failed": 1}
    db.retry("b")
    assert db.get("b").status == "queued"
    assert [j.id for j in db.jobs()] == ["a", "b"]  # waiting ones oldest first
    db.note("ai", NOW)
    db.note("ai", NOW - 2 * 86400)
    assert db.count("ai", NOW - 86400) == 1
    db.remove("a")
    assert db.get("a") is None
    again = JobDB(tmp_path / "jobs.db")  # a second connection (the app) sees the same data
    assert again.get("b").status == "queued"

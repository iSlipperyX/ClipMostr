import json
import os
import time
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
    assert game.caption_censor  # automation bleeps swearing by default
    assert not settings_for(Job("d", "u"), s.apply({"auto_bleep": False, "caption_censor": False}))[0].caption_censor


# ---------------------------------------------------------------- research → queue


def test_research_queues_picks_once(tmp_path):
    r = _runner(tmp_path, scan=lambda opts, history, log=None, progress=None, cancelled=None, now=None: _scan_result("a", "b"))
    assert r.research(_settings()) == 2
    jobs = {j.id: j for j in r.db.jobs()}
    assert set(jobs) == {"a", "b"} and jobs["a"].how == "auto"
    assert jobs["a"].permission == "Passed your rules (no permission on record)"
    assert {p["id"]: p["how"] for p in PickLog(tmp_path / "picks.json").picks} == {"a": "auto", "b": "auto"}
    assert r.hist.videos["a"]["sent"]
    assert r.research(_settings()) == 0  # already picked: never queued twice


def test_research_respects_the_rules(tmp_path):
    s = _settings(pick_from="watchlist")
    r = _runner(tmp_path, s, scan=lambda *a, **k: _scan_result("a", "b"))
    assert r.research(s) == 0
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
    assert r.db.meta_float("blocked_until") == NOW + pipeline.FIRST_PAUSE  # 2 hours the first time
    wait, why = r.wait_reason(r.load(), NOW + 60)
    assert wait == pipeline.FIRST_PAUSE - 60 and "sign in" in why
    assert job.stage == pipeline.SIGN_IN_WAIT and job.next_try == NOW + pipeline.FIRST_PAUSE


def test_a_second_bot_check_the_same_day_pauses_longer(tmp_path):
    r = _runner(tmp_path)
    r.block("Sign in to confirm you're not a bot", NOW)
    later = NOW + pipeline.FIRST_PAUSE + 3600
    r.block("Sign in to confirm you're not a bot", later)
    assert r.db.meta_float("blocked_until") == later + pipeline.BLOCK_PAUSE
    calm = later + pipeline.BLOCK_PAUSE + pipeline.CALM_DAY + 1
    r.block("Sign in to confirm you're not a bot", calm)  # a quiet day later it's the short one again
    assert r.db.meta_float("blocked_until") == calm + pipeline.FIRST_PAUSE


def test_while_paused_for_sign_in_the_status_says_so(tmp_path):
    """The waiting video isn't due until the pause ends, so the status used to read 'Waiting for the next
    check' for hours, which looked stuck."""
    clock = Clock()
    r = _runner(tmp_path, settings=_settings(youtube_api_key=""), clock=clock)
    r.add_manual("https://youtu.be/ddddddddddd")
    FakeEngine.plan["https://youtu.be/ddddddddddd"] = RuntimeError("Sign in to confirm you're not a bot")
    r.tick()
    clock.t = NOW + 600
    r.tick()
    assert r.db.meta("state").startswith("Paused until") and "sign in" in r.db.meta("state")


def test_try_now_ends_the_pause_and_frees_the_waiting_videos(tmp_path):
    r = _runner(tmp_path)
    r.add_manual("https://youtu.be/ddddddddddd")
    r.add_manual("https://youtu.be/eeeeeeeeeee")
    FakeEngine.plan["https://youtu.be/ddddddddddd"] = RuntimeError("Sign in to confirm you're not a bot")
    r.process(r.db.next_due(NOW), r.load())
    r.db.update("eeeeeeeeeee", NOW, next_try=NOW + 900, stage="Try 2 in 15 min")  # a retry: left alone
    assert pipeline.end_pause(r.db, NOW + 60) == 1
    assert not r.blocked(NOW + 61)
    assert r.db.get("ddddddddddd").next_try == 0 and r.db.get("ddddddddddd").stage == "Waiting"
    assert r.db.get("eeeeeeeeeee").next_try == NOW + 900
    assert r.gap_factor(NOW + 61) == 2  # still spaced out further for the day
    assert r.db.meta("nudge") == "1"


def test_picking_a_browser_login_ends_the_pause(tmp_path):
    s = _settings()
    r = _runner(tmp_path, settings=s)
    r.block("Sign in to confirm you're not a bot", NOW)
    r.lift_pause_for_sign_in(s, NOW + 60)
    assert r.blocked(NOW + 60)  # no browser set: it waits
    s.cookies_browser = "firefox"
    r.lift_pause_for_sign_in(s, NOW + 120)
    assert not r.blocked(NOW + 120) and "firefox" in r.logs[-1]
    r.block("Sign in to confirm you're not a bot", NOW + 600)  # asked again even with firefox
    r.lift_pause_for_sign_in(s, NOW + 660)
    assert r.blocked(NOW + 660)  # the same browser doesn't end it again


def test_the_runner_wakes_when_the_app_ends_a_pause(tmp_path):
    r = _runner(tmp_path, settings=_settings(youtube_api_key=""))
    slept = []

    def sleep(step):
        slept.append(step)
        if len(slept) == 3:
            r.db.set_meta("nudge", "1")
        if len(slept) > 10:
            r.db.set_meta("stop", "1")

    r.tick = lambda: 300.0 if len(slept) < 3 else 0.5
    r.run(sleep=sleep)
    assert len(slept) <= 11 and r.db.meta("nudge") == "0"


def test_after_a_bot_check_downloads_slow_down_then_recover(tmp_path):
    clock = Clock()
    r = _runner(tmp_path, settings=_settings(auto_gap_minutes=10), clock=clock)
    r.block("Sign in to confirm you're not a bot", NOW)
    after = NOW + pipeline.BLOCK_PAUSE
    r.db.set_meta("last_download", after)
    wait, why = r.wait_reason(r.load(), after + 1)
    assert round(wait) == 20 * 60 - 1 and "2x" in why  # twice the usual spacing
    r.block("HTTP Error 429", after + 3600)  # again the same day: four times
    assert r.gap_factor(after + 3600) == 4
    r.block("HTTP Error 429", after + 7200)
    assert r.gap_factor(after + 7200) == 4  # never more than that
    calm = after + 7200 + pipeline.BLOCK_PAUSE + pipeline.CALM_DAY + 1
    assert r.gap_factor(calm) == 1  # a quiet day later, back to normal
    assert "carries on by itself" in r.logs[-1]


def test_research_asks_youtube_nothing_while_paused(tmp_path):
    seen = []

    def scan(opts, history, log=None, progress=None, cancelled=None, now=None):
        seen.append(opts)
        result = _scan_result("a")
        result.blocked = "Sign in to confirm you're not a bot" if len(seen) == 1 else ""
        return result

    clock = Clock()
    r = _runner(tmp_path, scan=scan, clock=clock)
    r.research(r.load())
    assert seen[0].deep_workers == 1 and seen[0].deep_pause > 0 and seen[0].deep_count > 0
    assert r.blocked(NOW + 60)  # the replay checks hit a bot check: everything pauses
    clock.t = NOW + 3600
    r.research(r.load())
    assert seen[1].deep_count == 0


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
    assert r.db.meta("state") == "Waiting for the next check at " + datetime.fromtimestamp(NOW + 1800).strftime("%H:%M")


def test_a_check_that_picks_nothing_says_why(tmp_path):
    scans = []

    def scan(*a, **k):
        scans.append(1)
        return _scan_result("aaaaaaaaaaa", "bbbbbbbbbbb", score=80 if len(scans) == 1 else 10)

    clock = Clock()
    r = _runner(tmp_path, scan=scan, clock=clock)
    r.research(r.load())
    assert r.db.meta("pick_note") == ""
    clock.t = NOW + 3600
    r.research(r.load())  # the same two: already picked, now low too
    assert r.db.meta("pick_note") == "2 videos: 2 already picked"
    for j in r.db.jobs():
        r.db.update(j.id, status="done")
    r.tick()
    assert r.db.meta("state").startswith("Nothing new to pick in the last check (2 videos: 2 already picked)")
    assert pipeline.why_none([]) == "no videos found"


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
    import subprocess
    import sys

    clock = Clock()
    r = _runner(tmp_path, clock=clock)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        r.db.set_meta("runner_pid", str(other.pid))
        r.db.set_meta("heartbeat", clock.t - 30)
        assert "already running" in r.claim()
    finally:
        other.kill()
        other.wait()
    assert not pipeline.process_alive(other.pid)
    assert r.claim() == "" and any("ended without stopping" in line for line in r.logs)  # it crashed: take over
    r.db.set_meta("runner_pid", str(other.pid))
    r.db.set_meta("heartbeat", clock.t - pipeline.HEARTBEAT_STALE - 1)  # that one died
    assert r.claim() == ""


def test_a_heartbeat_from_before_the_pc_started_is_from_a_runner_thats_gone(tmp_path, monkeypatch):
    clock = Clock()
    r = _runner(tmp_path, clock=clock)
    r.db.set_meta("runner_pid", str(os.getpid()))  # a live process has its id now (ids are reused)
    r.db.set_meta("heartbeat", clock.t - 30)
    r.pid = os.getpid() + 1
    monkeypatch.setattr(pipeline, "boot_time", lambda: clock.t - 10)  # a fast restart
    assert not pipeline.runner_alive(r.db, clock.t)
    assert r.claim() == ""
    monkeypatch.undo()  # and the real one answers
    assert 0 <= pipeline.boot_time() < time.time()


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


def test_stop_interrupts_a_long_research_scan(tmp_path):
    from ytclip.research import ScanCancelled

    def scan(opts, history, log=None, progress=None, cancelled=None, now=None):
        assert cancelled() is False
        r.db.set_meta("stop", "1")  # Stop pressed (or the updater pausing automation) mid-scan
        if cancelled():
            raise ScanCancelled()
        raise AssertionError("not cancelled")

    r = _runner(tmp_path, scan=scan)
    with pytest.raises(pipeline.Stopped):
        r.research(r.load())


# ---------------------------------------------------------------- niches

GAMING = {"id": "game01", "name": "Gaming", "account": "@gameclips", "channels": "@C0", "preset": "Gaming / action",
          "view": "Gameplay below", "stack_folder": "D:/gameplay", "instructions": "clutch plays only"}


def test_a_niche_gives_its_style_and_its_own_folder(tmp_path):
    s = _settings(niches=[GAMING], auto_output_dir=str(tmp_path / "out"), ai_instructions="Good moments.")
    game, label = settings_for(Job("a", "u", niche="game01"), s)
    assert game.output_path == tmp_path / "out" / "Gaming"
    assert game.stack_enabled and game.stack_folder == "D:/gameplay" and game.clip_count == 8
    assert game.ai_instructions == ("Good moments.\nThese clips are for a TikTok account about Gaming: "
                                    "clutch plays only")
    assert label == "Gaming / action · Gameplay below (niche: Gaming)"
    plain, label = settings_for(Job("b", "u"), s)
    assert plain.output_path == tmp_path / "out" and label == "Shorts / TikTok / Reels"
    gone, _ = settings_for(Job("c", "u", niche="deleted"), s)  # its niche was deleted: Automation's style
    assert gone.output_path == tmp_path / "out"


def test_a_recipe_still_wins_over_the_niche_style(tmp_path):
    s = _settings(niches=[GAMING], auto_recipes="@Pod = Podcast moments", auto_output_dir=str(tmp_path / "out"))
    pod, label = settings_for(Job("a", "u", channel="Pod", niche="game01"), s)
    assert pod.output_path == tmp_path / "out" / "Gaming"  # still the niche's folder
    assert pod.min_len == 45 and label == "Podcast moments · Gameplay below (niche: Gaming, recipe: @Pod)"


def test_research_queues_each_pick_for_its_niche(tmp_path):
    history = History(None)
    history.sources["channels"]["@c0"] = {"id": "UCC0"}
    s = _settings(niches=[GAMING])
    r = _runner(tmp_path, s, scan=lambda *a, **k: _scan_result("a", "b"), history=history)
    assert r.research(s) == 1  # b (channel C1) fits no niche
    job = r.db.get("a")
    assert job.niche == "game01"
    assert any("Picked for Gaming: Video a" in line for line in r.logs)
    assert PickLog(tmp_path / "picks.json").picks[0]["niche"] == "game01"


def test_a_niche_deleted_during_the_scan_gets_no_picks(tmp_path):
    """A scan takes minutes; deleting a niche meanwhile keeps its videos out of the queue."""
    history = History(None)
    history.sources["channels"]["@c0"] = {"id": "UCC0"}
    pods = {"id": "pod001", "name": "Podcasts", "channels": "@C1"}
    s = _settings(niches=[GAMING, pods])
    current = {"s": s}

    def scan(*a, **k):
        current["s"] = s.apply({"niches": [pods]})  # deleted in the Niches tab while the scan runs
        return _scan_result("a", "b")

    r = _runner(tmp_path, s, scan=scan, history=history)
    r.load = lambda: current["s"]
    history.sources["channels"]["@c1"] = {"id": "UCC1"}
    assert r.research(s) == 1
    assert r.db.get("a") is None and r.db.get("b").niche == "pod001"


def test_a_queued_video_whose_niche_was_deleted_leaves_the_queue(tmp_path):
    pods = {"id": "pod001", "name": "Podcasts", "channels": "@C1"}
    s = _settings(niches=[GAMING, pods], auto_output_dir=str(tmp_path / "out"))
    r = _runner(tmp_path, s)
    r.add_manual("https://www.youtube.com/watch?v=abcdefghijk", niche="game01")
    gone = s.apply({"niches": [pods]})
    r.load = lambda: gone
    r.process(r.db.next_due(NOW), gone)
    assert r.db.get("abcdefghijk") is None and not FakeEngine.made
    assert any("its niche was deleted" in line for line in r.logs)


def test_with_no_niches_to_be_read_a_niche_video_waits_to_be_retried(tmp_path):
    """No niches at all may mean settings.json couldn't be read: the video stays, marked, to retry."""
    s = _settings(niches=[GAMING], auto_output_dir=str(tmp_path / "out"))
    r = _runner(tmp_path, s)
    r.add_manual("https://www.youtube.com/watch?v=abcdefghijk", niche="game01")
    r.load = Settings
    r.process(r.db.next_due(NOW), Settings())
    job = r.db.get("abcdefghijk")
    assert job.status == "failed" and "niche isn't in your settings" in job.error and not FakeEngine.made


def test_settings_older_than_the_niche_never_take_its_video_out(tmp_path):
    """The runner was handed settings from before the niche was made (or read mid-save): it looks again."""
    s = _settings(niches=[GAMING], auto_output_dir=str(tmp_path / "out"))
    r = _runner(tmp_path, s)
    r.add_manual("https://www.youtube.com/watch?v=abcdefghijk", niche="game01")
    r.process(r.db.next_due(NOW), _settings(auto_output_dir=str(tmp_path / "out")))
    assert r.db.get("abcdefghijk").status == "done" and not any("deleted" in line for line in r.logs)
    assert Path(FakeEngine.made[-1].s.output_dir).name == "Gaming"  # clipped with the niche's own settings


def test_a_niche_made_during_the_scan_keeps_its_first_pick(tmp_path):
    """Making the first niche while a scan runs: its pick is clipped, not dropped as 'deleted'."""
    history = History(None)
    history.sources["channels"]["@c0"] = {"id": "UCC0"}
    s = _settings(auto_output_dir=str(tmp_path / "out"))
    current = {"s": s}

    def scan(*a, **k):
        current["s"] = s.apply({"niches": [GAMING]})  # made in the Niches tab while the scan runs
        return _scan_result("a")

    r = _runner(tmp_path, s, scan=scan, history=history)
    r.load = lambda: current["s"]
    r.tick()
    assert r.db.get("a").status == "done" and r.db.get("a").niche == "game01"
    assert not any("deleted" in line for line in r.logs)


def test_a_catch_all_recipe_only_fills_in_what_the_niche_leaves_out(tmp_path):
    s = _settings(niches=[GAMING | {"view": ""}], auto_recipes="* = Podcast moments | Title on top",
                  auto_output_dir=str(tmp_path / "out"))
    game, label = settings_for(Job("a", "u", niche="game01"), s)
    assert label == "Gaming / action · Title on top (niche: Gaming, recipe: *)"
    plain, label = settings_for(Job("b", "u"), s)  # no niche: the catch-all is the recipe, as before
    assert label == "Podcast moments · Title on top (recipe: *)"
    both, label = settings_for(Job("a", "u", niche="game01"), s.apply({"niches": [GAMING]}))
    assert label == "Gaming / action · Gameplay below (niche: Gaming)"  # the niche has both: * isn't used


def test_a_recipes_own_second_videos_beat_the_niches(tmp_path):
    from ytclip.settings import LAYOUT_PRESETS, save_user_layout, save_user_preset

    parkour = Settings().apply(LAYOUT_PRESETS["Gameplay below"] | {"stack_folder": "C:/parkour"})
    save_user_layout("Parkour below", parkour)
    save_user_preset("Parkour preset", parkour)
    s = _settings(niches=[GAMING], auto_recipes="@Pod = Gaming / action | Parkour below\n@Cast = Parkour preset")
    pod, _ = settings_for(Job("a", "u", channel="Pod", niche="game01"), s)
    assert pod.stack_folder == "C:/parkour"  # from the recipe's view
    cast, _ = settings_for(Job("c", "u", channel="Cast", niche="game01"), s)
    assert cast.stack_folder == "C:/parkour"  # from the recipe's saved preset
    other, _ = settings_for(Job("b", "u", channel="Else", niche="game01"), s)
    assert other.stack_folder == "D:/gameplay"


def test_the_runner_marks_the_video_it_is_clipping_as_busy(tmp_path):
    from ytclip import cleanup

    s = _settings()
    r = _runner(tmp_path, s)
    r.add_manual("https://www.youtube.com/embed/abcdefghijk")
    assert r.db.get("abcdefghijk")  # embed links give the real id too
    seen = []
    FakeEngine.plan = {}
    orig = FakeEngine.export

    def export(self, p):
        seen.append(cleanup.busy_ids(r.db))
        return orig(self, p)

    FakeEngine.export = export
    try:
        job = r.db.next_due(NOW)
        r.db.update(job.id, NOW, url="https://example.com/watch?v=zzzzzzzzzzz")
        r.process(r.db.get(job.id), s)
    finally:
        FakeEngine.export = orig
    assert "zzzzzzzzzzz" in seen[0]


def test_a_niche_video_carries_its_niche_into_clips_json(tmp_path):
    s = _settings(niches=[GAMING], auto_output_dir=str(tmp_path / "out"))
    r = _runner(tmp_path, s)
    r.add_manual("https://www.youtube.com/watch?v=abcdefghijk", niche="game01")
    r.process(r.db.next_due(NOW), s)
    eng = FakeEngine.made[0]
    meta = eng.exported.meta["automation"]
    assert (meta["niche"], meta["niche_id"], meta["account"]) == ("Gaming", "game01", "@gameclips")
    assert eng.s.output_path == tmp_path / "out" / "Gaming"
    assert "(niche: Gaming)" in r.db.get("abcdefghijk").recipe


def test_clean_up_runs_once_a_day_when_switched_on(tmp_path, monkeypatch):
    from ytclip import cleanup

    calls = []

    def fake_plan(s, now, busy=(), older_than_days=0, kinds=()):
        calls.append((set(busy), older_than_days, list(kinds)))
        return cleanup.Plan()

    def fake_apply(plan, kinds):
        result = cleanup.Result(removed=2, freed=5_000_000, by_kind={"rejected": 2})
        result.failed = [("x.mp4", "in use")]
        return result

    monkeypatch.setattr(cleanup, "plan", fake_plan)
    monkeypatch.setattr(cleanup, "apply", fake_apply)
    clock = Clock()
    s = _settings(cleanup_auto=True, cleanup_kinds="rejected, posted", cleanup_days=3)
    r = _runner(tmp_path, s, scan=lambda *a, **k: _scan_result(), clock=clock)
    r.add_manual("https://youtu.be/aaaaaaaaaaa")
    r.tick()
    assert calls == [({"aaaaaaaaaaa"}, 3, ["rejected", "posted"])]
    assert any("Cleaned up: 2 rejected clips (4.8 MB freed)." in line for line in r.logs)
    assert any("Couldn't remove x.mp4: in use" in line for line in r.logs)
    clock.t += 3600
    r.tick()
    assert len(calls) == 1  # once a day
    clock.t += pipeline.DAY
    r.tick()
    assert len(calls) == 2
    r.load = lambda: s.apply({"cleanup_auto": False})
    clock.t += 2 * pipeline.DAY
    r.tick()
    assert len(calls) == 2

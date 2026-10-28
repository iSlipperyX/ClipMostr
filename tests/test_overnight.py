import json
import threading
import time
from pathlib import Path

from conftest import NOW
from test_pipeline import FakeEngine, _runner, _settings, fresh_engine  # noqa: F401

from ytclip import doctor, ffmpeg, orders, pipeline, report
from ytclip.jobs import Job, JobDB

GB = 1024 ** 3


# ---------------------------------------------------------------- the morning report


def _folder(tmp_path, name, problems):
    folder = tmp_path / name
    folder.mkdir()
    clips = [{"file": f"_rejected/{i}.mp4", "qc": {"ok": False, "problems": [p]}} for i, p in enumerate(problems)]
    (folder / "clips.json").write_text(json.dumps({"clips": clips + [{"file": "ok.mp4", "qc": {"ok": True}}]}))
    return str(folder)


def test_the_report_adds_up_the_night(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    s = _settings(niches=[{"id": "g", "name": "Gaming", "account": "@g"}, {"id": "p", "name": "Pods"}])
    db.add(Job("a", "u", title="A", niche="g", status="done", clips=4, rejected=2,
               folder=_folder(tmp_path, "a", ["AI rated it 3/10: dull", "AI rated it 2/10"])), NOW - 3600)
    db.add(Job("b", "u", title="B", niche="g", status="done", clips=3,
               folder=_folder(tmp_path, "b", [])), NOW - 7200)
    db.add(Job("c", "u", title="C", status="done", clips=2, rejected=1,
               folder=_folder(tmp_path, "c", ["Came out 3s long instead of 30s"])), NOW - 100)
    db.add(Job("d", "u", title="Broken one", status="failed", error="ffmpeg failed:\n  bad   data"), NOW - 50)
    db.add(Job("old", "u", title="Last week", status="done", clips=9), NOW - 9 * 86400)
    db.add(Job("w", "u", title="Waiting"), NOW)
    for kind in ("blocked", "disk_low", "watchdog", "watchdog", f"freed:{3 * GB}", "freed:junk"):
        db.note(kind, NOW - 600)
    o = orders.make(5, 30, 45, niche="g", now=NOW - 5000)
    o.made, o.updated = 2, NOW - 100
    db.add_order(o)
    r = report.build(db, s, hours=12, now=NOW)
    assert (r.videos, r.clips, r.rejected) == (3, 9, 3)
    assert r.failed == [("Broken one", "ffmpeg failed: bad data")]
    assert [(n.name, n.videos, n.clips, n.rejected) for n in r.niches] == [("Gaming", 2, 7, 2), ("No niche", 1, 2, 1)]
    assert r.reasons == [("the AI rated it low", 2), ("wrong length or size", 1)]
    assert (r.blocks, r.disk_pauses, r.watchdog, r.freed) == (1, 1, 2, 3 * GB)
    assert r.waiting == 1 and r.requests[0].made == 2
    lines = r.lines()
    assert lines[0] == "Last 12 hours: 9 clips ready from 3 videos, 3 rejected, 1 video failed."
    assert "Gaming (@g): 7 clips from 2 videos, 2 rejected" in lines
    assert "Rejected because: the AI rated it low (2); wrong length or size (1)" in lines
    assert any(line.startswith("Request, 5 clips of 30–45 s for Gaming: 2 of 5 ready") for line in lines)
    assert "Clean up freed 3.0 GB" in lines and "Failed: Broken one: ffmpeg failed: bad data" in lines
    json.dumps(r.to_json())


def test_a_quiet_night_says_so(tmp_path):
    r = report.build(JobDB(tmp_path / "jobs.db"), _settings(), hours=12, now=NOW)
    assert r.lines() == ["Nothing was clipped in the last 12 hours."]


# ---------------------------------------------------------------- the health check


def _levels(checks):
    return {c.name: c.level for c in checks}


def test_the_health_check_finds_what_automation_needs(tmp_path, monkeypatch):
    out = tmp_path / "out"
    s = _settings(youtube_api_key="", auto_output_dir=str(out), cache_dir=str(tmp_path / "cache"),
                  niches=[{"id": "g", "name": "Gaming"}, {"id": "p", "name": "Pods", "searches": "x", "paused": True}],
                  auto_polish=True)
    db = JobDB(tmp_path / "jobs.db")
    db.add_order(orders.make(4, 20, 40))
    db.set_meta("blocked_at", NOW - 3600)
    got = _levels(doctor.check(s, db, now=NOW))
    assert got["ffmpeg"] == doctor.GOOD
    assert got["YouTube Data API key"] == doctor.WARN and got["Anthropic key"] == doctor.WARN
    assert got["Automation folder"] == doctor.GOOD and not out.exists()  # checking makes no folders
    assert got["Niche Gaming"] == doctor.WARN and got["Paused niches"] == doctor.WARN
    assert got["Clip requests"] == doctor.BAD and got["Browser login"] == doctor.WARN
    assert got["TikTok Manager"] == doctor.WARN


def test_low_disk_and_an_old_downloader_are_flagged(tmp_path, monkeypatch):
    s = _settings(auto_output_dir=str(tmp_path), cache_dir=str(tmp_path), disk_min_free_gb=15)
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: type("U", (), {"free": 4 * GB})())
    checks = doctor.check(s, None, now=NOW)
    disk = next(c for c in checks if c.name.startswith("Free space"))
    assert disk.level == doctor.BAD and "under your 15 GB" in disk.detail
    assert doctor.ytdlp_age_days("2026.10.26", NOW) is not None and doctor.ytdlp_age_days("junk", NOW) is None
    import yt_dlp.version

    monkeypatch.setattr(yt_dlp.version, "__version__", "2020.01.01")
    assert _levels(doctor.check(s, None, now=NOW))["Downloader (yt-dlp)"] == doctor.WARN


def test_an_app_installed_inside_onedrive_is_flagged(tmp_path, monkeypatch):
    from ytclip import updater

    s = _settings(auto_output_dir=str(tmp_path))
    synced = tmp_path / "OneDrive" / "Desktop" / "ClipMonster"
    monkeypatch.setattr(updater, "install_dir", lambda: synced)
    app = next(c for c in doctor.check(s, None, now=NOW) if c.name == "App folder")
    assert app.level == doctor.WARN and "OneDrive" in app.detail and "ClipMonster" in app.fix
    monkeypatch.setenv("OneDrive", str(tmp_path / "Sync"))
    assert doctor.in_synced_folder(tmp_path / "Sync" / "Desktop" / "ClipMonster")
    assert doctor.in_synced_folder(Path("C:/Users/J/OneDrive - Work/ClipMonster"))
    assert not doctor.in_synced_folder(tmp_path / "SyncNot" / "ClipMonster")
    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path / "ClipMonster")
    assert "App folder" not in _levels(doctor.check(s, None, now=NOW))


def test_tiktok_manager_watching_the_folder_is_good(tmp_path, monkeypatch):
    tm = tmp_path / "roaming" / "TikTokManager"
    tm.mkdir(parents=True)
    (tm / "library.json").write_text(json.dumps({"items": []}))
    out = tmp_path / "out"
    out.mkdir()
    (tm / "settings.json").write_text(json.dumps({"watch_folders": [str(out)]}))
    s = _settings(auto_output_dir=str(out))
    assert _levels(doctor.check(s, None, now=NOW))["TikTok Manager"] == doctor.GOOD


def test_automation_runs_the_check_as_it_starts(tmp_path):
    r = _runner(tmp_path, _settings(youtube_api_key=""))
    r.run(once=True, sleep=lambda x: None)
    saved = json.loads(r.db.meta("health"))
    assert any(c["name"] == "YouTube Data API key" for c in saved)
    assert any("Health check: YouTube Data API key" in line for line in r.logs)


# ---------------------------------------------------------------- the disk guard


def test_low_disk_cleans_up_then_pauses_then_carries_on(tmp_path, monkeypatch):
    s = _settings(disk_min_free_gb=15)
    r = _runner(tmp_path, s)
    free = {"bytes": 5 * GB}
    monkeypatch.setattr(pipeline.Runner, "free_space", lambda self, s: (free["bytes"], "D:\\"))
    cleaned = []
    monkeypatch.setattr(pipeline.Runner, "cleanup", lambda self, s, kinds=None, days=None: cleaned.append(kinds))
    r.db.add(Job("v", "https://www.youtube.com/watch?v=v"), pipeline.time.time())
    r.db.set_meta("last_scan", r.now())
    wait = r.tick()
    assert cleaned == [list(pipeline.DISK_KINDS)] and wait == pipeline.DISK_RECHECK
    assert r.db.get("v").status == "queued" and "only 5.0 GB free on D:" in r.db.meta("state")
    assert r.db.count("disk_low", 0) == 1
    r.tick()  # still low: no second clean-up within the hour, no second event
    assert len(cleaned) == 1 and r.db.count("disk_low", 0) == 1
    free["bytes"] = 40 * GB
    r.tick()
    assert r.db.get("v").status == "done" and any("There's room again" in line for line in r.logs)


def test_the_disk_guard_can_be_switched_off(tmp_path, monkeypatch):
    r = _runner(tmp_path, _settings(disk_min_free_gb=0))
    monkeypatch.setattr(pipeline.Runner, "free_space", lambda self, s: (1, "C:\\"))
    assert r.disk_guard(r.load(), r.now()) == ""


def test_freed_space_is_noted_for_the_report(tmp_path, monkeypatch):
    from ytclip import cleanup

    r = _runner(tmp_path, _settings())
    monkeypatch.setattr(cleanup, "plan", lambda *a, **k: cleanup.Plan())
    monkeypatch.setattr(cleanup, "apply", lambda *a, **k: cleanup.Result(removed=2, freed=1234,
                                                                          by_kind={"downloads": 2}))
    r.cleanup(r.load(), kinds=["downloads"], days=0)
    assert [k for _, k in r.db.events_since(0)] == ["freed:1234"]


# ---------------------------------------------------------------- the watchdog


def _long_ffmpeg(errors):
    cmd = [ffmpeg.ffmpeg_exe(), "-v", "error", "-re", "-f", "lavfi", "-i", "anullsrc", "-t", "120", "-f", "null", "-"]
    try:
        ffmpeg.run_with_progress(cmd, 0, None)
    except Exception as exc:
        errors.append(exc)


def test_kill_running_stops_ffmpeg_and_the_caller_knows(tmp_path):
    errors = []
    t = threading.Thread(target=_long_ffmpeg, args=(errors,))
    t.start()
    for _ in range(100):
        if ffmpeg._live:
            break
        time.sleep(0.05)
    assert ffmpeg.kill_running() == 1
    t.join(10)
    assert not t.is_alive() and isinstance(errors[0], ffmpeg.Stopped)


def test_a_watchdog_fires_only_after_its_limit(monkeypatch):
    clock = {"t": 100.0}
    running = {"n": 0}
    monkeypatch.setattr(ffmpeg, "kill_running", lambda: running["n"])
    w = pipeline.Watchdog(60, last=lambda: 30.0, stage=lambda: "Exporting clip 2/5 40%", now=lambda: clock["t"])
    assert not w.check()  # nothing to stop (a slow download): it keeps watching
    running["n"] = 1
    assert w.check()
    assert w.reason() == "Stuck: no progress for 1 min while exporting clip 2/5, so it was stopped"
    quiet = pipeline.Watchdog(60, last=lambda: 90.0, stage=lambda: "x", now=lambda: clock["t"])
    assert not quiet.check()
    off = pipeline.Watchdog(0, last=lambda: 0.0, stage=lambda: "x", now=lambda: clock["t"])
    assert not off.check()


def test_a_stuck_video_is_stopped_tried_once_more_then_skipped(tmp_path, monkeypatch):
    class Quick(pipeline.Watchdog):
        def __init__(self, limit, last, stage, now=time.time, every=0.05):
            super().__init__(0.3 if limit else 0, last, stage, time.time, every)

    monkeypatch.setattr(pipeline, "Watchdog", Quick)

    class Hanging(FakeEngine):
        def prepare(self, p):
            self.progress("Measuring audio", 0.1)
            _long_ffmpeg(errs := [])
            raise errs[0]

    r = _runner(tmp_path, _settings(auto_retries=5), clock=time.time)
    r._engine = Hanging
    url = "https://www.youtube.com/watch?v=v"
    r.db.add(Job("v", url), time.time())
    r.process(r.db.get("v"), r.load())
    job = r.db.get("v")
    assert job.status == "queued" and job.error.startswith("Stuck: no progress") and "measuring audio" in job.error
    assert r.db.count("watchdog", 0) == 1
    r.process(r.db.get("v"), r.load())
    job = r.db.get("v")
    assert job.status == "failed" and job.stage == "Skipped: stuck twice"
    assert pipeline.classify("Stuck: x", "Stuck: y") == "permanent" and pipeline.classify("Stuck: x") == "retry"

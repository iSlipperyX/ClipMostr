"""Status bars (steps.py and gui/status_bars.py): which step a video is on, how far along it is, the time
left, and where the app shows it."""

import json
import os
import time

import pytest
from conftest import NOW
from test_pipeline import FakeEngine, _runner, _settings

from ytclip import steps
from ytclip.jobs import Job, JobDB
from ytclip.settings import Settings


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- which step


@pytest.mark.parametrize("message, step", [
    ("Fetching video info", "download"), ("Downloading (12 / 300 MB, 4.1 MB/s)", "download"),
    ("Merging audio and video", "download"), ("Downloaded already", "download"), ("Reading file", "download"),
    ("Measuring audio", "analyse"), ("Detecting scene cuts", "analyse"),
    ("Preparing audio for transcription", "transcript"), ("Loading Whisper 'medium' model (first use downloads it)",
                                                          "transcript"),
    ("Transcribing speech", "transcript"), ("Transcribing the clips", "transcript"),
    ("Finding clips", "find"), ("AI is picking clips", "find"), ("AI is polishing the clips", "polish"),
    ("Exporting clip 3/6", "export"), ("Making a clip again", "export"), ("Checking clip 2/6", "check"),
    ("Something else", ""),
])
def test_every_engine_message_has_its_step(message, step):
    assert steps.step_of(message) == step


def test_the_plan_follows_the_settings():
    s = Settings()
    assert steps.plan_for(s.apply({"ai_polish": True, "qc_enabled": True})) == [
        "download", "analyse", "transcript", "find", "polish", "export", "check"]
    assert steps.plan_for(s.apply({"ai_polish": False, "qc_enabled": False, "transcribe": "never"})) == [
        "download", "analyse", "find", "export"]
    assert steps.plan_for(s.apply({"qc_enabled": True}), prepare=False) == ["export", "check"]
    assert "export" not in steps.plan_for(s, export=False)


def test_weights_come_from_the_last_videos_timings():
    w = steps.weights_from_timings({"download": 30, "audio": 5, "scenes": 7, "transcript": 400, "junk": "x"})
    assert w["download"] == 30 and w["analyse"] == 12 and w["transcript"] == 400
    assert w["export"] == steps.DEFAULT_SECONDS["export"]  # not timed yet: the default
    assert steps.weights_from_timings(None) == steps.DEFAULT_SECONDS


# ---------------------------------------------------------------- tracking a job


def test_the_overall_bar_moves_through_the_steps_by_their_weights():
    clock = Clock()
    plan = ["download", "analyse", "export"]
    t = steps.Tracker(plan, {"download": 10, "analyse": 10, "export": 80}, clock=clock)
    st = t.update("Downloading (1 / 10 MB)", 0.5)
    assert (st.step, st.index, st.count, st.step_frac) == ("download", 1, 3, 0.5)
    assert st.overall == pytest.approx(0.05) and st.done == [] and st.plan == plan
    st = t.update("Measuring audio", 1.0)
    assert st.overall == pytest.approx(0.2) and st.done == ["download"] and st.label == "Listen"
    st = t.update("Exporting clip 1/4", 0.25)
    assert st.overall == pytest.approx(0.4) and st.done == ["download", "analyse"]
    st = t.update("Transcribing the clips", 0.5)  # Whisper typing out clips during export: still exporting
    assert st.step == "export" and st.overall == pytest.approx(0.4) and st.step_frac is None
    st = t.update("Exporting clip 2/4", 0.5)
    assert st.overall == pytest.approx(0.6)


def test_a_step_the_plan_didnt_expect_is_fitted_in_and_skipped_steps_count_as_done():
    t = steps.Tracker(["download", "find", "export"], clock=Clock())
    st = t.update("Transcribing speech", 0.1)
    assert st.plan == ["download", "transcript", "find", "export"] and st.done == ["download"]
    st = t.update("Exporting clip 1/2", 0.0)
    assert st.done == ["download", "transcript", "find"] and st.index == 4


def test_time_left_is_estimated_once_theres_something_to_go_on_and_counts_down():
    clock = Clock()
    t = steps.Tracker(None, clock=clock)  # a single-step task (a preview, an update download)
    assert t.update("Rendering preview", 0.01).eta == 0  # too early to say
    clock.t += 30
    st = t.update("Rendering preview", 0.25)
    assert st.overall == 0.25 and st.eta == pytest.approx(90)  # a quarter in 30 s: 90 s to go
    clock.t += 10
    st = t.update("Rendering preview", 0.25)  # no news for 10 s: smoothed, so it doesn't jump to 120 s
    assert 80 < st.eta < 100
    assert t.update("Rendering preview", None).overall is None


def test_status_texts_and_json():
    clock = Clock()
    t = steps.Tracker(["download", "transcript", "export"], {"download": 10, "transcript": 50, "export": 40},
                      clock=clock)
    t.update("Downloading", 1.0)
    clock.t += 60
    st = t.update("Transcribing speech", 0.5)
    assert st.headline() == "Step 2 of 3 · Transcribe"
    assert st.detail() == "Transcribing speech 50%"
    assert st.bar_text() == f"35% · {steps.clock_text(st.eta)}"
    assert st.summary().startswith("Step 2/3 · Transcribing speech 50% · 35% done · about")
    back = steps.Status.from_json(json.loads(json.dumps(st.to_json())))
    assert back.summary() == st.summary() and back.done == ["download"]
    assert steps.Status.from_json("nope") is None and steps.Status.from_json({"bogus": 1}).message == ""


@pytest.mark.parametrize("seconds, text", [(3, "about 5 s left"), (42, "about 40 s left"), (61, "about 1 min left"),
                                           (14 * 60 + 20, "about 14 min left"), (3600, "about 1 h left"),
                                           (4200, "about 1 h 10 min left")])
def test_time_left_reads_naturally(seconds, text):
    assert steps.clock_text(seconds) == text


# ---------------------------------------------------------------- automation writes it


def test_automation_keeps_the_working_videos_status_bar_and_clears_it_after(tmp_path):
    seen = []
    real = FakeEngine.prepare

    def prepare(self, p):
        real(self, p)
        self.progress("Exporting clip 1/3", 0.5)
        seen.append(r.db.get("abcdefghijk"))

    FakeEngine.prepare = prepare
    try:
        r = _runner(tmp_path, _settings(auto_output_dir=str(tmp_path / "out")))
        r.add_manual("https://www.youtube.com/watch?v=abcdefghijk")
        r.process(r.db.next_due(NOW), r.load())
    finally:
        FakeEngine.prepare = real
    bar = seen[0].status_bar
    assert bar.step == "export" and "download" in bar.done and 0 < bar.overall < 1
    assert seen[0].stage == "Exporting clip 1/3 50%"
    done = r.db.get("abcdefghijk")
    assert done.status == "done" and done.progress == "" and done.status_bar is None


def test_automation_says_what_its_checking_between_videos(tmp_path):
    from test_standing import _empty_scan

    shown = []

    def scan(opts, history, log=None, progress=None, cancelled=None, now=None):
        progress("Checking what's popular in US", 0.25)
        shown.append(json.loads(r.db.meta("activity")))
        return _empty_scan(opts, history)

    r = _runner(tmp_path, _settings(), scan=scan)
    r.research(r.load())
    assert shown[0]["text"] == "Checking what's popular in US" and shown[0]["frac"] == 0.25
    assert r.db.meta("activity") == ""  # cleared once the check is done


def test_a_job_saved_by_an_older_version_has_no_status_bar(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    db.add(Job("a", "u", status="working", stage="Downloading 5%"), NOW)
    assert db.get("a").status_bar is None and db.working().id == "a"
    db.update("a", progress="{not json")
    assert db.get("a").status_bar is None
    db.update("a", progress=json.dumps(steps.Status("x", "find", 2, 3, None, 0.5).to_json()), status="done")
    assert db.get("a").status_bar is None  # finished: no bar
    assert db.reset_interrupted() == 0 and db.working() is None
    db.close()


# ---------------------------------------------------------------- the app shows it


def test_labeled_bar_shows_percent_and_time_left(app):
    from ytclip.gui.status_bars import LabeledBar

    bar = LabeledBar()
    bar.set_fraction(0.42)
    assert bar.value() == 420 and bar.format() == "42%" and bar.isTextVisible()
    bar.set_status(steps.Status("x", "find", 2, 3, 0.5, 0.61, 240.0))
    assert bar.value() == 610 and bar.format() == "61% · about 4 min left"
    bar.set_fraction(None)
    assert bar.maximum() == 0  # busy, can't tell how far


def test_automation_tab_shows_the_video_now_its_steps_and_a_bar_in_its_row(app, tmp_path):
    from ytclip.gui.automation import COLUMNS, Automation
    from ytclip.gui.status_bars import BAR_ROLE

    db = JobDB(tmp_path / "jobs.db")
    tab = Automation(lambda: Settings().apply({"youtube_api_key": "k"}), db, launch=lambda: None)
    db.set_meta("runner_pid", str(os.getpid()))
    db.set_meta("heartbeat", time.time())
    tracker = steps.Tracker(["download", "analyse", "transcript", "find", "export"], clock=Clock())
    tracker.update("Downloading", 1.0)
    status = tracker.update("Transcribing speech", 0.4)
    db.add(Job("v1", "https://youtu.be/v1", title="Big podcast", status="working", stage="Transcribing speech 40%",
               progress=json.dumps(status.to_json())), NOW)
    db.add(Job("v2", "https://youtu.be/v2", title="Next one", stage="Waiting"), NOW)
    tab.refresh()
    assert not tab.now.isHidden() and "Big podcast" in tab.now.title.text()
    assert tab.now.right.text() == "Step 3 of 5 · Transcribe"
    assert tab.now.chips.texts()[:4] == ["✓ Download", "✓ Listen", "● Transcribe 40%", "Find clips"]
    assert tab.now.bar.value() == int(status.overall * 1000) and tab.now.detail.text() == "Transcribing speech 40%"
    col = COLUMNS.index("Status")
    row = next(r for r in range(tab.table.rowCount()) if tab.table.item(r, 0).text() == "Big podcast")
    cell = tab.table.item(row, col)
    assert cell.text().startswith("Working: Step 3/5 · Transcribing speech 40%")
    assert cell.data(BAR_ROLE) == pytest.approx(status.overall)
    other = next(r for r in range(tab.table.rowCount()) if r != row)
    assert tab.table.item(other, col).data(BAR_ROLE) is None

    db.update("v1", status="done", stage="Done", progress="")
    db.set_meta("activity", json.dumps({"text": "“MW4 funny”: Searching YouTube: MW4 funny moments", "frac": 0.3,
                                        "time": time.time()}))
    tab.refresh()
    assert "MW4 funny" in tab.now.title.text() and tab.now.bar.value() == 300
    assert tab.now.detail.text() == "Searching YouTube: MW4 funny moments" and tab.now.chips.isHidden()
    db.set_meta("activity", "")
    tab.refresh()
    assert tab.now.isHidden()
    tab.shutdown()


def test_uploads_get_a_bar_while_one_goes_up(app, tmp_path):
    from ytclip import uploads
    from ytclip.gui.automation import Automation

    db = JobDB(tmp_path / "jobs.db")
    s = Settings().apply({"upload_target": "folder", "upload_folder": str(tmp_path)})
    tab = Automation(lambda: s, db, launch=lambda: None)
    uploads._table(db)
    with db._lock:
        db.con.execute("INSERT INTO uploads (path, state, name) VALUES (?, 'uploading', 'clip 01.mp4')",
                       (str(tmp_path / "a.mp4"),))
    db.set_meta("upload_progress", 0.37)
    tab.refresh()
    assert not tab.upload_bar.isHidden() and tab.upload_bar.value() == 370
    assert tab.upload_bar.format() == "37% of clip 01.mp4"
    tab.shutdown()


def test_studio_queue_items_show_the_step_and_bar(app):
    from ytclip.engine import Project
    from ytclip.gui.main_window import QueueItem
    from ytclip.gui.tasks import Task

    task = Task("Analysing", lambda t: None, plan=["download", "analyse", "find"])
    status = task.status("Measuring audio", 0.5)
    p = Project("https://youtu.be/x")
    p.status = "working"
    item = QueueItem(p)
    item.show_state(p, "Measuring audio", 0.5, status)
    assert item.sub.text() == "Step 2/3 · Measuring audio 50%"
    assert item.bar.value() == int(status.overall * 1000) and not item.bar.isHidden()
    p.status = "queued"
    item.show_state(p)
    assert item.bar.format() == "Waiting"

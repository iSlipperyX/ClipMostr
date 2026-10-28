import os

import pytest
from conftest import NOW

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)  # no Qt or no display libraries

from ytclip.jobs import Job, JobDB  # noqa: E402
from ytclip.settings import Settings  # noqa: E402


def _make(app, tmp_path, **settings):
    from ytclip.gui.automation import Automation

    db = JobDB(tmp_path / "jobs.db")
    launched = []
    s = Settings().apply(settings)
    tab = Automation(lambda: s, db, launch=lambda: launched.append(1))
    return tab, db, launched


def test_empty_state_and_start(app, tmp_path):
    tab, db, launched = _make(app, tmp_path)
    assert not tab.empty.isHidden() and tab.table.isHidden() and tab.pause_btn.isHidden()
    assert "Not running" in tab.status.text() and "No YouTube API key" in tab.status.text()
    tab.start()
    assert launched == [1] and "Starting" in tab.status.text()
    assert tab.start_btn.isHidden()


def test_jobs_show_in_the_table(app, tmp_path):
    tab, db, _ = _make(app, tmp_path, youtube_api_key="k")
    db.add(Job("a", "https://youtu.be/a", title="Done video", channel="Pod", status="done", clips=4,
               permission="On your channel list"), NOW)
    db.add(Job("b", "https://youtu.be/b", title="Broken", status="failed", stage="Gave up", error="ffmpeg died"), NOW)
    db.add(Job("c", "https://youtu.be/c", title="Waiting one", stage="Waiting"), NOW)
    tab.refresh()
    from ytclip.gui.automation import COLUMNS

    col = {name: i for i, name in enumerate(COLUMNS)}
    rows = {tab.table.item(r, 0).text(): r for r in range(tab.table.rowCount())}
    assert tab.table.item(0, 0).text() == "Waiting one"  # waiting work is listed first
    r = rows["Done video"]
    assert tab.table.item(r, col["Status"]).text() == "Done" and tab.table.item(r, col["Clips"]).text() == "4"
    assert tab.table.item(r, col["Why it was picked"]).text() == "On your channel list"
    assert tab.table.isColumnHidden(col["Niche"])  # no niches, so no Niche column
    broken = tab.table.item(rows["Broken"], col["Status"])
    assert broken.text() == "Failed: Gave up" and "ffmpeg died" in broken.toolTip()
    assert tab.counts.text() == "1 waiting · 0 working · 1 done · 1 failed"


def test_running_state_pause_and_stop(app, tmp_path):
    import time

    tab, db, _ = _make(app, tmp_path, youtube_api_key="k")
    db.set_meta("runner_pid", str(os.getpid()))  # a process that is running
    db.set_meta("heartbeat", time.time())
    db.set_meta("state", "Clipping: Big video")
    tab.refresh()
    assert "Running" in tab.status.text() and "Clipping: Big video" in tab.status.text()
    assert tab.start_btn.isHidden() and not tab.stop_btn.isHidden()
    tab.toggle_pause()
    assert db.meta("paused") == "1" and "paused" in tab.status.text()
    assert "Resume" in tab.pause_btn.text()
    tab.stop()
    assert db.meta("stop") == "1"


def test_queue_a_link(app, tmp_path):
    tab, db, _ = _make(app, tmp_path)
    notices = []
    tab.notice.connect(notices.append)
    tab.link.setText("https://www.youtube.com/watch?v=abcdefghijk")
    tab.add_link()
    assert db.get("abcdefghijk").how == "manual" and notices[-1] == "Queued 1 video."
    tab.link.setText("https://www.youtube.com/watch?v=abcdefghijk")
    tab.add_link()
    assert "queued before" in notices[-1]
    tab.link.setText("hello")
    tab.add_link()
    assert "Give a video link" in notices[-1]


def test_try_now_ends_a_sign_in_pause(app, tmp_path):
    import time

    from ytclip import pipeline

    tab, db, _ = _make(app, tmp_path, youtube_api_key="k")
    tab.refresh()
    assert tab.try_btn.isHidden()
    pipeline.record_block(db, "Sign in to confirm you're not a bot", time.time())
    db.add(Job("a", "https://youtu.be/a", title="Held", stage=pipeline.SIGN_IN_WAIT,
               next_try=db.meta_float("blocked_until")), time.time())
    tab.refresh()
    assert not tab.try_btn.isHidden() and "no downloads until" in tab.status.text()  # shown even when stopped
    notices = []
    tab.notice.connect(notices.append)
    tab.try_btn.click()
    assert not pipeline.paused_until(db, time.time()) and db.get("a").next_try == 0
    assert notices[-1] == "Trying YouTube again now with 1 waiting video. Press Start to run automation."
    assert tab.try_btn.isHidden() and "no downloads until" not in tab.status.text()


def test_restarts_after_an_update(app, tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    db.set_meta("resume_after_update", "1")
    tab, db, launched = _make(app, tmp_path)
    assert launched == [1] and db.meta("resume_after_update") == "0"


def test_settings_dialog_automation_page(app):
    from PySide6.QtWidgets import QTabWidget

    from ytclip.gui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(Settings(), tab="Automation")
    tabs = dlg.findChild(QTabWidget)
    assert tabs.tabText(tabs.currentIndex()) == "Automation"
    assert dlg._editors["auto_preset"].get() == "Shorts / TikTok / Reels"
    dlg._editors["auto_layout"].widget.setCurrentIndex(dlg._editors["auto_layout"].widget.findData("Gameplay below"))
    assert dlg.result_settings.auto_layout == "Gameplay below"


def test_settings_dialog_keeps_the_longest_at_least_the_shortest(app):
    from ytclip.gui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(Settings(min_len=20, max_len=58))
    dlg._editors["min_len"].widget.setValue(60)
    assert (dlg.result_settings.min_len, dlg.result_settings.max_len) == (60, 60)
    assert dlg._editors["max_len"].get() == 60
    dlg._editors["max_len"].widget.setValue(45)
    assert (dlg.result_settings.min_len, dlg.result_settings.max_len) == (45, 45)

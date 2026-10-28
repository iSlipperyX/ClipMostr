import json
import os
import time

import pytest
from conftest import NOW, api_item

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)  # no Qt or no display libraries

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from ytclip import cleanup, niches  # noqa: E402
from ytclip.jobs import Job, JobDB  # noqa: E402
from ytclip.settings import Settings  # noqa: E402


class Host:
    """Stands in for the main window: owns the settings the tab reads and writes."""

    def __init__(self, **overrides):
        self.settings = Settings().apply({"research_auto": False} | overrides)
        self.changes = []

    def get(self):
        return self.settings

    def change(self, name, value):
        self.changes.append((name, value))
        self.settings = self.settings.apply({name: value})


def _tab(app, tmp_path, **overrides):
    from ytclip.gui.niches import Niches

    host = Host(**overrides)
    db = JobDB(tmp_path / "jobs.db")
    tab = Niches(host.get, host.change, db)
    notices = []
    tab.notice.connect(notices.append)
    return tab, host, db, notices


def test_first_niche_takes_the_channel_list_along(app, tmp_path):
    tab, host, _, notices = _tab(app, tmp_path, pick_channels="@Pod # ok", pick_searches="cats")
    assert tab.split.isHidden() and not tab.empty.isHidden()
    assert "move into the first niche" in tab.empty_text.text()
    tab.add()
    [n] = niches.load(host.settings)
    assert n.name == "My first niche" and n.channels == "@Pod # ok" and n.searches == "cats"
    assert host.settings.pick_channels == "" and host.settings.pick_searches == ""
    assert "channel list" in notices[0]
    assert not tab.split.isHidden() and tab.name.text() == "My first niche"
    tab.add()
    assert [x.name for x in niches.load(host.settings)] == ["My first niche", "New niche"]


def test_editing_a_niche_saves_it(app, tmp_path):
    tab, host, _, _ = _tab(app, tmp_path)
    tab.add()
    tab.name.setText("Gaming")
    tab.name.editingFinished.emit()
    tab.account.setText(" @gameclips ")
    tab.daily.setValue(4)
    tab.channels.setPlainText("@GameChan")
    tab.searches.setPlainText("minecraft highlights")
    tab.keywords.setText("fortnite")
    gaming = next(i for i in range(tab.categories.count()) if tab.categories.item(i).data(Qt.UserRole) == "20")
    tab.categories.item(gaming).setCheckState(Qt.Checked)
    tab.preset.setCurrentIndex(tab.preset.findData("Gaming / action"))
    tab.preset.activated.emit(tab.preset.currentIndex())
    tab.view.setCurrentIndex(tab.view.findData("Gameplay below"))
    tab.view.activated.emit(tab.view.currentIndex())
    tab.instructions.setPlainText("clutch plays")
    tab.enabled.setChecked(False)
    [n] = niches.load(host.settings)
    assert (n.name, n.account, n.daily, n.channels, n.searches, n.keywords, n.categories) == (
        "Gaming", "@gameclips", 4, "@GameChan", "minecraft highlights", "fortnite", "20")
    assert (n.preset, n.view, n.instructions, n.enabled) == ("Gaming / action", "Gameplay below", "clutch plays",
                                                             False)
    assert tab.list.item(0).text().startswith("Gaming\n@gameclips · 1 channel · 1 search · trending\nOff")


def test_text_still_being_typed_is_saved_before_settings_are_copied(app, tmp_path):
    """Opening Settings with Ctrl+, (or closing the app) mustn't lose a name or folder mid-edit."""
    tab, host, _, _ = _tab(app, tmp_path, niches=[{"id": "a", "name": "Gaming"}])
    tab.name.setText("Games")  # typed, but the box hasn't been left yet
    tab.stack_folder.setText("D:/gameplay")
    tab.commit()
    [n] = niches.load(host.settings)
    assert (n.name, n.stack_folder) == ("Games", "D:/gameplay")


def test_the_account_text_doesnt_promise_tiktok_manager_sorts_by_it(app, tmp_path):
    tab, *_ = _tab(app, tmp_path, niches=[{"id": "a", "name": "Gaming"}])
    assert "can send" not in tab.account.toolTip() and "can send" not in tab.hint.text()
    assert "doesn't queue clips by" in tab.account.toolTip()


def test_names_must_be_there_and_be_different(app, tmp_path):
    tab, host, _, notices = _tab(app, tmp_path, niches=[{"id": "a", "name": "Gaming"}, {"id": "b", "name": "Pods"}])
    tab.list.setCurrentRow(1)
    tab.name.setText("gaming")
    tab.name.editingFinished.emit()
    assert tab.name.text() == "Pods" and "already a niche called gaming" in notices[-1]
    tab.name.setText("   ")
    tab.name.editingFinished.emit()
    assert tab.name.text() == "Pods" and [n.name for n in niches.load(host.settings)] == ["Gaming", "Pods"]


def test_renaming_keeps_the_id_and_says_where_old_clips_are(app, tmp_path):
    tab, host, _, notices = _tab(app, tmp_path, niches=[{"id": "a", "name": "Gaming"}],
                                 auto_output_dir=str(tmp_path / "auto"))
    (tmp_path / "auto" / "Gaming").mkdir(parents=True)
    (tmp_path / "auto" / "Gaming" / "clip.mp4").write_bytes(b"x")
    tab.name.setText("Games")
    tab.name.editingFinished.emit()
    assert niches.find(host.settings, "a").name == "Games"
    assert "clips already made stay in" in notices[-1]


def test_warnings_point_out_problems(app, tmp_path):
    tab, host, _, _ = _tab(app, tmp_path, pick_daily=2, niches=[
        {"id": "a", "name": "Gaming", "daily": 3, "channels": "@Same\nnot a channel!", "searches": "cats"},
        {"id": "b", "name": "Pods", "daily": 2, "channels": "@same", "searches": "Cats"},
        {"id": "c", "name": "Empty"}])
    text = tab.warnings.text()
    assert "add up to 7 videos a day, but at most 2" in text and "raise:7" in text
    assert "No YouTube" not in text and "YouTube Data API key" in text
    assert "Empty has nothing to find videos with yet" in text
    assert "\"not a channel!\" isn't a channel link" in text
    assert "@same is in Gaming and Pods" in text.replace("@Same", "@same")
    assert "The search \"Cats\" is in Gaming and Pods" in text
    tab._link("raise:7")
    assert host.settings.pick_daily == 7 and "add up to" not in tab.warnings.text()


def test_the_list_shows_how_today_is_going(app, tmp_path):
    tab, host, db, _ = _tab(app, tmp_path, niches=[{"id": "a", "name": "Gaming", "daily": 3, "channels": "@G"}])
    db.add(Job("q", "u", niche="a", status="queued"), time.time())
    db.add(Job("d", "u", niche="a", status="done", clips=6), time.time())
    tab._refresh_stats()
    assert "Today 0/3 · 1 waiting · 6 clips this week" in tab.list.item(0).text()
    assert "1 niche · 0 picked today · up to 3 a day" in tab.summary.text()


def test_deleting_takes_its_waiting_videos_out_of_the_queue(app, tmp_path, monkeypatch):
    tab, host, db, _ = _tab(app, tmp_path, niches=[{"id": "a", "name": "Gaming", "channels": "@G"},
                                                   {"id": "b", "name": "Pods", "channels": "@P", "searches": "talk"}])
    db.add(Job("q", "u", niche="a", status="queued"), NOW)
    db.add(Job("w", "u", niche="a", status="working"), NOW)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: (asked.append(a[2]), QMessageBox.Yes)[1])
    tab.delete()
    assert "Its 1 video waiting to be clipped comes out of the queue" in asked[0]
    assert db.get("q") is None and db.get("w") is not None
    assert [n.name for n in niches.load(host.settings)] == ["Pods"] and tab.name.text() == "Pods"
    tab.delete()  # the last one: its sources go back to the channel list
    assert "It's your last niche" in asked[1]
    assert niches.load(host.settings) == [] and host.settings.pick_channels == "@P"
    assert host.settings.pick_searches == "talk" and not tab.empty.isHidden()


def test_saying_no_keeps_the_niche(app, tmp_path, monkeypatch):
    tab, host, _, _ = _tab(app, tmp_path, niches=[{"id": "a", "name": "Gaming"}])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    tab.delete()
    assert len(niches.load(host.settings)) == 1


# ---------------------------------------------------------------- other tabs


def test_research_shows_and_filters_by_niche(app):
    from ytclip.gui.research import NICHE_COLUMN, Research
    from ytclip.research import ScanResult
    from ytclip.trending import video_from_api

    host = Host(youtube_api_key="k", pick_from="any", pick_min_score=0,
                niches=[{"id": "g", "name": "Gaming", "keywords": "gaming"}])
    tab = Research(host.get, host.change)
    videos = [video_from_api(api_item(f"v{i}", title=t, channel=f"C{i}")) for i, t in
              enumerate(["Big gaming moment", "Calm news"])]
    tab._show_result(ScanResult(videos, NOW, ["US"]))
    assert not tab.table.isColumnHidden(NICHE_COLUMN)
    names = {tab._video_at(r).id: tab.table.item(r, NICHE_COLUMN).text() for r in range(2)}
    assert names == {"v0": "Gaming", "v1": "–"}
    assert tab.decisions["v1"].rule == "niche"
    tab.niche_filter.setCurrentIndex(tab.niche_filter.findData("g"))
    tab._apply_filter()
    assert [tab._video_at(r).id for r in range(2) if not tab.table.isRowHidden(r)] == ["v0"]
    host.settings = host.settings.apply({"niches": []})
    tab.settings_changed()
    assert tab.table.isColumnHidden(NICHE_COLUMN) and tab.niche_filter.isHidden()


def test_automation_queues_a_link_for_a_niche(app, tmp_path):
    from ytclip.gui.automation import COLUMNS, Automation

    host = Host(niches=[{"id": "g", "name": "Gaming"}])
    db = JobDB(tmp_path / "jobs.db")
    tab = Automation(host.get, db, launch=lambda: None)
    assert [tab.niche.itemText(i) for i in range(tab.niche.count())] == ["Gaming", "No niche"]
    tab.link.setText("https://youtu.be/abcdefghijk")
    tab.add_link()
    assert db.get("abcdefghijk").niche == "g"
    tab.refresh()
    assert tab.table.item(0, COLUMNS.index("Niche")).text() == "Gaming"
    host.settings = host.settings.apply({"niches": []})
    tab.niches_changed()
    tab.refresh()
    assert tab.table.item(0, COLUMNS.index("Niche")).text() == "(deleted niche)"


def test_library_filters_by_niche(app, tmp_path):
    from ytclip.gui.library import Library

    out = tmp_path / "out"
    for name, auto in (("a", {"niche_id": "g", "niche": "Gaming", "account": "@gameclips"}), ("b", None),
                       ("c", {"permission": "x"}), ("d", {"niche_id": "old", "niche": "Cooking"})):
        folder = out / name
        folder.mkdir(parents=True)
        meta = {"video": {"title": name.upper(), "channel": "Chan"}, "clips": [{"rank": 1, "file": "01.mp4"}]}
        if auto:
            meta["automation"] = auto
        (folder / "clips.json").write_text(json.dumps(meta), encoding="utf-8")
    host = Host(output_dir=str(out), niches=[{"id": "g", "name": "Gaming"}])
    lib = Library(host.get)
    lib.refresh()
    lines = [lib.videos.item(i).text() for i in range(lib.videos.count())]
    assert len(lines) == 4 and "A\nChan · Gaming (@gameclips) · 1 clips" in lines
    assert "D\nChan · Cooking · 1 clips" in lines  # a deleted niche: the name it had
    lib.niche.setCurrentIndex(lib.niche.findData("g"))
    lib._fill()
    assert lib.videos.count() == 1 and lib.videos.item(0).text().startswith("A\n")
    lib.niche.setCurrentIndex(lib.niche.findData("studio"))
    lib._fill()
    assert lib.videos.count() == 1 and lib.videos.item(0).text().startswith("B\n")


def test_settings_dialog_hides_the_global_lists_with_niches(app):
    from ytclip.gui.settings_dialog import SettingsDialog

    with_niches = SettingsDialog(Settings().apply({"niches": [{"id": "g", "name": "Gaming"}]}))
    assert "pick_channels" not in with_niches._editors and "pick_searches" not in with_niches._editors
    with_niches._reset()
    assert niches.load(with_niches.result_settings)[0].name == "Gaming"  # a reset keeps your niches
    assert "pick_channels" in SettingsDialog(Settings())._editors


# ---------------------------------------------------------------- the clean-up window


def _wait(app, dlg, timeout=5.0):
    end = time.time() + timeout
    while (dlg._job is not None or dlg.plan is None) and time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def test_cleanup_window_previews_then_cleans_what_is_ticked(app, tmp_path):
    from ytclip.gui.cleanup_dialog import CleanupDialog

    old = time.time() - 30 * 86400
    files = []
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        f = tmp_path / name
        f.write_bytes(b"x" * 2048)
        os.utime(f, (old, old))
        files.append(f)
    plan = cleanup.Plan(items=[cleanup.Item("rejected", files[0], 2048, title="Clip A"),
                               cleanup.Item("rejected", files[1], 2048, title="Clip B"),
                               cleanup.Item("downloads", files[2], 2048, title="c.mp4")],
                        notes=["A note"], kept_for_tm=3, tm_found=False)
    looked = []

    def look(s, now, busy=(), older_than_days=0):
        looked.append((set(busy), older_than_days))
        return plan

    host = Host()
    dlg = CleanupDialog(host.get, host.change, lambda: {"busyid"}, look=look)
    _wait(app, dlg)
    assert looked == [({"busyid"}, 0)]
    app.processEvents()
    assert dlg.clean_btn.isEnabled() and dlg.clean_btn.text() == "Clean up 2 items (4 KB)"  # ready once it has looked
    kinds = [dlg.tree.topLevelItem(i).data(0, Qt.UserRole) for i in range(dlg.tree.topLevelItemCount())]
    assert "posted" not in kinds and "removed" not in kinds  # no TikTok Manager here
    rejected = dlg.tree.topLevelItem(kinds.index("rejected"))
    downloads = dlg.tree.topLevelItem(kinds.index("downloads"))
    assert rejected.checkState(0) == Qt.Checked and downloads.checkState(0) == Qt.Unchecked
    assert rejected.text(1) == "2" and rejected.text(2) == "4 KB"
    assert "A note" in dlg.notes.text() and "3 clips stay because TikTok Manager" in dlg.notes.text()
    rejected.child(1).setCheckState(0, Qt.Unchecked)  # keep clip B
    app.processEvents()
    assert dlg.clean_btn.text() == "Clean up 1 item (2 KB)"
    cleaned = []
    dlg.cleaned.connect(cleaned.append)
    dlg.trash.setChecked(False)
    dlg.clean()
    assert not files[0].exists() and files[1].exists() and files[2].exists()
    assert cleaned == ["Cleared 1 rejected clip (2 KB freed)."]
    _wait(app, dlg)
    rejected = dlg.tree.topLevelItem(kinds.index("rejected"))
    assert rejected.child(1).checkState(0) == Qt.Unchecked  # clip B stays unticked when it looks again
    assert files[1] not in [it.path for it in dlg.selected()]
    dlg.tree.topLevelItem(kinds.index("partial")).setCheckState(0, Qt.Unchecked)
    dlg.auto.setChecked(True)
    assert dlg.days.value() == 3  # the daily clean-up never takes things under a day old
    dlg.days.setValue(5)
    dlg.done(0)
    assert host.settings.cleanup_auto and host.settings.cleanup_days == 5
    assert host.settings.cleanup_kinds == "previews, rejected, posted, removed"
    dlg.dispose()


def _rows(dlg):
    return {dlg.tree.topLevelItem(i).data(0, Qt.UserRole): dlg.tree.topLevelItem(i)
            for i in range(dlg.tree.topLevelItemCount())}


def test_cleanup_window_keeps_single_ticks_and_saves_what_it_shows(app, tmp_path):
    from ytclip.gui.cleanup_dialog import CleanupDialog

    files = []
    for name in ("v1.mp4", "v2.mp4", "v3.mp4", "r1.mp4", "r2.mp4"):
        files.append(tmp_path / name)
        files[-1].write_bytes(b"x" * 1024)
    downloads = [cleanup.Item("downloads", f, 1024, title=f.name) for f in files[:3]]
    rejected = [cleanup.Item("rejected", f, 1024, title=f.name) for f in files[3:]]
    plans = [cleanup.Plan(items=downloads + rejected, tm_found=False)]
    host = Host()
    dlg = CleanupDialog(host.get, host.change, set, look=lambda s, now, busy=(), older_than_days=0: plans[-1])
    _wait(app, dlg)
    rows = _rows(dlg)
    assert rows["downloads"].checkState(0) == Qt.Unchecked  # off to start with
    rows["downloads"].child(0).setCheckState(0, Qt.Checked)  # just the biggest download
    rows["rejected"].child(1).setCheckState(0, Qt.Unchecked)  # and keep one rejected clip
    app.processEvents()
    assert dlg.clean_btn.text() == "Clean up 2 items (2 KB)"
    dlg.look()  # say the age changed: the window looks again
    _wait(app, dlg)
    rows = _rows(dlg)
    assert [rows["downloads"].child(i).checkState(0) for i in range(3)] == [Qt.Checked, Qt.Unchecked, Qt.Unchecked]
    assert {it.path for it in dlg.selected()} == {files[0], files[3]}  # the other downloads stay unticked
    dlg.trash.setChecked(False)
    plans.append(cleanup.Plan(items=downloads[1:] + rejected[1:], tm_found=False))  # what's left after
    dlg.clean()
    _wait(app, dlg)
    rows = _rows(dlg)
    assert dlg.selected() == [] and not dlg.clean_btn.isEnabled()
    assert rows["rejected"].checkState(0) == Qt.Unchecked  # only the clip you kept is left
    dlg.auto.setChecked(True)
    dlg.done(0)
    assert "rejected" not in host.settings.cleanup_kinds  # the daily run doesn't take the clip you kept
    assert "downloads" not in host.settings.cleanup_kinds
    dlg.dispose()


def test_closing_the_cleanup_window_never_waits_for_a_look(app):
    import threading

    from ytclip.gui.cleanup_dialog import CleanupDialog

    release, finished = threading.Event(), threading.Event()

    def slow(s, now, busy=(), older_than_days=0):
        release.wait(5)
        finished.set()
        return cleanup.Plan(tm_found=False)

    host = Host()
    dlg = CleanupDialog(host.get, host.change, set, look=slow)
    start = time.time()
    dlg.done(0)
    dlg.dispose()
    app.processEvents()
    assert time.time() - start < 1
    release.set()  # the look ends after the window is gone: nothing is left to tell
    assert finished.wait(5)
    for _ in range(5):
        app.processEvents()
        time.sleep(0.06)


def test_quitting_while_the_cleanup_window_still_looks_doesnt_crash(tmp_path):
    """A look running when ClipMonster quits is left behind; a running QThread would abort the app."""
    import subprocess
    import sys
    from pathlib import Path

    script = tmp_path / "quit.py"
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from PySide6.QtWidgets import QApplication\n"
        "from ytclip import cleanup\n"
        "from ytclip.gui.cleanup_dialog import CleanupDialog\n"
        "from ytclip.settings import Settings\n"
        "app = QApplication([])\n"
        "def slow(s, now, busy=(), older_than_days=0):\n"
        "    time.sleep(5)\n"
        "    return cleanup.Plan()\n"
        "dlg = CleanupDialog(Settings, lambda name, value: None, set, look=slow)\n"
        "dlg.dispose()\n"
        "app.processEvents()\n"
        "sys.exit(0)\n", encoding="utf-8")
    env = os.environ | {"QT_QPA_PLATFORM": "offscreen", "APPDATA": str(tmp_path), "LOCALAPPDATA": str(tmp_path),
                        "XDG_CONFIG_HOME": str(tmp_path)}
    done = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr

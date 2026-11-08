import json
import os
import time

import pytest
from conftest import NOW, api_item

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)  # no Qt or no display libraries

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from ytclip import cleanup, categories  # noqa: E402
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


@pytest.fixture(autouse=True)
def new_category_answers(monkeypatch):
    """"New category" asks for a name and an account first: here it takes the name it suggests and the
    first account (made if there's none), as pressing Create straight away would."""
    from ytclip.gui import categories_tab
    from ytclip.posting.accounts import Accounts

    def exec_(dlg):
        if dlg.account.findData("") >= 0 and not any(dlg.account.itemData(i) not in ("", None, dlg.account.itemData(
                dlg.account.count() - 1)) for i in range(dlg.account.count())):
            accs = Accounts.load()
            a = accs.add("")
            a.username = "first"
            accs.save()
            dlg._fill("")
        first = next(i for i in range(dlg.account.count())
                     if dlg.account.itemData(i) not in ("", None, categories_tab.Categories.ADD_ACCOUNT))
        dlg.account.setCurrentIndex(first)
        dlg._check()
        assert dlg.buttons.button(dlg.buttons.StandardButton.Ok).isEnabled()
        return 1

    monkeypatch.setattr(categories_tab.NewCategoryDialog, "exec", exec_)


def _tab(app, tmp_path, **overrides):
    from ytclip.gui.categories_tab import Categories

    host = Host(**overrides)
    db = JobDB(tmp_path / "jobs.db")
    tab = Categories(host.get, host.change, db)
    notices = []
    tab.notice.connect(notices.append)
    return tab, host, db, notices


def test_first_category_takes_the_channel_list_along(app, tmp_path):
    tab, host, _, notices = _tab(app, tmp_path, pick_channels="@Pod # ok", pick_searches="cats")
    assert tab.split.isHidden() and not tab.empty.isHidden()
    assert "move into the first category" in tab.empty_text.text()
    tab.add()
    [n] = categories.load(host.settings)
    assert n.name == "My first category" and n.channels == "@Pod # ok" and n.searches == "cats"
    assert host.settings.pick_channels == "" and host.settings.pick_searches == ""
    assert "channel list" in notices[0]
    assert not tab.split.isHidden() and tab.name.text() == "My first category"
    assert n.account_id and n.account == "@first" and n.on  # made with its account
    tab.add()
    assert [x.name for x in categories.load(host.settings)] == ["My first category", "New category"]


def test_editing_a_category_saves_it(app, tmp_path):
    tab, host, _, _ = _tab(app, tmp_path)
    tab.add()
    tab.name.setText("Gaming")
    tab.name.editingFinished.emit()
    from ytclip.posting.accounts import Accounts

    accs = Accounts.load()
    acc = accs.add("")
    acc.username = "gameclips"
    accs.save()
    tab.accounts_changed()
    i = tab.account.findData(acc.id)
    tab.account.setCurrentIndex(i)
    tab.account.activated.emit(i)
    tab.daily.setValue(4)
    tab.channels.setPlainText("@GameChan")
    tab.searches.setPlainText("minecraft highlights")
    tab.keywords.setText("fortnite")
    gaming = next(i for i in range(tab.yt_categories.count()) if tab.yt_categories.item(i).data(Qt.UserRole) == "20")
    tab.yt_categories.item(gaming).setCheckState(Qt.Checked)
    tab.preset.setCurrentIndex(tab.preset.findData("Gaming / action"))
    tab.preset.activated.emit(tab.preset.currentIndex())
    tab.view.setCurrentIndex(tab.view.findData("Gameplay below"))
    tab.view.activated.emit(tab.view.currentIndex())
    tab.instructions.setPlainText("clutch plays")
    tab.enabled.setChecked(False)
    [n] = categories.load(host.settings)
    assert n.account_id == acc.id
    assert (n.name, n.account, n.daily, n.channels, n.searches, n.keywords, n.yt_categories) == (
        "Gaming", "@gameclips", 4, "@GameChan", "minecraft highlights", "fortnite", "20")
    assert (n.preset, n.view, n.instructions, n.enabled) == ("Gaming / action", "Gameplay below", "clutch plays",
                                                             False)
    assert tab.list.item(0).text().startswith("Gaming\n@gameclips · 1 channel · 1 search · trending\nOff")


def test_text_still_being_typed_is_saved_before_settings_are_copied(app, tmp_path):
    """Opening Settings with Ctrl+, (or closing the app) mustn't lose a name or folder mid-edit."""
    tab, host, _, _ = _tab(app, tmp_path, categories=[{"id": "a", "name": "Gaming", "account_id": "acc1"}])
    tab.name.setText("Games")  # typed, but the box hasn't been left yet
    tab.stack_folder.setText("D:/gameplay")
    tab.commit()
    [n] = categories.load(host.settings)
    assert (n.name, n.stack_folder) == ("Games", "D:/gameplay")


def test_add_an_account_from_the_category(app, tmp_path):
    from ytclip.posting.accounts import Accounts

    tab, host, _, _ = _tab(app, tmp_path, categories=[{"id": "a", "name": "Gaming"}])
    assert tab.account.itemText(0).startswith("No accounts yet") and tab.account.currentData() == ""
    asked = []

    def wizard(parent, category_id):  # the account wizard adds and signs in an account
        asked.append(category_id)
        accs = Accounts.load()
        a = accs.add("")
        a.username, a.logged_in = "newone", True
        accs.save()
        return a.id

    tab.onboard = wizard
    i = tab.account.findData(tab.ADD_ACCOUNT)
    tab.account.setCurrentIndex(i)
    tab.account.activated.emit(i)
    [n] = categories.load(host.settings)
    assert asked == ["a"] and n.account == "@newone" and n.on and tab.account.currentData() == n.account_id


def test_a_category_without_an_account_is_held_and_says_so(app, tmp_path):
    from ytclip.posting.accounts import Accounts

    accs = Accounts.load()
    a = accs.add("")
    a.username = "gameclips"
    accs.save()
    tab, host, _, _ = _tab(app, tmp_path, categories=[{"id": "a", "name": "Gaming", "channels": "@G"}])
    assert tab.account.itemText(0) == "⚠ Pick an account" and tab.account.currentData() == ""
    assert "needs an account" in tab.list.item(0).text() and "Needs an account" in tab.list.item(0).text()
    assert "need" in tab.warnings.text() and "Pick one" in tab.warnings.text()
    assert categories.active(host.settings) == [] and categories.paused_ids(host.settings) == {"a"}
    i = tab.account.findData(a.id)
    tab.account.setCurrentIndex(i)
    tab.account.activated.emit(i)
    [n] = categories.active(host.settings)
    assert n.account == "@gameclips" and "need" not in tab.warnings.text()


def test_a_new_category_needs_a_name_and_an_account(app, tmp_path):
    from ytclip.gui.categories_tab import NewCategoryDialog
    from ytclip.posting.accounts import Accounts

    dlg = NewCategoryDialog("New category", lambda: list(Accounts.load()), [], lambda _cid: "")
    ok = dlg.buttons.button(dlg.buttons.StandardButton.Ok)
    assert not ok.isEnabled() and "account" in dlg.problem.text()
    accs = Accounts.load()
    a = accs.add("")
    a.username = "one"
    accs.save()
    dlg._fill("")
    dlg.account.setCurrentIndex(dlg.account.findData(a.id))
    dlg._check()
    assert ok.isEnabled() and dlg.account_id() == a.id
    dlg.name_edit.setText("  ")
    assert not ok.isEnabled() and "name" in dlg.problem.text()


def test_names_must_be_there_and_be_different(app, tmp_path):
    tab, host, _, notices = _tab(app, tmp_path, categories=[{"id": "a", "name": "Gaming", "account_id": "acc1"}, {"id": "b", "name": "Pods", "account_id": "acc1"}])
    tab.list.setCurrentRow(1)
    tab.name.setText("gaming")
    tab.name.editingFinished.emit()
    assert tab.name.text() == "Pods" and "already a category called gaming" in notices[-1]
    tab.name.setText("   ")
    tab.name.editingFinished.emit()
    assert tab.name.text() == "Pods" and [n.name for n in categories.load(host.settings)] == ["Gaming", "Pods"]


def test_renaming_keeps_the_id_and_says_where_old_clips_are(app, tmp_path):
    tab, host, _, notices = _tab(app, tmp_path, categories=[{"id": "a", "name": "Gaming", "account_id": "acc1"}],
                                 auto_output_dir=str(tmp_path / "auto"))
    (tmp_path / "auto" / "Gaming").mkdir(parents=True)
    (tmp_path / "auto" / "Gaming" / "clip.mp4").write_bytes(b"x")
    tab.name.setText("Games")
    tab.name.editingFinished.emit()
    assert categories.find(host.settings, "a").name == "Games"
    assert "clips already made stay in" in notices[-1]


def test_warnings_point_out_problems(app, tmp_path):
    tab, host, _, _ = _tab(app, tmp_path, pick_daily=2, categories=[
        {"id": "a", "name": "Gaming", "account_id": "acc1", "daily": 3, "channels": "@Same\nnot a channel!", "searches": "cats"},
        {"id": "b", "name": "Pods", "account_id": "acc1", "daily": 2, "channels": "@same", "searches": "Cats"},
        {"id": "c", "name": "Empty", "account_id": "acc1"}])
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
    tab, host, db, _ = _tab(app, tmp_path, categories=[{"id": "a", "name": "Gaming", "account_id": "acc1", "daily": 3, "channels": "@G"}])
    db.add(Job("q", "u", category_id="a", status="queued"), time.time())
    db.add(Job("d", "u", category_id="a", status="done", clips=6), time.time())
    tab._refresh_stats()
    assert "Today 0/3 · 1 waiting · 6 clips this week" in tab.list.item(0).text()
    assert "1 category · 0 picked today · up to 3 a day" in tab.summary.text()


def test_deleting_takes_its_waiting_videos_out_of_the_queue(app, tmp_path, monkeypatch):
    tab, host, db, _ = _tab(app, tmp_path, categories=[{"id": "a", "name": "Gaming", "account_id": "acc1", "channels": "@G"},
                                                   {"id": "b", "name": "Pods", "account_id": "acc1", "channels": "@P", "searches": "talk"}])
    db.add(Job("q", "u", category_id="a", status="queued"), NOW)
    db.add(Job("w", "u", category_id="a", status="working"), NOW)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: (asked.append(a[2]), QMessageBox.Yes)[1])
    tab.delete()
    assert "Its 1 video waiting to be clipped comes out of the queue" in asked[0]
    assert db.get("q") is None and db.get("w") is not None
    assert [n.name for n in categories.load(host.settings)] == ["Pods"] and tab.name.text() == "Pods"
    tab.delete()  # the last one: its sources go back to the channel list
    assert "It's your last category" in asked[1]
    assert categories.load(host.settings) == [] and host.settings.pick_channels == "@P"
    assert host.settings.pick_searches == "talk" and not tab.empty.isHidden()


def test_saying_no_keeps_the_category(app, tmp_path, monkeypatch):
    tab, host, _, _ = _tab(app, tmp_path, categories=[{"id": "a", "name": "Gaming", "account_id": "acc1"}])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    tab.delete()
    assert len(categories.load(host.settings)) == 1


# ---------------------------------------------------------------- other tabs


def test_research_shows_and_filters_by_category(app):
    from ytclip.gui.research import CATEGORY_COLUMN, Research
    from ytclip.research import ScanResult
    from ytclip.trending import video_from_api

    host = Host(youtube_api_key="k", pick_from="any", pick_min_score=0,
                categories=[{"id": "g", "name": "Gaming", "account_id": "acc1", "keywords": "gaming"}])
    tab = Research(host.get, host.change)
    videos = [video_from_api(api_item(f"v{i}", title=t, channel=f"C{i}")) for i, t in
              enumerate(["Big gaming moment", "Calm news"])]
    tab._show_result(ScanResult(videos, NOW, ["US"]))
    assert not tab.table.isColumnHidden(CATEGORY_COLUMN)
    names = {tab._video_at(r).id: tab.table.item(r, CATEGORY_COLUMN).text() for r in range(2)}
    assert names == {"v0": "Gaming", "v1": "–"}
    assert tab.decisions["v1"].rule == "unsorted"
    tab.category_filter.setCurrentIndex(tab.category_filter.findData("g"))
    tab._apply_filter()
    assert [tab._video_at(r).id for r in range(2) if not tab.table.isRowHidden(r)] == ["v0"]
    host.settings = host.settings.apply({"categories": []})
    tab.settings_changed()
    assert tab.table.isColumnHidden(CATEGORY_COLUMN) and tab.category_filter.isHidden()


def test_automation_queues_a_link_for_a_category(app, tmp_path):
    from ytclip.gui.automation import COLUMNS, Automation

    host = Host(categories=[{"id": "g", "name": "Gaming", "account_id": "acc1"}])
    db = JobDB(tmp_path / "jobs.db")
    tab = Automation(host.get, db, launch=lambda: None)
    assert [tab.category_id.itemText(i) for i in range(tab.category_id.count())] == ["Gaming", "No category"]
    tab.link.setText("https://youtu.be/abcdefghijk")
    tab.add_link()
    assert db.get("abcdefghijk").category_id == "g"
    tab.refresh()
    assert tab.table.item(0, COLUMNS.index("Category")).text() == "Gaming"
    host.settings = host.settings.apply({"categories": []})
    tab.categories_changed()
    tab.refresh()
    assert tab.table.item(0, COLUMNS.index("Category")).text() == "(deleted category)"


def test_library_filters_by_category(app, tmp_path):
    from ytclip.gui.library import Library

    out = tmp_path / "out"
    for name, auto in (("a", {"category_id": "g", "category": "Gaming", "account": "@gameclips"}), ("b", None),
                       ("c", {"permission": "x"}), ("d", {"category_id": "old", "category": "Cooking"})):
        folder = out / name
        folder.mkdir(parents=True)
        meta = {"video": {"title": name.upper(), "channel": "Chan"}, "clips": [{"rank": 1, "file": "01.mp4"}]}
        if auto:
            meta["automation"] = auto
        (folder / "clips.json").write_text(json.dumps(meta), encoding="utf-8")
    host = Host(output_dir=str(out), categories=[{"id": "g", "name": "Gaming", "account_id": "acc1"}])
    lib = Library(host.get)
    lib.refresh()
    lines = [lib.videos.item(i).text() for i in range(lib.videos.count())]
    assert len(lines) == 4 and "A\nChan · Gaming (@gameclips) · 1 clips" in lines
    assert "D\nChan · Cooking · 1 clips" in lines  # a deleted category: the name it had
    lib.category_id.setCurrentIndex(lib.category_id.findData("g"))
    lib._fill()
    assert lib.videos.count() == 1 and lib.videos.item(0).text().startswith("A\n")
    lib.category_id.setCurrentIndex(lib.category_id.findData("studio"))
    lib._fill()
    assert lib.videos.count() == 1 and lib.videos.item(0).text().startswith("B\n")


def test_settings_dialog_hides_the_global_lists_with_categories(app):
    from ytclip.gui.settings_dialog import SettingsDialog

    with_categories = SettingsDialog(Settings().apply({"categories": [{"id": "g", "name": "Gaming", "account_id": "acc1"}]}))
    assert "pick_channels" not in with_categories._editors and "pick_searches" not in with_categories._editors
    with_categories._reset()
    assert categories.load(with_categories.result_settings)[0].name == "Gaming"  # a reset keeps your categories
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
                        notes=["A note"], kept_for_posting=3)
    looked = []

    def look(s, now, busy=(), older_than_days=0):
        looked.append((set(busy), older_than_days))
        return plan

    host = Host()
    host.settings = host.settings.copy(cleanup_auto=False)  # the daily clean-up is off
    dlg = CleanupDialog(host.get, host.change, lambda: {"busyid"}, look=look)
    _wait(app, dlg)
    assert looked == [({"busyid"}, 0)]
    app.processEvents()
    assert dlg.clean_btn.isEnabled() and dlg.clean_btn.text() == "Clean up 2 items (4 KB)"  # ready once it has looked
    kinds = [dlg.tree.topLevelItem(i).data(0, Qt.UserRole) for i in range(dlg.tree.topLevelItemCount())]
    assert "posted" in kinds and "removed" in kinds and "uploaded" not in kinds
    rejected = dlg.tree.topLevelItem(kinds.index("rejected"))
    downloads = dlg.tree.topLevelItem(kinds.index("downloads"))
    assert rejected.checkState(0) == Qt.Checked and downloads.checkState(0) == Qt.Unchecked
    assert rejected.text(1) == "2" and rejected.text(2) == "4 KB"
    assert "A note" in dlg.notes.text() and "3 clips stay because they are still waiting to post" in dlg.notes.text()
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
    plans = [cleanup.Plan(items=downloads + rejected)]
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
    plans.append(cleanup.Plan(items=downloads[1:] + rejected[1:]))  # what's left after
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
        return cleanup.Plan()

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


def test_standing_requests_are_edited_and_move_with_the_first_and_last_category(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    tab, host, _, _ = _tab(app, tmp_path, pick_finds="MW4 with good comedy")
    tab.add()
    assert host.settings.pick_finds == "" and tab._category().finds == "MW4 with good comedy"
    tab.finds.setPlainText("MW4 with good comedy\nWarzone funny moments")
    assert host.settings.categories[0]["finds"].endswith("Warzone funny moments")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    tab.delete()
    assert host.settings.pick_finds == "MW4 with good comedy\nWarzone funny moments"


def test_the_header_counts_categories_in_plain_english(app, tmp_path):
    tab, *_ = _tab(app, tmp_path, categories=[{"id": "a", "name": "A", "account_id": "x"}])
    assert "1 category ·" in tab.summary.text()
    tab, *_ = _tab(app, tmp_path, categories=[{"id": "a", "name": "A"}, {"id": "b", "name": "B"}])
    assert "2 categories ·" in tab.summary.text()  # not "categorys"
    assert tab.list.item(0).text().splitlines()[1].startswith("⚠ needs an account")


def test_the_list_names_the_account_even_before_its_name_is_stored(app, tmp_path):
    from ytclip.posting.accounts import Accounts

    accs = Accounts.load()
    a = accs.add("")
    a.username = "gamingclips"
    accs.save()
    tab, *_ = _tab(app, tmp_path, categories=[{"id": "g", "name": "Gaming", "account_id": a.id}])
    assert tab.list.item(0).text().splitlines()[1].startswith("@gamingclips")


def test_the_note_under_the_account_leaves_no_gap(app, tmp_path):
    """A wrapped note in the scrolling form is as tall as its text at its real width: one line when it
    fits on one, more when the window is narrow."""
    from ytclip.posting.accounts import Accounts

    accs = Accounts.load()
    a = accs.add("")
    a.username, a.logged_in = "gamingclips", True
    accs.save()
    tab, *_ = _tab(app, tmp_path, categories=[{"id": "g", "name": "Gaming", "account_id": a.id}])
    note = tab.account_note
    heights = {}
    for width in (1280, 640):
        tab.resize(width, 820)
        tab.show()
        for _ in range(4):
            app.processEvents()
        assert note.isVisible() and "Accounts tab" in note.text()
        heights[width] = note.height()
        assert note.height() == note.heightForWidth(note.width())  # no empty lines under the text
        assert tab.enabled.geometry().top() - note.geometry().bottom() < 16  # the next row follows it
    line = note.fontMetrics().lineSpacing()
    assert heights[1280] < 2 * line <= heights[640]
    tab.close()

import pytest
from conftest import NOW

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)  # no Qt or no display libraries

from ytclip import orders  # noqa: E402
from ytclip.jobs import Job, JobDB  # noqa: E402
from ytclip.settings import Settings  # noqa: E402

GAMING = {"id": "game01", "name": "Gaming", "account": "@gameclips", "channels": "@C0", "daily": 3,
          "min_len": 25, "max_len": 40}
PODS = {"id": "pod01", "name": "Podcasts", "searches": "podcast clips"}


class Holder:
    """Settings as the main window keeps them: changed in place by the tab's change callback."""

    def __init__(self, **kw):
        self.s = Settings().apply({"youtube_api_key": "k"} | kw)

    def __call__(self):
        return self.s

    def change(self, name, value):
        self.s = self.s.apply({name: value})


def _make(app, tmp_path, **kw):
    from ytclip.gui.automation import Automation

    db = JobDB(tmp_path / "jobs.db")
    holder = Holder(**kw)
    tab = Automation(holder, db, launch=lambda: None, change=holder.change)
    return tab, db, holder


def test_each_niche_has_a_row_with_todays_numbers(app, tmp_path):
    tab, db, _ = _make(app, tmp_path, niches=[GAMING, PODS])
    db.add(Job("a", "u", niche="game01", status="queued"), NOW)
    db.add(Job("b", "u", niche="game01", status="done", clips=4), __import__("time").time())
    tab.refresh()
    t = tab.niche_panel.table
    assert t.rowCount() == 2 and t.item(0, 0).text() == "Gaming" and t.item(0, 1).text() == "@gameclips"
    assert t.item(0, 2).text() == "0 of 3" and t.item(0, 3).text() == "1" and t.item(0, 4).text() == "4"
    assert t.item(0, 5).text() == "Picking and clipping"


def test_pausing_a_niche_saves_it_and_holds_it(app, tmp_path):
    tab, db, holder = _make(app, tmp_path, niches=[GAMING, PODS])
    edited = []
    tab.niches_edited.connect(lambda: edited.append(1))
    tab.niche_panel.table.selectRow(0)
    tab.niche_panel.toggle_pause()
    from ytclip import niches

    assert niches.paused_ids(holder()) == {"game01"} and edited == [1]
    assert tab.niche_panel.table.item(0, 5).text() == "Paused"
    assert tab.niche_panel.pause_btn.text().endswith("Resume niche")
    tab.niche_panel.toggle_pause()
    assert niches.paused_ids(holder()) == set()


def test_find_videos_now_asks_the_runner_for_that_niche(app, tmp_path):
    tab, db, _ = _make(app, tmp_path, niches=[GAMING, PODS])
    tab.niche_panel.table.selectRow(1)
    notes = []
    tab.niche_panel.notice.connect(notes.append)
    tab.niche_panel.find_now()
    assert db.meta("scan_now") == "pod01" and "Podcasts" in notes[0]


def test_requests_show_their_progress_and_can_be_cancelled(app, tmp_path):
    tab, db, _ = _make(app, tmp_path, niches=[GAMING])
    from ytclip.pipeline import Runner

    runner = Runner(db, load=lambda: Settings())
    o = runner.place(orders.make(10, 30, 45, niche="game01", links=["https://youtu.be/AAAAAAAAAAA"],
                                 notes="funny fails only"))
    tab.refresh()
    t = tab.request_panel.table
    assert t.rowCount() == 1 and t.item(0, 0).text() == "10 clips of 30–45 s, from your links"
    assert t.item(0, 1).text() == "Gaming" and t.item(0, 2).text() == "0 of 10"
    assert t.item(0, 3).text() == "Working on it: 1 video waiting" and t.item(0, 4).text() == "funny fails only"
    t.selectRow(0)
    assert tab.request_panel.cancel_btn.isEnabled()
    tab.request_panel.cancel()
    assert db.get_order(o.id).status == "cancelled" and db.order_jobs(o.id) == []
    assert t.item(0, 3).text().startswith("Cancelled")


def test_new_request_window_fills_in_the_niches_length_and_checks_input(app, tmp_path):
    from ytclip.gui.requests_panel import RequestDialog

    holder = Holder(niches=[GAMING, PODS])
    dlg = RequestDialog(holder(), "game01")
    assert (dlg.min_len.value(), dlg.max_len.value()) == (25, 40)  # the niche's own length
    assert dlg.warn.isHidden()  # keys are fine for picking; no special requests yet
    dlg.notes.setPlainText("only clutch plays")
    assert "Claude or Gemini key" in dlg.warn.text()
    dlg.from_links.setChecked(True)
    dlg.accept()
    assert dlg.order is None and "at least one link" in dlg.error.text()
    dlg.links.setPlainText("https://youtu.be/AAAAAAAAAAA\nhttps://youtu.be/BBBBBBBBBBB")
    dlg.count.setValue(6)
    dlg.accept()
    o = dlg.order
    assert o.count == 6 and o.niche == "game01" and len(o.links) == 2 and o.notes == "only clutch plays"
    assert (o.min_len, o.max_len) == (25, 40)


def test_placing_a_request_from_the_tab_queues_it(app, tmp_path, monkeypatch):
    from ytclip.gui import requests_panel

    tab, db, _ = _make(app, tmp_path, niches=[GAMING])

    class Accepting(requests_panel.RequestDialog):
        def exec(self):
            self.count.setValue(4)
            self.accept()
            return requests_panel.QDialog.DialogCode.Accepted

    monkeypatch.setattr("ytclip.gui.automation.RequestDialog", Accepting)
    tab.new_request("game01")
    mine = db.orders()
    assert len(mine) == 1 and mine[0].count == 4 and mine[0].niche == "game01" and mine[0].picks
    assert db.meta("scan_now") == "game01" and tab.pages.currentWidget() is tab.request_panel

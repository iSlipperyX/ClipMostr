"""The Sports page (gui/sports_view.py): on the PC's window, asking its own desk, and on a linked Mac, asking the
PC's desk over the link. Both are the same page; what's ticked and made on either is the PC's."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from test_mac_app import shut, wait, window  # noqa: E402,F401 - fixtures and helpers
from test_remote import mac_home, world  # noqa: E402,F401 - fixtures
from test_sports_desk import seed  # noqa: E402
from ytclip.settings import Settings  # noqa: E402
from ytclip.sportsdesk.desk import Desk  # noqa: E402


@pytest.fixture
def page(app, tmp_path):
    from ytclip.gui.sports_view import LocalBackend, SportsView

    s = Settings(categories=[{"id": "h1", "name": "Hoops", "sport": "basketball", "account_id": "a"}])
    desk = Desk(tmp_path / "sports.db", settings=lambda: s)
    seed(desk, 3, highlights=True)
    view = SportsView(LocalBackend(desk))
    view.resize(1280, 760)
    view.show()
    view.desk = desk
    yield view
    view.shutdown()
    view.close()
    view.deleteLater()
    desk.close()


def test_the_page_shows_the_games_and_the_best_plays_of_one(app, page):
    assert wait(app, lambda: page.list.count() == 3)
    assert page.current == "v0"  # the game with highlights is shown first
    panel = page.panel
    assert wait(app, lambda: panel.list.count() == 4)
    assert "Lakers" in panel.title.text()
    assert panel.make_btn.isEnabled() and "2" in panel.make_btn.text()
    assert "3 games" in page.count.text() and "1 ready" in page.count.text()
    assert page.filter.values()["sport"] == "any"


def test_picking_games_and_finding_their_highlights(app, page):
    assert wait(app, lambda: page.list.count() == 3)
    assert not page.analyse_btn.isEnabled()
    page.rows["v1"].tick.setChecked(True)
    page.rows["v2"].tick.setChecked(True)
    assert wait(app, lambda: page.desk.game("v1")["picked"] and page.desk.game("v2")["picked"])
    page.refresh(force=True)
    assert wait(app, lambda: page.analyse_btn.isEnabled() and "2 games" in page.analyse_btn.text())
    page.analyse_btn.click()
    assert wait(app, lambda: page.desk.waiting() == 2)
    page.view_buttons["picked"].click()
    assert wait(app, lambda: page.list.count() == 2)
    page.view_buttons["ready"].click()
    assert wait(app, lambda: page.list.count() == 1)


def test_ticking_plays_and_making_them_into_clips(app, page):
    panel = page.panel
    assert wait(app, lambda: panel.list.count() == 4)
    panel.rows["h3"].tick.setChecked(True)
    assert wait(app, lambda: sum(h["chosen"] for h in page.desk.game("v0")["highlights"]) == 3)
    page.refresh(force=True)
    assert wait(app, lambda: "3" in panel.make_btn.text())
    panel.make_btn.click()
    assert wait(app, lambda: page.desk.game("v0")["status"] == "queued")
    task = page.desk.claim("test:1")
    assert task["kind"] == "make" and task["game"] == "v0" and len(task["payload"]["ids"]) == 3


def test_open_in_studio_hands_over_the_ticked_plays(app, page):
    got = []
    page.open_studio.connect(lambda url, title, clips: got.append((url, title, clips)))
    assert wait(app, lambda: page.panel.list.count() == 4)
    page.panel.studio_btn.click()
    assert got and got[0][0].endswith("v0") and len(got[0][2]) == 2
    assert got[0][2][0]["edited"] and got[0][2][0]["title"] == "Play 1"


def test_a_filter_change_is_saved_on_the_desk(app, page):
    assert wait(app, lambda: page.list.count() == 3)
    page.filter.teams.setText("Lakers, Celtics")
    page.filter.find_btn.click()
    assert wait(app, lambda: page.desk.filter().team_list == ["Lakers", "Celtics"])
    assert wait(app, lambda: page.desk.state()["search"]["status"] == "queued")


def test_the_linked_mac_shows_the_pcs_sports_and_makes_clips_there(app, window, world):  # noqa: F811
    desk = world.host.desk
    seed(desk, 2, highlights=True)
    assert wait(app, lambda: window.session.state == "online")
    window.nav.setCurrentIndex(window.i_sports)
    view = window.sports.view
    assert wait(app, lambda: view.list.count() == 2, timeout=15)
    assert view.backend.where == "your PC" and not view.panel.folder_btn.isVisible()
    assert wait(app, lambda: view.panel.list.count() == 4)
    view.rows["v1"].tick.setChecked(True)
    assert wait(app, lambda: desk.game("v1")["picked"])
    view.panel.make_btn.click()
    assert wait(app, lambda: desk.game("v0")["status"] == "queued")
    world.host.add_event("sports", "Lakers at Celtics: 2 clips made")
    assert wait(app, lambda: any("2 clips made" in t for _, t in window.notify_calls), timeout=20)

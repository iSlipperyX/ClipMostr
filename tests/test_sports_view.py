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


def test_making_a_reel_of_the_best_plays(app, page):
    panel = page.panel
    assert wait(app, lambda: panel.list.count() == 4)
    assert panel.reel_btn.isVisible() and panel.reel_btn.isEnabled()
    panel.reel_btn.click()
    assert wait(app, lambda: page.desk.game("v0")["status"] == "queued")
    task = page.desk.claim("test:1")
    assert task["kind"] == "reel" and task["game"] == "v0" and len(task["payload"]["ids"]) == 2


def test_the_reels_settings_are_kept(app, page):
    assert wait(app, lambda: page.list.count() == 3)
    f = page.filter
    f.reel_voice.setCurrentIndex(f.reel_voice.findData("off"))
    f.reel_order.setCurrentIndex(f.reel_order.findData("timeline"))
    assert wait(app, lambda: (page.desk.filter().reel_voice, page.desk.filter().reel_order) == ("off", "timeline"))


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


# --- the games played: the last 24 hours, by drop-down and by list --------------------------------------------


def played_page(app, page):
    """The page on its "Last 24 hours" view, the stand-in scoreboards read."""
    import time

    from test_telegram_sports import scoreboards

    page.desk.slate_get = scoreboards(time.time())
    page.view_buttons["played"].click()
    p = page.played
    assert wait(app, lambda: p.data is not None and p.list.count() == 5)
    return p


def test_the_last_24_hours_are_listed_with_drop_downs(app, page):
    assert wait(app, lambda: page.list.count() == 3)
    p = played_page(app, page)
    assert p.isVisible() and not page.list.isVisible() and not page.list_foot.isVisible()
    assert [p.sport.itemText(i) for i in range(p.sport.count())] == ["All sports", "🏀  Basketball", "🏒  Ice hockey"]
    assert [p.league.itemData(i) for i in range(p.league.count())] == ["", "NBA", "NHL"]
    games = [p.game.itemText(i) for i in range(p.game.count())]
    assert games[0] == "Pick a game…" and "NBA: Lakers 108, Celtics 112  ·  Final" in games
    assert "5 games · 3 finished · 1 being played" in p.note.text() and "Not read:" in p.note.text()
    assert not p.reel_btn.isEnabled() and not p.find_btn.isEnabled()
    # one sport, then one league: the lists follow
    p.sport.setCurrentIndex(p.sport.findData("hockey"))
    assert p.list.count() == 1 and [p.league.itemData(i) for i in range(p.league.count())] == ["", "NHL"]
    p.sport.setCurrentIndex(0)
    p.league.setCurrentIndex(p.league.findData("NBA"))
    assert p.list.count() == 4 and p.game.itemText(1).startswith("Warriors 88, Nuggets 91")
    # a game still being played is listed but can't be asked for
    live = p.game.findData("nba-402")
    assert not p.game.model().item(live).isEnabled()
    p.list.setCurrentRow(0)
    assert p.chosen == "nba-402" and not p.reel_btn.isEnabled() and "still being played" in p.hint.text()
    # picking in the drop-down picks the row, and the other way round
    p.game.setCurrentIndex(p.game.findData("nba-401"))
    assert p.list.currentItem().data(0x0100) == "nba-401" and p.reel_btn.isEnabled() and not p.hint.isVisible()
    p.list.setCurrentRow(2)
    assert p.game.currentData() == p.chosen == "nba-404"
    # back to the games found: the list and its buttons return
    page.view_buttons["all"].click()
    assert wait(app, lambda: page.list.isVisible() and page.list.count() == 3 and not p.isVisible())


def test_a_listed_games_reel_is_asked_for_in_one_click(app, page):
    said = []
    page.notice.connect(said.append)
    p = played_page(app, page)
    p.game.setCurrentIndex(p.game.findData("nba-401"))
    p.reel_btn.click()
    assert wait(app, lambda: len(page.desk.orders()) == 1)
    o = page.desk.orders()[0]
    assert (o["title"], o["want"], o["by"], o["line"]) == ("Lakers at Celtics (NBA): a recap reel", "reel", "The PC",
                                                           "Lakers 108, Celtics 112")
    assert o["requires"] == {}  # asked for at the PC: the Telegram minimums don't hold it
    assert wait(app, lambda: said and said[0].startswith("Asked for Lakers at Celtics (NBA): a recap reel for Hoops"))
    assert "Asked for Lakers at Celtics" in p.hint.text()
    task = page.desk.claim("test:1")
    assert task["kind"] == "order"
    # and the status line says what's being worked on
    page.refresh(force=True)
    assert wait(app, lambda: page.now_line.text() == "Now: Looking for the games of Lakers at Celtics (NBA)")
    page.desk.finish(task, "queued")
    page.refresh(force=True)
    assert wait(app, lambda: not page.now_line.isVisible())


def test_find_its_footage_searches_for_the_picked_game(app, page):
    p = played_page(app, page)
    p.list.setCurrentRow(next(i for i in range(p.list.count()) if p.list.item(i).data(0x0100) == "nhl-501"))
    p.find_btn.click()
    assert wait(app, lambda: page.desk.state()["search"]["status"] == "queued")
    f = page.desk.filter()
    assert (f.sport, f.league, f.team_list, f.since) == ("hockey", "NHL", ["Avalanche", "Maple Leafs"], "3days")
    assert page.view == "all" and page.list.isVisible()


def test_scoreboards_that_cant_be_read_say_so(app, page):
    from ytclip.sportsdesk import feeds

    def down(url, headers):
        raise feeds.FeedError("The feed answered 503.")

    page.desk.slate_get = down
    page.view_buttons["played"].click()
    p = page.played
    assert wait(app, lambda: p.data is not None)
    assert not p.list.isVisible() and p.empty.isVisible() and "Not read:" in p.note.text()
    assert p.refresh_btn.isEnabled() and p.refresh_btn.text() == "Refresh"


def test_the_linked_mac_lists_the_games_and_asks_for_a_reel(app, window, world):  # noqa: F811
    import time

    from test_telegram_sports import scoreboards

    desk = world.host.desk
    desk.slate_get = scoreboards(time.time())
    assert wait(app, lambda: window.session.state == "online")
    window.nav.setCurrentIndex(window.i_sports)
    view = window.sports.view
    view.view_buttons["played"].click()
    p = view.played
    assert wait(app, lambda: p.data is not None and p.list.count() == 5, timeout=15)
    assert "on your PC" in p.reel_btn.toolTip()
    p.game.setCurrentIndex(p.game.findData("nhl-501"))
    p.reel_btn.click()
    assert wait(app, lambda: len(desk.orders()) == 1, timeout=15)
    o = desk.orders()[0]
    assert o["title"] == "Avalanche at Maple Leafs (NHL): a recap reel" and o["by"] not in ("", "The PC")
    assert wait(app, lambda: "Asked for Avalanche at Maple Leafs" in p.hint.text(), timeout=15)

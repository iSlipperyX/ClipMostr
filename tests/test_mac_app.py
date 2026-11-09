"""ClipMonster Remote's window (ytclip/mac/), driven against a real host server with automation's queue and
posting's library behind it: connecting, linking with a code, the pages, and acting on a video."""

import time

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from test_remote import mac_home, world  # noqa: E402,F401 - fixtures
from test_telegram import clip_folder, creator_folder  # noqa: E402
from ytclip.mac import hosts  # noqa: E402
from ytclip.mac import client as mclient  # noqa: E402
from ytclip.remote import state  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def wait(app, cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    app.processEvents()
    return cond()


def linked_host(w) -> hosts.Host:
    c, got = w.linked()
    h = hosts.Host(id=got["host_id"], name=got["name"], address="127.0.0.1", port=w.port, fp=got["fp"],
                   device_id=got["device_id"])
    hosts.remember(h, got["token"])
    return h


@pytest.fixture
def window(app, world):  # noqa: F811
    from ytclip.mac.window import MainWindow

    clip_folder(world.root, "Psychology/2026-11-08 Big Talk [abc]", clips=2)
    creator_folder(world.root)
    world.take()
    linked_host(world)
    win = MainWindow()
    win.notify_calls = []
    win.notify = lambda title, text: win.notify_calls.append((title, text))
    yield win
    win.quit()
    app.processEvents()


def test_it_connects_to_the_last_pc_and_shows_its_status(app, window, world):  # noqa: F811
    assert window.stack.currentWidget() is window.main
    assert wait(app, lambda: window.session.state == "online")
    assert wait(app, lambda: window.dashboard.c_review.value.text() == "1")
    assert "running" in window.dashboard.sub.text()
    assert wait(app, lambda: window.videos.list.count() == 3)
    from ytclip.gui.navigation import BADGE

    assert window.nav.sidebar.item_for(window.i_videos).data(BADGE) == 1
    assert window.videos.seg_btns["review"].text().endswith("1")


def test_ok_from_the_mac(app, window, world):  # noqa: F811
    assert wait(app, lambda: window.videos.list.count() == 3)
    window.nav.setCurrentIndex(window.i_videos)
    window.videos.set_filter("review")
    assert wait(app, lambda: window.videos.detail.v is not None and window.videos.detail.v["state"] == "review")
    vid = window.videos.detail.v["id"]
    assert window.videos.detail.ok_btn.isVisible() or not window.isVisible()
    window.videos.detail.ok_btn.click()
    assert wait(app, lambda: world.store.get(vid).review == "ok")
    assert wait(app, lambda: "OK" in window.notice_label.text())


def test_delete_asks_first(app, window, world, monkeypatch):  # noqa: F811
    assert wait(app, lambda: window.videos.list.count() == 3)
    window.nav.setCurrentIndex(window.i_videos)
    window.videos.set_filter("queued")
    assert wait(app, lambda: window.videos.detail.v is not None and window.videos.detail.v["state"] == "queued")
    vid = window.videos.detail.v["id"]
    monkeypatch.setattr(QMessageBox, "exec", lambda self: None)  # closed without pressing Delete
    window.videos.detail.del_btn.click()
    app.processEvents()
    assert world.store.get(vid) is not None

    def press_delete(self):
        for b in self.buttons():
            if b.text() == "Delete":
                b.click()
    monkeypatch.setattr(QMessageBox, "exec", press_delete)
    window.videos.detail.del_btn.click()
    assert wait(app, lambda: world.store.get(vid) is None)
    assert wait(app, lambda: all(i["id"] != vid for i in window.videos.items))


def test_caption_saves(app, window, world):  # noqa: F811
    assert wait(app, lambda: window.videos.list.count() == 3)
    window.nav.setCurrentIndex(window.i_videos)
    window.videos.set_filter("queued")
    d = window.videos.detail
    assert wait(app, lambda: d.v is not None)
    vid = d.v["id"]
    assert not d.save_btn.isEnabled()
    d.caption.setPlainText("A better caption")
    assert d.save_btn.isEnabled()
    d.save_btn.click()
    assert wait(app, lambda: world.store.get(vid).caption == "A better caption")


def test_pages_load(app, window, world):  # noqa: F811
    assert wait(app, lambda: window.session.state == "online")
    window.nav.setCurrentIndex(window.i_categories)
    assert wait(app, lambda: window.categories.list.count() == 2)
    window.nav.setCurrentIndex(window.i_ask)
    assert wait(app, lambda: window.ask.make_cat.count() == 3)
    window.ask.idea.setPlainText("why cats knock things off tables")
    window.ask.make_btn.click()
    assert wait(app, lambda: any(j.is_creation for j in world.runner.db.jobs()))
    window.nav.setCurrentIndex(window.i_requests)
    assert wait(app, lambda: window.requests.list.count() == 1)  # "No requests yet"
    window.nav.setCurrentIndex(window.i_week)
    assert wait(app, lambda: "Your week" in window.week.text.toHtml())
    window.nav.setCurrentIndex(window.i_posted)
    window.nav.setCurrentIndex(window.i_log)
    assert wait(app, lambda: window.log.text.toPlainText() != "")


def test_pause_from_the_top_bar(app, window, world):  # noqa: F811
    assert wait(app, lambda: window.session.state == "online" and window.dashboard.sub.text())
    window.pause_btn.click()
    assert wait(app, lambda: world.runner.db.meta("paused") == "1")
    assert wait(app, lambda: "Resume" in window.pause_btn.text())


def test_news_is_shown_and_announced(app, window, world):  # noqa: F811
    assert wait(app, lambda: window.session.state == "online")
    time.sleep(0.3)
    world.host.add_event("posted", "Posted “x” to @psych")
    assert wait(app, lambda: window.notify_calls, timeout=20)
    assert window.notify_calls[-1] == ("Posted", "Posted “x” to @psych")
    assert window.dashboard.news[0]["text"] == "Posted “x” to @psych"


def test_unlinked_on_the_pc_goes_back_to_connect(app, window, world):  # noqa: F811
    assert wait(app, lambda: window.session.state == "online")
    with state.edit(world.server.state_file) as data:
        data["devices"] = []
    window.session.client.close()
    window.refresh()
    assert wait(app, lambda: window.stack.currentWidget() is window.connect_page)
    assert "isn't linked" in window.connect_page.message.text()
    assert hosts.load()[0] == []


def test_offline_shows_a_banner(app, window, world):  # noqa: F811
    assert wait(app, lambda: window.session.state == "online")
    world.s.remote_on = False
    world.server.configure()
    window.session.client.close()
    window.refresh()
    assert wait(app, lambda: window.offline.isVisibleTo(window.main))
    assert "Can't reach" in window.offline_text.text()
    world.s.remote_on = True
    world.server.configure()
    window.refresh()
    assert wait(app, lambda: not window.offline.isVisibleTo(window.main))


def test_linking_from_the_connect_page(app, world):  # noqa: F811
    from ytclip.mac.connect import PairDialog
    from ytclip.mac.session import Tasks

    tasks = Tasks()
    code = world.code()
    d = PairDialog(tasks, "127.0.0.1", world.port, "PC")
    assert wait(app, lambda: "This PC's ID" in d.pc_id.text())
    d.code.setText(code.lower())
    d._typed(code.lower())
    assert d.code.text() == f"{code[:4]}-{code[4:]}"
    d._link()
    assert wait(app, lambda: d.result_host is not None, timeout=20)
    h = hosts.last()
    assert h.fp == world.server.fp and h.token()
    assert mclient.Client("127.0.0.1", world.port, h.fp, h.device_id, h.token()).get("/api/whoami")


def test_a_wrong_code_says_so(app, world):  # noqa: F811
    from ytclip.mac.connect import PairDialog
    from ytclip.mac.session import Tasks

    code = world.code()
    d = PairDialog(Tasks(), "127.0.0.1", world.port, "PC")
    d.code.setText(("B" if code[0] != "B" else "C") + code[1:])
    d._link()
    assert wait(app, lambda: "isn't right" in d.msg.text(), timeout=20)
    assert d.result_host is None and d.go.isEnabled()


def test_self_check(app, capsys):
    from ytclip.mac.app import self_check

    rc = self_check(app)
    out = capsys.readouterr().out
    assert "ClipMonster Remote" in out and "window ok" in out
    assert rc in (0, 1)  # 1 only where Qt Multimedia can't load (Linux CI without its sound libraries)

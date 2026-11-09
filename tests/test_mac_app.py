"""ClipMonster for Mac's Your PC pages (ytclip/mac/pc.py and the pages beside it), driven against a real host
server with automation's queue and posting's library behind it: connecting, linking with a code, the pages, and
acting on a video."""

import time

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from test_remote import mac_home, world  # noqa: E402,F401 - fixtures
from test_telegram import clip_folder, creator_folder  # noqa: E402
from ytclip.mac import hosts  # noqa: E402
from ytclip.mac import client as mclient  # noqa: E402
from ytclip.remote import state  # noqa: E402


def wait(app, cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    app.processEvents()
    return cond()


def shut(app, win) -> None:
    """Close ClipMonster for Mac for good, as Quit does once it may: without asking "Quit ClipMonster?" first, which
    a test that left something going would otherwise be stopped by."""
    win._quitting = True
    win.close()
    app.processEvents()


def linked_host(w) -> hosts.Host:
    c, got = w.linked()
    h = hosts.Host(id=got["host_id"], name=got["name"], address="127.0.0.1", port=w.port, fp=got["fp"],
                   device_id=got["device_id"])
    hosts.remember(h, got["token"])
    return h


@pytest.fixture
def window(app, world):  # noqa: F811
    """The Mac window's link to the PC (pc.PCLink): what the PC's pages call their window."""
    pytest.importorskip("ytclip.gui.main_window", exc_type=ImportError)  # no Qt Multimedia libraries (Linux CI)
    from ytclip.mac.window import MacWindow

    clip_folder(world.root, "Psychology/2026-11-08 Big Talk [abc]", clips=2)
    creator_folder(world.root)
    world.take()
    linked_host(world)
    win = MacWindow()
    link = win.link
    link.win_ = win
    link.notify_calls = []
    link.notify = lambda title, text: link.notify_calls.append((title, text))
    yield link
    shut(app, win)


def test_it_connects_to_the_last_pc_and_shows_its_status(app, window, world):  # noqa: F811
    assert window.linked and not window.nav.is_hidden(window.i_dash) and window.nav.is_hidden(window.i_link)
    assert window.nav.currentIndex() == 0  # it opens on the Studio, the PC's pages beside it
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
    assert window.videos.detail.ok_btn.isVisible() or not window.win_.isVisible()
    window.videos.detail.ok_btn.click()
    assert wait(app, lambda: world.store.get(vid).review == "ok")
    assert wait(app, lambda: "OK" in window.win_.status.text())


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
    assert wait(app, lambda: window.nav.currentWidget() is window.connect_page)
    assert "isn't linked" in window.connect_page.message.text()
    assert window.nav.is_hidden(window.i_dash) and not window.nav.is_hidden(window.i_link)
    assert hosts.load()[0] == []


def test_offline_shows_a_banner(app, window, world):  # noqa: F811
    assert wait(app, lambda: window.session.state == "online")
    world.s.remote_on = False
    world.server.configure()
    window.session.client.close()
    window.refresh()
    window.nav.setCurrentIndex(window.i_dash)
    assert wait(app, lambda: window.offline.isVisibleTo(window.bar) and window.bar.isVisibleTo(window.win_))
    assert "Can't reach" in window.offline_text.text()
    world.s.remote_on = True
    world.server.configure()
    window.refresh()
    assert wait(app, lambda: not window.offline.isVisibleTo(window.bar))


@pytest.fixture
def connect_page(app, world):  # noqa: F811
    from ytclip.mac.connect import ConnectPage
    from ytclip.mac.session import Tasks

    page = ConnectPage(Tasks())
    page.linked_to = []
    page.connect_to.connect(page.linked_to.append)
    return page


def test_linking_with_the_ip_address_port_and_code(app, world, connect_page):  # noqa: F811
    page = connect_page
    code = world.code()
    page.address.setText("127.0.0.1")
    page.port.setText(str(world.port))
    page._check()
    assert wait(app, lambda: "Found" in page.pc_line.text() and "a code is up" in page.pc_line.text())
    from ytclip.remote import protocol

    assert protocol.short_fingerprint(world.server.fp) in page.pc_line.text()  # the ID the PC's settings show
    page.code.setText(code.lower())
    page._code_edited(code.lower())
    assert page.code.text() == f"{code[:4]}-{code[4:]}"
    assert page.go_btn.text() == "Link"
    page.go_btn.click()
    assert wait(app, lambda: page.linked_to, timeout=20)
    h = page.linked_to[0]
    assert (h.address, h.port, h.fp) == ("127.0.0.1", world.port, world.server.fp) and h.token()
    assert hosts.last().id == h.id
    assert mclient.Client("127.0.0.1", world.port, h.fp, h.device_id, h.token()).get("/api/whoami")
    assert page.code.text() == ""  # a used code isn't left on screen


def test_a_wrong_code_says_so(app, world, connect_page):  # noqa: F811
    page = connect_page
    code = world.code()
    page.address.setText("127.0.0.1")
    page.port.setText(str(world.port))
    page.code.setText(("B" if code[0] != "B" else "C") + code[1:])
    page._go()
    assert wait(app, lambda: "isn't right" in page.form_msg.text(), timeout=20)
    assert not page.linked_to and page.go_btn.isEnabled() and page.code.isEnabled()


def test_the_form_checks_what_was_typed(app, world, connect_page):  # noqa: F811
    page = connect_page
    page.address.setText("")
    page._go()
    assert "IP address" in page.form_msg.text()
    page.address.setText("127.0.0.1")
    page.port.setText("0")
    page._go()
    assert "port" in page.form_msg.text()
    page.port.setText(str(world.port))
    page.code.setText("ABC")
    page._go()
    assert "8 letters" in page.form_msg.text()
    page.code.setText("")
    page._go()  # not linked, no code: asks for it
    assert wait(app, lambda: "isn't linked to" in page.form_msg.text())
    assert not page.linked_to


def test_address_with_its_port_goes_in_two_boxes(app, connect_page):
    page = connect_page
    page.address.setText("192.168.1.20:47999")
    assert page.target() == ("192.168.1.20", 47999)
    page._split_address()
    assert page.address.text() == "192.168.1.20" and page.port.text() == "47999"
    page.address.setText("https://10.0.0.5:47320/")
    page._split_address()
    assert page.address.text() == "10.0.0.5" and page.port.text() == "47320"


def test_a_linked_pc_connects_without_a_code(app, world, connect_page):  # noqa: F811
    page = connect_page
    h = linked_host(world)
    page.address.setText("127.0.0.1")
    page.port.setText(str(world.port))
    page._update_button()
    assert page.go_btn.text() == "Connect"
    page._go()
    assert page.linked_to and page.linked_to[0].id == h.id


def test_a_found_pc_fills_in_its_address(app, world, connect_page):  # noqa: F811
    page = connect_page
    page._pick_found({"name": "JUSTYN-PC", "id": "x", "address": "192.168.1.20", "port": 47320, "fp": "ab" * 32,
                      "version": "3.6.0", "api": 1})
    assert page.address.text() == "192.168.1.20" and page.port.text() == "47320"
    assert "authorization code" in page.form_msg.text() and not page.linked_to


def test_self_check(app, capsys):
    pytest.importorskip("ytclip.gui.main_window", exc_type=ImportError)  # no Qt Multimedia libraries (Linux CI)
    from ytclip.mac.app import self_check

    rc = self_check(app)
    out = capsys.readouterr().out
    assert "ClipMonster 3." in out and "window ok" in out and "keys in" in out
    assert rc in (0, 1)  # 1 where a tool it runs isn't installed here

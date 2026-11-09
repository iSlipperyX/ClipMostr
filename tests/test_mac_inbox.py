"""ClipMonster for Mac's Sports clips inbox (ytclip/mac/inbox.py), against a real host server: clips a sports category
sends show up with a notification, save to the Mac by themselves, and the PC hears which Mac has which."""

import urllib.request

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from test_mac_app import linked_host, wait  # noqa: E402
from test_remote import mac_home, world  # noqa: E402,F401 - fixtures
from test_sports import exported  # noqa: E402
from ytclip.categories import Category  # noqa: E402
from ytclip.mac import inbox, prefs  # noqa: E402
from ytclip.remote import deliveries  # noqa: E402
from ytclip.remote.host import HostError  # noqa: E402

FOOTBALL = {"id": "f1", "name": "Football", "sport": "soccer", "send_to_macs": True, "account_id": "a"}


@pytest.fixture
def sent(world, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(deliveries, "folder", lambda: tmp_path / "deliveries")
    world.s.categories = list(world.s.categories) + [FOOTBALL]
    cat = Category.from_json(FOOTBALL)

    def send(n=2, rejected=0):
        folder = exported(tmp_path / f"v{len(list(tmp_path.glob('v*')))}", n + rejected, rejected)
        return deliveries.add(folder, cat)

    return send


@pytest.fixture
def mac(app, world, sent, tmp_path, monkeypatch):  # noqa: F811
    """make() opens ClipMonster for Mac and gives its link to the PC (pc.PCLink), which holds the inbox."""
    from ytclip.mac.window import MacWindow

    prefs.set("inbox_folder", str(tmp_path / "Movies"))
    linked_host(world)

    def make(auto=True):
        prefs.set("inbox_auto_save", auto)
        win = MacWindow()
        link = win.link
        link.win_ = win
        link.notify_calls = []
        link.notify = lambda title, text: link.notify_calls.append((title, text))
        made.append(win)
        return link

    made = []
    yield make
    for win in made:
        win.quit()
    app.processEvents()


def device_id(win) -> str:
    return win.session.host.device_id


def test_clips_sent_while_away_are_saved_and_announced(app, world, sent, mac, tmp_path):  # noqa: F811
    clips = sent(2)
    win = mac(auto=True)
    assert wait(app, lambda: len(win.inbox.items) == 2)
    assert win.notify_calls[0] == ("Sports clips", "2 new clips are waiting.")
    saved = tmp_path / "Movies" / "Football"
    assert wait(app, lambda: len(list(saved.glob("*.mp4"))) == 2, timeout=20)
    names = sorted(p.name for p in saved.glob("*.mp4"))
    assert any("Watch the keeper 1" in n for n in names) and all(n[:4].isdigit() for n in names)
    me = device_id(win)
    assert wait(app, lambda: all(i["got"] for i in deliveries.for_device(me)))
    assert wait(app, lambda: win.nav.sidebar.item_for(win.i_inbox).data(_badge()) == 0)
    assert win.inbox.chip_for(win.inbox.items[0]) == ("On this Mac", inbox.theme.GOOD)
    assert clips[0]["id"] in inbox.Saved().data[win.session.host.id]


def _badge():
    from ytclip.gui.navigation import BADGE

    return BADGE


def test_a_new_clip_while_connected_rings_and_waits(app, world, sent, mac):  # noqa: F811
    win = mac(auto=False)
    assert wait(app, lambda: win.session is not None and win.session.state == "online")
    assert wait(app, lambda: win.inbox._known is not None)
    assert win.inbox.empty.text().startswith("No clips yet. Each new clip from Football")
    made = sent(1)
    world.host.add_event("delivery", "⚽ New Soccer (football) clip: Watch the keeper 1", item=made[0]["id"])
    assert wait(app, lambda: len(win.inbox.items) == 1, timeout=20)
    assert ("⚽ New Soccer (football) clip", "Watch the keeper 1") in win.notify_calls
    assert win.nav.sidebar.item_for(win.i_inbox).data(_badge()) == 1
    assert not deliveries.for_device(device_id(win))[0]["got"]  # not saved: auto-save is off
    assert win.inbox.chip_for(win.inbox.items[0])[0] == "New"
    win.win_.show()
    win.nav.setCurrentIndex(win.i_inbox)  # seeing it is enough to make it not new
    assert wait(app, lambda: deliveries.for_device(device_id(win))[0]["seen"])
    assert wait(app, lambda: win.nav.sidebar.item_for(win.i_inbox).data(_badge()) == 0)
    win.inbox.detail.save_btn.click()
    assert wait(app, lambda: win.inbox.saver.has(made[0]["id"]) is not None, timeout=20)
    assert win.inbox.detail.show_btn.isVisibleTo(win.inbox.detail)


def test_removing_one_from_the_inbox(app, world, sent, mac, monkeypatch):  # noqa: F811
    made = sent(2)
    win = mac(auto=False)
    assert wait(app, lambda: len(win.inbox.items) == 2)
    win.inbox.list.setCurrentRow(0)
    first = win.inbox.detail.v["id"]

    def press(self):
        for b in self.buttons():
            if b.text() == "Remove":
                b.click()
    monkeypatch.setattr(QMessageBox, "exec", press)
    win.inbox.detail.remove_btn.click()
    assert wait(app, lambda: len(win.inbox.items) == 1)
    assert [i["id"] for i in deliveries.for_device(device_id(win))] == [i["id"] for i in made if i["id"] != first]


def test_a_sent_clip_plays_from_the_pc(app, world, sent, mac):  # noqa: F811
    made = sent(1)
    win = mac(auto=False)
    assert wait(app, lambda: len(win.inbox.items) == 1)
    url = win.proxy.url(made[0]["id"], kind="d")
    with urllib.request.urlopen(url, timeout=10) as r:
        assert r.read() == b"\0" * 1001
    assert win.inbox.detail.player.url == url


def test_an_older_pc_says_so(app, world, sent, mac, monkeypatch):  # noqa: F811
    def old(*_a, **_k):
        raise HostError(404, "No such request.")

    monkeypatch.setattr(world.host, "deliveries", old)
    win = mac()
    assert wait(app, lambda: win.inbox.unsupported)
    assert "older ClipMonster" in win.inbox.empty.text()


def test_file_names_are_safe_and_numbered(tmp_path):
    item = {"id": "x", "at": 0, "category": "Football: EPL/UCL", "title": "⚽ Watch the keeper?! <wow>", "ext": ".mp4"}
    first = inbox.dest_for(item, tmp_path)
    assert first.parent.name == "Football EPL UCL" and "Watch the keeper" in first.name and "⚽" not in first.name
    first.parent.mkdir(parents=True)
    first.write_bytes(b"")
    assert inbox.dest_for(item, tmp_path).name.endswith("(2).mp4")

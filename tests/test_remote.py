"""The Mac app's connection (ytclip/remote/ and ytclip/mac/client.py): linking a Mac with a code, the pinned
certificate, keys, and every request the Mac makes, against a real HTTPS server on this machine with
automation's queue and posting's library behind it."""

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_telegram import clip_folder, creator_folder
from ytclip.jobs import JobDB
from ytclip.mac import client as mclient
from ytclip.mac import discover, hosts, keychain, proxy
from ytclip.pipeline import Runner
from ytclip.posting import intake
from ytclip.posting.accounts import Accounts
from ytclip.posting.store import Store
from ytclip.remote import certs, protocol, state
from ytclip.remote.host import Host
from ytclip.remote.server import RemoteServer
from ytclip.settings import Settings


REAL_ITERATIONS, REAL_MIN = protocol.ITERATIONS, protocol.MIN_ITERATIONS


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def mac_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPMONSTER_REMOTE_HOME", str(tmp_path / "mac"))
    monkeypatch.setattr(protocol, "ITERATIONS", 1000)  # fast tests; the real count is checked below
    monkeypatch.setattr(protocol, "MIN_ITERATIONS", 1000)
    # ClipMonster for Mac opened in a test: no welcome window (it would wait for an answer) or update check
    from ytclip.mac import prefs

    prefs.set("check_updates", False)
    Settings(setup_done=True).save()


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = SimpleNamespace(tmp=tmp_path, logs=[], clock=[time.time()])
    accs = Accounts.load()
    a = accs.add("")
    a.username, a.logged_in = "psych", True
    accs.save()
    w.accounts, w.account = accs, a
    w.root = tmp_path / "Automation"
    w.s = Settings(remote_on=True, remote_port=free_port(), auto_output_dir=str(w.root), creator_review="always",
                   categories=[{"id": "p1", "name": "Psychology", "account_id": a.id},
                               {"id": "g1", "name": "Gaming", "account_id": a.id}])
    w.runner = Runner(db=JobDB(tmp_path / "jobs.db"), load=lambda: w.s, log=w.logs.append)
    w.store = Store(tmp_path / "lib.json")
    w.host = Host(w.runner, posting=(w.store, accs, None))
    w.server = RemoteServer(w.runner, host=w.host, state_file=tmp_path / "remote.json",
                            status_file=tmp_path / "remote-status.json", cert_dir=tmp_path / "certs",
                            bind="127.0.0.1", discovery=False)
    monkeypatch.setattr(state, "path", lambda: tmp_path / "remote.json")
    assert w.server.configure()
    w.port = w.server.port

    def take():
        items, _ = intake.take(w.store, w.accounts, w.s)
        w.store.save()
        return items

    def code():
        with state.edit(w.server.state_file) as data:
            return state.start_pairing(data, time.time())

    def linked():
        c = mclient.Client("127.0.0.1", w.port)
        got = c.pair(code(), "Test Mac")
        return c, got

    w.take, w.code, w.linked = take, code, linked
    yield w
    w.server.stop()


# --- the protocol -----------------------------------------------------------------------------------------


def test_codes_read_as_typed():
    code = protocol.new_code()
    assert len(code) == 8 and set(code) <= set(protocol.CODE_CHARS)
    shown = protocol.show_code(code)
    assert shown == f"{code[:4]}-{code[4:]}"
    assert protocol.normalize_code(f" {shown.lower()} ") == code
    assert protocol.looks_like_code(shown.lower())
    assert not protocol.looks_like_code("ABCD-EFG0")  # 0 isn't one of the code's letters
    assert protocol.parse_auth(protocol.auth_header("dev1", "tok.en")) == ("dev1", "tok.en")
    assert protocol.parse_auth("Basic x") == ("", "")


def test_the_real_stretch_is_slow_enough():
    assert REAL_ITERATIONS >= 200_000 and REAL_MIN <= REAL_ITERATIONS
    t = time.perf_counter()
    protocol.stretch("ABCDEFGH", b"salt" * 4, REAL_ITERATIONS)
    assert time.perf_counter() - t > 0.02  # each guess costs real time; 32^8 of them can't be tried in ten minutes


def test_proofs_bind_the_certificate():
    key = protocol.stretch("ABCDEFGH", b"s" * 16, 1000)
    a = protocol.mac_proof(key, "aa" * 32, "n" * 32, "Mac")
    assert a == protocol.mac_proof(key, "AA" * 32, "n" * 32, "Mac")  # case of the hex doesn't matter
    assert a != protocol.mac_proof(key, "bb" * 32, "n" * 32, "Mac")  # someone else's certificate
    assert a != protocol.mac_proof(protocol.stretch("ABCDEFGJ", b"s" * 16, 1000), "aa" * 32, "n" * 32, "Mac")
    assert protocol._mac(key, "ab", "c") != protocol._mac(key, "a", "bc")  # fields can't be shifted


def test_certificate_is_made_once(tmp_path):
    key, cert = certs.ensure(tmp_path, "PC")
    fp = certs.fingerprint_of(cert)
    assert len(fp) == 64
    assert certs.ensure(tmp_path, "PC") == (key, cert) and certs.fingerprint_of(cert) == fp
    cert.write_text("broken")
    certs.ensure(tmp_path, "PC")
    assert certs.fingerprint_of(cert) != fp  # a broken one is made again


# --- linking -------------------------------------------------------------------------------------------------


def test_hello_needs_no_key_but_the_rest_does(world):
    c = mclient.Client("127.0.0.1", world.port)
    info = c.hello()
    assert info["app"] == "ClipMonster" and info["api"] == protocol.API_VERSION
    assert info["pairing"]["open"] is False
    assert c.seen_fp == world.server.fp
    with pytest.raises(mclient.ApiError) as e:
        c.get("/api/status")
    assert e.value.status == 401 and "isn't linked" in e.value.message


def test_linking_with_the_code(world):
    c, got = world.linked()
    assert got["fp"] == world.server.fp and got["name"] == socket.gethostname()
    data = state.load(world.server.state_file)
    assert [d["name"] for d in data["devices"]] == ["Test Mac"]
    assert data["devices"][0]["token_hash"] == protocol.token_hash(got["token"])
    assert got["token"] not in json.dumps(data)  # only its hash is kept on the PC
    assert data["pairing"] == {}  # the code is used up
    assert c.get("/api/whoami")["name"] == "Test Mac"
    assert any("Linked Test Mac" in line for line in world.logs)
    assert world.host.events_after(0)["events"][-1]["kind"] == "linked"


def test_wrong_codes_are_counted_and_then_the_code_dies(world):
    code = world.code()
    wrong = ("B" if code[0] != "B" else "C") + code[1:]
    c = mclient.Client("127.0.0.1", world.port)
    for n in range(protocol.MAX_WRONG - 1):
        with pytest.raises(mclient.ApiError) as e:
            c.pair(wrong)
        assert e.value.status == 403 and "isn't right" in e.value.message
    with pytest.raises(mclient.ApiError) as e:
        c.pair(wrong)
    assert "Too many wrong codes" in e.value.message
    with pytest.raises(mclient.ApiError) as e:
        c.pair(code)  # even the right code is gone now
    assert "No code is up" in e.value.message
    assert state.load(world.server.state_file)["devices"] == []


def test_a_code_runs_out(world):
    with state.edit(world.server.state_file) as data:
        code = state.start_pairing(data, time.time() - protocol.CODE_LIFE - 1)
    with pytest.raises(mclient.ApiError) as e:
        mclient.Client("127.0.0.1", world.port).pair(code)
    assert "No code is up" in e.value.message


def test_someone_in_the_middle_cant_link(world, tmp_path):
    """A computer that shows the Mac its own certificate and passes the Mac's proof on to the PC gets nowhere:
    the proof names the certificate the Mac saw."""
    code = world.code()
    data = state.load(world.server.state_file)
    salt = bytes.fromhex(data["pairing"]["salt"])
    key = protocol.stretch(code, salt, data["pairing"]["iterations"])
    fake_fp = certs.fingerprint_of(certs.ensure(tmp_path / "evil", "evil")[1])
    nonce = "ab" * 16
    c = mclient.Client("127.0.0.1", world.port)
    with pytest.raises(mclient.ApiError) as e:
        c.request("POST", "/api/pair", {"name": "Mac", "nonce": nonce,
                                        "proof": protocol.mac_proof(key, fake_fp, nonce, "Mac")}, auth=False)
    assert e.value.status == 403
    assert state.load(world.server.state_file)["devices"] == []


def test_a_mac_wont_talk_to_another_certificate(world):
    c, got = world.linked()
    other = mclient.Client("127.0.0.1", world.port, fp="00" * 32, device_id=got["device_id"], token=got["token"])
    with pytest.raises(mclient.WrongPC):
        other.get("/api/status")


def test_unlinking_on_the_pc_locks_the_mac_out(world):
    c, got = world.linked()
    assert c.get("/api/status")["host"]["app"] == "ClipMonster"
    with state.edit(world.server.state_file) as data:
        state.unlink(data, got["device_id"])
    c.close()
    with pytest.raises(mclient.ApiError) as e:
        c.get("/api/status")
    assert e.value.status == 401 and "any more" in e.value.message


def test_the_mac_can_unlink_itself(world):
    c, got = world.linked()
    assert "unlinked" in c.post("/api/unlink")["message"]
    assert state.load(world.server.state_file)["devices"] == []


def test_wrong_keys_are_slowed_down(world):
    c = mclient.Client("127.0.0.1", world.port, device_id="nope", token="nope")
    statuses = []
    for _ in range(14):
        try:
            c.get("/api/status")
        except mclient.ApiError as exc:
            statuses.append(exc.status)
    assert statuses[:12] == [401] * 12 and statuses[-1] == 429


# --- what the Mac sees and does --------------------------------------------------------------------------------


def test_status(world):
    c, _ = world.linked()
    st = c.get("/api/status")
    assert st["automation"]["paused"] is False and st["job"] is None
    assert st["posting"]["queued"] == 0
    assert [a["handle"] for a in st["accounts"]] == ["@psych"]
    assert st["queue"] == {"to_clip": 0, "to_make": 0, "next": []}


def test_videos_watch_ok_and_delete(world):
    clip_folder(world.root, "Psychology/2026-11-08 Big Talk [abc]")
    creator_folder(world.root)
    world.take()
    c, _ = world.linked()
    got = c.get("/api/videos")
    items = got["items"]
    assert len(items) == 3
    held = [i for i in items if i["state"] == "review"]
    assert len(held) == 1 and items[0] is not None and items[0]["state"] == "review"  # held for your OK first
    assert held[0]["ai_generated"] and held[0]["can"]["ok"] and held[0]["prompt"].startswith("the truth")
    assert c.get("/api/videos", rev=got["rev"]) == {"rev": got["rev"], "same": True}

    # watching: the file, with Range so it streams, and its picture
    vid = held[0]["id"]
    data = c.fetch(c.video_path(vid, "file"))
    assert data[4:8] == b"ftyp"
    resp = c.raw(c.video_path(vid, "file"), {"Range": "bytes=0-99"})
    assert resp.status == 206 and len(resp.read()) == 100
    assert resp.getheader("Content-Range").startswith("bytes 0-99/")
    resp._one_off.close()
    assert c.fetch(c.video_path(vid, "thumb"))[:3] == b"\xff\xd8\xff"

    # OK
    out = c.post(c.video_path(vid, "ok"))
    assert out["video"]["state"] == "queued" and world.store.get(vid).review == "ok"
    with pytest.raises(mclient.ApiError) as e:
        c.post(c.video_path(vid, "ok"))
    assert e.value.status == 409
    assert c.get("/api/videos", rev=got["rev"])["same"] is False  # the list changed

    # edit the caption
    clip = next(i for i in items if not i["ai_generated"])
    out = c.post(c.video_path(clip["id"], "edit"), {"caption": "New words", "hashtags": "#a   #b"})
    assert out["video"]["caption"] == "New words" and world.store.get(clip["id"]).hashtags == "#a #b"

    # post now needs posting running
    with pytest.raises(mclient.ApiError) as e:
        c.post(c.video_path(clip["id"], "post-now"))
    assert "Posting isn't running" in e.value.message

    # delete: its file goes
    path = Path(world.store.get(clip["id"]).path)
    assert path.is_file()
    out = c.post(c.video_path(clip["id"], "delete"))
    assert "Deleted from the PC" in out["message"] or "Recycle Bin" in out["message"]
    assert not path.exists() and world.store.get(clip["id"]) is None
    with pytest.raises(mclient.ApiError) as e:
        c.get(c.video_path(clip["id"]))
    assert e.value.status == 404


def test_retry_a_failed_post(world):
    clip_folder(world.root, "Psychology/2026-11-08 Big Talk [abc]", clips=1)
    world.take()
    it = world.store.queue()[0]
    it.status, it.last_error = "failed", "TikTok said no"
    world.store.save()
    c, _ = world.linked()
    v = c.get(c.video_path(it.id))
    assert v["state"] == "failed" and v["can"]["retry"]
    assert c.post(c.video_path(it.id, "retry"))["video"]["state"] == "queued"


def test_asking_for_videos(world):
    c, _ = world.linked()
    out = c.post("/api/make", {"idea": "the  real story of the Bermuda Triangle", "category": "p1"})
    assert "Bermuda Triangle" in out["message"] and out["held"] is True
    job = world.runner.db.get(out["job"])
    assert job.is_creation and job.category_id == "p1" and job.origins == ["creator", "mac"]

    out = c.post("/api/clip", {"links": "https://www.youtube.com/watch?v=dQw4w9WgXcQ not-a-link", "category": "g1"})
    assert len(out["added"]) == 1 and out["bad"] == ["not-a-link"]
    out = c.post("/api/clip", {"links": ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]})
    assert out["before"] and "clipped before" in out["message"]

    with pytest.raises(mclient.ApiError) as e:
        c.post("/api/requests", {"count": 3, "category": "g1"})  # no links and no YouTube key
    assert "YouTube Data API key" in e.value.message
    out = c.post("/api/requests", {"count": 3, "min_len": 30, "max_len": 45, "category": "g1",
                                   "links": ["https://www.youtube.com/watch?v=abcdefghijk"], "notes": "clutch plays"})
    reqs = c.get("/api/requests")["items"]
    assert reqs[0]["id"] == out["id"] and reqs[0]["category"] == "Gaming" and reqs[0]["can_cancel"]
    assert "3 clips of 30–45 s" in reqs[0]["title"]
    assert "Cancelled" in c.post(f"/api/requests/{out['id']}/cancel")["message"]
    assert c.get("/api/requests")["items"][0]["status"] == "cancelled"

    st = c.get("/api/status")
    assert st["queue"]["to_make"] == 1 and st["queue"]["to_clip"] == 1
    with pytest.raises(mclient.ApiError) as e:
        c.post("/api/make", {"idea": "x", "category": "nope"})
    assert e.value.status == 400


def test_pausing(world):
    c, _ = world.linked()
    assert c.post("/api/automation", {"paused": True})["paused"] is True
    assert world.runner.db.meta("paused") == "1" and c.get("/api/status")["automation"]["paused"]
    c.post("/api/automation", {"paused": False})
    assert world.runner.db.meta("paused") == "0" and world.runner.db.meta("nudge") == "1"

    saved = {}
    world.s.save = lambda path=None: saved.setdefault("yes", True)
    cats = c.get("/api/categories")["items"]
    assert [x["name"] for x in cats] == ["Psychology", "Gaming"]
    out = c.post("/api/categories/g1", {"paused": True})
    assert "Gaming is paused" in out["message"] and saved
    assert [x["paused"] for x in c.get("/api/categories")["items"]] == [False, True]


def test_events_wait_for_news(world):
    c, _ = world.linked()
    seq = c.get("/api/events")["seq"]
    threading.Timer(0.5, lambda: world.host.on_event(world.account, "posted", "Posted “x” to @psych")).start()
    t = time.monotonic()
    got = c.get("/api/events", after=seq, wait=5)
    assert 0.3 < time.monotonic() - t < 4
    assert got["events"][0]["text"] == "Posted “x” to @psych" and got["events"][0]["account"] == "@psych"
    world.runner.remote = world.server
    world.runner.tell("Low disk space")
    assert c.get("/api/events", after=got["seq"])["events"][0]["kind"] == "problem"


def test_week_and_log(world, monkeypatch):
    c, _ = world.linked()
    w = c.get("/api/week")
    assert w["headline"] and isinstance(w["sections"], list)
    log = world.tmp / "automation.log"
    log.write_text("\n".join(f"line {n}" for n in range(500)), encoding="utf-8")
    monkeypatch.setattr("ytclip.pipeline.log_path", lambda: log)
    assert c.get("/api/log", lines=3)["lines"] == ["line 497", "line 498", "line 499"]


def test_turning_it_off_and_on(world):
    c, _ = world.linked()
    world.s.remote_on = False
    assert world.server.configure() is False and not world.server.listening
    c.close()
    with pytest.raises(mclient.Offline) as e:
        c.get("/api/status")
    assert "Nothing is answering" in e.value.message
    world.s.remote_on = True
    assert world.server.configure()
    assert c.get("/api/status")


def test_turning_it_off_drops_macs_still_connected(world):
    c, _ = world.linked()
    assert c.get("/api/status")  # its connection stays open between requests (keep-alive)
    world.s.remote_on = False
    world.server.configure()
    with pytest.raises(mclient.Offline):  # not answered on the old connection either
        c.get("/api/status")


def test_a_port_in_use_is_said_plainly(world):
    other = RemoteServer(world.runner, host=world.host, state_file=world.tmp / "remote.json",
                         status_file=world.tmp / "rs2.json", cert_dir=world.tmp / "certs", bind="127.0.0.1",
                         discovery=False)
    assert other.configure() is False
    assert f"Port {world.port}" in other.error


def test_heartbeat_for_the_window(world):
    world.server._write_status()
    st = state.read_status(world.server.status_file)
    assert st["running"] and st["port"] == world.port and st["fingerprint"] == world.server.fp


# --- the Mac side ------------------------------------------------------------------------------------------------


def test_addresses_typed_by_hand():
    assert mclient.split_address("192.168.1.20") == ("192.168.1.20", 47320)
    assert mclient.split_address(" https://my-pc.tail1.ts.net:5000/ ") == ("my-pc.tail1.ts.net", 5000)
    assert mclient.split_address("[fe80::1]:47321") == ("fe80::1", 47321)
    assert mclient.split_address("pc:nope") == ("pc", 47320)


def test_hosts_and_keys_are_remembered():
    h = hosts.Host(id="pc1", name="PC", address="10.0.0.2", fp="ab" * 32, device_id="d1")
    hosts.remember(h, "secret-token")
    assert hosts.last().name == "PC" and hosts.last().token() == "secret-token"
    assert "secret-token" not in (hosts._path()).read_text()  # keys never sit in hosts.json
    hosts.remember(hosts.Host(id="pc2", name="Other", address="10.0.0.3", fp="cd" * 32, device_id="d2"), "t2")
    assert hosts.last().id == "pc2"
    hosts.forget("pc2")
    assert [x.id for x in hosts.load()[0]] == ["pc1"] and keychain.load("pc2:d2") == ""


def test_discovery_finds_the_pc(world):
    world.server.discovery = True
    port = free_port()
    import ytclip.remote.protocol as p

    old = p.DISCOVERY_PORT
    p.DISCOVERY_PORT = port
    try:
        world.server._open_discovery()
        assert world.server._udp is not None  # listening for the question
        found = discover.discover(timeout=3.0, port=port, targets=["127.0.0.1"])
        # a Mac that asked and went away before the answer (on Windows, the answer bouncing back made the
        # PC's next read fail) doesn't stop the PC answering the next one
        gone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        gone.sendto(p.DISCOVER_HELLO + b"gone", ("127.0.0.1", port))
        gone.close()
        time.sleep(0.3)
        again = discover.discover(timeout=3.0, port=port, targets=["127.0.0.1"])
    finally:
        p.DISCOVERY_PORT = old
    assert len(found) == 1
    assert found[0]["fp"] == world.server.fp and found[0]["port"] == world.port and found[0]["address"] == "127.0.0.1"
    assert [f["id"] for f in again] == [found[0]["id"]]


def test_the_player_streams_through_the_mac_only_proxy(world):
    clip_folder(world.root, "Psychology/2026-11-08 Big Talk [abc]", clips=1)
    world.take()
    c, _ = world.linked()
    vid = world.store.queue()[0].id
    p = proxy.MediaProxy(lambda: c)
    try:
        url = p.url(vid)
        req = urllib.request.Request(url, headers={"Range": "bytes=0-9"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 206 and len(r.read()) == 10
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url.replace(p._server.secret, "0" * 32), timeout=10)
        assert e.value.code == 404  # no secret, nothing
    finally:
        p.close()

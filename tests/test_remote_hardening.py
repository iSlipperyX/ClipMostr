"""The Mac↔PC link's hardening (ytclip/remote/, ytclip/mac/): what TLS either end accepts, the Mac never sending
its key unpinned, waiting longer and longer for a PC that's gone, picking a download up where it broke off,
and paths from the other computer landing only where they should, on Windows, a Mac or Linux."""

import os
import ssl
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_remote import mac_home, world  # noqa: F401 - fixtures
from test_telegram import clip_folder, creator_folder
from ytclip.mac import client as mclient
from ytclip.remote import paths, protocol
from ytclip.remote.paths import UnsafePath


# ---------------------------------------------------------------- TLS


def test_both_ends_take_only_modern_tls():
    ctx = protocol.harden(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert ctx.options & ssl.OP_NO_COMPRESSION
    tls12 = [c for c in ctx.get_ciphers() if c["protocol"] == "TLSv1.2"]
    assert tls12, "no TLS 1.2 cipher left"
    for c in tls12:
        assert c["kea"] == "kx-ecdhe" and c["aead"], c["name"]  # forward secrecy, authenticated encryption


def handshake(port, *, maximum=None, ciphers=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    if maximum:
        ctx.maximum_version = maximum
    if ciphers:
        ctx.set_ciphers(ciphers)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with ctx.wrap_socket(raw) as tls:
            return tls.version(), tls.cipher()[0]


def test_the_pc_turns_away_old_tls_and_weak_ciphers(world):  # noqa: F811
    version, _ = handshake(world.port)
    assert version in ("TLSv1.2", "TLSv1.3")
    with pytest.raises((ssl.SSLError, OSError)):
        handshake(world.port, maximum=ssl.TLSVersion.TLSv1_2, ciphers="AES128-SHA:AES256-SHA")  # no ECDHE, no AEAD
    _, cipher = handshake(world.port, maximum=ssl.TLSVersion.TLSv1_2)
    assert cipher.startswith("ECDHE-") and ("GCM" in cipher or "CHACHA20" in cipher)


def test_the_mac_never_sends_its_key_to_a_pc_it_hasnt_pinned(world):  # noqa: F811
    _, got = world.linked()
    unpinned = mclient.Client("127.0.0.1", world.port, fp="", device_id=got["device_id"], token=got["token"])
    with pytest.raises(mclient.WrongPC, match="won't send it its key"):
        unpinned.get("/api/status")
    pinned = mclient.Client("127.0.0.1", world.port, fp=world.server.fp, device_id=got["device_id"],
                            token=got["token"])
    assert isinstance(pinned.get("/api/status"), dict)  # the same key, pinned: answered


# ---------------------------------------------------------------- waiting for a PC that's gone


def test_backoff_doubles_up_to_its_cap_and_resets():
    b = protocol.Backoff(base=3, cap=60, jitter=0.2, rand=lambda: 0.5)  # 0.5: no jitter
    assert [b.next() for _ in range(7)] == [3, 6, 12, 24, 48, 60, 60]
    assert b.failures == 7 and b.peek() == 60
    b.reset()
    assert b.next() == 3


@pytest.mark.parametrize("r, want", [(0.0, 8.0), (1.0, 12.0)])
def test_backoff_jitter_stays_inside_its_share(r, want):
    b = protocol.Backoff(base=10, cap=60, jitter=0.2, rand=lambda: r)
    assert b.next() == pytest.approx(want)


def test_backoff_never_waits_less_than_nothing():
    assert protocol.Backoff(base=1, jitter=5, rand=lambda: 0.0).next() == 0.0


class NowTasks:
    """session.Tasks without a thread: each call is answered at once."""

    def run(self, fn, ok=None, fail=None):
        try:
            got = fn()
        except Exception as exc:  # noqa: BLE001
            fail and fail(exc)
        else:
            ok and ok(got)


class PC:
    def __init__(self):
        self.down: Exception | None = None
        self.asked: list[str] = []

    def get(self, path, **kw):
        self.asked.append(path)
        if self.down is not None:
            raise self.down
        return {"rev": "1", "videos_rev": ""} if path == "/api/status" else {"items": [], "rev": "1"}

    def close(self):
        pass


@pytest.fixture
def session(app, monkeypatch):
    from ytclip.mac import hosts, session as S

    monkeypatch.setattr(hosts, "remember", lambda h: None)
    host = SimpleNamespace(address="127.0.0.1", port=1, fp="ab", device_id="d", token=lambda: "t", seen_at=0.0)
    sess = S.Session(host, NowTasks())
    pc = PC()
    sess.client = pc
    monkeypatch.setattr(sess.backoff, "rand", lambda: 0.5)
    return sess, pc, S


@pytest.mark.parametrize("why", [mclient.Offline("no route"), mclient.WrongPC("other cert"),
                                 mclient.ApiError(401, "unlinked"), mclient.ApiError(429, "slow down")])
def test_a_lost_pc_is_asked_about_less_and_less_often(session, why):
    sess, pc, S = session
    pc.down = why
    waits = []
    for _ in range(7):
        sess.refresh_status()
        waits.append(sess.status_timer.interval())
    assert waits == [3000, 6000, 12000, 24000, 48000, 60000, 60000]
    pc.down = None
    sess.refresh_status()  # back: the usual pace again
    assert sess.status_timer.interval() == S.STATUS_EVERY and sess.state == "online" and sess.backoff.failures == 0


def test_a_pc_that_answers_no_to_one_thing_isnt_lost(session):
    sess, pc, S = session
    pc.down = mclient.ApiError(500, "a bug on the PC")
    sess.refresh_status()
    assert sess.status_timer.interval() == S.STATUS_EVERY and sess.backoff.failures == 0


def test_while_the_pc_is_gone_only_the_status_is_asked_for(session):
    sess, pc, _ = session
    pc.down = mclient.Offline("no route")
    sess.refresh_status()
    assert sess.state == "offline" and not sess.reachable
    pc.asked.clear()
    sess.refresh_videos()
    sess.refresh_deliveries()
    assert pc.asked == []
    pc.down = None
    sess.retry_now()  # the window's Try again
    assert pc.asked[0] == "/api/status" and sess.reachable and sess.status_timer.interval() == 3000


# ---------------------------------------------------------------- picking a download up again


class Resp:
    def __init__(self, status, body=b"", headers=None, break_after=None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.break_after = break_after
        self.sent = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, n=-1):
        if self.break_after is not None and self.sent >= self.break_after:
            raise ConnectionResetError("the Wi-Fi dropped")
        end = len(self.body) if n < 0 else self.sent + n
        if self.break_after is not None:
            end = min(end, self.break_after)
        chunk = self.body[self.sent:end]
        self.sent += len(chunk)
        return chunk


FILE = bytes(range(256)) * 4096  # 1 MiB


def serve(asked, *, whole=False, start_shift=0, total=None, status=None):
    """A PC that answers Range requests (or ignores them with `whole`)."""
    def raw(path, headers=None, timeout=None):
        asked.append(dict(headers or {}))
        rng = (headers or {}).get("Range", "")
        if status:
            return Resp(status, b"{}")
        if rng and not whole:
            start = int(rng.split("=")[1].rstrip("-")) + start_shift
            body = FILE[start:]
            return Resp(206, body, {"Content-Length": str(len(body)),
                                    "Content-Range": f"bytes {start}-{len(FILE) - 1}/{total or len(FILE)}"})
        return Resp(200, FILE, {"Content-Length": str(len(FILE))})
    return raw


def test_a_broken_download_keeps_its_part_and_asks_only_for_the_rest(tmp_path, monkeypatch):
    c = mclient.Client("127.0.0.1", 1, fp="ab")
    dest = tmp_path / "clip.mp4"
    first = Resp(200, FILE, {"Content-Length": str(len(FILE))}, break_after=300 * 1024)
    monkeypatch.setattr(c, "raw", lambda *a, **k: first)
    with pytest.raises(mclient.Offline, match="part way"):
        c.download("/f", dest, resume=True, expect=len(FILE))
    part = dest.with_name("clip.mp4.part")
    assert part.stat().st_size == 300 * 1024 and not dest.exists()
    asked = []
    monkeypatch.setattr(c, "raw", serve(asked))
    seen = []
    c.download("/f", dest, lambda got, total: seen.append((got, total)), resume=True, expect=len(FILE))
    assert asked == [{"Range": f"bytes={300 * 1024}-"}]
    assert dest.read_bytes() == FILE and not part.exists()
    assert seen[-1] == (len(FILE), len(FILE)) and seen[0][0] > 300 * 1024  # progress counts what was there


def test_without_resume_a_break_leaves_nothing(tmp_path, monkeypatch):
    c = mclient.Client("127.0.0.1", 1, fp="ab")
    dest = tmp_path / "clip.mp4"
    monkeypatch.setattr(c, "raw", lambda *a, **k: Resp(200, FILE, {"Content-Length": str(len(FILE))},
                                                       break_after=1000))
    with pytest.raises(mclient.Offline):
        c.download("/f", dest)
    assert not dest.with_name("clip.mp4.part").exists()


def test_a_part_that_doesnt_fit_the_file_starts_again(tmp_path, monkeypatch):
    c = mclient.Client("127.0.0.1", 1, fp="ab")
    dest = tmp_path / "clip.mp4"
    part = dest.with_name("clip.mp4.part")
    asked = []
    part.write_bytes(b"x" * (len(FILE) + 5))  # bigger than the file: not a part of it
    monkeypatch.setattr(c, "raw", serve(asked))
    c.download("/f", dest, resume=True, expect=len(FILE))
    assert asked == [{}] and dest.read_bytes() == FILE
    # the PC has another version of the file now (a different size)
    part.write_bytes(FILE[:1000])
    monkeypatch.setattr(c, "raw", serve(asked, total=len(FILE) + 1))
    with pytest.raises(mclient.Offline, match="different part"):
        c.download("/f", dest, resume=True, expect=len(FILE))
    assert not part.exists()
    # or no longer has that much of it
    part.write_bytes(FILE[:1000])
    monkeypatch.setattr(c, "raw", serve(asked, status=416))
    with pytest.raises(mclient.Offline, match="changed on the PC"):
        c.download("/f", dest, resume=True, expect=len(FILE))
    assert not part.exists()


def test_a_pc_that_ignores_the_range_sends_it_all_again(tmp_path, monkeypatch):
    c = mclient.Client("127.0.0.1", 1, fp="ab")
    dest = tmp_path / "clip.mp4"
    dest.with_name("clip.mp4.part").write_bytes(FILE[:5000])
    monkeypatch.setattr(c, "raw", serve([], whole=True))
    c.download("/f", dest, resume=True, expect=len(FILE))
    assert dest.read_bytes() == FILE  # written over, not added to


def test_a_part_from_the_wrong_offset_is_refused(tmp_path, monkeypatch):
    c = mclient.Client("127.0.0.1", 1, fp="ab")
    dest = tmp_path / "clip.mp4"
    dest.with_name("clip.mp4.part").write_bytes(FILE[:5000])
    monkeypatch.setattr(c, "raw", serve([], start_shift=10))
    with pytest.raises(mclient.Offline, match="different part"):
        c.download("/f", dest, resume=True, expect=len(FILE))
    assert not dest.exists()


@pytest.mark.parametrize("header, want", [("bytes 100-199/1000", (100, 1000)), ("bytes 0-0/*", (0, 0)),
                                          ("", (-1, 0)), ("items 1-2/3", (-1, 0))])
def test_content_range(header, want):
    assert mclient._content_range(header) == want


def test_a_real_pc_resumes_a_video_download(world, tmp_path):  # noqa: F811
    clip_folder(world.root, "Psychology/2026-11-08 Big Talk [abc]")
    creator_folder(world.root)
    world.take()
    c, _ = world.linked()
    vid = c.get("/api/videos")["items"][0]["id"]
    whole = c.fetch(c.video_path(vid, "file"))
    dest = tmp_path / "v.mp4"
    dest.with_name("v.mp4.part").write_bytes(whole[:len(whole) // 3])
    c.download(c.video_path(vid, "file"), dest, resume=True, expect=len(whole))
    assert dest.read_bytes() == whole


# ---------------------------------------------------------------- paths between the computers


@pytest.mark.parametrize("rel", [
    "", "   ", "/etc/passwd", "\\Windows\\System32", "C:\\Users\\me\\a.mp4", "C:a.mp4", "\\\\server\\share\\a.mp4",
    "../a.mp4", "Gaming/../../a.mp4", "Gaming\\..\\..\\a.mp4", "a\x00b.mp4", "/".join(["d"] * 13) + "/a.mp4",
])
def test_paths_that_could_land_anywhere_are_refused(tmp_path, rel):
    with pytest.raises(UnsafePath):
        paths.from_wire(rel, tmp_path, "mac")


@pytest.mark.parametrize("rel, platform, want", [
    ("Gaming/2026-11-10 Big play/01 - Big play.mp4", "mac", ("Gaming", "2026-11-10 Big play", "01 - Big play.mp4")),
    ("Gaming\\2026-11-10 Big play\\01.mp4", "mac", ("Gaming", "2026-11-10 Big play", "01.mp4")),
    ("./Gaming//a.mp4", "linux", ("Gaming", "a.mp4")),
    ("Talk/Q: why?.mp4", "windows", ("Talk", "Q- why-.mp4")),
    ("Talk/Q: why?.mp4", "mac", ("Talk", "Q- why?.mp4")),
    ("Talk/CON.mp4", "windows", ("Talk", "_CON.mp4")),
    ("Talk/con", "windows", ("Talk", "_con")),
    ("Talk/console.mp4", "windows", ("Talk", "console.mp4")),
    ("Talk/ends with dots...", "windows", ("Talk", "ends with dots")),
    ("Talk/._hidden.mp4", "mac", ("Talk", "_hidden.mp4")),
    ("Talk/a\x07b.mp4", "windows", ("Talk", "a-b.mp4")),
])
def test_each_name_is_made_safe_for_where_it_lands(tmp_path, rel, platform, want):
    assert paths.from_wire(rel, tmp_path, platform) == tmp_path.joinpath(*want)


def test_a_long_name_keeps_its_extension():
    name = paths.safe_part("x" * 300 + ".mp4", "linux")
    assert len(name) == paths.MAX_PART and name.endswith(".mp4")


def test_the_same_name_always_lands_in_the_same_place(tmp_path):
    a = paths.from_wire("Talk/Q: why?.mp4", tmp_path, "windows")
    assert a == paths.from_wire("Talk/Q: why?.mp4", tmp_path, "windows")


def test_names_that_are_nothing_are_refused():
    for name in ("", " ", ".", "..", "...", "   .  "):
        with pytest.raises(UnsafePath):
            paths.safe_part(name, "windows")


def test_a_link_already_in_the_folder_cant_lead_out(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "Gaming").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links aren't allowed here")
    with pytest.raises(UnsafePath, match="outside"):
        paths.from_wire("Gaming/a.mp4", root, "mac")


def test_to_wire_sends_only_relative_paths_with_slashes(tmp_path):
    f = tmp_path / "Gaming" / "a.mp4"
    assert paths.to_wire(f, tmp_path) == "Gaming/a.mp4"
    with pytest.raises(UnsafePath):
        paths.to_wire(Path("/somewhere/else.mp4"), tmp_path)
    with pytest.raises(UnsafePath):
        paths.to_wire(tmp_path, tmp_path)


def test_what_the_pc_sends_lands_inside_the_macs_folder(tmp_path):
    """to_wire on the PC, from_wire on the Mac: the same place under each one's own folder."""
    pc_root = tmp_path / "pc"
    rel = paths.to_wire(pc_root / "Gaming" / "2026-11-10 Big play" / "01 - Big play.mp4", pc_root)
    mac_root = tmp_path / "mac"
    assert paths.from_wire(rel, mac_root, "mac") == mac_root / "Gaming" / "2026-11-10 Big play" / "01 - Big play.mp4"


@pytest.mark.parametrize("path, root, want", [
    ("C:\\Users\\Me\\Videos\\ClipMonster\\Gaming\\a.mp4", "C:\\Users\\me\\videos\\clipmonster", ("Gaming", "a.mp4")),
    ("/Users/me/Movies/ClipMonster/Gaming/a.mp4", "/Users/me/Movies/ClipMonster", ("Gaming", "a.mp4")),
    ("/home/me/ClipMonster/Talk/b.mp4", "/home/me/ClipMonster/", ("Talk", "b.mp4")),
])
def test_translate_maps_one_computers_folder_onto_the_other(tmp_path, path, root, want):
    assert paths.translate(path, root, tmp_path, "mac") == tmp_path.joinpath(*want)


@pytest.mark.parametrize("path, root", [
    ("C:\\Users\\me\\Desktop\\a.mp4", "C:\\Users\\me\\Videos\\ClipMonster"),  # not under the folder
    ("/Users/Me/Movies/ClipMonster/a.mp4", "/Users/me/Movies/ClipMonster"),  # a Mac path: case counts
    ("C:\\Users\\me\\Videos\\ClipMonster", "C:\\Users\\me\\Videos\\ClipMonster"),  # the folder itself
    ("/Users/me/Movies/ClipMonster/a.mp4", "C:\\Users\\me\\Videos\\ClipMonster"),  # different computers
    ("C:\\Users\\me\\Videos\\ClipMonster\\..\\..\\x.mp4", "C:\\Users\\me\\Videos\\ClipMonster"),
    ("", "C:\\x"), ("C:\\x\\a.mp4", ""),
])
def test_translate_says_none_for_what_isnt_under_the_folder(tmp_path, path, root):
    assert paths.translate(path, root, tmp_path, "mac") is None


def test_this_computer_is_named():
    import sys

    want = "windows" if os.name == "nt" else "mac" if sys.platform == "darwin" else "linux"
    assert paths.platform_name() == want

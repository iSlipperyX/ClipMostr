"""ClipMonster for Mac making clips, and the PC lending it a hand (ytclip/mac/pc_ai.py, pc_jobs.py and window.py;
ytclip/remote/studio.py on the PC): the PC's local AI answering the Mac's questions, whole videos sent to the
PC and their clips brought back into the Mac's Library, against a real host server with a stand-in for the
PC's engine. Also the Mac's own pieces: its words in Settings, keys kept out of settings.json, the media
engine, fonts, and what the packaged app brings."""

import copy
import io
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from test_remote import mac_home, world  # noqa: F401 - fixtures
from ytclip import ai
from ytclip.analyze import Clip
from ytclip.ffmpeg import Cancelled
from ytclip.mac.client import ApiError, Offline
from ytclip.mac.pc_ai import PCAI
from ytclip.remote import studio
from ytclip.remote.host import HostError
from ytclip.remote.studio import AI_MAX, AITasks, MacJobs, _file_name, clean_settings
from ytclip.settings import Settings
from ytclip.subtitles import Cue

URL = "https://www.youtube.com/watch?v=abcdefghijk"
FOLDER = "Chan/2026-11-09 Big Talk [abc]"


class FakeEngine:
    """The PC's Engine for a video a Mac sent: it 'downloads' the video, finds two clips and 'exports' them as
    files for the Mac to fetch. `gate`, while set to an unset Event, holds it part way (to stop it there)."""

    made: list = []  # (settings, source) of each video made
    sizes: dict = {}  # file name -> size, of the last export
    gate = None

    def __init__(self, s, log=None, progress=None, cancelled=None):
        self.s, self.progress, self.cancelled = s, progress, cancelled

    def prepare(self, p):
        FakeEngine.made.append((self.s, p.source))
        self.progress("Downloading", 0.5)
        while FakeEngine.gate is not None and not FakeEngine.gate.is_set():
            if self.cancelled():
                raise Cancelled()
            time.sleep(0.02)
        p.video = SimpleNamespace(title="Big Talk", channel="Chan", id="abc", url=p.source)
        p.clips = [Clip(10, 40, 80, "One"), Clip(60, 90, 70, "Two")]

    def export(self, p):
        folder = Path(self.s.output_path) / FOLDER
        (folder / "thumbnails").mkdir(parents=True)
        (folder / "_rejected").mkdir()
        for i in (1, 2):
            data = os.urandom(300_000 + i)
            (folder / f"0{i} - clip.mp4").write_bytes(data)
            FakeEngine.sizes[f"0{i} - clip.mp4"] = len(data)
            (folder / "thumbnails" / f"0{i}.jpg").write_bytes(b"\xff\xd8 a picture")
        (folder / "_rejected" / "03 - weak.mp4").write_bytes(b"x")
        (folder / ".scratch").write_text("work in progress")
        meta = {"source": p.source, "video": {"title": "Big Talk", "channel": "Chan", "url": p.source},
                "clips": [{"file": f"0{i} - clip.mp4", "watch": p.source} for i in (1, 2)]}
        (folder / "clips.json").write_text(json.dumps(meta), encoding="utf-8")
        p.qc = {"passed": 2, "rejected": 0}
        return folder


@pytest.fixture(autouse=True)
def fresh_engine():
    FakeEngine.made, FakeEngine.sizes, FakeEngine.gate = [], {}, None
    yield
    if FakeEngine.gate is not None:
        FakeEngine.gate.set()
    ai.use_remote(None)


def mac_jobs(tmp_path, **settings) -> MacJobs:
    s = Settings(output_dir=str(tmp_path / "pc-out"), **settings)
    events = []
    mj = MacJobs(lambda: s, lambda m: None,
                 lambda kind, text, item="", device="": events.append((kind, text, item, device)),
                 root=tmp_path / "mac_jobs", engine=FakeEngine)
    mj.said = events
    return mj


# ================================================================== the PC: whole videos for a Mac


def test_names_from_the_mac_are_safe_on_the_pc():
    assert _file_name("Holiday: Day 1?.MOV") == "Holiday Day 1.mov"
    assert _file_name("../../Windows/system32/evil.mp4") == "evil.mp4"
    assert _file_name("C:\\Users\\x\\clip.mp4") == "clip.mp4"
    assert _file_name("CON.mp4") == "_CON.mp4" and _file_name("lpt1.mkv") == "_lpt1.mkv"
    assert _file_name(" .hidden. ") == "hidden"
    assert _file_name("a" * 300 + ".mp4") == "a" * 120 + ".mp4"
    assert _file_name("") == "video"


def test_only_the_macs_clip_settings_travel():
    sent = {"clip_count": 4, "max_len": True, "min_len": "20", "burn_captions": 1, "caption_font": "Arial",
            "aspect": "vertical_crop", "ai_polish": True, "sport": "football", "encoder": "libx264",
            "output_dir": "/Users/justyn/Movies", "ai_api_key": "sk-ant-x", "telegram_token": "t",
            "stack_video": "/Users/justyn/minecraft.mp4", "auto_output_dir": "/x", "not_a_setting": 1}
    assert clean_settings(sent) == {"clip_count": 4, "caption_font": "Arial", "aspect": "vertical_crop",
                                    "ai_polish": True, "sport": "football"}
    assert clean_settings("nonsense") == {}
    names = studio.travelling()
    assert "clip_count" in names and "whisper_model" in names
    assert not {"encoder", "output_dir", "ai_api_key", "gemini_api_key", "ai_provider"} & set(names)


def test_a_video_sent_from_the_mac_is_kept_under_its_own_name(tmp_path):
    mj = mac_jobs(tmp_path)
    data = os.urandom(5000)
    chunks = iter([data[:3000], data[3000:]])
    got = mj.receive("Holiday: Day 1.MOV", len(data), lambda n: next(chunks))
    f = mj._upload_file(got["upload"])
    assert got["size"] == 5000 and f.name == "Holiday Day 1.mov" and f.read_bytes() == data
    assert f.parent.parent == tmp_path / "mac_jobs" / "uploads"

    with pytest.raises(HostError) as no:
        mj.receive("notes.txt", 10, lambda n: b"x" * n)
    assert no.value.status == 400
    with pytest.raises(HostError) as no:
        mj.receive("clip.mp4", 0, lambda n: b"")
    assert no.value.status == 411
    with pytest.raises(HostError) as no:
        mj.receive("clip.mp4", studio.UPLOAD_MAX + 1, lambda n: b"")
    assert no.value.status == 413
    parts = iter([b"x" * 400, b""])
    with pytest.raises(HostError):  # stopped part way: nothing is left behind
        mj.receive("clip.mp4", 1000, lambda n: next(parts))
    assert [p.name for p in (tmp_path / "mac_jobs" / "uploads").iterdir()] == [got["upload"]]


def test_the_pc_says_how_big_a_video_it_has_room_for(tmp_path, monkeypatch):
    mj = mac_jobs(tmp_path)
    gb = 1024 ** 3
    monkeypatch.setattr(studio.shutil, "disk_usage", lambda p: SimpleNamespace(free=10 * gb))
    assert mj.room() == 8 * gb  # what's free, less room for the clips
    monkeypatch.setattr(studio.shutil, "disk_usage", lambda p: SimpleNamespace(free=gb))
    assert mj.room() == 0
    with pytest.raises(HostError) as no:
        mj.receive("big.mp4", 100, lambda n: b"x" * n)
    assert no.value.status == 507


def test_the_pc_refuses_what_it_cant_make(tmp_path):
    mj = mac_jobs(tmp_path)
    for body in ({}, {"source": "not a link"}, {"source": "https://" + "x" * 2000}):
        with pytest.raises(HostError) as no:
            mj.add("mac1", "MacBook", body)
        assert no.value.status == 400
    with pytest.raises(HostError) as no:
        mj.add("mac1", "MacBook", {"upload": "0123456789abcdef"})  # never sent
    assert no.value.status == 404
    for _ in range(studio.JOBS_MAX):
        mj.add("mac1", "MacBook", {"source": URL})
    with pytest.raises(HostError) as no:
        mj.add("mac1", "MacBook", {"source": URL})
    assert no.value.status == 429


def test_the_pc_makes_a_video_with_the_macs_settings(tmp_path):
    mj = mac_jobs(tmp_path)
    j = mj.add("mac1", "Justyn's MacBook", {"source": URL, "settings": {"clip_count": 3, "encoder": "h264_nvenc"}})
    assert j["status"] == "queued" and mj.jobs_for("mac1")["jobs"][0]["waiting"] == 0
    assert mj.jobs_for("someone-else")["jobs"] == []
    mj.make(mj._next())

    got = mj.job(j["id"], "mac1")
    assert got["status"] == "done" and got["clips"] == 2 and got["title"] == "Big Talk"
    assert got["folder"] == FOLDER
    paths = [f["path"] for f in got["files"]]
    assert f"{FOLDER}/01 - clip.mp4" in paths and f"{FOLDER}/clips.json" in paths
    assert f"{FOLDER}/thumbnails/01.jpg" in paths
    assert not [p for p in paths if "_rejected" in p or "/." in p]  # rejected clips and scratch files stay
    used, source = FakeEngine.made[-1]
    assert source == URL and used.clip_count == 3 and used.encoder == "auto"  # its clips, this PC's encoder
    assert Path(used.output_dir) == tmp_path / "mac_jobs" / j["id"] / "out"
    assert used.keep_source is False and used.auto_export is True
    assert mj.said[-1][0] == "studio" and "ready for your Mac" in mj.said[-1][1]

    assert mj.file(j["id"], "mac1", f"{FOLDER}/01 - clip.mp4").stat().st_size == FakeEngine.sizes["01 - clip.mp4"]
    for bad in ("../jobs.json", f"{FOLDER}/_rejected/03 - weak.mp4", "/etc/passwd"):
        with pytest.raises(HostError):
            mj.file(j["id"], "mac1", bad)
    with pytest.raises(HostError):
        mj.file(j["id"], "another-mac", f"{FOLDER}/01 - clip.mp4")

    mj.done(j["id"], "mac1")  # the Mac has them: they go
    assert not (tmp_path / "mac_jobs" / j["id"]).exists() and mj.jobs_for("mac1")["jobs"] == []


def test_a_sent_video_goes_once_its_clips_are_made(tmp_path):
    mj = mac_jobs(tmp_path)
    up = mj.receive("My Clip.mov", 4, lambda n: b"abcd")
    j = mj.add("mac1", "MacBook", {"upload": up["upload"], "name": "My Clip.mov"})
    assert j["name"] == "My Clip.mov" and j["source"] == ""
    mj.make(mj._next())
    assert Path(FakeEngine.made[-1][1]).name == "My Clip.mov"
    assert mj.job(j["id"], "mac1")["status"] == "done"
    assert not (tmp_path / "mac_jobs" / "uploads" / up["upload"]).exists()


def test_stopping_a_video_on_the_pc(tmp_path):
    mj = mac_jobs(tmp_path)
    waiting = mj.add("mac1", "MacBook", {"source": URL})
    assert mj.cancel(waiting["id"], "mac1")["status"] == "cancelled"

    FakeEngine.gate = threading.Event()
    j = mj.add("mac1", "MacBook", {"source": URL})
    t = threading.Thread(target=mj.make, args=(mj._next(),))
    t.start()
    end = time.time() + 5
    while mj.job(j["id"], "mac1")["status"] != "working" and time.time() < end:
        time.sleep(0.02)
    mj.cancel(j["id"], "mac1")
    t.join(5)
    got = mj.job(j["id"], "mac1")
    assert got["status"] == "cancelled" and got["error"] == "You stopped it."
    assert not (tmp_path / "mac_jobs" / j["id"]).exists()

    j = mj.add("mac1", "MacBook", {"source": URL})  # let go of while it's being made: stopped, then gone
    t = threading.Thread(target=mj.make, args=(mj._next(),))
    t.start()
    end = time.time() + 5
    while mj.job(j["id"], "mac1")["status"] != "working" and time.time() < end:
        time.sleep(0.02)
    assert "Stopping" in mj.done(j["id"], "mac1")["message"]
    t.join(5)
    with pytest.raises(HostError):
        mj.job(j["id"], "mac1")
    assert not (tmp_path / "mac_jobs" / j["id"]).exists() and not mj._forget


def test_automation_stopping_puts_the_video_back_in_line(tmp_path):
    mj = mac_jobs(tmp_path)
    FakeEngine.gate = threading.Event()
    j = mj.add("mac1", "MacBook", {"source": URL})
    t = threading.Thread(target=mj.make, args=(mj._next(),))
    t.start()
    end = time.time() + 5
    while mj.job(j["id"], "mac1")["status"] != "working" and time.time() < end:
        time.sleep(0.02)
    mj.stopping.set()
    t.join(5)
    assert mj.job(j["id"], "mac1")["status"] == "queued"  # it starts again next time
    FakeEngine.gate.set()
    mj.stopping.clear()
    mj.make(mj._next())
    assert mj.job(j["id"], "mac1")["status"] == "done"


def test_a_video_that_fails_says_why(tmp_path):
    class Broken(FakeEngine):
        def prepare(self, p):
            raise RuntimeError("This video is private.")

    mj = mac_jobs(tmp_path)
    mj._engine = Broken
    j = mj.add("mac1", "MacBook", {"source": URL})
    mj.make(mj._next())
    got = mj.job(j["id"], "mac1")
    assert got["status"] == "failed" and got["error"] == "This video is private."
    assert "✗" in mj.said[-1][1]


def working(mj, job_id, device="mac1"):
    """Wait until the video is being made."""
    end = time.time() + 5
    while mj.job(job_id, device)["status"] != "working" and time.time() < end:
        time.sleep(0.02)


def test_the_list_is_saved_though_windows_is_reading_it(tmp_path, monkeypatch):
    mj = mac_jobs(tmp_path)
    real, tries = Path.replace, []

    def replace(self, target):
        if self.name == "jobs.json.tmp":
            tries.append(1)
            if len(tries) < 4:  # Windows: a Mac's poll has jobs.json open this instant
                raise PermissionError(13, "Access is denied")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(studio.time, "sleep", lambda s: None)
    j = mj.add("mac1", "MacBook", {"source": URL})
    assert len(tries) == 4 and mj.job(j["id"], "mac1")["status"] == "queued"


def test_a_macs_poll_reads_the_list_once_and_under_the_lock(tmp_path, monkeypatch):
    mj = mac_jobs(tmp_path)
    up = mj.receive("My Clip.mov", 4, lambda n: b"abcd")
    mj.add("mac1", "MacBook", {"upload": up["upload"]})
    mj.add("mac1", "MacBook", {"source": URL})
    load, reads = mj._load, []

    def loading():
        reads.append(mj.lock._is_owned())
        return load()

    monkeypatch.setattr(mj, "_load", loading)
    got = mj.jobs_for("mac1")
    assert [j["waiting"] for j in got["jobs"]] == [1, 0] and reads == [True]
    del reads[:]
    mj._prune_uploads()
    assert reads == [True]


def test_progress_that_cant_be_saved_never_fails_the_video(tmp_path, monkeypatch):
    clock = [1000.0]
    save = {}

    class Slow(FakeEngine):
        def prepare(self, p):
            monkeypatch.setattr(mj, "_save", save["broken"])
            for i in range(3):
                clock[0] += 5  # each line is due to be saved
                self.progress("Transcribing", i / 3)
            monkeypatch.setattr(mj, "_save", save["real"])
            super().prepare(p)

    def broken(jobs):
        raise PermissionError(13, "Access is denied")

    mj = mac_jobs(tmp_path)
    mj._engine, mj.now = Slow, lambda: clock[0]
    save.update(real=mj._save, broken=broken)
    j = mj.add("mac1", "MacBook", {"source": URL})
    mj.make(mj._next())
    assert mj.job(j["id"], "mac1")["status"] == "done"


def test_one_bad_save_never_ends_the_making(tmp_path, monkeypatch):
    mj = mac_jobs(tmp_path)
    monkeypatch.setattr(studio, "LOOP_RETRY", 0.01)
    nxt, change, failed = mj._next, mj._change, []

    def flaky_next():
        if not failed:
            failed.append("next")
            raise PermissionError(13, "Access is denied")
        return nxt()

    def flaky_change(job_id, **changes):
        if changes.get("status") in ("working", "failed") and len(failed) < 3:
            failed.append(changes["status"])  # the save as it starts, then the one that says it failed
            raise PermissionError(13, "Access is denied")
        return change(job_id, **changes)

    monkeypatch.setattr(mj, "_next", flaky_next)
    monkeypatch.setattr(mj, "_change", flaky_change)
    j = mj.add("mac1", "MacBook", {"source": URL})
    mj.start()
    try:
        end = time.time() + 10
        while mj.job(j["id"], "mac1")["status"] != "done" and time.time() < end:
            time.sleep(0.02)
        assert failed == ["next", "working", "failed"] and mj._thread.is_alive()
        assert mj.job(j["id"], "mac1")["status"] == "done"
    finally:
        mj.stop()


def test_videos_still_arriving_count_against_the_room(tmp_path, monkeypatch):
    mj = mac_jobs(tmp_path)
    gb = 1024 ** 3
    monkeypatch.setattr(studio.shutil, "disk_usage", lambda p: SimpleNamespace(free=10 * gb))
    arriving, go = threading.Event(), threading.Event()

    def read(n):
        arriving.set()
        go.wait(5)
        return b""  # then it stops part way

    errors = []

    def first():
        try:
            mj.receive("one.mp4", 5 * gb, read)
        except HostError as exc:
            errors.append(exc.status)

    t = threading.Thread(target=first)
    t.start()
    try:
        assert arriving.wait(5)
        assert mj.room() == 3 * gb  # 10 free, 5 on their way, 2 kept for the clips
        with pytest.raises(HostError) as no:
            mj.check_upload("two.mp4", 4 * gb)
        assert no.value.status == 507
        with pytest.raises(HostError) as no:
            mj.receive("two.mp4", 4 * gb, lambda n: b"")
        assert no.value.status == 507
        assert mj.check_upload("small.mp4", 2 * gb) == "small.mp4"
    finally:
        go.set()
        t.join(5)
    assert errors == [400] and mj.room() == 8 * gb and not mj._incoming


def test_a_video_made_for_a_mac_never_spends_the_pcs_keys(tmp_path, monkeypatch):
    from ytclip.intelligence import llama_engine

    mj = mac_jobs(tmp_path, ai_provider="claude", ai_api_key="sk-ant-pc", gemini_api_key="g-pc", ai_fallback=True)
    monkeypatch.setattr(llama_engine, "backend_name", lambda: "llama.cpp")
    job = {"id": "0123456789abcdef", "settings": {"mode": "ai", "ai_polish": True}}

    monkeypatch.setattr(ai, "local_ready", lambda s: False)  # no model yet: smart signals, nothing polished
    s = mj.settings_for(job)
    assert (s.ai_provider, s.ai_fallback, s.mode, s.ai_polish) == ("local", False, "auto", False)
    assert not {"claude", "gemini"} & set(ai.order(s))

    monkeypatch.setattr(ai, "local_ready", lambda s: True)
    s = mj.settings_for(job)
    assert (s.ai_provider, s.ai_fallback, s.mode, s.ai_polish) == ("local", False, "ai", True)
    assert ai.order(s) == ["local"]  # and when it fails, nothing else is asked

    def cant(s):
        raise OSError("the model folder can't be read")

    monkeypatch.setattr(ai, "local_ready", cant)
    s = mj.settings_for(dict(job, settings={"mode": "heatmap", "ai_polish": True}))
    assert (s.ai_provider, s.mode, s.ai_polish) == ("local", "heatmap", False)


def test_clean_up_leaves_the_videos_being_made_for_a_mac(tmp_path, isolated_profile):
    from ytclip import cleanup
    from ytclip.jobs import JobDB
    from ytclip.settings import app_dir

    mj = mac_jobs(tmp_path)
    mj.root = app_dir() / "mac_jobs"
    db = JobDB(tmp_path / "jobs.db")
    none = tmp_path / "none.json"
    assert cleanup.busy_ids(db, none) == set()
    waiting = mj.add("mac1", "MacBook", {"source": "https://youtu.be/waiting1234"})
    j = mj.add("mac1", "MacBook", {"source": URL})
    assert cleanup.busy_ids(db, none) == {"waiting1234", "abcdefghijk"}

    FakeEngine.gate = threading.Event()
    mj.cancel(waiting["id"], "mac1")
    t = threading.Thread(target=mj.make, args=(mj._next(),))
    t.start()
    working(mj, j["id"])
    assert cleanup.busy_ids(db, none) == {"abcdefghijk"}
    FakeEngine.gate.set()
    t.join(5)
    saved = json.loads((mj.root / "jobs.json").read_text(encoding="utf-8"))["jobs"]
    assert [x.get("video_id") for x in saved if x["id"] == j["id"]] == ["abc"]  # its own id, once it was opened
    assert cleanup.busy_ids(db, none) == set()  # made: nothing of it is needed in the cache any more

    (mj.root / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "a", "status": "working", "source": "https://example.com/odd", "video_id": "realid12345"}, "junk"]}))
    assert cleanup.busy_ids(db, none) == {"https://example.com/odd", "realid12345"}
    (mj.root / "jobs.json").write_text("not json")
    assert cleanup.busy_ids(db, none) == set()


def test_an_unlinked_macs_videos_are_dropped(tmp_path):
    mj = mac_jobs(tmp_path)
    linked = {"mac1", "mac2"}
    mj.linked = lambda: linked
    made = mj.add("mac2", "Old Mac", {"source": URL})
    mj.make(mj._next())  # one is made and waits to be fetched
    up = mj.receive("Theirs.mov", 4, lambda n: b"abcd")
    first = mj.add("mac2", "Old Mac", {"upload": up["upload"]})
    for _ in range(studio.JOBS_MAX - 1):
        mj.add("mac2", "Old Mac", {"source": URL})
    with pytest.raises(HostError) as no:
        mj.add("mac1", "MacBook", {"source": URL})
    assert no.value.status == 429

    linked = None  # remote.json can't be read just now: nothing is dropped
    assert mj._next()["id"] == first["id"] and len(mj._load()) == studio.JOBS_MAX + 1
    linked = {"mac1"}  # Old Mac was unlinked: its videos no longer count, and none is made
    mine = mj.add("mac1", "MacBook", {"source": URL})
    assert mj._next()["id"] == mine["id"]
    assert [j["device"] for j in mj._load()] == ["mac1"]
    assert not (tmp_path / "mac_jobs" / "uploads" / up["upload"]).exists()
    assert not (tmp_path / "mac_jobs" / made["id"]).exists()  # the clips nobody can fetch


def test_a_video_being_made_stops_when_its_mac_is_unlinked(tmp_path, monkeypatch):
    mj = mac_jobs(tmp_path)
    monkeypatch.setattr(studio, "LINK_CHECK", 0.0)
    linked = {"mac1"}
    mj.linked = lambda: linked
    FakeEngine.gate = threading.Event()
    j = mj.add("mac1", "MacBook", {"source": URL})
    t = threading.Thread(target=mj.make, args=(mj._next(),))
    t.start()
    working(mj, j["id"])
    assert mj._next() is None and t.is_alive()  # it isn't touched while its Mac is linked
    linked = set()
    t.join(5)
    assert not t.is_alive() and mj._load() == [] and not mj._forget and not mj._cancel
    assert not (tmp_path / "mac_jobs" / j["id"]).exists()


def test_a_refused_videos_upload_doesnt_stay(tmp_path):
    mj = mac_jobs(tmp_path)
    uploads = tmp_path / "mac_jobs" / "uploads"
    used = mj.receive("First.mov", 4, lambda n: b"abcd")
    mj.add("mac1", "MacBook", {"upload": used["upload"]})
    for _ in range(studio.JOBS_MAX - 1):
        mj.add("mac1", "MacBook", {"source": URL})
    up = mj.receive("Late.mov", 4, lambda n: b"abcd")
    for sent in (up["upload"], used["upload"]):
        with pytest.raises(HostError) as no:
            mj.add("mac1", "MacBook", {"upload": sent})
        assert no.value.status == 429
    assert [p.name for p in uploads.iterdir()] == [used["upload"]]  # the one a waiting video is made from stays


def test_stray_uploads_are_swept_without_waiting_for_another(tmp_path, monkeypatch):
    mj = mac_jobs(tmp_path)
    uploads = tmp_path / "mac_jobs" / "uploads"
    used = mj.receive("First.mov", 4, lambda n: b"abcd")
    mj.add("mac1", "MacBook", {"upload": used["upload"]})
    mj.receive("Stray.mov", 4, lambda n: b"abcd")
    (uploads / "0123456789abcdef").mkdir()
    (uploads / "0123456789abcdef" / "Half.mov.part").write_bytes(b"ab")  # automation was killed mid-upload
    assert mj._next() is not None and len(list(uploads.iterdir())) == 3  # fresh ones are left alone
    mj.now = lambda: time.time() + studio.UPLOAD_KEEP + 60
    assert mj._next() is not None
    assert [p.name for p in uploads.iterdir()] == [used["upload"]]

    gb = 1024 ** 3
    for arrive in (lambda: mj.check_upload("New.mov", gb) == "New.mov",
                   lambda: mj.receive("New.mov", 4, lambda n: b"abcd")["size"] == 4):
        mj.now = time.time
        stray = uploads / mj.receive("Stray.mov", 4, lambda n: b"abcd")["upload"]
        mj.now = lambda: time.time() + studio.UPLOAD_KEEP + 60
        monkeypatch.setattr(studio.shutil, "disk_usage",  # the stray one is what fills the disk
                            lambda p: SimpleNamespace(free=(1 if stray.exists() else 10) * gb))
        assert arrive() and not stray.exists()


def test_news_of_a_video_is_for_the_mac_that_sent_it(tmp_path):
    mj = mac_jobs(tmp_path)
    j = mj.add("mac1", "MacBook", {"source": URL})
    mj.make(mj._next())
    assert mj.said[-1] == ("studio", "✓ 2 clips from Big Talk are ready for your Mac", j["id"], "mac1")

    class Broken(FakeEngine):
        def prepare(self, p):
            raise RuntimeError("This video is private.")

    mj._engine = Broken
    j = mj.add("mac2", "iMac", {"source": URL})
    mj.make(mj._next())
    assert mj.said[-1][2:] == (j["id"], "mac2")


# ================================================================== the PC: its local AI for a Mac


def ai_tasks(ask=None, find=None, ready=True, now=time.time) -> AITasks:
    t = AITasks(lambda: Settings(), lambda m: None, now, ask=ask, find=find)
    t._look = lambda: {"ready": ready, "model": "Qwen2.5 7B Instruct", "downloaded": True,
                       "why": "" if ready else "Your PC has no local AI: install it."}
    return t


def until_done(t, task_id, device="mac1"):
    end = time.time() + 10
    got = t.get(task_id, device, wait=2)
    while got["state"] in ("waiting", "working") and time.time() < end:
        got = t.get(task_id, device, wait=2)
    return got


def test_the_pcs_ai_answers_a_macs_question():
    def ask(s, body, log, check, progress):
        progress("Reading part 1 of 2", 0.5)
        return {"data": {"clips": [1]}, "model": "Qwen2.5 7B Instruct"}

    t = ai_tasks(ask=ask)
    started = t.start("mac1", {"kind": "ask", "content": "words", "schema": {}})
    got = until_done(t, started["id"])
    assert got["state"] == "done" and got["result"] == {"data": {"clips": [1]}, "model": "Qwen2.5 7B Instruct"}
    with pytest.raises(HostError) as no:
        t.get(started["id"], "another-mac")
    assert no.value.status == 404
    with pytest.raises(HostError) as no:
        t.start("mac1", {"kind": "write me a poem"})
    assert no.value.status == 400


def test_the_pcs_ai_says_when_it_cant():
    t = ai_tasks(ready=False)
    with pytest.raises(HostError) as no:
        t.start("mac1", {"kind": "ask"})
    assert no.value.status == 409 and "no local AI" in no.value.message

    def cut(s, body, log, check, progress):
        raise ai.AICutOff("The answer was cut off.")

    t = ai_tasks(ask=cut)
    got = until_done(t, t.start("mac1", {"kind": "ask"})["id"])
    assert got["state"] == "failed" and got["error"] == "The answer was cut off."
    assert got["result"] == {"kind": "cut_off"}  # so the Mac raises the same


def test_the_long_poll_answers_with_what_the_mac_hasnt_seen():
    step = threading.Event()

    def ask(s, body, log, check, progress):
        progress("Reading part 1 of 2", 0.5)
        step.wait(5)
        return {"data": {}, "model": "m"}

    t = ai_tasks(ask=ask)
    started = t.start("mac1", {"kind": "ask"})
    end = time.time() + 5
    while t.tasks[started["id"]].stage != "Reading part 1 of 2" and time.time() < end:
        time.sleep(0.01)
    t0 = time.time()
    got = t.get(started["id"], "mac1", wait=5, rev=0)  # it changed since the Mac last heard: no waiting
    assert time.time() - t0 < 1 and got["stage"] == "Reading part 1 of 2" and got["rev"] > 0
    t0 = time.time()
    again = t.get(started["id"], "mac1", wait=0.5, rev=got["rev"])  # nothing new: it waits it out
    assert time.time() - t0 >= 0.45 and again["rev"] == got["rev"]
    step.set()
    assert until_done(t, started["id"])["state"] == "done"


def test_a_question_the_mac_stops_stops_on_the_pc():
    running, stopped = threading.Event(), threading.Event()

    def ask(s, body, log, check, progress):
        running.set()
        try:
            while True:
                check()
                time.sleep(0.01)
        except studio.Stopped:
            stopped.set()
            raise

    t = ai_tasks(ask=ask)
    task_id = t.start("mac1", {"kind": "ask"})["id"]
    assert running.wait(5)
    t.cancel(task_id, "mac1")
    assert stopped.wait(5)
    assert until_done(t, task_id)["state"] == "stopped"


def test_a_question_nobody_waits_for_any_more_is_stopped():
    clock = [1000.0]
    running = threading.Event()

    def ask(s, body, log, check, progress):
        running.set()
        while True:
            check()
            time.sleep(0.01)

    t = ai_tasks(ask=ask, now=lambda: clock[0])
    task_id = t.start("mac1", {"kind": "ask"})["id"]
    assert running.wait(5)
    clock[0] += studio.AI_IDLE + 1  # the Mac went quiet (asking again would count as waiting: it doesn't)
    end = time.time() + 5
    while t.tasks[task_id].state != "stopped" and time.time() < end:
        time.sleep(0.02)
    assert t.tasks[task_id].state == "stopped"


def test_too_many_questions_at_once():
    gate = threading.Event()

    def ask(s, body, log, check, progress):
        gate.wait(5)
        return {"data": {}, "model": "m"}

    t = ai_tasks(ask=ask)
    for _ in range(AI_MAX):
        t.start("mac1", {"kind": "ask"})
    with pytest.raises(HostError) as no:
        t.start("mac2", {"kind": "ask"})
    assert no.value.status == 429
    gate.set()


def test_a_mac_never_spends_the_pcs_claude_key(monkeypatch):
    asked = {}

    def ask_json(s, task, system, content, schema, **kw):
        asked.update(kw, task=task, content=content)
        return ai.Answer({"ok": True}, "Qwen2.5 7B Instruct", "local")

    monkeypatch.setattr(ai, "ask_json", ask_json)
    out = studio._ask_local(Settings(ai_api_key="sk-ant-pc"), {
        "task": "polish", "content": [{"type": "text", "text": "words"}, {"type": "image", "source": {}}],
        "schema": {"type": "object"}, "max_tokens": 10 ** 9}, None, None, None)
    assert out == {"data": {"ok": True}, "model": "Qwen2.5 7B Instruct"}
    assert asked["only"] == ["local"] and asked["task"] == "polish" and asked["max_tokens"] == 32000
    assert asked["content"] == [{"type": "text", "text": "words"}]
    with pytest.raises(HostError):
        studio._ask_local(Settings(), {"content": "words"}, None, None, None)  # no schema


# ================================================================== the Mac asking the PC's AI, over the link


class Link:
    """What PCAI needs of the Mac's link to the PC (pc.PCLink)."""

    def __init__(self, client, lend=None, online=True, name="JUSTYN-PC"):
        self._client, self.lend, self._online, self._name = client, lend, online, name

    linked = property(lambda self: self._client is not None)
    online = property(lambda self: self.linked and self._online)
    name = property(lambda self: self._name if self.linked else "")

    def lends(self):
        return (self.lend or {}) if self.online else {}

    def client(self):
        return self._client


def ready_ai(world, ask=None, find=None):  # noqa: F811
    world.host.ai_tasks._look = lambda: {"ready": True, "model": "Qwen2.5 7B Instruct", "downloaded": True,
                                         "why": ""}
    if ask is not None:
        world.host.ai_tasks._ask = ask
    if find is not None:
        world.host.ai_tasks._find = find
    c, _ = world.linked()
    st = c.get("/api/status")
    return c, PCAI(Link(c, st["lend"]))


def test_the_pc_says_what_it_lends(world):  # noqa: F811
    world.host.ai_tasks._look = lambda: {"ready": False, "model": "", "why": "Your PC has no local AI."}
    c, _ = world.linked()
    assert c.request("GET", "/api/hello", auth=False)["lends"] == ["ai", "studio"]
    lend = c.get("/api/status")["lend"]
    assert lend["ai"]["ready"] is False and lend["studio"] is True and lend["making"] == ""
    assert isinstance(lend["room"], int)
    assert c.get("/api/ai")["why"] == "Your PC has no local AI."


def test_the_mac_asks_the_pcs_local_ai(world, monkeypatch):  # noqa: F811
    asked = []

    def ask(s, body, log, check, progress):
        asked.append(body)
        progress("Reading the transcript", 0.5)
        time.sleep(0.3)
        return {"data": {"clips": [{"start": 1}]}, "model": "Qwen2.5 7B Instruct"}

    monkeypatch.setattr("ytclip.mac.pc_ai.WAIT", 1)
    _, pcai = ready_ai(world, ask=ask)
    ai.use_remote(pcai)
    said = []
    s = Settings(ai_provider="pc")
    assert ai.order(s)[0] == "pc" and ai.model_for(s, "find") == "Qwen2.5 7B Instruct on your PC"
    answer = ai.ask_json(s, "polish", "Be brief.", [{"type": "text", "text": "words"},
                                                     {"type": "image", "source": {"data": "…"}}],
                         {"type": "object"}, progress=lambda m, f: said.append((m, f)))
    assert answer.provider == "pc" and answer.data == {"clips": [{"start": 1}]}
    assert answer.model == "Qwen2.5 7B Instruct on your PC"
    assert asked[0]["content"] == [{"type": "text", "text": "words"}]  # a local model can't see pictures
    assert asked[0]["task"] == "polish" and asked[0]["system"] == "Be brief."
    assert ("Reading the transcript", 0.5) in said
    assert "works: Qwen2.5 7B Instruct" in ai.check_key(s)


def test_the_pcs_clip_finder_reads_the_macs_transcript(world):  # noqa: F811
    got = []

    def find(s, body, log, check, progress):
        got.append(body)
        return {"clips": [{"start": 1.0, "end": 31.0, "score": 88, "title": "The hook", "origin": "ai",
                           "not_a_field": 1}, "junk"], "model": "Qwen2.5 7B Instruct"}

    _, pcai = ready_ai(world, find=find)
    ai.use_remote(pcai)
    clips, model = ai.find_clips_ai("Big Talk", 600.0, [Cue(0, 4.123456, "Hello there"), Cue(4.2, 9, "Big news")],
                                    np.array([0.1, 0.25]), 3, 15, 60, "funny bits", Settings(ai_provider="pc"))
    assert [c.title for c in clips] == ["The hook"] and clips[0].origin == "ai" and clips[0].end == 31.0
    assert model == "Qwen2.5 7B Instruct on your PC"
    body = got[0]
    assert body["sentences"][0] == [0.0, 4.123, "Hello there"] and body["score"] == [0.1, 0.25]
    assert (body["count"], body["min_len"], body["max_len"], body["instructions"]) == (3, 15.0, 60.0, "funny bits")

    world.host.ai_tasks._find = lambda *a: {"clips": [], "model": "m"}
    with pytest.raises(ai.AIError, match="no clips"):
        pcai.find("Big Talk", 600, [Cue(0, 4, "Hello")], None, 3, 15, 60, "")


def test_the_pcs_ai_failing_comes_back_as_the_same_failure(world):  # noqa: F811
    def refuse(s, body, log, check, progress):
        raise ai.AIRefused("The model wouldn't answer that.")

    _, pcai = ready_ai(world, ask=refuse)
    with pytest.raises(ai.AIRefused, match="wouldn't answer"):
        pcai.ask("find", "", "words", {}, "medium", 1000)
    world.host.ai_tasks._ask = lambda *a: (_ for _ in ()).throw(ai.AICutOff("Cut off."))
    with pytest.raises(ai.AICutOff):
        pcai.ask("find", "", "words", {}, "medium", 1000)
    world.host.ai_tasks._ask = lambda *a: (_ for _ in ()).throw(RuntimeError("out of memory"))
    with pytest.raises(ai.AIError, match="out of memory"):
        pcai.ask("find", "", "words", {}, "medium", 1000)


def test_stopping_in_the_studio_stops_the_question_on_the_pc(world, monkeypatch):  # noqa: F811
    running, stopped = threading.Event(), threading.Event()

    def ask(s, body, log, check, progress):
        running.set()
        progress("Thinking", 0.1)
        try:
            while True:
                check()
                time.sleep(0.02)
        except studio.Stopped:
            stopped.set()
            raise

    monkeypatch.setattr("ytclip.mac.pc_ai.WAIT", 1)
    _, pcai = ready_ai(world, ask=ask)

    def check():
        if running.is_set():
            raise Cancelled()  # the Studio's Cancel

    with pytest.raises(Cancelled):
        pcai.ask("find", "", "words", {}, "medium", 1000, check=check)
    assert stopped.wait(5)


class Unreachable:
    """A client to a PC that went away after it started on the question."""

    def __init__(self, error):
        self.error, self.posts = error, []

    def post(self, path, data=None, timeout=None):
        self.posts.append(path)
        return {"id": "0123456789abcdef", "state": "waiting"} if path == "/api/ai/tasks" else {}

    def request(self, method, path, data=None, query=None, timeout=None):
        raise self.error


def test_a_pc_lost_while_answering_is_given_up():
    clock = [0.0]
    client = Unreachable(Offline("No route to host"))
    pcai = PCAI(Link(client, {"ai": {"ready": True}}), now=lambda: clock[0],
                sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    with pytest.raises(ai.AIError, match="Lost your PC"):
        pcai.ask("find", "", "words", {}, "medium", 1000)
    assert clock[0] > 90 and client.posts[-1].endswith("/cancel")

    client = Unreachable(ApiError(404, "That question isn't on your PC any more."))
    with pytest.raises(ai.AIError, match="restarted"):
        PCAI(Link(client, {"ai": {"ready": True}})).ask("find", "", "words", {}, "medium", 1000)
    assert client.posts == ["/api/ai/tasks"]  # nothing left on the PC to stop


def test_why_the_pcs_ai_cant_answer():
    assert "No PC is linked" in PCAI(Link(None)).why()
    assert "can't be reached" in PCAI(Link(object(), online=False)).why()
    assert "older ClipMonster" in PCAI(Link(object(), lend={})).why()
    assert PCAI(Link(object(), {"ai": {"ready": False, "why": "Your PC has no local AI."}})).why() == \
        "Your PC has no local AI."
    p = PCAI(Link(object(), {"ai": {"ready": True, "model": "Qwen2.5 7B Instruct"}}))
    assert p.ready() and p.model() == "Qwen2.5 7B Instruct on your PC"


def test_the_pcs_ai_comes_before_the_macs_own(monkeypatch):
    link = Link(object(), {"ai": {"ready": True, "model": "Qwen2.5 7B Instruct"}})
    ai.use_remote(PCAI(link))
    monkeypatch.setattr(ai, "local_ready", lambda s: True)
    assert ai.order(Settings(ai_provider="claude", ai_api_key="sk-ant-x")) == ["claude", "pc", "local"]
    assert ai.order(Settings(ai_provider="pc")) == ["pc", "local"]
    assert ai.order(Settings(ai_provider="pc", ai_fallback=False)) == ["pc"]
    link.lend = {"ai": {"ready": False, "why": "Your PC has no local AI."}}
    assert ai.order(Settings(ai_provider="pc")) == ["local"]
    assert ai.missing_key(Settings(ai_provider="pc")) == "Your PC has no local AI."
    ai.use_remote(None)
    assert "No PC is linked" in ai.missing_key(Settings(ai_provider="pc"))


# ================================================================== the routes, with a real client


def test_a_video_goes_to_the_pc_and_its_clips_come_back(world, tmp_path):  # noqa: F811
    world.host.studio._engine = FakeEngine
    world.host.studio.start()  # (automation starts it with the server)
    c, _ = world.linked()
    video = tmp_path / "My Clip.MOV"
    video.write_bytes(os.urandom(2 * 1024 * 1024 + 7))
    sent = []
    up = c.upload(video, video.name, lambda done, total: sent.append((done, total)))
    assert up["size"] == video.stat().st_size and sent[-1] == (up["size"], up["size"])
    j = c.post("/api/studio/jobs", {"upload": up["upload"], "name": video.name, "settings": {"clip_count": 2}})
    end = time.time() + 15
    while c.get(f"/api/studio/jobs/{j['id']}")["status"] != "done" and time.time() < end:
        time.sleep(0.1)
    got = c.get(f"/api/studio/jobs/{j['id']}")
    assert got["status"] == "done" and got["clips"] == 2
    made_from = Path(FakeEngine.made[-1][1])
    assert made_from.name == "My Clip.mov" and FakeEngine.made[-1][0].clip_count == 2
    assert [x["id"] for x in c.get("/api/studio/jobs")["jobs"]] == [j["id"]]

    rel = f"{FOLDER}/01 - clip.mp4"
    dest = tmp_path / "here" / "01.mp4"
    dest.parent.mkdir()
    c.download(f"/api/studio/jobs/{j['id']}/file?path={rel.replace(' ', '%20')}", dest)
    assert dest.stat().st_size == FakeEngine.sizes["01 - clip.mp4"]
    with pytest.raises(ApiError) as no:
        c.download(f"/api/studio/jobs/{j['id']}/file?path=..%2Fjobs.json", tmp_path / "here" / "x")
    assert no.value.status == 404 and not (tmp_path / "here" / "x.part").exists()
    assert c.post(f"/api/studio/jobs/{j['id']}/done") == {"message": "Done."}
    assert c.get("/api/studio/jobs")["jobs"] == []


def test_a_refused_video_doesnt_break_the_connection(world, tmp_path):  # noqa: F811
    c, _ = world.linked()
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"x" * 300_000)
    with pytest.raises(ApiError) as no:
        c.upload(notes, notes.name)
    assert no.value.status == 400 and "isn't a video" in no.value.message
    assert c.get("/api/status")["lend"]["studio"]  # the next request goes through
    with pytest.raises(ApiError) as no:
        c.post("/api/studio/jobs", {"source": "not a link"})
    assert no.value.status == 400
    assert c.get("/api/studio/jobs") == {"jobs": [], "busy": "", "count": 0, "limit": 20}


def test_another_mac_cant_see_or_take_my_videos(world):  # noqa: F811
    world.host.studio._engine = FakeEngine
    mine, _ = world.linked()
    theirs, _ = world.linked()
    j = mine.post("/api/studio/jobs", {"source": URL})
    assert theirs.get("/api/studio/jobs")["jobs"] == []
    for call in (lambda: theirs.get(f"/api/studio/jobs/{j['id']}"),
                 lambda: theirs.post(f"/api/studio/jobs/{j['id']}/done"),
                 lambda: theirs.post(f"/api/studio/jobs/{j['id']}/cancel")):
        with pytest.raises(ApiError) as no:
            call()
        assert no.value.status == 404


def test_another_mac_doesnt_hear_about_my_videos(world):  # noqa: F811
    world.host.studio._engine = FakeEngine
    world.host.studio.start()
    mine, _ = world.linked()
    theirs, _ = world.linked()
    seq = theirs.get("/api/events")["seq"]
    j = mine.post("/api/studio/jobs", {"source": URL})
    end = time.time() + 15
    while mine.get(f"/api/studio/jobs/{j['id']}")["status"] != "done" and time.time() < end:
        time.sleep(0.1)
    world.host.add_event("problem", "Low disk space", alert=True)
    got = mine.get("/api/events", after=seq)
    assert [(e["kind"], e["item"]) for e in got["events"]] == [("studio", j["id"]), ("problem", "")]
    other = theirs.get("/api/events", after=seq)
    assert [e["kind"] for e in other["events"]] == ["problem"] and other["seq"] == got["seq"]  # one count for all


def test_unlinking_a_mac_drops_the_videos_it_sent(world, tmp_path):  # noqa: F811
    world.host.studio._engine = FakeEngine
    mine, _ = world.linked()
    theirs, _ = world.linked()
    video = tmp_path / "Mine.mov"
    video.write_bytes(b"abcd" * 1000)
    up = mine.upload(video, video.name)
    mine.post("/api/studio/jobs", {"upload": up["upload"], "name": video.name})
    kept = theirs.post("/api/studio/jobs", {"source": URL})
    mine.post("/api/unlink")
    nxt = world.host.studio._next()
    assert nxt["id"] == kept["id"] and [j["id"] for j in world.host.studio._load()] == [kept["id"]]
    assert not (world.host.studio.root / "uploads" / up["upload"]).exists()


# ================================================================== the Mac's window: Make on Your PC


pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtGui import QAction, QShortcut  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from test_mac_app import linked_host, shut, wait  # noqa: E402


@pytest.fixture
def mac(app, world, tmp_path):  # noqa: F811
    """ClipMonster for Mac, linked to the world's PC, saving clips in its own folder."""
    pytest.importorskip("ytclip.gui.main_window", exc_type=ImportError)  # no Qt Multimedia libraries (Linux CI)
    from ytclip.mac.window import MacWindow

    Settings(setup_done=True, output_dir=str(tmp_path / "Mac clips")).save()
    world.host.studio._engine = FakeEngine
    world.host.studio.start()  # (automation starts it with the server)
    host = linked_host(world)
    win = MacWindow()
    win.pc_jobs.timer.setInterval(250)
    win.device_id = host.device_id
    assert wait(app, lambda: win.link.online and (win.link.lends() or {}).get("studio"), timeout=15)
    yield win
    shut(app, win)


def only_job(win):
    jobs = win.pc_jobs.ordered()
    return jobs[0] if len(jobs) == 1 else None


def test_make_on_shows_once_a_pc_is_linked(app, mac):
    assert mac.make_on.isVisibleTo(mac) and mac.on_mac.isChecked()
    assert "Now:" not in mac.on_pc.toolTip()
    mac.on_pc.setChecked(True)
    assert "your PC makes them" in mac.input.placeholderText()
    from ytclip.mac import prefs

    assert prefs.load()["make_on"] == "pc"
    mac.link.switch_pc()  # unlinked: everything is made here
    assert not mac.make_on.isVisibleTo(mac) and not mac._on_pc()
    assert "drop video files" in mac.input.placeholderText()


def test_a_link_made_on_the_pc_lands_in_the_macs_library(app, mac, world, tmp_path):  # noqa: F811
    mac.on_pc.setChecked(True)
    mac.settings = mac.settings.copy(clip_count=4)
    mac.add_sources([URL])
    job = only_job(mac)
    assert job is not None and job["kind"] == "url"
    p = mac.projects[mac.queue.currentRow()]
    assert getattr(p, "pc_key", "") == job["key"] and "Sent 1 video" in mac.status.text()

    assert wait(app, lambda: only_job(mac)["status"] == "done", timeout=30), only_job(mac)
    job = only_job(mac)
    here = tmp_path / "Mac clips" / FOLDER
    assert Path(job["here"]) == here and job["clips"] == 2
    assert (here / "01 - clip.mp4").stat().st_size == FakeEngine.sizes["01 - clip.mp4"]
    assert (here / "thumbnails" / "02.jpg").is_file() and not (here / "_rejected").exists()
    meta = json.loads((here / "clips.json").read_text(encoding="utf-8"))
    assert meta["made_on"] == {"pc": mac.link.name} and meta["source"] == URL
    assert FakeEngine.made[-1][0].clip_count == 4  # the Mac's settings went with it
    assert any(folder == here for folder, _ in mac.library._all)
    assert "came over from your PC" in mac.status.text()
    assert "in your Library" in mac._pc_words(mac._row_job(job["key"]))
    assert wait(app, lambda: world.host.studio.jobs_for(mac.device_id)["jobs"] == [])  # the PC let them go


def test_a_video_file_goes_over_first_and_comes_back_as_its_own(app, mac, world, tmp_path):  # noqa: F811
    video = tmp_path / "Holiday.mov"
    video.write_bytes(os.urandom(1024 * 1024))
    mac.on_pc.setChecked(True)
    mac.add_sources([str(video)])
    assert wait(app, lambda: only_job(mac)["status"] == "done", timeout=30), only_job(mac)
    on_pc = Path(FakeEngine.made[-1][1])
    assert on_pc.name == "Holiday.mov" and "uploads" in on_pc.parts
    here = Path(only_job(mac)["here"])
    meta = json.loads((here / "clips.json").read_text(encoding="utf-8"))
    assert meta["source"] == str(video) and meta["video"]["url"] == str(video)  # Re-open opens the Mac's copy
    assert all(c["watch"] == str(video) for c in meta["clips"])
    assert not on_pc.parent.exists()  # the PC's copy went once it was made


def test_an_unreachable_pc_offers_to_make_it_here(app, mac, world, monkeypatch):  # noqa: F811
    from ytclip.gui.main_window import MainWindow as StudioWindow

    here = []
    monkeypatch.setattr(StudioWindow, "add_sources", lambda self, sources: here.extend(sources))
    mac.on_pc.setChecked(True)
    world.s.remote_on = False
    world.server.configure()
    mac.link.session.client.close()
    mac.link.refresh()
    assert wait(app, lambda: not mac.link.online, timeout=15)
    asked = []

    def make_here(box):
        asked.append(box.text())
        next(b for b in box.buttons() if b.text() == "Make on this Mac").click()

    monkeypatch.setattr(QMessageBox, "exec", make_here)
    mac.add_sources([URL])
    assert "can't be reached" in asked[0] and "on this Mac instead" in asked[0]
    assert here == [URL] and mac.pc_jobs.ordered() == []


def test_stop_and_remove_a_video_on_the_pc(app, mac, world, monkeypatch):  # noqa: F811
    FakeEngine.gate = threading.Event()
    mac.on_pc.setChecked(True)
    mac.add_sources([URL])
    assert wait(app, lambda: only_job(mac)["status"] == "working", timeout=20), only_job(mac)
    key = only_job(mac)["key"]
    assert "Being made on" in mac._pc_words(mac._row_job(key))
    mac.link.refresh()
    assert wait(app, lambda: mac.link.pc_state.text() == "Making clips for your Mac")  # the bar over its pages
    mac.pc_jobs.stop(key)
    assert wait(app, lambda: only_job(mac)["status"] == "cancelled", timeout=20), only_job(mac)
    p = mac._pc_projects[key]
    monkeypatch.setattr(QMessageBox, "exec", lambda self: None)
    mac._remove(p)  # (a stopped video goes without asking)
    assert mac.pc_jobs.ordered() == [] and p not in mac.projects
    assert wait(app, lambda: world.host.studio.jobs_for(mac.device_id)["jobs"] == [])


def test_quitting_while_a_video_goes_to_the_pc_asks_first(app, mac, monkeypatch):
    monkeypatch.setattr(mac, "_busy", lambda: "A video is still going to your PC.")
    asked = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: asked.append(box.text()))  # closed: Cancel
    mac.quit()
    assert asked and "Quitting stops it" in asked[0] and not mac._quitting


def test_quitting_from_the_dock_after_the_window_was_closed_stops_and_saves(app, mac, monkeypatch):
    """Quit from the Dock, logging out or shutting down once the window was closed with its red button: Qt closes
    only the windows on screen, so the window's own close is run for it. Otherwise what it's making didn't stop and
    nothing was saved, and an export still going died half-written (macOS: "ClipMonster quit unexpectedly")."""
    from PySide6.QtCore import QEvent

    monkeypatch.setattr(mac, "_busy", lambda: "")
    stopped = []
    monkeypatch.setattr(mac.tasks, "cancel_all", lambda: stopped.append(True))
    mac.show()
    assert mac._quit_watch.eventFilter(app, QEvent(QEvent.Quit)) is False  # on screen: Qt closes it itself
    assert mac._quitting and not getattr(mac, "_closing", False) and not stopped
    mac._quitting = False
    mac.hide()  # (what the red button does on a Mac)
    assert mac._quit_watch.eventFilter(app, QEvent(QEvent.Quit)) is False  # and Qt carries on quitting
    assert mac._quitting and mac._closing and mac._shut and stopped


def test_the_list_is_remembered(app, mac, tmp_path):
    from ytclip.mac import pc_jobs

    now = time.time()
    path = tmp_path / "pc_jobs.json"
    base = {"host": "h", "host_name": "PC", "kind": "url", "source": URL, "name": URL, "id": "0123456789abcdef",
            "files": [], "created": now, "updated": now}
    path.write_text(json.dumps({"jobs": [
        {**base, "key": "a", "status": "sending"},
        {**base, "key": "b", "status": "done", "updated": now - 8 * pc_jobs.DAY},
        {**base, "key": "c", "status": "queued"}, "junk"]}), encoding="utf-8")
    jobs = pc_jobs.PCJobs(mac.link, lambda: mac.settings, path=path)
    try:
        assert [j["key"] for j in jobs.ordered()] == ["a", "c"]
        assert jobs.get("a")["status"] == "failed" and "Send again" in jobs.get("a")["error"]
        assert jobs.waiting_for(jobs.get("c")) == "Waiting for PC to be linked again"  # sent to another PC
        assert [j["key"] for j in json.loads(path.read_text())["jobs"]] == ["a", "c"]
    finally:
        jobs.shutdown()


def test_a_video_waits_for_the_pc_it_was_sent_to(app, mac):
    assert mac.pc_jobs.can_send() == ""
    job = {"status": "queued", "host": mac.link.session.host.id, "host_name": "JUSTYN-PC"}
    assert mac.pc_jobs.waiting_for(job) == ""
    mac.link.state = "offline"
    assert mac.pc_jobs.waiting_for(job) == "Waiting for JUSTYN-PC to come back"
    assert "can't be reached" in mac.pc_jobs.can_send()
    mac.link.state = "online"
    words = mac._pc_words
    assert words({"status": "queued", "waiting": 2, "host_name": "PC"}) == "Waiting its turn on PC (2 ahead)"
    assert words({"status": "failed", "error": "This video is private.", "host_name": "PC"}) == \
        "PC couldn't make it: This video is private."
    assert words({"status": "fetching", "host_name": "PC"}) == "Made on PC: its clips are coming over"


# ================================================================== the Mac's list of videos on the PC


class PretendPC:
    """The PC as the Mac's list of videos (pc_jobs.PCJobs) talks to it: its client. It takes videos up to
    `limit` at once, and `others` are the ones other Macs have waiting there."""

    def __init__(self, limit=20, others=0, says_count=True):
        self.limit, self.others, self.says_count = limit, others, says_count
        self.jobs: dict[str, dict] = {}
        self.uploads, self.refused, self.fetched, self.asked = [], 0, [], []
        self.at_once = self.most_at_once = 0
        self.lock = threading.Lock()

    def taken(self) -> int:
        return self.others + sum(1 for j in self.jobs.values() if j["status"] in ("queued", "working"))

    def get(self, path, **query):
        self.asked.append(path)
        if path == "/api/studio/jobs":
            out = {"jobs": [dict(j) for j in self.jobs.values()], "busy": ""}
            if self.says_count:
                out.update(count=self.taken(), limit=self.limit)
            return out
        job = self.jobs.get(path.rsplit("/", 1)[-1])
        if job is None:
            raise ApiError(404, "That video isn't on your PC any more.")
        return dict(job)

    def upload(self, path, name="", progress=None, cancelled=None):
        self.uploads.append(name)
        return {"upload": "0123456789abcdef", "size": Path(path).stat().st_size}

    def post(self, path, data=None, timeout=None):
        if path == "/api/studio/jobs":
            with self.lock:
                self.at_once += 1
                self.most_at_once = max(self.most_at_once, self.at_once)
            time.sleep(0.01)
            try:
                with self.lock:
                    if self.taken() >= self.limit:
                        self.refused += 1
                        raise ApiError(429, "Your PC already has 20 videos to make. Wait for some to finish.")
                    job = {"id": f"{len(self.jobs):016x}", "status": "queued", "stage": "Waiting", "files": [],
                           "folder": "", "clips": 0}
                    self.jobs[job["id"]] = job
                    return dict(job)
            finally:
                with self.lock:
                    self.at_once -= 1
        job_id, what = path.split("/")[-2:]
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError(404, "That video isn't on your PC any more.")
        if what == "cancel" and job["status"] in ("queued", "working"):
            job.update(status="cancelled", stage="Stopped")
        if what == "done":
            del self.jobs[job_id]
            return {"message": "Done."}
        return dict(job)

    def made(self, job_id, files, folder=FOLDER):
        """The PC finished a video: these are its clips ({path under the folder: bytes})."""
        self.jobs[job_id].update(status="done", stage="Done", folder=folder, clips=len(files), contents=files,
                                 files=[{"path": f"{folder}/{name}", "size": len(data)}
                                        for name, data in sorted(files.items())])

    def download(self, path, dest, progress=None, cancelled=None):
        from urllib.parse import parse_qs, urlsplit

        url = urlsplit(path)
        job = self.jobs.get(url.path.split("/")[-2])
        if job is None:
            raise ApiError(404, "That video isn't on your PC any more.")
        rel = parse_qs(url.query)["path"][0]
        self.fetched.append(rel)
        with self.lock:
            self.at_once += 1
            self.most_at_once = max(self.most_at_once, self.at_once)
        time.sleep(0.02)
        Path(dest).write_bytes(job["contents"][rel[len(job["folder"]) + 1:]])
        with self.lock:
            self.at_once -= 1
        return dest


class PretendSession:
    def __init__(self, link, client, host_id):
        self.link, self.client, self.host = link, client, SimpleNamespace(id=host_id)
        self.held = None  # a list, to keep requests unanswered: (fn, ok, fail)

    def call(self, fn, ok=None, fail=None):
        if self.held is not None:
            self.held.append((fn, ok, fail))
        else:
            self.link.tasks.run(lambda: fn(self.client), ok, fail)


@pytest.fixture
def pc_list(app, tmp_path):
    """The Mac's list of videos on the PC, linked to a PretendPC. `jobs.pc` is that PC and `jobs.link` the link."""
    from PySide6.QtCore import QObject, Signal

    from ytclip.mac import pc_jobs
    from ytclip.mac.session import Tasks

    class PretendLink(QObject):
        changed = Signal()
        event = Signal(dict)

        def __init__(self):
            super().__init__()
            self.tasks = Tasks(parent=self)
            self.state, self.name, self.lend = "online", "JUSTYN-PC", {"studio": True, "room": 0}
            self.session = None

        linked = property(lambda self: self.session is not None)
        online = property(lambda self: self.linked and self.state == "online")

        def lends(self):
            return self.lend if self.online else {}

        def client(self):
            return self.session.client if self.session is not None else None

        def open(self, pc, host_id="pc1"):
            self.session = PretendSession(self, pc, host_id)
            self.changed.emit()
            return self.session

    link = PretendLink()
    pc = PretendPC()
    link.open(pc)
    s = Settings(output_dir=str(tmp_path / "Mac clips"))
    jobs = pc_jobs.PCJobs(link, lambda: s, path=tmp_path / "pc_jobs.json")
    jobs.timer.setInterval(40)
    jobs.pc, jobs.link, jobs.out = pc, link, tmp_path / "Mac clips"
    yield jobs
    jobs.shutdown()
    wait(app, lambda: not jobs._threads, timeout=5)
    link.tasks.shutdown()


def statuses(jobs) -> list[str]:
    return [j["status"] for j in jobs.ordered()]


def links(n: int) -> list[str]:
    return [f"https://www.youtube.com/watch?v={i:011d}" for i in range(n)]


def test_more_videos_than_the_pc_takes_wait_on_the_mac(app, pc_list):
    """A playlist of more videos than the PC takes at once: the rest wait on the Mac and go as it makes room.
    Every one was started at once, so each past the 20th failed and had to be sent again by hand."""
    keys = pc_list.send(links(23))
    assert statuses(pc_list)[0] == "sending" and statuses(pc_list)[1:] == ["waiting"] * 22  # one at a time
    assert wait(app, lambda: statuses(pc_list).count("queued") == 20)
    assert wait(app, lambda: pc_list.pc.asked.count("/api/studio/jobs") >= 3)  # (polled a few times since)
    assert statuses(pc_list) == ["queued"] * 20 + ["waiting"] * 3
    assert pc_list.pc.refused == 0 and pc_list.pc.most_at_once == 1
    assert pc_list.waiting_for(pc_list.get(keys[-1])) == ""

    pc_list.stop(keys[0])  # the PC has room for one more
    assert wait(app, lambda: statuses(pc_list) == ["cancelled"] + ["queued"] * 20 + ["waiting"] * 2), statuses(pc_list)
    pc_list.stop(keys[-1])  # one still on the Mac just stops
    assert pc_list.get(keys[-1])["status"] == "cancelled" and pc_list.pc.refused == 0
    pc_list.send_again(keys[-1])
    assert pc_list.get(keys[-1])["status"] == "waiting"


def test_a_full_pc_means_wait_not_failed(app, pc_list, tmp_path, monkeypatch):
    """The PC is full of another Mac's videos: this Mac's video waits and goes when there's room, and a video
    file isn't sent over just to be refused."""
    from ytclip.mac import pc_jobs

    monkeypatch.setattr(pc_jobs, "FULL_WAIT", 0.2)
    failed = []
    pc_list.failed.connect(failed.append)
    video = tmp_path / "Holiday.mov"
    video.write_bytes(b"abcd" * 100)
    pc_list.pc.others = 20
    key, = pc_list.send([str(video)])
    assert wait(app, lambda: "/api/studio/jobs" in pc_list.pc.asked and pc_list.get(key)["status"] == "waiting")
    assert wait(app, lambda: pc_list.pc.asked.count("/api/studio/jobs") >= 3)  # asked again after a while
    assert wait(app, lambda: pc_list.get(key)["stage"] == "Waiting for room on your PC")
    assert pc_list.get(key)["status"] == "waiting" and not pc_list.get(key)["error"]
    assert pc_list.pc.uploads == [] and pc_list.pc.refused == 0 and not failed
    pc_list.pc.others = 19
    assert wait(app, lambda: pc_list.get(key)["status"] == "queued"), pc_list.get(key)
    assert pc_list.pc.uploads == ["Holiday.mov"] and not failed


def test_a_pc_that_says_its_full_is_asked_again_later(app, pc_list, monkeypatch):
    """The PC answers 429 (it filled up meanwhile, or it's an older one that doesn't say how many it has)."""
    from ytclip.mac import pc_jobs

    monkeypatch.setattr(pc_jobs, "FULL_WAIT", 0.2)
    pc_list.pc.says_count, pc_list.pc.others = False, 20
    key, = pc_list.send([URL])
    assert wait(app, lambda: pc_list.pc.refused >= 2)
    assert pc_list.get(key)["status"] in ("waiting", "sending") and not pc_list.get(key)["error"]
    pc_list.pc.others = 0
    assert wait(app, lambda: pc_list.get(key)["status"] == "queued"), pc_list.get(key)


def test_a_pc_that_goes_away_isnt_tried_by_each_waiting_video(app, pc_list):
    class Gone(PretendPC):
        def post(self, path, data=None, timeout=None):
            self.refused += 1
            raise Offline("The PC can't be reached.")

    pc_list.link.open(Gone())
    keys = pc_list.send(links(3))
    assert wait(app, lambda: pc_list.get(keys[0])["status"] == "failed")
    wait(app, lambda: False, timeout=0.3)
    assert statuses(pc_list) == ["failed", "waiting", "waiting"] and pc_list.link.session.client.refused == 1


def test_a_poll_lost_with_its_pc_doesnt_stop_the_next(app, pc_list):
    """A poll still out when its PC is switched away never comes back. Polling stopped for good then, so no
    video was followed or fetched until ClipMonster was opened again."""
    key, = pc_list.send([URL])
    assert wait(app, lambda: pc_list.get(key)["status"] == "queued")
    first = pc_list.link.session
    first.held = []
    assert wait(app, lambda: len(first.held) == 1)  # a poll is out, and stays unanswered
    wait(app, lambda: False, timeout=0.2)
    assert len(first.held) == 1  # (one at a time)
    pc_list.link.session = None
    pc_list.link.changed.emit()
    pc_list.pc.asked.clear()
    pc_list.link.open(pc_list.pc)  # linked again: the same PC
    assert wait(app, lambda: "/api/studio/jobs" in pc_list.pc.asked)
    pc_list.pc.jobs.clear()
    first.held[0][1]({"jobs": []})  # the old poll answers after all: it's about another link
    assert pc_list.get(key)["status"] == "queued"


def test_a_closed_session_still_says_the_request_failed(app, mac):
    """A request that fails after its PC was switched away: the one who asked still hears (a poll waiting on it
    waited for ever)."""
    session = mac.link.session

    def gone(exc):
        raise RuntimeError("Signal source has been deleted")

    session._failed = gone
    heard = []

    def offline(client):
        raise Offline("The PC can't be reached.")

    session.call(offline, None, heard.append)
    assert wait(app, lambda: heard) and isinstance(heard[0], Offline)
    del session._failed


def test_stop_as_the_pc_finishes_still_brings_the_clips(app, pc_list):
    """Stop pressed just as the PC finished: it answers 'done', and the clips come over. The video was marked
    done with nothing fetched, and the PC let its clips go days later."""
    landed = []
    pc_list.landed.connect(landed.append)
    key, = pc_list.send([URL])
    assert wait(app, lambda: pc_list.get(key)["status"] == "queued")
    pc_list.timer.stop()
    wait(app, lambda: pc_list._polling is None, timeout=2)
    pc_list.pc.made(pc_list.get(key)["id"], {"01 - clip.mp4": b"one", "clips.json": b'{"clips": []}'})
    pc_list.stop(key)
    assert wait(app, lambda: pc_list.get(key)["status"] == "done"), pc_list.get(key)
    assert (pc_list.out / FOLDER / "01 - clip.mp4").read_bytes() == b"one" and landed == [key]
    assert pc_list.pc.jobs == {}  # and the PC was told they're here


def test_a_video_that_arrives_during_a_poll_isnt_called_gone(app, pc_list):
    """The PC's answer to a poll was made before a video reached it: that video is judged by the next poll.
    It was marked 'Your PC doesn't have this video any more' while the PC went on to make it."""
    first, = pc_list.send([URL])
    assert wait(app, lambda: pc_list.get(first)["status"] == "queued")
    session = pc_list.link.session
    session.held = []
    assert wait(app, lambda: len(session.held) == 1)
    fn, ok, _fail = session.held[0]
    old = fn(pc_list.pc)  # the PC's answer, before the second video
    session.held = None
    second, = pc_list.send([URL + "2"])
    assert wait(app, lambda: pc_list.get(second)["status"] == "queued")
    ok(old)
    assert pc_list.get(second)["status"] == "queued" and not pc_list.get(second)["error"]
    assert pc_list.get(first)["status"] == "queued"
    pc_list.pc.jobs.pop(pc_list.get(first)["id"])  # one the PC really lost is still said so
    assert wait(app, lambda: pc_list.get(first)["status"] == "failed")
    assert pc_list.get(second)["status"] == "queued"


def test_fetched_clips_replace_an_earlier_export_of_the_video(app, pc_list):
    """The Library already has clips of this video: they make way, as when it's exported again on the Mac.
    They were left on disk unlisted, where neither the Library nor Clean Up could see them."""
    here = pc_list.out / FOLDER
    (here / "thumbnails").mkdir(parents=True)
    for name in ("01 - old.mp4", "01 - old.srt", "02 - older.mp4", "thumbnails/02.jpg", "notes.txt"):
        (here / name).write_bytes(b"from before")
    (here / "clips.json").write_text(json.dumps({"clips": [
        {"file": "01 - old.mp4", "rank": 1}, {"file": "02 - older.mp4", "rank": 2}, {"file": "../../x", "rank": 3},
        "junk"]}), encoding="utf-8")
    (pc_list.out / "x").write_bytes(b"not a clip of this video")
    key, = pc_list.send([URL])
    assert wait(app, lambda: pc_list.get(key)["status"] == "queued")
    new = {"01 - clip.mp4": b"new one", "clips.json": json.dumps({"clips": [{"file": "01 - clip.mp4"}]}).encode()}
    pc_list.pc.made(pc_list.get(key)["id"], new)
    assert wait(app, lambda: pc_list.get(key)["status"] == "done"), pc_list.get(key)
    assert sorted(p.name for p in here.iterdir()) == ["01 - clip.mp4", "clips.json", "notes.txt", "thumbnails"]
    assert list((here / "thumbnails").iterdir()) == [] and (pc_list.out / "x").is_file()
    assert json.loads((here / "clips.json").read_text(encoding="utf-8"))["clips"] == [{"file": "01 - clip.mp4"}]


def test_an_earlier_export_stays_when_the_pc_lost_the_clips(app, pc_list):
    here = pc_list.out / FOLDER
    here.mkdir(parents=True)
    (here / "01 - old.mp4").write_bytes(b"from before")
    (here / "clips.json").write_text(json.dumps({"clips": [{"file": "01 - old.mp4", "rank": 1}]}), encoding="utf-8")
    key, = pc_list.send([URL])
    assert wait(app, lambda: pc_list.get(key)["status"] == "queued")
    pc_list.timer.stop()
    wait(app, lambda: pc_list._polling is None, timeout=2)
    job_id = pc_list.get(key)["id"]
    pc_list.pc.made(job_id, {"01 - clip.mp4": b"new one"})
    answer = pc_list.pc.get("/api/studio/jobs")
    del pc_list.pc.jobs[job_id]  # (the PC let them go just after it said they're made)
    pc_list._heard(key, answer["jobs"][0])
    assert wait(app, lambda: pc_list.get(key)["status"] == "failed"), pc_list.get(key)
    assert "doesn't have these clips" in pc_list.get(key)["error"]
    assert (here / "01 - old.mp4").read_bytes() == b"from before" and (here / "clips.json").is_file()


def test_two_videos_never_come_into_one_folder_at_once(app, pc_list):
    """The same video made twice, both finished while ClipMonster was closed: one is fetched, then the other."""
    keys = pc_list.send([URL, URL])
    assert wait(app, lambda: statuses(pc_list) == ["queued", "queued"])
    pc_list.pc.most_at_once = 0
    for n, key in enumerate(keys):
        files = {f"0{i} - clip.mp4": f"video {n} clip {i}".encode() for i in (1, 2, 3)}
        files["clips.json"] = json.dumps({"clips": [{"file": name, "rank": i + 1}
                                                    for i, name in enumerate(sorted(files))]}).encode()
        pc_list.pc.made(pc_list.get(key)["id"], files)
    assert wait(app, lambda: statuses(pc_list) == ["done", "done"]), pc_list.ordered()
    assert pc_list.pc.most_at_once == 1 and len(pc_list.pc.fetched) == 8
    here = pc_list.out / FOLDER
    texts = {p.read_bytes().decode()[:7] for p in here.glob("*.mp4")}
    assert len(texts) == 1 and len(list(here.glob("*.mp4"))) == 3  # one video's clips, whole
    assert not list(here.glob("*.part"))


def test_removing_a_video_whose_clips_are_coming_asks_first(app, mac, monkeypatch):
    from ytclip.engine import Project

    job = {"key": "k1", "host": mac.link.session.host.id, "host_name": "PC", "kind": "url", "source": URL,
           "name": URL, "id": "", "status": "fetching", "files": [], "created": 1.0, "updated": 1.0}
    mac.pc_jobs.jobs["k1"] = job
    mac.pc_jobs.added.emit("k1")
    p = mac._pc_projects["k1"]
    assert isinstance(p, Project)
    asked = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: asked.append(box.text()))  # closed: Cancel
    mac._remove(p)
    assert len(asked) == 1 and "deletes" in asked[0] and "your PC" in asked[0]
    assert mac.pc_jobs.get("k1") is not None and p in mac.projects
    monkeypatch.setattr(QMessageBox, "exec", lambda box: next(
        b for b in box.buttons() if box.buttonRole(b) == QMessageBox.DestructiveRole).click())
    mac._remove(p)
    assert mac.pc_jobs.get("k1") is None and p not in mac.projects


def test_a_folder_dropped_for_the_pc_sends_its_videos(app, mac, tmp_path, monkeypatch):
    """A folder of videos dropped with Make on: Your PC picked. It was dropped without a word."""
    from ytclip import download

    folder = tmp_path / "Holiday videos"
    folder.mkdir()
    (folder / "one.mov").write_bytes(b"\0" * 10)
    read, sent = [], []
    monkeypatch.setattr(download, "expand_sources",
                        lambda sources, *a, **kw: read.extend(sources) or [str(folder / "one.mov")])
    monkeypatch.setattr(mac.pc_jobs, "send", lambda sources: sent.extend(sources) or [])
    mac.on_pc.setChecked(True)
    mac.add_sources([str(folder), str(tmp_path / "nothing here")])
    assert wait(app, lambda: sent, timeout=15)
    assert read == [str(folder)] and sent == [str(folder / "one.mov")]


# ================================================================== the Mac's window: menus, keys and the rest


def test_no_key_does_two_things(app, mac):
    menu_keys = [a.shortcut().toString() for a in mac.findChildren(QAction) if not a.shortcut().isEmpty()]
    own_keys = [sc.key().toString() for sc in mac.findChildren(QShortcut)]
    assert len(menu_keys) == len(set(menu_keys)), menu_keys
    assert not set(menu_keys) & set(own_keys)
    assert {"Ctrl+E", "Ctrl+O", "Ctrl+V", "Ctrl+,", "Ctrl+Q", "Ctrl+1", "Ctrl+R"} <= set(menu_keys)


def test_the_studios_keys_work_only_in_the_studio(app, mac):
    assert mac._studio_keys and all(sc.isEnabled() for sc in mac._studio_keys)
    mac.tabs.setCurrentIndex(1)  # the Library: Space and the arrows belong to it there
    assert not any(sc.isEnabled() for sc in mac._studio_keys)
    mac.tabs.setCurrentIndex(mac.link.i_dash)
    assert not mac.header.isVisibleTo(mac) and mac.link.bar.isVisibleTo(mac)
    mac._go(0)
    assert all(sc.isEnabled() for sc in mac._studio_keys) and mac.header.isVisibleTo(mac)


def test_the_go_menu_leads_to_link_your_pc_before_one_is_linked(app, mac):
    mac.link.switch_pc()
    mac._go(mac.link.i_dash)
    assert mac.tabs.currentIndex() == mac.link.i_link


def test_the_footer_says_when_the_pc_is_older(app, mac):
    from ytclip import __version__

    mac._footer("3.5.2")
    assert "Your PC runs 3.5.2" in mac.version_label.text()
    mac._footer(__version__)
    assert "Your PC runs" not in mac.version_label.text()


def test_a_newer_mac_app_is_offered(app, mac, monkeypatch):
    from ytclip.mac import window as mw

    release = {"tag_name": "v99.0.0", "assets": [
        {"name": "ClipMonster-Setup.exe", "browser_download_url": "https://example.invalid/setup.exe"},
        {"name": mw.dmg_name(), "browser_download_url": "https://example.invalid/mac.dmg"}]}
    monkeypatch.setattr(mw.urllib.request, "urlopen", lambda req, timeout=10: io.BytesIO(json.dumps(release).encode()))
    mac._check_mac_update(manual=False)
    assert wait(app, lambda: "99.0.0 is out" in mac.version_label.text())
    assert "https://example.invalid/mac.dmg" in mac.version_label.text()


def test_closing_the_window_on_a_mac_keeps_it_running(app, mac):
    mac.show()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "platform", "darwin")
        mac.close()
    assert not mac.isVisible() and not getattr(mac, "_shut", False) and mac.link.session is not None
    mac.bring_up()
    assert mac.isVisible()


def test_the_first_start_asks_which_ai(app, mac):
    from ytclip.mac.welcome import Welcome

    dlg = Welcome(Settings(), mac.link, mac)
    assert dlg.chosen() == "pc"  # a PC is linked: its AI first
    dlg.radios["claude"].setChecked(True)
    dlg.accept()  # no key yet: it says so and stays
    assert "Paste the Claude API key" in dlg.msg.text() and not dlg.result_settings.setup_done
    dlg.keys["claude"].setText("sk-ant-test")
    dlg.accept()
    s = dlg.result_settings
    assert (s.setup_done, s.ai_provider, s.mode, s.ai_api_key) == (True, "claude", "ai", "sk-ant-test")
    assert not dlg.link_pc

    dlg = Welcome(Settings(), SimpleNamespace(linked=False), None)
    assert dlg.chosen() == "none" or dlg.chosen() == Settings().ai_provider
    dlg.radios["pc"].setChecked(True)
    assert dlg.go.text() == "Start and link my PC"
    dlg.accept()
    assert dlg.link_pc and dlg.result_settings.ai_provider == "pc"
    dlg = Welcome(Settings(), SimpleNamespace(linked=False), None)
    dlg.radios["none"].setChecked(True)
    dlg.accept()
    assert dlg.result_settings.mode == "auto"


# ================================================================== the Mac's own pieces


def test_api_keys_stay_out_of_settings_json(tmp_path):
    from ytclip import settings as S
    from ytclip.mac import keychain

    with pytest.MonkeyPatch.context() as mp:
        # the file keys go to without a keyring: a Mac runner's own Keychain is left alone by the tests
        mp.setattr(keychain, "_keyring", lambda: None)
        S.keep_secrets_in(keychain.SettingsSecrets())
        try:
            S.Settings(ai_api_key="sk-ant-1", gemini_api_key="g-1").save()
            path = S.app_dir() / "settings.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            assert raw["ai_api_key"] == "" and raw["gemini_api_key"] == ""
            assert keychain.load("setting:ai_api_key") == "sk-ant-1" and keychain.where() == "a private file"
            if os.name == "posix":  # a file only this user reads
                assert (keychain._file().stat().st_mode & 0o077) == 0
            assert S.Settings.load().ai_api_key == "sk-ant-1"

            raw["youtube_api_key"] = "yt-1"  # written before the keys moved: it moves now
            path.write_text(json.dumps(raw), encoding="utf-8")
            S.keep_secrets_in(keychain.SettingsSecrets())
            assert S.Settings.load().youtube_api_key == "yt-1" and keychain.load("setting:youtube_api_key") == "yt-1"

            S.Settings.load().copy(ai_api_key="").save()  # taken out in Settings: forgotten
            assert keychain.load("setting:ai_api_key") == "" and S.Settings.load().gemini_api_key == "g-1"
        finally:
            S.keep_secrets_in(None)


def test_settings_on_the_mac_speak_of_the_mac():
    from ytclip import settings as S
    from ytclip.gui.settings_dialog import MAC_GROUPS

    schema = copy.deepcopy(S.SCHEMA)
    S._for_the_mac(schema)
    for f in schema:
        if f.group not in MAC_GROUPS:
            continue
        words = " ".join([f.label, f.help] + [str(v) for _, v in f.choices or ()])
        for pc_word in ("this PC", "the PC ", "any PC", "Vulkan", "graphics card", "NVIDIA", "Explorer", "%APPDATA%"):
            assert pc_word not in words, (f.name, pc_word, words)
    by = {f.name: f for f in schema}
    assert dict(by["ai_provider"].choices)["pc"].startswith("Your PC's local AI")
    assert dict(by["encoder"].choices) == dict(S.MAC_ENCODERS)
    assert dict(by["local_model"].choices)["auto"] == "Pick for this Mac"
    # Safari's logins open only to an app given Full Disk Access by hand; picked, every download failed
    assert "safari" not in dict(by["cookies_browser"].choices) and "chrome" in dict(by["cookies_browser"].choices)


def test_the_macs_media_engine(monkeypatch):
    from ytclip import ffmpeg

    monkeypatch.setattr(ffmpeg, "VT_QUALITY", True)  # Apple silicon: a constant quality
    assert ffmpeg.encoder_args("h264_videotoolbox", 18) == ["-c:v", "h264_videotoolbox", "-q:v", "65",
                                                            "-allow_sw", "1"]
    assert ffmpeg.encoder_args("h264_videotoolbox", 28)[3] == "40"
    monkeypatch.setattr(ffmpeg, "VT_QUALITY", False)  # Intel: a bit rate
    args = ffmpeg.encoder_args("h264_videotoolbox", 20)
    assert args[:4] == ["-c:v", "h264_videotoolbox", "-b:v", "12.0M"] and "-maxrate" in args
    assert float(ffmpeg.encoder_args("h264_videotoolbox", 30)[3][:-1]) < 12


def test_whisper_uses_the_macs_graphics_at_balanced_too():
    from ytclip import speed

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "platform", "darwin")
        on_mac = (speed.use_gpu(Settings(speed="balanced", gpu_whisper=True)),
                  speed.use_gpu(Settings(speed="quiet", gpu_whisper=True)),
                  speed.use_gpu(Settings(speed="full", gpu_whisper=False)))
        mp.setattr(sys, "platform", "win32")
        on_pc = speed.use_gpu(Settings(speed="balanced", gpu_whisper=True))
    assert on_mac == (True, False, False) and on_pc is False


def test_whisper_cpp_is_whisper_enough_for_the_mac(monkeypatch, tmp_path):
    from ytclip import transcribe, whisper_cpp

    monkeypatch.setattr(transcribe, "has_faster_whisper", lambda: False)
    monkeypatch.setattr(whisper_cpp, "binary", lambda: None)
    assert not transcribe.available()
    monkeypatch.setattr(whisper_cpp, "binary", lambda: tmp_path / "whisper-cli")
    assert transcribe.available()


def test_the_app_finds_what_it_brought(tmp_path):
    from ytclip import bundled

    contents = tmp_path / "ClipMonster.app" / "Contents"
    exe = contents / "MacOS" / "ClipMonster"
    tool = contents / "Frameworks" / "whisper-cpp" / "whisper-cli"
    for f in (exe, tool):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")
    assert bundled.folders() == [] and bundled.find("whisper-cpp", "whisper-cli") is None  # from source
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "frozen", True, raising=False)
        mp.setattr(sys, "executable", str(exe))
        mp.setattr(sys, "_MEIPASS", str(contents / "Frameworks"), raising=False)
        found = bundled.find("whisper-cpp", "whisper-cli")
        folders = bundled.folders()
    assert found == tool.resolve() or found == tool
    assert [f.name for f in folders] == ["MacOS", "Frameworks", "Resources"]


def test_mac_fonts_are_found_by_their_file_names(tmp_path):
    from ytclip import fonts

    d = tmp_path / "Fonts"
    d.mkdir()
    for name in ("Arial.ttf", "Arial Bold.ttf", "Futura.ttc", "Impact.otf", "notes.txt"):
        (d / name).write_bytes(b"")
    assert fonts._mac_font_file("Arial", dirs=(d,)).name == "Arial Bold.ttf"  # the bold face, as libass asks
    assert fonts._mac_font_file("futura", dirs=(d,)).name == "Futura.ttc"
    assert fonts._mac_font_file("Impact", dirs=(d,)).name == "Impact.otf"
    assert fonts._mac_font_file("Comic Sans MS", dirs=(d, tmp_path / "missing")) is None


def test_the_first_builds_linked_pcs_come_over(tmp_path):
    from ytclip.mac import paths

    old = tmp_path / "ClipMonster Remote"
    old.mkdir()
    (old / "hosts.json").write_text('{"hosts": []}', encoding="utf-8")
    (old / "prefs.json").write_text('{"notify": false}', encoding="utf-8")
    new = tmp_path / "ClipMonster" / "pc"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(paths, "old_dir", lambda: old)
        mp.setattr(sys, "platform", "darwin")
        paths._bring_over(new)
    assert (new / "hosts.json").read_text(encoding="utf-8") == '{"hosts": []}'
    assert (new / "prefs.json").is_file() and not (new / "keys.json").exists()
    assert (old / "hosts.json").is_file()  # copied: the old app may still be there


def test_a_video_opened_with_clipmonster_is_added(app, mac, tmp_path):
    """A video dropped on the Dock icon, or opened with ClipMonster in Finder (macOS's FileOpen event)."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QFileOpenEvent
    from PySide6.QtWidgets import QApplication

    video = tmp_path / "talk.mp4"
    video.write_bytes(b"\0" * 10)
    added = []
    mac.add_sources = lambda sources, here=False: added.append(sources)
    mac.hide()
    assert QApplication.sendEvent(app, QFileOpenEvent(str(video)))
    assert added == [[str(video)]] and mac.isVisible()  # brought up, and the video added
    QApplication.sendEvent(app, QFileOpenEvent(QUrl.fromLocalFile(str(video))))
    assert len(added) == 2 and Path(added[1][0]) == video


@pytest.mark.skipif(sys.platform == "win32", reason="a Mac's (and Linux's) PATH")
def test_a_mac_app_finds_the_tools_terminal_does(tmp_path, monkeypatch):
    """An app opened from Finder gets only /usr/bin:/bin:/usr/sbin:/sbin: Homebrew's, Deno's and nvm's folders
    are added, those that are there, with the newest Node nvm has."""
    from ytclip.mac import app as mac_app

    home = tmp_path / "home"
    for d in (".deno/bin", ".nvm/versions/node/v9.11.2/bin", ".nvm/versions/node/v20.11.1/bin",
              ".nvm/versions/node/v18.20.4/bin"):
        (home / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    path = mac_app.find_tools(env).split(os.pathsep)
    assert path[:4] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]  # what it was given comes first
    assert str(home / ".deno" / "bin") in path and str(home / ".bun" / "bin") not in path  # only those there
    assert [p for p in path if ".nvm" in p] == [str(home / ".nvm/versions/node/v20.11.1/bin")]  # v20, not "v9"
    assert all(os.path.isdir(p) for p in path[4:])
    assert mac_app.find_tools(env).split(os.pathsep) == path == env["PATH"].split(os.pathsep)  # nothing doubled


def test_youtube_downloads_get_a_javascript_runtime(tmp_path, monkeypatch):
    """yt-dlp needs a JavaScript runtime for YouTube's player challenges, and only tries Deno by itself:
    ClipMonster.app brings its own Deno and uses it; from source, whatever is installed, else the deno
    package's."""
    from ytclip import bundled, download

    own = tmp_path / "ClipMonster.app" / "Contents" / "Frameworks" / "deno" / "deno"
    package = tmp_path / "site-packages" / "deno" / "bin" / "deno"
    for f in (own, package):
        f.parent.mkdir(parents=True)
        f.write_text("")
    installed = {}
    monkeypatch.setattr(download.shutil, "which", lambda name: installed.get(name))
    monkeypatch.setattr(bundled, "find", lambda *parts: None)
    monkeypatch.setitem(sys.modules, "deno", SimpleNamespace(find_deno_bin=lambda: str(package)))

    assert download.js_runtimes() == {"deno": {"path": str(package)}}  # from source, nothing installed
    installed.update(node="/usr/local/bin/node", bun="/opt/homebrew/bin/bun")
    assert download.js_runtimes() == {"node": {}, "bun": {}}  # what's installed

    monkeypatch.setattr(sys, "frozen", True, raising=False)  # in ClipMonster.app: its own, whatever's installed
    monkeypatch.setattr(bundled, "find", lambda *parts: own if parts == ("deno", "deno") else None)
    assert download.js_runtimes() == {"deno": {"path": str(own)}}
    assert download._base_opts(None, None)["js_runtimes"] == {"deno": {"path": str(own)}}

    monkeypatch.setattr(bundled, "find", lambda *parts: None)  # nothing anywhere: yt-dlp is left to itself
    monkeypatch.setitem(sys.modules, "deno", None)
    installed.clear()
    assert download.own_deno() is None and download.js_runtimes() == {}
    assert "js_runtimes" not in download._base_opts(None, None)


def test_the_app_runs_the_ffmpeg_it_brought(tmp_path, monkeypatch):
    """ClipMonster.app's ffmpeg has the Mac's media engine and caption burning: it's used even with another
    ffmpeg on the PATH (which may have neither). From source, the installed one, as before."""
    from ytclip import bundled, ffmpeg

    own = tmp_path / "ffmpeg"
    own.write_text("")
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: "/opt/homebrew/bin/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr(bundled, "find", lambda *parts: own if parts == ("ffmpeg", "ffmpeg") else None)
    ffmpeg.ffmpeg_exe.cache_clear()
    try:
        assert ffmpeg.ffmpeg_exe() == "/opt/homebrew/bin/ffmpeg"
        ffmpeg.ffmpeg_exe.cache_clear()
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert ffmpeg.ffmpeg_exe() == str(own)
        ffmpeg.ffmpeg_exe.cache_clear()
        monkeypatch.setattr(bundled, "find", lambda *parts: None)  # a build without it (the PC app's)
        assert ffmpeg.ffmpeg_exe() == "/opt/homebrew/bin/ffmpeg"
    finally:
        ffmpeg.ffmpeg_exe.cache_clear()


def test_the_build_gathers_fresh_tools_and_rebuilds_stale_ones(tmp_path, monkeypatch):
    """packaging/build_mac.py: ffmpeg and Deno copied from their packages every time, whisper.cpp and llama.cpp
    built only when missing or built from another tag."""
    import runpy
    import subprocess

    import imageio_ffmpeg

    build_mac = runpy.run_path(str(Path(__file__).resolve().parents[1] / "packaging" / "build_mac.py"))
    gather = build_mac["gather_tools"]
    src = tmp_path / "src"
    src.mkdir()
    (src / "ffmpeg").write_text("ffmpeg 2")
    (src / "deno").write_text("deno 2")
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: str(src / "ffmpeg"))
    monkeypatch.setitem(sys.modules, "deno", SimpleNamespace(find_deno_bin=lambda: str(src / "deno")))
    monkeypatch.setattr(build_mac["platform"], "machine", lambda: "arm64")
    built = []

    def run(cmd, **kw):
        if cmd[0].endswith("ffmpeg"):
            return SimpleNamespace(stdout=" V....D h264_videotoolbox    VideoToolbox H.264 Encoder\n")
        out = Path(cmd[2])
        built.append((Path(cmd[1]).name, "--metal" in cmd))
        name = "whisper-cli" if "whisper" in cmd[1] else "llama-server"
        out.mkdir(parents=True, exist_ok=True)
        (out / name).write_text("built")
        project = "whisper.cpp" if "whisper" in cmd[1] else "llama.cpp"
        tag = runpy.run_path(cmd[1])["TAG"]
        (out / "VERSION.txt").write_text(f"{project} {tag} (Metal)\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    tools = tmp_path / "tools"
    (tools / "ffmpeg").mkdir(parents=True)
    (tools / "ffmpeg" / "ffmpeg").write_text("ffmpeg 1")  # an older one from an earlier build
    assert gather(tools) == []
    assert (tools / "ffmpeg" / "ffmpeg").read_text() == "ffmpeg 2" and (tools / "deno" / "deno").read_text() == "deno 2"
    assert os.access(tools / "deno" / "deno", os.X_OK)
    assert built == [("build_whisper_cpp.py", True), ("build_llama_cpp.py", True)]  # for the Mac's graphics

    built.clear()
    assert gather(tools) == [] and built == []  # already built from these tags
    (tools / "llama-cpp" / "VERSION.txt").write_text("llama.cpp b1 (Metal)\n")  # built from an older tag
    assert gather(tools) == [] and built == [("build_llama_cpp.py", True)]

    built.clear()
    (tools / "whisper-cpp" / "whisper-cli").unlink()
    assert gather(tools, build_missing=False) == ["whisper-cpp/whisper-cli"] and built == []


@pytest.mark.skipif(sys.platform == "win32", reason="its stand-in programs are shell scripts")
def test_the_built_apps_self_check_starts_everything_it_brought(app, tmp_path, monkeypatch, capsys):
    """`ClipMonster.app/Contents/MacOS/ClipMonster --self-check`, as the workflows run it on each build: every
    program the app brought is started, and one that doesn't start fails the build."""
    pytest.importorskip("ytclip.gui.main_window", exc_type=ImportError)  # no Qt Multimedia libraries (Linux CI)
    from ytclip import bundled, ffmpeg
    from ytclip.mac.app import self_check

    frameworks = tmp_path / "Frameworks"

    def program(folder, name, script, version=""):
        f = frameworks / folder / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"#!/bin/sh\n{script}\n")
        f.chmod(0o755)
        if version:
            (f.parent / "VERSION.txt").write_text(version + "\n")
        return f

    program("ffmpeg", "ffmpeg", "echo ' V....D h264_videotoolbox    VideoToolbox H.264 Encoder'")
    program("deno", "deno", "echo deno 2.9.8")
    whisper = program("whisper-cpp", "whisper-cli", 'test "$1" = -h', "whisper.cpp v1.9.5 (Metal)")
    program("llama-cpp", "llama-server", 'test "$1" = --version', "llama.cpp b12194 (Metal)")
    certifi = pytest.importorskip("certifi")
    certs = frameworks / "certifi" / "cacert.pem"  # where certifi's hook puts them in the app
    certs.parent.mkdir(parents=True)
    shutil.copy(certifi.where(), certs)
    monkeypatch.setenv("SSL_CERT_FILE", str(certs))  # (as the app's start sets it: mac/app.py)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(bundled, "folders", lambda: [frameworks])
    ffmpeg.ffmpeg_exe.cache_clear()
    try:
        rc = self_check(app)
        out = capsys.readouterr().out
        assert "ffmpeg ok (VideoToolbox)" in out and "deno ok" in out, out
        assert "whisper.cpp v1.9.5 (Metal) ok" in out and "llama.cpp b12194 (Metal) ok" in out, out
        assert "HTTPS certificates of its own" in out, out
        assert rc == 0 or "video MISSING" in out  # (Qt's video can't load on some Linux test machines)
        whisper.write_text("#!/bin/sh\nexit 1\n")  # it doesn't start (a library missing, its signature broken)
        (frameworks / "deno" / "deno").unlink()
        # certificates that are there only on the Mac it was built on (python.org's Python folder)
        monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
        rc = self_check(app)
        out = capsys.readouterr().out
        assert rc == 1 and "whisper.cpp MISSING" in out and "deno MISSING" in out, out
        assert "HTTPS certificates MISSING" in out, out
    finally:
        ffmpeg.ffmpeg_exe.cache_clear()

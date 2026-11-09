"""ClipMonster for Mac making clips, and the PC lending it a hand (ytclip/mac/pc_ai.py, pc_jobs.py and window.py;
ytclip/remote/studio.py on the PC): the PC's local AI answering the Mac's questions, whole videos sent to the
PC and their clips brought back into the Mac's Library, against a real host server with a stand-in for the
PC's engine. Also the Mac's own pieces: its words in Settings, keys kept out of settings.json, the media
engine, fonts, and what the packaged app brings."""

import copy
import io
import json
import os
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
    mj = MacJobs(lambda: s, lambda m: None, lambda kind, text, item="": events.append((kind, text, item)),
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
    assert c.get("/api/studio/jobs") == {"jobs": [], "busy": ""}


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
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(bundled, "folders", lambda: [frameworks])
    ffmpeg.ffmpeg_exe.cache_clear()
    try:
        rc = self_check(app)
        out = capsys.readouterr().out
        assert "ffmpeg ok (VideoToolbox)" in out and "deno ok" in out, out
        assert "whisper.cpp v1.9.5 (Metal) ok" in out and "llama.cpp b12194 (Metal) ok" in out, out
        assert rc == 0 or "video MISSING" in out  # (Qt's video can't load on some Linux test machines)
        whisper.write_text("#!/bin/sh\nexit 1\n")  # it doesn't start (a library missing, its signature broken)
        (frameworks / "deno" / "deno").unlink()
        rc = self_check(app)
        out = capsys.readouterr().out
        assert rc == 1 and "whisper.cpp MISSING" in out and "deno MISSING" in out, out
    finally:
        ffmpeg.ffmpeg_exe.cache_clear()

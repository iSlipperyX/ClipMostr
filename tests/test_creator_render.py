"""Faster Creator renders (creator/compositor.py): scenes animated several at a time for Settings → Speed, on the
graphics card's encoder when there is one, falling back to the processor's when it fails."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ytclip.creator import compositor, motion
from ytclip.ffmpeg import probe


def s(speed="balanced", encoder="auto"):
    return SimpleNamespace(speed=speed, encoder=encoder)


def test_how_many_at_once():
    assert compositor.scene_workers(s("quiet"), 9, logical=16) == 1
    assert compositor.scene_workers(s("balanced"), 9, logical=16) == 2
    assert compositor.scene_workers(s("full"), 9, logical=16) == 4
    assert compositor.scene_workers(s("full"), 9, logical=4) == 2
    assert compositor.scene_workers(s("full"), 3, logical=64) == 3  # never more than there are scenes
    assert compositor.scene_workers(s("bogus"), 9, logical=16) == 2  # unknown: balanced


def test_scene_clips_stay_near_lossless_on_every_encoder():
    from ytclip import ffmpeg

    def inter(enc):
        return ffmpeg.video_args(enc, purpose="intermediate")

    assert inter("libx264")[:6] == ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14"]
    amf = inter("h264_amf")
    assert amf[:2] == ["-c:v", "h264_amf"] and ["-rc", "cqp"] == amf[6:8] and str(ffmpeg.SCENE_QP) in amf
    assert inter("h264_nvenc")[:2] == ["-c:v", "h264_nvenc"]
    assert inter("h264_qsv")[:2] == ["-c:v", "h264_qsv"]
    assert inter("h264_videotoolbox")[:2] == ["-c:v", "h264_videotoolbox"]  # a Mac's scenes on its media engine
    assert compositor._intermediate(Path("x.mp4"), "h264_amf")[-4:] == ["-pix_fmt", "yuv420p", "-an", "x.mp4"]


def jobs_for(tmp_path, n):
    return [compositor.SceneJob(Path(f"in{i}.png"), "image", 1.0, tmp_path / f"scene_{i}.mp4", motion.get("slow_push_in"))
            for i in range(n)]


class FakeRender:
    def __init__(self, fail_on=None, delay=None):
        self.calls = []
        self.lock = threading.Lock()
        self.fail_on = fail_on or (lambda dest, enc: None)
        self.delay = delay or (lambda dest: 0.01)

    def __call__(self, source, kind, seconds, dest, move, progress=None, check=None, encoder="libx264"):
        with self.lock:
            self.calls.append((dest.name, encoder, threading.current_thread().name))
        for k in range(4):
            if check:
                check()
            time.sleep(self.delay(dest) / 4)
            if progress:
                progress((k + 1) / 4)
        problem = self.fail_on(dest, encoder)
        if problem:
            raise problem
        dest.write_bytes(b"clip")
        return dest


def test_scenes_at_once_come_back_in_order(tmp_path):
    jobs = jobs_for(tmp_path, 6)
    render = FakeRender(delay=lambda dest: 0.2 if dest.name == "scene_0.mp4" else 0.02)  # the first is slowest
    said = []
    main = threading.current_thread().name
    clips, used = compositor.render_scenes(jobs, workers=3, encoder="h264_amf", render=render,
                                           progress=lambda m, f: said.append((m, f, threading.current_thread().name)))
    assert clips == [j.dest for j in jobs] and all(c.read_bytes() == b"clip" for c in clips)
    assert used == "h264_amf" and {e for _, e, _ in render.calls} == {"h264_amf"}
    assert len({t for _, _, t in render.calls}) > 1  # really several at once
    assert said and all(t == main for _, _, t in said)  # progress only from the calling thread
    assert said[-1][0] == "Animating scenes, 3 at a time: 6 of 6 done" and said[-1][1] == 1.0


def test_one_at_a_time_says_which_scene(tmp_path):
    said = []
    clips, used = compositor.render_scenes(jobs_for(tmp_path, 2), workers=1, render=FakeRender(),
                                           progress=lambda m, f: said.append((m, round(f, 3))))
    assert used == "libx264" and said[0] == ("Animating scene 1 of 2", 0.125) and said[-1] == ("Animating scene 2 of 2", 1.0)


def test_a_graphics_card_encoder_that_fails_is_dropped(tmp_path):
    jobs = jobs_for(tmp_path, 4)
    render = FakeRender(fail_on=lambda dest, enc: RuntimeError("AMF: out of sessions")
                        if enc == "h264_amf" and dest.name == "scene_1.mp4" else None)
    logs = []
    clips, used = compositor.render_scenes(jobs, workers=2, encoder="h264_amf", render=render, log=logs.append)
    assert used == "libx264" and all(c.is_file() for c in clips)
    assert ("scene_1.mp4", "libx264") in [(d, e) for d, e, _ in render.calls]  # made again on the processor
    assert len(logs) == 1 and "(h264_amf) failed on a scene (AMF: out of sessions)" in logs[0]


def test_a_real_failure_stops_the_lot(tmp_path):
    render = FakeRender(fail_on=lambda dest, enc: RuntimeError("no picture") if dest.name == "scene_0.mp4" else None,
                        delay=lambda dest: 0.05 if dest.name == "scene_0.mp4" else 1.0)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="no picture"):
        compositor.render_scenes(jobs_for(tmp_path, 8), workers=2, encoder="libx264", render=render)
    assert time.monotonic() - started < 2.5  # the others stopped at their next check, the rest never started
    assert len(render.calls) < 8


class Cancelled(Exception):
    pass


def test_cancelling_stops_every_scene(tmp_path):
    calls = {"n": 0}

    def check():  # cancelled while the first three are half done
        calls["n"] += 1
        if calls["n"] >= 2:
            raise Cancelled()

    render = FakeRender(delay=lambda dest: 1.6)
    jobs = jobs_for(tmp_path, 6)
    started = time.monotonic()
    with pytest.raises(Cancelled):
        compositor.render_scenes(jobs, workers=3, render=render, check=check)
    assert time.monotonic() - started < 1.5  # the three stopped at their next check: none ran to the end
    assert len(render.calls) == 3 and not any(j.dest.exists() for j in jobs)


def test_real_scenes_two_at_a_time(tmp_path):
    cv2 = pytest.importorskip("cv2")
    pic = tmp_path / "pic.png"
    img = (np.indices((480, 270)).sum(axis=0) % 64 * 4).astype(np.uint8)
    cv2.imwrite(str(pic), np.dstack([img, img[::-1], img]))
    jobs = [compositor.SceneJob(pic, "image", 0.4, tmp_path / f"scene_{i}.mp4", motion.get(m))
            for i, m in enumerate(("slow_push_in", "pan_left", "slow_pull_out"))]
    clips, used = compositor.render_scenes(jobs, workers=2, encoder="libx264")
    for c in clips:
        info = probe(c)
        assert (info.width, info.height) == (compositor.WIDTH, compositor.HEIGHT) and 0.3 < info.duration < 0.6

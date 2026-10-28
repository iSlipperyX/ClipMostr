"""2.5.0's faster clipping in the engine: Whisper on just the clips, extra clips kept by their
ratings, a video downloaded early, scene cuts on the graphics card."""

import json
from pathlib import Path

import numpy as np
import pytest

from ytclip import engine as engine_mod
from ytclip.analyze import Clip, Measurements
from ytclip.download import Video
from ytclip.engine import Engine, Project
from ytclip.settings import Settings
from ytclip.subtitles import Cue, Word


def _words(start, end, tag, step=1.0):
    out, t = [], start
    while t < end:
        out.append(Word(t, t + step * 0.8, f"{tag}{int(t)}"))
        t += step
    return out


def _project(tmp_path, duration=300.0):
    v = Video(id="vid12345678", title="T", channel="C", upload_date="", duration=duration, url="u",
              path=tmp_path / "v.mp4")
    p = Project("https://youtu.be/vid12345678", video=v)
    p.words = _words(0, duration, "cap")
    p.transcript_source = "captions"
    p.measurements = Measurements(duration=duration, db=np.zeros(int(duration * 2)),
                                  cues=[Cue(w.start, w.end, w.text) for w in p.words])
    return p


@pytest.fixture
def typed(monkeypatch):
    """Whisper stand-in: what it was asked to type out, answering with 'wh' words."""
    calls = []
    monkeypatch.setattr(engine_mod.whisper, "available", lambda: True)

    def fake(self, v, spans=None, checkpoint=None):
        calls.append(list(spans or []))
        words = [w for a, b in spans for w in _words(a, b, "wh")]
        return [Cue(w.start, w.end, w.text) for w in words], words

    monkeypatch.setattr(Engine, "_whisper", fake)
    return calls


def _engine(tmp_path, **kw):
    return Engine(Settings(cache_dir=str(tmp_path / "cache"), transcribe="clips", trim_silence=False, **kw))


def test_whisper_types_out_only_the_clips(tmp_path, typed):
    e, p = _engine(tmp_path), _project(tmp_path)
    p.clips = [Clip(100, 130, 80), Clip(20, 50, 70), Clip(200, 230, 60, enabled=False)]
    assert e.retype(p) == 2
    assert typed == [[(17.0, 53.0), (97.0, 133.0)]]  # three seconds either side; switched-off clips aren't typed
    inside = [w.text for w in p.words if 100 <= w.start < 130]
    assert inside and all(t.startswith("wh") for t in inside)
    assert all(w.text.startswith("cap") for w in p.words if 60 <= w.start < 90)  # the rest stays as captions
    assert p.transcript_source == "captions+whisper"
    assert [w.start for w in p.words] == sorted(w.start for w in p.words)
    assert any(c.text.startswith("wh") for c in p.measurements.cues)
    # Nothing new: nothing typed out again.
    assert e.retype(p) == 0 and len(typed) == 1


def test_only_new_clips_are_typed_out_and_the_cache_keeps_them(tmp_path, typed):
    e, p = _engine(tmp_path), _project(tmp_path)
    p.clips = [Clip(100, 130, 80)]
    e.retype(p)
    p.clips.append(Clip(250, 280, 70))
    e.retype(p)
    assert typed == [[(97.0, 133.0)], [(247.0, 283.0)]]
    cache = json.loads((tmp_path / "cache" / "vid12345678.whisper-base.clips.json").read_text(encoding="utf-8"))
    assert cache["spans"] == [[97.0, 133.0], [247.0, 283.0]]
    # The same video opened again: the saved stretches are used, nothing is typed out.
    q = _project(tmp_path)
    q.clips = [Clip(100, 130, 80), Clip(250, 280, 70)]
    assert e.retype(q) == 2 and len(typed) == 2
    assert all(w.text.startswith("wh") for w in q.words if 250 <= w.start < 280)


def test_retype_only_when_asked_and_only_over_captions(tmp_path, typed):
    p = _project(tmp_path)
    p.clips = [Clip(100, 130, 80)]
    for s in ("auto", "always", "never"):
        assert Engine(Settings(cache_dir=str(tmp_path), transcribe=s)).retype(p) == 0
    p.transcript_source = "whisper"  # the whole video was typed out already
    assert _engine(tmp_path).retype(p) == 0
    assert typed == []


def test_export_types_out_clips_added_by_hand(tmp_path, typed, monkeypatch):
    e, p = _engine(tmp_path), _project(tmp_path)
    p.clips = [Clip(100, 130, 80)]
    e.retype(p)
    hand = Clip(150, 170, 50, origin="manual", edited=True)
    assert e.retype(p, [hand]) == 1 and typed[-1] == [(147.0, 173.0)]
    assert (hand.start, hand.end) == (150, 170)  # clips passed in are never moved


def _rated(start, rating, score=50.0):
    c = Clip(start, start + 30, score)
    c.rating = rating
    return c


def test_keep_best_keeps_the_best_rated(tmp_path):
    e = Engine(Settings(qc_min_rating=5))
    p = Project("u")
    p.clips = [_rated(0, 8), _rated(100, 3), _rated(200, 9), _rated(300, 6), _rated(400, 7)]
    logs = []
    e.log = logs.append
    assert e.keep_best(p, 3) == 2
    assert [c.rating for c in p.clips if c.enabled] == [8, 9, 7]
    assert "Kept the 3 best of 5 clips" in logs[0] and "3–6/10" in logs[0]


def test_keep_best_drops_low_ratings_even_with_room(tmp_path):
    e = Engine(Settings(qc_min_rating=5))
    p = Project("u")
    p.clips = [_rated(0, 8), _rated(100, 2)]
    e.keep_best(p, 6)
    assert [c.enabled for c in p.clips] == [True, False]


def test_keep_best_without_ratings_goes_by_score(tmp_path):
    e = Engine(Settings(qc_min_rating=5))
    p = Project("u")
    p.clips = [_rated(0, 0, 40), _rated(100, 0, 90), _rated(200, 0, 70)]
    e.keep_best(p, 2)
    assert [c.score for c in p.clips if c.enabled] == [90, 70]


def test_a_part_series_stays_or_goes_whole(tmp_path):
    e = Engine(Settings(qc_min_rating=5))
    p = Project("u")
    first, second = _rated(0, 9), _rated(30, 4)  # part 2 starts where part 1 ends
    p.clips = [first, second, _rated(200, 7), _rated(300, 6)]
    e.keep_best(p, 3)
    assert not first.enabled and not second.enabled  # its part 2 is rated too low: both go
    assert [c.rating for c in p.clips if c.enabled] == [7, 6]


def test_overpick_only_with_polish_and_ratings():
    assert Engine(Settings(ai_polish=True, clip_count=6)).overpick() == 3
    assert Engine(Settings(ai_polish=True, clip_count=1)).overpick() == 1
    assert Engine(Settings(ai_polish=False, clip_count=6)).overpick() == 0
    assert Engine(Settings(ai_polish=True, polish_overpick=False)).overpick() == 0
    assert Engine(Settings(ai_polish=True, qc_min_rating=0)).overpick() == 0


def test_prepare_finds_extra_then_keeps_the_best(tmp_path, monkeypatch):
    e = Engine(Settings(ai_polish=True, clip_count=4, qc_min_rating=5))
    seen = {}
    monkeypatch.setattr(e, "open", lambda p: None)
    monkeypatch.setattr(e, "measure", lambda p: None)

    def find(p):
        seen["count"] = e.s.clip_count
        p.clips = [_rated(i * 100, 0, 90 - i) for i in range(e.s.clip_count)]

    def polish(p):
        for c, r in zip(p.clips, (9, 3, 8, 7, 6, 2)):
            c.rating = r
        p.polished = True

    monkeypatch.setattr(e, "find", find)
    monkeypatch.setattr(e, "polish", polish)
    p = Project("u")
    e.prepare(p)
    assert seen["count"] == 6 and e.s.clip_count == 4  # found 6, and the setting is back to 4
    assert [c.rating for c in p.clips if c.enabled] == [9, 8, 7, 6]


def test_a_video_downloaded_early_isnt_downloaded_again(tmp_path, monkeypatch):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"x")
    monkeypatch.setattr(engine_mod, "download", lambda *a, **k: pytest.fail("downloaded again"))
    monkeypatch.setattr(engine_mod, "probe", lambda p: type("M", (), {"duration": 60.0})())
    p = Project("https://youtu.be/abcdefghijk", video=Video(id="abcdefghijk", title="T", channel="C", upload_date="",
                                                             duration=60, url="u", path=path))
    Engine(Settings()).open(p)
    assert p.video.path == path


def test_scene_cuts_on_the_graphics_card_fall_back_to_the_cpu(tmp_path, monkeypatch):
    asked = []

    def detect(path, duration, progress, hwaccel=False):
        asked.append(hwaccel)
        if hwaccel:
            raise RuntimeError("ffmpeg failed: no hardware decoder")
        return [1.0, 2.0]

    monkeypatch.setattr(engine_mod, "detect_scenes", detect)
    v = Video(id="x", title="T", channel="C", upload_date="", duration=60, url="u", path=Path("v.mp4"))
    assert Engine(Settings(speed="balanced"))._scenes(v) == [1.0, 2.0] and asked == [True, False]
    asked.clear()
    assert Engine(Settings(speed="quiet"))._scenes(v) == [1.0, 2.0] and asked == [False]


def test_whisper_gets_the_speeds_threads_and_card(tmp_path, monkeypatch):
    got = {}
    monkeypatch.setattr(engine_mod.whisper, "transcribe", lambda *a, **k: got.update(k) or ([], []))
    v = Video(id="x", title="T", channel="C", upload_date="", duration=60, url="u", path=Path("v.mp4"))
    Engine(Settings(speed="full"))._whisper(v, spans=[(0, 5)])
    assert got["threads"] == engine_mod.speed.threads("full") and got["gpu"] and got["spans"] == [(0, 5)]
    Engine(Settings(speed="quiet"))._whisper(v)
    assert got["threads"] == engine_mod.speed.threads("quiet") and not got["gpu"]


def test_whisper_failing_on_the_clips_leaves_youtubes_captions(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod.whisper, "available", lambda: True)

    def broken(self, v, spans=None, checkpoint=None):
        raise OSError("model download failed")

    monkeypatch.setattr(Engine, "_whisper", broken)
    e, p = _engine(tmp_path), _project(tmp_path)
    logs = []
    e.log = logs.append
    p.clips = [Clip(100, 130, 80)]
    assert e.retype(p) == 0 and p.transcript_source == "captions"
    assert all(w.text.startswith("cap") for w in p.words) and "model download failed" in logs[0]

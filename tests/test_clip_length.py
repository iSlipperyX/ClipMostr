"""2.5.1: no clip comes out shorter than Min clip length, wherever its settings came from."""

import numpy as np
import pytest
from test_series import SENTS, _find_project

from ytclip import series
from ytclip.analyze import Clip, reach_length, trim_dead_air
from ytclip.engine import Engine
from ytclip.pipeline import Job, settings_for
from ytclip.settings import PRESETS, Settings, all_presets
from ytclip.subtitles import Word

# ---------------------------------------------------------------- growing a short clip


def test_a_short_clip_grows_to_a_sentence_end():
    # SENTS: 0-4.5, 5-9.5, ... so a clip starting at 100 reaches 60 s at the sentence ending 164.5
    assert reach_length(100.0, 117.0, SENTS, 60, 90) == (100.0, 164.8)
    assert reach_length(100.0, 170.0, SENTS, 60, 90) == (100.0, 170.0)  # long enough already


def test_with_no_room_after_it_the_start_goes_back_instead():
    start, end = reach_length(100.0, 117.0, SENTS, 60, 90, hi=120.0)
    assert end == 117.0 and start == pytest.approx(54.85) and end - start >= 60


def test_no_sentences_still_makes_it_long_enough():
    assert reach_length(10.0, 27.0, [], 60, 90) == (10.0, 70.0)
    assert reach_length(10.0, 27.0, [], 60, 90, lo=0.0, hi=40.0) is None  # 40 s of room isn't enough
    assert reach_length(10.0, 27.0, [], 60, 90, lo=0.0, hi=65.0) == (5.0, 65.0)


def test_a_part_only_grows_away_from_the_part_next_to_it():
    # Part 1 of 2: its end is where part 2 starts, so only its start may move.
    start, end = reach_length(100.0, 117.0, SENTS, 60, 90, grow_end=False)
    assert end == 117.0 and end - start >= 60
    # A middle part can't move either edge.
    assert reach_length(100.0, 117.0, SENTS, 60, 90, grow_start=False, grow_end=False) is None


def test_trimming_dead_air_never_goes_under_the_minimum():
    words = [Word(12, 13, "a"), Word(30, 31, "b")]
    clip = Clip(0.0, 61.0, 50)
    assert not trim_dead_air(clip, words, min_len=60)
    assert (clip.start, clip.end) == (0.0, 61.0)


# ---------------------------------------------------------------- the engine


def _blips(length=300):
    """A video whose good moments are short bursts: the finder's first guesses are ~20 s clips."""
    score = np.full(length, 0.2)
    for a in (30, 120, 220):
        score[a:a + 18] = 0.95
    return score


@pytest.mark.parametrize("trim", [False, True])
def test_every_clip_found_is_at_least_the_minimum(tmp_path, monkeypatch, trim):
    monkeypatch.setattr(Engine, "rescore", lambda self, p: setattr(p, "score", _blips()))
    p = _find_project(tmp_path)
    p.words = [Word(t, t + 0.4, "w") for t in np.arange(0.0, 300.0, 0.5)]
    logs = []
    Engine(Settings(mode="auto", min_len=60, max_len=90, clip_count=6, trim_silence=trim),
           log=logs.append).find(p)
    assert p.clips and all(c.length >= 60 - 0.01 for c in p.clips), [c.length for c in p.clips]
    assert all(c.length <= 90 + 0.5 for c in p.clips)
    on = sorted(p.clips, key=lambda c: c.start)
    assert all(a.end <= b.start + 0.01 for a, b in zip(on, on[1:]))  # and none overlap


def test_ai_picks_that_are_too_short_grow_or_go(tmp_path, monkeypatch):
    monkeypatch.setattr(Engine, "rescore", lambda self, p: setattr(p, "score", _blips()))
    monkeypatch.setattr("ytclip.ai.find_clips_ai",
                        lambda *a, **k: ([Clip(30, 47, 90, origin="ai"), Clip(120, 140, 80, origin="ai")], "m"))
    p = _find_project(tmp_path)
    Engine(Settings(mode="ai", ai_api_key="k", min_len=60, max_len=90, trim_silence=False)).find(p)
    assert len(p.clips) == 2 and all(c.length >= 60 for c in p.clips)


def test_a_clip_with_no_room_is_dropped_and_said(tmp_path):
    p = _find_project(tmp_path)
    p.clips = [Clip(0, 70, 90), Clip(70.5, 85, 50), Clip(85.5, 160, 80)]  # the middle one is boxed in
    logs = []
    e = Engine(Settings(min_len=60, max_len=90, series_parts=1), log=logs.append)
    assert e.hold_length(p) == 1
    assert [c.start for c in p.clips] == [0, 85.5] and "couldn't be made 60 s long" in logs[-1]


def test_a_video_shorter_than_the_minimum_and_clips_you_edited(tmp_path):
    p = _find_project(tmp_path, duration=40)
    p.clips = [Clip(0, 38, 90)]  # the whole 40 s video is shorter than a minute: the clip is all of it
    e = Engine(Settings(min_len=60, max_len=90))
    assert e.hold_length(p) == 0 and (p.clips[0].start, p.clips[0].end) == (0, 40)
    assert e.lengths(40) == (40, 90)
    p = _find_project(tmp_path)
    p.clips = [Clip(10, 25, 90, edited=True)]
    assert e.hold_length(p) == 0 and p.clips[0].length == 15


def test_a_longest_under_the_shortest_is_raised_to_it():
    assert Engine(Settings(min_len=60, max_len=58)).lengths(600) == (60, 60)


def test_next_parts_are_never_shorter_than_the_minimum():
    first = Clip(39.85, 129.8, 90.0)  # as long as allowed: the moment carries on
    score = np.full(400, 0.2)
    score[40:240] = 0.9  # the moment ends 110 s into part 2
    clips = [first]
    series.extend(clips, score, SENTS, 60, 90, count=5, max_parts=3, duration=400)
    assert len(clips) >= 2 and all(c.length >= 60 for c in clips)


# ---------------------------------------------------------------- where the length comes from


def test_presets_dont_change_clip_length():
    assert not any("min_len" in v or "max_len" in v for v in PRESETS.values())
    s = Settings(min_len=60, max_len=90).apply(all_presets()["Shorts / TikTok / Reels"])
    assert (s.min_len, s.max_len) == (60, 90)


def _auto(tmp_path, **kw):
    base = {"youtube_api_key": "K", "auto_output_dir": str(tmp_path / "out"), "min_len": 60, "max_len": 90}
    return Settings().apply(base | kw)


def test_automation_uses_your_clip_length_not_its_styles(tmp_path, monkeypatch):
    # An older preset saved with 15-second clips: Automation's style no longer sets the length.
    monkeypatch.setattr("ytclip.pipeline.all_presets",
                        lambda: {"Old shorts": {"aspect": "vertical_blur", "min_len": 15, "max_len": 58}})
    got, _ = settings_for(Job("a", "u"), _auto(tmp_path, auto_preset="Old shorts"))
    assert (got.min_len, got.max_len, got.aspect) == (60, 90, "vertical_blur")


def test_a_niches_own_length_still_wins(tmp_path):
    niche = {"id": "n1", "name": "Gaming", "min_len": 75, "max_len": 120}
    got, _ = settings_for(Job("a", "u", niche="n1"), _auto(tmp_path, niches=[niche]))
    assert (got.min_len, got.max_len) == (75, 120)
    only_min = {"id": "n1", "name": "Gaming", "min_len": 100}  # longer than Settings' longest: that grows
    got, _ = settings_for(Job("a", "u", niche="n1"), _auto(tmp_path, niches=[only_min]))
    assert (got.min_len, got.max_len) == (100, 100)
    only_max = {"id": "n1", "name": "Gaming", "max_len": 45}
    got, _ = settings_for(Job("a", "u", niche="n1"), _auto(tmp_path, niches=[only_max]))
    assert (got.min_len, got.max_len) == (45, 45)


def test_the_runner_says_what_length_it_clips(tmp_path):
    from test_pipeline import _runner

    r = _runner(tmp_path, _auto(tmp_path))
    r.db.add(Job("a", "https://youtu.be/aaaaaaaaaaa", title="T"), 0)
    r.process(r.db.get("a"), r.load())
    assert any("clips 60–90 s" in m for m in r.logs), r.logs

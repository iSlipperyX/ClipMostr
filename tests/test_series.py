import json

import numpy as np
from test_polish import _export_project

from ytclip import engine as engine_mod
from ytclip import layout, qc, series
from ytclip.analyze import Clip, Measurements
from ytclip.engine import Engine
from ytclip.settings import Settings
from ytclip.subtitles import Cue

SENTS = [Cue(t, t + 4.5, f"sentence {t}") for t in np.arange(0.0, 300.0, 5.0)]  # 0.5 s between sentences


def _score(hot=(40, 200), length=300):
    score = np.full(length, 0.2)
    score[hot[0]:hot[1]] = 0.9
    return score


def _first():
    return Clip(39.85, 99.8, 90.0, "the big story")  # sentence-aligned and as long as allowed


def test_a_moment_longer_than_one_clip_carries_on_in_parts():
    clips = [_first(), Clip(250.0, 270.0, 30.0, "a weak one")]
    n = series.extend(clips, _score(), SENTS, 20, 60, count=5, max_parts=3, duration=300)
    assert n == 2
    parts = sorted([c for c in clips if c.score > 50], key=lambda c: c.start)
    assert [(c.start, c.end) for c in parts] == [(39.85, 99.8), (99.85, 159.8), (159.85, 199.8)]
    assert all(c.length <= 60 for c in parts)  # and the last part ends with the moment (at 200 s)
    labels = series.labels(clips)
    got = {(c.start, lab) for c, lab in zip(clips, labels)}
    assert {(39.85, ("series-1", 1, 3)), (99.85, ("series-1", 2, 3)), (159.85, ("series-1", 3, 3)),
            (250.0, ("", 0, 0))} == got
    # every part but the last promises a next part
    assert [layout.has_next_part(c, clips) for c in parts] == [True, True, False]
    assert clips[:3] == parts  # parts sit together, in order, so their files are numbered in order


def test_parts_stop_when_the_moment_does():
    clips = [_first()]
    assert series.extend(clips, _score(hot=(40, 110)), SENTS, 20, 60, count=5, max_parts=3, duration=300) == 0
    assert len(clips) == 1


def test_a_clip_with_room_to_spare_or_parts_off_gets_no_next_part():
    short = [Clip(39.85, 79.8, 90.0)]
    assert series.extend(short, _score(), SENTS, 20, 60, 5, 3, 300) == 0
    assert series.extend([_first()], _score(), SENTS, 20, 60, 5, 1, 300) == 0


def test_a_weaker_clip_further_into_the_moment_gives_way():
    clips = [_first(), Clip(120.0, 140.0, 80.0, "inside"), Clip(165.0, 185.0, 95.0, "better")]
    series.extend(clips, _score(), SENTS, 20, 60, 5, 3, 300)
    clips.sort(key=lambda c: c.start)
    assert [c.title for c in clips] == ["the big story", "", "better"]  # part 2 took the weaker clip's place
    assert [lab[1:] for lab in series.labels(clips)] == [(1, 2), (2, 2), (0, 0)]  # the better one stopped part 3
    clips = [_first(), Clip(120.0, 140.0, 80.0, "inside", edited=True)]
    series.extend(clips, _score(), SENTS, 20, 60, 5, 3, 300)
    assert len(clips) == 2  # yours stays


def test_a_long_pause_ends_the_moment():
    sents = [c for c in SENTS if not 100 <= c.start < 105]  # 5.5 s of silence after the first clip
    clips = [_first()]
    assert series.extend(clips, _score(), sents, 20, 60, 5, 3, 300) == 0


def test_parts_come_out_of_the_clip_count():
    clips = [_first(), Clip(230.0, 250.0, 40.0, "b"), Clip(260.0, 280.0, 35.0, "c")]
    series.extend(clips, _score(), SENTS, 20, 60, count=3, max_parts=3, duration=300)
    assert len(clips) == 3 and all(c.score > 50 for c in clips)  # the weaker standalone clips made room
    clips = [_first(), Clip(230.0, 250.0, 40.0, "b")]
    series.extend(clips, _score(), SENTS, 20, 60, count=2, max_parts=3, duration=300)
    assert [lab[1:] for lab in series.labels(clips)] == [(1, 2), (2, 2)]  # cut down to the room there is
    clips, score = [_first(), Clip(230.0, 250.0, 75.0, "b")], _score()
    score[160:200] = 0.7  # still the moment, a little less good
    series.extend(clips, score, SENTS, 20, 60, count=3, max_parts=3, duration=300)
    assert [c.score for c in clips] == [90.0, 91.2, 75.0]  # (scored like the clip it carries on from) a weaker part 3 doesn't push out a better clip


def test_a_clip_already_there_is_the_next_part():
    clips = [_first(), Clip(99.85, 130.0, 70.0, "already")]
    assert series.extend(clips, _score(), SENTS, 20, 60, 5, 3, 300) == 0
    assert [lab[1:] for lab in series.labels(clips)] == [(1, 2), (2, 2)]


def test_a_rejected_part_breaks_the_series():
    clips = [Clip(0, 10, 80), Clip(10.5, 20, 70), Clip(21, 30, 60), Clip(100, 110, 50)]
    assert [lab[1:] for lab in series.labels(clips)] == [(1, 3), (2, 3), (3, 3), (0, 0)]
    assert [lab[1:] for lab in series.labels(clips, [True, False, True, True])] == [(0, 0)] * 4
    got = series.labels(clips, [False, True, True, True])  # the first part gone: the other two are a series of two
    assert [lab[1:] for lab in got] == [(0, 0), (1, 2), (2, 2), (0, 0)]


def _find_project(tmp_path, duration=300):
    p = _export_project(tmp_path)
    p.video.duration = duration
    p.measurements = Measurements(duration=duration, db=np.full(duration * 10, -20.0), cues=SENTS)
    p.clips = []
    return p


def test_the_engine_adds_parts_and_logs_it(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "pick_clips", lambda *a, **k: [_first(), Clip(250, 270, 30)])
    monkeypatch.setattr(Engine, "rescore", lambda self, p: setattr(p, "score", _score()))
    logs = []
    s = Settings(mode="auto", min_len=20, max_len=60, clip_count=5, trim_silence=False)
    p = _find_project(tmp_path)
    Engine(s, log=logs.append).find(p)
    assert len(p.clips) == 4 and any("Added 2 next parts" in m for m in logs)
    p = _find_project(tmp_path)
    Engine(s.copy(series_parts=1), log=logs.append).find(p)
    assert len(p.clips) == 2


def test_the_finders_own_parts_are_listed_together(tmp_path, monkeypatch):
    found = [Clip(100.0, 120.0, 90.0), Clip(10.0, 30.0, 80.0), Clip(120.5, 140.0, 70.0)]
    monkeypatch.setattr(engine_mod, "pick_clips", lambda *a, **k: [Clip(c.start, c.end, c.score) for c in found])
    monkeypatch.setattr(Engine, "rescore", lambda self, p: setattr(p, "score", _score()))
    monkeypatch.setattr(engine_mod, "clip_score", lambda score, a, b: {100.0: 90.0, 10.0: 80.0}.get(a, 70.0))
    p = _find_project(tmp_path)
    Engine(Settings(mode="auto", min_len=15, max_len=60, clip_count=5, trim_silence=False)).find(p)
    assert [c.start for c in p.clips] == [99.85, 119.85, 9.85]  # part 2 straight after part 1


def test_clips_json_numbers_the_parts_that_survived(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "export_clip", lambda src, media, dest, clip, w, s, prog=None, next_part=True:
                        dest.write_bytes(b"clip"))
    s = Settings(output_dir=str(tmp_path / "out"), thumbnails=False, tiktok_ready=False, write_srt=False)
    p = _export_project(tmp_path)
    p.clips = [Clip(10, 40, 80, "one"), Clip(41, 70, 70, "two"), Clip(71, 100, 60, "three"), Clip(200, 220, 50, "x")]
    records = json.loads((Engine(s).export(p) / "clips.json").read_text(encoding="utf-8"))["clips"]
    assert [(r.get("part"), r.get("parts")) for r in records] == [(1, 3), (2, 3), (3, 3), (None, None)]
    assert len({r.get("series") for r in records[:3]}) == 1

    monkeypatch.setattr(qc, "measure", lambda path: qc.FileStats(0 if "three" in path.name else 1))
    monkeypatch.setattr(qc, "judge_file", lambda stats, *a: qc.Verdict() if stats.duration else
                        qc.Verdict(False, ["The file can't be read"]))
    p = _export_project(tmp_path)
    p.clips = [Clip(10, 40, 80, "one"), Clip(41, 70, 70, "two"), Clip(71, 100, 60, "three")]
    out = Engine(s.copy(qc_enabled=True)).export(p)
    records = json.loads((out / "clips.json").read_text(encoding="utf-8"))["clips"]
    assert [(r.get("part"), r.get("parts")) for r in records] == [(1, 2), (2, 2), (None, None)]


# ---------------------------------------------------------------- found in review (2.4.0)

def test_spare_picks_fill_the_places_a_series_took():
    clips = [_first(), Clip(120.0, 140.0, 80.0, "inside"), Clip(230.0, 250.0, 40.0, "b")]
    spares = [Clip(201.0, 221.0, 35.0, "too close"), Clip(265.0, 285.0, 30.0, "spare")]
    series.extend(clips, _score(), SENTS, 20, 60, count=5, max_parts=3, duration=300, spares=spares)
    titles = [c.title for c in clips]
    assert len(clips) == 5 and "spare" in titles and "inside" not in titles
    assert "too close" not in titles  # it would look like a part 4


def test_parts_of_an_ai_pick_are_marked_as_parts_and_scored_on_its_scale():
    first = Clip(39.85, 99.8, 95.0, "claude liked it", origin="ai")  # signals alone say 88.8
    clips = [first]
    series.extend(clips, _score(), SENTS, 20, 60, count=5, max_parts=3, duration=300)
    parts = [c for c in clips if c is not first]
    assert parts and all(c.origin == "part" for c in parts)
    assert all(abs(c.score - (90.0 + 95.0 - 88.8)) < 0.2 for c in parts)


def test_fitting_clips_that_overlap_says_so_without_failing():
    logs = []
    db = np.where(np.arange(1200) % 40 < 4, -70.0, -20.0)  # a pause every 20 s
    m = Measurements(duration=600, db=db)
    clips = [Clip(10.0, 40.0, 80.0), Clip(30.0, 60.0, 70.0)]  # the second overlaps the first
    kept = Engine(Settings(), log=logs.append)._fit(clips, [], m, 600)
    assert len(kept) == 1 and any("Skipped 1" in line for line in logs)
    assert Engine(Settings(), log=logs.append)._fit([Clip(10.0, 40.0, 80.0), Clip(30.0, 60.0, 70.0)], [], m, 600,
                                                    silent=True)

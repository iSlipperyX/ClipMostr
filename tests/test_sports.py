"""Sports clips (sports.py): the crowd, the commentator's calls and the replays find the plays, each one is cut as
a whole play, and a sports category's clips go to the linked Macs (remote/deliveries.py)."""

import json
import wave
from pathlib import Path

import numpy as np
import pytest
from test_series import _find_project

from ytclip import analyze, categories, sports
from ytclip.analyze import Clip, Measurements
from ytclip.categories import Category
from ytclip.engine import Engine
from ytclip.remote import deliveries
from ytclip.settings import Settings
from ytclip.subtitles import Cue

RATE = 8000
ROARS = ((60, 68), (150, 160), (240, 246))  # the crowd erupting
SHOUT = 100  # one loud voice: not a crowd


def match_audio(path: Path, seconds: int = 300) -> Path:
    """A match: a murmur, a commentator talking all the way through, three roars and one shout."""
    rng = np.random.default_rng(7)
    t = np.arange(seconds * RATE) / RATE
    x = 0.02 * rng.standard_normal(len(t))
    x += 0.08 * np.sin(2 * np.pi * 180 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)) ** 2
    x += 0.04 * np.sin(2 * np.pi * 360 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))
    for a, b in ROARS:
        m = (t >= a) & (t < b)
        env = np.clip((t[m] - a) / 1.0, 0, 1) * np.clip((b - t[m]) / 2.0, 0, 1)
        x[m] += 0.35 * rng.standard_normal(m.sum()) * env
    m = (t >= SHOUT) & (t < SHOUT + 1)
    x[m] += 0.4 * np.sin(2 * np.pi * 300 * t[m])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


@pytest.fixture(scope="module")
def match(tmp_path_factory):
    path = match_audio(tmp_path_factory.mktemp("match") / "match.wav")
    cen, flat = [], []
    db, fine = analyze.loudness(path, 300, centroid=cen, flatness=flat)
    cues = [Cue(147, 149, "oh what a goal from him"), Cue(163, 166, "let's see that again")]
    return Measurements(duration=300, db=db, fine=fine, centroid=cen[0], flatness=flat[0], cues=cues)


# --- hearing the crowd ------------------------------------------------------------------------------------------


def test_a_roar_is_noise_and_a_voice_is_not():
    rng = np.random.default_rng(1)
    t = np.arange(4000) / RATE
    roar = (rng.standard_normal((1, 4000)) * 8000).astype(np.int16)
    voice = (np.sin(2 * np.pi * 200 * t) * 8000)[None, :].astype(np.int16)
    silence = np.zeros((1, 4000), np.int16)
    got = sports.spectral_flatness(np.vstack([roar, voice, silence]))
    assert got[0] > 0.4 and got[1] < 0.05 and got[2] == 0


def test_the_crowd_signal_finds_the_roars_not_the_shout(match):
    crowd = sports.crowd_signal(match.db, match.flatness, 300)
    for a, b in ROARS:
        assert crowd[a:b].max() > 0.6, (a, b)
    assert crowd[SHOUT - 1:SHOUT + 3].max() < 0.3  # one loud second of one voice
    assert crowd[10:50].max() < 0.2


def test_calls_mark_the_seconds_before_them():
    sport = sports.get("soccer")
    cues = [Cue(100, 102, "GOOOOAL what a strike"), Cue(200, 202, "and the corner is taken")]
    sig = sports.calls_signal(cues, 300, sport)
    assert sig[95] > 0.5 and sig[200] == 0  # the play is just before the call
    assert sports.calls_pattern(sports.get("football")).search("TOUCHDOWN Chiefs")
    assert sports.calls_pattern(sports.get("any")).search("down goes Frazier")  # every sport's calls
    assert not sports.calls_pattern(sport).search("the keeper takes his time")


def test_replays_credit_the_moment_before_them():
    sig = sports.replay_signal([Cue(130, 132, "let's see that again")], 300)
    assert sig[110] > 0.5 and sig[131] < 0.2
    assert sports.replay_times([Cue(5, 6, "here it is in slow motion"), Cue(9, 10, "nothing")]) == [5]


def test_cuts_after_a_moment_are_credited_to_it():
    cuts = [float(t) for t in range(50, 70, 2)] + [10.0, 200.0, 280.0]
    sig = sports.aftermath_signal(cuts, 300)
    assert sig[30:48].max() > 0.9 and int(np.argmax(sig)) < 50 and sig[150] == 0  # before the cuts start


# --- cutting whole plays ----------------------------------------------------------------------------------------


def test_each_roar_is_a_whole_play(match):
    sport = sports.get("soccer")
    sig = sports.build_signals(match, sport, [])
    assert {"crowd", "calls", "replay", "voice"} <= set(sig) and "scenes" not in sig
    score = analyze.combine(sig, sports.weights({}), 300)
    clips = sports.pick_moments(score, sig["crowd"], 5, *sport.lengths, sport, replays=sports.replay_times(match.cues))
    assert len(clips) == 3
    by_start = sorted(clips, key=lambda c: c.start)
    for c, (a, b) in zip(by_start, ROARS):
        assert c.start <= a - sport.lead + 2  # the build-up is in it
        assert c.end >= b - 1  # and the reaction
        assert sport.lengths[0] - 0.5 <= c.length <= sport.lengths[1]
    goal = by_start[1]
    assert goal.end <= 163  # stops where the replay starts
    assert clips[0] is goal  # the roar with the commentator's call is the best one
    assert "commentator" in sports.reason(goal, sig, match.cues, sport)
    assert not any(c.start < SHOUT < c.end and c is not by_start[0] for c in clips)


def test_plays_keep_their_distance_and_their_lengths():
    sport = sports.get("basketball")
    score = np.full(200, 0.1)
    crowd = np.zeros(200)
    for t in (50, 62, 150):  # two moments 12 s apart, one far off
        score[t:t + 3] = 1.0
        crowd[t:t + 5] = 1.0
    clips = sports.pick_moments(score, crowd, 5, 10, 30, sport, gap=8)
    clips.sort(key=lambda c: c.start)
    for a, b in zip(clips, clips[1:]):
        assert a.end <= b.start
    assert all(10 - 0.5 <= c.length <= 30 for c in clips)
    assert sports.pick_moments(np.zeros(5), None, 3, 10, 30, sport)[0].end == 5.0  # a very short video: all of it


def test_edges_only_move_outwards():
    sents = [Cue(10, 14, "a"), Cue(30, 36, "b")]
    assert sports.outward(12, 33, sents, 60, 100) == (9.9, 36.2)
    assert sports.outward(12, 33, sents, 20, 100) == (12, 33)  # not past the longest


# --- the engine and the category --------------------------------------------------------------------------------


def test_a_sports_category_brings_its_lengths_and_sport():
    c = Category.from_json({"name": "Premier League", "sport": "Soccer", "account_id": "a"})
    assert c.sport == "soccer" and c.sport_info.emoji == "⚽"
    assert c.clip_overrides() == {"min_len": 15, "max_len": 40, "sport": "soccer"}
    own = Category.from_json({"name": "NBA", "sport": "basketball", "min_len": 20, "account_id": "a"})
    assert own.clip_overrides() == {"min_len": 20, "max_len": 30, "sport": "basketball"}
    assert Category.from_json({"name": "x", "sport": "curling"}).sport == ""
    assert Category.from_json({"name": "x"}).clip_overrides() == {}


def test_the_category_clips_with_its_sport(tmp_path):
    from ytclip.pipeline import Job, settings_for

    s = Settings(auto_output_dir=str(tmp_path), categories=[{"id": "f1", "name": "Football", "sport": "soccer",
                                                              "account_id": "a"}])
    got, label = settings_for(Job("a", "u", category_id="f1"), s)
    assert got.sport == "soccer" and (got.min_len, got.max_len) == (15, 40)
    assert "whole plays" in label


def test_the_engine_picks_plays_for_a_sport(tmp_path, monkeypatch, match):
    p = _find_project(tmp_path)
    p.measurements = match
    p.words = []
    s = Settings(mode="auto", sport="soccer", min_len=15, max_len=40, clip_count=3, series_parts=1)
    e = Engine(s)
    assert e.weights["crowd"] > 0 and e.weights.get("scenes", 0) == 0
    e.find(p)
    assert len(p.clips) == 3
    assert all(c.reason.startswith("⚽") for c in p.clips)
    assert "Crowd roar" in e.signal_names(p)
    plain = Engine(Settings(mode="auto"))
    assert "crowd" not in plain.weights


def test_sports_skips_trimming_to_the_talk(tmp_path):
    from ytclip.subtitles import Word

    p = _find_project(tmp_path)
    p.clips = [Clip(10.0, 40.0, 50)]
    p.words = [Word(20, 21, "what"), Word(30, 31, "a goal")]
    Engine(Settings(sport="hockey", trim_silence=True, min_len=5)).trim(p)
    assert (p.clips[0].start, p.clips[0].end) == (10.0, 40.0)
    Engine(Settings(trim_silence=True, min_len=5)).trim(p)
    assert p.clips[0].start > 10.0


def test_the_ai_is_told_about_the_sport(tmp_path, monkeypatch):
    import ytclip.ai as ai

    told = {}

    def fake(title, duration, sents, score, count, lo, hi, instructions, *a, **k):
        told["text"] = instructions
        return [Clip(100.0, 125.0, 80, origin="ai")], "m"

    monkeypatch.setattr(ai, "find_clips_ai", fake)
    p = _find_project(tmp_path)
    Engine(Settings(mode="ai", sport="tennis", ai_instructions="no swearing", min_len=10, max_len=45)).find(p)
    assert "tennis footage" in told["text"] and "whole play" in told["text"] and "no swearing" in told["text"]


# --- sent to the Macs -------------------------------------------------------------------------------------------


def exported(tmp_path: Path, n: int = 3, rejected: int = 1) -> Path:
    folder = tmp_path / "Automation" / "Football" / "Match"
    (folder / "thumbnails").mkdir(parents=True)
    (folder / "_rejected").mkdir()
    recs = []
    for i in range(1, n + 1):
        name = f"{i:02d} - goal {i}.mp4" if i <= n - rejected else f"_rejected/{i:02d} - meh.mp4"
        (folder / name).write_bytes(b"\0" * (1000 + i))
        (folder / "thumbnails" / f"{i:02d}.jpg").write_bytes(b"jpg")
        recs.append({"rank": i, "file": name, "start": 10.0 * i, "end": 10.0 * i + 25, "length": 25.0, "score": 90 - i,
                     "title": f"goal {i}", "reason": "⚽ crowd roar", "watch": f"https://youtu.be/x?t={10 * i}",
                     **({"hook": f"Watch the keeper {i}"} if i == 1 else {}),
                     **({"qc": {"ok": False}} if i > n - rejected else {})})
    (folder / "clips.json").write_text(json.dumps({"video": {"title": "Arsenal v Spurs", "channel": "Sky"},
                                                   "source": "https://youtu.be/x", "clips": recs}), encoding="utf-8")
    return folder


def test_a_videos_clips_wait_for_the_macs(tmp_path):
    where = tmp_path / "deliveries"
    cat = Category.from_json({"id": "f1", "name": "Football", "sport": "soccer", "send_to_macs": True,
                              "account_id": "a"})
    folder = exported(tmp_path)
    made = deliveries.add(folder, cat, now=1000.0, where=where)
    assert [m["title"] for m in made] == ["Watch the keeper 1", "goal 2"]  # not the rejected one
    assert made[0]["emoji"] == "⚽" and made[0]["source_title"] == "Arsenal v Spurs" and made[0]["thumb"]
    (folder / "01 - goal 1.mp4").unlink()  # posted and cleaned up: the Macs still get it
    assert deliveries.file_of(made[0], where=where).read_bytes() == b"\0" * 1001
    mine = deliveries.for_device("mac1", where=where)
    assert [m["new"] for m in mine] == [True, True] and mine[0]["ext"] == ".mp4"
    deliveries.mark(made[0]["id"], "mac1", "got", now=1001.0, where=where)
    deliveries.mark(made[1]["id"], "mac1", "dismissed", now=1001.0, where=where)
    mine = deliveries.for_device("mac1", where=where)
    assert len(mine) == 1 and mine[0]["got"] and not mine[0]["new"]
    assert len(deliveries.for_device("mac2", where=where)) == 2
    # exported again (Make again): its clips are replaced, not doubled
    assert len(deliveries.add(folder, cat, now=1002.0, where=where)) == 1  # (goal 1's file is gone now)
    assert len(deliveries.load(where)["items"]) == 1


def test_clips_go_once_every_mac_has_them_or_they_get_old(tmp_path):
    where = tmp_path / "deliveries"
    cat = Category.from_json({"id": "f1", "name": "Football", "sport": "soccer", "account_id": "a"})
    made = deliveries.add(exported(tmp_path, 3, 0), cat, now=0.0, where=where)
    for m in made[:2]:
        deliveries.mark(m["id"], "mac1", "got", now=1.0, where=where)
    assert deliveries.prune(["mac1"], now=3600.0, where=where) == 0  # every Mac has two, but only an hour ago
    assert deliveries.prune(["mac1"], now=2 * 86400.0, where=where) == 2
    left = deliveries.load(where)["items"]
    assert [i["id"] for i in left] == [made[2]["id"]]
    assert not (where / made[0]["file"]).exists() and (where / left[0]["file"]).exists()
    assert deliveries.prune(["mac1"], now=8 * 86400.0, where=where) == 1  # a week old
    assert deliveries.prune([], where=where) == 0


def test_the_size_limit_lets_the_oldest_go(tmp_path):
    where = tmp_path / "deliveries"
    cat = Category.from_json({"id": "f1", "name": "Football", "account_id": "a"})
    made = deliveries.add(exported(tmp_path, 3, 0), cat, now=0.0, where=where)
    with deliveries.edit(where) as data:
        for i, item in enumerate(data["items"]):
            item["at"] = float(i)
    assert deliveries.prune(["mac1"], now=10.0, where=where, max_bytes=2100) == 1
    assert made[0]["id"] not in {i["id"] for i in deliveries.load(where)["items"]}


def test_automation_sends_a_sports_categorys_clips(tmp_path, monkeypatch):
    from ytclip.pipeline import Runner
    from ytclip.remote import state

    monkeypatch.setattr(state, "path", lambda: tmp_path / "remote.json")
    monkeypatch.setattr(deliveries, "folder", lambda: tmp_path / "deliveries")
    logs, events = [], []
    r = Runner(log=logs.append)
    r.remote = type("R", (), {"host": type("H", (), {"add_event": lambda self, *a, **k: events.append((a, k))})()})()
    cat = Category.from_json({"id": "f1", "name": "Football", "sport": "soccer", "send_to_macs": True,
                              "account_id": "a"})
    folder = exported(tmp_path)
    assert r.deliver(folder, cat) == 0  # no Mac is linked: nothing is kept for nobody
    with state.edit() as data:
        state.new_device(data, "MacBook", 1.0)
    assert r.deliver(folder, cat) == 2
    assert events[0][0][0] == "delivery" and events[0][0][1].startswith("⚽ 2 new Soccer (football) clips")
    assert any("Sent 2 clips" in m for m in logs)
    off = Category.from_json({"id": "g1", "name": "Gaming", "account_id": "a"})
    assert r.deliver(folder, off) == 0


def test_categories_round_trip_their_sport():
    s = Settings(categories=[{"id": "f1", "name": "Football", "sport": "soccer", "send_to_macs": True}])
    c = categories.load(s)[0]
    assert c.sport == "soccer" and c.send_to_macs
    assert categories.to_settings([c])[0]["sport"] == "soccer"

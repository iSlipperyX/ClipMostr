"""The Sports tab's recap reel (sportsdesk/montage, framing, scoreboard, overlays, sound, reel): the plan's cut
and length, the words (by an AI that can't invent names, or from the plays' own titles), following the action,
checking a read scoreboard, the graphics, the mix's graph, the desk queuing and making a reel with a stand-in
maker, the Mac asking for one, and the command line."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ytclip import ai
from ytclip.settings import Settings
from ytclip.sportsdesk import framing, montage, overlays, scoreboard, sound
from ytclip.sportsdesk import games as G
from ytclip.sportsdesk.desk import DeskError
from ytclip.subtitles import Cue

from test_sports_desk import desk, seed, worker  # noqa: F401 - fixtures


def hls(n=6, gap=90.0, length=14.0, first=30.0):
    """n highlights, best first, spread over a game."""
    out = []
    for i in range(n):
        a = first + i * gap
        out.append({"id": f"h{i + 1}", "start": a, "end": a + length, "rank": i + 1, "score": 90 - i * 5,
                    "chosen": i < 5, "title": f"Play {i + 1}", "play": ["Poster dunk", "Three", "Block", "Steal",
                                                                         "Layup", "Pass"][i % 6],
                    "players": ["LeBron James"] if i == 0 else []})
    return out


GAME = {"id": "g1", "sport": "basketball", "home": "Celtics", "away": "Lakers", "matchup": "Lakers at Celtics",
        "title": "LAKERS at CELTICS | FULL GAME HIGHLIGHTS", "score": ""}


def crowd_for(highlights, duration):
    c = np.zeros(int(duration) + 1)
    for h in highlights:
        c[int(h["start"] + 9)] = 1.0  # the roar, 9 s into each play
    return c


# ---------------------------------------------------------------- the plan


def test_the_plan_is_a_minute_or_more_with_the_best_play_last():
    h = hls(6)
    reel = montage.plan(GAME, h, crowd=crowd_for(h, 600), duration=600)
    assert montage.REEL_MIN <= reel.duration <= montage.REEL_MAX and reel.eligible
    assert reel.segments[0].kind == "teaser" and reel.segments[0].hid == "h1"
    plays = reel.plays
    assert montage.MIN_PLAYS <= len(plays) <= montage.MAX_PLAYS
    assert plays[-1].hid == "h1" and plays[-1].label == "NUMBER 1"
    assert [p.label for p in plays[:-1]] == [f"PLAY {k}" for k in range(len(plays), 1, -1)]
    for p in plays:
        assert p.start <= p.moment <= p.end
        assert montage.PLAY_MIN - 0.5 <= p.length <= montage.PLAY_STRETCH + 0.5
        assert p.moment - p.start > p.end - p.moment  # the build-up gets more than the reaction
    teaser = reel.segments[0]
    assert teaser.end < plays[-1].moment  # the opening stops short of the payoff
    starts = reel.starts()
    assert starts[0] == 0 and all(b > a for a, b in zip(starts, starts[1:]))


def test_as_it_happened_and_a_game_winner():
    h = hls(5)
    reel = montage.plan(GAME, h, crowd=crowd_for(h, 600), duration=600, order="timeline")
    assert [p.start for p in reel.plays] == sorted(p.start for p in reel.plays)
    assert reel.plays[0].hid == "h1" and reel.plays[0].label.startswith("#1")
    h[0]["title"] = "Buzzer-beater to win it"
    countdown = montage.plan(GAME, h, crowd=crowd_for(h, 600), duration=600)
    assert countdown.plays[-1].label == "THE GAME WINNER"


def test_a_play_stops_before_its_replay():
    h = hls(5)
    replay_at = h[1]["start"] + 11.5
    cues = [Cue(replay_at, replay_at + 3, "Let's see that again in slow motion")]
    reel = montage.plan(GAME, h, crowd=crowd_for(h, 600), cues=cues, duration=600)
    seg = next(p for p in reel.plays if p.hid == "h2")
    assert seg.end <= replay_at - montage.REPLAY_GUARD + 0.01


def test_the_plays_asked_for_are_used_and_topped_up():
    h = hls(7)
    reel = montage.plan(GAME, h, crowd=None, duration=800, ids=["h6", "h7"])
    got = {p.hid for p in reel.plays}
    assert {"h6", "h7"} <= got and len(got) >= montage.MIN_PLAYS


def test_too_few_plays():
    with pytest.raises(montage.ReelError):
        montage.plan(GAME, [], crowd=None)
    with pytest.raises(montage.ReelError):
        montage.plan(GAME, hls(1), crowd=None, duration=100)
    short = hls(2, gap=12, length=10)  # two plays back to back: no room to reach a minute
    reel = montage.plan(GAME, short, crowd=None, duration=60)
    assert not reel.eligible and any("under a minute" in n for n in reel.notes)


# ---------------------------------------------------------------- the words


@pytest.fixture
def reel():
    h = hls(5)
    return montage.plan(GAME, h, crowd=crowd_for(h, 600), duration=600)


def test_without_an_ai_the_words_come_from_the_plays(monkeypatch, reel):
    monkeypatch.setattr(ai, "order", lambda s: [])
    st = montage.story(GAME, reel, Settings(), {}, narrator=True)
    assert st.written_by == "" and "No AI" in st.note
    assert st.opening.startswith("Lakers at Celtics.") and "Five plays" in st.opening
    assert st.hook == "THE TOP 5 PLAYS"
    assert st.badges["h1"] == "LEBRON JAMES · POSTER DUNK" and st.badges["h2"] == "THREE"
    assert not any("PLAY AT" in b for b in st.badges.values())
    assert "Celtics" in st.hashtags and "basketball" in st.hashtags
    assert montage.story(GAME, reel, Settings(), {}, narrator=False).opening == ""


def test_the_ai_writes_the_words_but_cant_invent_a_scorer(monkeypatch, reel):
    asked = {}

    def ask(s, task, system, content, schema, **k):
        asked.update(system=system, content=content)
        return SimpleNamespace(model="claude-x", data={
            "opening": "The Lakers stunned Boston on the road. LeBron sealed it late, and the crowd went silent.",
            "hook": "LeBron silenced Boston",
            "caption": "Lakers at Celtics was wild", "hashtags": ["#NBA", "Lakers", "Lakers", "LeBron James"],
            "badges": [{"n": len(reel.plays), "text": "LeBron James · poster dunk"},
                       {"n": 1, "text": "Jayson Tatum · deep three"}]})

    monkeypatch.setattr(ai, "order", lambda s: ["claude"])
    monkeypatch.setattr(ai, "ask_json", ask)
    st = montage.story(GAME, reel, Settings(), {}, narrator=True)
    assert st.written_by == "claude-x" and st.hook == "LEBRON SILENCED BOSTON"
    assert st.opening.startswith("The Lakers stunned Boston")
    assert st.hashtags == ["NBA", "Lakers", "LeBronJames"]
    best = reel.plays[-1].hid
    assert st.badges[best] == "LEBRON JAMES · POSTER DUNK"
    first = reel.plays[0].hid
    assert "TATUM" not in st.badges.get(first, "")  # no one named Tatum in the plays, title or commentary
    assert "basketball" in asked["system"]


def test_a_failing_ai_leaves_the_plain_words(monkeypatch, reel):
    monkeypatch.setattr(ai, "order", lambda s: ["local"])
    monkeypatch.setattr(ai, "ask_json", lambda *a, **k: (_ for _ in ()).throw(ai.AIError("busy")))
    st = montage.story(GAME, reel, Settings(), {}, narrator=True)
    assert st.written_by == "" and "busy" in st.note and st.opening


# ---------------------------------------------------------------- following the action


def moving_dot(n=40, w=framing.SAMPLE_W, h=framing.SAMPLE_H, x0=0.2, x1=0.8):
    rng = np.random.default_rng(1)
    pitch = rng.integers(40, 90, (h, w), dtype=np.uint8)  # a still, textured pitch: the camera isn't moving
    frames = np.repeat(pitch[None], n, axis=0)
    for i in range(n):
        frames[i] += rng.integers(0, 3, (h, w), dtype=np.uint8)  # a little noise
        x = int((x0 + (x1 - x0) * i / (n - 1)) * w)
        frames[i, h // 2 - 6:h // 2 + 6, x - 6:x + 6] = 230
    return frames


def test_the_window_follows_the_moving_play():
    frames = moving_dot()
    xs, energy = framing.action_points(frames)
    assert abs(xs[5] - 0.27) < 0.08 and abs(xs[-2] - 0.78) < 0.08
    pts, found = framing.targets(xs, energy)
    assert found > 0.8
    half = (1080 * 9 / 16) / 1920 / 2
    cam = framing.ease(pts, framing.SAMPLE_FPS, half)
    assert cam[-1] > cam[0] + 0.25  # it went with the play
    assert np.all(np.abs(np.diff(cam)) < 0.06)  # smoothly
    assert cam.min() >= half - 1e-9 and cam.max() <= 1 - half + 1e-9  # never past the picture's edge


def test_the_window_holds_when_nothing_moves():
    still = np.full((20, framing.SAMPLE_H, framing.SAMPLE_W), 80, np.uint8)
    xs, energy = framing.action_points(still)
    pts, found = framing.targets(xs, energy)
    assert found == 0 and np.allclose(pts, pts[0])
    path = framing.ActionPath(np.array([0.0, 1.0]), np.array([0.3, 0.7]), 1.0)
    assert path.at(0.5) == pytest.approx(0.5) and framing.ActionPath(np.array([]), np.array([]), 0).at(3) == 0.5


# ---------------------------------------------------------------- the scoreboard


def test_a_read_scoreboard_is_checked():
    S = scoreboard.Score
    reads = [S("LAL", 50, "GSW", 48), S("LAL", 61, "GSW", 60), S("BOS", 3, "NYK", 1), S("LAL", 40, "GSW", 70),
             None, S("LAL", 99, "GSW", 101, "4th", "0:12")]
    out = scoreboard.consistent(reads, [10, 20, 30, 40, 50, 60])
    assert out[0] and out[1] and out[5]
    assert out[2] is None  # another game's bug
    assert out[3] is None  # a score that went down: a misread
    assert out[5].text() == "LAL 99 | 101 GSW  4th · 0:12"
    assert scoreboard._score({"left_team": "lal", "left_score": "1O", "right_team": "GSW", "right_score": "3"}) is None
    assert scoreboard._score({"left_team": " lal ", "left_score": " 7", "right_team": "GSW",
                              "right_score": "3"}).left == "LAL"


def test_the_teams_for_a_bug_without_a_score():
    assert scoreboard.teams_for(GAME) == ("LAKERS", "CELTICS")  # away at home
    assert scoreboard.teams_for({"sport": "soccer", "home": "Arsenal", "away": "Chelsea"}) == ("ARSENAL", "CHELSEA")
    assert scoreboard.short_name("Golden State Warriors") == "WARRIORS"


def test_only_an_ai_that_sees_pictures_reads_the_scoreboard(monkeypatch):
    monkeypatch.setattr(ai, "order", lambda s: ["local"])
    assert not scoreboard.can_read(Settings())
    assert scoreboard.read("x.mp4", [1.0, 2.0], Settings()) == ([None, None], "")
    monkeypatch.setattr(ai, "order", lambda s: ["local", "gemini"])
    assert scoreboard.can_read(Settings())


# ---------------------------------------------------------------- the graphics and the sound


def test_the_graphics(reel):
    reel.story = montage.Story(hook="LEBRON SILENCED BOSTON", badges={reel.plays[-1].hid: "LEBRON JAMES · DUNK"})
    reel.final = "Lakers 108-106 Celtics"
    for seg in reel.plays:
        seg.badge = reel.story.badges.get(seg.hid, "")
    best = reel.plays[-1]
    S = scoreboard.Score
    ass = overlays.build(reel, {best.hid: (S("LAL", 104, "BOS", 106), S("LAL", 107, "BOS", 106))},
                         teams=("LAKERS", "CELTICS"), matchup="Lakers at Celtics")
    assert "[Events]" in ass and "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    body = ass.split("[Events]")[1]
    for text in ("LEBRON", "NUMBER 1", "PLAY 4", "LAKERS", "CELTICS", "104", "107", "FINAL",
                 "LEBRON JAMES · DUNK"):
        assert text in body, text
    plain = overlays.build(reel, None, teams=("LAKERS", "CELTICS"))
    assert "FINAL" in plain.split("[Events]")[1]  # the final score from the title, even without a scoreboard
    reel.final = ""
    assert "FINAL" not in overlays.build(reel, None, teams=("A", "B")).split("[Events]")[1]


def test_the_mix_lifts_the_crowd_and_ducks_the_music(reel, tmp_path):
    snd = sound.Sound(tmp_path / "game.mp4", narrator=tmp_path / "n.wav", music=tmp_path / "m.wav",
                      effects=sound.effects_for(reel, tmp_path / "w.wav", tmp_path / "i.wav"))
    args, graph = sound.mix_graph(reel, snd, {"input_i": "-20", "input_tp": "-3", "input_lra": "6",
                                              "input_thresh": "-30", "target_offset": "0"})
    assert args.count("-i") == len(reel.segments) + 2 + len(snd.effects)  # + the narrator and the music
    assert "0.5*c0-0.5*c1" in graph and "between(t," in graph  # the crowd (side) lifted at each moment
    assert "sidechaincompress" in graph and "loudnorm" in graph and "alimiter" in graph
    assert graph.rstrip().endswith("[aout]")
    assert len(sound.lifts(reel)) == len(reel.plays)
    quiet = sound.Sound(tmp_path / "game.mp4", has_audio=False)
    args, graph = sound.mix_graph(reel, quiet)
    assert "anullsrc" in graph and "-i" not in args


# ---------------------------------------------------------------- the desk and the Mac


def test_a_reel_is_asked_for_and_queued(desk):  # noqa: F811
    seed(desk, 2, highlights=True)
    with pytest.raises(DeskError):
        desk.reel("v1")  # no highlights yet
    with pytest.raises(DeskError):
        desk.reel("nope")
    msg = desk.reel("v0", ids=["h1", "h3", "../x"])
    assert "reel" in msg["message"] and "Lakers" in msg["message"]
    assert desk.game("v0")["status"] == "queued"
    task = desk.claim("test:1")
    assert task["kind"] == "reel" and task["payload"]["ids"] == ["h1", "h3"]
    with pytest.raises(DeskError):
        desk.reel("v0")  # being worked on
    f = G.GameFilter.from_json({"reel_voice": "nope", "reel_framing": "fit", "reel_order": "timeline"})
    assert (f.reel_voice, f.reel_framing, f.reel_order) == ("en_storyteller", "fit", "timeline")
    assert G.GameFilter.from_json({"reel_voice": "off"}).reel_voice == "off"


def test_the_worker_makes_the_reel(worker, desk, monkeypatch, tmp_path):  # noqa: F811
    from ytclip.sportsdesk import reel as R

    seed(desk, 1, highlights=True)
    desk.set_filter({"category_id": "h1", "reel_voice": "off", "reel_framing": "fit"})
    made = {}

    def fake_make(p, game, highlights, s, out_root, **k):
        made.update(game=game, root=out_root, **k)
        folder = Path(out_root) / "reel"
        folder.mkdir(parents=True)
        (folder / "01 - reel.mp4").write_bytes(b"mp4")
        plan = montage.plan(game, highlights, crowd=None, duration=800)
        plan.story = montage.Story(written_by="")
        return R.ReelResult(folder, folder / "01 - reel.mp4", plan, {"length": 71.4, "rewards": True}, {}, [])

    monkeypatch.setattr(R, "make", fake_make)
    desk.reel("v0")
    assert worker.step()
    g = desk.game("v0")
    assert g["status"] == "ready" and g["reels"][-1]["length"] == 71.4 and g["reels"][-1]["sent"] == 2
    assert made["voice"] == "off" and made["framing_mode"] == "fit" and made["category"].name == "Hoops"
    assert "Hoops" in Path(made["root"]).parts and Path(made["root"]).name == "Reels"
    assert any("reel" in t for _, t in worker.news)
    assert worker.sent[-1][1] == "Hoops"


def test_a_game_that_cant_make_a_reel_says_why(worker, desk, monkeypatch):  # noqa: F811
    from ytclip.sportsdesk import reel as R

    seed(desk, 1, highlights=True)
    monkeypatch.setattr(R, "make", lambda *a, **k: (_ for _ in ()).throw(montage.ReelError("Not enough plays.")))
    desk.reel("v0")
    worker.step()
    g = desk.game("v0")
    assert g["status"] == "ready" and g["error"] == "Not enough plays."  # its highlights are still there


def test_the_mac_asks_the_pc_for_a_reel(world):  # noqa: F811
    c, _ = world.linked()
    desk = world.host.desk
    seed(desk, 1, highlights=True)
    assert "reel" in c.post("/api/sports/games/v0/reel", {"ids": ["h2"]})["message"]
    task = desk.claim("test:1")
    assert task["kind"] == "reel" and task["payload"]["ids"] == ["h2"]


from test_remote import mac_home, world  # noqa: E402,F401 - fixtures


# ---------------------------------------------------------------- the command line


def test_the_command_line_checks_what_its_given(capsys):
    from ytclip import cli

    with pytest.raises(SystemExit):
        cli.main(["sports-montage", "--help"])
    assert "--creator-rewards" in capsys.readouterr().out
    assert cli.main(["sports-montage", "game.mp4", "--voice", "nobody"]) == 2
    with pytest.raises(SystemExit):
        cli.main(["sports-montage", "game.mp4", "--sport", "curling"])


def test_reel_json_round_trips(reel):
    data = json.loads(json.dumps(reel.to_json()))
    assert data["eligible"] and len(data["segments"]) == len(reel.segments) and data["order"] == "countdown"

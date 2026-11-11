"""The reel compiler's maths and graphs, offline: where each play starts in the reel, the xfade offsets that
follow from it, the score bug's switch time, the shared filtergraph builder (rendering/filtergraph.py) that the
Creator and the reels both use, and one real (tiny) join through ffmpeg to prove the timing holds."""

import re
import subprocess

import pytest

from ytclip import ffmpeg
from ytclip.rendering import filtergraph as fg
from ytclip.sportsdesk import montage, overlays, reel as reel_module, scoreboard


def seg(kind, hid, start, end, moment, label=""):
    return montage.Segment(kind, hid, start, end, moment, label=label)


def a_reel(transition=0.25, style="mix", teaser=True):
    segs = [seg("teaser", "h3", 1000.0, 1003.2, 1003.55)] if teaser else []
    segs += [seg("play", "h1", 100.0, 116.0, 110.0, "PLAY 3"),
             seg("play", "h2", 500.0, 518.0, 511.0, "PLAY 2"),
             seg("play", "h3", 990.0, 1012.0, 1003.55, "NUMBER 1")]
    return montage.Reel(segs, transition=transition, style=style)


# ---------------------------------------------------------------- timing


def test_each_segment_starts_where_the_last_one_starts_to_cross_over():
    r = a_reel()
    assert r.starts() == [0.0, 2.95, 18.7, 36.45]
    assert r.duration == pytest.approx(3.2 + 16 + 18 + 22 - 3 * 0.25)
    last = r.segments[-1]
    assert r.starts()[-1] + last.length == pytest.approx(r.duration)  # the last play ends the reel


def test_no_transition_is_back_to_back():
    r = a_reel(transition=0.0)
    assert r.starts() == [0.0, 3.2, 19.2, 37.2] and r.duration == pytest.approx(59.2)


def test_one_segment_has_no_join():
    r = montage.Reel([seg("play", "h1", 0.0, 70.0, 30.0)])
    assert r.starts() == [0.0] and r.duration == 70.0
    graph = reel_module.video_graph(r, ass=False, fontsdir="")
    assert "xfade" not in graph and graph.endswith("[s0]format=yuv420p[vout]")


def test_the_xfade_offsets_are_the_reels_own_start_times():
    r = a_reel()
    graph = reel_module.video_graph(r, ass=False, fontsdir="")
    offsets = [float(x) for x in re.findall(r"offset=([\d.]+)", graph)]
    assert offsets == pytest.approx(r.starts()[1:])
    assert re.findall(r"duration=([\d.]+)", graph) == ["0.250"] * 3


def test_the_opening_flashes_and_the_plays_follow_the_style():
    names = re.findall(r"transition=(\w+)", reel_module.video_graph(a_reel(), False, ""))
    assert names == [reel_module.OPENING_FLASH, *reel_module.TRANSITIONS[:2]]
    names = re.findall(r"transition=(\w+)", reel_module.video_graph(a_reel(style="dissolve"), False, ""))
    assert names == [reel_module.OPENING_FLASH, "fade", "fade"]
    names = re.findall(r"transition=(\w+)", reel_module.video_graph(a_reel(teaser=False, style="whip"), False, ""))
    assert names == ["smoothleft", "smoothright"]


def test_an_unknown_style_falls_back_to_the_mix():
    names = re.findall(r"transition=(\w+)", reel_module.video_graph(a_reel(style="bogus"), False, ""))
    assert names[1:] == list(reel_module.TRANSITIONS[:2])


def test_the_graphics_are_burnt_in_by_name_after_the_joins():
    graph = reel_module.video_graph(a_reel(), ass=True, fontsdir=":fontsdir=fonts")
    tail = graph.split(";")[-1]
    assert tail == "[x3]subtitles=reel.ass:fontsdir=fonts,format=yuv420p[vout]"
    assert "subtitles" not in reel_module.video_graph(a_reel(), ass=False, fontsdir="")


# ---------------------------------------------------------------- the score bug


def _times(line):
    a, b = line.split(",")[1:3]
    sec = lambda t: (lambda h, m, s: int(h) * 3600 + int(m) * 60 + float(s))(*t.split(":"))  # noqa: E731
    return sec(a), sec(b)


def _bugs(ass):
    return [ln for ln in ass.split("[Events]")[1].splitlines() if ",Bug," in ln]


def test_the_score_changes_shortly_after_the_moment():
    r = a_reel()
    S = scoreboard.Score
    ass = overlays.build(r, {"h2": (S("LAL", 50, "BOS", 52), S("LAL", 53, "BOS", 52))}, teams=("LAKERS", "CELTICS"))
    bugs = [ln for ln in _bugs(ass) if "50" in ln or "53" in ln]
    assert len(bugs) == 2
    (a0, switch), (switch2, b) = _times(bugs[0]), _times(bugs[1])
    i = 2  # h2 is the reel's third segment
    a = r.starts()[i] + r.transition / 2
    moment = a + (511.0 - 500.0)
    assert a0 == pytest.approx(a, abs=0.01) and switch == switch2
    assert switch == pytest.approx(min(r.starts()[i] + 18.0 - r.transition / 2 - 0.3, moment + overlays.SCORE_SWITCH),
                                   abs=0.01)
    assert b == pytest.approx(r.starts()[i] + 18.0 - r.transition / 2, abs=0.01)
    assert "50" in bugs[0] and "53" in bugs[1]


def test_a_moment_at_the_very_end_switches_just_before_the_cut():
    r = montage.Reel([seg("play", "h1", 0.0, 66.0, 65.5)], transition=0.25)
    S = scoreboard.Score
    ass = overlays.build(r, {"h1": (S("A", 1, "B", 1), S("A", 2, "B", 1))})
    (_, switch), _ = [_times(ln) for ln in _bugs(ass)]
    assert switch == pytest.approx(66.0 - 0.3, abs=0.01)


def test_a_score_that_didnt_change_is_one_bug():
    r = a_reel()
    S = scoreboard.Score
    same = S("LAL", 50, "BOS", 52)
    ass = overlays.build(r, {"h2": (same, same)})
    assert len([ln for ln in _bugs(ass) if "50" in ln]) == 1


def test_without_a_score_the_bug_shows_the_teams_and_no_number():
    ass = overlays.build(a_reel(), None, teams=("LAKERS", "CELTICS"))
    bugs = _bugs(ass)
    assert len(bugs) == 3 and all("LAKERS  VS  CELTICS" in ln for ln in bugs)
    assert not any(re.search(r"\d+  \|", ln) for ln in bugs)


# ---------------------------------------------------------------- the shared builder


def test_join_clips_normalises_every_input_first():
    parts, last = fg.join_clips(3, ["fade", "wipeleft"], 0.5, [4.5, 9.0], fps=24)
    assert parts[:3] == [f"[{i}:v]settb=AVTB,fps=24,setsar=1,format=yuv420p[s{i}]" for i in range(3)]
    assert parts[3] == "[s0][s1]xfade=transition=fade:duration=0.500:offset=4.500[x1]"
    assert parts[4] == "[x1][s2]xfade=transition=wipeleft:duration=0.500:offset=9.000[x2]"
    assert last == "x2"


def test_join_clips_falls_back_and_cuts_when_the_join_is_too_short():
    parts, _ = fg.join_clips(3, ["fade"], 0.5, [1.0, 2.0])
    assert "transition=fade" in parts[3] and "transition=fade" in parts[4]  # the fallback when the list runs short
    parts, last = fg.join_clips(3, ["fade", "fade"], 0.01, [1.0, 2.0])
    assert parts[3:] == ["[s0][s1]concat=n=2:v=1:a=0[x1]", "[x1][s2]concat=n=2:v=1:a=0[x2]"]
    parts, _ = fg.join_clips(2, ["fade"], 0.5, [-0.4])
    assert "offset=0.000" in parts[2]  # never a negative offset


def test_join_clips_refuses_what_cant_be_joined():
    with pytest.raises(ValueError):
        fg.join_clips(0, [], 0.5, [])
    with pytest.raises(ValueError, match="need 2 join times"):
        fg.join_clips(3, ["fade", "fade"], 0.5, [1.0])
    parts, last = fg.join_clips(1, [], 0.5, [])
    assert last == "s0" and len(parts) == 1


def test_video_graph_finishes_with_the_look_and_4_2_0():
    graph, out = fg.video_graph(2, ["fade"], 0.4, [3.0], fps=30, finish=["eq=contrast=1.05", *fg.burn_in(["a.ass"])],
                                out="v")
    assert out == "v" and graph.endswith("[x1]eq=contrast=1.05,subtitles=a.ass,format=yuv420p[v]")
    assert fg.burn_in(["a.ass", "b.ass"], ":fontsdir=fonts") == ["subtitles=a.ass:fontsdir=fonts",
                                                                 "subtitles=b.ass:fontsdir=fonts"]


# ---------------------------------------------------------------- one real join


def _clip(path, colour, seconds, size="96x160"):
    subprocess.run([ffmpeg.ffmpeg_exe(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c={colour}:s={size}:r=30:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(path)], check=True, timeout=60)


def test_a_real_join_lasts_what_the_reel_says(tmp_path):
    segs = [seg("play", f"h{i}", 0.0, length, length / 2) for i, length in enumerate((1.5, 1.0, 1.2))]
    r = montage.Reel(segs, transition=0.25, style="dissolve")
    clips = []
    for i, (s, colour) in enumerate(zip(segs, ("red", "green", "blue"))):
        clips.append(tmp_path / f"{i}.mp4")
        _clip(clips[-1], colour, s.length)
    out = tmp_path / "reel.mp4"
    cmd = [ffmpeg.ffmpeg_exe(), "-hide_banner", "-v", "error", "-y"]
    for c in clips:
        cmd += ["-i", str(c)]
    cmd += ["-filter_complex", reel_module.video_graph(r, ass=False, fontsdir=""), "-map", "[vout]",
            *ffmpeg.video_args("libx264", 23, "delivery", 30), str(out)]
    subprocess.run(cmd, check=True, timeout=120, capture_output=True)
    info = ffmpeg.probe(out)
    assert info.duration == pytest.approx(r.duration, abs=0.07)  # 3.7 - 2 x 0.25 = 3.2 s
    assert ffmpeg.delivery_problems(info, 96, 160, 30.0) == []
    assert (info.codec, info.profile.lower(), info.pix_fmt) == ("h264", "high", "yuv420p")


def test_delivery_problems_say_what_differs():
    info = ffmpeg.MediaInfo(duration=70, width=720, height=1280, fps=25.0, codec="hevc", profile="Main",
                            pix_fmt="yuv420p10le")
    problems = ffmpeg.delivery_problems(info)
    assert len(problems) == 5 and "hevc" in problems[0] and "720x1280" in problems[3] and "25 fps" in problems[4]
    assert ffmpeg.delivery_problems(ffmpeg.MediaInfo()) == []  # what ffmpeg didn't print isn't held against it

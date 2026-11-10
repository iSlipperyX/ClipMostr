"""The timeline compiler (editor/timeline_compiler.py) against real ffmpeg renders: cuts land on their frame,
transitions straddle the cut, keyframed moves put pixels where the numbers say, overlays sit where they're
placed, and the whole thing comes out of one ffmpeg run with sound at the level asked for.

Small canvases and low frame rates keep each render to a second or two."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from ytclip.editor import motion_engine as M
from ytclip.editor import timeline_compiler as C
from ytclip.ffmpeg import ffmpeg_exe, probe
from ytclip.studio import timeline_schema as T

W, H, FPS = 180, 320, 10


def canvas():
    return T.Canvas(W, H, FPS)


def frames(path: Path, w=W, h=H) -> np.ndarray:
    """Every frame of a video as RGB: [n, h, w, 3]."""
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)


def picture(path: Path, array: np.ndarray) -> Path:
    import cv2

    cv2.imwrite(str(path), array[:, :, ::-1])
    return path


def flat(colour, w=W, h=H) -> np.ndarray:
    return np.full((h, w, 3), colour, np.uint8)


def render(tl, tmp_path, name="out.mp4"):
    return C.render(tl, tmp_path / name, base=tmp_path, work=tmp_path / "work")


def near(pixel, colour, slack=30) -> bool:
    return all(abs(int(a) - int(b)) <= slack for a, b in zip(pixel, colour))


RED, BLUE, GREEN, WHITE, BLACK = (235, 20, 20), (20, 20, 235), (20, 235, 20), (235, 235, 235), (16, 16, 16)


def colours(tl, **named):
    for key, colour in named.items():
        tl.assets[key] = T.Asset("color", color="#%02X%02X%02X" % colour)


# ---------------------------------------------------------------- the picture track

def test_a_cut_lands_on_its_frame(tmp_path):
    tl = T.standard("cut", canvas(), 3.0)
    colours(tl, red=RED, blue=BLUE)
    tl.track(T.VIDEO_BASE).clips += [T.VideoClip("a", "red", 0, 1.3), T.VideoClip("b", "blue", 1.3, 1.7)]
    report = render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert len(f) == 30 and report.duration == 3.0
    assert near(f[12, 100, 90], RED) and near(f[13, 100, 90], BLUE)  # frame 13 starts at 1.3 s
    assert report.how == {"a": "fill", "b": "fill"}


def test_a_transition_straddles_the_cut_and_keeps_the_length(tmp_path):
    tl = T.standard("fade", canvas(), 4.0)
    colours(tl, red=RED, blue=BLUE)
    tl.track(T.VIDEO_BASE).clips += [
        T.VideoClip("a", "red", 0, 2.0),
        T.VideoClip("b", "blue", 2.0, 2.0, transition_in=T.Transition("crossfade", 0.6))]
    render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert len(f) == 40  # the transition doesn't shorten the video: both clips reach past the cut
    assert near(f[16, 10, 10], RED) and near(f[23, 10, 10], BLUE)  # untouched either side of 1.7-2.3 s
    mid = f[20, 10, 10]  # the cut itself: half of each
    assert 90 < mid[0] < 170 and 90 < mid[2] < 170


def test_a_gap_shows_the_background_and_a_last_fade_goes_to_black(tmp_path):
    tl = T.standard("gap", T.Canvas(W, H, FPS, "#00EB00"), 4.0)
    colours(tl, red=RED)
    tl.track(T.VIDEO_BASE).clips += [
        T.VideoClip("a", "red", 1.0, 1.0),
        T.VideoClip("b", "red", 3.0, 1.0, transition_out=T.Transition("dip_black", 0.5))]
    render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert near(f[5, 50, 50], (0, 235, 0), 40)  # before the first clip
    assert near(f[15, 50, 50], RED) and near(f[25, 50, 50], (0, 235, 0), 40)
    assert near(f[32, 50, 50], RED) and f[39, 50, 50].max() < 60  # faded out at the very end


def square_picture(tmp_path, size=60):
    """A white square in the middle of a black picture the canvas's shape, three times its size."""
    img = flat(BLACK, W * 3, H * 3)
    cx, cy = W * 3 // 2, H * 3 // 2
    img[cy - size * 3 // 2:cy + size * 3 // 2, cx - size * 3 // 2:cx + size * 3 // 2] = WHITE
    return picture(tmp_path / "square.png", img)


def width_of_white(frame) -> int:
    row = frame[H // 2]
    return int((row.min(axis=1) > 128).sum())


def test_a_zoom_starts_and_ends_at_its_keyframes(tmp_path):
    square_picture(tmp_path)
    tl = T.standard("zoom", canvas(), 2.0)
    tl.assets["sq"] = T.Asset("image", "square.png")
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("z", "sq", 0, 2.0, keyframes=M.zoom(2.0, 1.0, 2.0)))
    report = render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert report.how == {"z": "zoompan"}
    assert abs(width_of_white(f[0]) - 60) <= 2  # scale 1 on the first frame
    assert abs(width_of_white(f[10]) - 90) <= 3  # half way: 1.5
    assert abs(width_of_white(f[19]) - 117) <= 4  # the last frame is 1.9 s in: 1.95


def test_a_zoom_keeps_its_timing_behind_a_transition(tmp_path):
    """A clip brought in by a transition starts early; its keyframes still count from its own start."""
    square_picture(tmp_path)
    tl = T.standard("zoom2", canvas(), 3.0)
    colours(tl, red=RED)
    tl.assets["sq"] = T.Asset("image", "square.png")
    tl.track(T.VIDEO_BASE).clips += [
        T.VideoClip("a", "red", 0, 1.0),
        T.VideoClip("z", "sq", 1.0, 2.0, keyframes=M.zoom(2.0, 1.0, 2.0), transition_in=T.Transition("crossfade", 0.4))]
    render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert abs(width_of_white(f[20]) - 90) <= 3  # 1 s into the clip, 2 s into the video: 1.5


def halves_picture(tmp_path):
    """Twice as wide as the canvas's shape: red on the left half, blue on the right."""
    img = flat(RED, W * 4, H * 2)
    img[:, W * 2:] = BLUE
    return picture(tmp_path / "halves.png", img)


def test_a_pan_slides_over_the_whole_source_and_stops_at_its_edge(tmp_path):
    halves_picture(tmp_path)
    tl = T.standard("pan", canvas(), 2.0)
    tl.assets["wide"] = T.Asset("image", "halves.png")
    # The picture's centre goes from far right of the canvas to far left: its left half shows, then its right.
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("p", "wide", 0, 2.0, keyframes=[
        T.Keyframe(0, x=1.5), T.Keyframe(2.0, x=-0.5)]))
    report = render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert report.how == {"p": "crop"}
    assert near(f[0, 160, 90], RED) and near(f[19, 160, 90], BLUE)
    assert near(f[0, 160, 5], RED) and near(f[0, 160, 175], RED)  # stopped at the edge: no background showing
    middle = f[10, 160]  # half way the seam is in the middle of the frame
    assert near(middle[20], RED) and near(middle[160], BLUE)


def test_a_small_or_see_through_clip_is_placed_exactly(tmp_path):
    tl = T.standard("place", canvas(), 2.0)
    colours(tl, blue=BLUE, red=RED)
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("bg", "blue", 0, 2.0))
    # A red box a quarter of the canvas each way, centred a quarter in from the top left, fading in over 1 s.
    tl.track(T.VIDEO_OVERLAY).clips.append(T.VideoClip("box", "red", 0.5, 1.0, keyframes=[
        T.Keyframe(0, 0.25, 0.25, 0.25, 0, 0.0), T.Keyframe(1.0, 0.25, 0.25, 0.25, 0, 1.0)]))
    report = render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert report.how["box"] == "overlay"
    cy, cx = H // 4, W // 4
    assert near(f[3, cy, cx], BLUE)  # not on yet
    half = f[10, cy, cx]  # half way through its fade
    assert 80 < half[0] < 170 and 80 < half[2] < 170
    assert f[14, cy, cx][0] > 180 and f[14, cy, cx][2] < 90  # nearly opaque
    assert near(f[14, cy, cx + 30], BLUE) and near(f[14, cy + 50, cx], BLUE)  # the box is 45 x 80
    assert near(f[14, cy, cx + 17], RED, 60) and near(f[14, cy + 34, cx], RED, 60)
    assert near(f[16, cy, cx], BLUE)  # gone after its second


def test_a_layer_moves_and_scales_by_its_keyframes(tmp_path):
    tl = T.standard("move", canvas(), 2.0)
    colours(tl, blue=BLUE, red=RED)
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("bg", "blue", 0, 2.0))
    tl.track(T.VIDEO_OVERLAY).clips.append(T.VideoClip("box", "red", 0, 2.0, keyframes=[
        T.Keyframe(0, 0.25, 0.5, 0.2), T.Keyframe(2.0, 0.75, 0.5, 0.4)]))
    render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")

    def box(frame):
        red = (frame[:, :, 0] > 150) & (frame[:, :, 2] < 100)
        ys, xs = np.nonzero(red)
        return xs.mean(), ys.mean(), xs.max() - xs.min() + 1

    x0, y0, w0 = box(f[0])
    x1, y1, w1 = box(f[19])
    assert abs(x0 - 0.25 * W) < 3 and abs(y0 - H / 2) < 3 and abs(w0 - 0.2 * W) <= 3
    assert abs(x1 - (0.25 + 0.5 * 1.9 / 2) * W) < 4 and abs(w1 - (0.2 + 0.2 * 1.9 / 2) * W) <= 4


def test_a_turned_clip_goes_by_overlay_and_a_slight_roll_stays_fast(tmp_path):
    square_picture(tmp_path)
    tl = T.standard("roll", canvas(), 1.0)
    tl.assets["sq"] = T.Asset("image", "square.png")
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("r", "sq", 0, 1.0, keyframes=M.ken_burns(
        "slow_push_in", 1.0, size=(W, H))))
    compiled = C.compile_timeline(tl, tmp_path)
    assert compiled.how == {"r": "zoompan"} and "rotate=" in compiled.graph
    tl.track(T.VIDEO_BASE).clips[0].keyframes = [T.Keyframe(0, rotation=0), T.Keyframe(1.0, rotation=45)]
    assert C.compile_timeline(tl, tmp_path).how == {"r": "overlay"}
    render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert width_of_white(f[0]) in range(57, 64)
    assert width_of_white(f[9]) > 70  # turned towards its diagonal, the square is wider across the middle


def test_a_video_clip_is_cut_from_its_source_and_holds_its_last_frame(tmp_path):
    # A source that's red for its first second, then blue.
    src = tmp_path / "src.mp4"
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=red:s={W}x{H}:r={FPS}:d=1",
                    "-f", "lavfi", "-i", f"color=c=blue:s={W}x{H}:r={FPS}:d=1", "-filter_complex",
                    "[0:v][1:v]concat=n=2:v=1:a=0", "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(src)],
                   check=True)
    tl = T.standard("video", canvas(), 3.0)
    tl.assets["src"] = T.Asset("video", "src.mp4", duration=2.0)
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("v", "src", 0, 3.0, source_in=0.5))
    report = render(tl, tmp_path)
    assert any("only 2.00 s long" in w for w in report.warnings)
    f = frames(tmp_path / "out.mp4")
    assert f[2, 50, 50][0] > 150  # red until the source's 1 s mark, 0.5 s in
    assert f[7, 50, 50][2] > 150 and f[29, 50, 50][2] > 150  # then blue, held to the end


# ---------------------------------------------------------------- the graph itself

def simple(tmp_path, n=3):
    tl = T.standard("graph", canvas(), float(n))
    for i in range(n):
        picture(tmp_path / f"p{i}.png", flat((40 * i, 80, 200 - 40 * i)))
        tl.assets[f"p{i}"] = T.Asset("image", f"p{i}.png")
        tl.track(T.VIDEO_BASE).clips.append(T.VideoClip(
            f"c{i}", f"p{i}", float(i), 1.0, keyframes=M.zoom(1.0),
            transition_in=T.Transition("whip_left" if i == 1 else "crossfade", 0.2)))
    return tl


def test_one_graph_one_output_no_intermediate_files(tmp_path):
    tl = simple(tmp_path)
    compiled = C.compile_timeline(tl, tmp_path, work=tmp_path / "work")
    cmd = C.command(compiled, tmp_path / "out.mp4")
    assert cmd.count("-i") == len(compiled.inputs) == 0  # stills are read inside the graph, one by one
    assert all(f"movie='../p{i}.png'" in compiled.graph for i in range(3)) and not compiled.staged
    assert sum(1 for a in cmd if a.endswith(".mp4")) == 1  # the only video file named is the result
    assert "xfade=transition=slideleft" in compiled.graph and "xfade=transition=fade" in compiled.graph
    assert compiled.graph.count("zoompan=") == 3
    assert compiled.graph.rstrip().endswith("[aout]") and "[vout]" in compiled.graph
    render(tl, tmp_path)
    made = sorted(p.name for p in (tmp_path / "work").iterdir())
    assert made == ["graph.txt"]  # the graph is all that's written on the way (no captions on this one)
    info = probe(tmp_path / "out.mp4")
    assert (info.width, info.height) == (W, H) and info.has_audio and abs(info.duration - 3.0) < 0.15


def test_awkward_file_names_are_copied_under_plain_ones(tmp_path):
    """A filter graph gives : ' , ; [ ] % and \\ meanings of their own. Stills named with them are copied into the
    work folder under plain names, so the graph never has to spell them."""
    odd = tmp_path / "it's 50% off; [new], 12:30"
    odd.mkdir()
    tl = T.standard("names", canvas(), 2.0)
    picture(odd / "a b's, [1]; 100%.png", flat(RED))
    picture(tmp_path / "plain (2)+b.png", flat(BLUE))
    tl.assets["odd"] = T.Asset("image", str(odd / "a b's, [1]; 100%.png"))
    tl.assets["plain"] = T.Asset("image", "plain (2)+b.png")
    tl.track(T.VIDEO_BASE).clips += [T.VideoClip("a", "odd", 0, 1.0, keyframes=M.zoom(1.0)),
                                     T.VideoClip("b", "plain", 1.0, 1.0),
                                     T.VideoClip("c", "odd", 1.5, 0.5)]
    tl.track(T.VIDEO_BASE).clips[1].duration = 0.5
    compiled = C.compile_timeline(tl, tmp_path, work=tmp_path / "work")
    assert list(compiled.staged) == ["stills/001.png"]  # once, though two clips show it
    assert compiled.graph.count("movie='stills/001.png'") == 2 and "movie='../plain (2)+b.png'" in compiled.graph
    render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert len(f) == 20 and near(f[3, 9, 9], RED) and near(f[12, 9, 9], BLUE) and near(f[18, 9, 9], RED)
    # Without a work folder the full path goes in the graph, escaped for the option and quoted for the graph.
    assert C.graph_quote("C:/a b/it's.png") == r"""'C\:/a b/it\'\''s.png'"""
    full = C.compile_timeline(tl, tmp_path)
    assert not full.staged
    script = tmp_path / "full.txt"
    script.write_text(full.graph, encoding="utf-8")
    cmd = C.command(full, tmp_path / "full.mp4", fps=FPS, script=str(script))
    assert subprocess.run(cmd, capture_output=True).returncode == 0
    assert len(frames(tmp_path / "full.mp4")) == 20


def test_transitions_never_eat_a_short_clip(tmp_path):
    tl = T.standard("short", canvas(), 2.4)
    colours(tl, red=RED, blue=BLUE, green=GREEN)
    tl.track(T.VIDEO_BASE).clips += [
        T.VideoClip("a", "red", 0, 1.0),
        T.VideoClip("b", "green", 1.0, 0.4, transition_in=T.Transition("crossfade", 2.0)),
        T.VideoClip("c", "blue", 1.4, 1.0, transition_in=T.Transition("crossfade", 2.0))]
    render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert len(f) == 24
    assert f[12, 9, 9][1] > 150  # the short clip still has a frame of its own, between its two transitions


def test_a_broken_timeline_says_what_is_wrong(tmp_path):
    tl = simple(tmp_path)
    tl.assets["p1"].path = "missing.png"
    with pytest.raises(T.TimelineError, match="assets.p1.path: the file isn't there"):
        C.compile_timeline(tl, tmp_path)


def test_the_graph_file_option_matches_the_ffmpeg(monkeypatch):
    monkeypatch.setattr(C, "_ffmpeg_major", lambda exe: 6)
    assert C.graph_args("g.txt") == ["-filter_complex_script", "g.txt"]
    monkeypatch.setattr(C, "_ffmpeg_major", lambda exe: 7)
    assert C.graph_args("g.txt") == ["-/filter_complex", "g.txt"]


def test_a_failing_graphics_encoder_falls_back_to_the_processor(tmp_path, monkeypatch):
    tl = simple(tmp_path, 1)
    monkeypatch.setattr(C, "pick_encoder", lambda pref: "h264_nvenc")
    real = C.video_args
    monkeypatch.setattr(C, "video_args", lambda enc, *a, **k: ["-c:v", "no_such_encoder"] if enc == "h264_nvenc"
                        else real(enc, *a, **k))
    said = []
    report = C.render(tl, tmp_path / "out.mp4", base=tmp_path, work=tmp_path / "work", log=said.append)
    assert report.encoder == "libx264" and (tmp_path / "out.mp4").stat().st_size > 1000
    assert "encoded on the processor" in said[0]

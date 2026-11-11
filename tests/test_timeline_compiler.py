"""The timeline compiler (editor/timeline_compiler.py) against real ffmpeg renders: cuts land on their frame,
transitions straddle the cut, keyframed moves put pixels where the numbers say, overlays sit where they're
placed, and the whole thing comes out of one ffmpeg run with sound at the level asked for.

Small canvases and low frame rates keep each render to a second or two."""

import subprocess
import sys
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


def test_the_picture_track_ends_on_a_count_of_frames_with_one_to_spare(tmp_path):
    """ffmpeg 7.1, the one the Windows build carries, has its fps filter drop a stream's last frame when the
    filter before it (a transition, an overlay) ends without saying how long that frame lasts: every video
    with a transition came out one frame short there. (The length tests around this one show it on that
    ffmpeg; this says how the graph guards against it on any.)"""
    tl = T.standard("fade", canvas(), 4.0)
    colours(tl, red=RED, blue=BLUE)
    tl.track(T.VIDEO_BASE).clips += [
        T.VideoClip("a", "red", 0, 2.0, transition_in=T.Transition("dip_black", 0.3)),
        T.VideoClip("b", "blue", 2.0, 2.0, transition_in=T.Transition("crossfade", 0.6),
                    transition_out=T.Transition("dip_black", 0.3))]
    graph = C.compile_timeline(tl, tmp_path).graph
    assert f",tpad=stop=1:stop_mode=clone,fps={FPS},trim=end_frame=40[" in graph
    render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert len(f) == 40 and near(f[30, 100, 90], BLUE)
    assert int(f[39, 100, 90, 2]) < 120  # the last frame is the fade's last step, not a spare one


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
    # (Windows doesn't allow a colon in a folder's name; the drive's own colon is in the full path below.)
    odd = tmp_path / ("it's 50% off; [new], 12.30" if sys.platform == "win32" else "it's 50% off; [new], 12:30")
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


# ---------------------------------------------------------------- what the review of the Studio found

def test_clips_that_meet_between_two_frames_leave_no_gap():
    """0.1 + 4.05 is a hair under 4.15, and 4.15 is frame 124.5 at 30 fps: both must land on the same frame, or
    a one-frame gap opens between the clips and the transition there is lost."""
    assert C.snap(0.1 + 4.05, 30) == C.snap(4.15, 30)
    tl = T.standard("join", T.Canvas(W, H, 30), 8.0)
    colours(tl, red=RED, blue=BLUE)
    tl.track(T.VIDEO_BASE).clips += [T.VideoClip("a", "red", 0.1, 4.05),
                                     T.VideoClip("b", "blue", 4.15, 3.85, transition_in=T.Transition("crossfade", 0.4))]
    segs = C._Builder(tl, None, None).segments(tl.track(T.VIDEO_BASE))
    assert [s.clip.id if s.clip else None for s in segs] == [None, "a", "b"]  # only the gap before the first
    assert segs[2].lead > 0  # the crossfade is still there
    # Over many joins written to the millisecond (as the Studio writes them), never a mismatch.
    for fps in (24, 25, 30, 60):
        for i in range(1, 4000):
            a, d = round(i * 0.0137, 3), round(1 + (i % 97) * 0.0513, 3)
            assert C.snap(a + d, fps) == C.snap(round(a + d, 3), fps)


def test_a_gap_is_exactly_its_frames_long(tmp_path):
    """A two-frame gap written d=0.0667 gave three frames, and every cut after it came a frame late."""
    tl = T.standard("gap", T.Canvas(W, H, 30, background="#14EB14"), 4.0)
    colours(tl, red=RED, blue=BLUE)
    tl.track(T.VIDEO_BASE).clips += [T.VideoClip("a", "red", 0.0, 1.0), T.VideoClip("b", "blue", 32 / 30, 1.0),
                                     T.VideoClip("c", "red", 62 / 30, 4.0 - 62 / 30)]
    render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert len(f) == 120
    assert near(f[29, 100, 90], RED) and near(f[30, 100, 90], GREEN) and near(f[31, 100, 90], GREEN)
    assert near(f[32, 100, 90], BLUE) and near(f[61, 100, 90], BLUE) and near(f[62, 100, 90], RED)


def beep(path: Path, seconds: float = 0.2) -> Path:
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f=880:r=48000:d={seconds}",
                    "-ac", "2", str(path)], check=True)
    return path


def sound(path: Path) -> np.ndarray:
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f",
                          "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def test_a_sound_used_again_and_again_is_one_input(tmp_path):
    """Fifty whooshes were fifty inputs on the command line; they're one, read as often as needed."""
    tl = simple(tmp_path, 4)
    beep(tmp_path / "beep.wav")
    tl.assets["beep"] = T.Asset("audio", "beep.wav", duration=0.2)
    tl.track(T.AUDIO_SFX).clips += [T.AudioClip(f"b{i}", "beep", t, 0.2) for i, t in enumerate((0.5, 1.5, 2.5, 3.5))]
    compiled = C.compile_timeline(tl, tmp_path, work=tmp_path / "work")
    assert len(compiled.audio_inputs) == 1 and len(compiled.sources) == 4
    assert {s.label for s in compiled.sources} == {"0:a"}
    render(tl, tmp_path)
    x = sound(tmp_path / "out.mp4")
    rms = lambda a, b: float(np.sqrt(np.mean(x[int(a * 8000):int(b * 8000)] ** 2)))  # noqa: E731
    for t in (0.5, 1.5, 2.5, 3.5):  # every one of them plays, each at its own time
        assert rms(t + 0.03, t + 0.17) > 10 * rms(t - 0.3, t - 0.05) + 0.01
    assert abs(len(x) / 8000 - 4.0) < 0.1  # and the sound runs the whole length


def seconds_of_sound(path: Path) -> float:
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f",
                          "f32le", "-"], capture_output=True, check=True).stdout
    return len(raw) / 4 / 8000


def test_a_repeated_sound_never_ends_the_mix_early(tmp_path):
    """Several reads of one input reach their mix together, the mix lost its clock when the first of them
    ended, and the bus (padded from that clock) stopped at its last clip: with music placed three times and no
    voice, a 12-second video had six seconds of sound and no effects after them."""
    tl = simple(tmp_path, 12)
    beep(tmp_path / "bed.wav", 2.0)
    beep(tmp_path / "beep.wav")
    tl.assets["bed"] = T.Asset("audio", "bed.wav", duration=2.0)
    tl.assets["beep"] = T.Asset("audio", "beep.wav", duration=0.2)
    tl.track(T.AUDIO_MUSIC).clips += [T.AudioClip(f"m{i}", "bed", 2.0 * i, 2.0, gain_db=-20) for i in range(3)]
    tl.track(T.AUDIO_SFX).clips += [T.AudioClip("late", "beep", 10.0, 0.2)]
    render(tl, tmp_path)
    assert abs(seconds_of_sound(tmp_path / "out.mp4") - 12.0) < 0.1
    x = sound(tmp_path / "out.mp4")
    rms = lambda a, b: float(np.sqrt(np.mean(x[int(a * 8000):int(b * 8000)] ** 2)))  # noqa: E731
    assert rms(10.03, 10.17) > 10 * rms(9.0, 9.9) + 0.01  # the late effect is there

    # The same when it's the voice (the first layer of the mix) that repeats a cut.
    spoken = simple(tmp_path, 12)
    spoken.assets["say"] = T.Asset("audio", "bed.wav", duration=2.0)
    spoken.assets["beep"] = T.Asset("audio", "beep.wav", duration=0.2)
    spoken.track(T.AUDIO_VOICE).clips += [T.AudioClip("v0", "say", 0.0, 2.0), T.AudioClip("v1", "say", 4.0, 2.0)]
    spoken.track(T.AUDIO_SFX).clips += [T.AudioClip("late", "beep", 10.0, 0.2)]
    render(spoken, tmp_path, "spoken.mp4")
    assert abs(seconds_of_sound(tmp_path / "spoken.mp4") - 12.0) < 0.1


def test_a_first_picture_one_frame_long_is_kept(tmp_path):
    """Joining streams end to end takes each one's length from the spacing of its frames, so a first stream
    of a single frame counted as no length at all: the frame was dropped and every cut came a frame early."""
    tl = T.standard("one", T.Canvas(W, H, 30, background="#14EB14"), 61 / 30)
    colours(tl, red=RED, blue=BLUE)
    tl.track(T.VIDEO_BASE).clips += [T.VideoClip("a", "red", 1 / 30, 1.0), T.VideoClip("b", "blue", 31 / 30, 1.0)]
    render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert len(f) == 61
    assert near(f[0, 100, 90], GREEN) and near(f[1, 100, 90], RED) and near(f[30, 100, 90], RED)
    assert near(f[31, 100, 90], BLUE) and near(f[60, 100, 90], BLUE)
    # A first clip of one frame, the same.
    tl = T.standard("one", T.Canvas(W, H, 30), 31 / 30)
    colours(tl, red=RED, blue=BLUE)
    tl.track(T.VIDEO_BASE).clips += [T.VideoClip("a", "red", 0.0, 1 / 30), T.VideoClip("b", "blue", 1 / 30, 1.0)]
    render(tl, tmp_path, "clip.mp4")
    f = frames(tmp_path / "clip.mp4")
    assert len(f) == 31 and near(f[0, 100, 90], RED) and near(f[1, 100, 90], BLUE)
    # With a transition later on: the join that takes the spare frame off must hand on a frame rate and a
    # time base like every other stream, or ffmpeg 7 refuses the transition after it and nothing renders.
    for name, first in (("gap", []), ("clip", [T.VideoClip("z", "blue", 0.0, 1 / 30)])):
        tl = T.standard("one", T.Canvas(W, H, 30, background="#14EB14"), 91 / 30)
        colours(tl, red=RED, blue=BLUE)
        tl.track(T.VIDEO_BASE).clips += [*first, T.VideoClip("a", "red", 1 / 30, 1.0), T.VideoClip("b", "blue", 31 / 30, 1.0),
                                         T.VideoClip("c", "red", 61 / 30, 1.0, transition_in=T.Transition("crossfade", 0.4))]
        render(tl, tmp_path, f"then-{name}.mp4")
        f = frames(tmp_path / f"then-{name}.mp4")
        assert len(f) == 91 and near(f[0, 100, 90], BLUE if first else GREEN) and near(f[1, 100, 90], RED)
        assert near(f[31, 100, 90], BLUE) and near(f[50, 100, 90], BLUE) and near(f[75, 100, 90], RED)
        assert not near(f[61, 100, 90], BLUE) and not near(f[61, 100, 90], RED)  # mid-crossfade at the cut


def test_a_transition_starts_on_its_own_frame_whatever_the_time_rounds_to(tmp_path):
    """A transition that starts on frame 26 of a 30 fps video starts at 0.86667 s. Written to four places that
    is 0.8667, just after the frame, so the transition began a frame late, and so did the clip after it and
    every cut after that: one transition in three, a frame each, on every ffmpeg."""
    tl = T.standard("late", T.Canvas(W, H, 30), 4.0)
    colours(tl, red=RED, blue=BLUE, white=WHITE)
    tl.track(T.VIDEO_BASE).clips += [
        T.VideoClip("a", "red", 0.0, 1.0),
        T.VideoClip("b", "blue", 1.0, 1.0, transition_in=T.Transition("crossfade", 0.3)),  # frames 26 to 33
        T.VideoClip("c", "white", 2.0, 1.0),
        T.VideoClip("d", "red", 3.0, 1.0, transition_in=T.Transition("crossfade", 0.3))]  # frames 86 to 93
    render(tl, tmp_path)
    f = frames(tmp_path / "out.mp4")
    assert len(f) == 120
    assert near(f[25, 100, 90], RED) and not near(f[27, 100, 90], RED) and near(f[34, 100, 90], BLUE)
    assert near(f[59, 100, 90], BLUE) and near(f[60, 100, 90], WHITE)  # the cut after it, on its frame
    assert near(f[85, 100, 90], WHITE) and not near(f[87, 100, 90], WHITE) and near(f[94, 100, 90], RED)
    assert near(f[119, 100, 90], RED)


def test_after_a_first_picture_of_one_frame_a_placed_clip_keeps_its_last_frame(tmp_path):
    """ffmpeg 7.1 (the Windows and Mac builds' own): a clip laid over the background (fitted inside the
    frame, turned, see-through) lost its last frame after a first picture of one frame, and a first picture
    of one frame that is itself placed was lost. Every cut after it came a frame early."""
    sq = picture(tmp_path / "wide.png", flat(RED, 320, 180))  # a wide picture, fitted inside the upright frame
    for name, first in (("gap", []), ("placed", [T.VideoClip("z", "wide", 0.0, 1 / 30, fit="contain")])):
        tl = T.standard("fit", T.Canvas(W, H, 30, background="#14EB14"), 61 / 30)
        colours(tl, blue=BLUE)
        tl.assets["wide"] = T.Asset("image", sq.name)
        tl.track(T.VIDEO_BASE).clips += [*first, T.VideoClip("a", "wide", 1 / 30, 1.0, fit="contain"),
                                         T.VideoClip("b", "blue", 31 / 30, 1.0)]
        render(tl, tmp_path, f"fit-{name}.mp4")
        f = frames(tmp_path / f"fit-{name}.mp4")
        assert len(f) == 61, name
        assert near(f[0, 160, 90], RED) == bool(first), name  # the frame's middle: the picture, or nothing yet
        assert near(f[1, 160, 90], RED) and near(f[30, 160, 90], RED), name
        assert near(f[31, 160, 90], BLUE) and near(f[60, 160, 90], BLUE), name


def test_inputs_are_named_from_the_work_folder_when_that_is_shorter(tmp_path):
    work = tmp_path / "work"
    clip = tmp_path / "pkg" / "scenes" / "scene_001_pexels_123.mp4"
    assert C.input_path(clip, work) == str(Path("..") / "pkg" / "scenes" / "scene_001_pexels_123.mp4")
    assert C.input_path(clip, None) == str(clip.resolve())
    assert C.input_path(work / "-odd.mp4", work) == str((work / "-odd.mp4").resolve())  # never read as an option


def test_a_relative_input_counts_up_the_way_the_system_will(tmp_path, monkeypatch):
    """Windows takes ".." off the text of the folder ffmpeg runs in; macOS and Linux follow the real
    folders. With the video's folder reached through a link (a subst drive, a junction), a name worked out
    from the real folders pointed nowhere on Windows."""
    import os

    real = tmp_path / "real" / "long" / "path" / "videos"
    (real / "w").mkdir(parents=True)
    clip = tmp_path / "real" / "long" / "path" / "broll" / "a clip with a long enough name.mp4"
    link = tmp_path / "s"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("no links here")
    work = link / "w"
    rel = C.input_path(clip, work)  # here: through the real folders
    assert not os.path.isabs(rel) and os.path.realpath(os.path.join(work, rel)) == os.path.realpath(clip)
    monkeypatch.setattr(C.sys, "platform", "win32")  # there: by the text of the path
    rel = C.input_path(clip, work)
    assert os.path.normpath(os.path.join(work, rel)) == str(clip)
    assert C.relative_to_work(clip, work) == os.path.relpath(clip, work)  # the link is left as written


def test_a_command_too_long_for_windows_is_refused_in_plain_words(tmp_path, monkeypatch):
    from ytclip import ffmpeg

    assert ffmpeg.command_too_long(["ffmpeg", "x" * 33000], windows=True)
    assert not ffmpeg.command_too_long(["ffmpeg", "x" * 33000], windows=False)
    assert not ffmpeg.command_too_long(["ffmpeg", "-i", "a.mp4"], windows=True)
    tl = simple(tmp_path, 1)
    monkeypatch.setattr(C, "command_too_long", lambda cmd: True)
    with pytest.raises(C.CompileError, match="more than Windows can start ffmpeg with"):
        render(tl, tmp_path)
    assert not (tmp_path / "out.mp4").exists()


def test_ffmpeg_that_cannot_start_is_a_plain_error(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise OSError(206, "The filename or extension is too long")

    monkeypatch.setattr(C, "run_with_progress", refuse)
    with pytest.raises(C.CompileError, match="couldn't be started"):
        render(simple(tmp_path, 1), tmp_path)


def test_the_loudness_passes_send_their_graph_in_a_file(tmp_path, monkeypatch):
    from ytclip.editor import audio_mixer as A

    seen = {}
    real = subprocess.run

    def spy(cmd, **kw):
        if "-f" in cmd and "null" in cmd:
            seen["cmd"] = list(cmd)
            option = next(a for a in cmd if a in ("-/filter_complex", "-filter_complex_script"))
            seen["graph"] = Path(cmd[cmd.index(option) + 1]).read_text(encoding="utf-8")
        return real(cmd, **kw)

    monkeypatch.setattr(A.subprocess, "run", spy)
    beep(tmp_path / "beep.wav", 1.0)
    got = A.measure(["-i", str(tmp_path / "beep.wav")], "[0:a]loudnorm=print_format=json[aout]", 1.0, tmp_path)
    assert got and "input_i" in got
    assert "-filter_complex" not in seen["cmd"] and "loudnorm" in seen["graph"]
    assert not list(tmp_path.glob("measure-*.txt"))  # and the file is gone afterwards


def test_a_mix_that_could_not_be_measured_is_said(tmp_path, monkeypatch):
    tl = simple(tmp_path, 1)
    beep(tmp_path / "beep.wav")
    tl.assets["beep"] = T.Asset("audio", "beep.wav", duration=0.2)
    tl.track(T.AUDIO_SFX).clips.append(T.AudioClip("b", "beep", 0.2, 0.2))
    monkeypatch.setattr(C, "measure_sound", lambda *a, **k: (0.0, None))
    assert any("couldn't be measured" in w for w in render(tl, tmp_path).warnings)


def test_a_silent_video_sent_to_a_sound_bus_is_named(tmp_path):
    """Stock clips often have no sound: asking for it gave ffmpeg's own dump, naming neither clip nor cause."""
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=s={W}x{H}:r={FPS}:d=2",
                    "-an", "-pix_fmt", "yuv420p", str(tmp_path / "silent.mp4")], check=True)
    tl = T.standard("silent", canvas(), 2.0)
    tl.assets["clip"] = T.Asset("video", "silent.mp4", duration=2.0)
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("stock", "clip", 0.0, 2.0, audio="sfx"))
    with pytest.raises(C.CompileError, match="Clip stock: silent.mp4 has no sound of its own"):
        render(tl, tmp_path)
    tl.track(T.VIDEO_BASE).clips[0].audio = ""
    assert render(tl, tmp_path).duration == 2.0


def test_what_reading_the_file_passed_over_is_in_the_render_report(tmp_path):
    tl = simple(tmp_path, 2)
    data = tl.to_json()
    clip = data["tracks"][0]["clips"][1]
    clip["transiton_in"] = clip.pop("transition_in")
    again = T.Timeline.from_json(data)
    assert again.notes == ["tracks[0].clips[1].transiton_in: isn't a field the timeline knows (ignored: did you mean "
                           "transition_in?)"]
    report = render(again, tmp_path)
    assert again.notes[0] in report.warnings

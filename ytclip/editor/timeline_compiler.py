"""A timeline becomes a video in one ffmpeg run: every picture, move, transition, overlay, caption and the
whole sound mix in a single -filter_complex graph, encoded once.

    compiled = compile_timeline(timeline, base=folder)     # the inputs and the graph, to read or test
    report = render(timeline, "out.mp4", base=folder)      # the finished MP4

Nothing is rendered to a file on the way: stills are animated, clips cut, joined and layered, the captions
burnt in and the sound mixed inside the one graph, so no picture is ever encoded twice. (The loudness needs
two listens to the mix first; those read the sound only and write nothing: editor/audio_mixer.py.)

**The picture track** (video_track_0). Each clip becomes a stream of whole frames exactly as long as it's
needed, by the cheapest way that gives its move:

* `zoompan`: a clip that fills the canvas and zooms. The source is scaled up first (three times for a still,
  twice for video), so the move lands on thirds of a pixel and a slow push-in doesn't shimmer. A long
  timeline scales its stills up less (still_upscale), to stay inside the memory a render may use.
* `crop`: a clip that fills the canvas at one size and slides (a pan across a wide source: how a 16:9 video
  is followed in a 9:16 frame). The whole source is there to slide over, and it stops at its edge.
* `overlay`: anything else (smaller than the canvas, see-through, turned more than a few degrees): the clip
  is placed on the background colour exactly where its keyframes say.

A gap is filled with the background colour. The streams are then joined in order: a cut is a plain join, a
transition an xfade (motion_engine.TRANSITION_XFADE). Both clips reach half a transition past the cut, a
still simply by lasting longer, a video from its own footage (its first or last frame holding when there's
no more), so cuts stay exactly where the timeline puts them and the voice stays in time. All times are
snapped to whole frames first.

**Layers** (video_track_1 and up) go over that in track order, each clip scaled, turned, faded and moved by
its keyframes (ffmpeg evaluates them per frame), on screen only for its own stretch.

**Finish**: the timeline's look (vignette, grain), then each subtitle track burnt in from an .ass file
(editor/kinetic_subtitles.py) written beside the graph; ffmpeg runs in that folder, so no path has to be
escaped inside a filter.

**Sources.** Stills are read inside the graph (the movie filter), named relative to the folder ffmpeg runs
in; videos and sounds are inputs cut at the source (-ss/-t): one for each video clip, one for each
different cut of a sound (a whoosh used fifty times is one input, read fifty times in the graph).
audio_mixer.graph() mixes them.

**The command line.** Windows starts a program with at most 32,767 characters of command line. The graph
is written to a file and passed by name (the loudness passes' graphs too), and inputs are named relative to
the folder ffmpeg runs in when that's shorter, so a 20-minute timeline of stock clips fits. One that still
doesn't is refused with a plain message before ffmpeg is started (CompileError), not by Windows.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import fonts
from ..ffmpeg import (command_too_long, ffmpeg_exe, major_version, pick_encoder, probe, run_with_progress,
                      video_args)
from ..rendering import filtergraph
from ..studio.timeline_schema import (AUDIO_ROLES, VIDEO_BASE, Asset, Timeline, Track, Transition, VideoClip, check)
from . import audio_mixer, kinetic_subtitles
from . import motion_engine as M

STILL_UPSCALE = 3
STILL_MEMORY = 1.0e9  # bytes of enlarged stills a render may hold (35 stills at three times 1080x1920)
VIDEO_UPSCALE = 2
MAX_FAST_ROLL = 3.0  # degrees a clip may turn and still take a fast path (more: placed exactly, by overlay)
TRANSITION_SHARE = 0.8  # a transition takes at most this much of the shorter clip beside it
VIGNETTE = "vignette=angle=PI/5:mode=forward"
GRAIN = "noise=alls=3:allf=t"


def still_upscale(stills: int, width: int, height: int, budget: float = STILL_MEMORY) -> float:
    """How far stills are scaled up before they're animated: three times when memory allows (a move then lands
    on thirds of a pixel), less on a long timeline. ffmpeg opens every input at the start, so each still's
    enlarged frame is in memory for the whole render: 28 MB each at three times 1080x1920."""
    for up in (3.0, 2.5, 2.0, 1.5):
        if stills * width * height * 1.5 * up * up <= budget:
            return up
    return 1.25


class CompileError(RuntimeError):
    """A plain-English reason a timeline couldn't be rendered."""


@dataclass
class Compiled:
    inputs: list[list[str]]  # each input's ffmpeg arguments, in order ("-i" included)
    graph: str
    video: str = "vout"
    audio: str = "aout"
    files: dict[str, str] = field(default_factory=dict)  # written beside the graph: the captions
    fonts: list[str] = field(default_factory=list)
    duration: float = 0.0
    how: dict[str, str] = field(default_factory=dict)  # clip id -> zoompan | crop | overlay | fill
    staged: dict[str, Path] = field(default_factory=dict)  # copied beside the graph first: name -> file
    audio_inputs: list[list[str]] = field(default_factory=list)  # the sound's own inputs (for measuring)
    sources: list[audio_mixer.Source] = field(default_factory=list)  # ...numbered from 0

    @property
    def input_args(self) -> list[str]:
        return [a for one in self.inputs for a in one]


def _t(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".") or "0"


def snap(t: float, fps: int) -> float:
    """A time on a whole frame. A time exactly between two frames goes up, and a hair of floating-point error
    either side of it goes the same way: one clip's end (start + duration) and the next clip's start are the
    same moment and must land on the same frame, or a one-frame gap opens between them."""
    return math.floor(t * fps + 0.5 + 1e-6) / fps


def _rooted(path: Path | str) -> str:
    """A full path to work a relative name out from, the way the system will read that name. macOS and
    Linux follow ".." through the real folders, so links are resolved; Windows takes ".." off the text of
    the folder ffmpeg runs in, so there a link (a subst drive, a junction, a folder of a mapped share) must
    stay as it was written, or "..\\..\\x.mp4" counts up from a folder the path never names."""
    return os.path.abspath(path) if sys.platform == "win32" else str(Path(path).resolve())


def relative_to_work(path: Path | str, work: Path | str) -> str:
    """A file's name from the folder ffmpeg runs in, or "" when there isn't one (another drive)."""
    try:
        return os.path.relpath(_rooted(path), _rooted(work))
    except ValueError:
        return ""


def input_path(path: Path, work: Path | None) -> str:
    """A file as an ffmpeg input: its full path, or its path from the folder ffmpeg runs in when that's
    shorter (a long timeline has hundreds of inputs, and Windows limits a command line's length)."""
    full = str(Path(path).resolve())
    if work is None:
        return full
    rel = relative_to_work(path, work)
    return rel if rel and len(rel) < len(full) and not rel.startswith("-") else full


def _even(v: float) -> int:
    return max(2, 2 * math.ceil(v / 2 - 1e-9))


_PLAIN = re.compile(r"^[A-Za-z0-9 _./()+\-]+$")  # a file name no filter graph could misread


def graph_quote(value: str) -> str:
    """A file name as a filter option's value inside a filter graph: escaped once for the option (\\ : ') and
    quoted for the graph (a quote inside leaves the quotes, escaped, and comes back in)."""
    inner = value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return "'" + inner.replace("'", "'\\''") + "'"


def _hexcolour(c: str) -> str:
    return "0x" + c.lstrip("#").upper()


@dataclass
class _Segment:
    """One stretch of the picture track: a clip or a gap, with the transition that brings it in."""

    start: float
    end: float
    clip: VideoClip | None = None
    transition: Transition = field(default_factory=Transition)
    lead: float = 0.0  # half the transition before it
    tail: float = 0.0  # half the transition after it

    @property
    def length(self) -> float:
        return (self.end + self.tail) - (self.start - self.lead)


class _Builder:
    def __init__(self, tl: Timeline, base: Path | None, work: Path | None = None):
        self.tl = tl
        self.base = base
        self.work = work  # the folder ffmpeg runs in, as it was given: relative names are worked out from it
        self.named: dict[str, str] = {}  # a still's file -> its name in the graph
        self.staged: dict[str, Path] = {}  # names in the work folder -> the files to copy there
        self.c = tl.canvas
        self.fps = tl.canvas.fps
        self.inputs: list[list[str]] = []
        self.parts: list[str] = []
        self.how: dict[str, str] = {}
        self.n = 0
        self.same = [f"fps={self.fps}", "settb=AVTB", "setsar=1", "format=yuv420p"]
        stills = sum(1 for t in tl.video_tracks() if t.id == VIDEO_BASE for c in t.clips
                     if c.asset in tl.assets and tl.assets[c.asset].kind == "image")
        self.still_upscale = still_upscale(stills, tl.canvas.width, tl.canvas.height)

    # ------------------------------------------------------------ small things
    def snap(self, t: float) -> float:
        return snap(t, self.fps)

    def label(self, stem: str) -> str:
        self.n += 1
        return f"{stem}{self.n}"

    def path(self, asset: Asset) -> str:
        return input_path(self.tl.resolve(asset, self.base), self.work)

    def add_input(self, args: list[str]) -> int:
        self.inputs.append(args)
        return len(self.inputs) - 1

    def still(self, asset: Asset) -> str:
        """A still as a source inside the graph (ffmpeg's movie filter). Stills are read there rather than as
        inputs: ffmpeg then only opens each when its turn comes, which halves the memory a long timeline
        takes and the time it takes too. The file is named relative to the folder ffmpeg runs in; a name with
        characters a filter graph would trip over (or on another drive) is copied there under a plain one."""
        given = self.tl.resolve(asset, self.base)
        path = given.resolve()
        if self.work is None:
            return "movie=" + graph_quote(str(path).replace("\\", "/"))
        key = str(path)
        if key not in self.named:
            rel = relative_to_work(given, self.work).replace(os.sep, "/")
            if not rel or not _PLAIN.match(rel) or len(rel) > 200:
                rel = f"stills/{len(self.staged) + 1:03d}{path.suffix.lower()}"
                self.staged[rel] = path
            self.named[key] = rel
        return "movie=" + graph_quote(self.named[key])

    def source(self, clip: VideoClip, asset: Asset, length: float, lead: float, shape: list[str],
               still_frames: bool = False) -> tuple[str, list[str]]:
        """(what the chain starts with, its first filters): the clip's source as `length` seconds of frames
        starting `lead` before the clip's first, cut and sized by `shape`. The start is an input pad ("[3:v]")
        or a source filter and its comma. A still is shaped once and then repeated, so a pan over a big
        picture costs one resize, not one a frame; with `still_frames` it's left as its one frame (zoompan
        makes the rest)."""
        if asset.kind == "color":
            return (f"color=c={_hexcolour(asset.color)}:s={self.c.width}x{self.c.height}:r={self.fps}:"
                    f"d={_t(length + 4 / self.fps)},"), list(shape)
        if asset.kind == "image":
            if still_frames:
                return self.still(asset) + ",", list(shape)
            return self.still(asset) + ",", [*shape, "loop=loop=-1:size=1:start=0", f"setpts=N/({self.fps}*TB)",
                                             f"fps={self.fps}"]
        # A video: read from half a transition before its start when the source has it, else hold its first frame.
        seek = max(0.0, clip.source_in - lead)
        missing = lead - (clip.source_in - seek)
        args = ["-stream_loop", "-1"] if clip.loop else []
        args += ["-ss", _t(seek), "-t", _t(length + 1.0), "-i", self.path(asset)]
        k = self.add_input(args)
        hold = [f"start_mode=clone:start_duration={_t(missing)}"] if missing > 0.0005 else []
        return f"[{k}:v]", [f"fps={self.fps}", "tpad=" + ":".join([*hold, f"stop_mode=clone:stop_duration={_t(length + 1.0)}"]),
                            *shape]

    def crop_source(self, clip: VideoClip) -> list[str]:
        k = clip.crop
        if k is None or k.whole:
            return []
        return [f"crop=w='2*floor(iw*{k.w:.5f}/2)':h='2*floor(ih*{k.h:.5f}/2)':x='iw*{k.x:.5f}':y='ih*{k.y:.5f}'"]

    # ------------------------------------------------------------ how a clip is drawn
    def plan(self, clip: VideoClip, asset: Asset, base_track: bool) -> str:
        keys = clip.keyframes
        if not base_track or asset.kind == "color":
            return "overlay" if not (base_track and asset.kind == "color" and not M.moving(keys)) else "fill"
        if clip.fit != "cover":
            return "overlay"
        lo_r, hi_r = M.extremes(keys, "rotation")
        if max(abs(lo_r), abs(hi_r)) > MAX_FAST_ROLL or M.extremes(keys, "opacity")[0] < 1.0 - 1e-6:
            return "overlay"
        need = M.rotation_cover(max(abs(lo_r), abs(hi_r)), self.c.width, self.c.height)
        if M.extremes(keys, "scale")[0] < need - 5e-4:
            return "overlay"
        if M.constant(keys, "scale") is not None:
            return "crop"
        return "zoompan" if M.covers(keys, clip.duration, size=(self.c.width, self.c.height)) else "overlay"

    def rotate(self, keys, time: str, out_w: int, out_h: int, alpha: bool = False) -> list[str]:
        if M.constant(keys, "rotation") == 0:
            return []
        angle = M.expr(keys, "rotation", time)
        fill = "black@0" if alpha else _hexcolour(self.c.background)
        return [f"rotate=a='({angle})*PI/180':ow={out_w}:oh={out_h}:c={fill}"]

    def whole_frames(self, seg: _Segment, asset: Asset, how: str) -> str:
        """A picture-track clip as a stream of canvas-sized frames, seg.length long. Returns its label."""
        clip = seg.clip
        assert clip is not None
        W, H, F = self.c.width, self.c.height, self.fps
        keys = clip.keyframes
        length, lead = seg.length, seg.lead
        out = self.label("b")
        # xfade wants both sides at one constant frame rate and time base (ffmpeg 7 refuses a stream whose rate
        # a filter before it left unsaid), so every stream ends the same way.
        tail = [f"trim=duration={_t(length)}", "setpts=PTS-STARTPTS", *self.same]
        roll = max(abs(v) for v in M.extremes(keys, "rotation"))
        k = M.rotation_cover(roll, W, H)
        if how == "fill":
            self.parts.append(f"{self.source(clip, asset, length, lead, [])[0]}{','.join(tail)}[{out}]")
            return out
        if how == "zoompan":
            still = asset.kind == "image"
            up = self.still_upscale if still else VIDEO_UPSCALE
            ow, oh = _even(W * k), _even(H * k)
            uw, uh = _even(ow * up), _even(oh * up)
            time = f"on/{F}-{_t(lead)}"
            s = M.expr(keys, "scale", time)
            x, y = M.expr(keys, "x", time), M.expr(keys, "y", time)
            z = f"max(1,({s})/{k:.6f})"
            # The picture's centre sits (x - 0.5) canvas-widths from the canvas's, so the view looks that far
            # the other way: as a share of the picture, divided by its scale.
            zx = f"max(0,min(iw-iw/zoom,(0.5-(({x})-0.5)/({s}))*iw-iw/zoom/2))"
            zy = f"max(0,min(ih-ih/zoom,(0.5-(({y})-0.5)/({s}))*ih-ih/zoom/2))"
            frames = round(length * F) + 2 if still else 1  # a couple to spare: the trim after it is exact
            # 4:2:0 before it's blown up: ffmpeg holds every still's big frame at once, and this halves them.
            shape = [*self.crop_source(clip), "format=yuv420p",
                     f"scale={uw}:{uh}:force_original_aspect_ratio=increase:flags=lanczos", f"crop={uw}:{uh}"]
            head, first = self.source(clip, asset, length, lead, shape, still_frames=still)
            steps = [*first,
                     f"zoompan=z='{z}':x='{zx}':y='{zy}':d={frames}:s={ow}x{oh}:fps={F}",
                     *(self.rotate(keys, f"t-{_t(lead)}", W, H) or ([f"crop={W}:{H}"] if (ow, oh) != (W, H) else []))]
            self.parts.append(f"{head}{','.join([*steps, *tail])}[{out}]")
            return out
        if how == "crop":
            s = M.constant(keys, "scale") or 1.0
            time = f"t-{_t(lead)}"
            cw, ch = _even(W * k), _even(H * k)
            x, y = M.expr(keys, "x", time), M.expr(keys, "y", time)
            shape = [*self.crop_source(clip), "format=yuv420p",
                     f"scale={_even(W * s)}:{_even(H * s)}:force_original_aspect_ratio=increase:flags=lanczos"]
            head, first = self.source(clip, asset, length, lead, shape)
            steps = [*first,
                     f"crop={cw}:{ch}:x='max(0,min(iw-{cw},(iw-{cw})/2-(({x})-0.5)*{W}))':"
                     f"y='max(0,min(ih-{ch},(ih-{ch})/2-(({y})-0.5)*{H}))':exact=1",
                     *(self.rotate(keys, time, W, H) or ([f"crop={W}:{H}"] if (cw, ch) != (W, H) else []))]
            self.parts.append(f"{head}{','.join([*steps, *tail])}[{out}]")
            return out
        # overlay: the clip placed on the background colour
        bg = self.label("bg")
        # A few frames to spare (the trim after is exact): overlay holds back its last frame otherwise.
        self.parts.append(f"color=c={_hexcolour(self.c.background)}:s={W}x{H}:r={F}:d={_t(length + 4 / F)}[{bg}]")
        placed = self.place(bg, clip, asset, start=lead, length=length - lead, lead=lead, show=(0.0, length))
        self.parts.append(f"[{placed}]{','.join(tail)}[{out}]")
        return out

    def place(self, under: str, clip: VideoClip, asset: Asset, *, start: float, length: float, lead: float = 0.0,
              show: tuple[float, float] | None = None, fades: tuple[float, float] = (0.0, 0.0)) -> str:
        """Lay `clip` over the stream `under`, its first frame at `start` seconds into that stream (with `lead`
        seconds of it shown before). Returns the label of the result."""
        W, H = self.c.width, self.c.height
        keys = clip.keyframes
        total = lead + length
        fit = "increase" if clip.fit == "cover" else "decrease"
        s_lo, s_hi = M.extremes(keys, "scale")
        local = f"t-{_t(lead)}"  # seconds into the clip, on the clip's own stream (before it's moved to its time)
        # Scaled once, with care, to the biggest it gets; its move then only ever shrinks it.
        size = f"scale={_even(W * s_hi)}:{_even(H * s_hi)}"
        if asset.kind != "color":
            size += f":force_original_aspect_ratio={fit}:flags=lanczos"
        head, steps = self.source(clip, asset, total, lead, [*self.crop_source(clip), size, "format=yuva420p"])
        steps += [f"trim=duration={_t(total)}", "setpts=PTS-STARTPTS"]
        if M.constant(keys, "rotation") != 0:
            angle = M.expr(keys, "rotation", local)
            steps.append(f"rotate=a='({angle})*PI/180':ow='2*ceil(hypot(iw,ih)/2)':oh=ow:c=black@0")
        steps += self.opacity(keys, lead, total)
        a, b = fades
        if a > 0:
            steps.append(f"fade=t=in:st={_t(lead)}:d={_t(min(a, length))}:alpha=1")
        if b > 0:
            d = min(b, length)
            steps.append(f"fade=t=out:st={_t(total - d)}:d={_t(d)}:alpha=1")
        at = start - lead  # where the clip's stream begins in `under`
        steps.append(f"setpts=PTS+{_t(at)}/TB")
        clock = f"t-{_t(start)}"  # seconds into the clip, in `under`'s time
        if M.constant(keys, "scale") is None:
            ratio = f"(({M.expr(keys, 'scale', clock)})/{s_hi:.6f})"
            steps.append(f"scale=w='max(2,2*round(iw*{ratio}/2))':h='max(2,2*round(ih*{ratio}/2))':eval=frame:"
                         "flags=bilinear")
        ov = self.label("ov")
        self.parts.append(f"{head}{','.join(steps)}[{ov}]")
        out = self.label("v")
        x, y = M.expr(keys, "x", clock), M.expr(keys, "y", clock)
        lo, hi = show if show is not None else (at, at + total)
        self.parts.append(f"[{under}][{ov}]overlay=x='({x})*{W}-w/2':y='({y})*{H}-h/2':eval=frame:"
                          f"enable='between(t,{_t(lo)},{_t(hi)})':eof_action=pass:format=auto[{out}]")
        return out

    def opacity(self, keys, lead: float, total: float) -> list[str]:
        """Filters that set a clip's opacity over its own time: one number, or a command for each frame of a
        fade (ffmpeg has no opacity that takes an expression of time)."""
        fixed = M.constant(keys, "opacity")
        if fixed is not None:
            return [] if fixed >= 1.0 - 1e-6 else [f"colorchannelmixer=aa={fixed:.4f}"]
        name = f"colorchannelmixer@{self.label('op')}"
        first, last = keys[0].t, keys[-1].t
        cmds: list[str] = []
        prev = None
        f0 = max(0, math.floor((first + lead) * self.fps))
        f1 = min(math.ceil(total * self.fps), math.ceil((last + lead) * self.fps) + 1)
        for f in range(f0, f1 + 1):
            t = f / self.fps
            v = round(M.value_at(keys, "opacity", t - lead), 4)
            if v != prev:
                cmds.append(f"{_t(t)} {name} aa {v:g}")
                prev = v
        start = M.value_at(keys, "opacity", -lead)
        return [f"sendcmd=c='{';'.join(cmds)}'", f"{name}=aa={start:.4f}"]

    # ------------------------------------------------------------ the picture track
    def segments(self, track: Track | None) -> list[_Segment]:
        total = self.snap(self.tl.duration)
        segs: list[_Segment] = []
        at = 0.0
        for clip in sorted(track.clips if track else [], key=lambda c: c.start):
            start, end = self.snap(clip.start), min(total, self.snap(clip.end))
            if start >= total or end - start < 1.0 / self.fps - 1e-9:
                continue
            if start - at > 1e-6:
                segs.append(_Segment(at, start))
            segs.append(_Segment(max(at, start), end, clip, clip.transition_in))
            at = end
        if total - at > 1e-6 or not segs:
            segs.append(_Segment(at, total))
        for prev, seg in zip(segs, segs[1:]):
            tr = seg.transition
            if seg.clip is None and prev.clip is not None:  # into a gap: the clip before fades away
                tr = seg.transition = prev.clip.transition_out
            if tr.cut:
                continue
            room = min(prev.end - prev.start, seg.end - seg.start) * TRANSITION_SHARE
            # A clip between two transitions gives each at most half of itself.
            room = min(room, (prev.end - prev.start) - 2 * prev.lead)
            half = math.floor(min(tr.duration, max(0.0, room)) / 2 * self.fps) / self.fps
            if half < 1.0 / self.fps:
                seg.transition = Transition()
                continue
            prev.tail = seg.lead = half
        return segs

    def picture_track(self) -> str:
        track = self.tl.track(VIDEO_BASE) if self.tl.track(VIDEO_BASE) in self.tl.video_tracks() else None
        segs = self.segments(track)
        labels = []
        for seg in segs:
            if seg.clip is None:
                lab = self.label("gap")
                # Spare frames and then an exact cut, like every other source: the colour source alone stops
                # on a rounded time, and a frame too many would push every later cut a frame late.
                self.parts.append(f"color=c={_hexcolour(self.c.background)}:s={self.c.width}x{self.c.height}:"
                                  f"r={self.fps}:d={_t(seg.length + 4 / self.fps)},"
                                  f"trim=end_frame={round(seg.length * self.fps)},setpts=PTS-STARTPTS,"
                                  f"{','.join(self.same)}[{lab}]")
                labels.append(lab)
                continue
            asset = self.tl.assets[seg.clip.asset]
            how = self.plan(seg.clip, asset, True)
            self.how[seg.clip.id] = how
            labels.append(self.whole_frames(seg, asset, how))
        last = labels[0]
        # Joining end to end takes a stream's length from the spacing of its frames, so a first stream of one
        # frame counts as no length and its frame is lost. It's given a second frame, taken off after the join.
        # (One frame is too short to carry a transition, so that join is always end to end.)
        doubled = len(segs) > 1 and round(segs[0].length * self.fps) == 1 and segs[1].lead <= 0
        if doubled:
            last = self.label("two")
            self.parts.append(f"[{labels[0]}]tpad=stop=1:stop_mode=clone[{last}]")
        for i, (seg, lab) in enumerate(zip(segs[1:], labels[1:])):
            out = self.label("j")
            if seg.lead > 0:
                name = M.TRANSITION_XFADE.get(seg.transition.type, "fade")
                self.parts.append(f"[{last}][{lab}]xfade=transition={name}:duration={_t(2 * seg.lead)}:"
                                  f"offset={_t(seg.start - seg.lead)}[{out}]")
            else:
                spare = ",trim=start_frame=1,setpts=PTS-STARTPTS" if doubled and i == 0 else ""
                self.parts.append(f"[{last}][{lab}]concat=n=2:v=1:a=0{spare}[{out}]")
            last = out
        ends: list[str] = []
        first, final = segs[0], segs[-1]
        if first.clip is not None and not first.clip.transition_in.cut:
            colour = "white" if first.clip.transition_in.type == "dip_white" else "black"
            ends.append(f"fade=t=in:st=0:d={_t(min(first.clip.transition_in.duration, first.end))}:c={colour}")
        if final.clip is not None and not final.clip.transition_out.cut:
            d = min(final.clip.transition_out.duration, final.end - final.start)
            colour = "white" if final.clip.transition_out.type == "dip_white" else "black"
            ends.append(f"fade=t=out:st={_t(final.end - d)}:d={_t(d)}:c={colour}")
        out = self.label("base")
        total = self.snap(self.tl.duration)
        # One frame to spare, then the exact count. ffmpeg 7.1 (the one the Windows build carries) has its fps
        # filter drop a stream's last frame when the filter before it (a transition, an overlay) ends without
        # saying how long that frame lasts: every video with a transition came out a frame short there. The
        # spare frame is the one it drops; where it isn't dropped, the count takes it off.
        steps = [*ends, "tpad=stop=1:stop_mode=clone", f"fps={self.fps}",
                 f"trim=end_frame={round(total * self.fps)}"]
        self.parts.append(f"[{last}]{','.join(steps)}[{out}]")
        return out

    # ------------------------------------------------------------ layers and the finish
    def layers(self, under: str) -> str:
        total = self.snap(self.tl.duration)
        for track in self.tl.video_tracks():
            if track.id == VIDEO_BASE:
                continue
            for clip in sorted(track.clips, key=lambda c: c.start):
                start, end = self.snap(clip.start), min(total, self.snap(clip.end))
                if start >= total or end - start < 1.0 / self.fps - 1e-9:
                    continue
                asset = self.tl.assets[clip.asset]
                self.how[clip.id] = "overlay"
                fades = (0.0 if clip.transition_in.cut else clip.transition_in.duration,
                         0.0 if clip.transition_out.cut else clip.transition_out.duration)
                under = self.place(under, clip, asset, start=start, length=end - start, fades=fades)
        return under

    def finish(self, last: str, files: dict[str, str], fontsdir: str) -> None:
        steps = [{"vignette": VIGNETTE, "grain": GRAIN}[name] for name in self.tl.look]
        steps += filtergraph.burn_in(list(files), fontsdir)
        steps.append("format=yuv420p")
        self.parts.append(f"[{last}]{','.join(steps)}[vout]")


# ---------------------------------------------------------------- the sound's inputs

def audio_plan(tl: Timeline, base: Path | None = None, offset: int = 0,
               work: Path | None = None) -> tuple[list[list[str]], list[audio_mixer.Source]]:
    """(the sounds' ffmpeg inputs, the mixer's sources) for every audio clip and every picture clip sent to a
    bus. Inputs are numbered from `offset`. Clips that are the same cut of the same file (a sound effect used
    again and again) share one input: the graph may read an input as often as it likes."""
    inputs: list[list[str]] = []
    sources: list[audio_mixer.Source] = []
    seen: dict[tuple[str, ...], int] = {}

    def add(asset: Asset, clip, role: str, gain: float, fade_in: float = 0.0, fade_out: float = 0.0) -> None:
        if clip.start >= tl.duration or clip.duration <= 0:
            return
        args = ["-stream_loop", "-1"] if clip.loop else []
        args += ["-ss", _t(clip.source_in), "-t", _t(clip.duration + 0.25), "-vn", "-i",
                 input_path(tl.resolve(asset, base), work)]
        k = seen.get(tuple(args))
        if k is None:
            inputs.append(args)
            k = seen[tuple(args)] = len(inputs) - 1
        sources.append(audio_mixer.Source(f"{offset + k}:a", role, clip.start, clip.duration, gain,
                                          fade_in, fade_out))

    for track in tl.audio_tracks():
        for clip in sorted(track.clips, key=lambda c: c.start):
            add(tl.assets[clip.asset], clip, track.role, clip.gain_db + track.gain_db, clip.fade_in, clip.fade_out)
    for track in tl.video_tracks():
        for clip in sorted(track.clips, key=lambda c: c.start):
            if clip.audio in AUDIO_ROLES and tl.assets[clip.asset].kind == "video":
                add(tl.assets[clip.asset], clip, clip.audio, clip.gain_db, 0.01, 0.01)
    return inputs, sources


# ---------------------------------------------------------------- compiling

def compile_timeline(tl: Timeline, base: str | Path | None = None, *, work: str | Path | None = None,
                     voice_gain_db: float = 0.0, measured: dict | None = None, fontsdir: str = "",
                     files: bool = True) -> Compiled:
    """The ffmpeg inputs and filter graph for `tl`. `base`: the folder relative asset paths are in. `work`:
    the folder ffmpeg will run in (stills are named relative to it; without it, by their full path).
    `voice_gain_db` and `measured` come from the loudness passes (render() runs them). `files`: check every
    asset's file is there. Raises TimelineError when the timeline is broken."""
    base = Path(base) if base is not None else None
    check(tl, base, files=files)
    b = _Builder(tl, base, Path(work) if work is not None else None)
    last = b.layers(b.picture_track())
    captions: dict[str, str] = {}
    used_fonts: list[str] = []
    for i, track in enumerate(tl.subtitle_tracks()):
        if not track.events:
            continue
        captions[f"captions_{i}.ass" if i else "captions.ass"] = kinetic_subtitles.to_ass(track, tl.canvas)
        used_fonts += kinetic_subtitles.fonts_used(track)
    b.finish(last, captions, fontsdir)
    audio_inputs, sources = audio_plan(tl, base, work=b.work)
    shifted = [audio_mixer.Source(f"{int(s.label.split(':')[0]) + len(b.inputs)}:a", s.role, s.start, s.duration,
                                  s.gain_db, s.fade_in, s.fade_out, s.skip) for s in sources]
    total = b.snap(tl.duration)
    sound = audio_mixer.graph(shifted, tl.audio, total, voice_gain_db=voice_gain_db, measured=measured)
    return Compiled(inputs=b.inputs + audio_inputs, graph=";\n".join(b.parts + sound.split(";")), files=captions,
                    fonts=sorted(set(used_fonts)), duration=total, how=b.how, staged=b.staged,
                    audio_inputs=audio_inputs,
                    sources=sources)


# ---------------------------------------------------------------- rendering

@dataclass
class RenderReport:
    path: Path
    duration: float
    encoder: str
    voice_gain_db: float = 0.0
    mix_lufs_before: float | None = None
    how: dict[str, str] = field(default_factory=dict)
    inputs: int = 0
    filters: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"duration": self.duration, "encoder": self.encoder, "voice_gain_db": round(self.voice_gain_db, 2),
                "mix_lufs_before": self.mix_lufs_before, "inputs": self.inputs, "filters": self.filters,
                "passes": 1, "clips": dict(self.how), "warnings": list(self.warnings)}


def _ffmpeg_major(exe: str) -> int:
    return major_version(exe)


def graph_args(script: str) -> list[str]:
    """How this ffmpeg is told to read its filter graph from a file (ffmpeg 7 renamed the option, and later
    dropped the old spelling)."""
    return ["-/filter_complex", script] if _ffmpeg_major(ffmpeg_exe()) >= 7 else ["-filter_complex_script", script]


def measure_sound(compiled: Compiled, tl: Timeline, cwd: Path | None = None) -> tuple[float, dict | None]:
    """(the voice bus's gain, the whole mix's loudness numbers): the two listens before the render."""
    inputs = [a for one in compiled.audio_inputs for a in one]
    if not compiled.sources:
        return 0.0, None
    gain = 0.0
    voice = audio_mixer.voice_graph(compiled.sources, tl.audio, compiled.duration)
    if voice:
        gain = audio_mixer.voice_gain(audio_mixer.measure(inputs, voice, compiled.duration, cwd), tl.audio)
    mix = audio_mixer.graph(compiled.sources, tl.audio, compiled.duration, voice_gain_db=gain, measuring=True)
    return gain, audio_mixer.measure(inputs, mix, compiled.duration, cwd)


def command(compiled: Compiled, dest: Path, *, encoder: str = "libx264", quality: int = 20, fps: int = 30,
            audio_kbps: int = 192, script: str = "graph.txt") -> list[str]:
    cmd = [ffmpeg_exe(), "-y", "-v", "error", *compiled.input_args, *graph_args(script), "-map",
           f"[{compiled.video}]", "-map", f"[{compiled.audio}]"]
    cmd += video_args(encoder, quality, "delivery", fps)
    cmd += ["-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ar", "44100",
            "-t", f"{compiled.duration:.3f}", "-movflags", "+faststart", str(Path(dest).resolve())]
    return cmd


def render(tl: Timeline, dest: str | Path, *, base: str | Path | None = None, work: str | Path | None = None,
           settings=None, progress: Callable[[str, "float | None"], None] | None = None,
           check_cancel: Callable[[], None] | None = None, log: Callable[[str], None] | None = None) -> RenderReport:
    """Render `tl` to `dest` (an .mp4). `work`: a folder for the graph and the captions (a temporary one when
    not given). `settings`: the app's Settings, for the encoder and quality. Raises TimelineError or
    CompileError."""
    from ..settings import work_folder

    progress = progress or (lambda m, f: None)
    log = log or (lambda m: None)
    dest = Path(dest)
    if work is None:
        with work_folder("timeline-") as tmp:
            return render(tl, dest, base=base, work=tmp, settings=settings, progress=progress,
                          check_cancel=check_cancel, log=log)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    warnings = [str(p) for p in check(tl, base, files=True)]
    warnings += [n for n in getattr(tl, "notes", []) if n not in warnings]  # what reading the file ignored
    if snap(tl.duration, tl.canvas.fps) < 1.0 / tl.canvas.fps:
        raise CompileError(f"The timeline is {tl.duration:g} s long, less than one frame: there's nothing to render.")
    silent_sources(tl, Path(base) if base is not None else None)
    progress("Putting the video together: listening to the mix", None)
    first = compile_timeline(tl, base, work=work)
    gain, measured = measure_sound(first, tl, work)
    if first.sources and measured is None:
        warnings.append("The mix couldn't be measured before rendering, so its loudness was set in one pass "
                        "(a little less exact).")
    if check_cancel:
        check_cancel()
    fontsdir = ":fontsdir=fonts" if first.fonts and fonts.stage(first.fonts, work) else ""
    compiled = compile_timeline(tl, base, work=work, voice_gain_db=gain, measured=measured, fontsdir=fontsdir)
    for name, text in compiled.files.items():
        (work / name).write_text(text, encoding="utf-8")
    for name, source in compiled.staged.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, work / name)
    (work / "graph.txt").write_text(compiled.graph, encoding="utf-8")
    quality = min(int(getattr(settings, "video_quality", 20)), 21)
    kbps = max(160, int(getattr(settings, "audio_bitrate", 192)))
    encoder = pick_encoder(getattr(settings, "encoder", "auto") or "auto")

    def step(f: float) -> None:
        if check_cancel:
            check_cancel()
        progress("Putting the video together", f)

    dest.parent.mkdir(parents=True, exist_ok=True)

    def run(enc: str) -> None:
        cmd = command(compiled, dest, encoder=enc, quality=quality, fps=tl.canvas.fps, audio_kbps=kbps)
        if command_too_long(cmd):
            raise CompileError(TOO_MANY_CLIPS.format(n=len(compiled.inputs)))
        try:
            run_with_progress(cmd, compiled.duration, step, cwd=work)
        except OSError as exc:  # ffmpeg couldn't be started at all
            raise CompileError(f"ffmpeg couldn't be started to render the timeline ({exc}).") from exc

    try:
        run(encoder)
    except RuntimeError as exc:
        if encoder == "libx264":
            raise CompileError(_plain_error(exc)) from exc
        said = " ".join(str(exc).split())[:160]
        log(f"The graphics card's encoder ({encoder}) failed ({said}), so the video is encoded on the processor.")
        encoder = "libx264"
        try:
            run(encoder)
        except RuntimeError as again:
            raise CompileError(_plain_error(again)) from again
    progress("Putting the video together", 1.0)
    before = None
    try:
        before = float(measured["input_i"]) if measured else None
    except (KeyError, TypeError, ValueError):
        pass
    return RenderReport(dest, compiled.duration, encoder, gain, before, compiled.how, len(compiled.inputs),
                        compiled.graph.count(";") + 1, warnings)


TOO_MANY_CLIPS = ("This timeline has {n} video and sound clips of its own, more than Windows can start ffmpeg with "
                  "in one run. Split it in two (two timelines, joined afterwards), or use fewer video clips: "
                  "stills don't count.")


def silent_sources(tl: Timeline, base: Path | None = None) -> None:
    """Refuse, by name, a video whose sound the timeline asks for when the file has none (stock clips often
    don't): ffmpeg's own words for it name neither the clip nor the cause."""
    asked: list[tuple[str, Asset]] = []
    for track in tl.video_tracks():
        asked += [(c.id, tl.assets[c.asset]) for c in track.clips
                  if c.audio in AUDIO_ROLES and c.asset in tl.assets and tl.assets[c.asset].kind == "video"]
    for track in tl.audio_tracks():
        asked += [(c.id, tl.assets[c.asset]) for c in track.clips
                  if c.asset in tl.assets and tl.assets[c.asset].kind == "video"]
    heard: dict[str, bool] = {}
    for clip_id, asset in asked:
        path = tl.resolve(asset, base)
        key = str(path)
        if key not in heard:
            try:
                heard[key] = bool(probe(path).has_audio)
            except Exception:  # noqa: BLE001 - can't tell: let ffmpeg try
                heard[key] = True
        if not heard[key]:
            raise CompileError(f"Clip {clip_id}: {asset.path} has no sound of its own, so it can't be sent to a "
                               f"sound bus. Take \"audio\" off the clip (or use a file that has sound).")


def _plain_error(exc: Exception) -> str:
    text = str(exc).replace("ffmpeg failed:", "").strip()
    return "ffmpeg couldn't render the timeline: " + (text[-500:] or "it gave no reason")

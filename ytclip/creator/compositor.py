"""Putting a creation together with ffmpeg: moving scenes, transitions, captions and the sound mix.

Three steps, each its own ffmpeg run so a long video shows real progress and a failure says which part:

1. render_scene: each scene's picture becomes a short clip with its camera move (motion.py), drawn frame
   by frame with OpenCV's sub-pixel warp and piped into ffmpeg (or ffmpeg's zoompan without OpenCV). A
   B-roll clip is scaled to cover 9:16 and looped or trimmed. Each clip runs half a transition past its
   scene at both ends, so neighbours overlap for the cross-fade. render_scenes does several at once
   (scene_workers: none in parallel when Settings → Speed is Quiet, a few when it's Balanced or Full
   power) and encodes them on the graphics card when there's an encoder for it (h264_amf on a Radeon),
   near-lossless; a graphics card encoder that fails is dropped for the processor's, and the scene redone.
2. mix_audio: the sound, in two passes:
       voice   → 70 Hz high-pass, speech compressor, gain to -14 LUFS (measured first)
       music   → looped to length, level, fades, sidechain-ducked about 16 dB under the voice
       effects → each delayed to its moment at its own level
       all three mixed → two-pass EBU R128 loudness to -14 LUFS / -1 dBTP, then a limiter as a last guard
3. assemble: the clips chained with xfade (a cross-dissolve or a dip to black on each join; "mixed" varies
   them), a light film look (vignette and grain), the kinetic captions and the hook card burnt in (libass),
   the mix laid under it, encoded for TikTok (1080x1920, 30 fps, H.264 high, AAC 44.1 kHz).

The graphs are built by pure functions (scene_filter, mix_graph, video_graph) so tests can read them.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import speed
from ..audio import ducking, mastering
from ..ffmpeg import NO_WINDOW, encoder_args, ffmpeg_exe, pick_encoder, run, run_with_progress
from . import motion

Progress = Callable[[str, "float | None"], None]
WIDTH, HEIGHT, FPS = 1080, 1920, 30
TRANSITION = 0.3  # seconds of each cross-fade
VOICE_HIGHPASS = "highpass=f=70:poles=2"
SFX_BUS_DB = 0.0
MIX_RATE = 48000
LIMIT = 0.871  # alimiter's ceiling, linear: -1.2 dBFS, under the -1 dBTP target
XFADES = {"fade": "fade", "dissolve": "dissolve", "fadeblack": "fadeblack"}
MIXED = ("fade", "fadeblack", "smoothup", "fade", "circleopen", "fade", "slideleft", "dissolve", "zoomin")


@dataclass
class Timeline:
    duration: float  # the whole video
    bounds: list[float]  # joins between scenes
    overlap: float = TRANSITION
    transitions: list[str] = field(default_factory=list)  # xfade name per join

    @property
    def spans(self) -> list[tuple[float, float]]:
        return motion.clip_spans(self.bounds, self.duration, self.overlap)


def make_timeline(scene_starts: list[float], duration: float, transition: str = "fade",
                  sfx_cues: list[str] | None = None) -> Timeline:
    """Joins where each scene's voice starts (just ahead of it), the cross-fade's length fitted to the
    shortest scene, and a transition per join: `transition` is an xfade name, or "mixed" to vary them
    (a whoosh cue gets a moving one)."""
    bounds = motion.scene_bounds(scene_starts)
    edges = [0.0, *bounds, duration]
    shortest = min((b - a for a, b in zip(edges, edges[1:])), default=duration)
    overlap = max(0.0, min(TRANSITION, shortest * 0.4))
    names: list[str] = []
    for i in range(len(bounds)):
        if transition == "mixed":
            cue = (sfx_cues or [])[i + 1] if sfx_cues and i + 1 < len(sfx_cues) else ""
            names.append("smoothup" if cue == "whoosh" else MIXED[i % len(MIXED)])
        else:
            names.append(XFADES.get(transition, "fade"))
    return Timeline(round(duration, 3), [round(b, 3) for b in bounds], round(overlap, 3), names)


# ---------------------------------------------------------------- 1. scenes

def scene_filter(kind: str, seconds: float, move: motion.Move | None = None) -> str:
    """ffmpeg filters for one scene from a single picture (zoompan) or a clip (cover, loop, trim)."""
    frames = max(1, round(seconds * FPS))
    if kind == "video":
        return (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,crop={WIDTH}:{HEIGHT},"
                f"fps={FPS},setsar=1,trim=duration={seconds:.3f},setpts=PTS-STARTPTS,format=yuv420p")
    return motion.zoompan(move or motion.get("slow_push_in"), frames, FPS, WIDTH, HEIGHT) + ",setsar=1,format=yuv420p"


SCENE_QP = 16  # scene clips on a graphics card encoder: near-lossless (they're encoded again once, at the end)


def intermediate_args(encoder: str = "libx264") -> list[str]:
    """A scene clip's encoder settings: near-lossless and fast, on the graphics card when `encoder` is one."""
    q = str(SCENE_QP)
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", "speed", "-rc", "cqp",
                "-qp_i", q, "-qp_p", q, "-bf", "0"]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p2", "-rc", "constqp", "-qp", q, "-bf", "0"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", q]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14"]


def _intermediate(dest: Path, encoder: str = "libx264") -> list[str]:
    """Scene clips are kept near-lossless: they're encoded again once, at the end."""
    return [*intermediate_args(encoder), "-pix_fmt", "yuv420p", "-an", str(dest)]


def scene_workers(s, scenes: int, logical: int | None = None) -> int:
    """How many scenes are animated at once (Settings → Speed): one when Quiet; with Balanced about a sixth of
    the processor's threads (2 on 16), with Full power a quarter (4 on 16). Each takes its warp and encoder."""
    n = logical or os.cpu_count() or 4
    want = {"quiet": 1, "balanced": max(1, min(3, n // 6)), "full": max(2, min(6, n // 4))}[
        speed.level(getattr(s, "speed", "balanced"))]
    return max(1, min(want, scenes))


def scene_encoder(s) -> str:
    """The encoder for scene clips: Settings → Export → Encoder ("auto" picks the graphics card's when ffmpeg
    can use it)."""
    return pick_encoder(getattr(s, "encoder", "auto") or "auto")


def render_scene(source: Path, kind: str, seconds: float, dest: Path, move: motion.Move,
                 progress: Callable[[float], None] | None = None, check: Callable[[], None] | None = None,
                 use_cv2: bool = True, encoder: str = "libx264") -> Path:
    """One scene's clip, `seconds` long, at 1080x1920 and 30 fps."""
    seconds = max(1.0 / FPS, seconds)
    if kind == "video":
        run([ffmpeg_exe(), "-y", "-v", "error", "-stream_loop", "-1", "-i", str(source), "-t", f"{seconds:.3f}",
             "-vf", scene_filter("video", seconds), *_intermediate(dest, encoder)])
        if progress:
            progress(1.0)
        return dest
    if use_cv2:
        try:
            return _render_cv2(source, seconds, dest, move, progress, check, encoder)
        except ImportError:
            pass
    run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(source), "-vf", scene_filter("image", seconds, move),
         "-frames:v", str(max(1, round(seconds * FPS))), *_intermediate(dest, encoder)])
    if progress:
        progress(1.0)
    return dest


@dataclass
class SceneJob:
    source: Path
    kind: str  # image | video
    seconds: float
    dest: Path
    move: motion.Move


class _Halted(Exception):
    """Another scene failed, or the video was cancelled: this one stops too."""


def render_scenes(jobs: list[SceneJob], *, workers: int = 1, encoder: str = "libx264",
                  progress: Progress | None = None, check: Callable[[], None] | None = None,
                  log: Callable[[str], None] | None = None, render: Callable | None = None) -> tuple[list[Path], str]:
    """Every scene's clip, `workers` at a time: (the clips in order, the encoder they ended up on). A graphics
    card encoder that fails on a scene is dropped for the processor's (x264) and that scene made again; any
    other failure, or `check` raising (a cancel), stops the lot. Progress and `check` only run on the calling
    thread."""
    render = render or render_scene
    n = len(jobs)
    done = [0.0] * n
    lock = threading.Lock()
    stop = threading.Event()
    state = {"encoder": encoder}
    parallel = workers > 1 and n > 1

    def halt() -> None:
        if stop.is_set():
            raise _Halted()
        if not parallel and check:
            check()

    def one(i: int) -> Path:
        job = jobs[i]

        def step(f: float) -> None:
            with lock:
                done[i] = max(done[i], min(1.0, f))
            if not parallel and progress:
                progress(f"Animating scene {i + 1} of {n}", (i + min(1.0, f)) / n)

        enc = state["encoder"]
        try:
            render(job.source, job.kind, job.seconds, job.dest, job.move, progress=step, check=halt, encoder=enc)
        except _Halted:
            raise
        except Exception as exc:
            if enc == "libx264":
                raise
            with lock:
                if state["encoder"] == enc:
                    state["encoder"] = "libx264"
                    if log:
                        why = " ".join(str(exc).split())[:160] or type(exc).__name__
                        log(f"The graphics card's encoder ({enc}) failed on a scene ({why}), so the scenes are "
                            "encoded on the processor instead.")
            render(job.source, job.kind, job.seconds, job.dest, job.move, progress=step, check=halt,
                   encoder="libx264")
        step(1.0)
        return job.dest

    if not parallel:
        for i in range(n):
            if check:
                check()
            one(i)
        return [j.dest for j in jobs], state["encoder"]

    threads = _cv2_threads(max(1, (os.cpu_count() or 4) // workers))
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scene") as pool:
            futures = [pool.submit(one, i) for i in range(n)]
            pending = set(futures)
            try:
                while pending:
                    finished, pending = wait(pending, timeout=0.4, return_when=FIRST_EXCEPTION)
                    for f in finished:
                        f.result()
                    if check:
                        check()
                    if progress:
                        with lock:
                            total, ready = sum(done) / n, sum(1 for d in done if d >= 1.0)
                        progress(f"Animating scenes, {workers} at a time: {ready} of {n} done", total)
            except BaseException:
                stop.set()
                for f in futures:
                    f.cancel()
                raise
    finally:
        _cv2_threads(threads)
    return [j.dest for j in jobs], state["encoder"]


def _cv2_threads(n: int | None) -> int | None:
    """Set OpenCV's own thread count (so parallel scenes share the processor rather than fight over it);
    returns what it was. None: OpenCV isn't there, or leave it."""
    if n is None:
        return None
    try:
        import cv2
    except ImportError:
        return None
    before = cv2.getNumThreads()
    cv2.setNumThreads(int(n))
    return before


def _render_cv2(source: Path, seconds: float, dest: Path, move: motion.Move, progress, check,
                encoder: str = "libx264") -> Path:
    import cv2
    import numpy as np

    data = np.fromfile(str(source), np.uint8)  # imread can't open non-ASCII paths on Windows
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"Couldn't read the picture {source.name}")
    # Work from the picture scaled to cover the frame at the move's deepest zoom, so the warp never
    # magnifies: an upscale happens once, with Lanczos, rather than every frame.
    h0, w0 = img.shape[:2]
    deepest = max(move.zoom0, move.zoom1) * 1.06
    cw, ch = motion.cover_size(w0, h0, WIDTH, HEIGHT)
    target = (max(1, round(cw * deepest)), max(1, round(ch * deepest)))
    if target[0] > w0:
        img = cv2.resize(img, target, interpolation=cv2.INTER_LANCZOS4)
    elif target[0] < w0 * 0.7:
        img = cv2.resize(img, target, interpolation=cv2.INTER_AREA)
    h, w = img.shape[:2]
    frames = max(1, round(seconds * FPS))
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{WIDTH}x{HEIGHT}",
           "-r", str(FPS), "-i", "-", *_intermediate(dest, encoder)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    try:
        for f in range(frames):
            if check and f % 15 == 0:
                check()
            m = np.array(motion.affine(move, f / max(1, frames - 1), w, h, WIDTH, HEIGHT), np.float32)
            frame = cv2.warpAffine(img, m, (WIDTH, HEIGHT), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
            proc.stdin.write(frame.tobytes())
            if progress and f % 30 == 0:
                progress(f / frames)
        proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0:
            raise RuntimeError("ffmpeg couldn't encode a scene: " + err.strip()[-300:])
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    if progress:
        progress(1.0)
    return dest


# ---------------------------------------------------------------- 2. sound

@dataclass
class MixInputs:
    voice: Path
    duration: float
    music: Path | None = None
    music_db: float = -20.0
    sfx: list = field(default_factory=list)  # sfx_library.SfxEvent
    voice_gain_db: float = 0.0  # brings the voice to -14 LUFS (measured)


def mix_graph(mi: MixInputs, measured: dict | None = None, measuring: bool = False) -> tuple[list[str], str]:
    """(ffmpeg input arguments, filter graph ending in [aout]) for the mix. Inputs: 0 the voice, then the
    music (looping), then each effect."""
    args = ["-i", str(mi.voice)]
    parts = [f"[0:a]aresample={MIX_RATE},aformat=channel_layouts=mono,{VOICE_HIGHPASS},{mastering.COMPRESSOR},"
             f"volume={mi.voice_gain_db:.2f}dB,aformat=channel_layouts=stereo,apad=whole_dur={mi.duration:.3f}[voice]"]
    n = 1
    layers = ["voice"]
    if mi.music is not None:
        args += ["-stream_loop", "-1", "-i", str(mi.music)]
        fade_out = max(0.0, mi.duration - 1.6)
        parts.append("[voice]asplit=2[vmain][vkey]")
        parts.append(f"[{n}:a]atrim=0:{mi.duration:.3f},asetpts=PTS-STARTPTS,aresample={MIX_RATE},"
                     f"aformat=channel_layouts=stereo,volume={mi.music_db:.1f}dB,afade=t=in:d=1.0,"
                     f"afade=t=out:st={fade_out:.3f}:d=1.6[mus]")
        parts.append(f"[mus][vkey]{ducking.DUCK}[ducked]")
        layers = ["vmain", "ducked"]
        n += 1
    sfx_labels = []
    for k, ev in enumerate(mi.sfx):
        args += ["-i", str(ev.path)]
        ms = max(0, round(ev.time * 1000))
        parts.append(f"[{n}:a]aresample={MIX_RATE},aformat=channel_layouts=stereo,volume={ev.gain_db:.1f}dB,"
                     f"adelay={ms}|{ms}[fx{k}]")
        sfx_labels.append(f"[fx{k}]")
        n += 1
    if sfx_labels:
        if len(sfx_labels) == 1:
            parts.append(f"{sfx_labels[0]}anull[sfx]")
        else:
            parts.append("".join(sfx_labels) + f"amix=inputs={len(sfx_labels)}:normalize=0:duration=longest[sfx]")
        layers.append("sfx")
    joined = "".join(f"[{x}]" for x in layers)
    tail = [mastering.loudnorm(None if measuring else measured)]
    if not measuring:
        tail += [f"alimiter=limit={LIMIT}:attack=4:release=60:level=false", f"aresample={MIX_RATE}",
                 f"atrim=0:{mi.duration:.3f}"]
    mix = f"amix=inputs={len(layers)}:normalize=0:duration=first," if len(layers) > 1 else ""
    parts.append(f"{joined}{mix}{','.join(tail)}[aout]")
    return args, ";".join(parts)


def measure_loudness(path: Path) -> dict | None:
    """loudnorm's measurement of a file (integrated LUFS, true peak, range)."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-af", mastering.loudnorm(None), "-f", "null", "-"]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW)
    return mastering.parse_measurement(done.stderr) if done.returncode == 0 else None


def ebur128(path: Path) -> dict:
    """The finished file's loudness as a meter reads it: {"lufs": -14.0, "true_peak": -1.2, "lra": 5.1}."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW)
    text = done.stderr[done.stderr.rfind("Summary:"):] if "Summary:" in done.stderr else ""
    out = {}
    for key, pattern in (("lufs", r"I:\s*(-?[\d.]+) LUFS"), ("lra", r"LRA:\s*(-?[\d.]+) LU"),
                         ("true_peak", r"Peak:\s*(-?[\d.]+|-inf) dBFS")):
        m = re.search(pattern, text)
        if m:
            try:
                out[key] = float(m[1])
            except ValueError:
                out[key] = -math.inf
    return out


def mix_audio(mi: MixInputs, dest: Path, progress: Progress | None = None) -> dict:
    """The finished sound as a WAV at `dest`. Returns what was measured on the way."""
    progress = progress or (lambda m, f: None)
    progress("Mixing the sound: measuring the voice", None)
    voice = measure_loudness(mi.voice)
    if voice:
        # The voice alone to -14 LUFS (the compressor adds a little; the final pass trims it exactly).
        mi.voice_gain_db = max(-20.0, min(30.0, mastering.TARGET_I - float(voice["input_i"]) - 1.5))
    progress("Mixing the sound: measuring the mix", 0.3)
    args, graph = mix_graph(mi, measuring=True)
    done = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", *args, "-filter_complex", graph, "-map", "[aout]",
                           "-t", f"{mi.duration:.3f}", "-f", "null", "-"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    measured = mastering.parse_measurement(done.stderr) if done.returncode == 0 else None
    if done.returncode != 0:
        raise RuntimeError("ffmpeg couldn't mix the sound: " + done.stderr.strip()[-400:])
    progress("Mixing the sound", 0.65)
    args, graph = mix_graph(mi, measured)
    run([ffmpeg_exe(), "-y", "-v", "error", *args, "-filter_complex", graph, "-map", "[aout]", "-t",
         f"{mi.duration:.3f}", "-c:a", "pcm_s24le", "-ar", str(MIX_RATE), str(dest)])
    progress("Mixing the sound", 1.0)
    return {"voice_lufs": float(voice["input_i"]) if voice else None, "voice_gain_db": round(mi.voice_gain_db, 2),
            "mix_before_lufs": float(measured["input_i"]) if measured else None}


# ---------------------------------------------------------------- 3. the video

def video_graph(timeline: Timeline, *, captions: bool, cards: bool, look: bool = True,
                fonts: str = "") -> tuple[str, str]:
    """(graph, output label) chaining the scene clips (inputs 0..n-1) with xfade, then the look, the
    hook card and the captions."""
    spans = timeline.spans
    n = len(spans)
    parts: list[str] = []
    for i in range(n):
        parts.append(f"[{i}:v]settb=AVTB,fps={FPS},setsar=1,format=yuv420p[s{i}]")
    last = "s0"
    for i in range(1, n):
        offset = max(0.0, timeline.bounds[i - 1] - timeline.overlap / 2)
        name = timeline.transitions[i - 1] if i - 1 < len(timeline.transitions) else "fade"
        if timeline.overlap > 0.02:
            parts.append(f"[{last}][s{i}]xfade=transition={name}:duration={timeline.overlap:.3f}:"
                         f"offset={offset:.3f}[x{i}]")
        else:
            parts.append(f"[{last}][s{i}]concat=n=2:v=1:a=0[x{i}]")
        last = f"x{i}"
    chain: list[str] = []
    if look:
        chain.append("vignette=angle=PI/5:mode=forward")
        chain.append("noise=alls=3:allf=t")
    if cards:
        chain.append(f"subtitles=cards.ass{fonts}")
    if captions:
        chain.append(f"subtitles=captions.ass{fonts}")
    chain.append("format=yuv420p")
    parts.append(f"[{last}]{','.join(chain)}[vout]")
    return ";".join(parts), "vout"


def assemble(clips: list[Path], timeline: Timeline, audio: Path, dest: Path, work: Path, s, *,
             captions: bool = True, cards: bool = False, progress: Callable[[float], None] | None = None) -> Path:
    """The finished MP4. `work` holds captions.ass / cards.ass and the staged fonts (ffmpeg runs there, so
    libass finds them by name, which sidesteps escaping Windows paths in filter arguments)."""
    from ..fonts import stage

    names = [getattr(s, "caption_font", "Montserrat Black"), getattr(s, "banner_font", "TikTok Sans Black")]
    fontsdir = ":fontsdir=fonts" if stage(names, work) else ""
    graph, out = video_graph(timeline, captions=captions, cards=cards, look=getattr(s, "creator_film_look", True),
                             fonts=fontsdir)
    cmd = [ffmpeg_exe(), "-y", "-v", "error"]
    for c in clips:
        cmd += ["-i", str(Path(c).resolve())]
    cmd += ["-i", str(Path(audio).resolve()), "-filter_complex", graph, "-map", f"[{out}]", "-map", f"{len(clips)}:a"]
    encoder = pick_encoder(getattr(s, "encoder", "auto"))
    cmd += codec_args(encoder, min(int(getattr(s, "video_quality", 20)), 21)) + ["-pix_fmt", "yuv420p", "-r", str(FPS)]
    cmd += ["-c:a", "aac", "-b:a", f"{max(160, int(getattr(s, 'audio_bitrate', 192)))}k", "-ar", "44100",
            "-t", f"{timeline.duration:.3f}", "-movflags", "+faststart", str(Path(dest).resolve())]
    run_with_progress(cmd, timeline.duration, progress, cwd=work)
    return dest


def codec_args(encoder: str, quality: int) -> list[str]:
    """The encoder with a bitrate ceiling: 10 Mbps is plenty for 1080x1920 at 30 fps (TikTok re-encodes
    anyway), and grain and soft gradients would otherwise take all they're given (a minute came to 150 MB+).
    AMF's and Quick Sync's quality modes can't be capped, so they use peak-limited VBR."""
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "vbr_peak", "-b:v", "8M", "-maxrate", "10M"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-b:v", "8M", "-maxrate", "10M"]
    cap = ["-maxrate", "10M", "-bufsize", "20M"]
    return encoder_args(encoder, quality) + cap + (["-profile:v", "high"] if encoder == "libx264" else [])


def describe(timeline: Timeline) -> dict:
    return {"duration": timeline.duration, "scenes": len(timeline.bounds) + 1, "transition_s": timeline.overlap,
            "transitions": sorted(set(timeline.transitions))}


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, default=str)

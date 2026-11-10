"""ffmpeg discovery, process helpers, media probing and encoder detection."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

# Keep ffmpeg from flashing a console window when running from the GUI .exe.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@lru_cache(maxsize=None)
def ffmpeg_exe() -> str:
    """The ffmpeg to run: the one the app brings (ClipMonster for Mac's, in ClipMonster.app; the PC app's is
    found on its PATH), else an installed one, else imageio-ffmpeg's."""
    if getattr(sys, "frozen", False):
        from . import bundled

        own = bundled.find("ffmpeg", "ffmpeg")
        if own is not None:
            return str(own)
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - only hit on broken installs
        raise RuntimeError(
            "ffmpeg was not found. Install it (e.g. `winget install Gyan.FFmpeg`) "
            "or `pip install imageio-ffmpeg`."
        ) from exc


# Every ffmpeg this process has running, so automation's watchdog can stop one that's stuck.
_live: set[subprocess.Popen] = set()
_live_lock = threading.Lock()


_killed: set[subprocess.Popen] = set()  # processes kill_running() stopped, while still tracked


class Cancelled(Exception):
    """You (or automation's Stop) cancelled the work part way. engine.Cancelled is this."""


class Stopped(RuntimeError):
    """kill_running() stopped this ffmpeg (automation's watchdog: it had stopped making progress)."""


@contextmanager
def tracked(proc: subprocess.Popen):
    """Keep `proc` on the list kill_running() stops while the block runs. If it was stopped that
    way, the block ends in Stopped, so half an answer (half a measurement) is never used."""
    with _live_lock:
        _live.add(proc)
    try:
        yield proc
    finally:
        with _live_lock:
            _live.discard(proc)
            killed = proc in _killed
            _killed.discard(proc)
    if killed:
        raise Stopped("ffmpeg was stopped because it had stopped making progress")


def kill_running() -> int:
    """Stop every ffmpeg this process is running (the call waiting on it then fails). How many."""
    with _live_lock:
        procs = list(_live)
    n = 0
    for proc in procs:
        if proc.poll() is None:
            try:
                with _live_lock:
                    if proc not in _live:  # finished meanwhile
                        continue
                    _killed.add(proc)
                proc.kill()
                n += 1
            except OSError:
                pass
    return n


@lru_cache(maxsize=4)
def major_version(exe: str) -> int:
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             creationflags=NO_WINDOW, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r"ffmpeg version n?(\d+)\.", out)
    return int(m[1]) if m else 0  # a git build ("N-1234-g...") counts as old: it takes the older spelling too


def graph_file_args(script: str) -> list[str]:
    """How this ffmpeg is told to read its filter graph from a file (ffmpeg 7 renamed the option, and later
    dropped the old spelling). A graph in a file never meets the command line's length limit."""
    return ["-/filter_complex", script] if major_version(ffmpeg_exe()) >= 7 else ["-filter_complex_script", script]


# Windows gives a program at most 32,767 characters of command line (CreateProcess).
WINDOWS_COMMAND_LIMIT = 32_000


def command_too_long(cmd: list[str], windows: bool | None = None) -> bool:
    """Whether Windows would refuse to start this command for its length."""
    windows = sys.platform == "win32" if windows is None else windows
    return windows and len(subprocess.list2cmdline([str(c) for c in cmd])) > WINDOWS_COMMAND_LIMIT


def communicate(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """subprocess.run for ffmpeg (text output), tracked for the watchdog."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    with tracked(proc):
        out, err = proc.communicate()
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    result = communicate(cmd, cwd)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        raise RuntimeError(f"ffmpeg failed:\n{tail}")
    return result


def close_pipes(proc: subprocess.Popen) -> None:
    """Close whichever of the process's pipes are open (a pipe left to the garbage collector holds a handle
    until then, and on Windows keeps the file it wrote locked)."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):  # a broken pipe to a process that's gone
                pass


def discard(*paths) -> int:
    """Delete intermediate files (scene clips, a mix, a narrator track) once what was made from them is
    written, so a long render doesn't hold several GB of them until its folder goes. A file another program
    still has open is left for the folder's own cleanup. Returns how many went."""
    n = 0
    for p in paths:
        if p is None:
            continue
        try:
            Path(p).unlink()
            n += 1
        except FileNotFoundError:
            pass
        except OSError:  # (Windows: still open somewhere)
            pass
    return n


def stop(proc: subprocess.Popen, wait: float = 10.0) -> None:
    """Kill the process if it's still running and reap it, so no ffmpeg is left working in the background."""
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        pass


def run_with_progress(cmd: list[str], duration: float, on_progress: Callable[[float], None] | None,
                      cwd: Path | None = None, on_stderr: Callable[[str], None] | None = None) -> None:
    """Run ffmpeg with `-progress pipe:1`, reporting 0-1 completion as it goes. Its pipes are closed and the
    process reaped however it ends; cancelled or failed part way, ffmpeg is stopped first."""
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    err_tail: list[str] = []

    def pump_stderr() -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                if on_stderr:
                    on_stderr(line)
                err_tail.append(line.rstrip())
                del err_tail[:-8]
        except (OSError, ValueError):  # closed under it
            pass

    t = threading.Thread(target=pump_stderr, daemon=True)
    t.start()
    assert proc.stdout is not None
    try:
        with tracked(proc):
            for line in proc.stdout:
                if on_progress and duration > 0 and line.startswith("out_time_us="):
                    try:
                        on_progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / duration))
                    except ValueError:
                        pass
            proc.wait()
    except BaseException:
        # Cancelled (or anything else) mid-run: don't leave ffmpeg working in the background.
        stop(proc)
        raise
    finally:
        t.join(timeout=2)
        close_pipes(proc)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(err_tail[-6:]))


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 30.0
    has_audio: bool = True
    codec: str = ""  # the video stream's: "h264"
    profile: str = ""  # "High", "Main" (as ffmpeg names it)
    pix_fmt: str = ""  # "yuv420p"


def probe(path: Path) -> MediaInfo:
    """Read basic stream info from `ffmpeg -i` (imageio-ffmpeg ships no ffprobe)."""
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                         creationflags=NO_WINDOW).stderr
    info = MediaInfo(has_audio="Audio:" in out)
    if m := re.search(r"Duration: (\d+):(\d+):([\d.]+)", out):
        info.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    for line in out.splitlines():
        if "Video:" in line:
            if m := re.search(r", (\d{2,5})x(\d{2,5})", line):
                info.width, info.height = int(m[1]), int(m[2])
            if m := re.search(r"([\d.]+) fps", line):
                info.fps = float(m[1])
            if m := re.search(r"Video: (\w+)(?: \(([^)]*)\))?", line):
                info.codec, info.profile = m[1], (m[2] or "") if m[2] and "/" not in m[2] else ""
            if m := re.search(r", (yuv\w+|nv12|rgb\w+|bgr\w+|gray\w*)[(,]", line):
                info.pix_fmt = m[1]
            break
    return info


HW_ENCODERS = ("h264_nvenc", "h264_amf", "h264_qsv")
if sys.platform == "darwin":  # the Mac's media engine: every Mac since 2011 has one
    HW_ENCODERS = ("h264_videotoolbox",)
# Apple silicon's media engine takes a constant quality; an Intel Mac's only a bit rate.
VT_QUALITY = sys.platform == "darwin" and platform.machine() == "arm64"


@lru_cache(maxsize=None)
def encoder_works(name: str) -> bool:
    """Whether this ffmpeg can encode with `name` here, tried with the very options an export uses."""
    try:
        subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                        *encoder_args(name, 23), "-pix_fmt", "yuv420p", "-f", "null", "-"], check=True,
                       capture_output=True, timeout=20, creationflags=NO_WINDOW)
        return True
    except Exception:
        return False


def pick_encoder(preference: str) -> str:
    if preference and preference not in ("auto", "libx264"):
        return preference if encoder_works(preference) else "libx264"
    if preference == "libx264":
        return "libx264"
    return next((e for e in HW_ENCODERS if encoder_works(e)), "libx264")


def encoder_args(encoder: str, quality: int) -> list[str]:
    """Map one 'lower is better' quality number (~CRF) onto each encoder's own knobs."""
    q = str(quality)
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", q, "-b:v", "0"]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                "-qp_i", q, "-qp_p", str(quality + 2), "-qp_b", str(quality + 4)]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", q]
    if encoder == "h264_videotoolbox":
        if VT_QUALITY:  # -q:v runs 1-100, higher is better: CRF 18 is about 65, 23 about 52, 28 about 40
            return ["-c:v", "h264_videotoolbox", "-q:v", str(round(min(85, max(30, 110 - 2.5 * quality)))),
                    "-allow_sw", "1"]
        rate = round(max(2.0, min(24.0, 12.0 * 0.86 ** (quality - 20))), 1)  # Mbit/s for 1080p: CRF 20 is 12
        return ["-c:v", "h264_videotoolbox", "-b:v", f"{rate}M", "-maxrate", f"{rate * 1.5:.1f}M",
                "-bufsize", f"{rate * 2:.0f}M", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", q]


# ---------------------------------------------------------------- one output, whichever encoder makes it
#
# A video ClipMonster makes should come out the same whether the Mac's media engine (VideoToolbox), an NVIDIA
# (NVENC), AMD (AMF) or Intel (Quick Sync) card or the processor (x264) encoded it: H.264 High, 4:2:0, a
# keyframe every two seconds, a constant frame rate and, for a finished video, the same bit rate ceiling.
# video_args() gives each encoder's own knobs for that; DELIVERY is what every finished video is checked
# against (delivery_problems), so a card's encoder that drifts from it shows up in the video's check.

PURPOSES = ("final", "intermediate", "delivery")
SCENE_QP = 16  # an intermediate clip on a card's encoder: near-lossless (it's encoded once more, at the end)
DELIVERY_RATE, DELIVERY_PEAK = 8, 10  # Mbit/s: plenty for 1080x1920 at 30 fps (TikTok encodes again anyway)
DELIVERY = {"codec": "h264", "profile": "High", "pix_fmt": "yuv420p"}


def _intermediate_args(encoder: str) -> list[str]:
    q = str(SCENE_QP)
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", "speed", "-rc", "cqp",
                "-qp_i", q, "-qp_p", q, "-bf", "0"]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p2", "-rc", "constqp", "-qp", q, "-bf", "0"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", q]
    if encoder == "h264_videotoolbox":  # (before, scenes on a Mac fell back to x264)
        if VT_QUALITY:
            return ["-c:v", "h264_videotoolbox", "-q:v", "90", "-allow_sw", "1"]
        return ["-c:v", "h264_videotoolbox", "-b:v", "40M", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14"]


def _delivery_args(encoder: str, quality: int) -> list[str]:
    """The encoder with the bit rate ceiling. AMF's and Quick Sync's quality modes can't be capped, and the
    Mac's media engine keeps to a bit rate better than to a cap, so those three use peak-limited VBR."""
    rate, peak, buf = f"{DELIVERY_RATE}M", f"{DELIVERY_PEAK}M", f"{2 * DELIVERY_PEAK}M"
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "vbr_peak", "-b:v", rate, "-maxrate", peak]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-b:v", rate, "-maxrate", peak]
    if encoder == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", rate, "-maxrate", peak, "-bufsize", buf, "-allow_sw", "1"]
    return encoder_args(encoder, quality) + ["-maxrate", peak, "-bufsize", buf]


def video_args(encoder: str, quality: int = 20, purpose: str = "final", fps: int = 30) -> list[str]:
    """The video encoder's arguments for `purpose`:

    final         a finished export (Settings → Export → Quality), no ceiling
    intermediate  a clip that's encoded again later (a Creator scene, a reel's play): near-lossless, fast
    delivery      a finished video with the bit rate ceiling (the Video Creator, recap reels)

    The final and delivery ones carry the same profile, keyframe spacing and frame rate on every encoder."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")
    if purpose == "intermediate":
        return _intermediate_args(encoder)
    args = _delivery_args(encoder, quality) if purpose == "delivery" else encoder_args(encoder, quality)
    return args + ["-profile:v", "high", "-g", str(2 * int(fps)), "-fps_mode", "cfr"]


def delivery_problems(info: MediaInfo, width: int = 1080, height: int = 1920, fps: float = 30.0) -> list[str]:
    """How a finished video differs from what every encoder should give (DELIVERY), in words; [] when it
    doesn't. Facts ffmpeg didn't print aren't held against it."""
    out = []
    if info.codec and info.codec != DELIVERY["codec"]:
        out.append(f"its picture is {info.codec}, not H.264")
    if info.profile and info.profile.lower() != DELIVERY["profile"].lower():
        out.append(f"its H.264 profile is {info.profile}, not High")
    if info.pix_fmt and info.pix_fmt != DELIVERY["pix_fmt"]:
        out.append(f"its colours are stored as {info.pix_fmt}, not yuv420p")
    if info.width and (info.width, info.height) != (width, height):
        out.append(f"it's {info.width}x{info.height}, not {width}x{height}")
    if info.fps and abs(info.fps - fps) > 0.05:
        out.append(f"it runs at {info.fps:g} fps, not {fps:g}")
    return out

"""ffmpeg discovery, process helpers, media probing and encoder detection."""

from __future__ import annotations

import re
import shutil
import subprocess
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


def run_with_progress(cmd: list[str], duration: float, on_progress: Callable[[float], None] | None,
                      cwd: Path | None = None, on_stderr: Callable[[str], None] | None = None) -> None:
    """Run ffmpeg with `-progress pipe:1`, reporting 0-1 completion as it goes."""
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    err_tail: list[str] = []

    def pump_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if on_stderr:
                on_stderr(line)
            err_tail.append(line.rstrip())
            del err_tail[:-8]

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
        proc.kill()
        proc.wait()
        raise
    proc.wait()
    t.join(timeout=2)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(err_tail[-6:]))


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 30.0
    has_audio: bool = True


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
            break
    return info


HW_ENCODERS = ("h264_nvenc", "h264_amf", "h264_qsv")


@lru_cache(maxsize=None)
def encoder_works(name: str) -> bool:
    try:
        subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                        "-c:v", name, "-f", "null", "-"], check=True, capture_output=True, timeout=20,
                       creationflags=NO_WINDOW)
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
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", q]

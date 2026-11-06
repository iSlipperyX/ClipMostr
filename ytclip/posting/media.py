"""ffmpeg discovery, probing, and the single analysis pass the scorer works from."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

# Keep ffmpeg from flashing a console window when running from the windowed .exe.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}

# Analysis runs on a small, low-fps copy of the picture: plenty for motion and cut detection.
ANALYSIS_FPS = 10
ANALYSIS_WIDTH = 192
AUDIO_WINDOW = 0.1  # seconds per loudness sample


@lru_cache(maxsize=None)
def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - only hit on broken installs
        raise RuntimeError("ffmpeg was not found. Install it with `winget install Gyan.FFmpeg`.") from exc


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if check and result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        raise RuntimeError(f"ffmpeg failed:\n{tail}")
    return result


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 30.0
    has_audio: bool = False
    bitrate_kbps: float = 0.0
    video_codec: str = ""
    size_bytes: int = 0


def probe(path: Path) -> MediaInfo:
    """Read basic stream info from `ffmpeg -i` (imageio-ffmpeg ships no ffprobe)."""
    out = _run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], check=False).stderr
    if "Invalid data found" in out or "No such file" in out:
        raise RuntimeError(f"Not a readable video: {path.name}")
    info = MediaInfo(has_audio=bool(re.search(r"Stream #\S+.*Audio:", out)))
    if m := re.search(r"Duration: (\d+):(\d+):([\d.]+)", out):
        info.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    if m := re.search(r"bitrate: (\d+) kb/s", out):
        info.bitrate_kbps = float(m[1])
    for line in out.splitlines():
        if "Video:" in line:
            if m := re.search(r"Video: (\w+)", line):
                info.video_codec = m[1]
            if m := re.search(r", (\d{2,5})x(\d{2,5})", line):
                info.width, info.height = int(m[1]), int(m[2])
            if m := re.search(r"([\d.]+) fps", line):
                info.fps = float(m[1])
            break
    # Phones store portrait video as landscape + a rotation flag.
    if re.search(r"rotate\s*:\s*-?(90|270)", out) or re.search(r"rotation of -?(90|270)", out):
        info.width, info.height = info.height, info.width
    try:
        info.size_bytes = path.stat().st_size
    except OSError:
        pass
    return info


@dataclass
class Analysis:
    """Raw measurements from one decode of the file. Scoring turns these into 0-100 marks."""

    info: MediaInfo = field(default_factory=MediaInfo)
    # Per-frame series at ANALYSIS_FPS
    motion: list[float] = field(default_factory=list)  # scdet mean absolute frame difference
    cut_times: list[float] = field(default_factory=list)
    brightness: list[float] = field(default_factory=list)  # YAVG 0-255
    contrast: list[float] = field(default_factory=list)  # YHIGH - YLOW
    saturation: list[float] = field(default_factory=list)
    blur: list[float] = field(default_factory=list)
    # Audio
    rms_db: list[float] = field(default_factory=list)  # one per AUDIO_WINDOW
    loudness_lufs: float | None = None
    true_peak_db: float | None = None
    silences: list[tuple[float, float]] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        # Series are only needed while scoring; keep the stored library small.
        for k in ("motion", "brightness", "contrast", "saturation", "blur", "rms_db"):
            d.pop(k)
        return d


def _parse_metadata_file(path: Path) -> list[dict[str, float]]:
    frames: list[dict[str, float]] = []
    if not path.exists():
        return frames
    cur: dict[str, float] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("frame:"):
            m = re.search(r"pts_time:([-\d.e]+)", line)
            cur = {"t": float(m[1]) if m else 0.0}
            frames.append(cur)
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            try:
                cur[k.strip()] = float(v)
            except ValueError:
                pass  # "-inf" for silent windows parses fine; anything else is skipped
    return frames


def analyze(path: Path, info: MediaInfo | None = None) -> Analysis:
    info = info or probe(path)
    a = Analysis(info=info)
    with tempfile.TemporaryDirectory(prefix="tokmanager_") as tmp:
        tmpd = Path(tmp)
        graph = [
            f"[0:v]fps={ANALYSIS_FPS},scale={ANALYSIS_WIDTH}:-2,scdet=threshold=12,signalstats,"
            f"blurdetect=block_width=32:block_height=32,metadata=print:file=video.txt[vout]"
        ]
        maps = ["-map", "[vout]", "-f", "null", "-"]
        if info.has_audio:
            # ffmpeg reads the metadata files relative to its working directory, which
            # sidesteps Windows drive-letter escaping inside filter arguments.
            graph.append(
                f"[0:a]aresample=16000,aformat=channel_layouts=mono,asplit=2[a1][a2];"
                f"[a1]ebur128=peak=true[aloud];"
                f"[a2]asetnsamples=n={int(16000 * AUDIO_WINDOW)}:p=0,astats=metadata=1:reset=1,"
                f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file=audio.txt,"
                f"silencedetect=n=-40dB:d=0.5[asil]"
            )
            maps += ["-map", "[aloud]", "-f", "null", "-", "-map", "[asil]", "-f", "null", "-"]
        cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path.resolve()),
               "-filter_complex", ";".join(graph), *maps]
        stderr = _run(cmd, cwd=tmpd).stderr

        for f in _parse_metadata_file(tmpd / "video.txt"):
            a.motion.append(f.get("lavfi.scd.mafd", 0.0))
            if "lavfi.scd.time" in f:
                a.cut_times.append(f["lavfi.scd.time"])
            if "lavfi.signalstats.YAVG" in f:
                a.brightness.append(f["lavfi.signalstats.YAVG"])
                a.contrast.append(f.get("lavfi.signalstats.YHIGH", 0) - f.get("lavfi.signalstats.YLOW", 0))
                a.saturation.append(f.get("lavfi.signalstats.SATAVG", 0.0))
            if "lavfi.blur" in f:
                a.blur.append(f["lavfi.blur"])
        if info.has_audio:
            a.rms_db = [max(-90.0, f.get("lavfi.astats.Overall.RMS_level", -90.0))
                        for f in _parse_metadata_file(tmpd / "audio.txt")]

    if info.has_audio:
        # ebur128 prints a summary block at the end; take the last values it reports.
        if m := re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", stderr):
            a.loudness_lufs = None if m[-1] == "-inf" else float(m[-1])
        if m := re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", stderr):
            a.true_peak_db = None if m[-1] == "-inf" else float(m[-1])
        start = None
        for line in stderr.splitlines():
            if m := re.search(r"silence_start: (-?[\d.]+)", line):
                start = max(0.0, float(m[1]))
            elif (m := re.search(r"silence_end: ([\d.]+)", line)) and start is not None:
                a.silences.append((start, float(m[1])))
                start = None
        if start is not None:  # silent right to the end
            a.silences.append((start, info.duration))
    return a


def thumbnail(src: Path, dest: Path, at: float) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", str(src),
          "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(dest)])
    return dest


def sample_frames(src: Path, duration: float, count: int, width: int = 512) -> list[bytes]:
    """JPEG frames spread across the video (first one at 0.5 s, to judge the hook)."""
    if duration <= 0:
        return []
    times = [min(0.5, duration / 2)] + [duration * (i + 1) / (count + 1) for i in range(count - 1)]
    frames: list[bytes] = []
    for t in times:
        out = subprocess.run([ffmpeg_exe(), "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
                              "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        if out.returncode == 0 and out.stdout:
            frames.append(out.stdout)
    return frames


def blank_clip(path: Path) -> Path:
    """A 3 s black 9:16 clip with silent sound, for checking TikTok's uploader without posting a real video."""
    if path.is_file() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".part" + path.suffix)
    _run([ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30:d=3",
          "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(tmp)])
    tmp.replace(path)
    return path

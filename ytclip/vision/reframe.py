"""Render a clip's picture through the virtual camera: cuts, crop, zooms and B-roll cutaways, frame by frame.

ffmpeg decodes the clip's frames into this process; each kept frame is cropped by the camera's window for
it with sub-pixel precision (an affine warp, so slow moves glide instead of stepping a pixel at a time),
B-roll is cut or faded in where planned, and the frames go straight to a second ffmpeg that encodes them
almost losslessly. That file stands in for the source in the export (export.py), which adds the layout,
captions and sound exactly as before. Frames inside a cut (silence_remover.py) are never written, and the
cuts land on frame edges, so the sound, cut the same way, stays in step.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..ffmpeg import NO_WINDOW, Cancelled, ffmpeg_exe, tracked
from .broll_selector import Insert
from .camera import Window

Progress = Callable[[float], None]
FADE_FRAMES = 5  # B-roll fades in and out over this many frames


@dataclass
class Job:
    src: Path
    start: float  # source seconds the clip starts at
    length: float  # source seconds it covers
    fps: float
    out_w: int
    out_h: int
    windows: Callable[[int, float], Window]  # (output frame number, source time) -> the crop for it
    keep: list[tuple[float, float]] = field(default_factory=list)  # source spans kept (absolute); [] = all
    broll: list[Insert] = field(default_factory=list)  # in output seconds
    decode_size: tuple[int, int] = (0, 0)  # (0, 0) = the source's own size


def kept(t: float, keep: list[tuple[float, float]]) -> bool:
    return not keep or any(a - 1e-6 <= t < b - 1e-6 for a, b in keep)


def _decoder(src: Path, start: float, length: float, fps: float, size: tuple[int, int], loop: bool = False,
             cover: tuple[int, int] | None = None) -> subprocess.Popen:
    vf = [f"fps={fps:.6f}"]
    if cover:
        w, h = cover
        vf.append(f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}")
    elif size != (0, 0):
        vf.append(f"scale={size[0]}:{size[1]}")
    vf.append("setsar=1")
    cmd = [ffmpeg_exe(), "-v", "error"] + (["-stream_loop", "-1"] if loop else []) + [
        "-ss", f"{max(0.0, start):.3f}", "-i", str(src), "-t", f"{length:.3f}", "-an", "-sn", "-vf", ",".join(vf),
        "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW)


def _encoder(dest: Path, w: int, h: int, fps: float) -> subprocess.Popen:
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
           "-r", f"{fps:.6f}", "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dest)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW)


class _Tail:
    """The last lines a program wrote to stderr, read on a thread so it never blocks."""

    def __init__(self, proc: subprocess.Popen):
        self.lines: list[str] = []
        self.t = threading.Thread(target=self._read, args=(proc,), daemon=True)
        self.t.start()

    def _read(self, proc: subprocess.Popen) -> None:
        for raw in proc.stderr or []:
            self.lines.append(raw.decode("utf-8", "replace").rstrip())
            del self.lines[:-8]

    def text(self) -> str:
        return " ".join(self.lines[-3:])


def crop(frame: np.ndarray, win: Window, out_w: int, out_h: int) -> np.ndarray:
    """The window of `frame`, scaled to out_w x out_h, with sub-pixel placement."""
    import cv2

    sx, sy = out_w / win.w, out_h / win.h
    m = np.array([[sx, 0, -win.x * sx], [0, sy, -win.y * sy]], dtype=np.float32)
    interp = cv2.INTER_AREA if sx < 0.7 else cv2.INTER_LINEAR
    return cv2.warpAffine(frame, m, (out_w, out_h), flags=interp, borderMode=cv2.BORDER_REPLICATE)


def render(job: Job, dest: Path, progress: Progress | None = None, frames_at: list[float] | None = None) -> int:
    """Write the clip's picture to `dest`. Returns how many frames were written.

    With `frames_at` (output seconds), only those frames are made, as dest-<n>.png, for previews."""
    import cv2

    sw, sh = job.decode_size
    if (sw, sh) == (0, 0):
        from ..ffmpeg import probe

        info = probe(job.src)
        sw, sh = info.width or 1920, info.height or 1080
    frame_bytes = sw * sh * 3
    dec = _decoder(job.src, job.start, job.length, job.fps, (sw, sh))
    dec_tail = _Tail(dec)
    enc = None if frames_at is not None else _encoder(dest, job.out_w, job.out_h, job.fps)
    enc_tail = _Tail(enc) if enc else None
    wanted = sorted(round(t * job.fps) for t in frames_at) if frames_at is not None else []
    total_out = max(1, int(round(sum(b - a for a, b in job.keep) * job.fps)) if job.keep
                    else int(job.length * job.fps))
    broll_dec: dict[int, tuple[subprocess.Popen, int]] = {}
    written = 0
    k = 0
    try:
        with tracked(dec):
            assert dec.stdout is not None
            while True:
                buf = dec.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                t_src = job.start + k / job.fps
                k += 1
                if not kept(t_src, job.keep):
                    continue
                n = written
                written += 1
                if frames_at is not None and n not in wanted:
                    if not wanted or n > wanted[-1]:
                        dec.kill()  # the frames asked for are made: stop reading (it would block on a full pipe)
                        break
                    continue
                frame = np.frombuffer(buf, np.uint8).reshape(sh, sw, 3)
                out = crop(frame, job.windows(n, t_src), job.out_w, job.out_h)
                out = _with_broll(out, n, job, broll_dec)
                if frames_at is not None:
                    cv2.imwrite(str(dest.with_name(f"{dest.stem}-{wanted.index(n)}.png")), out)
                    if n >= wanted[-1]:
                        dec.kill()
                        break
                    continue
                try:
                    enc.stdin.write(out.tobytes())  # type: ignore[union-attr]
                except (BrokenPipeError, OSError) as exc:
                    raise RuntimeError(f"ffmpeg stopped taking frames: {enc_tail.text() if enc_tail else exc}") from exc
                if progress and n % 15 == 0:
                    progress(min(1.0, n / total_out))  # raises Cancelled to stop
            dec.wait()
        if dec.returncode not in (0, None) and written == 0 and frames_at is None:
            raise RuntimeError(f"ffmpeg couldn't read the video: {dec_tail.text()}")
    except BaseException:
        for proc in [dec, enc] + [p for p, _ in broll_dec.values()]:
            if proc is not None and proc.poll() is None:
                proc.kill()
        raise
    finally:
        for proc, _ in broll_dec.values():
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    if enc is not None:
        enc.stdin.close()  # type: ignore[union-attr]
        enc.wait()
        if enc.returncode != 0:
            raise RuntimeError(f"ffmpeg couldn't encode the reframed video: {enc_tail.text() if enc_tail else ''}")
    if progress:
        progress(1.0)
    return written


def _with_broll(out: np.ndarray, n: int, job: Job, decoders: dict) -> np.ndarray:
    """Cut or fade to B-roll during an insert."""
    for i, ins in enumerate(job.broll):
        first, last = int(round(ins.start * job.fps)), int(round(ins.end * job.fps))
        if not first <= n < last:
            continue
        if i not in decoders:
            proc = _decoder(ins.path, ins.offset, (last - first) / job.fps + 1.0, job.fps, (0, 0), loop=True,
                            cover=(job.out_w, job.out_h))
            decoders[i] = (proc, first)
        proc, _ = decoders[i]
        buf = proc.stdout.read(job.out_w * job.out_h * 3) if proc.stdout else b""
        if len(buf) < job.out_w * job.out_h * 3:
            return out  # the clip ran out or couldn't be read: stay on the speaker
        b = np.frombuffer(buf, np.uint8).reshape(job.out_h, job.out_w, 3)
        edge = min(n - first, last - 1 - n)
        if edge >= FADE_FRAMES:
            return b
        a = (edge + 1) / (FADE_FRAMES + 1)
        import cv2

        return cv2.addWeighted(b, a, out, 1 - a, 0)
    return out

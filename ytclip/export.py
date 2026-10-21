"""Render clips: trimming, reframing (vertical/square), captions, audio polish, encoding."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable

from .analyze import Clip
from .captions import build_ass
from .ffmpeg import MediaInfo, encoder_args, ffmpeg_exe, pick_encoder, run, run_with_progress
from .settings import Settings
from .subtitles import Word


def output_size(aspect: str, res: int, src: MediaInfo) -> tuple[int, int]:
    """Output frame size; `res` is the short side."""
    even = lambda v: int(v) // 2 * 2  # noqa: E731
    if aspect in ("vertical_blur", "vertical_crop"):
        return even(res), even(res * 16 / 9)
    if aspect == "square":
        return even(res), even(res)
    w, h = src.width or 1920, src.height or 1080
    if min(w, h) > res:  # only ever scale down
        k = res / min(w, h)
        w, h = w * k, h * k
    return even(w), even(h)


def _video_filter(aspect: str, w: int, h: int, crop_x: float) -> str:
    cx = min(1.0, max(0.0, crop_x))
    if aspect == "vertical_crop":
        return (f"crop='min(iw,ih*9/16)':ih:'(iw-min(iw,ih*9/16))*{cx:.3f}':0,"
                f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if aspect == "square":
        return (f"crop='min(iw,ih)':'min(iw,ih)':'(iw-min(iw,ih))*{cx:.3f}':'(ih-min(iw,ih))/2',"
                f"scale={w}:{h},setsar=1")
    if aspect == "vertical_blur":
        # Blurred, zoomed copy fills the frame; the full-width original sits on top.
        return (f"split[bg][fg];"
                f"[bg]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,crop={w // 4}:{h // 4},"
                f"gblur=sigma=6,scale={w}:{h},eq=brightness=-0.08[bgb];"
                f"[fg]scale={w}:-2[fgs];[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1")
    return f"scale={w}:{h},setsar=1"


def export_clip(src: Path, src_info: MediaInfo, dest: Path, clip: Clip, words: list[Word], s: Settings,
                progress: Callable[[float], None] | None = None) -> None:
    dur = clip.length
    reframed = s.aspect != "original"
    overlay = (s.burn_captions and words) or s.title_overlay
    if s.fast_copy and not reframed and not overlay and not s.normalize_audio:
        run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{clip.start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
             "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(dest)])
        if progress:
            progress(1.0)
        return

    w, h = output_size(s.aspect, s.resolution, src_info)
    vf = _video_filter(s.aspect, w, h, clip.crop_x)

    with tempfile.TemporaryDirectory(prefix="clipmonster_") as tmp:
        if overlay:
            # libass reads the file relative to ffmpeg's working directory, which
            # sidesteps Windows drive-letter escaping in filter arguments.
            Path(tmp, "captions.ass").write_text(
                build_ass(words, clip.start, clip.end, w, h, s, clip.title), encoding="utf-8")
            vf += ",subtitles=captions.ass"

        af: list[str] = []
        if s.normalize_audio:
            af.append("loudnorm=I=-14:TP=-1.5:LRA=11")
        if s.fades:
            af.append(f"afade=t=in:d=0.15,afade=t=out:st={max(0.0, dur - 0.3):.3f}:d=0.3")

        cmd = [ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{clip.start:.3f}", "-i", str(src.resolve()),
               "-t", f"{dur:.3f}"]
        cmd += ["-filter_complex" if ";" in vf else "-vf", vf]
        cmd += encoder_args(pick_encoder(s.encoder), s.video_quality) + ["-pix_fmt", "yuv420p"]
        if src_info.has_audio:
            cmd += ["-c:a", "aac", "-b:a", f"{s.audio_bitrate}k", "-ar", "48000"]
            if af:
                cmd += ["-af", ",".join(af)]
        cmd += ["-movflags", "+faststart", str(dest.resolve())]
        run_with_progress(cmd, dur, progress, cwd=Path(tmp))


def thumbnail(src: Path, dest: Path, at: float) -> None:
    run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{at:.3f}", "-i", str(src),
         "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(dest)])

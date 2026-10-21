"""Render clips: trimming, reframing (vertical/square), captions, audio polish, encoding."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable

from . import fonts, layout
from .analyze import Clip
from .captions import build_ass
from .ffmpeg import MediaInfo, encoder_args, ffmpeg_exe, pick_encoder, probe, run, run_with_progress
from .layout import Box, Plan
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


def _fill(label_in: str, label_out: str, box: Box, fit: str, crop_x: float, bg: str) -> str:
    """Filter chain that makes `label_in` exactly box.w x box.h."""
    w, h = box.w, box.h
    if fit == "crop":
        cx = min(1.0, max(0.0, crop_x))
        return (f"[{label_in}]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h}:'(iw-{w})*{cx:.3f}':'(ih-{h})/2',setsar=1[{label_out}]")
    if fit == "fit":
        return (f"[{label_in}]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={bg},setsar=1[{label_out}]")
    bw, bh = max(2, w // 4 // 2 * 2), max(2, h // 4 // 2 * 2)
    return (f"[{label_in}]split[{label_out}_a][{label_out}_b];"
            f"[{label_out}_a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"gblur=sigma=6,scale={w}:{h},eq=brightness=-0.08[{label_out}_bg];"
            f"[{label_out}_b]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2[{label_out}_fg];"
            f"[{label_out}_bg][{label_out}_fg]overlay=(W-w)/2:(H-h)/2,setsar=1[{label_out}]")


def layout_graph(p: Plan, fps: float, dur: float, crop_x: float, bg: str,
                 has_second: bool) -> tuple[str, str]:
    """The video half of -filter_complex for a layout. Input 0 is the clip, input 1 the stacked video."""
    colour = "#" + bg.lstrip("#")
    parts = [f"color=c={colour}:s={p.width}x{p.height}:r={fps:.3f}:d={dur:.3f}[base]",
             _fill("0:v", "main", p.main, p.fit, crop_x, colour),
             f"[base][main]overlay={p.main.x}:{p.main.y}:eof_action=pass[l1]"]
    last = "l1"
    if p.second is not None and has_second:
        parts.append(_fill("1:v", "second", p.second, "crop", 0.5, colour))
        parts.append(f"[{last}][second]overlay={p.second.x}:{p.second.y}:eof_action=pass[l2]")
        last = "l2"
    return ";".join(parts), last


def _audio_filters(s: Settings, dur: float) -> list[str]:
    af: list[str] = []
    if s.normalize_audio:
        af.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    if s.fades:
        af.append(f"afade=t=in:d=0.15,afade=t=out:st={max(0.0, dur - 0.3):.3f}:d=0.3")
    return af


def export_clip(src: Path, src_info: MediaInfo, dest: Path, clip: Clip, words: list[Word], s: Settings,
                progress: Callable[[float], None] | None = None, still_at: float | None = None) -> None:
    """Render one clip. With `still_at` (seconds into the clip), write a single JPEG frame instead:
    the layout and captions are drawn exactly as the export would draw them."""
    dur = clip.length
    reframed = s.aspect != "original"
    overlay = (s.burn_captions and words) or s.title_overlay
    use_layout = layout.active(s)
    if (s.fast_copy and not reframed and not overlay and not s.normalize_audio and not use_layout
            and still_at is None):
        run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{clip.start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
             "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(dest)])
        if progress:
            progress(1.0)
        return

    aspect = layout.frame_aspect(s) if use_layout else s.aspect
    res = max(s.resolution, TIKTOK_MIN_SHORT_SIDE) if s.tiktok_ready else s.resolution
    w, h = output_size(aspect, res, src_info)
    fps = min(src_info.fps or 30.0, TIKTOK_MAX_FPS) if s.tiktok_ready else (src_info.fps or 30.0)
    seek = clip.start if still_at is None else clip.start + min(max(0.0, still_at), max(0.0, dur - 0.05))
    length = dur if still_at is None else 0.5

    with tempfile.TemporaryDirectory(prefix="clipmonster_") as tmp:
        cmd = [ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{seek:.3f}", "-i", str(src.resolve()),
               "-t", f"{length:.3f}"]
        second_audio = False
        area, side = None, 0
        font_names = [s.caption_font] if overlay else []
        if use_layout or layout.uses_safe_zone(s, w, h):
            p = layout.plan(s, w, h)
            area, side = p.caption_area, p.side_margin
        if use_layout:
            has_second = p.second is not None
            if has_second:
                second = Path(s.stack_video).expanduser() if s.stack_video else None
                if second is None or not second.is_file():
                    raise RuntimeError("Stacked layout is on but the second video is missing. "
                                       "Choose one under Layout → Second video.")
                info2 = probe(second)
                offset = layout.second_offset(clip.start, dur, info2.duration, s.stack_start, s.stack_offset)
                if still_at is not None and info2.duration:
                    offset = (offset + still_at) % info2.duration
                cmd += ["-stream_loop", "-1", "-ss", f"{offset:.3f}", "-i", str(second.resolve()),
                        "-t", f"{length:.3f}"]
                second_audio = info2.has_audio and s.stack_volume > 0 and src_info.has_audio
            graph, last = layout_graph(p, fps, length, clip.crop_x, s.banner_bg, has_second)
            top = layout.banner_text(s.banner_top_text, clip.title, clip.top_text) if p.top else ""
            bottom = layout.banner_text(s.banner_bottom_text, clip.title, clip.bottom_text) if p.bottom else ""
            if top or bottom:
                Path(tmp, "banners.ass").write_text(layout.banner_ass(p, s, top, bottom), encoding="utf-8")
                graph += f";[{last}]subtitles=banners.ass{_fontsdir([s.banner_font], tmp)}[lb]"
                last = "lb"
        else:
            graph, last = f"[0:v]{_video_filter(s.aspect, w, h, clip.crop_x)}[v0]", "v0"

        if overlay:
            # libass reads the file relative to ffmpeg's working directory, which
            # sidesteps Windows drive-letter escaping in filter arguments.
            Path(tmp, "captions.ass").write_text(
                build_ass(words, seek, clip.end, w, h, s, clip.title, area, side), encoding="utf-8")
            graph += f";[{last}]subtitles=captions.ass{_fontsdir(font_names, tmp)}[vc]"
            last = "vc"
        cap_fps = f"fps={TIKTOK_MAX_FPS}," if s.tiktok_ready and (src_info.fps or 0) > TIKTOK_MAX_FPS + 0.5 else ""
        graph += f";[{last}]{cap_fps}format=yuv420p[vout]"

        if still_at is not None:
            cmd += ["-filter_complex", graph, "-map", "[vout]", "-frames:v", "1", "-q:v", "3", str(dest.resolve())]
            run(cmd, cwd=Path(tmp))
            return

        if src_info.has_audio:
            af = _audio_filters(s, dur)
            if second_audio:
                graph += (f";[1:a]volume={s.stack_volume / 100:.2f}[a2];"
                          f"[0:a][a2]amix=inputs=2:duration=first:dropout_transition=0:normalize=0"
                          f"{',' + ','.join(af) if af else ''}[aout]")
            else:
                graph += f";[0:a]{','.join(af) or 'anull'}[aout]"
        cmd += ["-filter_complex", graph, "-map", "[vout]"]
        encoder = pick_encoder(s.encoder)
        cmd += encoder_args(encoder, s.video_quality) + ["-pix_fmt", "yuv420p"]
        if s.tiktok_ready:
            cmd += tiktok_rate_args(encoder)
        if src_info.has_audio:
            cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", f"{s.audio_bitrate}k",
                    "-ar", "44100" if s.tiktok_ready else "48000"]
        cmd += ["-t", f"{dur:.3f}", "-movflags", "+faststart", str(dest.resolve())]
        run_with_progress(cmd, dur, progress, cwd=Path(tmp))


def _fontsdir(names: list[str], tmp: str) -> str:
    """subtitles= option pointing libass at the bundled fonts this render uses (copied into tmp/fonts)."""
    return ":fontsdir=fonts" if fonts.stage(names, Path(tmp)) else ""


# TikTok's upload limits (checked 2026-10): 23-60 fps, 540x960 minimum, 10 minutes, 287.6 MB from the
# phone app (4 GB from a computer). Bitrate above ~20 Mbps only grows the file; TikTok re-encodes anyway.
TIKTOK_MAX_FPS = 60
TIKTOK_MIN_SHORT_SIDE = 540
TIKTOK_MAX_SECONDS = 10 * 60
TIKTOK_APP_MAX_BYTES = 287_600_000


def tiktok_rate_args(encoder: str) -> list[str]:
    """Cap the bitrate where the encoder's quality mode allows it (AMF's constant-QP and QSV's ICQ modes don't)."""
    if encoder == "libx264":
        return ["-maxrate", "20M", "-bufsize", "40M", "-profile:v", "high"]
    if encoder == "h264_nvenc":
        return ["-maxrate", "20M", "-bufsize", "40M"]
    return []


def tiktok_warnings(path: Path, duration: float) -> list[str]:
    """Anything about a finished clip that TikTok would refuse or that needs uploading from a computer."""
    out = []
    if duration > TIKTOK_MAX_SECONDS:
        out.append(f"{path.name} is {duration / 60:.1f} minutes; TikTok allows up to 10.")
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > TIKTOK_APP_MAX_BYTES:
        out.append(f"{path.name} is {size / 1e6:.0f} MB; the TikTok phone app takes up to 287 MB, "
                   "so upload it from a computer (up to 4 GB).")
    return out


def thumbnail(src: Path, dest: Path, at: float) -> None:
    run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{at:.3f}", "-i", str(src),
         "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(dest)])

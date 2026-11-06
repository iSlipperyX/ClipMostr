"""Turn an Analysis into a 0-100 TikTok readiness score with a breakdown and fix-it tips.

Every part scores 0-100 on its own; the total is a weighted average of the parts that
apply (no audio track skips nothing - it scores low; no ClipMonster/AI data simply
leaves those parts out).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from .config import Settings
from .media import ANALYSIS_FPS, AUDIO_WINDOW, Analysis

HOOK_SECONDS = 3.0

PART_LABELS = {
    "hook": "Hook (first 3 s)",
    "format": "Format",
    "duration": "Length",
    "pacing": "Pacing",
    "audio": "Audio",
    "visual": "Picture",
    "source": "Clip finder",
    "ai": "AI rating",
}


@dataclass
class Score:
    total: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)  # one-line explanation per part
    tips: list[str] = field(default_factory=list)

    @property
    def grade(self) -> str:
        return grade(self.total)

    def to_json(self) -> dict:
        return {"total": self.total, "parts": self.parts, "notes": self.notes, "tips": self.tips}

    @classmethod
    def from_json(cls, d: dict | None) -> "Score | None":
        if not d:
            return None
        return cls(total=d.get("total", 0.0), parts=d.get("parts", {}), notes=d.get("notes", {}),
                   tips=d.get("tips", []))


def grade(total: float) -> str:
    for cut, letter in ((90, "S"), (80, "A"), (68, "B"), (55, "C"), (40, "D")):
        if total >= cut:
            return letter
    return "F"


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def ramp(x: float, x0: float, x1: float, y0: float = 0.0, y1: float = 100.0) -> float:
    """Linear map of x from [x0, x1] onto [y0, y1], clamped at both ends."""
    if x1 == x0:
        return y1 if x >= x1 else y0
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    return statistics.fmean(xs) if xs else None


# --- individual parts --------------------------------------------------------------


def score_format(a: Analysis, tips: list[str]) -> tuple[float, str]:
    w, h = a.info.width, a.info.height
    if not w or not h:
        return 40.0, "unknown frame size"
    ratio = w / h
    if 0.54 <= ratio <= 0.59:
        shape, shape_name = 100.0, "9:16 vertical"
    elif ratio < 0.54:
        shape, shape_name = 85.0, "extra tall"
    elif ratio <= 0.85:
        shape, shape_name = 70.0, "4:5 / 3:4"
    elif ratio <= 1.1:
        shape, shape_name = 50.0, "square"
    else:
        shape, shape_name = 20.0, "landscape"
        tips.append("Landscape video shows tiny with black bars on TikTok: reframe it to 9:16 vertical "
                    "(ClipMonster's 'vertical' export does this).")
    if 50 <= shape < 100:
        tips.append("Vertical 9:16 fills the whole phone screen; consider reframing.")
    short = min(w, h)
    res = ramp(short, 480, 1080, 25, 100)
    if short < 720:
        tips.append(f"Resolution is low ({w}x{h}); export at least 720x1280, ideally 1080x1920.")
    fps = a.info.fps
    fps_pen = 15.0 if fps and fps < 23 else 0.0
    if fps_pen:
        tips.append(f"Frame rate is {fps:.0f} fps; TikTok expects 24-60 fps.")
    return clamp(shape * 0.7 + res * 0.3 - fps_pen), f"{shape_name}, {w}x{h}, {fps:.0f} fps"


def score_duration(a: Analysis, s: Settings, tips: list[str]) -> tuple[float, str]:
    d = a.info.duration
    lo, hi = s.ideal_min_len, s.ideal_max_len
    if d <= 0:
        return 0.0, "no duration"
    if lo <= d <= hi:
        v = 100.0
    elif d < lo:
        v = ramp(d, 3, lo, 35, 100)
    elif d <= 90:
        v = ramp(d, hi, 90, 100, 65)
    elif d <= 180:
        v = ramp(d, 90, 180, 65, 45)
    else:
        v = ramp(d, 180, 600, 45, 20)
    if d > 600:
        tips.append("Over 10 minutes: too long for most uploads and the official API.")
    elif d > hi * 1.5:
        tips.append(f"At {d:.0f}s it's long for TikTok; shorter cuts ({lo:.0f}-{hi:.0f}s) keep more viewers to the end.")
    return v, f"{d:.1f}s (sweet spot {lo:.0f}-{hi:.0f}s)"


def _window(series: list[float], fps: float, start: float, end: float) -> list[float]:
    return series[int(start * fps): max(int(start * fps) + 1, int(end * fps))]


def score_hook(a: Analysis, tips: list[str]) -> tuple[float, str]:
    """Does something happen immediately? Sound right away, energy and movement up front."""
    bits: list[str] = []
    parts: list[tuple[float, float]] = []  # (score, weight)

    if a.info.has_audio:
        lead_silence = 0.0
        for st, en in a.silences:
            if st <= 0.15:
                lead_silence = en
                break
        parts.append((ramp(lead_silence, 1.5, 0.0), 0.4))
        if lead_silence >= a.info.duration - 0.5:
            bits.append("silent throughout")
        elif lead_silence >= 0.4:
            tips.append(f"{lead_silence:.1f}s of silence at the start: trim it so the video opens on sound.")
            bits.append(f"{lead_silence:.1f}s silent start")
        else:
            bits.append("sound from the first frame")
        rate = 1 / AUDIO_WINDOW
        head = _mean([x for x in _window(a.rms_db, rate, 0, HOOK_SECONDS) if x > -89])
        body = _mean([x for x in a.rms_db if x > -89])
        if head is not None and body is not None:
            parts.append((ramp(head - body, -8, 2, 20, 100), 0.3))
            if head - body < -4:
                tips.append("The opening is quieter than the rest; start on the punchiest line.")

    head_motion = _mean(_window(a.motion, ANALYSIS_FPS, 0.1, HOOK_SECONDS))  # frame 0 has no diff
    body_motion = _mean(a.motion)
    cut_early = any(t <= HOOK_SECONDS for t in a.cut_times)
    if head_motion is not None:
        vis = ramp(head_motion, 0.1, 3.0, 20, 100)
        if body_motion and head_motion >= body_motion:
            vis = min(100.0, vis + 10)
        if cut_early:
            vis = min(100.0, vis + 15)
        parts.append((vis, 0.3 if a.info.has_audio else 1.0))
        if vis < 45:
            tips.append("Little happens on screen in the first 3 seconds; open on movement, a face or bold text.")
            bits.append("static opening")
        else:
            bits.append("moving opening")
    if not parts:
        return 40.0, "not measurable"
    total_w = sum(w for _, w in parts)
    return sum(v * w for v, w in parts) / total_w, ", ".join(bits)


def score_pacing(a: Analysis, tips: list[str]) -> tuple[float, str]:
    d = max(a.info.duration, 1.0)
    cpm = len(a.cut_times) / d * 60
    if cpm < 1:
        cut_score = 35.0
    elif cpm < 12:
        cut_score = ramp(cpm, 1, 12, 45, 100)
    elif cpm <= 40:
        cut_score = 100.0
    else:
        cut_score = ramp(cpm, 40, 90, 100, 70)
    motion = _mean(a.motion) or 0.0
    motion_score = ramp(motion, 0.1, 4.0, 15, 100)
    # A talking head with few cuts can still move; a slideshow with cuts can still be still.
    v = max(cut_score, motion_score) * 0.75 + min(cut_score, motion_score) * 0.25
    # Dead air drags the pace down regardless of the picture.
    if a.info.has_audio and a.silences:
        silent = sum(en - st for st, en in a.silences)
        ratio = silent / d
        if ratio > 0.1:
            v -= ramp(ratio, 0.1, 0.5, 0, 35)
            tips.append(f"{ratio * 100:.0f}% of the video is silence; tighten the gaps.")
    if v < 45 and cpm < 3:
        tips.append("Pacing is slow: add cuts, zooms or B-roll every few seconds.")
    return clamp(v), f"{cpm:.0f} cuts/min, motion {motion:.1f}"


def score_audio(a: Analysis, s: Settings, tips: list[str]) -> tuple[float, str]:
    if not a.info.has_audio:
        tips.append("No audio track. Videos without sound do much worse; add voice or a trending sound.")
        return 10.0, "no audio track"
    lufs = a.loudness_lufs
    if lufs is None or lufs < -60:
        tips.append("The audio track is silent.")
        return 10.0, "silent"
    off = abs(lufs - s.target_lufs)
    v = 100.0 if off <= 2 else clamp(100 - (off - 2) * 6)
    note = f"{lufs:.1f} LUFS"
    if lufs < s.target_lufs - 4:
        tips.append(f"Audio is quiet ({lufs:.0f} LUFS); normalise to about {s.target_lufs:.0f} LUFS.")
    elif lufs > s.target_lufs + 3:
        tips.append(f"Audio is very loud ({lufs:.0f} LUFS) and may get turned down or distort.")
    if a.true_peak_db is not None and a.true_peak_db > -0.3:
        v -= 12
        note += ", clipping"
        tips.append("Audio peaks hit 0 dB (clipping); lower the level or add a limiter.")
    return clamp(v), note


def score_visual(a: Analysis, tips: list[str]) -> tuple[float, str]:
    bright = _mean(a.brightness)
    if bright is None:
        return 50.0, "not measurable"
    b = 100.0 if 70 <= bright <= 180 else (ramp(bright, 20, 70, 20, 100) if bright < 70 else ramp(bright, 180, 235, 100, 30))
    if bright < 55:
        tips.append("The picture is dark; brighten it so it pops in the feed.")
    contrast = _mean(a.contrast) or 0.0
    c = ramp(contrast, 40, 160, 20, 100)
    sat = _mean(a.saturation) or 0.0
    col = ramp(sat, 5, 45, 50, 100)
    blur = _mean(a.blur)
    sharp = ramp(blur, 9, 3, 35, 100) if blur is not None else 70.0
    if blur is not None and blur > 7:
        tips.append("The picture looks soft or blurry; use a sharper source or higher bitrate.")
    # Bits per pixel per frame: low values mean visible compression blocks.
    i = a.info
    bpp = (i.bitrate_kbps * 1000) / max(1, i.width * i.height * (i.fps or 30)) if i.bitrate_kbps else None
    q = ramp(bpp, 0.02, 0.08, 40, 100) if bpp else 75.0
    if bpp is not None and bpp < 0.03 and (_mean(a.motion) or 0) > 0.3:  # still shots compress tiny
        tips.append("Bitrate is low for the resolution; re-export at higher quality to avoid blocky video.")
    v = b * 0.3 + c * 0.2 + col * 0.1 + sharp * 0.25 + q * 0.15
    return clamp(v), f"brightness {bright:.0f}, contrast {contrast:.0f}"


# --- total ------------------------------------------------------------------------


def score(a: Analysis, s: Settings, source_score: float | None = None, ai_score: float | None = None) -> Score:
    tips: list[str] = []
    out = Score()
    for key, (v, note) in (
        ("hook", score_hook(a, tips)),
        ("format", score_format(a, tips)),
        ("duration", score_duration(a, s, tips)),
        ("pacing", score_pacing(a, tips)),
        ("audio", score_audio(a, s, tips)),
        ("visual", score_visual(a, tips)),
    ):
        out.parts[key] = round(v, 1)
        out.notes[key] = note
    if source_score is not None:
        out.parts["source"] = round(clamp(source_score), 1)
        out.notes["source"] = "ClipMonster's interest score for this moment"
    if ai_score is not None:
        out.parts["ai"] = round(clamp(ai_score), 1)
        out.notes["ai"] = "The AI's prediction of viewer appeal"
    out.tips = list(dict.fromkeys(tips))  # drop repeats, keep order
    out.total = combine(out.parts, s)
    return out


def combine(parts: dict[str, float], s: Settings) -> float:
    w = s.weights
    num = den = 0.0
    for key, v in parts.items():
        weight = max(0.0, float(getattr(w, key, 0.0)))
        num += v * weight
        den += weight
    total = num / den if den else 0.0
    # A video with no sound or in landscape can't be rescued by the other parts.
    if parts.get("audio", 100) <= 10:
        total = min(total, 45.0)
    return round(total, 1)

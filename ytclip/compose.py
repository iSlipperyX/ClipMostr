"""What an exported frame is made of, as plain geometry: where each video lands, which part of it is
kept, the bars, the banner words and the captions.

export.py builds its ffmpeg filters from the same settings; this module states the result of those
filters in numbers, so the editor's export view can draw the frame without running ffmpeg, and the
tests can check the two against each other."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import captions, layout
from .analyze import Clip
from .captions import CaptionEvent, CaptionStyle
from .export import frame
from .ffmpeg import MediaInfo
from .layout import Box, Plan
from .settings import Settings
from .subtitles import Word

BLUR_SIGMA = 6.0  # gblur sigma, at a quarter of the frame's size
BLUR_BRIGHTNESS = -0.08  # eq brightness on the blurred fill


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float


@dataclass
class Placement:
    """One video drawn into the frame: the `src` part of it (source pixels) scaled onto `dest` (output
    pixels), clipped to `clip`. A blurred fill is drawn small, blurred and darkened, then scaled up."""

    source: str  # "main" or "second"
    src: Rect
    dest: Rect
    clip: Box
    blur: bool = False
    # Reframing: how far the kept window can slide across the scaled source, in output pixels
    # (0 when the whole width is already shown, so there is nothing to drag).
    slide: float = 0.0


@dataclass
class Composition:
    width: int
    height: int
    aspect: str
    background: str
    layers: list[Placement] = field(default_factory=list)
    plan: Plan | None = None
    banners: list[layout.BannerText] = field(default_factory=list)
    banner_colour: str = "#FFFFFF"
    style: CaptionStyle | None = None
    events: list[CaptionEvent] = field(default_factory=list)
    title: str = ""
    safe: Box | None = None
    settings: Settings | None = None

    def events_at(self, t: float) -> list[CaptionEvent]:
        """Caption events showing `t` seconds into the clip (libass shows start <= t < end)."""
        return [e for e in self.events if e.start <= t < e.end]

    @property
    def reframe(self) -> Placement | None:
        """The clip layer that dragging slides left and right, when the format crops it."""
        return next((p for p in self.layers if p.source == "main" and not p.blur and p.slide > 0), None)


# ffmpeg's integer rounding, so layers land on exactly the pixels the export puts them on.


def _rescale(a: int, b: int, c: int) -> int:
    """av_rescale: a * b / c rounded to nearest."""
    return (a * b + c // 2) // c


def _even_down(v: float) -> int:
    """How pad and overlay snap positions for 4:2:0 video: truncate, then clear the low bit."""
    return int(v) & ~1


def _crop_snap(v: float) -> int:
    """How crop snaps its expressions for 4:2:0 video: lrint (halves go to even), then clear the low bit."""
    return round(v) & ~1


def _fit_size(sw: int, sh: int, w: int, h: int, increase: bool, even: bool = False) -> tuple[int, int]:
    """scale=w:h:force_original_aspect_ratio=increase|decrease[:force_divisible_by=2]."""
    tw, th = _rescale(h, sw, sh), _rescale(w, sh, sw)
    if increase:
        return max(tw, w), max(th, h)
    w, h = min(tw, w), min(th, h)
    return (w // 2 * 2, h // 2 * 2) if even else (w, h)


def _cover(sw: int, sh: int, box: Box, crop_x: float, source: str) -> Placement:
    """Scale to cover the box, then crop a box-sized window: crop_x picks it horizontally, centred vertically."""
    W, H = _fit_size(sw, sh, box.w, box.h, increase=True)
    cx = min(1.0, max(0.0, crop_x))
    x, y = _crop_snap((W - box.w) * cx), _crop_snap((H - box.h) / 2)
    kx, ky = sw / W, sh / H
    return Placement(source, Rect(x * kx, y * ky, box.w * kx, box.h * ky), Rect(box.x, box.y, box.w, box.h), box,
                     slide=W - box.w)


def _contain(sw: int, sh: int, box: Box, source: str) -> Placement:
    """Scale to fit inside the box (even sizes), centred: the fit and the blurred layout's sharp copy."""
    w, h = _fit_size(sw, sh, box.w, box.h, increase=False, even=True)
    return Placement(source, Rect(0, 0, sw, sh),
                     Rect(box.x + _even_down((box.w - w) / 2), box.y + _even_down((box.h - h) / 2), w, h), box)


def _blur_fill(sw: int, sh: int, box: Box, source: str) -> Placement:
    p = _cover(sw, sh, box, 0.5, source)
    p.blur, p.slide = True, 0.0
    return p


def video_layers(s: Settings, aspect: str, plan: Plan | None, width: int, height: int, src_w: int, src_h: int,
                 crop_x: float, second: tuple[int, int] | None) -> list[Placement]:
    sw, sh = max(2, src_w), max(2, src_h)
    if plan is not None:  # layout_graph in export.py
        out: list[Placement] = []
        if plan.fit == "crop":
            out.append(_cover(sw, sh, plan.main, crop_x, "main"))
        elif plan.fit == "fit":
            out.append(_contain(sw, sh, plan.main, "main"))
        else:
            out += [_blur_fill(sw, sh, plan.main, "main"), _contain(sw, sh, plan.main, "main")]
        if plan.second is not None and second is not None:
            out.append(_cover(max(2, second[0]), max(2, second[1]), plan.second, 0.5, "second"))
        return out

    frame = Box(0, 0, width, height)  # _video_filter in export.py
    cx = min(1.0, max(0.0, crop_x))
    if aspect == "vertical_crop":
        cw = _crop_snap(min(sw, sh * 9 / 16))
        x = _crop_snap((sw - cw) * cx)
        w, h = _fit_size(cw, sh, width, height, increase=False, even=True)
        return [Placement("main", Rect(x, 0, cw, sh),
                          Rect(_even_down((width - w) / 2), _even_down((height - h) / 2), w, h), frame,
                          slide=(sw - cw) * w / cw)]
    if aspect == "square":
        m = min(sw, sh) & ~1
        return [Placement("main", Rect(_crop_snap((sw - m) * cx), _crop_snap((sh - m) / 2), m, m),
                          Rect(0, 0, width, height), frame, slide=(sw - m) * width / m)]
    if aspect == "vertical_blur":
        h = _rescale(width, sh, sw * 2) * 2  # scale=W:-2
        return [_blur_fill(sw, sh, frame, "main"),
                Placement("main", Rect(0, 0, sw, sh), Rect(0, _even_down((height - h) / 2), width, h), frame)]
    return [Placement("main", Rect(0, 0, sw, sh), Rect(0, 0, width, height), frame)]


def compose(s: Settings, media: MediaInfo, clip: Clip | None = None, words: list[Word] | None = None,
            second: tuple[int, int] | None = None, width: int | None = None, height: int | None = None,
            next_part: bool = True) -> Composition:
    """Everything the export draws for `clip` (None = a clip with no title, text overrides or reframe).
    `second` is the stacked video's size, when there is one to show; next_part as for export_clip."""
    aspect, w, h = frame(s, media)
    if width and height:
        w, h = width, height
    use_layout = layout.active(s)
    plan = layout.plan(s, w, h) if use_layout or layout.uses_safe_zone(s, w, h) else None
    crop_x = clip.crop_x if clip else 0.5
    c = Composition(w, h, aspect, s.banner_bg if use_layout else "#000000", plan=plan if use_layout else None,
                    banner_colour=s.banner_color, safe=plan.safe if plan else None, settings=s)
    c.layers = video_layers(s, aspect, plan if use_layout else None, w, h, media.width or 1920,
                            media.height or 1080, crop_x, second if use_layout else None)
    title = clip.title if clip else ""
    if use_layout and plan is not None:
        top = layout.banner_text(s.banner_top_text, title, clip.top_text if clip else None) if plan.top else ""
        bottom = layout.bottom_banner(s, title, clip.bottom_text if clip else None, next_part) if plan.bottom else ""
        c.banners = layout.banner_texts(plan, s, top, bottom)
    if (s.burn_captions and words) or s.title_overlay:
        area, side = (plan.caption_area, plan.side_margin) if plan is not None else (None, 0)
        c.style = captions.caption_style(s, w, h, area, side)
        c.title = captions.title_text(title, s)
        if s.burn_captions and words and clip is not None:
            c.events = captions.caption_events(words, clip.start, clip.end, s)
    return c

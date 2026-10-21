"""Frame layouts for export: text banners at the top/bottom and a second video stacked under (or over) the clip.

Everything here is pure geometry and string building, so the GUI sketch, the still preview and the
real export all share one plan. export.py turns a plan into an ffmpeg command."""

from __future__ import annotations

import random
from dataclasses import dataclass

from . import fonts
from .captions import _ass_colour, _escape
from .settings import Settings


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


@dataclass
class Plan:
    width: int
    height: int
    main: Box  # where the clip itself goes
    second: Box | None = None  # the stacked video (gameplay, satisfying clip...)
    top: Box | None = None  # banner areas: solid bars, or text-only zones over the video
    bottom: Box | None = None
    bars: bool = True  # False = banner text sits on top of the video with no bar behind it
    fit: str = "crop"  # how the clip fills `main`: crop | blur | fit
    top_text: Box | None = None  # where each banner's words go (inside the safe zone when there is one)
    bottom_text: Box | None = None
    safe: Box | None = None  # the part of the frame TikTok's buttons and captions don't cover

    @property
    def caption_area(self) -> tuple[int, int, int]:
        """(top, bottom, middle) for captions: inside the clip, and 'middle' means the seam when stacked."""
        mid = self.main.cy
        if self.second is not None:
            mid = self.main.bottom if self.second.y >= self.main.bottom else self.main.y
        top, bottom = self.main.y, self.main.bottom
        if self.safe is not None:
            top, bottom = max(top, self.safe.y), min(bottom, self.safe.bottom)
        if not self.bars:  # banner words drawn over the video: keep captions off them
            if self.top_text is not None:
                top = max(top, self.top_text.bottom)
            if self.bottom_text is not None:
                bottom = min(bottom, self.bottom_text.y)
        mid = min(max(mid, top), bottom)
        return top, bottom, mid

    @property
    def side_margin(self) -> int:
        return self.safe.x if self.safe is not None else 0


# TikTok's own interface over a 1080x1920 video: the Following / For You tabs on top, the caption,
# username and sound line at the bottom, and the like/comment/share buttons down the right side.
TIKTOK_SAFE = {"top": 131 / 1920, "bottom": 367 / 1920, "left": 120 / 1080, "right": 120 / 1080}


def safe_box(width: int, height: int) -> Box:
    left = _even(width * TIKTOK_SAFE["left"])
    right = _even(width * TIKTOK_SAFE["right"])
    top = _even(height * TIKTOK_SAFE["top"])
    bottom = _even(height * TIKTOK_SAFE["bottom"])
    return Box(left, top, width - left - right, height - top - bottom)


def uses_safe_zone(s: Settings, width: int, height: int) -> bool:
    return s.tiktok_ready and height > width


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def active(s: Settings) -> bool:
    return s.banner_top or s.banner_bottom or s.stack_enabled


def frame_aspect(s: Settings) -> str:
    """Stacking two videos only makes sense in a tall frame, so 'original' becomes 9:16 there."""
    if s.stack_enabled and s.aspect == "original":
        return "vertical_crop"
    return s.aspect


def fit_mode(s: Settings) -> str:
    if s.layout_fit != "auto":
        return s.layout_fit
    aspect = frame_aspect(s)
    if aspect in ("vertical_crop", "square"):
        return "crop"
    if aspect == "vertical_blur":
        return "blur"
    return "fit"  # original frame with banners: keep the whole picture, bars fill the rest


def plan(s: Settings, width: int, height: int) -> Plan:
    """Lay out the frame. With TikTok-ready output in a tall frame, banner words are kept inside the safe
    zone: the top bar grows if the tabs would cover its text, and the bottom bar sits just above TikTok's
    caption area and extends down behind it, so no video is hidden under the interface."""
    bars = s.banner_mode == "bar"
    safe = safe_box(width, height) if uses_safe_zone(s, width, height) else None
    th = _even(height * s.banner_top_size / 100) if s.banner_top else 0
    bh = _even(height * s.banner_bottom_size / 100) if s.banner_bottom else 0
    tx, tw = (safe.x, safe.w) if safe else (0, width)

    top = top_text = bottom = bottom_text = None
    if th:
        if safe is None:
            top = top_text = Box(0, 0, width, th)
        elif bars:  # the bar also covers TikTok's tabs, and its words sit just below them
            top, top_text = Box(0, 0, width, safe.y + th), Box(tx, safe.y, tw, th)
        else:
            top = top_text = Box(tx, safe.y, tw, th)
    if bh:
        if safe is None:
            bottom = bottom_text = Box(0, height - bh, width, bh)
        elif bars:
            below = height - safe.bottom
            bottom, bottom_text = Box(0, height - bh - below, width, bh + below), Box(tx, safe.bottom - bh, tw, bh)
        else:
            bottom = bottom_text = Box(tx, safe.bottom - bh, tw, bh)

    y0 = top.bottom if bars and top else 0
    y1 = bottom.y if bars and bottom else height
    content = max(2, y1 - y0)
    p = Plan(width, height, Box(0, y0, width, content), top=top, bottom=bottom, bars=bars, fit=fit_mode(s),
             top_text=top_text, bottom_text=bottom_text, safe=safe)
    if s.stack_enabled:
        main_h = _even(content * min(0.85, max(0.15, s.stack_split)))
        second_h = content - main_h
        if s.stack_position == "top":
            p.second = Box(0, y0, width, second_h)
            p.main = Box(0, y0 + second_h, width, main_h)
        else:
            p.main = Box(0, y0, width, main_h)
            p.second = Box(0, y0 + main_h, width, second_h)
    return p


def crop_ratio(s: Settings, width: int, height: int) -> float | None:
    """Width/height of the part of the source a cropped layout keeps (None when nothing is cropped),
    so the preview can show the reframe box."""
    p = plan(s, width, height)
    return p.main.w / p.main.h if p.fit == "crop" else None


# ---------------------------------------------------------------- banner text


def banner_text(template: str, title: str, override: str | None = None) -> str:
    """The text for one banner: a per-clip override wins, otherwise the template with {title} filled in."""
    if override is not None:
        return override.strip()
    try:
        return template.format_map({"title": title}).strip()
    except (KeyError, ValueError, IndexError):
        return template.strip()  # a stray brace in the user's text: show it as typed


def wrap(text: str, size: int, box: Box, uppercase: bool, font: str = "") -> tuple[list[str], int]:
    """Break text into lines and shrink the font until the block fits the box.

    libass can't wrap into a box of our choosing when text is positioned with \\pos, so lines are
    broken here using an average glyph width. Bold display fonts run wide, hence the generous factor."""
    words = text.split()
    if not words:
        return [], size
    glyph = fonts.glyph_width(font, uppercase) * 1.05  # a little slack for wide letters and spaces
    max_w, max_h = box.w * 0.9, box.h * 0.84
    while True:
        per_line = max(1, int(max_w / (size * glyph)))
        lines = _greedy(words, per_line)
        fits = len(lines) * size * 1.15 <= max_h and max(len(ln) for ln in lines) <= per_line
        if fits or size <= 14:
            return _balance(words, lines), size
        size = int(size * 0.92)


def _greedy(words: list[str], per_line: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if cur and len(cand) > per_line:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    return lines + [cur]


def _balance(words: list[str], lines: list[str]) -> list[str]:
    """Same number of lines with the longest one as short as possible, so "PART / 2" doesn't dangle."""
    n = len(lines)
    if n < 2:
        return lines
    width = max(len(w) for w in words)
    for target in range(max(width, len(" ".join(words)) // n), max(len(ln) for ln in lines) + 1):
        even = _greedy(words, target)
        if len(even) == n:
            return even
    return lines


def second_offset(clip_start: float, clip_len: float, second_duration: float, mode: str, fixed: float) -> float:
    """Where to start reading the stacked video. 'random' is seeded by the clip so re-exports match."""
    room = max(0.0, second_duration - clip_len)
    if mode == "random":
        return random.Random(int(clip_start * 1000)).uniform(0, room) if room > 1 else 0.0
    return min(max(0.0, fixed), room) if room else 0.0


def banner_ass(p: Plan, s: Settings, top_text: str, bottom_text: str) -> str:
    """An ASS file that draws the banner text (the bars themselves are part of the video filter)."""
    scale = min(p.width, p.height) / 1080
    bar = p.bars
    outline = 0 if bar else max(2, int(6 * scale))
    shadow = 0 if bar else max(1, int(2 * scale))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {p.width}
PlayResY: {p.height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Banner,{s.banner_font},{int(s.banner_font_size * scale)},{_ass_colour(s.banner_color)},&H000000FF,&H00000000,&H80000000,{fonts.ass_bold(s.banner_font)},0,0,0,100,100,0,0,1,{outline},{shadow},5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for box, text in ((p.top_text, top_text), (p.bottom_text, bottom_text)):
        if box is None or not text:
            continue
        if s.banner_uppercase:
            text = text.upper()
        lines, size = wrap(_escape(text), int(s.banner_font_size * scale), box, s.banner_uppercase, s.banner_font)
        body = "\\N".join(lines)
        events.append(f"Dialogue: 2,0:00:00.00,9:59:59.00,Banner,,0,0,0,,{{\\an5\\pos({box.cx},{box.cy})\\fs{size}}}{body}")
    return header + "\n".join(events) + "\n"

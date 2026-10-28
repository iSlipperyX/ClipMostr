"""Frame layouts for export: text banners at the top/bottom and a second video stacked under (or over) the clip.

Everything here is pure geometry and string building, so the GUI sketch, the still preview and the
real export all share one plan. export.py turns a plan into an ffmpeg command."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from . import fonts, textlayout
from .captions import BREAK, _ass_colour, _escape
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
    """The frame clips are actually exported in. Stacking two videos only makes sense in a tall frame, so
    'original' becomes 9:16 there; TikTok-ready output is always 9:16, so 'original' and 1:1 are fitted
    into it with a blurred fill."""
    if s.stack_enabled and s.aspect == "original":
        return "vertical_crop"
    if s.tiktok_ready and s.aspect not in ("vertical_blur", "vertical_crop"):
        return "vertical_blur"
    return s.aspect


def fit_mode(s: Settings) -> str:
    if s.layout_fit != "auto":
        return s.layout_fit
    aspect = frame_aspect(s)
    if s.stack_enabled or aspect in ("vertical_crop", "square"):  # a stacked half looks best filled edge to edge
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


# Bottom text like "Follow for part 2" promises a next part; it's only true when one exists.
_PART = r"(?:part|pt\.?|parte|partie|teil|deel)"
_MORE = re.compile(rf"\b{_PART}\s*(?:[1-9]\d*|one|two|three|four|ii+|iv)\b"  # part 2, Pt.3, Teil 2, part II
                   rf"|\b{_PART}s?\s*(?:\d+\s*)?(?:is\s+|are\s+)?(?:coming|soon)\b"  # part 2 is coming
                   r"|\bnext (?:part|one|video|clip|episode)\b|\bmore parts\b|\bstay tuned\b"
                   r"|\bcontinu(?:ed|es)\b|\bwhat happens next\b", re.I)
_HANDLE = re.compile(r"@[\w.]*\w")
CARRIES_ON = 3.0  # seconds: a clip starting this close to where another ends is that clip's part 2


def promises_more(text: str) -> bool:
    """Whether banner text promises a part 2, a next part or a continuation."""
    return bool(_MORE.search(text or ""))


def has_next_part(clip, clips, s: Settings | None = None) -> bool:
    """Whether another clip (switched on) picks up where this one stops, maybe replaying its last
    moment: its part 2. With settings, a clip the quality check will reject (the AI rated it under
    the check's bar, see qc.judge_content) isn't anyone's part 2."""
    bar = s.qc_min_rating if s is not None and s.qc_enabled else 0
    return any(o is not clip and o.enabled and not (bar and 0 < o.rating < bar)
               and o.start > clip.start and o.end > clip.end and abs(o.start - clip.end) <= CARRIES_ON
               for o in clips)


def own_words(template: str) -> str:
    """A banner template without the {title} that each clip fills in: the words you chose."""
    return template.replace("{title}", " ")


def uses_ai_bottom(s: Settings) -> bool:
    """Whether the AI polish's bottom lines can show: the bottom banner is on, its text promises a part 2
    (so a clip with none needs other words) and the 'no part 2' text isn't left blank."""
    return bool(s.banner_bottom and s.polish_bottom_text and promises_more(own_words(s.banner_bottom_text))
                and own_words(s.banner_bottom_alt).strip())


def bottom_handles(s: Settings) -> list[str]:
    """The @handles in your bottom texts, which the AI's bottom lines have to keep."""
    found: dict[str, str] = {}
    for h in _HANDLE.findall(f"{s.banner_bottom_text} {s.banner_bottom_alt}"):
        found.setdefault(h.lower(), h)
    return list(found.values())


def ai_line_fits(s: Settings, line: str) -> bool:
    """Whether the AI's bottom line for a clip can stand in for the 'no part 2' text: the setting is on,
    the line promises no next part, and it keeps every @handle in your bottom texts."""
    if not (s.polish_bottom_text and line.strip()) or promises_more(line):
        return False
    have = {h.lower() for h in _HANDLE.findall(line)}
    return all(h.lower() in have for h in bottom_handles(s))


def bottom_banner(s: Settings, title: str, override: str | None = None, next_part: bool = True,
                  ai: str = "") -> str:
    """The bottom banner's words for a clip. Text typed for the clip shows as typed. Otherwise it's the
    bottom text, unless that promises a part 2 the clip doesn't have (next_part, see has_next_part):
    then the AI polish's line for the clip (`ai`, when it fits) or else the 'no part 2' text."""
    if override is not None:
        return override.strip()
    if next_part or not promises_more(own_words(s.banner_bottom_text)):
        return banner_text(s.banner_bottom_text, title)
    return ai.strip() if ai and uses_ai_bottom(s) and ai_line_fits(s, ai) else banner_text(s.banner_bottom_alt, title)


def banner_text(template: str, title: str, override: str | None = None) -> str:
    """The text for one banner: a per-clip override wins, otherwise the template with {title} filled in."""
    if override is not None:
        return override.strip()
    try:
        return template.format_map({"title": title}).strip()
    except (KeyError, ValueError, IndexError):
        return template.strip()  # a stray brace in the user's text: show it as typed


def wrap(text: str, em_px: float, box: Box, font: str) -> tuple[list[list[str]], int]:
    """Break text into lines and shrink it until the block fits the box. Returns the lines and the ASS
    font size to draw them at. libass can't wrap into a box of our choosing when text is placed with
    \\pos, so ClipMonster breaks the lines itself, measured from the font file."""
    words = text.split()
    size = fonts.ass_size(font, em_px)
    if not words:
        return [], size
    max_w, max_h = box.w * 0.9, box.h * 0.84
    while True:
        lines = textlayout.break_lines(words, font, size, max_w)
        widest = max(textlayout.text_width(font, " ".join(ln), size) for ln in lines)
        if (widest <= max_w and len(lines) * size <= max_h) or size <= fonts.ass_size(font, 12):
            return lines, size
        size = max(1, int(size * 0.95))


def second_offset(clip_start: float, clip_len: float, second_duration: float, mode: str, fixed: float) -> float:
    """Where to start reading the stacked video. 'random' is seeded by the clip so re-exports match."""
    room = max(0.0, second_duration - clip_len)
    if mode == "random":
        return random.Random(int(clip_start * 1000)).uniform(0, room) if room > 1 else 0.0
    return min(max(0.0, fixed), room) if room else 0.0


@dataclass
class BannerText:
    box: Box
    block: textlayout.Block  # every line and word, placed in output pixels
    outline: int
    shadow: int


def banner_texts(p: Plan, s: Settings, top_text: str, bottom_text: str) -> list[BannerText]:
    """The banner words as they'll be burned in: shared by the .ass file and the editor's export view."""
    scale = min(p.width, p.height) / 1080
    outline = 0 if p.bars else max(2, int(6 * scale))
    shadow = 0 if p.bars else max(1, int(2 * scale))
    out = []
    for box, text in ((p.top_text, top_text), (p.bottom_text, bottom_text)):
        if box is None or not text or not text.strip():
            continue
        text = _escape(text.upper() if s.banner_uppercase else text)  # measured as libass will show it
        lines, size = wrap(text, s.banner_font_size * scale, box, s.banner_font)
        block = textlayout.place([[(w, 1.0, False) for w in ln] for ln in lines], s.banner_font, size,
                                 box.cx, box.cy, "middle")
        out.append(BannerText(box, block, outline, shadow))
    return out


def banner_ass(p: Plan, s: Settings, top_text: str, bottom_text: str) -> str:
    """An ASS file that draws the banner text (the bars themselves are part of the video filter)."""
    texts = banner_texts(p, s, top_text, bottom_text)
    outline = texts[0].outline if texts else 0
    shadow = texts[0].shadow if texts else 0
    size = fonts.ass_size(s.banner_font, s.banner_font_size * min(p.width, p.height) / 1080)
    # Encoding -1 lets libass work out each line's text direction, so Arabic or Hebrew words read
    # right to left in the right order (with 1, every line is laid out left to right).
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {p.width}
PlayResY: {p.height}
WrapStyle: 2
ScaledBorderAndShadow: yes
Kerning: no

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Banner,{s.banner_font},{size},{_ass_colour(s.banner_color)},&H000000FF,&H00000000,&H80000000,{fonts.ass_bold(s.banner_font)},0,0,0,100,100,0,0,1,{outline},{shadow},5,0,0,0,-1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for bt in texts:
        body = BREAK.join(" ".join(w.text for w in ln.words) for ln in bt.block.lines)
        events.append(f"Dialogue: 2,0:00:00.00,9:59:59.00,Banner,,0,0,0,,"
                      f"{{\\an5\\pos({bt.box.cx},{bt.box.cy})\\fs{bt.block.size}}}{body}")
    return header + "\n".join(events) + "\n"

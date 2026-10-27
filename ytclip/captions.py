"""Animated "word pop" captions as an ASS subtitle file for ffmpeg/libass to burn in."""

from __future__ import annotations

from dataclasses import dataclass

from . import fonts, textlayout
from .settings import Settings
from .subtitles import Word, censor, words_between

_SENTENCE_END = (".", "!", "?", "…")


def _ass_colour(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def _groups(words: list[Word], size: int) -> list[list[Word]]:
    """Chunk words into on-screen groups, breaking early at pauses and sentence ends."""
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        pause = nxt is not None and nxt.start - w.end > 0.6
        if len(cur) >= size or pause or w.text.endswith(_SENTENCE_END) or nxt is None:
            groups.append(cur)
            cur = []
    return groups


@dataclass
class CaptionStyle:
    """Where and how big captions and the title box are, in output pixels. Shared by the .ass file
    libass burns in and by the editor's live export view, so both put text in the same place."""

    size: int  # ASS font size: the height of one line (libass sizes fonts by their full line height)
    align: int  # ASS numpad alignment: 2 = bottom centre, 5 = middle, 8 = top centre
    margin_v: int
    margin_lr: int
    outline: int
    shadow: int
    bold: int
    title_size: int
    title_margin: int
    title_pad: int  # border width of the title's opaque box


def caption_style(s: Settings, width: int, height: int, area: tuple[int, int, int] | None = None,
                  side: int = 0) -> CaptionStyle:
    """`area` is (top, bottom, middle) of the part of the frame captions may use, when a layout or
    TikTok's safe zone leaves less than the whole frame; `side` is the minimum left/right margin."""
    scale = min(width, height) / 1080
    size = fonts.ass_size(s.caption_font, s.caption_size * scale)
    vertical = height > width
    if area is None:
        align, margin_v = {
            "lower": (2, int(height * (0.22 if vertical else 0.08))),
            "middle": (5, 0),
            "upper": (8, int(height * 0.14)),
        }.get(s.caption_position, (2, int(height * 0.1)))
        title_margin = int(height * 0.06)
    else:
        top, bottom, mid = area
        span = bottom - top
        align, margin_v = {
            "lower": (2, height - bottom + int(span * 0.1)),
            # Bottom-anchored just under the seam/middle, so the text straddles it.
            "middle": (2, max(0, height - mid - int(size * 0.6))),
            "upper": (8, top + int(span * 0.1)),
        }.get(s.caption_position, (2, height - bottom + int(span * 0.1)))
        title_margin = top + int(span * 0.05)
    return CaptionStyle(size=size, align=align, margin_v=margin_v, margin_lr=max(int(60 * scale), side),
                        outline=max(0, int(s.caption_outline * scale)), shadow=int(2 * scale),
                        bold=fonts.ass_bold(s.caption_font),
                        title_size=fonts.ass_size(s.caption_font, s.caption_size * scale * 0.8),
                        title_margin=title_margin, title_pad=int(14 * scale))


@dataclass
class CaptionEvent:
    start: float  # seconds from the clip's start
    end: float
    words: list[str]  # the group on screen, already cased
    active: int  # index of the word being spoken (drawn in the highlight colour, 112% size)


HOLD = 1.5  # seconds a caption may stay up after its last word, if nothing follows sooner


def caption_events(words: list[Word], clip_start: float, clip_end: float, s: Settings) -> list[CaptionEvent]:
    """Word-pop caption timing for one clip. Groups always start from the clip's first word, so a
    preview at any moment shows exactly the grouping the export will."""
    duration = clip_end - clip_start
    out: list[CaptionEvent] = []
    clip_words = words_between(words, clip_start, clip_end)
    groups = _groups(clip_words, max(1, s.caption_words))
    for g, group in enumerate(groups):
        texts = [censor(w.text) if s.caption_censor else w.text for w in group]
        texts = [t.upper() if s.caption_uppercase else t for t in texts]
        # Keep the last words up a little longer, until the next group starts, so short pauses
        # (and caption timing that runs ahead of the speech) don't flash an empty screen.
        nxt = groups[g + 1][0].start if g + 1 < len(groups) else clip_end
        group_end = max(group[-1].end, min(nxt, group[-1].end + HOLD))
        for i, w in enumerate(group):
            a = max(0.0, w.start - clip_start)
            b = min(duration, (group[i + 1].start if i + 1 < len(group) else group_end) - clip_start)
            if b > a:
                out.append(CaptionEvent(a, b, texts, i))
    return out


def title_text(title: str, s: Settings) -> str:
    return (title.upper() if s.caption_uppercase else title) if title and s.title_overlay else ""


ACTIVE_SCALE = 1.12  # the spoken word pops to 112%
BREAK = "\\N"  # an explicit .ass line break


def caption_lines(words: list[str], s: Settings, st: CaptionStyle, width: int) -> list[list[str]]:
    """Line breaks for one caption group. Measured with every word at full pop size, so the lines
    never change as the highlight moves along."""
    return textlayout.break_lines(words, s.caption_font, st.size, (width - 2 * st.margin_lr) / ACTIVE_SCALE)


def caption_block(ev: CaptionEvent, s: Settings, st: CaptionStyle, width: int, height: int) -> textlayout.Block:
    """Where every word of a caption event lands, exactly as libass places it."""
    lines, k = [], 0
    for ln in caption_lines(ev.words, s, st, width):
        lines.append([(w, ACTIVE_SCALE if k + j == ev.active else 1.0, k + j == ev.active) for j, w in enumerate(ln)])
        k += len(ln)
    if st.align == 8:
        return textlayout.place(lines, s.caption_font, st.size, width / 2, st.margin_v, "top")
    if st.align == 5:
        return textlayout.place(lines, s.caption_font, st.size, width / 2, height / 2, "middle")
    return textlayout.place(lines, s.caption_font, st.size, width / 2, height - st.margin_v, "bottom")


def title_block(title: str, s: Settings, st: CaptionStyle, width: int) -> textlayout.Block:
    lines = textlayout.break_lines(title.split(), s.caption_font, st.title_size, width - 2 * st.margin_lr)
    return textlayout.place([[(w, 1.0, False) for w in ln] for ln in lines], s.caption_font, st.title_size,
                            width / 2, st.title_margin, "top")


def build_ass(words: list[Word], clip_start: float, clip_end: float, width: int, height: int,
              s: Settings, title: str = "", area: tuple[int, int, int] | None = None, side: int = 0,
              offset: float = 0.0) -> str:
    """The captions .ass file. `offset` shifts every time earlier, for rendering a still from inside the clip."""
    st = caption_style(s, width, height, area, side)
    lr = st.margin_lr
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
Kerning: no

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{s.caption_font},{st.size},{_ass_colour(s.caption_color)},&H000000FF,&H00000000,&H80000000,{st.bold},0,0,0,100,100,0,0,1,{st.outline},{st.shadow},{st.align},{lr},{lr},{st.margin_v},1
Style: Title,{s.caption_font},{st.title_size},&H00FFFFFF,&H000000FF,&H00000000,&HC0000000,{st.bold},0,0,0,100,100,0,0,3,{st.title_pad},0,8,{lr},{lr},{st.title_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    duration = clip_end - clip_start
    title = title_text(title, s)
    if title:
        text = BREAK.join(_escape(" ".join(ln)) for ln in
                           textlayout.break_lines(title.split(), s.caption_font, st.title_size, width - 2 * lr))
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(duration - offset)},Title,,0,0,0,,{text}")

    if s.burn_captions:
        hl = _ass_colour(s.caption_highlight)
        for ev in caption_events(words, clip_start, clip_end, s):
            if ev.end <= offset:
                continue
            pop = round(ACTIVE_SCALE * 100)
            out_lines, k = [], 0
            for ln in caption_lines(ev.words, s, st, width):
                out_lines.append(" ".join(f"{{\\c{hl}\\fscx{pop}\\fscy{pop}}}{_escape(t)}{{\\r}}"
                                          if k + j == ev.active else _escape(t) for j, t in enumerate(ln)))
                k += len(ln)
            times = f"{_ts(ev.start - offset)},{_ts(ev.end - offset)}"
            events.append(f"Dialogue: 0,{times},Cap,,0,0,0,,{BREAK.join(out_lines)}")
    return header + "\n".join(events) + "\n"

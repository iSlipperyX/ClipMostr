"""Animated "word pop" captions as an ASS subtitle file for ffmpeg/libass to burn in."""

from __future__ import annotations

from .settings import Settings
from .subtitles import Word, words_between

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


def build_ass(words: list[Word], clip_start: float, clip_end: float, width: int, height: int,
              s: Settings, title: str = "") -> str:
    scale = min(width, height) / 1080
    size = int(s.caption_size * scale)
    vertical = height > width
    align, margin_v = {
        "lower": (2, int(height * (0.22 if vertical else 0.08))),
        "middle": (5, 0),
        "upper": (8, int(height * 0.14)),
    }.get(s.caption_position, (2, int(height * 0.1)))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{s.caption_font},{size},{_ass_colour(s.caption_color)},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{max(0, int(s.caption_outline * scale))},{int(2 * scale)},{align},{int(60 * scale)},{int(60 * scale)},{margin_v},1
Style: Title,{s.caption_font},{int(size * 0.8)},&H00FFFFFF,&H000000FF,&H00000000,&HC0000000,-1,0,0,0,100,100,0,0,3,{int(14 * scale)},0,8,{int(60 * scale)},{int(60 * scale)},{int(height * 0.06)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    duration = clip_end - clip_start
    if title and s.title_overlay:
        text = _escape(title.upper() if s.caption_uppercase else title)
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(duration)},Title,,0,0,0,,{text}")

    if s.burn_captions:
        hl = _ass_colour(s.caption_highlight)
        clip_words = words_between(words, clip_start, clip_end)
        for group in _groups(clip_words, max(1, s.caption_words)):
            group_end = group[-1].end
            for i, w in enumerate(group):
                a = max(0.0, w.start - clip_start)
                b = min(duration, (group[i + 1].start if i + 1 < len(group) else group_end) - clip_start)
                if b <= a:
                    continue
                parts = []
                for j, other in enumerate(group):
                    t = _escape(other.text.upper() if s.caption_uppercase else other.text)
                    parts.append(f"{{\\c{hl}\\fscx112\\fscy112}}{t}{{\\r}}" if j == i else t)
                events.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(events) + "\n"

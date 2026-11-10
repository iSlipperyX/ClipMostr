"""Kinetic captions for a timeline: word times in, a subtitle track and a ready-to-burn .ass file out.

Two steps, so the captions can be edited in between (they're plain JSON on the timeline):

    track = build_track(words, canvas, style, duration=64.2, emphasis={"never", "money"})
    text = to_ass(track, canvas)          # what ffmpeg's subtitles filter (libass) burns in

**build_track** groups the timed words (whisper's, or the voice engine's own) into captions of one to three
words, breaking early at pauses and sentence ends, and decides for each word:

* its colour: the script's emphasis words in the style's emphasis colour; then by meaning (the rules the
  Studio's captions already use, rendering/ass_builder.py): money and finance in green, danger and suspense
  in red, questions and curiosity in yellow. A colour written on a word in the timeline always wins;
* its emoji: a few words bring a small icon with them (💰 with "money", 🔥 with "insane", ⏰ with "minutes"),
  at most one a caption and not more often than every few seconds, so they punctuate rather than clutter;
* its box: where it's drawn on the canvas, measured from the font exactly as libass lays it out
  (textlayout.py), for the timeline view and for anything placed against the words.

**to_ass** writes the animation, by preset:

* `pop` (the default): the group pops in, 80% → 110% → 100% over 150 ms (\\fscx80\\fscy80, then two \\t
  transforms); the word being spoken lights up in the highlight colour and swells to 112%.
* `bounce`: the same, with a springier spoken word: it overshoots to 128%, dips to 96% and settles at 108%.
* `karaoke`: the group pops in once and the highlight sweeps through each word as it's said (\\kf).
* `plain`: no movement, colours only.

Captions sit centred in the safe zone: on a vertical video the block's bottom is at y = 1200 of 1920 (clear of
TikTok's buttons and caption text), on a wide one near the bottom, or wherever the style's `position` says.

**Emoji** are drawn as vector shapes (ASS drawings), not font glyphs: libass can't draw colour emoji fonts,
and a missing glyph would show an empty box in a finished video. Each icon pops in over its word with a small
wobble. The shapes are this file's own (ICONS); an emoji without a shape here is simply not drawn.
"""

from __future__ import annotations

import math
import re

from .. import fonts, textlayout
from ..captions import ACTIVE_SCALE, BREAK, HOLD, KINETIC_BOTTOM, _ass_colour, _escape, _groups, _ts
from ..rendering.ass_builder import DANGER, bare, keyword_kind
from ..studio.timeline_schema import (SUBTITLES, Canvas, SubtitleEvent, SubtitleStyle, Track, WordEvent)
from ..subtitles import Word, censor

POP_MS = (80, 150)  # the group's pop: to 110% by 80 ms, settled by 150 ms
ICON_GAP = 3.5  # seconds between one icon and the next
LANDSCAPE_BOTTOM = 0.88  # where captions end on a wide video
MARGIN = 60 / 1080  # the side margin, as a share of the canvas's short side


# ---------------------------------------------------------------- icons

def _circle(cx: float, cy: float, r: float, clockwise: bool = True) -> str:
    k = 0.5523 * r
    pts = [(cx, cy - r), (cx + k, cy - r, cx + r, cy - k, cx + r, cy), (cx + r, cy + k, cx + k, cy + r, cx, cy + r),
           (cx - k, cy + r, cx - r, cy + k, cx - r, cy), (cx - r, cy - k, cx - k, cy - r, cx, cy - r)]
    if not clockwise:  # the same circle drawn the other way round cuts a hole (non-zero winding)
        pts = [(cx, cy - r), (cx - k, cy - r, cx - r, cy - k, cx - r, cy), (cx - r, cy + k, cx - k, cy + r, cx, cy + r),
               (cx + k, cy + r, cx + r, cy + k, cx + r, cy), (cx + r, cy - k, cx + k, cy - r, cx, cy - r)]
    out = f"m {pts[0][0]:.0f} {pts[0][1]:.0f}"
    for p in pts[1:]:
        out += " b " + " ".join(f"{v:.0f}" for v in p)
    return out


def _poly(*pts: tuple[float, float]) -> str:
    return f"m {pts[0][0]:g} {pts[0][1]:g} l " + " ".join(f"{x:g} {y:g}" for x, y in pts[1:])


def _star() -> str:
    pts = []
    for i in range(10):
        r = 48 if i % 2 == 0 else 20
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((round(50 + r * math.cos(a), 1), round(52 + r * math.sin(a), 1)))
    return _poly(*pts)


# name: (kind, shape or text, colour). "draw" shapes are on a 100x100 grid, y downwards; a second outline
# drawn anticlockwise inside a clockwise one is a hole.
ICONS: dict[str, tuple[str, str, str]] = {
    "dollar": ("text", "$", "#00FF66"),
    "question": ("text", "?", "#FFE600"),
    "exclaim": ("text", "!", "#FF3B30"),
    "hundred": ("text", "100", "#FF3B30"),
    "warning": ("draw", _poly((50, 4), (98, 92), (2, 92)) + " " + _poly((43, 34), (43, 64), (57, 64), (57, 34)) +
                " " + _poly((43, 71), (43, 84), (57, 84), (57, 71)), "#FFCC00"),
    "fire": ("draw", "m 50 4 b 58 26 84 40 80 66 b 78 86 64 98 50 98 b 36 98 22 86 20 66 b 19 52 28 42 34 34 "
                     "b 36 46 42 50 46 48 b 42 34 44 16 50 4", "#FF6A00"),
    "heart": ("draw", "m 50 30 b 50 10 20 5 12 30 b 5 55 35 75 50 94 b 65 75 95 55 88 30 b 80 5 50 10 50 30",
              "#FF2D55"),
    "star": ("draw", _star(), "#FFD60A"),
    "bolt": ("draw", _poly((60, 2), (18, 56), (44, 56), (36, 98), (82, 40), (56, 40)), "#FFE600"),
    "up": ("draw", _poly((50, 4), (92, 50), (66, 50), (66, 96), (34, 96), (34, 50), (8, 50)), "#00FF66"),
    "down": ("draw", _poly((50, 96), (8, 50), (34, 50), (34, 4), (66, 4), (66, 50), (92, 50)), "#FF3B30"),
    "check": ("draw", _poly((6, 54), (22, 38), (40, 56), (78, 12), (94, 28), (40, 90)), "#00FF66"),
    "cross": ("draw", _poly((16, 4), (50, 38), (84, 4), (96, 16), (62, 50), (96, 84), (84, 96), (50, 62), (16, 96),
                            (4, 84), (38, 50), (4, 16)), "#FF3B30"),
    "clock": ("draw", _circle(50, 50, 46) + " " + _poly((45, 20), (45, 55), (74, 55), (74, 45), (55, 45), (55, 20)),
              "#FFFFFF"),
    "eye": ("draw", "m 2 50 b 26 12 74 12 98 50 b 74 88 26 88 2 50 " + _circle(50, 50, 15, clockwise=False),
            "#FFFFFF"),
    "crown": ("draw", _poly((8, 86), (8, 28), (30, 52), (50, 14), (70, 52), (92, 28), (92, 86)), "#FFD60A"),
}
EMOJI_ICONS = {"💰": "dollar", "💵": "dollar", "🤑": "dollar", "💸": "dollar", "❓": "question", "🤔": "question",
               "❗": "exclaim", "‼": "exclaim", "💯": "hundred", "⚠": "warning", "🚨": "warning", "🔥": "fire",
               "❤": "heart", "💔": "heart", "⭐": "star", "🌟": "star", "⚡": "bolt", "📈": "up", "🚀": "up",
               "📉": "down", "✅": "check", "✔": "check", "❌": "cross", "⏰": "clock", "⏳": "clock", "🕒": "clock",
               "👀": "eye", "👁": "eye", "👑": "crown", "🏆": "crown"}

# Words that bring an icon. Money and questions come from the colour rules; these add the rest.
_EMOJI_WORDS = {
    "⚠": {"warning", "danger", "dangerous", "illegal", "banned", "scam", "scammed", "fraud", "risk", "beware"},
    "🔥": {"fire", "hot", "insane", "crazy", "viral", "wild", "burn", "burning", "explosive", "unreal"},
    "❤": {"love", "loved", "heart", "loves", "favorite", "favourite"},
    "⭐": {"best", "star", "famous", "legend", "legendary", "greatest", "perfect", "favourite"},
    "⚡": {"power", "powerful", "energy", "fast", "faster", "instantly", "instant", "shock", "shocking", "speed"},
    "📈": {"grow", "grows", "growth", "growing", "rise", "rising", "increase", "doubled", "tripled", "boost",
           "skyrocketed", "record"},
    "📉": {"fall", "falling", "drop", "dropped", "crash", "crashed", "decline", "collapsed", "plunged"},
    "✅": {"true", "proven", "works", "worked", "correct", "yes", "confirmed", "fixed"},
    "❌": {"wrong", "false", "mistake", "mistakes", "myth", "lie", "lied", "fail", "failed", "no"},
    "⏰": {"time", "minutes", "minute", "hours", "seconds", "years", "clock", "deadline", "midnight", "late"},
    "👀": {"look", "watch", "see", "secret", "secrets", "hidden", "notice", "spot"},
    "👑": {"king", "queen", "winner", "win", "wins", "won", "champion", "champions", "first", "crown"},
    "💯": {"percent", "hundred", "guaranteed", "absolutely"},
}
_WORD_EMOJI = {w: e for e, words in _EMOJI_WORDS.items() for w in words}
_PUNCT = re.compile(r"^[^\w$]+|[^\w?!%$]+$")


def emoji_for(word: str) -> str:
    """The emoji a caption word brings with it, "" for most words."""
    key = bare(word)
    if key in _WORD_EMOJI:
        return _WORD_EMOJI[key]
    kind = keyword_kind(word)
    if kind == "money":
        return "💰"
    if kind == "question" and word.rstrip("\"'”’)").endswith("?"):
        return "❓"
    return ""


def icon_of(emoji: str) -> tuple[str, str, str] | None:
    """The shape drawn for an emoji (or an icon's own name), None when there isn't one."""
    key = (emoji or "").strip().replace("️", "")
    return ICONS.get(EMOJI_ICONS.get(key[:1], key)) if key else None


# ---------------------------------------------------------------- words to a track

def colour_for(word: str, style: SubtitleStyle, emphasis: frozenset[str] | set[str] = frozenset()) -> str:
    """A word's own colour ("" = the style's, with the highlight while it's spoken)."""
    if emphasis and bare(word) in emphasis:
        return style.emphasis
    if not style.keyword_colors:
        return ""
    return {"money": style.money, "danger": style.danger, "question": style.question}.get(keyword_kind(word), "")


def build_track(words: list[Word], canvas: Canvas, style: SubtitleStyle | None = None, *, duration: float,
                emphasis: set[str] | frozenset[str] = frozenset(), track_id: str = SUBTITLES) -> Track:
    """A subtitle track from timed words (subtitles.Word: start, end, text), laid out on `canvas`."""
    style = style or SubtitleStyle()
    marked = frozenset(bare(w) for w in emphasis if w)
    timed = sorted((w for w in words if w.text.strip() and w.end > 0 and w.start < duration), key=lambda w: w.start)
    groups = _groups(timed, max(1, min(style.max_words, 3 if style.preset != "plain" else 12)))
    events: list[SubtitleEvent] = []
    last_icon = -1e9
    for g, group in enumerate(groups):
        start = max(0.0, group[0].start)
        if events and start < events[-1].end:  # word times that run into each other: the earlier caption gives way
            events[-1].end = start
            for w in events[-1].words:
                w.end = min(w.end, start)
                w.start = min(w.start, w.end)
        # The last words stay up until the next group starts, so a short pause never flashes an empty screen.
        nxt = groups[g + 1][0].start if g + 1 < len(groups) else duration
        end = min(duration, max(group[-1].end, min(nxt, group[-1].end + HOLD)))
        if end - start < 0.02:
            continue
        out: list[WordEvent] = []
        iconed = False
        for w in group:
            text = censor(w.text) if style.censor else w.text
            text = text.upper() if style.uppercase else text
            a = min(max(start, w.start), end)
            b = min(max(a, w.end), end)
            emoji = ""
            if style.emoji and not iconed and a - last_icon >= ICON_GAP:
                emoji = emoji_for(w.text)
                if emoji and icon_of(emoji):
                    iconed, last_icon = True, a
                else:
                    emoji = ""
            out.append(WordEvent(text, round(a, 3), round(b, 3), colour_for(w.text, style, marked), emoji,
                                 bare(w.text) in marked))
        events.append(SubtitleEvent(round(start, 3), round(end, 3), out))
    track = Track(track_id, "subtitle", events=events, style=style)
    layout(track, canvas)
    return track


# ---------------------------------------------------------------- where it all goes

class Geometry:
    """A style's sizes on a canvas, in output pixels."""

    def __init__(self, style: SubtitleStyle, canvas: Canvas):
        scale = min(canvas.width, canvas.height) / 1080
        self.font = style.font
        self.size = fonts.ass_size(style.font, style.size * scale)  # the ASS font size (line height)
        self.letter = style.size * scale
        self.bold = fonts.ass_bold(style.font)
        self.outline = max(0, round(style.outline * scale))
        self.shadow = round(2 * scale)
        self.margin_lr = round(MARGIN * min(canvas.width, canvas.height))
        bottom = style.position or (KINETIC_BOTTOM if canvas.vertical else LANDSCAPE_BOTTOM)
        self.bottom = round(canvas.height * bottom)
        self.margin_v = canvas.height - self.bottom
        self.max_width = (canvas.width - 2 * self.margin_lr) / ACTIVE_SCALE
        self.center_x = canvas.width / 2


def size_of(ev: SubtitleEvent, geo: Geometry) -> int:
    """The font size a caption is drawn at: the style's, or smaller when one of its words alone is too long
    for the line (a word can't wrap, and it must stay inside the margins even while it pops)."""
    widest = max((textlayout.text_width(geo.font, w.text, geo.size) for w in ev.words), default=0.0)
    if widest <= geo.max_width:
        return geo.size
    return max(8, int(geo.size * geo.max_width / widest))


def lines_of(ev: SubtitleEvent, geo: Geometry) -> list[list[int]]:
    """Line breaks for a caption (word indexes per line), measured with room for the spoken word's pop so
    the lines never change as the highlight moves along."""
    texts = [w.text for w in ev.words]
    out, k = [], 0
    for line in textlayout.break_lines(texts, geo.font, size_of(ev, geo), geo.max_width):
        out.append(list(range(k, k + len(line))))
        k += len(line)
    return out


def block_of(ev: SubtitleEvent, geo: Geometry) -> textlayout.Block:
    lines = [[(ev.words[i].text, 1.0, False) for i in line] for line in lines_of(ev, geo)]
    return textlayout.place(lines, geo.font, size_of(ev, geo), geo.center_x, geo.bottom, "bottom")


def layout(track: Track, canvas: Canvas) -> Track:
    """Fill in every word's box: [x, y, width, height] on the canvas, at rest."""
    geo = Geometry(track.style or SubtitleStyle(), canvas)
    for ev in track.events:
        if not ev.words:
            continue
        block = block_of(ev, geo)
        k = 0
        for line in block.lines:
            for placed in line.words:
                ev.words[k].box = [round(placed.x, 1), round(line.top, 1), round(placed.width, 1),
                                   round(line.height, 1)]
                k += 1
    return track


# ---------------------------------------------------------------- the .ass file

def _word_tags(word: WordEvent, style: SubtitleStyle, active: bool, first: bool, preset: str) -> str:
    colour = word.color or (style.highlight if active else style.color)
    tag = f"\\1c{_ass_colour(colour)}"
    a, b = POP_MS
    if preset == "bounce":
        if active and first:
            tag += f"\\fscx80\\fscy80\\t(0,{a},\\fscx128\\fscy128)\\t({a},{b},\\fscx96\\fscy96)\\t({b},230,\\fscx108\\fscy108)"
        elif active:
            tag += "\\fscx100\\fscy100\\t(0,70,\\fscx128\\fscy128)\\t(70,140,\\fscx96\\fscy96)\\t(140,220,\\fscx108\\fscy108)"
        elif first:
            tag += f"\\fscx80\\fscy80\\t(0,{a},\\fscx110\\fscy110)\\t({a},{b},\\fscx100\\fscy100)"
        else:
            tag += "\\fscx100\\fscy100"
        return tag
    final = round(ACTIVE_SCALE * 100) if active else 100
    if first:
        tag += f"\\fscx80\\fscy80\\t(0,{a},\\fscx110\\fscy110)\\t({a},{b},\\fscx{final}\\fscy{final})"
    elif active:
        tag += f"\\fscx100\\fscy100\\t(0,90,\\fscx{final}\\fscy{final})"
    else:
        tag += "\\fscx100\\fscy100"
    return tag


def _lines_text(ev: SubtitleEvent, lines: list[list[int]], render) -> str:
    return BREAK.join(" ".join(render(i) for i in line) for line in lines)


def _icon_event(word: WordEvent, ev: SubtitleEvent, block: textlayout.Block, geo: Geometry, style: SubtitleStyle,
                offset: float) -> str | None:
    icon = icon_of(word.emoji)
    if icon is None or ev.end <= offset:
        return None
    kind, shape, colour = icon
    size = geo.letter * 1.15  # the icon's height in pixels
    x = geo.center_x
    y = block.top - size * 0.62
    if y - size / 2 < size * 0.2:  # no room above: beside the first line instead
        y = block.top + size / 2
        x = max(size, block.lines[0].x - size * 0.8) if block.lines else x
    start = max(word.start, ev.start)
    settled = start < offset or style.preset == "plain"  # a still from inside the caption shows it settled
    head = (f"\\an5\\pos({x:.0f},{y:.0f})\\1c{_ass_colour(colour)}\\3c{_ass_colour(style.outline_color)}"
            f"\\bord{max(2, round(geo.outline * 0.7))}\\shad0")
    if kind == "draw":
        pct = size  # the shapes are 100 units tall, so the scale in % is the height in pixels
        move = (f"\\fscx{pct:.0f}\\fscy{pct:.0f}" if settled else
                f"\\fscx{pct * 0.3:.0f}\\fscy{pct * 0.3:.0f}\\frz-14\\t(0,110,\\fscx{pct * 1.22:.0f}\\fscy{pct * 1.22:.0f}"
                f"\\frz6)\\t(110,220,\\fscx{pct:.0f}\\fscy{pct:.0f}\\frz0)")
        body = f"{{{head}{move}\\p1}}{shape}{{\\p0}}"
    else:
        fs = round(geo.size * 1.25)
        move = ("" if settled else
                "\\fscx30\\fscy30\\frz-14\\t(0,110,\\fscx122\\fscy122\\frz6)\\t(110,220,\\fscx100\\fscy100\\frz0)")
        body = f"{{{head}\\fs{fs}{move}}}{_escape(shape)}"
    return f"Dialogue: 2,{_ts(start - offset)},{_ts(ev.end - offset)},Icon,,0,0,0,,{body}"


def to_ass(track: Track, canvas: Canvas, offset: float = 0.0) -> str:
    """The track as an .ass file. `offset` shifts every time earlier (a still from inside the video)."""
    style = track.style or SubtitleStyle()
    geo = Geometry(style, canvas)
    preset = style.preset
    base, hl = _ass_colour(style.color), _ass_colour(style.highlight)
    outline = _ass_colour(style.outline_color)
    lr = geo.margin_lr
    # Karaoke fills from the secondary colour to the primary as each word is said.
    primary, secondary = (hl, base) if preset == "karaoke" else (base, "&H000000FF")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {canvas.width}
PlayResY: {canvas.height}
WrapStyle: 2
ScaledBorderAndShadow: yes
Kerning: no

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{style.font},{geo.size},{primary},{secondary},{outline},&H80000000,{geo.bold},0,0,0,100,100,0,0,1,{geo.outline},{geo.shadow},2,{lr},{lr},{geo.margin_v},-1
Style: Icon,{style.font},{geo.size},&H00FFFFFF,&H000000FF,{outline},&H80000000,{geo.bold},0,0,0,100,100,0,0,1,{geo.outline},0,5,0,0,0,-1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    out: list[str] = []
    a, b = POP_MS
    for ev in track.events:
        if ev.end <= offset or not ev.words:
            continue
        lines = lines_of(ev, geo)
        size = size_of(ev, geo)
        small = f"{{\\fs{size}}}" if size != geo.size else ""  # a caption shrunk to fit a long word
        if preset == "plain":
            text = _lines_text(ev, lines, lambda i: (f"{{\\1c{_ass_colour(ev.words[i].color)}}}" if ev.words[i].color
                                                     else f"{{\\1c{base}}}") + _escape(ev.words[i].text))
            out.append(f"Dialogue: 0,{_ts(ev.start - offset)},{_ts(ev.end - offset)},Cap,,0,0,0,,{small}{text}")
        elif preset == "karaoke":
            def sung(i: int, ev=ev) -> str:
                w = ev.words[i]
                until = ev.words[i + 1].start if i + 1 < len(ev.words) else max(w.end, min(ev.end, w.end + 0.2))
                lead = ""
                if i == 0 and w.start - ev.start > 0.005:
                    lead = f"{{\\k{round((w.start - ev.start) * 100)}}}"
                colour = f"\\1c{_ass_colour(w.color)}" if w.color else f"\\1c{hl}"
                return f"{lead}{{\\kf{max(1, round((until - w.start) * 100))}{colour}}}{_escape(w.text)}"

            pop = "" if ev.start < offset else f"{{\\fscx80\\fscy80\\t(0,{a},\\fscx110\\fscy110)\\t({a},{b},\\fscx100\\fscy100)}}"
            out.append(f"Dialogue: 0,{_ts(ev.start - offset)},{_ts(ev.end - offset)},Cap,,0,0,0,,"
                       f"{small}{pop}{_lines_text(ev, lines, sung)}")
        else:  # pop, bounce: one event for each word's turn
            for k, word in enumerate(ev.words):
                start = ev.start if k == 0 else max(ev.start, word.start)
                end = ev.words[k + 1].start if k + 1 < len(ev.words) else ev.end
                end = min(max(end, start), ev.end)
                if end - start < 0.005 or end <= offset:
                    continue
                first = k == 0 and start >= offset
                text = _lines_text(ev, lines, lambda i, k=k, first=first: "{" + _word_tags(
                    ev.words[i], style, i == k, first, preset) + "}" + _escape(ev.words[i].text))
                out.append(f"Dialogue: 0,{_ts(start - offset)},{_ts(end - offset)},Cap,,0,0,0,,{small}{text}")
        if style.emoji:
            block = None
            for word in ev.words:
                if word.emoji:
                    block = block or block_of(ev, geo)
                    line = _icon_event(word, ev, block, geo, style, offset)
                    if line:
                        out.append(line)
    return header + "\n".join(out) + "\n"


def fonts_used(track: Track) -> list[str]:
    return [(track.style or SubtitleStyle()).font]


def from_text(text: str, start: float, end: float) -> list[Word]:
    """Evenly timed words for a line with no word times (a caption typed by hand)."""
    words = text.split()
    if not words or end <= start:
        return []
    step = (end - start) / len(words)
    return [Word(round(start + i * step, 3), round(start + (i + 1) * step, 3), w) for i, w in enumerate(words)]


__all__ = ["ICONS", "EMOJI_ICONS", "DANGER", "Geometry", "build_track", "layout", "to_ass", "emoji_for", "icon_of",
           "colour_for", "fonts_used", "from_text"]

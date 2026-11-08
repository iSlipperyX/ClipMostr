"""Original framing drawn over the clip: a hook card that opens it and a context lower-third.

They're what makes a clip more than a re-upload: an editor's headline that frames why this moment matters,
and a tag that says where it's from and what it's about, crediting the source.

* **Hook card** (first 1.8 s): the picture dims behind a bold headline (the AI's hook, else the clip's
  title) that punches in word by word, then the card fades away as the speaker carries on. The clip keeps
  playing under it: a static title card loses viewers in the first second.
* **Lower-third** (from about 2 s to 7 s): a slim panel with an accent bar that slides in from the left,
  showing the topic on top and "SOURCE · <channel>" under it, then slides out.

Both are ASS events (libass draws them), placed in TikTok's safe zone (layout.safe_box): nothing under the
buttons on the right or the caption text at the bottom.
"""

from __future__ import annotations

from .. import fonts, layout, textlayout

INTRO_FADE_MS = 260
LOWER_START = 2.2
LOWER_LENGTH = 4.8


def _colour(hex_rgb: str, alpha: int = 0) -> str:
    h = (hex_rgb or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _ts(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def headline(clip, s) -> str:
    text = (getattr(clip, "hook", "") or getattr(clip, "title", "") or "").strip()
    return text.upper() if getattr(s, "caption_uppercase", True) else text


def context_lines(s, clip, channel: str, topic: str) -> tuple[str, str]:
    """(topic, credit) for the lower-third. `lower_third_text` may use {topic}, {channel} and {title}."""
    template = (getattr(s, "lower_third_text", "") or "{topic}").strip()
    title = getattr(clip, "title", "") or ""
    top = template.replace("{topic}", topic or title).replace("{channel}", channel).replace("{title}", title)
    top = " ".join(top.split())
    credit = f"SOURCE · {channel.upper()}" if channel else ""
    return top[:48], credit[:48]


def build(width: int, height: int, s, clip, *, intro: bool, lower: bool, channel: str = "", topic: str = "",
          duration: float = 60.0, offset: float = 0.0, top_reserved: int = 0) -> str:
    """The cards .ass file ("" when there's nothing to draw). `offset` shifts times for a still from inside
    the clip; `top_reserved` keeps the headline below a top banner."""
    safe = layout.safe_box(width, height) if height > width else layout.Box(0, 0, width, height)
    scale = min(width, height) / 1080
    font = getattr(s, "banner_font", "") or "TikTok Sans Black"
    head_size = fonts.ass_size(font, 78 * scale)
    tag_size = fonts.ass_size(font, 46 * scale)
    credit_size = fonts.ass_size(font, 30 * scale)
    accent = getattr(s, "caption_highlight", "#FFD400")
    events: list[str] = []
    intro_len = max(0.8, min(4.0, float(getattr(s, "intro_seconds", 1.8) or 1.8)))
    text = headline(clip, s)
    if intro and text and intro_len > offset:
        a, b = 0.0 - offset, intro_len - offset
        # The dimmed picture: a full-frame box, fading in quickly and out with the headline.
        box = f"m 0 0 l {width} 0 {width} {height} 0 {height}"
        events.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Card,,0,0,0,,"
                      f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H000000&\\alpha&HFF&\\t(0,120,\\alpha&H70&)"
                      f"\\fad(0,{INTRO_FADE_MS})\\p1}}{box}{{\\p0}}")
        lines = textlayout.break_lines(text.split(), font, head_size, safe.w * 0.92)
        top = max(safe.y + int(height * 0.08), top_reserved + int(height * 0.03))
        y = top + int((safe.h * 0.42 - len(lines) * head_size) / 2) + head_size
        delay = 0
        for ln in lines:
            words = []
            for w in ln:
                # Each word punches in a beat after the last, then the whole card settles.
                words.append(f"{{\\alpha&HFF&\\fscx130\\fscy130\\t({delay},{delay + 1},\\alpha&H00&)"
                             f"\\t({delay},{delay + 140},\\fscx100\\fscy100)}}{_escape(w)}")
                delay += 70
            events.append(f"Dialogue: 3,{_ts(a)},{_ts(b)},Headline,,0,0,0,,"
                          f"{{\\an2\\pos({width // 2},{y})\\fad(0,{INTRO_FADE_MS})}}" + " ".join(words))
            y += head_size
        # An accent rule under the headline
        rule_w = int(safe.w * 0.22)
        events.append(f"Dialogue: 3,{_ts(a)},{_ts(b)},Card,,0,0,0,,"
                      f"{{\\an8\\pos({width // 2},{y + int(14 * scale)})\\bord0\\shad0\\1c{_colour(accent)}"
                      f"\\fscx0\\t(80,380,\\fscx100)\\fad(0,{INTRO_FADE_MS})\\p1}}"
                      f"m 0 0 l {rule_w} 0 {rule_w} {int(10 * scale)} 0 {int(10 * scale)}{{\\p0}}")
    top_line, credit = context_lines(s, clip, channel, topic)
    start = max(LOWER_START, (intro_len + 0.3) if intro else LOWER_START)
    end = min(start + LOWER_LENGTH, duration - 0.5)
    if lower and (top_line or credit) and end - start > 1.5 and end > offset:
        a, b = start - offset, end - offset
        x = safe.x + int(8 * scale)
        # Above the captions: the bottom of the safe zone, less room for two caption lines.
        bottom = safe.bottom - int(height * 0.20) if height > width else int(height * 0.78)
        pad = int(20 * scale)
        tw = max(fonts.metrics(font).width(top_line, tag_size), fonts.metrics(font).width(credit, credit_size))
        panel_w = int(min(safe.w * 0.86, tw + 2 * pad + 12 * scale))
        panel_h = int(tag_size + credit_size + 2.4 * pad)
        y0 = bottom - panel_h
        slide = int(40 * scale)
        move = f"\\move({x - slide},{y0},{x},{y0},0,240)"
        events.append(f"Dialogue: 4,{_ts(a)},{_ts(b)},Card,,0,0,0,,"
                      f"{{\\an7{move}\\bord0\\shad0\\1c&H000000&\\1a&H38&\\fad(200,300)\\p1}}"
                      f"m 0 0 l {panel_w} 0 {panel_w} {panel_h} 0 {panel_h}{{\\p0}}")
        bar = int(8 * scale)
        events.append(f"Dialogue: 5,{_ts(a)},{_ts(b)},Card,,0,0,0,,"
                      f"{{\\an7{move}\\bord0\\shad0\\1c{_colour(accent)}\\fad(200,300)\\p1}}"
                      f"m 0 0 l {bar} 0 {bar} {panel_h} 0 {panel_h}{{\\p0}}")
        tx = x + bar + pad
        if top_line:
            events.append(f"Dialogue: 6,{_ts(a)},{_ts(b)},Tag,,0,0,0,,"
                          f"{{\\an7\\move({tx - slide},{y0 + pad},{tx},{y0 + pad},0,240)\\fad(200,300)}}"
                          f"{_escape(top_line)}")
        if credit:
            cy = y0 + pad + tag_size + int(pad * 0.3)
            events.append(f"Dialogue: 6,{_ts(a)},{_ts(b)},Credit,,0,0,0,,"
                          f"{{\\an7\\move({tx - slide},{cy},{tx},{cy},0,240)\\fad(200,300)}}{_escape(credit)}")
    if not events:
        return ""
    bold = fonts.ass_bold(font)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
Kerning: no

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Card,{font},20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,-1
Style: Headline,{font},{head_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,{max(2, int(5 * scale))},{max(1, int(3 * scale))},2,0,0,0,-1
Style: Tag,{font},{tag_size},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,0,0,7,0,0,0,-1
Style: Credit,{font},{credit_size},{_colour('#D0D4DC')},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,1,0,1,0,0,7,0,0,0,-1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    return header + "\n".join(events) + "\n"

"""The reel's graphics, drawn by libass over the 1080x1920 picture: the opening's hook banner, a score bug, each
play's countdown label and player badge, and the final score.

* **Hook banner** (the opening): the picture darkens toward the top, the hook punches in word by word with a
  glowing accent edge, and a small line under it says what this is ("TOP 5 PLAYS · LAKERS AT CELTICS").
* **Score bug** (every play): a slim dark bar at the top: the teams and the score as the broadcast showed it at
  the start of the play, changing to the score after it once the moment has passed; just the teams when the
  score couldn't be read (scoreboard.py). Never a made-up number.
* **Countdown label**: "PLAY 4" slams in big as each play starts, then shrinks to a chip under the score bug for
  the rest of the play. The last is "NUMBER 1" or "THE GAME WINNER".
* **Player badge**: a lower-third for the first few seconds of each play: who and what ("STEPH CURRY · 34-FOOT
  DAGGER").
* **Final**: over the last seconds, "FINAL" and the score, when the title gave it.

Everything sits inside TikTok's safe zone (layout.safe_box): nothing under the buttons on the right, the caption
at the bottom or the tabs at the top.
"""

from __future__ import annotations

from .. import fonts, layout, textlayout
from ..rendering.cards import _colour, _escape, _ts
from .montage import Reel

W, H = 1080, 1920
LABEL_BIG = 1.25  # seconds the countdown label shows big before it shrinks to a chip
BADGE_FROM, BADGE_FOR = 0.55, 2.6
FINAL_FOR = 2.8
SCORE_SWITCH = 1.6  # seconds after a play's moment the bug shows the score after it


def build(reel: Reel, scores: dict[str, tuple] | None = None, *, teams: tuple[str, str] = ("", ""),
          matchup: str = "", font: str = "TikTok Sans Black", accent: str = "#FFD400",
          score_bug: bool = True) -> str:
    """The .ass file for the reel. `scores` maps a play's highlight id to (score at its start, after it), each a
    scoreboard.Score or None; `teams` are the names for a bug without a score. `score_bug` False leaves the score
    bug out (the editor's "Score bug" switch)."""
    scores = scores or {}
    safe = layout.safe_box(W, H)
    head = fonts.ass_size(font, 84)
    sub = fonts.ass_size(font, 34)
    bug = fonts.ass_size(font, 40)
    bug_small = fonts.ass_size(font, 26)
    label = fonts.ass_size(font, 132)
    badge = fonts.ass_size(font, 44)
    m = fonts.metrics(font)
    acc = _colour(accent)
    ev: list[str] = []
    starts = reel.starts()
    half = reel.transition / 2

    # --- the opening -----------------------------------------------------------------------------------------
    if reel.segments and reel.segments[0].kind == "teaser":
        seg = reel.segments[0]
        a, b = 0.0, starts[0] + seg.length - half
        shade = f"m 0 0 l {W} 0 {W} {int(H * 0.55)} 0 {int(H * 0.55)}"
        ev.append(f"Dialogue: 1,{_ts(a)},{_ts(b)},Box,,0,0,0,,{{\\an7\\pos(0,0)\\1c&H000000&\\1a&H50&\\blur40"
                  f"\\fad(0,180)\\p1}}{shade}{{\\p0}}")
        text = (reel.story.hook or "").upper()
        lines = textlayout.break_lines(text.split(), font, head, safe.w * 0.94) if text else []
        y = safe.y + int(H * 0.11) + head
        delay = 0
        for ln in lines:
            words = []
            for w in ln:
                words.append(f"{{\\alpha&HFF&\\fscx140\\fscy140\\t({delay},{delay + 1},\\alpha&H00&)"
                             f"\\t({delay},{delay + 150},\\fscx100\\fscy100)}}{_escape(w)}")
                delay += 85
            body = " ".join(words)
            # A soft glow in the accent colour behind the white words.
            ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Glow,,0,0,0,,{{\\an2\\pos({W // 2},{y})\\3c{acc}\\blur14"
                      f"\\fad(0,180)}}{body}")
            ev.append(f"Dialogue: 3,{_ts(a)},{_ts(b)},Hook,,0,0,0,,{{\\an2\\pos({W // 2},{y})\\fad(0,180)}}{body}")
            y += int(head * 1.02)
        n = len(reel.plays)
        tag = f"TOP {n} PLAYS" + (f" · {matchup.upper()}" if matchup else "")
        tag = _fit(tag, font, sub, safe.w * 0.9)
        rule = int(safe.w * 0.18)
        ev.append(f"Dialogue: 3,{_ts(a)},{_ts(b)},Box,,0,0,0,,{{\\an8\\pos({W // 2},{y + 16})\\1c{acc}\\fscx0"
                  f"\\t(120,420,\\fscx100)\\fad(0,180)\\p1}}m 0 0 l {rule} 0 {rule} 8 0 8{{\\p0}}")
        ev.append(f"Dialogue: 3,{_ts(a)},{_ts(b)},Small,,0,0,0,,{{\\an8\\pos({W // 2},{y + 40})\\alpha&HFF&"
                  f"\\t(250,450,\\alpha&H00&)\\fad(0,180)}}{_escape(tag)}")

    # --- the plays -------------------------------------------------------------------------------------------
    plays = [(i, s) for i, s in enumerate(reel.segments) if s.kind == "play"]
    bug_y = safe.y + 18
    for k, (i, seg) in enumerate(plays):
        a = starts[i] + (half if i else 0.0)
        b = starts[i] + seg.length - (half if i < len(reel.segments) - 1 else 0.0)
        moment = a + (seg.moment - seg.start)
        before, after = scores.get(seg.hid, (None, None))
        # The score bug: the score at the start, then after the moment.
        switch = min(b - 0.3, moment + SCORE_SWITCH)
        if before is not None and after is not None and after.text() != before.text() and switch > a + 0.5:
            texts = [(a, switch, before), (switch, b, after)]
        else:
            texts = [(a, b, before or after)]
        for j, (t0, t1, sc) in enumerate(texts):
            if t1 - t0 < 0.2 or not score_bug:
                continue
            ev += _bug(t0, t1, sc, teams, font, bug, bug_small, acc, bug_y, m, fade_in=j == 0, fade_out=j == len(texts) - 1)
        # The countdown label: big, then a chip under the bug.
        if seg.label:
            big_end = min(b, a + LABEL_BIG)
            chip_y = bug_y + int(bug * 1.9)
            ev.append(f"Dialogue: 6,{_ts(a)},{_ts(big_end)},Label,,0,0,0,,{{\\an5\\pos({W // 2},{int(H * 0.36)})"
                      f"\\fscx160\\fscy160\\alpha&HFF&\\t(0,60,\\alpha&H00&)\\t(0,170,\\fscx100\\fscy100)"
                      f"\\3c{acc}\\fad(0,120)}}{_escape(seg.label)}")
            if b - big_end > 1.0:
                cw = int(m.width(seg.label, bug_small) + 36)
                ev.append(f"Dialogue: 5,{_ts(big_end)},{_ts(b)},Box,,0,0,0,,{{\\an8\\pos({W // 2},{chip_y})\\1c{acc}"
                          f"\\fad(150,150)\\p1}}m 0 0 l {cw} 0 {cw} {int(bug_small * 1.5)} 0 {int(bug_small * 1.5)}"
                          f"{{\\p0}}")
                ev.append(f"Dialogue: 6,{_ts(big_end)},{_ts(b)},Chip,,0,0,0,,{{\\an8\\pos({W // 2},"
                          f"{chip_y + int(bug_small * 0.2)})\\fad(150,150)}}{_escape(seg.label)}")
        # The player badge.
        text = (reel.story.badges.get(seg.hid) or "").strip()
        if text:
            t0, t1 = a + BADGE_FROM, min(b - 0.2, a + BADGE_FROM + BADGE_FOR)
            if t1 - t0 > 1.0:
                ev += _badge(t0, t1, text, font, badge, acc, safe, m)
    # --- the final score -------------------------------------------------------------------------------------
    if plays and reel.final:
        i, seg = plays[-1]
        end = starts[i] + seg.length
        a = max(starts[i] + 2.0, end - FINAL_FOR)
        y = int(H * 0.60)
        ev.append(f"Dialogue: 7,{_ts(a)},{_ts(end)},Small,,0,0,0,,{{\\an2\\pos({W // 2},{y - int(head * 0.95)})"
                  f"\\1c{acc}\\fad(200,0)}}FINAL")
        line = _fit(reel.final.upper(), font, head * 0.8, safe.w)
        ev.append(f"Dialogue: 7,{_ts(a)},{_ts(end)},Final,,0,0,0,,{{\\an2\\pos({W // 2},{y})\\fscx80\\fscy80"
                  f"\\t(0,180,\\fscx100\\fscy100)\\fad(120,0)}}{_escape(line)}")
    return _header(font, head, sub, bug, bug_small, label, badge, acc) + "\n".join(ev) + "\n"


def _fit(text: str, font: str, size: float, width: float) -> str:
    m = fonts.metrics(font)
    while text and m.width(text, size) > width:
        text = text[:-2].rstrip(" ·") + "…" if len(text) > 3 else text[:-1]
    return text


def _bug(a: float, b: float, sc, teams: tuple[str, str], font: str, size: int, small: int, acc: str, y: int, m,
         *, fade_in: bool, fade_out: bool) -> list[str]:
    if sc is not None:
        main = f"{sc.left}  {sc.left_score}  |  {sc.right_score}  {sc.right}"
        tail = " · ".join(x for x in (sc.period, sc.clock) if x).upper()
    else:
        main = "  vs  ".join(x for x in teams if x) or ""
        tail = ""
    if not main:
        return []
    main = _fit(main.upper(), font, size, W * 0.62)
    w_main = m.width(main, size) * 1.06  # (bold heavy type runs a little wider than measured)
    w_tail = m.width(tail, small) * 1.06 if tail else 0
    pad = 26
    panel_w = int(w_main + (w_tail + 34 if tail else 0) + 2 * pad + 10)
    panel_h = int(size * 1.55)
    x0 = (W - panel_w) // 2
    fad = f"\\fad({220 if fade_in else 0},{220 if fade_out else 0})"
    out = [f"Dialogue: 4,{_ts(a)},{_ts(b)},Box,,0,0,0,,{{\\an7\\pos({x0},{y})\\1c&H101014&\\1a&H20&{fad}\\p1}}"
           f"{_round_rect(panel_w, panel_h, 14)}{{\\p0}}",
           f"Dialogue: 5,{_ts(a)},{_ts(b)},Box,,0,0,0,,{{\\an7\\pos({x0},{y})\\1c{acc}{fad}\\p1}}"
           f"m 0 0 l 10 0 10 {panel_h} 0 {panel_h}{{\\p0}}",
           f"Dialogue: 6,{_ts(a)},{_ts(b)},Bug,,0,0,0,,{{\\an4\\pos({x0 + pad + 10},{y + panel_h // 2}){fad}}}"
           f"{_escape(main)}"]
    if tail:
        out.append(f"Dialogue: 6,{_ts(a)},{_ts(b)},BugSmall,,0,0,0,,{{\\an6\\pos({x0 + panel_w - pad},"
                   f"{y + panel_h // 2}){fad}}}{_escape(tail)}")
    return out


def _badge(a: float, b: float, text: str, font: str, size: int, acc: str, safe, m) -> list[str]:
    text = _fit(text.upper(), font, size, safe.w * 0.82)
    pad = 22
    w = int(m.width(text, size) * 1.06 + 2 * pad + 14)  # (bold heavy type runs a little wider than measured)
    h = int(size * 1.6)
    x = safe.x + 6
    y = safe.bottom - int(H * 0.17) - h
    slide = 50
    mv = f"\\move({x - slide},{y},{x},{y},0,220)"
    return [f"Dialogue: 8,{_ts(a)},{_ts(b)},Box,,0,0,0,,{{\\an7{mv}\\1c&H000000&\\1a&H30&\\fad(180,240)\\p1}}"
            f"m 0 0 l {w} 0 {w} {h} 0 {h}{{\\p0}}",
            f"Dialogue: 9,{_ts(a)},{_ts(b)},Box,,0,0,0,,{{\\an7{mv}\\1c{acc}\\fad(180,240)\\p1}}"
            f"m 0 0 l 10 0 10 {h} 0 {h}{{\\p0}}",
            f"Dialogue: 9,{_ts(a)},{_ts(b)},Badge,,0,0,0,,{{\\an4\\move({x + 14 + pad - slide},{y + h // 2},"
            f"{x + 14 + pad},{y + h // 2},0,220)\\fad(180,240)}}{_escape(text)}"]


def _round_rect(w: int, h: int, r: int) -> str:
    r = min(r, h // 2)
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} l 0 {r} b 0 0 0 0 {r} 0")


def _header(font: str, head: int, sub: int, bug: int, bug_small: int, label: int, badge: int, acc: str) -> str:
    bold = fonts.ass_bold(font)
    white = "&H00FFFFFF"
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes
Kerning: no

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Box,{font},20,{white},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,-1
Style: Hook,{font},{head},{white},&H000000FF,&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,4,3,2,0,0,0,-1
Style: Glow,{font},{head},&HFF000000,&H000000FF,{acc},&HFF000000,{bold},0,0,0,100,100,0,0,1,9,0,2,0,0,0,-1
Style: Small,{font},{sub},{white},&H000000FF,&H00000000,&H80000000,{bold},0,0,0,100,100,2,0,1,2,2,8,0,0,0,-1
Style: Bug,{font},{bug},{white},&H000000FF,&H00000000,&H00000000,{bold},0,0,0,100,100,1,0,1,0,0,4,0,0,0,-1
Style: BugSmall,{font},{bug_small},{_colour('#C8CDD6')},&H000000FF,&H00000000,&H00000000,{bold},0,0,0,100,100,1,0,1,0,0,6,0,0,0,-1
Style: Label,{font},{label},{white},&H000000FF,&H00000000,&H90000000,{bold},0,0,0,100,100,2,0,1,6,5,5,0,0,0,-1
Style: Chip,{font},{bug_small},&H00101014,&H000000FF,&H00000000,&H00000000,{bold},0,0,0,100,100,1,0,1,0,0,8,0,0,0,-1
Style: Badge,{font},{badge},{white},&H000000FF,&H00000000,&H00000000,{bold},0,0,0,100,100,1,0,1,0,0,4,0,0,0,-1
Style: Final,{font},{int(head * 0.8)},{white},&H000000FF,&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,5,3,2,0,0,0,-1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

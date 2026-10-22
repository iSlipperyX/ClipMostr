"""Line breaking and placement for burned-in text, computed the way libass lays it out.

ClipMonster decides every line break itself (the .ass files use WrapStyle 2, so libass never wraps on its
own), and this module says exactly where each line and word lands. The export writes those breaks into the
.ass file; the editor's export view draws the same lines at the same places. Units are output pixels."""

from __future__ import annotations

from dataclasses import dataclass

from . import fonts


@dataclass
class Word:
    text: str
    x: float  # left edge of the word's advance box
    width: float  # already scaled
    scale: float = 1.0
    highlight: bool = False


@dataclass
class Line:
    words: list[Word]
    top: float
    height: float
    baseline: float
    x: float
    width: float


@dataclass
class Block:
    font: str
    size: int  # ASS font size (line height)
    lines: list[Line]

    @property
    def em(self) -> float:
        return self.size * fonts.metrics(self.font).em_ratio

    @property
    def top(self) -> float:
        return self.lines[0].top if self.lines else 0.0

    @property
    def bottom(self) -> float:
        return self.lines[-1].top + self.lines[-1].height if self.lines else 0.0


def text_width(font: str, text: str, size: float) -> float:
    return fonts.metrics(font).width(text, size)


def break_lines(words: list[str], font: str, size: float, max_width: float) -> list[list[str]]:
    """Fewest lines that fit `max_width`, then as even as possible, so a last word never dangles alone.
    A single word wider than the limit gets a line to itself."""
    if not words:
        return []
    m = fonts.metrics(font)
    widths = [m.width(w, size) for w in words]
    space = m.width(" ", size)

    def greedy(limit: float) -> list[list[int]]:
        lines: list[list[int]] = [[]]
        cur = 0.0
        for i, w in enumerate(widths):
            add = w if not lines[-1] else space + w
            if lines[-1] and cur + add > limit:
                lines.append([i])
                cur = w
            else:
                lines[-1].append(i)
                cur += add
        return lines

    def widest(lines: list[list[int]]) -> float:
        return max(sum(widths[i] for i in ln) + space * (len(ln) - 1) for ln in lines)

    best = greedy(max_width)
    n = len(best)
    if n > 1:  # shrink the limit while the line count holds, to even the lines out
        lo, hi = max(widths), widest(best)
        for _ in range(20):
            mid = (lo + hi) / 2
            if len(greedy(mid)) <= n:
                hi = mid
            else:
                lo = mid
        best = greedy(hi)
    return [[words[i] for i in ln] for ln in best]


def place(lines: list[list[tuple[str, float, bool]]], font: str, size: int, center_x: float, anchor_y: float,
          valign: str) -> Block:
    """Stack lines of (word, scale, highlight) centred on `center_x`. valign 'bottom' puts the block's bottom
    at anchor_y, 'top' its top, 'middle' its centre. A scaled word makes its line taller, as in libass."""
    m = fonts.metrics(font)
    asc = size * m.ascent_ratio
    desc = size - asc
    space = m.width(" ", size)
    built: list[Line] = []
    for ln in lines:
        top_scale = max((sc for _, sc, _ in ln), default=1.0)
        words = []
        x = 0.0
        for j, (text, sc, hl) in enumerate(ln):
            if j:
                x += space
            w = m.width(text, size) * sc
            words.append(Word(text, x, w, sc, hl))
            x += w
        built.append(Line(words, 0.0, (asc + desc) * top_scale, asc * top_scale, 0.0, x))
    total = sum(ln.height for ln in built)
    y = {"bottom": anchor_y - total, "top": anchor_y}.get(valign, anchor_y - total / 2)
    for ln in built:
        ln.top = y
        ln.baseline = y + ln.baseline
        ln.x = center_x - ln.width / 2
        for w in ln.words:
            w.x += ln.x
        y += ln.height
    return Block(font, size, built)


def ass_lines(lines: list[list[str]]) -> str:
    """Lines as .ass text with explicit breaks (the caller escapes and styles the words)."""
    return "\\N".join(" ".join(ln) for ln in lines)

"""Draws burned-in text the way libass does, so the editor's export view shows exactly what the export will.

Everything is in output pixels: the caller sets the painter's transform to map the output frame onto the
screen. Word positions come from ytclip.textlayout (the same numbers the .ass files are written from);
this module only turns them into glyph outlines, borders and shadows."""

from __future__ import annotations


from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPainterPathStroker, QTransform

from .. import textlayout
from . import qt_fonts

_REF = 512  # glyph outlines are taken at this pixel size and scaled, so any size is exact


def _ref_font(name: str) -> QFont:
    f = qt_fonts.qfont(name, _REF)
    f.setKerning(False)  # the .ass files turn kerning off too, so advances are the font's own
    f.setHintingPreference(QFont.PreferNoHinting)
    return f


def _key(block: textlayout.Block) -> tuple:
    return block.font, block.size, tuple((w.text, round(w.x, 3), round(ln.baseline, 3), w.scale, w.highlight)
                                         for ln in block.lines for w in ln.words)


def text_path(block: textlayout.Block) -> list[tuple[QPainterPath, textlayout.Word]]:
    """One outline per word, placed where libass puts it."""
    return _word_paths(_key(block), block)


_cache: dict[tuple, list] = {}  # outlines by what they show, so text that stays put is built once


def _cached(key: tuple, build):
    hit = _cache.get(key)
    if hit is None:
        if len(_cache) > 400:
            _cache.clear()
        hit = _cache[key] = build()
    return hit


def _word_paths(k: tuple, block: textlayout.Block) -> list[tuple[QPainterPath, textlayout.Word]]:
    return _cached(("words", k), lambda: _build_words(block))


def _build_words(block: textlayout.Block) -> list[tuple[QPainterPath, textlayout.Word]]:
    font = _ref_font(block.font)
    out = []
    for line in block.lines:
        for w in line.words:
            em = block.em * w.scale
            p = QPainterPath()
            p.addText(QPointF(0, 0), font, w.text)
            k = em / _REF
            p = QTransform(k, 0, 0, k, w.x, line.baseline).map(p)
            p.setFillRule(Qt.WindingFill)  # variable fonts overlap their contours; odd-even would punch holes
            out.append((p, w))
    return out


def _border_paths(k: tuple, block: textlayout.Block, outline: float) -> list[QPainterPath]:
    """Each word grown by its border (outline scaled with the word's pop, as \\fscx/\\fscy do in libass).
    Building these is the slow part of drawing text, so they're kept while the words stay put."""
    return _cached(("border", k, outline),
                   lambda: [_bordered(path, outline * w.scale) for path, w in _word_paths(k, block)])


def _bordered(path: QPainterPath, width: float) -> QPainterPath:
    if width <= 0:
        return path
    st = QPainterPathStroker()
    st.setWidth(2 * width)
    st.setJoinStyle(Qt.RoundJoin)
    st.setCapStyle(Qt.RoundCap)
    return st.createStroke(path).united(path)


def paint_block(g: QPainter, block: textlayout.Block, colour: QColor, *, highlight: QColor | None = None,
                outline: float = 0, outline_colour: QColor = QColor(0, 0, 0), shadow: float = 0,
                shadow_colour: QColor = QColor(0, 0, 0, 128)) -> None:
    """Fill, border and drop shadow, in libass's order: shadow under border under fill. A word's border
    and shadow grow with its pop scale, as \\fscx/\\fscy scale them in libass."""
    g.save()
    g.setRenderHint(QPainter.Antialiasing, True)
    g.setPen(Qt.NoPen)
    k = _key(block)
    words = _word_paths(k, block)
    borders = _border_paths(k, block, outline) if outline > 0 or shadow > 0 else [p for p, _ in words]
    if shadow > 0:
        g.setBrush(shadow_colour)
        for path, (_, w) in zip(borders, words):
            g.drawPath(path.translated(shadow * w.scale, shadow * w.scale))
    if outline > 0:
        g.setBrush(outline_colour)
        for path in borders:
            g.drawPath(path)
    for path, w in words:
        g.setBrush(highlight if w.highlight and highlight is not None else colour)
        g.drawPath(path)
    g.restore()


def paint_boxed(g: QPainter, block: textlayout.Block, colour: QColor, box_colour: QColor, pad: float) -> None:
    """ASS BorderStyle 3: an opaque box behind each line, `pad` pixels bigger than the line all round."""
    g.save()
    g.setRenderHint(QPainter.Antialiasing, True)
    g.setPen(Qt.NoPen)
    g.setBrush(QBrush(box_colour))
    for line in block.lines:
        g.drawRect(QRectF(line.x - pad, line.top - pad, line.width + 2 * pad, line.height + 2 * pad))
    g.setBrush(colour)
    for path, _ in text_path(block):
        g.drawPath(path)
    g.restore()


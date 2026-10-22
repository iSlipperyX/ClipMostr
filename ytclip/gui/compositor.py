"""Paints a whole exported frame (videos, bars, banner words, captions, title) from a Composition, so the
editor's export view shows what the render will look like while the video plays."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen

from .. import captions
from ..compose import BLUR_BRIGHTNESS, BLUR_SIGMA, Composition, Placement
from . import overlay


def _box_blur(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    """Running mean over 2r+1 samples along axis 0 or 1, edges repeated."""
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float32)
    n = a.shape[axis]
    if axis == 0:
        return (c[2 * r + 1:2 * r + 1 + n] - c[:n]) * (1 / (2 * r + 1))
    return (c[:, 2 * r + 1:2 * r + 1 + n] - c[:, :n]) * (1 / (2 * r + 1))


# BT.601 RGB <-> YCbCr (full range): ffmpeg blurs the fill in YUV 4:2:0, where the colour planes are half
# size, so the same sigma spreads colour twice as far as brightness. Doing the same keeps the look identical.
_TO_YUV = np.array([[0.299, 0.587, 0.114], [-0.168736, -0.331264, 0.5], [0.5, -0.418688, -0.081312]], np.float32)
_TO_RGB = np.linalg.inv(_TO_YUV).astype(np.float32)


_GBLUR_SPREAD = 0.8  # ffmpeg's one-step gblur spreads less than a true Gaussian of its sigma (measured)


def _radius(sigma: float) -> int:
    """Box radius whose three passes approximate a Gaussian of `sigma`."""
    return max(1, round(((12 * sigma * sigma / 3 + 1) ** 0.5 - 1) / 2))


def blurred(img: QImage, w: int, h: int, sigma: float = BLUR_SIGMA) -> QImage:
    """The blurred, darkened fill, as ffmpeg's gblur + eq make it, at the same w x h."""
    sigma *= _GBLUR_SPREAD
    small = img.convertToFormat(QImage.Format_RGB888).scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    a = np.frombuffer(small.constBits(), np.uint8).reshape(h, small.bytesPerLine())[:, :w * 3]
    yuv = a.reshape(h, w, 3).astype(np.float32) @ _TO_YUV.T
    luma, chroma = yuv[:, :, :1], yuv[:, :, 1:]
    for _ in range(3):
        luma = _box_blur(_box_blur(luma, _radius(sigma), 0), _radius(sigma), 1)
        chroma = _box_blur(_box_blur(chroma, _radius(2 * sigma), 0), _radius(2 * sigma), 1)
    # eq's brightness shifts 8-bit (limited range) luma; in full range that's 255/219 times as much.
    luma = luma + BLUR_BRIGHTNESS * 256 * 255 / 219
    out = np.clip(np.concatenate([luma, chroma], axis=2) @ _TO_RGB.T, 0, 255).astype(np.uint8)
    out = np.ascontiguousarray(out)
    return QImage(out.data, w, h, w * 3, QImage.Format_RGB888).copy()


class FramePainter:
    """Holds the blurred fill between frames: it changes slowly, so it's refreshed at most `blur_fps` times
    a second rather than on every frame."""

    def __init__(self, blur_fps: float = 10.0):
        self.blur_every = 1.0 / blur_fps
        self._blur: dict[int, tuple[float, QImage]] = {}

    def reset(self) -> None:
        self._blur.clear()

    def _fill_image(self, i: int, p: Placement, frame: QImage, src_w: int, clock: float | None) -> QImage:
        cached = self._blur.get(i)
        if cached is not None and clock is not None and 0 <= clock - cached[0] < self.blur_every:
            return cached[1]
        bw, bh = max(2, int(p.clip.w) // 4 // 2 * 2), max(2, int(p.clip.h) // 4 // 2 * 2)
        k = frame.width() / max(1, src_w)
        src = frame.copy(round(p.src.x * k), round(p.src.y * k), max(1, round(p.src.w * k)), max(1, round(p.src.h * k)))
        img = blurred(src, bw, bh)
        self._blur[i] = (clock, img)
        return img

    def paint(self, g: QPainter, c: Composition, frames: dict[str, QImage | None], t: float | None,
              src_size: dict[str, tuple[int, int]], clock: float | None = None, show_safe: bool = False) -> None:
        """Draw the frame in output pixels. `t` is seconds into the clip (None hides captions and title);
        `src_size` is each video's real size, since a decoded frame may be smaller than the source.
        `clock` (seconds, monotonic) lets the blurred fill be reused between close frames; None redraws it."""
        g.save()
        g.setRenderHint(QPainter.SmoothPixmapTransform, True)
        g.fillRect(QRectF(0, 0, c.width, c.height), QColor(c.background))
        for i, p in enumerate(c.layers):
            frame = frames.get(p.source)
            if frame is None or frame.isNull():
                continue
            sw, sh = src_size.get(p.source, (frame.width(), frame.height()))
            kx, ky = frame.width() / max(1, sw), frame.height() / max(1, sh)
            g.save()
            g.setClipRect(QRectF(p.clip.x, p.clip.y, p.clip.w, p.clip.h))
            if p.blur:
                img = self._fill_image(i, p, frame, sw, clock)
                g.drawImage(QRectF(p.dest.x, p.dest.y, p.dest.w, p.dest.h), img)
            else:
                g.drawImage(QRectF(p.dest.x, p.dest.y, p.dest.w, p.dest.h), frame,
                            QRectF(p.src.x * kx, p.src.y * ky, p.src.w * kx, p.src.h * ky))
            g.restore()
        for bt in c.banners:
            overlay.paint_block(g, bt.block, QColor(c.banner_colour), outline=bt.outline, shadow=bt.shadow)
        st, s = c.style, c.settings
        if st is not None and s is not None and t is not None:
            if c.title:
                overlay.paint_boxed(g, captions.title_block(c.title, s, st, c.width), QColor("#FFFFFF"),
                                    QColor(0, 0, 0), st.title_pad)
            for ev in c.events_at(t):
                overlay.paint_block(g, captions.caption_block(ev, s, st, c.width, c.height),
                                    QColor(s.caption_color), highlight=QColor(s.caption_highlight),
                                    outline=st.outline, shadow=st.shadow)
        if show_safe and c.safe is not None:
            paint_safe_zone(g, QRectF(0, 0, c.width, c.height),
                            QRectF(c.safe.x, c.safe.y, c.safe.w, c.safe.h), max(1.0, c.width / 540))
        g.restore()


def paint_safe_zone(g: QPainter, frame: QRectF, safe: QRectF, line: float = 1.0) -> None:
    """Shade where TikTok draws its tabs, buttons and caption, and outline the safe area."""
    shade = QColor(255, 80, 80, 46)
    g.fillRect(QRectF(frame.left(), frame.top(), frame.width(), safe.top() - frame.top()), shade)
    g.fillRect(QRectF(frame.left(), safe.bottom(), frame.width(), frame.bottom() - safe.bottom()), shade)
    g.fillRect(QRectF(frame.left(), safe.top(), safe.left() - frame.left(), safe.height()), shade)
    g.fillRect(QRectF(safe.right(), safe.top(), frame.right() - safe.right(), safe.height()), shade)
    g.setPen(QPen(QColor(255, 120, 120, 200), line, Qt.DashLine))
    g.setBrush(Qt.NoBrush)
    g.drawRect(safe)

"""The editor's export view must show what the export renders. These tests render the same frame both ways,
with ffmpeg/libass and with the Qt painter, and compare the pixels."""

import copy
import subprocess

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402

from ytclip import captions, compose, fonts, layout  # noqa: E402
from ytclip.analyze import Clip  # noqa: E402
from ytclip.export import export_clip  # noqa: E402
from ytclip.ffmpeg import ffmpeg_exe, probe  # noqa: E402
from ytclip.gui import overlay  # noqa: E402
from ytclip.gui.compositor import FramePainter  # noqa: E402
from ytclip.settings import Settings  # noqa: E402
from ytclip.subtitles import Word  # noqa: E402


def _has_libass() -> bool:
    try:
        out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False
    return " subtitles " in out.stdout


pytestmark = pytest.mark.skipif(not _has_libass(), reason="needs ffmpeg built with libass")

GREY = (128, 128, 128)
WORDS = [Word(i * 0.4, i * 0.4 + 0.35, w)
         for i, w in enumerate("honestly this is the craziest thing I've ever seen in my life".split())]


def _array(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format_RGB888)
    w, h = img.width(), img.height()
    return np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine())[:, :w * 3].reshape(h, w, 3).astype(int)


def _libass(tmp_path, ass: str, w: int, h: int, font_names: list[str]) -> np.ndarray:
    (tmp_path / "t.ass").write_text(ass, encoding="utf-8")
    opt = ":fontsdir=fonts" if fonts.stage(font_names, tmp_path) else ""
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i", f"color=0x808080:s={w}x{h}:d=1",
                    "-vf", f"subtitles=t.ass{opt}", "-frames:v", "1", "out.png"], cwd=tmp_path, check=True)
    return _array(QImage(str(tmp_path / "out.png")))


def _qt(draw, w: int, h: int) -> np.ndarray:
    img = QImage(w, h, QImage.Format_RGB888)
    img.fill(QColor(*GREY))
    g = QPainter(img)
    draw(g)
    g.end()
    return _array(img)


def _ink(a: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(np.abs(a - np.array(GREY)).max(axis=2) > 24)
    return xs.min(), ys.min(), xs.max(), ys.max()


def _assert_same_text(a: np.ndarray, b: np.ndarray) -> None:
    for got, want in zip(_ink(b), _ink(a)):
        assert abs(int(got) - int(want)) <= 1, (_ink(b), _ink(a))
    d = np.abs(a - b).max(axis=2)
    assert (d > 64).mean() < 0.001  # only anti-aliasing differences along glyph edges
    assert d.mean() < 0.2


@pytest.mark.parametrize("font", ["TikTok Sans Black", "Montserrat Black", "Bebas Neue", "Permanent Marker"])
def test_banner_words_match_libass(app, tmp_path, font):
    s = Settings(banner_top=True, banner_bottom=True, banner_font=font, banner_mode="overlay")
    p = layout.plan(s, 1080, 1920)
    top, bottom = "When he realised the camera was on", "Follow for part 2"
    a = _libass(tmp_path, layout.banner_ass(p, s, top, bottom), 1080, 1920, [font])

    def draw(g):
        for bt in layout.banner_texts(p, s, top, bottom):
            overlay.paint_block(g, bt.block, QColor(s.banner_color), outline=bt.outline, shadow=bt.shadow)

    _assert_same_text(a, _qt(draw, 1080, 1920))


@pytest.mark.parametrize("w,h,kw,t,area", [
    (1080, 1920, dict(caption_words=6, caption_size=70), 1.1, None),
    (1080, 1920, dict(caption_position="upper", title_overlay=True, caption_font="Anton"), 2.1, None),
    (1080, 1920, dict(caption_position="middle", caption_font="Bangers"), 0.1, (300, 1500, 900)),
    (1920, 1080, dict(caption_font="Luckiest Guy"), 3.1, None),
    (720, 1280, dict(caption_font="TikTok Sans Black"), 0.6, None),
])
def test_captions_match_libass(app, tmp_path, w, h, kw, t, area):
    s = Settings(burn_captions=True, **kw)
    title = "My big title here that wraps maybe onto two lines"
    a = _libass(tmp_path, captions.build_ass(WORDS, 0, 10, w, h, s, title, area, 0, offset=t), w, h, [s.caption_font])
    st = captions.caption_style(s, w, h, area, 0)

    def draw(g):
        if captions.title_text(title, s):
            overlay.paint_boxed(g, captions.title_block(captions.title_text(title, s), s, st, w), QColor("#FFFFFF"),
                                QColor(0, 0, 0), st.title_pad)
        for ev in captions.caption_events(WORDS, 0, 10, s):
            if ev.start <= t < ev.end:
                overlay.paint_block(g, captions.caption_block(ev, s, st, w, h), QColor(s.caption_color),
                                    highlight=QColor(s.caption_highlight), outline=st.outline, shadow=st.shadow)

    _assert_same_text(a, _qt(draw, w, h))


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("videos")
    # Still pictures full of hard edges: nothing moves, so any difference is in where things are drawn.
    for name, src in (("wide.mp4", "testsrc2=s=1280x720:r=30"), ("tall.mp4", "testsrc=s=480x854:r=30"),
                      ("second.mp4", "mandelbrot=s=640x360:r=30")):
        src += ",loop=loop=-1:size=1:start=0"
        subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i", src, "-t", "4", "-c:v", "libx264",
                        "-crf", "14", "-pix_fmt", "yuv420p", str(d / name)], check=True)
    return d


def _frame(path, t: float, out) -> QImage:
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", str(out)],
                   check=True)
    return QImage(str(out))


def _difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Per-pixel difference after averaging 3x3 blocks: ffmpeg's bicubic scaling and 4:2:0 colour soften
    hard edges a little differently from Qt, but anything misplaced by more than a pixel or two still shows."""
    h, w = a.shape[0] // 3 * 3, a.shape[1] // 3 * 3
    pool = lambda x: x[:h, :w].reshape(h // 3, 3, w // 3, 3, 3).mean(axis=(1, 3))  # noqa: E731
    return np.abs(pool(a) - pool(b)).max(axis=2)


FRAMES = {
    "tiktok blur": dict(),
    "vertical crop": dict(aspect="vertical_crop"),
    "square": dict(aspect="square", tiktok_ready=False),
    "original": dict(aspect="original", tiktok_ready=False),
    "bars": dict(banner_top=True, banner_bottom=True),
    "text over video": dict(banner_top=True, banner_bottom=True, banner_mode="overlay", aspect="vertical_crop"),
    "stacked": dict(stack_enabled=True, stack_start="fixed", stack_offset=0.5, banner_top=True, title_overlay=True),
    "stacked on top, fit": dict(stack_enabled=True, stack_position="top", layout_fit="fit", stack_split=0.6),
    "blurred layout": dict(banner_top=True, layout_fit="blur"),
}


@pytest.mark.parametrize("source", ["wide.mp4", "tall.mp4"])
@pytest.mark.parametrize("name", list(FRAMES))
def test_export_view_matches_a_real_export_frame(app, tmp_path, videos, source, name):
    s = Settings(resolution=540, burn_captions=True, stack_video=str(videos / "second.mp4"), **FRAMES[name])
    info = probe(videos / source)
    clip = Clip(0.5, 3.5, 50, title="Clip title for the test", crop_x=0.3)
    at = 1.2
    export_clip(videos / source, info, tmp_path / "still.png", clip, WORDS, s, still_at=at)
    second = probe(videos / "second.mp4")
    c = compose.compose(s, info, clip, WORDS, second=(second.width, second.height))
    frames = {"main": _frame(videos / source, clip.start + at, tmp_path / "main.png")}
    if s.stack_enabled:
        off = layout.second_offset(clip.start, clip.length, second.duration, s.stack_start, s.stack_offset)
        frames["second"] = _frame(videos / "second.mp4", off + at, tmp_path / "second.png")
    sizes = {"main": (info.width, info.height), "second": (second.width, second.height)}

    def render(comp):
        return _qt(lambda g: FramePainter().paint(g, comp, frames, at, sizes), comp.width, comp.height)

    a = _array(QImage(str(tmp_path / "still.png")))
    b = render(c)
    assert a.shape == b.shape
    d = _difference(a, b)
    # Scaling filters and 4:2:0 colour leave thin differences along hard edges; nothing more.
    assert d.mean() < 8, d.mean()
    assert (d > 40).mean() < 0.045, (d > 40).mean()
    # And every video layer sits exactly where the export puts it: nudging any one of them by two
    # pixels in any direction makes the match worse.
    exact = d.mean()
    for i, layer in enumerate(c.layers):
        if layer.blur:
            continue
        for dx, dy in ((2, 0), (-2, 0), (0, 2), (0, -2)):
            moved = copy.deepcopy(c)
            moved.layers[i].dest.x += dx
            moved.layers[i].dest.y += dy
            assert _difference(a, render(moved)).mean() > exact, (layer.source, dx, dy)


def test_export_view_reframes_like_the_export(app):
    s = Settings(aspect="vertical_crop")
    left = compose.compose(s, probe_like(1920, 1080), Clip(0, 1, 0, crop_x=0.0))
    right = compose.compose(s, probe_like(1920, 1080), Clip(0, 1, 0, crop_x=1.0))
    assert left.reframe is not None and left.reframe.slide > 0
    assert left.reframe.src.x == 0 and right.reframe.src.x + right.reframe.src.w == pytest.approx(1920)
    assert compose.compose(Settings(aspect="vertical_blur"), probe_like(1920, 1080)).reframe is None


def probe_like(w, h):
    from ytclip.ffmpeg import MediaInfo
    return MediaInfo(duration=10, width=w, height=h)

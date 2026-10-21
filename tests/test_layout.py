"""Layout geometry, banner text and the filter graph it builds. No ffmpeg needed."""

from ytclip import fonts, layout
from ytclip.export import layout_graph
from ytclip.settings import Settings


def test_inactive_by_default():
    assert not layout.active(Settings())


def test_bars_shrink_the_clip_area():
    s = Settings(banner_top=True, banner_top_size=10, banner_bottom=True, banner_bottom_size=10, tiktok_ready=False)
    p = layout.plan(s, 1080, 1920)
    assert (p.top.y, p.top.h) == (0, 192)
    assert p.bottom.bottom == 1920
    assert p.main.y == p.top.bottom and p.main.bottom == p.bottom.y


def test_overlay_banners_leave_the_clip_full_frame():
    s = Settings(banner_top=True, banner_mode="overlay")
    p = layout.plan(s, 1080, 1920)
    assert (p.main.y, p.main.h) == (0, 1920)
    assert p.top is not None and not p.bars


def test_stack_splits_content_and_respects_position():
    s = Settings(stack_enabled=True, stack_split=0.6, banner_top=True, banner_top_size=10, tiktok_ready=False)
    p = layout.plan(s, 1080, 1920)
    assert p.main.y == 192 and p.second.y == p.main.bottom and p.second.bottom == 1920
    assert p.main.h % 2 == 0 and p.second.h % 2 == 0
    assert p.caption_area[2] == p.main.bottom  # 'middle' captions sit on the seam

    top = layout.plan(s.copy(stack_position="top"), 1080, 1920)
    assert top.second.y == 192 and top.main.y == top.second.bottom


def test_stack_makes_original_vertical():
    s = Settings(stack_enabled=True, aspect="original")
    assert layout.frame_aspect(s) == "vertical_crop"
    assert layout.fit_mode(s) == "crop"
    assert layout.fit_mode(s.copy(aspect="vertical_blur")) == "crop"  # stacked halves always fill
    assert layout.fit_mode(Settings(banner_top=True, aspect="vertical_blur")) == "blur"
    assert layout.fit_mode(s.copy(layout_fit="fit")) == "fit"
    assert layout.fit_mode(Settings(banner_top=True, aspect="original", tiktok_ready=False)) == "fit"


def test_banner_text_override_and_template():
    assert layout.banner_text("{title}!", "Big win") == "Big win!"
    assert layout.banner_text("{title}", "Big win", "Wait for it") == "Wait for it"
    assert layout.banner_text("odd {brace", "x") == "odd {brace"


def test_wrap_shrinks_long_text_to_fit():
    box = layout.Box(0, 0, 1080, 200)
    lines, size = layout.wrap("word " * 40, 80, box, True)
    assert len(lines) * size * 1.15 <= box.h * 0.84 or size <= 14
    assert size < 80
    assert layout.wrap("", 80, box, True) == ([], 80)


def test_second_offset_is_stable_and_in_range():
    a = layout.second_offset(12.0, 30, 600, "random", 0)
    assert a == layout.second_offset(12.0, 30, 600, "random", 0)
    assert 0 <= a <= 570
    assert layout.second_offset(1, 30, 20, "random", 0) == 0.0  # shorter than the clip: just loop it
    assert layout.second_offset(1, 30, 100, "fixed", 500) == 70


def test_graph_uses_both_inputs():
    s = Settings(stack_enabled=True, banner_top=True)
    p = layout.plan(s, 1080, 1920)
    graph, last = layout_graph(p, 30, 10, 0.5, "#000000", True)
    assert "[0:v]" in graph and "[1:v]" in graph and last == "l2"
    graph, last = layout_graph(p, 30, 10, 0.5, "#000000", False)
    assert "[1:v]" not in graph and last == "l1"


def test_tiktok_safe_zone_keeps_banner_words_clear_of_the_interface():
    s = Settings(banner_top=True, banner_top_size=5, banner_bottom=True, banner_bottom_size=8)
    p = layout.plan(s, 1080, 1920)
    safe = p.safe
    assert (safe.y, 1920 - safe.bottom, safe.x) == (130, 366, 120)
    # The top bar also covers TikTok's tabs; its words sit just below them.
    assert p.top_text.y == safe.y and p.top_text.h == 96 and p.top.bottom == p.top_text.bottom
    # The bottom bar sits above TikTok's caption area and fills the strip behind it.
    assert p.bottom_text.bottom == safe.bottom and p.bottom.bottom == 1920
    assert p.main.bottom == p.bottom.y
    assert p.top_text.x == 120 and p.top_text.w == 1080 - 240


def test_tiktok_safe_zone_only_in_tall_frames_and_when_on():
    s = Settings(banner_top=True)
    assert layout.plan(s, 1920, 1080).safe is None
    assert layout.plan(s.copy(tiktok_ready=False), 1080, 1920).safe is None


def test_captions_stay_inside_the_safe_zone():
    p = layout.plan(Settings(), 1080, 1920)
    top, bottom, mid = p.caption_area
    assert top == p.safe.y and bottom == p.safe.bottom and top <= mid <= bottom


def test_bundled_fonts_exist_and_are_never_faux_bolded():
    for f in fonts.BUNDLED.values():
        assert (fonts.FONTS_DIR / f.file).is_file(), f.file
        assert fonts.ass_bold(f.name) == 0
    assert fonts.ass_bold("Arial Black") == -1
    assert fonts.glyph_width("Bebas Neue", True) < fonts.glyph_width("Archivo Black", True)


def test_staging_copies_only_bundled_fonts(tmp_path):
    assert not fonts.stage(["Arial Black"], tmp_path)
    assert fonts.stage(["Anton", "Anton", "Arial"], tmp_path)
    assert [f.name for f in (tmp_path / "fonts").iterdir()] == ["Anton-Regular.ttf"]


def test_wrap_balances_lines():
    box = layout.Box(0, 0, 840, 300)
    lines, _ = layout.wrap("FOLLOW FOR PART 2", 80, box, True, "Montserrat Black")
    assert lines == ["FOLLOW FOR", "PART 2"]  # not "FOLLOW FOR PART" / "2"


def test_captions_avoid_banner_words_laid_over_the_video():
    p = layout.plan(Settings(banner_top=True, banner_bottom=True, banner_mode="overlay"), 1080, 1920)
    top, bottom, _ = p.caption_area
    assert top >= p.top_text.bottom and bottom <= p.bottom_text.y

"""Layout geometry, banner text and the filter graph it builds. No ffmpeg needed."""

from ytclip import fonts, layout, textlayout
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


def test_part_2_only_shows_when_a_clip_carries_this_one_on():
    from ytclip.analyze import Clip

    for text in ("Follow for part 2", "PART 2 in comments", "pt. 3 tomorrow", "Part two soon", "Next part on my page",
                 "To be continued", "Part II", "Pt2", "Follow for more parts", "More parts coming", "Part 1/3",
                 "Part 1 of 3", "Parte 2", "Teil 2", "Partie 2", "Continued in comments", "Stay tuned",
                 "Story continues on my page", "Follow to see what happens next", "The next one is wild"):
        assert layout.promises_more(text), text
    for text in ("Follow for more", "Wait for the end", "Departure 2", "The best part", "{title}", "Discontinued",
                 "Continue watching", "Who's next?", "1/3 of people", "Follow @part2clips", "The part I love"):
        assert not layout.promises_more(text), text
    a, b, c = Clip(0, 10, 80), Clip(12, 20, 80), Clip(60, 70, 80)
    assert layout.has_next_part(a, [a, b, c]) and not layout.has_next_part(b, [a, b, c])
    assert not layout.has_next_part(a, [a, Clip(12, 20, 80, enabled=False)])
    assert layout.has_next_part(a, [a, Clip(9, 20, 80)])  # part 2 replays the last moment of part 1
    assert not layout.has_next_part(a, [a, Clip(2, 9, 80)])  # inside it: not a part 2
    s = Settings(banner_bottom_text="Follow for part 2", banner_bottom_alt="Follow for more")
    assert layout.bottom_banner(s, "T", None, next_part=True) == "Follow for part 2"
    assert layout.bottom_banner(s, "T", None, next_part=False) == "Follow for more"
    assert layout.bottom_banner(s, "T", "My own part 2 text", next_part=False) == "My own part 2 text"  # yours stays
    assert layout.bottom_banner(s.copy(banner_bottom_text="Like it?"), "T", None, False) == "Like it?"
    titled = s.copy(banner_bottom_text="{title}")  # a title that says "part 2" isn't your promise
    assert layout.bottom_banner(titled, "The part 2 nobody talks about", None, False) == "The part 2 nobody talks about"


def test_a_part_2_the_quality_check_will_reject_doesnt_count():
    from ytclip.analyze import Clip

    a, b = Clip(0, 10, 80), Clip(11, 20, 80, rating=3)
    s = Settings(qc_enabled=True, qc_min_rating=5)
    assert layout.has_next_part(a, [a, b]) and not layout.has_next_part(a, [a, b], s)
    assert layout.has_next_part(a, [a, b], s.copy(qc_enabled=False))
    assert layout.has_next_part(a, [a, Clip(11, 20, 80)], s)  # not rated: the check keeps it


def test_the_ais_bottom_line_only_replaces_a_part_2_that_isnt_there():
    s = Settings(banner_bottom=True, banner_bottom_text="Follow for part 2", banner_bottom_alt="Follow for more")
    assert layout.uses_ai_bottom(s)
    assert layout.bottom_banner(s, "T", None, False, ai="Would you stay?") == "Would you stay?"
    assert layout.bottom_banner(s, "T", None, True, ai="Would you stay?") == "Follow for part 2"  # it's true here
    assert layout.bottom_banner(s, "T", "Mine", False, ai="Would you stay?") == "Mine"
    assert layout.bottom_banner(s, "T", None, False, ai="Part 2 on my page") == "Follow for more"  # never a promise
    off = s.copy(polish_bottom_text=False)
    assert layout.bottom_banner(off, "T", None, False, ai="Would you?") == "Follow for more"
    blank = s.copy(banner_bottom_alt="")  # no words on a clip with no part 2: the AI doesn't add any
    assert layout.bottom_banner(blank, "T", None, False, ai="Would you?") == "" and not layout.uses_ai_bottom(blank)
    for fixed in ("Follow @bigtalkclips", "", "{title}"):  # your own line, a blank bar or the title stays
        f = s.copy(banner_bottom_text=fixed)
        assert not layout.uses_ai_bottom(f)
        assert layout.bottom_banner(f, "Title", None, False, ai="Would you?") == layout.banner_text(fixed, "Title")
    handle = s.copy(banner_bottom_text="Follow @BigTalk for part 2", banner_bottom_alt="Follow for more")
    assert layout.bottom_handles(handle) == ["@BigTalk"]
    assert layout.bottom_banner(handle, "T", None, False, ai="Would you stay? @bigtalk") == "Would you stay? @bigtalk"
    assert layout.bottom_banner(handle, "T", None, False, ai="Would you stay?") == "Follow for more"  # lost the handle


def test_wrap_shrinks_long_text_to_fit():
    box = layout.Box(0, 0, 1080, 200)
    font = "TikTok Sans Black"
    lines, size = layout.wrap("WORD " * 40, 80, box, font)
    assert size < fonts.ass_size(font, 80)
    assert len(lines) * size <= box.h * 0.84
    for ln in lines:
        assert textlayout.text_width(font, " ".join(ln), size) <= box.w * 0.9
    assert layout.wrap("", 80, box, font) == ([], fonts.ass_size(font, 80))


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
    # Measured from the files: a condensed face is narrower than a wide one at the same letter size.
    assert (fonts.metrics("Bebas Neue").width("FOLLOW", fonts.ass_size("Bebas Neue", 80))
            < fonts.metrics("Archivo Black").width("FOLLOW", fonts.ass_size("Archivo Black", 80)))


def test_staging_copies_only_bundled_fonts(tmp_path):
    assert not fonts.stage(["Arial Black"], tmp_path)
    assert fonts.stage(["Anton", "Anton", "Arial"], tmp_path)
    assert [f.name for f in (tmp_path / "fonts").iterdir()] == ["Anton-Regular.ttf"]


def test_wrap_balances_lines():
    font, text = "Montserrat Black", "FOLLOW FOR PART 2"
    full = textlayout.text_width(font, text, fonts.ass_size(font, 80))
    box = layout.Box(0, 0, int(full * 0.97 / 0.9), 300)  # one line won't fit; greedy would leave "2" alone
    lines, _ = layout.wrap(text, 80, box, font)
    assert len(lines) == 2 and lines[-1] != ["2"]  # evened out, not "FOLLOW FOR PART" / "2"
    widths = [textlayout.text_width(font, " ".join(ln), 100) for ln in lines]
    assert max(widths) < textlayout.text_width(font, "FOLLOW FOR PART", 100)


def test_captions_avoid_banner_words_laid_over_the_video():
    p = layout.plan(Settings(banner_top=True, banner_bottom=True, banner_mode="overlay"), 1080, 1920)
    top, bottom, _ = p.caption_area
    assert top >= p.top_text.bottom and bottom <= p.bottom_text.y


def test_old_text_sizes_are_converted_to_letter_heights(tmp_path):
    import json

    from ytclip.settings import load_user_layouts, migrate, user_layouts_path

    old = {"caption_size": 70, "caption_font": "Montserrat Black", "banner_font_size": 96}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(old))
    s = Settings.load(path)
    # Same pixels on screen as before: the old raw .ass size times the font's em ratio.
    assert s.caption_size == round(70 * fonts.metrics("Montserrat Black").em_ratio) == 45
    assert s.banner_font_size == round(96 * fonts.metrics("TikTok Sans Black").em_ratio)
    s.save(path)
    assert Settings.load(path).caption_size == 45  # converted once, never again
    assert migrate({"caption_size": 50, "text_units": 2})["caption_size"] == 50
    user_layouts_path().write_text(json.dumps({"Mine": {"banner_font_size": 100, "banner_font": "Anton"}}))
    assert load_user_layouts()["Mine"]["banner_font_size"] == round(100 * fonts.metrics("Anton").em_ratio)


def test_a_size_looks_the_same_in_every_font():
    for name in ("Montserrat Black", "Anton", "Bebas Neue", "Luckiest Guy"):
        assert abs(fonts.ass_size(name, 60) * fonts.metrics(name).em_ratio - 60) < 1


def test_caption_lines_never_move_as_the_highlight_moves():
    from ytclip import captions

    s = Settings(burn_captions=True, caption_words=6, caption_size=80)
    st = captions.caption_style(s, 1080, 1920)
    words = "THIS IS THE CRAZIEST THING EVER".split()
    blocks = [captions.caption_block(captions.CaptionEvent(0, 1, words, i), s, st, 1080, 1920) for i in range(6)]
    breaks = {tuple(len(ln.words) for ln in b.lines) for b in blocks}
    assert len(breaks) == 1 and len(next(iter(breaks))) > 1
    for b in blocks:
        assert all(ln.x >= st.margin_lr - 1 and ln.x + ln.width <= 1080 - st.margin_lr + 1 for ln in b.lines)


def test_a_clip_can_hide_a_banners_words():
    s = Settings(banner_top=True, banner_bottom=True)
    p = layout.plan(s, 1080, 1920)
    top = layout.banner_text(s.banner_top_text, "Title", "")
    assert top == ""
    assert [bt.box for bt in layout.banner_texts(p, s, top, "Follow")] == [p.bottom_text]
    assert "Dialogue" in layout.banner_ass(p, s, top, "Follow")


def test_banner_words_stay_inside_their_box():
    s = Settings(banner_top=True, banner_top_text="{title}", banner_font="Luckiest Guy", banner_font_size=120)
    p = layout.plan(s, 1080, 1920)
    for text in ("short", "a much much longer banner title that has to wrap onto several lines to fit at all"):
        (bt,) = layout.banner_texts(p, s, text, "")
        assert bt.block.top >= bt.box.y and bt.block.bottom <= bt.box.bottom
        assert all(ln.x >= bt.box.x and ln.x + ln.width <= bt.box.x + bt.box.w for ln in bt.block.lines)

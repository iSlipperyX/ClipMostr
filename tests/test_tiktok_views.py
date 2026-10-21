"""TikTok-size output by default, and the top/bottom view (layout) presets."""

import pytest

from ytclip import cli, layout
from ytclip.export import frame
from ytclip.ffmpeg import MediaInfo
from ytclip.settings import (LAYOUT_PRESETS, PRESETS, Settings, all_layouts, delete_user_layout, find_layout,
                             layout_keys, load_user_layouts, save_user_layout)

LANDSCAPE = MediaInfo(width=1920, height=1080)


def test_defaults_export_at_tiktok_size():
    assert frame(Settings(), LANDSCAPE) == ("vertical_blur", 1080, 1920)


@pytest.mark.parametrize("aspect", ["original", "square", "vertical_blur", "vertical_crop"])
@pytest.mark.parametrize("res", [480, 720, 1080, 1440, 2160])
def test_tiktok_ready_is_always_9_16_between_540_and_1080(aspect, res):
    _, w, h = frame(Settings(aspect=aspect, resolution=res), LANDSCAPE)
    assert w * 16 == h * 9
    assert (w, h) == (min(max(res, 540), 1080), min(max(res, 540), 1080) * 16 // 9)


def test_turning_tiktok_off_keeps_the_chosen_frame():
    off = Settings(tiktok_ready=False, aspect="original", resolution=720)
    assert frame(off, LANDSCAPE) == ("original", 1280, 720)
    assert frame(off.copy(aspect="square"), LANDSCAPE) == ("square", 720, 720)
    assert frame(off.copy(resolution=1440, aspect="vertical_crop"), LANDSCAPE)[1:] == (1440, 2560)


def test_every_builtin_preset_is_tiktok_sized_except_square():
    for name, values in PRESETS.items():
        s = Settings().apply(values)
        _, w, h = frame(s, LANDSCAPE)
        if name == "Square posts":
            assert (w, h) == (1080, 1080)
        else:
            assert (w, h) == (1080, 1920), name


def test_square_then_another_preset_turns_tiktok_back_on():
    s = Settings().apply(PRESETS["Square posts"]).apply(PRESETS["Shorts / TikTok / Reels"])
    assert s.tiktok_ready and frame(s, LANDSCAPE)[1:] == (1080, 1920)


def test_layout_presets_only_touch_layout_settings():
    keys = layout_keys()
    for name, values in LAYOUT_PRESETS.items():
        assert set(values) <= keys, name
        assert "stack_video" not in values  # built-ins keep the user's chosen second video


def test_layout_presets_replace_the_previous_view():
    s = Settings(stack_video="/games/parkour.mp4").apply(LAYOUT_PRESETS["Gameplay below + title"])
    assert s.stack_enabled and s.banner_top and not s.banner_bottom and s.stack_video == "/games/parkour.mp4"
    s = s.apply(LAYOUT_PRESETS["Title top + follow bottom"])
    assert not s.stack_enabled and s.banner_top and s.banner_bottom
    s = s.apply(LAYOUT_PRESETS["Clip only"])
    assert not layout.active(s)


def test_top_and_bottom_views_put_the_second_video_where_named():
    below = layout.plan(Settings().apply(LAYOUT_PRESETS["Gameplay below"]), 1080, 1920)
    above = layout.plan(Settings().apply(LAYOUT_PRESETS["Gameplay above"]), 1080, 1920)
    assert below.second.y > below.main.y and above.second.y < above.main.y
    big = layout.plan(Settings().apply(LAYOUT_PRESETS["Big clip, small gameplay"]), 1080, 1920)
    assert big.main.h > big.second.h


def test_user_layouts_save_load_and_delete():
    s = Settings(banner_top=True, banner_top_text="WAIT FOR IT", stack_enabled=True,
                 stack_video="C:/clips/minecraft.mp4", banner_font="Bangers", clip_count=9)
    save_user_layout("My gameplay", s)
    saved = load_user_layouts()["My gameplay"]
    assert saved["stack_video"] == "C:/clips/minecraft.mp4" and saved["banner_font"] == "Bangers"
    assert "clip_count" not in saved  # only the view, not clip finding
    assert "My gameplay" in all_layouts() and find_layout("my game") == "My gameplay"
    assert Settings().apply(all_layouts()["My gameplay"]).banner_top_text == "WAIT FOR IT"
    delete_user_layout("My gameplay")
    assert "My gameplay" not in all_layouts()


def test_find_layout_prefers_exact_names():
    assert find_layout("Gameplay below") == "Gameplay below"
    assert find_layout("gameplay below +") == "Gameplay below + title"
    assert find_layout("nope") is None


def test_cli_layout_and_no_tiktok(tmp_path):
    args = cli.build_parser().parse_args(["x.mp4", "--layout", "gameplay above"])
    s = cli.settings_from_args(args)
    assert s.stack_enabled and s.stack_position == "top"
    args = cli.build_parser().parse_args(["x.mp4", "-f", "original", "--no-tiktok"])
    s = cli.settings_from_args(args)
    assert frame(s, LANDSCAPE)[0] == "original"
    with pytest.raises(SystemExit):
        cli.settings_from_args(cli.build_parser().parse_args(["x.mp4", "--layout", "nope"]))


def test_cli_lists_layouts(capsys):
    assert cli.main(["--list-layouts"]) == 0
    out = capsys.readouterr().out
    assert "Gameplay below" in out and "Title on top" in out

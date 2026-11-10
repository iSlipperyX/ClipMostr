"""Keyframes: what a value is at a time, the same thing as an ffmpeg expression, and the ready-made moves."""

import math

import pytest

from ytclip.editor import motion_engine as M
from ytclip.studio.timeline_schema import TRANSITIONS, Keyframe


def evaluate(expression: str, t: float) -> float:
    """An ffmpeg expression, worked out here."""
    env = {"t": t, "PI": math.pi, "cos": math.cos, "sin": math.sin, "pow": math.pow,
           "lt": lambda a, b: 1.0 if a < b else 0.0, "if": None}
    text = expression.replace("if(", "_if(")
    env["_if"] = lambda c, a, b: a if c else b
    return float(eval(text, {"__builtins__": {}}, env))  # noqa: S307 - our own expression


KEYS = [Keyframe(0.0, x=0.3, scale=1.0), Keyframe(1.0, x=0.7, scale=1.4, ease="ease_in_out"),
        Keyframe(2.0, x=0.7, scale=1.4, rotation=10, ease="hold"),
        Keyframe(3.0, x=0.5, scale=1.0, rotation=0, opacity=0.0, ease="ease_out")]


def test_value_between_keyframes_follows_the_later_ones_ease():
    assert M.value_at(KEYS, "x", -1) == 0.3 and M.value_at(KEYS, "x", 9) == 0.5  # held before and after
    assert M.value_at(KEYS, "x", 0.5) == pytest.approx(0.5)  # ease in-out is halfway at half time
    assert M.value_at(KEYS, "x", 0.25) < 0.4  # ... and slow to start
    assert M.value_at(KEYS, "rotation", 1.99) == 0 and M.value_at(KEYS, "rotation", 2.0) == 10  # hold: a jump
    assert M.value_at(KEYS, "opacity", 2.5) < 0.5  # ease out: most of the way early
    assert M.value_at([], "scale", 1.0) == 1.0 and M.value_at([], "x", 1.0) == 0.5
    assert [M.ease(k, 0) for k in ("linear", "ease_in", "ease_out", "ease_in_out")] == [0, 0, 0, 0]
    assert [M.ease(k, 1) for k in ("linear", "ease_in", "ease_out", "ease_in_out")] == [1, 1, 1, 1]
    assert M.ease("ease_in", 0.5) < 0.5 < M.ease("ease_out", 0.5)


@pytest.mark.parametrize("prop", M.PROPS)
def test_the_ffmpeg_expression_says_the_same(prop):
    text = M.expr(KEYS, prop, "t")
    for i in range(-4, 70):
        t = i / 20
        assert evaluate(text, t) == pytest.approx(M.value_at(KEYS, prop, t), abs=2e-3), (prop, t)


def test_expressions_of_things_that_dont_move_are_numbers():
    assert M.expr(KEYS, "y") == "0.5" and M.expr([], "scale") == "1"
    assert M.constant(KEYS, "y") == 0.5 and M.constant(KEYS, "x") is None and M.constant([], "opacity") == 1.0
    assert M.moving(KEYS) and not M.moving([Keyframe(0, scale=1.2), Keyframe(2, scale=1.2)]) and not M.moving([])
    assert M.extremes(KEYS, "scale") == (1.0, 1.4)
    shifted = M.expr(KEYS, "x", "(on/30-0.5)")
    assert "(on/30-0.5)" in shifted and "t" not in shifted.replace("lt(", "").replace("(on/30-0.5)", "")


def test_transitions_all_have_an_ffmpeg_name():
    assert set(M.TRANSITION_XFADE) == set(TRANSITIONS) - {"cut"}
    assert M.TRANSITION_XFADE["whip_left"] == "slideleft" and M.TRANSITION_XFADE["dip_black"] == "fadeblack"
    assert M.XFADE_TRANSITION["fade"] == "crossfade" and M.XFADE_TRANSITION["zoomin"] == "zoom_in"


@pytest.mark.parametrize("name", list(M.motion.MOVES))
@pytest.mark.parametrize("size, overflow", [((1080, 1920), (1.0, 1.0)), ((1920, 1080), (1.0, 1.0)),
                                            ((1080, 1920), (3.16, 1.0))])
def test_every_ken_burns_move_keeps_the_frame_covered(name, size, overflow):
    keys = M.ken_burns(name, 5.0, size=size, overflow=overflow)
    if name == "static":
        assert not M.moving(keys)
        return
    assert keys[0].t == 0 and keys[-1].t == 5.0 and M.moving(keys)
    assert M.covers(keys, 5.0, overflow, size)  # never shows what's behind the picture, even while it rolls
    assert all(abs(k.rotation) <= 3 for k in keys)
    strong, gentle = M.ken_burns(name, 5.0, 2.0, size=size), M.ken_burns(name, 5.0, 0.5, size=size)
    travel = lambda ks: abs(ks[-1].scale - ks[0].scale) + abs(ks[-1].x - ks[0].x) + abs(ks[-1].y - ks[0].y)  # noqa: E731
    assert travel(strong) > travel(gentle)


def test_covering_is_judged_honestly():
    assert not M.covers([Keyframe(0, x=0.6)], 1.0)  # moved aside at scale 1: the background shows
    assert M.covers([Keyframe(0, x=0.6, scale=1.2)], 1.0) and not M.covers([Keyframe(0, x=0.61, scale=1.2)], 1.0)
    assert M.covers([Keyframe(0, x=0.9)], 1.0, overflow=(1.8, 1.0))  # a wide source has room to pan
    assert not M.covers([Keyframe(0, rotation=3)], 1.0)
    assert M.covers([Keyframe(0, rotation=3, scale=M.rotation_cover(3, 9, 16))], 1.0)
    assert M.rotation_cover(0, 9, 16) == 1.0 and M.rotation_cover(90, 16, 9) == pytest.approx(16 / 9)
    assert M.reach(1.2) == pytest.approx(0.1) and M.reach(1.0, 1.5) == pytest.approx(0.25)


def test_the_other_moves():
    z = M.zoom(4.0, 1.0, 1.3, "ease_out")
    assert [(k.t, k.scale, k.ease) for k in z] == [(0.0, 1.0, "linear"), (4.0, 1.3, "ease_out")]
    p = M.punch_in(1.0, 3.0)
    assert M.value_at(p, "scale", 0.9) == 1.0 and M.value_at(p, "scale", 1.08) == pytest.approx(1.12)
    assert M.value_at(p, "scale", 1.9) == pytest.approx(1.12) and M.value_at(p, "scale", 3.0) == 1.0  # eases back out
    back = M.punch_in(1.0, 3.0, hold=0.5)
    assert M.value_at(back, "scale", 1.5) == pytest.approx(1.12) and M.value_at(back, "scale", 1.9) < 1.11
    s = M.shake(0.5, 0.4, seed="a")
    assert s == M.shake(0.5, 0.4, seed="a") != M.shake(0.5, 0.4, seed="b")  # the same shake every render
    assert max(abs(k.x - 0.5) for k in s) <= 0.012 + 1e-9 and (s[0].x, s[-1].x) == (0.5, 0.5)
    assert all(M.covers([k], 0.1) for k in s)  # zoomed in just enough that its edges never show
    w = M.whip_in(0.3, "left")
    assert w[0].x > 1.0 and w[-1].x == 0.5 and w[-1].ease == "ease_out"
    f = M.fade(2.0, 0.5, 0.25)
    assert [(k.t, k.opacity) for k in f] == [(0.0, 0.0), (0.5, 1.0), (1.75, 1.0), (2.0, 0.0)]
    both = M.combine(M.zoom(2.0, 1.0, 1.2), M.fade(2.0, 0.5, 0.5))
    assert [k.t for k in both] == [0.0, 0.5, 1.5, 2.0]
    assert both[1].scale == pytest.approx(1.05) and both[1].opacity == 1.0 and both[-1].opacity == 0.0
    assert [k.t for k in M.shift(z, 1.5)] == [1.5, 5.5] and z[0].t == 0.0  # a copy

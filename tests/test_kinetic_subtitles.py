"""Kinetic captions: timed words to a subtitle track (groups, colours, icons, boxes) and the .ass that burns in."""

import re
import subprocess

import numpy as np
import pytest

from ytclip.editor import kinetic_subtitles as K
from ytclip.ffmpeg import ffmpeg_exe
from ytclip.studio import timeline_schema as T
from ytclip.subtitles import Word

VERTICAL = T.Canvas.named("vertical")
WIDE = T.Canvas.named("landscape")


def words(text: str, start: float = 0.2, each: float = 0.3, gap: float = 0.05) -> list[Word]:
    out, t = [], start
    for w in text.split():
        if w == "|":  # a pause
            t += 1.0
            continue
        out.append(Word(round(t, 3), round(t + each, 3), w))
        t += each + gap
    return out


def lines(ass: str, style: str = "Cap") -> list[str]:
    return [ln for ln in ass.splitlines() if ln.startswith("Dialogue:") and f",{style}," in ln]


def test_words_group_in_ones_to_threes_breaking_at_pauses_and_sentences():
    track = K.build_track(words("This is how you save. Money | fast every single month now"), VERTICAL, duration=10.0)
    assert [e.text for e in track.events] == ["THIS IS HOW", "YOU SAVE.", "MONEY", "FAST EVERY SINGLE", "MONTH NOW"]
    assert all(1 <= len(e.words) <= 3 for e in track.events)
    first, second = track.events[0], track.events[1]
    assert first.start == 0.2 and first.end == second.start  # held until the next one, so the screen never blinks
    assert track.events[2].end == pytest.approx(track.events[2].words[0].end + 1.0, abs=0.06)  # ... or the pause
    assert track.events[-1].end <= 10.0
    assert not [p for p in T.validate(T.Timeline(duration=10.0, tracks=[track])) if p.level == "error"]
    two = K.build_track(words("one two three four five"), VERTICAL, T.SubtitleStyle(max_words=2), duration=10.0)
    assert [len(e.words) for e in two.events] == [2, 2, 1]
    plain = K.build_track(words("one two three four five six seven"), VERTICAL,
                          T.SubtitleStyle(preset="plain", max_words=6, uppercase=False), duration=10.0)
    assert [e.text for e in plain.events] == ["one two three four five six", "seven"]


def test_overlapping_word_times_never_overlap_captions():
    said = [Word(0.0, 0.5, "one"), Word(0.4, 0.9, "two"), Word(0.8, 1.4, "three."), Word(1.3, 1.8, "four"),
            Word(9.9, 12.0, "late"), Word(15.0, 16.0, "never")]
    track = K.build_track(said, VERTICAL, duration=10.5)
    for a, b in zip(track.events, track.events[1:]):
        assert a.end <= b.start + 1e-9
    assert track.events[-1].text == "LATE" and track.events[-1].end == 10.5  # cut at the end; "never" starts after it
    assert not [p for p in T.validate(T.Timeline(duration=10.5, tracks=[track])) if p.level == "error"]


def test_colours_by_meaning_and_by_the_scripts_emphasis():
    style = T.SubtitleStyle()
    said = words("I paid $5,000 in a dangerous scam. Why? Because nobody warned me")
    track = K.build_track(said, VERTICAL, style, duration=20.0, emphasis={"Nobody", "warned!"})
    colour = {w.text: w.color for e in track.events for w in e.words}
    assert colour["$5,000"] == colour["PAID"] == style.money == "#00FF66"
    assert colour["DANGEROUS"] == colour["SCAM."] == style.danger == "#FF3B30"
    assert colour["WHY?"] == style.question
    assert colour["NOBODY"] == colour["WARNED"] == style.emphasis  # the script's own words win
    assert colour["I"] == colour["IN"] == ""  # everything else: the style's colour, lit while it's said
    assert [w.emphasis for e in track.events for w in e.words if w.text in ("NOBODY", "ME")] == [True, False]
    off = K.build_track(said, VERTICAL, T.SubtitleStyle(keyword_colors=False), duration=20.0)
    assert {w.color for e in off.events for w in e.words} == {""}
    assert K.colour_for("money", T.SubtitleStyle(money="#112233")) == "#112233"


def test_icons_come_with_a_few_words_and_never_crowd():
    said = words("This insane money trick is fire and wild and hot | | | | then the secret wins")
    track = K.build_track(said, VERTICAL, duration=30.0)
    icons = [(w.text, w.emoji, w.start) for e in track.events for w in e.words if w.emoji]
    assert [i[:2] for i in icons] == [("INSANE", "🔥"), ("SECRET", "👀")]  # one, then nothing for 3.5 s
    assert icons[1][2] - icons[0][2] >= K.ICON_GAP
    assert max(sum(1 for w in e.words if w.emoji) for e in track.events) == 1
    none = K.build_track(said, VERTICAL, T.SubtitleStyle(emoji=False), duration=30.0)
    assert not [w for e in none.events for w in e.words if w.emoji]
    assert K.emoji_for("$40k") == "💰" and K.emoji_for("Why?") == "❓" and K.emoji_for("table") == ""
    assert K.icon_of("🔥")[0] == "draw" and K.icon_of("💰") == K.ICONS["dollar"] and K.icon_of("⚠️") == K.ICONS["warning"]
    assert K.icon_of("🦄") is None and K.icon_of("") is None
    assert set(K.EMOJI_ICONS.values()) <= set(K.ICONS)
    for kind, shape, colour in K.ICONS.values():
        assert re.match(r"^#[0-9A-F]{6}$", colour)
        if kind == "draw":  # only ASS drawing commands, on the 100-unit grid
            assert re.match(r"^[mlb\d .\-]+$", shape) and shape.startswith("m ")
            assert all(-2 <= float(n) <= 102 for n in re.findall(r"-?\d+(?:\.\d+)?", shape))


@pytest.mark.parametrize("canvas, bottom", [(VERTICAL, 1200), (WIDE, round(1080 * K.LANDSCAPE_BOTTOM))])
def test_every_word_has_its_box_inside_the_safe_zone(canvas, bottom):
    track = K.build_track(words("Extraordinarily complicated explanations nobody ever asked for"), canvas, duration=10.0)
    geo = K.Geometry(track.style, canvas)
    for ev in track.events:
        boxes = [w.box for w in ev.words]
        assert all(b and len(b) == 4 for b in boxes)
        for x, y, w, h in boxes:
            assert y + h <= bottom + 1 and y > canvas.height * 0.3
        for top in {b[1] for b in boxes}:
            row = [b for b in boxes if b[1] == top]
            grow = 0.06 * max(b[2] for b in row)  # the spoken word swells 12%, pushing the line out both ways
            assert row[0][0] - grow >= geo.margin_lr - 1 and row[-1][0] + row[-1][2] + grow <= canvas.width - geo.margin_lr + 1
        on_a_line = [b for b in boxes if b[1] == boxes[0][1]]
        assert all(a[0] + a[2] <= b[0] + 0.5 for a, b in zip(on_a_line, on_a_line[1:]))  # left to right, no overlap
        centre = (min(b[0] for b in on_a_line) + max(b[0] + b[2] for b in on_a_line)) / 2
        assert abs(centre - canvas.width / 2) < 2
    wrapped = [e for e in track.events if len({w.box[1] for w in e.words}) > 1]
    assert wrapped or canvas is WIDE  # long words don't fit one line of a vertical video: they wrap
    if canvas is VERTICAL:  # and one too long for a line by itself is drawn smaller, not off the edge
        assert K.size_of(track.events[0], geo) < geo.size == K.size_of(track.events[-1], geo)
        assert f"{{\\fs{K.size_of(track.events[0], geo)}}}" in lines(K.to_ass(track, canvas))[0]
    lower = K.build_track(words("one two"), VERTICAL, T.SubtitleStyle(position=0.9), duration=5.0)
    assert lower.events[0].words[0].box[1] > track.events[0].words[0].box[1]


def test_pop_is_the_80_110_100_group_pop_with_the_spoken_word_lit():
    track = K.build_track(words("save more money"), VERTICAL, duration=5.0)
    ass = K.to_ass(track, VERTICAL)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass and "Style: Cap,Montserrat Black," in ass
    cap = lines(ass)
    assert len(cap) == 3  # one event for each word's turn
    first = cap[0].split(",,", 2)[-1]
    # Every word of the group pops in together: 80% → 110% by 80 ms → settled by 150 ms.
    assert first.count("\\fscx80\\fscy80\\t(0,80,\\fscx110\\fscy110)") == 3
    assert "\\t(80,150,\\fscx112\\fscy112)}SAVE" in first and first.count("\\t(80,150,\\fscx100\\fscy100)") == 2
    assert "{\\1c&H0000E6FF" in first  # the spoken word in the highlight colour (#FFE600, as ASS writes it)
    second = cap[1].split(",,", 2)[-1]
    assert "\\fscx80" not in second and "\\fscx100\\fscy100\\t(0,90,\\fscx112\\fscy112)}MORE" in second
    assert "{\\1c&H0066FF00\\fscx100\\fscy100}MONEY" in second  # money stays green before and while it's said
    starts = [ln.split(",")[1] for ln in cap]
    assert starts == ["0:00:00.20", "0:00:00.55", "0:00:00.90"]
    assert [ln.split(",")[2] for ln in cap][:2] == starts[1:]  # each turn ends where the next begins


def test_bounce_karaoke_and_plain():
    said = words("save more money")
    bounce = K.to_ass(K.build_track(said, VERTICAL, T.SubtitleStyle(preset="bounce"), duration=5.0), VERTICAL)
    assert "\\t(0,70,\\fscx128\\fscy128)\\t(70,140,\\fscx96\\fscy96)\\t(140,220,\\fscx108\\fscy108)}MORE" in bounce
    karaoke = K.to_ass(K.build_track(said, VERTICAL, T.SubtitleStyle(preset="karaoke"), duration=5.0), VERTICAL)
    cap = lines(karaoke)
    assert len(cap) == 1  # one event; the highlight sweeps through it
    assert re.search(r"\{\\fscx80\\fscy80\\t\(0,80,\\fscx110\\fscy110\)\\t\(80,150,\\fscx100\\fscy100\)\}"
                     r"\{\\kf35\\1c&H0000E6FF\}SAVE \{\\kf35[^}]*\}MORE(?: |\\N)\{\\kf\d+\\1c&H0066FF00\}MONEY", cap[0])
    assert re.search(r"Style: Cap,[^,]+,\d+,&H0000E6FF&?,&H00FFFFFF&?,", karaoke)  # fills from white to the highlight
    plain = K.to_ass(K.build_track(said, VERTICAL, T.SubtitleStyle(preset="plain"), duration=5.0), VERTICAL)
    assert "\\t(" not in plain and "\\fscx" not in plain and len(lines(plain)) == 1


def test_icons_are_drawn_over_their_word():
    track = K.build_track(words("that is insane"), VERTICAL, duration=5.0)
    ass = K.to_ass(track, VERTICAL)
    icon = lines(ass, "Icon")
    assert len(icon) == 1 and icon[0].startswith("Dialogue: 2,0:00:00.90,")  # when "insane" is said, above the words
    body = icon[0].split(",,", 2)[-1]
    assert "\\p1}m " in body and body.endswith("{\\p0}") and "\\frz-14\\t(0,110," in body
    x, y = map(float, re.search(r"\\pos\((\d+),(\d+)\)", body).groups())
    top = min(w.box[1] for w in track.events[0].words)
    assert x == 540 and y < top
    track.events[0].words[2].emoji = "🦄"  # no shape for it: not drawn, never an empty box
    assert not lines(K.to_ass(track, VERTICAL), "Icon")
    track.events[0].words[2].emoji = "💰"
    assert "}$" in lines(K.to_ass(track, VERTICAL), "Icon")[0]  # a text icon


def test_a_still_from_inside_the_video_shows_captions_settled():
    track = K.build_track(words("that is insane | and so on"), VERTICAL, duration=8.0)
    ass = K.to_ass(track, VERTICAL, offset=1.0)
    cap = lines(ass)
    assert cap[0].split(",")[1] == "0:00:00.00" and "\\fscx80" not in cap[0]  # mid-caption: no pop to replay
    assert "\\frz-14" not in lines(ass, "Icon")[0]
    assert any("\\fscx80\\fscy80" in ln for ln in cap[1:])  # the caption after it still pops in
    assert not lines(K.to_ass(track, VERTICAL, offset=7.9))


def test_text_that_would_break_ass_is_made_safe_and_swearing_is_masked():
    track = K.build_track(words("{\\b1}bold shit happens"), VERTICAL, duration=5.0)
    ass = K.to_ass(track, VERTICAL)
    assert "{\\b1}" not in ass and "(\\\\B1)BOLD" in ass
    assert "SHIT" not in ass and "SH*T" in ass
    kept = K.build_track(words("shit happens"), VERTICAL, T.SubtitleStyle(censor=False), duration=5.0)
    assert kept.events[0].words[0].text == "SHIT"
    assert [(w.start, w.end, w.text) for w in K.from_text("one two", 1.0, 2.0)] == [(1.0, 1.5, "one"), (1.5, 2.0, "two")]
    assert K.from_text("", 0, 1) == [] and K.from_text("x", 2, 1) == []


def test_libass_draws_it_where_the_boxes_say(tmp_path):
    """Burn one caption over black and find the letters: they're inside the union of the word boxes."""
    canvas = T.Canvas(540, 960, 10)
    track = K.build_track(words("save more", start=0.0, each=0.5, gap=0.0), canvas,
                          T.SubtitleStyle(preset="plain", emoji=False), duration=2.0)
    (tmp_path / "c.ass").write_text(K.to_ass(track, canvas), encoding="utf-8")
    from ytclip import fonts

    fontsdir = ":fontsdir=fonts" if fonts.stage(K.fonts_used(track), tmp_path) else ""
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=540x960:r=10:d=1", "-vf",
                          f"subtitles=c.ass{fontsdir}", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, cwd=tmp_path, check=True).stdout
    frame = np.frombuffer(raw, np.uint8).reshape(960, 540)
    ys, xs = np.nonzero(frame > 128)
    boxes = [w.box for w in track.events[0].words]
    left, right = min(b[0] for b in boxes), max(b[0] + b[2] for b in boxes)
    top, bottom = min(b[1] for b in boxes), max(b[1] + b[3] for b in boxes)
    assert len(xs) > 500
    assert left - 6 <= xs.min() and xs.max() <= right + 6 and top - 4 <= ys.min() and ys.max() <= bottom + 4
    assert xs.min() < left + 12 and xs.max() > right - 12  # and they fill it side to side

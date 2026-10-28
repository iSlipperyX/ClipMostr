import numpy as np

from ytclip import cuts
from ytclip.analyze import FINE, Clip, cap_length
from ytclip.subtitles import Word, sentences


def _sound(length, quiet, speech=-20.0, silence=-90.0):
    """Fine loudness for `length` seconds: speech everywhere but the (start, end) pauses in `quiet`."""
    f = np.full(int(length / FINE), speech, np.float32)
    for a, b in quiet:
        f[int(round(a / FINE)):int(round(b / FINE))] = silence
    return f


# sentences end at 10.0, 20.0 and 30.0 (0.3 s breaths); words inside them have 0.06 s gaps every second
BREATHS = [(10.0, 10.3), (20.0, 20.3), (30.0, 30.3)]
WORD_GAPS = [(t, t + 0.08) for t in np.arange(1.0, 40.0, 1.0) if all(abs(t - a) > 0.5 for a, _ in BREATHS)]
SOUND = _sound(40, BREATHS + WORD_GAPS)


def test_pauses_are_found_against_the_sound_around_them():
    p = cuts.Pauses(SOUND)
    assert (10.0, 10.3) in [(round(a, 1), round(b, 1)) for a, b in p.runs(9, 11)]
    assert p.quiet_at(10.15) and not p.quiet_at(10.6)
    assert cuts.Pauses(_sound(40, [(10.0, 10.3)], silence=-24.0)).runs(9, 11) == []  # a dip too small to trust
    music = _sound(40, [(10.0, 10.3)], speech=-20.0, silence=-45.0)
    assert cuts.Pauses(music).runs(9, 11)  # a pause over quiet music still counts


def test_an_edge_in_a_word_moves_into_the_nearest_pause():
    p = cuts.Pauses(SOUND)
    clips = [Clip(10.6, 19.5, 80)]  # starts in a word just after a breath, ends in a word just before one
    assert cuts.clean_edges(clips, p, 40, min_len=5, max_len=20) == 1
    assert 10.0 <= clips[0].start <= 10.3 and 20.0 <= clips[0].end <= 20.3


def test_an_edge_already_in_a_pause_stays():
    clips = [Clip(10.2, 20.1, 80)]
    assert cuts.clean_edges(clips, cuts.Pauses(SOUND), 40, 5, 20) == 0


def test_edges_stay_inside_the_lengths_and_off_the_neighbours():
    p = cuts.Pauses(SOUND)
    capped = [Clip(10.2, 19.6, 80)]  # the breath at 20 would make it longer than allowed
    cuts.clean_edges(capped, p, 40, min_len=5, max_len=9.5)
    assert capped[0].length <= 10.0 and capped[0].end < 20.0
    pair = [Clip(10.2, 19.6, 80), Clip(19.9, 29.8, 70)]  # can't end inside the next clip
    cuts.clean_edges(pair, p, 40, 5, 20)
    assert pair[0].end <= pair[1].start


def test_clips_you_edited_and_quiet_less_sound_are_left_alone():
    edited = [Clip(10.6, 19.5, 80, edited=True)]
    assert cuts.clean_edges(edited, cuts.Pauses(SOUND), 40, 5, 20) == 0
    assert cuts.clean_edges([Clip(10.6, 19.5, 80)], cuts.Pauses(np.zeros(0)), 40) == 0
    flat = cuts.Pauses(np.full(2000, -20.0))
    assert cuts.clean_edges([Clip(10.6, 19.5, 80)], flat, 40) == 0


def test_a_part_still_carries_on_from_the_part_before():
    sound = _sound(40, [(10.0, 10.3), (19.2, 19.5), (22.4, 22.7), (30.0, 30.3)])
    parts = [Clip(10.2, 20.4, 80), Clip(21.5, 30.1, 80)]  # the nearest pauses would pull them 3.2 s apart
    cuts.clean_edges(parts, cuts.Pauses(sound), 40, 5, 20)
    assert (parts[0].end, parts[1].start) == (20.4, 21.5)
    alone = [Clip(10.2, 20.4, 80)]
    cuts.clean_edges(alone, cuts.Pauses(sound), 40, 5, 20)
    assert 19.2 <= alone[0].end <= 19.5  # without a part after it, the end does move


def test_auto_caption_words_get_the_pauses_the_audio_has():
    words = [Word(t, t + 1.0, w) for t, w in [(8.0, "so"), (9.0, "it"), (10.25, "works"), (11.25, "and")]]
    for a, b in zip(words, words[1:]):
        a.end = b.start  # auto-captions only time where each word starts
    assert not cuts.punctuated(words)
    assert cuts.refine_words(words, cuts.Pauses(SOUND)) == 1
    assert round(words[1].end, 1) == 10.0 and round(words[2].start, 1) == 10.3
    got = sentences(words, pause=cuts.BREATH - 0.01)
    assert [c.text for c in got] == ["so it", "works and"]


def test_punctuation_is_noticed():
    assert cuts.punctuated([Word(0, 1, "Hello."), Word(1, 2, "there")])
    assert cuts.punctuated([])


def test_rounding_out_to_sentences_never_runs_over_the_longest_length():
    from ytclip.subtitles import Cue

    sents = [Cue(t, t + 4.5, "s") for t in np.arange(0.0, 100.0, 5.0)]
    assert cap_length(0.0, 64.8, sents, 20, 60) == 59.8  # back to the last sentence that fits, with its breath
    assert cap_length(0.0, 59.7, sents, 20, 60) == 59.7  # already fits
    assert cap_length(40.0, 64.8, sents, 30, 20) == 64.8  # nothing fits and stays long enough: left alone


# ---------------------------------------------------------------- found in review (2.4.0)

def test_two_clips_never_become_a_series_by_moving_their_edges():
    from ytclip import layout

    sound = _sound(40, [(10.0, 10.3), (20.0, 20.3), (22.95, 23.1), (30.0, 30.3)])
    clips = [Clip(10.2, 19.5, 80), Clip(23.4, 30.1, 70)]  # 3.9 s apart; the pauses near would leave 2.8 s
    cuts.clean_edges(clips, cuts.Pauses(sound), 40, 5, 20)
    assert clips[1].start - clips[0].end > layout.CARRIES_ON
    assert not layout.has_next_part(clips[0], clips)  # so the first doesn't promise a part 2


def test_edges_never_take_a_clip_under_the_shortest_length():
    clips = [Clip(9.9, 20.1, 80)]  # the shortest allowed; the breath just after its start would make it shorter
    cuts.clean_edges(clips, cuts.Pauses(SOUND), 40, min_len=10.2, max_len=20)
    assert clips[0].length >= 10.2 - 1e-6


def test_found_pauses_keep_the_words_in_order():
    words = [Word(0.5, 0.9, "so"), Word(1.0, 1.05, "the"), Word(1.1, 1.5, "thing"), Word(1.6, 2.0, "is")]
    cuts.refine_words(words, cuts.Pauses(_sound(10, [(0.92, 1.18)])))
    starts = [w.start for w in words]
    assert starts == sorted(starts) and all(a.end <= b.start + 1e-6 for a, b in zip(words, words[1:]))

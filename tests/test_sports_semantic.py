"""The event search's meaning side, offline: a stand-in for the sentence model (so no 90 MB download and no
onnxruntime), how the commentary is cut into windows, how a cosine becomes a 0-1 match, the vectors kept on
disk, and where a clip's edges land (tests/test_event_search.py covers the search with word matching)."""

from types import SimpleNamespace

import numpy as np
import pytest

from ytclip.sportsdesk import semantic_search as ss
from ytclip.subtitles import Cue

# Phrases that mean the same thing share an axis; everything else only shares the small "said something" axis.
MEANINGS = (
    ("pitch invader", "streaker", "onto the pitch", "ran onto", "fan on the field", "stewards"),
    ("red card", "sent off", "marching orders"),
    ("corner", "set piece"),
    ("rain", "weather", "downpour"),
)


class FakeMiniLM:
    """Looks like embeddings.MiniLM to the search (name "minilm", unit vectors) and counts what it's asked."""

    name = "minilm"
    label = "all-MiniLM-L6-v2 (test)"
    device = "CPU"

    def __init__(self):
        self.asked: list[list[str]] = []

    def vector(self, text: str) -> np.ndarray:
        low = text.lower()
        v = np.array([sum(p in low for p in group) for group in MEANINGS] + [0.15], np.float32)
        return v / np.linalg.norm(v)

    def embed(self, texts: list[str]) -> np.ndarray:
        self.asked.append(list(texts))
        return np.stack([self.vector(t) for t in texts]) if texts else np.zeros((0, len(MEANINGS) + 1), np.float32)

    def fit(self, texts):  # only word matching learns its vocabulary from the windows
        raise AssertionError("the sentence model isn't fitted")


GAME = [
    (300, "We're under way in the rain here."),
    (900, "Lovely spell of possession from the home side."),
    (1800, "Oh, there's somebody on the field, a supporter has got over the hoardings."),
    (1806, "He's running onto the pitch and the stewards are chasing him round the centre circle."),
    (1813, "They've got him in the end, and the game can carry on."),
    (2500, "Corner kick to the visitors."),
    (3000, "Half-time whistle."),
]
DURATION = 5400.0


def cues(lines=GAME):
    return [Cue(t, t + 5.5, text) for t, text in lines]


# ---------------------------------------------------------------- meaning, not words


def test_a_moment_nobody_names_is_found_by_its_meaning():
    q = ss.parse("streaker runs onto the pitch", "soccer")
    assert q.concepts == []  # no stock idea to look for: only the meaning can find it
    model = FakeMiniLM()
    r = ss.search(q, cues(), DURATION, sport="soccer", embedder=model, use_ai=False)
    assert r.reader == "all-MiniLM-L6-v2 (test)" and r.device == "CPU"
    assert r.hits, r.notes
    best = r.hits[0]
    assert best.sources == ["meaning"] and 1795 <= best.t_event <= 1815
    assert best.start < 1806 < best.end  # the run itself is in the clip
    assert not any(abs(h.t_event - 2500) < 30 for h in r.hits)  # the corner means something else


def test_meaning_alone_needs_a_strong_match():
    r = ss.search(ss.parse("heavy downpour stops play", "soccer"), cues(), DURATION, sport="soccer",
                  embedder=FakeMiniLM(), use_ai=False)
    # "rain" is said once, early, beside other words: the window means it only partly
    for h in r.hits:
        assert h.score >= ss.QUALIFY * 100


def test_a_window_found_by_meaning_is_timed_a_third_of_the_way_in():
    model = FakeMiniLM()
    r = ss.search(ss.parse("streaker runs onto the pitch", "soccer"), cues(), DURATION, sport="soccer",
                  embedder=model, use_ai=False)
    starts = [w.start for w in ss.windows(cues(), DURATION)]
    assert any(abs(r.hits[0].t_event - (s + ss.WINDOW / 3)) < 1e-6 for s in starts)


def test_the_question_and_its_alternatives_are_read_once():
    model = FakeMiniLM()
    ss.search(ss.parse("streaker runs onto the pitch", "soccer"), cues(), DURATION, sport="soccer",
              embedder=model, use_ai=False)
    windows_asked, question_asked = model.asked[0], model.asked[1]
    assert len(windows_asked) == len(ss.windows(cues(), DURATION))
    assert "streaker runs onto the pitch" in question_asked


@pytest.mark.parametrize("cos, reader, want", [
    (0.10, "minilm", 0.0), (0.18, "minilm", 0.0), (0.40, "minilm", 0.5), (0.62, "minilm", 1.0), (0.9, "minilm", 1.0),
    (0.05, "words", 0.0), (0.25, "words", 0.5), (0.45, "words", 1.0), (0.40, "something new", 0.5),
])
def test_cosines_are_scaled_per_reader(cos, reader, want):
    assert ss._scale(np.array([cos]), reader)[0] == pytest.approx(want, abs=1e-6)


# ---------------------------------------------------------------- windows


def test_windows_follow_the_stride_and_skip_silence():
    lines = [Cue(7, 12, "first thing"), Cue(13, 14, "second"), Cue(40, 45, "much later")]
    wins = ss.windows(lines, 60.0)
    assert [(w.start, w.end) for w in wins] == [(5.0, 20.0), (10.0, 25.0), (30.0, 45.0), (35.0, 46.0),
                                                (40.0, 46.0)]
    assert wins[0].text == "first thing second" and wins[2].text == "much later"
    assert ss.windows([], 60.0) == []


def test_windows_never_run_past_the_video():
    wins = ss.windows([Cue(0, 4, "a"), Cue(95, 99, "b")], 97.0)
    assert max(w.end for w in wins) <= 97.0


def test_every_cue_is_in_some_window():
    wins = ss.windows(cues(), DURATION)
    for c in cues():
        assert any(w.start <= c.start < w.end and c.text.split()[0] in w.text for w in wins)


# ---------------------------------------------------------------- the vectors on disk


def test_the_sentence_models_vectors_are_kept_per_video(tmp_path):
    s = SimpleNamespace(cache_path=tmp_path)
    model = FakeMiniLM()
    texts = ["red card", "corner"]
    first = ss.embed_cached(model, texts, s, "game:1")
    again = ss.embed_cached(model, texts, s, "game:1")
    assert len(model.asked) == 1 and np.array_equal(first, again)
    assert len(list((tmp_path / "embeddings").glob("game_1.*.npy"))) == 1
    ss.embed_cached(model, texts + ["rain"], s, "game:1")  # other windows: worked out afresh
    assert len(model.asked) == 2


def test_nothing_is_kept_without_a_key_settings_or_the_sentence_model(tmp_path):
    s = SimpleNamespace(cache_path=tmp_path)
    model = FakeMiniLM()
    ss.embed_cached(model, ["a"], s, "")
    ss.embed_cached(model, ["a"], None, "k")
    assert len(model.asked) == 2 and not (tmp_path / "embeddings").exists()

    class Words(FakeMiniLM):
        name = "words"

    ss.embed_cached(Words(), ["a"], s, "k")
    assert not (tmp_path / "embeddings").exists()


def test_a_broken_cache_file_is_worked_out_again(tmp_path):
    s = SimpleNamespace(cache_path=tmp_path)
    model = FakeMiniLM()
    ss.embed_cached(model, ["a", "b"], s, "g")
    path = next((tmp_path / "embeddings").glob("g.*.npy"))
    path.write_bytes(b"not numpy")
    got = ss.embed_cached(model, ["a", "b"], s, "g")
    assert got.shape[0] == 2 and len(model.asked) == 2


# ---------------------------------------------------------------- the clip's edges


def test_a_start_inside_a_sentence_takes_the_whole_sentence():
    said = [Cue(97.0, 102.0, "Here he comes down the left.")]
    start, _ = ss.snap_edges(99.0, 140.0, 110.0, said, None, 600.0)
    assert start == 97.0


def test_a_start_deep_inside_a_long_sentence_leaves_it_out():
    said = [Cue(90.0, 104.0, "A long, long sentence about something else entirely, still going.")]
    start, _ = ss.snap_edges(99.0, 140.0, 110.0, said, None, 600.0)
    assert start == 104.0


def test_an_end_inside_a_sentence_waits_for_it_or_stops_before_it():
    start, end = ss.snap_edges(90.0, 130.0, 110.0, [Cue(128.0, 132.0, "Short one.")], None, 600.0)
    assert end == 132.0
    start, end = ss.snap_edges(90.0, 130.0, 110.0, [Cue(125.0, 140.0, "A very long one.")], None, 600.0)
    assert end == 125.0


def test_edges_keep_the_moment_and_stay_inside_the_video():
    start, end = ss.snap_edges(9.5, 10.5, 10.0, [], None, 11.0)
    assert start <= 9.0 and end == 11.0  # a second before, and the end of the video (not 12)
    start, end = ss.snap_edges(-5.0, 30.0, 2.0, [], None, 0.0)
    assert start == 0.0 and end == 30.0


def test_cut_uses_the_ideas_own_build_up_and_aftermath():
    c = ss.CONCEPTS["red_card"]
    start, end = ss.cut(1000.0, c, [], 5000.0, before=ss.PAD_BEFORE, after=ss.PAD_AFTER)
    assert (1000.0 - start, end - 1000.0) == pytest.approx((c.before, c.after))
    start, end = ss.cut(1000.0, None, [], 5000.0, before=10.0, after=20.0)
    assert (start, end) == (990.0, 1020.0)


def test_cut_stays_while_the_commentary_is_still_on_it():
    c = ss.CONCEPTS["red_card"]
    plain = ss.cut(1000.0, c, [], 5000.0, before=12.0, after=25.0)[1]
    talk = [Cue(plain + 1, plain + 4, "Still talking about that red card."),
            Cue(plain + 6, plain + 9, "What a sending off that was.")]
    longer = ss.cut(1000.0, c, talk, 5000.0, before=12.0, after=25.0)[1]
    assert plain < longer <= plain + ss.STRETCH + 5.0


def test_cut_reaches_back_to_what_led_to_it():
    start, _ = ss.cut(1000.0, None, [], 5000.0, before=12.0, after=25.0, lead_from=975.0)
    assert start == 971.0
    start, _ = ss.cut(1000.0, None, [], 5000.0, before=12.0, after=25.0, lead_from=900.0)  # too long ago
    assert start == 988.0

import pytest
from conftest import NOW, api_item, spiky_heatmap

from ytclip import clipability as c
from ytclip.trending import DeepInfo, video_from_api


def _video(**kw):
    v = video_from_api(api_item(**kw))
    v.velocity = kw.get("views", 100_000) / max(1, kw.get("hours_old", 10.0))
    return v


def test_length_fit_shape():
    assert c.length_fit(45) == 0  # already a Short
    assert c.length_fit(60) == 0
    assert 0.15 < c.length_fit(150) < 0.45
    assert c.length_fit(8 * 60) == pytest.approx(0.9)
    assert c.length_fit(30 * 60) == c.length_fit(90 * 60) == 1.0
    assert 0.75 <= c.length_fit(10 * 3600) < 1.0
    values = [c.length_fit(m * 60) for m in range(1, 91)]
    assert values == sorted(values)  # never worse to be longer, up to 90 min


def test_freshness_decays_after_a_day_and_a_half():
    assert c.freshness(1) == c.freshness(36) == 1.0
    assert c.freshness(7 * 24) < 1.0
    assert c.freshness(30 * 24) == pytest.approx(0.25)


def test_momentum_blends_absolute_and_rank():
    assert c.momentum(0, []) == 0
    assert c.momentum(c.VELOCITY_CAP, []) == pytest.approx(1.0)
    assert c.momentum(10 * c.VELOCITY_CAP, []) == 1.0
    pool = [10, 100, 1000, 10_000]
    assert c.momentum(10_000, pool) > c.momentum(1000, pool) > c.momentum(10, pool)
    # The best of a quiet region still gets the full rank half.
    assert c.momentum(1000, [10, 100, 1000]) > c.momentum(1000, [1000, 50_000, 90_000])


def test_engagement_handles_hidden_counts():
    assert c.engagement(_video(views=1000, likes=None, comments=None)) is None
    assert c.engagement(_video(views=1000, likes=None, comments=10)) == pytest.approx(30 / 1000 / c.ENGAGEMENT_CAP)
    assert c.engagement(_video(views=1000, likes=None, comments=None), likes=30) == pytest.approx(0.5)
    assert c.engagement(_video(views=100, likes=100, comments=100)) == 1.0
    assert c.engagement(_video(views=0, likes=5, comments=5)) is None


def test_replay_peaks_finds_standout_moments():
    strength, peaks = c.replay_peaks(spiky_heatmap(1200, (300, 700, 1000)), 1200)
    assert strength > 0.8
    assert len(peaks) == 3
    for found, expected in zip(peaks, (300, 700, 1000)):
        assert abs(found - expected) <= 15


def test_replay_peaks_flat_graph_is_weak():
    flat = [{"start_time": i * 12, "end_time": (i + 1) * 12, "value": 0.9 + (i % 3) * 0.03} for i in range(100)]
    strength, _ = c.replay_peaks(flat, 1200)
    assert strength < 0.5


def test_replay_peaks_ignores_the_intro_spike():
    # Everyone watches the first seconds: that alone isn't a clip.
    _, peaks = c.replay_peaks(spiky_heatmap(1200, (5, 600)), 1200)
    assert all(p > 30 for p in peaks)


def test_replay_peaks_without_data():
    assert c.replay_peaks([], 600) == (0.0, [])


def test_score_reweights_missing_parts():
    v = _video(likes=None, comments=None)
    c.score(v, [v.velocity], NOW)
    assert "engagement" not in v.parts and "replays" not in v.parts
    interest = {k: p for k, p in v.parts.items() if k in c.WEIGHTS}
    blend = sum(c.WEIGHTS[k] * p for k, p in interest.items()) / sum(c.WEIGHTS[k] for k in interest)
    assert v.score == pytest.approx(blend * c.material(v.parts["length"], v.parts["category"]) * 100, abs=0.1)


def test_material_multiplier():
    assert c.material(1, 1) == 1.0
    assert c.material(0, 1) == c.FIT_FLOOR
    assert c.material(0.5, 0.15) < c.material(1, 0.6) < c.material(1, 1)


def test_hot_music_video_is_still_weak_material():
    music = _video(views=12_000_000, hours_old=30, likes=900_000, comments=60_000, minutes=3.6, category="10")
    game = _video(views=1_500_000, hours_old=30, likes=60_000, comments=4_000, minutes=40, category="20")
    for v in (music, game):
        c.score(v, [music.velocity, game.velocity], NOW)
    assert music.score < 50 < game.score


def test_score_uses_heatmap_and_reports_moments():
    v = _video(minutes=20)
    v.deep = DeepInfo(heatmap=spiky_heatmap(1200, (300, 700)), captions=True, chapters=5, checked=NOW)
    c.score(v, [v.velocity], NOW)
    assert v.parts["replays"] > 0.6 and v.parts["transcript"] == 1.0
    assert len(v.moments) == 2
    text = " ".join(v.reasons)
    assert "5:00" in text and "chapters" in text


def test_failed_deep_check_is_ignored():
    v = _video()
    v.deep = DeepInfo(error="Sign in to confirm your age", checked=NOW)
    c.score(v, [v.velocity], NOW)
    assert "replays" not in v.parts


def test_category_matters():
    gaming, music = _video(category="20"), _video(category="10")
    for v in (gaming, music):
        c.score(v, [gaming.velocity], NOW)
    assert gaming.score > music.score
    assert any("copyright" in r for r in music.reasons)


@pytest.mark.parametrize("kw,cap,phrase", [
    ({"live": "live"}, 15, "Live right now"),
    ({"live": "upcoming"}, 5, "premiered"),
    ({"minutes": 0.75}, 20, "Already a Short"),
])
def test_unclippable_videos_are_capped(kw, cap, phrase):
    v = _video(views=10_000_000, hours_old=1, **kw)
    c.score(v, [v.velocity], NOW)
    assert v.score <= cap
    assert phrase in v.reasons[0]


def test_better_video_scores_higher():
    hot = _video(views=2_000_000, hours_old=5, likes=150_000, comments=20_000, minutes=35)
    hot.deep = DeepInfo(heatmap=spiky_heatmap(35 * 60, (400, 900, 1500)), captions=True, checked=NOW)
    meh = _video(views=200_000, hours_old=24 * 10, likes=2_000, comments=50, minutes=4, category="25")
    pool = [hot.velocity, meh.velocity]
    for v in (hot, meh):
        c.score(v, pool, NOW)
    assert hot.score >= 80 > 45 > meh.score
    assert c.verdict(hot.score) == "Excellent clip potential"
    assert c.verdict(meh.score) == "Weak clip potential"


def test_helpers():
    assert c.compact(999) == "999" and c.compact(1500) == "1.5K" and c.compact(2_000_000) == "2M"
    assert c.age_text(0.2) == "12 min" and c.age_text(5) == "5 h" and c.age_text(72) == "3 days"
    assert set(c.WEIGHTS) | set(c.FIT) == set(c.PART_LABELS)
    assert sum(c.WEIGHTS.values()) == pytest.approx(1.0)

"""The Research dashboard's numbers (analytics.py): momentum, viral potential, longevity, clusters, outliers,
the category velocity chart and the Most replayed graph's clip windows."""

import pytest
from conftest import NOW, api_item

from ytclip import analytics
from ytclip.analytics import Momentum
from ytclip.trending import DeepInfo, History, video_from_api

H = 3600.0


def vid(i="v", **kw):
    return video_from_api(api_item(i, **kw))


def heatmap(values, seg=10.0):
    return [{"start_time": k * seg, "end_time": (k + 1) * seg, "value": x} for k, x in enumerate(values)]


# ---------------------------------------------------------------- momentum


def test_momentum_is_measuring_until_two_checks_far_enough_apart():
    v = vid(views=100_000, hours_old=10)
    m = analytics.momentum(v, [], NOW)
    assert not m.known and m.label == "Measuring" and m.delta == 0
    assert m.hourly == m.baseline == pytest.approx(10_000)  # the average since upload
    assert "Measuring" in m.text() and m.short() == "Measuring"
    m = analytics.momentum(v, [[NOW - 600, 99_000], [NOW, 100_000]], NOW)
    assert not m.known  # ten minutes is too noisy


def test_momentum_compares_lately_with_the_last_day():
    v = vid(views=100_000, hours_old=30)
    snaps = [[NOW - 20 * H, 20_000], [NOW - H, 60_000], [NOW, 100_000]]
    m = analytics.momentum(v, snaps, NOW)
    assert m.known and not m.since_upload
    assert m.hourly == pytest.approx(40_000)  # the last hour
    assert m.baseline == pytest.approx(80_000 / 20)  # 4K an hour over the day
    assert m.delta == pytest.approx(9.0) and m.label == "Exploding" and m.arrow == "⇈"
    assert m.short() == "⇈ +900%" and "its last day" in m.text()


def test_momentum_with_only_two_checks_compares_with_the_average_since_upload():
    v = vid(views=50_000, hours_old=10)
    m = analytics.momentum(v, [[NOW - 2 * H, 46_000], [NOW, 50_000]], NOW)
    assert m.known and m.since_upload
    assert m.hourly == pytest.approx(2_000) and m.baseline == pytest.approx(5_000)
    assert m.label == "Fading" and m.delta == pytest.approx(-0.6)


@pytest.mark.parametrize("delta, label", [(1.2, "Exploding"), (0.4, "Accelerating"), (0.12, "Rising"),
                                          (0.0, "Steady"), (-0.3, "Cooling"), (-0.7, "Fading")])
def test_momentum_labels(delta, label):
    m = Momentum(hourly=1000 * (1 + delta), baseline=1000, known=True, span_minutes=60)
    assert m.label == label


def test_a_check_that_went_down_is_not_used():
    """YouTube sometimes corrects a view count down: that check isn't a sensible start."""
    v = vid(views=10_000, hours_old=5)
    m = analytics.momentum(v, [[NOW - H, 12_000], [NOW, 10_000]], NOW)
    assert not m.known


# ---------------------------------------------------------------- viral potential


def test_viral_potential_uses_the_plans_weights():
    assert analytics.VIRAL_WEIGHTS == {"velocity": 0.35, "peakiness": 0.25, "engagement": 0.20, "saturation": 0.20}
    assert analytics.viral_score({"velocity": 1, "peakiness": 1, "engagement": 1, "saturation": 1}) == 100
    assert analytics.viral_score({"velocity": 1, "peakiness": 0, "engagement": 0, "saturation": 0}) == 35
    # A part that couldn't be measured is left out and the rest re-weighted
    assert analytics.viral_score({"velocity": 1, "saturation": 0}) == pytest.approx(100 * 0.35 / 0.55, abs=0.1)
    assert analytics.viral_score({}) == 0


def test_velocity_and_saturation_parts():
    assert analytics.velocity_part(0) == 0 and analytics.velocity_part(99) == 0
    assert analytics.velocity_part(250_000) == pytest.approx(1.0, abs=0.01)
    assert 0.25 < analytics.velocity_part(1_000) < analytics.velocity_part(10_000) < 0.65
    assert analytics.saturation_part(1, 10) == 1.0 and analytics.saturation_part(10, 10) == pytest.approx(0.2)
    assert analytics.saturation_part(3, 1) == 1.0


def test_engagement_ratio_is_likes_and_comments_per_view():
    assert analytics.engagement_ratio(vid(views=10_000, likes=300, comments=100)) == pytest.approx(0.04)
    assert analytics.engagement_ratio(vid(likes=None)) is None
    hidden = vid(views=10_000, likes=None, comments=50)
    hidden.deep = DeepInfo(likes=450)  # yt-dlp can still see the likes
    assert analytics.engagement_ratio(hidden) == pytest.approx(0.05)


def test_insights_score_each_video_and_leave_out_what_isnt_measured():
    a = vid("a", views=500_000, likes=30_000, comments=1_000, hours_old=5)
    a.velocity = 100_000
    a.deep = DeepInfo(heatmap=heatmap([0.2] * 30 + [1.0, 0.9] + [0.2] * 30), checked=NOW)
    b = vid("b", views=5_000, likes=None, hours_old=50)
    b.velocity = 100
    found = analytics.insights([a, b], {}, now=NOW)
    assert set(found["a"].parts) == {"velocity", "peakiness", "engagement", "saturation"}
    assert set(found["b"].parts) == {"velocity", "saturation"}  # no replay graph, likes hidden
    assert found["a"].viral > found["b"].viral
    assert found["a"].cluster == found["b"].cluster == "Gaming"


def test_videos_cluster_by_your_category_else_youtubes():
    a, b, c = vid("a", category="20"), vid("b", category="10"), vid("c", category="")
    a.category_id = "g1"
    names = {"g1": "R6 clips"}
    assert [analytics.cluster_key(x, names) for x in (a, b, c)] == ["R6 clips", "Music", "Other"]
    d = vid("d", category="20")  # a gaming video that fits none of yours, beside your own "Gaming"
    assert analytics.cluster_key(d, {"g1": "Gaming"}) == "Gaming (YouTube)"


def test_a_crowded_cluster_lowers_saturation():
    videos = [vid(f"g{i}", category="20") for i in range(5)] + [vid("m", category="10")]
    found = analytics.insights(videos, {}, now=NOW)
    assert found["m"].parts["saturation"] == 1.0
    assert found["g0"].parts["saturation"] == pytest.approx(0.2)


# ---------------------------------------------------------------- longevity


@pytest.mark.parametrize("title, category, hours_old, live, want", [
    ("BREAKING: studio shuts down", "20", 5, "none", "ephemeral"),
    ("Streamer responds to the drama", "22", 10, "none", "ephemeral"),
    ("Cup final highlights", "17", 20, "none", "ephemeral"),
    ("Live now: the big match", "17", 1, "live", "ephemeral"),
    ("How to beat every boss", "20", 5, "none", "evergreen"),
    ("The funniest moments of the year", "23", 20, "none", "evergreen"),
    ("A video from last month", "24", 24 * 9, "none", "evergreen"),
    ("Physics of black holes", "28", 10, "none", "evergreen"),
    ("My trip to the coast", "19", 10, "none", "evergreen"),
])
def test_longevity(title, category, hours_old, live, want):
    v = vid(title=title, category=category, hours_old=hours_old, live=live)
    kind, why = analytics.longevity(v, Momentum(), NOW)
    assert kind == want and why


def test_a_young_video_fading_fast_is_ephemeral():
    v = vid(title="Some gameplay", hours_old=20)
    kind, why = analytics.longevity(v, Momentum(hourly=200, baseline=1000, known=True), NOW)
    assert kind == "ephemeral" and "fading" in why


# ---------------------------------------------------------------- outliers and arbitrage


def test_outliers_stand_far_above_their_cluster():
    speeds = {"a": 1000, "b": 1200, "c": 900, "d": 1500, "e": 1100, "x": 8000}
    out = analytics.outliers(speeds)
    assert list(out) == ["x"] and out["x"] == pytest.approx(8000 / 1150, abs=0.1)
    # A cluster that's all over the place has no outlier, however big its biggest is
    assert analytics.outliers({"a": 100, "b": 1000, "c": 10_000, "d": 3000, "e": 500, "f": 30_000}) == {}
    assert analytics.outliers({"a": 1, "b": 100}) == {}  # too few to say


def test_small_clusters_are_compared_with_the_whole_check():
    videos = []
    for i in range(6):
        v = vid(f"g{i}", category="20")
        v.velocity = 1000 + 50 * i
        videos.append(v)
    star = vid("m", category="10")
    star.velocity = 20_000
    found = analytics.insights(videos + [star], {}, now=NOW)
    assert found["m"].outlier > 3 and found["m"].outlier_of == "this check"
    assert not any(found[f"g{i}"].outlier for i in range(6))


def test_live_arbitrage_is_fast_recent_and_not_used_yet():
    videos = []
    for i in range(10):
        v = vid(f"v{i}", hours_old=10, minutes=20)
        v.velocity = 500 * (i + 1)
        videos.append(v)
    videos[9].used = True  # the fastest, but you've clipped it already
    found = analytics.insights(videos, {}, now=NOW)
    assert {k for k, i in found.items() if i.arbitrage} == {"v8"}  # the rest aren't in the fastest fifth
    assert "nothing from you yet" in found["v8"].arbitrage_why and "10 h old" in found["v8"].arbitrage_why
    videos[8].duration = 45  # a Short: nothing to clip from it
    assert not any(i.arbitrage for i in analytics.insights(videos, {}, now=NOW).values())
    videos[8].duration, videos[8].published = 1200, NOW - 100 * H  # too old to race for
    assert not any(i.arbitrage for i in analytics.insights(videos, {}, now=NOW).values())


def test_accelerating_videos_count_as_arbitrage():
    h = History(None)
    v = vid("up", views=20_000, hours_old=30)
    h.videos["up"] = {"first_seen": NOW - 20 * H, "snapshots": [[NOW - 20 * H, 2_000], [NOW - H, 12_000],
                                                                 [NOW, 20_000]]}
    calm = [vid(f"c{i}") for i in range(5)]
    for c in calm:
        c.velocity = 50_000  # all faster, but steady
    found = analytics.insights([v] + calm, {}, h, now=NOW)
    assert found["up"].momentum.label == "Exploding"
    assert found["up"].arbitrage and "exploding" in found["up"].arbitrage_why


# ---------------------------------------------------------------- clusters and the velocity chart


def test_clusters_best_first_with_their_numbers():
    gaming = [vid(f"g{i}", category="20", likes=8000, comments=500, views=100_000) for i in range(3)]
    for k, g in enumerate(gaming):
        g.velocity = 50_000 * (k + 1)
    music = [vid("m0", category="10", likes=100, comments=0, views=100_000)]
    music[0].velocity = 200
    found = analytics.insights(gaming + music, {}, now=NOW)
    cs = analytics.clusters(found)
    assert [c.name for c in cs] == ["Gaming", "Music"]
    g = cs[0]
    assert g.count == 3 and g.momentum is None  # nothing measured lately yet
    assert g.speed == pytest.approx(300_000) and g.top(1)[0].video.id == "g2"
    assert g.longevity_mix() == {"ephemeral": 0, "evergreen": 3}


def test_your_categories_come_first():
    mine = vid("m", category="20", views=1000)
    mine.category_id, mine.velocity = "g1", 10
    hot = vid("h", category="10", views=10_000_000)
    hot.velocity = 900_000
    cs = analytics.clusters(analytics.insights([hot, mine], {"g1": "R6 clips"}, now=NOW))
    assert [(c.name, c.yours) for c in cs] == [("R6 clips", True), ("Music", False)]


def test_category_velocity_hour_by_hour_from_the_checks():
    h = History(None)
    a, b = vid("a"), vid("b")
    # a: 1000 views an hour over the last 3 hours; b: 500 an hour over the last hour only
    h.videos["a"] = {"snapshots": [[NOW - 3 * H, 0], [NOW, 3000]]}
    h.videos["b"] = {"snapshots": [[NOW - H, 1000], [NOW, 1500]]}
    found = analytics.insights([a, b], {}, h, now=NOW)
    row = analytics.cluster_velocity(found, h, NOW)["Gaming"]
    assert len(row) == analytics.HOURS
    assert row[-1] == pytest.approx(1500) and row[-2] == pytest.approx(1000) and row[-3] == pytest.approx(1000)
    assert row[-4] is None and row[0] is None
    assert analytics.clusters(found, h, NOW)[0].velocity == row


def test_checks_too_far_apart_say_nothing_about_an_hour():
    h = History(None)
    h.videos["a"] = {"snapshots": [[NOW - 10 * H, 0], [NOW, 10_000]]}
    found = analytics.insights([vid("a")], {}, h, now=NOW)
    assert analytics.cluster_velocity(found, h, NOW)["Gaming"] == [None] * analytics.HOURS


# ---------------------------------------------------------------- the Most replayed graph


def test_clip_windows_frame_each_peak_with_its_punchline_and_drop_off():
    values = [0.2] * 20 + [0.5, 0.9, 1.0, 0.8, 0.3] + [0.2] * 20 + [0.6, 0.7, 0.2] + [0.2] * 10
    ws = analytics.clip_windows(heatmap(values), duration=len(values) * 10)
    assert len(ws) == 2
    first, second = ws
    assert first.start <= 210 <= first.peak <= 230 <= first.end and first.peak == 225
    assert first.height == 1.0 and first.drop == pytest.approx(0.7)
    assert 15 <= first.end - first.start <= 90
    assert second.start >= first.end and 450 <= second.peak <= 470
    assert analytics.clip_windows(heatmap([0.5] * 3)) == []  # too little graph


def test_clip_windows_never_overlap_and_respect_the_lengths():
    values = [0.1] * 10 + [1.0, 0.1, 0.95] + [0.1] * 10 + [0.9] * 15 + [0.1] * 10
    ws = analytics.clip_windows(heatmap(values), max_len=60)
    for w in ws:
        assert w.end - w.start <= 60 + 1e-6
    for x, y in zip(ws, ws[1:]):
        assert x.end <= y.start


def test_the_intro_spike_and_end_screen_are_not_moments():
    """Most replayed graphs nearly always start high (people skipping in) and fall into the end screen."""
    values = [1.0, 0.8, 0.3] + [0.2] * 40 + [0.5, 0.6, 0.2] + [0.2] * 20 + [0.9, 0.2]
    ws = analytics.clip_windows(heatmap(values))
    assert [w.peak for w in ws] == [445.0]  # only the moment in the body
    assert ws[0].height == 1.0  # the body's own top, not the intro's
    assert all(20 <= t <= 670 - 33.5 for t, _ in analytics.drop_offs(heatmap(values)))
    assert analytics.edges(600) == (20.0, 570.0) and analytics.edges(3600) == (108.0, 3420.0)


def test_touching_windows_merge_when_they_still_fit():
    values = [0.1] * 10 + [0.9, 0.1, 0.1, 1.0] + [0.1] * 30
    ws = analytics.clip_windows(heatmap(values), max_len=90)
    assert len(ws) == 1 and ws[0].peak == 135.0 and ws[0].end - ws[0].start <= 90


def test_a_flat_graph_has_no_clip_windows():
    assert analytics.clip_windows(heatmap([0.5] * 40)) == []


def test_drop_offs_are_the_steepest_falls():
    values = [0.3] * 10 + [1.0, 0.2] + [0.3] * 10 + [0.8, 0.7, 0.4] + [0.3] * 10
    drops = analytics.drop_offs(heatmap(values))
    assert drops[0] == (110.0, pytest.approx(0.8))
    assert any(t == 230.0 for t, _ in drops)
    assert analytics.drop_offs(heatmap([0.5] * 10)) == []


def test_curve_scales_to_the_top_and_skips_bad_segments():
    segs = analytics.curve([{"start_time": 0, "end_time": 5, "value": 2}, {"start_time": 5, "end_time": 10,
                                                                           "value": 4}, {"bad": 1},
                            {"start_time": 10, "end_time": 10, "value": 9}])
    assert segs == [(0.0, 5.0, 0.5), (5.0, 10.0, 1.0)]
    assert analytics.curve([]) == []


def test_text_helpers():
    assert analytics.compact(1_234_567) == "1.2M" and analytics.compact(999) == "999"
    assert analytics.compact(12_000) == "12K"
    assert analytics.age_text(0.25) == "15 min" and analytics.age_text(5) == "5 h"
    assert analytics.age_text(72) == "3 days"

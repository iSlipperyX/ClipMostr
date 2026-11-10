"""Research's market side (velocity against the channel's week, Shorts saturation, opportunity, badges) and
the sports Clip-Ability index."""

import numpy as np
import pytest

from ytclip import clipability
from ytclip.research import ScanOptions, scan, velocity
from ytclip.trending import History, TrendingVideo

NOW = 1_794_000_000.0


def video(i, velocity_=1000.0, channel="c1", minutes=20, title="", category_id="", replays=None, score=60.0):
    v = TrendingVideo(f"v{i}", title or f"Video {i}", "Chan", NOW - 86400, minutes * 60, 100_000, channel_id=channel,
                      category_id=category_id)
    v.velocity, v.score = velocity_, score
    if replays is not None:
        v.parts["replays"] = replays
    return v


def history_with(videos):
    h = History(None)
    for v in videos:
        h.videos[v.id] = {"first_seen": NOW - 3600, "last_seen": NOW - 600, "snapshots": [], "channel": v.channel_id,
                          "pace": v.velocity}
    return h


def test_velocity_delta_is_against_the_channels_week():
    usual = [video(i, 1000.0 + i) for i in range(4)]
    hot = video(9, 5000.0)
    p = velocity.measure(hot, velocity.baselines(history_with(usual + [hot]), NOW), usual + [hot])
    assert (p.basis, p.peers) == ("channel", 4) and p.delta == pytest.approx(5.0, abs=0.02)


def test_a_new_channel_is_measured_against_its_category_then_the_scan():
    others = [video(i, 2000.0, channel=f"x{i}", category_id="sports") for i in range(3)]
    v = video(9, 4000.0, channel="new", category_id="sports")
    assert velocity.measure(v, {}, others + [v]).basis == "category"
    v.category_id = ""
    p = velocity.measure(v, {}, others + [v])
    assert p.basis == "scan" and p.delta == 2.0
    assert velocity.measure(v, {}, [v]).delta == 0.0  # nothing to compare with: not measured
    old = history_with([video(i, 1.0) for i in range(4)])
    for rec in old.videos.values():
        rec["last_seen"] = NOW - 8 * 86400
    assert velocity.baselines(old, NOW) == {}


def shorts_answer(n, own=0):
    items = [{"snippet": {"channelId": f"other{i}", "title": "Insane red card Arsenal Chelsea #shorts"}} for i in range(n)]
    items += [{"snippet": {"channelId": "c1", "title": "red card Arsenal Chelsea"}} for _ in range(own)]
    items += [{"snippet": {"channelId": "zz", "title": "My cat does a thing"}}]
    return {"items": items}


def test_saturation_counts_other_channels_shorts_and_is_remembered():
    usual = [video(i, 1000.0) for i in range(4)]
    hot = video(9, 5000.0, title="Arsenal vs Chelsea red card controversy", replays=0.3)
    h = history_with(usual)
    asked = []

    def get(url):
        asked.append(url)
        return shorts_answer(1, own=3)

    spent = velocity.annotate(usual + [hot], h, NOW, api_key="K", checks=2, get=get)
    assert spent == 100 and len(asked) == 1 and "videoDuration=short" in asked[0] and "publishedAfter" in asked[0]
    assert hot.pace["shorts"] == 1 and hot.badge == velocity.HOT and "TikTok can't be checked" in hot.pace["why"]
    assert velocity.annotate(usual + [hot], h, NOW + 60, api_key="K", checks=2, get=get) == 0  # remembered
    assert hot.badge == velocity.HOT and hot.opportunity > usual[0].opportunity


def test_badges_say_only_what_was_measured():
    hot = video(9, 5000.0, replays=0.2)
    p = velocity.Pace(velocity=5000, baseline=1000, delta=5.0, basis="channel", peers=4)
    assert velocity.badge(hot, p)[0] == velocity.RISING  # saturation not checked: no arbitrage claim
    p.saturation, p.shorts = 0.9, 11
    assert velocity.badge(hot, p)[0] == velocity.SATURATED
    p.saturation, p.shorts = 0.0, 0
    assert velocity.badge(hot, p)[0] == velocity.HOT
    assert velocity.badge(video(1, minutes=4), p)[0] == ""  # too short to be long-form arbitrage
    calm = velocity.Pace(delta=1.0, basis="channel")
    assert velocity.badge(video(2, replays=0.8), calm)[0] == velocity.RETENTION
    assert velocity.opportunity(hot, velocity.Pace(delta=5.0, saturation=0.0)) > velocity.opportunity(
        hot, velocity.Pace(delta=5.0)) > velocity.opportunity(hot, velocity.Pace(delta=5.0, saturation=1.0))


def test_checks_stop_at_the_daily_cap_and_on_an_error():
    from ytclip.trending import ResearchError

    usual = [video(i, 1000.0) for i in range(4)]
    risers = [video(10 + i, 5000.0, title=f"Big story number {i} today") for i in range(4)]
    h = history_with(usual)
    import time

    h.market = {"day": time.strftime("%Y-%m-%d", time.gmtime(NOW)), "count": velocity.DAILY_CHECKS - 1}
    assert velocity.annotate(usual + risers, h, NOW, api_key="K", checks=5, get=lambda u: {"items": []}) == 100

    def broken(url):
        raise ResearchError("quota used up")

    said = []
    h2 = history_with(usual)
    assert velocity.annotate(usual + risers, h2, NOW, api_key="K", checks=5, get=broken, log=said.append) == 0
    assert "quota used up" in said[0] and all(v.pace["saturation"] is None for v in risers)
    assert velocity.annotate(usual + risers, h2, NOW, api_key="", checks=5, get=broken) == 0


def test_topics_and_category_pills():
    vids = [video(1, 5000.0, title="VAR drama: red card in Arsenal vs Chelsea", category_id="soccer"),
            video(2, 900.0, title="Top 10 goals of the week", category_id="soccer", replays=0.9),
            video(3, 1000.0, title="Cooking pasta", category_id="food"), video(4, 1000.0), video(5, 1000.0)]
    velocity.annotate(vids, History(None), NOW)
    assert "soccer-refs" in vids[0].pace["topics"] and "soccer-goals" in vids[1].pace["topics"]
    report = velocity.topic_report(vids)
    assert {n.key for n in report} >= {"soccer-refs", "soccer-goals"} and report[0].opportunity >= report[-1].opportunity
    pills = velocity.by_category(vids, {"soccer": "Soccer", "food": "Food", "empty": "Empty"})
    assert pills["soccer"].videos == 2 and pills["soccer"].badge in (velocity.RISING, velocity.RETENTION)
    assert pills["empty"].videos == 0 and pills["empty"].badge == ""


def test_a_scan_fills_pace_and_keeps_the_channel(tmp_path):
    vids = [video(i, channel="c1") for i in range(4)]
    h = History(tmp_path / "research.json")
    result = scan(ScanOptions("K", ["US"], deep_count=0), h, now=NOW, fetch=lambda key, region, category, limit: vids)
    assert all("delta" in v.pace for v in result.videos)
    assert h.videos["v0"]["channel"] == "c1" and "pace" in h.videos["v0"]
    again = History(tmp_path / "research.json").last_videos()
    assert again and "delta" in again[0].pace


# ---------------------------------------------------------------- the sports index


def test_the_sports_index_uses_the_directives_weights():
    assert clipability.SPORTS_WEIGHTS == {"momentum": 0.30, "replayed": 0.25, "emotion": 0.20, "controversy": 0.15,
                                          "audio": 0.10}
    assert sum(clipability.SPORTS_WEIGHTS.values()) == pytest.approx(1.0)


def test_a_spiky_replay_graph_beats_a_flat_one():
    flat = [{"start_time": i * 10, "end_time": i * 10 + 10, "value": 0.3} for i in range(100)]
    spiky = [dict(h, value=1.0 if i in (40, 41, 77) else 0.08) for i, h in enumerate(flat)]
    assert clipability.replayed_shape(flat, 1000) == 0.0
    assert clipability.replayed_shape(spiky, 1000) > 0.7
    assert clipability.replayed_shape([], 1000) is None


def test_emotion_controversy_and_audio():
    rng = np.random.default_rng(5)
    dull = np.full(2400, -22.0) + rng.normal(0, 0.3, 2400)
    lively = dull.copy()
    lively[::200] += 16  # roars
    bright = np.full(2400, 1500.0)
    moving = bright + rng.normal(0, 700, 2400).clip(-1200, 3000)
    assert clipability.emotion(lively, moving, roars=6, duration=1200) > clipability.emotion(dull, bright, 0, 1200) + 0.4
    assert clipability.emotion(np.zeros(2), None) is None
    c, terms = clipability.controversy(["He was robbed! That's never a penalty.", "terrible ref, what a fight"], 10)
    assert c > 0.5 and terms == ["robbed", "never a penalty", "terrible ref", "fight"]
    assert clipability.controversy(["A calm afternoon of football."])[0] == 0.0
    assert clipability.controversy([])[0] is None
    assert "fight" not in clipability.controversy_terms("a fighting chance")
    flat = np.where(np.arange(2400) % 2 == 0, 0.05, 0.6)
    clear = np.where(np.arange(2400) % 2 == 0, -14.0, -34.0)
    muddy = np.where(np.arange(2400) % 2 == 0, -24.0, -22.0)
    assert clipability.audio_quality(clear, flat) > clipability.audio_quality(muddy, flat) + 0.3


def test_missing_parts_are_left_out_not_counted_as_zero():
    spiky = [{"start_time": i * 10, "end_time": i * 10 + 10, "value": 1.0 if i in (40, 77) else 0.05} for i in range(100)]
    full = clipability.sports_index(momentum=0.9, heatmap=spiky, duration=1000, texts=["robbed", "terrible ref"])
    assert set(full["missing"]) == {"emotion", "audio"} and full["score"] > 60 and "Not measured" in full["reasons"][-1]
    only = clipability.sports_index(momentum=0.5)
    assert only["score"] == 50.0 and only["parts"] == {"momentum": 0.5}
    assert clipability.sports_index()["score"] == 0.0

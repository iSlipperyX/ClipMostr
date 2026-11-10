"""Reels of found moments, the climax order, transition styles and the feed's lower-thirds."""

import pytest

from ytclip.sportsdesk import montage, reel as reel_module

GAME = {"id": "g1", "matchup": "Arsenal vs Chelsea", "home": "Arsenal", "away": "Chelsea", "score": "2-1",
        "sport": "soccer", "league": "Premier League", "title": "Arsenal 2-1 Chelsea"}


def hit(i, t, score=80.0, length=37.0, **kw):
    return {"id": f"e{i}", "t_event": t, "start": t - 12, "end": t - 12 + length, "score": score, "label": "Red card",
            "quote": "He's sent off!", "badge": f"{50 + i}' RED CARD · PLAYER {i}", "certain": True,
            "feed": {"when": f"{50 + i}'", "players": [f"Player {i}"]}} | kw


def plays(n=5):
    return [{"id": f"h{i}", "start": 100.0 + 300 * i, "end": 130.0 + 300 * i, "rank": i + 1, "score": 90 - i,
             "play": "Goal", "title": f"Goal {i}", "chosen": True} for i in range(n)]


def test_an_event_reel_runs_in_game_order_with_the_feeds_badges():
    r = montage.plan_events(GAME, [hit(1, 3000, 70), hit(2, 1000, 95), hit(3, 5000, 60)], duration=6000, title="Red card")
    assert r.kind == "events" and [s.kind for s in r.segments] == ["teaser", "play", "play", "play"]
    assert [s.hid for s in r.plays] == ["e2", "e1", "e3"] and r.segments[0].hid == "e2"  # the surest opens it
    assert r.plays[0].badge == "52' RED CARD · PLAYER 2" and r.plays[0].label == "RED CARD 1/3"
    assert all(montage.EVENT_MIN <= s.length <= montage.EVENT_MAX + 1 for s in r.plays)
    assert all(s.start <= s.moment <= s.end for s in r.plays)
    st = montage.event_story(GAME, r, narrator=True)
    assert st.hook == "EVERY RED CARDS" or st.hook == "EVERY RED CARD" or "RED CARD" in st.hook
    assert st.badges["e2"] == "52' RED CARD · PLAYER 2" and "Arsenal vs Chelsea" in st.opening and st.written_by == ""


def test_a_short_event_reel_says_its_under_a_minute_and_flags_mentions():
    r = montage.plan_events(GAME, [hit(1, 3000, certain=False)], duration=6000, title="Red card")
    assert [s.kind for s in r.segments] == ["play"] and not r.eligible
    assert "under a minute" in r.notes[0] and "only talked about" in r.notes[1]
    assert montage.event_story(GAME, r, narrator=False).opening == ""
    with pytest.raises(montage.ReelError):
        montage.plan_events(GAME, [], duration=6000)


def test_climax_keeps_the_games_order_and_ends_on_the_best():
    hs = plays()
    hs[0], hs[2] = dict(hs[2], rank=1), dict(hs[0], rank=3)  # the best play is the third in time
    r = montage.plan(GAME, hs, crowd=None, duration=2000, order="climax")
    order = [s.hid for s in r.plays]
    assert order[-1] == "h2" and order[:-1] == sorted(order[:-1]) and r.order == "climax"
    events = montage.plan_events(GAME, [hit(1, 1000, 70), hit(2, 2000, 95), hit(3, 3000, 60)], duration=6000,
                                 order="climax")
    assert [s.hid for s in events.plays] == ["e1", "e3", "e2"]


def test_transitions_follow_the_style_and_length():
    r = montage.plan(GAME, plays(), crowd=None, duration=2000, transition=0.3, style="dissolve")
    assert r.transition == 0.3 and r.style == "dissolve"
    graph = reel_module.video_graph(r, ass=False, fontsdir="")
    assert "transition=fade:duration=0.300" in graph and "wipeleft" not in graph
    assert f"transition={reel_module.OPENING_FLASH}" in graph  # the opening still flashes into the first play
    r.style = "whip"
    assert "smoothleft" in reel_module.video_graph(r, False, "") and "smoothright" in reel_module.video_graph(r, False, "")
    r.style = "zoom"
    assert reel_module.video_graph(r, False, "").count("zoomin") == len(r.segments) - 2
    default = montage.plan(GAME, plays(), crowd=None, duration=2000)
    assert default.transition == montage.TRANSITION and "wipeleft" in reel_module.video_graph(default, False, "")
    assert montage.plan(GAME, plays(), crowd=None, duration=2000, transition=5).transition == 0.6
    assert 65 <= r.duration <= 86


def test_a_lower_third_takes_the_feeds_minute_and_scorer():
    hs = plays()
    hs[0]["title"] = "Haaland game winner"
    r = montage.plan(GAME, hs, crowd=None, duration=2000)
    best = next(s for s in r.plays if s.hid == "h0")
    timeline = [{"video": best.moment + 1, "badge": "90+4' GOAL · E. HAALAND", "scoring": True, "kind": "goal"},
                {"video": best.moment - 1, "badge": "90+3' YELLOW CARD · R. JAMES", "scoring": False, "kind": "yellow_card"},
                {"video": 5.0, "badge": "1' GOAL · NOBODY", "scoring": True}]
    got = montage.feed_badges(r, timeline)
    assert got == {"h0": "90+4' WINNER · E. HAALAND"}

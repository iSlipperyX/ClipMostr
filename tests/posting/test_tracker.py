"""Creator Rewards progress (posting/tracker.py)."""

from ytclip.posting import tracker
from ytclip.posting.store import Profile, TikTokVideo

DAY = 86400.0
NOW = 1_800_000_000.0


def test_no_stats_yet_is_not_shown_as_zero_progress():
    p = tracker.progress(Profile(), {}, NOW)
    assert not p.known and not p.eligible
    assert "No stats yet" in p.notes[0]


def test_views_count_new_videos_whole_and_older_ones_from_the_window_start():
    new = TikTokVideo("a", create_time=NOW - 3 * DAY, views=40_000, duration=72)
    old = TikTokVideo("b", create_time=NOW - 90 * DAY, views=500_000, duration=30,
                      history=[[NOW - 40 * DAY, 400_000], [NOW - 31 * DAY, 450_000], [NOW - 1 * DAY, 499_000]])
    assert tracker.views_in_window(new, NOW) == (40_000, False)
    assert tracker.views_in_window(old, NOW) == (50_000, False)  # since the last point before the window
    # History that doesn't reach back far enough is scaled to the window and marked as an estimate.
    short = TikTokVideo("c", create_time=NOW - 60 * DAY, views=13_000, history=[[NOW - 10 * DAY, 10_000]])
    assert tracker.views_in_window(short, NOW) == (9_000, True)


def test_progress_reports_goals_qualifying_posts_and_estimates():
    profile = Profile(followers=6_000, fetched_at=NOW,
                      history=[[NOW - 10 * DAY, 5_000], [NOW, 6_000]])  # +100 a day
    vids = {
        "a": TikTokVideo("a", create_time=NOW - 2 * DAY, views=30_000, duration=75),
        "b": TikTokVideo("b", create_time=NOW - 5 * DAY, views=20_000, duration=45),
        "c": TikTokVideo("c", create_time=NOW - 20 * DAY, views=10_000, duration=64),
    }
    p = tracker.progress(profile, vids, NOW)
    assert p.known and not p.eligible
    assert (p.followers.value, p.followers.target) == (6_000, 10_000)
    assert round(p.followers.days_left) == 40
    assert p.views.value == 60_000 and 0.59 < p.views.fraction < 0.61
    assert p.views.days_left is not None  # 50k in the last week is well past the pace the goal needs
    assert (p.posts, p.qualifying) == (3, 2)
    text = " ".join(p.notes)
    assert "Followers: 4,000 to go, about 6 weeks" in text
    assert "1 of the last 3 posts are a minute or shorter" in text


def test_eligible_account_says_how_to_apply():
    profile = Profile(followers=12_000, fetched_at=NOW)
    vids = [TikTokVideo("a", create_time=NOW - DAY, views=150_000, duration=70)]
    p = tracker.progress(profile, vids, NOW)
    assert p.eligible and p.followers.done and p.views.done
    assert "Creator Rewards Program" in p.notes[0]
    assert "The post from the last 30 days is over a minute." == p.notes[-1]


def test_a_pace_too_slow_for_the_rolling_total_has_no_estimate():
    profile = Profile(followers=100, fetched_at=NOW)
    vids = [TikTokVideo("a", create_time=NOW - 20 * DAY, views=3_000, duration=70)]
    p = tracker.progress(profile, vids, NOW)
    assert p.views.days_left is None and p.followers.days_left is None
    assert any("needs about 3,333 views a day" in n for n in p.notes)

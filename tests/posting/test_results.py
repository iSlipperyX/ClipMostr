"""Sharing how posted clips did with ClipMonster (results.json) and the Account page's notes."""

import json

import pytest

from ytclip.posting import results
from ytclip.posting.store import Store, TikTokVideo
from posting_helpers import NOW, clipmonster_folder, mark_posted, two_accounts

DAY = 86400.0


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))


def posted_store(tmp_path, views=(5000, 300), age_days=3.0, others=(1000, 1000, 1000)):
    """Two ClipMonster clips posted by hand on @cats and linked to TikTok videos, plus older videos."""
    accs, a, _ = two_accounts()
    store = Store(tmp_path / "lib.json")
    items = sorted(store.add_paths([clipmonster_folder(tmp_path, name="Pod/Talk [abc]")]),
                   key=lambda i: i.title, reverse=True)  # good, fine
    store.enqueue([i.id for i in items], a.id)
    videos = []
    for n, (it, v) in enumerate(zip(items, views)):
        when = NOW - age_days * DAY + n * 3600
        mark_posted(store, it.id, now=when)
        videos.append(TikTokVideo(f"t{n}", it.caption or it.name, when, f"https://tiktok.com/v/t{n}", views=v))
    videos += [TikTokVideo(f"o{n}", f"older {n}", NOW - (10 + n) * DAY, views=v) for n, v in enumerate(others)]
    store.update_tiktok_videos(a.id, videos)
    return store, a, items


def test_outcomes_have_views_compared_with_the_usual(tmp_path):
    store, a, (good, fine) = posted_store(tmp_path)
    rows = results.outcomes(store, NOW)
    assert [r.clip for r in rows] == ["fine", "good"]  # newest first
    by = {r.clip: r for r in rows}
    assert by["good"].views == 5000 and by["good"].lift == 5.0  # the usual is the median of 5 videos: 1000
    assert by["fine"].lift == 0.3
    assert by["good"].hook == "He quit on air" and by["good"].channel == "Pod People"
    assert by["good"].video_id == ""  # "abc" isn't a YouTube video id
    good.source_group = "abcdefghijk"
    assert next(r for r in results.outcomes(store, NOW) if r.clip == "good").video_id == "abcdefghijk"
    assert by["good"].rating == 8 and by["good"].account == a.id


def test_young_posts_and_thin_accounts_are_left_out(tmp_path):
    store, _, _ = posted_store(tmp_path, age_days=1)
    assert results.outcomes(store, NOW) == []  # under 48 hours old
    store, _, _ = posted_store(tmp_path / "b", others=())
    assert results.outcomes(store, NOW) == []  # only 2 videos old enough: no "usual" yet


def test_unlinked_or_non_clipmonster_posts_are_left_out(tmp_path):
    store, a, (good, fine) = posted_store(tmp_path)
    fine.tiktok_id = ""
    good.source_group = good.source_channel = ""
    assert results.outcomes(store, NOW) == []


def test_share_writes_what_clipmonster_reads(tmp_path):
    store, _, _ = posted_store(tmp_path)
    path = results.results_path()
    assert results.share(store, now=NOW) == 2
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["written_at"] == NOW
    row = next(r for r in data["outcomes"] if r["clip"] == "good")
    assert {k: row[k] for k in ("channel", "lift", "hook", "rating")} == {
        "channel": "Pod People", "lift": 5.0, "hook": "He quit on air", "rating": 8}
    assert not path.with_suffix(".tmp").exists()


def test_insights_for_the_account_page(tmp_path):
    assert results.insights([]) == []
    store, a, _ = posted_store(tmp_path)
    rows = results.outcomes(store, NOW)
    assert results.insights(rows) == []  # too few
    rows = rows * 2
    lines = results.insights(rows)
    assert lines[0].startswith("4 ClipMonster clips are 2+ days old")
    assert "Best hook so far: “He quit on air” (5.0×)." in lines


def test_refreshing_stats_shares_results(tmp_path, monkeypatch):
    from ytclip.posting import jobs, tiktok
    from ytclip.posting.config import Settings
    from ytclip.posting.store import Profile

    store, a, _ = posted_store(tmp_path)
    shared = []
    monkeypatch.setattr(results, "share", lambda st, **k: shared.append(st) or 2)
    monkeypatch.setattr(tiktok, "refresh_account", lambda s, acc, log, cancelled: (Profile("cats"), []))
    logs = []
    jobs.refresh_account(store, Settings(), a, logs.append, lambda: False)
    assert shared == [store] and logs[-1].startswith("@cats refreshed")

"""Tests for the parts that don't need TikTok: scheduling, captions, store, scoring maths."""

import datetime as dt
import subprocess

import pytest

from ytclip.posting import schedule
from ytclip.posting.config import Settings
from ytclip.posting.store import INTERRUPTED, Store, TikTokVideo, VideoItem
from ytclip.posting.tiktok import compose_caption, normalize_hashtags
from ytclip.posting.tiktok.browser import _short_number


def ts(y, mo, d, h, mi=0):
    return dt.datetime(y, mo, d, h, mi).timestamp()


def item(name, score=None, group="", **kw):
    it = VideoItem(path=f"C:/x/{name}.mp4", title=name, source_group=group, status="queued", **kw)
    if score is not None:
        it.score = {"total": score, "parts": {}}
    return it


# --- schedule ----------------------------------------------------------------------


def test_daily_slots_spread_evenly():
    s = Settings(posts_per_day=3, first_post_time="09:00", last_post_time="21:00")
    assert [t.strftime("%H:%M") for t in schedule.daily_slots(s)] == ["09:00", "15:00", "21:00"]
    s.posts_per_day = 1
    assert [t.strftime("%H:%M") for t in schedule.daily_slots(s)] == ["09:00"]


def test_plan_fills_slots_in_queue_order_and_rolls_to_next_day():
    s = Settings(posts_per_day=2, first_post_time="10:00", last_post_time="18:00", min_gap_minutes=60,
                 vary_minutes=0)
    q = [item("a"), item("b"), item("c")]
    plan = schedule.plan(q, s, now=ts(2026, 10, 20, 12), last_post=None)
    assert plan[q[0].id] == ts(2026, 10, 20, 18)
    assert plan[q[1].id] == ts(2026, 10, 21, 10)
    assert plan[q[2].id] == ts(2026, 10, 21, 18)


def test_plan_respects_gap_after_last_post_and_pins():
    s = Settings(posts_per_day=4, first_post_time="08:00", last_post_time="20:00", min_gap_minutes=180,
                 vary_minutes=0)
    pinned = item("pinned", pinned_at=ts(2026, 10, 20, 16))
    q = [item("a"), pinned, item("b")]
    # Last post at 9:30 -> nothing before 12:30; slots are 8, 12, 16, 20.
    plan = schedule.plan(q, s, now=ts(2026, 10, 20, 9, 45), last_post=ts(2026, 10, 20, 9, 30))
    assert plan[pinned.id] == ts(2026, 10, 20, 16)
    assert plan[q[0].id] == ts(2026, 10, 20, 20)  # 16:00 is the pin; 12:00 is inside the gap
    assert plan[q[2].id] == ts(2026, 10, 21, 8)


def test_due_picks_first_ready_queued_item():
    a, b = item("a"), item("b")
    a.planned_at, b.planned_at = 200.0, 100.0
    b.status = "failed"
    assert schedule.due([a, b], now=150) is None
    assert schedule.due([a, b], now=250) is a


def hm(times):
    return [t.strftime("%H:%M") for t in times]


def test_slots_that_dont_fit_the_gap_are_spread_out_not_dropped():
    # 9 a day from 09:00 to 21:00 at least 2 h apart: only 7 fit, every 2 hours (it used to plan 5).
    s = Settings(posts_per_day=9, first_post_time="09:00", last_post_time="21:00", min_gap_minutes=120,
                 vary_minutes=0)
    assert schedule.fitting(s) == 7
    assert hm(schedule.daily_slots(s)) == ["09:00", "11:00", "13:00", "15:00", "17:00", "19:00", "21:00"]
    plan = schedule.plan([item(str(n)) for n in range(7)], s, now=ts(2026, 10, 20, 0), last_post=None)
    assert sorted(plan.values()) == [ts(2026, 10, 20, h) for h in (9, 11, 13, 15, 17, 19, 21)]
    assert "Only 7 of 9 fit" in schedule.describe(s, dt.date(2026, 10, 20))[0]
    s.min_gap_minutes = 0
    assert schedule.fitting(s) == 9


def test_the_new_defaults_are_two_a_day_three_hours_or_more_apart():
    s = Settings(vary_minutes=0)
    assert (s.posts_per_day, s.min_gap_minutes) == (2, 180)
    assert hm(schedule.daily_slots(s)) == ["11:00", "21:00"]
    assert schedule.describe(s, dt.date(2026, 10, 20)) == []


def test_varied_times_move_a_little_each_day_and_stay_the_same_when_replanned():
    s = Settings(posts_per_day=3, first_post_time="09:00", last_post_time="21:00", min_gap_minutes=180,
                 vary_minutes=15, schedule_seed="acc1")
    days = [dt.date(2026, 11, 1) + dt.timedelta(days=n) for n in range(30)]
    seen = set()
    for day in days:
        times = schedule.slots_on(s, day)
        assert times == schedule.slots_on(s, day)  # steady, so the 30 s replan doesn't move posts around
        assert len(times) == 3
        for when, nominal in zip(times, (9, 15, 21)):
            assert abs((when - dt.datetime.combine(day, dt.time(nominal))).total_seconds()) <= 15 * 60
        assert all((b - a).total_seconds() >= 180 * 60 for a, b in zip(times, times[1:]))
        seen.add(tuple(t.time() for t in times))
    assert len(seen) > 20  # not the same minute every day
    other = Settings(**{**s.__dict__, "schedule_seed": "acc2"})
    assert [schedule.slots_on(other, d) for d in days] != [schedule.slots_on(s, d) for d in days]
    s.vary_minutes = 0
    assert [t.time() for t in schedule.slots_on(s, days[0])] == [dt.time(9), dt.time(15), dt.time(21)]


def test_varied_times_keep_the_gap_when_slots_are_exactly_a_gap_apart():
    s = Settings(posts_per_day=7, first_post_time="09:00", last_post_time="21:00", min_gap_minutes=120,
                 vary_minutes=30, schedule_seed="x")
    for n in range(60):
        times = schedule.slots_on(s, dt.date(2026, 11, 1) + dt.timedelta(days=n))
        assert all((b - a).total_seconds() >= 120 * 60 for a, b in zip(times, times[1:]))
        assert all(t.date() == times[0].date() for t in times)


def test_a_late_window_never_spills_past_midnight():
    s = Settings(posts_per_day=2, first_post_time="23:00", last_post_time="23:59", min_gap_minutes=30,
                 vary_minutes=30, schedule_seed="x")
    for n in range(30):
        day = dt.date(2026, 11, 1) + dt.timedelta(days=n)
        assert all(t.date() == day for t in schedule.slots_on(s, day))
        assert schedule.slots_on(s, day)


def test_warm_up_posts_nothing_then_one_then_two_then_the_accounts_own_number():
    start = dt.datetime(2026, 11, 1, 19, 30).timestamp()
    s = Settings(posts_per_day=3, first_post_time="09:00", last_post_time="21:00", min_gap_minutes=180,
                 vary_minutes=0, warm_up_start=start)
    counts = [len(schedule.slots_on(s, dt.date(2026, 11, 1) + dt.timedelta(days=n))) for n in range(16)]
    assert counts == [0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 3, 3]
    assert schedule.warm_up_day(s, dt.date(2026, 11, 2)) == 2
    assert schedule.warm_up_day(s, dt.date(2026, 11, 15)) is None
    assert "Warm-up day 1 of 14: no posts yet" in schedule.describe(s, dt.date(2026, 11, 1))[0]
    assert "1 a day for now" in schedule.describe(s, dt.date(2026, 11, 5))[0]
    assert "3 a day from Sun 15 Nov" in schedule.describe(s, dt.date(2026, 11, 5))[0]
    # The queue waits through the quiet days rather than piling up on day 4.
    q = [item("a"), item("b"), item("c")]
    plan = schedule.plan(q, s, now=start, last_post=None)
    assert [dt.datetime.fromtimestamp(plan[i.id]).strftime("%d %H:%M") for i in q] == \
        ["04 09:00", "05 09:00", "06 09:00"]
    s.warm_up_start = 0.0
    assert schedule.warm_up_cap(s, dt.date(2026, 11, 1)) is None


def test_more_than_three_a_day_gets_a_warning():
    s = Settings(posts_per_day=4, first_post_time="09:00", last_post_time="21:00", min_gap_minutes=180)
    notes = schedule.describe(s, dt.date(2026, 11, 1))
    assert len(notes) == 1 and "more than the 3" in notes[0]
    s.posts_per_day = 3
    assert schedule.describe(s, dt.date(2026, 11, 1)) == []


def test_spread_keeps_same_source_apart():
    q = [item("a1", 90, "A"), item("a2", 85, "A"), item("b1", 60, "B"), item("a3", 50, "A")]
    order = [i.name for i in schedule.spread(q)]
    assert order == ["a1", "b1", "a2", "a3"]
    assert [i.name for i in schedule.by_score(q)] == ["a1", "a2", "b1", "a3"]


# --- captions ------------------------------------------------------------------------


def test_hashtags_are_normalised_and_deduplicated():
    assert normalize_hashtags("fyp, #FYP #funny! cats") == "#fyp #funny #cats"


def test_caption_composition():
    s = Settings(default_hashtags="#fyp", caption_template="{caption} {hashtags}")
    it = item("My clip", caption="Wait for it #fyp", hashtags="funny cats")
    assert compose_caption(it, s) == "Wait for it #fyp #funny #cats"
    it.caption = ""
    assert compose_caption(it, s).startswith("My clip ")
    s.caption_template = "{oops}"
    assert compose_caption(it, s) == "My clip #funny #cats #fyp"


# --- TikTok helpers ------------------------------------------------------------------


@pytest.mark.parametrize("text,value", [("845", 845), ("1.2K", 1200), ("3.4M", 3400000), ("", 0), ("1,234", 1234)])
def test_short_numbers(text, value):
    assert _short_number(text) == value


# --- store ---------------------------------------------------------------------------


def test_store_queue_order_and_persistence(tmp_path):
    vids = tmp_path / "vids"
    vids.mkdir()
    for n in ("one", "two", "three"):
        (vids / f"{n}.mp4").write_bytes(b"x")
    st = Store(tmp_path / "lib.json")
    added = st.add_paths([vids])
    assert len(added) == 3
    assert st.add_paths([vids]) == []  # no duplicates
    ids = [i.id for i in added]
    st.enqueue(ids, "acct")
    st.set_order([ids[2], ids[0], ids[1]])
    st.dequeue([ids[0]])
    assert [i.id for i in st.queue("acct")] == [ids[2], ids[1]]
    st.save()
    again = Store(tmp_path / "lib.json")
    assert [i.id for i in again.queue("acct")] == [ids[2], ids[1]]
    assert again.get(ids[0]).status == "new" and again.get(ids[0]).account == ""


def test_clipmonster_index_is_read(tmp_path):
    import json

    (tmp_path / "01 - clip.mp4").write_bytes(b"x")
    (tmp_path / "clips.json").write_text(json.dumps({
        "video": {"id": "abc123", "title": "Source"},
        "clips": [{"rank": 1, "file": "01 - clip.mp4", "score": 77.5, "title": "Great moment", "reason": "Big laugh"}],
    }), encoding="utf-8")
    st = Store(tmp_path / "lib.json")
    (it,) = st.add_paths([tmp_path])
    assert (it.title, it.source_score, it.source_group, it.source_reason) == ("Great moment", 77.5, "abc123", "Big laugh")


def test_posted_items_link_to_account_videos(tmp_path):
    st = Store(tmp_path / "lib.json")
    it = item("x", caption="This changed everything")
    it.status, it.posted_at, it.account = "posted", ts(2026, 10, 20, 12), "acct"
    st.items[it.id] = it
    st.update_tiktok_videos("acct", [
        TikTokVideo(id="1", description="Something else", create_time=ts(2026, 10, 20, 12, 1)),
        TikTokVideo(id="2", description="This changed everything #fyp", create_time=ts(2026, 10, 20, 12, 2)),
    ])
    assert it.tiktok_id == "2"


def test_crash_mid_upload_is_not_queued_again(tmp_path):
    # The upload may have gone through, so autopilot must not post it a second time.
    st = Store(tmp_path / "lib.json")
    it = item("x", queue_pos=0, account="acct")
    it.status = "uploading"
    st.items[it.id] = it
    st.save()
    later = Store(tmp_path / "lib.json")
    assert later.get(it.id).status == "uploading"  # the window just loads it: only the poster decides
    assert later.interrupted() == 1  # the posting worker starting up
    again = later.get(it.id)
    assert (again.status, again.last_error, again.queue_pos) == ("failed", INTERRUPTED, 0)
    assert schedule.due([again], now=ts(2030, 1, 1, 12)) is None


def test_interrupted_upload_found_on_tiktok_becomes_posted(tmp_path):
    st = Store(tmp_path / "lib.json")
    it = item("x", caption="This changed everything", queue_pos=0, account="acct",
               upload_started_at=ts(2026, 10, 20, 12))
    it.status, it.last_error = "failed", INTERRUPTED
    other = item("y", queue_pos=1, account="acct")
    st.items.update({it.id: it, other.id: other})
    st.update_tiktok_videos("acct", [
        TikTokVideo(id="1", description="This changed everything", create_time=ts(2026, 10, 19, 12)),  # older post
        TikTokVideo(id="2", description="This changed everything #fyp", create_time=ts(2026, 10, 20, 12, 3)),
    ])
    assert (it.status, it.tiktok_id, it.last_error, it.queue_pos) == ("posted", "2", "", None)
    assert it.posted_at == ts(2026, 10, 20, 12, 3) and other.queue_pos == 0


def test_interrupted_upload_not_on_tiktok_stays_failed(tmp_path):
    st = Store(tmp_path / "lib.json")
    it = item("x", caption="This changed everything", queue_pos=0, account="acct",
               upload_started_at=ts(2026, 10, 20, 12))
    it.status, it.last_error = "failed", INTERRUPTED
    st.items[it.id] = it
    st.update_tiktok_videos("acct", [
        TikTokVideo(id="1", description="Something else", create_time=ts(2026, 10, 20, 12, 3)),
        TikTokVideo(id="2", description="This changed everything", create_time=ts(2026, 10, 18, 12)),
    ])
    assert (it.status, it.tiktok_id) == ("failed", "")


# --- scoring end to end ------------------------------------------------------------


@pytest.fixture(scope="module")
def sample_videos(tmp_path_factory):
    from ytclip.posting.media import ffmpeg_exe

    d = tmp_path_factory.mktemp("samples")
    ff = ffmpeg_exe()
    good, bad = d / "good.mp4", d / "bad.mp4"
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=540x960:r=30:d=25",
                    "-f", "lavfi", "-i", "sine=f=330:d=25,volume=4", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(good)], check=True)
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=120",
                    "-c:v", "libx264", "-preset", "ultrafast", str(bad)], check=True)
    return good, bad


def test_vertical_with_sound_beats_silent_landscape(sample_videos):
    from ytclip.posting.media import analyze
    from ytclip.posting.scoring import score

    good, bad = sample_videos
    s = Settings()
    g, b = score(analyze(good), s), score(analyze(bad), s)
    assert g.total > b.total + 25
    assert g.parts["format"] == pytest.approx(100 * 0.7 + 30 * 0.3, abs=15)
    assert b.parts["audio"] <= 10 and b.total <= 45
    assert any("9:16" in t for t in b.tips)
    assert any("No audio" in t for t in b.tips)

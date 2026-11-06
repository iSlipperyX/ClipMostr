"""Multi-account: the account list, upgrading a single-account install, per-account queues and autopilot."""

import datetime as dt

import pytest

from ytclip.posting import jobs
from ytclip.posting.accounts import Account, Accounts
from ytclip.posting.config import Settings, app_dir, cache_dir
from ytclip.posting.store import Profile, Store, TikTokVideo, VideoItem
from ytclip.posting.tiktok import compose_caption


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))


def ts(y, mo, d, h, mi=0):
    return dt.datetime(y, mo, d, h, mi).timestamp()


def videos(tmp_path, *names):
    folder = tmp_path / "vids"
    folder.mkdir(exist_ok=True)
    for n in names:
        (folder / f"{n}.mp4").write_bytes(b"x")
    return folder


def two_accounts() -> tuple[Accounts, Account, Account]:
    accs = Accounts.load()
    a = accs.add("")
    b = accs.add("Second", copy_schedule_from=a)
    accs.save()
    return accs, a, b


# --- the account list ----------------------------------------------------------------------


def test_a_fresh_install_has_no_accounts_until_one_is_added():
    accs = Accounts.load()
    assert len(accs) == 0
    a = accs.add("", Settings(posts_per_day=3))
    assert not a.logged_in and a.autopilot and a.posts_per_day == 3 and a.warm_up_start
    assert a.browser_dir() == cache_dir() / "browser-profiles" / a.id


def test_accounts_round_trip():
    accs, a, b = two_accounts()
    b.username, b.niche = "second_one", "gaming"
    accs.save()
    again = Accounts.load()
    assert again.ids() == [a.id, b.id]
    assert again.get(b.id).username == "second_one" and again.get(b.id).niche == "gaming"
    assert again.find_username("@SECOND_ONE").id == b.id
    assert again.find_username("second_one", exclude=b.id) is None
    assert (app_dir() / "accounts.json").is_file()


def test_new_account_copies_schedule_and_posts_by_itself():
    accs = Accounts.load()
    a = accs.add("")
    a.posts_per_day, a.first_post_time, a.default_hashtags = 6, "07:00", "#cats"
    b = accs.add("", copy_schedule_from=a)
    assert (b.posts_per_day, b.first_post_time, b.default_hashtags) == (6, "07:00", "#cats")
    assert b.autopilot and b.label == "New account"
    assert b.browser_profile != a.browser_profile


def test_removing_an_account_deletes_its_login():
    accs, a, b = two_accounts()
    (b.browser_dir() / "Default").mkdir()
    removed = accs.remove(b.id)
    assert removed is b and accs.ids() == [a.id]
    Accounts.delete_login(removed)
    assert not (cache_dir() / b.browser_profile).exists()
    assert accs.remove("nope") is None
    assert accs.remove(a.id) is a and len(accs) == 0


def test_changes_from_the_other_process_merge_field_by_field(tmp_path):
    accs, a, b = two_accounts()
    other = Accounts.load()  # the background worker's copy
    other.get(a.id).logged_in = True  # it logged a in
    other.get(a.id).upload_check = "ok"
    other.save()
    a.posts_per_day = 4  # meanwhile you changed a's schedule in the window
    b.name = "Gaming"
    accs.save()
    final = Accounts.load()
    fa = final.get(a.id)
    assert fa.logged_in and fa.upload_check == "ok" and fa.posts_per_day == 4
    assert final.get(b.id).name == "Gaming"
    assert accs.get(a.id) is a and a.logged_in  # the window's object took the worker's change in place
    # refresh() takes in a save without saving anything itself
    other.refresh()
    other.get(b.id).username = "dogs"
    other.save()
    assert accs.refresh() and b.username == "dogs" and b.name == "Gaming"
    assert not accs.refresh()


def test_an_account_removed_in_the_window_stays_removed():
    accs, a, b = two_accounts()
    other = Accounts.load()
    accs.remove(b.id)
    accs.save()
    other.get(a.id).username = "cats"
    other.save()  # the worker saved after the removal, without having seen it
    assert Accounts.load().ids() == [a.id] and Accounts.load().get(a.id).username == "cats"
    assert other.ids() == [a.id]


def test_apply_lays_account_values_over_app_settings():
    s = Settings(posts_per_day=3, default_hashtags="#fyp", caption_template="{caption} | {hashtags}")
    a = Account(posts_per_day=7, default_hashtags="#gaming", default_privacy="private", niche="speedruns",
                autopilot=True, logged_in=True)
    t = a.apply(s)
    assert (t.posts_per_day, t.default_hashtags, t.default_privacy, t.ai_niche) == (7, "#gaming", "private", "speedruns")
    assert t.autopilot and t.browser_logged_in
    assert t.caption_template == "{caption} | {hashtags}"  # app-wide settings are kept
    assert s.posts_per_day == 3  # the original isn't changed
    it = VideoItem(path="C:/x/a.mp4", title="a", caption="Watch this")
    assert compose_caption(it, t) == "Watch this | #gaming"


# --- per-account queues ----------------------------------------------------------------------


def test_each_account_has_its_own_queue_order(tmp_path):
    st = Store(tmp_path / "lib.json")
    items = st.add_paths([videos(tmp_path, "a1", "a2", "b1", "b2")])
    a1, a2, b1, b2 = (i.id for i in items)
    st.enqueue([a1, a2], "A")
    st.enqueue([b1, b2], "B")
    st.enqueue([a1], "B")  # already queued for A: stays there
    assert [i.id for i in st.queue("A")] == [a1, a2]
    assert [i.id for i in st.queue("B")] == [b1, b2]
    assert [i.queue_pos for i in st.queue("B")] == [0, 1]
    st.set_order([b2, b1])
    st.dequeue([a1])
    assert [i.id for i in st.queue("A")] == [a2] and st.queue("A")[0].queue_pos == 0
    assert [i.id for i in st.queue("B")] == [b2, b1]
    assert len(st.queue()) == 3
    st.save()
    again = Store(tmp_path / "lib.json")
    assert [i.id for i in again.queue("B")] == [b2, b1]
    assert again.get(a1).account == "" and again.get(a1).status == "new"


def test_move_between_queues_and_copy_a_posted_video(tmp_path):
    st = Store(tmp_path / "lib.json")
    x, y = st.add_paths([videos(tmp_path, "x", "y")])
    st.enqueue([x.id, y.id], "A")
    x.pinned_at, x.status, x.last_error = 123.0, "failed", "boom"
    st.move_to([x.id], "B")
    assert [i.id for i in st.queue("B")] == [x.id]
    assert x.status == "queued" and x.pinned_at is None and x.last_error == ""
    assert [i.id for i in st.queue("A")] == [y.id] and y.queue_pos == 0

    y.status, y.posted_at, y.queue_pos, y.tiktok_id = "posted", 1000.0, None, "555"
    y.score, y.caption = {"total": 80, "parts": {"hook": 90}}, "Nice"
    copy = st.copy_for(y.id, "B")
    assert copy.id != y.id and copy.path == y.path and copy.account == "B" and copy.status == "queued"
    assert copy.caption == "Nice" and copy.total == 80 and copy.tiktok_id == "" and copy.posted_at is None
    copy.score["parts"]["hook"] = 10
    assert y.score["parts"]["hook"] == 90  # its own copy of the score
    assert y.status == "posted" and y.account == "A"
    assert [i.id for i in st.queue("B")] == [x.id, copy.id]
    assert st.add_paths([videos(tmp_path, "y")]) == []  # still known, no third entry


def test_forget_account_returns_its_queue_to_the_library(tmp_path):
    st = Store(tmp_path / "lib.json")
    q, p, other = st.add_paths([videos(tmp_path, "q", "p", "o")])
    st.enqueue([q.id], "B")
    st.enqueue([other.id], "A")
    p.status, p.account, p.posted_at = "posted", "B", 5.0
    st.update_profile("B", Profile(username="bee", followers=3))
    st.forget_account("B")
    assert q.status == "new" and q.account == "" and q.queue_pos is None
    assert p.status == "posted" and p.account == "B"  # history keeps where it went
    assert "B" not in st.stats
    assert [i.id for i in st.queue("A")] == [other.id]


def test_adopt_unqueues_videos_of_unknown_accounts(tmp_path):
    st = Store(tmp_path / "lib.json")
    gone, kept = st.add_paths([videos(tmp_path, "gone", "kept")])
    st.enqueue([gone.id], "deleted")
    st.enqueue([kept.id], "A")
    assert st.adopt(["A"])
    assert gone.status == "new" and gone.account == ""
    assert [i.id for i in st.queue("A")] == [kept.id]
    assert not st.adopt(["A"])


def test_stats_and_post_links_stay_with_their_account(tmp_path):
    st = Store(tmp_path / "lib.json")
    mine = VideoItem(path="C:/x/m.mp4", caption="Same words", status="posted", account="A", posted_at=ts(2026, 10, 20, 12))
    theirs = VideoItem(path="C:/x/t.mp4", caption="Same words", status="posted", account="B",
                       posted_at=ts(2026, 10, 20, 12))
    st.items = {mine.id: mine, theirs.id: theirs}
    st.update_tiktok_videos("B", [TikTokVideo(id="9", description="Same words", create_time=ts(2026, 10, 20, 12, 1))])
    assert theirs.tiktok_id == "9" and mine.tiktok_id == ""
    assert st.video_for(theirs).id == "9" and st.video_for(mine) is None
    st.update_profile("A", Profile(username="aaa", followers=10))
    st.update_profile("A", Profile(username="aaa", followers=12))
    assert st.profile("A").followers == 12 and len(st.profile("A").history) == 2
    assert st.profile("B").followers == 0
    st.save()
    again = Store(tmp_path / "lib.json")
    assert again.profile("A").followers == 12 and "9" in again.videos("B")


# --- schedules and autopilot across accounts ----------------------------------------------------


def make_accounts(**overrides) -> tuple[Account, Account]:
    a = Account(id="A", username="aaa", posts_per_day=2, first_post_time="10:00", last_post_time="18:00",
                min_gap_minutes=60, vary_minutes=0, autopilot=True, logged_in=True)
    b = Account(id="B", username="bbb", posts_per_day=1, first_post_time="12:00", last_post_time="12:00",
                min_gap_minutes=60, vary_minutes=0, autopilot=True, logged_in=True)
    for k, v in overrides.items():
        acc, field = k.split("_", 1)
        setattr(a if acc == "a" else b, field, v)
    return a, b


def queued_store(tmp_path) -> tuple[Store, list[VideoItem], list[VideoItem]]:
    st = Store(tmp_path / "lib.json")
    items = st.add_paths([videos(tmp_path, "a1", "a2", "b1", "b2")])
    st.enqueue([items[0].id, items[1].id], "A")
    st.enqueue([items[2].id, items[3].id], "B")
    return st, items[:2], items[2:]


def test_replan_uses_each_accounts_own_schedule_and_last_post(tmp_path):
    st, (a1, a2), (b1, b2) = queued_store(tmp_path)
    a, b = make_accounts()
    # B posted at 10:30 today: an hour's gap still leaves its 12:00 slot.
    old = VideoItem(path="C:/x/old.mp4", status="posted", account="B", posted_at=ts(2026, 10, 20, 10, 30))
    st.items[old.id] = old
    jobs.replan(st, [a, b], Settings(), now=ts(2026, 10, 20, 9))
    assert (a1.planned_at, a2.planned_at) == (ts(2026, 10, 20, 10), ts(2026, 10, 20, 18))
    assert (b1.planned_at, b2.planned_at) == (ts(2026, 10, 20, 12), ts(2026, 10, 21, 12))
    old.posted_at = ts(2026, 10, 20, 11, 30)  # now 12:00 is inside B's gap: B waits until tomorrow
    jobs.replan(st, [a, b], Settings(), now=ts(2026, 10, 20, 9))
    assert b1.planned_at == ts(2026, 10, 21, 12)
    assert a1.planned_at == ts(2026, 10, 20, 10)  # B's posts don't hold A back


def test_autopilot_picks_the_most_overdue_video_among_accounts_with_autopilot(tmp_path):
    st, (a1, _), (b1, _) = queued_store(tmp_path)
    a, b = make_accounts()
    s = Settings()
    noon = ts(2026, 10, 20, 12, 5)
    jobs.replan(st, [a, b], s, now=ts(2026, 10, 20, 9))
    # Both are due at 12:05: A's 10:00 slot is older than B's 12:00.
    for it in (a1, b1):
        it.pinned_at = it.planned_at
    acc, item = jobs.autopilot_pick(st, [a, b], s, now=noon)
    assert (acc.id, item.id) == ("A", a1.id)
    a.autopilot = False
    acc, item = jobs.autopilot_pick(st, [a, b], s, now=noon)
    assert (acc.id, item.id) == ("B", b1.id)
    b.logged_in = False  # can't post in the browser without a login
    assert jobs.autopilot_pick(st, [a, b], s, now=noon) is None
    b.logged_in = True
    b.autopilot = False
    assert jobs.autopilot_pick(st, [a, b], s, now=noon) is None


def test_autopilot_skips_and_fails_a_due_video_whose_file_is_missing(tmp_path):
    st, (a1, a2), _ = queued_store(tmp_path)
    a, b = make_accounts(b_autopilot=False)
    a1.pinned_at = ts(2026, 10, 20, 9)
    a2.pinned_at = ts(2026, 10, 20, 9, 30)
    (tmp_path / "vids" / "a1.mp4").unlink()
    said = []
    acc, item = jobs.autopilot_pick(st, [a, b], Settings(), now=ts(2026, 10, 20, 10), log=said.append)
    assert item.id == a2.id
    assert a1.status == "failed" and "missing" in a1.last_error
    assert said and "a1" in said[0]


def test_post_item_refuses_a_video_queued_for_another_account(tmp_path):
    st, (a1, _), _ = queued_store(tmp_path)
    _, b = make_accounts()
    with pytest.raises(RuntimeError, match="different account"):
        jobs.post_item(st, a1.id, Settings(), b, print, lambda: False)
    assert a1.status == "queued" and a1.attempts == 0


def test_next_post_per_account(tmp_path):
    st, (a1, _), (b1, _) = queued_store(tmp_path)
    a, b = make_accounts()
    jobs.replan(st, [a, b], Settings(), now=ts(2026, 10, 20, 9))
    assert jobs.next_post(st, "A") is a1 and jobs.next_post(st, "B") is b1
    assert jobs.next_post(st) is a1


def test_each_account_varies_its_own_times_and_old_account_lists_get_the_new_fields(tmp_path):
    from ytclip.posting import safefile

    path = tmp_path / "accounts.json"
    # A list saved before vary_minutes / warm_up_start existed keeps its own schedule.
    safefile.save(path, {"version": 1, "active": "old", "accounts": [
        {"id": "old", "username": "iclip", "posts_per_day": 9, "first_post_time": "09:00",
         "last_post_time": "21:00", "min_gap_minutes": 120}]})
    accs = Accounts.load(path)
    a = accs.get("old")
    assert (a.posts_per_day, a.min_gap_minutes, a.vary_minutes, a.warm_up_start) == (9, 120, 15, 0.0)
    assert a.apply(Settings()).schedule_seed == "old"
    b = accs.add("New", copy_schedule_from=a)
    assert b.apply(Settings()).schedule_seed == b.id != "old"
    assert b.vary_minutes == 15 and b.warm_up_start > 0  # a new account always starts its own warm-up


def test_warm_up_counts_from_when_a_recent_account_was_added():
    a = Account(created_at=1_000_000.0)
    a.start_warm_up(now=1_000_000.0 + 3 * 86400)
    assert a.warm_up_start == 1_000_000.0
    a.start_warm_up(now=1_000_000.0 + 30 * 86400)  # added long ago: starts today
    assert a.warm_up_start == 1_000_000.0 + 30 * 86400

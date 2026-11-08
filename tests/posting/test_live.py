"""The live view: reading an account's TikTok profile in its browser and turning the changes into a feed."""

import json
import time

import pytest

from fake_tiktok_browser import FakeTikTok
from ytclip.posting import live, results
from ytclip.posting.accounts import Accounts
from ytclip.posting.config import Settings
from ytclip.posting.store import Profile, Store, TikTokVideo, VideoItem, _add_point
from ytclip.posting.tiktok.browser import BrowserError

HOUR = 3600


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))


@pytest.fixture
def fake(monkeypatch):
    return FakeTikTok("cats").install(monkeypatch)


def settings(**kw) -> Settings:
    return Settings(**kw)


def connected_account(username="cats", **kw):
    """The first account, logged in to TikTok (in its browser profile) as @username."""
    accs = Accounts.load()
    a = accs.add("")
    a.username, a.logged_in = username, True
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def reading(at, followers=10, videos=(), full=False):
    vids = [TikTokVideo(id=v[0], description=v[1], create_time=v[2], views=v[3], likes=v[4] if len(v) > 4 else 0,
                        comments=v[5] if len(v) > 5 else 0, url=f"https://t/{v[0]}") for v in videos]
    return live.Reading(at=at, profile=Profile(username="cats", followers=followers, fetched_at=at), videos=vids,
                        full=full)


# --- reading the profile page ---------------------------------------------------------------


def test_read_gets_the_profile_and_newest_videos_hidden_then_all_of_them(fake):
    a = connected_account()
    now = time.time()
    for n in range(45):
        fake.add_video(f"v{n}", f"clip {n}", views=n * 10, create_time=now - n * HOUR)
    r = live.read(settings(), a)
    assert r.profile.username == "cats" and r.profile.followers == 10
    assert len(r.videos) == live.RECENT_VIDEOS and r.videos[0].id == "v0" and not r.full
    assert fake.opened[0]["headless"] and fake.opened[0]["profile_dir"] == a.browser_dir()  # its own login, hidden
    full = live.read(settings(), a, full=True)
    assert len(full.videos) == 45 and full.full and full.videos[-1].views == 440


def test_the_window_shows_while_reading_only_when_asked(fake):
    a = connected_account()
    fake.add_video("v1", "a cat")
    live.read(settings(live_show_browser=True), a)
    assert not fake.opened[0]["headless"]


@pytest.mark.parametrize("error,code", [
    (BrowserError("This account's TikTok browser window is already open. Close it and try again."), "busy"),
    (BrowserError("Couldn't find msedge. Pick another browser in Settings."), "no_browser"),
    (BrowserError("TikTok didn't return a profile for @cats."), "no_profile"),
    (BrowserError("TikTok is asking for a captcha. Turn on 'Show the browser' and try again."), "captcha"),
    (type("TimeoutError", (Exception,), {})("Timeout 20000ms exceeded"), "page"),
])
def test_what_went_wrong_in_the_browser_is_said_plainly(fake, error, code):
    a = connected_account()
    fake.fail_with = error
    with pytest.raises(live.LiveError) as e:
        live.read(settings(), a)
    assert e.value.code == code and str(e.value)
    if code == "page":
        assert "didn't load in time" in str(e.value)
    if code == "captcha":
        assert "Settings → Posting" in str(e.value)


def test_a_profile_without_its_videos_is_a_failed_reading(fake):
    a = connected_account()
    fake.add_video("v1", "a cat")
    fake.withhold_videos = True
    with pytest.raises(live.LiveError) as e:
        live.read(settings(), a)
    assert e.value.code == "no_videos" and "captcha" in str(e.value)


def test_an_account_with_no_login_and_no_username_isnt_read(fake):
    a = connected_account(username="", logged_in=False)
    with pytest.raises(live.LiveError) as e:
        live.read(settings(), a)
    assert e.value.code == "no_user" and e.value.code in live.STOP_CODES and not fake.opened


def test_a_busy_browser_is_tried_again_soon_not_backed_off(tmp_path):
    w = live.Watcher("a", tmp_path)
    s = settings(live_interval=600)
    for _ in range(3):
        w.failed(s, "The account's browser window is open…", "busy", 1000.0)
    assert w.next_at == 1000.0 + live.MIN_INTERVAL and not w.stopped


# --- turning readings into events -------------------------------------------------------


def test_first_reading_says_its_watching_then_changes_become_events(tmp_path):
    a = connected_account()
    st = Store(tmp_path / "lib.json")
    w = live.Watcher(a.id)
    s = settings()
    t0 = time.time() - 600
    ev = w.apply(st, a, s, reading(t0, 10, [("v1", "cat jumps", t0 - 5 * HOUR, 100, 5)]))
    assert [e.kind for e in ev] == ["start"] and "Watching @cats live" in ev[0].text
    ev = w.apply(st, a, s, reading(t0 + 60, 12, [("v1", "cat jumps", t0 - 5 * HOUR, 220, 13, 1)]))
    kinds = [e.kind for e in ev]
    assert kinds == ["moved", "followers"]
    assert ev[0].text == "+120 views, +8 likes, +1 comment on “cat jumps”"
    assert ev[1].text == "+2 followers (12 now)"
    assert st.videos(a.id)["v1"].views == 220 and st.profile(a.id).followers == 12
    totals = live.today(w.log.events, now=t0 + 61)
    assert totals["views"] == 120 and totals["followers"] == 2


def test_quiet_readings_add_nothing_and_many_videos_are_summed_up(tmp_path):
    a = connected_account()
    st = Store(tmp_path / "lib.json")
    w = live.Watcher(a.id)
    t0 = time.time() - 900
    vids = [(f"v{n}", f"clip number {n}", t0 - n * HOUR, 100) for n in range(10)]
    w.apply(st, a, settings(), reading(t0, 10, vids))
    assert w.apply(st, a, settings(), reading(t0 + 60, 10, vids)) == []
    moved = [(v[0], v[1], v[2], 100 + 10 * (n + 1)) for n, v in enumerate(vids)]
    ev = w.apply(st, a, settings(), reading(t0 + 120, 10, moved))
    assert [e.kind for e in ev].count("moved") == live.FEED_VIDEOS
    assert ev[0].video_id == "v9"  # the biggest gain first
    more = ev[-1]
    assert more.kind == "more" and more.text == "+100 views across 4 more videos"
    assert live.today(w.log.events, now=t0 + 121)["views"] == sum(10 * (n + 1) for n in range(10))


def test_milestones_alert_once_when_crossed(tmp_path):
    a = connected_account()
    st = Store(tmp_path / "lib.json")
    w = live.Watcher(a.id)
    t0 = time.time() - 900
    w.apply(st, a, settings(), reading(t0, 95, [("v1", "dog", t0 - HOUR, 900)]))
    ev = w.apply(st, a, settings(), reading(t0 + 60, 101, [("v1", "dog", t0 - HOUR, 1200)]))
    alerts = [e.text for e in ev if e.alert]
    assert "🏆 “dog” passed 1K views." in alerts and "🏆 @cats reached 100 followers." in alerts
    ev = w.apply(st, a, settings(), reading(t0 + 120, 102, [("v1", "dog", t0 - HOUR, 1300)]))
    assert not [e for e in ev if e.alert]


def test_a_video_taking_off_is_called_out(tmp_path):
    a = connected_account()
    st = Store(tmp_path / "lib.json")
    w = live.Watcher(a.id)
    t0 = time.time() - 3 * HOUR
    views = 1000
    for n in range(0, 120):  # two hours at a steady 10 views a minute
        w.apply(st, a, settings(), reading(t0 + n * 60, 10, [("v1", "the clip", t0 - HOUR, views)]))
        views += 10
    hot = []
    for n in range(120, 131):  # then 100 a minute
        views += 100
        hot += [e for e in w.apply(st, a, settings(), reading(t0 + n * 60, 10, [("v1", "the clip", t0 - HOUR, views)]))
                if e.kind == "hot"]
    assert len(hot) == 1 and hot[0].alert and "“the clip” is taking off" in hot[0].text


def test_after_a_gap_the_change_is_one_line_and_yesterdays_part_isnt_counted_today(tmp_path):
    a = connected_account()
    st = Store(tmp_path / "lib.json")
    w = live.Watcher(a.id)
    now = time.time()
    yesterday = live.day_start(now) - 2 * HOUR
    w.apply(st, a, settings(), reading(yesterday, 10, [("v1", "a", yesterday - HOUR, 100), ("v2", "b", yesterday - HOUR, 50)]))
    ev = w.apply(st, a, settings(), reading(now, 14, [("v1", "a", yesterday - HOUR, 400), ("v2", "b", yesterday - HOUR, 70)]))
    assert [e.kind for e in ev] == ["away", "followers"]
    assert ev[0].text.startswith("Since ") and "+320 views across 2 videos" in ev[0].text
    assert live.today(w.log.events, now=now)["views"] == 0


def test_a_new_run_compares_with_the_stats_already_saved(tmp_path):
    a = connected_account()
    st = Store(tmp_path / "lib.json")
    t0 = time.time() - 300
    st.update_profile(a.id, Profile(username="cats", followers=10, fetched_at=t0), now=t0)
    st.update_tiktok_videos(a.id, [TikTokVideo(id="v1", description="a", create_time=t0 - HOUR, views=100)], now=t0)
    w = live.Watcher(a.id)  # the app was restarted
    ev = w.apply(st, a, settings(), reading(t0 + 120, 11, [("v1", "a", t0 - HOUR, 130)]))
    assert [e.kind for e in ev] == ["moved", "followers"] and ev[0].views == 30


def test_older_videos_first_seen_in_a_full_reading_arent_new(tmp_path):
    a = connected_account()
    st = Store(tmp_path / "lib.json")
    w = live.Watcher(a.id)
    t0 = time.time() - 600
    w.apply(st, a, settings(), reading(t0, 10, [("v1", "a", t0 - HOUR, 100)]))
    ev = w.apply(st, a, settings(), reading(t0 + 60, 10, [("v1", "a", t0 - HOUR, 100), ("old", "b", t0 - 90 * 86400, 5)],
                                             full=True))
    assert ev == []
    ev = w.apply(st, a, settings(), reading(t0 + 120, 10, [("fresh", "c", t0 + 100, 3), ("v1", "a", t0 - HOUR, 100)]))
    assert [e.kind for e in ev] == ["new"] and "New video on @cats: “c”" in ev[0].text


# --- helpers ---------------------------------------------------------------------


def queued(st, a, caption, added_at, hashtags=""):
    it = VideoItem(path="C:/clips/x.mp4", title="x", caption=caption, hashtags=hashtags, added_at=added_at,
                   hook=caption)
    st.items[it.id] = it
    st.enqueue([it.id], a.id)
    return it


# --- errors, backoff and the feed on disk -----------------------------------------------


def test_errors_back_off_and_a_lost_login_stops_until_resumed(tmp_path):
    a = connected_account()
    w = live.Watcher(a.id)
    s = settings(live_interval=120)
    now = 1_000_000.0
    ev = w.failed(s, "TikTok's page didn't load in time.", "page", now)
    assert w.next_at == now + 240 and len(ev) == 1 and "Trying again in 4 min" in ev[0].text
    assert w.failed(s, "again", "", now) == []  # not every failure fills the feed
    assert w.next_at == now + 480
    for _ in range(10):
        w.failed(s, "again", "", now)
    assert w.next_at == now + live.MAX_BACKOFF
    ev = w.failed(s, "Cats isn't logged in.", "no_user", now)
    assert w.stopped and ev[0].alert and ev[0].text.startswith("Stopped watching")
    w.resume()
    assert not w.stopped and w.failures == 0 and w.next_at == 0


def test_the_feed_is_kept_on_disk_and_survives_a_torn_line(tmp_path):
    a = connected_account()
    st = Store(tmp_path / "lib.json")
    w = live.Watcher(a.id)
    t0 = time.time() - 600
    w.apply(st, a, settings(), reading(t0, 10, [("v1", "a", t0 - HOUR, 1)]))
    w.apply(st, a, settings(), reading(t0 + 60, 11, [("v1", "a", t0 - HOUR, 5)]))
    path = w.log.path
    with path.open("a", encoding="utf-8") as f:
        f.write('{"at": 1, "kind": "mov')  # the app was killed mid-write
    again = live.EventLog(a.id)
    assert [e.kind for e in again.events] == ["start", "moved", "followers"]
    lines = [json.dumps({"at": n, "kind": "moved", "text": str(n)}) for n in range(int(live.MAX_EVENTS * 1.6))]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    trimmed = live.EventLog(a.id)
    assert len(trimmed.events) == live.MAX_EVENTS
    assert len(path.read_text(encoding="utf-8").splitlines()) == live.MAX_EVENTS


def test_live_readings_keep_history_points_half_an_hour_apart():
    h = []
    for n in range(10):  # a reading a minute for 10 minutes: still two points
        h = _add_point(h, [n * 60.0, n, 0, 0, 0], 1800)
    assert [p[0] for p in h] == [0.0, 540.0]
    h = _add_point(h, [1900.0, 99, 0, 0, 0], 1800)
    assert [p[0] for p in h] == [0.0, 1900.0]
    h = _add_point(h, [1960.0, 100, 0, 0, 0], 1800)
    assert [p[0] for p in h] == [0.0, 1900.0, 1960.0]
    assert _add_point([[0, 1]], [5, 2], 0) == [[0, 1], [5, 2]]  # a manual refresh always adds one


def test_a_reading_without_saves_or_bio_keeps_the_ones_known(tmp_path):
    st = Store(tmp_path / "lib.json")
    st.update_tiktok_videos("A", [TikTokVideo(id="v1", views=10, saves=7)])
    st.update_profile("A", Profile(username="cats", bio="my bio", followers=1))
    st.update_tiktok_videos("A", [TikTokVideo(id="v1", views=20)])  # a grid-only reading has no saves
    st.update_profile("A", Profile(username="cats", followers=2))  # nor the bio
    assert st.videos("A")["v1"].saves == 7 and st.profile("A").bio == "my bio"


# --- the front door: stats come from the account's own browser ---------------------------


def test_stats_come_from_the_accounts_own_browser(fake):
    from ytclip.posting import tiktok

    a = connected_account()
    fake.add_video("v1", "hello", views=42)
    profile, vids = tiktok.refresh_account(settings(), a, print, lambda: False)
    assert profile.username == "cats" and [v.views for v in vids] == [42]
    assert fake.opened[0]["profile_dir"] == a.browser_dir()


def test_results_for_clipmonster_carry_the_category(tmp_path):
    st = Store(tmp_path / "lib.json")
    now = time.time()
    for n in range(4):
        v = TikTokVideo(id=f"t{n}", create_time=now - 5 * 86400, views=100 * (n + 1))
        st.update_tiktok_videos("A", [v])
    it = VideoItem(path="C:/x.mp4", status="posted", account="A", posted_at=now - 5 * 86400, tiktok_id="t3",
                   source_channel="Pod", category_id="n-gaming")
    st.items[it.id] = it
    rows = results.outcomes(st, now)
    assert rows and rows[0].category_id == "n-gaming"


# --- found in review ------------------------------------------------------------------------


def _watch(tmp_path):
    a = connected_account()
    return a, Store(tmp_path / "lib.json"), live.Watcher(a.id, tmp_path / "live")


def test_today_counts_the_whole_day_even_past_the_feed_limit(tmp_path):
    a, st, w = _watch(tmp_path)
    s = settings(live_interval=30)
    t = live.day_start(time.time()) + 300  # today, so a restart keeps them too
    w.apply(st, a, s, reading(t, videos=[(f"v{i}", f"clip number {i}", t - 86400, 0) for i in range(10)]))
    for k in range(1, 400):  # 400 readings 30 s apart, each 10 videos moving: ~4,000 events
        w.apply(st, a, s, reading(t + 30 * k, videos=[(f"v{i}", f"clip number {i}", t - 86400, 10 * k)
                                                      for i in range(10)]))
    assert len(w.log.events) > live.MAX_EVENTS
    assert live.today(w.log.events, t + 30 * 399)["views"] == 10 * 10 * 399
    again = live.EventLog(a.id, tmp_path / "live")  # after a restart too
    assert live.today(again.events, t + 30 * 399)["views"] == 10 * 10 * 399


def test_older_events_are_trimmed_while_the_app_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "MAX_EVENTS", 10)
    log = live.EventLog("x", tmp_path)
    old = time.time() - 3 * 86400
    for n in range(40):
        log.add([live.Event(at=old + n, kind="moved", text=f"e{n}")])
    lines = (tmp_path / "x.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log.events) == 10 and log.events[-1].text == "e39"
    assert len(lines) <= 10 + live.MAX_EVENTS // 2 + 1


def test_a_torn_last_line_doesnt_swallow_the_next_event(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text('{"at": 1, "kind": "moved", "text": "ok"}\n{"at": 2, "kind": "mo', encoding="utf-8")
    log = live.EventLog("x", tmp_path)
    log.add([live.Event(at=time.time(), kind="seen", text="marked as posted")])
    assert [e.text for e in live.EventLog("x", tmp_path).events] == ["ok", "marked as posted"]


@pytest.mark.parametrize("interval", [120, 300, 600, 1800])
def test_a_reading_on_time_is_never_summed_up_as_while_you_were_away(tmp_path, interval):
    a, st, w = _watch(tmp_path)
    s = settings(live_interval=interval)
    t = 1_800_000_000.0
    w.apply(st, a, s, reading(t, videos=[("v1", "hello there", t - 86400, 100)]))
    events = w.apply(st, a, s, reading(t + interval + 8, videos=[("v1", "hello there", t - 86400, 150)]))
    assert [e.kind for e in events] == ["moved"]


def test_a_milestone_is_announced_once_even_when_the_count_wobbles(tmp_path):
    a, st, w = _watch(tmp_path)
    s = settings()
    t = 1_800_000_000.0
    said = []
    for k, (f, views) in enumerate([(998, 9990), (1000, 10_001), (999, 9_995), (1000, 10_002), (1001, 10_010)]):
        said += [e.text for e in w.apply(st, a, s, reading(t + 60 * k, followers=f,
                                                           videos=[("v1", "a cat", t - 86400, views)]))
                 if e.kind == "milestone"]
    assert sorted(said) == ["🏆 @cats reached 1K followers.", "🏆 “a cat” passed 10K views."]


def test_a_reading_without_numbers_keeps_the_ones_known(tmp_path):
    a, st, w = _watch(tmp_path)
    s = settings()
    t = 1_800_000_000.0
    st.update_profile(a.id, Profile(username="cats", followers=12_000, likes=5, fetched_at=t - 3600), now=t - 3600)
    events = w.apply(st, a, s, live.Reading(at=t, profile=Profile(username="cats", fetched_at=t, has_stats=False),
                                            videos=[]))
    assert not [e for e in events if e.kind in ("followers", "milestone")]
    p = st.profile(a.id)
    assert (p.followers, p.likes, p.has_stats) == (12_000, 5, True)
    assert len(p.history) == 1  # no made-up point at 0


def test_readings_of_a_different_tiktok_account_are_refused(tmp_path):
    a, st, w = _watch(tmp_path)
    t = 1_800_000_000.0
    r = live.Reading(at=t, profile=Profile(username="dogs", followers=99_999, fetched_at=t), videos=[])
    events = w.apply(st, a, settings(), r)
    assert w.stopped and "@dogs" in w.stopped and events[0].kind == "error"
    assert st.profile(a.id).followers == 0 and a.username == "cats"


def test_without_stats_or_videos_the_start_line_comes_once(tmp_path):
    a, st, w = _watch(tmp_path)
    t = 1_800_000_000.0
    kinds = []
    for k in range(4):
        r = live.Reading(at=t + 60 * k, profile=Profile(username="cats", fetched_at=t + 60 * k, has_stats=False),
                         videos=[])
        kinds += [e.kind for e in w.apply(st, a, settings(), r)]
    assert kinds == ["start"]


def test_a_failing_trim_still_saves_new_events(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "MAX_EVENTS", 10)
    log = live.EventLog("x", tmp_path)
    monkeypatch.setattr(log, "_rewrite", lambda: False)  # a locked temp file, say
    old = time.time() - 3 * 86400
    for n in range(30):
        log.add([live.Event(at=old + n, kind="moved", text=f"e{n}")])
    assert live.EventLog("x", tmp_path).events[-1].text == "e29"


def test_views_lately_is_never_just_a_minute_of_change(tmp_path):
    st = Store(tmp_path / "lib.json")
    t = 1_800_000_000.0
    for i in range(32):
        st.update_tiktok_videos("A", [TikTokVideo(id="v", views=i * 100)], now=t + i * 60, spacing=1800)
    v = st.videos("A")["v"]
    assert len(v.history) == 3 and v.views_change() == 3100  # since the point half an hour back, not +100


# --- found in the 1.5.1 review --------------------------------------------------------------


def test_a_grid_only_reading_doesnt_count_as_every_number_dropping(fake, tmp_path):
    a, st, w = _watch(tmp_path)
    s = settings()
    fake.add_video("v1", "a cat", views=1234, likes=80, comments=5)
    w.apply(st, a, s, live.read(s, a))
    fake.grid_only = True
    with pytest.raises(live.LiveError) as e:
        live.read(s, a)
    assert e.value.code == "no_videos"
    v = st.videos(a.id)["v1"]
    assert (v.views, v.likes, v.comments) == (1234, 80, 5) and v.create_time


def test_a_grid_refresh_keeps_what_only_the_full_reading_has(tmp_path):
    st = Store(tmp_path / "lib.json")
    st.update_tiktok_videos("A", [TikTokVideo(id="v1", views=1234, likes=80, comments=5, create_time=1e9, cover="c")])
    st.update_tiktok_videos("A", [TikTokVideo(id="v1", views=1200)])  # off the grid: "1.2K", nothing else
    v = st.videos("A")["v1"]
    assert (v.views, v.likes, v.comments, v.create_time, v.cover) == (1234, 80, 5, 1e9, "c")
    assert [p[1] for p in v.history] == [1234, 1234]
    st.update_tiktok_videos("A", [TikTokVideo(id="v1", views=1300)])  # "1.3K": it did go up
    assert st.videos("A")["v1"].views == 1300


def test_a_login_that_ran_out_stops_an_account_without_a_username(fake):
    a = connected_account(username="")
    fake.session = False
    with pytest.raises(live.LiveError) as e:
        live.read(settings(), a)
    assert e.value.code == "logged_out" and e.value.code in live.STOP_CODES and "Log in again" in str(e.value)


def test_a_reading_says_whether_the_login_is_still_there(fake):
    a = connected_account()
    fake.add_video("v1", "a cat")
    assert live.read(settings(), a).logged_in is True
    fake.session = False  # the @username is known: the public profile is still read
    r = live.read(settings(), a)
    assert r.logged_in is False and r.videos


def test_live_readings_dont_read_past_a_captcha(fake):
    a = connected_account()
    fake.add_video("v1", "a cat")
    live.read(settings(), a)
    assert fake.opened[-1]["captcha_wait"] == live.CAPTCHA_WAIT
    fake.fail_with = BrowserError("TikTok's captcha wasn't solved in time.")
    with pytest.raises(live.LiveError) as e:
        live.read(settings(live_show_browser=True), a)
    assert e.value.code == "captcha" and "next reading" in str(e.value)

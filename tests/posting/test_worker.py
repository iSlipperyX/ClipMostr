"""The hands-off path: automation's clips come into the queue, get posted, linked, recorded and cleared away."""

import json
import time
from pathlib import Path

import pytest

from posting_helpers import clipmonster_folder
from ytclip.posting import actions as actions_mod
from ytclip.posting import intake, jobs, live, records
from ytclip.posting.accounts import Accounts
from ytclip.posting.config import Settings
from ytclip.posting.service import Service, read_status
from ytclip.posting.store import INTERRUPTED, Profile, Store, TikTokVideo

HOUR = 3600.0
DAY = 86400.0


class Main:
    """The bits of ClipMonster's Settings posting reads."""

    def __init__(self, root: Path, niches=()):
        self.auto_output_path = root
        self.niches = [dict(n) for n in niches]


def niche(nid="n1", name="Pod", account_id="", account=""):
    return {"id": nid, "name": name, "account_id": account_id, "account": account}


def setup(tmp_path, n_accounts=1, niches=(), automation=None, name="Pod/Talk [abc]"):
    accs = Accounts.load()
    made = [accs.add("" if i == 0 else f"Acc {i}") for i in range(n_accounts)]
    for i, a in enumerate(made):
        a.username, a.logged_in = f"user{i}", True
    accs.save()
    root = tmp_path / "Automation"
    clipmonster_folder(tmp_path, name=name, automation=automation)
    return accs, made, Main(root, niches), root


# --- intake ---------------------------------------------------------------------------------


def test_one_account_takes_every_finished_clip_with_its_facts(tmp_path):
    accs, (a,), main, root = setup(tmp_path, automation={"niche": "Pod", "niche_id": "n1", "how": "trending",
                                                          "recipe": "Podcast"})
    store = Store(tmp_path / "lib.json")
    items, got = intake.take(store, accs, main)
    assert sorted(i.title for i in items) == ["fine", "good"] and got == {a.id: 2}  # never the rejected clip
    assert all(i.status == "queued" and i.account == a.id and i.imported_by == "auto" for i in items)
    good = next(i for i in items if i.title == "good")
    f = good.facts
    assert f["source_title"] == "Big Talk" and f["source_channel"] == "Pod People"
    assert (f["start"], f["end"]) == (60, 125) and f["clip_score"] == 81 and f["hook"] == "He quit on air"
    assert intake.take(store, accs, main) == ([], {})  # nothing twice


def test_each_niche_posts_to_its_own_account(tmp_path):
    accs, (a, b), _, root = setup(tmp_path, 2, automation={"niche": "Games", "niche_id": "g",
                                                             "account_id": "", "account": ""})
    main = Main(root, [niche("g", "Games", account_id=b.id)])
    store = Store(tmp_path / "lib.json")
    items, got = intake.take(store, accs, main)
    assert got == {b.id: 2} and all(i.account == b.id for i in items)


def test_the_account_saved_with_the_clip_wins_over_the_niche_now(tmp_path):
    accs, (a, b), _, root = setup(tmp_path, 2, automation={"niche_id": "g", "account_id": "x"})
    # account_id "x" no longer exists: falls back to the niche's account
    main = Main(root, [niche("g", "Games", account_id=a.id)])
    store = Store(tmp_path / "lib.json")
    assert intake.take(store, accs, main)[1] == {a.id: 2}


def test_a_niche_from_before_3_0_matches_by_username(tmp_path):
    accs, (a, b), _, root = setup(tmp_path, 2, automation={"niche_id": "g"})
    main = Main(root, [niche("g", "Games", account="@USER1")])
    store = Store(tmp_path / "lib.json")
    assert intake.take(store, accs, main)[1] == {b.id: 2}


def test_clips_wait_when_their_niche_has_no_account(tmp_path):
    accs, (a, b), _, root = setup(tmp_path, 2, automation={"niche_id": "g"})
    main = Main(root, [niche("g", "Games")])
    store = Store(tmp_path / "lib.json")
    items, got = intake.take(store, accs, main)
    assert got == {} and len(intake.unassigned(store)) == 2
    from ytclip import niches

    s = type("S", (), {"niches": [niche("g", "Games", account_id=b.id)]})()
    assert niches.find(s, "g").account_id == b.id


def test_niches_follow_their_accounts(tmp_path):
    from ytclip import niches

    accs, (a, b), _, _ = setup(tmp_path, 2)
    s = type("S", (), {})()
    s.niches = [niche("g", "Games", account="@user1"), niche("p", "Pod", account_id=a.id),
                niche("x", "Gone", account_id="deleted", account="@old")]
    assert niches.link_accounts(s, accs)
    by = {n.id: n for n in niches.load(s)}
    assert by["g"].account_id == b.id and by["g"].account == "@user1"
    assert by["p"].account == "@user0"
    assert by["x"].account_id == "" and by["x"].account == "@old"
    assert not niches.link_accounts(s, accs)


def test_parts_of_a_series_queue_together_in_order(tmp_path):
    accs = Accounts.load()
    a = accs.add("")
    accs.save()
    folder = tmp_path / "Automation" / "Story [s1]"
    folder.mkdir(parents=True)
    clips = []
    for n in (2, 1, 3):
        name = f"0{n} - part {n}.mp4"
        (folder / name).write_bytes(b"x")
        clips.append({"file": name, "title": f"part {n}", "series": "s", "part": n, "parts": 3, "qc": {"ok": True}})
    (folder / "clips.json").write_text(json.dumps({"video": {"title": "Story"}, "clips": clips}), encoding="utf-8")
    store = Store(tmp_path / "lib.json")
    intake.take(store, accs, Main(tmp_path / "Automation"))
    assert [i.part for i in store.queue(a.id)] == [1, 2, 3]


# --- records and clearing up -------------------------------------------------------------------


def posted(tmp_path, linked=True, age=2 * HOUR, **kw):
    accs, (a,), main, root = setup(tmp_path, **kw)
    store = Store(tmp_path / "lib.json")
    items, _ = intake.take(store, accs, main)
    good = next(i for i in items if i.title == "good")
    now = time.time()
    good.status, good.posted_at, good.posted_via = "posted", now - age, "browser"
    good.queue_pos = good.planned_at = None
    if linked:
        store.update_tiktok_videos(a.id, [TikTokVideo(id="7001", description="He quit on air #fyp",
                                                      create_time=now - age + 30, views=1234, likes=56,
                                                      url="https://www.tiktok.com/@user0/video/7001")])
        good.tiktok_id, good.tiktok_url = "7001", "https://www.tiktok.com/@user0/video/7001"
    return accs, a, main, root, store, good, now


def test_a_linked_post_is_recorded_then_its_files_are_deleted(tmp_path):
    accs, a, main, root, store, good, now = posted(tmp_path)
    folder = Path(good.path).parent
    (folder / "01 - good.srt").write_text("1\n", encoding="utf-8")
    s = Settings(records_folder=str(tmp_path / "Records"))
    r = records.finish(store, accs, s, [root], now=now)
    assert r.cleaned == 1 and r.recorded == 1 and not r.failed
    assert not Path(good.path).exists() and not (folder / "01 - good.srt").exists()
    assert (folder / "02 - fine.mp4").exists()  # the other clip is still waiting to post
    index = json.loads((folder / "clips.json").read_text(encoding="utf-8"))
    assert [c["file"] for c in index["clips"] if not c["file"].startswith("_rejected")] == ["02 - fine.mp4"]
    rec = Path(good.record)
    assert rec.is_file() and rec.with_suffix(".json").is_file() and rec.parent.parent == tmp_path / "Records"
    text = rec.read_text(encoding="utf-8")
    for want in ("He quit on air", "Big Talk", "Pod People", "https://www.tiktok.com/@user0/video/7001", "1,234"):
        assert want in text
    data = json.loads(rec.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["tiktok_id"] == "7001" and data["source"]["title"] == "Big Talk"
    assert good.cleaned_at == now
    assert records.finish(store, accs, s, [root], now=now).cleaned == 0  # once


def test_the_video_folder_goes_once_its_last_clip_is_cleared(tmp_path):
    accs, a, main, root, store, good, now = posted(tmp_path)
    fine = next(i for i in store.items.values() if i.title == "fine")
    fine.status, fine.posted_at, fine.tiktok_id = "posted", now - HOUR, "7002"
    store.update_tiktok_videos(a.id, [TikTokVideo(id="7002", description="fine", create_time=now - HOUR)])
    folder = Path(good.path).parent
    s = Settings(records_folder=str(tmp_path / "Records"))
    r = records.finish(store, accs, s, [root], now=now)
    assert r.cleaned == 2 and not folder.exists()
    assert folder.parent.exists() and root.exists()  # the niche folder ("Pod") and Automation stay


def test_a_post_never_seen_on_the_profile_keeps_its_files(tmp_path):
    accs, a, main, root, store, good, now = posted(tmp_path, linked=False, age=3 * DAY)
    s = Settings(records_folder=str(tmp_path / "Records"))
    r = records.finish(store, accs, s, [root], now=now)
    assert r.cleaned == 0 and r.kept and Path(good.path).exists()
    assert good.keep_reason == records.UNSEEN and Path(good.record).is_file()
    assert records.finish(store, accs, s, [root], now=now).kept == []  # said once
    # it turns up after all: cleared then
    store.update_tiktok_videos(a.id, [TikTokVideo(id="9", description="He quit on air", create_time=now - 3 * DAY)])
    good.tiktok_id = "9"
    assert records.finish(store, accs, s, [root], now=now).cleaned == 1 and not Path(good.path).exists()


def test_with_deleting_off_only_the_record_is_written(tmp_path):
    accs, a, main, root, store, good, now = posted(tmp_path)
    s = Settings(records_folder=str(tmp_path / "Records"), delete_after_post=False)
    r = records.finish(store, accs, s, [root], now=now)
    assert r.recorded == 1 and r.cleaned == 0 and Path(good.path).exists() and Path(good.record).is_file()


def test_snapshots_at_an_hour_a_day_and_a_week(tmp_path):
    accs, a, main, root, store, good, now = posted(tmp_path, age=0)
    s = Settings(records_folder=str(tmp_path / "Records"))
    records.finish(store, accs, s, [root], now=now)
    posted_at = store.video_for(good).create_time
    assert records.update_records(store, accs, s, now=posted_at + 30 * 60) == 0
    assert records.update_records(store, accs, s, now=posted_at + HOUR + 1) == 1
    assert list(good.snapshots) == ["1h"] and good.snapshots["1h"][1] == 1234
    records.update_records(store, accs, s, now=posted_at + 8 * DAY)
    assert set(good.snapshots) == {"1h", "24h", "7d"}
    text = Path(good.record).read_text(encoding="utf-8")
    assert "1 hour" in text or "1h" in text


# --- the worker ---------------------------------------------------------------------------------


class FakeTikTok:
    """Stands in for the browser: posting puts a video on the profile, reading returns the profile."""

    def __init__(self, now):
        self.now = now
        self.videos: list[TikTokVideo] = []
        self.posted: list[str] = []
        self.fail_with = None

    def post(self, store, item_id, s, account, log, cancelled, progress=None):
        from ytclip.posting import jobs, tiktok

        it = store.get(item_id)

        def upload(*_a, **_k):
            if self.fail_with:
                raise self.fail_with
            self.posted.append(it.title)
            caption = tiktok.compose_caption(it, account.apply(s))
            n = len(self.posted)
            self.videos.insert(0, TikTokVideo(id=f"v{n}", description=caption, create_time=self.now() + 5,
                                              views=10, url=f"https://t/v{n}"))
            return {"via": "browser"}

        orig, tiktok.post = tiktok.post, upload
        try:
            return jobs.post_item(store, item_id, s, account, log, cancelled, progress)
        finally:
            tiktok.post = orig

    def read(self, s, account, full=False, log=None, cancelled=None):
        return live.Reading(at=self.now(), profile=Profile(username=account.username, followers=5,
                                                           fetched_at=self.now(), video_count=len(self.videos)),
                            videos=list(self.videos), full=full, logged_in=True)


@pytest.fixture
def clock(monkeypatch):
    t = [time.time()]
    monkeypatch.setattr(time, "time", lambda: t[0])
    return t


def worker(tmp_path, clock, accs, main, **kw):
    s = Settings(records_folder=str(tmp_path / "Records"), ai_hashtags=False, **kw)
    svc = Service(main_load=lambda: main, now=lambda: clock[0], settings_load=lambda: s,
                  actions=actions_mod.Actions(tmp_path / "actions.db"), status_file=tmp_path / "status.json",
                  live_folder=tmp_path / "live")
    fake = FakeTikTok(lambda: clock[0])
    svc.post_fn, svc.read_fn = fake.post, fake.read
    svc.analyze_fn = lambda store, item_id, s: None
    svc.check_fn = lambda s, a, log, cancelled, full=False: [("Upload page", "ok", "")]
    return svc, fake


def test_hands_off_from_new_clip_to_record(tmp_path, clock):
    accs, (a,), main, root = setup(tmp_path)
    a.first_post_time, a.last_post_time, a.posts_per_day, a.warm_up_start = "00:00", "23:59", 2, 0.0
    accs.save()
    svc, fake = worker(tmp_path, clock, accs, main)
    svc.start()
    svc.tick()  # takes the clips in and plans their times
    assert len(svc.store.queue(a.id)) == 2
    clock[0] = jobs.next_post(svc.store, a.id).planned_at + 1
    # The next passes post the clip that's due, read the profile, link the post and clear it away.
    for _ in range(4):
        svc.tick()
        clock[0] += 120
    assert fake.posted, "a clip should have posted"
    first = next(i for i in svc.store.items.values() if i.title == fake.posted[0])
    assert first.status == "posted" and first.tiktok_id == "v1"
    assert first.cleaned_at and not Path(first.path).exists() and Path(first.record).is_file()
    feed = live.EventLog(a.id, tmp_path / "live")
    feed.load()
    assert any(e.kind == "posted" for e in feed.events)
    assert read_status(tmp_path / "status.json")["pid"] == svc.pid
    # A second process sees the same library.
    other = Store(svc.store.path)
    assert other.get(first.id).cleaned_at


def test_a_post_that_fails_is_said_once_and_the_queue_goes_on(tmp_path, clock):
    accs, (a,), main, root = setup(tmp_path)
    a.first_post_time, a.last_post_time, a.warm_up_start = "00:00", "23:59", 0.0
    accs.save()
    svc, fake = worker(tmp_path, clock, accs, main)
    fake.fail_with = RuntimeError("TikTok's Post button didn't respond.")
    svc.start()
    svc.tick()
    clock[0] = jobs.next_post(svc.store, a.id).planned_at + 1
    svc.tick()
    failed = [i for i in svc.store.items.values() if i.status == "failed"]
    assert len(failed) == 1 and "Post button" in failed[0].last_error
    feed = live.EventLog(a.id, tmp_path / "live")
    feed.load()
    assert [e for e in feed.events if e.kind == "error" and e.alert]


def test_a_lost_login_stops_posting_and_says_so(tmp_path, clock):
    accs, (a,), main, root = setup(tmp_path)
    a.first_post_time, a.last_post_time, a.warm_up_start = "00:00", "23:59", 0.0
    accs.save()
    svc, fake = worker(tmp_path, clock, accs, main)
    fake.fail_with = RuntimeError("Not logged in to TikTok in this browser profile.")
    svc.start()
    svc.tick()
    clock[0] = jobs.next_post(svc.store, a.id).planned_at + 1
    svc.tick()
    assert not svc.accounts.get(a.id).logged_in
    assert not Accounts.load().get(a.id).logged_in  # saved for the window


def test_an_upload_cut_off_by_a_crash_is_never_posted_again_by_itself(tmp_path, clock):
    accs, (a,), main, root = setup(tmp_path)
    store = Store()
    items, _ = intake.take(store, accs, main)
    items[0].status = "uploading"
    store.save()
    svc, fake = worker(tmp_path, clock, accs, main)
    svc.start()
    it = svc.store.get(items[0].id)
    assert it.status == "failed" and it.last_error == INTERRUPTED


def test_the_window_asks_and_the_worker_does_it(tmp_path, clock):
    accs, (a,), main, root = setup(tmp_path)
    a.autopilot = False  # paused: only Post now posts
    accs.save()
    svc, fake = worker(tmp_path, clock, accs, main)
    svc.start()
    svc.intake()
    item = svc.store.queue(a.id)[0]
    window = actions_mod.Actions(tmp_path / "actions.db")
    rid = window.submit("post_now", account=a.id, item=item.id)
    assert window.submit("post_now", account=a.id, item=item.id) == rid  # pressed twice: one request
    svc.tick()
    req = window.get(rid)
    assert req.state == "done" and fake.posted == [item.title]
    rid = window.submit("test_posting", account=a.id, full=True)
    svc.tick()
    assert window.get(rid).state == "done" and "passed" in window.get(rid).result


def test_a_window_without_the_background_worker_only_does_what_it_is_asked(tmp_path, clock):
    accs, (a,), main, root = setup(tmp_path)
    a.first_post_time, a.last_post_time, a.warm_up_start = "00:00", "23:59", 0.0
    accs.save()
    svc, fake = worker(tmp_path, clock, accs, main)
    svc.posting = False
    svc.start()
    svc.tick()
    assert fake.posted == [] and not svc.store.items  # no intake, no posting

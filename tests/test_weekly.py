"""The weekly report (weekly.py): each account's week from the daily stats record, estimates without it, what
automation made, and automation sending it once on Monday mornings."""

import json
import time
from types import SimpleNamespace

from ytclip import notify, weekly
from ytclip.jobs import Job, JobDB, JobType
from ytclip.posting.accounts import Accounts
from ytclip.posting.store import Profile, Store, TikTokVideo
from ytclip.settings import Settings

DAY = 86400.0
MONDAY_9 = time.mktime((2026, 11, 9, 9, 5, 0, 0, 0, -1))  # Monday 9 Nov 2026, 09:05 local
NOW = MONDAY_9


def setup(tmp_path):
    store = Store(tmp_path / "library.json")
    accs = Accounts.load(tmp_path / "accounts.json")
    a = accs.add("")
    a.username = "mindfacts"
    return store, accs, a


def post(store, account, vid, *, views, days_ago, seconds=75.0, creator=False, title="", history=()):
    """A video this app posted `days_ago`, matched to its TikTok video with `views`."""
    from ytclip.posting.store import VideoItem

    it = VideoItem(path=f"C:/clips/{vid}.mp4", title=title or vid, status="posted", account=account.id,
                   posted_at=NOW - days_ago * DAY, tiktok_id=vid, tiktok_url=f"https://www.tiktok.com/@x/video/{vid}",
                   ai_generated=creator, info={"duration": seconds})
    store.items[it.id] = it
    tiktok(store, account, vid, views=views, days_ago=days_ago, seconds=seconds, history=history)
    return it


def tiktok(store, account, vid, *, views, days_ago, seconds=75.0, description="", history=()):
    store.videos(account.id)[vid] = TikTokVideo(vid, description=description or f"caption {vid}",
                                                create_time=NOW - days_ago * DAY, views=views, duration=seconds,
                                                url=f"https://www.tiktok.com/@x/video/{vid}",
                                                history=[list(h) for h in history] + [[NOW - 600, views, 0, 0, 0]])


def reading(record, account, at, followers, views):
    record.days.append({"at": at, "accounts": {account.id: {"read_at": at, "followers": followers, "views": views}}})


def busy_week(tmp_path):
    store, accs, a = setup(tmp_path)
    store.stats_for(a.id).profile = Profile(followers=1_200, fetched_at=NOW - 600,
                                            history=[[NOW - 600, 1_200, 0, 0]])
    tiktok(store, a, "old", views=12_000, days_ago=30, seconds=65, description="The oldie")
    post(store, a, "n1", views=5_000, days_ago=3, title="Why we procrastinate")
    post(store, a, "n2", views=8_000, days_ago=2, creator=True, title="The Stanford prison myth")
    post(store, a, "n3", views=200, days_ago=5, seconds=30, title="A dud")
    tiktok(store, a, "hand", views=1_000, days_ago=4, seconds=90, description="Posted from my phone")
    record = weekly.Record(tmp_path / "days.json")
    reading(record, a, NOW - 14 * DAY, 900, {"old": 1_000})
    reading(record, a, NOW - 7 * DAY - 3600, 1_000, {"old": 3_000})
    reading(record, a, NOW - 6 * DAY, 1_010, {"old": 3_500})
    return store, accs, a, record


def test_a_week_from_the_daily_record(tmp_path):
    store, accs, a, record = busy_week(tmp_path)
    w = weekly.build(store, accs, now=NOW, record=record)
    line = w.accounts[0]
    assert (line.posts, line.clips, line.creator, line.own, line.short) == (4, 2, 1, 1, 1)
    # old: about 3,000 a week ago (between the readings either side) -> about 9,000 this week; the new ones all
    assert 8_900 <= line.views - (5_000 + 8_000 + 200 + 1_000) <= 9_000 and not line.estimated
    assert 2_000 <= line.views_before <= 2_030  # 1,000 -> a little over 3,000 (between the readings)
    assert line.earning == line.views - 200  # n3 is 30 s: it can't earn
    assert line.gained == 200 and line.followers == 1_200
    assert line.best.title == "The Stanford prison myth" and line.best.kind == "creator"
    assert line.weakest.title == "A dud"
    assert line.took_off.title == "The oldie" and line.took_off.kind == "own"
    assert (line.creator_avg, line.creator_n, line.clip_avg, line.clip_n) == (8_000, 1, 2_600, 2)
    text = "\n".join(w.lines())
    assert w.headline() == "Past week: 4 posts, 23.2K views (11 times the week before), +200 followers."
    assert weekly.change(1_310, 1_000) == "up 31% on the week before"
    assert weekly.change(800, 1_000) == "down 20% on the week before"
    assert weekly.change(1_002, 1_000) == "the same as the week before"
    assert weekly.change(3_000, 1_000) == "3 times the week before"
    assert weekly.change(4_500, 1_000) == "4.5 times the week before"
    assert "4 posts: 2 clips, 1 Creator video, 1 posted by hand" in text
    assert "Best post: “The Stanford prison myth”, 8K views" in text
    assert "Weakest: “A dud”, 200 views" in text
    assert "An older video took off: “The oldie” got 9" in text
    assert "Creator videos averaged 8K views a post (1), clips 2.6K (2)" in text
    assert "1 post was a minute or shorter, so it can't earn" in text
    assert "Creator Rewards: 1,200 of 10,000 followers" in text
    links = [link for _, rows in w.sections() for _, link in rows if link]
    assert "https://www.tiktok.com/@x/video/n2" in links
    json.dumps(w.to_json())


def test_without_the_record_older_videos_are_estimates(tmp_path):
    store, accs, a = setup(tmp_path)
    store.stats_for(a.id).profile = Profile(followers=50, fetched_at=NOW)
    tiktok(store, a, "old", views=3_000, days_ago=40, history=[[NOW - 2 * DAY, 2_000, 0, 0, 0]])
    w = weekly.build(store, accs, now=NOW, record=weekly.Record(tmp_path / "none.json"))
    line = w.accounts[0]
    assert line.estimated and line.views_before is None and line.gained is None
    assert w.headline() == "Past week: 0 posts, about " + weekly.fmt(line.views) + " views."
    assert "Nothing posted this week." in "\n".join(w.lines())


def test_an_account_not_signed_in_and_a_quiet_week(tmp_path):
    store, accs, a = setup(tmp_path)
    w = weekly.build(store, accs, now=NOW, record=weekly.Record(tmp_path / "none.json"))
    assert w.empty and w.headline() == "Nothing to report for the past week."
    assert w.sections()[0][1][0][0].startswith("No stats yet")


def test_what_automation_made(tmp_path):
    store, accs, _ = setup(tmp_path)
    db = JobDB(tmp_path / "jobs.db")
    db.add(Job("a", "u", status="done", clips=4), NOW - DAY)
    db.add(Job("b", "u", status="done", clips=3), NOW - 2 * DAY)
    db.add(Job("c", "u", status="done", kind=JobType.GENERATIVE_CREATOR.value), NOW - DAY)
    db.add(Job("d", "u", status="failed", error="boom"), NOW - DAY)
    db.add(Job("old", "u", status="done", clips=9), NOW - 9 * DAY)
    w = weekly.build(store, accs, db, now=NOW, record=weekly.Record(tmp_path / "none.json"))
    assert (w.videos_clipped, w.clips_made, w.creator_made, w.failed) == (2, 7, 1, 1)
    assert not w.empty
    assert w.headline() == "Past week: automation made 7 clips from 2 YouTube videos, 1 Creator video, 1 failed."
    assert w.sections()[-1] == ("Automation", [("Automation made 7 clips from 2 YouTube videos, 1 Creator video, "
                                                "1 failed.", "")])


def test_the_record_takes_a_reading_a_day(tmp_path):
    store, accs, a = setup(tmp_path)
    store.stats_for(a.id).profile = Profile(followers=10, fetched_at=NOW - 60)
    tiktok(store, a, "v", views=100, days_ago=3)
    record = weekly.Record(tmp_path / "days.json")
    assert record.take(store, accs.ids(), NOW)
    assert not record.take(store, accs.ids(), NOW + 3600)  # not due
    assert record.take(store, accs.ids(), NOW + 21 * 3600)  # due, but not read again: left out
    assert record.days[-1]["accounts"] == {}
    record.save()
    again = weekly.Record(tmp_path / "days.json")
    assert again.days[0]["accounts"][a.id]["views"] == {"v": 100}
    assert again.value_at(a.id, NOW, lambda e: e["views"].get("v")) == (100.0, True)
    assert again.value_at(a.id, NOW - 3 * DAY, lambda e: e["views"].get("v")) == (None, False)
    reading(again, a, NOW + 3 * DAY, 20, {"v": 400})
    got, exact = again.value_at(a.id, NOW + 1.5 * DAY, lambda e: e["views"].get("v"))
    assert 240 < got < 260 and not exact  # halfway, but the readings are 3 days apart: a guess
    old = weekly.Record(tmp_path / "days.json")
    old.days = [{"at": NOW - 30 * DAY, "accounts": {}}]
    old.take(store, accs.ids(), NOW)
    assert len(old.days) == 1  # three weeks kept


def test_sent_once_on_monday_morning(tmp_path):
    store, accs, a, record = busy_week(tmp_path)
    db = JobDB(tmp_path / "jobs.db")
    sent = []
    logs = []

    def run(at, s=None):
        return weekly.tick(s or Settings(), db, at, logs.append, store=store, accounts=accs, record=record,
                           notify_fn=sent.append)

    assert run(NOW - 3 * DAY) is None and db.meta("weekly_sent")  # the first run: the coming Monday's is first
    assert run(NOW - 10 * 60) is None  # Monday 08:55
    w = run(NOW)
    assert w is not None and sent == [w]
    assert any(m.startswith("Weekly report: Past week: 4 posts") for m in logs)
    saved = weekly.saved()
    assert [p.name for p in saved] == ["week-2026-11-09.json"]
    assert weekly.load(saved[0]).headline() == w.headline()
    assert saved[0].with_suffix(".txt").read_text(encoding="utf-8").startswith("ClipMonster weekly report, 2–9 Nov")
    assert run(NOW + 3600) is None and len(sent) == 1  # once a week
    assert run(NOW + 7 * DAY) is not None and len(sent) == 2  # next Monday
    assert run(NOW + 14 * DAY, Settings().apply({"weekly_report": False})) is None and len(sent) == 2


def test_a_missed_monday_is_sent_later_that_week(tmp_path):
    store, accs, a, record = busy_week(tmp_path)
    db = JobDB(tmp_path / "jobs.db")
    db.set_meta("weekly_sent", weekly.week_key(NOW - 7 * DAY))
    sent = []
    weekly.tick(Settings(), db, NOW + 2 * DAY, store=store, accounts=accs, record=record, notify_fn=sent.append)
    assert len(sent) == 1


def test_old_reports_are_cleared(tmp_path):
    w = weekly.Week(since=NOW - 7 * DAY, until=NOW)
    for n in range(weekly.REPORTS_KEPT + 3):
        w.until = NOW - n * 7 * DAY
        weekly.save(w, tmp_path)
    assert len(weekly.saved(tmp_path)) == weekly.REPORTS_KEPT
    assert len(list(tmp_path.glob("*.txt"))) == weekly.REPORTS_KEPT


def test_the_notification_opens_the_report():
    assert notify.parse_link(notify.report_link()) == ("report", "week")
    assert notify.parse_link("clipmonster://report/../../x") is None


def test_the_runner_hands_the_report_to_telegram(tmp_path, monkeypatch):
    from ytclip.pipeline import Runner

    db = JobDB(tmp_path / "jobs.db")
    w = weekly.Week(since=0, until=1)
    monkeypatch.setattr(weekly, "tick", lambda s, db, now, log=None: w)
    got = []
    r = Runner(db, load=Settings, now=lambda: NOW)
    r.telegram = SimpleNamespace(send_week=got.append)
    assert r.weekly(Settings(), NOW) is w and got == [w]
    assert db.meta_float("weekly_at") == NOW
    monkeypatch.setattr(weekly, "tick", lambda *a, **k: 1 / 0)
    assert r.weekly(Settings(), NOW) is None  # logged, never raised


def test_the_dialog_shows_the_week(tmp_path, app):
    from PySide6.QtWidgets import QApplication

    from ytclip.gui.weekly_view import WeeklyDialog, saved_label, week_html

    store, accs, a, record = busy_week(tmp_path)
    w = weekly.build(store, accs, now=NOW, record=record)
    page = week_html(w)
    assert "The Stanford prison myth" in page and "href='https://www.tiktok.com/@x/video/n2'" in page
    weekly.save(w)
    dlg = WeeklyDialog(SimpleNamespace(store=store, accounts=accs))
    assert dlg.which.count() == 2 and dlg.which.itemText(1) == "Week to Mon 9 Nov 2026"
    dlg.which.setCurrentIndex(1)
    assert "Stanford" in dlg.view.toPlainText()
    dlg.copy()
    assert QApplication.clipboard().text().startswith("ClipMonster weekly report")
    assert saved_label(tmp_path / "week-junk.json") == "week-junk"
    dlg.which.setCurrentIndex(0)  # the past 7 days, worked out now (from the app's own record: none yet)
    assert "@mindfacts" in dlg.view.toPlainText()
    dlg.close()
    dlg.deleteLater()

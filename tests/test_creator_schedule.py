"""The Video Creator's daily schedule (creator/schedule.py): trending phrases per category, the day's ideas
(topic list and trends taking turns, nothing reused within a month), planning the jobs once a day, and the
runner doing it hourly."""

import json
import time
from types import SimpleNamespace

from ytclip import categories
from ytclip.creator import orchestrator, schedule, web
from ytclip.jobs import JobDB
from ytclip.settings import Settings

DAY = 86400.0
NOW = time.mktime((2026, 11, 9, 0, 20, 0, 0, 0, -1))  # 00:20 local: before the 1 am start


def vid(title, cat="psy"):
    return SimpleNamespace(title=title, category_id=cat)


TRENDING = [vid("GTA 6 trailer 3 breakdown", "games"), vid("GTA 6 release date confirmed!", "games"),
            vid("Everything we know about GTA 6", "games"), vid("Minecraft 1.22 update is here", "games"),
            vid("The Minecraft 1.22 update changes everything", "games"), vid("GTA 6 cars", "other")]


def cat(**kw):
    base = dict(id="psy", name="Psychology", account_id="acc1", account="@mindfacts", creator_daily=2)
    return categories.Category.from_json(base | kw)


def settings(*cats, **kw):
    return Settings().apply({"categories": [c.to_json() for c in cats], **kw})


def test_trending_phrases_shared_by_several_titles():
    got = schedule.trending_topics(TRENDING, "games")
    assert got[0] == ("GTA 6", 3)
    assert ("Minecraft 1.22", 2) in got  # "update" is too generic to end a phrase on
    assert not any(p.lower() in ("update", "minecraft", "1.22") for p, _ in got)  # inside a longer phrase
    assert not any("release date" in p for p, _ in got)  # in one title only
    assert schedule.trending_topics(TRENDING, "games", exclude=("GTA 6",))[0][0] != "GTA 6"
    assert schedule.trending_topics(TRENDING, "psy") == []


def test_topic_lines():
    assert schedule.topic_lines("One idea\n\n# a note\nTwo # with a note\none IDEA") == ["One idea", "Two"]


def test_ideas_take_turns_and_are_not_reused(tmp_path):
    used = schedule.Used(tmp_path / "s.json")
    c = cat(id="games", name="Gaming", creator_topics="Speedrun tricks explained\nThe history of the PS1")
    ideas = schedule.ideas_for(c, TRENDING, used, NOW, 3)
    assert [i.prompt for i in ideas] == ["Speedrun tricks explained", schedule.TRENDING_PROMPT.format(topic="GTA 6"),
                                         "The history of the PS1"]
    assert ideas[1].search == "GTA 6" and "trending in Gaming" in ideas[1].why
    for i in ideas:
        used.mark(i.search or i.prompt, NOW)
    nxt = schedule.ideas_for(c, TRENDING, used, NOW + DAY, 2)
    assert nxt[0].search == "Minecraft 1.22"  # the list is used up for a month; trends go on
    assert "Speedrun" not in " ".join(i.prompt for i in nxt)
    later = schedule.ideas_for(c, TRENDING, used, NOW + 31 * DAY, 1)
    assert later[0].prompt == "Speedrun tricks explained"  # a month on, back again


def test_without_topics_or_trends_the_name_and_title_words(tmp_path):
    used = schedule.Used(tmp_path / "s.json")
    c = cat(keywords="dopamine, habits")
    ideas = schedule.ideas_for(c, [], used, NOW, 3)
    assert len(ideas) == 3 and len({i.prompt for i in ideas}) == 3
    assert all(any(w in i.prompt for w in ("dopamine", "habits", "Psychology")) for i in ideas)
    for n in range(40):  # every angle used up: still the day's videos, least recently used first
        for i in schedule.ideas_for(c, [], used, NOW + n * 60, 2):
            used.mark(i.prompt, NOW + n * 60)
    assert len(schedule.ideas_for(c, [], used, NOW + 41 * 60, 2)) == 2


def test_plan_queues_the_days_videos_once(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    s = settings(cat(creator_topics="Why we procrastinate"), cat(id="none", name="No account", account_id=""),
                 cat(id="zero", name="Zero", creator_daily=0))
    logs = []
    used = schedule.Used(tmp_path / "s.json")
    assert schedule.plan(s, db, [], NOW, logs.append, used) == 2
    jobs = db.created_since(0)
    assert [j.category_id for j in jobs] == ["psy", "psy"] and all(j.is_creation for j in jobs)
    assert all(j.origins == ["creator", "schedule"] for j in jobs)
    one_am = schedule.day_start(NOW) + 3600
    assert all(j.next_try == one_am for j in jobs)  # made overnight
    req = orchestrator.CreateRequest.from_json(jobs[0].request)
    assert req.prompt == "Why we procrastinate" and req.category == "psy"
    assert "Planned a Creator video for Psychology: “Why we procrastinate” (from its topic list), made from 01:00" \
        in logs[0]
    assert schedule.plan(s, db, [], NOW + 3600, logs.append, used) == 0  # today's are planned
    assert db.next_due(NOW) is None and db.next_due(one_am).is_creation
    assert schedule.plan(s, db, [], NOW + DAY, logs.append, used) == 2  # tomorrow's
    saved = json.loads((tmp_path / "s.json").read_text())
    assert "why we procrastinate" in saved["used"]


def test_straight_away_and_catching_up(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    s = settings(cat(creator_daily=1), creator_schedule_when="now")
    schedule.plan(s, db, [], NOW, used=schedule.Used(tmp_path / "s.json"))
    assert db.created_since(0)[0].next_try == NOW
    afternoon = schedule.day_start(NOW) + 15 * 3600
    assert schedule.due_at(Settings(), afternoon) == afternoon  # past 1 am: made now


def test_the_request_searches_the_trending_topic(monkeypatch):
    seen = {}

    def gather(prompt, log=None, check=None, query=""):
        seen["query"] = query
        return web.Research(query)

    monkeypatch.setattr(web, "gather", gather)
    from test_creator_web import _good_json

    from ytclip.creator import director

    monkeypatch.setattr(director, "write", lambda s, brief, **kw:
                        director.normalise(director.Storyboard.from_json(_good_json())))
    maker = orchestrator.VideoCreatorOrchestrator(Settings().apply({"creator_factcheck": "off"}))
    maker.storyboard(orchestrator.CreateRequest(prompt=schedule.TRENDING_PROMPT.format(topic="GTA 6"),
                                                search="GTA 6"))
    assert seen["query"] == "GTA 6"
    assert web.query_of("Make a video about the latest GTA 6 news") == "GTA 6"  # without a search: as before


def test_the_runner_plans_hourly(tmp_path):
    from ytclip.pipeline import Runner

    db = JobDB(tmp_path / "jobs.db")
    s = settings(cat(id="games", name="Gaming", creator_daily=1, yt_categories="20"))
    hist = SimpleNamespace(last_videos=lambda: [SimpleNamespace(title=v.title, category_id="", category="20",
                                                                channel="x", channel_id="x", id=str(i))
                                                for i, v in enumerate(TRENDING)])
    logs = []
    r = Runner(db, load=lambda: s, log=logs.append, now=lambda: NOW, history=lambda: hist)
    import ytclip.creator.schedule as sched

    calls = []
    real = sched.plan

    def plan(s, db, videos, now, log=None, used=None):
        calls.append(videos)
        return real(s, db, videos, now, log, schedule.Used(tmp_path / "s.json"))

    sched.plan, saved = plan, sched.plan
    try:
        assert r.plan_creator(s, NOW) == 1
    finally:
        sched.plan = saved
    assert calls and db.meta_float("creator_plan_at") == NOW
    assert any("Planned a Creator video for Gaming" in m for m in logs)

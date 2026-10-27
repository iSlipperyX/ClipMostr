import pytest
from conftest import NOW, api_item

from ytclip.policy import DAY, Decision, PickLog, Policy, decide, picked
from ytclip.settings import Settings
from ytclip.trending import video_from_api


def vid(vid="v", score=80, minutes=30, hours_old=5, channel="Pod", origins=("watchlist",), license="youtube",
        category="22", title=None, live="none", language=""):
    v = video_from_api(api_item(vid, title=title or f"Video {vid}", minutes=minutes, hours_old=hours_old,
                                channel=channel, license=license, category=category, live=live,
                                language=language))
    v.score, v.origins = score, list(origins)
    return v


def one(v, policy=None, log=None, **kw):
    return decide([v], policy or Policy(), log or PickLog(None), now=NOW, **kw)[0]


def test_defaults_come_from_settings():
    p = Policy.from_settings(Settings())
    assert p.pick_from == "watchlist" and p.daily == 3 and p.per_channel_week == 2
    assert p.block_categories == {"10", "1"}
    assert "trailer" in p.block_words


def test_a_listed_channel_passes_and_says_why():
    d = one(vid(), notes={"UCPod": "clipping programme"})
    assert d.picked and d.rule == "picked"
    assert d.reason == "On your channel list: clipping programme"


def test_creative_commons_passes_with_a_credit_reminder():
    d = one(vid(origins=("chart",), license="creativeCommon"))
    assert d.picked and "Creative Commons" in d.reason and "credit" in d.reason


@pytest.mark.parametrize("pick_from,origins,license,ok", [
    ("watchlist", ("chart",), "youtube", False),
    ("watchlist", ("chart", "watchlist"), "youtube", True),
    ("cc", ("watchlist",), "youtube", False),
    ("cc", ("chart",), "creativeCommon", True),
    ("any", ("chart",), "youtube", True),
])
def test_source_policy(pick_from, origins, license, ok):
    d = one(vid(origins=origins, license=license), Policy(pick_from=pick_from))
    assert d.picked == ok
    if not ok:
        assert d.rule == "source"
    if pick_from == "any" and ok:
        assert d.reason == "Passed your rules (no permission on record)"


@pytest.mark.parametrize("kwargs,policy,rule,text", [
    ({"score": 50}, {}, "score", "Clip-ability 50 is under your minimum of 65"),
    ({"minutes": 4}, {}, "length", "Only 4:00 long; your minimum is 8 min"),
    ({"minutes": 300}, {}, "length", "5:00:00 long; your maximum is 180 min"),
    ({"hours_old": 24 * 5}, {}, "age", "Posted 5 days ago; your limit is 3"),
    ({"category": "10"}, {"block_categories": {"10"}}, "category", "Music is blocked"),
    ({"title": "New Official TRAILER"}, {"block_words": ["trailer"]}, "word", 'contains "trailer"'),
    ({"channel": "Bad Chan"}, {"block_channels": ["bad chan"]}, "channel", "Bad Chan is on your blocked channels"),
    ({"channel": "BadChan"}, {"block_channels": ["badchan"]}, "channel", "blocked"),  # an @handle
    ({"channel": "Bad"}, {"block_channels": ["UCBad"]}, "channel", "blocked"),  # a channel id
    ({"live": "live"}, {}, "live", "Live right now"),
    ({"live": "upcoming"}, {}, "live", "premiered"),
])
def test_each_rule_names_itself(kwargs, policy, rule, text):
    d = one(vid(**kwargs), Policy(**policy))
    assert not d.picked and d.rule == rule
    assert text in d.reason


def test_zero_means_no_limit():
    assert one(vid(minutes=600, hours_old=24 * 30), Policy(max_minutes=0, max_age_days=0)).picked


def test_already_picked_or_sent_videos_are_skipped():
    log = PickLog(None)
    v = vid("a")
    log.record(Decision(v, True, "ok"), "auto", now=NOW - 3600)
    assert one(vid("a"), log=log).reason == "Already picked today."
    sent = vid("b")
    sent.sent = True
    assert one(sent).reason == "Already sent to the Studio."


def test_daily_limit_counts_the_log_and_this_round_best_first():
    log = PickLog(None)
    log.record(Decision(vid("old"), True, "ok"), "auto", now=NOW - 2 * 3600)
    log.record(Decision(vid("older"), True, "ok"), "auto", now=NOW - 2 * DAY)  # outside the last 24 hours
    videos = [vid("a", 70, channel="A"), vid("b", 90, channel="B"), vid("c", 80, channel="C")]
    ds = decide(videos, Policy(daily=3), log, now=NOW)
    assert [d.video.id for d in ds] == ["b", "c", "a"]
    assert [d.video.id for d in picked(ds)] == ["b", "c"]
    assert ds[2].rule == "daily_cap" and "limit of 3 picks a day" in ds[2].reason


def test_per_channel_limit_over_a_week():
    log = PickLog(None)
    log.record(Decision(vid("x"), True, "ok"), "auto", now=NOW - 3 * DAY)
    videos = [vid("a", 90), vid("b", 85), vid("c", 80, channel="Other")]
    ds = decide(videos, Policy(per_channel_week=2, daily=10), log, now=NOW)
    assert [d.video.id for d in picked(ds)] == ["a", "c"]
    skipped = next(d for d in ds if d.video.id == "b")
    assert skipped.rule == "channel_cap" and "Already 2 picks from Pod in the last 7 days" in skipped.reason


def test_decide_changes_nothing():
    log = PickLog(None)
    decide([vid("a")], Policy(), log, now=NOW)
    assert log.picks == []


def test_log_saves_reloads_and_forgets_old_picks(tmp_path):
    path = tmp_path / "picks.json"
    log = PickLog(path)
    v = vid("a", license="creativeCommon", origins=("cc",))
    rec = log.record(Decision(v, True, "Creative Commons licence"), "cli", now=NOW - 200 * DAY)
    assert rec["how"] == "cli" and rec["license"] == "creativeCommon" and rec["origins"] == ["cc"]
    log.record(Decision(vid("b"), True, "ok"), "auto", now=NOW)  # recording prunes the 200-day-old pick
    log.save()
    again = PickLog(path)
    assert [p["id"] for p in again.picks] == ["b"]
    assert again.get("b")["permission"] == "ok"


def test_a_broken_log_file_starts_empty(tmp_path):
    path = tmp_path / "picks.json"
    path.write_text("{not json", encoding="utf-8")
    assert PickLog(path).picks == []


def test_two_processes_sharing_the_log_keep_each_others_picks(tmp_path):
    path = tmp_path / "picks.json"
    app, runner = PickLog(path), PickLog(path)
    runner.record(Decision(vid("r"), True, "ok"), "auto", now=NOW)
    runner.save()
    app.record(Decision(vid("a"), True, "ok"), "studio", now=NOW)
    app.save()  # must not drop the runner's pick
    assert sorted(p["id"] for p in PickLog(path).picks) == ["a", "r"]
    app.reload()
    assert app.get("r") is not None


def test_language_rule():
    p = Policy.from_settings(Settings())
    assert p.languages == {"en"}
    d = one(vid(language="id"), p)
    assert not d.picked and d.rule == "language"
    assert d.reason == "In Indonesian; you only pick English videos."
    assert one(vid(language="en-GB"), p).picked
    assert one(vid(), p).picked  # no language on record: let it through
    assert one(vid(language="id"), Policy(languages=set())).picked  # empty setting: any language


def test_language_codes():
    from ytclip.policy import language_codes, parse_languages
    assert language_codes("es-MX, en; EN  pt-BR junk!") == ["es", "en", "pt"]
    assert parse_languages("") == set()

import json

import pytest
from conftest import NOW, api_item

from ytclip import learn, niches
from ytclip.jobs import Job, JobDB
from ytclip.niches import Index, Niche
from ytclip.policy import DAY, PickLog, Policy, decide
from ytclip.research import ScanOptions, scan
from ytclip.settings import Settings
from ytclip.trending import DeepInfo, History, video_from_api

GAMING = {"id": "game01", "name": "Gaming", "account": "@gameclips", "daily": 2, "channels": "@GameChan # ok",
          "searches": "minecraft highlights", "keywords": "fortnite, speed run", "categories": "20"}
PODS = {"id": "pod001", "name": "Podcasts", "daily": 1, "channels": "@PodChan\nUC" + "p" * 22,
        "keywords": "podcast", "categories": "22"}


def _s(*items, **kw):
    return Settings().apply({"niches": [dict(i) if isinstance(i, dict) else i for i in items], "pick_min_score": 0}
                            | kw)


def vid(v="v", channel="Other", title=None, category="24", score=80, origins=("watchlist",), niche="",
        license="youtube"):
    out = video_from_api(api_item(v, title=title or f"Video {v}", channel=channel, category=category, minutes=30,
                                  hours_old=5, license=license))
    out.score, out.origins, out.niche = score, list(origins), niche
    return out


# ---------------------------------------------------------------- reading niches


def test_niches_load_from_settings_and_survive_hand_edits():
    s = _s(GAMING, {"name": "  Gaming  "}, {"name": ""}, "junk", {"id": "x/y", "name": "Pets", "daily": "lots",
                                                                  "enabled": 0, "channels": None})
    got = niches.load(s)
    assert [n.name for n in got] == ["Gaming", "Pets"]  # a repeat of a name, a blank name and junk are left out
    pets = got[1]
    assert pets.id == "xy" and pets.daily == niches.DEFAULT_DAILY and pets.enabled is False and pets.channels == ""
    assert niches.active(s) == [got[0]]
    assert niches.find(s, "game01").account == "@gameclips" and niches.find(s, "nope") is None
    assert niches.load(Settings().apply({"niches": "not a list"})) == []
    assert Niche.from_json({"name": "Big", "daily": 900}).daily == niches.MAX_DAILY


def test_a_repeated_or_missing_id_is_the_same_on_every_read():
    """The runner, the rules and the Research tab each read the niches; they must agree on ids."""
    s = _s(GAMING, PODS | {"id": "game01"}, {"name": "No id", "keywords": "cats"}, pick_from="any")
    first, again = niches.load(s), niches.load(s)
    assert [n.id for n in first] == [n.id for n in again]
    assert first[0].id == "game01" and first[1].id == "game01-2" and first[2].id
    videos = [vid("c", title="cats")]
    niches.assign_from_settings(videos, s, None)
    assert videos[0].niche == first[2].id and _decide(videos, s)["c"].picked


def test_names_and_folders():
    got = niches.load(_s(GAMING))
    assert niches.name_taken(got, "gaming") and niches.name_taken(got, " Gaming ")
    assert not niches.name_taken(got, "gaming", except_id="game01")
    assert niches.unique_name(got, "Gaming") == "Gaming 2"
    assert Niche(name='Cats: "best" bits?').folder == "Cats best bits"
    # Windows device names can't be folders: they get a mark, before any dot.
    assert [Niche(name=x).folder for x in ("Aux", "con", "NUL.", "Prn.clips", "LPT1", "Auxiliary")] == [
        "Aux_", "con_", "NUL_", "Prn_.clips", "LPT1_", "Auxiliary"]
    assert niches.name_taken(niches.load(_s({"name": "Aux"})), "aux")


def test_niches_round_trip_through_settings_json(tmp_path):
    path = tmp_path / "settings.json"
    _s(GAMING).save(path)
    back = niches.load(Settings.load(path))
    assert back[0].to_json() == Niche.from_json(GAMING).to_json()


# ---------------------------------------------------------------- what a scan reads


def test_without_niches_the_scan_reads_the_picking_lists():
    s = Settings().apply({"pick_channels": "@Pod\nnot a channel!", "pick_searches": "a\nb"})
    channels, searches, bad = niches.scan_sources(s)
    assert [c.label for c in channels] == ["@Pod"] and searches == ["a", "b"] and bad == ["not a channel!"]


def test_with_niches_the_scan_reads_every_switched_on_niche_once():
    s = _s(GAMING, PODS | {"channels": "@gamechan\n@PodChan\nnot a channel!", "searches": "Minecraft highlights"},
           {"name": "Off", "enabled": False, "channels": "@Hidden", "searches": "secret"},
           pick_channels="@Legacy", pick_searches="old search")
    channels, searches, bad = niches.scan_sources(s)
    assert [c.label for c in channels] == ["@GameChan", "@PodChan"]  # a channel in two niches is read once
    assert searches == ["minecraft highlights"]  # the same search in two niches is read once
    assert bad == [("Podcasts's channel list", "not a channel!")]
    opts = ScanOptions.from_settings(s)
    assert [n.name for n in opts.niches] == ["Gaming", "Podcasts", "Off"]  # all of them sort videos


# ---------------------------------------------------------------- sorting videos into niches


def _history():
    h = History(None)
    h.sources["channels"]["@gamechan"] = {"id": "UCGameChan"}
    h.sources["channels"]["@podchan"] = {"id": "UCPodChan"}
    h.sources["searches"]["minecraft highlights"] = {"videos": ["found"]}
    return h


def test_a_video_goes_to_the_niche_that_lists_its_channel_first():
    index = Index.build(niches.load(_s(GAMING, PODS)), _history())
    assert index.niche_for(vid("a", channel="GameChan")) == "game01"
    assert index.niche_for(vid("b", channel="PodChan", title="fortnite podcast")) == "pod001"  # channel beats words
    p = vid("c", channel="x")
    p.channel_id = "UC" + "p" * 22  # listed by id: no lookup needed
    assert index.niche_for(p) == "pod001"


def test_then_a_search_then_title_words_then_categories():
    index = Index.build(niches.load(_s(GAMING, PODS)), _history())
    assert index.niche_for(vid("found", category="22", title="a podcast")) == "game01"  # its search found it
    assert index.niche_for(vid("w", category="22", title="FORTNITE win")) == "game01"  # a title word
    assert index.niche_for(vid("s", category="24", title="Speed run record")) == "game01"
    assert index.niche_for(vid("t", category="20", title="The best podcast")) == "pod001"  # words beat categories
    assert index.niche_for(vid("u", category="22", title="Nothing")) == "pod001"
    assert index.niche_for(vid("z", category="24", title="Fortnight")) == ""  # whole words only


def test_a_switched_off_niche_keeps_its_own_channels_videos():
    """Its channel's videos are held back, not handed to another niche's account by a title word."""
    s = _s(GAMING | {"enabled": False}, PODS | {"keywords": "gamechan, fortnite"}, pick_from="any")
    videos = [vid("a", channel="GameChan", title="Fortnite"), vid("b", title="Fortnite", category="20")]
    niches.assign_from_settings(videos, s, _history())
    assert [v.niche for v in videos] == ["game01", "pod001"]  # b: the off niche's category doesn't count
    ds = _decide(videos, s)
    assert ds["a"].rule == "niche" and "switched off" in ds["a"].reason and ds["b"].picked


def test_title_words_in_scripts_without_spaces_and_odd_spacing():
    index = Index.build(niches.load(_s({"id": "jp", "name": "JP", "keywords": "マイクラ, 小猫, call of duty"})), None)
    assert index.niche_for(vid("a", title="マイクラ実況プレイ")) == "jp"
    assert index.niche_for(vid("b", title="可爱的小猫视频")) == "jp"
    assert index.niche_for(vid("c", title="Call of  Duty\u00a0Warzone")) == "jp"
    assert index.niche_for(vid("d", title="call of dutyish")) == ""


def test_assign_fills_in_every_video():
    videos = [vid("a", channel="GameChan"), vid("b", title="nothing")]
    niches.assign_from_settings(videos, _s(GAMING), _history())
    assert [v.niche for v in videos] == ["game01", ""]
    niches.assign(videos, [], None)
    assert [v.niche for v in videos] == ["", ""]


def test_the_scan_sorts_its_videos_into_niches():
    from ytclip import sources

    def fake_sources(key, channels, searches, history, now, log=None, language=""):
        history.sources["channels"]["@gamechan"] = {"id": "UCGameChan"}
        v = video_from_api(api_item("listed", channel="GameChan", minutes=40))
        v.origins = ["watchlist"]
        return sources.Gathered([v], watch_ids={"UCGameChan"})

    opts = ScanOptions.from_settings(_s(GAMING))
    opts.api_key, opts.regions, opts.deep_count = "K", ["US"], 0
    chart = video_from_api(api_item("chart", channel="Somebody", title="Fortnite clutch", minutes=30))
    result = scan(opts, History(None), fetch=lambda *a: [chart], sources=fake_sources, now=NOW,
                  deep=lambda *a, **k: DeepInfo())
    assert {v.id: v.niche for v in result.videos} == {"listed": "game01", "chart": "game01"}


# ---------------------------------------------------------------- picking


def _decide(videos, s, log=None):
    return {d.video.id: d for d in decide(videos, Policy.from_settings(s), log or PickLog(None), now=NOW)}


def test_with_niches_a_video_must_fit_a_switched_on_one():
    s = _s(GAMING, PODS | {"enabled": False}, pick_from="any")
    ds = _decide([vid("a", niche="game01"), vid("b"), vid("c", niche="pod001")], s)
    assert ds["a"].picked
    assert ds["b"].rule == "niche" and "none of your niches" in ds["b"].reason
    assert ds["c"].rule == "niche" and "switched off" in ds["c"].reason


def test_with_every_niche_off_nothing_is_picked():
    s = _s(GAMING | {"enabled": False}, pick_from="any")
    assert not any(d.picked for d in _decide([vid("a"), vid("b", niche="game01")], s).values())


def test_each_niche_has_its_own_daily_limit():
    s = _s(GAMING, PODS, pick_daily=10, pick_channel_weekly=0)
    videos = [vid(f"g{i}", channel=f"G{i}", niche="game01", score=90 - i) for i in range(4)]
    videos += [vid(f"p{i}", channel=f"P{i}", niche="pod001", score=70 - i) for i in range(3)]
    ds = _decide(videos, s)
    assert [k for k, d in ds.items() if d.picked] == ["g0", "g1", "p0"]
    assert ds["g2"].rule == "niche_cap" and "Gaming already has its 2 picks" in ds["g2"].reason
    assert ds["p1"].rule == "niche_cap" and "its 1 pick for today" in ds["p1"].reason


def test_earlier_picks_count_towards_a_niches_limit():
    s = _s(GAMING, PODS, pick_daily=10, pick_channel_weekly=0)
    log = PickLog(None)
    for i in range(2):
        d = _decide([vid(f"old{i}", niche="game01")], s, log)[f"old{i}"]
        log.record(d, "auto", NOW - 3600)
    assert log.picks[0]["niche"] == "game01" and log.count_since(NOW - DAY, niche="game01") == 2
    ds = _decide([vid("new", niche="game01"), vid("pod", niche="pod001")], s, log)
    assert ds["new"].rule == "niche_cap" and ds["pod"].picked


def test_the_overall_limit_is_shared_out_between_niches():
    """One busy niche with better scores doesn't take every pick of the day."""
    s = _s(GAMING | {"daily": 5}, PODS | {"daily": 5}, {"id": "sci001", "name": "Science", "daily": 5},
           pick_daily=4, pick_channel_weekly=0)
    videos = [vid(f"g{i}", channel=f"G{i}", niche="game01", score=95 - i) for i in range(5)]
    videos += [vid(f"p{i}", channel=f"P{i}", niche="pod001", score=60 - i) for i in range(5)]
    videos += [vid("s0", channel="S", niche="sci001", score=55)]
    ds = _decide(videos, s)
    assert sorted(k for k, d in ds.items() if d.picked) == ["g0", "g1", "p0", "s0"]
    assert ds["p1"].rule == "daily_cap" and ds["g2"].rule == "daily_cap"


def test_the_niche_with_fewer_picks_today_goes_first():
    """A niche that got today's earlier picks doesn't also take the last one from a better video."""
    s = _s(GAMING, PODS | {"daily": 2}, pick_daily=2, pick_channel_weekly=0)
    log = PickLog(None)
    first = _decide([vid("g0", channel="G0", niche="game01", score=90)], s, log)["g0"]
    log.record(first, "auto", NOW - 3600)
    ds = _decide([vid("g1", channel="G1", niche="game01", score=88), vid("p0", channel="P", niche="pod001", score=60)],
                 s, log)
    assert ds["p0"].picked and ds["g1"].rule == "daily_cap"


def test_a_channel_limit_doesnt_cost_its_niche_a_turn():
    s = _s(GAMING | {"daily": 5}, PODS | {"daily": 5}, pick_daily=2, pick_channel_weekly=1)
    videos = [vid("g0", channel="Same", niche="game01", score=95), vid("g1", channel="Same", niche="game01", score=94),
              vid("g2", channel="Else", niche="game01", score=93), vid("p0", channel="P", niche="pod001", score=50)]
    ds = _decide(videos, s)
    assert ds["g0"].picked and ds["g1"].rule == "channel_cap" and ds["p0"].picked and ds["g2"].rule == "daily_cap"


def test_decisions_come_back_best_first():
    s = _s(GAMING, PODS, pick_daily=10)
    videos = [vid("p0", channel="P", niche="pod001", score=60), vid("g0", channel="G", niche="game01", score=90),
              vid("x", score=99)]
    assert [d.video.id for d in decide(videos, Policy.from_settings(s), PickLog(None), now=NOW)] == ["x", "g0", "p0"]


def test_without_niches_nothing_changes():
    s = Settings().apply({"pick_daily": 2, "pick_from": "any", "pick_channel_weekly": 0})
    ds = _decide([vid(f"v{i}", channel=f"C{i}", score=90 - i, niche="ignored") for i in range(3)], s)
    assert [k for k, d in ds.items() if d.picked] == ["v0", "v1"] and ds["v2"].rule == "daily_cap"


# ---------------------------------------------------------------- the queue


def test_jobs_remember_their_niche_and_old_queues_get_the_column(tmp_path):
    import sqlite3

    path = tmp_path / "jobs.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, url TEXT, status TEXT, created REAL, updated REAL, "
                "next_try REAL, clips INTEGER)")
    con.execute("INSERT INTO jobs VALUES ('old', 'u', 'done', 1, 1, 0, 3)")
    con.commit()
    con.close()
    db = JobDB(path)
    assert db.get("old").niche in ("", None)
    db.add(Job("a", "u", niche="game01", status="queued"), NOW)
    db.add(Job("b", "u", niche="game01", status="done", clips=5), NOW)
    db.add(Job("c", "u", niche="pod001", status="working"), NOW)
    counts = db.niche_counts(NOW - DAY)
    assert counts["game01"] == {"waiting": 1, "done": 1, "clips": 5}
    assert counts["pod001"]["waiting"] == 1 and counts[""]["done"] == 0
    assert db.drop_waiting("game01") == 1 and db.get("a") is None and db.get("b") is not None
    assert db.drop_waiting("pod001") == 0  # being clipped: it finishes


# ---------------------------------------------------------------- learning per niche


def test_hook_examples_come_from_the_niche_once_it_has_enough():
    rows = [{"lift": 3.0, "hook": f"game hook {i}", "niche": "game01", "channel": "G"} for i in range(6)]
    rows += [{"lift": 4.0, "hook": f"pod hook {i}", "niche": "pod001", "channel": "P"} for i in range(3)]
    lessons = learn.Lessons.from_outcomes([learn.Outcome.from_json(r) for r in rows])
    good, _ = lessons.hook_examples(niche="game01")
    assert all(h.startswith("game") for h, _ in good)
    good, _ = lessons.hook_examples(niche="pod001")  # only 3 of its own: all posts are used
    assert good[0][0].startswith("pod")
    assert "game hook" in lessons.prompt_text("game01") and "pod hook" not in lessons.prompt_text("game01")


# ---------------------------------------------------------------- the command line


def test_ytclip_niches_lists_them(tmp_path, capsys):
    from ytclip import cli

    _s(GAMING, PODS | {"enabled": False}, pick_daily=1).save()
    assert cli.main(["niches", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["name"], r["on"], r["channels"], r["searches"]) for r in rows] == [("Gaming", True, 1, 1),
                                                                                 ("Podcasts", False, 2, 0)]
    assert rows[0]["folder"].endswith("Gaming")
    assert cli.main(["niches"]) == 0
    assert "add up to 2 videos a day" in capsys.readouterr().out


def test_ytclip_auto_add_takes_a_niche(capsys):
    from ytclip import cli

    _s(GAMING).save()
    assert cli.main(["auto", "--add", "https://youtu.be/abcdefghijk", "--niche", "gaming"]) == 0
    assert JobDB().get("abcdefghijk").niche == "game01"
    assert cli.main(["auto", "--add", "https://youtu.be/bbbbbbbbbbb", "--niche", "Cats"]) == 2
    assert "No niche called Cats (yours: Gaming)" in capsys.readouterr().out


@pytest.fixture(autouse=True)
def _no_real_results(monkeypatch, tmp_path):
    monkeypatch.setattr(learn, "results_path", lambda: tmp_path / "no-results.json")

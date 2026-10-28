"""Learning from results: TikTok Manager's results.json -> channel bonuses, hook examples and notes."""

import json
from pathlib import Path

import numpy as np
import pytest
from conftest import NOW
from test_policy import vid

from ytclip import ai, cli, learn, polish
from ytclip.analyze import Clip, Measurements
from ytclip.download import Video
from ytclip.engine import Engine, Project
from ytclip.learn import Lessons, Outcome
from ytclip.policy import PickLog, Policy, decide
from ytclip.settings import Settings

DAY = 86400


def row(channel="Pod", lift=1.0, hook="", length=30, rating=None, days_ago=3, views=None):
    return {"channel": channel, "video_id": "abc", "views": views if views is not None else int(1000 * lift),
            "lift": lift, "length": length, "hook": hook, "rating": rating, "score": 70,
            "posted_at": NOW - days_ago * DAY, "account": "cats"}


def write(rows, path=None):
    path = path or learn.results_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "written_at": NOW, "outcomes": rows}), encoding="utf-8")
    return path


def test_no_file_or_a_broken_one_means_no_lessons(tmp_path):
    assert Lessons.load(tmp_path / "missing.json").empty
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert Lessons.load(bad).empty
    bad.write_text(json.dumps({"outcomes": [{"channel": "Pod", "lift": "lots"}, {"lift": -1}, "x"]}), encoding="utf-8")
    assert Lessons.load(bad).empty


def test_old_results_are_dropped(tmp_path):
    path = write([row(days_ago=10), row(days_ago=200)], tmp_path / "r.json")
    assert len(Lessons.load(path, now=NOW).outcomes) == 1


def test_one_lucky_clip_counts_less_than_several():
    one = Lessons.from_outcomes([Outcome.from_json(row(lift=4))]).channel("Pod")
    many = Lessons.from_outcomes([Outcome.from_json(row(lift=4)) for _ in range(6)]).channel("Pod")
    assert one.posts == 1 and one.lift == pytest.approx(4)
    assert 0 < one.bonus < many.bonus <= learn.MAX_BONUS
    flop = Lessons.from_outcomes([Outcome.from_json(row(lift=0.1)) for _ in range(20)]).channel("pod")
    assert flop.bonus == -learn.MAX_BONUS  # capped


def test_channel_names_match_loosely_and_notes_read_well():
    lessons = Lessons.from_outcomes([Outcome.from_json(row("The Pod Show", 2)) for _ in range(3)]
                                    + [Outcome.from_json(row("Dull Talks", 0.3)) for _ in range(3)])
    points, note = lessons.bonus("the pod show!")
    assert points == pytest.approx(4.8) and note == "Your clips from The Pod Show get 2.0× your usual views (3 posts): +5."
    points, note = lessons.bonus("Dull Talks")
    assert points < -5 and "0.3×" in note and note.endswith(f"{points:.0f}.")
    assert lessons.bonus("Someone Else") == (0.0, "")


def test_decide_ranks_and_filters_with_lessons():
    lessons = Lessons.from_outcomes([Outcome.from_json(row("Hit", 3)) for _ in range(4)]
                                    + [Outcome.from_json(row("Flop", 0.2)) for _ in range(4)])
    videos = [vid("a", score=80, channel="Flop"), vid("b", score=74, channel="Hit"), vid("c", score=70, channel="Flop")]
    policy = Policy(pick_from="any", daily=1)
    plain = decide(videos, policy, PickLog(None), now=NOW)
    assert [d.video.id for d in plain if d.picked] == ["a"]
    learned = decide(videos, policy, PickLog(None), now=NOW, lessons=lessons)
    by_id = {d.video.id: d for d in learned}
    assert [d.video.id for d in learned][0] == "b" and by_id["b"].picked  # moved to the top
    assert "Hit" in by_id["b"].learned and by_id["b"].bonus > 0
    assert by_id["c"].rule == "score" and "after your results" in by_id["c"].reason  # 70 - 8.5 < 65
    assert by_id["a"].rule == "daily_cap" and "Flop" in by_id["a"].learned


def test_hook_examples_need_enough_posts_and_clear_winners():
    few = Lessons.from_outcomes([Outcome.from_json(row(hook="Big", lift=5)) for _ in range(3)])
    assert few.hook_examples() == ([], []) and few.prompt_text() == ""
    rows = [row(hook="He quit on air", lift=4), row(hook="She said what", lift=2), row(hook="Meh", lift=1),
            row(hook="Talking about stuff", lift=0.3), row(hook="Part two", lift=0.5), row(hook="Okay", lift=1.1),
            row(hook="", lift=9)]
    lessons = Lessons.from_outcomes([Outcome.from_json(r) for r in rows])
    good, bad = lessons.hook_examples()
    assert good == [("He quit on air", 4), ("She said what", 2)]
    assert bad == [("Talking about stuff", 0.3), ("Part two", 0.5)]
    text = lessons.prompt_text()
    assert '- worked: "He quit on air" (4.0×)' in text and '- flopped: "Talking about stuff" (0.3×)' in text


def test_length_and_rating_notes():
    rows = [row(length=15, lift=3, rating=8) for _ in range(3)] + [row(length=45, lift=0.8, rating=4) for _ in range(3)]
    lessons = Lessons.from_outcomes([Outcome.from_json(r) for r in rows])
    assert lessons.length_note().startswith("Clips under 20 s do best (3.0×")
    assert "raising the quality check's minimum rating" in lessons.rating_note()
    flat = Lessons.from_outcomes([Outcome.from_json(row(length=n, lift=1, rating=r)) for n in (15, 45) for r in (3, 8)
                                  for _ in range(3)])
    assert flat.length_note() == "" and flat.rating_note() == ""
    upside_down = Lessons.from_outcomes([Outcome.from_json(row(lift=0.5, rating=9)) for _ in range(3)]
                                        + [Outcome.from_json(row(lift=2, rating=3)) for _ in range(3)])
    assert "isn't a good guide" in upside_down.rating_note()


def test_summary_lines():
    assert Lessons().summary_lines() == []
    lessons = Lessons.from_outcomes([Outcome.from_json(row("Hit", 3)) for _ in range(4)]
                                    + [Outcome.from_json(row("Flop", 0.2)) for _ in range(4)])
    lines = lessons.summary_lines()
    assert lines[0].startswith("Learning from 8 posted clips")
    assert "Hit" in lines[1] and "Flop" in lines[2]


def test_for_settings_reads_the_shared_file_only_when_learning_is_on():
    write([row("Hit", 3)])
    assert not learn.for_settings(Settings()).empty
    assert learn.for_settings(Settings(learn_from_results=False)) is None


def test_polish_prompt_carries_the_guide():
    blocks = polish.build_content("T", [Clip(0, 10, 50)], [], [None], guide="How earlier hooks did: ...")
    assert blocks[0]["text"].startswith("How earlier hooks did: ...\n\nVideo title: T")
    assert polish.build_content("T", [Clip(0, 10, 50)], [], [None])[0]["text"].startswith("Video title: T")


def test_engine_polish_passes_hook_examples(monkeypatch):
    write([row(hook=f"Hook {i}", lift=lift) for i, lift in enumerate((5, 4, 1, 1, 0.2, 0.3))])
    seen = {}

    def ask(title, clips, w, src, settings, log=None, check=None, guide="", bottom="", handles=None):
        model = ai.model_for(settings, "polish")
        seen["guide"] = guide
        return polish.Polish({}, False, model)

    monkeypatch.setattr(polish, "ask", ask)
    p = Project("u", video=Video("v", "Big Talk", "Pod", "", 100, "u", Path("src.mp4")),
                measurements=Measurements(100, np.zeros(200)))
    p.clips = [Clip(0, 20, 80)]
    logs = []
    Engine(Settings(ai_api_key="k"), log=logs.append).polish(p)
    assert '"Hook 0" (5.0×)' in seen["guide"] and "The AI sees which of your earlier hooks worked." in logs
    Engine(Settings(ai_api_key="k", learn_from_results=False)).polish(p)
    assert seen["guide"] == ""


def test_cli_results(capsys):
    assert cli.main(["results"]) == 0
    assert "No results yet" in capsys.readouterr().out
    write([row("Hit", 3, hook=f"h{i}") for i in range(4)] + [row("Flop", 0.2, hook=f"f{i}") for i in range(4)])
    assert cli.main(["results"]) == 0
    out = capsys.readouterr().out
    assert "Learning from 8 posted clips" in out and "Hit" in out and "Hooks that worked" in out
    assert cli.main(["results", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["posts"] == 8 and data["channels"][0]["name"] == "Hit" and "worked" in data["polish_guide"]


def test_automation_tab_shows_whats_working(app, tmp_path):
    from ytclip.gui.automation import Automation
    from ytclip.jobs import JobDB

    s = Settings()
    tab = Automation(lambda: s, JobDB(tmp_path / "jobs.db"), launch=lambda: None)
    assert tab.learned.isHidden()
    write([row("Hit", 3) for _ in range(4)])
    tab.refresh()
    assert not tab.learned.isHidden() and "What's working" in tab.learned.text() and "Hit" in tab.learned.text()
    s.learn_from_results = False
    tab.refresh()
    assert "learning is off" in tab.learned.text()
    tab.shutdown()

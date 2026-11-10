"""The learner (intelligence/learner.py): results.json sorted into top performers and underperformers, kept in
an SQLite word index, recalled as matched examples for a prompt, and turned into the Creator's pace. Every
test runs on a fixture file and a fixed clock: no AI, no TikTok."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from ytclip import learn
from ytclip.intelligence import learner as L
from ytclip.settings import Settings

NOW = 1_794_316_800.0  # 2026-11-10
DAY = 86400.0


def row(hook, lift, *, cat="hoops", title="", origin="clip", days=10, words=0, scenes=0, length=40.0, vid=""):
    return {"hook": hook, "lift": lift, "category_id": cat, "clip": title or hook, "origin": origin,
            "posted_at": NOW - days * DAY, "length": length, "words": words, "scenes": scenes,
            "video_id": vid, "views": int(lift * 1000), "account": "acc1"}


TOP = [
    row("He dunked on the whole team", 4.1), row("Nobody expected this buzzer beater", 3.2),
    row("The block that ended the game", 2.6), row("Watch his ankles break live", 2.2),
    row("This dunk broke the internet", 5.0),
    row("Red card in the derby was wild", 3.0, cat="footy"), row("The goal nobody saw coming", 2.4, cat="footy"),
]
BOTTOM = [
    row("Top 10 plays of the week", 0.3), row("Here are some highlights from tonight", 0.4),
    row("A long recap of last season", 0.2, days=60),  # too long ago to avoid (but still remembered)
]
MIDDLE = [row("Just a normal play", 1.1), row("Another decent pass", 0.9)]


def results_file(tmp_path, rows=TOP + BOTTOM + MIDDLE, written=NOW - 3600):
    path = tmp_path / "results.json"
    path.write_text(json.dumps({"version": 1, "written_at": written, "outcomes": rows}), encoding="utf-8")
    return path


def outcomes(rows):
    return [learn.Outcome.from_json(r) for r in rows]


@pytest.fixture
def memory(tmp_path):
    with L.Memory(tmp_path / "learner.db") as mem:
        mem.replace(L.distill(outcomes(TOP + BOTTOM + MIDDLE), {"hoops": "basketball", "footy": "soccer"}), NOW)
        yield mem


# ---------------------------------------------------------------- sorting


@pytest.mark.parametrize("lift, tier", [(2.0, "top"), (5.5, "top"), (1.99, ""), (1.0, ""), (0.51, ""),
                                        (0.5, "bottom"), (0.0, "bottom")])
def test_the_tiers_are_two_times_and_half(lift, tier):
    assert L.tier_of(lift) == tier


def test_distil_keeps_only_what_teaches_something():
    got = L.distill(outcomes(TOP + BOTTOM + MIDDLE), {"hoops": "basketball"})
    assert {e.text for e in got if e.tier == "top"} == {r["hook"] for r in TOP}
    assert {e.text for e in got if e.tier == "bottom"} == {r["hook"] for r in BOTTOM}
    assert not any("normal" in e.text or "decent" in e.text for e in got)
    dunk = next(e for e in got if e.text.startswith("This dunk"))
    assert (dunk.kind, dunk.sport, dunk.category_id, dunk.lift) == ("hook", "basketball", "hoops", 5.0)
    assert [e.lift for e in got if e.tier == "top"] == sorted([e.lift for e in got if e.tier == "top"], reverse=True)
    assert len({e.id for e in got}) == len(got)


def test_a_video_without_a_hook_teaches_with_its_title():
    got = L.distill(outcomes([row("", 3.0, title="Why nobody guards Curry"), row("", 3.0, title="Wow")]))
    assert [(e.text, e.kind) for e in got] == [("Why nobody guards Curry", "title")]  # one word teaches nothing


def test_the_same_words_twice_count_once_at_the_more_telling_lift():
    got = L.distill(outcomes([row("He did it AGAIN!", 2.5), row("he did it again", 6.0),
                              row("Boring recap", 0.4), row("boring recap!", 0.1)]))
    assert [(e.tier, e.lift) for e in got] == [("bottom", 0.1), ("top", 6.0)]


@pytest.mark.parametrize("text, want", [
    ("Why does nobody talk about this?", "a 6-word question"),
    ("You won't believe these 3 dunks", "a 6-word statement with a number and “you”"),
    ("THIS IS INSANE!", "a 3-word exclamation, all caps"),
])
def test_shape_says_how_an_opening_is_built(text, want):
    assert L.shape(text) == want


# ---------------------------------------------------------------- the memory


def test_the_memory_is_sqlite_with_a_word_index(memory, tmp_path):
    assert memory.counts() == {"top": len(TOP), "bottom": len(BOTTOM)}
    con = sqlite3.connect(tmp_path / "learner.db")
    n = con.execute("SELECT COUNT(*) FROM tokens WHERE token = 'dunk'").fetchone()[0]
    assert n == 2  # "dunked" and "dunk" share a stem
    con.close()


def test_replacing_is_all_at_once_and_reopens(tmp_path):
    path = tmp_path / "l.db"
    with L.Memory(path) as mem:
        mem.replace(L.distill(outcomes(TOP)), NOW)
        mem.replace(L.distill(outcomes(BOTTOM)), NOW + 1)
    with L.Memory(path) as mem:
        assert mem.counts() == {"bottom": len(BOTTOM)} and mem.distilled_at() == NOW + 1


def test_search_ranks_shared_rare_words_then_category_and_sport(memory):
    got = memory.search("Insane dunk to win it", "top", category_id="hoops", sport="basketball")
    assert got[0][0].text in ("This dunk broke the internet", "He dunked on the whole team")
    assert all(e.category_id == "hoops" for e, _ in got)  # the soccer ones share no word, category or sport
    footy = memory.search("Late winner in the derby", "top", category_id="footy", sport="soccer")
    assert footy[0][0].text == "Red card in the derby was wild"
    assert [s for _, s in got] == sorted([s for _, s in got], reverse=True)


def test_search_skips_what_has_nothing_in_common(memory):
    assert memory.search("cooking pasta at home", "top") == []


def test_search_can_keep_to_recent_ones(memory):
    old = memory.search("recap of the season", "bottom", since=NOW - L.AVOID_DAYS * DAY)
    assert not any("last season" in e.text for e, _ in old)
    assert any("last season" in e.text for e, _ in memory.search("recap of the season", "bottom"))


# ---------------------------------------------------------------- the prompt


def test_recall_gives_three_to_five_examples_and_what_to_avoid(memory):
    shot = L.recall("Best dunk of the game", category_id="hoops", sport="basketball", memory=memory, now=NOW)
    assert 3 <= len(shot.good) <= 5 and all(e.tier == "top" for e in shot.good)
    assert 1 <= len(shot.bad) <= L.AVOID and all(e.tier == "bottom" for e in shot.bad)
    text = shot.text()
    assert "did best on your TikTok accounts" in text and "Avoid structures similar to" in text
    assert '"This dunk broke the internet" (5.0×)' in text
    assert "Don't copy their words" in text
    assert "last season" not in text  # flopped too long ago to count


def test_one_or_two_lucky_posts_are_no_pattern(tmp_path):
    with L.Memory(tmp_path / "l.db") as mem:
        mem.replace(L.distill(outcomes(TOP[:2] + BOTTOM[:1])), NOW)
        shot = L.recall("dunk", category_id="hoops", memory=mem, now=NOW)
    assert shot.good == [] and len(shot.bad) == 1
    assert "did best" not in shot.text() and "Avoid structures" in shot.text()


def test_nothing_learned_is_no_paragraph(tmp_path):
    with L.Memory(tmp_path / "l.db") as mem:
        assert L.recall("anything", memory=mem, now=NOW).text() == ""


@pytest.mark.parametrize("purpose, word", [("hook", "hooks"), ("script", "openings and titles"),
                                           ("reel", "reel hooks")])
def test_the_paragraph_names_what_its_for(memory, purpose, word):
    text = L.recall("dunk game", category_id="hoops", memory=memory, now=NOW, purpose=purpose).text()
    assert f"Earlier {word} that did best" in text


def test_guide_distils_when_due_and_then_recalls(tmp_path):
    results = results_file(tmp_path)
    with L.Memory(tmp_path / "l.db") as mem:
        text = L.guide(Settings(), "dunk of the night", category_id="hoops", sport="basketball", memory=mem,
                       results=results, now=NOW)
        assert "This dunk broke the internet" in text and mem.distilled_at() == NOW


def test_guide_is_silent_when_learning_is_off_or_broken(tmp_path, monkeypatch):
    results = results_file(tmp_path)
    with L.Memory(tmp_path / "l.db") as mem:
        assert L.guide(Settings(learn_from_results=False), "dunk", memory=mem, results=results, now=NOW) == ""

        def broken(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(L, "recall", broken)
        assert L.guide(Settings(), "dunk", memory=mem, results=results, now=NOW) == ""


def test_guide_with_no_results_yet_is_empty(tmp_path):
    with L.Memory(tmp_path / "l.db") as mem:
        assert L.guide(Settings(), "dunk", memory=mem, results=tmp_path / "none.json", now=NOW) == ""


# ---------------------------------------------------------------- every 24 hours


def test_refresh_runs_once_a_day_or_when_posting_writes_new_results(tmp_path):
    results = results_file(tmp_path, written=NOW - 3600)
    with L.Memory(tmp_path / "l.db") as mem:
        assert mem.due(NOW)  # never distilled
        assert L.refresh(memory=mem, results=results, now=NOW) == len(TOP) + len(BOTTOM)
        assert L.refresh(memory=mem, results=results, now=NOW + 3600) is None  # not due
        assert L.refresh(memory=mem, results=results, now=NOW + L.EVERY) is not None  # a day later
        results_file(tmp_path, rows=TOP, written=NOW + L.EVERY + 60)  # posting read new stats
        assert L.refresh(memory=mem, results=results, now=NOW + L.EVERY + 120) == len(TOP)
        assert L.refresh(memory=mem, results=results, now=NOW + L.EVERY + 180, force=True) == len(TOP)


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", '{"outcomes": "no"}', '{"outcomes": [{"lift": "x"}]}'])
def test_a_missing_or_broken_results_file_teaches_nothing_and_doesnt_raise(tmp_path, content):
    path = tmp_path / "results.json"
    path.write_text(content, encoding="utf-8")
    with L.Memory(tmp_path / "l.db") as mem:
        assert L.refresh(memory=mem, results=path, now=NOW) == 0
        assert mem.counts() == {}


def test_results_from_before_3_2_still_count_by_their_niche_key(tmp_path):
    old = [dict(r, niche=r["category_id"]) for r in TOP]
    for r in old:
        del r["category_id"]
    with L.Memory(tmp_path / "l.db") as mem:
        L.refresh(memory=mem, results=results_file(tmp_path, rows=old), now=NOW)
        assert {e.category_id for e in mem.all("top")} == {"hoops", "footy"}


def test_the_sport_comes_from_the_categories(tmp_path):
    s = Settings().apply({"categories": [{"id": "hoops", "name": "NBA", "sport": "basketball"},
                                         {"id": "footy", "name": "Football"}]})
    assert L.sports_of(s).get("hoops") == "basketball" and L.sports_of(None) == {}


# ---------------------------------------------------------------- pacing


def creator(lift, wpm, per, cat="facts", length=70.0):
    words = round(wpm * length / 60)
    return row(f"made {lift} {wpm}", lift, cat=cat, origin="creator", words=words, scenes=round(length / per),
               length=length)


def test_pace_needs_two_top_creator_videos_with_their_script_known():
    assert L.calibrate(outcomes([creator(3.0, 170, 4)])).wpm is None
    no_script = [dict(creator(3.0, 170, 4), words=0)] * 3
    assert L.calibrate(outcomes(no_script)).wpm is None
    clips = [row("clip", 3.0, length=40, words=200)] * 3  # not Creator videos
    assert L.calibrate(outcomes(clips)).wpm is None


def test_pace_moves_toward_the_best_videos_gently_at_first():
    two = L.calibrate(outcomes([creator(3.0, 172, 4.0), creator(2.5, 168, 4.0)]), default_wpm=150)
    pull = 2 / (2 + L.PACE_SHRINK)
    assert two.wpm == pytest.approx(150 + (170 - 150) * pull, abs=0.2)
    assert two.scene_seconds == pytest.approx(L.DEFAULT_SCENE + (4.0 - L.DEFAULT_SCENE) * pull, abs=0.05)
    many = L.calibrate(outcomes([creator(3.0, 170, 4.0)] * 12), default_wpm=150)
    assert many.wpm > two.wpm and many.scene_seconds < two.scene_seconds
    assert "best Creator video" in many.note


def test_pace_stays_natural():
    fast = L.calibrate(outcomes([creator(4.0, 260, 1.0)] * 30))
    assert fast.wpm <= L.WPM_RANGE[1] and fast.scene_seconds >= L.SCENE_RANGE[0]
    slow = L.calibrate(outcomes([creator(4.0, 80, 15.0)] * 30))
    assert slow.wpm >= L.WPM_RANGE[0] and slow.scene_seconds <= L.SCENE_RANGE[1]


def test_a_category_with_enough_of_its_own_uses_its_own():
    rows = [creator(3.0, 175, 4, cat="facts")] * 3 + [creator(3.0, 135, 7, cat="calm")] * 3
    assert L.calibrate(outcomes(rows), "calm").wpm < L.calibrate(outcomes(rows), "facts").wpm
    mixed = L.calibrate(outcomes(rows), "new category")
    assert L.calibrate(outcomes(rows), "calm").wpm < mixed.wpm < L.calibrate(outcomes(rows), "facts").wpm


def test_the_note_says_which_way_the_flops_went():
    rows = [creator(3.0, 170, 4)] * 3 + [creator(0.3, 140, 7)] * 2
    assert "flopped were slower" in L.calibrate(outcomes(rows)).note


def test_pacing_is_off_when_learning_is_off(tmp_path):
    path = results_file(tmp_path, rows=[creator(3.0, 170, 4)] * 3)
    assert L.pacing(Settings(learn_from_results=False), results=path, now=NOW).wpm is None
    assert L.pacing(Settings(), results=path, now=NOW).wpm is not None


def test_the_creator_brief_follows_the_learned_pace(tmp_path, monkeypatch):
    """orchestrator.storyboard: the pace goes into the brief, held within 15% of the voice's own."""
    from ytclip.creator import director, orchestrator

    path = results_file(tmp_path, rows=[creator(3.0, 178, 3.2)] * 20)
    monkeypatch.setattr(learn, "results_path", lambda: path)
    seen = {}

    def write(s, brief, **kw):
        seen["brief"] = brief
        raise director.DirectorError("stop here")

    monkeypatch.setattr(director, "write", write)
    s = Settings().apply({"output_dir": str(tmp_path / "Out"), "creator_rewards_length": True})
    maker = orchestrator.VideoCreatorOrchestrator(s, register=False)
    with pytest.raises(orchestrator.CreatorError):
        maker.storyboard(orchestrator.CreateRequest(prompt="space facts", seconds=65))
    brief = seen["brief"]
    assert 0 < brief.scene_seconds < L.DEFAULT_SCENE
    assert brief.wpm > 150 * 0.99


# ---------------------------------------------------------------- posting writes what pacing needs


def test_posting_keeps_a_creator_videos_words_and_scenes():
    from ytclip.posting import results

    made = SimpleNamespace(ai_generated=True, facts={"creator": {"script": "one two three four", "scenes": 9}})
    assert results._pace(made) == {"words": 4, "scenes": 9}
    assert results._pace(SimpleNamespace(ai_generated=False, facts={"creator": {"script": "x"}})) == {}
    assert results._pace(SimpleNamespace(ai_generated=True, facts={"creator": {"script": "a b", "scenes": "?"}})) == \
        {"words": 2, "scenes": 0}
    o = learn.Outcome.from_json({"lift": 2.0, "words": "171", "scenes": -3})
    assert (o.words, o.scenes) == (171, 0)

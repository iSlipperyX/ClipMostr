"""The fact check after the script is written (creator/factcheck.py): verdicts from the AI (faked here), which
sentences get rewritten and which are only marked, the guard rails on rewrites, and where the results show up."""

import json

from ytclip import ai
from ytclip.creator import director, factcheck, orchestrator
from ytclip.settings import Settings

SOURCES = [{"title": "GTA 6 release date confirmed for November 19, 2026", "url": "https://www.ign.com/a",
            "publisher": "IGN", "date": "2026-11-06T14:00:00Z", "kind": "news"},
           {"title": "Rockstar shows GTA 6 trailer 3", "url": "https://www.theverge.com/b", "publisher": "The Verge",
            "date": "2026-10-21T09:30:00Z", "kind": "news"}]


def board():
    b = director.Storyboard(title="GTA 6 news", hook="GTA 6 finally has a date", scenes=[
        director.Scene(1, "GTA 6 finally has a date."),
        director.Scene(2, "Rockstar says it launches on May 26, 2026. Pre-orders open soon."),
        director.Scene(3, "It will cost 70 dollars. Fans are excited."),
        director.Scene(4, "Trailer 3 dropped in October."),
    ])
    b.sources = list(SOURCES)
    return b


CLAIMS = [
    {"n": 2, "verdict": "contradicted", "source": 1, "why": "IGN: November 19, 2026",
     "fix": "Rockstar says it launches on November 19, 2026."},
    {"n": 4, "verdict": "unsupported", "source": 0, "why": "no price in the sources",
     "fix": "The price hasn't been confirmed yet."},
    {"n": 5, "verdict": "unsupported", "source": 0, "why": "", "fix": "Some fans are excited."},
    {"n": 6, "verdict": "supported", "source": 2, "why": "The Verge, October 21"},
    {"n": 6, "verdict": "contradicted", "fix": "a duplicate is ignored"},
    {"n": 99, "verdict": "contradicted", "fix": "no such sentence"},
    {"n": "x", "verdict": "supported"},
]


def asker(claims, calls=None):
    def ask(s, task, system, prompt, schema, **kw):
        if calls is not None:
            calls.append(prompt)
        return ai.Answer({"claims": claims}, "local-model", "local")

    return ask


def test_numbered_sentences():
    lines = factcheck.numbered(board())
    assert [n for n, _, _ in lines] == [1, 2, 3, 4, 5, 6]
    assert lines[2] == (3, 1, "Pre-orders open soon.")


def test_fix_mode_rewrites_contradicted_and_unconfirmed_specifics():
    b, calls = board(), []
    rep = factcheck.check(Settings(), b, "Today's date is 8 November 2026. [1] IGN ...", have_sources=True,
                          ask=asker(CLAIMS, calls))
    assert "1. GTA 6 finally has a date." in calls[0] and "[1] IGN" in calls[0]
    assert b.scenes[1].speech_text == "Rockstar says it launches on November 19, 2026. Pre-orders open soon."
    assert b.scenes[2].speech_text == "The price hasn't been confirmed yet. Fans are excited."  # vague: kept
    assert b.scenes[3].speech_text == "Trailer 3 dropped in October."
    assert [c.n for c in rep.fixed] == [2, 4] and [c.n for c in rep.flagged] == [5]
    assert [c.n for c in rep.supported] == [6]
    assert rep.summary() == "Fact check (local-model): 1 confirmed, 2 fixed, 1 to check yourself."
    b.factcheck = rep.to_json()
    again = director.Storyboard.from_json(json.loads(b.dumps()))
    assert again.factcheck["claims"][0]["changed"] is True  # kept with the storyboard
    marks = factcheck.marks(again)
    assert set(marks) == {2, 3} and len(marks[3]) == 2


def test_flag_mode_changes_nothing():
    b = board()
    before = b.script
    rep = factcheck.check(Settings(), b, "facts", have_sources=True, mode="flag", ask=asker(CLAIMS))
    assert b.script == before and not rep.fixed and [c.n for c in rep.flagged] == [2, 4, 5]


def test_no_sources_no_check():
    calls = []
    rep = factcheck.check(Settings(), board(), "Today's date is ...", have_sources=False, ask=asker(CLAIMS, calls))
    assert not calls and rep.skipped and "nothing was found online" in rep.summary()
    assert factcheck.check(Settings(), board(), "x", have_sources=True, mode="off", ask=asker(CLAIMS, calls)).skipped
    assert not calls


def test_the_ai_failing_keeps_the_script():
    def ask(*a, **k):
        raise ai.AIError("the local AI isn't ready")

    b = board()
    before = b.script
    rep = factcheck.check(Settings(), b, "facts", have_sources=True, ask=ask)
    assert b.script == before and "couldn't check it" in rep.skipped


def test_rewrites_that_cant_be_used():
    long_hook = {"n": 1, "verdict": "contradicted", "fix": "GTA 6 has a date and it is November 19 2026 says "
                                                           "Rockstar in a post"}
    rambling = {"n": 2, "verdict": "contradicted", "fix": " ".join(["word"] * 40) + "."}
    same = {"n": 4, "verdict": "unsupported", "fix": "It will cost 70 dollars"}
    b = board()
    before = b.script
    rep = factcheck.check(Settings(), b, "facts", have_sources=True, ask=asker([long_hook, rambling, same]))
    assert b.script == before and not rep.fixed and len(rep.flagged) == 3


def test_a_fixed_hook_changes_the_hook_card():
    b = board()
    factcheck.check(Settings(), b, "facts", have_sources=True,
                    ask=asker([{"n": 1, "verdict": "contradicted", "fix": "GTA 6 lands November 19."}]))
    assert b.scenes[0].speech_text == "GTA 6 lands November 19." and b.hook == "GTA 6 lands November 19"


def test_readme_lists_fixes_and_marks_with_sources():
    b = board()
    b.factcheck = factcheck.check(Settings(), b, "facts", have_sources=True, ask=asker(CLAIMS)).to_json()
    text = orchestrator.readme(b, {"pictures": []}, {"length": 64.0}, "01.mp4")
    assert "## Fact check" in text and "1 statement confirmed" in text
    assert '**Fixed** (scene 2): "Rockstar says it launches on May 26, 2026."' in text
    assert "[IGN](<https://www.ign.com/a>)" in text
    assert '**To check** (scene 3, unsupported): "Fans are excited."' in text
    b.factcheck = {"mode": "fix", "skipped": "nothing was found online to check against", "claims": []}
    assert "Not run: nothing was found online" in factcheck.readme_section(b)


def test_the_creator_checks_what_it_wrote(monkeypatch):
    from test_creator_web import ALL, _good_json, opener_for

    from ytclip.creator import web

    real_gather = web.gather
    monkeypatch.setattr(web, "gather", lambda prompt, log=None, check=None:
                        real_gather(prompt, log=log, check=check, opener=opener_for(ALL)))
    monkeypatch.setattr(director, "write", lambda s, brief, **kw:
                        director.normalise(director.Storyboard.from_json(_good_json())))
    seen = {}

    def ask(s, task, system, prompt, schema, **kw):
        seen["prompt"] = prompt
        return ai.Answer({"claims": [{"n": 1, "verdict": "supported", "source": 1}]}, "m", "local")

    monkeypatch.setattr(ai, "ask_json", ask)
    maker = orchestrator.VideoCreatorOrchestrator(Settings())
    b = maker.storyboard(orchestrator.CreateRequest(prompt="the latest GTA 6 news"))
    assert "November 19, 2026" in seen["prompt"] and "1. GTA 6 finally has a date." in seen["prompt"]
    assert b.factcheck["claims"][0]["verdict"] == "supported" and b.factcheck["model"] == "m"
    seen.clear()
    maker = orchestrator.VideoCreatorOrchestrator(Settings().apply({"creator_factcheck": "off"}))
    b = maker.storyboard(orchestrator.CreateRequest(prompt="the latest GTA 6 news"))
    assert not seen and b.factcheck == {}

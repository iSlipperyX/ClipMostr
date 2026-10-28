"""Find videos for a typed request (lookup.py): the searches it makes, how it scores what comes back."""

import json
import urllib.parse

import pytest
from conftest import NOW, api_item

from ytclip import cli, lookup
from ytclip.ai import AIError
from ytclip.settings import Settings
from ytclip.trending import ResearchBlocked

# ---------------------------------------------------------------- the request → searches


@pytest.mark.parametrize("request_, queries, subject", [
    ("a Call of Duty MW4 Video with good comedy", ["Call of Duty MW4 funny moments", "Call of Duty MW4"],
     ["call", "duty", "mw4"]),
    ("Example a Call of Duty MW4 Video with good comedy", ["Call of Duty MW4 funny moments", "Call of Duty MW4"],
     ["call", "duty", "mw4"]),
    ("find me funny Minecraft hardcore videos", ["Minecraft hardcore funny moments", "Minecraft hardcore"],
     ["minecraft", "hardcore"]),
    ("Fortnite clutch moments", ["Fortnite clutch moments", "Fortnite"], ["fortnite"]),
    ("Kai Cenat stream with rage", ["Kai Cenat rage moments", "Kai Cenat"], ["kai", "cenat"]),
    ("MrBeast challenge videos", ["MrBeast challenge videos"], ["mrbeast", "challenge"]),
    ("Dungeons & Dragons live play", ["Dungeons & Dragons live play"], ["dungeons", "dragons", "live", "play"]),
    ("funny fails", ["funny fails"], []),
])
def test_the_request_becomes_searches(request_, queries, subject):
    plan = lookup.plain_plan(request_)
    assert plan.queries == queries and plan.subject == subject and not plan.by_ai


def test_comedy_is_matched_by_the_words_funny_videos_use():
    plan = lookup.plain_plan("Call of Duty MW4 with good comedy")
    assert {"funny", "hilarious", "comedy"} <= set(plan.vibe)


def test_an_empty_request_says_what_to_type():
    with pytest.raises(lookup.FindError, match="Call of Duty"):
        lookup.plain_plan("  find me  ")


# ---------------------------------------------------------------- one request against a fake YouTube


class FakeYouTube:
    """search.list and videos.list, from a table of videos; records what was asked."""

    def __init__(self, videos, results=None):
        self.videos = {v["id"]: v for v in videos}
        self.results = results or {}  # query -> ids (default: every video)
        self.asked = []

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        self.asked.append((parsed.path.rsplit("/", 1)[-1], q))
        if parsed.path.endswith("/search"):
            ids = self.results.get(q["q"], list(self.videos))
            return {"items": [{"id": {"videoId": i}} for i in ids]}
        return {"items": [self.videos[i] for i in q["id"].split(",") if i in self.videos]}


def _item(vid, title, minutes=20.0, tags=(), description="", **kw):
    item = api_item(vid, title=title, minutes=minutes, **kw)
    item["snippet"]["tags"] = list(tags)
    item["snippet"]["description"] = description
    return item


VIDEOS = [
    _item("mw4fun", "Call of Duty MW4 FUNNY MOMENTS #3 (trolling lobbies)", views=900_000),
    _item("mw4camp", "MW4 ranked grind - full match", tags=["call of duty", "mw4"], views=50_000),
    _item("bf6fun", "Battlefield 6 funny moments", views=2_000_000),
    _item("mw4short", "MW4 funny clip #shorts", minutes=0.5),
    _item("mw4rev", "Call of Duty MW4 review: is it worth it?", minutes=14, description="Honest review"),
]


def test_a_request_finds_matching_videos_best_first(tmp_path):
    yt = FakeYouTube(VIDEOS)
    logs = []
    r = lookup.find("a Call of Duty MW4 video with good comedy", Settings(), "KEY", log=logs.append, get=yt,
                    now=NOW)
    ids = [v.id for v in r.videos]
    assert ids[0] == "mw4fun"  # about MW4 and funny
    assert "bf6fun" not in ids and r.weak >= 1  # funny, but another game: left out
    assert "mw4short" not in ids and any("shorter than 1 min" in m for m in logs)  # a Short has nothing to cut
    assert all(v.match >= lookup.WEAK and v.origins == ["request"] for v in r.videos)
    assert r.videos[0].reasons[0].startswith("Match ") and "“mw4”" in r.videos[0].match_why
    searches = [q for path, q in yt.asked if path == "search"]
    assert [q["q"] for q in searches] == ["Call of Duty MW4 funny moments", "Call of Duty MW4"]
    assert all(q["type"] == "video" and q["order"] == "relevance" for q in searches)
    assert r.quota == 2 * lookup.SEARCH_COST + 1 and not r.rated_by


def test_how_recent_and_what_language(tmp_path):
    yt = FakeYouTube(VIDEOS)
    lookup.find("MW4 funny", Settings(), "KEY", since="week", language="en", get=yt, now=NOW)
    q = next(q for path, q in yt.asked if path == "search")
    assert q["publishedAfter"].endswith("Z") and q["relevanceLanguage"] == "en"


def test_a_shorter_skip_length_comes_from_research(tmp_path):
    long_only = lookup.find("MW4 funny", Settings(), "KEY", min_minutes=15, get=FakeYouTube(VIDEOS), now=NOW)
    assert "mw4rev" not in [v.id for v in long_only.videos]


def test_a_bad_key_or_used_up_quota_stops_it():
    def blocked(url):
        raise ResearchBlocked("Today's YouTube API quota is used up.")

    with pytest.raises(ResearchBlocked, match="quota"):
        lookup.find("MW4 funny", Settings(), "KEY", get=blocked)
    with pytest.raises(lookup.FindError, match="API key"):
        lookup.find("MW4 funny", Settings(), "")


def test_nothing_found_is_an_empty_result():
    r = lookup.find("MW4 funny", Settings(), "KEY", get=FakeYouTube([]), now=NOW)
    assert r.videos == [] and r.quota == 2 * lookup.SEARCH_COST


# ---------------------------------------------------------------- with Claude


@pytest.fixture
def claude(monkeypatch):
    calls = []
    state = {"fail": ""}

    def ask(prompt, system, schema, s, check=None, max_tokens=0):
        calls.append(prompt)
        if state["fail"]:
            raise AIError(state["fail"])
        if "queries" in schema["properties"]:
            return {"queries": ["Modern Warfare 4 funny moments", "MW4 funny moments"],
                    "subject": ["modern warfare 4", "mw4", "call of duty"], "vibe": ["funny", "trolling"],
                    "about": "Funny Call of Duty: Modern Warfare 4 gameplay"}, "claude-test"
        lines = [ln for ln in prompt.splitlines() if ln[:1].isdigit()]
        return {"videos": [{"n": int(ln.split(".")[0]), "match": 9 if "FUNNY" in ln else 6 if "MW4" in ln else 1,
                            "why": "test"} for ln in lines]}, "claude-test"

    monkeypatch.setattr(lookup, "_ask", ask)
    return calls, state


def test_claude_writes_the_searches_and_rates_the_matches(claude):
    calls, _ = claude
    yt = FakeYouTube(VIDEOS)
    r = lookup.find("MW4 with good comedy", Settings(ai_api_key="sk-ant-x"), "KEY", get=yt, now=NOW)
    assert [q["q"] for p, q in yt.asked if p == "search"] == ["Modern Warfare 4 funny moments", "MW4 funny moments"]
    assert r.plan.by_ai and r.plan.about.startswith("Funny") and r.rated_by == "claude-test"
    assert r.videos[0].id == "mw4fun" and r.videos[0].match == 90
    assert "bf6fun" not in [v.id for v in r.videos]  # Claude rated it 1/10
    assert len(calls) == 2 and "Understood as: Funny" in calls[1]


def test_without_claude_it_still_finds_them_by_their_words(claude):
    _, state = claude
    state["fail"] = "Anthropic's servers are busy"
    r = lookup.find("MW4 with good comedy", Settings(ai_api_key="sk-ant-x"), "KEY", get=FakeYouTube(VIDEOS),
                    now=NOW)
    assert r.videos[0].id == "mw4fun" and not r.plan.by_ai and not r.rated_by
    assert any("couldn't plan" in p for p in r.problems) and any("couldn't rate" in p for p in r.problems)


def test_no_ai_key_means_no_claude(claude):
    calls, _ = claude
    lookup.find("MW4 with good comedy", Settings(), "KEY", get=FakeYouTube(VIDEOS), now=NOW)
    assert calls == []


# ---------------------------------------------------------------- remembered


def test_requests_and_their_results_are_remembered(tmp_path):
    path = tmp_path / "finds.json"
    for text in ("MW4 funny", "Fortnite clutch", "mw4 FUNNY"):
        r = lookup.find(text, Settings(), "KEY", get=FakeYouTube(VIDEOS), now=NOW)
        lookup.remember(r, path)
    assert lookup.recent_requests(path) == ["mw4 FUNNY", "Fortnite clutch"]  # the same request once, newest first
    back = lookup.last_result(path)
    assert back.request == "mw4 FUNNY" and [v.id for v in back.videos] == [v.id for v in r.videos]
    assert back.videos[0].match == r.videos[0].match and back.plan.queries == r.plan.queries
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    assert lookup.last_result(tmp_path / "bad.json") is None and lookup.recent_requests(tmp_path / "bad.json") == []


# ---------------------------------------------------------------- ytclip find


def test_ytclip_find_lists_and_queues(monkeypatch, capsys):
    yt = FakeYouTube(VIDEOS)
    real = lookup.find
    monkeypatch.setattr(lookup, "find", lambda *a, **k: real(*a, **(k | {"get": yt, "now": NOW})))
    Settings(youtube_api_key="KEY").save()
    assert cli.main(["find", "Call of Duty MW4 with good comedy", "--json", "--queue", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["videos"][0]["url"].endswith("mw4fun") and out["searched"][0] == "Call of Duty MW4 funny moments"
    from ytclip.jobs import JobDB

    db = JobDB()
    try:
        jobs = db.jobs()
        assert [j.url for j in jobs] == ["https://www.youtube.com/watch?v=mw4fun"]
        assert jobs[0].permission == "Added by you" and jobs[0].title.startswith("Call of Duty MW4 FUNNY")
    finally:
        db.close()
    assert cli.main(["find", "MW4 funny", "--niche", "nope"]) == 2


def test_find_uses_whichever_ai_settings_chose(monkeypatch):
    from ytclip import ai

    asked = []

    def ask_json(s, task, system, content, schema, **kw):
        asked.append((task, s.ai_provider))
        if "queries" in schema["properties"]:
            return ai.Answer({"queries": ["MW4 funny moments"], "subject": ["mw4"], "vibe": ["funny"],
                              "about": "x"}, "gemini-test", "gemini")
        return ai.Answer({"videos": [{"n": 1, "match": 9, "why": "yes"}]}, "gemini-test", "gemini")

    monkeypatch.setattr(ai, "ask_json", ask_json)
    s = Settings(ai_provider="gemini", gemini_api_key="g-key")
    r = lookup.find("MW4 with good comedy", s, "KEY", get=FakeYouTube(VIDEOS[:1]), now=NOW)
    assert asked == [("polish", "gemini"), ("polish", "gemini")] and r.rated_by == "gemini-test"
    assert r.plan.by_ai and r.videos[0].match == 90
    asked.clear()
    lookup.find("MW4 with good comedy", Settings(), "KEY", get=FakeYouTube(VIDEOS[:1]), now=NOW)
    assert asked == []  # no key for either AI: by the words

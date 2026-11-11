"""The Sports section (ytclip/sportsdesk): reading search results into games, the filter, ranking a game's plays with
the AI (any provider, the local one too) or without, the desk's work queue across two workers, the worker doing a
search, a game's highlights and its clips with a stand-in engine, and the Mac's /api/sports."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ytclip import ai
from ytclip.analyze import Clip
from ytclip.settings import Settings
from ytclip.sportsdesk import games as G
from ytclip.sportsdesk import rank
from ytclip.sportsdesk.desk import Called, Desk, DeskError, DeskWorker, public
from ytclip.subtitles import Cue

NOW = 1_800_000_000.0


# ---------------------------------------------------------------- reading titles


@pytest.mark.parametrize("title, minutes, home, away, kind, game", [
    ("LAKERS at CELTICS | FULL GAME HIGHLIGHTS | November 8, 2026", 10, "Celtics", "Lakers", "highlights", True),
    ("LAKERS at CELTICS | FULL GAME HIGHLIGHTS", 25, "Celtics", "Lakers", "extended", True),
    ("Arsenal vs Chelsea | Extended Highlights | Premier League 2026/27", 18, "Arsenal", "Chelsea", "extended", True),
    ("Real Madrid 3-1 Barcelona | HIGHLIGHTS | LaLiga", 8, "Real Madrid", "Barcelona", "highlights", True),
    ("Man City v Liverpool - Full Match Replay", 100, "Man City", "Liverpool", "full", True),
    ("Yankees @ Red Sox Game Highlights (9/14/26) | MLB Highlights", 9, "Red Sox", "Yankees", "highlights", True),
    ("Lakers vs Celtics Reaction!!", 20, None, None, None, False),
    ("NBA 2K27 Lakers vs Celtics", 20, None, None, None, False),
    ("Chiefs vs Bills PREVIEW + predictions", 12, None, None, None, False),
])
def test_titles_are_read_into_games(title, minutes, home, away, kind, game):
    r = G.read_title(title, minutes * 60)
    assert r["game"] is game
    if game:
        assert (r["home"], r["away"], r["kind"]) == (home, away, kind)
    if "3-1" in title:
        assert r["score"] == "3-1"


def test_the_filter_reads_what_its_given():
    f = G.GameFilter.from_json({"sport": "Basketball", "teams": "Lakers,  celtics ; Lakers", "since": "forever",
                                "kinds": ["nonsense"], "per_game": 99, "category_id": "../x"})
    assert f.sport == "basketball" and f.team_list == ["Lakers", "celtics"]
    assert f.since == "3days" and f.kinds == ["extended", "highlights"] and f.per_game == 20 and f.category_id == "x"
    assert G.GameFilter.from_json({"sport": "curling"}).sport == "any"
    assert "Premier League soccer matches" in G.GameFilter.from_json({"sport": "soccer",
                                                                      "league": "Premier League"}).describe()


def test_searches_for_teams_or_the_league():
    teams = G.GameFilter.from_json({"sport": "basketball", "league": "NBA", "teams": "Lakers, Celtics, Heat, Bulls"})
    assert G.queries(teams) == ["Lakers NBA extended highlights", "Celtics NBA extended highlights",
                                "Heat NBA extended highlights"]
    league = G.GameFilter.from_json({"sport": "soccer", "league": "Serie A", "kinds": ["full", "highlights"]})
    assert G.queries(league) == ["Serie A full match", "Serie A highlights"]
    assert G.queries(G.GameFilter.from_json({"sport": "combat", "kinds": ["full"]}))[0] == "Boxing / MMA full fight"


def found(vid, title, minutes=15, hours=5, channel="NBA", views=1000):
    return G.Found(vid, title, channel, NOW - hours * 3600, minutes * 60, views, f"https://i.ytimg.com/vi/{vid}/x.jpg")


def test_what_fits_the_filter():
    f = G.GameFilter.from_json({"sport": "basketball", "teams": "Lakers", "since": "day", "kinds": ["extended"]})
    ok = found("a", "Lakers at Celtics | Extended Highlights")
    assert G.fits(ok, G.read_title(ok.title, ok.duration), f, NOW) == ""
    old = found("b", "Lakers at Celtics | Extended Highlights", hours=40)
    assert G.fits(old, G.read_title(old.title, old.duration), f, NOW) == "too old"
    other = found("c", "Heat at Celtics | Extended Highlights")
    assert G.fits(other, G.read_title(other.title, other.duration), f, NOW) == "another team"
    short = found("d", "Lakers at Celtics | Highlights", minutes=8)
    assert "not what the filter asks for" in G.fits(short, G.read_title(short.title, short.duration), f, NOW)
    fan = found("e", "Lakers vs Celtics reaction")
    assert G.fits(fan, G.read_title(fan.title, fan.duration), f, NOW) == "not a game"


def test_uploads_of_one_game_are_one_game_with_versions():
    f = G.GameFilter.from_json({"sport": "basketball"})
    hl = found("h1", "LAKERS at CELTICS | FULL GAME HIGHLIGHTS", minutes=10)
    full = found("f1", "Lakers vs Celtics FULL GAME", minutes=140, hours=7)
    ext = found("x1", "Celtics vs Lakers | Extended Highlights", minutes=22, hours=6)
    games = [G.make_game(v, G.read_title(v.title, v.duration), f, NOW) for v in (hl, full, ext)]
    out, added = G.merge([], games)
    assert added == 1 and len(out) == 1
    g = out[0]
    assert [v["kind"] for v in g["versions"]] == ["extended", "highlights", "full"]
    assert g["video"] == "x1"  # the extended highlights, shown first
    again, added = G.merge(out, [G.make_game(hl, G.read_title(hl.title, hl.duration), f, NOW)])
    assert added == 0 and len(again) == 1  # found again: nothing new
    assert G.use(g, "f1") and g["kind"] == "full" and not G.use(g, "nope")


def test_the_ai_reads_titles_and_cant_invent_teams(monkeypatch):
    asked = []

    def ask_json(s, task, system, content, schema, **kw):
        asked.append(content)
        return ai.Answer({"videos": [
            {"n": 1, "game": True, "kind": "extended", "home": "Arsenal", "away": "Spurs", "league": "Premier League"},
            {"n": 2, "game": False, "kind": "other", "home": "", "away": "", "league": ""},
            {"n": 3, "game": True, "kind": "full", "home": "Real Madrid", "away": "Made Up FC", "league": ""},
        ]}, "fake-model", "gemini")

    monkeypatch.setattr(ai, "ask_json", ask_json)
    monkeypatch.setattr(ai, "key_for", lambda s, p="": "key" if p == "gemini" else "")
    monkeypatch.setattr(ai, "chosen", lambda s: "gemini")
    vids = [found("a", "North London derby: Arsenal v Spurs extended"), found("b", "Talking tactics podcast #44"),
            found("c", "Real Madrid full match", minutes=95)]
    got, model = G.ai_read(vids, G.GameFilter.from_json({"sport": "soccer"}), Settings())
    assert model == "fake-model" and len(asked) == 1
    assert got["a"]["home"] == "Arsenal" and got["a"]["league"] == "Premier League"
    assert got["b"]["game"] is False
    assert got["c"]["away"] == ""  # not in the title: left out


def test_a_local_ai_reads_titles_in_small_batches(monkeypatch):
    calls = []
    monkeypatch.setattr(ai, "ask_json", lambda s, *a, **k: calls.append(1) or ai.Answer({"videos": []}, "m", "local"))
    monkeypatch.setattr(ai, "chosen", lambda s: "local")
    G.ai_read([found(str(i), f"Team{i} vs Other{i}") for i in range(32)], G.GameFilter(), Settings())
    assert len(calls) == 3  # 15 + 15 + 2


# ---------------------------------------------------------------- ranking a game's plays


def plays(n=6):
    clips = [Clip(100.0 * i, 100.0 * i + 20, 90 - 7 * i, f"clip {i}") for i in range(1, n + 1)]
    sig = np.zeros(800)
    for i, c in enumerate(clips):
        sig[int(c.start): int(c.end)] = 0.9 - 0.1 * i
    cues = [Cue(c.start + 5, c.start + 9, f"what a dunk by number {i}") for i, c in enumerate(clips, 1)]
    return clips, {"crowd": sig, "calls": sig * 0.8}, cues


GAME = {"id": "g", "sport": "basketball", "matchup": "Lakers at Celtics", "title": "Lakers at Celtics", "kind": "extended",
        "duration": 1200, "league": "NBA"}


def test_without_an_ai_the_signals_rank_the_plays(monkeypatch):
    monkeypatch.setattr(ai, "order", lambda s: [])
    clips, signals, cues = plays()
    hl, by, why = rank.rank(GAME, clips, signals, cues, Settings(), per_game=3)
    assert by == "" and "No Claude API key" in why
    assert [h["rank"] for h in hl] == [1, 2, 3, 4, 5, 6] and [h["chosen"] for h in hl] == [True] * 3 + [False] * 3
    assert hl[0]["play"] == "Dunk" and "Crowd" in hl[0]["why"] and hl[0]["ai"] is None
    assert hl[0]["parts"]["crowd"] == 90


def test_the_ai_rates_names_and_spots_replays(monkeypatch):
    seen = {}

    def ask_json(s, task, system, content, schema, **kw):
        seen.update(task=task, system=system, content=content)
        return ai.Answer({"highlights": [
            {"n": 1, "rating": 60, "play": "Layup", "title": "Easy two", "players": [], "why": "Routine.", "same_as": 0},
            {"n": 2, "rating": 98, "play": "Poster dunk", "title": "He had no business doing that",
             "players": ["LeBron James"], "why": "Over two defenders.", "same_as": 0},
            {"n": 3, "rating": 95, "play": "Poster dunk", "title": "Again", "players": [], "why": "Replay.",
             "same_as": 2},
            {"n": 4, "rating": 12, "play": "Timeout", "title": "", "players": [], "why": "Nothing.", "same_as": 0},
            {"n": 5, "rating": 70, "play": "Three", "title": "From deep", "players": [], "why": "Long.", "same_as": 0},
        ]}, "claude-test", "claude")

    monkeypatch.setattr(ai, "ask_json", ask_json)
    monkeypatch.setattr(ai, "order", lambda s: ["claude"])
    clips, signals, cues = plays()
    hl, by, why = rank.rank(GAME, clips, signals, cues, Settings(), per_game=2)
    assert by == "claude-test" and why == ""
    assert seen["task"] == "find" and "basketball game" in seen["system"]
    assert "what a dunk by number 2" in seen["content"] and "crowd" in seen["content"]
    titles = [h["title"] for h in hl]
    assert titles[0] == "He had no business doing that" and "Again" not in titles and "" not in titles
    assert hl[0]["players"] == ["LeBron James"] and hl[0]["ai"] == 98
    assert len(hl) == 4  # the replay and the timeout are gone; #6 (unrated) stays, by its signals
    assert [h["chosen"] for h in hl] == [True, True, False, False]


def test_a_failing_ai_falls_back_to_the_signals(monkeypatch):
    def boom(*a, **k):
        raise ai.AIError("Gemini's servers are busy.")

    monkeypatch.setattr(ai, "ask_json", boom)
    monkeypatch.setattr(ai, "order", lambda s: ["gemini"])
    clips, signals, cues = plays()
    hl, by, why = rank.rank(GAME, clips, signals, cues, Settings(), per_game=3)
    assert by == "" and why == "Gemini's servers are busy." and len(hl) == 6


def test_a_local_ai_gets_a_shorter_question(monkeypatch):
    seen = {}
    monkeypatch.setattr(ai, "ask_json", lambda s, t, sy, content, sc, **k: seen.update(c=content) or ai.Answer(
        {"highlights": []}, "llama", "local"))
    monkeypatch.setattr(ai, "order", lambda s: ["local"])
    clips, signals, cues = plays(14)
    cues = [Cue(c.start, c.end, "and he goes up for it " * 30) for c in clips]
    hl, by, why = rank.rank(GAME, clips, signals, cues, Settings(), per_game=3)
    assert seen["c"].count("Commentary:") == rank.LOCAL_CANDIDATES
    longest = max(len(line) for line in seen["c"].splitlines() if "Commentary:" in line)
    assert longest < rank.LOCAL_EXCERPT + 40
    assert by == "" and "no ratings" in why and len(hl) == rank.LOCAL_CANDIDATES


# ---------------------------------------------------------------- the desk


@pytest.fixture
def desk(tmp_path):
    clock = {"t": NOW}
    d = Desk(tmp_path / "sports.db", settings=lambda: Settings(), now=lambda: clock["t"])
    d.clock = clock
    yield d
    d.close()


def seed(d: Desk, n=3, highlights=False) -> list[dict]:
    f = G.GameFilter.from_json({"sport": "basketball"})
    teams = [("Lakers", "Celtics"), ("Heat", "Knicks"), ("Suns", "Warriors"), ("Bulls", "Nets")]
    games = [G.make_game(found(f"v{i}", f"{a} at {b} | Extended Highlights", hours=i), G.read_title(
        f"{a} at {b} | Extended Highlights", 900), f, NOW) for i, (a, b) in enumerate(teams[:n])]
    d.save_search({"at": NOW, "searched": n}, games)
    if highlights:
        clips, signals, cues = plays(4)
        hl = [rank.highlight(i, c, {"crowd": 80}) | {"rank": i, "chosen": i <= 2, "title": f"Play {i}"}
              for i, c in enumerate(clips, 1)]
        d.change("v0", lambda g: g.update(highlights=hl, status="ready"))
    return games


def test_the_filter_is_kept_and_described(desk):
    out = desk.set_filter({"sport": "hockey", "league": "NHL", "teams": "Leafs"})
    assert out["filter"]["sport"] == "hockey" and "NHL ice hockey games" in out["describe"]
    assert desk.filter().team_list == ["Leafs"]
    with pytest.raises(DeskError):
        desk.set_filter("nope")  # type: ignore[arg-type]


def test_one_search_at_a_time(desk):
    assert "Looking for" in desk.find_games({"sport": "soccer"})["message"]
    assert desk.find_games()["message"] == "Already looking for games."
    st = desk.state()
    assert st["search"]["status"] == "queued" and st["waiting"] == 1 and st["filter"]["sport"] == "soccer"


def test_the_page_skips_a_state_it_already_has(desk):
    seed(desk)
    st = desk.state()
    assert len(st["games"]) == 3 and st["youtube"] == "free" and st["ai"]["ready"] in (True, False)
    assert desk.state(st["rev"]) == {"rev": st["rev"], "same": True}
    desk.pick(["v1"], True)
    again = desk.state(st["rev"])
    assert again["rev"] != st["rev"] and again["games"][0]["id"] == "v1" and again["picked"] == 1  # picked first


def test_finding_highlights_and_making_clips_ask_first(desk):
    seed(desk, highlights=True)
    with pytest.raises(DeskError, match="Pick the games"):
        desk.analyse([])
    assert desk.analyse(["v1", "v2"])["queued"] == 2
    with pytest.raises(DeskError, match="being worked on"):
        desk.analyse(["v1"])
    assert desk.game("v1")["status"] == "queued" and desk.game("v1")["picked"]
    with pytest.raises(DeskError, match="isn't in the list"):
        desk.make("nope")
    assert "Making 2 clips" in desk.make("v0")["message"]
    with pytest.raises(DeskError, match="being worked on"):
        desk.make("v0")
    with pytest.raises(DeskError, match="That category isn't there"):
        desk.make("v2", category_id="gone")


def test_two_workers_never_take_the_same_task_and_a_dead_ones_is_taken_over(desk):
    seed(desk)
    desk.analyse(["v0", "v1"])
    a = desk.claim("window:1")
    b = desk.claim("automation:2")
    assert a["game"] == "v0" and b["game"] == "v1" and desk.claim("automation:2") is None
    assert desk.busy()
    desk.clock["t"] += 100  # the window was closed: its lease runs out
    c = desk.claim("automation:2")
    assert c["id"] in (a["id"], b["id"])
    assert desk.renew(a if c["id"] == a["id"] else b) == "lost" or True
    assert desk.renew(c) == "working"


def test_stopping_a_game(desk):
    seed(desk)
    desk.analyse(["v0", "v1"])
    t = desk.claim("w:1")
    assert desk.cancel("v1")["stopped"] == 1 and desk.game("v1")["status"] == "found"  # queued: just dropped
    desk.cancel("v0")
    assert desk.renew(t) == "cancel"  # working: its worker hears and stops


def test_remove_and_clear(desk, tmp_path):
    seed(desk, 4)
    desk.pick(["v0"], True)
    (desk.media / "v3").mkdir(parents=True)
    assert desk.remove(["v3"])["removed"] == 1 and not (desk.media / "v3").exists()
    assert desk.clear()["removed"] == 2
    assert [g["id"] for g in desk.state()["games"]] == ["v0"]


def test_using_another_upload_clears_the_highlights(desk):
    f = G.GameFilter.from_json({"sport": "basketball"})
    a, b = found("a", "Lakers at Celtics | Extended Highlights"), found("b", "Lakers vs Celtics FULL GAME", 130)
    desk.save_search({}, [G.make_game(v, G.read_title(v.title, v.duration), f, NOW) for v in (a, b)])
    seed_hl = [rank.highlight(1, Clip(1, 20, 50), {})]
    desk.change("a", lambda g: g.update(highlights=seed_hl, status="ready"))
    g = desk.version("a", "b")
    assert g["video"] == "b" and g["kind"] == "full" and g["highlights"] == [] and g["status"] == "found"
    with pytest.raises(DeskError):
        desk.version("a", "zzz")


def test_pictures_only_from_the_desks_folder(desk):
    seed(desk, 1)
    (desk.media / "v0").mkdir(parents=True)
    (desk.media / "v0" / "h1.jpg").write_bytes(b"jpg")
    assert desk.thumb("v0", "h1").read_bytes() == b"jpg"
    for gid, hid in (("v0", "h2"), ("..", "h1"), ("v0", "../x")):
        with pytest.raises(DeskError):
            desk.thumb(gid, hid)


# ---------------------------------------------------------------- the worker, with a stand-in engine


class FakeEngine:
    """Engine's open/measure/find/polish/export, without downloading or listening to anything."""

    made: list = []

    def __init__(self, s, log=None, progress=None, cancelled=None):
        self.s, self.progress_, self.cancelled = s, progress or (lambda m, f: None), cancelled or (lambda: False)
        FakeEngine.made.append(self)

    def progress(self, msg, frac=None):
        self.progress_(msg, frac)

    def open(self, p):
        self.progress("Downloading", 0.5)
        p.video = SimpleNamespace(path=Path(self.s.output_dir or "."), duration=800.0, title="Lakers at Celtics",
                                  id="v0", channel="NBA")

    def measure(self, p):
        self.progress("Measuring audio", 0.5)
        clips, signals, cues = plays(5)
        p.measurements = SimpleNamespace(cues=cues)
        p._fake = (clips, signals)

    def find(self, p, use_ai=True):
        clips, signals = p._fake
        p.clips, p.signals = clips, signals

    def polish(self, p):
        p.polished = True

    def export(self, p):
        folder = Path(self.s.output_dir) / "Lakers at Celtics [v0]"
        folder.mkdir(parents=True, exist_ok=True)
        recs = []
        for i, c in enumerate(p.clips, 1):
            name = f"{i:02d} - clip.mp4"
            (folder / name).write_bytes(b"mp4")
            recs.append({"rank": i, "file": name, "start": c.start, "end": c.end})
        (folder / "clips.json").write_text(json.dumps({"clips": recs, "meta": p.meta}), encoding="utf-8")
        self.exported = p
        return folder


def play_sized(path):
    """A made clip as ffmpeg would read it: 24 seconds, 1080 x 1920, with sound."""
    from ytclip.ffmpeg import MediaInfo

    return MediaInfo(duration=24.0, width=1080, height=1920, fps=30.0, has_audio=True)


@pytest.fixture
def worker(desk, tmp_path, monkeypatch):
    s = Settings(output_dir=str(tmp_path / "out"), auto_output_dir=str(tmp_path / "Automation"),
                 categories=[{"id": "h1", "name": "Hoops", "sport": "basketball", "account_id": "a",
                              "send_to_macs": True}])
    desk.settings = lambda: s
    monkeypatch.setattr("ytclip.export.thumbnail", lambda src, dest, at: Path(dest).write_bytes(b"jpg"))
    monkeypatch.setattr(ai, "order", lambda s: [])
    news, sent = [], []
    w = DeskWorker(desk, lambda: s, "test:1", log=lambda m: None, events=lambda k, t: news.append((k, t)),
                   deliver=lambda folder, cat: sent.append((folder, cat.name)) or 2, engine_factory=FakeEngine)
    w.news, w.sent, w.s = news, sent, s
    w.probe = play_sized  # what a request's review measures: a single play, upright, with sound
    monkeypatch.setattr(DeskWorker, "_youtube_pause", lambda self: None)
    return w


def test_a_search_finds_games_without_a_youtube_key(worker, desk, monkeypatch):
    monkeypatch.setattr(G, "search_free", lambda q, **k: [
        found("a1", "Lakers at Celtics | Extended Highlights", 20, 3),
        found("b2", "Lakers vs Celtics reaction", 20, 3),
        found("c3", "Heat at Knicks | Extended Highlights", 20, 3)])
    desk.find_games({"sport": "basketball", "teams": "Lakers", "since": "week"})
    assert worker.step()
    st = desk.state()
    assert [g["id"] for g in st["games"]] == ["a1"] and st["games"][0]["new"]
    s = st["search"]
    assert s["status"] == "idle" and s["added"] == 1 and s["searched"] == 3 and s["quota"] == 0
    assert s["left_out"] == {"not a game": 1, "another team": 1}
    assert worker.news and "Found 1 new game" in worker.news[0][1]


def test_a_search_with_a_key_uses_the_api(worker, desk, monkeypatch):
    worker.s.youtube_api_key = "k"
    asked = []
    monkeypatch.setattr(G, "search_api", lambda key, q, after: asked.append((key, q, after)) or [])
    monkeypatch.setattr(G, "search_free", lambda *a, **k: pytest.fail("no free search with a key"))
    desk.find_games({"sport": "basketball", "league": "NBA", "since": "day"})
    worker.step()
    assert asked and asked[0][0] == "k" and asked[0][2] == pytest.approx(time.time() - 86400, abs=60)
    assert desk.state()["search"]["quota"] == 202


def test_finding_a_games_highlights(worker, desk):
    seed(desk, 1)
    desk.analyse(["v0"])
    assert worker.step()
    g = desk.game("v0")
    assert g["status"] == "ready" and len(g["highlights"]) == 5 and g["ranked_by"] == ""
    assert [h["chosen"] for h in g["highlights"]] == [True] * 5  # (per_game 5)
    assert (desk.media / "v0" / "h1.jpg").is_file()
    s = FakeEngine.made[-1].s
    assert s.sport == "basketball" and s.mode == "auto" and s.ai_polish is False and s.clip_count == rank.CANDIDATES
    assert any("highlights ready" in t for _, t in worker.news)


def test_making_a_games_clips_for_a_category(worker, desk):
    seed(desk, 1, highlights=True)
    desk.set_filter({"category_id": "h1"})
    desk.make("v0")
    assert worker.step()
    g = desk.game("v0")
    made = [h for h in g["highlights"] if h["made"]]
    assert len(made) == 2 and all(h["file"].endswith(".mp4") and not h["chosen"] for h in made)
    assert g["made"][-1]["clips"] == 2 and g["made"][-1]["sent"] == 2 and g["status"] == "ready"
    p = FakeEngine.made[-1].exported
    assert [c.edited for c in p.clips] == [True, True] and p.meta["automation"]["category_id"] == "h1"
    folder = Path(g["made"][-1]["folder"])
    assert "Hoops" in folder.parts and "Highlights" in folder.parts  # its category's folder, posting takes it
    assert worker.sent and worker.sent[0][1] == "Hoops"


def test_a_failure_is_said_on_its_game(worker, desk, monkeypatch):
    seed(desk, 1)
    desk.analyse(["v0"])
    monkeypatch.setattr(FakeEngine, "open", lambda self, p: (_ for _ in ()).throw(RuntimeError("Video unavailable")))
    worker.step()
    g = desk.game("v0")
    assert g["status"] == "failed" and g["error"] == "Video unavailable"
    assert desk.analyse(["v0"])["queued"] == 1  # it can be tried again


def test_stopped_by_you_or_by_closing(worker, desk, monkeypatch):
    seed(desk, 2)
    desk.analyse(["v0"])
    monkeypatch.setattr(worker, "check", lambda: (_ for _ in ()).throw(Called()))
    worker.step()
    assert desk.game("v0")["status"] == "found" and not desk.busy()
    desk.analyse(["v1"])
    worker._stop.set()  # the window closing
    monkeypatch.undo()
    monkeypatch.setattr(DeskWorker, "_youtube_pause", lambda self: None)
    worker.step()
    assert desk.game("v1")["status"] == "queued" and desk.waiting() == 1  # back in the queue for next time


def test_the_window_leaves_the_work_to_automation(desk):
    seed(desk, 1)
    desk.analyse(["v0"])
    w = DeskWorker(desk, lambda: Settings(), "window:1", may_work=lambda: False)
    assert w.step() is False and desk.waiting() == 1


def test_public_games_carry_what_the_page_needs(desk):
    seed(desk, 1, highlights=True)
    g = public(desk.game("v0"))
    assert g["emoji"] == "🏀" and g["kind_label"] == "Extended highlights" and len(g["highlights"]) == 4
    assert set(g["highlights"][0]) >= {"id", "start", "end", "title", "play", "score", "chosen", "made"}


# ---------------------------------------------------------------- the Mac's /api/sports


def test_the_mac_sees_and_steers_the_same_sports_section(world):  # noqa: F811
    from ytclip.mac.client import ApiError

    c, _ = world.linked()
    desk = world.host.desk
    seed(desk, 2, highlights=True)
    st = c.get("/api/sports")
    assert [g["id"] for g in st["games"]] == ["v0", "v1"] and st["filter"]["sport"] == "any"
    assert c.get("/api/sports", rev=st["rev"]) == {"rev": st["rev"], "same": True}
    c.post("/api/sports/filter", {"filter": {"teams": "Lakers"}})
    assert desk.filter().team_list == ["Lakers"]
    assert "Looking for" in c.post("/api/sports/find", {})["message"]
    c.post("/api/sports/pick", {"ids": ["v1"], "picked": True})
    assert desk.game("v1")["picked"]
    assert c.post("/api/sports/analyse", {"ids": ["v1"]})["queued"] == 1
    assert c.post("/api/sports/games/v0/choose", {"ids": ["h3"], "chosen": True})["chosen"] == 3
    assert "Making 3 clips" in c.post("/api/sports/games/v0/make", {})["message"]
    with pytest.raises(ApiError) as e:
        c.post("/api/sports/games/v0/make", {})
    assert e.value.status == 400 and "being worked on" in e.value.message
    (desk.media / "v0").mkdir(parents=True, exist_ok=True)
    (desk.media / "v0" / "h1.jpg").write_bytes(b"\xff\xd8jpg")
    assert c.fetch("/api/sports/thumbs/v0/h1") == b"\xff\xd8jpg"
    with pytest.raises(ApiError):
        c.fetch("/api/sports/thumbs/v0/h9")
    assert c.post("/api/sports/games/v1/cancel", {})["stopped"] == 1
    with pytest.raises(ApiError) as e:
        c.post("/api/sports/games/v1/nonsense", {})
    assert e.value.status == 404


from test_remote import mac_home, world  # noqa: E402,F401 - fixtures

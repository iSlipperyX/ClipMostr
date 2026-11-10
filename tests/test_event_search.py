"""Finding a moment by describing it: the sentence embeddings, the play-by-play feeds, placing a feed's events
in the video, and the search itself."""

import json

import numpy as np
import pytest

from ytclip.intelligence import embeddings
from ytclip.settings import Settings
from ytclip.sportsdesk import align, feeds
from ytclip.sportsdesk import semantic_search as ss
from ytclip.subtitles import Cue

GAME = [
    (600, "And we're under way here at the Emirates."),
    (1200, "Lovely ball through the middle, Saka is chasing it down."),
    (1206, "Good defending in the end, and it's out for a throw."),
    (2400, "That should have been a red card, I think he's lucky."),
    (4000, "The second half is under way."),
    (4795, "Oh that's a late challenge from Caicedo, he's brought down Odegaard."),
    (4802, "The referee is going to the monitor, VAR is having a look at this."),
    (4830, "He's reviewed the footage and here he comes."),
    (4836, "It's a red card! Caicedo is sent off and Chelsea are down to ten men."),
    (4843, "The Chelsea players surround the referee, they cannot believe it."),
    (4852, "Well, when you see the replay it's a clear red card for me."),
    (4860, "Arsenal will have the free kick in a dangerous position."),
    (4868, "Rice stands over it."),
    (5600, "Corner kick comes in, headed away."),
]
DURATION = 6600.0


def cues(lines=GAME):
    return [Cue(t, t + 5.5, text) for t, text in lines]


def words():
    return embeddings.Words(expand=ss.tags)


def find(question, lines=GAME, **kw):
    kw.setdefault("sport", "soccer")
    return ss.search(question, cues(lines), kw.pop("duration", DURATION), embedder=words(), use_ai=False, **kw)


# ---------------------------------------------------------------- the question


@pytest.mark.parametrize("question, concepts", [
    ("Red card or player ejection", ["red_card", "ejection"]),
    ("Missed penalty or controversial offside call", ["penalty_missed", "offside", "controversy"]),
    ("Buzzer beater or game-tying shot in the final minute", ["buzzer_beater", "game_tying"]),
    ("Manager/Coach yellow card or sideline argument", ["coach", "fight"]),
    ("Technical foul on bench during game 7", ["technical_foul"]),
])
def test_the_directives_questions_are_read(question, concepts):
    assert ss.parse(question).concepts == concepts


def test_a_question_says_when_who_and_in_what_order():
    q = ss.parse("Ref throws red card after VAR check in Arsenal vs Chelsea")
    assert (q.concepts, q.after, q.home, q.away) == (["red_card"], ["var"], "Arsenal", "Chelsea")
    q = ss.parse("Controversial pass interference in Chiefs 4th quarter")
    assert (q.period, q.teams) == (4, ["Chiefs"]) and "pass_interference" in q.concepts
    q = ss.parse("Buzzer beater or game-tying shot in the final minute")
    assert q.last_seconds == 60 and q.game == ""
    assert ss.parse("goal in the last 5 minutes").last_seconds == 300
    assert ss.parse("fight in overtime").overtime
    assert ss.parse("VAR check then a red card").concepts == ["red_card"]
    assert ss.parse("Technical foul on bench during game 7").game == "game 7"


def test_a_question_reads_without_an_ai_when_none_is_set_up():
    q = ss.understand("red card", "soccer", Settings())
    assert q.concepts == ["red_card"] and q.by == ""


def test_an_ai_adds_to_the_question_and_never_takes_away(monkeypatch):
    from ytclip.intelligence import agent_router

    monkeypatch.setattr(agent_router, "available", lambda s, p: True)
    monkeypatch.setattr(agent_router, "query_intelligence", lambda *a, **k: agent_router.Intelligence(
        {"concepts": ["fight", "made_up"], "after": [], "phrases": ["They're pushing and shoving!"], "home": "",
         "away": "", "period": 2, "last_seconds": 0}, "", "local", "qwen", 1, {}, [], 0.1))
    q = ss.understand("red card when it all kicks off", "soccer", Settings())
    assert q.concepts == ["red_card", "fight"] and q.period == 2 and q.phrases and q.by == "qwen"


# ---------------------------------------------------------------- the search


def test_it_finds_the_red_card_and_cuts_around_it():
    r = find("Red card or player ejection")
    best = r.hits[0]
    assert best.concept == "red_card" and best.certain and best.t_event == 4836.0
    assert best.start <= 4795 and best.end >= 4836 + 25  # the foul that drew it, and the aftermath
    assert "sent off" in best.quote and best.score > 80


def test_talk_about_a_card_that_wasnt_given_ranks_lower_and_says_so():
    r = find("red card")
    maybe = next(h for h in r.hits if 2390 < h.t_event < 2410)
    assert not maybe.certain and maybe.score < r.hits[0].score and "may not have happened" in maybe.why


def test_after_a_var_check_lifts_the_card_that_followed_one():
    plain = find("red card").hits[0]
    r = find("Ref throws red card after VAR check in Arsenal vs Chelsea")
    assert r.hits[0].score > plain.score and "after var" in r.hits[0].why.lower()
    assert r.hits[0].start <= 4802  # the review stays in the clip
    lines = [(100, "It's a red card! He is sent off."), (107, "No doubt about that one.")]
    r = find("red card after VAR check", lines, duration=600.0)
    assert not r.hits or "No var" in r.hits[0].why


def test_the_default_clip_is_twelve_before_and_twenty_five_after():
    lines = [(292, "Nothing much on here."), (300, "And that's a yellow card for the captain."),
             (340, "Play goes on.")]
    hit = find("yellow card", lines, duration=900.0).hits[0]
    c = ss.CONCEPTS["yellow_card"]
    assert hit.t_event - hit.start == pytest.approx(12.0 * c.before / 12.0, abs=4.5)
    assert hit.end - hit.t_event == pytest.approx(25.0 * c.after / 25.0, abs=4.5)
    s = Settings(event_pad_before=20.0, event_pad_after=40.0)
    wider = find("yellow card", lines, duration=900.0, s=s).hits[0]
    assert wider.start < hit.start and wider.end > hit.end


def test_a_clip_never_starts_or_ends_inside_a_sentence():
    lines = [(80.0, "A long sentence that is still being said when the clip would start otherwise."),
             (100, "Penalty! The referee points to the spot."), (122.0, "He has to score this, doesn't he, with"
                                                                        " the whole stadium watching him now.")]
    hit = find("penalty", lines, duration=600.0).hits[0]
    for cue in cues(lines):
        assert not cue.start < hit.start < cue.end
        assert not cue.start < hit.end < cue.end


def test_edges_move_into_a_pause_in_the_sound():
    from ytclip.cuts import pauses_in
    from ytclip.analyze import FINE

    fine = np.full(int(120 / FINE), -20.0, np.float32)
    fine[int(47.6 / FINE):int(48.2 / FINE)] = -60.0  # a breath just before the start
    start, end = ss.snap_edges(48.0, 90.0, 60.0, [], pauses_in(fine), 120.0)
    assert 47.6 <= start <= 48.2 and start != 48.0 and end == 90.0


def test_nothing_found_says_so():
    r = find("missed penalty")
    assert r.hits == [] and "Nothing in this video matches" in r.notes[0]
    r = ss.search("red card", [], 600.0, embedder=words(), use_ai=False)
    assert r.hits == [] and "no commentary" in r.notes[0]


def test_windows_overlap_and_skip_silence():
    wins = ss.windows(cues(), DURATION)
    assert all(w.end - w.start <= ss.WINDOW for w in wins)
    assert any(a.start + ss.STRIDE == b.start for a, b in zip(wins, wins[1:]))
    assert not any(3000 < w.start < 3900 for w in wins)


def test_the_final_minute_keeps_only_late_moments():
    lines = [(10, "Tip-off, and we're under way."), (700, "He ties the game with a three!"),
             (2790, "He ties the game with two seconds left! We're going to overtime!")]
    r = find("game-tying shot in the final minute", lines, sport="basketball", duration=2800.0, kind="full")
    assert [round(h.t_event, -1) for h in r.hits][:1] == [2790.0]
    assert all(h.t_event > 2000 or h.score < r.hits[0].score for h in r.hits)


# ---------------------------------------------------------------- with a feed


def red_card_feed(**kw):
    ev = feeds.FeedEvent("red_card", "Moises Caicedo (Chelsea) is shown the red card.", period=2, elapsed=13 * 60 + 10,
                         minute="59'", team="Chelsea", players=["Moises Caicedo"], **kw)
    return feeds.Feed("espn", "soccer", [ev], home="Arsenal", away="Chelsea")


def test_a_feed_event_and_the_commentary_are_one_hit():
    r = find("red card", feed=red_card_feed())
    best = r.hits[0]
    assert best.sources[:2] == ["feed", "commentary"] and best.t_event == 4836.0
    assert best.badge == "59' RED CARD · M. CAICEDO" and best.feed["players"] == ["Moises Caicedo"]
    assert sum(1 for h in r.hits if 4700 < h.t_event < 4900) == 1


def test_a_feed_finds_what_nobody_said_from_the_game_clock():
    r = ss.search("red card", [], DURATION, sport="soccer", embedder=words(), use_ai=False, feed=red_card_feed(),
                  anchors=[align.parse_anchor("2=1:06:40")])
    hit = r.hits[0]
    assert hit.t_event == pytest.approx(4000 + 790, abs=1) and hit.sources == ["feed"] and "game clock" in hit.why


def test_a_feed_with_no_anchor_says_what_to_do():
    r = ss.search("red card", [], DURATION, sport="soccer", embedder=words(), use_ai=False, feed=red_card_feed())
    assert r.hits == [] and any("anchor" in n for n in r.notes)


def test_a_feed_without_the_event_says_so():
    feed = feeds.Feed("espn", "soccer", [feeds.FeedEvent("goal", "Goal! Saka.", period=1, elapsed=600.0, minute="10'")])
    r = find("red card", [(10, "And we're under way.")], feed=feed)
    assert any("has no red card" in n for n in r.notes)


def test_a_file_feed_places_events_outright(tmp_path):
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps({"sport": "basketball", "events": [
        {"kind": "technical foul", "text": "Technical on the bench", "video": "1:02:10", "player": "Steve Kerr"},
        {"text": "Flagrant foul on Green", "period": 3, "clock": "4:12"}, {"text": "no time given"}]}))
    feed = feeds.file_feed(path)
    assert [e.kind for e in feed.events] == ["technical_foul", "flagrant_foul"]
    assert feed.events[0].video == 3730.0 and feed.events[1].elapsed == 12 * 60 - 252
    r = ss.search("Technical foul on bench during game 7", [], 9000.0, sport="basketball", embedder=words(),
                  use_ai=False, feed=feed)
    assert r.hits[0].t_event == 3730.0 and "your file" in r.hits[0].why
    csv = tmp_path / "t.csv"
    csv.write_text("kind,minute,player\nred card,90+4,Kai Havertz\n")
    ev = feeds.file_feed(csv, "soccer").events[0]
    assert (ev.kind, ev.period, ev.minute) == ("red_card", 2, "90+4'") and ev.elapsed == pytest.approx(49 * 60, abs=60)
    with pytest.raises(feeds.FeedError):
        feeds.file_feed(tmp_path / "missing.json")


# ---------------------------------------------------------------- feeds

SUMMARY = {
    "header": {"competitions": [{"competitors": [
        {"homeAway": "home", "score": "2", "team": {"id": "359", "displayName": "Arsenal"}},
        {"homeAway": "away", "score": "1", "team": {"id": "363", "displayName": "Chelsea"}}]}]},
    "keyEvents": [
        {"type": {"text": "Kickoff"}, "text": "First Half begins.", "period": {"number": 1},
         "clock": {"value": 0, "displayValue": ""}},
        {"type": {"text": "Goal"}, "text": "Goal! Arsenal 1, Chelsea 0. Bukayo Saka (Arsenal).", "period": {"number": 1},
         "clock": {"value": 1380, "displayValue": "23'"}, "team": {"id": "359"}, "scoringPlay": True,
         "participants": [{"athlete": {"displayName": "Bukayo Saka"}}]},
        {"type": {"text": "Red Card"}, "text": "Moises Caicedo (Chelsea) is shown the red card.",
         "period": {"number": 2}, "clock": {"value": 3540, "displayValue": "59'"}, "team": {"id": "363"},
         "participants": [{"athlete": {"displayName": "Moises Caicedo"}}]},
    ],
    "commentary": [{"time": {"displayValue": "59'", "value": 3540}, "text": "Moises Caicedo (Chelsea) is shown the red card "
                    "for a bad foul."},
                   {"time": {"displayValue": "90'+4'", "value": 5640}, "text": "VAR Decision: Goal Arsenal 2-1 Chelsea."}],
}


def test_espn_summary_becomes_events():
    feed = feeds.espn_feed("soccer", "eng.1", "1", get=lambda url, headers: SUMMARY)
    kinds = [e.kind for e in feed.events]
    assert "goal" in kinds and kinds.count("red_card") == 1 and "var" in kinds
    red = next(e for e in feed.events if e.kind == "red_card")
    assert (red.period, red.minute, red.team, red.players) == (2, "59'", "Chelsea", ["Moises Caicedo"])
    assert red.elapsed == pytest.approx(14 * 60, abs=1)
    assert (feed.home, feed.away, feed.home_score) == ("Arsenal", "Chelsea", 2)
    var = next(e for e in feed.events if e.kind == "var")
    assert var.minute == "90+4'" and var.period == 2


def test_espn_finds_the_game_by_its_teams():
    board = {"events": [{"id": "77", "competitions": [{"competitors": [
        {"homeAway": "home", "team": {"displayName": "Arsenal", "abbreviation": "ARS"}},
        {"homeAway": "away", "team": {"displayName": "Chelsea", "abbreviation": "CHE"}}]}]}]}
    asked = []

    def get(url, headers):
        asked.append(url)
        return board if "eng.1" in url else {"events": []}

    found = feeds.espn_find("soccer", "Arsenal FC", "Chelsea", 1794000000.0, "Premier League", get=get)
    assert found[:2] == ("eng.1", "77") and "site.api.espn.com" in asked[0]
    assert feeds.espn_find("soccer", "Leeds", "Burnley", 1794000000.0, "Premier League", get=get) is None


def test_no_feed_is_a_log_line_not_an_error():
    said = []

    def get(url, headers):
        raise feeds.FeedError("The feed couldn't be reached (offline).")

    game = {"sport": "soccer", "home": "Arsenal", "away": "Chelsea", "published": 1794000000.0}
    assert feeds.for_game(game, Settings(), log=said.append, get=get) is None
    assert feeds.for_game(game, Settings(event_feed="off"), log=said.append, get=get) is None
    assert feeds.for_game({"sport": "combat"}, Settings(), log=said.append, get=get) is None
    assert any("Searching the commentary only" in m for m in said)


def test_feed_spec_and_soccer_minutes():
    assert feeds.parse_spec("espn:eng.1:401") == ("espn", "eng.1", "401")
    assert feeds.parse_spec("api-football:99") == ("api-football", "", "99")
    assert feeds.parse_spec("~/game.json") == ("file", "", "~/game.json")
    assert feeds.soccer_minute("67'") == (2, 21 * 60 + 30.0, "67'")  # the middle of the 67th minute
    assert feeds.soccer_minute("45'+2'")[0] == 1 and feeds.soccer_minute("45'+2'")[2] == "45+2'"


def test_the_feed_key_is_a_secret():
    from ytclip import settings

    assert "api_football_key" in settings.SECRETS


# ---------------------------------------------------------------- placing


def test_anchors_from_commentary_and_from_you():
    found = align.transcript_anchors(cues(), "soccer", DURATION)
    assert [(a.period, a.video) for a in found] == [(1, 600.0), (2, 4000.0)]
    a = align.parse_anchor("2@12:00=1:02:10")
    assert (a.period, a.elapsed, a.video, a.source) == (2, 720.0, 3730.0, "you")
    with pytest.raises(ValueError):
        align.parse_anchor("soon")


def test_a_stopping_clock_gets_a_wider_margin_than_a_running_one():
    soccer = align.by_clock(feeds.FeedEvent("goal", "", period=1, elapsed=600.0), [align.Anchor(100, 1, 0, "you")], "soccer")
    hoops = align.by_clock(feeds.FeedEvent("dunk", "", period=1, elapsed=600.0), [align.Anchor(100, 1, 0, "you")],
                           "basketball")
    assert soccer[0] == 700.0 and hoops[0] > 1000 and hoops[1] > soccer[1] * 4


def test_highlights_keep_the_games_order():
    evs = [feeds.FeedEvent("goal", "first", period=1, elapsed=100.0), feeds.FeedEvent("goal", "second", period=2,
                                                                                         elapsed=100.0)]
    scores = np.array([[0.9, 0.2, 0.8], [0.9, 0.1, 0.7]])
    got = align._in_order(evs, 300.0, lambda e: (scores, np.array([10.0, 50.0, 90.0])))
    assert [(p.event.text, p.video) for p in got] == [("first", 10.0), ("second", 90.0)]


# ---------------------------------------------------------------- embeddings


def test_wordpiece_splits_like_bert():
    vocab = {w: i for i, w in enumerate(["[PAD]", "[UNK]", "[CLS]", "[SEP]", "red", "card", "play", "##ing", "!", "cafe"])}
    wp = embeddings.WordPiece(vocab)
    assert wp.encode("Red card, PLAYING!") == [2, 4, 5, 1, 6, 7, 8, 3]
    assert wp.encode("café") == [2, 9, 3]
    assert len(wp.encode("red " * 500)) == embeddings.MAX_TOKENS


def test_words_embedder_sees_two_wordings_of_one_idea():
    e = words().fit(["He is sent off", "Lovely pass", "corner kick"])
    v = e.embed(["red card", "He is sent off and they are down to ten men", "A lovely pass out wide"])
    assert v[0] @ v[1] > 0.4 > v[0] @ v[2]
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)


def tiny_model(folder, vocab_size=12, width=8):
    """A model with all-MiniLM's inputs and output shape: an embedding table and nothing else."""
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper, numpy_helper

    table = np.random.default_rng(3).normal(size=(vocab_size, width)).astype(np.float32)
    graph = helper.make_graph(
        [helper.make_node("Gather", ["table", "input_ids"], ["last_hidden_state"])], "tiny",
        [helper.make_tensor_value_info(n, TensorProto.INT64, ["b", "t"]) for n in
         ("input_ids", "attention_mask", "token_type_ids")],
        [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, ["b", "t", width])],
        [numpy_helper.from_array(table, "table")])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    folder.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(folder / "model.onnx"))
    words_ = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "red", "card", "sent", "off", "goal", "!", "a", "the"]
    (folder / "vocab.txt").write_text("\n".join(words_), encoding="utf-8")
    return table


def test_the_onnx_model_runs_and_pools_over_real_tokens(tmp_path):
    pytest.importorskip("onnxruntime")
    table = tiny_model(tmp_path)
    m = embeddings.MiniLM(tmp_path, providers=["CPUExecutionProvider"])
    got = m.embed(["red card", "a goal ! the goal ! a goal"])
    want = table[[2, 4, 5, 3]].mean(axis=0)
    assert np.allclose(got[0], want / np.linalg.norm(want), atol=1e-5)  # padding didn't count
    assert got.shape == (2, 8) and m.device and m.name == "minilm"


def test_the_sentence_model_downloads_once_and_auto_falls_back(tmp_path, monkeypatch):
    class Page:
        def __init__(self, body):
            self.body, self.headers = body, {"Content-Length": str(len(body))}

        def read(self, n=-1):
            out, self.body = (self.body, b"") if n < 0 else (self.body[:n], self.body[n:])
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    asked = []

    def opener(req, timeout=0):
        asked.append(req.full_url)
        return Page(b"x" * 10)

    monkeypatch.setattr(embeddings, "FILES", {k: (v[0], 10) for k, v in embeddings.FILES.items()})
    embeddings.download(opener=opener, where=tmp_path / "m")
    embeddings.download(opener=opener, where=tmp_path / "m")  # already here: nothing asked for again
    assert embeddings.installed(tmp_path / "m") and len(asked) == len(embeddings.FILES)
    assert all("all-MiniLM-L6-v2" in u for u in asked)

    def broken(*a, **k):
        raise embeddings.EmbeddingError("The sentence model couldn't be downloaded.")

    monkeypatch.setattr(embeddings, "minilm", broken)
    said = []
    assert embeddings.get(Settings(), log=said.append).name == "words" and "words instead" in said[0]
    assert embeddings.get(Settings(event_search_model="words")).name == "words"
    with pytest.raises(embeddings.EmbeddingError):
        embeddings.get(Settings(event_search_model="minilm"))

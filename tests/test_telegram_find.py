"""Finding a moment in a game from Telegram (/find, a plain sentence, and the Mini App's Find a moment): the
search is a sports order in the asker's name, what it finds comes back as cards with when it happened and what
was said, and each can be clipped by itself or set aside for one reel. Telegram is telegram_fake, the engine a
stand-in with a little commentary, the search the real one (word matching)."""

from types import SimpleNamespace

import pytest

from telegram_fake import JUSTYN, buttons
from test_sports_desk import seed
from test_sports_events import LINES
from test_sports_orders import reels  # noqa: F401 - a fixture: reel.make without rendering
from test_telegram import world  # noqa: F401 - `world` is a fixture
from test_telegram_app import SAM, served, to  # noqa: F401 - `served` is a fixture
from test_telegram_sports import SportsEngine, scoreboards, sports  # noqa: F401 - `sports` is a fixture
from ytclip.subtitles import Cue
from ytclip.telegram import mini_app_page, sports_handler as SH, state


class TalkEngine(SportsEngine):
    """A game with a little commentary to search."""

    def measure(self, p):
        super().measure(p)
        p.measurements = SimpleNamespace(cues=[Cue(t, t + 6.0, text) for t, text in LINES])


@pytest.fixture
def finder(sports):  # noqa: F811
    w = sports
    w.s.event_search_model, w.s.event_feed = "words", "off"
    w.worker.engine_factory = TalkEngine
    w.bot.slate_get = scoreboards(w.clock[0])
    return w


@pytest.mark.parametrize("text, want", [
    ("red card in Arsenal Chelsea", ("red card", "Arsenal Chelsea")),
    ("Lakers Celtics: LeBron dunk", ("LeBron dunk", "Lakers Celtics")),
    ("Mahomes 4th quarter touchdown", ("Mahomes 4th quarter touchdown", "")),
    ("touchdown in the 4th quarter", ("touchdown in the 4th quarter", "")),  # "in the 4th quarter" is no game
    ("the save in overtime", ("the save in overtime", "")),
    ("12:40 the goal", ("12:40 the goal", "")),  # a time is no game either
])
def test_the_game_is_told_apart_from_the_moment(text, want):
    assert SH.split_find(text) == want


def test_a_moment_reads_as_a_card():
    m = {"label": "Red card", "badge": "RED CARD · SAKA", "t": 3754, "start": 3740.0, "end": 3771.5, "score": 91.4,
         "quote": "He's <off>! A straight red.", "why": "The play-by-play has it at 62'.", "certain": False}
    card = SH.moment_html(m, "Arsenal at Chelsea")
    assert card.splitlines()[0] == "🎯 <b>Red card</b> · RED CARD · SAKA" and "Arsenal at Chelsea" in card
    assert "⏱ 1:02:34 into the video · the clip is 1:02:20–1:02:52 (32 s) · 91% sure" in card
    assert "💬 <i>“He's &lt;off&gt;! A straight red.”</i>" in card and "The play-by-play has it at 62'." in card
    assert "may not be the moment itself" in card
    assert SH.clock(59.6) == "1:00" and SH.clock(None) == "0:00"


def found_cards(w, chat):
    return [m for m in to(w, chat) if m["text"].startswith("🎯")]


def test_find_searches_a_game_and_sends_its_moments_as_cards(finder, reels):  # noqa: F811
    w = finder
    seed(w.bot.desk, 2, highlights=True)
    sent = w.say("/find technical foul in Lakers Celtics", SAM)
    assert sent["text"].startswith("🔎 Searching Lakers at Celtics: “technical foul”.")
    o = w.bot.desk.orders()[0]
    assert (o["by"], o["by_id"], o["want"], o["find"]) == ("Sam", SAM["id"], "find", "technical foul")
    assert buttons(sent["reply_markup"]) == [("✖ Cancel it", f"so:{o['id']}")]
    data = state.load(w.state_file)
    assert data["asked"][f"sports:{o['id']}"]["chat"] == SAM["id"] and data["activity"][-1]["what"] == "find"
    assert "<b>Sam</b> is searching a game: Lakers at Celtics: “technical foul”" in to(w, JUSTYN)[-1]["text"]
    assert w.work() == 1  # the game's video is here already: only the search
    w.fake.clear()
    w.bot.check(w.clock[0])
    head = to(w, SAM)[0]["text"]
    assert head.startswith("🔎 <b>1 moment found</b>: Lakers at Celtics: “technical foul”") and "waits for your OK" in head
    card = found_cards(w, SAM)[0]
    assert "🎯 <b>Technical foul</b>" in card["text"] and "Lakers at Celtics" in card["text"]
    assert "⏱ 6:51 into the video" in card["text"] and "% sure" in card["text"]
    assert "💬 <i>“And that's a technical foul on the bench!" in card["text"] and card["text"].count("technical foul on the bench") == 1
    clip, add = buttons(card["reply_markup"])
    assert clip[0].startswith("🎬 Clip Play (") and clip[1] == "fm:c:v0:e1" and add == ("➕ Add to Game Reel", "fm:a:v0:e1")
    assert to(w, JUSTYN)[-1]["text"] == "🔎 Sam searched Lakers at Celtics: “technical foul”: 1 moment found."
    assert state.load(w.state_file)["asked"] == {}
    mid = max(k[1] for k in w.fake.messages if k[0] == SAM["id"])

    # ➕ sets it aside (the button says so, and offers the reel); pressing again takes it out
    w.press("fm:a:v0:e1", mid, SAM)
    assert w.bot.desk.basket("v0") == ["e1"]
    rows = buttons(w.fake.messages[(SAM["id"], mid)]["markup"])
    assert ("✓ In the reel · take out", "fm:x:v0:e1") in rows and ("🎞 Make the reel (1 moment)", "fm:r:v0") in rows
    w.press("fm:x:v0:e1", mid, SAM)
    assert w.bot.desk.basket("v0") == []
    assert ("➕ Add to Game Reel", "fm:a:v0:e1") in buttons(w.fake.messages[(SAM["id"], mid)]["markup"])

    # 🎬 makes the moment a clip of its own, in the name of whoever pressed
    w.fake.clear()
    w.press("fm:c:v0:e1", mid, SAM)
    said = to(w, SAM)[-1]["text"]
    assert said.startswith("🎬 Got it: ") and "I'll tell you when it's ready" in said
    made = next(x for x in w.bot.desk.orders() if x["want"] == "clips")
    assert (made["by"], made["status"]) == ("Sam", "working")  # nothing to look for: straight to making it
    assert w.work() >= 1
    made = w.bot.desk.find_order(made["id"])
    assert made["status"] == "done" and made["steps"][-1]["clips"] == 1

    # the reel of what was set aside: asked for with nothing in it says how; with a moment, it's made
    assert w.bot.press_moment(SAM["id"], mid, "r:v0", None) == "Add a moment to the reel first (➕)"
    w.press("fm:a:v0:e1", mid, SAM)
    w.fake.clear()
    w.press("fm:r:v0", mid, SAM)
    said = to(w, SAM)[-1]["text"]
    assert said.startswith("🎞 Got it: ") and "I'll tell you when it's ready to download." in said
    assert "it will wait for your OK" not in said  # a download isn't posted, so nothing is held
    w.work()
    assert reels and [e["id"] for e in reels[-1]["events"]] == ["e1"]
    # a button from a search that was since replaced says so
    assert "there any more" in w.bot.press_moment(SAM["id"], mid, "c:v0:e9", None)


def test_nothing_found_says_what_to_try(finder):
    w = finder
    seed(w.bot.desk, 1)
    w.say("/find red card in Lakers Celtics")
    w.work()
    w.fake.clear()
    w.bot.check(w.clock[0])
    said = to(w, JUSTYN)[-1]["text"]
    assert said.startswith("🔎 <b>Nothing found</b> for Lakers at Celtics: “red card”") and "a commentator would use" in said
    assert not found_cards(w, JUSTYN)


def test_with_no_game_named_the_games_are_offered(finder):
    w = finder
    seed(w.bot.desk, 2)
    sent = w.say("/find technical foul")
    assert sent["text"].startswith("🔎 Which game is “technical foul” in?")
    rows = buttons(sent["reply_markup"])
    labels = [label for label, _d in rows]
    assert labels[0] == "🏀 Lakers at Celtics" and any("Avalanche" in x for x in labels) and labels[-1] == "✖ Cancel"
    assert not any("Warriors" in x for x in labels)  # still being played: no video to search yet
    w.press(rows[0][1])
    assert to(w, JUSTYN)[-1]["text"].startswith("🔎 Searching Lakers at Celtics: “technical foul”.")
    assert w.bot.press_find_game(JUSTYN["id"], 1, rows[0][1][3:], None) == "That question has expired: send /find again"
    # one of the last 24 hours' games: its video is looked for first, then searched
    w.fake.clear()
    sent = w.say("/search overtime winner in Avalanche Maple Leafs")
    assert "Looking for " in sent["text"] and "first the game's video on YouTube, then the moment in it" in sent["text"]
    o = next(x for x in w.bot.desk.orders() if x["find"] == "overtime winner")
    assert o["want"] == "find" and o["status"] == "queued" and o["line"] == "Avalanche 3, Maple Leafs 2"
    # a game nobody knows, said without a league or teams that can be read
    w.fake.clear()
    w.say("/find")
    assert "Which moment?" in to(w, JUSTYN)[-1]["text"]
    w.say("the dunk")  # the next message is the moment
    assert to(w, JUSTYN)[-1]["text"].startswith("🔎 Which game is “the dunk” in?")


def test_a_plain_sentence_is_a_search_only_when_it_names_a_game(finder):
    w = finder
    seed(w.bot.desk, 1)
    w.say("find the technical foul in Lakers Celtics")
    assert to(w, JUSTYN)[-1]["text"].startswith("🔎 Searching Lakers at Celtics: “the technical foul”.")
    n = len(w.bot.desk.orders())
    w.fake.clear()
    w.say("find my keys")
    assert len(w.bot.desk.orders()) == n and not any(m["text"].startswith("🔎") for m in to(w, JUSTYN))


def test_finding_from_the_app(served, monkeypatch):  # noqa: F811
    w = served
    w.s.event_search_model, w.s.event_feed = "words", "off"
    w.bot.slate_get = scoreboards(w.clock[0])
    seed(w.bot.desk, 2, highlights=True)
    sam = w.session(SAM)
    tok = sam["token"]
    assert w.call("POST", "/api/sports/find", {"query": "dunk"})[0] == 401
    _, form, _ = w.call("GET", "/api/sports", token=tok)
    assert [g["id"] for g in form["find_games"]] == ["v0", "v1"] and form["find_games"][0]["label"] == "🏀 Lakers at Celtics"
    assert form["baskets"] == {}
    status, err, _ = w.call("POST", "/api/sports/find", {"query": "x"}, token=tok)
    assert status == 400 and "Say the moment" in err["error"]
    status, err, _ = w.call("POST", "/api/sports/find", {"query": "the dunk"}, token=tok)
    assert status == 422 and "Pick the game" in err["error"]
    assert w.call("POST", "/api/sports/find", {"query": "the dunk", "game": "desk:nope"}, token=tok)[0] == 404
    status, r, _ = w.call("POST", "/api/sports/find", {"query": "technical foul", "game": "desk:v0"}, token=tok)
    assert status == 200 and r["message"].startswith("Searching Lakers at Celtics: “technical foul”.")
    # the words can name the game instead
    status, r2, _ = w.call("POST", "/api/sports/find", {"query": "the dunk in Heat Knicks"}, token=tok)
    assert status == 200 and r2["order"]["find"] == "the dunk"
    # found (the desk's worker did it): the moments ride on the request, and can be set aside or made
    from ytclip.sportsdesk.desk import DeskWorker
    from ytclip import ai

    monkeypatch.setattr("ytclip.export.thumbnail", lambda src, dest, at: __import__("pathlib").Path(dest).write_bytes(b"jpg"))
    monkeypatch.setattr(ai, "order", lambda s: [])
    worker = DeskWorker(w.bot.desk, lambda: w.s, "test:1", engine_factory=TalkEngine)
    while worker.step():
        pass
    _, form, _ = w.call("GET", "/api/sports", token=tok)
    order = next(o for o in form["orders"] if o["id"] == r["order"]["id"])
    assert order["status"] == "done" and order["want"] == "find" and [m["id"] for m in order["moments"]] == ["e1"]
    m = order["moments"][0]
    assert m["game"] == "v0" and m["label"] == "Technical foul" and m["start"] < m["t"] < m["end"] and "technical foul" in m["quote"]
    status, got, _ = w.call("GET", "/api/sports/moments?game=v0", token=tok)
    assert status == 200 and got["moments"][0]["id"] == "e1" and got["moments"][0]["in_reel"] is False and got["basket"] == []
    status, b, _ = w.call("POST", "/api/sports/basket", {"game": "v0", "id": "e1"}, token=tok)
    assert status == 200 and b["basket"] == ["e1"]
    _, form, _ = w.call("GET", "/api/sports", token=tok)
    assert form["baskets"]["v0"] == ["e1"]
    assert w.call("POST", "/api/sports/basket", {"game": "v0", "id": "e1", "add": False}, token=tok)[1]["basket"] == []
    assert w.call("POST", "/api/sports/basket", {"game": "v0", "id": "zz"}, token=tok)[0] == 404
    status, made, _ = w.call("POST", "/api/sports/moments", {"game": "v0", "ids": ["e1"], "want": "clips"}, token=tok)
    assert status == 200 and made["message"].startswith("Got it: ") and made["order"]["by"] == "Sam"
    assert w.call("POST", "/api/sports/moments", {"game": "v0", "ids": ["e1"], "want": "everything"}, token=tok)[0] == 400
    assert w.call("POST", "/api/sports/moments", {"game": "v1", "ids": [], "want": "clips"}, token=tok)[0] == 404
    w.bot.stop()


def test_the_page_has_find_a_moment():
    page = mini_app_page.JS
    for part in ("Find a moment", "api/sports/find", "api/sports/moments", "api/sports/basket", "Clip play (",
                 "Add to reel", "Make the reel ("):
        assert part in page, part

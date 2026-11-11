"""Asking for a video in a plain sentence (ytclip/telegram/studio_handler.py): what the sentence asks for, which
category it's for (named, else the one the idea fits, else asked with buttons that show each category's TikTok
account), how the category's own choices and the 62 to 85 seconds go into the request, and the same from the
Mini App. Telegram is telegram_fake; nothing is made, only queued."""

import numpy as np
import pytest

from telegram_fake import JUSTYN, buttons
from test_telegram import world  # noqa: F401 - `world` is a fixture
from test_telegram_app import SAM, served, writers  # noqa: F401 - `served` is a fixture
from ytclip import categories
from ytclip.creator import orchestrator as creator
from ytclip.settings import Settings
from ytclip.studio.orchestrator import StudioRequest, read_request
from ytclip.telegram import studio_handler as S
from ytclip.telegram import state

CATS = [{"id": "p1", "name": "Psychology", "account": "@PsychologyFacts"},
        {"id": "h1", "name": "History", "account": "@oldworld", "creator_captions": "karaoke"},
        {"id": "g1", "name": "Gaming"}]


def cats(extra=()):
    return categories.load(Settings(categories=CATS + list(extra)))


@pytest.mark.parametrize("text, kind, idea, more", [
    ("Make a 65s video about 3 dark psychology tricks for @PsychologyFacts", "make", "3 dark psychology tricks",
     {"seconds": 65.0, "handle": "@psychologyfacts", "sure": True}),
    ("Turn this link https://example.org/news/rome into a reel for History", "link", "for History",
     {"url": "https://example.org/news/rome", "sure": True}),
    ("Hey, could you make a short video on the Bermuda Triangle with a british voice", "make", "the Bermuda Triangle",
     {"voice": "en_british_male"}),
    ("Make a 30 second video about speedrun glitches in zelda with karaoke captions", "make",
     "speedrun glitches in zelda", {"seconds": 30.0, "captions": "karaoke"}),
    ("a 2 minute video about the moon, no captions", "make", "the moon", {"seconds": 120.0, "captions": "off"}),
    ("https://example.org/news/a-story", "link", "", {"sure": False}),  # a bare link: an article, or a video on it?
    ("https://youtu.be/abc123", "clip", "", {"links": ["https://youtu.be/abc123"]}),
    ("how are you today", "", "how are you today", {"sure": False}),
    ("the 5 seconds rule", "", "the 5 seconds rule", {"seconds": 0.0}),  # a number in an idea is no length
])
def test_what_a_sentence_asks_for(text, kind, idea, more):
    ask = S.understand(text)
    assert (ask.kind, ask.idea) == (kind, idea)
    for key, value in more.items():
        assert getattr(ask, key) == value, key
    assert S.Ask.from_json(ask.to_json()) == ask and S.Ask.from_json("junk") == S.Ask()


def test_the_category_is_the_one_named_then_the_one_the_idea_fits_then_asked():
    cs = cats()

    def found(text):
        cat, how, idea = S.resolve(S.understand(text, assume="make"), cs)
        return (cat.name if cat else None), how, idea

    assert found("3 dark psychology tricks for @PsychologyFacts") == ("Psychology", "named", "3 dark psychology tricks")
    assert found("the fall of Carthage for History") == ("History", "named", "the fall of Carthage")
    assert found("the fall of Carthage, for the history account.") == ("History", "named", "the fall of Carthage")
    # nothing named: what the idea is about, when that's clear
    assert found("why Roman concrete lasted two thousand years") == ("History", "guessed",
                                                                      "why Roman concrete lasted two thousand years")
    assert found("the manipulation tricks narcissists use") == ("Psychology", "guessed",
                                                                 "the manipulation tricks narcissists use")
    assert found("speedrun glitches in zelda")[:2] == ("Gaming", "guessed")
    # not clear: asked. An account no category posts to: said
    assert found("my cat's birthday") == (None, "", "my cat's birthday")
    assert found("the history of gaming consoles")[0] is None  # two categories fit as well as each other
    assert found("the moon for @nobody") == (None, "unknown", "the moon")
    # an account that several categories post to: the one among them the idea fits
    shared = cats([{"id": "c1", "name": "True Crime", "account": "@oldworld"}])
    cat, how, _ = S.resolve(S.understand("make a video about an unsolved murder for @oldworld"), shared)
    assert (cat.name, how) == ("True Crime", "guessed")
    # a category's own words about itself count (Categories → what it's about)
    told = cats([{"id": "s1", "name": "Deep Dives", "instructions": "ocean, sharks and sea creatures"}])
    assert S.classify("the shark that lives 400 years", told)[0].name == "Deep Dives"


def test_a_guess_needs_to_be_clear_and_a_model_is_only_used_when_given():
    cs = cats()
    cat, score, how = S.classify("why Roman concrete lasted", cs)
    assert cat.name == "History" and how == "words" and score >= S.SURE["words"][0]
    assert S.classify("", cs)[0] is None and S.classify("something or other", cs)[0] is None

    class Embedder:  # a stand-in for the sentence model: a direction per text
        name = "model"
        like = {"Gaming": 0.9}

        def embed(self, texts):
            rows = [[1.0, 0.0]]  # the idea
            for text in texts[1:]:
                x = next((v for k, v in self.like.items() if k in text), 0.1)
                rows.append([x, (1 - x * x) ** 0.5])
            return np.array(rows)

    cat, score, how = S.classify("anything at all", cs, Embedder())
    assert (cat.name, score, how) == ("Gaming", 0.9, "model")

    class Unsure(Embedder):
        like = {"Gaming": 0.5, "History": 0.48}  # two alike: not a guess anyone should act on

    assert S.classify("anything at all", cs, Unsure())[0] is None

    class Broken(Embedder):
        def embed(self, texts):
            raise RuntimeError("the model didn't load")

    assert S.classify("anything at all", cs, Broken()) == (None, 0.0, "model")


def test_the_request_carries_the_categorys_choices_and_a_rewards_length():
    s = Settings(categories=CATS)
    history = cats()[1]
    req, said = S.build_request(S.understand("Make a 30 second video about the fall of Rome"), history, s)
    assert isinstance(req, StudioRequest) and (req.seconds, req.rewards, req.orientation, req.format) == (
        62.0, True, "vertical", "short")
    assert req.captions == "karaoke" and req.voice == "en_documentary" and req.category == "h1"
    assert said[0] == "posts to @oldworld" and "you said 30" in said[1] and "upright 9:16" in said
    assert "Calm Documentary voice" in said and "karaoke captions" in said
    # what the sentence says wins over the category; a length in range is kept; over it is brought down
    req, said = S.build_request(S.understand("a 75s video about Rome with pop captions with a british voice"), history, s)
    assert (req.seconds, req.captions, req.voice) == (75.0, "pop", "en_british_male") and "75 s" in said
    assert S.build_request(S.understand("a 3 minute video about Rome"), history, s)[0].seconds == 85.0
    assert S.build_request(S.understand("a video about Rome"), history, s)[0].seconds == 0  # the Studio's own 62-85
    assert [S.clamp_seconds(x) for x in (0, 10, 61, 62, 70.4, 85, 600)] == [0, 62, 62, 62, 70, 85, 85]
    # an article is always the Studio's; an idea is the classic Creator's when Settings say so
    s.telegram_engine = "creator"
    assert isinstance(S.build_request(S.understand("a video about Rome"), history, s)[0], creator.CreateRequest)
    link = S.build_request(S.understand("turn https://example.org/a into a video"), history, s)[0]
    assert isinstance(link, StudioRequest) and link.url == "https://example.org/a"


def test_presets_fill_what_a_request_leaves_open(tmp_path):
    s = Settings(categories=[{"id": "h1", "name": "History", "creator_voice": "en_british_male",
                              "creator_visuals": "stock", "creator_captions": "bounce", "creator_style": "documentary"}])
    got = creator.with_presets(StudioRequest(prompt="Rome", category="h1"), s)
    assert (got.voice, got.visuals, got.captions, got.style) == ("en_british_male", "stock", "bounce", "documentary")
    kept = creator.with_presets(StudioRequest(prompt="Rome", category="History", voice="en_male_deep", captions="off"), s)
    assert (kept.voice, kept.captions) == ("en_male_deep", "off")  # what the request says stays
    classic = creator.with_presets(creator.CreateRequest(prompt="Rome", category="h1"), s)
    assert classic.voice == "en_british_male" and classic.visuals == ""  # the classic Creator has no stock footage
    plain = StudioRequest(prompt="Rome", category="nope")
    assert creator.with_presets(plain, s) is plain
    edit = StudioRequest(prompt="Rome", category="h1", retouch={"folder": str(tmp_path), "source": "a.mp4",
                                                                 "name": "a (v2).mp4", "edits": {}})
    assert creator.with_presets(edit, s) is edit  # a finished video being changed keeps its own choices


@pytest.fixture
def asking(world):  # noqa: F811
    w = world
    writers(w)
    other = w.accounts.add("")
    other.username, other.logged_in = "oldworld", True
    w.accounts.save()
    w.s.categories = w.s.categories + [{"id": "h1", "name": "History", "account_id": other.id,
                                        "creator_captions": "karaoke"}]
    return w


def jobs(w):
    return sorted(w.runner.db.jobs(limit=20), key=lambda j: j.created)


def test_a_sentence_in_the_chat_queues_the_video_for_the_category_named(asking):
    w = asking
    sent = w.say("Make a 65s video about 3 dark psychology tricks for History")
    assert sent["text"].startswith("✨ Got it: a video about “3 dark psychology tricks” for History.")
    assert "for your OK before it posts" in sent["text"] and "🧭" not in sent["text"]
    assert "🎛 posts to @oldworld · 65 s · upright 9:16 · Calm Documentary voice · karaoke captions" in sent["text"]
    assert not buttons(sent.get("reply_markup"))
    job = jobs(w)[-1]
    req = read_request(job.request)
    assert job.is_creation and job.category_id == "h1" and job.origins == ["creator", "telegram"]
    assert isinstance(req, StudioRequest) and (req.prompt, req.seconds, req.rewards, req.captions) == (
        "3 dark psychology tricks", 65.0, True, "karaoke")
    assert state.load(w.state_file)["asked"][f"job:{job.id}"]["chat"] == JUSTYN["id"]
    # the account says it too
    sent = w.say("make a video about the fall of Carthage for @oldworld")
    assert "for History" in sent["text"] and jobs(w)[-1].category_id == "h1"


def test_a_link_to_an_article_becomes_a_video_and_a_bare_link_is_asked_about(asking):
    w = asking
    sent = w.say("Turn this link https://example.org/news/rome-concrete into a reel for History")
    assert sent["text"].startswith("🔗 Got it: a video from the article at example.org for History.")
    req = read_request(jobs(w)[-1].request)
    assert isinstance(req, StudioRequest) and req.url == "https://example.org/news/rome-concrete" and req.prompt == ""
    n = len(jobs(w))
    sent = w.say("https://example.org/news/a-story")
    assert sent["text"] == "🔗 A link to example.org. What should I do with it?"
    read, clip, _nothing = buttons(sent["reply_markup"])
    assert read[0] == "📰 Make a video from the article" and clip[0] == "✂️ Clip the video on that page"
    assert len(jobs(w)) == n
    w.press(read[1])
    asked = w.fake.last()
    assert asked["text"].startswith("✨ A video from example.org. Which category is it for?")
    w.press(buttons(asked["reply_markup"])[0][1])
    assert len(jobs(w)) == n + 1 and read_request(jobs(w)[-1].request).url == "https://example.org/news/a-story"
    # a video link is clipped, as it always was; /make with one says which command takes it
    w.fake.clear()
    w.say("/make https://youtu.be/abc123")
    assert "That's a link to a video. /clip cuts clips from it" in w.fake.last()["text"]


def test_a_guessed_category_says_so_and_can_be_changed_while_it_waits(asking):
    w = asking
    sent = w.say("make a video about why Roman concrete lasted two thousand years")
    assert "for History" in sent["text"] and "🧭 It reads like one for <b>History</b>, so that's where I put it." in sent["text"]
    job = jobs(w)[-1]
    change = buttons(sent["reply_markup"])
    assert change == [("↔ Not History? Change it", f"rc:{job.id}")]
    said = max(k[1] for k in w.fake.messages)
    w.press(change[0][1], said)
    again = w.fake.last()
    assert again["text"].startswith("Which category is “why Roman concrete lasted two thousand years” for?")
    picks = buttons(again["reply_markup"])
    assert [label for label, _ in picks[:3]] == ["Psychology · @psych", "Gaming · @psych", "History · @oldworld"]
    w.press(picks[0][1])
    moved = w.runner.db.get(job.id)
    req = read_request(moved.request)
    assert moved.category_id == "p1" and req.category == "p1" and req.captions == "" and req.voice == "en_male_deep"
    text = w.fake.messages[(JUSTYN["id"], said)]["text"]
    assert "for Psychology" in text and "posts to @psych" in text and "🧭" not in text  # the first reply, put right
    assert any("moved “why Roman concrete lasted" in line for line in w.logs)
    # once it's being made it stays where it is
    w.runner.db.update(job.id, w.clock[0], status="working")
    assert "being made already" in w.bot.press_recat(JUSTYN["id"], said, job.id, None)


def test_when_the_category_cant_be_told_the_keyboard_shows_each_account(asking):
    w = asking
    n = len(jobs(w))
    sent = w.say("make me a video about my cat's birthday")
    assert sent["text"] == "✨ A video about “my cat's birthday”. Which category is it for?"
    rows = sent["reply_markup"]["inline_keyboard"]
    assert [r[0]["text"] for r in rows[:3]] == ["Psychology · @psych", "Gaming · @psych", "History · @oldworld"]
    assert all(len(r) == 1 for r in rows[:3]) and [b["text"] for b in rows[-1]] == ["No category", "✖ Cancel"]
    assert len(jobs(w)) == n
    w.press(rows[2][0]["callback_data"])
    assert len(jobs(w)) == n + 1 and jobs(w)[-1].category_id == "h1"
    assert "a video about “my cat's birthday” for History" in w.fake.last()["text"]
    sent = w.say("a video about the moon for @nobody")
    assert "No category posts to @nobody. Which category is it for?" in sent["text"]
    # words that don't ask for a video are still asked about, as before
    sent = w.say("how are you today")
    assert sent["text"].startswith("What should I do with “how are you today”?")


def test_asking_in_a_sentence_from_the_app(served):  # noqa: F811
    w = served
    tok = w.session(SAM)["token"]
    status, out, _ = w.call("POST", "/api/make", {"idea": "Make a 65s video about dark psychology tricks",
                                                  "category": "auto"}, token=tok)
    assert status == 200 and out["category"] == "Psychology" and out["how"] == "guessed" and "<" not in out["message"]
    assert "a video about “dark psychology tricks” for Psychology" in out["message"]
    req = read_request(w.runner.db.get(out["job"]).request)
    assert (req.prompt, req.seconds) == ("dark psychology tricks", 65.0)
    # the length picked in the app; an article's link; a category that can't be told has to be picked
    status, out, _ = w.call("POST", "/api/make", {"idea": "https://example.org/news/x for Gaming", "category": "auto",
                                                  "seconds": 300}, token=tok)
    req = read_request(w.runner.db.get(out["job"]).request)
    assert status == 200 and out["category_id"] == "g1" and (req.url, req.seconds) == ("https://example.org/news/x", 85.0)
    status, err, _ = w.call("POST", "/api/make", {"idea": "my cat's birthday", "category": "auto"}, token=tok)
    assert status == 422 and "Pick one" in err["error"]
    assert w.call("POST", "/api/make", {"idea": "https://youtu.be/abc", "category": "auto"}, token=tok)[0] == 400
    assert w.call("POST", "/api/make", {"idea": "x" * 1400, "category": "p1"}, token=tok)[0] == 400
    assert w.call("POST", "/api/make", {"idea": "the moon", "category": "p1", "seconds": "long"}, token=tok)[0] == 400
    status, out, _ = w.call("POST", "/api/make", {"idea": "the moon", "category": ""}, token=tok)
    assert status == 200 and out["category_id"] == "" and w.runner.db.get(out["job"]).category_id == ""

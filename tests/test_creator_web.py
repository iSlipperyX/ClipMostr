"""Looking a topic up online before the Creator writes a script (creator/web.py): the searches, reading
articles, the prompt with today's date, and the sources kept with the video. No network: a fake opener
serves recorded answers."""

import json
import time
import urllib.error
import urllib.parse

import pytest

from ytclip import ai
from ytclip.creator import director, orchestrator, web
from ytclip.settings import Settings

GOOGLE = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>GTA 6</title>
<item><title>GTA 6 release date confirmed for November 19, 2026 - IGN</title>
<link>https://news.google.com/rss/articles/CBMiAAA?oc=5</link>
<pubDate>Thu, 06 Nov 2026 14:00:00 GMT</pubDate>
<description>&lt;a href="https://news.google.com/x"&gt;GTA 6 release date confirmed for November 19, 2026&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color="#6f6f6f"&gt;IGN&lt;/font&gt;</description>
<source url="https://www.ign.com">IGN</source></item>
<item><title>Rockstar shows GTA 6 trailer 3 - The Verge</title>
<link>https://news.google.com/rss/articles/CBMiBBB?oc=5</link>
<pubDate>Tue, 21 Oct 2026 09:30:00 GMT</pubDate>
<description>Trailer 3 is here</description><source url="https://www.theverge.com">The Verge</source></item>
<item><title>GTA 6 delayed to 2026 - Polygon</title>
<link>https://news.google.com/rss/articles/CBMiCCC?oc=5</link>
<pubDate>Fri, 02 May 2025 16:00:00 GMT</pubDate>
<source url="https://www.polygon.com">Polygon</source></item>
</channel></rss>"""

BING = b"""<?xml version="1.0" encoding="utf-8"?><rss version="2.0" xmlns:News="https://www.bing.com/news/search?q=x&amp;format=rss"><channel>
<item><title>GTA 6 release date confirmed for November 19, 2026</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;url=https%3a%2f%2fwww.ign.com%2farticles%2fgta-6-date&amp;c=1</link>
<description>Rockstar confirmed the date in a post on Thursday.</description>
<pubDate>Thu, 06 Nov 2026 14:05:00 GMT</pubDate><News:Source>IGN</News:Source></item>
<item><title>Take-Two earnings: GTA 6 on track</title>
<link>http://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fwww.gamespot.com%2ftake-two&amp;c=2</link>
<description>Take-Two says GTA 6 is on track.</description>
<pubDate>Wed, 05 Nov 2026 22:00:00 GMT</pubDate><News:Source>GameSpot</News:Source></item>
</channel></rss>"""

ARTICLE = b"""<html><head><script>var a = "<p>not this</p>";</script></head><body>
<nav><p>Home. News. Reviews. Guides and more links in a menu that should be skipped.</p></nav>
<article><h1>GTA 6 date</h1>
<p>Rockstar Games confirmed on Thursday that Grand Theft Auto VI will release on November 19, 2026, for PS5 and Xbox Series X.</p>
<p>Photo: Rockstar</p>
<p>The game was first planned for fall 2025 before being delayed twice, the company said in a short statement today.</p>
<p>Subscribe to our newsletter for more news like this every single day of the week!</p>
</article><footer><p>All rights reserved by the publisher of this website and its partners.</p></footer></body></html>"""

WIKI = json.dumps({"query": {"pages": [{"title": "Grand Theft Auto VI", "fullurl":
                   "https://en.wikipedia.org/wiki/Grand_Theft_Auto_VI",
                   "revisions": [{"timestamp": "2026-11-07T10:00:00Z"}],
                   "extract": "Grand Theft Auto VI is an upcoming action-adventure game by Rockstar Games. "
                              "It is scheduled for release on 19 November 2026."}]}}).encode()


def opener_for(answers):
    """A fake urllib: the first key found in the URL picks the answer (bytes, or an exception to raise)."""
    asked = []

    def opener(url, timeout):
        asked.append(url)
        for key, answer in answers.items():
            if key in url:
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        raise urllib.error.URLError("not recorded")

    opener.asked = asked
    return opener


ALL = {"news.google.com/rss": GOOGLE, "bing.com/news": BING, "wikipedia.org": WIKI, "ign.com": ARTICLE,
       "gamespot.com": urllib.error.URLError("timed out")}


def test_query_drops_the_asking_words():
    assert web.query_of("Make a video with the latest GTA 6 news") == "GTA 6"
    assert web.query_of("Tell me everything you need to know about the James Webb telescope") == \
        "James Webb telescope"
    assert web.query_of("the latest news") == "the latest news"  # nothing left: search what was asked


def test_google_news_items():
    items = web.parse_rss(GOOGLE)
    assert [s.title for s in items][:2] == ["GTA 6 release date confirmed for November 19, 2026",
                                           "Rockstar shows GTA 6 trailer 3"]
    assert items[0].publisher == "IGN" and items[0].date == "2026-11-06T14:00:00Z"
    assert "href" not in items[0].summary
    assert web.parse_rss(b"<html>not a feed") == []


def test_bing_links_go_straight_to_the_publisher():
    items = web.bing_news("GTA 6", opener_for({"bing.com": BING}))
    assert items[0].url == "https://www.ign.com/articles/gta-6-date"
    assert items[0].publisher == "IGN"


def test_article_text_keeps_sentences_only():
    text = web.page_text(ARTICLE)
    assert text.startswith("Rockstar Games confirmed on Thursday")
    assert "delayed twice" in text
    for junk in ("not this", "Photo:", "Subscribe", "menu", "All rights reserved"):
        assert junk not in text
    assert len(web.page_text(ARTICLE.replace(b"<p>The", b"<p>" + b"Long sentence here. " * 200 + b"The"), 300)) <= 300


def test_wikipedia_intro():
    got = web.wikipedia("GTA 6", opener_for({"wikipedia.org": WIKI}))
    assert got[0].title == "Grand Theft Auto VI" and got[0].kind == "wiki"
    assert "19 November 2026" in got[0].text and got[0].date.startswith("2026-11-07")


def test_gather_newest_first_once_each_and_reads_articles():
    logs = []
    res = web.gather("the latest GTA 6 news", log=logs.append, opener=opener_for(ALL))
    news = [s for s in res.sources if s.kind == "news"]
    assert [s.date[:10] for s in news] == sorted((s.date[:10] for s in news), reverse=True)
    titles = [s.title for s in news]
    assert titles.count("GTA 6 release date confirmed for November 19, 2026") == 1  # in both feeds: once
    ign = next(s for s in news if "ign.com" in s.url)
    assert "November 19, 2026" in ign.text  # read from the publisher
    assert res.sources[-1].kind == "wiki"
    assert not res.problems
    assert "newest 2026-11-06" in logs[0] and "read 1 article" in logs[0]


def test_gather_carries_on_when_sites_are_down():
    logs = []
    res = web.gather("GTA 6", log=logs.append, opener=opener_for({"wikipedia.org": WIKI,
                                                                 "google": urllib.error.URLError("blocked"),
                                                                 "bing": TimeoutError("timed out")}))
    assert [s.kind for s in res.sources] == ["wiki"] and len(res.problems) == 2
    down = web.gather("GTA 6", log=logs.append, opener=opener_for({}))
    assert not down.found and "Couldn't look it up online" in logs[-1]


def test_brief_has_todays_date_the_sources_and_the_rules():
    now = time.mktime((2026, 11, 8, 12, 0, 0, 0, 0, -1))
    res = web.gather("GTA 6 news", opener=opener_for(ALL))
    text = web.brief(res, now)
    assert "Today's date is Sunday 8 November 2026" in text
    assert "[1] GTA 6 release date confirmed for November 19, 2026 — IGN, 2026-11-06" in text
    assert "newest source wins" in text and "Rockstar Games confirmed" in text
    assert len(web.brief(res, now, limit=600)) < 1100
    offline = web.brief(None, now)
    assert "8 November 2026" in offline and "don't state dates" in offline


def _good_json():
    scenes = [{"scene_id": 1, "speech_text": "GTA 6 finally has a date.", "visual_type": "generated_image",
               "visual_prompt": "neon city", "camera_motion": "slow_push_in", "sfx_cue": "bass_drop",
               "sfx_offset_ms": 0}]
    for i in range(2, 11):
        scenes.append({"scene_id": i, "speech_text": " ".join(["Rockstar"] + [f"w{i}x{k}" for k in range(16)]) + ".",
                       "visual_type": "generated_image", "visual_prompt": "city", "camera_motion": "pan_left",
                       "sfx_cue": "none", "sfx_offset_ms": 0})
    return {"title": "GTA 6 date", "category": "Gaming", "target_duration": 65, "music_mood": "Epic", "hook": "",
            "caption": "", "hashtags": [], "scenes": scenes}


def test_the_script_writer_gets_the_facts(monkeypatch):
    monkeypatch.setattr(director, "script_writers", lambda s: ["local"])
    prompts = []

    def ask(s, task, system, prompt, schema, **kw):
        prompts.append((system, prompt))
        return ai.Answer(_good_json(), "m", "local")

    director.write(Settings(), director.Brief("GTA 6", facts="Today's date is X. [1] Source"), ask=ask)
    system, prompt = prompts[0]
    assert "Today's date is X. [1] Source" in prompt and "beat what you" in system
    director.write(Settings(), director.Brief("GTA 6"), ask=ask)
    assert "Today's date is" in prompts[-1][1]  # always today's date, even without looking it up


def test_the_storyboard_keeps_its_sources(monkeypatch):
    real_gather = web.gather
    calls = {}

    def gather(prompt, log=None, check=None):
        calls["prompt"] = prompt
        return real_gather(prompt, log=log, check=check, opener=opener_for(ALL))

    monkeypatch.setattr(web, "gather", gather)
    seen = {}

    def write(s, brief, **kw):
        seen["facts"] = brief.facts
        board = director.normalise(director.Storyboard.from_json(_good_json()))
        return board

    monkeypatch.setattr(director, "write", write)
    maker = orchestrator.VideoCreatorOrchestrator(Settings())
    board = maker.storyboard(orchestrator.CreateRequest(prompt="the latest GTA 6 news"))
    assert calls["prompt"] == "the latest GTA 6 news"
    assert "November 19, 2026" in seen["facts"]
    assert board.sources[0]["publisher"] == "IGN" and board.sources[-1]["kind"] == "wiki"
    again = director.Storyboard.from_json(json.loads(board.dumps()))
    assert again.sources == board.sources  # an edited storyboard keeps them
    text = orchestrator.readme(board, {"pictures": []}, {"length": 64.0}, "01.mp4")
    assert "## Sources looked up" in text and "(IGN, 2026-11-06)" in text
    # Off in Settings: nothing is looked up, the date still goes in.
    calls.clear()
    maker = orchestrator.VideoCreatorOrchestrator(Settings().apply({"creator_web": False}))
    board = maker.storyboard(orchestrator.CreateRequest(prompt="the latest GTA 6 news"))
    assert not calls and board.sources == [] and "Today's date is" in seen["facts"]
    # Your own script: nothing to look up.
    board = maker.storyboard(orchestrator.CreateRequest(script="My own words about the game. And more of them."))
    assert board.sources == []

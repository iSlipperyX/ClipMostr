import urllib.parse

import pytest
from conftest import NOW, api_item

from ytclip import sources
from ytclip.research import ScanOptions, scan
from ytclip.sources import ChannelRef, gather, parse_channel, parse_channels, parse_searches
from ytclip.trending import ChartUnavailable, DeepInfo, History, ResearchBlocked, video_from_api

UC_ID = "UC" + "x" * 22


@pytest.mark.parametrize("text,kind,value", [
    ("@SomePodcast", "handle", "SomePodcast"),
    ("somepodcast", "handle", "somepodcast"),
    ("https://www.youtube.com/@SomePodcast/videos", "handle", "SomePodcast"),
    ("youtube.com/@SomePodcast", "handle", "SomePodcast"),
    (f"https://www.youtube.com/channel/{UC_ID}", "id", UC_ID),
    (UC_ID, "id", UC_ID),
    ("https://www.youtube.com/user/oldname", "username", "oldname"),
    ("https://www.youtube.com/c/CustomName", "handle", "CustomName"),
])
def test_parse_channel(text, kind, value):
    ref = parse_channel(text)
    assert (ref.kind, ref.value) == (kind, value)


def test_parse_channels_keeps_notes_dedupes_and_reports_junk():
    refs, bad = parse_channels("@Pod  # clipping programme, credit in caption\n"
                               "@pod\n"
                               "https://www.youtube.com/watch?v=abc\n"
                               "@Gamer, @Other\n\n")
    assert [r.label for r in refs] == ["@Pod", "@Gamer", "@Other"]
    assert refs[0].note == "clipping programme, credit in caption"
    assert bad == ["https://www.youtube.com/watch?v=abc"]


def test_parse_searches():
    assert parse_searches("gaming highlights\n  Gaming   Highlights \n# a comment\npodcast clips # why") == [
        "gaming highlights", "podcast clips"]


class FakeAPI:
    """Answers the four YouTube Data API calls sources.py makes, and counts them."""

    def __init__(self, videos=None, channels=None, uploads=None, search=None, fail=None):
        self.videos = {v["id"]: v for v in (videos or [])}
        self.channels = channels or {}  # forHandle/id -> channel id
        self.uploads = uploads or {}  # playlist -> [video ids]
        self.search = search or {}  # query -> [video ids]
        self.fail = fail or {}  # endpoint -> exception
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        q = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((endpoint, q))
        if endpoint in self.fail:
            raise self.fail[endpoint]
        if endpoint == "channels":
            who = q.get("forHandle") or q.get("id") or q.get("forUsername")
            cid = self.channels.get(who)
            return {"items": [{"id": cid, "snippet": {"title": who},
                               "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}}] if cid else []}
        if endpoint == "playlistItems":
            ids = self.uploads.get(q["playlistId"], [])[:int(q["maxResults"])]
            return {"items": [{"contentDetails": {"videoId": i}} for i in ids]}
        if endpoint == "search":
            assert q["videoLicense"] == "creativeCommon" and q["type"] == "video"
            return {"items": [{"id": {"videoId": i}} for i in self.search.get(q["q"], [])]}
        if endpoint == "videos":
            assert "status" in q["part"]
            return {"items": [self.videos[i] for i in q["id"].split(",") if i in self.videos]}
        raise AssertionError(url)

    def count(self, endpoint):
        return sum(e == endpoint for e, _ in self.calls)


def _api():
    return FakeAPI(
        videos=[api_item("p1", channel="Pod", hours_old=5), api_item("p2", channel="Pod", hours_old=30),
                api_item("old", channel="Pod", hours_old=24 * 30),
                api_item("cc1", channel="Free", license="creativeCommon", hours_old=10)],
        channels={"@Pod": "UCPod"},
        uploads={"UUPod": ["p1", "p2", "old"]},
        search={"gaming highlights": ["cc1", "p1"]},
    )


def test_gather_reads_channels_and_searches_and_tags_origins():
    api, history = _api(), History(None)
    g = gather("KEY", [parse_channel("@Pod", "allows clips")], ["gaming highlights"], history, NOW, get=api)
    by_id = {v.id: v for v in g.videos}
    assert set(by_id) == {"p1", "p2", "cc1"}  # "old" is past MAX_AGE_DAYS
    assert by_id["p1"].origins == ["cc", "watchlist"]
    assert by_id["cc1"].origins == ["cc"] and by_id["cc1"].license == "creativeCommon"
    assert g.watch_ids == {"UCPod"} and g.notes == {"UCPod": "allows clips"}
    # 1 to find the channel, 1 for its uploads, 100 for the search, 1 for the video details
    assert g.quota == 103
    assert history.sources["channels"]["@pod"]["id"] == "UCPod"


def test_gather_only_rereads_sources_when_due():
    api, history = _api(), History(None)
    refs = [parse_channel("@Pod")]
    gather("KEY", refs, ["gaming highlights"], history, NOW, get=api)
    g = gather("KEY", refs, ["gaming highlights"], history, NOW + 600, get=api)
    assert (api.count("channels"), api.count("playlistItems"), api.count("search")) == (1, 1, 1)
    assert api.count("videos") == 2  # fresh view counts every scan, for momentum
    assert g.quota == 1 and {v.id for v in g.videos} == {"p1", "p2", "cc1"}

    gather("KEY", refs, ["gaming highlights"], history, NOW + sources.CHANNEL_EVERY + 1, get=api)
    assert (api.count("playlistItems"), api.count("search")) == (2, 1)
    gather("KEY", refs, ["gaming highlights"], history, NOW + sources.SEARCH_EVERY + 1, get=api)
    assert api.count("search") == 2


def test_gather_reports_unknown_channels_and_retries_them_a_day_later():
    api, history = _api(), History(None)
    refs = [parse_channel("@Nobody")]
    g = gather("KEY", refs, [], history, NOW, get=api)
    assert g.videos == [] and "No YouTube channel @Nobody" in g.problems[0]
    gather("KEY", refs, [], history, NOW + 3600, get=api)
    assert api.count("channels") == 1
    gather("KEY", refs, [], history, NOW + sources.RESOLVE_RETRY + 1, get=api)
    assert api.count("channels") == 2


def test_gather_raises_when_every_call_would_fail():
    api = FakeAPI(fail={"channels": ResearchBlocked("The YouTube API key was rejected.")})
    with pytest.raises(ResearchBlocked):
        gather("KEY", [parse_channel("@Pod")], [], History(None), NOW, get=api)


def test_a_failed_search_is_reported_and_not_retried_until_due():
    api = FakeAPI(fail={"search": ChartUnavailable("Bad request")})
    history = History(None)
    g = gather("KEY", [], ["x"], history, NOW, get=api)
    assert "Creative Commons search \"x\"" in g.problems[0]
    gather("KEY", [], ["x"], history, NOW + 60, get=api)
    assert api.count("search") == 1


def test_removed_sources_are_forgotten():
    api, history = _api(), History(None)
    gather("KEY", [parse_channel("@Pod")], ["gaming highlights"], history, NOW, get=api)
    gather("KEY", [], [], history, NOW + 60, get=api)
    assert history.sources == {"channels": {}, "searches": {}}


def test_sources_survive_a_restart(tmp_path):
    api = _api()
    history = History(tmp_path / "research.json")
    gather("KEY", [parse_channel("@Pod")], [], history, NOW, get=api)
    history.save()
    again = History(tmp_path / "research.json")
    gather("KEY", [parse_channel("@Pod")], [], again, NOW + 60, get=api)
    assert api.count("channels") == 1 and api.count("playlistItems") == 1


def test_scan_merges_sources_with_charts_and_marks_listed_channels():
    chart = [api_item("hot", channel="Pod", minutes=30), api_item("other", channel="Else", minutes=30)]

    def fetch(key, region, category, limit):
        out = []
        for rank, item in enumerate(chart, 1):
            v = video_from_api(item)
            v.regions, v.origins = {region: rank}, ["chart"]
            out.append(v)
        return out

    def fake_sources(key, channels, searches, history, now, log=None, language=""):
        assert language == ""
        v = video_from_api(api_item("listed", channel="Pod", minutes=40))
        v.origins = ["watchlist"]
        dup = video_from_api(api_item("hot", channel="Pod", minutes=30))
        dup.origins = ["cc"]
        return sources.Gathered([v, dup], quota=3, problems=["one problem"], watch_ids={"UCPod"},
                                notes={"UCPod": "ok to clip"})

    opts = ScanOptions(api_key="K", regions=["US"], channels=[ChannelRef("@pod", "handle", "Pod")],
                       unreadable=["junk"], deep_count=0)
    result = scan(opts, History(None), fetch=fetch, sources=fake_sources, now=NOW,
                  deep=lambda *a, **k: DeepInfo())
    by_id = {v.id: v for v in result.videos}
    assert by_id["hot"].origins == ["cc", "chart", "watchlist"] and by_id["hot"].regions == {"US": 1}
    assert by_id["listed"].origins == ["watchlist"]
    assert by_id["other"].origins == ["chart"]
    assert result.quota == 4 and result.notes == {"UCPod": "ok to clip"}
    assert "one problem" in result.problems and any("junk" in p for p in result.problems)


def test_scan_skips_sources_when_none_are_set():
    called = []
    scan(ScanOptions(api_key="K", regions=["US"], deep_count=0), History(None), fetch=lambda *a: [],
         sources=lambda *a, **k: called.append(1), now=NOW)
    assert not called


def test_settings_feed_the_scan_options():
    from ytclip.settings import Settings

    s = Settings().apply({"pick_channels": "@Pod # fine\nnot a channel!", "pick_searches": "a\nb"})
    opts = ScanOptions.from_settings(s)
    assert [r.label for r in opts.channels] == ["@Pod"] and opts.searches == ["a", "b"]
    assert opts.unreadable == ["not a channel!"]
    assert opts.language == "en"
    assert ScanOptions.from_settings(Settings().apply({"pick_languages": "es-MX, en"})).language == "es"
    assert ScanOptions.from_settings(Settings().apply({"pick_languages": ""})).language == ""


def test_search_favours_the_picking_language():
    from ytclip.sources import search_cc
    api = FakeAPI(search={"talk": ["a"]})
    assert search_cc("KEY", "talk", NOW - 86400, get=api, language="en") == ["a"]
    assert api.calls[-1][1]["relevanceLanguage"] == "en"
    search_cc("KEY", "talk", NOW - 86400, get=api)
    assert "relevanceLanguage" not in api.calls[-1][1]

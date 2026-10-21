import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import NOW, api_item

from ytclip import trending
from ytclip.trending import (ChartUnavailable, DeepInfo, History, ResearchError, TrendingVideo, deep_from_info,
                             fetch_chart, parse_duration, parse_regions, video_from_api)


def test_parse_regions_normalises_and_dedupes():
    assert parse_regions("us, gb ca;US / au") == ["US", "GB", "CA", "AU"]
    assert parse_regions("usa, x, 12, DE") == ["DE"]
    assert parse_regions("") == []


@pytest.mark.parametrize("iso,seconds", [
    ("PT15S", 15), ("PT4M2S", 242), ("PT1H", 3600), ("PT1H2M3S", 3723), ("P1DT2H", 93600), ("P0D", 0),
    ("", 0), ("garbage", 0), ("PT1.5S", 1.5),
])
def test_parse_duration(iso, seconds):
    assert parse_duration(iso) == seconds


def test_video_from_api_reads_every_field():
    v = video_from_api(api_item("xyz", "Title", views=1234, likes=None, comments=7, minutes=61.5, hours_old=2,
                                category="23", caption=True, channel="Funny"))
    assert (v.id, v.title, v.channel, v.channel_id) == ("xyz", "Title", "Funny", "UCFunny")
    assert v.views == 1234 and v.likes is None and v.comments == 7
    assert v.duration == 61.5 * 60
    assert v.published == NOW - 2 * 3600
    assert v.category == "23" and v.captions and v.live == "none"
    assert v.thumbnail.endswith("/xyz/hqdefault.jpg")
    assert v.url == "https://www.youtube.com/watch?v=xyz"


def test_video_round_trips_through_json():
    v = video_from_api(api_item())
    v.regions = {"US": 3, "GB": 1}
    v.deep = DeepInfo(heatmap=[{"start_time": 0, "end_time": 1, "value": 1}], chapters=2, captions=True)
    back = TrendingVideo.from_json(json.loads(json.dumps(v.to_json())))
    assert back == v
    assert back.best_rank == 1


def test_fetch_chart_pages_and_ranks():
    calls = []

    def get(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        calls.append(q)
        if "pageToken" not in q:
            return {"items": [api_item(f"a{i}") for i in range(50)], "nextPageToken": "P2"}
        return {"items": [api_item(f"b{i}") for i in range(20)]}

    videos = fetch_chart(" KEY ", "GB", "20", limit=70, get=get)
    assert len(videos) == 70
    assert [v.regions["GB"] for v in videos[:2]] == [1, 2] and videos[-1].regions["GB"] == 70
    assert calls[0]["chart"] == "mostPopular" and calls[0]["regionCode"] == "GB"
    assert calls[0]["videoCategoryId"] == "20" and calls[0]["key"] == "KEY"
    assert calls[0]["maxResults"] == "50" and calls[1]["maxResults"] == "20" and calls[1]["pageToken"] == "P2"


def test_fetch_chart_stops_when_chart_runs_out():
    pages = iter([{"items": [api_item("only")], "nextPageToken": ""}])
    videos = fetch_chart("KEY", "US", "", limit=200, get=lambda url: next(pages))
    assert [v.id for v in videos] == ["only"]


def test_fetch_chart_omits_category_for_all():
    seen = []
    fetch_chart("KEY", "US", "", limit=5, get=lambda url: seen.append(url) or {"items": []})
    assert "videoCategoryId" not in seen[0]


def test_fetch_chart_needs_a_key():
    with pytest.raises(ResearchError, match="API key"):
        fetch_chart("  ", "US", get=lambda url: pytest.fail("should not call the API"))


# The real body YouTube returns for a bad key (captured 2026-10-21).
BAD_KEY = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                     "errors": [{"message": "API key not valid. Please pass a valid API key.", "domain": "global",
                                 "reason": "badRequest"}],
                     "status": "INVALID_ARGUMENT",
                     "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]}}


def _body(reason, message="x", code=403):
    return json.dumps({"error": {"code": code, "message": message, "errors": [{"reason": reason}]}}).encode()


@pytest.mark.parametrize("status,body,cls,text", [
    (400, json.dumps(BAD_KEY).encode(), ResearchError, "key was rejected"),
    (403, _body("quotaExceeded"), ResearchError, "quota is used up"),
    (403, _body("accessNotConfigured", "YouTube Data API v3 has not been used in project 1"), ResearchError,
     "isn't enabled"),
    (404, _body("videoChartNotFound", "The requested video chart is not supported", 404), ChartUnavailable, ""),
    (400, _body("invalidArgument", "Invalid regionCode 'ZZ'", 400), ChartUnavailable, ""),
    (503, b"<html>", ResearchError, "busy"),
    (418, _body("teapot", "short and stout", 418), ResearchError, "418: short and stout"),
])
def test_friendly_errors(status, body, cls, text):
    err = trending._friendly(status, body)
    assert type(err) is cls
    assert text in str(err)


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):
        status, body = self.routes[urllib.parse.urlsplit(self.path).path]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_get_over_http(server):
    _Handler.routes = {"/ok": (200, json.dumps({"items": [api_item("live")]}).encode()),
                       "/quota": (403, _body("quotaExceeded")),
                       "/junk": (200, b"not json")}
    assert trending._get(server + "/ok")["items"][0]["id"] == "live"
    with pytest.raises(ResearchError, match="quota"):
        trending._get(server + "/quota")
    with pytest.raises(ResearchError, match="couldn't be read"):
        trending._get(server + "/junk")


def test_get_unreachable():
    with pytest.raises(ResearchError, match="Couldn't reach YouTube"):
        trending._get("http://127.0.0.1:9/nothing", timeout=2)


def test_deep_from_info():
    info = {"heatmap": [{"start_time": 0, "end_time": 5, "value": 0.5}, {"start_time": 5}],
            "chapters": [{}, {}, {}], "subtitles": {"live_chat": []}, "automatic_captions": {"en": []},
            "like_count": 99}
    d = deep_from_info(info)
    assert d.heatmap == [{"start_time": 0.0, "end_time": 5.0, "value": 0.5}]
    assert d.chapters == 3 and d.captions and d.likes == 99 and not d.error
    assert not deep_from_info({"subtitles": {"live_chat": []}}).captions


def test_deep_check_reports_failures(monkeypatch):
    import yt_dlp

    class Boom:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            raise yt_dlp.utils.DownloadError("ERROR: Video unavailable")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Boom)
    logged = []
    d = trending.deep_check("https://youtu.be/x", log=logged.append)
    assert d.error == "Video unavailable" and d.checked > 0
    assert "Video unavailable" in logged[0]


# ---------------------------------------------------------------- history


def _video(vid="v", views=1000, hours_old=10):
    return video_from_api(api_item(vid, views=views, hours_old=hours_old))


def test_history_lifetime_then_recent_velocity(tmp_path):
    h = History(tmp_path / "r.json")
    v = _video(views=10_000, hours_old=10)
    h.record(v, NOW)
    assert v.new and not v.recent and v.velocity == pytest.approx(1000)

    later = _video(views=40_000, hours_old=11)
    h.record(later, NOW + 3600)
    assert not later.new and later.recent and later.velocity == pytest.approx(30_000)
    assert later.first_seen == NOW


def test_history_ignores_too_recent_snapshots(tmp_path):
    h = History(None)
    h.record(_video(views=10_000), NOW)
    v = _video(views=10_500)
    h.record(v, NOW + 5 * 60)  # five minutes is too noisy to call momentum
    assert not v.recent


def test_history_keeps_a_bounded_number_of_snapshots():
    h = History(None)
    for i in range(40):
        h.record(_video(views=1000 + i), NOW + i * 600)
    assert len(h.videos["v"]["snapshots"]) == trending.SNAPSHOTS


def test_history_deep_cache_expires():
    h = History(None)
    h.record(_video(), NOW)
    h.store_deep("v", DeepInfo(chapters=2, checked=NOW))
    assert h.cached_deep("v", NOW + 60).chapters == 2
    assert h.cached_deep("v", NOW + trending.DEEP_FRESH + 1) is None
    h.store_deep("v", DeepInfo(checked=NOW, error="blocked"))
    assert h.cached_deep("v", NOW + 60) is None  # failures are retried next scan


def test_history_saves_loads_and_forgets(tmp_path):
    path = tmp_path / "research.json"
    h = History(path)
    old, fresh = _video("old"), _video("fresh")
    h.record(old, NOW)
    h.record(fresh, NOW + trending.FORGET_AFTER)
    h.mark_sent("fresh")
    h.remember([fresh], {"regions": ["US"]}, NOW + trending.FORGET_AFTER + 10)
    h.save()

    back = History(path)
    assert set(back.videos) == {"fresh"}
    assert back.videos["fresh"]["sent"]
    assert [v.id for v in back.last_videos()] == ["fresh"]
    assert back.last_scan["regions"] == ["US"]

    again = _video("fresh")
    back.record(again, NOW + trending.FORGET_AFTER + 20)
    assert again.sent


def test_history_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "research.json"
    path.write_text("{not json", encoding="utf-8")
    h = History(path)
    assert h.videos == {} and h.last_videos() == []

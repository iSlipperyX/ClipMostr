import pytest
from conftest import NOW, api_item, spiky_heatmap

from ytclip.research import ScanCancelled, ScanOptions, last_result, scan, youtube_key
from ytclip.settings import Settings
from ytclip.trending import ChartUnavailable, DeepInfo, History, ResearchError, video_from_api

CHARTS = {
    "US": [api_item("shared", views=900_000, minutes=40), api_item("us1", views=500_000, minutes=25),
           api_item("short", views=5_000_000, minutes=0.5), api_item("soon", live="upcoming"),
           api_item("stream", live="live", minutes=0)],
    "GB": [api_item("gb1", views=80_000, minutes=12), api_item("shared", views=910_000, minutes=40)],
}


def fake_fetch(charts=CHARTS, fail=None):
    def fetch(key, region, category, limit):
        if fail and region in fail:
            raise fail[region]
        out = []
        for rank, item in enumerate(charts.get(region, []), 1):
            v = video_from_api(item)
            v.regions[region] = rank
            out.append(v)
        return out[:limit]
    return fetch


class FakeDeep:
    def __init__(self):
        self.calls = []

    def __call__(self, url, cookies=None, log=None):
        self.calls.append(url.rsplit("=", 1)[-1])
        return DeepInfo(heatmap=spiky_heatmap(1500, (400, 900)), captions=True, checked=NOW)


def opts(**kw):
    base = dict(api_key="KEY", regions=["US", "GB"], min_minutes=3, deep_count=10)
    return ScanOptions(**(base | kw))


def test_scan_merges_regions_filters_and_ranks():
    deep = FakeDeep()
    logs = []
    result = scan(opts(), History(None), logs.append, fetch=fake_fetch(), deep=deep, now=NOW)
    ids = [v.id for v in result.videos]
    assert set(ids) == {"shared", "us1", "gb1", "stream"}  # short and upcoming are dropped, live is kept
    shared = next(v for v in result.videos if v.id == "shared")
    assert shared.regions == {"US": 1, "GB": 2} and shared.views == 910_000
    assert [v.score for v in result.videos] == sorted((v.score for v in result.videos), reverse=True)
    assert result.videos[-1].id == "stream"  # live streams can't be clipped yet
    assert set(deep.calls) == {"shared", "us1", "gb1"}  # never the live stream
    assert not any(v.new for v in result.videos)  # the first scan has nothing to compare with
    assert result.first
    assert result.quota == 2 and result.regions == ["US", "GB"]
    assert any("Skipped 2" in m for m in logs)


def test_scan_limits_deep_checks_and_reuses_cache():
    history = History(None)
    deep = FakeDeep()
    scan(opts(deep_count=1), history, fetch=fake_fetch(), deep=deep, now=NOW)
    assert len(deep.calls) == 1
    best = deep.calls[0]
    scan(opts(deep_count=1), history, fetch=fake_fetch(), deep=deep, now=NOW + 1800)
    assert deep.calls.count(best) == 1  # cached heatmap reused within DEEP_FRESH


def test_scan_survives_a_failing_deep_check():
    def boom(url, cookies=None, log=None):
        raise RuntimeError("network down")

    result = scan(opts(regions=["GB"]), History(None), fetch=fake_fetch(), deep=boom, now=NOW)
    assert all(v.deep and v.deep.error == "network down" for v in result.videos)
    assert all("replays" not in v.parts for v in result.videos)


def test_second_scan_measures_real_momentum():
    history = History(None)
    scan(opts(regions=["US"], deep_count=0), history, fetch=fake_fetch(), now=NOW)
    grown = {"US": [api_item("us1", views=800_000, minutes=25)]}
    result = scan(opts(regions=["US"], deep_count=0), history, fetch=fake_fetch(grown), now=NOW + 3600)
    v = result.videos[0]
    assert v.recent and v.velocity == pytest.approx(300_000) and not v.new
    newcomer = scan(opts(regions=["GB"], deep_count=0), history, fetch=fake_fetch(), now=NOW + 3700)
    assert not newcomer.first
    assert {v.id: v.new for v in newcomer.videos} == {"gb1": True, "shared": False}


def test_missing_category_chart_is_a_problem_not_a_failure():
    fetch = fake_fetch(fail={"GB": ChartUnavailable("notFound")})
    result = scan(opts(category="20"), History(None), fetch=fetch, deep=FakeDeep(), now=NOW)
    assert result.videos and result.problems == ["GB has no Gaming chart."]


def test_first_region_error_stops_the_scan():
    fetch = fake_fetch(fail={"US": ResearchError("The YouTube API key was rejected.")})
    with pytest.raises(ResearchError, match="rejected"):
        scan(opts(), History(None), fetch=fetch, now=NOW)


def test_later_region_error_keeps_earlier_results():
    fetch = fake_fetch(fail={"GB": ResearchError("Couldn't reach YouTube.")})
    result = scan(opts(), History(None), fetch=fetch, deep=FakeDeep(), now=NOW)
    assert result.videos and result.problems == ["GB: Couldn't reach YouTube."]


def test_scan_needs_regions():
    with pytest.raises(ResearchError, match="region"):
        scan(opts(regions=[]), History(None), fetch=fake_fetch(), now=NOW)


def test_scan_can_be_cancelled():
    flag = {"stop": False}

    def fetch(*a):
        flag["stop"] = True
        return fake_fetch()(*a)

    with pytest.raises(ScanCancelled):
        scan(opts(), History(None), fetch=fetch, cancelled=lambda: flag["stop"], now=NOW)


def test_last_result_restores_the_previous_scan(tmp_path):
    path = tmp_path / "research.json"
    first = scan(opts(), History(path), fetch=fake_fetch(), deep=FakeDeep(), now=NOW)
    restored = last_result(History(path))
    assert [v.id for v in restored.videos] == [v.id for v in first.videos]
    assert [v.score for v in restored.videos] == [v.score for v in first.videos]
    assert restored.time == NOW and restored.regions == ["US", "GB"]
    assert not any(v.new for v in restored.videos)
    assert last_result(History(tmp_path / "missing.json")) is None


def test_options_from_settings(monkeypatch):
    s = Settings().apply({"research_regions": "us, de", "research_category": "23", "research_per_region": 120,
                          "research_min_minutes": 8, "research_deep_count": 4, "youtube_api_key": " k1 "})
    o = ScanOptions.from_settings(s)
    assert (o.api_key, o.regions, o.category, o.per_region, o.min_minutes, o.deep_count) == \
        ("k1", ["US", "DE"], "23", 120, 8, 4)
    assert o.quota == 6
    monkeypatch.setenv("YOUTUBE_API_KEY", "from-env")
    assert youtube_key(Settings()) == "from-env"
    assert ScanOptions.from_settings(Settings().apply({"research_regions": "??"})).regions == ["US"]


def test_cancel_during_replay_checks_doesnt_wait_for_them():
    import threading
    import time

    flag = {"stop": False}
    release = threading.Event()

    def slow_deep(url, cookies=None, log=None):
        flag["stop"] = True
        release.wait(5)
        return DeepInfo(checked=NOW)

    started = time.monotonic()
    with pytest.raises(ScanCancelled):
        scan(opts(deep_count=10), History(None), fetch=fake_fetch(), deep=slow_deep,
             cancelled=lambda: flag["stop"], now=NOW)
    release.set()
    assert time.monotonic() - started < 4

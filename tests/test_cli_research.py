import json

import pytest
from conftest import NOW, api_item

from ytclip import cli, research
from ytclip.research import ScanResult
from ytclip.trending import video_from_api


def _result(n=3):
    videos = []
    for i in range(n):
        v = video_from_api(api_item(f"v{i}", title=f"Video {i}", hours_old=1))
        v.regions = {"US": i + 1}
        v.score, v.reasons, v.new = 90 - i * 10, [f"reason {i}"], True
        videos.append(v)
    return ScanResult(videos, NOW, ["US"])


@pytest.fixture
def fake_scan(monkeypatch):
    seen = {}

    def scan(opts, history, log=None, progress=None, **kw):
        seen["opts"] = opts
        return _result()

    monkeypatch.setattr(research, "scan", scan)
    return seen


def test_category_lookup():
    assert cli._category_id("gaming") == "20"
    assert cli._category_id("People") == "22"
    assert cli._category_id("23") == "23"
    assert cli._category_id("all") == ""
    with pytest.raises(SystemExit):
        cli._category_id("knitting")


def test_research_needs_a_key(capsys):
    assert cli.main(["research"]) == 2
    assert "YouTube Data API key" in capsys.readouterr().out


def test_research_rejects_bad_regions(capsys):
    assert cli.main(["research", "--api-key", "k", "-r", "usa"]) == 2


def test_research_json(fake_scan, capsys):
    assert cli.main(["research", "--api-key", "k", "-r", "gb,us", "-c", "comedy", "--top", "2", "--deep", "0",
                     "--per-region", "500", "--json"]) == 0
    o = fake_scan["opts"]
    assert (o.api_key, o.regions, o.category, o.deep_count, o.per_region) == ("k", ["GB", "US"], "23", 0, 200)
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == ["v0", "v1"]
    assert rows[0]["url"] == "https://www.youtube.com/watch?v=v0" and rows[0]["score"] == 90


def test_research_table(fake_scan, capsys):
    assert cli.main(["research", "--api-key", "k"]) == 0
    out = capsys.readouterr().out
    assert "Video 0" in out and "reason 0" in out and "Clip-ability" in out


def test_research_reports_errors(monkeypatch, capsys):
    from ytclip.trending import ResearchError

    def scan(*a, **kw):
        raise ResearchError("Today's YouTube API quota is used up.")

    monkeypatch.setattr(research, "scan", scan)
    assert cli.main(["research", "--api-key", "k"]) == 1
    assert "quota" in capsys.readouterr().out


def test_list_categories(capsys):
    assert cli.main(["research", "--list-categories"]) == 0
    assert "Gaming" in capsys.readouterr().out


def test_clip_flag_sends_top_videos_to_the_clipper(fake_scan, monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "process", lambda url, engine, dry_run: sent.append(url))
    assert cli.main(["research", "--api-key", "k", "--clip", "2"]) == 0
    assert sent == ["https://www.youtube.com/watch?v=v0", "https://www.youtube.com/watch?v=v1"]


def test_normal_cli_still_works(capsys):
    assert cli.main([]) == 2
    assert "ytclip research --help" in capsys.readouterr().out

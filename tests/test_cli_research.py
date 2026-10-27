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


def _picks_result():
    from ytclip.trending import video_from_api as vfa

    out = []
    for i, (origins, lic) in enumerate([(["chart"], "youtube"), (["watchlist"], "youtube"),
                                        (["cc"], "creativeCommon")]):
        v = vfa(api_item(f"p{i}", title=f"Pick {i}", hours_old=1, minutes=30, channel=f"C{i}", license=lic))
        v.score, v.origins, v.reasons = 90 - i, origins, [f"reason {i}"]
        out.append(v)
    return ScanResult(out, NOW, ["US"], notes={"UCC1": "allows clips"})


def test_picks_flag_shows_decisions_in_json(monkeypatch, capsys):
    monkeypatch.setattr(research, "scan", lambda *a, **kw: _picks_result())
    assert cli.main(["research", "--api-key", "k", "--picks", "--json"]) == 0
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["p0"]["pick"] == {"picked": False, "reason": "Not on your channel list and not Creative Commons.",
                                  "rule": "source"}
    assert rows["p1"]["pick"]["picked"] and rows["p1"]["pick"]["reason"] == "On your channel list: allows clips"
    assert rows["p2"]["pick"]["picked"] and rows["p2"]["license"] == "creativeCommon"
    assert rows["p2"]["origins"] == ["cc"]


def test_picks_table(monkeypatch, capsys):
    monkeypatch.setattr(research, "scan", lambda *a, **kw: _picks_result())
    monkeypatch.setattr(cli.console, "width", 250)  # room for every column
    assert cli.main(["research", "--api-key", "k", "--picks"]) == 0
    out = capsys.readouterr().out
    assert "2 picked" in out and "your list" in out and "✓ On your channel list" in out


def test_clip_with_picks_clips_only_picks_and_records_them(monkeypatch):
    from ytclip.policy import PickLog, picks_path

    monkeypatch.setattr(research, "scan", lambda *a, **kw: _picks_result())
    sent = []
    monkeypatch.setattr(cli, "process", lambda url, engine, dry_run: sent.append(url))
    assert cli.main(["research", "--api-key", "k", "--picks", "--clip", "5"]) == 0
    assert sent == ["https://www.youtube.com/watch?v=p1", "https://www.youtube.com/watch?v=p2"]
    log = PickLog(picks_path())
    assert {p["id"]: p["how"] for p in log.picks} == {"p1": "cli", "p2": "cli"}


def test_a_failed_clip_is_not_recorded_as_picked(monkeypatch):
    from ytclip.policy import PickLog, picks_path

    monkeypatch.setattr(research, "scan", lambda *a, **kw: _picks_result())

    def process(url, engine, dry_run):
        if url.endswith("p1"):
            raise RuntimeError("download failed")

    monkeypatch.setattr(cli, "process", process)
    assert cli.main(["research", "--api-key", "k", "--picks", "--clip", "5"]) == 0
    assert [p["id"] for p in PickLog(picks_path()).picks] == ["p2"]


def test_watch_rounds_keep_clipping_new_arrivals(monkeypatch):
    rounds = []

    def scan(*a, **kw):
        r = _result()
        if rounds:  # later rounds: v0 was sent; v2 is a new arrival above the alert score
            for v in r.videos:
                v.new = v.id == "v2"
                v.sent = v.id == "v0"
            r.videos[2].score = 95
        rounds.append(r)
        if len(rounds) == 3:
            raise KeyboardInterrupt
        return r

    monkeypatch.setattr(research, "scan", scan)
    monkeypatch.setattr("time.sleep", lambda s: None)
    sent = []
    monkeypatch.setattr(cli, "process", lambda url, engine, dry_run: sent.append(url.rsplit("=", 1)[-1]))
    assert cli.main(["research", "--api-key", "k", "--clip", "1", "--watch", "1"]) == 0
    assert sent == ["v0", "v2"]

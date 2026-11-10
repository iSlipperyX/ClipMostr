"""`ytclip studio` at its seams: the request it builds, the checks it prints, queueing, rendering a timeline
again. The orchestrator is replaced (tests/test_studio_orchestrator.py runs the real one)."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from ytclip import cli
from ytclip.creator import orchestrator as creator
from ytclip.creator.director import Scene, Storyboard
from ytclip.settings import Settings
from ytclip.studio import orchestrator as O
from ytclip.studio import timeline_schema as T


class FakeStudio:
    made: list = []

    def __init__(self, s, log=None, progress=None, **kw):
        self.s, self.failed_at, self.visual_credits = s, "", ["Scene 2: Video by A on Pexels (https://www.pexels.com/video/1/)"]
        self._log, self._progress = log, progress

    def accept(self, req):
        if not (req.prompt or req.url or req.script or req.storyboard):
            raise creator.CreatorError("Say what the video is about")
        return req

    def storyboard(self, req):
        return Storyboard("A board", scenes=[Scene(1, "Hello there.")])

    def create(self, req):
        FakeStudio.made.append(req)
        if "fail" in req.prompt:
            self.failed_at = "script"
            raise creator.CreatorError("example.org asks programs not to read that page")
        self._progress("Writing the script: reading the page", None)
        folder = Path(self.s.output_dir) / "Created" / "x"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "timeline.json").write_text("{}", encoding="utf-8")
        return NS(storyboard=Storyboard("Lighthouses"), duration=64.0, video=folder / "01 - Lighthouses.mp4",
                  folder=folder, warnings=["Its loudness is -15.2 LUFS (aimed for -14)."],
                  checks={"width": 1920, "height": 1080, "lufs": -14.1, "size_mb": 12.5})


@pytest.fixture
def fake(monkeypatch, tmp_path):
    FakeStudio.made = []
    s = Settings().apply({"output_dir": str(tmp_path / "Out"), "categories": [{"id": "h", "name": "History"}]})
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: s))
    monkeypatch.setattr(O, "StudioOrchestrator", FakeStudio)
    return s


def test_the_flags_become_the_request(fake, capsys):
    code = cli.main(["studio", "The angle", "--url", "https://example.org/a", "--format", "long", "--seconds", "480",
                     "--captions", "karaoke", "--tts", "kokoro", "--voice", "deep_male", "--visuals", "stock",
                     "--ai", "local_first", "--style", "documentary", "--category", "History", "--title", "T",
                     "--no-post", "--no-creator-rewards", "--fps", "60"])
    out = capsys.readouterr().out
    assert code == 0
    req = FakeStudio.made[0]
    assert (req.prompt, req.url, req.format, req.seconds, req.captions, req.tts, req.voice, req.visuals, req.ai,
            req.style, req.category, req.title, req.post, req.rewards, req.fps, req.orientation) == \
        ("The angle", "https://example.org/a", "long", 480.0, "karaoke", "kokoro", "deep_male", "stock",
         "local_first", "documentary", "h", "T", False, False, 60, "")
    assert "✓ Lighthouses" in out and "1920x1080" in out and "64 s" in out
    assert "ytclip studio --timeline" in out and "Video by A on Pexels" in out and "⚠ Its loudness" in out
    assert out.count("Writing the script") == 1  # a step is announced once


def test_defaults_leave_everything_to_settings(fake):
    assert cli.main(["studio", "--url", "https://example.org/a"]) == 0
    req = FakeStudio.made[0]
    assert (req.prompt, req.format, req.orientation, req.seconds, req.captions, req.tts, req.visuals, req.ai,
            req.rewards, req.post, req.fps) == ("", "", "", 0, "", "", "", "", None, True, 30)


def test_nothing_to_make_prints_the_help(fake, capsys):
    assert cli.main(["studio"]) == 2
    out = capsys.readouterr().out
    assert "--url" in out and "ytclip studio --timeline" in out and FakeStudio.made == []


def test_a_failure_says_why_and_in_which_step(fake, capsys):
    assert cli.main(["studio", "fail this", "--url", "https://example.org/a"]) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "✗ example.org asks programs not to read that page" in out and "(writing the script)" in out


def test_the_script_and_storyboard_can_be_your_own_and_only_the_storyboard_printed(fake, tmp_path, capsys):
    mine = tmp_path / "script.txt"
    mine.write_text("Line one.\n\nLine two.", encoding="utf-8")
    board = tmp_path / "storyboard.json"
    board.write_text(json.dumps({"title": "B", "scenes": [{"speech_text": "Hi."}]}), encoding="utf-8")
    assert cli.main(["studio", "--script", str(mine), "--storyboard", str(board)]) == 0
    req = FakeStudio.made[0]
    assert req.script.startswith("Line one.") and req.storyboard["title"] == "B"
    capsys.readouterr()
    assert cli.main(["studio", "A prompt", "--storyboard-only"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["title"] == "A board" and len(FakeStudio.made) == 1  # nothing was rendered


def test_schedule_queues_a_request_automation_reads_back_as_the_studios(fake, tmp_path, monkeypatch, capsys):
    from ytclip import jobs

    db_path = tmp_path / "jobs.db"
    real = jobs.JobDB
    monkeypatch.setattr(jobs, "JobDB", lambda *a, **k: real(db_path))
    assert cli.main(["studio", "--url", "https://example.org/a", "--format", "long", "--schedule"]) == 0
    assert "Queued" in capsys.readouterr().out and FakeStudio.made == []
    db = real(db_path)
    try:
        job = next(j for j in db.jobs() if j.is_creation)
    finally:
        db.close()
    assert job.title == "https://example.org/a"
    req = O.read_request(job.request)
    assert isinstance(req, O.StudioRequest) and req.url == "https://example.org/a" and req.format == "long"
    plain = O.read_request(creator.CreateRequest(prompt="x").to_json())
    assert type(plain) is creator.CreateRequest  # the Video Creator's own requests stay its own


def test_timeline_renders_a_folder_again(fake, tmp_path, monkeypatch, capsys):
    seen = []

    def render_timeline(path, out=None, **kw):
        seen.append((Path(path), out))
        return NS(duration=20.0, encoder="libx264", path=Path(path) / "T (edit).mp4", warnings=[])

    monkeypatch.setattr(O, "render_timeline", render_timeline)
    assert cli.main(["studio", "--timeline", str(tmp_path), "--out", str(tmp_path / "o.mp4")]) == 0
    assert seen == [(tmp_path, tmp_path / "o.mp4")] and "Rendered again" in capsys.readouterr().out

    def broken(path, out=None, **kw):
        raise creator.CreatorError("timeline.json can't be used: tracks[0].clips[2].asset is 'nope'")

    monkeypatch.setattr(O, "render_timeline", broken)
    assert cli.main(["studio", "--timeline", str(tmp_path)]) == 1
    assert "can't be used" in capsys.readouterr().out


def test_schema_prints_the_timelines_json_schema(fake, capsys):
    assert cli.main(["studio", "--schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema == T.JSON_SCHEMA and schema["properties"]["tracks"]


def test_check_lists_what_is_ready_without_touching_the_network(fake, capsys, monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    assert cli.main(["studio", "--check"]) == 0
    out = capsys.readouterr().out
    for word in ("Script writer:", "Voice engines:", "Pictures:", "Stock footage:", "Article reader:", "built-in",
                 "ElevenLabs"):
        assert word in out, word
    assert "no stock footage key" in " ".join(out.split())

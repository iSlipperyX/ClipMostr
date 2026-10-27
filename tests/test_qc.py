import json
import subprocess
from pathlib import Path

import pytest

from ytclip import engine as engine_mod
from ytclip import qc
from ytclip.analyze import Clip
from ytclip.download import Video
from ytclip.engine import Engine, Project
from ytclip.ffmpeg import MediaInfo, ffmpeg_exe
from ytclip.qc import FileStats, judge_content, judge_file
from ytclip.settings import Settings
from ytclip.subtitles import Word

FFMPEG_LOG = """
[blackdetect @ 0x1] black_start:0 black_end:1.5 black_duration:1.5
[blackdetect @ 0x1] black_start:8 black_end:9 black_duration:1
[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 4
[freezedetect @ 0x2] lavfi.freezedetect.freeze_duration: 2
[freezedetect @ 0x2] lavfi.freezedetect.freeze_end: 6
[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 18
[Parsed_volumedetect_0 @ 0x3] mean_volume: -22.5 dB
[Parsed_volumedetect_0 @ 0x3] max_volume: -1.0 dB
"""


def test_parse_ffmpeg_output():
    s = qc.parse_stats(FFMPEG_LOG, FileStats(duration=20))
    assert (s.mean_db, s.max_db) == (-22.5, -1.0)
    assert s.black == 2.5
    assert s.frozen == 4.0  # 2 s, plus a freeze that runs to the end


def good(**kw):
    return FileStats(**({"duration": 30, "width": 1080, "height": 1920, "has_audio": True, "mean_db": -16,
                         "max_db": -1} | kw))


@pytest.mark.parametrize("stats,problem", [
    (good(duration=0), "can't be read"),
    (good(duration=24), "Came out 24s long instead of 30s"),
    (good(width=720, height=1280), "720×1280 instead of 1080×1920"),
    (good(has_audio=False), "No sound"),
    (good(max_db=-60, mean_db=-80), "Silent"),
    (good(black=20), "black for 67%"),
    (good(frozen=20), "frozen for 67%"),
])
def test_file_problems(stats, problem):
    v = judge_file(stats, 30, (1080, 1920), True)
    assert not v.ok and any(problem in p for p in v.problems)


def test_file_warnings_and_passes():
    assert judge_file(good(), 30, (1080, 1920), True).to_json() == {"ok": True}
    v = judge_file(good(mean_db=-35, black=4, frozen=8), 30.5, None, True)
    assert v.ok and len(v.warnings) == 3
    assert judge_file(good(has_audio=False), 30, None, want_audio=False).ok  # the source had no sound either
    black = judge_file(good(black=29, frozen=30), 30, None, True)
    assert black.problems == ["The picture is black for 97% of it"]


def _words(*items):
    return [Word(a, b, t) for a, b, t in items]


def test_content_checks():
    w = _words((9.0, 9.5, "so"), (9.6, 10.2, "the"), (10.3, 11.0, "thing"), (12, 13, "is."), (28.5, 29.5, "and"),
               (30.2, 31, "then."))
    v = judge_content(Clip(10, 30, 80), w, 5)
    assert v.ok and set(v.warnings) == {"Hardly any talking", "Starts mid-sentence", "Ends mid-sentence"}
    unpunctuated = [Word(x.start, x.end, x.text.rstrip(".")) for x in w]
    assert judge_content(Clip(10, 30, 80), unpunctuated, 5).warnings == ["Hardly any talking"]


def test_low_ai_ratings_are_rejected():
    low = Clip(0, 30, 80, rating=3, rating_note="needs context from earlier")
    v = judge_content(low, [], 5)
    assert not v.ok and v.problems == ["AI rated it 3/10: needs context from earlier"]
    assert judge_content(low, [], 0).ok  # 0 ignores ratings
    assert judge_content(Clip(0, 30, 80), [], 5).ok  # not rated


# ---------------------------------------------------------------- with real files


def _make(path: Path, video: str, seconds: float = 4, audio: str = "sine") -> Path:
    src = {"sine": f"sine=d={seconds}", "silence": "anullsrc=r=44100:cl=stereo"}[audio]
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", src,
                    "-t", str(seconds), "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_measure_real_files(tmp_path):
    ok = qc.measure(_make(tmp_path / "ok.mp4", "testsrc2=s=180x320:r=15"))
    assert ok.duration == pytest.approx(4, abs=0.1) and (ok.width, ok.height) == (180, 320)
    assert ok.max_db > -30 and ok.black == 0 and ok.frozen == 0
    bad = qc.measure(_make(tmp_path / "bad.mp4", "color=black:s=180x320:r=15", audio="silence"))
    assert bad.max_db < -80 and bad.black > 3.5
    v = judge_file(bad, 4, (180, 320), True)
    assert "Silent" in v.problems and any("black" in p for p in v.problems)


def test_engine_rejects_bad_clips_into_their_own_folder(tmp_path, monkeypatch):
    def fake_export(src, media, dest, clip, words, s, prog=None):
        _make(dest, "color=black:s=1080x1920:r=15" if clip.title == "dud" else "testsrc2=s=1080x1920:r=15",
              seconds=clip.length, audio="silence" if clip.title == "dud" else "sine")
        if s.write_srt:
            dest.with_suffix(".srt").write_text("1\n", encoding="utf-8")

    monkeypatch.setattr(engine_mod, "export_clip", fake_export)
    video = Video(id="vid12345678", title="Talk", channel="Pod", upload_date="", duration=60,
                  url="https://youtu.be/vid12345678", path=tmp_path / "src.mp4")
    p = Project(video.url, video=video, media=MediaInfo(60, 1920, 1080))
    p.clips = [Clip(0, 3, 80, "fine"), Clip(10, 13, 70, "dud"), Clip(20, 23, 60, "rated low", rating=2)]
    s = Settings(output_dir=str(tmp_path / "out"), thumbnails=False, qc_enabled=True, fades=False)
    eng = Engine(s)
    out = eng.export(p)
    data = json.loads((out / "clips.json").read_text(encoding="utf-8"))
    files = [r["file"] for r in data["clips"]]
    assert not files[0].startswith("_rejected/")
    assert files[1].startswith("_rejected/") and files[2].startswith("_rejected/")
    assert (out / files[1]).exists() and (out / files[1]).with_suffix(".srt").exists()
    assert not (out / Path(files[1]).name).exists()
    assert data["clips"][1]["qc"]["problems"][0] == "Silent"
    assert data["clips"][2]["qc"]["problems"] == ["AI rated it 2/10"]
    assert data["qc"] == {"passed": 1, "rejected": 2, "warned": 0} == p.qc
    # exporting again clears the old set, rejected files included
    p.clips = p.clips[:1]
    eng.export(p)
    assert not list((out / "_rejected").glob("*.mp4"))


def test_check_folder_again(tmp_path):
    folder = tmp_path / "video"
    folder.mkdir()
    _make(folder / "01.mp4", "testsrc2=s=180x320:r=15", seconds=3)
    _make(folder / "02.mp4", "testsrc2=s=180x320:r=15", seconds=3)
    (folder / "clips.json").write_text(json.dumps({"clips": [
        {"rank": 1, "file": "01.mp4", "start": 0, "end": 3, "length": 3},
        {"rank": 2, "file": "02.mp4", "start": 5, "end": 8, "length": 3, "rating": 2},
        {"rank": 3, "file": "03.mp4", "start": 9, "end": 12, "length": 3},
    ]}), encoding="utf-8")
    logs = []
    result = qc.check_folder(folder, logs.append, min_rating=5, size=(180, 320))
    assert result == {"passed": 1, "rejected": 2, "warned": 0}
    data = json.loads((folder / "clips.json").read_text(encoding="utf-8"))
    assert [r["file"] for r in data["clips"]] == ["01.mp4", "_rejected/02.mp4", "03.mp4"]
    assert data["clips"][2]["qc"]["problems"] == ["The file is missing"]
    assert logs[0] == "✓ 01.mp4"
    assert qc.check_folder(folder, min_rating=5)["rejected"] == 2  # running it again changes nothing


def test_cli_qc(tmp_path, monkeypatch, capsys):
    from ytclip import cli

    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: Settings()))
    folder = tmp_path / "Pod" / "video"
    folder.mkdir(parents=True)
    _make(folder / "01.mp4", "testsrc2=s=180x320:r=15", seconds=3)
    (folder / "clips.json").write_text(json.dumps({"clips": [{"rank": 1, "file": "01.mp4", "length": 3}]}))
    assert cli.main(["qc", str(tmp_path), "--size", "180x320"]) == 0
    assert "1 ready" in capsys.readouterr().out
    assert cli.main(["qc", str(tmp_path / "nothing-here")]) == 2


def test_polish_ratings_reach_the_clip():
    from ytclip import polish

    answer = json.dumps({"burned_in_subtitles": False, "clips": [
        {"clip": 1, "hook": "h", "rating": 3, "rating_note": "Needs context", "subject_x": 0.5, "fixes": []},
        {"clip": 2, "hook": "h", "rating": 9, "rating_note": "ignored when high", "subject_x": 0.5, "fixes": []},
        {"clip": 3, "hook": "h", "rating": 40, "rating_note": "", "subject_x": 0.5, "fixes": []}]})
    result = polish.parse(answer, 3)
    clips = [Clip(0, 10, 80), Clip(20, 30, 80), Clip(40, 50, 80)]
    polish.apply(result, clips, [])
    assert [(c.rating, c.rating_note) for c in clips] == [(3, "Needs context"), (9, ""), (10, "")]

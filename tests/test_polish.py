import json
import random
from pathlib import Path

import numpy as np
import pytest

from ytclip import engine as engine_mod
from ytclip import ai, polish, qc
from ytclip.analyze import Clip, Measurements, trim_dead_air
from ytclip.download import Video
from ytclip.engine import Engine, Project
from ytclip.ffmpeg import MediaInfo
from ytclip.polish import ClipPolish, Polish, fix_words, parse, reapply_fixes
from ytclip.settings import Settings
from ytclip.stackpool import StackPool, pool_files, preview_video
from ytclip.subtitles import Word, censor


def words(text, start=0.0, step=0.5):
    return [Word(start + i * step, start + i * step + 0.4, w) for i, w in enumerate(text.split())]


# ---------------------------------------------------------------- stack-video folder


def _pool_folder(tmp_path, names=("a.mp4", "b.mp4", "c.mkv")):
    folder = tmp_path / "gameplay"
    folder.mkdir()
    for n in names:
        (folder / n).write_bytes(b"x")
    (folder / "notes.txt").write_text("not a video")
    return folder


def test_pool_lists_only_videos(tmp_path):
    folder = _pool_folder(tmp_path)
    assert [f.name for f in pool_files(folder)] == ["a.mp4", "b.mp4", "c.mkv"]
    assert pool_files(tmp_path / "missing") == []


def test_pool_deals_every_video_before_repeating_and_never_twice_in_a_row(tmp_path):
    folder = _pool_folder(tmp_path)
    pool = StackPool(tmp_path / "pool.json", random.Random(1))
    dealt = [pool.next(folder).name for _ in range(30)]
    for i in range(0, 30, 3):
        assert sorted(dealt[i:i + 3]) == ["a.mp4", "b.mp4", "c.mkv"]
    assert all(a != b for a, b in zip(dealt, dealt[1:]))


def test_pool_carries_on_after_a_restart_and_copes_with_changes(tmp_path):
    folder = _pool_folder(tmp_path)
    first = StackPool(tmp_path / "pool.json", random.Random(2))
    got = [first.next(folder).name]
    again = StackPool(tmp_path / "pool.json", random.Random(3))  # a new run picks up the same deck
    got += [again.next(folder).name, again.next(folder).name]
    assert sorted(got) == ["a.mp4", "b.mp4", "c.mkv"]
    for f in folder.iterdir():
        f.unlink()
    (folder / "new.mp4").write_bytes(b"x")
    assert again.next(folder).name == "new.mp4"
    assert StackPool(tmp_path / "pool.json").next(tmp_path / "empty-or-missing") is None


def test_previews_show_the_folder_when_it_has_videos(tmp_path):
    folder = _pool_folder(tmp_path)
    single = tmp_path / "one.mp4"
    single.write_bytes(b"x")
    assert preview_video(Settings(stack_video=str(single), stack_folder=str(folder))) == str(folder / "a.mp4")
    assert preview_video(Settings(stack_video=str(single), stack_folder=str(tmp_path / "nope"))) == str(single)


def _export_project(tmp_path, words_=None):
    video = Video(id="vid12345678", title="Big Talk", channel="Pod", upload_date="", duration=300,
                  url="https://youtu.be/vid12345678", path=tmp_path / "src.mp4")
    p = Project(video.url, video=video, media=MediaInfo(300, 1920, 1080))
    p.clips = [Clip(10, 40, 80, "one"), Clip(60, 90, 70, "two"), Clip(120, 150, 60, "three")]
    p.words = words_ or []
    return p


@pytest.fixture
def fake_export(monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "export_clip", lambda src, media, dest, clip, w, s, prog=None, **kw:
                        calls.append((dest.name, s)) or dest.write_bytes(b"clip"))
    return calls


def test_export_gives_each_clip_a_different_second_video(tmp_path, fake_export):
    folder = _pool_folder(tmp_path)
    s = Settings(output_dir=str(tmp_path / "out"), stack_enabled=True, stack_folder=str(folder),
                 thumbnails=False, tiktok_ready=False, write_srt=False)
    eng = Engine(s)
    eng.stack_pool = StackPool(tmp_path / "pool.json", random.Random(5))
    out = eng.export(_export_project(tmp_path))
    used = [Path(call[1].stack_video).name for call in fake_export]
    assert sorted(used) == ["a.mp4", "b.mp4", "c.mkv"]
    records = json.loads((out / "clips.json").read_text(encoding="utf-8"))["clips"]
    assert [r["second_video"] for r in records] == used


def test_export_without_a_folder_keeps_the_single_video(tmp_path, fake_export):
    s = Settings(output_dir=str(tmp_path / "out"), stack_enabled=True, stack_video="one.mp4",
                 thumbnails=False, tiktok_ready=False, write_srt=False)
    out = Engine(s).export(_export_project(tmp_path))
    assert {call[1].stack_video for call in fake_export} == {"one.mp4"}
    assert "second_video" not in json.loads((out / "clips.json").read_text(encoding="utf-8"))["clips"][0]


# ---------------------------------------------------------------- dead air


def test_trim_pulls_edges_in_to_the_speech():
    c = Clip(10.0, 40.0, 80)
    w = [Word(12.0, 12.4, "So"), Word(12.5, 13.0, "anyway"), Word(35.0, 35.5, "done.")]
    assert trim_dead_air(c, w)
    assert (c.start, c.end) == (11.75, 36.0)


def test_trim_leaves_small_gaps_and_short_results_alone():
    c = Clip(10.0, 40.0, 80)
    assert not trim_dead_air(c, [Word(10.5, 11.0, "Hi"), Word(39.0, 39.6, "bye")])
    c = Clip(10.0, 40.0, 80)
    assert not trim_dead_air(c, [Word(20.0, 21.0, "Hi")], min_len=20)
    assert not trim_dead_air(Clip(0, 30, 80), [])


def test_engine_trims_but_not_hand_edited_clips():
    s = Settings(min_len=5)
    p = Project("x", video=Video("v", "t", "c", "", 100, "u", Path("x")))
    p.words = [Word(15.0, 15.5, "Hello"), Word(20.0, 20.5, "there.")]
    p.clips = [Clip(10, 30, 50), Clip(10, 30, 50, edited=True)]
    Engine(s).trim(p)
    assert (p.clips[0].start, p.clips[0].end) == (14.75, 21.0)
    assert (p.clips[1].start, p.clips[1].end) == (10, 30)
    Engine(s.copy(trim_silence=False)).trim(p2 := Project("x", clips=[Clip(10, 30, 50)], words=p.words))
    assert p2.clips[0].start == 10


# ---------------------------------------------------------------- swearing


@pytest.mark.parametrize("text,out", [
    ("fucking", "f*cking"), ("SHIT!", "SH*T!"), ("motherfucker", "motherf*cker"), ("bitches", "b*tches"),
    ("classic", "classic"), ("Scunthorpe", "Scunthorpe"), ("shitake", "shitake"), ("hello there", "hello there"),
])
def test_censor(text, out):
    assert censor(text) == out


def test_captions_and_srt_are_bleeped_when_asked(tmp_path, fake_export):
    from ytclip.captions import caption_events

    w = words("what the fuck is this", start=11)
    ev = caption_events(w, 10, 40, Settings(caption_censor=True, caption_words=5, caption_uppercase=True))
    assert ev[0].words == ["WHAT", "THE", "F*CK", "IS", "THIS"]
    assert caption_events(w, 10, 40, Settings(caption_words=5))[0].words[2] == "FUCK"
    s = Settings(output_dir=str(tmp_path / "out"), thumbnails=False, tiktok_ready=False, caption_censor=True)
    out = Engine(s).export(_export_project(tmp_path, w))
    assert "f*ck" in next(out.glob("01 *.srt")).read_text(encoding="utf-8")


# ---------------------------------------------------------------- AI polish


def test_parse_cleans_up_the_answer():
    answer = json.dumps({"burned_in_subtitles": True, "clips": [
        {"clip": 1, "hook": '  "He quit   on live TV"  ', "subject_x": 1.7,
         "fixes": [{"heard": "chat gpt", "correct": "ChatGPT"}, {"heard": "Joe Rogen", "correct": "Joe Rogan"},
                   {"heard": "same", "correct": "same"}]},
        {"clip": 1, "hook": "duplicate", "subject_x": 0.2, "fixes": []},
        {"clip": 9, "hook": "out of range", "subject_x": 0.2, "fixes": []},
        {"clip": 2, "hook": "x" * 100, "subject_x": "left", "fixes": []},
    ]})
    p = parse(answer, 2)
    assert p.burned_in_subtitles
    assert p.clips[0].hook == "He quit on live TV" and p.clips[0].subject_x == 1.0
    assert p.clips[0].fixes == [("Joe Rogen", "Joe Rogan")]  # uneven and no-op swaps are dropped
    assert len(p.clips[1].hook) <= polish.MAX_HOOK and p.clips[1].subject_x is None
    with pytest.raises(polish.PolishError):
        parse("not json", 1)


def test_fix_words_keeps_timing_and_punctuation():
    w = words("I asked Joe Rogen, and joe rogen said no.")
    before = [(x.start, x.end) for x in w]
    assert fix_words(w, 0, 100, "Joe Rogen", "Joe Rogan") == 2
    assert [x.text for x in w] == ["I", "asked", "Joe", "Rogan,", "and", "Joe", "Rogan", "said", "no."]
    assert [(x.start, x.end) for x in w] == before
    assert fix_words(w, 0, 1.0, "said", "told") == 0  # outside the clip


def test_apply_respects_edits_and_fills_the_banner():
    clips = [Clip(0, 10, 80), Clip(20, 30, 80, edited=True, crop_x=0.3), Clip(40, 50, 80, top_text="Mine")]
    w = words("joe rogen talks", start=1) + words("joe rogen again", start=21)
    result = Polish({0: ClipPolish("He said what", 0.8, [("joe rogen", "Joe Rogan")]),
                     1: ClipPolish("Edited clip", 0.9, [("joe rogen", "Joe Rogan")]),
                     2: ClipPolish("Third", None, [])}, True, "claude-sonnet-5-5")
    summary = polish.apply(result, clips, w, "{title}", banner_on=True)
    assert [c.hook for c in clips] == ["He said what", "Edited clip", "Third"]
    assert clips[0].top_text == "He said what" and clips[0].crop_x == 0.8
    assert clips[1].top_text is None and clips[1].crop_x == 0.3  # hand-edited: only the hook is noted
    assert clips[2].top_text == "Mine"
    assert [x.text for x in w[:2]] == ["Joe", "Rogan"] and w[3].text == "joe"
    assert summary["caption_fixes"] == 1 and summary["hooks"] == 3 and summary["crops_moved"] == 1
    again = words("joe rogen talks", start=1)
    assert reapply_fixes(summary, again) == 1 and again[1].text == "Rogan"


def test_banner_left_alone_when_it_is_off_or_has_no_title():
    clips = [Clip(0, 10, 80)]
    polish.apply(Polish({0: ClipPolish("Hook", None, [])}), clips, [], "Follow for more", banner_on=True)
    assert clips[0].top_text is None and clips[0].hook == "Hook"


def test_parse_reads_the_bottom_line():
    answer = json.dumps({"burned_in_subtitles": False, "clips": [
        {"clip": 1, "hook": "The #1 mistake #fyp", "bottom_text": ' "Would you   have stayed?" #fyp ', "subject_x": 0.5,
         "fixes": [], "rating": 8, "rating_note": ""}]})
    assert parse(answer, 1).clips[0].bottom_text == "Would you have stayed?"
    assert parse(answer, 1).clips[0].hook == "The #1 mistake"  # a number isn't a hashtag
    line = "Would you have walked out of that meeting halfway? Follow @bigtalkclips_daily"
    long = json.dumps({"burned_in_subtitles": False, "clips": [
        {"clip": 1, "hook": "h", "bottom_text": line, "subject_x": 0.5, "fixes": [], "rating": 8, "rating_note": ""}]})
    assert parse(long, 1).clips[0].bottom_text == ""  # left out rather than cut through its @handle


def test_the_ai_writes_each_clips_bottom_line_but_never_a_part_2():
    clips = [Clip(0, 10, 80), Clip(11, 20, 80), Clip(40, 50, 80), Clip(60, 70, 80, edited=True),
             Clip(80, 90, 80, bottom_text="Mine")]
    result = Polish({0: ClipPolish("a", bottom_text="Part 2 on my page"),  # promises one: never kept
                     1: ClipPolish("b", bottom_text="Would you stay?"),
                     2: ClipPolish("c", bottom_text="Would you do it?"),
                     3: ClipPolish("d", bottom_text="Edited clip"), 4: ClipPolish("e", bottom_text="Not mine")})
    summary = polish.apply(result, clips, [], "{title}", banner_on=True, bottom_on=True)
    assert [c.bottom_ai for c in clips] == ["", "Would you stay?", "Would you do it?", "", "Not mine"]
    assert [c.bottom_text for c in clips] == [None, None, None, None, "Mine"]  # your own text is never touched
    assert summary["bottom_lines"] == 3
    off = [Clip(0, 10, 80)]
    summary = polish.apply(Polish({0: ClipPolish("a", bottom_text="Would you?")}), off, [], "{title}", bottom_on=False)
    assert off[0].bottom_ai == "Would you?" and summary["bottom_lines"] == 0  # kept, but your settings don't show it


def test_the_ai_is_told_the_usual_line_and_the_handles_to_keep(tmp_path):
    blocks = polish.build_content("Title", [Clip(0, 10, 80), Clip(11, 20, 80)], [], [None] * 2,
                                  bottom="Follow for more", handles=["@bigtalk"])
    assert "usual bottom line: Follow for more" in blocks[0]["text"]
    assert "must include @bigtalk" in blocks[0]["text"]
    assert "carries on" not in blocks[1]["text"]
    assert "usual bottom line" not in polish.build_content("Title", [Clip(0, 10, 80)], [], [None])[0]["text"]


def test_content_has_a_frame_per_clip_and_the_transcript(tmp_path):
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"\xff\xd8jpeg")
    blocks = polish.build_content("Title", [Clip(0, 10, 80), Clip(20, 30, 80)], words("hello world"), [frame, None])
    assert [b["type"] for b in blocks] == ["text", "text", "image", "text"]
    assert "Transcript: hello world" in blocks[1]["text"] and "(nobody speaks)" in blocks[3]["text"]


def _polish_project():
    p = Project("u", video=Video("v", "Big Talk", "Pod", "", 100, "u", Path("src.mp4")),
                measurements=Measurements(100, np.zeros(200)))
    p.clips = [Clip(0, 20, 80), Clip(30, 50, 70, enabled=False)]
    p.words = words("hello joe rogen")
    return p


def test_engine_polish_applies_the_answer(monkeypatch):
    seen = {}

    def ask(title, clips, w, src, settings, log=None, check=None, guide="", bottom="", handles=None):
        model = ai.model_for(settings, "polish")
        seen.update(title=title, n=len(clips), model=model, guide=guide)
        return Polish({0: ClipPolish("Hook!", 0.7, [("joe rogen", "Joe Rogan")])}, True, model)

    monkeypatch.setattr(polish, "ask", ask)
    s = Settings(ai_api_key="k", burn_captions=True, banner_top=True, banner_top_text="{title}")
    p = _polish_project()
    Engine(s).polish(p)
    assert seen == {"title": "Big Talk", "n": 1, "model": "claude-sonnet-5-5", "guide": ""}  # only enabled clips go
    assert p.polished and p.captions_off and p.clips[0].top_text == "Hook!"
    assert p.meta["polish"]["captions_off"] and p.words[2].text == "Rogan"
    p2 = _polish_project()
    Engine(s.copy(polish_burned_subs="keep")).polish(p2)
    assert not p2.captions_off


def test_engine_polish_writes_the_bottom_line_when_that_banner_is_on(monkeypatch):
    seen = {}

    def ask(title, clips, w, src, settings, log=None, check=None, guide="", bottom="", handles=None):
        model = ai.model_for(settings, "polish")
        seen.update(bottom=bottom, handles=handles)
        return Polish({0: ClipPolish("Hook!", bottom_text="Would you stay? @me")}, False, model)

    monkeypatch.setattr(polish, "ask", ask)
    logs = []
    s = Settings(ai_api_key="k", banner_bottom=True, banner_bottom_text="Follow @me for part 2",
                 banner_bottom_alt="Follow for more {title}")
    p = _polish_project()
    Engine(s, log=logs.append).polish(p)
    assert seen == {"bottom": "Follow for more", "handles": ["@me"]}
    assert p.clips[0].bottom_ai == "Would you stay? @me" and p.clips[0].bottom_text is None
    assert any("1 bottom lines" in m for m in logs)
    for other in (s.copy(polish_bottom_text=False), s.copy(banner_bottom_text="Follow @me"),
                  s.copy(banner_bottom=False)):
        logs.clear()
        Engine(other, log=logs.append).polish(_polish_project())
        assert seen == {"bottom": "", "handles": []} and not any("bottom lines" in m for m in logs)


def test_export_tells_each_clip_whether_a_part_2_follows(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "export_clip", lambda src, media, dest, clip, w, s, prog=None, next_part=True:
                        calls.append(next_part) or dest.write_bytes(b"clip"))
    s = Settings(output_dir=str(tmp_path / "out"), thumbnails=False, tiktok_ready=False, write_srt=False)
    p = _export_project(tmp_path)
    p.clips = [Clip(0, 10, 80), Clip(11, 20, 70), Clip(50, 60, 60)]
    Engine(s).export(p)
    assert calls == [True, False, False]


def test_a_clip_never_promises_a_part_2_the_quality_check_rejects(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "export_clip", lambda src, media, dest, clip, w, s, prog=None, next_part=True:
                        calls.append((clip.title, next_part)) or dest.write_bytes(b"clip"))
    monkeypatch.setattr(qc, "measure", lambda path: qc.FileStats(0 if "two" in path.name else 1))
    monkeypatch.setattr(qc, "judge_file", lambda stats, *a: qc.Verdict() if stats.duration else
                        qc.Verdict(False, ["The file can't be read"]))
    s = Settings(output_dir=str(tmp_path / "out"), thumbnails=False, tiktok_ready=False, write_srt=False,
                 qc_enabled=True, qc_min_rating=5, banner_bottom=True, banner_bottom_text="Follow for part 2")

    def project(rating=0):
        p = _export_project(tmp_path)
        p.clips = [Clip(10, 40, 80, "one"), Clip(41, 70, 70, "too", rating=rating), Clip(120, 150, 60, "three")]
        return p

    Engine(s).export(project(rating=3))  # the AI rated part 2 too low: the check will reject it
    assert calls == [("one", False), ("too", False), ("three", False)]
    calls.clear()
    p = project()
    p.clips[1].title = "two"  # its file fails the check: part 1 is made again without the promise
    logs = []
    out = Engine(s, log=logs.append).export(p)
    assert calls == [("one", True), ("two", False), ("three", False), ("one", False)]
    assert any("again without promising a part 2" in m for m in logs)
    files = [r["file"] for r in json.loads((out / "clips.json").read_text(encoding="utf-8"))["clips"]]
    assert files[1].startswith("_rejected/") and (out / files[0]).exists()
    calls.clear()
    p = project()
    p.clips[1].title = "two"
    Engine(s.copy(banner_bottom_text="Follow for more")).export(p)  # the same words either way: made once
    assert [c for c in calls if c[0] == "one"] == [("one", True)]


def test_engine_polish_failure_never_stops_the_video(monkeypatch):
    def ask(*a, **k):
        raise polish.PolishError("No Anthropic API key set (Settings → AI).")

    monkeypatch.setattr(polish, "ask", ask)
    logs = []
    p = _polish_project()
    Engine(Settings(), log=logs.append).polish(p)
    assert not p.polished and "No Anthropic API key" in p.polish_error
    assert any("AI polish skipped" in m for m in logs)


def test_captions_off_reaches_the_export_and_the_hook_reaches_clips_json(tmp_path, fake_export):
    s = Settings(output_dir=str(tmp_path / "out"), burn_captions=True, thumbnails=False, tiktok_ready=False,
                 write_srt=False)
    p = _export_project(tmp_path)
    p.captions_off = True
    p.clips[0].hook = "He quit on live TV"
    out = Engine(s).export(p)
    assert not any(call[1].burn_captions for call in fake_export)
    data = json.loads((out / "clips.json").read_text(encoding="utf-8"))
    assert data["clips"][0]["hook"] == "He quit on live TV" and "hook" not in data["clips"][1]
    assert "Hook: **He quit on live TV**" in (out / "README.md").read_text(encoding="utf-8")


def test_ask_needs_a_key(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(polish.PolishError, match="No Claude API key"):
        polish.ask("t", [Clip(0, 10, 80)], [], Path("x.mp4"), Settings(ai_api_key="  "))


def test_cli_flags(tmp_path, monkeypatch):
    from ytclip import cli

    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: Settings()))
    folder = _pool_folder(tmp_path)
    s = cli.settings_from_args(cli.build_parser().parse_args(["x", "--stack", str(folder), "--polish", "--bleep"]))
    assert s.stack_enabled and s.stack_folder == str(folder.resolve()) and s.ai_polish and s.caption_censor
    one = folder / "a.mp4"
    s = cli.settings_from_args(cli.build_parser().parse_args(["x", "--stack", str(one)]))
    assert s.stack_video == str(one.resolve()) and s.stack_folder == ""
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit, match="no videos"):
        cli.settings_from_args(cli.build_parser().parse_args(["x", "--stack", str(tmp_path / "empty")]))

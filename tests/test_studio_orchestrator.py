"""The Studio's run from a prompt or an article's address to a video's folder, offline: a stand-in for the AI,
the page reader and the voice, textures for pictures. One test renders for real; the others replace the render
with a marker file so they stay quick."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from ytclip import ffmpeg
from ytclip.creator import factcheck, soundbed, voice_engine, web
from ytclip.creator.director import Scene, Storyboard
from ytclip.creator.orchestrator import CreateRequest, CreatorError
from ytclip.editor import timeline_compiler
from ytclip.settings import Settings
from ytclip.studio import orchestrator as O
from ytclip.studio import scraper, timeline_builder, voice_studio
from ytclip.studio import timeline_schema as T

URL = "https://example.org/news/lighthouse"
ARTICLE = scraper.Article(
    url=URL, title="The keeper who never left", site="Example News", byline="A. Writer", date="2026-11-01",
    description="A lighthouse story.", extractor="built-in", asked=URL,
    text="\n\n".join(f"Paragraph {i} of the report describes the island station, its lamp room and the winter "
                     f"of storm number {i} in careful detail for the record." for i in range(1, 12)))


def script(n=5, words=10, title="The lighthouse nobody left"):
    beats = ["hook", "setup", "build", "twist", "payoff", "cta"]
    scenes = [{"scene_id": 1, "speech_text": "One man kept this light burning alone.", "visual_type": "texture",
               "visual_prompt": "a lighthouse in a storm", "camera_motion": "slow_push_in", "sfx_cue": "bass_drop",
               "sfx_offset_ms": 0, "emphasis": ["alone"], "keywords": ["lighthouse"], "beat": "hook",
               "mood": "Mysterious", "stock_query": "lighthouse storm"}]
    for i in range(2, n + 1):
        text = " ".join(["Then"] + [f"wave{i}n{k}" for k in range(words - 1)]) + "."
        scenes.append({"scene_id": i, "speech_text": text, "visual_type": "texture", "visual_prompt": f"picture {i}",
                       "camera_motion": "pan_left" if i % 2 else "slow_pull_out", "sfx_cue": "none",
                       "sfx_offset_ms": 0, "emphasis": [], "keywords": ["sea"],
                       "beat": beats[min(i - 1, 5)] if i < n else "cta", "mood": "Mysterious",
                       "stock_query": "ocean waves"})
    return {"title": title, "category": "History", "target_duration": 20, "music_mood": "Mysterious",
            "hook": "He never left the rock", "caption": "Forty winters on one rock", "hashtags": ["history"],
            "cta": "Follow for part two", "scenes": scenes}


class FakeAI:
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, prompt, schema, *, system, what, max_tokens=8000):
        self.asked.append(NS(prompt=prompt, system=system, what=what))
        got = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return NS(data=got, model="test-model", provider="local")


class StubVoice(voice_engine.KokoroEngine):
    def missing(self):
        return ""

    def prepare(self, voice):
        pass

    def speak(self, text, voice, speed):
        rate = 24000
        word = 0.30 * voice.speed / speed
        t = np.arange(int(word * rate)) / rate
        tone = (0.4 * np.sin(2 * np.pi * 200 * t) * np.hanning(len(t))).astype(np.float32)
        gap = np.zeros(int(0.08 * voice.speed / speed * rate), np.float32)
        return np.concatenate([np.concatenate([tone, gap]) for _ in text.split()]), rate


@pytest.fixture
def voice(monkeypatch):
    monkeypatch.setitem(voice_engine.ENGINE_CLASSES, "kokoro", StubVoice)
    monkeypatch.setattr(voice_engine, "pick_engines", lambda s: ["kokoro"])


@pytest.fixture
def quick(monkeypatch):
    """The render and the finished file's checks replaced, for tests about everything around them."""
    seen = NS(timelines=[], sizes=[])

    def render(tl, dest, *, base=None, work=None, settings=None, progress=None, check_cancel=None, log=None):
        T.validate(tl, base=base)  # every file the timeline names is where it says
        seen.timelines.append(tl)
        Path(dest).write_bytes(b"video")
        return timeline_compiler.RenderReport(Path(dest), tl.duration, "libx264", 1.5, -15.0)

    def checks(self, video, duration, rewards, board, warnings, size=(1080, 1920)):
        seen.sizes.append(size)
        return {"ok": True, "problems": [], "warnings": list(warnings), "width": size[0], "height": size[1],
                "length": round(duration, 2), "lufs": -14.0, "true_peak": -1.2, "size_mb": 0.1,
                "originality": 1.0, "creator_rewards": duration >= 61}

    monkeypatch.setattr(timeline_compiler, "render", render)
    monkeypatch.setattr(O.StudioOrchestrator, "checks", checks)
    return seen


def settings(tmp_path, **over):
    return Settings().apply({"output_dir": str(tmp_path / "Out"), "creator_word_timing": "sound",
                             "creator_visuals": "texture", "creator_web": False, "creator_factcheck": "off",
                             "studio_stock": False} | over)


def studio(tmp_path, ai=None, read=None, log=None, states=None, **over):
    return O.StudioOrchestrator(settings(tmp_path, **over), log=log.append if log is not None else None,
                                register=False, ask=ai or FakeAI(script()),
                                read=read or (lambda url, **kw: ARTICLE),
                                on_state=states.append if states is not None else None)


# ---------------------------------------------------------------- INPUT

def test_a_request_needs_something_to_make_a_video_from(tmp_path):
    st = studio(tmp_path)
    with pytest.raises(CreatorError, match="what the video is about"):
        st.accept(O.StudioRequest())
    for bad, says in ((dict(prompt="x", format="medium"), "short or long"),
                      (dict(prompt="x", captions="sparkly"), "caption style"),
                      (dict(prompt="x", visuals="magic"), "picture source"),
                      (dict(prompt="x", ai="psychic"), "way to ask the AI"),
                      (dict(prompt="x", tts="parrot"), "voice engine"),
                      (dict(prompt="x", orientation="diagonal"), "vertical, landscape or square"),
                      (dict(prompt="x", seconds=3), "8 seconds"),
                      (dict(url="file:///etc/passwd"), "http"),
                      (dict(url="http://127.0.0.1/admin"), "")):
        with pytest.raises(CreatorError) as err:
            studio(tmp_path).accept(O.StudioRequest(**bad))
        assert says in str(err.value), bad
    ok = st.accept(O.StudioRequest(url=" https://example.org/a ", captions="karaoke", visuals="stock", tts="auto"))
    assert ok.url == "https://example.org/a" and st.state == "input"


def test_the_shape_follows_the_format_unless_asked_otherwise(tmp_path):
    st = studio(tmp_path)
    assert (st.canvas(O.StudioRequest()).width, st.canvas(O.StudioRequest()).height) == (1080, 1920)
    assert st.canvas(O.StudioRequest(format="long")).aspect == "16:9"
    assert st.canvas(O.StudioRequest(format="long", orientation="vertical")).vertical
    assert st.shape_name(O.StudioRequest(orientation="square")) == "square"
    assert st.canvas(O.StudioRequest(fps=60)).fps == 60


def test_only_an_upright_minute_is_held_to_the_rewards_length(tmp_path):
    st = studio(tmp_path, creator_rewards_length=True, creator_duration=65)
    assert st.target(O.StudioRequest()) == (65.0, True)
    assert st.target(O.StudioRequest(seconds=75)) == (75.0, True)
    assert st.target(O.StudioRequest(seconds=30)) == (30.0, False)  # asked to be short: it is
    assert st.target(O.StudioRequest(seconds=30, rewards=True))[0] >= 63  # unless Rewards was asked for by name
    assert st.target(O.StudioRequest(format="long")) == (O.LONG_SECONDS, False)
    assert st.target(O.StudioRequest(format="long", seconds=480)) == (480.0, False)
    assert st.target(O.StudioRequest(orientation="landscape", seconds=65)) == (65.0, False)
    assert st.target(O.StudioRequest(seconds=200)) == (200.0, False)
    assert st.target(CreateRequest(seconds=70)) == (70.0, True)  # a plain Creator request works too
    off = studio(tmp_path, creator_rewards_length=False)
    assert off.target(O.StudioRequest()) == (65.0, False)


def test_the_voice_engine_asked_for_is_this_runs_only(tmp_path):
    st = studio(tmp_path)
    st.accept(O.StudioRequest(prompt="x", tts="piper"))
    assert st.s.creator_tts == "piper"
    st.accept(O.StudioRequest(prompt="x"))
    assert st.s.creator_tts == "auto"


# ---------------------------------------------------------------- the steps, in order

def test_steps_only_go_forward_one_at_a_time(tmp_path):
    seen = []
    st = studio(tmp_path, states=seen)
    st.enter("input")
    with pytest.raises(RuntimeError, match="can't go from"):
        st.enter("voice")
    st.enter("script")
    with pytest.raises(RuntimeError):
        st.enter("script")
    with pytest.raises(ValueError):
        st.enter("coffee")
    st.enter("voice")
    assert seen == ["input", "script", "voice"] and [h[0] for h in st.history] == ["input", "script"]
    st.fail()
    assert st.state == O.FAILED and st.failed_at == "voice" and seen[-1] == O.FAILED
    st.enter("input")  # a new run starts clean
    assert st.history == [] and st.failed_at == ""
    assert set(O.STATE_WORDS) == set(O.STATES) | {O.FAILED}


# ---------------------------------------------------------------- SCRIPT

def test_an_address_is_read_and_retold_with_the_source_kept(tmp_path, monkeypatch):
    ai, log, asked = FakeAI(script()), [], []
    monkeypatch.setattr(web, "gather", lambda *a, **k: pytest.fail("an article needs no web search"))
    st = studio(tmp_path, ai=ai, log=log, read=lambda url, **kw: asked.append(url) or ARTICLE)
    board = st.storyboard(O.StudioRequest(url=URL, seconds=20, category="History"))
    assert asked == [URL] and st.state == "script" and st.article is ARTICLE
    assert board.sources[0]["url"] == URL and board.sources[0]["publisher"] == "Example News"
    assert board.prompt == f"Retell: {URL}" and board.written_by == "test-model"
    assert board.scenes[0].beat == "hook" and board.scenes[-1].beat == "cta" and board.cta == "Follow for part two"
    prompt = ai.asked[0].prompt
    assert "retells the source article" in prompt and "Paragraph 3 of the report" in prompt
    assert "vertical 9:16" in prompt and "20 seconds" in prompt
    assert any("Read \"The keeper who never left\" from Example News" in m for m in log)


def test_a_prompt_is_looked_up_and_written_in_the_shape_asked_for(tmp_path, monkeypatch):
    found = NS(found=True, query="lighthouses", sources=[NS(short=lambda: {"title": "Lights", "url": "https://l.example"},
                                                            title="Lights", publisher="L", date="2026-11-02",
                                                            text="Lamps burned oil.", summary="")])
    looked, checked = [], []
    monkeypatch.setattr(web, "gather", lambda prompt, **kw: looked.append((prompt, kw.get("query"))) or found)

    def check(s, board, facts, **kw):
        checked.append((facts, kw["have_sources"], kw["mode"]))
        return factcheck.Report(kw["mode"])

    monkeypatch.setattr(factcheck, "check", check)
    ai = FakeAI(script())
    st = studio(tmp_path, ai=ai, creator_web=True, creator_factcheck="fix")
    board = st.storyboard(O.StudioRequest(prompt="How lighthouses worked", seconds=20, orientation="landscape",
                                          search="lighthouse history", title="My title", ai="cloud_first"))
    assert looked == [("How lighthouses worked", "lighthouse history")]
    assert "wide 16:9" in ai.asked[0].prompt and "Lamps burned oil." in ai.asked[0].prompt
    assert board.title == "My title" and board.sources == [{"title": "Lights", "url": "https://l.example"}]
    assert checked and checked[0][1:] == (True, "fix") and "Lamps burned oil." in checked[0][0]


def test_an_article_is_fact_checked_against_itself(tmp_path, monkeypatch):
    checked = []

    def check(s, board, facts, **kw):
        checked.append(facts)
        return factcheck.Report("fix")

    monkeypatch.setattr(factcheck, "check", check)
    studio(tmp_path, creator_factcheck="fix").storyboard(O.StudioRequest(url=URL, seconds=20))
    assert "The source this script retells (Example News, published 2026-11-01)" in checked[0]
    assert "Paragraph 7 of the report" in checked[0] and "Today's date is" in checked[0]


def test_your_own_script_is_used_as_it_is_and_the_address_left_alone(tmp_path):
    log = []
    st = studio(tmp_path, read=lambda url, **kw: pytest.fail("not read"), log=log)
    board = st.storyboard(O.StudioRequest(url=URL, script="First line of mine here.\n\nSecond line of mine here.",
                                          seconds=20))
    assert board.written_by == "your script" and len(board.scenes) >= 2
    assert any("the address isn't read" in m for m in log)


@pytest.mark.parametrize("error, at", [(scraper.ScrapeError("example.org asks programs not to read that page"), "script")])
def test_a_page_that_cant_be_read_stops_the_run_with_its_reason(tmp_path, error, at):
    def read(url, **kw):
        raise error

    st = studio(tmp_path, read=read)
    with pytest.raises(CreatorError, match="asks programs not to read"):
        st.create(O.StudioRequest(url=URL, seconds=20))
    assert st.state == O.FAILED and st.failed_at == at


# ---------------------------------------------------------------- the whole run (render replaced)

def test_a_run_goes_through_every_step_and_leaves_the_videos_folder(tmp_path, voice, quick):
    states, log = [], []
    st = studio(tmp_path, states=states, log=log)
    result = st.create(O.StudioRequest(url=URL, seconds=20, post=False))
    assert states == list(O.STATES) and st.state == "done"
    assert [h[0] for h in st.history] == list(O.STATES)
    folder = result.folder
    assert result.video.is_file() and result.video.parent == folder and folder.parent == tmp_path / "Out" / "Created"
    names = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    assert {"timeline.json", "clips.json", "README.md", "storyboard.json", "assets/voice.wav",
            "assets/voice.words.json", "scenes/scene_01.png", "scenes/scene_05.png"} <= names
    assert any(n.startswith("assets/sfx/") for n in names)

    tl = T.Timeline.load(folder / "timeline.json")
    T.validate(tl, base=folder)  # after the move, every path in it still leads to its file
    assert all(not Path(a.path).is_absolute() for a in tl.assets.values() if a.path)
    assert (tl.canvas.width, tl.canvas.height) == (1080, 1920) and abs(tl.duration - result.duration) < 0.01
    assert len(tl.track(T.VIDEO_BASE).clips) == 5 and len(tl.track(T.AUDIO_VOICE).clips) == 1
    assert tl.track(T.AUDIO_MUSIC).clips[0].gain_db == timeline_builder.music_gain(st.s.creator_music_db)
    assert tl.audio.duck_db == -16.0
    # The script's twist was its last scene but one, so the writer made that the payoff (every video has one).
    assert [m.kind for m in tl.markers] == ["hook", "payoff", "cta"]
    assert any(c.id.startswith("auto_riser") for c in tl.track(T.AUDIO_SFX).clips)  # a riser runs into the payoff
    assert tl.track(T.SUBTITLES).style.preset == "pop" and tl.track(timeline_builder.TITLES) is not None
    assert tl.meta["source_url"] == URL and tl.meta["sources"][0]["url"] == URL

    index = json.loads((folder / "clips.json").read_text(encoding="utf-8"))
    studio_block = index["creator"]["studio"]
    assert studio_block["source_url"] == URL and studio_block["timeline_file"] == "timeline.json"
    assert studio_block["canvas"] == "1080x1920" and studio_block["timeline"]["scenes"] == 5
    assert [s[0] for s in studio_block["steps"]][:4] == ["input", "script", "voice", "visuals"]
    assert index["automation"]["permission"] == O.PERMISSION
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert "**Made by:** ClipMonster Studio" in readme and "## Retold from" in readme and URL in readme
    assert "ytclip studio --timeline" in readme
    assert quick.sizes == [(1080, 1920)]
    assert not result.checks["creator_rewards"]  # 20 seconds as asked


def test_a_long_wide_video_gets_a_wide_timeline_and_its_chapters(tmp_path, voice, quick):
    board = Storyboard("Lights of the coast", scenes=[
        Scene(i + 1, f"Scene {i + 1} tells one more part of the long story here.", "texture", f"p{i}",
              chapter={0: "The rock", 3: "The storm"}.get(i, ""), beat="hook" if i == 0 else "build")
        for i in range(6)], hook="Lights of the coast", music_mood="Epic")
    st = studio(tmp_path)
    result = st.create(O.StudioRequest(prompt="x", format="long", seconds=30, captions="karaoke", post=False), board)
    tl = quick.timelines[0]
    assert (tl.canvas.width, tl.canvas.height) == (1920, 1080) and quick.sizes == [(1920, 1080)]
    assert tl.track(T.SUBTITLES).style.preset == "karaoke"
    assert [m.label for m in tl.markers if m.kind == "chapter"] == ["The rock", "The storm"]
    picture = tl.assets[tl.track(T.VIDEO_BASE).clips[0].asset]
    assert picture.width > picture.height  # drawn wide
    readme = (result.folder / "README.md").read_text(encoding="utf-8")
    assert "## Chapters (for the description)" in readme and "0:00 The rock" in readme


def test_captions_can_be_left_off_and_scene_files_not_kept(tmp_path, voice, quick):
    st = studio(tmp_path, studio_keep_timeline=False, creator_hook_card=False)
    result = st.create(O.StudioRequest(prompt="Lighthouses", seconds=20, captions="off", post=False))
    assert quick.timelines[0].subtitle_tracks() == []
    names = {p.name for p in result.folder.rglob("*") if p.is_file()}
    assert "timeline.json" not in names and "voice.wav" not in names and not (result.folder / "scenes").exists()
    index = json.loads((result.folder / "clips.json").read_text(encoding="utf-8"))
    assert index["creator"]["studio"]["timeline_file"] == ""
    assert "ytclip studio --timeline" not in (result.folder / "README.md").read_text(encoding="utf-8")


def test_cancelling_stops_at_the_next_step(tmp_path, voice, quick):
    stop = []
    st = O.StudioOrchestrator(settings(tmp_path), register=False, ask=FakeAI(script()),
                              cancelled=lambda: bool(stop), on_state=lambda state: state == "visuals" and stop.append(1))
    with pytest.raises(ffmpeg.Cancelled):
        st.create(O.StudioRequest(prompt="Lighthouses", seconds=20, post=False))
    assert st.state == O.FAILED and st.failed_at == "visuals"
    assert not list((tmp_path / "Out").rglob("*.mp4"))


def test_a_cloud_narration_is_fitted_not_read_again(tmp_path, monkeypatch):
    st = studio(tmp_path)
    track = NS(engine_key="elevenlabs", engine="ElevenLabs", duration=50.0)
    monkeypatch.setattr(voice_engine, "CLOUD_ENGINES", {"elevenlabs"})
    monkeypatch.setattr(voice_studio, "retime", lambda t, speed, out=None: ("fitted", speed, out.name))
    monkeypatch.setattr(voice_engine, "synthesize", lambda *a, **k: pytest.fail("paid for twice"))
    assert st.reread(None, track, "v", 1.1, tmp_path, None) == ("fitted", 1.1, "voice_fitted.wav")


# ---------------------------------------------------------------- the files

def test_sound_effects_travel_with_the_timeline_and_your_own_files_stay_put(tmp_path):
    pkg, elsewhere = tmp_path / "pkg", tmp_path / "elsewhere"
    (pkg / "scenes").mkdir(parents=True)
    elsewhere.mkdir()
    for f in (pkg / "scenes" / "scene_01.png", elsewhere / "whoosh.wav", elsewhere / "my broll.mp4",
              elsewhere / "song.mp3"):
        f.write_bytes(b"x")
    tl = T.standard("t", duration=4)
    tl.assets = {"p": T.Asset("image", str(pkg / "scenes" / "scene_01.png")),
                 "w": T.Asset("audio", str(elsewhere / "whoosh.wav")),
                 "b": T.Asset("video", str(elsewhere / "my broll.mp4"), duration=9),
                 "m": T.Asset("audio", str(elsewhere / "song.mp3")), "c": T.Asset("color", color="#000000")}
    tl.track(T.AUDIO_SFX).clips += [T.AudioClip("a", "w", 0, 1), T.AudioClip("b", "w", 2, 1)]
    tl.track(T.AUDIO_MUSIC).clips.append(T.AudioClip("m", "m", 0, 4))
    O.gather_files(tl, pkg)
    assert tl.assets["p"].path == "scenes/scene_01.png" and tl.assets["w"].path == "assets/sfx/whoosh.wav"
    assert (pkg / "assets" / "sfx" / "whoosh.wav").is_file()
    assert tl.assets["b"].path == str(elsewhere / "my broll.mp4")  # your B-roll and music aren't copied
    assert tl.assets["m"].path == str(elsewhere / "song.mp3")
    folder = tmp_path / "video"
    vis = [NS(path=pkg / "scenes" / "scene_01.png"), NS(path=elsewhere / "my broll.mp4")]
    import dataclasses

    @dataclasses.dataclass
    class V:
        path: Path

    moved = O.move_package(pkg, folder, [V(v.path) for v in vis])
    assert moved[0].path == folder / "scenes" / "scene_01.png" and moved[0].path.is_file()
    assert moved[1].path == elsewhere / "my broll.mp4"
    assert (folder / "assets" / "sfx" / "whoosh.wav").is_file() and not list(pkg.rglob("*.png"))


def test_a_long_timeline_warns_when_memory_is_short():
    tl = T.standard("long", duration=600)
    tl.assets["p"] = T.Asset("image", "p.png")
    tl.track(T.VIDEO_BASE).clips += [T.VideoClip(f"s{i}", "p", i * 4, 4) for i in range(150)]
    note = O.memory_note(tl, available=4e9)
    assert "150 scenes" in note and "6.4 GB" in note and "4.0 GB is free" in note
    assert O.memory_note(tl, available=32e9) == ""
    short = T.standard("short", duration=60)
    assert O.memory_note(short, available=2e9) == ""


def test_render_timeline_says_when_there_is_nothing_to_render(tmp_path):
    with pytest.raises(CreatorError, match="no timeline"):
        O.render_timeline(tmp_path / "missing")
    (tmp_path / "timeline.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CreatorError, match="can't be used"):
        O.render_timeline(tmp_path)


# ---------------------------------------------------------------- for real

def test_an_article_becomes_a_finished_video_and_its_timeline_renders_again(tmp_path, voice):
    log = []
    st = studio(tmp_path, log=log, creator_music="composed")
    result = st.create(O.StudioRequest(url=URL, seconds=20, post=False))
    info = ffmpeg.probe(result.video)
    assert (info.width, info.height) == (1080, 1920) and info.has_audio
    assert abs(info.duration - result.duration) < 0.2 and 12 < info.duration < 30
    assert result.checks["ok"] and abs(result.checks["lufs"] - (-14.0)) <= 1.0 and result.checks["true_peak"] <= -0.5
    assert st.render_report.encoder and st.mix["music"].startswith("composed here")
    assert (result.folder / "thumbnails" / "01.jpg").is_file()

    # Edit the timeline (another caption style, no film look) and render it again from the folder alone.
    path = result.folder / "timeline.json"
    tl = T.Timeline.load(path)
    tl.track(T.SUBTITLES).style.preset = "plain"
    tl.look = []
    tl.dump(path)
    again = O.render_timeline(path, settings=st.s)
    assert again.path.is_file() and again.path.name.endswith("(edit).mp4") and again.path.parent == result.folder
    assert abs(ffmpeg.probe(again.path).duration - info.duration) < 0.2

"""The Studio's run from a prompt or an article's address to a video's folder, offline: a stand-in for the AI,
the page reader and the voice, textures for pictures. One test renders for real; the others replace the render
with a marker file so they stay quick."""

import json
import time
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
from ytclip.studio import scriptwriter as W
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


# ---------------------------------------------------------------- what the review of the Studio found

def test_the_fact_checks_wording_is_not_kept_when_it_is_the_articles_own(tmp_path, monkeypatch):
    """"Rewritten to say what the source says" is, at its laziest, the source's sentence: the copy check ran
    before the fact check and never saw it."""
    lifted = ARTICLE.text.split("\n\n")[2]  # a whole paragraph of the article

    def check(s, board, facts, **kw):
        report = factcheck.Report("fix", checked=3)
        was = board.scenes[1].speech_text
        report.claims = [factcheck.Claim(2, 2, was, "contradicted", fix=lifted, changed=True),
                         factcheck.Claim(3, 3, board.scenes[2].speech_text, "contradicted",
                                         fix="The lamp room was rebuilt later.", changed=True)]
        board.scenes[1].speech_text = lifted
        board.scenes[2].speech_text = "The lamp room was rebuilt later."
        return report

    monkeypatch.setattr(factcheck, "check", check)
    log = []
    st = studio(tmp_path, log=log, creator_factcheck="fix")
    board = st.storyboard(O.StudioRequest(url=URL, seconds=20))
    assert lifted not in board.script and board.scenes[1].speech_text.startswith("Then wave2n0")  # put back
    assert board.scenes[2].speech_text == "The lamp room was rebuilt later."  # an honest fix stays
    claims = {c["n"]: c for c in board.factcheck["claims"]}
    assert claims[2]["changed"] is False and claims[3]["changed"] is True  # the first stays flagged for you
    assert any("the article's own" in m for m in log)


def test_the_articles_sentences_put_into_scenes_side_by_side_go_back_too(tmp_path, monkeypatch):
    """Each rewrite was under the mark on its own (a short's scenes are a sentence each), so each was kept,
    and together they were the article word for word."""
    words = ARTICLE.text.split("\n\n")[2].split()
    parts = [" ".join(words[0:9]) + ".", " ".join(words[9:18]) + ".", " ".join(words[18:]) + ""]
    assert all(len(p.split()) < W.COPY_RUN for p in parts)

    def check(s, board, facts, **kw):
        report = factcheck.Report("fix", checked=4)
        report.claims = [factcheck.Claim(n, n, board.scenes[n - 1].speech_text, "contradicted", fix=parts[n - 2],
                                         changed=True) for n in (2, 3, 4)]
        report.claims.append(factcheck.Claim(5, 5, board.scenes[4].speech_text, "contradicted",
                                             fix="The lamp room was rebuilt later.", changed=True))
        for n in (2, 3, 4):
            board.scenes[n - 1].speech_text = parts[n - 2]
        board.scenes[4].speech_text = "The lamp room was rebuilt later."
        return report

    monkeypatch.setattr(factcheck, "check", check)
    log = []
    board = studio(tmp_path, log=log, creator_factcheck="fix").storyboard(O.StudioRequest(url=URL, seconds=20))
    assert not W.copied(board.script, ARTICLE.text).lifted
    assert [sc.speech_text.startswith("Then wave") for sc in board.scenes[1:4]] == [True, True, True]
    assert board.scenes[4].speech_text == "The lamp room was rebuilt later."  # the honest fix stays
    claims = {c["n"]: c["changed"] for c in board.factcheck["claims"]}
    assert claims == {2: False, 3: False, 4: False, 5: True}
    assert any("3 scenes was the article's own" in m for m in log)
    assert not any(n.startswith("some phrases") for n in board.notes)


def test_the_classic_creator_fits_a_cloud_narration_too(tmp_path, monkeypatch):
    """ElevenLabs is in the shared voice-engine list: the classic Creator re-read the whole script, paid twice."""
    from ytclip.creator.orchestrator import VideoCreatorOrchestrator

    classic = VideoCreatorOrchestrator(settings(tmp_path), register=False)
    track = NS(engine_key="elevenlabs", engine="ElevenLabs", duration=50.0)
    monkeypatch.setattr(voice_engine, "CLOUD_ENGINES", {"elevenlabs"})
    monkeypatch.setattr(voice_studio, "retime", lambda t, speed, out=None: ("fitted", speed, out.name))
    monkeypatch.setattr(voice_engine, "synthesize", lambda *a, **k: pytest.fail("paid for twice"))
    assert classic.reread(None, track, "v", 0.9, tmp_path, None) == ("fitted", 0.9, "voice_fitted.wav")


def test_a_long_storyboard_or_script_handed_in_is_taken_whole(tmp_path):
    """The classic cap of 20 scenes cut an eight-minute storyboard to two and a half, without a word."""
    long_board = script(45)
    st = studio(tmp_path)
    board = st.storyboard(O.StudioRequest(storyboard=long_board, format="long", seconds=480))
    assert len(board.scenes) == 45
    paragraphs = "\n\n".join(f"Paragraph number {i} says something about the sea." for i in range(60))
    board = studio(tmp_path).storyboard(O.StudioRequest(script=paragraphs, format="long", seconds=480))
    assert len(board.scenes) == 60
    # The classic Creator still takes one short video's worth, and now says what it left out.
    from ytclip.creator.orchestrator import VideoCreatorOrchestrator

    log = []
    classic = VideoCreatorOrchestrator(settings(tmp_path), log=log.append, register=False)
    assert len(classic.storyboard(CreateRequest(script=paragraphs)).scenes) == 20
    assert any("kept the first 20 of 60 scenes" in m and "Studio" in m for m in log)


def test_a_long_storyboard_with_a_scene_that_splits_keeps_its_ending(tmp_path):
    """Tidying a storyboard can split a long scene in two. The limit was the number of scenes handed in, so
    a 45-scene storyboard with one long scene came back without its last scene, and one more went missing
    each time it was handed back."""
    long_board = script(45)
    long_board["scenes"][20]["speech_text"] = (
        "The keeper wrote in his log every single night for forty years without missing a day of it. "
        "Nobody on the mainland ever read a page of what he had written there until the winter he died alone.")
    ending = long_board["scenes"][-1]["speech_text"]
    log = []
    st = studio(tmp_path, log=log)
    board = st.storyboard(O.StudioRequest(storyboard=long_board, format="long", seconds=480))
    assert len(board.scenes) == 46 and board.scenes[-1].speech_text == ending
    again = st.storyboard(O.StudioRequest(storyboard=board.to_json(), format="long", seconds=480))  # preview, then Make
    assert len(again.scenes) == 46 and again.scenes[-1].speech_text == ending
    assert not any("kept the first" in m for m in log)
    # A cut the classic Creator made is said once: not again when the trimmed storyboard is handed back.
    from ytclip.creator.orchestrator import VideoCreatorOrchestrator

    paragraphs = "\n\n".join(f"Paragraph number {i} says something about the sea." for i in range(30))
    log.clear()
    classic = VideoCreatorOrchestrator(settings(tmp_path), log=log.append, register=False)
    preview = classic.storyboard(CreateRequest(script=paragraphs))
    assert sum("kept the first 20 of 30" in m for m in log) == 1
    trimmed = preview.to_json()
    trimmed["scenes"] = trimmed["scenes"][:8]
    log.clear()
    made = classic.storyboard(CreateRequest(storyboard=trimmed))
    assert len(made.scenes) == 8 and not any("kept the first" in m for m in log)
    assert not any(n.startswith("kept the first") for n in made.notes)


def test_a_long_or_wide_video_keeps_its_length_even_with_rewards_ticked(tmp_path):
    log = []
    st = studio(tmp_path, log=log, creator_rewards_length=True)
    assert st.target(O.StudioRequest(seconds=480, rewards=True)) == (480.0, False)
    assert st.target(O.StudioRequest(seconds=480, rewards=True, format="long")) == (480.0, False)
    assert st.target(O.StudioRequest(seconds=70, rewards=True, orientation="landscape")) == (70.0, False)
    assert st.target(O.StudioRequest(format="long", rewards=True)) == (O.LONG_SECONDS, False)
    assert st.target(O.StudioRequest(seconds=70, rewards=True)) == (70.0, True)
    assert sum("isn't applied here" in m for m in log) == 1  # said once, not for every step


def test_two_effects_with_the_same_file_name_both_travel(tmp_path):
    pkg, mine = tmp_path / "pkg", tmp_path / "my sfx"
    for cue in ("whoosh", "impact"):
        (mine / cue).mkdir(parents=True)
        (mine / cue / "01.wav").write_bytes(cue.encode())
    pkg.mkdir()
    tl = T.standard("t", duration=4)
    tl.assets = {"w": T.Asset("audio", str(mine / "whoosh" / "01.wav")), "i": T.Asset("audio", str(mine / "impact" / "01.wav"))}
    tl.track(T.AUDIO_SFX).clips += [T.AudioClip("a", "w", 0, 1), T.AudioClip("b", "i", 2, 1), T.AudioClip("c", "w", 3, 1)]
    O.gather_files(tl, pkg)
    assert tl.assets["w"].path == "assets/sfx/01.wav" and tl.assets["i"].path == "assets/sfx/01_2.wav"
    assert (pkg / tl.assets["w"].path).read_bytes() == b"whoosh" and (pkg / tl.assets["i"].path).read_bytes() == b"impact"


def test_memory_is_read_by_the_apps_own_reader(monkeypatch):
    """The warning was promised in the README but asked psutil, which the app doesn't ship."""
    from ytclip.intelligence import hardware

    tl = T.standard("long", duration=600)
    tl.assets["p"] = T.Asset("image", "p.png")
    tl.track(T.VIDEO_BASE).clips += [T.VideoClip(f"s{i}", "p", i * 4, 4) for i in range(150)]
    monkeypatch.setattr(hardware, "ram_free_bytes", lambda *a: int(4e9))
    assert "4.0 GB is free" in O.memory_note(tl)
    monkeypatch.setattr(hardware, "ram_free_bytes", lambda *a: int(64e9))
    assert O.memory_note(tl) == ""
    monkeypatch.setattr(hardware, "ram_free_bytes", lambda *a: 0)  # it couldn't be read: no false alarm
    assert O.memory_note(tl) == ""


def creation(folder: Path, **over) -> Path:
    """A Studio video's folder as the Studio leaves it (the files only)."""
    (folder / "scenes").mkdir(parents=True)
    (folder / "assets" / "sfx").mkdir(parents=True)
    for name in ("01 - Title.mp4", "scenes/scene_01.png", "scenes/scene_02_pexels_123.mp4", "assets/voice.wav",
                 "assets/sfx/whoosh.wav", "storyboard.json", "README.md"):
        (folder / name).write_bytes(b"x")
    tl = T.standard("Title", duration=2)
    tl.assets["c"] = T.Asset("color", color="#101010")
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("a", "c", 0, 2))
    tl.dump(folder / "timeline.json")
    data = {"video": {"id": "c-1", "title": "Title"}, "clips": [
        {"rank": 1, "file": "01 - Title.mp4", "title": "Title", "caption": "A caption", "hashtags": ["history"]}],
        "automation": {"category": "History", "category_id": "h", "account_id": "acct"},
        "creator": {"ai_generated": True, "studio": {"timeline": True}}} | over
    (folder / "clips.json").write_text(json.dumps(data), encoding="utf-8")
    return folder


def test_an_edit_is_rendered_out_of_sight_then_listed_with_its_video(tmp_path, monkeypatch):
    folder = creation(tmp_path / "Automation" / "History" / "Created" / "v1")
    seen = {}

    def render(tl, dest, *, base=None, work=None, **kw):
        seen["dest"], seen["work"] = Path(dest), Path(work)
        assert Path(dest).name.startswith(".") and Path(work).parent == folder  # never a name posting takes in
        Path(dest).write_bytes(b"edited")
        return timeline_compiler.RenderReport(Path(dest), 2.0, "libx264")

    monkeypatch.setattr(timeline_compiler, "render", render)
    log = []
    report = O.render_timeline(folder, settings=Settings(), log=log.append)
    assert report.path == folder / "Title (edit).mp4" and report.path.read_bytes() == b"edited"
    assert not seen["work"].exists()
    clips = json.loads((folder / "clips.json").read_text(encoding="utf-8"))["clips"]
    assert [c["file"] for c in clips] == ["01 - Title.mp4", "Title (edit).mp4"]
    assert clips[1] == clips[0] | {"rank": 2, "file": "Title (edit).mp4", "title": "Title (edit)",
                                   "edit_of": "01 - Title.mp4"}
    assert any("waits there for your OK" in m for m in log)
    O.render_timeline(folder, settings=Settings())  # rendered again over the last edit: still listed once
    assert len(json.loads((folder / "clips.json").read_text(encoding="utf-8"))["clips"]) == 2

    # A render that fails leaves no half-made video behind, and nothing new in the list.
    def fail(tl, dest, **kw):
        Path(dest).write_bytes(b"half")
        raise timeline_compiler.CompileError("ffmpeg couldn't render the timeline: no")

    monkeypatch.setattr(timeline_compiler, "render", fail)
    other = creation(tmp_path / "Automation" / "History" / "Created" / "v2")
    with pytest.raises(CreatorError, match="couldn't render"):
        O.render_timeline(other, settings=Settings())
    assert sorted(p.name for p in other.iterdir() if p.suffix == ".mp4") == ["01 - Title.mp4"]
    assert not list(other.glob(".render-*"))
    # A folder that isn't a creation's (a timeline someone wrote) is rendered and nothing is listed.
    assert O.list_edit(tmp_path, tmp_path / "x.mp4") == ""


def test_posting_takes_in_the_video_and_its_edit_but_never_the_makings(tmp_path):
    """Intake searched the whole folder: a Studio video's raw stock clips and its unlisted edit were queued
    with no AI label and no review."""
    from ytclip.posting.store import Store

    root = tmp_path / "Automation"
    folder = creation(root / "History" / "Created" / "v1")
    (folder / ".render-abc").mkdir()
    (folder / ".render-abc" / "edit.mp4").write_bytes(b"x")
    (folder / "Stray (edit).mp4").write_bytes(b"x")  # a video beside it that the list doesn't have
    store = Store(tmp_path / "library.json")
    took = store.add_paths([root], watched=True, now=1e12)
    assert [Path(i.path).name for i in took] == ["01 - Title.mp4"]
    assert took[0].ai_generated and took[0].review != "waiting"  # (review for it is decided when it's queued)
    O.list_edit(folder, folder / "Stray (edit).mp4")
    took = store.add_paths([root], watched=True, now=1e12)
    assert [Path(i.path).name for i in took] == ["Stray (edit).mp4"]
    edit = took[0]
    assert edit.ai_generated and edit.review == "waiting" and edit.caption == "A caption" and edit.category_id == "h"
    # By hand, a folder still never brings in a scene's stock clip; the file itself can be picked.
    by_hand = Store(tmp_path / "other.json")
    assert sorted(Path(i.path).name for i in by_hand.add_paths([folder])) == ["01 - Title.mp4", "Stray (edit).mp4"]
    assert [Path(i.path).name for i in by_hand.add_paths([folder / "scenes" / "scene_02_pexels_123.mp4"])] \
        == ["scene_02_pexels_123.mp4"]


def test_the_timeline_stays_while_an_edit_of_it_is_still_to_post(tmp_path):
    """Posting the original cleared the timeline and its files from under the edit."""
    from ytclip.posting import records

    root = tmp_path / "Automation"
    folder = creation(root / "History" / "Created" / "v1")
    (folder / "Title (edit).mp4").write_bytes(b"x")
    O.list_edit(folder, folder / "Title (edit).mp4")
    (folder / "01 - Title.mp4").unlink()  # posted and deleted
    records._tidy(folder, {"01 - Title.mp4"}, [root], lambda p: p.unlink())
    for name in ("timeline.json", "storyboard.json", "scenes/scene_02_pexels_123.mp4", "assets/voice.wav",
                 "clips.json", "Title (edit).mp4"):
        assert (folder / name).exists(), name
    assert [c["file"] for c in json.loads((folder / "clips.json").read_text(encoding="utf-8"))["clips"]] \
        == ["Title (edit).mp4"]
    (folder / "Title (edit).mp4").unlink()  # the edit posted too: now everything goes
    records._tidy(folder, {"Title (edit).mp4"}, [root], lambda p: p.unlink())
    assert not folder.exists()


def fake_render(monkeypatch):
    def render(tl, dest, *, base=None, work=None, **kw):
        Path(dest).write_bytes(b"edited")
        return timeline_compiler.RenderReport(Path(dest), 2.0, "libx264")

    monkeypatch.setattr(timeline_compiler, "render", render)


def test_an_edit_saved_anywhere_else_is_not_called_listed_and_never_slips_into_the_queue(tmp_path, monkeypatch):
    """Saved into a folder inside the video's, the edit was written into clips.json under a name posting never
    looks up, the log promised the AI-generated label and the OK, and posting took it in with neither."""
    from ytclip.posting.store import Store

    root = tmp_path / "Automation"
    folder = creation(root / "History" / "Created" / "v1")
    fake_render(monkeypatch)
    log = []
    O.render_timeline(folder, folder / "edits" / "v2.mp4", settings=Settings(), log=log.append)
    assert (folder / "edits" / "v2.mp4").read_bytes() == b"edited"
    clips = json.loads((folder / "clips.json").read_text(encoding="utf-8"))["clips"]
    assert [c["file"] for c in clips] == ["01 - Title.mp4"]  # no record posting can't match
    assert not any("waits there for your OK" in m for m in log)
    # What the render says is what posting does: one folder down, an unlisted video is left alone.
    (said,) = log
    assert "isn't listed with the video" in said and "Posting leaves an unlisted video there alone" in said
    assert "no AI-generated label" not in said and said.endswith("the edit is listed, labelled and held.")
    assert O.list_edit(folder, folder / "edits" / "v2.mp4") == ""
    took = Store(tmp_path / "library.json").add_paths([root], watched=True, now=1e12)
    assert [Path(i.path).name for i in took] == ["01 - Title.mp4"]
    # Further down, or anywhere else, a watched folder takes it for an ordinary video, and the render says that.
    for place in (folder / "edits" / "old" / "v3.mp4", root / "History" / "Clips" / "v4.mp4"):
        log.clear()
        O.render_timeline(folder, place, settings=Settings(), log=log.append)
        (said,) = log
        assert "no AI-generated label and no wait for your OK" in said and "leaves an unlisted" not in said
        assert said.endswith("the edit is listed, labelled and held.")
        took = [i for i in Store(tmp_path / f"{place.stem}.json").add_paths([root], watched=True, now=1e12)
                if Path(i.path).name == place.name]
        assert len(took) == 1 and not took[0].ai_generated and took[0].review != "waiting"
        place.unlink()
    assert [c["file"] for c in json.loads((folder / "clips.json").read_text(encoding="utf-8"))["clips"]] \
        == ["01 - Title.mp4"]  # asking what would happen listed nothing
    # A timeline written by hand, in a folder that's nobody's creation: leaving the place out lists nothing
    # either, so nothing is promised.
    own = tmp_path / "mine"
    own.mkdir()
    tl = T.standard("Mine", duration=2)
    tl.assets["c"] = T.Asset("color", color="#101010")
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("a", "c", 0, 2))
    tl.dump(own / "timeline.json")
    log.clear()
    O.render_timeline(own, tmp_path / "elsewhere" / "mine.mp4", settings=Settings(), log=log.append)
    assert not any("listed, labelled and held" in m for m in log)
    log.clear()
    O.render_timeline(own, settings=Settings(), log=log.append)
    assert log == []
    # An ordinary clips folder one level under another is still taken in as before.
    plain = root / "History" / "Clips" / "more"
    plain.mkdir(parents=True)
    (plain / "a clip.mp4").write_bytes(b"x")
    took = Store(tmp_path / "second.json").add_paths([root], watched=True, now=1e12)
    assert sorted(Path(i.path).name for i in took) == ["01 - Title.mp4", "a clip.mp4"]


def test_rendering_over_an_edit_says_what_posting_will_do_with_it(tmp_path, monkeypatch):
    """The log promised the queue every time; an edit you removed from the queue (or one that posted) never
    comes back by itself."""
    from ytclip.posting.store import Store

    root = tmp_path / "Automation"
    folder = creation(root / "History" / "Created" / "v1")
    fake_render(monkeypatch)
    log = []
    O.render_timeline(folder, settings=Settings(), log=log.append)
    assert any("joins the queue" in m for m in log)
    store = Store(tmp_path / "library.json")
    took = {Path(i.path).name: i for i in store.add_paths([root], watched=True, now=1e12)}
    store.remove([took["Title (edit).mp4"].id])
    log.clear()
    O.render_timeline(folder, settings=Settings(), log=log.append)
    assert not any("joins the queue" in m for m in log)
    (said,) = log
    assert "replaces the edit made before" in said and "Queue tab" not in said  # no button there adds a video
    assert "render it under a new name" in said and '"title" in timeline.json' in said and "--out" in said
    assert store.add_paths([root], watched=True, now=1e12) == []  # as the log said
    # And the way it gives works, both ways: a new name beside the video is listed, taken in and held.
    log.clear()
    O.render_timeline(folder, folder / "Title, shorter.mp4", settings=Settings(), log=log.append)
    assert any("joins the queue" in m for m in log)
    data = json.loads((folder / "timeline.json").read_text(encoding="utf-8"))
    data["title"] = "Title two"
    (folder / "timeline.json").write_text(json.dumps(data), encoding="utf-8")
    O.render_timeline(folder, settings=Settings(), log=log.append)
    took = {Path(i.path).name: i for i in store.add_paths([root], watched=True, now=1e12)}
    assert sorted(took) == ["Title two (edit).mp4", "Title, shorter.mp4"]
    assert all(i.ai_generated and i.review == "waiting" for i in took.values())


def test_an_edit_rendered_again_after_the_last_one_posted_and_was_cleared_is_queued(tmp_path, monkeypatch):
    """With "delete after posting" on (the default), the edit that posted keeps its entry and its path. The next
    render under the same name was promised the queue and never taken in: the path counted as known."""
    from ytclip.posting import records
    from ytclip.posting.accounts import Accounts
    from ytclip.posting.config import Settings as Posting
    from ytclip.posting.store import Store, TikTokVideo

    accs = Accounts.load()
    a = accs.add("")
    a.username, a.logged_in = "history", True
    accs.save()
    root = tmp_path / "Automation"
    folder = creation(root / "History" / "Created" / "v1")
    fake_render(monkeypatch)
    log = []
    O.render_timeline(folder, settings=Settings(), log=log.append)
    store = Store(tmp_path / "library.json")
    took = {Path(i.path).name: i for i in store.add_paths([root], watched=True, now=1e12)}
    edit, now = took["Title (edit).mp4"], time.time()
    edit.account, edit.review = a.id, ""
    edit.status, edit.posted_at, edit.posted_via = "posted", now - 7200, "browser"
    edit.queue_pos = edit.planned_at = None
    store.update_tiktok_videos(a.id, [TikTokVideo(id="7009", description="A caption", create_time=now - 7100,
                                                  views=10, url="https://www.tiktok.com/@history/video/7009")])
    edit.tiktok_id, edit.tiktok_url = "7009", "https://www.tiktok.com/@history/video/7009"
    done = records.finish(store, accs, Posting(records_folder=str(tmp_path / "Records")), [root], now=now)
    assert done.cleaned == 1 and not done.failed and edit.cleaned_at
    assert not (folder / "Title (edit).mp4").exists() and (folder / "timeline.json").exists()
    assert store.add_paths([root], watched=True, now=1e12) == []
    log.clear()
    O.render_timeline(folder, settings=Settings(), log=log.append)
    assert any("joins the queue" in m for m in log)
    (again,) = store.add_paths([root], watched=True, now=1e12)  # as the log said
    assert Path(again.path).name == "Title (edit).mp4" and again.id != edit.id
    assert again.ai_generated and again.review == "waiting" and again.status != "posted"
    assert store.add_paths([root], watched=True, now=1e12) == []  # once
    # Only an edit comes back under a name that posted: the same clip exported again isn't posted twice.
    plain = root / "History" / "Clips"
    plain.mkdir(parents=True)
    (plain / "a clip.mp4").write_bytes(b"x")
    (clip,) = store.add_paths([plain], watched=True, now=1e12)
    clip.status, clip.cleaned_at = "posted", now
    assert store.add_paths([plain], watched=True, now=1e12) == []
    # And one you took out of the queue stays out, edit or not.
    store.remove([again.id])
    O.render_timeline(folder, settings=Settings(), log=log.append)
    assert store.add_paths([root], watched=True, now=1e12) == []


def test_the_marker_arrives_before_the_stock_clips(tmp_path, monkeypatch):
    """timeline.json is what tells intake a scenes/ folder holds a video's makings. Moved last, a scan landing
    mid-delivery (seconds, when the work folder is on another drive) queued the raw stock clips."""
    from ytclip.posting.store import Store

    root = tmp_path / "Automation"
    pkg, folder = tmp_path / "work" / "pkg", root / "History" / "Created" / "v1"
    for name in ("assets/voice.wav", "assets/sfx/whoosh.wav", "scenes/scene_01.png", "scenes/scene_02_pexels_1.mp4",
                 "scenes/scene_03_pixabay_9.mp4", "timeline.json"):
        (pkg / name).parent.mkdir(parents=True, exist_ok=True)
        (pkg / name).write_bytes(b"x")
    folder.mkdir(parents=True)
    store, moved, taken = Store(tmp_path / "library.json"), [], []
    real = O.shutil.move

    def move(src, dest):
        taken.extend(store.add_paths([root], watched=True, now=1e12))  # intake looks before every file lands
        moved.append(Path(src).name)
        return real(src, dest)

    monkeypatch.setattr(O.shutil, "move", move)
    O.move_package(pkg, folder, [])
    assert moved[0] == "timeline.json" and len(moved) == 6
    assert taken == [] and store.add_paths([root], watched=True, now=1e12) == []


def test_what_a_cut_off_render_left_is_cleared_away(tmp_path, monkeypatch):
    """The work folder of a render is inside the video's folder. Left by a crash it was never reclaimed, and
    it kept the folder from going once everything in it had posted."""
    import os

    from ytclip.posting import records

    root = tmp_path / "Automation"
    folder = creation(root / "History" / "Created" / "v1")
    old, live = folder / ".render-old", folder / ".render-live"
    for d in (old, live):
        (d / "stills").mkdir(parents=True)
        (d / ".edit.mp4").write_bytes(b"half")
        (d / "stills" / "001.png").write_bytes(b"x")
    long_ago = 1_000_000.0
    for f in (old, *old.rglob("*")):
        os.utime(f, (long_ago, long_ago))
    fake_render(monkeypatch)
    O.render_timeline(folder, settings=Settings())  # the next render clears what's gone quiet, only that
    assert not old.exists() and (live / ".edit.mp4").exists()
    assert O.sweep_renders(tmp_path / "nowhere") == 0
    # Clean up, and delete-after-posting: with nothing left to post, a leftover goes with the rest.
    for name in ("01 - Title.mp4", "Title (edit).mp4"):
        (folder / name).unlink()
    records._tidy(folder, {"01 - Title.mp4", "Title (edit).mp4"}, [root], lambda p: p.unlink())
    assert not folder.exists()


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
    # The edit is listed with its video (so it's labelled and reviewed like it), and nothing is left of the work.
    listed = json.loads((result.folder / "clips.json").read_text(encoding="utf-8"))["clips"]
    assert [c["file"] for c in listed] == [result.video.name, again.path.name]
    assert listed[1]["edit_of"] == result.video.name and listed[1]["title"].endswith("(edit)")
    assert not list(result.folder.glob(".render-*")) and not list(result.folder.rglob(".edit.mp4"))


# ---------------------------------------------------------------- automation's queue

def test_a_queued_studio_request_is_made_by_the_studio_and_a_creators_by_the_creator(tmp_path, monkeypatch):
    from test_pipeline import _runner

    from ytclip.creator import director
    from ytclip.creator import orchestrator as creator

    made = []

    def by(name):
        def create(self, req, board=None):
            made.append((name, type(req).__name__, getattr(req, "url", "")))
            self._progress("Laying out the timeline", None)
            self._progress("Putting the video together", 0.5)
            folder = tmp_path / "Automation" / name
            folder.mkdir(parents=True, exist_ok=True)
            return creator.CreationResult("c-1", folder, folder / "01.mp4", director.from_script("One line here."),
                                          64.0, timings={"render": 9.0})
        return create

    monkeypatch.setattr(O.StudioOrchestrator, "create", by("studio"))
    monkeypatch.setattr(creator.VideoCreatorOrchestrator, "create", by("creator"))
    s = Settings().apply({"auto_output_dir": str(tmp_path / "Automation"), "output_dir": str(tmp_path / "Out"),
                          "auto_start_hour": 0, "auto_end_hour": 24})
    r = _runner(tmp_path, s)
    a = r.add_creation(O.StudioRequest(url=URL, format="long"))
    b = r.add_creation(CreateRequest(prompt="memory"))
    assert r.db.get(a).title == URL
    r.process(r.db.get(a), s)
    r.process(r.db.get(b), s)
    assert made == [("studio", "StudioRequest", URL), ("creator", "CreateRequest", "")]
    assert r.db.get(a).status == "done" and r.db.get(a).recipe == "Studio"
    assert r.db.get(b).status == "done" and r.db.get(b).recipe == "Video Creator"


def test_the_studios_progress_words_are_ones_the_status_bar_knows():
    from ytclip import steps

    for message, step in (("Writing the script: reading the page", "script"), ("Recording the voice", "voice"),
                          ("Making pictures: finding footage for scene 2 of 9", "visuals"),
                          ("Laying out the timeline", "render"), ("Putting the video together", "render"),
                          ("Checking the video", "check")):
        assert steps.step_of(message) == step, message

"""The Video Creator (ytclip/creator): the storyboard, the voice's timing, motion, sound effects, the music bed,
the mix and video graphs, the whole run end to end (with a stand-in voice and built-in pictures), the
automation job, the CLI and how posting reads what it made."""

import json
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from ytclip import ai, settings as settings_mod
from ytclip.creator import (compositor, director, motion, orchestrator, originality, sfx_library, soundbed,
                            visual_generator, voice_engine)
from ytclip.creator.director import Scene, Storyboard
from ytclip.settings import Settings



def _board(n=8, words=12, title="Why your brain lies to you"):
    scenes = [Scene(i + 1, " ".join(f"word{i}x{k}" for k in range(words)) + ".") for i in range(n)]
    scenes[0].speech_text = "Your brain is lying to you right now."
    return Storyboard(title=title, scenes=scenes, music_mood="Dark", hook="Your brain lies",
                      caption="How memory fools you", hashtags=["#psychology", "brain facts", "psychology"])


# ---------------------------------------------------------------- director

def test_normalise_fixes_what_a_grammar_cant():
    b = Storyboard(title="  A title, with \"quotes\".  ", music_mood="weird", hashtags=["#Psych", "psych", "a b!"],
                   scenes=[Scene(1, "This hook line is short enough here. And then it keeps going with a "
                                    "second sentence.", camera_motion="spin", sfx_cue="laser"),
                           Scene(2, "Two words."),
                           Scene(3, "", visual_prompt="empty scene"),
                           Scene(4, "A proper scene with enough words to stand on its own feet.",
                                 camera_motion="pan_left", emphasis=["proper", "missing"])])
    out = director.normalise(b, director.get_style("cinematic"))
    texts = [s.speech_text for s in out.scenes]
    assert texts[0] == "This hook line is short enough here."  # split off
    assert texts[1].startswith("And then it keeps going")
    assert all(s.speech_text for s in out.scenes)  # the empty scene is gone
    assert [s.scene_id for s in out.scenes] == list(range(1, len(out.scenes) + 1))
    assert all(s.camera_motion in director.MOTIONS for s in out.scenes)
    assert all(a.camera_motion != b.camera_motion for a, b in zip(out.scenes, out.scenes[1:]))
    assert out.scenes[0].sfx_cue == "bass_drop"
    assert all(s.sfx_cue in director.SFX_CUES for s in out.scenes)
    assert all("cinematic" in s.visual_prompt for s in out.scenes)
    assert out.music_mood in director.MOODS
    assert out.hashtags == ["Psych", "ab"]
    last = out.scenes[-1]
    assert last.emphasis == ["proper"]  # emphasis only on words that are said
    assert out.notes


def test_split_hook_keeps_short_hooks():
    scenes = [Scene(1, "Short hook here. Then more words follow in the same scene for a while.")]
    assert director.split_hook(scenes, 10)
    assert scenes[0].speech_text == "Short hook here." and len(scenes) == 2
    one = [Scene(1, "Already short.")]
    assert not director.split_hook(one, 10)


def test_problems_ask_for_the_right_length():
    b = _board(n=3, words=5)
    out = director.problems(b, 65, 150)
    assert any("needs" in p and "Add about" in p for p in out)
    assert any("at least" in p for p in out)
    lo, aim, hi = director.word_target(65, 150)
    assert lo < aim < hi and 150 <= aim <= 170
    long = _board(n=10, words=40)
    assert any("Cut about" in p for p in director.problems(long, 65, 150))


def _answer(board_json):
    return ai.Answer(board_json, "test-model", "local")


def _good_json(words=16, n=10):
    scenes = [{"scene_id": 1, "speech_text": "Nobody tells you this about your memory.", "visual_type": "generated_image",
               "visual_prompt": "a brain made of glass", "camera_motion": "slow_push_in", "sfx_cue": "bass_drop",
               "sfx_offset_ms": 0}]
    for i in range(2, n + 1):
        scenes.append({"scene_id": i, "speech_text": " ".join(["memory"] + [f"fact{i}w{k}" for k in range(words - 1)]) + ".",
                       "visual_type": "generated_image", "visual_prompt": f"scene {i}", "camera_motion": "pan_left",
                       "sfx_cue": "none", "sfx_offset_ms": 0})
    return {"title": "Your memory is fake", "category": "Psychology", "target_duration": 65, "music_mood": "Mysterious",
            "hook": "Your memory is fake", "caption": "It rewrites itself", "hashtags": ["memory"], "scenes": scenes}


def test_write_asks_again_until_the_length_fits(monkeypatch):
    monkeypatch.setattr(director, "script_writers", lambda s: ["local"])
    asked = []

    def ask(s, task, system, prompt, schema, **kw):
        asked.append(prompt)
        assert kw["only"] == ["local"]
        return _answer(_good_json(words=5 if len(asked) == 1 else 17))

    b = director.write(Settings(), director.Brief("memory", seconds=65, wpm=150), ask=ask)
    assert len(asked) == 2 and "needs fixing" in asked[1] and "Add about" in asked[1]
    assert b.written_by == "test-model" and b.prompt == "memory"
    lo, _, hi = director.word_target(65, 150)
    assert lo <= b.word_count <= hi


def test_write_refuses_a_copy_of_an_earlier_script(monkeypatch):
    monkeypatch.setattr(director, "script_writers", lambda s: ["local"])
    ask = lambda *a, **k: _answer(_good_json(words=17))  # noqa: E731
    with pytest.raises(director.DirectorError, match="too close"):
        director.write(Settings(), director.Brief("memory"), ask=ask, similar=lambda b: (0.9, "Old video"))


def test_write_without_an_ai_says_how_to_go_on(monkeypatch):
    monkeypatch.setattr(director, "script_writers", lambda s: [])
    with pytest.raises(director.DirectorError):
        director.write(Settings(), director.Brief("memory"), ask=lambda *a, **k: None)


def test_from_script_keeps_your_words_and_paragraphs():
    text = ("If someone insults you, don't fight back. Ask them one question.\n\n"
            "It sounds kind. It isn't. That question quietly flips the whole conversation around on them.\n\n"
            "Try it next time and watch what happens.")
    b = director.from_script(text, category="Psychology")
    assert b.scenes[0].speech_text == "If someone insults you, don't fight back."
    assert " ".join(s.speech_text for s in b.scenes).split() == text.split()
    assert b.scenes[-1].speech_text.startswith("Try it next time")  # a blank line starts a scene
    assert b.written_by == "your script" and b.title.startswith("If someone insults you")
    assert all(not s.emphasis for s in b.scenes)
    with pytest.raises(director.DirectorError):
        director.from_script("   ")


def test_storyboard_round_trips_and_ignores_unknown_keys():
    b = _board()
    again = Storyboard.from_json(json.loads(b.dumps()) | {"extra": 1})
    assert again.to_json() == b.to_json()
    raw = {"title": "x", "scenes": [{"speech_text": "hi", "bogus": 2}, "not a scene"]}
    assert Storyboard.from_json(raw).scenes[0].scene_id == 1
    with pytest.raises(ValueError):
        Storyboard.from_json(["no"])


def test_styles_have_distinct_emphasis_colours():
    colours = {s.emphasis for s in director.STYLES.values()}
    assert len(colours) == len(director.STYLES)
    assert all(s.highlight != s.emphasis for s in director.STYLES.values())
    assert director.style_for("Horror stories") == "horror"
    assert director.style_for("Daily history") == "history"


# ---------------------------------------------------------------- originality

def test_originality_remembers_and_compares(tmp_path):
    f = tmp_path / "hist.json"
    script = "the quick brown fox jumps over the lazy dog every single morning without fail"
    originality.remember("c-1", "Fox facts", "foxes", script, file=f)
    entries = originality.load(f)
    score, title = originality.closest(script, entries)
    assert score == 1.0 and title == "Fox facts"
    assert originality.closest("something completely different said in other words here today", entries)[0] == 0
    assert originality.closest(script, entries, skip_id="c-1")[0] == 0
    assert originality.earlier_titles("foxes", entries) == ["Fox facts"]


# ---------------------------------------------------------------- motion

def test_push_in_follows_the_spec():
    m = motion.get("slow_push_in")
    assert motion.at(m, 0)[0] == pytest.approx(1.0)
    assert motion.at(m, 1)[0] == pytest.approx(1.15)
    assert motion.at(m, 0.5)[0] == pytest.approx(1.075)
    out = motion.get("slow_pull_out")
    assert motion.at(out, 0)[0] == pytest.approx(1.15) and motion.at(out, 1)[0] == pytest.approx(1.0)
    still = motion.get("slow_push_in", 0)
    assert motion.at(still, 1)[0] == pytest.approx(motion.at(still, 0)[0])


@pytest.mark.parametrize("name", list(motion.MOVES))
def test_every_move_stays_inside_the_picture(name):
    m = motion.get(name, 1.5)
    for p in np.linspace(0, 1, 11):
        x, y, w, h = motion.viewport(m, p, 1350, 2400, 1080, 1920)
        assert x >= -0.5 and y >= -0.5 and x + w <= 1350.5 and y + h <= 2400.5


def test_scene_spans_overlap_for_the_crossfade():
    bounds = motion.scene_bounds([0.1, 4.0, 9.0])
    assert bounds == [3.75, 8.75]
    spans = motion.clip_spans(bounds, 12.0, 0.3)
    assert spans[0] == (0.0, pytest.approx(3.9)) and spans[1] == (pytest.approx(3.6), pytest.approx(8.9))
    assert spans[-1][1] == 12.0


# ---------------------------------------------------------------- voice timing

def _speech(words: int, rate=24000, word=0.28, gap=0.09):
    parts = []
    t = np.arange(int(word * rate)) / rate
    for _ in range(words):
        parts.append((0.5 * np.sin(2 * np.pi * 180 * t) * np.hanning(len(t))).astype(np.float32))
        parts.append(np.zeros(int(gap * rate), np.float32))
    return np.concatenate(parts)


def test_estimated_word_times_are_ordered_and_inside_the_sound():
    text = "Seven quite ordinary words said out loud"
    audio = _speech(7)
    words = voice_engine.estimate_words(text, audio, 24000, offset=1.0)
    assert [w.text for w in words] == text.split()
    assert all(isinstance(w.start, float) and isinstance(w.end, float) for w in words)
    assert all(a.end <= b.start + 1e-6 for a, b in zip(words, words[1:]))
    assert words[0].start >= 1.0 and words[-1].end <= 1.0 + len(audio) / 24000 + 0.01


def test_align_takes_whispers_times_for_the_scripts_words():
    from ytclip.subtitles import Word

    script = [Word(i, i + 0.5, w) for i, w in enumerate(["Your", "brain", "lies,", "constantly."])]
    heard = [Word(0.2, 0.4, "your"), Word(0.5, 0.9, "brain"), Word(1.0, 1.3, "lies"), Word(1.4, 2.0, "constantly")]
    out, share = voice_engine.align(script, heard)
    assert share == 1.0
    assert [w.text for w in out] == ["Your", "brain", "lies,", "constantly."]  # the script's spelling
    assert out[1].start == pytest.approx(0.5)


def test_voice_aliases_and_learned_pace():
    assert voice_engine.profile("deep_male").key == "en_male_deep"
    assert voice_engine.profile("nonsense").key == voice_engine.DEFAULT_VOICE
    base = voice_engine.learned_wpm("en_male_deep", "kokoro")
    voice_engine.remember_wpm("en_male_deep", 170, 1.0, "kokoro")
    assert voice_engine.learned_wpm("en_male_deep", "kokoro") != base
    assert voice_engine.learned_wpm("en_male_deep", "piper") == base  # each engine learns its own


class ToneVoice(voice_engine.Engine):
    """A stand-in voice: a tone per word, faster when asked to speak faster."""

    key = "tone"

    def missing(self):
        return ""

    def speak(self, text, voice, speed):
        return _speech(len(text.split()), word=0.3 / speed, gap=0.1 / speed), 24000


@pytest.fixture
def tone_voice(monkeypatch):
    monkeypatch.setitem(voice_engine.ENGINE_CLASSES, "tone", ToneVoice)
    monkeypatch.setitem(voice_engine.ENGINE_NAMES, "tone", "Test tones")
    monkeypatch.setattr(voice_engine, "pick_engines", lambda s: ["tone"])


def test_synthesize_reads_every_scene_in_order(tmp_path, tone_voice):
    b = _board(n=4, words=6)
    track = voice_engine.synthesize(b.scenes, Settings(), tmp_path / "v.wav")
    assert track.engine == "Test tones" and track.engine_key == "tone"
    assert [s.scene_id for s in track.scenes] == [1, 2, 3, 4]
    assert all(a.end < b.start for a, b in zip(track.scenes, track.scenes[1:]))
    assert len(track.words) == b.word_count
    with wave.open(str(tmp_path / "v.wav")) as w:
        assert w.getframerate() == 24000
        assert w.getnframes() / 24000 == pytest.approx(track.duration, abs=0.01)
    faster = voice_engine.synthesize(b.scenes, Settings(), tmp_path / "f.wav", speed=1.2)
    assert faster.duration < track.duration and faster.speed == 1.2


def test_a_failing_engine_falls_back_to_the_next(tmp_path, tone_voice, monkeypatch):
    class Broken(ToneVoice):
        key = "broken"

        def speak(self, text, voice, speed):
            raise RuntimeError("model file is damaged")

    monkeypatch.setitem(voice_engine.ENGINE_CLASSES, "broken", Broken)
    monkeypatch.setitem(voice_engine.ENGINE_NAMES, "broken", "Broken")
    logs = []
    track = voice_engine.synthesize(_board(n=2).scenes, Settings(), tmp_path / "v.wav", log=logs.append,
                                    engines=["broken", "tone"])
    assert track.engine_key == "tone" and any("Broken" in m for m in logs)
    with pytest.raises(voice_engine.VoiceError, match="damaged"):
        voice_engine.synthesize(_board(n=2).scenes, Settings(), tmp_path / "v.wav", engines=["broken"])


# ---------------------------------------------------------------- sound effects and music

def test_builtin_effects_are_made_once_and_sound(tmp_path):
    for cue in ("whoosh", "riser", "bass_drop", "impact"):
        path = sfx_library.builtin(cue)
        assert path.is_file()
        with wave.open(str(path)) as w:
            data = np.frombuffer(w.readframes(w.getnframes()), np.int16)
        assert np.abs(data).max() > 3000  # not silence
    whoosh = sfx_library.builtin("whoosh")
    made = whoosh.stat().st_mtime_ns
    assert sfx_library.builtin("whoosh") == whoosh and whoosh.stat().st_mtime_ns == made  # made once


def test_your_own_effects_win(tmp_path):
    folder = tmp_path / "sfx"
    (folder / "whoosh").mkdir(parents=True)
    shutil.copy(sfx_library.builtin("whoosh"), folder / "whoosh" / "mine.wav")
    shutil.copy(sfx_library.builtin("impact"), folder / "Big Impact 02.wav")
    lib = sfx_library.library(folder)
    assert set(lib) == {"whoosh", "impact"}
    assert sfx_library.sound_for("whoosh", folder).name == "mine.wav"
    assert sfx_library.sound_for("riser", folder).parent == sfx_library.builtin_folder()


def test_sfx_plan_times_cues_and_adds_whooshes_on_cuts():
    scenes = [Scene(1, "a", sfx_cue="bass_drop"), Scene(2, "b", sfx_cue="riser", sfx_offset_ms=500),
              Scene(3, "c"), Scene(4, "d"), Scene(5, "e")]
    starts = [0.1, 5.0, 12.0, 20.0, 27.0]
    cuts = motion.scene_bounds(starts)
    ev = sfx_library.plan(scenes, starts, cuts, seed="t", duration=30)
    cues = [(e.cue, round(e.time, 1)) for e in ev]
    assert cues[0] == ("bass_drop", 0.1)
    riser = next(e for e in ev if e.cue == "riser")
    assert riser.time + riser.length == pytest.approx(5.5, abs=0.01)  # builds up to the moment
    whooshes = [e for e in ev if e.cue == "whoosh"]
    assert whooshes and all(b.time - a.time >= sfx_library.AUTO_GAP - 1 for a, b in zip(whooshes, whooshes[1:]))
    assert not sfx_library.plan(scenes, starts, cuts, auto_whoosh=False, duration=30) == ev
    assert [e.time for e in ev] == sorted(e.time for e in ev)


def test_music_bed_is_composed_when_theres_no_folder(tmp_path):
    s = Settings().apply({"creator_music": "auto", "music_folder": ""})
    path, note = soundbed.music_for(s, "Suspense", 12, tmp_path, "seed")
    assert path is not None and path.is_file() and "composed" in note.lower()
    with wave.open(str(path)) as w:
        assert w.getnframes() / w.getframerate() >= 12
    assert soundbed.music_for(s.apply({"creator_music": "off"}), "Suspense", 12, tmp_path)[0] is None
    mine = tmp_path / "music" / "dark"
    mine.mkdir(parents=True)
    shutil.copy(path, mine / "track.wav")
    p2, note2 = soundbed.music_for(s.apply({"music_folder": str(tmp_path / "music")}), "Dark", 12, tmp_path)
    assert p2 == mine / "track.wav"


# ---------------------------------------------------------------- graphs

def test_mix_graph_ducks_music_under_the_voice_and_limits():
    events = [sfx_library.SfxEvent(1.5, "whoosh", Path("w.wav"), -6.0), sfx_library.SfxEvent(3.0, "impact",
                                                                                              Path("i.wav"), -4.0)]
    mi = compositor.MixInputs(Path("v.wav"), 30.0, Path("m.wav"), -20, events, voice_gain_db=2.5)
    args, graph = compositor.mix_graph(mi, {"input_i": -20, "input_tp": -3, "input_lra": 5, "input_thresh": -30,
                                            "target_offset": 0})
    assert args.count("-i") == 4 and "-stream_loop" in args
    assert "sidechaincompress" in graph and "[mus][vkey]" in graph
    assert "adelay=1500|1500" in graph and "adelay=3000|3000" in graph
    assert "volume=2.50dB" in graph and "volume=-20.0dB" in graph
    assert "loudnorm=I=-14" in graph and f"alimiter=limit={compositor.LIMIT}" in graph
    assert graph.endswith("[aout]")
    _, measure = compositor.mix_graph(compositor.MixInputs(Path("v.wav"), 10.0), measuring=True)
    assert "alimiter" not in measure and "print_format=json" in measure


def test_video_graph_crossfades_every_join():
    tl = compositor.make_timeline([0.1, 4.0, 9.0, 13.0], 17.0, "mixed", ["bass_drop", "whoosh", "none", "none"])
    graph, out = compositor.video_graph(tl, captions=True, cards=False)
    assert graph.count("xfade=") == 3 and out == "vout"
    assert "transition=smoothup" in graph  # a whoosh gets a moving transition
    assert "subtitles=captions.ass" in graph and "cards.ass" not in graph
    assert tl.overlap == pytest.approx(0.3)
    plain = compositor.make_timeline([0.1, 4.0], 8.0, "fade")
    assert plain.transitions == ["fade"]


@pytest.mark.parametrize("encoder", ["libx264", "h264_nvenc", "h264_amf", "h264_qsv", "h264_videotoolbox"])
def test_finished_videos_cap_the_bitrate_on_every_encoder(encoder):
    from ytclip.ffmpeg import video_args

    args = video_args(encoder, 20, "delivery")
    assert args[args.index("-c:v") + 1] == encoder
    assert "10M" in " ".join(args)


# ---------------------------------------------------------------- pictures

def test_texture_pictures_are_bright_enough(tmp_path):
    path = visual_generator.texture(tmp_path / "t.png", director.get_style("cinematic").palette, 7, size=(270, 480))
    import cv2

    img = cv2.imread(str(path))
    assert img.shape[:2] == (480, 270)
    assert img.mean() > 25


def test_picture_order_falls_back_to_what_works():
    ready = {"comfyui": "not running", "sdxl_turbo": "torch isn't installed", "flux_schnell": "torch isn't installed",
             "local_broll": "no folder", "texture": ""}
    assert visual_generator.order_for("auto", ready) == ["local_broll", "texture"]  # B-roll only when it matches
    assert visual_generator.order_for("comfyui", ready | {"comfyui": ""})[0] == "comfyui"
    assert visual_generator.order_for("comfyui", ready)[-1] == "texture"


def test_comfy_workflow_placeholders_are_filled():
    text = json.dumps({"3": {"inputs": {"text": "{{prompt}}", "seed": "{{seed}}", "w": "{{width}}"}},
                       "4": {"inputs": {"text": "{{negative}}"}}})
    graph = visual_generator.fill_workflow(text, 'a "quoted" prompt', "blurry", 42, 768, 1344)
    assert graph["3"]["inputs"] == {"text": 'a "quoted" prompt', "seed": 42, "w": 768}
    assert graph["4"]["inputs"]["text"] == "blurry"


# ---------------------------------------------------------------- the whole run

def _settings(tmp_path, **kw):
    cats = [{"id": "psy", "name": "Psychology", "account_id": "acc1", "account": "@mindfacts"}]
    return Settings().apply({"auto_output_dir": str(tmp_path / "Automation"), "output_dir": str(tmp_path / "Out"),
                             "categories": cats, "creator_visuals": "texture", "creator_word_timing": "sound",
                             "creator_rewards_length": False, "creator_duration": 14, "creator_hook_card": True,
                             "hw_encoder": "off"} | kw)


SCRIPT = ("Your memory is lying to you.\n\n"
          "Every time you remember something, your brain rebuilds it from pieces.\n\n"
          "And every rebuild changes it a little, so the memories you are surest of are often the most edited.")


def test_make_a_video_end_to_end(tmp_path, tone_voice):
    s = _settings(tmp_path)
    steps_seen = []
    jobs = __import__("ytclip.jobs", fromlist=["JobDB"])
    db = jobs.JobDB(tmp_path / "jobs.db")
    maker = orchestrator.VideoCreatorOrchestrator(s, progress=lambda m, f: steps_seen.append(m), db=db)
    req = orchestrator.CreateRequest(prompt="memory", script=SCRIPT, category="Psychology", title="Memory lies")
    result = maker.create(req)
    assert result.video.is_file() and result.video.parent == result.folder
    assert result.folder.parent == tmp_path / "Automation" / "Psychology" / "Created"
    c = result.checks
    assert (c["width"], c["height"]) == (1080, 1920)
    assert abs(c["lufs"] - (-14)) <= 1.0 and c["true_peak"] <= -0.5
    assert result.duration > 8
    assert result.account == "@mindfacts"
    for name in ("clips.json", "README.md", "storyboard.json"):
        assert (result.folder / name).is_file()
    assert len(list((result.folder / "scenes").glob("*.png"))) == len(result.storyboard.scenes)
    index = json.loads((result.folder / "clips.json").read_text(encoding="utf-8"))
    assert index["automation"]["account_id"] == "acc1" and index["creator"]["ai_generated"] is True
    from ytclip.posting.clipsjson import read_folder_index

    info = read_folder_index(result.folder)[result.video.name]
    assert info["ai_generated"] is True and "#" in info["hashtags"]
    for step in ("Writing the script", "Recording the voice", "Making pictures", "Animating scene",
                 "Mixing the sound", "Putting the video together", "Checking the video"):
        assert any(m.startswith(step) for m in steps_seen), step
    job = db.get(f"create-{result.id}")
    assert job.status == "done" and job.is_creation and job.category_id == "psy"
    db.close()
    # The same script again: the originality check notices.
    warnings = []
    maker.checks(result.video, result.duration, False, result.storyboard, warnings)
    assert any("shares 100%" in w for w in warnings)


def test_cancelling_stops_between_steps(tmp_path, tone_voice):
    from ytclip.ffmpeg import Cancelled

    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 3

    maker = orchestrator.VideoCreatorOrchestrator(_settings(tmp_path), cancelled=cancelled, register=False)
    with pytest.raises(Cancelled):
        maker.create(orchestrator.CreateRequest(script=SCRIPT))
    assert not list((tmp_path / "Automation").rglob("*.mp4"))


def test_target_keeps_creator_rewards_videos_over_a_minute(tmp_path):
    maker = orchestrator.VideoCreatorOrchestrator(_settings(tmp_path))
    assert maker.target(orchestrator.CreateRequest(seconds=30, rewards=True)) == (64.0, True)
    assert maker.target(orchestrator.CreateRequest(seconds=120, rewards=True)) == (80.0, True)
    assert maker.target(orchestrator.CreateRequest(seconds=30, rewards=False)) == (30.0, False)


def test_no_prompt_and_no_script_is_a_plain_error(tmp_path):
    with pytest.raises(orchestrator.CreatorError, match="Say what"):
        orchestrator.VideoCreatorOrchestrator(_settings(tmp_path)).storyboard(orchestrator.CreateRequest())


def test_hook_card_only_when_it_adds_something():
    b = _board()
    b.hook = "Your brain is lying"
    assert not orchestrator.hook_adds(b)
    b.hook = "The truth about memory"
    assert orchestrator.hook_adds(b)


def test_output_folder_names(tmp_path):
    s = _settings(tmp_path)
    cat = orchestrator.find_category(s, "psychology")
    assert cat is not None and orchestrator.find_category(s, "psy") == cat
    f = orchestrator.output_folder(s, cat, "Psychology", "What: a <title>?", "c-1234abcd", True)
    assert f.parent == tmp_path / "Automation" / "Psychology" / "Created" and f.name.endswith("[c-1234abcd]")
    assert not set('<>:"?') & set(f.name)
    g = orchestrator.output_folder(s, None, "", "T", "c-1", False)
    assert g.parent == tmp_path / "Out" / "Created"


# ---------------------------------------------------------------- automation, CLI, settings, GUI

def test_queued_creations_are_made_by_the_runner(tmp_path, monkeypatch):
    from test_pipeline import _runner

    made = []

    def create(self, req, board=None):  # the real run is test_make_a_video_end_to_end
        made.append(req)
        self._progress("Recording the voice: scene 1 of 3", 0.3)
        folder = tmp_path / "Automation" / "Psychology" / "Created" / "x"
        folder.mkdir(parents=True)
        return orchestrator.CreationResult("c-1", folder, folder / "01.mp4", director.from_script(SCRIPT), 64.0,
                                           timings={"render": 12.0})

    monkeypatch.setattr(orchestrator.VideoCreatorOrchestrator, "create", create)
    s = _settings(tmp_path, auto_start_hour=0, auto_end_hour=24)
    r = _runner(tmp_path, s)
    req = orchestrator.CreateRequest(script=SCRIPT, title="Memory lies")
    job_id = r.add_creation(req, "psy")
    job = r.db.get(job_id)
    assert job.is_creation and job.kind == "creator" and job.category_id == "psy"
    assert orchestrator.CreateRequest.from_json(job.request).title == "Memory lies"
    assert r.db.next_due(r.now()).id == job_id
    r.process(job, s)
    done = r.db.get(job_id)
    assert done.status == "done" and done.clips == 1 and done.folder.endswith("x")
    assert made[0].category == "psy" and made[0].title == "Memory lies"
    assert json.loads(r.db.meta("creator_timings")) == {"render": 12.0}


def test_a_creation_that_cant_be_made_fails_once(tmp_path, monkeypatch):
    from test_pipeline import _runner

    s = _settings(tmp_path)
    r = _runner(tmp_path, s)
    monkeypatch.setattr(director, "script_writers", lambda s: [])
    job_id = r.add_creation(orchestrator.CreateRequest(prompt="memory"))
    r.process(r.db.get(job_id), s)
    job = r.db.get(job_id)
    assert job.status == "failed" and "local AI" in job.error


def test_old_job_databases_get_the_new_columns(tmp_path):
    import sqlite3

    from ytclip.jobs import JobDB, JobType

    path = tmp_path / "old.db"
    db = JobDB(path)
    db.close()
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)")]
    con.close()
    assert "kind" in cols and "request" in cols
    assert JobType.GENERATIVE_CREATOR.value == "creator"


def test_cli_check_and_schedule(tmp_path, monkeypatch, capsys):
    from ytclip import cli
    from ytclip.jobs import JobDB

    s = _settings(tmp_path)
    s.save()
    assert cli.main(["create", "--check"]) == 0
    out = capsys.readouterr().out
    assert "Voices" in out and "Pictures" in out and "Script writer" in out
    prompts = tmp_path / "prompts.txt"
    prompts.write_text("# ideas\nWhy cats purr\n\nThe deepest hole ever dug\n", encoding="utf-8")
    assert cli.main(["create", "--file", str(prompts), "--category", "Psychology", "--schedule"]) == 0
    db = JobDB()
    made = [j for j in db.jobs() if j.is_creation]
    db.close()
    assert sorted(j.title for j in made) == ["The deepest hole ever dug", "Why cats purr"]
    assert all(j.category_id == "psy" for j in made)
    assert cli.main(["create"]) == 2


def test_settings_choices_match_the_engine():
    assert {k for k, _ in settings_mod.CREATOR_VOICES} == set(voice_engine.VOICES)
    assert {k for k, _ in settings_mod.CREATOR_VISUALS} <= set(visual_generator.BACKENDS)
    assert {k for k, _ in settings_mod.CREATOR_STYLES} - {""} == set(director.STYLES)
    assert {k for k, _ in settings_mod.CREATOR_TTS} - {"auto"} == set(voice_engine.ENGINE_CLASSES) - {"tone",
                                                                                                        "broken"}
    fields = [f for f in settings_mod.SCHEMA if f.group == "Creator"]
    assert len(fields) > 10


def test_kinetic_captions_colour_emphasised_words():
    from ytclip.rendering import ass_builder

    line = ass_builder.kinetic_line(["the", "BIGGEST", "lie"], 0, True, Settings(), lambda c: c, lambda t: t,
                                    "&HFFFFFF&", "&H00FFFF&", 110, emphasis={"biggest"}, emphasis_colour="&H0000FF&")
    assert "&H0000FF&" in line


def test_creator_tab_shows_a_storyboard_and_edits_it(app, tmp_path):
    from ytclip.gui.creator_view import CreatorView

    s = {"v": _settings(tmp_path)}
    view = CreatorView(lambda: s["v"])
    assert view.category.itemData(0) == "psy"
    assert view.table.isHidden() and not view.empty.isHidden()
    board = director.from_script(SCRIPT)
    view.show_board(board)
    assert view.table.rowCount() == len(board.scenes)
    assert "scenes" in view.count.text()
    view.table.item(0, 1).setText("A brand new hook.")
    assert board.scenes[0].speech_text == "A brand new hook."
    view.table.item(1, 3).setText("pan left")
    assert board.scenes[1].camera_motion == "pan_left"
    view.title.setText("New title")
    view.title.textEdited.emit("New title")
    req = view.request()
    assert req.storyboard["title"] == "New title" and req.category == "psy"
    view.mode_script.setChecked(True)
    assert not view.script.isHidden()
    view.reload()  # settings changed elsewhere: nothing lost
    assert view.table.rowCount() == len(board.scenes)
    view.deleteLater()

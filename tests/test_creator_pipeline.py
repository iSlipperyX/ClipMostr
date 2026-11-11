"""The Video Creator's pipeline end to end at its seams, offline: the storyboard an AI hands back (held to
STORYBOARD_SCHEMA), cleaning it up, a stand-in for the Kokoro voice (no model, no onnxruntime), the narration
landing inside Creator Rewards' 62-85 s, how many scenes a length (or a learned pace) asks for, and the
intermediate files going once they're used (tests/test_creator.py covers the rest)."""

import numpy as np
import pytest

from ytclip import ffmpeg
from ytclip.creator import director, orchestrator, voice_engine
from ytclip.creator.director import Scene, Storyboard
from ytclip.intelligence import agent_router
from ytclip.settings import Settings


def scene(i, text, **kw):
    return {"scene_id": i, "speech_text": text, "visual_type": "generated_image", "visual_prompt": f"picture {i}",
            "camera_motion": "slow_push_in", "sfx_cue": "none", "sfx_offset_ms": 0, "emphasis": [], "keywords": []} | kw


def answer(n=10, words=14):
    scenes = [scene(1, "Nobody tells you this about your memory.", sfx_cue="bass_drop", emphasis=["memory"])]
    for i in range(2, n + 1):
        scenes.append(scene(i, " ".join(["Memory"] + [f"detail{i}x{k}" for k in range(words - 1)]) + ".",
                            camera_motion="pan_left", keywords=["brain", "light"]))
    return {"title": "Your memory is fake", "category": "Psychology", "target_duration": 65, "music_mood": "Mysterious",
            "hook": "Your memory is fake", "caption": "It rewrites itself", "hashtags": ["memory", "psychology"],
            "scenes": scenes}


# ---------------------------------------------------------------- the storyboard's shape


def test_a_whole_answer_has_the_storyboards_shape():
    assert agent_router.conforms(answer(), director.STORYBOARD_SCHEMA) == ""


@pytest.mark.parametrize("break_it, says", [
    (lambda a: a.pop("scenes"), "scenes"),
    (lambda a: a.update(music_mood="Jazzy"), "music_mood"),
    (lambda a: a.update(target_duration="sixty"), "target_duration"),
    (lambda a: a["scenes"][2].update(camera_motion="spin"), "camera_motion"),
    (lambda a: a["scenes"][1].pop("speech_text"), "speech_text"),
    (lambda a: a.update(hashtags="memory"), "hashtags"),
])
def test_what_breaks_the_shape_is_named(break_it, says):
    a = answer()
    break_it(a)
    why = agent_router.conforms(a, director.STORYBOARD_SCHEMA)
    assert why and says in why


def test_an_extra_key_is_let_through_and_dropped():
    a = answer()
    a["scenes"][1]["mood_board"] = "x"  # an older or chattier model: harmless, so not a reason to ask again
    assert agent_router.conforms(a, director.STORYBOARD_SCHEMA) == ""
    assert not hasattr(Storyboard.from_json(a).scenes[1], "mood_board")


def test_an_answer_becomes_a_storyboard_and_back():
    b = Storyboard.from_json(answer())
    assert len(b.scenes) == 10 and b.scenes[0].sfx_cue == "bass_drop" and b.scenes[1].keywords == ["brain", "light"]
    again = Storyboard.from_json(b.to_json())
    assert again == b


def test_a_storyboard_from_an_older_version_still_reads():
    old = {"title": "Old", "scenes": [{"speech_text": "First line here.", "transition": "fade"}, "junk",
                                      {"speech_text": "Second line here."}], "voice": "old key"}
    b = Storyboard.from_json(old)
    assert [s.scene_id for s in b.scenes] == [1, 3] and b.scenes[0].camera_motion == "slow_push_in"
    with pytest.raises(ValueError):
        Storyboard.from_json(["not", "a", "board"])


def test_a_cleaned_storyboard_is_still_in_the_schemas_terms():
    raw = answer()
    raw["scenes"][3]["camera_motion"] = "pan_left"  # the same move twice in a row
    raw["scenes"][4]["speech_text"] = "[music swells] Memory 🔥 is #weird, Narrator: really."
    b = director.normalise(Storyboard.from_json(raw))
    for sc in b.scenes:
        assert sc.camera_motion in director.MOTIONS and sc.sfx_cue in director.SFX_CUES
        assert sc.visual_type in director.VISUAL_TYPES and sc.visual_prompt and sc.keywords
        assert not any(ch in sc.speech_text for ch in "[]#🔥")
    assert all(a.camera_motion != c.camera_motion for a, c in zip(b.scenes, b.scenes[1:]))
    assert [s.scene_id for s in b.scenes] == list(range(1, len(b.scenes) + 1))
    assert b.music_mood in director.MOODS
    data = b.to_json()
    board_only = {k: data[k] for k in director.STORYBOARD_SCHEMA["properties"]}
    assert agent_router.conforms(board_only, director.STORYBOARD_SCHEMA) == ""


# ---------------------------------------------------------------- a stand-in Kokoro


class StubKokoro(voice_engine.KokoroEngine):
    """Kokoro as the Creator sees it (key "kokoro", so its speed rules apply), reading 0.4 s a word at the
    voice's own pace without any model."""

    spoken: list[tuple[str, float]] = []

    def missing(self):
        return ""

    def prepare(self, voice):
        pass

    def speak(self, text, voice, speed):
        StubKokoro.spoken.append((text, speed))
        rate = 24000
        word = 0.32 * voice.speed / speed
        t = np.arange(int(word * rate)) / rate
        tone = (0.4 * np.sin(2 * np.pi * 200 * t) * np.hanning(len(t))).astype(np.float32)
        gap = np.zeros(int(0.08 * voice.speed / speed * rate), np.float32)
        return np.concatenate([np.concatenate([tone, gap]) for _ in text.split()]), rate


@pytest.fixture
def kokoro(monkeypatch):
    StubKokoro.spoken = []
    monkeypatch.setitem(voice_engine.ENGINE_CLASSES, "kokoro", StubKokoro)
    monkeypatch.setattr(voice_engine, "pick_engines", lambda s: ["kokoro"])
    return StubKokoro


def board(n=12, words=14):
    return director.normalise(Storyboard.from_json(answer(n, words)))


def test_kokoro_reads_every_scene_into_one_track(tmp_path, kokoro):
    b = board(6)
    track = voice_engine.synthesize(b.scenes, Settings(), tmp_path / "v.wav")
    assert (track.engine, track.engine_key) == ("Kokoro-82M", "kokoro")
    assert [t for t, _ in kokoro.spoken] == [s.speech_text for s in b.scenes]
    assert [s.scene_id for s in track.scenes] == [s.scene_id for s in b.scenes]
    assert track.scenes[0].start == pytest.approx(voice_engine.LEAD_IN, abs=0.01)
    assert all(b2.start - a.end >= voice_engine.SCENE_GAP - 0.01 for a, b2 in zip(track.scenes, track.scenes[1:]))
    assert len(track.words) == b.word_count and track.duration > track.speech_seconds


def test_kokoros_speed_is_kept_to_what_still_sounds_natural(tmp_path, kokoro):
    b = board(2)
    for asked in (0.3, 3.0):
        kokoro.spoken = []
        voice_engine.synthesize(b.scenes, Settings(), tmp_path / "v.wav", speed=asked)
        assert all(voice_engine.MIN_SPEED <= sp <= voice_engine.MAX_SPEED for _, sp in kokoro.spoken)


def test_a_missing_kokoro_hands_over_to_the_next_voice(tmp_path, kokoro, monkeypatch):
    monkeypatch.setattr(StubKokoro, "missing", lambda self: "kokoro-onnx isn't installed (ImportError)")

    class Tone(StubKokoro):
        key = "tone"

        def missing(self):
            return ""

    monkeypatch.setitem(voice_engine.ENGINE_CLASSES, "tone", Tone)
    monkeypatch.setitem(voice_engine.ENGINE_NAMES, "tone", "Test tones")
    track = voice_engine.synthesize(board(2).scenes, Settings(), tmp_path / "v.wav", engines=["kokoro", "tone"])
    assert track.engine_key == "tone"
    with pytest.raises(voice_engine.VoiceError, match="Kokoro-82M: kokoro-onnx isn't installed"):
        voice_engine.synthesize(board(2).scenes, Settings(), tmp_path / "v.wav", engines=["kokoro"])


# ---------------------------------------------------------------- 62-85 seconds


def maker(tmp_path, **kw):
    s = Settings().apply({"output_dir": str(tmp_path / "Out"), "creator_word_timing": "sound",
                          "creator_rewards_length": True} | kw)
    return orchestrator.VideoCreatorOrchestrator(s, register=False)


@pytest.mark.parametrize("asked", [0, 15, 30, 61, 62, 65, 72, 85, 90, 600])
def test_a_rewards_video_always_aims_inside_62_to_85_seconds(tmp_path, asked):
    seconds, rewards = maker(tmp_path).target(orchestrator.CreateRequest(seconds=asked, rewards=True))
    lo, hi = director.REWARDS_RANGE
    assert rewards and lo + 2 <= seconds <= hi - 5
    if lo + 2 <= asked <= hi - 5:
        assert seconds == asked  # inside the range, what you asked for


@pytest.mark.parametrize("n, words", [(9, 14), (12, 14), (14, 18)])
def test_the_narration_lands_inside_the_rewards_range(tmp_path, kokoro, n, words):
    m = maker(tmp_path)
    req = orchestrator.CreateRequest(seconds=65, rewards=True)
    seconds, rewards = m.target(req)
    work = tmp_path / "work"
    work.mkdir()
    track = m.voice(board(n, words), req, work, seconds, rewards)
    lo, hi = director.REWARDS_RANGE
    assert orchestrator.REWARDS_MIN - orchestrator.HOLD_MAX <= track.duration <= hi
    if track.duration < orchestrator.REWARDS_MIN:  # the last scene's hold makes up the rest (render)
        assert orchestrator.REWARDS_MIN - track.duration <= orchestrator.HOLD_MAX
    assert 0.8 <= track.speed <= 1.2


def test_a_script_far_too_long_is_read_faster_but_no_faster_than_1_2(tmp_path, kokoro):
    m = maker(tmp_path)
    req = orchestrator.CreateRequest(seconds=65, rewards=True)
    work = tmp_path / "work"
    work.mkdir()
    track = m.voice(board(16, 26), req, work, *m.target(req))
    assert track.speed == pytest.approx(1.2)


# ---------------------------------------------------------------- scenes per length


@pytest.mark.parametrize("seconds", [62, 65, 72, 85])
def test_a_rewards_length_asks_for_a_picture_every_4_to_8_seconds(seconds):
    lo, hi = director.scene_range(seconds)
    assert director.MIN_SCENES <= lo < hi <= director.MAX_SCENES
    assert seconds / hi >= 4.0 - 0.6 and seconds / lo <= 8.0 + 0.6


@pytest.mark.parametrize("per", [3.0, 4.5, 6.0, 8.0])
def test_a_learned_pace_sets_the_scene_count(per):
    lo, hi = director.scene_range(65, per)
    assert director.MIN_SCENES <= lo < hi <= director.MAX_SCENES
    assert lo <= 65 / per <= hi or hi == director.MAX_SCENES or lo == director.MIN_SCENES


def test_a_faster_pace_means_more_scenes():
    assert director.scene_range(65, 3.0) > director.scene_range(65, 7.0)
    assert director.scene_range(65, 0.0) == director.scene_range(65)


def test_the_brief_carries_the_learned_pace_into_the_prompt(monkeypatch):
    monkeypatch.setattr(director, "script_writers", lambda s: ["local"])
    asked = []

    def ask(s, task, system, prompt, schema, **kw):
        asked.append(prompt)
        from ytclip import ai
        return ai.Answer(answer(12, 14), "test-model", "local")

    director.write(Settings(), director.Brief("memory", seconds=65, scene_seconds=3.5), ask=ask)
    lo, hi = director.scene_range(65, 3.5)
    assert f"Scenes: {lo}-{hi} scenes" in asked[0]


# ---------------------------------------------------------------- intermediates


def test_intermediates_go_once_used(tmp_path):
    keep = tmp_path / "final.mp4"
    keep.write_bytes(b"v")
    clips = [tmp_path / f"scene{i}.mp4" for i in range(3)]
    for c in clips:
        c.write_bytes(b"x" * 100)
    assert ffmpeg.discard(*clips, tmp_path / "mix.wav", None) == 3  # one never made, one not given
    assert not any(c.exists() for c in clips) and keep.exists()
    assert ffmpeg.discard(*clips) == 0  # twice is harmless


@pytest.mark.parametrize("assembly_fails", [False, True])
def test_the_render_drops_scene_clips_and_the_mix_either_way(tmp_path, kokoro, monkeypatch, assembly_fails):
    """orchestrator._render: once the video is put together (or that failed), the scene clips and the mix
    are deleted straight away, not when the work folder goes."""
    from types import SimpleNamespace

    from ytclip.creator import compositor, soundbed

    work = tmp_path / "work"
    work.mkdir()
    b = board(5)
    track = voice_engine.synthesize(b.scenes, Settings(), work / "voice.wav")
    made: list = []

    def render_scenes(jobs, **kw):
        for j in jobs:
            j.dest.write_bytes(b"scene")
            made.append(j.dest)
        return [j.dest for j in jobs], "libx264"

    def mix_audio(mi, dest, progress=None):
        dest.write_bytes(b"mix")
        return {}

    def assemble(clips, timeline, audio, dest, work_dir, s, **kw):
        assert all(c.exists() for c in clips) and audio.exists()  # still there while they're needed
        if assembly_fails:
            raise RuntimeError("the encoder fell over")
        dest.write_bytes(b"video")

    monkeypatch.setattr(compositor, "render_scenes", render_scenes)
    monkeypatch.setattr(compositor, "mix_audio", mix_audio)
    monkeypatch.setattr(compositor, "assemble", assemble)
    monkeypatch.setattr(soundbed, "music_for", lambda *a, **k: (None, "no music"))
    visuals = [SimpleNamespace(path=tmp_path / f"p{i}.png", kind="image") for i in range(len(b.scenes))]
    m = maker(tmp_path, creator_sfx=False, creator_hook_card=False)
    if assembly_fails:
        with pytest.raises(RuntimeError, match="fell over"):
            m._render(b, track, visuals, track.duration, work, director.get_style(""))
    else:
        out = m._render(b, track, visuals, track.duration, work, director.get_style(""))
        assert out.read_bytes() == b"video"
    assert len(made) == len(b.scenes) and not any(p.exists() for p in made)
    assert not (work / "mix.wav").exists() and (work / "voice.wav").exists()


def test_the_scene_object_is_what_the_voice_reads():
    sc = Scene(1, "Three words here.")
    assert sc.words == ["Three", "words", "here."]

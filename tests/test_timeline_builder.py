"""A storyboard, its narration and its pictures laid out as a timeline (studio/timeline_builder.py)."""

from pathlib import Path

import pytest

from ytclip.creator import director, sfx_library, voice_engine
from ytclip.creator.director import Scene, Storyboard
from ytclip.editor import audio_mixer, motion_engine
from ytclip.settings import Settings
from ytclip.studio import timeline_builder as B
from ytclip.studio import timeline_schema as T
from ytclip.studio.visual_sourcer import SourcedVisual
from ytclip.subtitles import Word

STYLE = director.get_style("cinematic")
VERTICAL, WIDE = T.Canvas.named("vertical"), T.Canvas.named("landscape")


def narration(texts, gap=0.35, per_word=0.36):
    """A voice track for `texts` without any sound: each word 0.3 s, scenes a breath apart."""
    t, scenes, words = 0.3, [], []
    for i, text in enumerate(texts, 1):
        a = t
        for w in text.split():
            words.append(Word(t, t + 0.3, w))
            t += per_word
        scenes.append(voice_engine.SceneVoice(i, text, a, t))
        t += gap
    return voice_engine.VoiceTrack(Path("voice.wav"), round(t + 0.6, 3), 24000, scenes, words, "Kokoro", "Deep male",
                                   1.0, "whisper", "kokoro")


def setup(n=5, beats=("hook", "setup", "twist", "payoff", "cta"), **scene_kw):
    texts = [f"Scene {i} says a handful of {'marked' if i == 2 else 'plain'} words here." for i in range(1, n + 1)]
    scenes = [Scene(i, text, "generated_image", f"picture {i}", beat=beats[i - 1] if i <= len(beats) else "build",
                    emphasis=["marked"] if i == 2 else [], **scene_kw) for i, text in enumerate(texts, 1)]
    board = Storyboard("A title", scenes=scenes, hook="The thing nobody says", music_mood="Epic", prompt="p",
                       caption="cap", hashtags=["a"])
    visuals = [SourcedVisual(i, "image", Path(f"scenes/scene_{i:02d}.png"), "texture", width=1350, height=2400)
               for i in range(1, n + 1)]
    return board, narration(texts), visuals


def build(board, track, visuals, s=None, canvas=VERTICAL, **kw):
    return B.build(board, track, visuals, s or Settings(), canvas=canvas, duration=track.duration, style=STYLE, **kw)


def test_every_scene_is_a_clip_from_just_before_its_words_to_the_next(tmp_path):
    board, track, visuals = setup()
    tl = build(board, track, visuals)
    T.validate(tl)
    clips = tl.track(T.VIDEO_BASE).clips
    assert [c.id for c in clips] == [f"scene_{i:02d}" for i in range(1, 6)]
    assert clips[0].start == 0 and abs(clips[-1].end - track.duration) < 0.002
    for a, b in zip(clips, clips[1:]):
        assert abs(a.end - b.start) < 0.002  # no gaps, no overlaps: the transition is centred on the join
    for clip, voice in zip(clips[1:], track.scenes[1:]):
        assert abs(clip.start - (voice.start - 0.25)) < 0.002  # the picture arrives a moment before its words
    assert clips[0].transition_in.cut and all(not c.transition_in.cut for c in clips[1:])
    assert {c.transition_in.type for c in clips[1:]} <= set(T.TRANSITIONS)
    assert all(len(c.keyframes) >= 2 for c in clips)  # stills move
    assert len(clips[2].keyframes) > 2  # the twist's punch-in on top of its camera move
    assert tl.assets[clips[0].asset].source == "built-in textures"
    assert len(tl.track(T.AUDIO_VOICE).clips) == 1 and tl.track(T.AUDIO_MUSIC).clips == []
    assert tl.look == ["vignette", "grain"] and tl.meta["made_by"] == "ClipMonster Studio"


def test_one_transition_everywhere_or_none_when_scenes_are_too_short(tmp_path):
    board, track, visuals = setup()
    tl = build(board, track, visuals, Settings().apply({"creator_transition": "fadeblack"}))
    assert {c.transition_in.type for c in tl.track(T.VIDEO_BASE).clips[1:]} == {"dip_black"}
    assert tl.track(T.VIDEO_BASE).clips[1].transition_in.duration == 0.3


def test_markers_come_from_the_scripts_beats_and_the_effects_follow_them():
    board, track, visuals = setup()
    board.scenes[3].chapter = "Part two"
    tl = build(board, track, visuals)
    kinds = [(m.kind, round(m.t, 2)) for m in tl.markers]
    assert kinds[0] == ("hook", 0.0) and tl.markers[0].label == "The thing nobody says"
    assert ("retention", round(track.scenes[2].start, 2)) in kinds
    assert ("payoff", round(track.scenes[3].start, 2)) in kinds and ("cta", round(track.scenes[4].start, 2)) in kinds
    assert [m.label for m in tl.markers if m.kind == "chapter"] == ["Part two"]
    sfx = {c.id.rsplit("_", 1)[0]: c for c in tl.track(T.AUDIO_SFX).clips}
    assert {"auto_bass_drop", "auto_impact", "auto_riser"} <= set(sfx)
    assert abs(sfx["auto_impact"].start - track.scenes[2].start) < 0.01
    riser = sfx["auto_riser"]
    assert abs(riser.start + riser.duration - track.scenes[3].start) < 0.25  # it runs up to the payoff
    assert B.chapters(tl) == [(tl.markers[[m.kind for m in tl.markers].index("chapter")].t, "Part two")]
    info = B.describe(tl)
    assert info["scenes"] == 5 and info["captions"] == "pop" and any(e.startswith("riser at") for e in info["effects"])


def test_a_long_videos_whooshes_are_seconds_apart_and_can_be_switched_off():
    board, track, visuals = setup(n=14, beats=("hook",))
    tl = build(board, track, visuals)
    whooshes = sorted(c.start for c in tl.track(T.AUDIO_SFX).clips if "whoosh" in c.id)
    assert len(whooshes) >= 2 and all(b - a >= sfx_library.AUTO_GAP - 0.5 for a, b in zip(whooshes, whooshes[1:]))
    quiet = build(board, track, visuals, Settings().apply({"creator_auto_whoosh": False}))
    assert not [c for c in quiet.track(T.AUDIO_SFX).clips if "whoosh" in c.id]
    silent = build(board, track, visuals, Settings().apply({"creator_sfx": False}))
    assert silent.track(T.AUDIO_SFX).clips == []
    every = audio_mixer.plan_sfx(silent)  # asked without a gap: one on each change of picture that's free
    assert sum(1 for p in every if p.cue == "whoosh") > len(whooshes)


def test_the_storyboards_own_cues_are_kept_and_nothing_lands_on_top_of_them():
    board, track, visuals = setup()
    board.scenes[0].sfx_cue = "sub_boom"
    board.scenes[2].sfx_cue = "glass_hit"
    tl = build(board, track, visuals)
    ids = [c.id for c in tl.track(T.AUDIO_SFX).clips]
    assert "cue_sub_boom_1" in ids and "cue_glass_hit_2" in ids
    assert not any(i.startswith("auto_bass_drop") for i in ids)  # the hook already has the script's boom
    assert not any(i.startswith("auto_impact") for i in ids)  # and the twist its glass
    starts = sorted(c.start for c in tl.track(T.AUDIO_SFX).clips)
    assert all(b - a >= 0.99 for a, b in zip(starts, starts[1:]))


def test_music_sits_where_settings_say_under_speech_and_comes_up_in_the_pauses():
    assert B.music_gain(-20) == -6.0  # -20 under speech would be -4 in the pauses: held to -6
    assert B.music_gain(-30) == -14.0 and B.music_gain(-12) == -6.0
    board, track, visuals = setup()
    tl = build(board, track, visuals, Settings().apply({"creator_music_db": -28}), music=Path("assets/bed.wav"),
               music_note="composed here (epic bed)")
    clip = tl.track(T.AUDIO_MUSIC).clips[0]
    assert (clip.start, clip.duration, clip.gain_db, clip.loop) == (0.0, tl.duration, -12.0, True)
    assert clip.fade_in > 0 and clip.fade_out > 0 and tl.audio.duck_db == -16.0
    assert tl.assets[clip.asset].source == "composed here (epic bed)" and tl.meta["music"].startswith("composed")


def test_captions_take_the_styles_colours_and_the_scripts_emphasis():
    board, track, visuals = setup()
    s = Settings().apply({"creator_caption_size": 72, "creator_uppercase": False, "studio_captions": "bounce"})
    tl = build(board, track, visuals, s)
    subs = tl.track(T.SUBTITLES)
    assert (subs.style.preset, subs.style.size, subs.style.uppercase) == ("bounce", 72, False)
    assert subs.style.highlight == STYLE.highlight and subs.style.emphasis == STYLE.emphasis
    marked = [w.text for ev in subs.events for w in ev.words if w.emphasis]
    assert marked == ["marked"]
    assert sum(len(ev.words) for ev in subs.events) == len(track.words)
    assert build(board, track, visuals, s, captions="karaoke").track(T.SUBTITLES).style.preset == "karaoke"
    assert build(board, track, visuals, s, captions="off").subtitle_tracks() == []
    with pytest.raises(ValueError, match="caption style"):
        build(board, track, visuals, s, captions="sparkly")


def test_the_hook_is_a_plain_title_near_the_top_for_two_seconds():
    board, track, visuals = setup()
    tl = build(board, track, visuals, hook_title=True)
    title = tl.track(B.TITLES)
    assert title is not None and title.style.preset == "plain" and not title.style.emoji
    assert " ".join(w.text for ev in title.events for w in ev.words) == "THE THING NOBODY SAYS"
    assert title.events[-1].end <= B.HOOK_SECONDS and title.style.position == B.HOOK_POSITION["vertical"]
    boxes = [w.box for ev in title.events for w in ev.words]
    caption_boxes = [w.box for ev in tl.track(T.SUBTITLES).events for w in ev.words if ev.start < 2.2]
    assert max(b[1] + b[3] for b in boxes) < min(b[1] for b in caption_boxes)  # above the captions, never over them
    wide = build(board, track, visuals, canvas=WIDE, hook_title=True)
    assert wide.track(B.TITLES).style.position == B.HOOK_POSITION["wide"]
    assert build(board, track, visuals).track(B.TITLES) is None
    board.hook = ""
    assert build(board, track, visuals, hook_title=True).track(B.TITLES) is None


def test_a_clip_plays_from_a_little_way_in_and_loops_only_when_far_too_short():
    board, track, visuals = setup(n=3, beats=("hook", "build", "cta"))
    seconds = [b - a for a, b in B.spans(track, track.duration, "fade", ["none"] * 3)[0]]
    visuals[0] = SourcedVisual(1, "video", Path("scenes/a.mp4"), "pexels", duration=20.0, width=1080, height=1920,
                               start=1.6, credit="Video by A on Pexels", url="https://www.pexels.com/video/1/")
    visuals[1] = SourcedVisual(2, "video", Path("b.mp4"), "local_broll", duration=seconds[1] * 0.5, width=1920,
                               height=1080)
    visuals[2] = SourcedVisual(3, "video", Path("c.mp4"), "pixabay", duration=seconds[2] * 0.95, width=1080,
                               height=1920)
    tl = build(board, track, visuals)
    a, b, c = tl.track(T.VIDEO_BASE).clips
    assert (a.source_in, a.loop, a.keyframes, a.audio) == (1.6, False, [], "")  # muted, still, from 1.6 s in
    assert b.loop and b.source_in == 0  # half the scene's length: it plays again
    assert not c.loop  # nearly long enough: its last frame holds
    asset = tl.assets[a.asset]
    assert (asset.kind, asset.source, asset.credit, asset.duration) == ("video", "Pexels", "Video by A on Pexels", 20.0)
    assert tl.meta["credits"] == ["Scene 1: Video by A on Pexels (https://www.pexels.com/video/1/)"]
    assert B.describe(tl)["clips"] == 3


def test_a_picture_of_another_shape_keeps_its_camera_move_on_the_picture():
    assert B.overflow_of(1350, 2400, VERTICAL) == (1.0, 1.0)
    ox, oy = B.overflow_of(3000, 2000, VERTICAL)  # a wide photo in an upright video: lots of room sideways
    assert ox > 2.5 and oy == 1.0
    assert B.overflow_of(0, 0, WIDE) == (1.0, 1.0)
    board, track, visuals = setup(n=3, beats=("hook", "build", "cta"), camera_motion="pan_left")
    visuals[1] = SourcedVisual(2, "image", Path("wide.jpg"), "pexels", width=3000, height=2000)
    tl = build(board, track, visuals)
    keys = tl.track(T.VIDEO_BASE).clips[1].keyframes
    assert keys[0].x != keys[-1].x  # it pans, using the room the wide photo has
    assert motion_engine.covers(keys, tl.track(T.VIDEO_BASE).clips[1].duration, (ox, oy))  # never past its edge


def test_the_three_lists_must_be_the_same_length():
    board, track, visuals = setup()
    with pytest.raises(ValueError, match="same number"):
        build(board, track, visuals[:-1])

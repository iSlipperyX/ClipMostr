"""A storyboard, its narration and its pictures laid out as a timeline (studio/timeline_schema.py).

    tl = build(board, track, visuals, settings, canvas=Canvas.named("vertical"), duration=track.duration,
               style=style, music=music_file)
    tl.dump(folder / "timeline.json")           # everything the render does is written here, and editable

What goes where:

* **Pictures** (track `video_track_0`): one clip a scene, from just before its first word to just before the
  next scene's. A still gets the scene's camera move as two keyframes (the Creator's moves, at Settings →
  Creator → Camera movement's strength), and a small punch-in on the scene the script marked as the twist.
  A stock or B-roll clip plays from a little way in, muted. Each change of picture is a transition
  (Settings → Creator → Scene changes), centred on the cut.
* **Voice** (`audio_track_voice`): the narration, whole.
* **Music** (`audio_track_music`): the Creator's choice for the storyboard's mood, under the whole video, faded in and
  out. Its level here is the level in the pauses; the timeline's master ducks it under speech.
* **Effects** (`audio_track_sfx`): the storyboard's own cues at their scenes, then the automatic ones the markers ask for
  (editor/audio_mixer.place_sfx): a bass drop on the hook, an impact on the twist, a riser into the payoff,
  a whoosh on a change of picture every few seconds.
* **Captions** (`subtitle_track`): the narration's words as kinetic captions, the storyboard's emphasis words in
  the style's second colour.
* **The hook** (`titles`): the hook in plain big letters near the top for the first two seconds, when it
  says more than the first words the voice does.
* **Markers**: the hook at 0, the twist ("retention"), the payoff, the call to action, and each chapter of a
  long video (the chapter list in the README is read from these).
"""

from __future__ import annotations

from pathlib import Path

from ..creator import compositor, sfx_library
from ..editor import audio_mixer, kinetic_subtitles, motion_engine
from . import visual_sourcer
from .timeline_schema import (AUDIO_MUSIC, AUDIO_SFX, AUDIO_VOICE, SUBTITLES, VIDEO_BASE, Asset, AudioClip,
                              AudioMaster, Canvas, Marker, SubtitleStyle, Timeline, Track, Transition, VideoClip,
                              standard)

TITLES = "titles"
HOOK_SECONDS = 2.2  # the hook's big letters stay this long
HOOK_POSITION = {"vertical": 0.27, "wide": 0.24}  # where their bottom sits, down the canvas
DUCK_DB = -16.0  # how far the music drops under speech
MUSIC_PAUSE_MAX_DB = -6.0  # the music's level in the pauses is never over this
TWIST_PUNCH = 1.06
LOOP_UNDER = 0.9  # a clip shorter than this share of its scene plays again (longer: its last frame holds)
CAPTION_PRESETS = ("pop", "karaoke", "bounce", "plain", "off")


def music_gain(under_speech_db: float, duck_db: float = DUCK_DB) -> float:
    """The music clip's level so that, ducked by `duck_db` while someone speaks, it sits where Settings →
    Creator → Music level says. In the pauses it comes up, though never past MUSIC_PAUSE_MAX_DB."""
    return round(min(MUSIC_PAUSE_MAX_DB, float(under_speech_db) - float(duck_db)), 1)


def overflow_of(width: int, height: int, canvas: Canvas) -> tuple[float, float]:
    """How far a picture reaches past the canvas each way once it covers it (1, 1 = the same shape)."""
    if width <= 0 or height <= 0:
        return 1.0, 1.0
    scale = max(canvas.width / width, canvas.height / height)
    return max(1.0, width * scale / canvas.width), max(1.0, height * scale / canvas.height)


def spans(track, duration: float, transition: str, cues: list[str]) -> tuple[list[tuple[float, float]], float, list[str]]:
    """(each scene's start and end on the timeline, the transitions' length, each join's transition)."""
    starts = [sc.start for sc in track.scenes]
    plan = compositor.make_timeline(starts, duration, transition, cues)
    edges = [0.0, *plan.bounds, round(duration, 3)]
    return list(zip(edges, edges[1:])), plan.overlap, [motion_engine.XFADE_TRANSITION.get(n, "crossfade")
                                                       for n in plan.transitions]


def caption_style(s, style, preset: str, canvas: Canvas) -> SubtitleStyle:
    return SubtitleStyle(preset=preset, font=s.creator_caption_font, size=int(s.creator_caption_size),
                         highlight=style.highlight, emphasis=style.emphasis, uppercase=bool(s.creator_uppercase))


def hook_track(text: str, s, canvas: Canvas, duration: float) -> Track | None:
    """The hook as a title: plain big letters near the top, for the first seconds."""
    words = " ".join(str(text or "").split())
    until = min(HOOK_SECONDS, duration - 0.1)
    if not words or until <= 0.5:
        return None
    style = SubtitleStyle(preset="plain", font=s.creator_caption_font,
                          size=int(s.creator_caption_size * (1.05 if len(words) <= 40 else 0.9)),
                          highlight="#FFFFFF", uppercase=bool(s.creator_uppercase), max_words=12,
                          position=HOOK_POSITION["vertical" if canvas.vertical else "wide"], keyword_colors=False,
                          emoji=False)
    return kinetic_subtitles.build_track(kinetic_subtitles.from_text(words, 0.12, until), canvas, style,
                                         duration=until, track_id=TITLES)


def markers_for(board, track, scene_spans: list[tuple[float, float]]) -> list[Marker]:
    out = [Marker(0.0, "hook", " ".join(str(board.hook or "the hook").split())[:80])]
    kinds = {"twist": "retention", "payoff": "payoff", "cta": "cta"}
    seen: set[str] = set()
    for sc, voice, (a, _b) in zip(board.scenes, track.scenes, scene_spans):
        if getattr(sc, "chapter", ""):
            out.append(Marker(round(a, 3), "chapter", str(sc.chapter)[:80]))
        kind = kinds.get(getattr(sc, "beat", ""))
        if kind and (kind == "retention" or kind not in seen):  # one payoff, one call to action; twists as written
            seen.add(kind)
            out.append(Marker(round(voice.start, 3), kind, " ".join(sc.speech_text.split()[:8])))
    return sorted(out, key=lambda m: (m.t, m.kind != "chapter"))


def build(board, track, visuals, s, *, canvas: Canvas, duration: float, style, music: Path | None = None,
          music_note: str = "", captions: str = "", hook_title: bool | None = None, meta: dict | None = None
          ) -> Timeline:
    """The timeline for `board` read by `track` over `visuals` (one a scene, in order), `duration` seconds
    long. `captions`: a preset (pop, karaoke, bounce, plain), "off", or "" for Settings → Creator."""
    if not (len(board.scenes) == len(track.scenes) == len(visuals)):
        raise ValueError(f"{len(board.scenes)} scenes, {len(track.scenes)} narrated, {len(visuals)} pictures: "
                         "they should be the same number.")
    duration = round(float(duration), 3)
    tl = standard(board.title, canvas, duration)
    cues = [sc.sfx_cue for sc in board.scenes]
    scene_spans, overlap, joins = spans(track, duration, s.creator_transition, cues)

    # Pictures
    base = tl.track(VIDEO_BASE)
    for i, (sc, vis, (a, b)) in enumerate(zip(board.scenes, visuals, scene_spans)):
        vis = visual_sourcer._wrap(vis)
        length = round(b - a, 3)
        asset = tl.add_asset(visual_sourcer.asset_of(vis), f"scene_{sc.scene_id:02d}")
        clip = VideoClip(f"scene_{sc.scene_id:02d}", asset, round(a, 3), length,
                         label=" ".join(sc.speech_text.split()[:6]))
        if vis.kind == "image":
            keys = motion_engine.ken_burns(sc.camera_motion, length, float(s.creator_motion),
                                           overflow_of(vis.width, vis.height, canvas), (canvas.width, canvas.height))
            if getattr(sc, "beat", "") == "twist" and length > 1.2:
                keys = motion_engine.combine(keys, motion_engine.punch_in(overlap / 2 + 0.05, length, TWIST_PUNCH))
            clip.keyframes = keys
        else:
            clip.source_in = round(max(0.0, vis.start), 3)
            have = vis.duration - clip.source_in if vis.duration else 0.0
            clip.loop = bool(vis.duration) and have < length * LOOP_UNDER
            if clip.loop:
                clip.source_in = 0.0
        if i and overlap > 0.05:
            clip.transition_in = Transition(joins[i - 1], round(overlap, 3))
        base.clips.append(clip)

    # Voice
    voice = tl.add_asset(Asset("audio", str(track.path), duration=round(float(track.duration), 3),
                               source=f"{track.voice} ({track.engine})".strip()), "voice")
    tl.track(AUDIO_VOICE).clips.append(AudioClip("voice", voice, 0.0, round(min(float(track.duration), duration), 3)))

    # Music
    tl.audio = AudioMaster(duck_db=DUCK_DB)
    if music is not None:
        bed = tl.add_asset(Asset("audio", str(music), source=music_note or "music"), "music")
        tl.track(AUDIO_MUSIC).clips.append(AudioClip(
            "music", bed, 0.0, duration, gain_db=music_gain(s.creator_music_db), fade_in=0.4,
            fade_out=round(min(2.0, duration / 4), 2), loop=True, label=board.music_mood))

    # Markers, then the effects they ask for
    tl.markers = markers_for(board, track, scene_spans)
    if s.creator_sfx:
        sfx = tl.track(AUDIO_SFX)
        events = sfx_library.plan(board.scenes, [sc.start for sc in track.scenes], [], folder=s.creator_sfx_folder,
                                  auto_whoosh=False, level_db=s.creator_sfx_db, seed=board.title, duration=duration)
        for n, e in enumerate(events, 1):
            asset = tl.add_asset(Asset("audio", str(e.path), duration=round(e.length, 3), source="the storyboard"),
                                 e.cue)
            sfx.clips.append(AudioClip(f"cue_{e.cue}_{n}", asset, round(e.time, 3),
                                       round(max(0.05, min(e.length or 1.0, duration - e.time)), 3),
                                       gain_db=e.gain_db, label="the storyboard's cue"))
        audio_mixer.place_sfx(tl, folder=s.creator_sfx_folder, level_db=s.creator_sfx_db, seed=board.title,
                              whoosh=bool(s.creator_auto_whoosh), whoosh_gap=sfx_library.AUTO_GAP)

    # Captions, and the hook's title
    preset = (captions or getattr(s, "studio_captions", "pop") or "pop").lower()
    if preset not in CAPTION_PRESETS:
        raise ValueError(f"There's no caption style called {captions!r}: use {', '.join(CAPTION_PRESETS)}.")
    others = [t for t in tl.tracks if t.kind != "subtitle"]
    subs: list[Track] = []
    if preset != "off":
        emphasis = {w for sc in board.scenes for w in sc.emphasis if w}
        subs.append(kinetic_subtitles.build_track(track.words, canvas, caption_style(s, style, preset, canvas),
                                                  duration=duration, emphasis=emphasis, track_id=SUBTITLES))
    if hook_title:
        title = hook_track(board.hook, s, canvas, duration)
        if title is not None:
            subs.append(title)
    tl.tracks = others + subs

    if s.creator_film_look:
        tl.look = ["vignette", "grain"]
    tl.meta = {"made_by": "ClipMonster Studio", "prompt": board.prompt, "written_by": board.written_by,
               "category": board.category, "music": music_note, "music_mood": board.music_mood,
               "sources": list(board.sources or []), "credits": visual_sourcer.credits(visuals),
               "caption": board.caption, "hashtags": list(board.hashtags or []), **(meta or {})}
    return tl


def describe(tl: Timeline) -> dict:
    """A few numbers about a timeline, for the README and clips.json."""
    base = tl.track(VIDEO_BASE)
    clips = base.clips if base else []
    kinds: dict[str, int] = {}
    for c in clips:
        if not c.transition_in.cut:
            kinds[c.transition_in.type] = kinds.get(c.transition_in.type, 0) + 1
    return {"canvas": f"{tl.canvas.width}x{tl.canvas.height}@{tl.canvas.fps}", "duration": tl.duration,
            "scenes": len(clips), "transitions": kinds,
            "clips": sum(1 for c in clips if tl.assets[c.asset].kind == "video"),
            "effects": [f"{c.id.rsplit('_', 1)[0].removeprefix('auto_').removeprefix('cue_')} at {c.start:.1f} s"
                        for t in tl.audio_tracks("sfx") for c in t.clips],
            "captions": next((t.style.preset for t in tl.subtitle_tracks() if t.id == SUBTITLES and t.style), "off"),
            "markers": [{"t": m.t, "kind": m.kind, "label": m.label} for m in tl.markers]}


def chapters(tl: Timeline) -> list[tuple[float, str]]:
    """(seconds, title) of each chapter marker."""
    return [(m.t, m.label) for m in tl.markers if m.kind == "chapter"]

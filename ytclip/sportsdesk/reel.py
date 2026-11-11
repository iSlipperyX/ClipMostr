"""A game's recap reel, made: one 1080x1920 video of its best plays (montage.py plans it) with the narrator, the
graphics and the mixed sound, in a folder posting picks up like any clip.

    make(p, game, highlights, s, out_root, ...) → ReelResult

`p` is the game's engine.Project after open() and measure() (the video is downloaded and its sound measured;
both are cached, so a game whose highlights were just found costs nothing more). The steps:

1. **Plan** the cut (montage.plan) and **write the words** (montage.story: the AI, or the plays' own titles).
2. **Read the score** at each play off the broadcast's scoreboard (scoreboard.py; Claude or Gemini).
3. **Record the narrator** (the Video Creator's voices: Kokoro, Piper, Windows' or eSpeak), when one is on.
4. **Frame each stretch** 9:16: following the action (framing.py, through vision/reframe.py's sub-pixel
   crop), or the whole picture over a blurred fill of itself.
5. **Mix the sound** (sound.py) and draw the graphics (overlays.py).
6. **Put it together**: the stretches joined by quick transitions (a white flash out of the opening, then
   whips and slides), the graphics burnt in, encoded on the graphics card when there's an encoder for it.
7. **Check** it (1080x1920, 65-85 s for Creator Rewards, sound at -14 LUFS) and write clips.json with the
   caption and hashtags posting uses.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from .. import sports
from ..ffmpeg import discard, ffmpeg_exe, pick_encoder, probe, run, run_with_progress, video_args
from ..rendering import filtergraph
from . import framing, montage, overlays, scoreboard, sound

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]

FPS = 30
W, H = 1080, 1920
COMMENTARY_CHARS = 320
OPENING_FLASH = "fadewhite"
TRANSITIONS = ("wipeleft", "slideleft", "smoothleft", "wiperight", "slideup", "zoomin")
# How one play hands over to the next (Settings → Sports → Reel transitions), as ffmpeg xfade names
STYLES = {
    "mix": TRANSITIONS,  # a different quick wipe, slide or zoom each time
    "dissolve": ("fade",),  # a cross-dissolve
    "whip": ("smoothleft", "smoothright", "slideleft", "slideright"),  # whip-pans, alternating sides
    "zoom": ("zoomin",),  # a push in to the next play
}
PERMISSION = "Picked by you in Sports: a recap reel of the game's best plays"


@dataclass
class ReelResult:
    folder: Path
    file: Path
    reel: montage.Reel
    checks: dict = field(default_factory=dict)
    info: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class Stop(Exception):
    pass


# ---------------------------------------------------------------- the picture of one stretch


def quantize(reel: montage.Reel) -> None:
    """Every stretch a whole number of frames long, so the picture and the sound join at the same instants."""
    for seg in reel.segments:
        frames = max(FPS, round(seg.length * FPS))
        seg.end = round(seg.start + frames / FPS, 4)


def render_follow(src: Path, media, seg: montage.Segment, dest: Path, progress=None) -> dict:
    from ..rendering.plan import decode_size
    from ..vision import reframe
    from ..vision.camera import window

    sw, sh = decode_size(media)
    path = framing.follow(src, seg.start, seg.length, sw, sh)

    def windows(_n: int, t_src: float):
        return window(path.at(t_src - seg.start), 0.5, 1.0, sw, sh, 9 / 16, eye_line=0.5)

    job = reframe.Job(src=src, start=seg.start, length=seg.length, fps=FPS, out_w=W, out_h=H, windows=windows,
                      decode_size=(sw, sh))
    reframe.render(job, dest, progress)
    return {"framing": "follow", "action_found": round(path.found, 2)}


def render_fit(src: Path, seg: montage.Segment, dest: Path, encoder: str) -> dict:
    vf = (f"fps={FPS},split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"boxblur=30:2,eq=brightness=-0.10:saturation=1.1[bg];[b]scale={W}:-2[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p")
    run(_ff() + ["-y", "-v", "error", "-ss", f"{seg.start:.3f}", "-i", str(src), "-t", f"{seg.length:.3f}", "-an",
                 "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", *video_args(encoder, purpose="intermediate"), "-pix_fmt", "yuv420p",
                 str(dest)])
    return {"framing": "fit"}


def _ff() -> list[str]:
    return [ffmpeg_exe()]


# ---------------------------------------------------------------- putting it together


def video_graph(reel: montage.Reel, ass: bool, fontsdir: str) -> str:
    """The plays joined (a white flash out of the opening, then the transition style's wipes and slides), with
    the graphics burnt in (rendering/filtergraph.py builds it)."""
    names: list[str] = []
    style = STYLES.get(getattr(reel, "style", "mix"), TRANSITIONS)
    k = 0
    for i in range(1, len(reel.segments)):
        if reel.segments[i - 1].kind == "teaser":
            names.append(OPENING_FLASH)
        else:
            names.append(style[k % len(style)])
            k += 1
    starts = reel.starts()
    graph, _ = filtergraph.video_graph(len(reel.segments), names, reel.transition, starts[1:], fps=FPS,
                                       finish=filtergraph.burn_in(["reel.ass"], fontsdir) if ass else [])
    return graph


def assemble(clips: list[Path], reel: montage.Reel, audio: Path, dest: Path, work: Path, s, *, ass: bool,
             fontsdir: str, progress: Callable[[float], None] | None = None) -> None:
    cmd = _ff() + ["-y", "-v", "error"]
    for c in clips:
        cmd += ["-i", str(Path(c).resolve())]
    cmd += ["-i", str(Path(audio).resolve()), "-filter_complex", video_graph(reel, ass, fontsdir),
            "-map", "[vout]", "-map", f"{len(clips)}:a"]
    encoder = pick_encoder(getattr(s, "encoder", "auto") or "auto")
    cmd += video_args(encoder, min(int(getattr(s, "video_quality", 20) or 20), 21), "delivery", FPS)
    cmd += ["-pix_fmt", "yuv420p", "-r", str(FPS)]
    cmd += ["-c:a", "copy", "-t", f"{reel.duration:.3f}", "-movflags", "+faststart", str(Path(dest).resolve())]
    run_with_progress(cmd, reel.duration, progress, cwd=work)


def checks(dest: Path) -> dict:
    from ..creator.compositor import ebur128

    info = probe(dest)
    out = {"width": info.width, "height": info.height, "length": round(info.duration, 2),
           "bytes": dest.stat().st_size, "sound": info.has_audio}
    out |= {k: (round(v, 1) if isinstance(v, float) else v) for k, v in ebur128(dest).items()}
    out["rewards"] = info.duration >= montage.REEL_MIN - 0.5
    return out


# ---------------------------------------------------------------- the whole reel


def commentary_for(reel: montage.Reel, cues) -> dict[str, str]:
    from ..subtitles import text_between

    out = {}
    for seg in reel.plays:
        text = " ".join(text_between(list(cues), seg.start - 2, seg.end).split())
        if text:
            out[seg.hid] = text[:COMMENTARY_CHARS]
    return out


def make(p, game: dict, highlights: list[dict], s, out_root: Path, *, ids: list[str] | None = None,
         category=None, voice: str = "en_storyteller", framing_mode: str = "follow", order: str = "countdown",
         log: Log | None = None, progress: Progress | None = None, check: Callable[[], None] | None = None,
         work: Path | None = None, events: list[dict] | None = None, events_title: str = "",
         timeline: list[dict] | None = None) -> ReelResult:
    """Make the reel of `game` from `p` (opened and measured). Raises montage.ReelError for a game that can't
    make one, and whatever `check` raises to stop.

    `events` (an event search's hits, semantic_search.EventHit as dicts) makes a reel of those moments instead
    of the game's best plays. `timeline` is the game's placed play-by-play (desk: game["timeline"]); with it a
    play's lower-third carries the feed's own minute and scorer."""
    from ..organize import safe_name

    log = log or (lambda m: None)
    progress = progress or (lambda m, f: None)
    check = check or (lambda: None)
    sport = sports.get(game.get("sport", "")) or sports.SPORTS["any"]
    src = Path(p.video.path)
    media = p.media or probe(src)
    m = p.measurements
    crowd = None
    if m is not None:
        try:
            crowd = sports.build_signals(m, sport, []).get("crowd")
        except Exception:  # noqa: BLE001 - no crowd signal: plays are cut around their middle
            crowd = None
    t0 = time.time()
    timings: dict[str, float] = {}

    progress("Planning the reel", None)
    style = str(getattr(s, "reel_transitions", "mix") or "mix")
    seconds = float(getattr(s, "reel_transition_seconds", montage.TRANSITION) or montage.TRANSITION)
    narrator = bool(voice) and voice != "off"
    if events:
        reel = montage.plan_events(game, events, duration=media.duration or (p.video.duration or 0),
                                   scene_cuts=m.scene_cuts if m else [], order=order, title=events_title,
                                   transition=seconds, style=style)
        quantize(reel)
        reel.story = montage.event_story(game, reel, narrator)
    else:
        reel = montage.plan(game, highlights, crowd=crowd, cues=p.cues, scene_cuts=m.scene_cuts if m else [],
                            duration=media.duration or (p.video.duration or 0), ids=ids, order=order,
                            transition=seconds, style=style)
        quantize(reel)
        commentary = commentary_for(reel, p.cues)
        progress("Writing the opening and the badges", None)
        reel.story = montage.story(game, reel, s, commentary, narrator=narrator, log=log, check=check)
        if timeline:  # the feed's own minute and scorer beat a badge written from the commentary
            reel.story.badges.update(montage.feed_badges(reel, timeline))
    if reel.story.note:
        log(reel.story.note)
    check()

    work = work or Path(s.cache_path) / "reels" / f"{game.get('id') or p.video.id}-{int(t0)}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        warnings: list[str] = list(reel.notes)
        info: dict = {"story_by": reel.story.written_by}

        # The score at the start and the end of each play.
        scores: dict[str, tuple] = {}
        if scoreboard.can_read(s):
            progress("Reading the scoreboard", None)
            times = []
            for seg in reel.plays:
                times += [seg.start + 0.3, min(seg.end - 0.3, seg.moment + 3.0)]
            read, by = scoreboard.read(src, times, s, log=log, check=check)
            for k, seg in enumerate(reel.plays):
                scores[seg.hid] = (read[2 * k], read[2 * k + 1])
            info["scoreboard"] = {"by": by, "read": sum(1 for x in read if x)}
        check()
        t1 = time.time()
        timings["words"] = round(t1 - t0, 1)

        # The narrator.
        nar_path = None
        if narrator and reel.story.opening:
            from ..creator import voice_engine

            progress("Recording the narrator", None)
            try:
                track = voice_engine.synthesize([SimpleNamespace(scene_id=1, speech_text=reel.story.opening)], s,
                                                work / "narrator.wav", voice=voice, log=log, check=check)
                nar_path = track.path
                info["narrator"] = {"voice": track.voice, "engine": track.engine, "seconds": round(track.duration, 2),
                                    "text": reel.story.opening}
            except voice_engine.VoiceError as exc:
                warnings.append(f"The narrator couldn't be recorded ({exc}), so the reel has none.")
                log(warnings[-1])
        check()
        t2 = time.time()
        timings["narrator"] = round(t2 - t1, 1)

        # Each stretch's picture.
        encoder = pick_encoder(getattr(s, "encoder", "auto") or "auto")
        clips: list[Path] = []
        framed: list[dict] = []
        n = len(reel.segments)
        for i, seg in enumerate(reel.segments):
            check()
            dest = work / f"seg{i:02d}.mp4"
            what = "the opening" if seg.kind == "teaser" else f"play {i if reel.segments[0].kind == 'teaser' else i + 1} of {len(reel.plays)}"

            def step(f: float, i=i, what=what) -> None:
                check()
                progress(f"Framing {what}", (i + f) / n * 0.6)

            step(0.0)
            if framing_mode == "fit":
                try:
                    framed.append(render_fit(src, seg, dest, encoder))
                except RuntimeError as exc:
                    if encoder == "libx264":
                        raise
                    log(f"The graphics card's encoder ({encoder}) couldn't take a play ({str(exc)[-120:]}); "
                        "the processor encodes the rest.")
                    encoder = "libx264"
                    framed.append(render_fit(src, seg, dest, encoder))
            else:
                framed.append(render_follow(src, media, seg, dest, step))
            got = probe(dest).duration
            if got and abs(got - seg.length) > 0.5 / FPS:
                seg.end = round(seg.start + got, 4)  # what was really written: the joins follow it
            clips.append(dest)
        info["framing"] = framed
        t3 = time.time()
        timings["picture"] = round(t3 - t2, 1)

        # The sound and the graphics.
        from ..creator import sfx_library, soundbed

        folder_fx = getattr(s, "creator_sfx_folder", "") or ""
        snd = sound.Sound(src=src, has_audio=media.has_audio, narrator=nar_path)
        snd.effects = sound.effects_for(reel, sfx_library.sound_for("whoosh", folder_fx, game.get("id", "")),
                                        sfx_library.sound_for("impact", folder_fx, game.get("id", "")))
        music, where = soundbed.music_for(s, "Epic", reel.duration + 2, work, seed=game.get("id", ""))
        snd.music = music
        info["music"] = where
        progress("Mixing the sound", 0.62)
        info["mix"] = sound.mix(reel, snd, work / "sound.m4a",
                                lambda msg, f: progress(msg, 0.62 + 0.08 * (f or 0)),
                                bitrate=max(160, int(getattr(s, "audio_bitrate", 192) or 192)))
        check()
        font = getattr(s, "banner_font", "") or "TikTok Sans Black"
        accent = getattr(s, "caption_highlight", "") or "#FFD400"
        teams = scoreboard.teams_for(game)
        for seg in reel.plays:
            seg.badge = reel.story.badges.get(seg.hid, "")
        ass = overlays.build(reel, scores, teams=teams, matchup=game.get("matchup") or "", font=font, accent=accent)
        (work / "reel.ass").write_text(ass, encoding="utf-8")
        from ..fonts import stage

        fontsdir = ":fontsdir=fonts" if stage([font], work) else ""
        t4 = time.time()
        timings["sound"] = round(t4 - t3, 1)

        # Together.
        stamp = time.strftime("%Y-%m-%d %H.%M")
        matchup = game.get("matchup") or game.get("title") or "Game"
        folder = Path(out_root) / safe_name(f"{stamp} {matchup} reel", 80)
        folder.mkdir(parents=True, exist_ok=True)
        out = work / "reel.mp4"
        progress("Putting the reel together", 0.72)
        try:
            assemble(clips, reel, work / "sound.m4a", out, work, s, ass=True, fontsdir=fontsdir,
                     progress=lambda f: (check(), progress("Putting the reel together", 0.72 + 0.26 * f)))
        finally:  # the framed plays, the mix and the narrator are in the reel now: free their space at once
            discard(*clips, work / "sound.m4a", nar_path)
        timings["assemble"] = round(time.time() - t4, 1)
        result_checks = checks(out)
        if not result_checks.get("rewards"):
            warnings.append(f"The reel is {result_checks.get('length', 0):.0f} s: under a minute, so not for Creator "
                            "Rewards.")
        if result_checks.get("lufs") is not None and abs(result_checks["lufs"] + 14) > 1.0:
            warnings.append(f"Its sound came out at {result_checks['lufs']} LUFS, not -14.")
        info["timings"] = timings
        return _deliver(p, game, reel, out, folder, s, category, sport, result_checks, info, warnings, bool(nar_path))
    finally:  # stopped, failed or done: the work folder (framed plays, sound, graphics) goes either way
        shutil.rmtree(work, ignore_errors=True)


def _deliver(p, game: dict, reel: montage.Reel, video: Path, folder: Path, s, category, sport, result_checks: dict,
             info: dict, warnings: list[str], narrated: bool) -> ReelResult:
    from ..analyze import Clip
    from ..download import Video
    from ..export import thumbnail
    from ..organize import clip_filename, write_index

    st = reel.story
    title = (f"{game.get('matchup') or game.get('title')}: {st.hook.lower()}" if reel.kind == "events" else
             f"{game.get('matchup') or game.get('title')}: top {len(reel.plays)} plays")
    clip = Clip(0.0, round(result_checks.get("length") or reel.duration, 2), 100.0, title, origin="manual",
                reason=st.caption, hook=st.hook.title() if st.hook else "")
    name = clip_filename(1, clip, folder, "{rank:02d} - {label}")
    (folder / "thumbnails").mkdir(exist_ok=True)
    last = reel.starts()[-1] + (reel.segments[-1].moment - reel.segments[-1].start)
    try:
        thumbnail(video, folder / "thumbnails" / "01.jpg", min(last, reel.duration - 0.5))
    except Exception:  # noqa: BLE001 - a missing thumbnail never stops a reel
        pass
    tags = " ".join(f"#{t}" for t in st.hashtags)
    auto = {"permission": PERMISSION, "origins": ["sports"], "how": "manual", "recipe": "Sports recap reel",
            "picked_at": time.time()}
    if category is not None:
        auto |= {"category": category.name, "category_id": category.id, "account": category.account,
                 "account_id": category.account_id}
    v = p.video
    video_meta = Video(id=v.id, title=v.title, channel=v.channel, upload_date=v.upload_date, duration=v.duration,
                       url=v.url, path=folder / name, is_local=v.is_local, license=v.license)
    details = [{"qc": result_checks, "caption": f"{st.caption} {tags}".strip(), "hashtags": st.hashtags,
                # A synthetic narrator's voice is AI-generated content, which TikTok asks to be labelled.
                "ai_generated": narrated, "reel": True}]
    extra = {"automation": auto,
             "sports": {"matchup": game.get("matchup"), "league": game.get("league"), "sport": sport.id,
                        "reel": reel.to_json(), "made": info}}
    write_index(folder, video_meta, [clip], [name], ["Sports recap reel"], "sports", extra=extra, details=details)
    (folder / "reel.json").write_text(json.dumps(reel.to_json(), indent=2, default=str), encoding="utf-8")
    part = folder / (name + ".part")
    shutil.move(str(video), str(part))
    os.replace(part, folder / name)
    return ReelResult(folder, folder / name, reel, result_checks, info, warnings)

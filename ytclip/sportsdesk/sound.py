"""The reel's sound: the game's own audio cut with the picture, the crowd lifted at each play's moment, the
narrator over the opening, a music bed under it all, a whoosh on every change of play, at -14 LUFS.

**Commentary and crowd.** A broadcast mixes the commentators into the middle of the stereo picture and the crowd
across it, so the two are pulled apart by mid/side: the middle (L+R) carries the commentary, the sides (L−R)
the crowd's width. The sides are lifted around each play's moment (the roar swells over the dunk) and the
middle is left alone, so the commentator stays clear. That's a real separation on a stereo broadcast; on a mono
one the sides are empty and the lift does nothing, and the game's sound plays as it was. (A neural stem
splitter such as Demucs would separate mono too, but it's a large download for a few dB of crowd.)

**Ducking.** The music ducks under the commentary (keyed from the middle's voice band) and the narrator, by
about 16 dB (audio/ducking.py's compressor), and swells back in the gaps. The game's sound ducks about 10 dB
under the narrator, so their words never fight.

**Level.** The game's sound and the narrator are first brought to a set level each (-18 and -16 LUFS, so the
narrator sits just over the game), then the finished mix goes through audio/mastering.py's two-pass loudnorm to
-14 LUFS with true peaks under -1 dBTP, TikTok's own playback level.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..audio import ducking, mastering
from ..ffmpeg import NO_WINDOW, ffmpeg_exe, run
from .montage import Reel

RATE = 48000
GAME_LUFS = -18.0
NARRATOR_LUFS = -16.0
MUSIC_DB = -15.0  # under the game, before ducking
MUSIC_STEPS = {"low": -6.0, "normal": 0.0, "loud": 5.0}  # the editor's "Music": dB on MUSIC_DB
CROWD_LIFT_DB = 5.0  # the sides, around each play's moment
CROWD_BASE_DB = 1.0  # the sides, the rest of the time
LIFT_BEFORE, LIFT_AFTER = 0.6, 3.6  # seconds around the moment the crowd is lifted
GAME_DUCK = "sidechaincompress=threshold=0.025:ratio=6:attack=25:release=450:makeup=1:knee=3"
NARRATOR_AT = 0.25  # seconds into the reel the narrator starts
LIMIT = 0.79  # -2 dBFS, limited at 4x the rate (so between-sample peaks count): AAC adds up to a dB on top
SFX_DB = {"whoosh": -10.0, "impact": -8.0}


@dataclass
class Sound:
    src: Path  # the game's video (its audio)
    has_audio: bool = True
    narrator: Path | None = None
    narrator_gain_db: float = 0.0
    game_gain_db: float = 0.0
    music: Path | None = None
    music_db: float = MUSIC_DB
    effects: list[tuple[float, Path, float]] = field(default_factory=list)  # (reel seconds, file, gain dB)


def lifts(reel: Reel) -> list[tuple[float, float]]:
    """Reel seconds where the crowd is lifted: around each play's moment."""
    out = []
    for at, seg in zip(reel.starts(), reel.segments):
        if seg.kind != "play":
            continue
        m = at + (seg.moment - seg.start)
        out.append((max(at, m - LIFT_BEFORE), min(at + seg.length, m + LIFT_AFTER)))
    return out


def effects_for(reel: Reel, whoosh: Path, impact: Path | None) -> list[tuple[float, Path, float]]:
    """A whoosh just before every change of play; an impact as the opening hands over to the first play."""
    out = []
    for i, at in enumerate(reel.starts()):
        if i == 0:
            continue
        if reel.segments[i - 1].kind == "teaser" and impact is not None:
            out.append((max(0.0, at - 0.02), impact, SFX_DB["impact"]))
        out.append((max(0.0, at - 0.18), whoosh, SFX_DB["whoosh"]))
    return out


def game_inputs(reel: Reel, snd: "Sound") -> list[str]:
    """One input per stretch, each read from its own place in the video (one input cut several ways would hold
    the whole game's sound in memory while the cuts out of order wait for each other)."""
    if not snd.has_audio:
        return []
    args: list[str] = []
    for seg in reel.segments:
        args += ["-ss", f"{seg.start:.3f}", "-t", f"{seg.length:.3f}", "-i", str(snd.src)]
    return args


def game_graph(reel: Reel, has_audio: bool) -> tuple[list[str], str]:
    """The game's own sound along the reel: each stretch's audio (inputs 0..n-1), crossfaded like the picture."""
    parts = []
    d = reel.duration
    if not has_audio:
        return [f"anullsrc=r={RATE}:cl=stereo,atrim=0:{d:.3f}[game]"], "game"
    for k, seg in enumerate(reel.segments):
        parts.append(f"[{k}:a]aresample={RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"apad=whole_dur={seg.length:.3f},atrim=0:{seg.length:.3f},asetpts=PTS-STARTPTS[g{k}]")
    last = "g0"
    for k in range(1, len(reel.segments)):
        parts.append(f"[{last}][g{k}]acrossfade=d={reel.transition:.3f}:c1=tri:c2=tri[gx{k}]")
        last = f"gx{k}"
    parts.append(f"[{last}]apad=whole_dur={d:.3f},atrim=0:{d:.3f}[game]")
    return parts, "game"


def _between(spans: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spans) or "0"


def mix_graph(reel: Reel, snd: Sound, measured: dict | None = None, *, stage: str = "mix", trim_db: float = 0.0,
              limit: float = LIMIT) -> tuple[list[str], str]:
    """(ffmpeg inputs, filter graph ending in [aout]). `stage` "game" stops after the game's sound (to measure
    it), "measure" ends in loudnorm's measuring pass, "mix" is the finished mix."""
    d = reel.duration
    args = game_inputs(reel, snd)
    parts, _ = game_graph(reel, snd.has_audio)
    if stage == "game":
        parts.append(f"[game]{mastering.loudnorm(None)}[aout]")
        return args, ";".join(parts)
    lift = 10 ** (CROWD_LIFT_DB / 20)
    base = 10 ** (CROWD_BASE_DB / 20)
    parts.append(f"[game]volume={snd.game_gain_db:.2f}dB,asplit=2[gl][gr]")
    parts.append("[gl]pan=mono|c0=0.5*c0+0.5*c1,asplit=2[mid][midk]")
    parts.append(f"[gr]pan=mono|c0=0.5*c0-0.5*c1,volume='if({_between(lifts(reel))},{lift:.4f},{base:.4f})'"
                 f":eval=frame[side]")
    parts.append("[mid][side]amerge=inputs=2,pan=stereo|c0=c0+c1|c1=c0-c1[game2]")
    # The commentary's voice band, as the music's ducking key.
    parts.append("[midk]highpass=f=250,lowpass=f=3500,aformat=channel_layouts=stereo[ckey]")
    layers = []
    n = len(reel.segments) if snd.has_audio else 0
    keys = ["ckey"]
    if snd.narrator is not None:
        args += ["-i", str(snd.narrator)]
        ms = int(NARRATOR_AT * 1000)
        parts.append(f"[{n}:a]aresample={RATE},aformat=channel_layouts=mono,{mastering.HIGHPASS},"
                     f"{mastering.COMPRESSOR},volume={snd.narrator_gain_db:.2f}dB,adelay={ms},"
                     f"aformat=channel_layouts=stereo,apad=whole_dur={d:.3f},atrim=0:{d:.3f},asplit=3[nar][nk1][nk2]")
        parts.append(f"[game2][nk1]{GAME_DUCK}[game3]")
        layers += ["game3", "nar"]
        keys.append("nk2")
        n += 1
    else:
        layers.append("game2")
    if snd.music is not None:
        args += ["-stream_loop", "-1", "-i", str(snd.music)]
        fade_out = max(0.0, d - 2.0)
        parts.append(f"[{n}:a]atrim=0:{d:.3f},asetpts=PTS-STARTPTS,aresample={RATE},aformat=channel_layouts=stereo,"
                     f"volume={snd.music_db:.1f}dB,afade=t=in:d=0.6,afade=t=out:st={fade_out:.3f}:d=2.0[mus]")
        if len(keys) > 1:
            parts.append("".join(f"[{k}]" for k in keys) + f"amix=inputs={len(keys)}:normalize=0[key]")
            key = "key"
        else:
            key = keys[0]
        parts.append(f"[mus][{key}]{ducking.DUCK}[ducked]")
        layers.append("ducked")
        n += 1
    else:
        for k in keys:
            parts.append(f"[{k}]anullsink")
    fx = []
    for j, (at, path, gain) in enumerate(snd.effects):
        args += ["-i", str(path)]
        ms = max(0, round(at * 1000))
        parts.append(f"[{n}:a]aresample={RATE},aformat=channel_layouts=stereo,volume={gain:.1f}dB,"
                     f"adelay={ms}|{ms}[fx{j}]")
        fx.append(f"[fx{j}]")
        n += 1
    if fx:
        parts.append("".join(fx) + (f"amix=inputs={len(fx)}:normalize=0:duration=longest[sfx]" if len(fx) > 1
                                    else "anull[sfx]"))
        layers.append("sfx")
    joined = "".join(f"[{x}]" for x in layers)
    mix = f"amix=inputs={len(layers)}:normalize=0:duration=first," if len(layers) > 1 else ""
    tail = [mastering.loudnorm(measured if stage == "mix" else None)]
    if stage == "mix":
        if abs(trim_db) > 0.01:
            tail.append(f"volume={trim_db:.2f}dB")
        tail += [f"aresample={RATE * 4}", f"alimiter=limit={limit:.4f}:attack=3:release=60:level=false",
                 f"aresample={RATE}"]
    tail.append(f"apad=whole_dur={d:.3f},atrim=0:{d:.3f}")
    parts.append(f"{joined}{mix}{','.join(tail)}[aout]")
    return args, ";".join(parts)


def _measure(args: list[str], graph: str) -> dict | None:
    done = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", *args, "-filter_complex", graph,
                           "-map", "[aout]", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError("ffmpeg couldn't mix the reel's sound: " + done.stderr.strip()[-400:])
    return mastering.parse_measurement(done.stderr)


def encode(wav: Path, dest: Path, bitrate: int) -> dict:
    """The mix as AAC, and its loudness as a meter reads it (AAC adds a little to the peaks)."""
    from ..creator.compositor import ebur128

    run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(wav), "-c:a", "aac", "-b:a", f"{bitrate}k", "-ar", str(RATE),
         str(dest)])
    return ebur128(dest)


def mix(reel: Reel, snd: Sound, dest: Path, progress: Callable[[str, "float | None"], None] | None = None,
        bitrate: int = 192) -> dict:
    """The finished sound as AAC at `dest` (an .m4a), at -14 LUFS with true peaks under -1 dBTP as measured
    after encoding; returns what was measured."""
    from ..creator.compositor import measure_loudness

    progress = progress or (lambda m, f: None)
    info: dict = {}
    if snd.has_audio:
        progress("Mixing the sound: the game's", 0.0)
        game = _measure(*mix_graph(reel, snd, stage="game"))
        if game:
            snd.game_gain_db = max(-12.0, min(18.0, GAME_LUFS - float(game["input_i"])))
            info["game_lufs"] = float(game["input_i"])
    if snd.narrator is not None:
        progress("Mixing the sound: the narrator", 0.2)
        nar = measure_loudness(snd.narrator)
        if nar:
            snd.narrator_gain_db = max(-20.0, min(24.0, NARRATOR_LUFS - float(nar["input_i"]) - 1.0))
    progress("Mixing the sound", 0.4)
    measured = _measure(*mix_graph(reel, snd, stage="measure"))
    wav = dest.with_suffix(".wav")
    trim, limit = 0.0, LIMIT
    got: dict = {}
    for attempt in range(3):
        args, graph = mix_graph(reel, snd, measured, trim_db=trim, limit=limit)
        run([ffmpeg_exe(), "-y", "-v", "error", *args, "-filter_complex", graph, "-map", "[aout]",
             "-t", f"{reel.duration:.3f}", "-c:a", "pcm_s24le", "-ar", str(RATE), str(wav)])
        got = encode(wav, dest, bitrate)
        progress("Mixing the sound", 0.6 + 0.2 * attempt)
        lufs, peak = got.get("lufs"), got.get("true_peak")
        if lufs is None:
            break
        off = mastering.TARGET_I - lufs
        over = (peak or -99) - (mastering.TARGET_TP - 0.1)
        if abs(off) <= 0.4 and over <= 0:
            break
        # Off target after encoding (two loud sounds summed, AAC's overshoot): nudge and make it again.
        trim += off
        if over > 0:
            limit = max(0.5, limit * 10 ** (-over / 20))
    wav.unlink(missing_ok=True)
    progress("Mixing the sound", 1.0)
    info |= {"game_gain_db": round(snd.game_gain_db, 2), "narrator_gain_db": round(snd.narrator_gain_db, 2),
             "mix_before_lufs": float(measured["input_i"]) if measured else None, "trim_db": round(trim, 2),
             "lufs": got.get("lufs"), "true_peak": got.get("true_peak"),
             "music": snd.music.name if snd.music else "", "crowd_lifts": len(lifts(reel))}
    return info

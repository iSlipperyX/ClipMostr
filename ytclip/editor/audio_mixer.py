"""The timeline's sound: three buses mixed and mastered inside the one ffmpeg run that renders the picture.

    voice bus   every clip on a voice track, each at its own time
                → 70 Hz high-pass → soft-knee compressor (3:1 above -20 dB, 6 dB knee)
                → gain to -14 LUFS (measured beforehand)
    music bus   every clip on a music track (looped to its clip's length when asked)
                → ducked under the voice by a sidechain compressor keyed from the voice bus:
                  about 16 dB down while someone speaks (50 ms attack, 300 ms release), back up in the gaps
    effects bus every clip on an sfx track, at its own level
    master      the three summed → two-pass EBU R128 loudness to -14 LUFS / -1 dBTP → a limiter as a last guard

The levels come from the timeline's `audio` block (AudioMaster), so a timeline can ask for its own target or
ducking depth. graph() builds the filters from the clips; the timeline compiler supplies each clip's ffmpeg
input. Loudness needs to hear the mix before setting its gain, so render() runs the graph twice first,
sound only and to nowhere (no file is written): once for the voice bus's level, once for the whole mix. The
picture is encoded once.

place_sfx() is the automatic sound design: a whoosh on each change of picture that has a transition, a bass
drop on the hook and an impact on each retention marker, never two effects on top of each other. The sounds
are the Video Creator's own (creator/sfx_library.py: synthesised here, or your files).
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..audio import mastering
from ..ffmpeg import NO_WINDOW, command_too_long, ffmpeg_exe, graph_file_args
from ..studio.timeline_schema import AUDIO_SFX, VIDEO_BASE, Asset, AudioClip, AudioMaster, Timeline

RATE = 48000
VOICE_HIGHPASS = "highpass=f=70:poles=2"
LIMIT_UNDER_TP = 0.2  # the limiter's ceiling sits this many dB under the true-peak target
DUCK_RATIO = 20.0
# Speech levelled to L LUFS reads about this many dB above L on the sidechain's RMS detector while someone is
# actually speaking (LUFS averages the pauses in; measured on narration: tests/test_timeline_audio.py).
SPEECH_ABOVE_LUFS = 1.2
NEAR = 1.0  # automatic effects keep this far apart
FORMAT = f"aresample={RATE},aformat=sample_fmts=fltp:channel_layouts=stereo"


@dataclass
class Source:
    """One clip's sound: the ffmpeg input it comes from and where it goes."""

    label: str  # an input pad: "3:a"
    role: str  # voice | music | sfx
    start: float  # seconds into the video
    duration: float
    gain_db: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    skip: float = 0.0  # seconds to drop from the front of the input first (a picture clip's lead-in)


def duck_filter(master: AudioMaster) -> str:
    """The sidechain compressor that pulls the music down `duck_db` under speech at the voice bus's level.
    A compressor takes (1 - 1/ratio) dB off for every dB the key is over the threshold, so the threshold is
    set that far under where speech sits."""
    depth = abs(master.duck_db)
    threshold_db = master.voice_lufs + SPEECH_ABOVE_LUFS - depth / (1 - 1 / DUCK_RATIO)
    threshold = max(0.000976563, min(1.0, 10 ** (threshold_db / 20)))
    return (f"sidechaincompress=threshold={threshold:.6f}:ratio={DUCK_RATIO:g}:attack={master.duck_attack_ms:g}:"
            f"release={master.duck_release_ms:g}:makeup=1:knee=2.5:level_sc=1")


def _clip(src: Source, name: str, total: float) -> str:
    length = max(0.01, min(src.duration, total - src.start))
    steps = [f"atrim=start={src.skip:.3f}:duration={length:.3f}" if src.skip > 0 else f"atrim=duration={length:.3f}",
             "asetpts=PTS-STARTPTS", FORMAT]
    if abs(src.gain_db) > 0.01:
        steps.append(f"volume={src.gain_db:.2f}dB")
    if src.fade_in > 0:
        steps.append(f"afade=t=in:d={min(src.fade_in, length):.3f}")
    if src.fade_out > 0:
        d = min(src.fade_out, length)
        steps.append(f"afade=t=out:st={max(0.0, length - d):.3f}:d={d:.3f}")
    ms = max(0, round(src.start * 1000))
    if ms:
        steps.append(f"adelay={ms}:all=1")
    return f"[{src.label}]{','.join(steps)}[{name}]"


def _bus(sources: list[Source], role: str, total: float, tail: list[str]) -> tuple[list[str], str | None]:
    mine = [s for s in sources if s.role == role and s.start < total and s.duration > 0]
    if not mine:
        return [], None
    parts = [_clip(s, f"{role}{i}", total) for i, s in enumerate(mine)]
    labels = "".join(f"[{role}{i}]" for i in range(len(mine)))
    mix = f"amix=inputs={len(mine)}:normalize=0:dropout_transition=0:duration=longest," if len(mine) > 1 else ""
    # Every bus runs the whole length, so the mix never ends early and the ducking always has a key to follow.
    parts.append(f"{labels}{mix}{','.join([*tail, f'apad=whole_dur={total:.3f}', f'atrim=duration={total:.3f}'])}"
                 f"[{role}bus]")
    return parts, f"{role}bus"


def voice_chain(master: AudioMaster, gain_db: float = 0.0) -> list[str]:
    steps = [VOICE_HIGHPASS, mastering.COMPRESSOR] if master.voice_compress else []
    if abs(gain_db) > 0.01:
        steps.append(f"volume={gain_db:.2f}dB")
    return steps


def voice_graph(sources: list[Source], master: AudioMaster, total: float) -> str | None:
    """The voice bus alone, ending in a loudness measurement: what its gain is worked out from. None when the
    timeline has no voice."""
    parts, bus = _bus(sources, "voice", total, voice_chain(master))
    if bus is None:
        return None
    parts.append(f"[{bus}]{mastering.loudnorm(None, master.voice_lufs, master.true_peak)}[aout]")
    return ";".join(parts)


def graph(sources: list[Source], master: AudioMaster, total: float, *, voice_gain_db: float = 0.0,
          measured: dict | None = None, measuring: bool = False, out: str = "aout") -> str:
    """The whole mix as filters ending in [out]. `voice_gain_db` brings the voice bus to its level;
    `measured` is the first loudness pass's numbers. With `measuring`, it ends in that measurement."""
    parts: list[str] = []
    v_parts, voice = _bus(sources, "voice", total, voice_chain(master, voice_gain_db))
    m_parts, music = _bus(sources, "music", total, [])
    s_parts, sfx = _bus(sources, "sfx", total, [])
    parts += v_parts + m_parts + s_parts
    layers: list[str] = []
    if voice and music and master.duck_db < -0.5:
        parts.append(f"[{voice}]asplit=2[voicemain][voicekey]")
        parts.append(f"[{music}][voicekey]{duck_filter(master)}[ducked]")
        layers += ["voicemain", "ducked"]
    else:
        layers += [x for x in (voice, music) if x]
    if sfx:
        layers.append(sfx)
    if not layers:
        parts.append(f"anullsrc=r={RATE}:cl=stereo,atrim=duration={total:.3f}[{out}]")
        return ";".join(parts)
    tail = [mastering.loudnorm(None if measuring else measured, master.target_lufs, master.true_peak)]
    if not measuring:
        limit = 10 ** ((master.true_peak - LIMIT_UNDER_TP) / 20)
        tail += [f"alimiter=limit={limit:.3f}:attack=4:release=60:level=false", f"aresample={RATE}",
                 f"atrim=duration={total:.3f}"]
    mix = f"amix=inputs={len(layers)}:normalize=0:dropout_transition=0:duration=first," if len(layers) > 1 else ""
    parts.append("".join(f"[{x}]" for x in layers) + mix + ",".join(tail) + f"[{out}]")
    return ";".join(parts)


def measure(inputs: list[str], filters: str, total: float, cwd: Path | None = None) -> dict | None:
    """Run a graph that ends in loudnorm's measurement, to nowhere, and read its numbers. None if it can't be
    measured (silence, or ffmpeg failed): the mix then falls back to one-pass loudness. The graph goes to
    ffmpeg in a file: a long timeline's is tens of thousands of characters, more than Windows lets a command
    line hold."""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="measure-", dir=cwd, delete=False,
                                         encoding="utf-8") as f:
            f.write(filters)
            script = Path(f.name)
    except OSError:
        return None
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-v", "info", *inputs, *graph_file_args(str(script)), "-map",
           "[aout]", "-t", f"{total:.3f}", "-f", "null", "-"]
    try:
        if command_too_long(cmd):
            return None
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
                              creationflags=NO_WINDOW, timeout=max(180.0, total * 4))
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        script.unlink(missing_ok=True)
    return mastering.parse_measurement(done.stderr) if done.returncode == 0 else None


def voice_gain(measured: dict | None, master: AudioMaster) -> float:
    """The gain that brings the voice bus to its level, from its measurement (0 when it couldn't be measured)."""
    if not measured:
        return 0.0
    try:
        return max(-30.0, min(30.0, master.voice_lufs - float(measured["input_i"])))
    except (KeyError, TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- automatic sound effects

@dataclass
class Placement:
    t: float  # when the sound starts
    cue: str
    why: str


def plan_sfx(tl: Timeline, lengths: dict[str, float] | None = None, *, whoosh: bool = True,
             whoosh_gap: float = 0.0) -> list[Placement]:
    """Where effects belong on a timeline: a bass drop on the hook (the very start, and each hook marker), an
    impact on each retention marker, a riser running up to each payoff, and a whoosh peaking on each change of
    picture that has a transition. Nothing lands within a second of an effect already there or already
    planned. `lengths` gives each cue's length in seconds (a whoosh peaks on its cut, a riser ends on its
    moment). `whoosh` off leaves the changes of picture silent; `whoosh_gap` keeps that many seconds between
    one whoosh and the next (a video with a new picture every three seconds doesn't want one on each)."""
    lengths = lengths or {}
    taken = sorted(c.start for t in tl.audio_tracks("sfx") for c in t.clips)
    out: list[Placement] = []

    def free(t: float) -> bool:
        return all(abs(t - x) >= NEAR for x in taken) and all(abs(t - p.t) >= NEAR for p in out)

    def add(t: float, cue: str, why: str) -> None:
        t = max(0.0, t)
        if t < tl.duration - 0.3 and free(t):
            out.append(Placement(round(t, 3), cue, why))

    add(0.0, "bass_drop", "the hook")
    for m in sorted(tl.markers, key=lambda m: m.t):
        if m.kind == "hook":
            add(m.t, "bass_drop", m.label or "a hook")
        elif m.kind == "retention":
            add(m.t, "impact", m.label or "a retention beat")
        elif m.kind == "payoff":
            add(m.t - lengths.get("riser", 1.2), "riser", m.label or "the payoff")
    base = tl.track(VIDEO_BASE)
    last = -1e9
    for clip in sorted(base.clips, key=lambda c: c.start)[1:] if base and whoosh else []:
        if not clip.transition_in.cut and clip.start - last >= whoosh_gap:
            before = len(out)
            add(clip.start - lengths.get("whoosh", 0.5) * 0.45, "whoosh", "a change of picture")
            if len(out) > before:
                last = clip.start
    return sorted(out, key=lambda p: p.t)


def place_sfx(tl: Timeline, *, folder: str | Path = "", level_db: float = 0.0, seed: str = "",
              whoosh: bool = True, whoosh_gap: float = 0.0) -> list[AudioClip]:
    """Put plan_sfx()'s effects on the timeline's sfx track (the Video Creator's sounds, or your files in
    `folder`, named for their cue). Returns the clips it added."""
    from ..creator import sfx_library

    yours = sfx_library.library(folder)
    sounds = {cue: sfx_library.sound_for(cue, folder, f"{seed}:{cue}", yours)
              for cue in ("bass_drop", "impact", "riser", "whoosh")}
    lengths = {cue: sfx_library.length_of(path) for cue, path in sounds.items()}
    track = tl.ensure_track(AUDIO_SFX)
    added = []
    for i, p in enumerate(plan_sfx(tl, lengths, whoosh=whoosh, whoosh_gap=whoosh_gap)):
        asset = tl.add_asset(Asset("audio", str(sounds[p.cue]), duration=round(lengths[p.cue], 3),
                                   source="made here" if not yours.get(p.cue) else "your sound effects"), p.cue)
        clip = AudioClip(f"auto_{p.cue}_{i + 1}", asset, p.t, round(min(lengths[p.cue], tl.duration - p.t), 3),
                         gain_db=sfx_library.CUES[p.cue][1] + level_db, label=p.why)
        track.clips.append(clip)
        added.append(clip)
    track.clips.sort(key=lambda c: c.start)
    return added

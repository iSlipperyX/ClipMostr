"""The audio half of the export's -filter_complex: cuts with click-free joins, mastering, bleeps, ducked
music and loudness, built from the settings and the edit plan.

In order:

1. the kept stretches of speech, each with a 5 ms fade at both ends (a raised-cosine, "qsin", curve), joined
   back to back: a cut in the middle of a waveform would otherwise click;
2. mastering (audio/mastering.py): 80 Hz high-pass, then the soft-knee speech compressor;
3. bleeps: each swear word is muted and, with the tone on, replaced by an 800 Hz beep;
4. music (audio/ducking.py), ducked under the speech by a sidechain compressor;
5. loudness: two-pass EBU R128 to -14 LUFS / -1 dBTP when mastering, else the older one-pass normalise;
6. the overall fade in and out, and the sample rate TikTok wants.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..audio import ducking, mastering
from ..ffmpeg import NO_WINDOW, ffmpeg_exe

JOIN_FADE = 0.005  # 5 ms at each join
BEEP_HZ = 800
BEEP_LEVEL = 0.1  # about -20 dBFS, near speech level before mastering: masks the word without a jolt
BLEEP_PAD = 0.03


@dataclass
class AudioPlan:
    duration: float  # output seconds
    spans: list[tuple[float, float]] = field(default_factory=list)  # kept stretches, seconds into the input
    bleeps: list[tuple[float, float]] = field(default_factory=list)  # output seconds
    bleep_tone: bool = True  # False: just silence the word
    music: Path | None = None
    music_level_db: float = -18.0
    master: bool = False
    normalize: bool = False  # the old one-pass loudnorm (Settings → Export → Normalise loudness)
    fades: bool = True
    rate: int = 48000
    stack_volume: float = 0.0  # a stacked second video's sound mixed in (0 = none)
    measured: dict | None = None  # the first loudness pass's numbers, for the second

    @property
    def edits(self) -> bool:
        return len(self.spans) > 1 or (bool(self.spans) and (self.spans[0][0] > 0.001 or
                                                             abs(self.spans[0][1] - self.duration) > 0.02))


def _enable(spans: list[tuple[float, float]], pad: float = 0.0) -> str:
    return "+".join(f"between(t,{max(0.0, a - pad):.3f},{b + pad:.3f})" for a, b in spans)


def speech(label: str, plan: AudioPlan) -> tuple[list[str], str]:
    """Filters that cut and join `label` (the source sound) per the plan. Returns (filters, output label)."""
    parts: list[str] = []
    if plan.edits:
        n = len(plan.spans)
        outs = "".join(f"[cut{i}]" for i in range(n))
        parts.append(f"[{label}]asplit={n}{outs}" if n > 1 else f"[{label}]anull[cut0]")
        for i, (a, b) in enumerate(plan.spans):
            length = b - a
            fade = min(JOIN_FADE, length / 4)
            parts.append(f"[cut{i}]atrim=start={a:.6f}:end={b:.6f},asetpts=PTS-STARTPTS,"
                         f"afade=t=in:d={fade:.4f}:curve=qsin,"
                         f"afade=t=out:st={max(0.0, length - fade):.6f}:d={fade:.4f}:curve=qsin[seg{i}]")
        parts.append("".join(f"[seg{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[speech]")
        return parts, "speech"
    parts.append(f"[{label}]anull[speech]")
    return parts, "speech"


def chain(plan: AudioPlan, source: str, music: str | None = None, second: str | None = None,
          measuring: bool = False) -> tuple[str, str]:
    """The whole audio graph from input labels ("0:a", "2:a", ...). Returns (graph, output label).
    With `measuring`, it ends in the loudness measurement instead of the fades (the first pass)."""
    parts, last = speech(source, plan)
    steps = [f"aresample={plan.rate}"] + mastering.speech_chain(plan.master)
    if plan.bleeps:
        steps.append(f"volume=volume=0:enable='{_enable(plan.bleeps, BLEEP_PAD)}'")
    parts.append(f"[{last}]{','.join(steps)}[voice]")
    last = "voice"
    if plan.bleeps and plan.bleep_tone:
        parts.append(f"aevalsrc=exprs='{BEEP_LEVEL}*sin(2*PI*{BEEP_HZ}*t)*({_enable(plan.bleeps, BLEEP_PAD)})'"
                     f":s={plan.rate}:d={plan.duration:.3f},aformat=channel_layouts=stereo[beep]")
        parts.append(f"[{last}]aformat=channel_layouts=stereo[voice_st];"
                     f"[voice_st][beep]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[bleeped]")
        last = "bleeped"
    if second is not None and plan.stack_volume > 0:
        parts.append(f"[{second}]volume={plan.stack_volume:.2f},aresample={plan.rate}[a2];"
                     f"[{last}][a2]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[withsecond]")
        last = "withsecond"
    if music is not None:
        parts.append(ducking.graph(last, music, "withmusic", plan.music_level_db, plan.duration))
        last = "withmusic"
    tail: list[str] = []
    if plan.master:
        tail.append(mastering.loudnorm(None if measuring else plan.measured))
    elif plan.normalize and not measuring:
        tail.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    if not measuring:
        if plan.fades:
            tail.append(f"afade=t=in:d=0.15,afade=t=out:st={max(0.0, plan.duration - 0.3):.3f}:d=0.3")
        tail.append(f"aresample={plan.rate}")
    parts.append(f"[{last}]{','.join(tail) or 'anull'}[aout]")
    return ";".join(parts), "aout"


def measure(inputs: list[str], plan: AudioPlan, source: str, music: str | None, second: str | None,
            cwd: Path | None = None) -> dict | None:
    """The first loudness pass: run the chain to its end and read loudnorm's numbers. None if it fails
    (the second pass then falls back to one-pass loudnorm)."""
    graph, out = chain(plan, source, music, second, measuring=True)
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-v", "info", *inputs, "-filter_complex", graph, "-map",
           f"[{out}]", "-t", f"{plan.duration:.3f}", "-f", "null", "-"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
                              creationflags=NO_WINDOW, timeout=max(120.0, plan.duration * 4))
    except (OSError, subprocess.SubprocessError):
        return None
    return mastering.parse_measurement(done.stderr) if done.returncode == 0 else None

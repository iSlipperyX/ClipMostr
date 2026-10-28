"""Quality check: look at every exported clip before calling it ready to upload.

Each clip file is decoded once (small and fast) to measure its loudness, black frames and
frozen picture, and its length, size and audio are compared with what was asked for.
With a transcript, it also checks there's talking in it and that it doesn't start or end
mid-sentence; with AI polish on, Claude's 1-10 "does this stand alone?" rating counts too.

Problems reject a clip: its files move to a `_rejected` folder beside the others, so
whatever picks up the ready clips never sees them. Warnings are noted in clips.json but
the clip stays. Every clip gets a `qc` record in clips.json saying which.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .analyze import Clip
from .ffmpeg import communicate, ffmpeg_exe, probe
from .subtitles import Word, words_between

REJECTED = "_rejected"
_SENTENCE_END = (".", "!", "?", "…", '."', '?"', '!"')


@dataclass
class FileStats:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    mean_db: float | None = None
    max_db: float | None = None
    black: float = 0.0  # seconds of black picture
    frozen: float = 0.0  # seconds of frozen picture


@dataclass
class Verdict:
    ok: bool = True
    problems: list[str] = field(default_factory=list)  # why it was rejected
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"ok": self.ok, **({"problems": self.problems} if self.problems else {}),
                **({"warnings": self.warnings} if self.warnings else {})}


def _spans(text: str, start_key: str, end_key: str, duration: float) -> float:
    """Total seconds covered by ffmpeg's start/end detector lines (an open span runs to the end)."""
    total, opened = 0.0, None
    for m in re.finditer(rf"({start_key}|{end_key})[:=]\s*([\d.]+)", text):
        t = float(m[2])
        if m[1] == start_key:
            opened = t
        elif opened is not None:
            total += max(0.0, t - opened)
            opened = None
    if opened is not None:
        total += max(0.0, duration - opened)
    return total


def parse_stats(text: str, stats: FileStats) -> FileStats:
    if m := re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", text):
        stats.mean_db = -91.0 if m[1] == "-inf" else float(m[1])
    if m := re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", text):
        stats.max_db = -91.0 if m[1] == "-inf" else float(m[1])
    stats.black = _spans(text, "black_start", "black_end", stats.duration)
    stats.frozen = _spans(text, "lavfi.freezedetect.freeze_start", "lavfi.freezedetect.freeze_end", stats.duration)
    return stats


def measure(path: Path) -> FileStats:
    """Read a clip file's size and length, then decode it once at low resolution for the rest."""
    info = probe(path)
    stats = FileStats(info.duration, info.width, info.height, info.has_audio)
    if not info.duration:
        return stats
    audio = ["-af", "volumedetect"] if info.has_audio else ["-an"]
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-v", "info", "-i", str(path),
           "-vf", "scale=240:-2,blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-50dB:d=2", *audio, "-f", "null", "-"]
    out = communicate(cmd).stderr
    return parse_stats(out, stats)


def judge_file(stats: FileStats, expected_length: float, size: tuple[int, int] | None,
               want_audio: bool) -> Verdict:
    v = Verdict()
    if not stats.duration:
        v.problems.append("The file can't be read")
        v.ok = False
        return v
    if expected_length and abs(stats.duration - expected_length) > max(1.5, expected_length * 0.05):
        v.problems.append(f"Came out {stats.duration:.0f}s long instead of {expected_length:.0f}s")
    if size and (stats.width, stats.height) != size:
        v.problems.append(f"Came out {stats.width}×{stats.height} instead of {size[0]}×{size[1]}")
    if want_audio and not stats.has_audio:
        v.problems.append("No sound")
    elif want_audio and stats.max_db is not None and stats.max_db < -45:
        v.problems.append("Silent")
    elif want_audio and stats.mean_db is not None and stats.mean_db < -32:
        v.warnings.append(f"Very quiet (average {stats.mean_db:.0f} dB)")
    for seconds, what, reject, warn in ((stats.black, "black", 0.4, 0.1), (stats.frozen, "frozen", 0.5, 0.2)):
        share = seconds / stats.duration
        if what == "frozen" and stats.black / stats.duration > 0.4:
            break  # a black picture is "frozen" too; once is enough
        if share > reject:
            v.problems.append(f"The picture is {what} for {share:.0%} of it")
        elif share > warn:
            v.warnings.append(f"The picture is {what} for {seconds:.0f}s")
    v.ok = not v.problems
    return v


def judge_content(clip: Clip, words: list[Word], min_rating: int) -> Verdict:
    v = Verdict()
    inside = words_between(words, clip.start, clip.end)
    if words and clip.length > 0:
        talk = sum(min(w.end, clip.end) - max(w.start, clip.start) for w in inside)
        if talk / clip.length < 0.15:
            v.warnings.append("Hardly any talking")
        punctuated = any(w.text.endswith(_SENTENCE_END) for w in words)
        if inside and punctuated:
            before = [w for w in words if w.end <= clip.start + 0.01]
            if before and not before[-1].text.endswith(_SENTENCE_END) and inside[0].text[:1].islower() \
                    and inside[0].start - before[-1].end < 1.0:
                v.warnings.append("Starts mid-sentence")
            after = [w for w in words if w.start >= clip.end - 0.01]
            if after and not inside[-1].text.endswith(_SENTENCE_END) and after[0].start - inside[-1].end < 1.0:
                v.warnings.append("Ends mid-sentence")
    if min_rating and clip.rating and clip.rating < min_rating:
        v.problems.append(f"AI rated it {clip.rating}/10" + (f": {clip.rating_note}" if clip.rating_note else ""))
    v.ok = not v.problems
    return v


def merge(*verdicts: Verdict) -> Verdict:
    out = Verdict()
    for v in verdicts:
        out.problems += v.problems
        out.warnings += v.warnings
    out.ok = not out.problems
    return out


def reject(folder: Path, name: str) -> str:
    """Move a clip and its .srt into the _rejected folder; returns its new relative path."""
    dest = folder / REJECTED
    dest.mkdir(exist_ok=True)
    for f in (folder / name, (folder / name).with_suffix(".srt")):
        if f.exists():
            target = dest / f.name
            target.unlink(missing_ok=True)
            shutil.move(str(f), str(target))
    return f"{REJECTED}/{name}"


def summary(verdicts: list[Verdict]) -> dict:
    return {"passed": sum(v.ok for v in verdicts), "rejected": sum(not v.ok for v in verdicts),
            "warned": sum(v.ok and bool(v.warnings) for v in verdicts)}


def check_folder(folder: Path, log: Callable[[str], None] | None = None, min_rating: int = 5,
                 size: tuple[int, int] | None = None, measure_file: Callable[[Path], FileStats] = measure) -> dict:
    """Re-check an exported folder from its clips.json (`ytclip qc`). Without the transcript only the
    file checks and saved AI ratings apply. Rejected clips move to _rejected; clips.json is updated."""
    index = folder / "clips.json"
    data = json.loads(index.read_text(encoding="utf-8"))
    verdicts = []
    for rec in data.get("clips", []):
        path = folder / rec["file"]
        if rec["file"].startswith(f"{REJECTED}/"):
            verdicts.append(Verdict(False, (rec.get("qc") or {}).get("problems", ["Rejected before"])))
            continue
        if path.exists():
            stats = measure_file(path)
            file_v = judge_file(stats, rec.get("length", 0), size, stats.has_audio)
        else:
            file_v = Verdict(False, ["The file is missing"])
        clip = Clip(rec.get("start", 0), rec.get("end", 0), 0, rating=int(rec.get("rating") or 0),
                    rating_note=rec.get("rating_note", ""))
        v = merge(file_v, judge_content(clip, [], min_rating))
        if not v.ok and path.exists():
            rec["file"] = reject(folder, rec["file"])
        rec["qc"] = v.to_json()
        verdicts.append(v)
        if log:
            log(f"{'✓' if v.ok else '✗'} {Path(rec['file']).name}"
                + (f": {'; '.join(v.problems or v.warnings)}" if v.problems or v.warnings else ""))
    data["qc"] = summary(verdicts)
    index.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data["qc"]


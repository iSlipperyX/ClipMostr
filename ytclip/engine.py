"""The pipeline shared by the GUI and CLI: open → measure → transcribe → find clips → export."""

from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import cuts, layout, series, speed
from . import transcribe as whisper
from .analyze import (SIGNAL_LABELS, Clip, Measurements, breathing_room, build_signals, chapter_clips, clip_score,
                      cap_length, combine, fit_to_sentences, loudness, pick_clips, quiet_frames, reach_length, snap,
                      trim_dead_air)
from .download import Video, download, is_url, local_video
from .export import export_clip, thumbnail, tiktok_warnings
from .export import frame as export_frame
from .ffmpeg import Cancelled, MediaInfo, probe
from .organize import clip_filename, video_dir, write_index
from .scenes import detect_scenes
from .settings import Settings
from .stackpool import StackPool, pool_files
from .subtitles import (Cue, Word, censor, parse_vtt, parse_vtt_words, sentences, srt, text_between, to_dicts,
                        words_between)

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]


@dataclass
class Project:
    """One video moving through the pipeline."""

    source: str
    status: str = "queued"  # queued | working | ready | exporting | done | error
    stage: str = ""
    error: str = ""
    video: Video | None = None
    media: MediaInfo | None = None
    measurements: Measurements | None = None
    words: list[Word] = field(default_factory=list)
    transcript_source: str = ""  # captions | whisper | "" (none); captions+whisper once the clips are typed out
    typed: list = field(default_factory=list)  # (start, end) stretches Whisper typed out over the captions
    signals: dict[str, np.ndarray] = field(default_factory=dict)
    score: np.ndarray = field(default_factory=lambda: np.zeros(0))
    clips: list[Clip] = field(default_factory=list)
    mode_used: str = ""
    ai_model: str = ""  # the model that actually answered (may differ after a refusal fallback)
    ai_error: str = ""  # why AI mode fell back to signals, if it did
    polished: bool = False  # the AI polish answered (polish.py)
    polish_error: str = ""
    captions_off: bool = False  # the video has subtitles of its own, so ours stay off
    qc: dict = field(default_factory=dict)  # the quality check's tally after export (qc.py)
    export_dir: Path | None = None
    meta: dict = field(default_factory=dict)  # extra sections for clips.json, e.g. why automation picked it
    timings: dict[str, float] = field(default_factory=dict)  # seconds each stage took (see TIMED)

    @property
    def name(self) -> str:
        return self.video.title if self.video else self.source

    @property
    def cues(self) -> list[Cue]:
        return self.measurements.cues if self.measurements else []

    def transcript(self, clip: Clip) -> str:
        return text_between(self.cues, clip.start, clip.end)


RETYPE_PAD = 3.0  # seconds either side of a clip Whisper types out too, so trimming has words to go by

# The stages a video's time is reported in, in order (automation logs them per video).
TIMED = (("download", "download"), ("audio", "audio"), ("scenes", "scene cuts"), ("transcript", "transcript"),
         ("find", "finding clips"), ("polish", "AI polish"), ("export", "export"), ("check", "quality check"))


@contextmanager
def timed(p: Project, key: str):
    start = time.monotonic()
    try:
        yield
    finally:
        p.timings[key] = p.timings.get(key, 0.0) + time.monotonic() - start


def clock_text(seconds: float) -> str:
    """'45 s', '6:05' (minutes:seconds) or '1:02:10'."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    h, rest = divmod(seconds, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def timing_text(timings: dict[str, float]) -> str:
    """'took 6:05 (download 1:10, audio 12 s, transcript 2:40, ...)'; stages under a second are left out."""
    total = sum(timings.values())
    parts = [f"{label} {clock_text(timings[key])}" for key, label in TIMED if timings.get(key, 0) >= 1]
    return f"took {clock_text(total)}" + (f" ({', '.join(parts)})" if parts else "")


class Engine:
    def __init__(self, settings: Settings, log: Log | None = None, progress: Progress | None = None,
                 cancelled: Callable[[], bool] | None = None):
        self.s = settings
        self.log = log or (lambda m: None)
        self._progress = progress or (lambda m, f: None)
        self._cancelled = cancelled or (lambda: False)
        self.stack_pool: StackPool | None = None  # created on first use; tests swap in their own

    def progress(self, msg: str, frac: float | None = None) -> None:
        if self._cancelled():
            raise Cancelled()
        self._progress(msg, frac)

    # ------------------------------------------------------------ stage 1: open

    def open(self, p: Project) -> None:
        with timed(p, "download"):
            self._open(p)

    def _open(self, p: Project) -> None:
        if is_url(p.source):
            if p.video is not None and p.video.path.exists():  # automation downloaded it early
                self.progress("Downloaded already", None)
            else:
                p.video = download(p.source, self.s.cache_path, self.s.quality, self.s.cookies_browser or None,
                                   self.log, self.progress)
        else:
            self.progress("Reading file", None)
            p.video = local_video(Path(p.source))
        p.media = probe(p.video.path)
        if not p.video.duration:
            p.video.duration = p.media.duration

    # ------------------------------------------------------------ stage 2: measure (cached)

    def measure(self, p: Project) -> None:
        v = p.video
        assert v is not None
        cache = self.s.cache_path / f"{v.id}.analysis.npz"
        db, fine, scene_cuts, scenes_done = None, None, [], False
        if cache.exists():
            try:
                data = np.load(cache)
                db, scene_cuts, scenes_done = data["db"], list(data["cuts"]), bool(data["scenes_done"])
                fine = data["fine"] if "fine" in data.files else None  # caches before 2.4.0 have no fine loudness
            except Exception:
                db = None
        if db is None or fine is None:
            with timed(p, "audio"):
                db, fine = loudness(v.path, v.duration, lambda f: self.progress("Measuring audio", f))
        if self.s.detect_scenes and not scenes_done:
            with timed(p, "scenes"):
                scene_cuts = self._scenes(v)
            scenes_done = True
        self.s.cache_path.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache, db=db, fine=fine, cuts=np.array(scene_cuts, dtype=float), scenes_done=scenes_done)

        with timed(p, "transcript"):
            cues, words, source = self._transcript(p)
        if words and source == "captions" and not cuts.punctuated(words):
            n = cuts.refine_words(words, cuts.pauses_in(fine))  # auto-captions: find the pauses by listening
            if n:
                self.log(f"Found {n} pauses in the speech to cut at (the captions have no punctuation).")
        p.words, p.transcript_source = words, source
        p.measurements = Measurements(duration=v.duration, db=db, heatmap=v.heatmap,
                                      scene_cuts=scene_cuts if self.s.detect_scenes else [], cues=cues, fine=fine)

    def _scenes(self, v: Video) -> list[float]:
        """Scene cuts, decoding the video on the graphics card unless Speed is Quiet (and on the
        CPU if the card can't decode it)."""
        def report(f: float) -> None:
            self.progress("Detecting scene cuts", f)

        if speed.hwaccel(self.s.speed):
            try:
                return detect_scenes(v.path, v.duration, report, hwaccel=True)
            except RuntimeError:
                pass
        return detect_scenes(v.path, v.duration, report)

    def _whisper(self, v: Video, spans: list[tuple[float, float]] | None = None,
                 checkpoint: Path | None = None) -> tuple[list[Cue], list[Word]]:
        return whisper.transcribe(v.path, self.s.whisper_model, self.s.whisper_language or v.language, v.duration,
                                  self.progress, threads=speed.threads(self.s.speed), spans=spans,
                                  checkpoint=checkpoint, gpu=speed.use_gpu(self.s), log=self.log)

    def _transcript(self, p: Project) -> tuple[list[Cue], list[Word], str]:
        v = p.video
        assert v is not None
        if v.subtitles and v.subtitles.exists() and self.s.transcribe != "always":
            return parse_vtt(v.subtitles), parse_vtt_words(v.subtitles), "captions"
        if self.s.transcribe == "never" or not whisper.available():
            return [], [], ""
        cache = self.s.cache_path / f"{v.id}.whisper-{self.s.whisper_model}.json"
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            return [Cue(**c) for c in data["cues"]], [Word(**w) for w in data["words"]], "whisper"
        # Typing out a long video takes a while: a checkpoint lets a stopped run carry on from there.
        checkpoint = cache.with_name(cache.name.replace(".json", ".partial.json"))
        cues, words = self._whisper(v, checkpoint=checkpoint)
        cache.write_text(json.dumps({"cues": to_dicts(cues), "words": to_dicts(words)}), encoding="utf-8")
        checkpoint.unlink(missing_ok=True)
        return cues, words, "whisper"

    def retype(self, p: Project, clips: list[Clip] | None = None) -> int:
        """Transcription → For the clips: YouTube's captions found the clips, now Whisper types out
        just them (and a little either side) for their captions and titles. Stretches typed out
        before are kept, here and in a cache, so only new clips cost anything. Returns how many
        stretches were typed out now."""
        v, m = p.video, p.measurements
        if (self.s.transcribe != "clips" or v is None or m is None or not p.transcript_source.startswith("captions")
                or not whisper.available()):
            return 0
        own = clips is None
        clips = [c for c in (p.clips if own else clips) if c.enabled]
        wanted = whisper.merge_spans([(c.start - RETYPE_PAD, c.end + RETYPE_PAD) for c in clips], v.duration)
        new = [s for s in wanted if not any(a <= s[0] + 0.01 and s[1] - 0.01 <= b for a, b in p.typed)]
        if not new:
            return 0
        cache = self.s.cache_path / f"{v.id}.whisper-{self.s.whisper_model}.clips.json"
        saved: dict = {"spans": [], "cues": [], "words": []}
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        have = [tuple(s) for s in saved.get("spans", [])]
        todo = [s for s in new if not any(a <= s[0] + 0.01 and s[1] - 0.01 <= b for a, b in have)]
        cues = [Cue(**c) for c in saved.get("cues", [])]
        words = [Word(**w) for w in saved.get("words", [])]
        if todo:
            try:
                got_cues, got_words = self._whisper(v, spans=todo)
            except Cancelled:
                raise
            except Exception as exc:  # the clips still have YouTube's captions: carry on with those
                self.log(f"Whisper couldn't type out the clips ({exc}), so their captions are YouTube's.")
                return 0
            for a, b in todo:  # replace anything saved for these stretches
                cues = [c for c in cues if not a <= (c.start + c.end) / 2 < b] + whisper.inside(got_cues, a, b)
                words = [w for w in words if not a <= (w.start + w.end) / 2 < b] + whisper.inside(got_words, a, b)
            have = whisper.merge_spans(have + todo, v.duration, gap=0.0)
            cues.sort(key=lambda c: c.start)
            words.sort(key=lambda w: w.start)
            self.s.cache_path.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"spans": have, "cues": to_dicts(cues), "words": to_dicts(words)}),
                             encoding="utf-8")
        for a, b in new:  # Whisper's words take the captions' place in these stretches
            p.words = ([w for w in p.words if not a <= (w.start + w.end) / 2 < b] + whisper.inside(words, a, b))
            m.cues = ([c for c in m.cues if not a <= (c.start + c.end) / 2 < b] + whisper.inside(cues, a, b))
        p.words.sort(key=lambda w: w.start)
        m.cues.sort(key=lambda c: c.start)
        p.typed = whisper.merge_spans(list(p.typed) + new, v.duration, gap=0.0)
        p.transcript_source = "captions+whisper"
        self.log(f"Whisper typed out {len(new)} clip stretch{'es' if len(new) != 1 else ''} "
                 f"({clock_text(sum(b - a for a, b in new))}) for their captions; the captions found them.")
        if own:  # edges go by Whisper's words now, which can sit a little apart from the captions'
            self.trim(p)
            self.hold_length(p)
            self.clean_cuts(p)
        return len(new)

    # ------------------------------------------------------------ stage 3: find clips

    def find(self, p: Project, use_ai: bool = True, strict: bool = False) -> None:
        """Score the video and pick clips. Cheap unless AI mode calls out to the AI.

        If the AI call fails, unattended runs fall back to smart signals (recording why in
        p.ai_error); with strict=True - the user explicitly asked for AI - the error is raised."""
        m, v = p.measurements, p.video
        assert m is not None and v is not None
        self.progress("Finding clips", None)
        self.rescore(p)
        sents = self._sentences(p)
        mode = self.s.mode
        p.ai_error = ""
        if mode == "chapters" and not v.chapters:
            self.log("No chapters on this video - using smart signals instead.")
            mode = "auto"
        if mode == "ai" and use_ai:
            from .ai import AIError, find_clips_ai

            self.progress("AI is picking clips", None)
            try:
                clips, model = find_clips_ai(
                    v.title, v.duration, sents, p.score, self.s.clip_count, *self.lengths(v.duration),
                    self.s.ai_instructions, self.s, self.log,
                    check=lambda: self.progress("AI is picking clips", None))
                self.progress("AI is picking clips", None)  # a cancel during the call discards its result
                clips = self._fit(clips, sents, m, v.duration)
                if not clips:
                    raise AIError("None of the AI's clips were long enough once aligned to sentences.")
                p.clips, p.mode_used, p.ai_model = clips, "ai", model
                self.parts(p, sents)
                self.trim(p)
                self.hold_length(p, sents)
                self.clean_cuts(p)
                return
            except Cancelled:
                raise
            except Exception as exc:
                if strict:
                    raise
                p.ai_error = str(exc)
                self.log(f"AI clip finder failed: {exc} Using smart signals instead.")
                mode = "auto"
        if mode == "chapters":
            p.clips = chapter_clips(v.chapters, p.score)
        else:
            # With a transcript, edges are aligned to sentences afterwards (better than
            # pauses alone), so skip pause-snapping and padding inside pick_clips.
            align = bool(sents) and self.s.snap_to_pauses

            def pick(count: int) -> list[Clip]:
                return pick_clips(p.score, m.db, count, *self.lengths(v.duration),
                                  self.s.min_gap, self.s.quality_floor, self.s.snap_to_pauses and not align,
                                  0.0 if align else self.s.pad_start, 0.0 if align else self.s.pad_end)

            clips = pick(self.s.clip_count)
            # The picks after those, for the places a part series may free up (series.extend)
            chosen = {(c.start, c.end) for c in clips}
            spares = ([c for c in pick(self.s.clip_count + self.s.series_parts - 1) if (c.start, c.end) not in chosen]
                      if self.s.series_parts >= 2 else [])
            if align:
                clips = self._fit(clips, sents, m, v.duration)
                spares = self._fit(spares, sents, m, v.duration, silent=True)
                for c in clips + spares:
                    c.score = clip_score(p.score, c.start, c.end)
                clips.sort(key=lambda c: c.score, reverse=True)
            p.clips = clips
            self.parts(p, sents, spares)
        p.mode_used = mode
        self.trim(p)
        if mode != "chapters":  # a chapter is as long as the video made it
            self.hold_length(p, sents)
        self.clean_cuts(p)
        self.label(p)

    def _sentences(self, p: Project) -> list[Cue]:
        # Without punctuation, a breath the audio shows (cuts.refine_words) ends a sentence.
        if p.words:
            return sentences(p.words, pause=0.9 if cuts.punctuated(p.words) else cuts.BREATH - 0.01)
        return p.measurements.cues if p.measurements else []

    def lengths(self, duration: float = 0.0) -> tuple[float, float]:
        """The shortest and longest a clip may be: Min/Max clip length, or the whole video when it's
        shorter, and never a longest under the shortest."""
        shortest = min(self.s.min_len, duration) if duration else self.s.min_len
        return shortest, max(self.s.max_len, self.s.min_len)

    def hold_length(self, p: Project, sents: list[Cue] | None = None) -> int:
        """The last word on clip length: every clip is at least Min clip length (unless the video is
        shorter). A short one grows to a sentence's end, or back to a sentence's start, within Max clip
        length and without running into the clips beside it; one with no room to grow is dropped.
        Clips you edited stay as they are. Returns how many were dropped."""
        if p.video is None or not p.clips:
            return 0
        sents = self._sentences(p) if sents is None else sents
        shortest, longest = self.lengths(p.video.duration)
        grown, dropped = 0, []
        on = sorted((c for c in p.clips if c.enabled), key=lambda c: c.start)
        labels = dict(zip(map(id, p.clips), series.labels(p.clips)))
        for i, c in enumerate(on):
            if c.edited or c.length >= shortest - 0.01:
                continue
            _, part, parts = labels[id(c)]
            lo = max((o.end for o in on[:i] if o.end <= c.start + 0.01 and o not in dropped), default=0.0)
            hi = min((o.start for o in on[i + 1:] if o.start >= c.end - 0.01), default=p.video.duration)
            where = reach_length(c.start, c.end, sents, shortest, longest, lo, hi,
                                 grow_start=part <= 1, grow_end=not (part and part < parts))
            if where is None:
                dropped.append(c)
                continue
            c.start, c.end = round(where[0], 2), round(where[1], 2)
            grown += 1
        for c in dropped:
            p.clips.remove(c)
        if grown:
            self.log(f"Made {grown} clip{'s' if grown != 1 else ''} longer, to at least {shortest:.0f} s.")
        if dropped:
            self.log(f"Dropped {len(dropped)} clip{'s' if len(dropped) != 1 else ''} that couldn't be made "
                     f"{shortest:.0f} s long without running into another clip.")
        return len(dropped)

    def clean_cuts(self, p: Project) -> None:
        """The last word on clip edges: none starts or ends part-way through a word (cuts.py)."""
        m = p.measurements
        if m is None or not len(m.fine) or not self.s.snap_to_pauses:
            return
        n = cuts.clean_edges(p.clips, cuts.pauses_in(m.fine), m.duration, *self.lengths(m.duration))
        if n:
            self.log(f"Moved the edges of {n} clip{'s' if n != 1 else ''} into a pause, so no word is cut off.")

    def parts(self, p: Project, sents: list[Cue], spares: list[Clip] = ()) -> None:
        """Part series (series.py): a great moment longer than one clip carries on in Part 2, 3..."""
        if self.s.series_parts < 2 or p.video is None:
            return
        n = series.extend(p.clips, p.score, sents, *self.lengths(p.video.duration), self.s.clip_count,
                          self.s.series_parts, p.video.duration, spares)
        if not n and any(sid for sid, _, _ in series.labels(p.clips)):  # the finder's own clips carry on
            series.keep(p.clips, len(p.clips))  # list them together, in order
        if n:
            self.log(f"Added {n} next part{'s' if n != 1 else ''}: a great moment ran longer than one clip.")

    def trim(self, p: Project) -> None:
        """Pull clip edges in to the speech (Trim dead air), leaving hand-edited clips alone."""
        if not (self.s.trim_silence and p.words):
            return
        floor = self.lengths(p.video.duration if p.video else 0.0)[0]  # never shorter than Min clip length
        n = 0
        for c, (_, part, parts) in zip(p.clips, series.labels(p.clips)):
            if c.edited:
                continue
            was = c.start, c.end
            if trim_dead_air(c, p.words, max(0.25, self.s.pad_start), max(0.5, self.s.pad_end), min_len=floor):
                # where one part meets the next stays put, so the series holds together
                c.start = was[0] if part > 1 else c.start
                c.end = was[1] if part and part < parts else c.end
                n += (c.start, c.end) != was
        if n:
            self.log(f"Trimmed dead air from {n} clip{'s' if n != 1 else ''}.")

    # ------------------------------------------------------------ stage 3b: AI polish

    def polish(self, p: Project) -> None:
        """Hooks, caption fixes, speaker crops and a burned-in subtitle check from one AI call.
        Never fails the video: on any error the clips go out as they are (reason in p.polish_error)."""
        from . import learn, niches, polish

        p.polished, p.polish_error, p.captions_off = False, "", False
        clips = [c for c in p.clips if c.enabled]
        if not clips or p.video is None:
            return
        self.progress("AI is polishing the clips", None)
        niche = niches.find(self.s, (p.meta.get("automation") or {}).get("niche_id", ""))
        lessons = learn.for_settings(self.s)
        guide = lessons.prompt_text(niche.id if niche else "") if lessons else ""
        if guide:
            self.log("The AI sees which of your earlier hooks worked.")
        if niche:  # hooks that suit the account these clips are for
            about = f"These clips are for a TikTok account about {niche.name}."
            guide = "\n\n".join(x for x in (about + (f" {niche.instructions.strip()}" if niche.instructions.strip()
                                                       else ""), guide) if x)
        notes = str(((p.meta.get("automation") or {}).get("request") or {}).get("notes") or "").strip()
        if notes:  # a clip request's special requests shape the hooks and bottom lines too
            guide = "\n\n".join(x for x in (f"Special requests for these clips: {notes}", guide) if x)
        # Bottom lines stand in for the 'no part 2' text on clips with no part 2 (layout.bottom_banner).
        bottom_on = layout.uses_ai_bottom(self.s)
        usual = " ".join(layout.own_words(self.s.banner_bottom_alt).split()) if bottom_on else ""
        try:
            result = polish.ask(p.video.title, clips, p.words, p.video.path, self.s, self.log,
                                check=lambda: self.progress("AI is polishing the clips", None), guide=guide,
                                bottom=usual, handles=layout.bottom_handles(self.s) if bottom_on else [])
            self.progress("AI is polishing the clips", None)
        except Cancelled:
            raise
        except Exception as exc:
            p.polish_error = str(exc) or exc.__class__.__name__
            self.log(f"AI polish skipped: {p.polish_error}")
            return
        summary = polish.apply(result, clips, p.words, self.s.banner_top_text, self.s.banner_top, bottom_on=bottom_on)
        p.polished = True
        if result.burned_in_subtitles and self.s.polish_burned_subs == "off" and self.s.burn_captions:
            p.captions_off = True
            self.log("This video already has subtitles in the picture, so ClipMonster's captions are off for it.")
        summary["captions_off"] = p.captions_off
        p.meta["polish"] = summary
        self.log(f"Polished: {summary['hooks']} hooks, "
                 + (f"{summary['bottom_lines']} bottom lines, " if summary["bottom_lines"] else "")
                 + f"{summary['caption_fixes']} caption fixes, {summary['crops_moved']} crops moved.")

    def _fit(self, clips: list[Clip], sents: list[Cue], m: Measurements, duration: float,
             silent: bool = False) -> list[Clip]:
        """Align edges to sentences (or pauses without a transcript), add a breath of room,
        enforce the minimum length and drop overlaps - best-scoring clips win."""
        quiet = quiet_frames(m.db) if self.s.snap_to_pauses and not sents else np.zeros(0, bool)
        min_len, max_len = self.lengths(duration)
        kept: list[Clip] = []
        dropped = 0
        for c in sorted(clips, key=lambda c: c.score, reverse=True):
            if sents and self.s.snap_to_pauses:
                c.start, c.end = fit_to_sentences(c.start, c.end, sents, min_len, max_len)
                c.start, c.end = breathing_room(c.start, c.end, sents, 0.15, 0.3)
                c.end = cap_length(c.start, c.end, sents, min_len, max_len)
            elif len(quiet):
                c.start, c.end = snap(c.start, quiet, -1.5, 0.5), snap(c.end, quiet, -0.5, 1.5)
            # pad_start/pad_end are deliberate user choices, so they may reach into neighbours.
            c.start = max(0.0, c.start - self.s.pad_start)
            c.end = min(duration, c.end + self.s.pad_end)
            where = reach_length(c.start, c.end, sents, min_len, max_len, 0.0, duration)  # too short: grow it
            if where is not None:
                c.start, c.end = where
            if c.length < min_len - 0.01 or any(c.start < k.end and c.end > k.start for k in kept):
                dropped += 1
                continue
            kept.append(c)
        if dropped and not silent:
            self.log(f"Skipped {dropped} clip(s) that were too short or overlapped another clip.")
        return kept

    def rescore(self, p: Project) -> None:
        m = p.measurements
        assert m is not None
        p.signals = build_signals(m, self.s.keyword_list)
        p.score = combine(p.signals, self.s.weights, m.seconds)
        for c in p.clips:
            if c.origin != "ai":
                c.score = clip_score(p.score, c.start, c.end)

    def label(self, p: Project, words: int = 8) -> None:
        for c in p.clips:
            if not c.title:
                c.title = " ".join(p.transcript(c).split()[:words])

    def signal_names(self, p: Project) -> list[str]:
        names = [SIGNAL_LABELS[k] for k in p.signals if self.s.weights.get(k, 0) > 0]
        if p.mode_used == "ai":
            from .ai import model_for

            names.insert(0, f"AI ({p.ai_model or model_for(self.s)})")
        return names

    def overpick(self) -> int:
        """How many clips to find beyond Clips per video, for polish's ratings to choose from."""
        if not (self.s.ai_polish and self.s.polish_overpick and self.s.qc_min_rating):
            return 0
        return max(1, math.ceil(self.s.clip_count / 2))

    def keep_best(self, p: Project, count: int) -> int:
        """Keep the `count` best clips and switch the rest off (Find extra clips, keep the best the AI
        rates): ones the AI rated too low go first, then the lowest rated, then the lowest scored. A
        part series stays or goes whole. Returns how many were switched off."""
        clips = [c for c in p.clips if c.enabled]
        groups: dict[str, list[Clip]] = {}
        for c, (sid, _, _) in zip(clips, series.labels(clips)):
            groups.setdefault(sid or f"clip{id(c)}", []).append(c)
        floor = self.s.qc_min_rating

        def rank(group: list[Clip]) -> tuple:
            rated = [c.rating for c in group if c.rating]
            passes = all(not c.rating or not floor or c.rating >= floor for c in group)
            return (passes, min(rated) if rated else 0, max(c.score for c in group))

        kept = 0
        dropped: list[Clip] = []
        for group in sorted(groups.values(), key=rank, reverse=True):
            fits = kept + len(group) <= count and rank(group)[0]
            if fits:
                kept += len(group)
            else:
                dropped += group
        for c in dropped:
            c.enabled = False
        if dropped:
            low = sorted(c.rating for c in dropped if c.rating)
            self.log(f"Kept the {kept} best of {len(clips)} clips"
                     + (f"; left out {len(dropped)} the AI rated {low[0]}–{low[-1]}/10 or that didn't fit"
                        if low else f"; left out {len(dropped)}") + ".")
        return len(dropped)

    def prepare(self, p: Project) -> None:
        p.status = "working"
        self.open(p)
        self.measure(p)
        want, extra = self.s.clip_count, self.overpick()
        with timed(p, "find"):
            if extra:
                self.s, wanted = self.s.copy(clip_count=want + extra), self.s
                try:
                    self.find(p)
                finally:
                    self.s = wanted
            else:
                self.find(p)
        with timed(p, "transcript"):
            self.retype(p)
        if self.s.ai_polish:
            with timed(p, "polish"):
                self.polish(p)
        if extra:
            self.keep_best(p, want)
        p.status = "ready"

    # ------------------------------------------------------------ stage 4: export

    def export(self, p: Project, clips: list[Clip] | None = None) -> Path:
        check_before = p.timings.get("check", 0.0)
        start = time.monotonic()
        try:
            return self._export(p, clips)
        finally:  # the quality check inside it is timed on its own
            p.timings["export"] = (p.timings.get("export", 0.0) + time.monotonic() - start
                                   - (p.timings.get("check", 0.0) - check_before))

    def _export(self, p: Project, clips: list[Clip] | None = None) -> Path:
        v, media = p.video, p.media
        assert v is not None and media is not None
        clips = [c for c in (clips if clips is not None else p.clips) if c.enabled]
        self.retype(p, clips)  # clips added or moved since: their own words, before captions are made
        folder = video_dir(self.s.output_path, v, self.s.folder_template)
        thumbs = folder / "thumbnails"
        folder.mkdir(parents=True, exist_ok=True)
        if self.s.thumbnails:
            thumbs.mkdir(exist_ok=True)
        self._remove_previous_export(folder)
        p.qc = {}
        p.meta.pop("qc", None)

        base = self.s.copy(burn_captions=False) if p.captions_off else self.s
        pool = None
        if base.stack_enabled and base.stack_folder and pool_files(base.stack_folder):
            pool = self.stack_pool = self.stack_pool or StackPool()
        files: list[str] = []
        details: list[dict] = []
        made: list[tuple[Settings, bool]] = []  # what each clip was made with, to make one again
        for i, clip in enumerate(clips, 1):
            name = clip_filename(i, clip, folder, self.s.file_template)
            label = f"Exporting clip {i}/{len(clips)}"
            s, extra = base, {}
            second = pool.next(base.stack_folder) if pool else None
            if second:
                s = base.copy(stack_video=str(second))
                extra["second_video"] = second.name
            next_part = layout.has_next_part(clip, clips, self.s)  # a part 2 the AI rated too low will be rejected
            export_clip(v.path, media, folder / name, clip, p.words, s,
                        lambda f, i=i: self.progress(label, (i - 1 + f) / len(clips)), next_part=next_part)
            made.append((s, next_part))
            if self.s.tiktok_ready:
                for warning in tiktok_warnings(folder / name, clip.length):
                    self.log(f"TikTok: {warning}")
            if self.s.thumbnails:
                thumbnail(v.path, thumbs / f"{i:02d}.jpg", clip.start + clip.length / 2)
            if self.s.write_srt and p.words:
                text = srt(words_between(p.words, clip.start, clip.end), clip.start)
                Path(folder / name).with_suffix(".srt").write_text(
                    censor(text) if self.s.caption_censor else text, encoding="utf-8")
            files.append(name)
            details.append(extra)

        if self.s.qc_enabled and clips:
            with timed(p, "check"):
                self.check(p, folder, clips, files, details, base)
            self._unpromise(p, folder, clips, files, made)
        from .qc import REJECTED

        # Number the parts of each series from what will be posted (the check may have rejected some).
        for extra, label in zip(details, series.labels(clips, [not f.startswith(f"{REJECTED}/") for f in files])):
            extra.update(series.details(label))
        write_index(folder, v, clips, files, self.signal_names(p), p.mode_used or self.s.mode, p.meta, details)
        if not self.s.keep_source and not v.is_local:
            from .cleanup import remove_download

            remove_download(v.path)  # with its transcripts and measurements: nothing else uses them
        p.export_dir = folder
        self.log(f"Exported {len(files)} clips to {folder}")
        return folder

    def check(self, p: Project, folder: Path, clips: list[Clip], files: list[str], details: list[dict],
              s: Settings) -> None:
        """The quality check (qc.py): rejected clips move to _rejected and `files` is updated to match."""
        from . import qc

        _, w, h = export_frame(s, p.media)
        verdicts = []
        for i, (clip, name) in enumerate(zip(clips, files)):
            self.progress(f"Checking clip {i + 1}/{len(clips)}", i / len(clips))
            stats = qc.measure(folder / name)
            v = qc.merge(qc.judge_file(stats, 0 if s.fast_copy else clip.length, None if s.fast_copy else (w, h),
                                       p.media.has_audio),
                         qc.judge_content(clip, p.words, s.qc_min_rating))
            if not v.ok:
                files[i] = qc.reject(folder, name)
                self.log(f"Rejected {name}: {'; '.join(v.problems)}")
            details[i]["qc"] = v.to_json()
            verdicts.append(v)
        p.qc = p.meta["qc"] = qc.summary(verdicts)
        self.log(f"Quality check: {p.qc['passed']} ready, {p.qc['rejected']} rejected"
                 + (f", {p.qc['warned']} with warnings" if p.qc["warned"] else ""))

    def _unpromise(self, p: Project, folder: Path, clips: list[Clip], files: list[str],
                   made: list[tuple[Settings, bool]]) -> None:
        """A part 2 whose file the quality check rejected won't be posted, so each clip that promised it
        in its bottom text is made again without the promise."""
        from .qc import REJECTED

        kept = [i for i, f in enumerate(files) if not f.startswith(f"{REJECTED}/")]
        survivors = [clips[i] for i in kept]

        def bottom(i: int, next_part: bool) -> str:
            c = clips[i]
            return layout.bottom_banner(made[i][0], c.title, c.bottom_text, next_part, c.bottom_ai)

        again = [i for i in kept if made[i][1] and made[i][0].banner_bottom
                 and not layout.has_next_part(clips[i], survivors) and bottom(i, True) != bottom(i, False)]
        for n, i in enumerate(again, 1):
            export_clip(p.video.path, p.media, folder / files[i], clips[i], p.words, made[i][0],
                        lambda f, n=n: self.progress("Making a clip again", (n - 1 + f) / len(again)),
                        next_part=False)
            self.log(f"Made {files[i]} again without promising a part 2: its part 2 was rejected.")

    @staticmethod
    def _remove_previous_export(folder: Path) -> None:
        """Re-exports replace the previous set: delete only files our last index listed."""
        index = folder / "clips.json"
        if not index.exists():
            return
        try:
            old = json.loads(index.read_text(encoding="utf-8")).get("clips", [])
        except ValueError:
            return
        for rec in old:
            f = folder / rec["file"]
            f.unlink(missing_ok=True)
            f.with_suffix(".srt").unlink(missing_ok=True)
            (folder / "thumbnails" / f"{rec['rank']:02d}.jpg").unlink(missing_ok=True)

"""The pipeline shared by the GUI and CLI: open → measure → transcribe → find clips → export."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import transcribe as whisper
from .analyze import (SIGNAL_LABELS, Clip, Measurements, breathing_room, build_signals, chapter_clips, clip_score,
                      combine, fit_to_sentences, loudness_db, pick_clips, quiet_frames, snap, trim_dead_air)
from .download import Video, download, is_url, local_video
from .export import export_clip, thumbnail, tiktok_warnings
from .export import frame as export_frame
from .ffmpeg import MediaInfo, probe
from .organize import clip_filename, video_dir, write_index
from .scenes import detect_scenes
from .settings import Settings
from .stackpool import StackPool, pool_files
from .subtitles import (Cue, Word, censor, parse_vtt, parse_vtt_words, sentences, srt, text_between, to_dicts,
                        words_between)

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]


class Cancelled(Exception):
    pass


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
    transcript_source: str = ""
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

    @property
    def name(self) -> str:
        return self.video.title if self.video else self.source

    @property
    def cues(self) -> list[Cue]:
        return self.measurements.cues if self.measurements else []

    def transcript(self, clip: Clip) -> str:
        return text_between(self.cues, clip.start, clip.end)


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
        if is_url(p.source):
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
        db, cuts, scenes_done = None, [], False
        if cache.exists():
            try:
                data = np.load(cache)
                db, cuts, scenes_done = data["db"], list(data["cuts"]), bool(data["scenes_done"])
            except Exception:
                db = None
        if db is None:
            db = loudness_db(v.path, v.duration, lambda f: self.progress("Measuring audio", f))
        if self.s.detect_scenes and not scenes_done:
            cuts = detect_scenes(v.path, v.duration, lambda f: self.progress("Detecting scene cuts", f))
            scenes_done = True
        self.s.cache_path.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache, db=db, cuts=np.array(cuts, dtype=float), scenes_done=scenes_done)

        cues, words, source = self._transcript(p)
        p.words, p.transcript_source = words, source
        p.measurements = Measurements(duration=v.duration, db=db, heatmap=v.heatmap,
                                      scene_cuts=cuts if self.s.detect_scenes else [], cues=cues)

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
        cues, words = whisper.transcribe(v.path, self.s.whisper_model, self.s.whisper_language or v.language,
                                         v.duration, self.progress)
        cache.write_text(json.dumps({"cues": to_dicts(cues), "words": to_dicts(words)}), encoding="utf-8")
        return cues, words, "whisper"

    # ------------------------------------------------------------ stage 3: find clips

    def find(self, p: Project, use_ai: bool = True, strict: bool = False) -> None:
        """Score the video and pick clips. Cheap unless AI mode calls out to Claude.

        If the AI call fails, unattended runs fall back to smart signals (recording why in
        p.ai_error); with strict=True - the user explicitly asked for AI - the error is raised."""
        m, v = p.measurements, p.video
        assert m is not None and v is not None
        self.rescore(p)
        sents = sentences(p.words) if p.words else m.cues
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
                    v.title, v.duration, sents, p.score, self.s.clip_count, self.s.min_len, self.s.max_len,
                    self.s.ai_instructions, self.s.ai_api_key, self.s.ai_model, self.log,
                    check=lambda: self.progress("AI is picking clips", None))
                self.progress("AI is picking clips", None)  # a cancel during the call discards its result
                clips = self._fit(clips, sents, m, v.duration)
                if not clips:
                    raise AIError("None of Claude's clips were long enough once aligned to sentences.")
                p.clips, p.mode_used, p.ai_model = clips, "ai", model
                self.trim(p)
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
            clips = pick_clips(p.score, m.db, self.s.clip_count, self.s.min_len, self.s.max_len,
                               self.s.min_gap, self.s.quality_floor, self.s.snap_to_pauses and not align,
                               0.0 if align else self.s.pad_start, 0.0 if align else self.s.pad_end)
            if align:
                clips = self._fit(clips, sents, m, v.duration)
                for c in clips:
                    c.score = clip_score(p.score, c.start, c.end)
                clips.sort(key=lambda c: c.score, reverse=True)
            p.clips = clips
        p.mode_used = mode
        self.trim(p)
        self.label(p)

    def trim(self, p: Project) -> None:
        """Pull clip edges in to the speech (Trim dead air), leaving hand-edited clips alone."""
        if not (self.s.trim_silence and p.words):
            return
        floor = min(self.s.min_len, p.video.duration if p.video else self.s.min_len) * 0.75
        n = sum(trim_dead_air(c, p.words, max(0.25, self.s.pad_start), max(0.5, self.s.pad_end), min_len=floor)
                for c in p.clips if not c.edited)
        if n:
            self.log(f"Trimmed dead air from {n} clip{'s' if n != 1 else ''}.")

    # ------------------------------------------------------------ stage 3b: AI polish

    def polish(self, p: Project) -> None:
        """Hooks, caption fixes, speaker crops and a burned-in subtitle check from one Claude call.
        Never fails the video: on any error the clips go out as they are (reason in p.polish_error)."""
        from . import polish

        p.polished, p.polish_error, p.captions_off = False, "", False
        clips = [c for c in p.clips if c.enabled]
        if not clips or p.video is None:
            return
        self.progress("AI is polishing the clips", None)
        try:
            result = polish.ask(p.video.title, clips, p.words, p.video.path, self.s.ai_api_key,
                                self.s.ai_polish_model, self.log,
                                check=lambda: self.progress("AI is polishing the clips", None))
            self.progress("AI is polishing the clips", None)
        except Cancelled:
            raise
        except Exception as exc:
            p.polish_error = str(exc) or exc.__class__.__name__
            self.log(f"AI polish skipped: {p.polish_error}")
            return
        summary = polish.apply(result, clips, p.words, self.s.banner_top_text, self.s.banner_top)
        p.polished = True
        if result.burned_in_subtitles and self.s.polish_burned_subs == "off" and self.s.burn_captions:
            p.captions_off = True
            self.log("This video already has subtitles in the picture, so ClipMonster's captions are off for it.")
        summary["captions_off"] = p.captions_off
        p.meta["polish"] = summary
        self.log(f"Polished: {summary['hooks']} hooks, {summary['caption_fixes']} caption fixes, "
                 f"{summary['crops_moved']} crops moved.")

    def _fit(self, clips: list[Clip], sents: list[Cue], m: Measurements, duration: float) -> list[Clip]:
        """Align edges to sentences (or pauses without a transcript), add a breath of room,
        enforce the minimum length and drop overlaps - best-scoring clips win."""
        quiet = quiet_frames(m.db) if self.s.snap_to_pauses and not sents else np.zeros(0, bool)
        min_len = min(self.s.min_len, duration)
        kept: list[Clip] = []
        dropped = 0
        for c in sorted(clips, key=lambda c: c.score, reverse=True):
            if sents and self.s.snap_to_pauses:
                c.start, c.end = fit_to_sentences(c.start, c.end, sents, self.s.min_len, self.s.max_len)
                c.start, c.end = breathing_room(c.start, c.end, sents, 0.15, 0.3)
            elif len(quiet):
                c.start, c.end = snap(c.start, quiet, -1.5, 0.5), snap(c.end, quiet, -0.5, 1.5)
            # pad_start/pad_end are deliberate user choices, so they may reach into neighbours.
            c.start = max(0.0, c.start - self.s.pad_start)
            c.end = min(duration, c.end + self.s.pad_end)
            if c.length < min_len * 0.75 or any(c.start < k.end and c.end > k.start for k in kept):
                dropped += 1
                continue
            kept.append(c)
        if dropped:
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
            names.insert(0, f"AI ({p.ai_model or self.s.ai_model})")
        return names

    def prepare(self, p: Project) -> None:
        p.status = "working"
        self.open(p)
        self.measure(p)
        self.find(p)
        if self.s.ai_polish:
            self.polish(p)
        p.status = "ready"

    # ------------------------------------------------------------ stage 4: export

    def export(self, p: Project, clips: list[Clip] | None = None) -> Path:
        v, media = p.video, p.media
        assert v is not None and media is not None
        clips = [c for c in (clips if clips is not None else p.clips) if c.enabled]
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
        for i, clip in enumerate(clips, 1):
            name = clip_filename(i, clip, folder, self.s.file_template)
            label = f"Exporting clip {i}/{len(clips)}"
            s, extra = base, {}
            second = pool.next(base.stack_folder) if pool else None
            if second:
                s = base.copy(stack_video=str(second))
                extra["second_video"] = second.name
            export_clip(v.path, media, folder / name, clip, p.words, s,
                        lambda f, i=i: self.progress(label, (i - 1 + f) / len(clips)))
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
            self.check(p, folder, clips, files, details, base)
        write_index(folder, v, clips, files, self.signal_names(p), p.mode_used or self.s.mode, p.meta, details)
        if not self.s.keep_source and not v.is_local:
            v.path.unlink(missing_ok=True)
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

"""The edit plan for one clip: everything the pro editing changes, worked out before the export runs.

    cuts (audio/silence_remover.py)  ->  which stretches of the clip are kept, frame-exact
    the speaker (vision/face_tracker.py) and the camera (vision/camera.py)  ->  a crop window per frame
    zooms (camera.py)  ->  punch-ins on new sentences, a slow push-in on stories
    B-roll (vision/broll_selector.py)  ->  cutaways matched to the words
    words, bleeps, music, loudness  ->  captions and the sound chain (rendering/filtergraph.py)

When the picture changes (any of the first four), vision/reframe.py renders it to a file that stands in
for the source in export.export_clip; otherwise the export reads the source as before and only the
captions, cards and sound differ.

`make` never fails a clip on the way: anything that can't be done (no OpenCV, no face, no music) is left
out and said in `notes`, and the clip is made without it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .. import layout
from ..audio import ducking, silence_remover
from ..ffmpeg import MediaInfo, probe
from ..subtitles import Word, censor, words_between
from .filtergraph import AudioPlan

Progress = Callable[[str, "float | None"], None]
SLOW_CATEGORIES = ("Story", "Curiosity")


@dataclass
class EditPlan:
    start: float  # source seconds
    end: float
    spans: list[tuple[float, float]]  # kept source stretches (absolute)
    duration: float  # the edited clip's length
    words: list[Word]  # in edited-clip time
    fps: float
    audio: AudioPlan
    video: Path | None = None  # the rendered picture (no sound), when the picture changed
    still: Path | None = None  # a single rendered frame, for previews
    video_size: tuple[int, int] = (0, 0)
    cards_intro: bool = False
    cards_lower: bool = False
    channel: str = ""
    topic: str = ""
    category: str = ""
    tracked: bool = False
    notes: list[str] = field(default_factory=list)
    broll: list = field(default_factory=list)

    @property
    def cut_seconds(self) -> float:
        return round((self.end - self.start) - self.duration, 2)


def picture_changes(s) -> bool:
    return bool(s.track_speaker or s.punch_zoom or s.slow_zoom or s.cut_silences or s.cut_fillers
                or str(s.broll_folder or "").strip())


def active(s) -> bool:
    """Any of the pro editing is on (Settings → Editing, and the sound and caption options it brings)."""
    return bool(picture_changes(s) or s.master_audio or str(s.music_folder or "").strip() or s.bleep_audio
                or s.hook_intro or s.lower_third)


def target(s, media: MediaInfo) -> tuple[str, int, int]:
    """How the main video is shown: ("crop", w, h) when it's cropped to a box of that size, else
    ("full", 0, 0) when the whole picture is shown (blurred fill, fit, original)."""
    from ..export import frame

    aspect, w, h = frame(s, media)
    if layout.active(s):
        p = layout.plan(s, w, h)
        return ("crop", p.main.w, p.main.h) if p.fit == "crop" else ("full", 0, 0)
    if aspect in ("vertical_crop", "square"):
        return "crop", w, h
    return "full", 0, 0


def decode_size(media: MediaInfo) -> tuple[int, int]:
    """The size frames are read at: the source's own, but at most 1440 tall (4K sources are read smaller)."""
    sw, sh = media.width or 1920, media.height or 1080
    if sh > 1440:
        sw, sh = int(sw * 1440 / sh) // 2 * 2, 1440
    return sw // 2 * 2, sh // 2 * 2


def _fps(s, media: MediaInfo) -> float:
    from ..export import TIKTOK_MAX_FPS

    fps = media.fps or 30.0
    return min(fps, TIKTOK_MAX_FPS) if s.tiktok_ready else fps


def spans_for(clip, words: list[Word], s, fps: float) -> list[tuple[float, float]]:
    if not (s.cut_silences or s.cut_fillers) or not words:
        return silence_remover.keep_spans(clip.start, clip.end, [], fps)
    cuts = silence_remover.cuts_for(words, clip.start, clip.end, pauses=s.cut_silences, fillers=s.cut_fillers,
                                    longest=s.silence_longest, keep=s.silence_keep, filler_words=s.filler_words)
    return silence_remover.keep_spans(clip.start, clip.end, cuts, fps)


def predicted_length(clip, words: list[Word], s, fps: float = 30.0) -> float:
    """How long the clip will be once its pauses and fillers are cut."""
    return silence_remover.length(spans_for(clip, words, s, fps))


def bleeps_for(words: list[Word]) -> list[tuple[float, float]]:
    return [(w.start, w.end) for w in words if censor(w.text) != w.text]


def category_of(clip, words: list[Word]) -> str:
    if getattr(clip, "category", ""):
        return clip.category
    from ..intelligence.viral_heuristics import guess_category

    return guess_category(" ".join(w.text for w in words))


def make(src: Path, media: MediaInfo, clip, words: list[Word], s, work: Path, *, scene_cuts: list[float] = (),
         channel: str = "", topic: str = "", broll_used: dict | None = None, seed: str = "",
         progress: Progress | None = None, log: Callable[[str], None] | None = None,
         still_at: float | None = None, picture: bool = True) -> EditPlan:
    """Work out (and, when the picture changes, render) the edit of one clip. `still_at` (edited-clip
    seconds) renders just that frame, for a preview. `picture` False skips the render (tests, planning)."""
    log = log or (lambda m: None)
    progress = progress or (lambda m, f: None)
    from .. import vision

    fps = _fps(s, media)
    vision_ok = vision.available()
    wants_picture = picture_changes(s)
    if wants_picture and not vision_ok:
        log("Camera moves, cuts and B-roll need OpenCV, which isn't installed, so this clip is made without them.")
    spans = spans_for(clip, words, s, fps) if vision_ok else silence_remover.keep_spans(clip.start, clip.end, [], fps)
    clip_words = words_between(words, clip.start, clip.end)
    out_words = silence_remover.remap_words(clip_words, spans)
    duration = round(silence_remover.length(spans), 6)
    category = category_of(clip, out_words)
    notes: list[str] = []
    if len(spans) > 1:
        notes.append(f"cut {clip.length - duration:.1f} s of pauses and filler words")
    rel_spans = [(round(a - clip.start, 6), round(b - clip.start, 6)) for a, b in spans]
    bleeps = bleeps_for(out_words) if s.bleep_audio else []
    music = ducking.pick(s.music_folder, category, seed) if str(s.music_folder or "").strip() else None
    if str(s.music_folder or "").strip() and music is None:
        notes.append("no music: the music folder has no audio files")
    audio = AudioPlan(duration=duration, spans=rel_spans, bleeps=bleeps, bleep_tone=s.bleep_tone != "mute",
                      music=music, music_level_db=float(s.music_level_db), master=s.master_audio,
                      normalize=s.normalize_audio, fades=s.fades, rate=44100 if s.tiktok_ready else 48000)
    plan = EditPlan(clip.start, clip.end, spans, duration, out_words, fps, audio,
                    cards_intro=bool(s.hook_intro), cards_lower=bool(s.lower_third), channel=channel, topic=topic,
                    category=category, notes=notes)
    if not (wants_picture and vision_ok and picture):
        return plan
    _picture(plan, src, media, clip, words, s, work, scene_cuts, broll_used, seed, progress, log, still_at)
    return plan


def _picture(plan: EditPlan, src: Path, media: MediaInfo, clip, words, s, work: Path, scene_cuts, broll_used,
             seed: str, progress: Progress, log, still_at: float | None) -> None:
    from ..vision import broll_selector, camera, face_tracker, reframe

    mode, tw, th = target(s, media)
    sw, sh = decode_size(media)
    fps = plan.fps
    length = clip.end - clip.start
    times = np.arange(int(np.ceil(length * fps)) + 2) / fps
    cuts = [c - clip.start for c in scene_cuts if clip.start < c < clip.end]
    tracking = None
    if s.track_speaker:
        try:
            tracking = face_tracker.track(src, clip.start, length, words, (media.width or sw, media.height or sh),
                                          progress=lambda m, f: progress("Finding the speaker", f))
        except Exception as exc:  # no OpenCV model, an unreadable frame: a fixed framing
            log(f"Couldn't follow the speaker ({exc}); this clip keeps a fixed framing.")
    if tracking is not None and tracking.found:
        plan.tracked = True
        plan.notes.append(f"followed the speaker ({tracking.detector}, a face in {tracking.coverage:.0%} of it)")
        if mode == "full":  # a blurred or fitted format: with a speaker to follow, crop to them instead
            mode, tw, th = "crop", *_crop_box(s, media)
    elif s.track_speaker and tracking is not None:
        plan.notes.append("no speaker to follow (no face in most of it)")
    if mode == "crop":
        aspect = tw / th
        out_w, out_h = tw // 2 * 2, th // 2 * 2
    else:
        aspect = None
        k = min(1.0, 1920 / sw)
        out_w, out_h = int(sw * k) // 2 * 2, int(sh * k) // 2 * 2
    bw, _ = camera.base_window(sw, sh, aspect)
    home = ((bw / 2 + min(1.0, max(0.0, clip.crop_x)) * (sw - bw)) / sw, 0.5)
    if plan.tracked:
        xs, ys, jumps = camera.raw_targets(tracking.targets, times, home)
        cam_x, cam_y = camera.follow(xs, ys, jumps, times, cuts, fps, s.camera_smoothing, s.camera_deadband)
    else:
        cam_x, cam_y = np.full(len(times), home[0]), np.full(len(times), home[1])
    n_out = max(1, int(round(plan.duration * fps)) + 1)
    out_times = np.arange(n_out) / fps
    zoom = np.ones(n_out)
    starts = camera.sentence_starts(plan.words)
    if s.punch_zoom:
        zoom *= camera.punch_path(out_times, starts, float(s.punch_scale))
    if s.slow_zoom and plan.category in SLOW_CATEGORIES:
        gaps = []
        for a, b in zip(words_between(words, clip.start, clip.end), words_between(words, clip.start, clip.end)[1:]):
            if b.start - a.end >= camera.TOPIC_PAUSE:
                t = silence_remover.to_output(b.start, plan.spans)
                if t is not None:
                    gaps.append(t)
        out_cuts = [t for t in (silence_remover.to_output(c + clip.start, plan.spans) for c in cuts) if t is not None]
        zoom *= camera.slow_path(out_times, camera.topic_starts(plan.words, gaps, out_cuts), float(s.slow_zoom_scale))
        plan.notes.append("slow push-in")
    if s.punch_zoom:
        plan.notes.append("punch-ins on new sentences")

    def windows(n: int, t_src: float) -> camera.Window:
        k = min(len(cam_x) - 1, max(0, int(round((t_src - clip.start) * fps))))
        z = zoom[min(n, len(zoom) - 1)]
        return camera.window(cam_x[k], cam_y[k], z, sw, sh, aspect)

    inserts = []
    if str(s.broll_folder or "").strip():
        inserts = broll_selector.plan(plan.words, plan.duration, s.broll_folder, float(s.broll_every),
                                      float(s.broll_seconds), broll_used, seed=seed)
        lengths: dict[Path, float] = {}
        for ins in inserts:
            if ins.path not in lengths:
                try:
                    lengths[ins.path] = probe(ins.path).duration
                except Exception:
                    lengths[ins.path] = 0.0
            span = max(0.0, lengths[ins.path] - (ins.end - ins.start) - 0.3)
            h = int(hashlib.sha1(f"{seed}|{ins.path}|{ins.start:.2f}".encode()).hexdigest()[:8], 16)
            ins.offset = round((h % 1000) / 1000 * span, 2) if span else 0.0
        if inserts:
            plan.notes.append(f"{len(inserts)} B-roll cutaway{'s' if len(inserts) != 1 else ''}")
        elif not broll_selector.library(s.broll_folder):
            plan.notes.append("no B-roll: the B-roll folder has no videos")
    plan.broll = inserts
    job = reframe.Job(src, clip.start, length, fps, out_w, out_h, windows, plan.spans, inserts, (sw, sh))
    plan.video_size = (out_w, out_h)
    if still_at is not None:
        dest = work / "still.png"
        reframe.render(job, dest, frames_at=[min(max(0.0, still_at), max(0.0, plan.duration - 1 / fps))])
        plan.still = dest.with_name("still-0.png")
        return
    dest = work / "picture.mp4"
    reframe.render(job, dest, lambda f: progress("Editing the picture", f))
    plan.video = dest


def _crop_box(s, media: MediaInfo) -> tuple[int, int]:
    from ..export import frame

    _, w, h = frame(s, media)
    if layout.active(s):
        p = layout.plan(s, w, h)
        return p.main.w, p.main.h
    return w, h

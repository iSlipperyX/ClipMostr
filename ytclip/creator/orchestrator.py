"""The Video Creator from start to finish: prompt → script → voice → pictures → render → checks → posting queue.

    orchestrator = VideoCreatorOrchestrator()
    result = orchestrator.create(CreateRequest(
        prompt="3 stoic habits that will change your life",
        category="Self-Improvement",
        voice="en_male_deep",
        seconds=65,
        visuals="sdxl_turbo",  # or "local_broll", "comfyui", "texture", "auto"
    ))

Each creation gets its own folder, in its category's Automation folder when it's for posting:

    Videos/ClipMonster/Automation/<Category>/Created/2026-11-08 3 Stoic Habits [c-1a2b3c4d]/
        01 - 3 Stoic Habits That Will Change Your Life.mp4
        clips.json        what posting reads: title, caption, hashtags, category, account, AI-generated
        README.md         the script, the scenes and how each was made
        storyboard.json   the storyboard, to make it again or edit it
        thumbnails/01.jpg
        scenes/scene_01.png ...   the pictures (kept for reuse; posting only looks at videos)

Posting brings it in like automation's clips (posting/intake.py) and queues it for the account its category
posts to. The length is kept over a minute for Creator Rewards (62-85 s): the voice's speed is nudged
towards the target and, if it's still a touch short, the last scene holds a little longer with the music
(never padding words in). The checks before it's handed over: 1080x1920, over 61 s, sound present,
loudness within a dB of -14 LUFS with true peaks under -1 dBTP, the size TikTok takes, and a script not
too close to one made before. jobs.db records it as a GENERATIVE_CREATOR job.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .. import categories as cats
from ..ffmpeg import Cancelled, discard, probe
from ..organize import safe_name
from ..settings import Settings, work_folder
from . import (compositor, director, factcheck, motion, originality, sfx_library, soundbed, visual_generator,
               voice_engine, web)

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]

PERMISSION = "Made by ClipMonster's Video Creator (original: script, voice, pictures and sound made on this PC)"
REWARDS_MIN = 61.5  # the finished video's least length for Creator Rewards (TikTok counts "over 1 minute")
HOLD_MAX = 4.0  # seconds the last scene may hold on with the music to get there
SPEED_STEP = 0.04  # re-read the voice when its length misses by more than this share
TIKTOK_BYTES = 287_600_000


class CreatorError(RuntimeError):
    """A plain-English reason a video couldn't be made."""


@dataclass
class CreateRequest:
    prompt: str = ""
    script: str = ""  # your own narration instead of the AI's (blank lines start new scenes)
    title: str = ""
    category: str = ""  # a category's id or name ("" = none)
    voice: str = ""  # voice_engine profile ("" = Settings → Creator)
    seconds: float = 0.0  # 0 = Settings → Creator → Video length
    visuals: str = ""  # visual_generator.BACKENDS ("" = Settings → Creator)
    style: str = ""  # director.STYLES ("" = Settings, else from the category)
    rewards: bool | None = None  # keep it 62-85 s (None = Settings)
    storyboard: dict | None = None  # an (edited) storyboard to make as it is
    post: bool = True  # into the Automation folder (queued for posting); False = Output/Created
    search: str = ""  # what to look up online ("" = from the prompt); the schedule's trending topics set it

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "CreateRequest":
        data = json.loads(text or "{}")
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def label(self) -> str:
        what = self.title or self.prompt or (self.script.strip().split("\n", 1)[0] if self.script else "")
        return " ".join(what.split())[:80] or "A new video"


@dataclass
class CreationResult:
    id: str
    folder: Path
    video: Path
    storyboard: director.Storyboard
    duration: float
    checks: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    category: str = ""
    account: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.checks.get("ok", True))


def new_id() -> str:
    return "c-" + uuid.uuid4().hex[:8]


def find_category(s: Settings, text: str) -> cats.Category | None:
    """A category by id or (case-insensitive) name."""
    if not text:
        return None
    found = cats.find(s, text)
    if found:
        return found
    low = text.strip().lower()
    return next((c for c in cats.load(s) if c.name.strip().lower() == low or c.folder.lower() == low), None)


def output_folder(s: Settings, category: cats.Category | None, category_name: str, title: str, cid: str,
                  post: bool) -> Path:
    if post:
        base = s.auto_output_path / (category.folder if category else cats.folder_name(category_name or "Created"))
    else:
        base = s.output_path / "Created"
    day = time.strftime("%Y-%m-%d")
    root_len = len(str(base.resolve())) + len("/Created/")
    room = max(12, 200 - root_len - 60)
    name = f"{day} {safe_name(title, min(50, room))} [{cid}]"
    return (base / "Created" / name) if post else (base / name)


class VideoCreatorOrchestrator:
    """Runs a creation. `cancelled()` returning True stops it at the next step (raises Cancelled)."""

    permission = PERMISSION  # how the video was made, kept with it for posting

    def __init__(self, settings: Settings | None = None, log: Log | None = None, progress: Progress | None = None,
                 cancelled: Callable[[], bool] | None = None, register: bool = True, db=None):
        self.s = settings or Settings.load()
        self._log = log or (lambda m: None)
        self._progress = progress or (lambda m, f: None)
        self._cancelled = cancelled or (lambda: False)
        self.register = register
        self.db = db
        self.timings: dict[str, float] = {}

    # ------------------------------------------------------------ helpers
    def log(self, msg: str) -> None:
        self._log(msg)

    def check(self) -> None:
        if self._cancelled():
            raise Cancelled()

    def stage(self, prefix: str) -> Progress:
        """Progress for one step: messages that don't start with the step's own words get them in front,
        so the status bar always knows which step it's on."""
        def report(msg: str, frac: float | None = None) -> None:
            self._progress(msg if msg.startswith(prefix) else f"{prefix}: {msg}", frac)
        return report

    def _timed(self, key: str, started: float) -> None:
        self.timings[key] = round(self.timings.get(key, 0.0) + time.monotonic() - started, 1)

    # ------------------------------------------------------------ steps
    def target(self, req: CreateRequest) -> tuple[float, bool]:
        """(seconds to aim for, keep it over a minute)."""
        rewards = self.s.creator_rewards_length if req.rewards is None else bool(req.rewards)
        seconds = float(req.seconds or self.s.creator_duration or 65)
        if rewards:
            seconds = min(max(seconds, director.REWARDS_RANGE[0] + 2), director.REWARDS_RANGE[1] - 5)
        return seconds, rewards

    def storyboard(self, req: CreateRequest) -> director.Storyboard:
        """Step 1: the script and storyboard (the AI's, your script's, or the one handed in)."""
        started = time.monotonic()
        self.check()
        category = find_category(self.s, req.category)
        cat_name = category.name if category else req.category
        seconds, _ = self.target(req)
        style = req.style or self.s.creator_style or director.style_for(
            cat_name, req.prompt, category.instructions if category else "")
        report = self.stage("Writing the script")
        report("Writing the script", None)
        if req.storyboard:
            given = director.Storyboard.from_json(req.storyboard)
            board = director.normalise(given, director.get_style(style), hook_words=40,
                                       max_scenes=self.scene_limit(len(given.scenes)))
            board.written_by = board.written_by or "you (edited)"
            self.say_cut(board)
        elif req.script.strip():
            board = director.from_script(req.script, title=req.title, category=cat_name, style=style,
                                         seconds=seconds, prompt=req.prompt,
                                         max_scenes=self.scene_limit(len(req.script.split()) // 3 + 1))
            self.log(f"Using your script: {len(board.scenes)} scenes, {board.word_count} words.")
            self.say_cut(board)
        else:
            if not req.prompt.strip():
                raise CreatorError("Say what the video is about, or paste a script.")
            voice = voice_engine.profile(req.voice or self.s.creator_voice)
            engines = voice_engine.pick_engines(self.s)
            wpm = voice_engine.learned_wpm(voice.key, engines[0] if engines else "kokoro")
            found = None
            if getattr(self.s, "creator_web", True):
                report("Writing the script: looking it up online", None)
                found = web.gather(req.prompt, log=self.log, check=self.check,
                                   **({"query": req.search} if req.search.strip() else {}))
            brief = director.Brief(prompt=req.prompt, category=cat_name or "",
                                   instructions=category.instructions if category else "", seconds=seconds,
                                   wpm=wpm, style=style, avoid=originality.earlier_titles(req.prompt),
                                   facts=web.brief(found))
            brief.lessons, brief.wpm, brief.scene_seconds = self.lessons(req, category, wpm)
            entries = originality.load()
            try:
                board = director.write(self.s, brief, log=self.log, check=self.check, progress=report,
                                       similar=lambda b: originality.closest(b.script, entries))
            except director.DirectorError as exc:
                raise CreatorError(str(exc)) from exc
            board.sources = [src.short() for src in found.sources] if found else []
            mode = getattr(self.s, "creator_factcheck", "fix")
            if mode != "off":
                report("Writing the script: checking the facts", None)
                checked = factcheck.check(self.s, board, brief.facts, have_sources=bool(found and found.found),
                                          mode=mode, log=self.log, check_cancel=self.check, progress=report)
                board.factcheck = checked.to_json()
        if req.title:
            board.title = req.title
        board.category = cat_name or board.category
        board.prompt = board.prompt or req.prompt
        self._timed("script", started)
        report("Writing the script", 1.0)
        return board

    def lessons(self, req: CreateRequest, category, wpm: float) -> tuple[str, float, float]:
        """(what earlier videos taught, as text for the writer; the pace to write for; seconds a scene) from how
        your posted videos did (learn.py, intelligence/learner.py). ("", `wpm`, 0) before there are results."""
        from .. import learn
        from ..intelligence import learner

        lessons = learn.for_settings(self.s)
        if lessons is None or lessons.empty:
            return "", wpm, 0.0
        cid = category.id if category else ""
        few = learner.guide(self.s, req.prompt, category_id=cid, sport=category.sport if category else "",
                            purpose="script")
        text = few or lessons.creator_text(cid)
        if text:
            n = sum(1 for o in lessons.outcomes if o.creator)
            self.log(f"Learning from how your earlier videos did ({n} Creator video{'s' if n != 1 else ''}"
                     f" with results" + (", and your clips' hooks)." if "your TikTok clips" in text
                                          else "; the openings closest to this one)." if few else ")."))
        scene_seconds = 0.0
        pace = learner.calibrate(lessons.outcomes, cid, default_wpm=wpm)
        if pace.wpm:
            # The voice is sped up or slowed to land on the length (0.8-1.2x): aim where it can reach.
            wpm = round(max(wpm * 0.85, min(wpm * 1.15, pace.wpm)), 1)
            scene_seconds = pace.scene_seconds or 0.0
            self.log(pace.note)
        return text, wpm, scene_seconds

    def scene_limit(self, given: int) -> int:
        """The most scenes a storyboard or script handed in may have (`given`: how many it has, or could)."""
        return director.MAX_SCENES + 4

    def say_cut(self, board: director.Storyboard) -> None:
        for note in board.notes:
            if note.startswith("kept the first"):
                self.log(f"This is longer than one video takes: {note}. Make the rest as a second video"
                         + ("." if type(self).scene_limit is not VideoCreatorOrchestrator.scene_limit
                            else ", or make it with the Studio, which takes long videos."))

    def voice(self, board: director.Storyboard, req: CreateRequest, work: Path, seconds: float,
              rewards: bool) -> voice_engine.VoiceTrack:
        """Step 2: the narration, at a speed that lands it on the target length."""
        started = time.monotonic()
        report = self.stage("Recording the voice")
        voice = req.voice or self.s.creator_voice
        try:
            track = voice_engine.synthesize(board.scenes, self.s, work / "voice.wav", voice=voice, log=self.log,
                                            progress=report, check=self.check)
            speed = self.voice_speed(track, seconds, rewards)
            if speed != 1.0:
                how = "fitting it" if track.engine_key in voice_engine.CLOUD_ENGINES else "reading it again"
                self.log(f"The voice ran {track.duration:.0f} s for a {seconds:.0f} s target: {how} "
                         f"{'slower' if speed < 1 else 'faster'} ({speed:.2f}x).")
                track = self.reread(board, track, voice, speed, work, report)
        except voice_engine.VoiceError as exc:
            raise CreatorError(str(exc)) from exc
        voice_engine.remember_wpm(voice, track.wpm, track.speed, track.engine_key)
        self.check()
        track = voice_engine.time_words(track, self.s, self.log, self.stage("Lining up the words"))
        self.log(f"Voice: {track.voice} ({track.engine}), {track.speech_seconds:.1f} s of speech, "
                 f"{track.wpm:.0f} words a minute; word times {track.timing}.")
        self._timed("voice", started)
        return track

    def voice_speed(self, track: voice_engine.VoiceTrack, seconds: float, rewards: bool) -> float:
        """How much faster (over 1) or slower the narration must be read to land on `seconds`; 1.0 when it's
        close enough as it is."""
        # How far off the length is: aim the speech (not the breath and tail) at the target.
        fixed = track.duration - track.speech_seconds + voice_engine.LEAD_IN
        want_speech = max(5.0, seconds - fixed)
        have = track.speech_seconds
        factor = have / want_speech
        if rewards and track.duration < REWARDS_MIN:
            factor = min(factor, have / max(5.0, REWARDS_MIN + 0.8 - fixed))
        return max(0.8, min(1.2, factor)) if abs(factor - 1) > SPEED_STEP else 1.0

    def reread(self, board: director.Storyboard, track: voice_engine.VoiceTrack, voice: str, speed: float,
               work: Path, report: Progress) -> voice_engine.VoiceTrack:
        """The narration again at `speed`, by the engine that read it. A cloud engine (ElevenLabs) charges for
        every character it reads, so its recording is fitted to the length instead (same pitch), not paid
        for twice."""
        if track.engine_key in voice_engine.CLOUD_ENGINES:
            from ..studio import voice_studio  # lazy: the Studio's modules import this one

            self.log(f"{track.engine} isn't asked to read it again: the recording is fitted instead.")
            return voice_studio.retime(track, speed, work / "voice_fitted.wav")
        return voice_engine.synthesize(board.scenes, self.s, work / "voice.wav", voice=voice, log=self.log,
                                       progress=report, check=self.check, speed=speed, engines=[track.engine_key])

    def create(self, req: CreateRequest, board: director.Storyboard | None = None) -> CreationResult:
        """Make the video. `board`: a storyboard already written (the Creator tab's preview)."""
        cid = new_id()
        self.timings = {}
        board = board or self.storyboard(req)
        return self.render(board, req, cid)

    def render(self, board: director.Storyboard, req: CreateRequest, cid: str | None = None) -> CreationResult:
        """Steps 2-5 for a written storyboard."""
        cid = cid or new_id()
        s = self.s
        category = find_category(s, req.category)
        cat_name = category.name if category else (req.category or board.category)
        style = director.get_style(req.style or s.creator_style or director.style_for(
            cat_name, board.prompt, category.instructions if category else ""))
        seconds, rewards = self.target(req)
        warnings: list[str] = []
        with work_folder("creator-") as tmp:
            work = Path(tmp)
            # 2. the voice
            track = self.voice(board, req, work, seconds, rewards)
            duration = track.duration
            if rewards and duration < REWARDS_MIN:
                hold = min(HOLD_MAX, REWARDS_MIN + 0.3 - duration)
                if hold > 0:
                    duration += hold
                    self.log(f"The last scene holds {hold:.1f} s longer with the music, to keep it over a minute.")
            if rewards and duration < REWARDS_MIN:
                warnings.append(f"It's {duration:.0f} s, under the minute Creator Rewards needs: the script is too "
                                "short for this voice. Ask for a longer video or a slower voice.")
            # 3. the pictures
            started = time.monotonic()
            self.check()
            folder = output_folder(s, category, cat_name, board.title, cid, req.post)
            pictures = work / "scenes"  # moved in with the video at the end
            pictures.mkdir()
            visuals = visual_generator.make_all(board, s, pictures, choice=req.visuals or s.creator_visuals,
                                                style=style, log=self.log, progress=self.stage("Making pictures"),
                                                check=self.check)
            self._timed("visuals", started)
            # 4. render: scenes, sound, captions, the whole
            started = time.monotonic()
            video = self._render(board, track, visuals, duration, work, style)
            self._timed("render", started)
            # 5. checks, then hand it over
            started = time.monotonic()
            report = self.stage("Checking the video")
            report("Checking the video", None)
            checks = self.checks(video, duration, rewards, board, warnings)
            self._timed("check", started)
            result = self._deliver(cid, board, req, video, folder, pictures, visuals, track, checks, warnings,
                                   category, cat_name, style, duration)
        report("Checking the video", 1.0)
        return result

    # ------------------------------------------------------------ rendering
    def _render(self, board, track: voice_engine.VoiceTrack, visuals, duration: float, work: Path, style) -> Path:
        from ..captions import build_ass
        from ..rendering import ass_builder, cards

        s = self.s
        starts = [sc.start for sc in track.scenes]
        timeline = compositor.make_timeline(starts, duration, s.creator_transition,
                                            [sc.sfx_cue for sc in board.scenes])
        spans = timeline.spans
        jobs = [compositor.SceneJob(vis.path, vis.kind, b - a, work / f"scene_{i + 1:02d}.mp4",
                                    motion.get(sc.camera_motion, s.creator_motion))
                for i, ((a, b), vis, sc) in enumerate(zip(spans, visuals, board.scenes))]
        workers = compositor.scene_workers(s, len(jobs))
        encoder = compositor.scene_encoder(s)
        started = time.monotonic()
        clips, used = compositor.render_scenes(jobs, workers=workers, encoder=encoder,
                                               progress=self.stage("Animating scene"), check=self.check, log=self.log)
        where = "the processor (x264)" if used == "libx264" else f"the graphics card ({used})"
        self.log(f"Animated {len(jobs)} scene{'s' if len(jobs) != 1 else ''} in {time.monotonic() - started:.0f} s"
                 + (f", {workers} at a time" if workers > 1 else "") + f", encoded on {where}.")
        self.render_info = {"scene_workers": workers, "scene_encoder": used}
        # The sound
        mix_report = self.stage("Mixing the sound")
        mix_report("Mixing the sound", None)
        events = sfx_library.plan(board.scenes, starts, timeline.bounds, folder=s.creator_sfx_folder,
                                  auto_whoosh=s.creator_auto_whoosh, level_db=s.creator_sfx_db, seed=board.title,
                                  duration=duration) if s.creator_sfx else []
        music, music_note = soundbed.music_for(s, board.music_mood, duration, work, board.title)
        mi = compositor.MixInputs(track.path, duration, music, s.creator_music_db, events)
        self.mix = compositor.mix_audio(mi, work / "mix.wav", mix_report)
        self.mix |= {"music": music_note, "effects": [f"{e.cue} at {e.time:.1f} s" for e in events]}
        # Captions and the hook card
        cs = s.apply({"caption_style": "kinetic", "burn_captions": True, "title_overlay": False,
                      "caption_font": s.creator_caption_font, "caption_size": s.creator_caption_size,
                      "caption_uppercase": s.creator_uppercase, "caption_highlight": style.highlight,
                      "caption_words": 3, "caption_censor": True, "intro_seconds": 2.2})
        emphasis = {ass_builder.bare(w) for sc in board.scenes for w in sc.emphasis if w}
        (work / "captions.ass").write_text(build_ass(track.words, 0.0, duration, compositor.WIDTH, compositor.HEIGHT,
                                                     cs, emphasis=emphasis, emphasis_colour=style.emphasis),
                                           encoding="utf-8")
        has_cards = False
        self.hook_card = None
        if s.creator_hook_card and hook_adds(board):
            from ..analyze import Clip

            card_clip = Clip(0, duration, 0, board.title, hook=board.hook)
            text = cards.build(compositor.WIDTH, compositor.HEIGHT, cs, card_clip, intro=True, lower=False,
                               duration=duration)
            if text:
                (work / "cards.ass").write_text(text, encoding="utf-8")
                has_cards = True
                self.hook_card = cards.complete_at(card_clip, cs)  # the TikTok cover (posting/cover.py)
        self.timeline = timeline
        out = work / "final.mp4"
        put = self.stage("Putting the video together")
        try:
            compositor.assemble(clips, timeline, work / "mix.wav", out, work, cs, captions=True, cards=has_cards,
                                progress=lambda f: (self.check(), put("Putting the video together", f)))
        finally:  # the scene clips and the mix are in the video now (or of no use): free their space at once
            discard(*clips, work / "mix.wav")
        return out

    # ------------------------------------------------------------ checks
    def checks(self, video: Path, duration: float, rewards: bool, board, warnings: list[str],
               size: tuple[int, int] = (compositor.WIDTH, compositor.HEIGHT)) -> dict:
        info = probe(video)
        loud = compositor.ebur128(video)
        problems: list[str] = []
        if (info.width, info.height) != tuple(size):
            problems.append(f"It came out {info.width}x{info.height}, not {size[0]}x{size[1]}.")
        if not info.has_audio:
            problems.append("It has no sound.")
        if info.duration < 3:
            problems.append("It's almost empty.")
        if rewards and info.duration < 61.0:
            warnings.append(f"It's {info.duration:.1f} s: Creator Rewards needs over a minute.")
        lufs, tp = loud.get("lufs"), loud.get("true_peak")
        if lufs is not None and abs(lufs - (-14.0)) > 1.0:
            warnings.append(f"Its loudness is {lufs:.1f} LUFS (aimed for -14).")
        if tp is not None and tp > -0.5:
            warnings.append(f"Its peaks reach {tp:.1f} dBTP (aimed for under -1).")
        size = video.stat().st_size
        if size > TIKTOK_BYTES:
            warnings.append(f"It's {size / 1e6:.0f} MB: upload it from a computer (the phone app takes 287 MB).")
        score, like = originality.closest(board.script)
        if score >= director.ORIGINALITY_LIMIT:
            warnings.append(f"Its script shares {score:.0%} of its wording with \"{like}\".")
        if problems:
            raise CreatorError("The finished video failed its check: " + " ".join(problems))
        return {"ok": True, "problems": [], "warnings": list(warnings), "width": info.width, "height": info.height,
                "length": round(info.duration, 2), "lufs": lufs, "true_peak": tp, "lra": loud.get("lra"),
                "size_mb": round(size / 1e6, 1), "originality": round(1 - score, 3),
                "creator_rewards": info.duration >= 61.0}

    # ------------------------------------------------------------ handing over
    def _deliver(self, cid, board, req: CreateRequest, video: Path, folder: Path, pictures: Path, visuals, track,
                 checks: dict, warnings: list[str], category, cat_name: str, style, duration: float) -> CreationResult:
        from ..download import Video
        from ..export import thumbnail
        from ..analyze import Clip
        from ..organize import clip_filename, write_index

        folder.mkdir(parents=True, exist_ok=True)
        clip = Clip(0.0, round(checks.get("length") or duration, 2), 100.0, board.title, origin="creator",
                    reason=board.caption, hook=board.hook)
        name = clip_filename(1, clip, folder, "{rank:02d} - {label}")
        (folder / "thumbnails").mkdir(exist_ok=True)
        try:
            thumbnail(video, folder / "thumbnails" / "01.jpg", min(2.6, duration / 2))
        except Exception:  # noqa: BLE001 - a missing thumbnail never stops a video
            pass
        if getattr(self.s, "creator_keep_scenes", True):
            dest = folder / "scenes"
            dest.mkdir(exist_ok=True)
            for v in visuals:
                if v.kind == "image" and Path(v.path).parent == pictures:
                    shutil.move(str(v.path), str(dest / Path(v.path).name))
        tags = " ".join(f"#{t}" for t in board.hashtags)
        caption = f"{board.caption} {tags}".strip()
        account = (category.account if category else "") or ""
        auto = {"permission": self.permission, "origins": ["creator"], "how": "creator", "recipe": "Video Creator",
                "picked_at": time.time(), "category": cat_name or "", "category_id": category.id if category else "",
                "account": account, "account_id": category.account_id if category else ""}
        creator = {
            "id": cid, "prompt": board.prompt, "written_by": board.written_by, "style": style.key,
            "voice": track.voice, "voice_engine": track.engine, "voice_speed": round(track.speed, 3),
            "word_timing": track.timing, "music_mood": board.music_mood,
            "pictures": sorted({visual_generator.BACKEND_NAMES.get(v.source, v.source) for v in visuals}),
            "mix": getattr(self, "mix", {}), "timeline": compositor.describe(getattr(self, "timeline", None))
            if getattr(self, "timeline", None) else {}, "timings": dict(self.timings),
            "render": getattr(self, "render_info", {}),
            "ai_generated": True, "caption": caption, "hashtags": board.hashtags, "notes": board.notes,
            "sources": board.sources, "script": board.script,
            "factcheck": {k: v for k, v in (board.factcheck or {}).items() if k != "claims"} |
            ({"fixed": sum(1 for c in board.factcheck.get("claims", []) if c.get("changed")),
              "to_check": sum(1 for c in board.factcheck.get("claims", [])
                              if c.get("verdict") != "supported" and not c.get("changed"))}
             if board.factcheck else {}),
        } | self.creator_extra()
        video_meta = Video(id=cid, title=board.title, channel="ClipMonster Video Creator", upload_date=time.strftime("%Y%m%d"),
                           duration=checks.get("length") or duration, url="", path=folder / name)
        details = [{"qc": checks, "ai_generated": True, "caption": board.caption, "hashtags": board.hashtags,
                    "scenes": len(board.scenes),
                    **({"hook_card": self.hook_card} if getattr(self, "hook_card", None) else {})}]
        write_index(folder, video_meta, [clip], [name], ["Video Creator"], "creator",
                    extra={"automation": auto, "creator": creator}, details=details)
        (folder / "storyboard.json").write_text(board.dumps(), encoding="utf-8")
        (folder / "README.md").write_text(self.readme_text(board, creator, checks, name), encoding="utf-8")
        # The video goes in last, under a name posting ignores until it's whole.
        part = folder / (name + ".part")
        shutil.move(str(video), str(part))
        os.replace(part, folder / name)
        final = folder / name
        originality.remember(cid, board.title, board.prompt, board.script)
        result = CreationResult(cid, folder, final, board, checks.get("length") or duration, checks, warnings,
                                dict(self.timings), cat_name or "", account)
        if self.register:
            self._register(result, req)
        self.log(f"✓ Made \"{board.title}\" ({result.duration:.0f} s) → {final}")
        for w in warnings:
            self.log(f"⚠ {w}")
        if req.post and category is None:
            self.log("It's in the Automation folder without a category: posting queues it if you have one account, "
                     "else it waits in the Library for you to pick one.")
        elif req.post and category is not None and not category.account_id:
            self.log(f"{category.name} has no TikTok account yet, so it waits in the Library until you link one "
                     "(Categories tab).")
        return result

    def creator_extra(self) -> dict:
        """More for clips.json's "creator" block (the Studio adds its own)."""
        return {}

    def readme_text(self, board: director.Storyboard, creator: dict, checks: dict, file: str) -> str:
        return readme(board, creator, checks, file)

    def _register(self, result: CreationResult, req: CreateRequest) -> None:
        """A done GENERATIVE_CREATOR job in jobs.db, so the Automation tab lists it with the clips."""
        from ..jobs import Job, JobDB, JobType

        db = self.db
        own = db is None
        try:
            db = db or JobDB()
            category = find_category(self.s, req.category)
            db.add(Job(id=f"create-{result.id}", url=f"creator:{result.id}", title=result.storyboard.title,
                       channel="Video Creator", permission=self.permission, origins=["creator"], how="creator",
                       status="done", stage="Done", folder=str(result.folder), clips=1, recipe="Video Creator",
                       category_id=category.id if category else "", kind=JobType.GENERATIVE_CREATOR.value,
                       request=req.to_json()))
        except Exception as exc:  # noqa: BLE001 - the video is made either way
            self.log(f"Couldn't note it in the automation history: {exc}")
        finally:
            if own and db is not None:
                db.close()


def queue(db, req: CreateRequest, category_id: str = "", now: float | None = None,
          origins: list[str] | None = None, due: float = 0.0) -> str:
    """Put a video in automation's queue for the Video Creator to make (a GENERATIVE_CREATOR job in jobs.db),
    made once `due` (a time) has come. `origins` says who asked: ["creator"] by hand, ["creator", "schedule"]
    the daily schedule, ["creator", "telegram"] a message. Returns the job's id."""
    from ..jobs import Job, JobType

    cid = new_id()
    job_id = f"create-{cid}"
    db.add(Job(id=job_id, url=f"creator:{cid}", title=req.label(), channel="Video Creator", permission=PERMISSION,
               origins=list(origins or ["creator"]), how="creator", stage="Waiting", category_id=category_id,
               kind=JobType.GENERATIVE_CREATOR.value, request=req.to_json(), next_try=due), now or time.time())
    return job_id


def hook_adds(board: director.Storyboard) -> bool:
    """The hook card is worth showing: there's a hook and it isn't just the first words the voice says (the
    captions show those already)."""
    hook = [w for w in director.clean_speech(board.hook).lower().split()]
    first = director.clean_speech(board.scenes[0].speech_text).lower().split() if board.scenes else []
    bare = lambda ws: [w.strip(".,!?;:'\"") for w in ws]  # noqa: E731
    return bool(hook) and bare(hook) != bare(first[:len(hook)])


def readme(board: director.Storyboard, creator: dict, checks: dict, file: str) -> str:
    lines = [f"# {board.title}", "",
             "**Made by:** ClipMonster's Video Creator  ",
             f"**Prompt:** {board.prompt or '—'}  ",
             f"**Script by:** {board.written_by or '—'}  ",
             f"**Voice:** {creator.get('voice')} ({creator.get('voice_engine')})  ",
             f"**Pictures:** {', '.join(creator.get('pictures') or [])}  ",
             f"**Music:** {(creator.get('mix') or {}).get('music', '—')}  ",
             f"**Length:** {checks.get('length', 0):.1f} s · **Loudness:** {checks.get('lufs')} LUFS, "
             f"peaks {checks.get('true_peak')} dBTP  ",
             f"**Caption:** {creator.get('caption', '')}  ",
             "**Labelled AI-generated when posted.**", "",
             f"[▶ {file}](<{file}>)", "", "| # | Narration | Picture | Camera | Sound |", "|---|---|---|---|---|"]
    for sc in board.scenes:
        lines.append(f"| {sc.scene_id} | {sc.speech_text} | {sc.visual_prompt[:90]} | {sc.camera_motion} | "
                     f"{'' if sc.sfx_cue == 'none' else sc.sfx_cue} |")
    if board.sources:
        lines += ["", "## Sources looked up when it was written", ""]
        for s in board.sources:
            when = f", {s['date'][:10]}" if s.get("date") else ""
            who = f" ({s['publisher']}{when})" if s.get("publisher") or when else ""
            lines.append(f"- [{s.get('title', '')}](<{s.get('url', '')}>){who}" if s.get("url") else
                         f"- {s.get('title', '')}{who}")
    fc = factcheck.readme_section(board)
    if fc:
        lines.append(fc.rstrip("\n"))
    for w in checks.get("warnings") or []:
        lines.append(f"\n⚠ {w}")
    return "\n".join(lines) + "\n"

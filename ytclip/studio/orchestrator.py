"""The Studio from start to finish: a prompt or an article's address in, a finished video and its editable
timeline out.

    studio = StudioOrchestrator()
    result = studio.create(StudioRequest(url="https://example.org/a-public-article", seconds=60))
    result = studio.create(StudioRequest(prompt="How lighthouses worked before electricity",
                                         format="long", seconds=480, captions="karaoke"))
    result.video, result.folder, result.checks

It is the Video Creator's orchestrator (creator/orchestrator.py) with the middle replaced, so everything
around a creation is the same: categories and their accounts, the Automation folder posting reads, clips.json,
the originality and fact checks, what earlier videos taught, the jobs list, review before posting.

The run is a small state machine; `state` says where it is and `history` how long each step took:

    INPUT     the request checked (one of: prompt, address, your script, a storyboard) and its shape chosen:
              short = vertical 9:16, long = wide 16:9, or the shape asked for
    SCRIPT    an address is read (studio/scraper.py: public pages only) and the script written from it in new
              words, or from the prompt with today's facts looked up (studio/scriptwriter.py); fact-checked
    VOICE     the narration and its word times (studio/voice_studio.py), fitted to the length
    VISUALS   a picture or clip for each scene (studio/visual_sourcer.py: your B-roll, stock sites, drawn here)
    TIMELINE  everything laid out as a timeline (studio/timeline_builder.py) and written as timeline.json
    RENDER    the timeline rendered in one ffmpeg run (editor/timeline_compiler.py)
    CHECK     the finished file measured: size, length, sound, loudness, wording close to an earlier video
    DELIVER   the video's folder: the video, clips.json, README.md, storyboard.json, timeline.json and the
              files it uses, so render_timeline() can make it again after you edit the timeline
    DONE      (or FAILED, with `failed_at` saying in which step)

A step can only follow the one before it; cancelling stops at the next step's start or inside a long one.

The video's folder:

    2026-11-10 How Lighthouses Worked [c-1a2b3c4d]/
        01 - How Lighthouses Worked.mp4
        clips.json  README.md  storyboard.json  thumbnails/01.jpg
        timeline.json             the edit, as plain JSON (studio/timeline_schema.py)
        scenes/                   each scene's picture or stock clip
        assets/                   voice.wav, voice.words.json, the music, the sound effects
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..creator import director, factcheck, originality, soundbed, voice_engine, web
from ..creator.orchestrator import (HOLD_MAX, REWARDS_MIN, CreateRequest, CreationResult, CreatorError,
                                    VideoCreatorOrchestrator, find_category, hook_adds, new_id, output_folder,
                                    readme)
from ..editor import timeline_compiler
from ..ffmpeg import Cancelled
from ..settings import work_folder
from . import scraper, scriptwriter, timeline_builder, visual_sourcer, voice_studio
from .timeline_schema import VIDEO_BASE, Canvas, Timeline, TimelineError

STATES = ("input", "script", "voice", "visuals", "timeline", "render", "check", "deliver", "done")
FAILED = "failed"
STATE_WORDS = {"input": "Checking the request", "script": "Writing the script", "voice": "Recording the voice",
               "visuals": "Finding and making the pictures", "timeline": "Laying out the timeline",
               "render": "Rendering the video", "check": "Checking the video", "deliver": "Saving the video",
               "done": "Done", FAILED: "Stopped"}
FORMATS = {"short": "vertical", "long": "landscape"}
LONG_SECONDS = 300.0  # a long video's length when none is asked for
MAX_SCENES = 600  # the most scenes a storyboard handed in may have (20 minutes at a scene every 2 s)
MEMORY_PER_SCENE = 40e6  # bytes ffmpeg holds for each scene of a 1080p timeline while it renders
PERMISSION = ("Made by ClipMonster Studio: script written for this video, voice synthesised, pictures made here "
              "or licensed stock footage (credits in README.md)")


@dataclass
class StudioRequest(CreateRequest):
    url: str = ""  # a public article to retell ("" = from the prompt)
    format: str = ""  # short (vertical, about a minute) | long (wide, minutes) | "" = short
    orientation: str = ""  # vertical | landscape | square ("" = the format's)
    captions: str = ""  # pop | karaoke | bounce | plain | off ("" = Settings → Creator → Studio captions)
    tts: str = ""  # the voice engine: auto | kokoro | piper | windows | espeak | elevenlabs ("" = Settings)
    ai: str = ""  # who writes: local_first | cloud_first | cloud_deep | local_only | cloud_only ("" = Settings)
    fps: int = 30

    def label(self) -> str:
        return super().label() if (self.title or self.prompt or self.script) else (self.url or "A new video")[:80]


STUDIO_KEYS = frozenset(StudioRequest.__dataclass_fields__) - frozenset(CreateRequest.__dataclass_fields__)


def read_request(text: str) -> CreateRequest:
    """A queued request read back (jobs.db keeps it as JSON): a StudioRequest when it was one, which is when
    any of the Studio's own fields is in it, else the Video Creator's."""
    data = json.loads(text or "{}")
    if isinstance(data, dict) and STUDIO_KEYS & set(data):
        return StudioRequest.from_json(text)
    return CreateRequest.from_json(text)


def as_request(req: CreateRequest) -> StudioRequest:
    if isinstance(req, StudioRequest):
        return dataclasses.replace(req)
    return StudioRequest(**dataclasses.asdict(req))


class StudioOrchestrator(VideoCreatorOrchestrator):
    """Runs a Studio creation. `ask`, `read` and `stock` stand in for the AI, the web page reader and the
    stock sites (tests); `on_state(state)` is told each step as it starts."""

    permission = PERMISSION

    def __init__(self, settings=None, log=None, progress=None, cancelled=None, register: bool = True, db=None, *,
                 ask=None, read=None, stock=None, on_state: Callable[[str], None] | None = None):
        super().__init__(settings, log, progress, cancelled, register, db)
        self._ask, self._read, self._stock = ask, read or scraper.read, stock
        self._on_state = on_state or (lambda state: None)
        self.state = "input"
        self.failed_at = ""
        self.history: list[tuple[str, float]] = []
        self._entered = time.monotonic()
        self._base_settings = self.s
        self.article: scraper.Article | None = None
        self.tl: Timeline | None = None
        self.render_report = None
        self.visual_credits: list[str] = []
        self.canvas_used: Canvas | None = None

    # ------------------------------------------------------------ the state machine
    def enter(self, state: str) -> None:
        """Move to `state`: the next step, or the first again for a new run."""
        if state not in STATES:
            raise ValueError(f"no such step: {state}")
        now = time.monotonic()
        if state == "input":
            self.history, self.failed_at = [], ""
        else:
            at = STATES.index(self.state) if self.state in STATES else -1
            if STATES.index(state) != at + 1:
                raise RuntimeError(f"the Studio can't go from \"{self.state}\" to \"{state}\"")
            self.history.append((self.state, round(now - self._entered, 2)))
        self.state, self._entered = state, now
        self._on_state(state)
        if state != "done":
            self.check()

    def fail(self) -> None:
        if self.state != FAILED:
            self.failed_at = self.state
            self.history.append((self.state, round(time.monotonic() - self._entered, 2)))
            self.state = FAILED
            self._on_state(FAILED)

    def run(self, step: Callable, *a, **kw):
        """`step` with the Studio's own errors turned into CreatorError, and the state set to failed."""
        try:
            return step(*a, **kw)
        except Cancelled:
            self.fail()
            raise
        except (scraper.ScrapeError, scriptwriter.ScriptError, visual_sourcer.StockError,
                timeline_compiler.CompileError, TimelineError, voice_engine.VoiceError) as exc:
            self.fail()
            raise CreatorError(str(exc)) from exc
        except Exception:
            self.fail()
            raise

    # ------------------------------------------------------------ INPUT
    def canvas(self, req: StudioRequest) -> Canvas:
        shape = (req.orientation or FORMATS.get((req.format or "short").lower(), "")).lower()
        try:
            return Canvas.named(shape or "vertical", int(req.fps or 30))
        except ValueError as exc:
            raise CreatorError(str(exc)) from exc

    def accept(self, req: CreateRequest) -> StudioRequest:
        """INPUT: the request checked and made whole. Raises CreatorError saying what to change."""
        self.enter("input")
        req = as_request(req)
        req.url, req.prompt = req.url.strip(), req.prompt.strip()
        req.format = (req.format or "").strip().lower()
        if req.format and req.format not in FORMATS:
            raise CreatorError(f"There's no format called \"{req.format}\": use short or long.")
        if not (req.url or req.prompt or req.script.strip() or req.storyboard):
            raise CreatorError("Say what the video is about, give a public article's address, or paste a script.")
        if req.url:
            try:
                req.url = scraper.check_url(req.url, lookup=False)
            except scraper.ScrapeError as exc:
                raise CreatorError(str(exc)) from exc
        self.canvas(req)
        from ..intelligence import agent_router

        for value, known, what in (
                (req.captions, timeline_builder.CAPTION_PRESETS, "caption style"),
                (req.visuals, visual_sourcer.CHOICES, "picture source"),
                (req.ai, tuple(p for p in agent_router.PRIORITIES if p != "settings"), "way to ask the AI"),
                (req.tts, ("auto", *voice_engine.ENGINE_CLASSES), "voice engine")):
            if value and value not in known:
                raise CreatorError(f"\"{value}\" isn't a {what}. Use one of: {', '.join(known)}.")
        if req.seconds and not 8 <= req.seconds <= scriptwriter.MAX_SECONDS:
            raise CreatorError(f"A video can be 8 seconds to {scriptwriter.MAX_SECONDS / 60:.0f} minutes long "
                               f"(asked for {req.seconds:.0f} s).")
        self.s = self._base_settings.apply({"creator_tts": req.tts}) if req.tts else self._base_settings
        return req

    def target(self, req: CreateRequest) -> tuple[float, bool]:
        """(seconds to aim for, keep it over a minute for Creator Rewards). Only an upright video of about a
        minute is held to the Rewards length; a wide or long one, or one asked to be under a minute, isn't."""
        req = as_request(req)
        long = req.format == "long"
        seconds = float(req.seconds or (LONG_SECONDS if long else self.s.creator_duration or 65))
        asked_short = bool(req.seconds) and req.seconds < director.REWARDS_RANGE[0]
        # The Rewards length is for an upright video of about a minute. Asked for by name (a flag, a ticked box)
        # on a long or a wide one, the length you gave wins: an eight-minute video isn't cut to 80 seconds.
        fits = self.canvas(req).vertical and not long and seconds < scriptwriter.LONG_FROM
        if req.rewards is None:
            rewards = bool(self.s.creator_rewards_length) and fits and not asked_short
        else:
            rewards = bool(req.rewards) and fits
        if req.rewards and not fits and not getattr(self, "_rewards_said", False):
            self._rewards_said = True
            self.log(f"Creator Rewards' length ({director.REWARDS_RANGE[0]:.0f}-{director.REWARDS_RANGE[1]:.0f} s, "
                     f"upright) isn't applied here: the video is made {seconds:.0f} s long, as asked.")
        if rewards:
            seconds = min(max(seconds, director.REWARDS_RANGE[0] + 2), director.REWARDS_RANGE[1] - 5)
        return seconds, rewards

    def scene_limit(self, given: int) -> int:
        """A long video's storyboard is taken whole: twenty minutes is a few hundred scenes."""
        return max(director.MAX_SCENES + 4, min(int(given), MAX_SCENES))

    def shape_name(self, req: StudioRequest) -> str:
        c = self.canvas(req)
        return "vertical" if c.vertical else ("square" if c.width == c.height else "landscape")

    # ------------------------------------------------------------ SCRIPT
    def storyboard(self, req: CreateRequest) -> director.Storyboard:
        """SCRIPT: the storyboard, from the article at `req.url`, the prompt, your script, or as handed in."""
        if self.state != "input":
            req = self.accept(req)
        req = as_request(req)
        self.enter("script")
        if req.storyboard or req.script.strip():
            if req.url:
                self.log("Using your script as it is; the address isn't read.")
            return super().storyboard(req)
        started = time.monotonic()
        category = find_category(self.s, req.category)
        cat_name = category.name if category else req.category
        seconds, _ = self.target(req)
        report = self.stage("Writing the script")
        article = None
        if req.url:
            report("Writing the script: reading the page", None)
            article = self._read(req.url, log=self.log, check=self.check)
            self.log(f"Read \"{article.title}\" from {article.site or scraper._host(article.url)}: "
                     f"{article.words} words. The script retells it in new words and keeps its address as the "
                     "source.")
        self.article = article
        voice = voice_engine.profile(req.voice or self.s.creator_voice)
        engines = voice_engine.pick_engines(self.s)
        wpm = voice_engine.learned_wpm(voice.key, engines[0] if engines else "kokoro")
        found = None
        if article is None and getattr(self.s, "creator_web", True):
            report("Writing the script: looking it up online", None)
            found = web.gather(req.prompt, log=self.log, check=self.check,
                               **({"query": req.search} if req.search.strip() else {}))
        lessons, wpm, scene_seconds = self.lessons(req, category, wpm)
        topic = req.prompt or article.title
        ask = scriptwriter.ScriptRequest(
            prompt=req.prompt, article=article, category=cat_name or "",
            instructions=category.instructions if category else "", seconds=seconds, wpm=wpm,
            style=req.style or self.s.creator_style, orientation=self.shape_name(req),
            avoid=originality.earlier_titles(topic), facts=web.brief(found), lessons=lessons,
            scene_seconds=scene_seconds, priority=req.ai)
        entries = originality.load()
        report("Writing the script", None)
        board = scriptwriter.write(self.s, ask, ask=self._ask, log=self.log, check=self.check, progress=report,
                                   similar=lambda b: originality.closest(b.script, entries))
        if found:
            board.sources = [*board.sources, *[src.short() for src in found.sources]]
        mode = getattr(self.s, "creator_factcheck", "fix")
        if mode != "off" and (article is not None or (found and found.found)):
            report("Writing the script: checking the facts", None)
            facts = ask.facts if article is None else (
                web.brief(None) + f"\n\nThe source this script retells ({article.site or scraper._host(article.url)}"
                + (f", published {article.date[:10]}" if article.date else "") + f"), \"{article.title}\":\n"
                + scraper.distil(article, 6000)
                + "\n\nRules for facts: every date, number, name and claim in the script must be one this source "
                  "supports. Leave out what it doesn't say rather than guess.")
            drafted = [sc.speech_text for sc in board.scenes]
            hook = board.hook
            scene_of = {n: i for n, i, _ in factcheck.numbered(board)}
            checked = factcheck.check(self.s, board, facts, have_sources=True, mode=mode, log=self.log,
                                      check_cancel=self.check, progress=report)
            if article is not None:
                self.keep_retold(board, article, checked, drafted, hook, scene_of)
            board.factcheck = checked.to_json()
        if req.title:
            board.title = req.title
        board.category = cat_name or board.category
        self._timed("script", started)
        report("Writing the script", 1.0)
        return board

    def keep_retold(self, board: director.Storyboard, article, checked, drafted: list[str], hook: str,
                    scene_of: dict[int, int]) -> int:
        """The fact check corrects a sentence "to say what the source says", and the nearest way to say that is
        the source's own sentence. Each scene it rewrote is compared with the article again: one that now
        carries the article's wording goes back to the script's own words, and its claim stays flagged for you
        to look at instead. Returns how many scenes were put back."""
        undone = 0
        for i, (sc, was) in enumerate(zip(board.scenes, drafted)):
            if sc.speech_text == was:
                continue
            now, before = scriptwriter.copied(sc.speech_text, article.text), scriptwriter.copied(was, article.text)
            if now.run >= scriptwriter.COPY_RUN and now.run > before.run:
                sc.speech_text = was
                if i == 0:
                    board.hook = hook
                for c in checked.claims:
                    if c.changed and scene_of.get(c.n) == i:
                        c.changed = False
                undone += 1
        if undone:
            self.log(f"The fact check's wording for {undone} scene{'s' if undone != 1 else ''} was the article's "
                     "own, so the script's words were kept there and the claim is flagged for you to check.")
        still = scriptwriter.copied(board.script, article.text)
        if still.lifted and not scriptwriter.copied(" ".join(drafted), article.text).lifted:
            self.log(f"Some phrases are still the article's ({still.run} words in a row: \"{still.words[:80]}\"). "
                     "Read the script before you post.")
        return undone

    # ------------------------------------------------------------ the whole run
    def create(self, req: CreateRequest, board: director.Storyboard | None = None) -> CreationResult:
        """Make the video. `board`: a storyboard already written (a preview that was edited)."""
        self.timings = {}
        cid = new_id()

        def all_of_it():
            request = self.accept(req)
            if board is None:
                written = self.storyboard(request)
            else:
                self.enter("script")
                written = board
            return self._make(written, request, cid)
        return self.run(all_of_it)

    def render(self, board: director.Storyboard, req: CreateRequest, cid: str | None = None) -> CreationResult:
        """Everything after the script, for a storyboard written earlier."""
        def rest():
            request = self.accept(req)
            self.enter("script")
            return self._make(board, request, cid or new_id())
        return self.run(rest)

    def _make(self, board: director.Storyboard, req: StudioRequest, cid: str) -> CreationResult:
        s = self.s
        canvas = self.canvas(req)
        self.canvas_used = canvas
        category = find_category(s, req.category)
        cat_name = category.name if category else (req.category or board.category)
        style = scriptwriter.style_of(scriptwriter.ScriptRequest(
            prompt=board.prompt, category=cat_name or "", instructions=category.instructions if category else "",
            style=req.style or s.creator_style, orientation=self.shape_name(req)))
        seconds, rewards = self.target(req)
        warnings: list[str] = []
        with work_folder("studio-") as tmp:
            work = Path(tmp)
            pkg = work / "pkg"  # laid out as the video's folder will be, so the timeline's paths stay true
            scenes, assets = pkg / "scenes", pkg / "assets"
            scenes.mkdir(parents=True)
            assets.mkdir()

            self.enter("voice")
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

            self.enter("visuals")
            started = time.monotonic()
            spans, _, _ = timeline_builder.spans(track, duration, s.creator_transition,
                                                 [sc.sfx_cue for sc in board.scenes])
            on_screen = {sc.scene_id: b - a + 0.4 for sc, (a, b) in zip(board.scenes, spans)}
            visuals = visual_sourcer.source_all(
                board, s, scenes, canvas=(canvas.width, canvas.height), seconds=on_screen,
                choice=req.visuals or s.creator_visuals, style=style, log=self.log,
                progress=self.stage("Making pictures"), check=self.check, stock=self._stock)
            self.visual_credits = visual_sourcer.credits(visuals)
            self._timed("visuals", started)

            self.enter("timeline")
            started = time.monotonic()
            report = self.stage("Laying out the timeline")
            report("Laying out the timeline", None)
            wav, _ = voice_studio.export(track, assets)
            music, music_note = soundbed.music_for(s, board.music_mood, duration, assets, board.title)
            self.hook_card = None
            title = bool(s.creator_hook_card) and hook_adds(board)
            tl = timeline_builder.build(
                board, dataclasses.replace(track, path=wav), visuals, s, canvas=canvas, duration=duration,
                style=style, music=music, music_note=music_note, captions=req.captions, hook_title=title,
                meta={"id": cid, "source_url": self.article.url if self.article else (req.url or "")})
            if title and tl.track(timeline_builder.TITLES) is not None:
                until = tl.track(timeline_builder.TITLES).events[-1].end
                self.hook_card = {"at": round(min(1.0, until / 2), 2), "until": round(until, 2)}
            gather_files(tl, pkg)
            tl.dump(pkg / "timeline.json")
            self.tl = tl
            note = memory_note(tl)
            if note:
                self.log(note)
            self.mix = {"music": music_note, "effects": timeline_builder.describe(tl)["effects"],
                        "duck_db": tl.audio.duck_db}
            self._timed("timeline", started)

            self.enter("render")
            started = time.monotonic()
            video = work / "final.mp4"
            put = self.stage("Putting the video together")
            rendered = timeline_compiler.render(tl, video, base=pkg, work=work / "render", settings=s, progress=put,
                                                check_cancel=self.check, log=self.log)
            self.render_report = rendered
            self.render_info = rendered.to_json()
            self.mix |= {"voice_gain_db": round(rendered.voice_gain_db, 2), "mix_before_lufs": rendered.mix_lufs_before}
            warnings += rendered.warnings
            self._timed("render", started)

            self.enter("check")
            started = time.monotonic()
            report = self.stage("Checking the video")
            report("Checking the video", None)
            checks = self.checks(video, duration, rewards, board, warnings, size=(canvas.width, canvas.height))
            self._timed("check", started)

            self.enter("deliver")
            folder = output_folder(s, category, cat_name, board.title, cid, req.post)
            folder.mkdir(parents=True, exist_ok=True)
            if getattr(s, "studio_keep_timeline", True):
                visuals = move_package(pkg, folder, visuals)
            self.timeline = None  # the Creator's own timeline record; the Studio's is timeline.json
            result = self._deliver(cid, board, req, video, folder, work / "nothing", visuals, track, checks, warnings,
                                   category, cat_name, style, duration)
            try:  # the Creator makes this for its pictures; without the timeline's files it's empty
                (folder / "scenes").rmdir()
            except OSError:
                pass
            report("Checking the video", 1.0)
        self.enter("done")
        self.history.append(("done", 0.0))
        return result

    # ------------------------------------------------------------ what's written beside the video
    def creator_extra(self) -> dict:
        tl = self.tl
        kept = bool(getattr(self.s, "studio_keep_timeline", True))
        return {"studio": {
            "source_url": self.article.url if self.article else "",
            "source_title": self.article.title if self.article else "",
            "canvas": f"{self.canvas_used.width}x{self.canvas_used.height}" if self.canvas_used else "",
            "timeline": timeline_builder.describe(tl) if tl else {},
            "timeline_file": "timeline.json" if kept else "",
            "credits": list(self.visual_credits),
            "chapters": [[t, title] for t, title in timeline_builder.chapters(tl)] if tl else [],
            "steps": [[name, secs] for name, secs in self.history],
        }}

    def readme_text(self, board: director.Storyboard, creator: dict, checks: dict, file: str) -> str:
        text = readme(board, creator, checks, file).replace(
            "**Made by:** ClipMonster's Video Creator  ", "**Made by:** ClipMonster Studio  ", 1)
        studio = creator.get("studio") or {}
        lines = []
        if studio.get("source_url"):
            lines += ["", "## Retold from", "",
                      f"[{studio.get('source_title') or studio['source_url']}](<{studio['source_url']}>). The script "
                      "tells it in new words; credit the source when you post."]
        if studio.get("credits"):
            lines += ["", "## Stock footage", ""] + [f"- {c}" for c in studio["credits"]]
            lines += ["", "Used under each site's licence (Pexels License, Pixabay Content License). A link back is "
                          "appreciated by both and asked for by Pexels where you can give one."]
        marks = [(t, title) for t, title in studio.get("chapters") or []]
        stamps = scriptwriter.chapter_lines(marks)
        if stamps:
            lines += ["", "## Chapters (for the description)", "", "```", stamps, "```"]
        if studio.get("timeline_file"):
            lines += ["", "## Editing it", "",
                      "`timeline.json` is the whole edit: clips, camera moves, transitions, sound levels, every "
                      "caption word. Change it and render it again with:", "",
                      "    ytclip studio --timeline \"<this folder>/timeline.json\""]
        return text.rstrip("\n") + "\n" + ("\n".join(lines) + "\n" if lines else "")


# ---------------------------------------------------------------- the video's files

def gather_files(tl: Timeline, pkg: Path) -> None:
    """Make the timeline's files travel with it: the sound effects are copied into pkg/assets/sfx, and every
    path inside `pkg` is written relative to it (your own B-roll and music stay where they are, by full path)."""
    pkg = Path(pkg).resolve()
    sfx = pkg / "assets" / "sfx"
    copied: dict[str, str] = {}
    for key, asset in tl.assets.items():
        if not asset.path:
            continue
        path = Path(asset.path)
        if not path.is_absolute():
            continue
        try:
            asset.path = path.resolve().relative_to(pkg).as_posix()
            continue
        except ValueError:
            pass
        used_as_sfx = any(c.asset == key for t in tl.audio_tracks("sfx") for c in t.clips)
        if used_as_sfx and path.is_file():
            if str(path) not in copied:
                sfx.mkdir(parents=True, exist_ok=True)
                dest = sfx / path.name
                n = 1
                while dest.exists():  # another effect with the same file name (whoosh/01.wav, impact/01.wav)
                    n += 1
                    dest = sfx / f"{path.stem}_{n}{path.suffix}"
                shutil.copyfile(path, dest)
                copied[str(path)] = str(dest)
            asset.path = Path(copied[str(path)]).resolve().relative_to(pkg).as_posix()


def move_package(pkg: Path, folder: Path, visuals: list) -> list:
    """Everything in `pkg` moved into the video's folder (same layout); the visuals' paths follow."""
    pkg, folder = Path(pkg), Path(folder)
    for src in sorted(p for p in pkg.rglob("*") if p.is_file()):
        dest = folder / src.relative_to(pkg)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    out = []
    for v in visuals:
        try:
            rel = Path(v.path).resolve().relative_to(pkg.resolve())
            out.append(dataclasses.replace(v, path=folder / rel))
        except ValueError:
            out.append(v)
    return out


def memory_note(tl: Timeline, available: float | None = None) -> str:
    """A warning when a long timeline will need more memory to render than the computer has free."""
    base = tl.track(VIDEO_BASE)
    n = len(base.clips) if base else 0
    scale = (tl.canvas.width * tl.canvas.height) / (1080 * 1920)
    need = 0.4e9 + n * MEMORY_PER_SCENE * scale
    if available is None:
        try:
            from ..intelligence import hardware

            available = float(hardware.ram_free_bytes())
        except Exception:  # noqa: BLE001
            available = 0.0
        if available <= 0:  # it couldn't be read
            return ""
    if need < available * 0.8:
        return ""
    return (f"This timeline has {n} scenes: rendering it in one run needs about {need / 1e9:.1f} GB of memory and "
            f"{available / 1e9:.1f} GB is free. Close other programs, or make it in two shorter parts.")


def _render_folder(folder: Path):
    """A work folder for rendering a timeline again: inside the video's own folder when that can be written
    to, so every clip is named from there in a few characters (Windows limits a command's length, and a long
    video has hundreds of clips); else the usual work folder."""
    try:
        return tempfile.TemporaryDirectory(prefix=".render-", dir=folder, ignore_cleanup_errors=True)
    except OSError:
        return work_folder("timeline-")


def list_edit(folder: Path, video: Path) -> bool:
    """Add a re-rendered video to its folder's clips.json, as an edit of the video the Studio made there: it
    then carries the same AI-generated label, caption, hashtags and category when posting takes it in, always
    waits for your OK in the queue (posting/store.py), and counts as something left to post, so Clean up
    keeps the timeline while it's there. True when it was listed (False: not a creation's folder)."""
    index = folder / "clips.json"
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
        name = video.resolve().relative_to(folder.resolve()).as_posix()
    except (OSError, ValueError):
        return False
    clips = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(clips, list) or not isinstance(data.get("creator"), dict):
        return False
    originals = [c for c in clips if isinstance(c, dict) and c.get("file") and not c.get("edit_of")]
    if not originals or any(isinstance(c, dict) and c.get("file") == name for c in clips):
        return bool(originals)  # listed already (rendered again over the last edit)
    first = originals[0]
    clips.append({**first, "rank": max((int(c.get("rank") or 0) for c in clips if isinstance(c, dict)), default=0) + 1,
                  "file": name, "title": f"{first.get('title') or video.stem} (edit)", "edit_of": first["file"]})
    try:
        tmp = index.with_name(index.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, index)
    except OSError:
        return False
    return True


def render_timeline(path: str | Path, out: str | Path | None = None, *, settings=None, log=None, progress=None,
                    check_cancel=None):
    """Render a timeline.json again (after editing it). The video goes beside it as "<title> (edit).mp4" unless
    `out` says where. It's rendered to a hidden file first and put in place when it's whole, so a render that
    fails or is stopped never leaves half a video where posting could find it. Returns the compiler's
    RenderReport."""
    from ..organize import safe_name
    from ..settings import Settings

    log = log or (lambda m: None)
    path = Path(path)
    if path.is_dir():
        path = path / "timeline.json"
    if not path.is_file():
        raise CreatorError(f"There's no timeline at {path}.")
    try:
        tl = Timeline.load(path)
    except (TimelineError, ValueError, OSError) as exc:
        raise CreatorError(f"{path.name} can't be used: {exc}") from exc
    for note in getattr(tl, "notes", []):
        log(f"{path.name}: {note}")
    note = memory_note(tl)
    if note:
        log(note)
    folder = path.parent
    dest = Path(out) if out else folder / f"{safe_name(tl.title or 'video', 60)} (edit).mp4"
    with _render_folder(folder) as tmp:
        work = Path(tmp)
        part = work / ".edit.mp4"  # a name posting's intake passes over
        try:
            report = timeline_compiler.render(tl, part, base=folder, work=work, settings=settings or Settings.load(),
                                              progress=progress, check_cancel=check_cancel, log=log)
        except (timeline_compiler.CompileError, TimelineError) as exc:
            raise CreatorError(str(exc)) from exc
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(part, dest)
            except OSError:  # another drive
                shutil.copyfile(part, dest)
        except OSError as exc:
            raise CreatorError(f"The video was rendered but couldn't be saved as {dest.name} ({exc.strerror or exc}). "
                               "If it's open in a player, close it and render again.") from exc
    report.path = dest
    if list_edit(folder, dest):
        log(f"{dest.name} is listed with its video: if this folder is one posting watches, the edit joins the "
            "queue labelled AI-generated and waits there for your OK. Remove the one you don't want posted.")
    return report

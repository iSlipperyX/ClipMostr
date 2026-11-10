"""Command line entry point: ytclip URL|FILE [...]"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__, fresh, learn
from .download import expand_sources
from .engine import Engine, Project
from .organize import stamp
from .settings import (ASPECTS, LAYOUT_PRESETS, PRESETS, PRO_EDIT, Settings, all_layouts, all_presets,
                       find_layout)
from .qc import REJECTED as QC_REJECTED
from .sports import SPORTS
from .stackpool import pool_files

# Redirected output on Windows defaults to cp1252, which can't encode ✓, → or most
# video titles; switch to UTF-8 so printing never crashes the run.
for _stream in (sys.stdout, sys.stderr):
    if _stream and (getattr(_stream, "encoding", "") or "").lower() not in ("utf-8", "utf8"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

console = Console()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ytclip",
        description="Download YouTube videos (or open local files) and cut them into their best clips.",
        epilog='Example: ytclip "https://youtu.be/VIDEO" --preset "Shorts / TikTok / Reels"\n'
           "Trending research: ytclip research --help\nFind videos for a request: ytclip find --help\n"
           "Automation: ytclip auto --help\n"
           "Quality check: ytclip qc --help\nWhat's working: ytclip results --help\n"
           "Local AI (no key, offline): ytclip local-ai --help\n"
           "Sports recap reel: ytclip sports-montage --help\n"
           "TikTok posting: ytclip posting --help\nClear out space: ytclip clean --help\n"
           "Check this install's libraries: ytclip self-test",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("sources", nargs="*", help="video/playlist/channel URLs, video files or folders")
    p.add_argument("-a", "--batch-file", type=Path, help="text file with one URL or path per line")
    p.add_argument("-p", "--preset", help="start from a preset (see --list-presets)")
    p.add_argument("--list-presets", action="store_true", help="show presets and exit")
    p.add_argument("-o", "--output", type=Path, help="output folder")
    p.add_argument("-n", "--clips", type=int, help="clips per video")
    p.add_argument("--min", dest="min_len", type=float, help="minimum clip length in seconds")
    p.add_argument("--max", dest="max_len", type=float, help="maximum clip length in seconds")
    p.add_argument("--mode", choices=["auto", "ai", "chapters"],
                   help="auto: smart signals; ai: the AI (local, Claude or Gemini) picks from the transcript; "
                        "chapters: one clip per chapter")
    p.add_argument("-f", "--format", dest="aspect", choices=[a for a, _ in ASPECTS], help="output framing")
    p.add_argument("--vertical", action="store_true", help="shortcut for --format vertical_blur")
    p.add_argument("--captions", action="store_true", help="burn in animated word-by-word captions")
    p.add_argument("--top-text", metavar="TEXT", help='top banner text; {title} = clip title (e.g. "{title}")')
    p.add_argument("--bottom-text", metavar="TEXT", help="bottom banner text, e.g. \"Follow for part 2\"")
    p.add_argument("-l", "--layout", metavar="NAME",
                   help='top/bottom view preset, e.g. "Gameplay below" (see --list-layouts)')
    p.add_argument("--list-layouts", action="store_true", help="show top/bottom view presets and exit")
    p.add_argument("--no-tiktok", action="store_true",
                   help="turn off TikTok-ready output (needed to keep the original frame or 1:1)")
    p.add_argument("--stack", metavar="VIDEO", type=Path,
                   help="stack a second video (gameplay, satisfying clips...) under each clip, 9:16; give a "
                        "folder to use a different video from it for each clip")
    p.add_argument("--polish", action="store_true",
                   help="AI polish: hooks for the top banner, bottom lines for clips with no part 2, caption "
                        "fixes, speaker crops (needs a Claude or Gemini key)")
    p.add_argument("--ai", choices=["local", "claude", "gemini"],
                   help="which AI to use for --mode ai and --polish this run (default: Settings → AI); local "
                        "runs on this PC with no key (see: ytclip local-ai --help)")
    p.add_argument("--pro", action="store_true",
                   help="pro edit: follow the speaker, punch-ins, cut dead air and fillers, kinetic captions, "
                        "-14 LUFS mastering, hook card and lower-third (Settings → Editing)")
    p.add_argument("--creator-rewards", action="store_true",
                   help="clips over a minute (62-88 s), the length TikTok's Creator Rewards pays for")
    p.add_argument("--music", metavar="FOLDER", type=Path,
                   help="licensed music to put under each clip, ducked under the voice (subfolders = moods)")
    p.add_argument("--broll", metavar="FOLDER", type=Path,
                   help="your own B-roll clips, cut in every 15-20 s where their file names match the words")
    p.add_argument("--bleep", action="store_true", help="mask strong swearing in captions (f*ck, sh*t)")
    p.add_argument("--qc", action="store_true", help="check each clip after export; failures move to _rejected")
    p.add_argument("--keywords", help="comma-separated words/phrases to boost")
    p.add_argument("--sport", choices=sorted(SPORTS), help="clip it as sports: the crowd, the commentary and replays "
                                                         "find the plays, each clip a whole play (and that sport's "
                                                         "clip lengths, unless --min/--max)")
    p.add_argument("-q", "--quality", type=int, help="max download height, e.g. 720")
    p.add_argument("--encoder", choices=["auto", "libx264", "h264_amf", "h264_nvenc", "h264_qsv"])
    p.add_argument("--fast", action="store_true", help="copy streams instead of re-encoding")
    p.add_argument("--limit", type=int, help="max videos to take from each playlist/channel")
    p.add_argument("--delete-source", action="store_true", help="delete the full video once clips are cut")
    p.add_argument("--dry-run", action="store_true", help="analyse and print the clip plan without exporting")
    p.add_argument("--cookies-from-browser", metavar="BROWSER",
                   help="a browser's login to download with: clipmonster (its own, from ytclip sign-in), chrome, "
                        "edge, firefox...")
    p.add_argument("--check-update", action="store_true", help="check GitHub for a newer ClipMonster release")
    p.add_argument("--update", action="store_true", help="download and install the newest release (packaged app only)")
    p.add_argument("--channel", choices=("stable", "beta", "dev"),
                   help="with --check-update/--update: this update channel instead of Settings → Updates'")
    p.add_argument("--go-back", action="store_true",
                   help="install the version you had before the last update again (packaged app only)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def run_update(install: bool, channel: str = "", go_back: bool = False) -> int:
    import os
    import time

    from . import updater

    s = Settings.load()
    if go_back:
        version = updater.previous_version()
        if not version:
            console.print("There's no previous version to go back to. One is kept from each update from 2.8.0 on.")
            return 1
        rel = None
    else:
        channel = channel or updater.channel_setting(s)
        repo = updater.configured_repo(s.update_repo)
        try:
            rel = updater.check(repo, channel)
        except updater.UpdateError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            return 1
        updater.save_state(last_check=time.time())
        if not rel:
            console.print(f"ClipMonster {escape(updater.build_label())} is up to date on the {channel} channel.")
            return 0
        version = rel.version
        what = "Switch to" if rel.switch else "Available:"
        console.print(f"{what} [bold]ClipMonster {escape(rel.label)}[/] (you have {escape(__version__)}). "
                      f"{escape(rel.page_url)}")
        if not install:
            return 0
    target = updater.install_dir()
    if target is None:
        console.print("Running from source: update with [bold]git pull[/] instead.")
        return 1
    if not updater.stop_automation():
        console.print(f"[red]{escape(updater.AUTOMATION_BUSY)}[/]")
        return 1
    others = updater.other_instances(target)
    if others:
        console.print(f"[red]Close these first; they keep the ClipMonster folder in use: {escape(', '.join(others))}[/]")
        return 1
    try:
        if rel is None:
            new_app = updater.prepare_previous(target)
            updater.save_state(skipped=__version__)
        else:
            with Progress(TextColumn("[cyan]{task.description}"), BarColumn(), console=console, transient=True) as bar:
                task = bar.add_task("Downloading update", total=1)
                new_app = updater.prepare(rel, target, lambda f: bar.update(task, completed=f))
    except updater.UpdateError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        return 1
    updater.save_state(pending=version)
    updater.launch_swap(new_app, target, os.getpid(), relaunch="")
    console.print(f"[green]✓ Ready to install {escape(version)}.[/] It's installed in the background "
                  f"once this command exits; the next ytclip run reports the result. "
                  f"Log: {escape(str(updater.update_log()))}")
    return 0


def settings_from_args(args: argparse.Namespace) -> Settings:
    s = Settings.load()
    if args.preset:
        presets = all_presets()
        match = next((k for k in presets if k.lower().startswith(args.preset.lower())), None)
        if not match:
            raise SystemExit(f"Unknown preset '{args.preset}'. Try --list-presets.")
        s = s.apply(presets[match])
    if args.layout:
        name = find_layout(args.layout)
        if not name:
            raise SystemExit(f"Unknown layout '{args.layout}'. Try --list-layouts.")
        s = s.apply(all_layouts()[name])
    overrides = {
        "output_dir": str(args.output) if args.output else None,
        "clip_count": args.clips, "min_len": args.min_len, "max_len": args.max_len, "mode": args.mode,
        "aspect": "vertical_blur" if args.vertical else args.aspect, "quality": args.quality,
        "encoder": args.encoder, "keywords": args.keywords, "playlist_limit": args.limit,
        "cookies_browser": args.cookies_from_browser,
        "burn_captions": True if args.captions else None,
        "fast_copy": True if args.fast else None,
        "keep_source": False if args.delete_source else None,
        "tiktok_ready": False if args.no_tiktok else None,
        "ai_polish": True if args.polish else None,
        "ai_provider": args.ai,
        "caption_censor": True if args.bleep else None,
        "qc_enabled": True if args.qc else None,
        "sport": args.sport,
    }
    if args.sport and args.min_len is None and args.max_len is None:
        overrides["min_len"], overrides["max_len"] = SPORTS[args.sport].lengths
    if args.pro:
        overrides |= PRO_EDIT
    if args.creator_rewards:
        overrides["creator_rewards"] = True
    for flag, field in ((args.music, "music_folder"), (args.broll, "broll_folder")):
        if flag is not None:
            if not flag.is_dir():
                raise SystemExit(f"No such folder: {flag}")
            overrides[field] = str(flag.resolve())
    if args.top_text is not None:
        overrides |= {"banner_top": True, "banner_top_text": args.top_text}
    if args.bottom_text is not None:
        overrides |= {"banner_bottom": True, "banner_bottom_text": args.bottom_text}
    if args.stack:
        if args.stack.is_dir():
            if not pool_files(args.stack):
                raise SystemExit(f"--stack: no videos in {args.stack}")
            overrides |= {"stack_enabled": True, "stack_folder": str(args.stack.resolve())}
        elif not args.stack.is_file():
            raise SystemExit(f"--stack: no such video or folder: {args.stack}")
        else:
            overrides |= {"stack_enabled": True, "stack_video": str(args.stack.resolve()), "stack_folder": ""}
    return s.apply({k: v for k, v in overrides.items() if v is not None})


def process(source: str, engine: Engine, dry_run: bool) -> None:
    p = Project(source)
    with Progress(TextColumn("[cyan]{task.description}"), BarColumn(), TimeElapsedColumn(),
                  console=console, transient=True) as bar:
        task = bar.add_task("Starting", total=None)
        engine._progress = lambda msg, f: bar.update(task, description=msg, completed=f, total=1 if f is not None else None)
        engine.open(p)
        v = p.video
        console.print(f"[bold]{escape(v.title)}[/]  [dim]{escape(v.channel)} · {stamp(v.duration)}[/]")
        engine.measure(p)
        engine.find(p)
        engine.retype(p)
        if engine.s.ai_polish:
            engine.polish(p)

    if p.ai_error:
        console.print(f"[yellow]⚠ AI clip finder failed: {escape(p.ai_error)} Used smart signals instead.[/]")
    if p.polish_error:
        console.print(f"[yellow]⚠ AI polish skipped: {escape(p.polish_error)}[/]")
    console.print(f"[dim]Picked using: {', '.join(engine.signal_names(p)) or p.mode_used}"
                  f"{' · transcript: ' + p.transcript_source if p.transcript_source else ''}[/]")
    table = Table(show_edge=False, header_style="bold")
    hooks = any(c.hook for c in p.clips)
    for col in ("#", "Time", "Len", "Score", "Title") + (("Hook",) if hooks else ()):
        table.add_column(col)
    for i, c in enumerate(p.clips, 1):
        table.add_row(str(i), f"{stamp(c.start)}–{stamp(c.end)}", f"{c.length:.0f}s", f"{c.score:.0f}", escape(c.title),
                      *((escape(c.hook),) if hooks else ()))
    console.print(table)
    if dry_run:
        return

    with Progress(TextColumn("[cyan]{task.description}"), BarColumn(), TimeElapsedColumn(),
                  console=console, transient=True) as bar:
        task = bar.add_task("Exporting", total=1)
        engine._progress = lambda msg, f: bar.update(task, description=msg, completed=f or 0)
        folder = engine.export(p)
    if p.qc:
        console.print(f"[green]✓ {p.qc['passed']} clips ready[/]"
                      + (f", [red]{p.qc['rejected']} rejected[/] (in {QC_REJECTED})" if p.qc["rejected"] else "")
                      + f" → {escape(str(folder))}")
    else:
        console.print(f"[green]✓ {len(p.clips)} clips →[/] {escape(str(folder))}")


def build_research_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ytclip research",
        description="See what's trending on YouTube by region and score how well each video will cut into clips.",
        epilog="Example: ytclip research -r US,GB -c gaming --top 15   (needs a YouTube Data API key: "
               "--api-key, the YOUTUBE_API_KEY variable, or Settings → Research in the app)",
    )
    p.add_argument("-r", "--regions", help="comma-separated country codes, e.g. US,GB,CA (default: your settings)")
    p.add_argument("-c", "--category", help="category name or id, e.g. gaming, comedy, 20 (see --list-categories)")
    p.add_argument("-n", "--top", type=int, default=20, help="how many videos to list (default 20)")
    p.add_argument("--per-region", type=int, help="videos to read from each chart, up to 200")
    p.add_argument("--min-minutes", type=float, help="skip videos shorter than this")
    p.add_argument("--deep", type=int, help="how many top candidates get a most-replayed check (0 = none)")
    p.add_argument("--watch", type=float, metavar="MINUTES",
                   help="keep checking every MINUTES and print new arrivals (Ctrl+C to stop)")
    p.add_argument("--picks", action="store_true",
                   help="apply your picking rules (Settings → Picking): show what they pick and why the rest "
                        "were skipped")
    p.add_argument("--clip", type=int, metavar="N",
                   help="cut up to N videos straight away with your settings: the top N, or with --picks the "
                        "picked ones. With --watch, every check clips what's new")
    p.add_argument("--json", action="store_true", help="print results as JSON")
    p.add_argument("--api-key", help="YouTube Data API key (otherwise from settings or YOUTUBE_API_KEY)")
    p.add_argument("--list-categories", action="store_true", help="show category names and ids and exit")
    return p


def _category_id(text: str) -> str:
    from .trending import CATEGORIES

    t = text.strip().lower()
    if t in ("", "all", "any"):
        return ""
    if t in CATEGORIES:
        return t
    match = next((cid for cid, name in CATEGORIES.items() if cid and name.lower().startswith(t)), None)
    if match is None:
        raise SystemExit(f"Unknown category '{text}'. Try --list-categories.")
    return match


ORIGIN_LABELS = {"watchlist": "your list", "cc": "CC search"}


def _research_table(videos: list, title: str, decisions: dict | None = None, match: bool = False) -> Table:
    from .clipability import age_text, compact

    table = Table(title=title, show_edge=False, header_style="bold", title_justify="left")
    cols = (["Match"] if match else []) + ["Score", "Video", "Found in", "Views/h", "Length", "Posted", "Why"] + (
        ["Pick"] if decisions else [])
    for col in cols:
        table.add_column(col, overflow="fold" if col in ("Video", "Why", "Pick") else "ellipsis")
    for v in videos:
        colour = "green" if v.score >= 75 else "cyan" if v.score >= 55 else "yellow" if v.score >= 40 else "dim"
        where = [f"{c}#{n}" for c, n in sorted(v.regions.items(), key=lambda kv: kv[1])[:3]]
        where += [ORIGIN_LABELS[o] for o in v.origins if o in ORIGIN_LABELS]
        row = [f"[bold {colour}]{v.score:.0f}[/]", f"{escape(v.title)}\n[dim]{escape(v.channel)} · {v.url}[/]",
               " ".join(where), ("↑" if v.recent else "") + compact(v.velocity),
               stamp(v.duration) if v.live == "none" else v.live, age_text(v.age_hours()),
               escape(v.reasons[0]) if v.reasons else ""]
        if match:
            row.insert(0, f"[bold]{v.match:.0f}[/]")
        if decisions:
            d = decisions.get(v.id)
            row.append(("[green]✓ " if d.picked else "[dim]– ") + escape(d.reason)
                       + (f"\n📈 {escape(d.learned)}" if d.learned else "") + "[/]" if d else "")
        table.add_row(*row)
    return table


def build_find_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ytclip find",
        description="Find YouTube videos for a request you describe, scored for how well they match it and how "
                    "well they'll clip. With an AI key, Claude writes the searches and rates the matches.",
        epilog='Example: ytclip find "Call of Duty MW4 with good comedy" --since month   (about 200 of the '
               "YouTube API's 10,000 free daily units)",
    )
    p.add_argument("request", nargs="+", help="what you want, in your own words")
    p.add_argument("-n", "--top", type=int, default=20, help="how many videos to list (default 20)")
    p.add_argument("--since", choices=("any", "year", "month", "week"), default="any",
                   help="only videos posted in this time")
    p.add_argument("--min-minutes", type=float, help="skip videos shorter than this (default: your settings)")
    p.add_argument("--no-ai", action="store_true", help="match by the request's words, without Claude")
    p.add_argument("--queue", type=int, metavar="N", help="add the best N to Automation's queue")
    add_category_option(p, help="the category (name or id) videos added with --queue are for")
    p.add_argument("--json", action="store_true", help="print results as JSON")
    p.add_argument("--api-key", help="YouTube Data API key (otherwise from settings or YOUTUBE_API_KEY)")
    return p


def _renamed_warning(old: str, new: str) -> None:
    print(f"Note: {old} is now {new} (3.2 renamed niches to categories). {old} still works for now.",
          file=sys.stderr)


class _NicheAlias(argparse.Action):
    """--niche, from before 3.2: the same as --category, with a note to use the new name."""

    def __call__(self, parser, namespace, values, option_string=None):
        _renamed_warning("--niche", "--category")
        setattr(namespace, self.dest, values)


def add_category_option(p: argparse.ArgumentParser, **kw) -> None:
    """--category NAME, and the old --niche NAME as a hidden alias of it."""
    p.add_argument("--category", dest="category", **kw)
    p.add_argument("--niche", dest="category", action=_NicheAlias, help=argparse.SUPPRESS,
                   **{k: v for k, v in kw.items() if k == "metavar"})


def run_find(argv: list[str]) -> int:
    import contextlib
    import json

    from . import lookup, categories
    from .policy import language_codes
    from .research import youtube_key
    from .trending import ResearchError

    args = build_find_parser().parse_args(argv)
    s = Settings.load()
    key = (args.api_key or youtube_key(s)).strip()
    if not key:
        console.print("[red]Needs a YouTube Data API key:[/] --api-key, the YOUTUBE_API_KEY variable, or "
                      "Settings → Research in the app.")
        return 2
    category = None
    if args.category:
        category = categories.find(s, args.category) or next((n for n in categories.load(s)
                                                    if n.name.lower() == args.category.strip().lower()), None)
        if category is None:
            console.print(f"[red]No category called {escape(args.category)}.[/]")
            return 2
    request = " ".join(args.request)
    log = (lambda m: None) if args.json else (lambda m: console.print(f"[dim]{escape(m)}[/]"))  # noqa: E731
    try:
        with console.status("Finding videos…") if not args.json else contextlib.nullcontext():
            memory = fresh.load()  # each run of the same request searches the next way (fresh.py)
            result = lookup.find(request, s, key, args.since,
                                 s.research_min_minutes if args.min_minutes is None else args.min_minutes,
                                 next(iter(language_codes(s.pick_languages)), ""), log,
                                 use_ai=False if args.no_ai else None, memory=memory,
                                 request_key=fresh.request_key(request), skip=fresh.used_now())
            memory.save()
    except (ResearchError, ValueError) as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        return 1
    try:
        lookup.remember(result)
    except OSError:
        pass
    top = result.videos[:max(1, args.top)]
    if args.json:
        print(json.dumps({"request": result.request, "searched": result.plan.queries, "order": result.plan.order,
                          "quota": result.quota, "rated_by": result.rated_by, "left_out_used": result.skipped,
                          "videos": [
                              {"url": v.url, "title": v.title, "channel": v.channel, "match": v.match, "new": v.new,
                               "why": v.match_why, "clipability": v.score, "rank": lookup.rank(v),
                               "minutes": round(v.duration / 60, 1), "views": v.views} for v in top]}, indent=1))
    else:
        for p in result.problems:
            console.print(f"[yellow]{escape(p)}[/]")
        if not top:
            console.print(f"Nothing matched “{escape(result.request)}”. Try other words or a longer --since.")
        else:
            console.print(_research_table(top, f"Found for “{result.request}” · searched "
                                          + ", ".join(f"“{q}”" for q in result.plan.queries), match=True))
            console.print(f"[dim]{result.quota} API units · "
                          + (f"rated by {result.rated_by}" if result.rated_by else "matched by their words")
                          + (f" · {result.weak} that didn't match left out" if result.weak else "")
                          + (f" · {result.skipped} already used left out" if result.skipped else "")
                          + f" · {len(result.fresh)} new · {fresh.ORDER_WORDS.get(result.plan.order, '')} first[/]")
    if args.queue:
        from .jobs import JobDB
        from .pipeline import Runner

        db = JobDB()
        try:
            runner = Runner(db)
            added = sum(runner.add_manual(v.url, v.title, category.id if category else "")
                        for v in result.videos[:args.queue])
        finally:
            db.close()
        if not args.json:
            console.print(f"Queued {added} in Automation" + (f" for {escape(category.name)}" if category else "") + ".")
    return 0


def run_research(argv: list[str]) -> int:
    import json
    import time

    from .policy import Policy, PickLog, decide, picks_path
    from .research import ScanOptions, history_path, scan
    from .trending import CATEGORIES, History, ResearchError, parse_regions

    args = build_research_parser().parse_args(argv)
    if args.list_categories:
        for cid, name in CATEGORIES.items():
            console.print(f"{cid or '-':>3}  {name}")
        return 0
    s = Settings.load()
    opts = ScanOptions.from_settings(s)
    if args.api_key:
        opts.api_key = args.api_key.strip()
    if args.regions:
        opts.regions = parse_regions(args.regions)
        if not opts.regions:
            console.print("[red]--regions needs two-letter country codes, e.g. US,GB[/]")
            return 2
    if args.category is not None:
        opts.category = _category_id(args.category)
    if args.per_region:
        opts.per_region = max(1, min(200, args.per_region))
    if args.min_minutes is not None:
        opts.min_minutes = args.min_minutes
    if args.deep is not None:
        opts.deep_count = max(0, args.deep)
    if not opts.api_key:
        console.print("[red]A YouTube Data API key is needed.[/] Get a free one: enable \"YouTube Data API v3\" "
                      "in a Google Cloud project, create an API key, then pass --api-key, set YOUTUBE_API_KEY, "
                      "or paste it in the app under Settings → Research.")
        return 2

    history = History(history_path())
    picks = PickLog(picks_path()) if args.picks else None
    policy = Policy.from_settings(s)
    log = lambda m: console.print(f"[dim]{escape(m)}[/]")  # noqa: E731
    engine: Engine | None = None
    first = True
    while True:
        try:
            with Progress(TextColumn("[cyan]{task.description}"), BarColumn(), TimeElapsedColumn(),
                          console=console, transient=True, disable=args.json) as bar:
                task = bar.add_task("Checking", total=None)
                result = scan(opts, history, None if args.json else log,
                              lambda m, f: bar.update(task, description=m, completed=f,
                                                      total=1 if f is not None else None))
        except ResearchError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            if first:
                return 1
        except KeyboardInterrupt:
            return 130 if first else 0
        else:
            for problem in result.problems:
                log(problem)
            decisions = {d.video.id: d for d in decide(result.videos, policy, picks, notes=result.notes,
                                                       lessons=learn.for_settings(s))} if picks else None
            top = result.videos[:max(1, args.top)]
            if decisions:
                # Picks first, so they're on screen even when they aren't the highest scorers.
                # Then videos from your channel list and Creative Commons searches, so you can see why
                # they were skipped, then the charts.
                chosen = [v for v in result.videos if decisions[v.id].picked]
                sourced = [v for v in result.videos if v not in chosen and set(v.origins) - {"chart"}]
                rest = [v for v in top if v not in chosen and v not in sourced]
                top = chosen + (sourced + rest)[:max(0, args.top - len(chosen))]
            stamp_now = time.strftime("%H:%M")
            if args.json:
                keep = ("id", "url", "title", "channel", "score", "parts", "reasons", "moments", "regions",
                        "views", "velocity", "recent", "duration", "published", "category", "new", "origins",
                        "license", "category_id")
                rows = []
                for v in top:
                    row = {k: (v.url if k == "url" else getattr(v, k)) for k in keep}
                    if decisions:
                        d = decisions[v.id]
                        row["pick"] = {"picked": d.picked, "reason": d.reason, "rule": d.rule,
                                       **({"learned": d.learned, "bonus": d.bonus} if d.learned else {})}
                    rows.append(row)
                print(json.dumps(rows, indent=1))
            elif first:
                n_picked = sum(d.picked for d in decisions.values()) if decisions else 0
                console.print(_research_table(top, f"Clip-ability · {', '.join(result.regions)}"
                                                   f"{' · ' + CATEGORIES[opts.category] if opts.category else ''}"
                                                   + (f" · {n_picked} picked" if decisions else ""), decisions))
            else:
                fresh = [v for v in result.new if v.score >= s.research_alert]
                if decisions:
                    fresh = [v for v in result.videos if decisions[v.id].picked]
                if fresh:
                    console.print(_research_table(fresh, f"{stamp_now} · {len(fresh)} new worth clipping", decisions))
                else:
                    console.print(f"[dim]{stamp_now} · checked {len(result.videos)} videos, nothing new "
                                  + ("picked" if decisions else f"above {s.research_alert}") + "[/]")
            if args.clip:
                todo = _to_clip(result, decisions, first, s.research_alert, args.clip)
                engine = engine or Engine(s, log=log)
                for v in todo:
                    console.rule(f"[bold]{escape(v.title)}")
                    try:
                        process(v.url, engine, dry_run=False)
                    except Exception as exc:
                        console.print(f"[red]✗ {escape(str(exc))}[/]")
                        continue
                    history.mark_sent(v.id)
                    if picks is not None and decisions:
                        picks.record(decisions[v.id], "cli")
                        picks.save()
                history.save()
        if not args.watch:
            return 0
        first = False
        try:
            time.sleep(max(1.0, args.watch) * 60)
        except KeyboardInterrupt:
            return 0


def _to_clip(result, decisions: dict | None, first: bool, alert: float, limit: int) -> list:
    """What --clip cuts this round: the picks, or else the best unsent videos (later rounds: only
    new arrivals above the alert score)."""
    if decisions:
        return [v for v in result.videos if decisions[v.id].picked][:limit]
    pool = result.videos if first else [v for v in result.new if v.score >= alert]
    return [v for v in pool if not v.sent][:limit]


def build_auto_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ytclip auto",
        description="Run automation: check Research, queue what your picking rules pick, and clip it, "
                    "without anyone clicking. Runs until stopped (Ctrl+C).",
        epilog="Settings → Automation and Settings → Picking control what it does. The app's Automation tab "
               "shows the same queue.",
    )
    p.add_argument("--once", action="store_true", help="do what's due now, then exit (for scheduled runs)")
    p.add_argument("--status", action="store_true", help="show the queue and what automation is doing")
    p.add_argument("--add", nargs="+", metavar="URL", help="queue videos yourself")
    add_category_option(p, metavar="NAME", default="", help="with --add: the category they're for")
    p.add_argument("--retry", nargs="+", metavar="ID", help="try failed videos again (ids from --status)")
    p.add_argument("--remove", nargs="+", metavar="ID", help="take videos out of the queue")
    p.add_argument("--pause", action="store_true", help="pause a running automation")
    p.add_argument("--resume", action="store_true", help="carry on after --pause")
    p.add_argument("--stop", action="store_true", help="ask a running automation to stop")
    p.add_argument("--wait", action="store_true",
                   help="with --stop: wait (up to 90 s) until it has; exit code 10 = it was running and stopped")
    p.add_argument("--try-now", action="store_true",
                   help="end a pause after YouTube asked this PC to sign in, and try again now")
    p.add_argument("--install", action="store_true", help="start automation whenever you sign in to Windows")
    p.add_argument("--uninstall", action="store_true", help="stop starting automation with Windows")
    return p


AUTO_COLOURS = {"working": "cyan", "queued": "white", "done": "green", "failed": "red"}


def _auto_status(db) -> None:
    from .clipability import age_text
    from .pipeline import paused_until, runner_alive

    alive = runner_alive(db)
    state = db.meta("state") or "Stopped"
    paused = db.meta("paused") == "1"
    console.print("[bold]Automation:[/] " + ("[green]running[/]" if alive else "[dim]not running[/]")
                  + (" · [yellow]paused[/]" if paused else "") + (f" · {escape(state)}" if alive else ""))
    blocked = paused_until(db, time.time())
    if blocked and not (alive and state.startswith("Paused until")):
        console.print(f"[yellow]YouTube asked this PC to sign in: no downloads until "
                      f"{time.strftime('%H:%M', time.localtime(blocked))} (ytclip auto --try-now to try sooner).[/]")
    counts = db.counts()
    console.print(f"[dim]{counts['queued']} waiting · {counts['working']} working · {counts['done']} done · "
                  f"{counts['failed']} failed[/]")
    jobs = db.jobs(limit=40)
    if not jobs:
        console.print("Nothing queued yet.")
        return
    from . import categories

    names = {n.id: n.name for n in categories.load(Settings.load())}
    table = Table(show_edge=False, header_style="bold")
    for col in ("Id", "Video", "Status", "Clips", "Updated", "Why it was picked"):
        table.add_column(col, overflow="fold" if col in ("Video", "Status", "Why it was picked") else "ellipsis")
    now = time.time()
    for j in jobs:
        status = f"[{AUTO_COLOURS.get(j.status, 'white')}]{j.status}[/]"
        if j.stage and j.stage not in ("Done", "Waiting"):
            status += f": {escape(j.stage)}"
        if j.error and j.status != "done":
            status += f"\n[dim]{escape(j.error[:200])}[/]"
        where = escape(j.channel) + (f" · {escape(names[j.category_id])}" if j.category_id in names else "")
        table.add_row(j.id, f"{escape(j.title or j.url)}\n[dim]{where}[/]", status,
                      f"{j.clips} (+{j.rejected} rejected)" if j.rejected else str(j.clips or ""),
                      age_text((now - j.updated) / 3600) + " ago", escape(j.permission))
    console.print(table)


def run_auto(argv: list[str]) -> int:
    from . import pipeline
    from .jobs import JobDB

    args = build_auto_parser().parse_args(argv)
    if args.install or args.uninstall:
        err = pipeline.set_start_with_windows(bool(args.install))
        if err:
            console.print(f"[red]{escape(err)}[/]")
            return 1
        console.print("Automation starts whenever you sign in to Windows." if args.install
                      else "Automation no longer starts with Windows.")
        return 0
    db = JobDB()
    if args.pause or args.resume:
        db.set_meta("paused", "1" if args.pause else "0")
        console.print("Paused." if args.pause else "Resumed.")
        return 0
    if args.try_now:
        if not pipeline.paused_until(db, time.time()):
            console.print("Automation isn't paused for a YouTube sign-in.")
            return 0
        n = pipeline.end_pause(db, time.time())
        console.print("Trying YouTube again now" + (f" ({n} video{'s' if n != 1 else ''} waiting)." if n else "."))
        return 0
    if args.stop and args.wait:  # the installer, before it replaces the app's files
        db.close()
        from . import updater

        was = pipeline.runner_alive(JobDB())
        if not updater.stop_automation(resume=False):
            console.print("[red]Automation didn't stop within 90 seconds.[/]")
            return 1
        console.print("Automation stopped. A video in progress went back in the queue." if was
                      else "Automation wasn't running.")
        return 10 if was else 0
    if args.stop:
        db.set_meta("stop", "1")
        console.print("Asked automation to stop. A video in progress goes back in the queue.")
        return 0
    if args.retry or args.remove:
        for job_id in args.retry or args.remove:
            if not db.get(job_id):
                console.print(f"[red]No video {escape(job_id)} in the queue.[/]")
                return 1
            db.retry(job_id) if args.retry else db.remove(job_id)
        console.print("Done.")
        return 0
    if args.add:
        from . import categories

        category = ""
        if args.category:
            mine = categories.load(Settings.load())
            found = next((n for n in mine if n.name.lower() == " ".join(args.category.split()).lower()), None)
            if not found:
                names = ", ".join(n.name for n in mine) or "none yet"
                console.print(f"[red]No category called {escape(args.category)} (yours: {escape(names)}).[/]")
                return 2
            category = found.id
        runner = pipeline.Runner(db)
        for url in args.add:
            try:
                added = runner.add_manual(url, category_id=category)
            except ValueError as exc:
                console.print(f"[red]{escape(url)}: {escape(str(exc))}[/]")
                return 2
            console.print(f"Queued {escape(url)}" if added else f"[yellow]Already queued before: {escape(url)}[/]")
        return 0
    if args.status:
        _auto_status(db)
        return 0
    log = pipeline.file_logger(lambda line: console.print(escape(line), highlight=False))
    from . import speed

    return pipeline.Runner(db, log=log, priority=speed.set_priority, posting=not args.once,
                           telegram=not args.once, remote=not args.once, sports=not args.once).run(once=args.once)


def run_qc(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="ytclip qc", description="Check exported clips again: silence, black or frozen "
                                "picture, wrong length or size, and low AI ratings. Failed clips move to _rejected.")
    p.add_argument("folders", nargs="+", type=Path, help="video folders (with a clips.json) or a folder of them")
    p.add_argument("--min-rating", type=int, help="reject AI ratings under this (0 ignores them)")
    p.add_argument("--size", metavar="WxH", help="the frame size every clip should have, e.g. 1080x1920")
    args = p.parse_args(argv)
    from . import qc

    s = Settings.load()
    size = None
    if args.size:
        try:
            w, h = (int(x) for x in args.size.lower().split("x"))
            size = (w, h)
        except ValueError:
            raise SystemExit("--size must look like 1080x1920")
    indexes = [f for root in args.folders for f in ([root / "clips.json"] if (root / "clips.json").is_file()
                                                       else sorted(root.rglob("clips.json")))]
    if not indexes:
        console.print("[red]No clips.json found in those folders.[/]")
        return 2
    total = {"passed": 0, "rejected": 0, "warned": 0}
    for index in indexes:
        console.print(f"[bold]{escape(str(index.parent))}[/]")
        result = qc.check_folder(index.parent, lambda m: console.print(f"  {escape(m)}"),
                                 s.qc_min_rating if args.min_rating is None else args.min_rating, size)
        total = {k: total[k] + result[k] for k in total}
    console.print(f"[green]{total['passed']} ready[/] · [red]{total['rejected']} rejected[/]"
                  + (f" · [yellow]{total['warned']} with warnings[/]" if total["warned"] else ""))
    return 0


def run_results(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="ytclip results", description="What's working: how your posted clips did, "
                                "from the stats posting reads from TikTok.")
    p.add_argument("--json", action="store_true", help="print the lessons as JSON")
    p.add_argument("--learner", action="store_true",
                   help="what the AI is shown: the top-performing and underperforming openings it learns from, "
                        "and the Video Creator's pace")
    p.add_argument("--query", default="", help="with --learner: the examples a video about this would get")
    p.add_argument("--category", default="", help="with --learner: for this category (its id)")
    p.add_argument("--refresh", action="store_true", help="with --learner: distil results.json again now")
    args = p.parse_args(argv)
    import json

    if args.learner:
        return _learner_report(args)
    lessons = learn.Lessons.load()
    if args.json:
        print(json.dumps({"posts": len(lessons.outcomes), "written_at": lessons.written_at,
                          "channels": [vars(c) for c in sorted(lessons.channels.values(), key=lambda c: -c.bonus)],
                          "notes": lessons.summary_lines(top=0)[1:], "polish_guide": lessons.prompt_text()}, indent=1))
        return 0
    if lessons.empty:
        console.print("No results yet. Once clips have been posted for 48 hours, how they did is written to "
                      f"{escape(str(learn.results_path()))} and shows here.")
        return 0
    for line in lessons.summary_lines(top=0):
        console.print(escape(line))
    table = Table(show_edge=False, header_style="bold")
    for col in ("Channel", "Posts", "Usual views ×", "Pick bonus"):
        table.add_column(col, justify="left" if col == "Channel" else "right")
    for c in sorted(lessons.channels.values(), key=lambda c: -c.bonus):
        colour = "green" if c.bonus >= 1 else "red" if c.bonus <= -1 else "dim"
        table.add_row(escape(c.name), str(c.posts), f"{c.lift:.2f}", f"[{colour}]{c.bonus:+.0f}[/]")
    console.print(table)
    good, bad = lessons.hook_examples()
    for title, rows in (("Hooks that worked", good), ("Hooks that flopped", bad)):
        if rows:
            console.print(f"[bold]{title}[/]: " + "; ".join(f"{escape(h)} ({lift:.1f}×)" for h, lift in rows))
    return 0


def _learner_report(args) -> int:
    import json

    from .intelligence import learner

    s = Settings.load()
    if args.refresh:
        learner.refresh(s, force=True)
    got = learner.report(s, query=args.query, category_id=args.category)
    if args.json:
        print(json.dumps(got, indent=1, default=str))
        return 0
    counts = got["counts"]
    if not counts:
        console.print("Nothing to learn from yet: no posted video has done 2x its account's usual views, or half "
                      "of them, after its first two days.")
        return 0
    console.print(f"{counts.get('top', 0)} top-performing and {counts.get('bottom', 0)} underperforming openings "
                  f"in {escape(got['database'])}.")
    for title, key in (("Top performers", "top"), ("Underperformers", "bottom")):
        rows = got[key]
        if rows:
            console.print(f"[bold]{title}[/]:")
            for e in rows:
                console.print(f"  {e['lift']:.1f}×  {escape(e['text'])}  [dim]({escape(e['shape'])})[/]")
    if got["prompt"]:
        console.print("[bold]What the AI is shown[/]" + (f" for “{escape(args.query)}”" if args.query else "") + ":")
        console.print(escape(got["prompt"]))
    pace = got["pacing"]
    console.print(escape(pace["note"]) if pace.get("wpm") else
                  "[dim]Pacing: as usual until two Creator videos have done 2x their account's usual views.[/]")
    return 0


def run_clean(argv: list[str]) -> int:
    from . import cleanup
    from .jobs import JobDB
    from .settings import CLEANUP_KINDS

    p = argparse.ArgumentParser(prog="ytclip clean", description="Clear out what nothing needs any more: unfinished "
                                "downloads, preview renders, downloaded videos and their analysis, rejected clips, "
                                "posted clips whose files were kept, and clips taken out of the posting queue. "
                                "Without --delete it only "
                                "lists what would go.")
    p.add_argument("--only", metavar="KINDS", default="",
                   help="comma separated: " + ", ".join(k for k, _ in CLEANUP_KINDS)
                        + " (default: all but downloads and analysis)")
    p.add_argument("--older-than", type=float, default=0.0, metavar="DAYS",
                   help="only things older than this (posted clips count from when you posted them)")
    p.add_argument("--delete", action="store_true", help="really remove them")
    p.add_argument("--list", action="store_true", help="list every file")
    args = p.parse_args(argv)
    kinds = cleanup.parse_kinds(args.only) if args.only else \
        [k for k in cleanup.KINDS if k not in ("downloads", "analysis")]
    if not kinds:
        console.print(f"[red]Nothing to clear: --only takes {escape(', '.join(cleanup.KINDS))}[/]")
        return 2
    s = Settings.load()
    db = JobDB()
    try:
        plan = cleanup.plan(s, busy=cleanup.busy_ids(db), older_than_days=args.older_than, kinds=kinds)
    finally:
        db.close()
    for note in plan.notes:
        console.print(f"[yellow]{escape(note)}[/]")
    totals = plan.totals()
    table = Table(show_edge=False, header_style="bold")
    for col in ("What", "How many", "Space"):
        table.add_column(col, justify="left" if col == "What" else "right")
    for k in kinds:
        n, size = totals[k]
        table.add_row(escape(cleanup.LABELS[k]), str(n), cleanup.size_text(size) if n else "")
    console.print(table)
    if plan.kept_for_posting:
        n = plan.kept_for_posting
        console.print(f"[dim]{n} clip{'s' if n != 1 else ''} stay because they're still waiting to post.[/]")
    if args.list:
        for it in plan.items:
            console.print(f"[dim]{escape(cleanup.LABELS[it.kind])}:[/] {escape(str(it.path))}")
    if not plan.items:
        console.print("Nothing to clear.")
        return 0
    if not args.delete:
        console.print(f"Run again with --delete to remove {plan.count(kinds)} item"
                      f"{'s' if plan.count(kinds) != 1 else ''} and free {cleanup.size_text(plan.size(kinds))}.")
        return 0
    result = cleanup.apply(plan, kinds)
    console.print(f"[green]Cleared {escape(result.summary())}.[/]")
    for path, error in result.failed:
        console.print(f"[yellow]Couldn't remove {escape(path)}: {escape(error)}[/]")
    return 1 if result.failed else 0


def run_categories(argv: list[str]) -> int:
    from . import categories
    from .jobs import JobDB
    from .policy import PickLog, picks_path

    p = argparse.ArgumentParser(prog="ytclip categories", description="Your categories (one TikTok account's topic each, "
                                "set up in the app's Categories tab) and how each is doing.")
    p.add_argument("--json", action="store_true", help="print them as JSON")
    args = p.parse_args(argv)
    s = Settings.load()
    all_categories = categories.load(s)
    now = time.time()
    picks = PickLog(picks_path())
    db = JobDB()
    try:
        counts = db.category_counts(now - 7 * 86400)
    finally:
        db.close()
    rows = []
    for n in all_categories:
        refs, _ = n.channel_refs()
        c = counts.get(n.id, {})
        rows.append({"id": n.id, "name": n.name, "account": n.account, "on": n.enabled, "per_day": n.daily,
                     "channels": len(refs), "searches": len(n.search_list()),
                     "picked_today": picks.count_since(now - 86400, category_id=n.id), "waiting": c.get("waiting", 0),
                     "clips_this_week": c.get("clips", 0), "folder": str(s.auto_output_path / n.folder)})
    if args.json:
        import json

        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    if not rows:
        console.print("No categories yet. Add them in ClipMonster's Categories tab: each one is a TikTok account's topic, "
                      "with its own channels, searches, look and folder.")
        return 0
    table = Table(show_edge=False, header_style="bold")
    for col in ("Category", "Account", "Per day", "Sources", "Today", "Waiting", "Clips (7 days)"):
        table.add_column(col, justify="left" if col in ("Category", "Account") else "right")
    for r in rows:
        table.add_row(escape(r["name"]) + ("" if r["on"] else " [dim](off)[/]"), escape(r["account"]),
                      str(r["per_day"]), f"{r['channels']} ch · {r['searches']} cc", str(r["picked_today"]),
                      str(r["waiting"]), str(r["clips_this_week"]))
    console.print(table)
    total = sum(r["per_day"] for r in rows if r["on"])
    if total > s.pick_daily:
        console.print(f"[yellow]Your categories add up to {total} videos a day, but at most {s.pick_daily} are picked a "
                      "day in all (Settings → Picking → Videos picked per day).[/]")
    return 0


def _category_by_name(name: str):
    """(category id, error message) for a category name typed on the command line."""
    from . import categories

    if not name:
        return "", ""
    mine = categories.load(Settings.load())
    found = next((n for n in mine if n.name.lower() == " ".join(name.split()).lower()), None)
    if found:
        return found.id, ""
    return "", f"No category called {name} (yours: {', '.join(n.name for n in mine) or 'none yet'})."


def run_request(argv: list[str]) -> int:
    from . import categories, orders, pipeline
    from .jobs import Job, JobDB
    from .research import youtube_key

    p = argparse.ArgumentParser(
        prog="ytclip request",
        description="Ask automation for clips: how many, how long, for which category, from your links or from videos "
                    "it picks, with special requests for the AI. Automation fills it and shows its progress.",
        epilog='Example: ytclip request 10 --length 30-45 --category Gaming --notes "only clutch plays, no swearing '
               'in the hook"')
    p.add_argument("count", nargs="?", type=int, help="how many clips you want")
    p.add_argument("--length", default="", metavar="SECONDS", help="clip length, e.g. 30-45 (default: the category's "
                   "or Automation's style)")
    add_category_option(p, default="", metavar="NAME", help="the category they're for")
    p.add_argument("--link", nargs="+", action="extend", default=[], metavar="URL", help="videos to clip (default: automation picks "
                   "videos for the category)")
    p.add_argument("--notes", default="", metavar="TEXT", help="special requests for the AI, in plain words")
    p.add_argument("--list", action="store_true", help="show your requests and how far along they are")
    p.add_argument("--cancel", nargs="+", metavar="ID", help="cancel requests (ids from --list)")
    args = p.parse_args(argv)
    db = JobDB()
    try:
        if args.cancel:
            for oid in args.cancel:
                if not db.get_order(oid):
                    console.print(f"[red]No request {escape(oid)}.[/]")
                    return 1
                n = pipeline.Runner(db).cancel(oid)
                console.print(f"Cancelled {escape(oid)}" + (f"; {n} waiting video{'s' if n != 1 else ''} left the queue."
                                                             if n else "."))
            return 0
        if args.list or args.count is None:
            names = {n.id: n.name for n in categories.load(Settings.load())}
            mine = db.orders()
            if not mine:
                console.print("No requests yet. Example: ytclip request 10 --length 30-45 --category Gaming")
                return 0
            table = Table(show_edge=False, header_style="bold")
            for col in ("Id", "Request", "Status", "Ready", "Special requests"):
                table.add_column(col, overflow="fold")
            colours = {"open": "cyan", "done": "green", "short": "yellow", "cancelled": "dim"}
            for o in mine:
                table.add_row(o.id, escape(o.title(names.get(o.category_id, ""))),
                              f"[{colours[o.status]}]{orders.STATUS_WORDS[o.status]}[/]\n[dim]{escape(o.message)}[/]",
                              f"{o.made}/{o.count}", escape(o.notes))
            console.print(table)
            return 0
        category, err = _category_by_name(args.category)
        if err:
            console.print(f"[red]{escape(err)}[/]")
            return 2
        s = Settings.load()
        try:
            if args.length:
                lo, hi = orders.parse_length(args.length)
            else:  # the category's length, else Automation's style
                job = Job(id="", url="", category_id=category)
                style = pipeline.settings_for(job, s)[0]
                lo, hi = style.min_len, style.max_len
            order = orders.make(args.count, lo, hi, category_id=category, links=args.link, notes=args.notes)
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            return 2
        order = pipeline.Runner(db, log=pipeline.file_logger()).place(order)
        console.print(f"Request {order.id}: {escape(order.title(categories.name_of(s, category)))}. {escape(order.message)}.")
        if order.notes and not pipeline.has_ai_key(s):
            console.print("[yellow]Special requests need a Claude or Gemini key (Settings → AI): without one the clips are "
                          "picked by smart signals.[/]")
        if order.picks and not youtube_key(s):
            console.print("[yellow]Automation needs a YouTube Data API key to pick videos for it (Settings → "
                          "Research), or give it links with --link.[/]")
        if not pipeline.runner_alive(db):
            console.print("[dim]Automation isn't running: start it in the app or with ytclip auto.[/]")
        return 0
    finally:
        db.close()


def _weekly_report(as_json: bool) -> int:
    from . import weekly
    from .jobs import JobDB
    from .posting.accounts import Accounts
    from .posting.store import Store

    db = JobDB()
    try:
        w = weekly.build(Store(), Accounts.load(), db)
    finally:
        db.close()
    if as_json:
        import json

        print(json.dumps(w.to_json(), indent=1, ensure_ascii=False))
        return 0
    console.print(f"[bold]{escape(w.headline())}[/]  [dim]({escape(w.span())})[/]")
    for heading, rows in w.sections():
        console.print(f"\n[bold]{escape(heading)}[/]")
        for text, link in rows:
            console.print(f"  {escape(text)}" + (f"  [dim]{escape(link)}[/]" if link else ""))
    return 0


def run_report(argv: list[str]) -> int:
    from . import report
    from .jobs import JobDB

    p = argparse.ArgumentParser(prog="ytclip report", description="What automation did while you were away: "
                                "clips per category and request, rejected clips and why, failures, pauses.")
    p.add_argument("--hours", type=float, default=12.0, help="how far back to look (default 12)")
    p.add_argument("--week", action="store_true", help="the weekly report instead: each TikTok account's views, "
                   "followers, best and weakest post for the past 7 days (what Monday's notification sums up)")
    p.add_argument("--json", action="store_true", help="print it as JSON")
    args = p.parse_args(argv)
    if args.week:
        return _weekly_report(args.json)
    db = JobDB()
    try:
        r = report.build(db, Settings.load(), hours=args.hours)
    finally:
        db.close()
    if args.json:
        import json

        print(json.dumps(r.to_json(), indent=1, ensure_ascii=False))
        return 0
    lines = r.lines()
    console.print(f"[bold]{escape(lines[0])}[/]")
    for line in lines[1:]:
        colour = "red" if line.startswith("Failed") else "yellow" if line.startswith(("YouTube", "Paused",
                                                                                     "Stopped")) else ""
        console.print(f"  [{colour}]{escape(line)}[/]" if colour else f"  {escape(line)}")
    return 0


def run_doctor(argv: list[str]) -> int:
    from . import doctor
    from .jobs import JobDB

    p = argparse.ArgumentParser(prog="ytclip doctor", description="Check that everything automation needs is in "
                                "place: ffmpeg, the downloader, keys, folders, free space, TikTok posting, categories.")
    p.add_argument("--offline", action="store_true", help="don't test the keys online")
    p.add_argument("--json", action="store_true", help="print the checks as JSON")
    args = p.parse_args(argv)
    db = JobDB()
    try:
        checks = doctor.check(Settings.load(), db, online=not args.offline)
    finally:
        db.close()
    if args.json:
        import json

        print(json.dumps([c.to_json() for c in checks], indent=1, ensure_ascii=False))
    else:
        marks = {doctor.GOOD: "[green]✓[/]", doctor.WARN: "[yellow]⚠[/]", doctor.BAD: "[red]✗[/]"}
        for c in checks:
            console.print(f"{marks[c.level]} [bold]{escape(c.name)}[/]: {escape(c.detail)}"
                          + (f"\n    [dim]Fix: {escape(c.fix)}[/]" if c.fix else ""))
        bad = sum(c.level == doctor.BAD for c in checks)
        warn = sum(c.level == doctor.WARN for c in checks)
        console.print("All good." if not (bad or warn) else f"{bad} problem{'s' if bad != 1 else ''}, "
                      f"{warn} warning{'s' if warn != 1 else ''}.")
    return 1 if any(c.level == doctor.BAD for c in checks) else 0


def run_local_ai(argv: list[str]) -> int:
    """`ytclip local-ai`: what the local AI would run on, download its model, and try it."""
    from .intelligence import hardware, llama_engine, models

    p = argparse.ArgumentParser(prog="ytclip local-ai",
                                description="The AI that runs on this PC: free, private, no key. It finds clips "
                                "from the transcript like Claude or Gemini do (Settings → AI → Local).")
    p.add_argument("action", nargs="?", choices=("status", "download", "test"), default="status",
                   help="status (default): this PC and the model it would use; download: fetch the model now; "
                        "test: load it and ask it to pick a clip from a short sample transcript")
    p.add_argument("--model", choices=[k for k, _ in models.CHOICES if k != "custom"],
                   help="which model (default: Settings → AI → Local model)")
    p.add_argument("--file", type=Path, help="use your own GGUF file instead")
    p.add_argument("--cpu", action="store_true", help="don't use the graphics card")
    args = p.parse_args(argv)
    s = Settings.load()
    if args.file:
        s = s.copy(local_model="custom", local_model_path=str(args.file))
    elif args.model:
        s = s.copy(local_model=args.model)
    if args.cpu:
        s = s.copy(local_gpu=False)
    hw = hardware.detect()
    model, path = models.resolve(s, hw)
    exe = llama_engine.server_binary()
    console.print(f"[bold]This PC:[/] {escape(hw.describe())}")
    mem = hardware.memory_now(hw, exe if hw.has_gpu else None)
    if hw.has_gpu:
        free = (f"{mem.vram_free_gb:.1f} GB free now (from {mem.source})" if mem.vram_free_gb is not None
                else "free memory not reported")
        console.print(f"[bold]Graphics memory:[/] {hw.vram_gb:.1f} GB on {escape(hw.gpu_name)} (from "
                      f"{escape(hw.vram_source)}), {escape(free)}")
        for card in hw.cards:
            console.print(f"  [dim]{escape(card.source)}: {escape(card.name)}, {card.total_gb:.1f} GB"
                          + (f", {card.free_gb:.1f} GB free" if card.free_gb is not None else "") + "[/]")
    console.print(f"[bold]RAM free now:[/] {mem.ram_free_gb:.1f} of {mem.ram_total_gb:.0f} GB")
    console.print(f"[bold]Runs with:[/] {escape(llama_engine.backend_name() or llama_engine.missing_backend())}")
    name = model.label if model else (path.name or "no file chosen")
    console.print(f"[bold]Model:[/] {escape(name)}  [dim]{escape(str(path))}[/]")
    console.print(f"[bold]Status:[/] {escape(llama_engine.status(s))}")
    if args.action == "status":
        from rich.table import Table

        table = Table(title="The models, on this PC", title_justify="left", header_style="bold")
        for col in ("Model", "Tier", "Download", "Here", "On this PC", "Licence"):
            table.add_column(col)
        for m in models.MODELS.values():
            fit = models.runs_on_card(m, hw, mem.ram_free_gb)
            here = "downloaded" if models.installed(models.folder() / m.file) else ""
            pick = " (picked for this PC)" if m.key == models.choose(hw).key else ""
            table.add_row(m.short + pick, models.TIERS[m.tier][0], f"{m.size_gb:.1f} GB", here,
                          fit.describe("the card") if fit.fits else "too big for this PC's free memory", m.licence)
        console.print(table)
        return 0
    if args.action == "download" and model is None:
        console.print("[yellow]A custom file isn't downloaded: choose it with --file.[/]")
        return 2
    better = models.better_choice(s, hw) if args.action == "download" else None
    if better is not None:  # "Pick for this PC" is on a model downloaded earlier: fetch the one that suits it
        model, path = better, models.folder() / better.file
        console.print(f"[bold]Downloading the model that suits this PC best:[/] {escape(better.label)}")
    if model is not None and not models.installed(path):
        if not models.free_space_ok(model, path):
            console.print(f"[red]Not enough free space for {model.size_gb:.1f} GB.[/]")
            return 1
        with Progress(TextColumn("[cyan]{task.description}"), BarColumn(), TextColumn("{task.percentage:>3.0f}%"),
                      TimeElapsedColumn(), console=console, transient=True) as bar:
            task = bar.add_task(f"Downloading {model.name}", total=1.0)
            try:
                models.download(model, lambda m, f: bar.update(task, completed=f or 0.0))
            except models.DownloadError as exc:
                console.print(f"[red]{escape(str(exc))}[/]")
                return 1
        console.print("[green]✓ Model downloaded.[/]")
    if args.action == "download":
        return 0
    from .intelligence import transcript_parser

    sample = "\n".join((
        "[0.0-4.0] So I quit my job in March.",
        "[4.0-9.0] Everyone told me it was the dumbest thing I'd ever do.",
        "[9.0-15.0] And for six months they were right, I made nothing.",
        "[15.0-22.0] Then one video took off, eleven million views in two days.",
        "[22.0-26.0] And here's the thing nobody tells you about that.",
        "[26.0-31.0] The money from that one video was forty dollars.",
        "[31.0-33.0] Forty.",
        "[33.0-41.0] What paid was what came after: the next fifty videos had an audience waiting.",
        "[41.0-45.0] Anyway, let's talk about the setup I use now."))
    started = time.monotonic()
    try:
        with console.status("Loading the model and asking it to pick a clip…"):
            reply = llama_engine.ask_json(
                s, "You find the most engaging self-contained moment in a transcript for a short video.",
                f"Transcript:\n{sample}\n\nPick 1 clip, 15 to 60 seconds long.", transcript_parser.schema(1, 15, 60),
                max_tokens=1200, log=lambda m: console.print(f"[dim]{escape(m)}[/]"),
                prompt_schema=transcript_parser.schema(1, 15, 60, strict=False))
    except llama_engine.LocalAIError as exc:
        console.print(f"[red]✗ {escape(str(exc))}[/]")
        return 1
    finally:
        llama_engine.shutdown()
    console.print(f"[green]✓ Answered in {time.monotonic() - started:.0f} s on {escape(reply.device or 'this PC')}"
                  f" ({escape(reply.backend)}).[/]")
    try:
        console.print_json(json.dumps(json.loads(reply.text)))
    except ValueError:
        console.print(escape(reply.text.strip()[:1200]))
    return 0


def run_whisper_test(argv: list[str]) -> int:
    from . import speed, whisper_cpp
    from . import transcribe as whisper
    from .ffmpeg import probe

    s = Settings.load()
    p = argparse.ArgumentParser(prog="ytclip whisper-test", description="Time each Whisper model on this PC, on a "
                                "minute of one of your videos, so you can pick the quickest one that's good enough.")
    p.add_argument("video", type=Path, help="a video or audio file (a download in the downloads folder will do)")
    p.add_argument("--models", default="small,medium,large-v3-turbo",
                   help="models to try, comma-separated (default: small,medium,large-v3-turbo)")
    p.add_argument("--seconds", type=float, default=60.0, help="how much of the video to type out (default 60)")
    p.add_argument("--speed", choices=speed.LEVELS, default=speed.level(s.speed), help="Speed to test at")
    args = p.parse_args(argv)
    if not args.video.is_file():
        console.print(f"[red]No file {escape(str(args.video))}[/]")
        return 2
    if not whisper.available():
        console.print("[red]Whisper isn't installed in this copy of ClipMonster.[/]")
        return 1
    duration = probe(args.video).duration
    start = max(0.0, duration / 2 - args.seconds / 2)
    span = [(start, min(duration, start + args.seconds))]
    length = span[0][1] - span[0][0]
    threads = speed.threads(args.speed)
    ways = [("CPU", False)]
    if args.speed == "full" and not whisper_cpp.unavailable():
        ways.append(("graphics card", True))
    console.print(f"Typing out {length:.0f} s from the middle of {escape(args.video.name)}, at "
                  f"{speed.WORDS[args.speed]} ({threads} CPU threads).")
    table = Table(show_edge=False, header_style="bold")
    for col in ("Model", "On", "Took", "Speed", "First words"):
        table.add_column(col)
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        for where, gpu in ways:
            notes: list[str] = []
            began = time.monotonic()
            try:
                with console.status(f"{model} on the {where}…"):
                    _, words = whisper.transcribe(args.video, model, s.whisper_language, duration, lambda m, f: None,
                                                  threads=threads, spans=span, gpu=gpu, log=notes.append)
            except Exception as exc:
                table.add_row(model, where, "failed", "", escape(str(exc)[:60]))
                continue
            took = time.monotonic() - began
            if gpu and any("CPU" in n for n in notes):  # it fell back to the CPU: don't credit the card
                where = "CPU (card failed)"
            table.add_row(model, where, f"{took:.1f} s", f"{length / took:.1f}x real time",
                          escape(" ".join(w.text for w in words[:8])))
    console.print(table)
    console.print("[dim]The first run of a model includes downloading it. Pick one in Settings → Transcription.[/]")
    return 0


def run_posting(argv: list[str]) -> int:
    from .posting import jobs, live, records, schedule
    from .posting import config as posting_config
    from .posting.accounts import Accounts
    from .posting.store import Store

    p = argparse.ArgumentParser(
        prog="ytclip posting", description="TikTok posting: each account, its queue, and what was posted. Posting "
        "runs by itself in automation's background process (ytclip auto run); accounts are added and logged in "
        "in the app's Accounts tab.")
    p.add_argument("--posted", type=int, default=10, metavar="N", help="show the last N posts (default 10)")
    args = p.parse_args(argv)
    accounts = Accounts.load()
    if not len(accounts):
        console.print("No TikTok account yet. Add one in the app's Accounts tab and log in to TikTok.")
        return 0
    store = Store()
    s = posting_config.Settings.load()
    now = time.time()
    jobs.replan(store, accounts, s, now)
    table = Table(show_edge=False, header_style="bold")
    for col in ("Account", "Login", "Waiting", "Next post", "Followers"):
        table.add_column(col)
    for a in accounts:
        q = [i for i in store.queue(a.id) if i.status == "queued"]
        nxt = jobs.next_post(store, a.id)
        prof = store.profile(a.id)
        table.add_row(escape(a.label), "[green]logged in[/]" if a.logged_in else "[yellow]not logged in[/]",
                      str(len(q)), "paused" if not a.autopilot else schedule.fmt_when(nxt.planned_at, now) if nxt
                      else "—", live.fmt(prof.followers) if prof.fetched_at else "—")
    console.print(table)
    posted = store.posted()[:max(0, args.posted)]
    if posted:
        t2 = Table(show_edge=False, header_style="bold", title="Posted")
        for col in ("When", "Clip", "Account", "Views", "Files"):
            t2.add_column(col)
        for it in posted:
            v = store.video_for(it)
            t2.add_row(schedule.fmt_when(it.posted_at, now), escape(it.headline), escape(accounts.label_for(it.account)),
                       live.fmt(v.views) if v else "", "cleared" if it.cleaned_at else "kept" if it.keep_reason
                       else "waiting to see it on TikTok" if not it.tiktok_id else "")
        console.print(t2)
    console.print(f"[dim]Records: {escape(str(records.records_folder(s)))}[/]")
    return 0


def run_sign_in(argv: list[str], wait: float = 1800.0, poll: float = 2.0) -> int:
    from . import pipeline, youtube_login
    from .jobs import JobDB

    p = argparse.ArgumentParser(prog="ytclip sign-in", description="Sign ClipMonster in to YouTube: opens Firefox "
                                "with a profile kept just for ClipMonster. Sign in (a spare Google account is "
                                "best), then close that Firefox; downloads use the sign-in from then on.")
    p.add_argument("--status", action="store_true", help="only say whether it's signed in")
    p.add_argument("--forget", action="store_true", help="delete ClipMonster's sign-in and stop using it")
    args = p.parse_args(argv)
    s = Settings.load()
    if args.forget:
        try:
            youtube_login.forget()
        except OSError as exc:
            console.print(f"[red]✗ {escape(str(exc))}[/]")
            return 1
        if s.cookies_browser == youtube_login.CHOICE:
            s.copy(cookies_browser="").save()
        console.print("ClipMonster's YouTube sign-in is gone; downloads aren't signed in now.")
        return 0
    if not args.status:
        try:
            youtube_login.open_sign_in()
        except FileNotFoundError:
            console.print(f"[red]✗ Firefox isn't installed.[/] Get it from {youtube_login.GET_FIREFOX_URL} and run "
                          "this again.")
            return 1
        console.print("Firefox is open on Google's sign-in page. Sign in to YouTube, then close that Firefox window.")
        end = time.monotonic() + wait
        time.sleep(poll * 2)  # Firefox takes a moment to lock its profile
        while youtube_login.firefox_open() and time.monotonic() < end:
            time.sleep(poll)
    got = youtube_login.read_sign_in()
    if not got.signed_in:
        console.print("[yellow]⚠ ClipMonster's Firefox isn't signed in to YouTube.[/] Run [bold]ytclip sign-in[/] "
                      "to sign in.")
        return 1
    if s.cookies_browser != youtube_login.CHOICE:
        s.copy(cookies_browser=youtube_login.CHOICE).save()
    db = JobDB()
    try:
        n = pipeline.end_pause(db, time.time()) if pipeline.paused_until(db, time.time()) else 0
    finally:
        db.close()
    console.print("[green]✓ Signed in to YouTube.[/] Downloads use this sign-in"
                  + (f", and {n} waiting video{'s carry' if n != 1 else ' carries'} on now." if n else "."))
    return 0


def run_create(argv: list[str]) -> int:
    """ytclip create: an original video from a prompt, made on this PC (creator/)."""
    import json as _json

    from .creator import director, orchestrator, visual_generator, voice_engine
    from .ffmpeg import Cancelled

    p = argparse.ArgumentParser(
        prog="ytclip create", description="Make an original faceless video from a prompt, on this PC: the local AI "
        "writes the script, a neural voice reads it, pictures are made and animated, music and sound effects are "
        "mixed in, and kinetic captions burnt on. It goes to the category's Automation folder and its account's "
        "posting queue, labelled AI-generated.",
        epilog='Examples:  ytclip create "Dark psychology secrets" --category Psychology --voice deep_male '
               '--creator-rewards\n           ytclip create --file prompts.txt --category DailyFacts --schedule',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("prompt", nargs="?", default="", help="what the video is about")
    p.add_argument("--file", type=Path, help="one prompt per line (# starts a comment): a video each")
    p.add_argument("--script", type=Path, help="your own narration instead of the AI's (a blank line starts a scene)")
    p.add_argument("--storyboard", type=Path, help="make a storyboard.json as it is (from --storyboard-only, edited)")
    p.add_argument("--title", default="", help="the video's title (default: the AI's)")
    p.add_argument("--category", default="", help="category name or id: its folder and its TikTok account")
    p.add_argument("--voice", default="", help="voice (--list-voices), e.g. deep_male, documentary, female")
    p.add_argument("--duration", type=float, default=0, help="seconds to aim for (default: Settings → Creator)")
    p.add_argument("--visuals", choices=visual_generator.BACKENDS, default="",
                   help="where pictures come from (default: Settings → Creator)")
    p.add_argument("--style", choices=["", *director.STYLES], default="", help="the look (default: from the category)")
    rewards = p.add_mutually_exclusive_group()
    rewards.add_argument("--creator-rewards", dest="rewards", action="store_true", default=None,
                         help="keep it 62-85 s, over the minute Creator Rewards needs")
    rewards.add_argument("--no-creator-rewards", dest="rewards", action="store_false")
    p.add_argument("--schedule", action="store_true", help="queue it for automation to make instead of making it now")
    p.add_argument("--no-post", action="store_true", help="save it in Output/Created instead of the posting queue")
    p.add_argument("--storyboard-only", action="store_true", help="only write the script and storyboard (JSON)")
    p.add_argument("--list-voices", action="store_true", help="the voices, and which engines work here")
    p.add_argument("--check", action="store_true", help="what's ready on this PC: AI, voices, pictures")
    p.add_argument("--setup-pictures", action="store_true",
                   help="set up AI pictures: PyTorch for this PC's graphics card and SDXL-Turbo (about 10 GB)")
    args = p.parse_args(argv)
    s = Settings.load()
    if args.setup_pictures:
        from .creator import picture_engine

        try:
            with console.status("Setting up AI pictures") as spin:
                picture_engine.install(log=lambda m: console.print(escape(m)),
                                       progress=lambda m, f: spin.update(escape(m) + (f" {f:.0%}" if f else "")))
        except (picture_engine.EngineError, OSError) as exc:
            console.print(f"[red]✗ Couldn't set up AI pictures:[/] {escape(str(exc))}")
            return 1
        console.print(f"[green]✓ AI pictures are {escape(picture_engine.describe())}.[/]")
        return 0
    if args.list_voices or args.check:
        engines = voice_engine.available(s)
        console.print("[bold]Voices[/] (--voice):")
        for v in voice_engine.VOICES.values():
            console.print(f"  {v.key:<18} {escape(v.label)} [dim]· {escape(v.blurb)}[/]")
        console.print("[bold]Voice engines:[/] " + ", ".join(
            f"{voice_engine.ENGINE_NAMES[k]} [green]ready[/]" if not why else
            f"{voice_engine.ENGINE_NAMES[k]} [dim]({escape(why)})[/]" for k, why in engines.items()))
        if args.check:
            pics = visual_generator.available(s)
            console.print("[bold]Pictures:[/] " + ", ".join(
                f"{visual_generator.BACKEND_NAMES[k]} [green]ready[/]" if not why else
                f"{visual_generator.BACKEND_NAMES[k]} [dim]({escape(why)})[/]" for k, why in pics.items()))
            writers = director.script_writers(s)
            console.print("[bold]Script writer:[/] " + (", ".join(writers) if writers else
                                                       f"[yellow]{escape(director.no_writer_text(s))}[/]"))
            from .creator import picture_engine

            console.print("[bold]AI pictures engine:[/] " + escape(picture_engine.describe()) +
                          ("" if picture_engine.ready() else " [dim](ytclip create --setup-pictures)[/]"))
            console.print("[bold]Look it up online first:[/] " + ("on (Google News, Bing News, Wikipedia)" if
                                                                s.creator_web else "off"))
        return 0
    prompts = [args.prompt] if args.prompt.strip() else []
    if args.file:
        prompts += [ln.strip() for ln in args.file.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")]
    script = args.script.read_text(encoding="utf-8") if args.script else ""
    board = _json.loads(args.storyboard.read_text(encoding="utf-8")) if args.storyboard else None
    if not prompts and not script and board is None:
        p.print_help()
        return 2
    category = orchestrator.find_category(s, args.category) if args.category else None
    if args.category and category is None:
        console.print(f"[yellow]⚠ There's no category called {escape(args.category)}.[/] The video goes to an "
                      f"Automation folder of that name; posting queues it only if you have a single account.")
    base = dict(title=args.title, category=category.id if category else args.category, voice=args.voice,
                seconds=args.duration, visuals=args.visuals, style=args.style, rewards=args.rewards,
                post=not args.no_post)
    requests = ([orchestrator.CreateRequest(prompt=x, **base) for x in prompts] if not (script or board) else
                [orchestrator.CreateRequest(prompt=prompts[0] if prompts else "", script=script, storyboard=board, **base)])
    if args.schedule:
        from .jobs import JobDB

        db = JobDB()
        try:
            for req in requests:
                orchestrator.queue(db, req, category.id if category else "")
        finally:
            db.close()
        console.print(f"[green]✓ {len(requests)} video{'s' if len(requests) != 1 else ''} queued.[/] Automation makes "
                      "them in turn (start it in the Automation tab or with [bold]ytclip auto run[/]).")
        return 0
    log = lambda m: console.print(f"[dim]{escape(m)}[/]")  # noqa: E731
    last = {"m": ""}

    def progress(msg: str, frac) -> None:
        head = msg.split(":")[0]
        if head != last["m"]:
            last["m"] = head
            console.print(f"[cyan]…[/] {escape(head)}")

    failed = 0
    for i, req in enumerate(requests, 1):
        if len(requests) > 1:
            console.rule(f"[bold]Video {i}/{len(requests)}: {escape(req.label())}")
        maker = orchestrator.VideoCreatorOrchestrator(s, log=log, progress=progress)
        try:
            if args.storyboard_only:
                sb = maker.storyboard(req)
                console.print(sb.dumps(), markup=False, highlight=False)
                continue
            result = maker.create(req)
        except KeyboardInterrupt:
            console.print("[red]Interrupted.[/]")
            return 130
        except Cancelled:
            return 130
        except Exception as exc:  # noqa: BLE001
            failed += 1
            console.print(f"[red]✗ {escape(str(exc))}[/]")
            continue
        c = result.checks
        console.print(f"[green]✓ {escape(result.storyboard.title)}[/] · {result.duration:.0f} s · {c.get('lufs')} LUFS · "
                      f"{c.get('size_mb')} MB\n  {escape(str(result.video))}")
        for w in result.warnings:
            console.print(f"  [yellow]⚠ {escape(w)}[/]")
    return 1 if failed else 0


def run_studio(argv: list[str]) -> int:
    """ytclip studio: a finished video and its editable timeline from a prompt or an article (studio/)."""
    import json as _json

    from .creator import director, orchestrator, visual_generator, voice_engine
    from .ffmpeg import Cancelled
    from .intelligence import agent_router
    from .studio import orchestrator as studio
    from .studio import timeline_builder, timeline_schema, visual_sourcer

    p = argparse.ArgumentParser(
        prog="ytclip studio",
        description="Make a finished video from a prompt or a public article's address: the script is written (an "
        "article is retold in new words, its address kept as the source), a voice reads it, each scene gets "
        "footage or a picture (your B-roll, Pexels and Pixabay with your keys, or drawn on this PC), and the "
        "whole edit is laid out as a timeline: camera moves, transitions, kinetic captions, music ducked under "
        "the voice, sound effects. One ffmpeg run renders it. timeline.json stays in the video's folder: edit "
        "it and render it again with --timeline.",
        epilog="Examples:\n"
               '  ytclip studio "Why the Roman Empire really fell" --category History\n'
               '  ytclip studio --url https://example.org/a-public-article --seconds 60 --captions karaoke\n'
               '  ytclip studio "How lighthouses worked before electricity" --format long --seconds 480 --no-post\n'
               '  ytclip studio "3 money habits" --visuals stock --tts kokoro --voice deep_male --ai local_first\n'
               '  ytclip studio --timeline "Videos/ClipMonster/Output/Created/2026-11-10 Lighthouses [c-1a2b3c4d]"\n'
               "  ytclip studio --check",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("prompt", nargs="?", default="", help="what the video is about (with --url: the angle to take)")
    p.add_argument("--url", default="", help="a public article to retell (pages behind a sign-in or a paywall, and "
                                             "pages whose robots.txt says no, aren't read)")
    p.add_argument("--format", choices=["short", "long"], default="",
                   help="short: vertical 9:16, about a minute (the default). long: wide 16:9, minutes")
    p.add_argument("--orientation", choices=["vertical", "landscape", "square"], default="",
                   help="the shape, when it isn't the format's")
    p.add_argument("--seconds", "--duration", dest="seconds", type=float, default=0,
                   help="seconds to aim for (default: Settings → Creator for a short, 300 for a long)")
    p.add_argument("--fps", type=int, default=30, choices=[24, 25, 30, 50, 60])
    p.add_argument("--captions", choices=list(timeline_builder.CAPTION_PRESETS), default="",
                   help="caption style (default: Settings → Creator → Studio captions)")
    p.add_argument("--tts", choices=["auto", *voice_engine.ENGINE_CLASSES], default="",
                   help="voice engine (default: Settings → Creator). elevenlabs sends the script to ElevenLabs with "
                        "your key and is only used when named here or in Settings")
    p.add_argument("--voice", default="", help="voice (ytclip create --list-voices), e.g. deep_male, documentary")
    p.add_argument("--visuals", choices=list(visual_sourcer.CHOICES), default="",
                   help="where pictures come from. stock: stock footage first for every scene")
    p.add_argument("--ai", choices=[x for x in agent_router.PRIORITIES if x != "settings"], default="",
                   help="who writes the script (default: Settings → Creator → Script writer)")
    p.add_argument("--style", choices=["", *director.STYLES], default="", help="the look (default: from the category)")
    p.add_argument("--title", default="", help="the video's title (default: the AI's)")
    p.add_argument("--category", default="", help="category name or id: its folder and its TikTok account")
    p.add_argument("--script", type=Path, help="your own narration instead of the AI's (a blank line starts a scene)")
    p.add_argument("--storyboard", type=Path, help="make a storyboard.json as it is (from --storyboard-only, edited)")
    rewards = p.add_mutually_exclusive_group()
    rewards.add_argument("--creator-rewards", dest="rewards", action="store_true", default=None,
                         help="keep a vertical video 62-85 s, over the minute Creator Rewards needs")
    rewards.add_argument("--no-creator-rewards", dest="rewards", action="store_false")
    p.add_argument("--schedule", action="store_true", help="queue it for automation to make instead of making it now")
    p.add_argument("--no-post", action="store_true", help="save it in Output/Created instead of the posting queue")
    p.add_argument("--storyboard-only", action="store_true", help="only write the script and storyboard (JSON)")
    p.add_argument("--timeline", type=Path, help="render a timeline.json (or the video folder holding one) again")
    p.add_argument("--out", type=Path, help="with --timeline: where the video goes (default: beside the timeline); "
                                            "with --storyboard-only: the file the storyboard is saved to")
    p.add_argument("--schema", action="store_true", help="print the timeline's JSON Schema")
    p.add_argument("--check", action="store_true", help="what's ready on this PC: AI, voices, pictures, stock keys")
    args = p.parse_args(argv)
    if args.schema:
        console.print(_json.dumps(timeline_schema.JSON_SCHEMA, indent=2), markup=False, highlight=False)
        return 0
    s = Settings.load()
    # With --storyboard-only the storyboard is what's printed (to a file, often): what's happening goes to
    # the other stream, so it never lands inside the JSON.
    said = Console(stderr=True) if args.storyboard_only else console
    log = lambda m: said.print(f"[dim]{escape(m)}[/]")  # noqa: E731
    last = {"m": ""}

    def progress(msg: str, frac) -> None:
        head = msg.split(":")[0]
        if head != last["m"]:
            last["m"] = head
            said.print(f"[cyan]…[/] {escape(head)}")

    def read_text(path: Path) -> str:
        """A text file as people's tools save it: UTF-8 with or without a byte-order mark, or the UTF-16
        Windows PowerShell's ">" writes."""
        raw = path.read_bytes()
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return raw.decode("utf-16")
        return raw.decode("utf-8-sig")

    if args.check:
        ready = lambda why: "[green]ready[/]" if not why else f"[dim]({escape(why)})[/]"  # noqa: E731
        writers = director.script_writers(s)
        console.print("[bold]Script writer:[/] " + (", ".join(writers) if writers else
                                                   f"[yellow]{escape(director.no_writer_text(s))}[/]"))
        console.print("[bold]Voice engines:[/] " + ", ".join(
            f"{voice_engine.ENGINE_NAMES.get(k, k)} {ready(why)}" for k, why in voice_engine.available(s).items()))
        console.print("[bold]Pictures:[/] " + ", ".join(
            f"{visual_generator.BACKEND_NAMES[k]} {ready(why)}" for k, why in visual_generator.available(s).items()))
        stock = visual_sourcer.Stock(s, Path("."))  # only asked what it has; nothing is searched or saved
        names = [visual_sourcer.PROVIDER_NAMES[x] for x in visual_sourcer.PROVIDERS if stock.keys.get(x)]
        console.print("[bold]Stock footage:[/] " + (f"[green]ready[/] ({' and '.join(names)}, your key)"
                                                   if not stock.missing() else f"[dim]{escape(stock.missing())}[/]"))
        from importlib.util import find_spec

        readers = [name for name, module in (("trafilatura", "trafilatura"), ("readability-lxml", "readability"))
                   if find_spec(module)]
        console.print("[bold]Article reader:[/] " + ", ".join([*readers, "built-in"])
                      + " [dim](the first that reads a page well is used)[/]")
        return 0
    if args.timeline:
        try:
            done = studio.render_timeline(args.timeline, args.out, settings=s, log=log, progress=progress)
        except KeyboardInterrupt:
            console.print("[red]Interrupted.[/]")
            return 130
        except Cancelled:
            return 130
        except (orchestrator.CreatorError, OSError) as exc:
            console.print(f"[red]✗ {escape(str(exc))}[/]")
            return 1
        console.print(f"[green]✓ Rendered again[/] · {done.duration:.0f} s · {escape(done.encoder)}\n"
                      f"  {escape(str(done.path))}")
        for w in done.warnings:
            console.print(f"  [yellow]⚠ {escape(w)}[/]")
        return 0
    script, board = "", None
    for file, what in ((args.script, "script"), (args.storyboard, "storyboard")):
        if not file:
            continue
        try:
            text = read_text(file)
            if what == "script":
                script = text
            else:
                board = _json.loads(text)
                if not isinstance(board, dict):
                    raise ValueError("it isn't a storyboard (a JSON object, as --storyboard-only writes)")
        except (OSError, ValueError) as exc:
            why = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            console.print(f"[red]✗ {escape(str(file))} can't be read as a {what}: {escape(why)}[/]")
            return 1
    if not (args.prompt.strip() or args.url.strip() or script or board is not None):
        p.print_help()
        return 2
    category = orchestrator.find_category(s, args.category) if args.category else None
    if args.category and category is None:
        console.print(f"[yellow]⚠ There's no category called {escape(args.category)}.[/] The video goes to an "
                      f"Automation folder of that name; posting queues it only if you have a single account.")
    req = studio.StudioRequest(
        prompt=args.prompt, url=args.url, script=script, storyboard=board, title=args.title,
        category=category.id if category else args.category, voice=args.voice, seconds=args.seconds,
        visuals=args.visuals, style=args.style, rewards=args.rewards, post=not args.no_post, format=args.format,
        orientation=args.orientation, captions=args.captions, tts=args.tts, ai=args.ai, fps=args.fps)
    maker = studio.StudioOrchestrator(s, log=log, progress=progress)
    try:
        if args.schedule:
            from .jobs import JobDB

            maker.accept(req)  # a request that can't be made is refused now, not at three in the morning
            db = JobDB()
            try:
                orchestrator.queue(db, req, category.id if category else "")
            finally:
                db.close()
            console.print("[green]✓ Queued.[/] Automation makes it in turn (start it in the Automation tab or with "
                          "[bold]ytclip auto run[/]).")
            return 0
        if args.storyboard_only:
            made = maker.storyboard(req)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(made.dumps() + "\n", encoding="utf-8")
                said.print(f"[green]✓ Storyboard saved[/] · {len(made.scenes)} scenes\n  {escape(str(args.out))}\n"
                           f"  [dim]Edit it, then make it with: ytclip studio --storyboard \"{escape(str(args.out))}\"[/]")
            else:
                # Plain ASCII, unwrapped: it survives any console's encoding and being sent to a file with ">".
                sys.stdout.write(_json.dumps(made.to_json(), indent=2, ensure_ascii=True) + "\n")
                sys.stdout.flush()
            return 0
        result = maker.create(req)
    except KeyboardInterrupt:
        console.print("[red]Interrupted.[/]")
        return 130
    except Cancelled:
        return 130
    except Exception as exc:  # noqa: BLE001
        where = f" ({studio.STATE_WORDS.get(maker.failed_at, maker.failed_at).lower()})" if maker.failed_at else ""
        console.print(f"[red]✗ {escape(str(exc))}[/][dim]{escape(where)}[/]")
        return 1
    c = result.checks
    console.print(f"[green]✓ {escape(result.storyboard.title)}[/] · {result.duration:.0f} s · "
                  f"{c.get('width')}x{c.get('height')} · {c.get('lufs')} LUFS · {c.get('size_mb')} MB\n"
                  f"  {escape(str(result.video))}")
    if (result.folder / "timeline.json").is_file():
        console.print(f"  [dim]Edit {escape(str(result.folder / 'timeline.json'))} and render it again with "
                      f"ytclip studio --timeline[/]")
    for line in maker.visual_credits:
        console.print(f"  [dim]{escape(line)}[/]")
    for w in result.warnings:
        console.print(f"  [yellow]⚠ {escape(w)}[/]")
    return 0


def run_sports_montage(argv: list[str]) -> int:
    """ytclip sports-montage: one 65-85 s vertical recap reel of a game's best plays (sportsdesk/reel.py)."""
    from . import categories
    from .creator import voice_engine
    from .engine import Cancelled
    from .sportsdesk import games as G, montage, rank, reel as R

    p = argparse.ArgumentParser(
        prog="ytclip sports-montage",
        description="Make one vertical recap reel of a game: its 4 to 7 best plays cut to 65-85 s, an opening "
        "teaser, a narrator reading a two-sentence story of the game, countdown labels, the score bug and player "
        "badges, the camera following the action, the crowd lifted at each big moment and the loudness set for "
        "TikTok. The plays are found and ranked as the Sports tab does (by your AI, local or with a key, when one "
        "is set up).",
        epilog='Examples:  ytclip sports-montage "https://youtu.be/…" --sport soccer --creator-rewards '
               '--output-dir "Videos/Sports"\n           ytclip sports-montage game.mp4 --sport basketball '
               '--voice off --order timeline\n           ytclip sports-montage game.mp4 --sport soccer '
               '--order climax --transitions dissolve --feed espn:eng.1:401879244 --anchor 1=4:10 --anchor 2=1:06:40\n'
               '           ytclip sports-montage game.mp4 --sport soccer --events "red card or controversial call"',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", help="the game: a YouTube link or a video file")
    p.add_argument("--sport", choices=sorted(SPORTS), default="any", help="the sport (default: any)")
    p.add_argument("--home", default="", help="the home team (default: read from the title)")
    p.add_argument("--away", default="", help="the away team (default: read from the title)")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="where the reel's folder goes (default: the category's Automation folder, else "
                        "Automation/Sports/Reels)")
    p.add_argument("--category", default="", help="category name or id: its folder, look and TikTok account")
    p.add_argument("--voice", default="en_storyteller",
                   help="the narrator's voice (ytclip create --list-voices), or off for none")
    p.add_argument("--framing", choices=sorted(G.REEL_FRAMING), default="follow",
                   help="follow: the 9:16 picture follows the action; fit: the whole picture over a blurred fill")
    p.add_argument("--order", choices=sorted(G.REEL_ORDER), default="countdown",
                   help="countdown: the best play last; timeline: as it happened")
    p.add_argument("--plays", type=int, default=0,
                   help=f"how many plays ({montage.MIN_PLAYS}-{montage.MAX_PLAYS}; default: as many as fit)")
    p.add_argument("--creator-rewards", action="store_true",
                   help="fail (exit 3) when the game hasn't enough plays for over a minute; the reel is kept")
    p.add_argument("--no-ai", action="store_true", help="rank the plays by the crowd and commentary only")
    p.add_argument("--events", default="", metavar="QUESTION",
                   help='make the reel of the moments an event search finds instead of the best plays: '
                        '"red card or player ejection", "controversial call"')
    _add_event_options(p)
    p.add_argument("--transitions", choices=("mix", "dissolve", "whip", "zoom"), default="",
                   help="how one play hands over to the next (default: Settings → Sports → Reel transitions)")
    args = p.parse_args(argv)
    if args.voice != "off" and args.voice not in voice_engine.VOICES:
        console.print(f"[red]No voice called {escape(args.voice)}.[/] Try: off, "
                      + ", ".join(voice_engine.VOICES))
        return 2
    s = Settings.load()
    cat = None
    if args.category:
        cat = categories.find(s, args.category) or next(
            (n for n in categories.load(s) if n.name.lower() == " ".join(args.category.split()).lower()), None)
        if cat is None:
            console.print(f"[red]No category called {escape(args.category)}.[/]")
            return 2
    sport = SPORTS[args.sport]
    base = s
    if cat is not None:  # the category's look and account, as automation and the Sports tab use
        from .jobs import Job
        from .pipeline import settings_for

        base, _label = settings_for(Job(id="sports-montage", url=args.source, category_id=cat.id), s)
    own = cat.clip_overrides() if cat else {}
    es = base.apply({"sport": sport.id, "mode": "auto", "clip_count": rank.CANDIDATES, "quality_floor": 0.2,
                     "series_parts": 1, "ai_polish": False, "min_len": own.get("min_len", sport.lengths[0]),
                     "max_len": own.get("max_len", sport.lengths[1])}
                    | ({"reel_transitions": args.transitions} if args.transitions else {})
                    | ({"event_search_model": "words"} if args.words else {}))
    by_ai = ""
    root = (args.output_dir.expanduser() if args.output_dir else
            (s.auto_output_path / cat.folder if cat else s.auto_output_path / "Sports") / "Reels")
    log = lambda m: console.print(f"[dim]{escape(m)}[/]")  # noqa: E731
    with Progress(TextColumn("[cyan]{task.description}"), BarColumn(), TimeElapsedColumn(),
                  console=console, transient=True) as bar:
        task = bar.add_task("Starting", total=None)

        def progress(msg: str, f) -> None:
            bar.update(task, description=msg, completed=f, total=1 if f is not None else None)

        engine = Engine(es, log=log, progress=progress)
        proj = Project(args.source)
        try:
            engine.open(proj)
            v = proj.video
            read = G.read_title(v.title, v.duration)
            home, away = args.home or read.get("home") or "", args.away or read.get("away") or ""
            game = {"id": f"cli-{v.id}", "video": v.id, "url": args.source, "title": v.title, "sport": sport.id,
                    "home": home, "away": away, "matchup": G.matchup(home, away, sport.id) or v.title,
                    "score": read.get("score") or "", "duration": v.duration}
            console.print(f"[bold]{escape(game['matchup'])}[/]  [dim]{escape(v.title)} · {stamp(v.duration)}[/]")
            engine.measure(proj)
            if args.events:
                found, marks = _event_search(args.events, proj, game, es, args, log, progress)
                for note in found.notes:
                    console.print(f"[yellow]{escape(note)}[/]")
                if not found.hits:
                    return 1
                res = R.make(proj, game, [], es, root, category=cat, voice=args.voice, framing_mode=args.framing,
                             order=("timeline" if args.order == "countdown" else args.order), log=log,
                             progress=progress, events=[h.to_json() for h in found.hits],
                             events_title=(found.query.labels or ["Moment"])[0], timeline=marks)
            else:
                progress("Finding the plays", None)
                engine.find(proj, use_ai=False)
                clips = [c for c in proj.clips if c.enabled]
                if not clips:
                    console.print("[red]No plays stood out: the crowd and the commentary never rose much.[/]")
                    return 1
                progress("Ranking the plays", None)
                hl, by_ai, why_not = rank.rank(game, clips, proj.signals, proj.cues, es, montage.MAX_PLAYS, log=log,
                                               use_ai=not args.no_ai)
                if why_not:
                    console.print(f"[yellow]⚠ {escape(why_not)}[/]")
                ids = [h["id"] for h in hl[:max(montage.MIN_PLAYS, min(montage.MAX_PLAYS, args.plays))]] \
                    if args.plays else None
                marks = []
                if args.feed or args.anchor:  # the feed's own minute and scorer on each play's lower-third
                    _found, marks = _event_search("", proj, game, es, args, log, progress)
                res = R.make(proj, game, hl, es, root, ids=ids, category=cat, voice=args.voice,
                             framing_mode=args.framing, order=args.order, log=log, progress=progress,
                             timeline=marks or None)
        except Cancelled:
            return 130
        except montage.ReelError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            return 1
    for w in res.warnings:
        console.print(f"[yellow]⚠ {escape(w)}[/]")
    length = float(res.checks.get("length") or res.reel.duration)
    loud = res.checks.get("lufs")
    what = (f"{len(res.reel.plays)} found moments" if res.reel.kind == "events" else
            f"the top {len(res.reel.plays)} plays")
    console.print(f"[green]✓ {length:.1f} s reel of {what}[/]"
                  + (f" [dim](ranked by {escape(by_ai)})[/]" if by_ai else "")
                  + (f" [dim]· {loud:.1f} LUFS[/]" if isinstance(loud, (int, float)) else "")
                  + f" → {escape(str(res.file))}")
    if res.reel.story.opening and res.info.get("narrator"):
        console.print(f"[dim]Narrator: “{escape(res.reel.story.opening)}”[/]")
    if not res.checks.get("rewards"):
        console.print("[yellow]Under a minute: not for Creator Rewards.[/]")
        return 3 if args.creator_rewards else 0
    return 0


def _add_event_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--feed", default="",
                   help="the game's play-by-play: auto (default: Settings → Sports), off, espn:LEAGUE:EVENT_ID "
                        "(espn:eng.1:401879244), api-football:FIXTURE_ID, or a JSON/CSV timeline file you wrote")
    p.add_argument("--anchor", action="append", default=[], metavar="PERIOD=TIME",
                   help="where a period starts in the video, to place the feed's events: 1=4:10 (the first half "
                        "kicks off at 4:10), 2=1:06:40, or 3@7:30=1:41:05 (7:30 into the third at 1:41:05). Repeatable")
    p.add_argument("--words", action="store_true",
                   help="match by words only: don't use (or download) the sentence model")


def _event_search(question: str, proj, game: dict, es, args, log, progress):
    """(the search's result or None, the game's placed timeline) for a measured project. With no question only
    the timeline is worked out."""
    from .intelligence import embeddings
    from .sportsdesk import align, feeds, scoreboard
    from .sportsdesk import semantic_search as ss

    m = proj.measurements
    sport = SPORTS.get(game.get("sport") or "any") or SPORTS["any"]
    duration = float(getattr(m, "duration", 0) or proj.video.duration or 0)
    cues = list(proj.cues or [])
    crowd = None
    if m is not None and hasattr(m, "db"):
        try:
            from . import sports as sports_mod

            crowd = sports_mod.build_signals(m, sport, []).get("crowd")
        except Exception:  # noqa: BLE001 - no crowd signal: the words and the feed decide
            crowd = None
    query = ss.understand(question, sport.id, es, use_ai=not getattr(args, "no_ai", False), log=log) if question else None
    about = dict(game)
    if query is not None and query.home and query.away and not (game.get("home") and game.get("away")):
        about.update(home=query.home, away=query.away)
    feed = feeds.for_game(about, es, spec=args.feed, log=log)
    anchors = [align.parse_anchor(a) for a in args.anchor]
    if (feed is not None and not anchors and scoreboard.can_read(es) and any(e.video is None for e in feed.events)
            and align.is_full_game(duration, feed.sport, game.get("kind", ""))
            and not align.transcript_anchors(cues, feed.sport, duration)):
        progress("Reading the game clock off the scoreboard", None)
        anchors += align.clock_anchors(Path(proj.video.path), duration, feed.sport, es, log=log)
    embedder = embeddings.get(es, expand=ss.tags, log=log, progress=progress)
    marks = ss.timeline(feed, cues, duration, s=es, anchors=anchors, kind=game.get("kind", ""), embedder=embedder,
                        cache_key=proj.video.id) if feed is not None else []
    if query is None:
        return None, marks
    result = ss.search(query, cues, duration, sport=sport.id, s=es, fine=getattr(m, "fine", None),
                       excitement=crowd, feed=feed, anchors=anchors, kind=game.get("kind", ""), embedder=embedder,
                       limit=getattr(args, "limit", 0) or ss.MAX_HITS, cache_key=proj.video.id, log=log,
                       progress=progress)
    return result, marks


def run_sports_find(argv: list[str]) -> int:
    """ytclip sports-find: the moments of a game that match a description (sportsdesk/semantic_search.py)."""
    import json as _json

    from .engine import Cancelled
    from .intelligence import embeddings
    from .sportsdesk import align, feeds, games as G, montage, reel as R

    p = argparse.ArgumentParser(
        prog="ytclip sports-find",
        description="Find a moment in a game by describing it. The commentary (captions, or Whisper's transcript) "
        "is read in overlapping 15-second windows and compared with your question by meaning and by the words a "
        "commentator uses for it; the game's official play-by-play (ESPN, API-Football with a key, or a timeline "
        "file) is placed in the video and matched too. Each moment found is cut from about 12 s before to 25 s "
        "after, on pauses between sentences.",
        epilog='Examples:  ytclip sports-find "red card or player ejection" "https://youtu.be/…" --sport soccer\n'
               '           ytclip sports-find "Ref throws red card after VAR check in Arsenal vs Chelsea" game.mp4 '
               '--sport soccer --feed espn:eng.1:401879244 --anchor 1=4:10 --anchor 2=1:06:40\n'
               '           ytclip sports-find "technical foul on bench during game 7" game7.mp4 --sport basketball '
               '--json\n           ytclip sports-find "controversial pass interference in the 4th quarter" chiefs.mp4 '
               '--sport football --reel --output-dir "Videos/Sports"',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question", help='the moment, in words: "missed penalty or controversial offside call"')
    p.add_argument("source", help="the game: a YouTube link or a video file")
    p.add_argument("--sport", choices=sorted(SPORTS), default="any", help="the sport (default: any)")
    p.add_argument("--home", default="", help="the home team (default: read from the title or the question)")
    p.add_argument("--away", default="", help="the away team")
    _add_event_options(p)
    p.add_argument("--limit", type=int, default=5, help="at most this many moments (default 5)")
    p.add_argument("--no-ai", action="store_true", help="read the question by its words only (no AI)")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.add_argument("--reel", action="store_true", help="make one vertical reel of the moments found")
    p.add_argument("--output-dir", type=Path, default=None, help="where the reel's folder goes")
    p.add_argument("--voice", default="off", help="the reel's narrator (default: off)")
    args = p.parse_args(argv)
    for a in args.anchor:
        try:
            align.parse_anchor(a)
        except ValueError:
            console.print(f"[red]“{escape(a)}” isn't an anchor.[/] Write it as PERIOD=TIME, such as 2=58:40.")
            return 2
    s = Settings.load()
    sport = SPORTS[args.sport]
    es = s.apply({"sport": sport.id, "mode": "auto", "series_parts": 1, "ai_polish": False}
                 | ({"event_search_model": "words"} if args.words else {}))
    quiet = args.json
    log = (lambda m: None) if quiet else (lambda m: console.print(f"[dim]{escape(m)}[/]"))
    with Progress(TextColumn("[cyan]{task.description}"), BarColumn(), TimeElapsedColumn(),
                  console=console, transient=True, disable=quiet) as bar:
        task = bar.add_task("Starting", total=None)

        def progress(msg: str, f) -> None:
            bar.update(task, description=msg, completed=f, total=1 if f is not None else None)

        engine = Engine(es, log=log, progress=progress)
        proj = Project(args.source)
        try:
            engine.open(proj)
            v = proj.video
            read = G.read_title(v.title, v.duration)
            home, away = args.home or read.get("home") or "", args.away or read.get("away") or ""
            game = {"id": f"cli-{v.id}", "video": v.id, "url": args.source, "title": v.title, "sport": sport.id,
                    "home": home, "away": away, "matchup": G.matchup(home, away, sport.id) or v.title,
                    "score": read.get("score") or "", "duration": v.duration, "kind": read.get("kind") or "",
                    "published": getattr(v, "published", 0) or 0}
            engine.measure(proj)
            result, marks = _event_search(args.question, proj, game, es, args, log, progress)
            res = None
            if args.reel and result.hits:
                root = args.output_dir.expanduser() if args.output_dir else s.auto_output_path / "Sports" / "Reels"
                res = R.make(proj, game, [], es, root, voice=args.voice, order="timeline", log=log, progress=progress,
                             events=[h.to_json() for h in result.hits],
                             events_title=(result.query.labels or ["Moment"])[0], timeline=marks)
        except Cancelled:
            return 130
        except (embeddings.EmbeddingError, feeds.FeedError, montage.ReelError) as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            return 1
    if args.json:
        out = result.to_json() | {"game": {k: game[k] for k in ("title", "matchup", "sport", "home", "away")},
                                  "timeline": marks, "reel": str(res.file) if res else ""}
        print(_json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if result.hits else 1
    console.print(f"[bold]{escape(game['matchup'])}[/]  [dim]{escape(v.title)} · {stamp(v.duration)}[/]")
    how = " + ".join(x for x in (result.feed, "the commentary" if result.windows else "") if x) or "nothing to read"
    console.print(f"[dim]Looking for: {escape(result.query.describe())} · from {escape(how)} · "
                  f"{escape(result.reader)}{' on ' + escape(result.device) if result.device else ''}[/]")
    if result.hits:
        table = Table(show_lines=False)
        for col in ("#", "At", "Clip", "Sure", "What", "Why"):
            table.add_column(col, overflow="fold")
        for h in result.hits:
            table.add_row(h.id, stamp(h.t_event), f"{stamp(h.start)}-{stamp(h.end)} ({h.length:.0f} s)",
                          f"{h.score:.0f}" + ("" if h.certain else " ?"), escape(h.badge or h.label), escape(h.why))
        console.print(table)
    for note in result.notes:
        console.print(f"[yellow]{escape(note)}[/]")
    if res is not None:
        for w in res.warnings:
            console.print(f"[yellow]⚠ {escape(w)}[/]")
        console.print(f"[green]✓ {float(res.checks.get('length') or res.reel.duration):.1f} s reel[/] → "
                      f"{escape(str(res.file))}")
    return 0 if result.hits else 1


def run_ai(argv: list[str]) -> int:
    """ytclip ai: ask the AI router, and see what it would use, what it has used and what this computer runs."""
    import json as _json

    from .intelligence import agent_router, hardware, usage

    p = argparse.ArgumentParser(
        prog="ytclip ai",
        description="The AI router: the local model first (free, on this computer), a cloud AI (Claude or Gemini, "
        "whichever has a key) when the local one can't answer or you ask for deep reasoning. Every call's tokens "
        "and cost are counted in ai_usage.json in the app's folder.",
        epilog='Examples:  ytclip ai ask "Give three hooks for a clip about a last-minute winner"\n'
               '           ytclip ai ask "Rate this title 1-10" --schema rating.json --priority cloud_deep\n'
               "           ytclip ai route\n           ytclip ai usage --days 7\n           ytclip ai hardware",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ask = sub.add_parser("ask", help="ask a question")
    ask.add_argument("prompt")
    ask.add_argument("--priority", choices=agent_router.PRIORITIES, default="local_first")
    ask.add_argument("--schema", type=Path, default=None, help="a JSON Schema file: the answer is JSON of that shape")
    ask.add_argument("--system", default="", help="instructions that come before the question")
    ask.add_argument("--max-tokens", type=int, default=2000)
    ask.add_argument("--json", action="store_true", help="print the answer with how it was got, as JSON")
    route = sub.add_parser("route", help="which AIs would be asked, in order")
    route.add_argument("--priority", choices=agent_router.PRIORITIES, default="local_first")
    route.add_argument("--json", action="store_true")
    use = sub.add_parser("usage", help="tokens and cost, by day and model")
    use.add_argument("--days", type=int, default=30)
    use.add_argument("--json", action="store_true")
    hw = sub.add_parser("hardware", help="what each kind of model runs on here")
    hw.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    s = Settings.load()
    if args.cmd == "ask":
        schema = None
        if args.schema:
            try:
                schema = _json.loads(args.schema.expanduser().read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                console.print(f"[red]{escape(str(args.schema))} isn't a readable JSON Schema ({escape(str(exc))}).[/]")
                return 2
        try:
            got = agent_router.query_intelligence(args.prompt, schema, args.priority, system=args.system, settings=s,
                                                  max_tokens=args.max_tokens,
                                                  log=None if args.json else lambda m: console.print(f"[dim]{escape(m)}[/]"))
        except agent_router.RouterError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            for t in exc.tried:
                console.print(f"[dim]  {escape(str(t.get('provider')))}: {escape(str(t.get('error')))}[/]")
            return 1
        if args.json:
            print(_json.dumps(got.to_json(), indent=2, ensure_ascii=False))
            return 0
        print(_json.dumps(got.data, indent=2, ensure_ascii=False) if schema else got.text)
        u = got.usage or {}
        cost = f" · ${u.get('usd', 0):.4f}" if u.get("usd") else " · free" if got.tier == "local" else ""
        console.print(f"[dim]{escape(got.model)} ({escape(got.provider)}, {got.tier}) · "
                      f"{u.get('tokens_in', 0)} in / {u.get('tokens_out', 0)} out tokens{cost} · {got.seconds:.1f} s"
                      + (f" · after {len(got.tried)} that couldn't" if got.tried else "") + "[/]")
        return 0
    if args.cmd == "route":
        steps = agent_router.route(s, args.priority)
        if args.json:
            print(_json.dumps([x.to_json() for x in steps], indent=2))
            return 0
        for i, x in enumerate(steps, 1):
            d = x.to_json()
            state = ("[green]ready[/]" if d.get("ready") else
                     f"[yellow]not ready[/] [dim]({escape(str(d.get('why') or ''))})[/]")
            console.print(f"{i}. {d.get('tier')} · {escape(str(d.get('provider')))} · "
                          f"{escape(str(d.get('model') or ''))} · {state}")
        if not any(x.ready for x in steps):
            console.print("[yellow]No AI is set up: download a local model (ytclip local-ai) or add a Claude or "
                          "Gemini key in Settings → AI.[/]")
            return 1
        return 0
    if args.cmd == "usage":
        rows = usage.summary(args.days)
        budget = usage.status(s)
        if args.json:
            print(_json.dumps({"rows": rows, "budget": dict(budget), "file": str(usage.path())}, indent=2))
            return 0
        if not rows:
            console.print(f"[dim]No AI calls counted yet ({escape(str(usage.path()))}).[/]")
        else:
            table = Table()
            for col in ("Day", "AI", "Model", "Calls", "Tokens in", "Tokens out", "Cost"):
                table.add_column(col)
            for r in rows:
                table.add_row(r["day"], r["provider"], r["model"], str(r["calls"]), f"{r['in']:,}", f"{r['out']:,}",
                              "free" if r["provider"] in ("local", "pc") else f"${r['usd']:.4f}"
                              + (" ~" if r.get("estimated") else ""))
            console.print(table)
        for period in ("day", "month"):
            limit = float(budget.get(f"{period}_limit") or 0)
            spent = float(budget.get(f"{period}_usd") or 0)
            console.print(f"[dim]This {period}: ${spent:.2f}" + (f" of ${limit:.2f}" if limit else " (no limit set)") + "[/]")
        if budget.over:
            console.print(f"[yellow]The {budget.over}'s spending limit is reached: paid AIs are paused, the local "
                          "one carries on.[/]")
        return 0
    info = hardware.stack()
    if args.json:
        print(_json.dumps(info, indent=2))
        return 0
    for key, label in (("computer", "Computer"), ("llm", "Local AI (GGUF)"), ("whisper", "Whisper"),
                       ("onnx", "Sentence model & voice (ONNX)"), ("memory_for_models_gb", "Memory for models (GB)"),
                       ("reserve_gb", "Kept free (GB)")):
        console.print(f"{label + ':':34}{escape(str(info.get(key)))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--apply-update"]:  # the updater's install step, run from the unpacked new version
        from .swap import main as apply_update

        return apply_update(argv[1:])
    try:  # files from older versions (3.2: niches became categories)
        from .categories import migrate_disk

        migrate_disk()
    except Exception:
        pass
    if argv[:1] == ["clean"]:
        return run_clean(argv[1:])
    if argv[:1] in (["categories"], ["niches"]):
        if argv[:1] == ["niches"]:
            _renamed_warning("ytclip niches", "ytclip categories")
        return run_categories(argv[1:])
    if argv[:1] == ["request"]:
        return run_request(argv[1:])
    if argv[:1] == ["sports-find"]:
        return run_sports_find(argv[1:])
    if argv[:1] == ["ai"]:
        return run_ai(argv[1:])
    if argv[:1] == ["sports-montage"]:
        return run_sports_montage(argv[1:])
    if argv[:1] == ["create"]:
        return run_create(argv[1:])
    if argv[:1] == ["studio"]:
        return run_studio(argv[1:])
    if argv[:1] == ["report"]:
        return run_report(argv[1:])
    if argv[:1] == ["whisper-test"]:
        return run_whisper_test(argv[1:])
    if argv[:1] == ["local-ai"]:
        return run_local_ai(argv[1:])
    if argv[:1] == ["sign-in"]:
        return run_sign_in(argv[1:])
    if argv[:1] == ["posting"]:
        return run_posting(argv[1:])
    if argv[:1] == ["doctor"]:
        return run_doctor(argv[1:])
    if argv[:1] == ["self-test"]:
        from .selftest import main as self_test

        return self_test(argv[1:])
    if argv[:1] == ["results"]:
        return run_results(argv[1:])
    if argv[:1] == ["research"]:
        return run_research(argv[1:])
    if argv[:1] == ["find"]:
        return run_find(argv[1:])
    if argv[:1] == ["auto"]:
        return run_auto(argv[1:])
    if argv[:1] == ["remote"]:
        from .remote.cli import main as run_remote

        return run_remote(argv[1:])
    if argv[:1] == ["host-setup"]:
        from .linux_host import main as run_host_setup

        return run_host_setup(argv[1:])
    if argv[:1] == ["qc"]:
        return run_qc(argv[1:])
    args = build_parser().parse_args(argv)
    from . import updater

    finished = updater.finished_update_message()  # result of an update started last time
    if finished:
        console.print(f"[green]✓ {escape(finished[0])}[/]" if finished[1] else f"[yellow]⚠ {escape(finished[0])}[/]")
    if args.check_update or args.update or args.go_back:
        return run_update(install=args.update, channel=args.channel or "", go_back=args.go_back)
    if args.list_layouts:
        for name, values in all_layouts().items():
            tag = "" if name in LAYOUT_PRESETS else " [dim](yours)[/]"
            console.print(f"[bold]{escape(name)}[/]{tag}: {escape(', '.join(f'{k}={v}' for k, v in values.items()))}")
        return 0
    if args.list_presets:
        for name, values in all_presets().items():
            tag = "" if name in PRESETS else " [dim](yours)[/]"
            console.print(f"[bold]{escape(name)}[/]{tag}: {escape(', '.join(f'{k}={v}' for k, v in values.items()))}")
        return 0

    sources = list(args.sources)
    if args.batch_file:
        sources += [ln.strip() for ln in args.batch_file.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")]
    if not sources:
        build_parser().print_help()
        return 2
    s = settings_from_args(args)
    if s.min_len > s.max_len:
        console.print("[red]--min can't be larger than --max[/]")
        return 2

    log = lambda m: console.print(f"[dim]{escape(m)}[/]")  # noqa: E731
    try:
        videos = expand_sources(sources, s.playlist_limit or None, s.cookies_browser or None, log)
    except Exception as exc:
        console.print(f"[red]Couldn't read sources: {escape(str(exc))}[/]")
        return 1

    engine = Engine(s, log=log)
    failed = 0
    for i, src in enumerate(videos, 1):
        console.rule(f"[bold]Video {i}/{len(videos)}")
        try:
            process(src, engine, args.dry_run)
        except KeyboardInterrupt:
            console.print("[red]Interrupted.[/]")
            return 130
        except Exception as exc:
            failed += 1
            console.print(f"[red]✗ {escape(src)}: {escape(str(exc))}[/]")

    if len(videos) > 1:
        console.rule()
        console.print(f"Done: {len(videos) - failed} succeeded, {failed} failed. Clips are in {escape(str(s.output_path))}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

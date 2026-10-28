"""Command line entry point: ytclip URL|FILE [...]"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__, learn
from .download import expand_sources
from .engine import Engine, Project
from .organize import stamp
from .settings import ASPECTS, LAYOUT_PRESETS, PRESETS, Settings, all_layouts, all_presets, find_layout
from .qc import REJECTED as QC_REJECTED
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
           "Upload to a storage server: ytclip upload --help\nClear out space: ytclip clean --help",
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
                   help="auto: smart signals; ai: the AI (Claude or Gemini) picks from the transcript; chapters: one clip per chapter")
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
    p.add_argument("--ai", choices=["claude", "gemini"],
                   help="which AI to use for --mode ai and --polish this run (default: Settings → AI)")
    p.add_argument("--bleep", action="store_true", help="mask strong swearing in captions (f*ck, sh*t)")
    p.add_argument("--qc", action="store_true", help="check each clip after export; failures move to _rejected")
    p.add_argument("--keywords", help="comma-separated words/phrases to boost")
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
    }
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
    p.add_argument("--niche", help="the niche (name or id) videos added with --queue are for")
    p.add_argument("--json", action="store_true", help="print results as JSON")
    p.add_argument("--api-key", help="YouTube Data API key (otherwise from settings or YOUTUBE_API_KEY)")
    return p


def run_find(argv: list[str]) -> int:
    import contextlib
    import json

    from . import lookup, niches
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
    niche = None
    if args.niche:
        niche = niches.find(s, args.niche) or next((n for n in niches.load(s)
                                                    if n.name.lower() == args.niche.strip().lower()), None)
        if niche is None:
            console.print(f"[red]No niche called {escape(args.niche)}.[/]")
            return 2
    request = " ".join(args.request)
    log = (lambda m: None) if args.json else (lambda m: console.print(f"[dim]{escape(m)}[/]"))  # noqa: E731
    try:
        with console.status("Finding videos…") if not args.json else contextlib.nullcontext():
            result = lookup.find(request, s, key, args.since,
                                 s.research_min_minutes if args.min_minutes is None else args.min_minutes,
                                 next(iter(language_codes(s.pick_languages)), ""), log,
                                 use_ai=False if args.no_ai else None)
    except (ResearchError, ValueError) as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        return 1
    try:
        lookup.remember(result)
    except OSError:
        pass
    top = result.videos[:max(1, args.top)]
    if args.json:
        print(json.dumps({"request": result.request, "searched": result.plan.queries, "quota": result.quota,
                          "rated_by": result.rated_by, "videos": [
                              {"url": v.url, "title": v.title, "channel": v.channel, "match": v.match,
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
                          + (f" · {result.weak} that didn't match left out" if result.weak else "") + "[/]")
    if args.queue:
        from .jobs import JobDB
        from .pipeline import Runner

        db = JobDB()
        try:
            runner = Runner(db)
            added = sum(runner.add_manual(v.url, v.title, niche.id if niche else "")
                        for v in result.videos[:args.queue])
        finally:
            db.close()
        if not args.json:
            console.print(f"Queued {added} in Automation" + (f" for {escape(niche.name)}" if niche else "") + ".")
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
                        "license", "niche")
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
    p.add_argument("--niche", metavar="NAME", default="", help="with --add: the niche they're for")
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
    from . import niches

    names = {n.id: n.name for n in niches.load(Settings.load())}
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
        where = escape(j.channel) + (f" · {escape(names[j.niche])}" if j.niche in names else "")
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
        from . import niches

        niche = ""
        if args.niche:
            mine = niches.load(Settings.load())
            found = next((n for n in mine if n.name.lower() == " ".join(args.niche.split()).lower()), None)
            if not found:
                names = ", ".join(n.name for n in mine) or "none yet"
                console.print(f"[red]No niche called {escape(args.niche)} (yours: {escape(names)}).[/]")
                return 2
            niche = found.id
        runner = pipeline.Runner(db)
        for url in args.add:
            try:
                added = runner.add_manual(url, niche=niche)
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

    return pipeline.Runner(db, log=log, priority=speed.set_priority).run(once=args.once)


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
                                "from the results TikTok Manager shares after each stats refresh.")
    p.add_argument("--json", action="store_true", help="print the lessons as JSON")
    args = p.parse_args(argv)
    import json

    lessons = learn.Lessons.load()
    if args.json:
        print(json.dumps({"posts": len(lessons.outcomes), "written_at": lessons.written_at,
                          "channels": [vars(c) for c in sorted(lessons.channels.values(), key=lambda c: -c.bonus)],
                          "notes": lessons.summary_lines(top=0)[1:], "polish_guide": lessons.prompt_text()}, indent=1))
        return 0
    if lessons.empty:
        console.print("No results yet. In TikTok Manager, post some clips, refresh stats and give it 48 hours: "
                      f"it shares how they did in {escape(str(learn.results_path()))}.")
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


def run_clean(argv: list[str]) -> int:
    from . import cleanup
    from .jobs import JobDB
    from .settings import CLEANUP_KINDS

    p = argparse.ArgumentParser(prog="ytclip clean", description="Clear out what nothing needs any more: unfinished "
                                "downloads, preview renders, downloaded videos and their analysis, rejected clips, "
                                "and clips TikTok Manager says you posted or removed. Without --delete it only "
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
    if plan.kept_for_tm:
        console.print(f"[dim]{plan.kept_for_tm} clip{'s' if plan.kept_for_tm != 1 else ''} stay because TikTok "
                      "Manager still has them.[/]")
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


def run_niches(argv: list[str]) -> int:
    from . import niches
    from .jobs import JobDB
    from .policy import PickLog, picks_path

    p = argparse.ArgumentParser(prog="ytclip niches", description="Your niches (one TikTok account's topic each, "
                                "set up in the app's Niches tab) and how each is doing.")
    p.add_argument("--json", action="store_true", help="print them as JSON")
    args = p.parse_args(argv)
    s = Settings.load()
    all_niches = niches.load(s)
    now = time.time()
    picks = PickLog(picks_path())
    db = JobDB()
    try:
        counts = db.niche_counts(now - 7 * 86400)
    finally:
        db.close()
    rows = []
    for n in all_niches:
        refs, _ = n.channel_refs()
        c = counts.get(n.id, {})
        rows.append({"id": n.id, "name": n.name, "account": n.account, "on": n.enabled, "per_day": n.daily,
                     "channels": len(refs), "searches": len(n.search_list()),
                     "picked_today": picks.count_since(now - 86400, niche=n.id), "waiting": c.get("waiting", 0),
                     "clips_this_week": c.get("clips", 0), "folder": str(s.auto_output_path / n.folder)})
    if args.json:
        import json

        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    if not rows:
        console.print("No niches yet. Add them in ClipMonster's Niches tab: each one is a TikTok account's topic, "
                      "with its own channels, searches, look and folder.")
        return 0
    table = Table(show_edge=False, header_style="bold")
    for col in ("Niche", "Account", "Per day", "Sources", "Today", "Waiting", "Clips (7 days)"):
        table.add_column(col, justify="left" if col in ("Niche", "Account") else "right")
    for r in rows:
        table.add_row(escape(r["name"]) + ("" if r["on"] else " [dim](off)[/]"), escape(r["account"]),
                      str(r["per_day"]), f"{r['channels']} ch · {r['searches']} cc", str(r["picked_today"]),
                      str(r["waiting"]), str(r["clips_this_week"]))
    console.print(table)
    total = sum(r["per_day"] for r in rows if r["on"])
    if total > s.pick_daily:
        console.print(f"[yellow]Your niches add up to {total} videos a day, but at most {s.pick_daily} are picked a "
                      "day in all (Settings → Picking → Videos picked per day).[/]")
    return 0


def _niche_by_name(name: str):
    """(niche id, error message) for a niche name typed on the command line."""
    from . import niches

    if not name:
        return "", ""
    mine = niches.load(Settings.load())
    found = next((n for n in mine if n.name.lower() == " ".join(name.split()).lower()), None)
    if found:
        return found.id, ""
    return "", f"No niche called {name} (yours: {', '.join(n.name for n in mine) or 'none yet'})."


def run_request(argv: list[str]) -> int:
    from . import niches, orders, pipeline
    from .jobs import Job, JobDB
    from .research import youtube_key

    p = argparse.ArgumentParser(
        prog="ytclip request",
        description="Ask automation for clips: how many, how long, for which niche, from your links or from videos "
                    "it picks, with special requests for the AI. Automation fills it and shows its progress.",
        epilog='Example: ytclip request 10 --length 30-45 --niche Gaming --notes "only clutch plays, no swearing '
               'in the hook"')
    p.add_argument("count", nargs="?", type=int, help="how many clips you want")
    p.add_argument("--length", default="", metavar="SECONDS", help="clip length, e.g. 30-45 (default: the niche's "
                   "or Automation's style)")
    p.add_argument("--niche", default="", metavar="NAME", help="the niche they're for")
    p.add_argument("--link", nargs="+", action="extend", default=[], metavar="URL", help="videos to clip (default: automation picks "
                   "videos for the niche)")
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
            names = {n.id: n.name for n in niches.load(Settings.load())}
            mine = db.orders()
            if not mine:
                console.print("No requests yet. Example: ytclip request 10 --length 30-45 --niche Gaming")
                return 0
            table = Table(show_edge=False, header_style="bold")
            for col in ("Id", "Request", "Status", "Ready", "Special requests"):
                table.add_column(col, overflow="fold")
            colours = {"open": "cyan", "done": "green", "short": "yellow", "cancelled": "dim"}
            for o in mine:
                table.add_row(o.id, escape(o.title(names.get(o.niche, ""))),
                              f"[{colours[o.status]}]{orders.STATUS_WORDS[o.status]}[/]\n[dim]{escape(o.message)}[/]",
                              f"{o.made}/{o.count}", escape(o.notes))
            console.print(table)
            return 0
        niche, err = _niche_by_name(args.niche)
        if err:
            console.print(f"[red]{escape(err)}[/]")
            return 2
        s = Settings.load()
        try:
            if args.length:
                lo, hi = orders.parse_length(args.length)
            else:  # the niche's length, else Automation's style
                job = Job(id="", url="", niche=niche)
                style = pipeline.settings_for(job, s)[0]
                lo, hi = style.min_len, style.max_len
            order = orders.make(args.count, lo, hi, niche=niche, links=args.link, notes=args.notes)
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            return 2
        order = pipeline.Runner(db, log=pipeline.file_logger()).place(order)
        console.print(f"Request {order.id}: {escape(order.title(niches.name_of(s, niche)))}. {escape(order.message)}.")
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


def run_report(argv: list[str]) -> int:
    from . import report
    from .jobs import JobDB

    p = argparse.ArgumentParser(prog="ytclip report", description="What automation did while you were away: "
                                "clips per niche and request, rejected clips and why, failures, pauses.")
    p.add_argument("--hours", type=float, default=12.0, help="how far back to look (default 12)")
    p.add_argument("--json", action="store_true", help="print it as JSON")
    args = p.parse_args(argv)
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
                                "place: ffmpeg, the downloader, keys, folders, free space, TikTok Manager, niches.")
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


def run_upload(argv: list[str]) -> int:
    from . import gdrive, pipeline, uploads
    from .jobs import JobDB

    p = argparse.ArgumentParser(
        prog="ytclip upload", description="Upload finished clips to your storage server (Settings → Upload). "
        "Automation and the app upload by themselves; this shows how it's going or does it now.")
    p.add_argument("--sign-in", action="store_true", help="sign in to Google Drive (opens your browser)")
    p.add_argument("--sign-out", action="store_true", help="forget the Google Drive sign-in")
    p.add_argument("--now", action="store_true", help="upload everything waiting now, then exit")
    p.add_argument("--retry", action="store_true", help="try failed uploads again")
    args = p.parse_args(argv)
    s = Settings.load()
    if args.sign_out:
        gdrive.sign_out()
        console.print("Signed out of Google Drive. Nothing is uploaded there until you sign in again.")
        return 0
    if args.sign_in:
        console.print("Your browser is opening Google's sign-in page. Sign in and allow ClipMonster to add files "
                      "to your Drive…")
        try:
            account = gdrive.sign_in(s.gdrive_client_id, s.gdrive_client_secret)
        except gdrive.DriveError as exc:
            console.print(f"[red]✗ {escape(str(exc))}[/]")
            return 1
        if not s.upload_target:
            s.copy(upload_target="gdrive").save()
        console.print(f"[green]✓ Signed in to Google Drive as {escape(account.email or 'your account')}.[/] Clips "
                      "upload by themselves from now on.")
        return 0
    db = JobDB()
    try:
        if args.retry:
            n = uploads.retry_failed(db)
            pipeline.request_uploads(db)
            console.print(f"{n} failed upload{'s' if n != 1 else ''} will be tried again.")
        target = uploads.target_for(s)
        if target is None:
            console.print("Uploading is off. Pick a server in Settings → Upload.")
            return 0
        problem = target.problem(s)
        if problem:
            console.print(f"[yellow]⚠ {escape(problem)}[/]")
            return 1
        if args.now:
            uploads.sync(db, s)
            db.set_meta("upload_hold", 0)
            sent = 0
            with Progress(TextColumn("[cyan]{task.description}"), BarColumn(), console=console, transient=True) as bar:
                task = bar.add_task("Uploading", total=None)
                while uploads.upload_next(db, s, f"{os.getpid()}:cli",
                                          lambda m: (bar.update(task, description=escape(m)), console.print(escape(m)))):
                    sent += 1
            console.print(f"{sent} clip{'s' if sent != 1 else ''} uploaded.")
        console.print(escape(uploads.status(db).text()))
        return 0
    finally:
        db.close()


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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--apply-update"]:  # the updater's install step, run from the unpacked new version
        from .swap import main as apply_update

        return apply_update(argv[1:])
    if argv[:1] == ["clean"]:
        return run_clean(argv[1:])
    if argv[:1] == ["niches"]:
        return run_niches(argv[1:])
    if argv[:1] == ["request"]:
        return run_request(argv[1:])
    if argv[:1] == ["report"]:
        return run_report(argv[1:])
    if argv[:1] == ["whisper-test"]:
        return run_whisper_test(argv[1:])
    if argv[:1] == ["sign-in"]:
        return run_sign_in(argv[1:])
    if argv[:1] == ["upload"]:
        return run_upload(argv[1:])
    if argv[:1] == ["doctor"]:
        return run_doctor(argv[1:])
    if argv[:1] == ["results"]:
        return run_results(argv[1:])
    if argv[:1] == ["research"]:
        return run_research(argv[1:])
    if argv[:1] == ["find"]:
        return run_find(argv[1:])
    if argv[:1] == ["auto"]:
        return run_auto(argv[1:])
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

"""Command line entry point: ytclip URL|FILE [...]"""

from __future__ import annotations

import argparse
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
           "Trending research: ytclip research --help\nAutomation: ytclip auto --help\n"
           "Quality check: ytclip qc --help\nWhat's working: ytclip results --help",
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
                   help="auto: smart signals; ai: Claude picks from the transcript; chapters: one clip per chapter")
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
                   help="AI polish: hooks for the top banner, caption fixes, speaker crops (needs an Anthropic key)")
    p.add_argument("--bleep", action="store_true", help="mask strong swearing in captions (f*ck, sh*t)")
    p.add_argument("--qc", action="store_true", help="check each clip after export; failures move to _rejected")
    p.add_argument("--keywords", help="comma-separated words/phrases to boost")
    p.add_argument("-q", "--quality", type=int, help="max download height, e.g. 720")
    p.add_argument("--encoder", choices=["auto", "libx264", "h264_amf", "h264_nvenc", "h264_qsv"])
    p.add_argument("--fast", action="store_true", help="copy streams instead of re-encoding")
    p.add_argument("--limit", type=int, help="max videos to take from each playlist/channel")
    p.add_argument("--delete-source", action="store_true", help="delete the full video once clips are cut")
    p.add_argument("--dry-run", action="store_true", help="analyse and print the clip plan without exporting")
    p.add_argument("--cookies-from-browser", metavar="BROWSER", help="use your browser login (chrome, edge, firefox...)")
    p.add_argument("--check-update", action="store_true", help="check GitHub for a newer ClipMonster release")
    p.add_argument("--update", action="store_true", help="download and install the newest release (packaged app only)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def run_update(install: bool) -> int:
    import os
    import time

    from . import updater

    s = Settings.load()
    repo = updater.configured_repo(s.update_repo)
    try:
        rel = updater.check(repo, s.include_prereleases)
    except updater.UpdateError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        return 1
    updater.save_state(last_check=time.time())
    if not rel:
        console.print(f"ClipMonster {__version__} is up to date.")
        return 0
    console.print(f"[bold]ClipMonster {escape(rel.version)}[/] is available (you have {__version__}). {escape(rel.page_url)}")
    if not install:
        return 0
    target = updater.install_dir()
    if target is None:
        console.print("Running from source: update with [bold]git pull[/] instead.")
        return 1
    cwd = Path.cwd().resolve()
    if cwd == target or target in cwd.parents:
        # The terminal itself sits in the folder, and Windows won't let it be replaced.
        console.print(f"[red]Run this from outside the ClipMonster folder, e.g.:[/]\n  cd \"{target.parent}\"\n"
                      f"  \"{target / 'ytclip.exe'}\" --update")
        return 1
    others = updater.other_instances(target)
    if others:
        console.print(f"[red]Close these first; they keep the ClipMonster folder in use: {escape(', '.join(others))}[/]")
        return 1
    try:
        with Progress(TextColumn("[cyan]{task.description}"), BarColumn(), console=console, transient=True) as bar:
            task = bar.add_task("Downloading update", total=1)
            zip_path = updater.download(rel, lambda f: bar.update(task, completed=f))
            bar.update(task, description="Unpacking", completed=1)
            new_app = updater.extract(zip_path, target)
    except updater.UpdateError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        return 1
    updater.save_state(pending=rel.version)
    updater.launch_swap(new_app, target, os.getpid(), relaunch="")
    console.print(f"[green]✓ Downloaded and verified {escape(rel.version)}.[/] It's installed in the background "
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


def _research_table(videos: list, title: str, decisions: dict | None = None) -> Table:
    from .clipability import age_text, compact

    table = Table(title=title, show_edge=False, header_style="bold", title_justify="left")
    cols = ["Score", "Video", "Found in", "Views/h", "Length", "Posted", "Why"] + (["Pick"] if decisions else [])
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
        if decisions:
            d = decisions.get(v.id)
            row.append(("[green]✓ " if d.picked else "[dim]– ") + escape(d.reason)
                       + (f"\n📈 {escape(d.learned)}" if d.learned else "") + "[/]" if d else "")
        table.add_row(*row)
    return table


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
                        "license")
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
    p.add_argument("--retry", nargs="+", metavar="ID", help="try failed videos again (ids from --status)")
    p.add_argument("--remove", nargs="+", metavar="ID", help="take videos out of the queue")
    p.add_argument("--pause", action="store_true", help="pause a running automation")
    p.add_argument("--resume", action="store_true", help="carry on after --pause")
    p.add_argument("--stop", action="store_true", help="ask a running automation to stop")
    p.add_argument("--install", action="store_true", help="start automation whenever you sign in to Windows")
    p.add_argument("--uninstall", action="store_true", help="stop starting automation with Windows")
    return p


AUTO_COLOURS = {"working": "cyan", "queued": "white", "done": "green", "failed": "red"}


def _auto_status(db) -> None:
    from .clipability import age_text
    from .pipeline import runner_alive

    alive = runner_alive(db)
    state = db.meta("state") or "Stopped"
    paused = db.meta("paused") == "1"
    console.print("[bold]Automation:[/] " + ("[green]running[/]" if alive else "[dim]not running[/]")
                  + (" · [yellow]paused[/]" if paused else "") + (f" · {escape(state)}" if alive else ""))
    counts = db.counts()
    console.print(f"[dim]{counts['queued']} waiting · {counts['working']} working · {counts['done']} done · "
                  f"{counts['failed']} failed[/]")
    jobs = db.jobs(limit=40)
    if not jobs:
        console.print("Nothing queued yet.")
        return
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
        table.add_row(j.id, f"{escape(j.title or j.url)}\n[dim]{escape(j.channel)}[/]", status,
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
        runner = pipeline.Runner(db)
        for url in args.add:
            try:
                added = runner.add_manual(url)
            except ValueError as exc:
                console.print(f"[red]{escape(url)}: {escape(str(exc))}[/]")
                return 2
            console.print(f"Queued {escape(url)}" if added else f"[yellow]Already queued before: {escape(url)}[/]")
        return 0
    if args.status:
        _auto_status(db)
        return 0
    log = pipeline.file_logger(lambda line: console.print(escape(line), highlight=False))
    return pipeline.Runner(db, log=log).run(once=args.once)


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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["results"]:
        return run_results(argv[1:])
    if argv[:1] == ["research"]:
        return run_research(argv[1:])
    if argv[:1] == ["auto"]:
        return run_auto(argv[1:])
    if argv[:1] == ["qc"]:
        return run_qc(argv[1:])
    args = build_parser().parse_args(argv)
    from . import updater

    finished = updater.finished_update_message()  # result of an update started last time
    if finished:
        console.print(f"[green]✓ {escape(finished[0])}[/]" if finished[1] else f"[yellow]⚠ {escape(finished[0])}[/]")
    if args.check_update or args.update:
        return run_update(install=args.update)
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

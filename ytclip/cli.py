"""Command line entry point: ytclip URL|FILE [...]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .download import expand_sources
from .engine import Engine, Project
from .organize import stamp
from .settings import ASPECTS, PRESETS, Settings, all_presets

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
        epilog='Example: ytclip "https://youtu.be/VIDEO" --preset "Shorts / TikTok / Reels"',
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
    overrides = {
        "output_dir": str(args.output) if args.output else None,
        "clip_count": args.clips, "min_len": args.min_len, "max_len": args.max_len, "mode": args.mode,
        "aspect": "vertical_blur" if args.vertical else args.aspect, "quality": args.quality,
        "encoder": args.encoder, "keywords": args.keywords, "playlist_limit": args.limit,
        "cookies_browser": args.cookies_from_browser,
        "burn_captions": True if args.captions else None,
        "fast_copy": True if args.fast else None,
        "keep_source": False if args.delete_source else None,
    }
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

    if p.ai_error:
        console.print(f"[yellow]⚠ AI clip finder failed: {escape(p.ai_error)} Used smart signals instead.[/]")
    console.print(f"[dim]Picked using: {', '.join(engine.signal_names(p)) or p.mode_used}"
                  f"{' · transcript: ' + p.transcript_source if p.transcript_source else ''}[/]")
    table = Table(show_edge=False, header_style="bold")
    for col in ("#", "Time", "Len", "Score", "Title"):
        table.add_column(col)
    for i, c in enumerate(p.clips, 1):
        table.add_row(str(i), f"{stamp(c.start)}–{stamp(c.end)}", f"{c.length:.0f}s", f"{c.score:.0f}", escape(c.title))
    console.print(table)
    if dry_run:
        return

    with Progress(TextColumn("[cyan]{task.description}"), BarColumn(), TimeElapsedColumn(),
                  console=console, transient=True) as bar:
        task = bar.add_task("Exporting", total=1)
        engine._progress = lambda msg, f: bar.update(task, description=msg, completed=f or 0)
        folder = engine.export(p)
    console.print(f"[green]✓ {len(p.clips)} clips →[/] {escape(str(folder))}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from . import updater

    finished = updater.finished_update_message()  # result of an update started last time
    if finished:
        console.print(f"[green]✓ {escape(finished[0])}[/]" if finished[1] else f"[yellow]⚠ {escape(finished[0])}[/]")
    if args.check_update or args.update:
        return run_update(install=args.update)
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

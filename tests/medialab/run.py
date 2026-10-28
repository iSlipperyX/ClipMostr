"""Run ClipMonster on the media lab's videos and score what it made against truth.json.

The videos are served from a local web server, so the real downloader (yt-dlp) fetches each page,
its video and its caption track, exactly as it would from a site. Then the real analysis, clip
finding and ffmpeg export run, and every clip is checked:

* its file exists, plays for as long as clips.json says, and is 1080x1920 for a vertical style;
* it starts and ends in the gap between two sentences, never inside one (no cut mid-word);
* its length is inside what was asked for;
* the good moments in each video were found (recall);
* long good stretches came out as Part 1, 2, 3 in order (once part series exist).

    python tests/medialab/run.py LAB_DIR OUT_DIR [--only NAME ...] [--json report.json]

Exit code 1 when any hard check fails. Soft numbers (recall, lengths) are in the report, so two
runs can be compared.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

EDGE_SLACK = 0.06  # seconds a cut may sit inside a sentence's first or last sound


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def serve(folder: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(Quiet, directory=str(folder))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def inside_speech(t: float, sentences: list[list[float]]) -> tuple[float, float] | None:
    for a, b in sentences:
        if a + EDGE_SLACK < t < b - EDGE_SLACK:
            return a, b
    return None


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def lab_settings(out: Path, **extra):
    from ytclip.settings import Settings, all_presets

    s = Settings().apply(all_presets()["Shorts / TikTok / Reels"])
    return s.apply({"output_dir": str(out), "cache_dir": str(out / ".downloads"), "ai_polish": False,
                    "mode": "auto", "thumbnails": False, "check_updates": False, "burn_captions": True,
                    "qc_enabled": True} | extra)


def check_folder(folder: Path, truth: dict, s) -> dict:
    from ytclip.ffmpeg import probe

    index = json.loads((folder / "clips.json").read_text(encoding="utf-8"))
    clips = index["clips"]
    hard: list[str] = []
    cuts_inside = 0
    lengths = []
    for c in clips:
        f = folder / c["file"]
        if not f.exists():
            hard.append(f"{c['file']}: file missing")
            continue
        info = probe(f)
        if abs(info.duration - c["length"]) > 0.35:
            hard.append(f"{c['file']}: plays {info.duration:.2f}s, clips.json says {c['length']:.2f}s")
        if s.aspect.startswith("vertical") and (info.width, info.height) != (1080, 1920):
            hard.append(f"{c['file']}: {info.width}x{info.height}, not 1080x1920")
        for edge in ("start", "end"):
            hit = inside_speech(c[edge], truth["sentences"])
            if hit and not (edge == "start" and c[edge] < 0.5) and not (edge == "end" and
                                                                          c[edge] > truth["duration"] - 0.5):
                cuts_inside += 1
                hard.append(f"{c['file']}: {edge} {c[edge]:.2f}s is inside the sentence {hit[0]:.2f}-{hit[1]:.2f}s")
        lengths.append(c["length"])
    kept = [c for c in clips if not c["file"].startswith("_rejected/")]
    found = sum(1 for m in truth["moments"] if any(overlap(m, (c["start"], c["end"])) >= 3 for c in kept))
    series = truth.get("series") or []
    parts = sorted((c for c in kept if c.get("part")), key=lambda c: (c.get("series", ""), c["part"]))
    covered = 0.0
    for a, b in series:
        covered += sum(overlap((a, b), (c["start"], c["end"])) for c in kept) / max(1.0, b - a)
    return {
        "clips": len(clips), "kept": len(kept), "rejected": len(clips) - len(kept),
        "hard": hard, "cuts_inside_speech": cuts_inside,
        "min_len": min(lengths, default=0), "max_len": max(lengths, default=0),
        "outside_length": sum(1 for x in lengths if x < s.min_len * 0.75 - 0.01 or x > s.max_len + 1.5),
        "moments": len(truth["moments"]), "moments_found": found,
        "series_covered": round(covered / len(series), 2) if series else None,
        "parts": [(c.get("series", ""), c["part"], c.get("parts")) for c in parts],
    }


def run_engine(lab: Path, out: Path, names: list[str], base: str, **extra) -> dict:
    from ytclip.engine import Engine, Project

    truths = json.loads((lab / "truth.json").read_text())
    report = {}
    for name in names:
        s = lab_settings(out / name, **extra)
        log: list[str] = []
        t0 = time.time()
        engine = Engine(s, log=log.append)
        p = Project(f"{base}/{name}.html")
        try:
            engine.prepare(p)
            folder = engine.export(p) if [c for c in p.clips if c.enabled] else None
        except Exception as exc:  # report it and carry on with the next video
            report[name] = {"error": f"{exc.__class__.__name__}: {exc}", "log": log[-15:]}
            print(f"{name}: ERROR {exc}", flush=True)
            continue
        r = check_folder(folder, truths[name], s) if folder else {"clips": 0, "hard": [], "kept": 0}
        r |= {"seconds": round(time.time() - t0, 1), "transcript": p.transcript_source}
        report[name] = r
        print(f"{name}: {r.get('kept', 0)} clips kept of {r.get('clips', 0)}, "
              f"{r.get('moments_found', 0)}/{r.get('moments', 0)} moments, "
              f"{r.get('cuts_inside_speech', 0)} cuts inside speech, {len(r['hard'])} problems, "
              f"{r['seconds']}s", flush=True)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lab", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    truths = json.loads((args.lab / "truth.json").read_text())
    names = args.only or list(truths)
    server, base = serve(args.lab)
    try:
        report = run_engine(args.lab, args.out, names, base)
    finally:
        server.shutdown()
    total = {
        "videos": len(report),
        "errors": sum(1 for r in report.values() if "error" in r),
        "clips": sum(r.get("clips", 0) for r in report.values()),
        "kept": sum(r.get("kept", 0) for r in report.values()),
        "hard_problems": sum(len(r.get("hard", [])) for r in report.values()),
        "cuts_inside_speech": sum(r.get("cuts_inside_speech", 0) for r in report.values()),
        "moments_found": sum(r.get("moments_found", 0) for r in report.values()),
        "moments": sum(r.get("moments", 0) for r in report.values()),
    }
    print(json.dumps(total))
    if args.json:
        args.json.write_text(json.dumps({"total": total, "videos": report}, indent=1))
    return 1 if total["errors"] or total["hard_problems"] else 0


if __name__ == "__main__":
    sys.exit(main())

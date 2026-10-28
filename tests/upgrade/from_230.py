"""Upgrade test: a profile 2.3.0 made, opened by this version.

2.3.0 (a checkout of the v2.3.0 tag, given as BASE) makes a real profile: settings with a niche,
a clipped video (jobs.db, clips.json, the analysis cache), a video still waiting, and a failed one.
Then this version uses that profile: status, doctor, report and requests read it, automation
clips the waiting video (re-measuring the cache 2.3.0 left, which has no fine loudness) and a
request is placed. Nothing may be lost: the old video, its clips and the niche stay as they were.

    python tests/upgrade/from_230.py BASE LAB_DIR WORK_DIR
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "medialab"))

from run import serve  # noqa: E402


def main() -> int:
    base, lab, work = (Path(a).resolve() for a in sys.argv[1:4])
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    server, url = serve(lab)
    env = dict(os.environ, APPDATA=str(work / "roaming"), LOCALAPPDATA=str(work / "local"),
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    env.pop("YOUTUBE_API_KEY", None)
    problems: list[str] = []
    steps: list[str] = []

    def ytclip(root: Path, *args: str, ok: bool = True) -> str:
        out = subprocess.run([sys.executable, "-m", "ytclip", *args], cwd=root, env=env, capture_output=True,
                             text=True, timeout=900)
        text = out.stdout + out.stderr
        steps.append(f"{'2.3.0' if root == base else 'new'}$ ytclip {' '.join(args)} -> {out.returncode}")
        if ok and out.returncode:
            problems.append(f"ytclip {' '.join(args)} ({root.name}) exited {out.returncode}: {text[-600:]}")
        if "Traceback" in text:
            problems.append(f"ytclip {' '.join(args)} ({root.name}) crashed: {text[-1200:]}")
        return text

    profile = work / "roaming" / "ClipMonster"
    profile.mkdir(parents=True)
    settings = {"auto_output_dir": str(work / "clips"), "cache_dir": str(work / "cache"), "auto_gap_minutes": 0,
                "auto_polish": False, "check_updates": False, "auto_delete_sources": False,
                "niches": [{"id": "talk", "name": "Talk", "account": "@talk", "searches": "talk",
                            "instructions": "Keep it light."}]}
    (profile / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    # ---- 2.3.0 makes the profile
    ytclip(base, "auto", "--add", f"{url}/talk-short.html", "--niche", "Talk")
    ytclip(base, "auto", "--once")
    ytclip(base, "auto", "--add", f"{url}/no-such-video.html")
    ytclip(base, "auto", "--once", ok=False)
    ytclip(base, "auto", "--add", f"{url}/vertical-8.html")
    con = sqlite3.connect(profile / "jobs.db")
    old_jobs = {r[0]: r[1] for r in con.execute("SELECT id, status FROM jobs")}
    old_cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)")]
    con.close()
    done = [j for j, st in old_jobs.items() if st == "done"]
    if len(done) != 1:
        problems.append(f"2.3.0 didn't clip the first video: {old_jobs}")
    old_settings = json.loads((profile / "settings.json").read_text(encoding="utf-8"))
    old_index = sorted((work / "clips").rglob("clips.json"))
    old_clips = {str(p): json.loads(p.read_text(encoding="utf-8"))["clips"] for p in old_index}

    # ---- this version opens it
    ytclip(ROOT, "auto", "--status")
    ytclip(ROOT, "doctor", "--offline", ok=False)  # warnings (no YouTube key) exit 0, problems exit 1
    report = ytclip(ROOT, "report", "--hours", "24")
    if "1 video failed" not in report and "failed" not in report:
        problems.append(f"the report doesn't mention the failed video: {report}")
    ytclip(ROOT, "request", "--list")
    ytclip(ROOT, "auto", "--once")
    con = sqlite3.connect(profile / "jobs.db")
    con.row_factory = sqlite3.Row
    jobs = {r["id"]: dict(r) for r in con.execute("SELECT * FROM jobs")}
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    if integrity != "ok":
        problems.append(f"jobs.db integrity: {integrity}")
    for jid, status in old_jobs.items():
        if jid not in jobs:
            problems.append(f"{jid} was lost from the queue")
        elif status == "done" and jobs[jid]["status"] != "done":
            problems.append(f"{jid} was done in 2.3.0 and is {jobs[jid]['status']} now")
    waiting = [j for j in jobs.values() if "vertical-8" in j["url"]]
    if not waiting or waiting[0]["status"] != "done":
        problems.append(f"the video 2.3.0 left waiting wasn't clipped: {waiting}")
    if "order" not in jobs[next(iter(jobs))]:
        problems.append("jobs.db didn't gain the request column")
    for path, clips in old_clips.items():
        if json.loads(Path(path).read_text(encoding="utf-8"))["clips"] != clips:
            problems.append(f"{path} changed")
    now_settings = json.loads((profile / "settings.json").read_text(encoding="utf-8"))
    if now_settings.get("niches") != old_settings.get("niches"):
        problems.append("the niches changed")
    caches = list((work / "cache").glob("*.analysis.npz"))
    import numpy as np

    fine = [c.name for c in caches if "fine" in np.load(c).files]
    ytclip(ROOT, "request", "2", "--length", "15-25", "--link", f"{url}/talk-10.html")
    ytclip(ROOT, "auto", "--once")
    lines = ytclip(ROOT, "request", "--list")
    if "2 of 2" not in lines and "ready" not in lines.lower():
        problems.append(f"the request didn't finish: {lines}")
    server.shutdown()
    result = {"steps": steps, "old_columns": old_cols, "jobs": {k: v["status"] for k, v in jobs.items()},
              "caches_with_fine_loudness": fine, "problems": problems}
    (work / "upgrade.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

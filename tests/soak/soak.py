"""Soak test: automation runs for an hour on the media lab's videos while things go wrong.

The real `ytclip auto` runs in its own process (worker.py), with its own profile, on videos the
real downloader fetches from a local web server. On a timeline the test:

* kills automation mid-export (kill -9) and starts it again: the video goes back in the queue;
* makes an export hang (the watchdog stops it and tries once more);
* reports the disk nearly full for a few minutes (the disk guard cleans up, pauses, carries on);
* makes a download fail with YouTube's sign-in request (a pause), then presses Try now;
* queues a broken link (it fails, and nothing else does);
* changes settings while it runs (fewer clips per video, a niche paused and resumed);
* places a clip request with links, and cancels another.

Then it checks the end state: every video done or failed for a reason, every clip on disk and
playable at the size asked for, jobs.db intact, no ffmpeg left running, the pauses in the morning
report, and no tracebacks in the log.

    python tests/soak/soak.py LAB_DIR WORK_DIR [--minutes 60]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "medialab"))

from run import serve  # noqa: E402

VIDEOS = ["talk-short", "talk-10", "vertical-8", "series-12", "burned-subs-6", "indonesian-6", "quiet-8",
          "square-ish-6", "gaming-10", "fast-auto-8", "talk-20", "series-long-15", "podcast-40"]


class Soak:
    def __init__(self, lab: Path, work: Path, minutes: float):
        self.lab, self.work, self.minutes = lab, work, minutes
        self.profile = work / "profile"
        self.out = work / "clips"
        self.control = work / "control.json"
        self.events = work / "events.jsonl"
        self.timeline: list[str] = []
        self.problems: list[str] = []
        self.proc: subprocess.Popen | None = None
        self.started = time.time()
        self.round = 0

    # ------------------------------------------------------------ the profile and the worker
    def env(self) -> dict:
        env = dict(os.environ, APPDATA=str(self.profile / "roaming"), LOCALAPPDATA=str(self.profile / "local"),
                   SOAK_CONTROL=str(self.control), NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
                   PYTHONUNBUFFERED="1")
        env.pop("YOUTUBE_API_KEY", None)
        env.pop("ANTHROPIC_API_KEY", None)
        return env

    def py(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        out = subprocess.run([sys.executable, "-m", "ytclip", *args], env=self.env(), cwd=ROOT, capture_output=True,
                             text=True, timeout=300)
        if check and out.returncode:
            raise RuntimeError(f"ytclip {' '.join(args)} failed: {out.stdout}{out.stderr}")
        return out

    def settings(self, **change) -> None:
        sys.path.insert(0, str(ROOT))
        path = self.profile / "roaming" / "ClipMonster" / "settings.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.update(change)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def set_control(self, **flags) -> None:
        data = json.loads(self.control.read_text()) if self.control.exists() else {}
        data.update(flags)
        self.control.write_text(json.dumps(data))

    def start(self) -> None:
        log = open(self.work / "worker.out", "a", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "soak" / "worker.py")], env=self.env(),
                                     cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        self.say(f"automation started (process {self.proc.pid})")

    def say(self, msg: str) -> None:
        line = f"{(time.time() - self.started) / 60:6.1f} min  {msg}"
        self.timeline.append(line)
        print(line, flush=True)

    # ------------------------------------------------------------ reading what automation does
    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.profile / "roaming" / "ClipMonster" / "jobs.db", timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def jobs(self) -> list[sqlite3.Row]:
        with self.db() as con:
            return con.execute("SELECT * FROM jobs").fetchall()

    def meta(self, key: str) -> str:
        with self.db() as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else ""

    def count(self, kind: str) -> int:
        with self.db() as con:
            return con.execute("SELECT COUNT(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]

    def wait_for(self, what: str, test, limit: float = 900, every: float = 2.0) -> bool:
        end = time.time() + limit
        while time.time() < end:
            try:
                if test():
                    return True
            except sqlite3.Error:
                pass
            if self.proc and self.proc.poll() is not None:
                self.problems.append(f"automation exited (code {self.proc.returncode}) while waiting for {what}")
                return False
            time.sleep(every)
        self.problems.append(f"gave up waiting for {what} after {limit / 60:.0f} min")
        return False

    def busy(self) -> list[sqlite3.Row]:
        return [j for j in self.jobs() if j["status"] in ("queued", "working")]

    # ------------------------------------------------------------ the timeline
    def queue_round(self) -> None:
        self.round += 1
        tag = "" if self.round == 1 else f"?round={self.round}"
        for name in VIDEOS:
            self.py("auto", "--add", f"{self.base}/{name}.html{tag}")
        self.say(f"queued round {self.round}: {len(VIDEOS)} videos")

    def run(self) -> int:
        server, self.base = serve(self.lab)
        self.work.mkdir(parents=True, exist_ok=True)
        self.settings(auto_output_dir=str(self.out), cache_dir=str(self.work / "cache"), auto_gap_minutes=0,
                      watchdog_minutes=1, disk_min_free_gb=10, auto_polish=False, check_updates=False,
                      auto_retries=3, cleanup_auto=False,
                      niches=[{"id": "talk", "name": "Talk", "account": "@talk", "searches": "x"}])
        self.set_control()
        self.queue_round()
        self.py("auto", "--add", f"{self.base}/no-such-video.html")  # a broken link
        self.py("request", "3", "--length", "15-30", "--link", f"{self.base}/talk-10.html?req=1",
                f"{self.base}/vertical-8.html?req=1", "--notes", "funny bits")
        self.py("request", "2", "--link", f"{self.base}/quiet-8.html?req=2")
        cancel = json.loads(self.db().execute("SELECT data FROM orders ORDER BY created DESC").fetchone()[0])["id"]
        self.py("request", "--cancel", cancel)
        self.say("queued a broken link, a request with two links, and cancelled a second request")
        self.start()

        # 1. kill -9 mid-export, start again
        if self.wait_for("an export", lambda: "Exporting" in self.meta("state"), 600, 0.5):
            victim = next((j["id"] for j in self.jobs() if j["status"] == "working"), "")
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()
            self.say(f"killed automation mid-export ({victim})")
            time.sleep(3)
            self.start()
            self.wait_for("the killed video to be done", lambda: any(j["id"] == victim and j["status"] == "done"
                                                                      for j in self.jobs()), 900)
            self.say("the killed video was clipped after the restart")

        # 2. a hung export: the watchdog stops it (1 min limit) and it's tried once more
        self.set_control(hang_export=1)
        self.say("an export will hang")
        if self.wait_for("the watchdog", lambda: self.count("watchdog") >= 1, 600):
            self.say("the watchdog stopped the hung export")

        # 3. disk nearly full for 3 minutes
        self.set_control(low_disk=True)
        self.say("the disk reports 1 GB free")
        if self.wait_for("the disk guard", lambda: self.meta("disk_low") == "1", 600):
            self.say(f"automation paused: {self.meta('state')}")
        time.sleep(180)
        working = [j["id"] for j in self.jobs() if j["status"] == "working"]
        if working:
            self.problems.append(f"videos were clipped with the disk full: {working}")
        self.set_control(low_disk=False)
        self.say("the disk has room again")
        self.wait_for("automation to carry on", lambda: self.meta("disk_low") == "0", 900)

        # 4. a sign-in request from "YouTube", then Try now
        self.set_control(block_downloads=1)
        self.say("the next download meets a sign-in request")
        if self.wait_for("the sign-in pause", lambda: self.count("blocked") >= 1, 900):
            self.say(f"automation paused: {self.meta('state')}")
            time.sleep(60)
            out = self.py("auto", "--try-now")
            self.say(f"pressed Try now: {out.stdout.strip()}")
            self.wait_for("downloads again", lambda: any(j["status"] == "working" for j in self.jobs()), 600)

        # 5. settings changed while it runs
        self.settings(clip_count=3)
        self.say("changed Clips per video to 3 while it runs")
        changed_at = time.time()
        self.settings(niches=[{"id": "talk", "name": "Talk", "account": "@talk", "searches": "x", "paused": True}])
        time.sleep(30)
        self.settings(niches=[{"id": "talk", "name": "Talk", "account": "@talk", "searches": "x"}])
        self.say("paused and resumed a niche")

        # 6. keep it busy until the hour is up
        while time.time() - self.started < self.minutes * 60:
            if not self.busy():
                self.queue_round()
            if self.proc.poll() is not None:
                self.problems.append(f"automation exited by itself (code {self.proc.returncode})")
                self.start()
            time.sleep(10)
        self.say("time's up: waiting for the video in progress, then stopping")
        self.wait_for("the queue to empty", lambda: not self.busy(), 1800, 5)
        self.py("auto", "--stop")
        try:
            self.proc.wait(120)
        except subprocess.TimeoutExpired:
            self.problems.append("automation didn't stop within 2 minutes of --stop")
            self.proc.kill()
        server.shutdown()
        self.check(changed_at)
        return self.finish()

    # ------------------------------------------------------------ the end state
    def check(self, changed_at: float) -> None:
        from ytclip.ffmpeg import probe

        jobs = self.jobs()
        self.summary = {"videos": len(jobs), "done": 0, "failed": 0, "clips": 0, "files_checked": 0}
        for j in jobs:
            if j["status"] not in ("done", "failed"):
                self.problems.append(f"{j['id']} ended {j['status']} ({j['stage']})")
            if j["status"] == "failed":
                self.summary["failed"] += 1
                if "no-such-video" not in j["url"]:
                    self.problems.append(f"{j['id']} failed: {j['error'][:200]}")
                continue
            if j["status"] != "done":
                continue
            self.summary["done"] += 1
            folder = Path(j["folder"] or "")
            index = folder / "clips.json"
            if not index.exists():
                if j["clips"]:
                    self.problems.append(f"{j['id']}: no clips.json in {folder}")
                continue
            data = json.loads(index.read_text(encoding="utf-8"))
            for c in data["clips"]:
                f = folder / c["file"]
                if not f.exists():
                    self.problems.append(f"{j['id']}: {c['file']} is missing")
                    continue
                info = probe(f)
                self.summary["files_checked"] += 1
                if not c["file"].startswith("_rejected/"):
                    self.summary["clips"] += 1
                    if (info.width, info.height) != (1080, 1920) or abs(info.duration - c["length"]) > 0.5:
                        self.problems.append(f"{j['id']}: {c['file']} is {info.width}x{info.height}, "
                                             f"{info.duration:.1f}s (clips.json says {c['length']}s)")
            if j["updated"] > changed_at + 120 and not j["order"] and not j["niche"]:
                kept = [c for c in data["clips"] if not c["file"].startswith("_rejected/")]
                if len(kept) > 3:
                    self.problems.append(f"{j['id']}: {len(kept)} clips after Clips per video went to 3")
        broken = [j for j in jobs if "no-such-video" in j["url"]]
        if not broken or broken[0]["status"] != "failed":
            self.problems.append("the broken link didn't end failed")
        with self.db() as con:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            orders = [json.loads(r[0]) for r in con.execute("SELECT data FROM orders")]
        if ok != "ok":
            self.problems.append(f"jobs.db integrity check: {ok}")
        for o in orders:
            if o["status"] == "open":
                self.problems.append(f"request {o['id']} still open ({o.get('message', '')})")
        self.summary["requests"] = [(o["count"], o["made"], o["status"]) for o in orders]
        for kind in ("watchdog", "disk_low", "blocked"):
            n = self.count(kind)
            self.summary[kind] = n
            if not n:
                self.problems.append(f"no {kind} event was recorded")
        left = subprocess.run(["pgrep", "-f", "anullsrc|ffmpeg -v"], capture_output=True, text=True).stdout.split()
        left = [pid for pid in left if pid != str(os.getpid())]
        if left:
            self.problems.append(f"ffmpeg still running after automation stopped: {left}")
        log = self.profile / "roaming" / "ClipMonster" / "automation.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        if "Traceback" in text:
            self.problems.append("a traceback in automation.log")
        report = self.py("report", "--hours", "3", "--json")
        self.report = json.loads(report.stdout)
        if self.report["videos"] != self.summary["done"]:
            self.problems.append(f"the report counts {self.report['videos']} videos, jobs.db {self.summary['done']}")
        self.lines = self.py("report", "--hours", "3").stdout

    def finish(self) -> int:
        out = {"minutes": round((time.time() - self.started) / 60, 1), "timeline": self.timeline,
               "summary": self.summary, "problems": self.problems, "report": self.lines.splitlines()}
        (self.work / "soak.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(json.dumps(out["summary"]), flush=True)
        print(self.lines, flush=True)
        for p in self.problems:
            print("PROBLEM:", p, flush=True)
        return 1 if self.problems else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lab", type=Path)
    ap.add_argument("work", type=Path)
    ap.add_argument("--minutes", type=float, default=60)
    args = ap.parse_args()
    return Soak(args.lab, args.work, args.minutes).run()


if __name__ == "__main__":
    sys.exit(main())

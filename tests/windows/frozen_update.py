"""Updates a real PyInstaller build of ClipMonster with its own helper, the way installed copies do.

Run on Windows after `python build.py` (the Tests workflow's frozen-update job does):

    python tests/windows/frozen_update.py dist/ClipMonster

It copies the build twice into a temporary folder: an "installed" copy, and an "update" copy marked
with an extra file. The update's ytclip.exe then installs itself over the installed copy (with a
program holding a folder inside it open), keeping the replaced copy as the previous version, and the
result is checked.
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main(build: Path) -> int:
    base = Path(tempfile.mkdtemp(prefix="cm-frozen-update-"))
    app = base / "ClipMonster"
    new = base / ".cm-up" / "ClipMonster"
    log = base / "update.log"
    shutil.copytree(build, app)
    shutil.copytree(build, new)
    (new / "_internal" / "only-in-the-update.txt").write_text("new", encoding="utf-8")
    (app / "_internal" / "only-in-the-old-one.txt").write_text("old", encoding="utf-8")
    (app / "my-notes.txt").write_text("keep me", encoding="utf-8")
    # A terminal open inside the app folder: that folder can't be renamed while it's held.
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=str(app / "_internal"))
    try:
        start = time.monotonic()
        r = subprocess.run([str(new / "ytclip.exe"), "--apply-update", "--wait-pid", "0", "--new-dir", str(new),
                            "--install-dir", str(app), "--log-file", str(log),
                            "--keep-previous", str(base / "previous"), "--old-version", "1.0.0"],
                           capture_output=True, text=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        took = time.monotonic() - start
    finally:
        holder.kill()
        holder.wait()
    text = log.read_text(encoding="utf-8") if log.exists() else "(no log)"
    print(text)
    print(r.stdout, r.stderr)
    problems = []
    if r.returncode:
        problems.append(f"helper exit code {r.returncode}")
    if not text.rstrip().endswith("Update finished"):
        problems.append("the log doesn't end with 'Update finished'")
    if not (app / "_internal" / "only-in-the-update.txt").exists():
        problems.append("the new files weren't installed")
    if (app / "_internal" / "only-in-the-old-one.txt").exists():
        problems.append("an old file was left behind")
    if (app / "my-notes.txt").read_text(encoding="utf-8") != "keep me":
        problems.append("your own file was touched")
    if any(p.name.startswith(".cm-old") for p in app.iterdir()):
        problems.append("the backup folder was left behind")
    kept = base / "previous" / "ClipMonster"
    if not (kept / "_internal" / "only-in-the-old-one.txt").exists() or not (kept / "ClipMonster.exe").exists():
        problems.append("the previous version wasn't kept")
    elif (base / "previous" / "version.txt").read_text(encoding="utf-8") != "1.0.0":
        problems.append("the previous version's number wasn't kept")
    if "powershell" in text.lower():
        problems.append("PowerShell was mentioned")
    v = subprocess.run([str(app / "ytclip.exe"), "--version"], capture_output=True, text=True, timeout=120)
    if v.returncode:
        problems.append(f"the installed ytclip.exe doesn't run: {v.stderr.strip()[:300]}")
    print(f"Installed in {took:.1f} s; the installed ytclip.exe says: {v.stdout.strip()}")
    shutil.rmtree(base, ignore_errors=True)
    for p in problems:
        print(f"FAIL: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve()))

"""Installs ClipMonster-Setup.exe for real, installs it again over itself (an update), then uninstalls it.

Run on a throwaway Windows machine after `python build.py --installer` (the Tests workflow's
frozen-update job does). It installs into this account's %LOCALAPPDATA%\\Programs\\ClipMonster and
changes the Start with Windows entry, so it refuses to run anywhere but CI:

    python tests/windows/setup_exe.py dist/ClipMonster-Setup.exe
"""

import os
import subprocess
import sys
import time
import winreg
from pathlib import Path

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{92B949DB-584A-40C4-892B-F7016573CE28}_is1"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClipMonster Automation"
APP = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "ClipMonster"
SILENT = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]


def entry(name: str) -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return ""


def install(setup: Path, log: Path) -> int:
    start = time.monotonic()
    code = subprocess.run([str(setup), *SILENT, f"/LOG={log}"], timeout=900).returncode
    print(f"Setup exited with {code} after {time.monotonic() - start:.0f} s")
    return code


def main(setup: Path) -> int:
    if not os.environ.get("CI"):
        print("This installs and uninstalls ClipMonster for this Windows account: it only runs in CI.")
        return 2
    problems = []
    logs = Path(os.environ.get("RUNNER_TEMP", os.environ["TEMP"]))
    if install(setup, logs / "setup-1.log"):
        problems.append("the first install failed")
    for name in ("ClipMonster.exe", "ytclip.exe", "_internal", "unins000.exe"):
        if not (APP / name).exists():
            problems.append(f"{name} wasn't installed in {APP}")
    v = subprocess.run([str(APP / "ytclip.exe"), "--version"], capture_output=True, text=True, timeout=120)
    print(f"The installed ytclip.exe says: {v.stdout.strip()}")
    if v.returncode:
        problems.append(f"the installed ytclip.exe doesn't run: {v.stderr.strip()[:300]}")
    where = entry("InstallLocation").rstrip("\\")
    if os.path.normcase(where) != os.path.normcase(str(APP)):
        problems.append(f"the uninstall entry says it's in '{where}'")
    print(f"Installed apps list: {entry('DisplayName')} {entry('DisplayVersion')}")
    stop = subprocess.run([str(APP / "ytclip.exe"), "auto", "--stop", "--wait"], capture_output=True, text=True,
                          timeout=180)
    if stop.returncode != 0:  # nothing was running: 0 (10 = it was running and stopped)
        problems.append(f"`ytclip auto --stop --wait` exited with {stop.returncode}: {stop.stdout}{stop.stderr}")

    # Over itself, the way people update with a newly downloaded Setup.exe
    (APP / "_internal" / "left-by-an-old-version.txt").write_text("old", encoding="utf-8")
    if install(setup, logs / "setup-2.log"):
        problems.append("installing over the installed copy failed")
    if (APP / "_internal" / "left-by-an-old-version.txt").exists():
        problems.append("a file from the old version was left in _internal")

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:  # Start with Windows, as the app sets it
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{APP / "ClipMonster.exe"}" --automation')
    uninstaller = entry("UninstallString").strip('"')
    if not uninstaller:
        problems.append("there's no uninstall entry")
    else:
        subprocess.run([uninstaller, *SILENT], timeout=300)
        end = time.monotonic() + 180  # the uninstaller hands over to a copy of itself and returns at once
        while (APP / "ClipMonster.exe").exists() and time.monotonic() < end:
            time.sleep(2)
        time.sleep(5)
        if (APP / "ClipMonster.exe").exists() or (APP / "_internal").exists():
            problems.append("uninstalling left the app's files behind")
        if entry("DisplayName"):
            problems.append("uninstalling left the uninstall entry behind")
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, RUN_VALUE)
            problems.append("uninstalling left Start with Windows pointing at the removed app")
        except OSError:
            pass
    for log in sorted(logs.glob("setup-*.log")):
        print(f"--- {log.name} (end)")
        print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]))
    for p in problems:
        print(f"FAIL: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve()))

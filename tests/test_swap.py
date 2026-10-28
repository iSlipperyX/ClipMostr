"""The updater's install step (swap.py), against the things that broke real updates: folders held open
by File Explorer or a terminal, a file that stays locked, your own files kept in the app folder, a
leftover backup from an earlier run. On Windows the locks are real; elsewhere they're simulated."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ytclip import swap, updater


def write_build(folder: Path, tag: str) -> None:
    for d in ("_internal/sub/deep", "_internal/held", f"_internal/only-{tag}/inner", f"_internal/empty-{tag}"):
        (folder / d).mkdir(parents=True, exist_ok=True)
    files = {"ClipMonster.exe": f"exe {tag}", "ytclip.exe": f"cli {tag}", "_internal/a.dll": f"dll {tag}",
             "_internal/sub/deep/b.pyd": f"pyd {tag}", "_internal/held/c.dat": f"dat {tag}",
             f"_internal/only-{tag}.txt": tag, f"_internal/only-{tag}/inner/f.txt": tag}
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")


class Case:
    def __init__(self, root: Path):
        self.root = root
        self.app = root / "ClipMonster"
        write_build(self.app, "old")
        (self.app / "my-stuff").mkdir()
        (self.app / "my-stuff" / "notes.txt").write_text("keep me", encoding="utf-8")
        self.new = root / ".cm-up" / "ClipMonster"
        write_build(self.new, "new")
        self.log_file = root / "update.log"
        self.lines: list[str] = []

    def log(self, line: str) -> None:
        self.lines.append(line)

    def read(self, rel: str) -> str:
        p = self.app / rel
        return p.read_text(encoding="utf-8").strip() if p.exists() else "<missing>"

    def run(self, wait_pid: int = 0, **kw) -> bool:
        kw.setdefault("sleep", lambda s: None)
        return swap.apply(self.new, self.app, wait_pid, "", self.log, **kw)

    def backups(self) -> list[Path]:
        return [p for p in self.app.iterdir() if p.name.startswith(".cm-old")]

    def check_updated(self) -> None:
        assert self.read("ClipMonster.exe") == "exe new" and self.read("ytclip.exe") == "cli new"
        assert self.read("_internal/a.dll") == "dll new" and self.read("_internal/sub/deep/b.pyd") == "pyd new"
        assert self.read("_internal/only-new.txt") == "new" and self.read("_internal/only-new/inner/f.txt") == "new"
        assert not (self.app / "_internal/only-old.txt").exists() and not (self.app / "_internal/only-old").exists()
        assert (self.app / "_internal/empty-new").is_dir()
        assert self.read("my-stuff/notes.txt") == "keep me"
        assert self.backups() == []
        assert self.lines[-1] == "Update finished"

    def check_untouched(self) -> None:
        assert self.read("ClipMonster.exe") == "exe old" and self.read("ytclip.exe") == "cli old"
        assert self.read("_internal/a.dll") == "dll old" and self.read("_internal/sub/deep/b.pyd") == "pyd old"
        assert self.read("_internal/only-old.txt") == "old" and self.read("_internal/only-old/inner/f.txt") == "old"
        assert not (self.app / "_internal/only-new.txt").exists() and not (self.app / "_internal/only-new").exists()
        assert (self.app / "_internal/empty-old").is_dir()
        assert (self.new / "_internal/a.dll").read_text(encoding="utf-8") == "dll new"  # the update is still there
        assert self.backups() == []
        assert self.read("my-stuff/notes.txt") == "keep me"
        assert any("previous version restored" in line for line in self.lines)
        assert self.lines[-1].startswith("Update failed: ")


@pytest.fixture
def case(tmp_path):
    return Case(tmp_path)


def test_plain_update(case):
    assert case.run()
    case.check_updated()
    assert case.lines[0] == "Update started: waiting for process 0 to exit"
    assert any(line.startswith("New version installed in ") for line in case.lines)


def test_waits_for_the_app_to_close(case):
    import threading

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    threading.Thread(target=p.wait, daemon=True).start()  # reaped as it ends, like the app's own exit
    start = time.monotonic()
    assert case.run(p.pid, sleep=time.sleep)
    assert time.monotonic() - start >= 1.0
    case.check_updated()


def test_gives_up_on_an_app_that_doesnt_close(case, monkeypatch):
    monkeypatch.setattr(swap, "process_alive", lambda pid: True)
    assert not case.run(1234, wait=0)
    assert case.lines[-1] == "Update failed: ClipMonster did not close within 2 minutes"
    assert case.read("ClipMonster.exe") == "exe old"


def test_other_programs_from_the_folder_must_end_first(case, monkeypatch):
    monkeypatch.setattr(swap, "app_processes", lambda folder: [(77, folder / "ytclip.exe")])
    assert not case.run(others=0)
    assert case.lines[-1] == "Update failed: Close these first: ytclip.exe (process 77)"
    assert case.read("ClipMonster.exe") == "exe old"


def _real_rename():
    return os.rename


def test_a_folder_that_cant_move_moves_file_by_file(case, monkeypatch):
    """What File Explorer or a terminal open in _internal does: the folder can't be renamed, its files can."""
    rename = _real_rename()
    held = case.app / "_internal"

    def fake(src, dst):
        if Path(src) == held:
            raise PermissionError(13, "The process cannot access the file because it is being used")
        rename(src, dst)

    monkeypatch.setattr(swap.os, "rename", fake)
    assert case.run()
    case.check_updated()
    assert any("file by file" in line for line in case.lines)


def test_a_locked_file_rolls_everything_back(case, monkeypatch):
    rename = _real_rename()
    locked = case.app / "_internal/sub/deep/b.pyd"
    held = case.app / "_internal"

    def fake(src, dst):
        if Path(src) in (locked, held):  # its folder can't move either, so it's met file by file
            raise PermissionError(13, "in use")
        rename(src, dst)

    monkeypatch.setattr(swap.os, "rename", fake)
    assert not case.run()
    case.check_untouched()
    assert "b.pyd" in case.lines[-1]


def test_a_failed_copy_rolls_everything_back(case, monkeypatch):
    copy2 = swap.shutil.copy2

    def fake(src, dst):
        if Path(src).name == "b.pyd":
            raise OSError(32, "The process cannot access the file")
        return copy2(src, dst)

    monkeypatch.setattr(swap.shutil, "copy2", fake)
    assert not case.run()
    case.check_untouched()


def test_a_restore_that_cant_finish_leaves_instructions(case, monkeypatch):
    rename = _real_rename()
    state = {"installing": True}
    copy2 = swap.shutil.copy2

    def fake_copy(src, dst):
        if Path(src).name == "b.pyd":
            state["installing"] = False
            raise OSError(32, "in use")
        return copy2(src, dst)

    def fake_rename(src, dst):
        if not state["installing"] and Path(dst).name == "ytclip.exe":  # putting the old ytclip.exe back fails
            raise PermissionError(13, "in use")
        rename(src, dst)

    monkeypatch.setattr(swap.shutil, "copy2", fake_copy)
    monkeypatch.setattr(swap.os, "rename", fake_rename)
    assert not case.run()
    note = (case.root / swap.NOTE).read_text(encoding="utf-8")
    assert "Don't delete anything" in note and "ytclip.exe" in note
    assert any((b / swap.KEEP).exists() for b in case.backups())
    assert any(line.startswith("RESTORE FAILED") for line in case.lines)
    assert case.read("ClipMonster.exe") == "exe old"  # everything else went back


def test_a_leftover_backup_and_failure_note_from_an_earlier_run(case):
    write_build(case.app / ".cm-old", "stale")
    write_build(case.app / ".cm-old-20240101000000", "stale")
    (case.root / swap.NOTE).write_text("old note", encoding="utf-8")
    assert case.run()
    case.check_updated()
    assert not (case.root / swap.NOTE).exists()


def test_the_app_restarts_after_an_update_and_after_a_failure(case, monkeypatch):
    started = []
    monkeypatch.setattr(swap, "start_app", lambda install, relaunch, log: started.append((install, relaunch)))
    assert swap.apply(case.new, case.app, 0, "ClipMonster.exe", case.log, sleep=lambda s: None)
    assert started == [(case.app, "ClipMonster.exe")]
    monkeypatch.setattr(swap, "process_alive", lambda pid: True)
    assert not swap.apply(case.new, case.app, 5, "ClipMonster.exe", case.log, sleep=lambda s: None, wait=0)
    assert len(started) == 2  # the old version is opened again


def test_command_runs_the_new_versions_helper(tmp_path, monkeypatch):
    new = tmp_path / "new"
    cmd = swap.command(new, tmp_path / "app", 9, tmp_path / "u.log", "")
    assert cmd[:3] == [sys.executable, "-m", "ytclip.swap"] and "--relaunch" not in cmd and "" not in cmd
    monkeypatch.setattr(sys, "frozen", True, raising=False)  # the installed app
    cmd = swap.command(new, tmp_path / "app", 9, tmp_path / "u.log", "ClipMonster.exe")
    assert cmd[:2] == [str(new / "ytclip.exe"), "--apply-update"]
    assert cmd[cmd.index("--wait-pid") + 1] == "9" and cmd[-2:] == ["--relaunch", "ClipMonster.exe"]
    assert "powershell" not in " ".join(cmd).lower()


def test_the_helper_runs_as_its_own_process(case):
    """End to end, the way updater.launch_swap starts it: a separate process writing the update log."""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    cmd = swap.command(case.new, case.app, 0, case.log_file, "")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    log = case.log_file.read_text(encoding="utf-8")
    assert log.splitlines()[-1].endswith("Update finished")
    case.lines = [line.split(" ", 2)[2] for line in log.splitlines()]
    case.check_updated()


def test_cli_hands_apply_update_to_the_helper(case):
    from ytclip import cli

    assert cli.main(["--apply-update", "--new-dir", str(case.new), "--install-dir", str(case.app),
                     "--log-file", str(case.log_file)]) == 0
    assert case.read("ClipMonster.exe") == "exe new"


def test_launch_swap_starts_the_helper_without_powershell(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    calls = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    updater.launch_swap(tmp_path / "new", tmp_path / "ClipMonster", 1, relaunch="")
    updater.launch_swap(tmp_path / "new", tmp_path / "ClipMonster", 1)
    assert "--relaunch" not in calls[0][0] and "" not in calls[0][0]
    assert calls[1][0][calls[1][0].index("--relaunch") + 1] == "ClipMonster.exe"
    assert all("powershell" not in " ".join(c).lower() for c, _ in calls)
    assert updater.last_attempt()[0] == "requested"


def test_tidy_removes_the_unpacked_update_and_old_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    app = tmp_path / "ClipMonster"
    app.mkdir()
    write_build(tmp_path / ".cm-up" / "ClipMonster", "new")
    for v in ("2.3.1", "2.4.0"):
        (updater.updates_dir() / f"ClipMonster-windows-{v}.zip").write_bytes(b"x")
    (updater.updates_dir() / "update.log").write_text("keep", encoding="utf-8")
    assert updater.tidy(app) == 3
    assert not (tmp_path / ".cm-up").exists()
    assert [p.name for p in updater.updates_dir().iterdir()] == ["update.log"]
    assert updater.tidy(app) == 0


# ---------------------------------------------------------------- real Windows locks


def _lock(path: Path):
    """Open a file the way a running program does that shares nothing (Windows only)."""
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                              wintypes.DWORD, wintypes.HANDLE)
    h = k.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, no sharing, OPEN_EXISTING
    assert h and h != wintypes.HANDLE(-1).value
    return lambda: k.CloseHandle(wintypes.HANDLE(h))


@pytest.mark.skipif(os.name != "nt", reason="Windows file locks")
def test_windows_folders_held_open_by_programs(case):
    holders = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=str(d))
               for d in (case.app, case.app / "_internal" / "held")]
    try:
        assert case.run()
    finally:
        for p in holders:
            p.kill()
            p.wait()
    case.check_updated()
    assert (case.app / "_internal" / "held").is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows file locks")
def test_windows_a_locked_old_file_rolls_everything_back(case, monkeypatch):
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              cwd=str(case.app / "_internal" / "sub"))  # so _internal moves file by file
    release = _lock(case.app / "_internal" / "sub" / "deep" / "b.pyd")
    try:
        assert not case.run()
    finally:
        release()
        holder.kill()
        holder.wait()
    case.check_untouched()


@pytest.mark.skipif(os.name != "nt", reason="Windows file locks")
def test_windows_a_locked_new_file_rolls_everything_back(case):
    release = _lock(case.new / "_internal" / "sub" / "deep" / "b.pyd")
    try:
        assert not case.run()
    finally:
        release()
    case.check_untouched()

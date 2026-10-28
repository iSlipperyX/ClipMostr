"""Installs a downloaded update over the app folder: what updater.launch_swap starts.

It runs as the NEW version's own ytclip.exe, from the unpacked update (`ytclip.exe --apply-update
...`), once the app has exited: a running app can't replace its own files on Windows. Until 2.4.1
this was a hidden PowerShell script started with -ExecutionPolicy Bypass, which is the kind of
thing Windows Defender's behaviour checks treat as malware.

The install folder itself is never renamed: an open File Explorer window, a terminal or any
program whose working folder is inside it would block that. Instead the app's own files move into
a backup folder inside it, file by file when a whole folder can't move, and the new files are
copied in (this program runs from them, so they can't move). Anything else in the folder (your own
files) is left alone. If anything fails, everything is put back.

Keep the command line stable: an older version starts the newer version's helper with it. The
replaced version is kept as the previous one (--keep-previous), for going back to it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .procs import app_processes, process_alive

APP_ITEMS = ("ClipMonster.exe", "ytclip.exe", "_internal")
EXE_NAME = "ClipMonster.exe"
BACKUP = ".cm-old"
KEEP = ".keep-recovery"  # in a backup holding files a failed restore couldn't put back
NOTE = "CLIPMONSTER-UPDATE-FAILED.txt"


class SwapError(RuntimeError):
    pass


def is_complete(folder: Path) -> bool:
    return (folder / EXE_NAME).is_file() and (folder / "_internal").is_dir()


def old_backups(install: Path) -> list[Path]:
    try:
        return [p for p in install.iterdir() if p.is_dir() and p.name.startswith(BACKUP)]
    except OSError:
        return []


class Swap:
    def __init__(self, new_dir: Path, install: Path, log: Callable[[str], None],
                 sleep: Callable[[float], None] = time.sleep, pause: float = 0.5):
        self.new = Path(os.path.abspath(new_dir))
        self.install = Path(os.path.abspath(install))
        self.log, self.sleep, self.pause = log, sleep, pause
        self.moves: list[tuple[Path, Path, str]] = []  # every move made, newest last, to undo on failure
        self.created: list[Path] = []  # folders made for moves and copies, to remove on failure
        self.copied: list[Path] = []  # new files copied in, to remove on failure
        self.emptied: list[Path] = []  # old folders emptied file by file
        self.stuck: list[tuple[Path, Path, str]] = []  # moves a restore couldn't undo
        self._copy_start: int | None = None  # self.created from here on are folders the copy made

    # ------------------------------------------------------------ small steps, each retried
    def retry(self, action: Callable[[], None], what: str, tries: int = 40) -> None:
        for i in range(1, tries + 1):
            try:
                action()
                return
            except OSError as exc:
                if i == tries:
                    raise SwapError(f"Could not {what} (in use by another program?): {exc}") from exc
                self.sleep(self.pause)

    def ensure_dir(self, folder: Path) -> None:
        if folder.is_dir():
            return
        self.ensure_dir(folder.parent)
        folder.mkdir()
        self.created.append(folder)

    def move_file(self, src: Path, dst: Path, what: str) -> None:
        self.ensure_dir(dst.parent)
        self.retry(lambda: os.rename(src, dst), f"move {what}")
        self.moves.append((src, dst, "file"))

    def move_contents(self, src: Path, dst: Path, what: str) -> None:
        """Files can move out of a folder that's held open: recreate its folders (empty ones too) and
        move each file. The emptied folders stay where they are."""
        self.ensure_dir(dst)
        for folder, dirs, files in os.walk(src):
            rel = Path(folder).relative_to(src)
            for d in dirs:
                self.ensure_dir(dst / rel / d)
            for f in files:
                self.move_file(Path(folder, f), dst / rel / f, f"{what}\\{rel / f}")

    def move_tree(self, src: Path, dst: Path, what: str) -> None:
        """A folder moves in one step when it can, and file by file when something holds it open."""
        if not dst.exists():
            self.ensure_dir(dst.parent)
            try:
                self.retry(lambda: os.rename(src, dst), f"move {what}", tries=6)
                self.moves.append((src, dst, "dir"))
                return
            except SwapError as exc:
                self.log(f"Moving {what} file by file: {exc}")
        self.move_contents(src, dst, what)
        if self.install in src.parents:
            self.emptied.append(src)

    def move_aside(self, src: Path, dst: Path, what: str) -> None:
        if src.is_dir():
            self.move_tree(src, dst, what)
        elif src.exists():
            self.move_file(src, dst, what)

    def copy_in(self, src: Path, dst: Path) -> None:
        if src.is_dir():
            self.ensure_dir(dst)
            for folder, dirs, files in os.walk(src):
                rel = Path(folder).relative_to(src)
                for d in dirs:
                    self.ensure_dir(dst / rel / d)
                for f in files:
                    self.copy_file(Path(folder, f), dst / rel / f)
        else:
            self.copy_file(src, dst)

    def copy_file(self, src: Path, dst: Path) -> None:
        self.ensure_dir(dst.parent)
        self.retry(lambda: shutil.copy2(src, dst), f"copy the new {dst.relative_to(self.install)}")
        self.copied.append(dst)

    # ------------------------------------------------------------ undo
    def undo(self) -> None:
        """Puts back as much as it can, newest first, and keeps going past anything it can't."""
        for f in reversed(self.copied):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.log(f"Could not remove the new {f}: {exc}")
        self.copied.clear()
        if self._copy_start is not None:  # the copy's own folders go first, so old folders move back in one step
            for d in sorted(self.created[self._copy_start:], key=lambda p: len(str(p)), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
            del self.created[self._copy_start:]
            self._copy_start = None
        keep: set[Path] = set()
        for src, dst, kind in reversed(self.moves):
            try:
                if kind == "file":
                    self.ensure_dir(src.parent)
                    self.retry(lambda s=src, d=dst: os.rename(d, s), f"put back {dst}", tries=120)
                elif not src.exists():
                    self.retry(lambda s=src, d=dst: os.rename(d, s), f"put back {dst}", tries=120)
                else:  # a file-by-file move left folders here: merge it back
                    keep.add(src)
                    for folder, dirs, files in os.walk(dst):
                        back = src / Path(folder).relative_to(dst)
                        back.mkdir(parents=True, exist_ok=True)
                        keep.add(back)
                        for f in files:
                            self.retry(lambda a=Path(folder, f), b=back / f: os.rename(a, b), f"put back {f}",
                                       tries=120)
                    remove_quietly(dst, self.log)
            except (SwapError, OSError) as exc:
                self.log(f"Could not put back {dst}: {exc}")
                self.stuck.append((src, dst, kind))
        self.moves.clear()
        for d in sorted(self.created, key=lambda p: len(str(p)), reverse=True):
            if d in keep:
                continue
            try:
                d.rmdir()  # only while empty
            except OSError:
                pass
        if self.stuck:
            raise SwapError(f"{len(self.stuck)} item(s) could not be put back")

    # ------------------------------------------------------------ the install
    def run(self) -> Path:
        """Move the old app aside and copy the new one in; returns the backup folder. Puts everything
        back (and raises) if that fails."""
        names = list(dict.fromkeys([*APP_ITEMS, *sorted(p.name for p in self.new.iterdir())]))
        if is_complete(self.install):
            for d in old_backups(self.install):
                if not (d / KEEP).exists():
                    remove_quietly(d, self.log)
            remove_quietly(Path(str(self.install) + ".old"), self.log)
        backup = self.install / BACKUP
        if backup.exists():
            backup = self.install / f"{BACKUP}-{time.strftime('%Y%m%d%H%M%S')}"
        try:
            for name in names:
                self.move_aside(self.install / name, backup / name, f"the old {name}")
            hide(backup)
            self._copy_start = len(self.created)
            for item in sorted(self.new.iterdir()):
                self.copy_in(item, self.install / item.name)
            if not is_complete(self.install):
                raise SwapError("The new version is incomplete")
        except Exception as exc:
            why = str(exc) or exc.__class__.__name__
            try:
                self.undo()
                self.log(f"Install failed, previous version restored: {why}")
            except SwapError as again:
                self.write_note(backup)
                self.log(f"RESTORE FAILED: {again}. Recovery instructions: {self.install.parent / NOTE}")
            raise SwapError(why) from exc
        self.log(f"New version installed in {self.install}")
        self.tidy_emptied()
        return backup

    def tidy_emptied(self) -> None:
        """Folders emptied by a file-by-file move that the new version doesn't have."""
        for tree in self.emptied:
            if not tree.is_dir():
                continue
            dirs = [Path(f) for f, _, _ in os.walk(tree)]
            for d in sorted(dirs, key=lambda p: len(str(p)), reverse=True):
                if (self.new / d.relative_to(self.install)).is_dir():
                    continue
                try:
                    d.rmdir()
                except OSError:
                    pass

    def write_note(self, backup: Path) -> None:
        lines = ["ClipMonster's update could not finish, and some of the previous version's files could not be put",
                 "back automatically. Don't delete anything. To recover, move each of these back by hand, or",
                 f"download ClipMonster again and copy it over '{self.install}':", ""]
        lines += [f"  move '{dst}'  back to  '{src}'" for src, dst, _ in self.stuck]
        try:
            (self.install.parent / NOTE).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        except OSError:
            pass
        try:
            if backup.is_dir():
                (backup / KEEP).write_text("", encoding="utf-8")
        except OSError:
            pass


def remove_quietly(folder: Path, log: Callable[[str], None]) -> None:
    if not folder.is_dir():
        return
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        log(f"Left {folder} behind (harmless, removed after the next successful update): {exc}")


def hide(folder: Path) -> None:
    if sys.platform == "win32" and folder.is_dir():
        try:
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(folder), 0x2)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass


def start_app(install: Path, relaunch: str, log: Callable[[str], None], args: tuple[str, ...] = ()) -> None:
    if not relaunch:
        return
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:  # not from the app folder, so nothing pins it during the next update
        subprocess.Popen([str(install / relaunch), *args], cwd=str(Path.home()), creationflags=flags,
                         close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as exc:
        log(f"Could not restart ClipMonster: {exc}")


def keep_previous(backup: Path, previous: Path, version: str, log: Callable[[str], None]) -> bool:
    """Keep the replaced version (the backup folder) as `previous`\\ClipMonster, with its version in
    `previous`\\version.txt, in place of the one kept before. False (and the backup is left for
    removing) when it can't be."""
    if not is_complete(backup):
        return False
    app = previous / "ClipMonster"
    try:
        remove_quietly(previous, log)
        if previous.exists():
            return False
        previous.mkdir(parents=True)
        try:
            os.rename(backup, app)  # the same drive: instant
        except OSError:
            shutil.copytree(backup, app)  # another drive
            remove_quietly(backup, log)
        (previous / "version.txt").write_text(version or "", encoding="utf-8")
        hidden = [p for p in app.iterdir() if p.name.startswith(BACKUP)]
        for p in hidden:
            remove_quietly(p, log)
        log(f"Kept the previous version{' (' + version + ')' if version else ''} in {previous}")
        return True
    except OSError as exc:
        log(f"Could not keep the previous version ({exc}); it's removed instead")
        remove_quietly(previous, log)
        return False


def file_log(path: Path) -> Callable[[str], None]:
    """Logging must never stop an install or a restore: it retries briefly, then gives up."""
    def write(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\r\n"
        for _ in range(5):
            try:
                with open(path, "a", encoding="utf-8", newline="") as f:
                    f.write(line)
                return
            except OSError:
                time.sleep(0.2)
    return write


def apply(new_dir: Path, install: Path, wait_pid: int, relaunch: str, log: Callable[[str], None],
          sleep: Callable[[float], None] = time.sleep, wait: float = 120.0, others: float = 60.0,
          relaunch_args: tuple[str, ...] = (), previous: Path | None = None, old_version: str = "") -> bool:
    """The whole update: wait for the app to exit, swap, keep the replaced version in `previous` (when
    given), restart the app. Logs every outcome in the words updater.last_attempt() reads. True if the
    new version is installed."""
    install = Path(os.path.abspath(install))
    try:
        log(f"Update started: waiting for process {wait_pid} to exit")
        end = time.monotonic() + wait
        while wait_pid and process_alive(wait_pid):
            if time.monotonic() >= end:
                raise SwapError("ClipMonster did not close within 2 minutes")
            sleep(0.5)
        # Anything else running from the app folder (a second window, ytclip.exe, automation) must end too.
        end = time.monotonic() + others
        while app_processes(install) and time.monotonic() < end:
            sleep(1)
        still = app_processes(install)
        if still:
            raise SwapError("Close these first: " + ", ".join(f"{p.name} (process {pid})" for pid, p in still))
        swap = Swap(new_dir, install, log, sleep)
        backup = swap.run()
        if previous is not None:
            keep_previous(backup, previous, old_version, log)
        for d in old_backups(install):
            remove_quietly(d, log)
        try:
            (install.parent / NOTE).unlink(missing_ok=True)
        except OSError:
            pass
        # Done before the restart: the new version removes the unpacked update (updater.tidy), and
        # this helper runs from it.
        log("Update finished")
        start_app(install, relaunch, log, relaunch_args)
        return True
    except Exception as exc:
        log(f"Update failed: {exc}")
        try:  # unless it's open already (reopened while this waited)
            if is_complete(install) and not any(p.name.lower() == EXE_NAME.lower()
                                                for _, p in app_processes(install)):
                start_app(install, relaunch, log, relaunch_args)
        except Exception:
            pass
        return False


def command(new_dir: Path, install: Path, wait_pid: int, log_file: Path, relaunch: str,
            relaunch_args: tuple[str, ...] = (), keep_previous: Path | None = None,
            old_version: str = "") -> list[str]:
    """How updater.launch_swap starts this: the new version's ytclip.exe, or Python from source.
    Only 2.8.0 and later understand --relaunch-arg, --keep-previous and --old-version, so they're only
    given to helpers of those versions (any version an update or going back installs from then on)."""
    if getattr(sys, "frozen", False):
        cmd = [str(new_dir / "ytclip.exe"), "--apply-update"]
    else:
        cmd = [sys.executable, "-m", "ytclip.swap"]
    cmd += ["--wait-pid", str(wait_pid), "--new-dir", str(new_dir), "--install-dir", str(install),
            "--log-file", str(log_file)]
    if relaunch:  # an empty argument would be dropped by some callers and break the parameter list
        cmd += ["--relaunch", relaunch]
        for arg in relaunch_args:
            cmd.append(f"--relaunch-arg={arg}")  # one piece, so an argument starting with - isn't an option
    if keep_previous is not None:
        cmd += ["--keep-previous", str(keep_previous)]
        if old_version:
            cmd += ["--old-version", old_version]
    return cmd


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ytclip --apply-update", description="Install a downloaded ClipMonster update.")
    p.add_argument("--wait-pid", type=int, default=0)
    p.add_argument("--new-dir", type=Path, required=True)
    p.add_argument("--install-dir", type=Path, required=True)
    p.add_argument("--log-file", type=Path, required=True)
    p.add_argument("--relaunch", default="")
    p.add_argument("--relaunch-arg", action="append", default=[], help="an argument for --relaunch (repeatable)")
    p.add_argument("--keep-previous", type=Path, default=None, help="keep the replaced version in this folder")
    p.add_argument("--old-version", default="", help="the replaced version's number, for --keep-previous")
    args = p.parse_args(argv)
    ok = apply(args.new_dir, args.install_dir, args.wait_pid, args.relaunch, file_log(args.log_file),
               relaunch_args=tuple(args.relaunch_arg), previous=args.keep_previous, old_version=args.old_version)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

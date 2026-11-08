"""Launch the ClipMonster desktop app."""

from __future__ import annotations

import os
import subprocess
import sys
import threading


def _prepare_process() -> None:
    from ..settings import app_dir

    # A windowed .exe has no console: libraries that print (yt-dlp, tqdm, Hugging Face)
    # would crash writing to None, so send their output to a log file instead.
    if sys.stdout is None or sys.stderr is None:
        log = open(app_dir() / "clipmonster.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log

    # Child processes (ffmpeg, Node for yt-dlp) would each flash a console window.
    if os.name == "nt":
        original = subprocess.Popen.__init__

        def no_window(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            original(self, *args, **kwargs)

        subprocess.Popen.__init__ = no_window

    if getattr(sys, "frozen", False):
        # Let yt-dlp and friends find the bundled ffmpeg first.
        os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")
        # Don't run with the app folder as the working directory: this process and every
        # ffmpeg it starts would inherit it, and Windows won't let an updater replace a
        # folder that some process is "standing in". Resolve relative file arguments first.
        sys.argv[1:] = [os.path.abspath(a) if os.path.exists(a) else a for a in sys.argv[1:]]
        try:
            os.chdir(os.path.expanduser("~"))
        except OSError:
            pass

    _migrate_files()


def _migrate_files() -> None:
    """Files from older versions (3.2: niches became categories). Never stops the app starting."""
    try:
        from ..categories import migrate_disk

        for line in migrate_disk():
            print(f"ClipMonster 3.2: {line}.", file=sys.stderr)
    except Exception as exc:  # a file it can't read or write: it tries again next start
        print(f"Couldn't update old settings files: {exc}", file=sys.stderr)


def main() -> int:
    _prepare_process()
    if "--automation" in sys.argv[1:]:  # Start with Windows: the runner alone, no window
        from ..pipeline import run_headless

        return run_headless()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from ..settings import APP_NAME, Settings
    from . import qt_fonts, theme

    if os.name == "nt":
        try:  # own taskbar icon instead of python.exe's
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClipMonster.Studio")
        except Exception:
            pass

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    if _hand_over(sys.argv[1:]):  # a notification's link, and the window is already open: it takes it
        return 0
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setWindowIcon(theme.app_icon())
    try:
        appearance = Settings.load().appearance
    except Exception:
        appearance = "dark"
    theme.apply(app, appearance)  # before any window: widgets take the colours as they're built
    from .main_window import MainWindow

    qt_fonts.register()  # bundled fonts, for the font picker and layout sketch
    old_copies = []
    if getattr(sys, "frozen", False):
        try:  # copies unzipped by hand before the installer: offered for removing once the window is up
            from .. import updater
            from ..pipeline import run_entry

            old_copies = updater.old_copies(run_entry())
            updater.refresh_uninstall_entry()
        except Exception:
            pass
        try:  # after an update or a move, Start with Windows starts this copy
            from ..pipeline import refresh_start_with_windows

            refresh_start_with_windows()
        except Exception:
            pass

    # What a crash or a closed window left in the work folder, cleared off the UI thread.
    from ..cleanup import sweep_work

    threading.Thread(target=sweep_work, daemon=True, name="sweep-work").start()
    win = MainWindow()
    win.old_copies = old_copies
    win.show()
    theme.title_bar(int(win.winId()))
    win.link_server = _listen(win)  # kept alive with the window
    try:
        from ..notify import register_links

        register_links()  # clipmonster:// opens this copy (a notification's "Sign in again")
    except Exception:
        pass
    # Anything passed on the command line: a clipmonster:// link, or files dropped onto the .exe to queue.
    if len(sys.argv) > 1:
        win.handle_args(sys.argv[1:])
    return app.exec()


def _server_name() -> str:
    import getpass

    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    return "ClipMonster-" + "".join(c for c in user if c.isalnum())[:40]


def _hand_over(args: list[str]) -> bool:
    """When started with a clipmonster:// link while a window is already open, give it the link and
    stop: True if it was handed over. Anything else starts as it always did."""
    from ..notify import parse_link

    if not any(parse_link(a) for a in args):
        return False
    import json

    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(_server_name())
    if not sock.waitForConnected(800):
        return False
    sock.write(json.dumps(args).encode("utf-8"))
    sock.flush()
    ok = sock.waitForBytesWritten(1500)
    sock.disconnectFromServer()
    return ok


def _listen(win):
    """Take links handed over by a second start (see _hand_over)."""
    import json

    from PySide6.QtNetwork import QLocalServer

    server = QLocalServer(win)
    name = _server_name()
    QLocalServer.removeServer(name)  # one left by a crash
    if not server.listen(name):
        return None

    def take() -> None:
        sock = server.nextPendingConnection()
        if sock is None:
            return

        def read() -> None:
            try:
                args = json.loads(bytes(sock.readAll()).decode("utf-8"))
            except ValueError:
                return
            if isinstance(args, list):
                win.handle_args([str(a) for a in args])

        sock.readyRead.connect(read)
        sock.disconnected.connect(sock.deleteLater)

    server.newConnection.connect(take)
    return server


if __name__ == "__main__":
    sys.exit(main())

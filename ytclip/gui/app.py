"""Launch the ClipMonster desktop app."""

from __future__ import annotations

import os
import subprocess
import sys


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


def _dark_title_bar(hwnd: int) -> None:
    """Match the Windows title bar to the dark theme (Windows 10 20H1+ / 11)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        on = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(on), ctypes.sizeof(on))
    except Exception:
        pass


def main() -> int:
    _prepare_process()
    if "--automation" in sys.argv[1:]:  # Start with Windows: the runner alone, no window
        from ..pipeline import run_headless

        return run_headless()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from ..settings import APP_NAME
    from . import qt_fonts, theme
    from .main_window import MainWindow

    if os.name == "nt":
        try:  # own taskbar icon instead of python.exe's
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClipMonster.Studio")
        except Exception:
            pass

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setWindowIcon(theme.app_icon())
    theme.apply(app)
    qt_fonts.register()  # bundled fonts, for the font picker and layout sketch
    if getattr(sys, "frozen", False):
        try:  # after an update or a move, Start with Windows starts this copy
            from ..pipeline import refresh_start_with_windows

            refresh_start_with_windows()
        except Exception:
            pass

    win = MainWindow()
    win.show()
    _dark_title_bar(int(win.winId()))
    # Anything passed on the command line (e.g. files dropped onto the .exe) gets queued.
    if len(sys.argv) > 1:
        win.add_sources(sys.argv[1:])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

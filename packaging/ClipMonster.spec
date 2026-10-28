# PyInstaller spec: builds dist/ClipMonster/ with ClipMonster.exe (desktop app)
# and ytclip.exe (command line) sharing one set of libraries.
# Build with:  python build.py
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(SPECPATH, "clipmonster.ico")
VERSION_INFO = os.path.join(SPECPATH, "version_info.txt")  # written by build.py: Explorer > Properties > Details
VERSION = VERSION_INFO if os.path.exists(VERSION_INFO) else None

datas = (
    collect_data_files("imageio_ffmpeg", includes=["binaries/*"])  # bundled ffmpeg.exe
    + collect_data_files("yt_dlp_ejs")  # YouTube challenge-solver scripts
    + collect_data_files("faster_whisper")  # Silero VAD model
    + [(os.path.join(ROOT, "ytclip", "fonts"), os.path.join("ytclip", "fonts"))]  # bundled caption/banner fonts
)
binaries = collect_dynamic_libs("ctranslate2")
hiddenimports = (
    collect_submodules("yt_dlp_ejs")
    + ["faster_whisper", "ctranslate2", "anthropic", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets"]
)
excludes = [
    "tkinter", "matplotlib", "IPython", "pytest", "PIL", "scipy", "pandas", "torch",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick3D", "PySide6.Qt3DCore",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtBluetooth",
    "PySide6.QtDesigner", "PySide6.QtSql", "PySide6.QtTest",
]


def analysis(script):
    return Analysis(
        [os.path.join(SPECPATH, script)],
        pathex=[ROOT],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        excludes=excludes,
        noarchive=False,
    )


gui = analysis("launch_gui.py")
cli = analysis("launch_cli.py")

gui_exe = EXE(
    PYZ(gui.pure), gui.scripts, [],
    exclude_binaries=True, name="ClipMonster", icon=ICON, console=False, upx=False, version=VERSION,
)
cli_exe = EXE(
    PYZ(cli.pure), cli.scripts, [],
    exclude_binaries=True, name="ytclip", icon=ICON, console=True, upx=False, version=VERSION,
)
COLLECT(
    gui_exe, gui.binaries, gui.datas,
    cli_exe, cli.binaries, cli.datas,
    name="ClipMonster", upx=False,
)

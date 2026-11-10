# PyInstaller spec: builds dist/ClipMonster/ with ClipMonster.exe (desktop app)
# and ytclip.exe (command line) sharing one set of libraries.
# Build with:  python build.py
#
# Both exes share one copy of the Python code too: it's collected as files in _internal (noarchive)
# instead of an archive inside each exe, which carried the same 30 MB twice. Files the libraries bring
# that ClipMonster never loads are left out (packaging/slim.py).
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(SPECPATH, "clipmonster.ico")
VERSION_INFO = os.path.join(SPECPATH, "version_info.txt")  # written by build.py: Explorer > Properties > Details
VERSION = VERSION_INFO if os.path.exists(VERSION_INFO) else None

datas = (
    collect_data_files("imageio_ffmpeg", includes=["binaries/*"])  # bundled ffmpeg.exe
    + collect_data_files("yt_dlp_ejs")  # YouTube challenge-solver scripts
    + collect_data_files("faster_whisper")  # Silero VAD model
    + collect_data_files("playwright")  # Playwright's Node driver; posting drives your installed Edge/Chrome
    + collect_data_files("cv2", includes=["data/*.xml"])  # OpenCV's face detector (the fallback to YuNet)
    + [(os.path.join(ROOT, "ytclip", "fonts"), os.path.join("ytclip", "fonts"))]  # bundled caption/banner fonts
)
binaries = collect_dynamic_libs("ctranslate2")


def optional(package, data=True, libs=False):
    """The Video Creator's voice packages, when this build has them (pip install -e .[creator,piper])."""
    global datas, binaries
    try:
        __import__(package)
    except Exception:
        return []
    if data:
        datas += collect_data_files(package)
    if libs:
        binaries += collect_dynamic_libs(package)
    return collect_submodules(package)


creator_imports = (
    optional("kokoro_onnx") + optional("espeakng_loader", libs=True) + optional("phonemizer")
    + optional("language_tags") + optional("segments") + optional("csvw")
    + optional("onnxruntime", data=False, libs=True) + optional("soundfile", libs=True)
    + optional("_soundfile_data", libs=True) + optional("piper", libs=True)
)
hiddenimports = (
    collect_submodules("yt_dlp_ejs")
    + collect_submodules("ytclip.intelligence") + collect_submodules("ytclip.vision")
    + collect_submodules("ytclip.audio") + collect_submodules("ytclip.rendering")
    + collect_submodules("ytclip.compliance") + collect_submodules("ytclip.creator") + creator_imports
    + collect_submodules("ytclip.sportsdesk")
    + ["cv2", "faster_whisper", "ctranslate2", "anthropic", "playwright.sync_api", "PySide6.QtNetwork",
       "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets"]
)
excludes = [
    "tkinter", "matplotlib", "IPython", "pytest", "PIL", "scipy", "pandas", "torch",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick3D", "PySide6.Qt3DCore",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtBluetooth",
    "PySide6.QtDesigner", "PySide6.QtSql", "PySide6.QtTest",
]


sys.path.insert(0, SPECPATH)
import slim  # noqa: E402

left_out = {}


def analysis(script):
    a = Analysis(
        [os.path.join(SPECPATH, script)],
        pathex=[ROOT],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        excludes=excludes,
        noarchive=True,  # the code as files in _internal, shared by both exes (one copy instead of two)
    )
    a.binaries = slim.slim(a.binaries, left_out)
    a.datas = slim.slim(a.datas, left_out)
    return a


gui = analysis("launch_gui.py")
cli = analysis("launch_cli.py")
print(slim.summary(left_out))

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

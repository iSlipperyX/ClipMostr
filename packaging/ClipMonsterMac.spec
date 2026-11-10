# PyInstaller spec: builds dist/ClipMonster.app, ClipMonster for Mac.
# Build with:  python packaging/build_mac.py   (on a Mac; it also gathers the tools below, makes the icon and the .dmg)
#
# The whole Studio and Library (ytclip, with the Mac's folders, media engine and Metal), the Your PC pages
# (ytclip.mac), and the programs it runs, from build/mac-tools (CLIPMONSTER_MAC_TOOLS):
#   ffmpeg/ffmpeg              imageio-ffmpeg's, with the Mac's media engine (VideoToolbox) and libass
#   deno/deno                  for YouTube's player challenges (yt-dlp), from the deno package
#   whisper-cpp/whisper-cli    Whisper on the Mac's graphics (packaging/build_whisper_cpp.py --metal)
#   llama-cpp/llama-server     the local AI on them (packaging/build_llama_cpp.py --metal)
# Left out: what only the PC does (posting through a browser, the Creator's voices and pictures) and
# faster-whisper, which whisper.cpp stands in for.
import glob
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, ROOT)
from ytclip import __version__ as VERSION  # noqa: E402
from ytclip.mac import APP_NAME, BUNDLE_ID  # noqa: E402

ICON = os.path.join(SPECPATH, "clipmonster.icns")
SHORT = VERSION.split("+")[0].split("-")[0]
TOOLS = os.environ.get("CLIPMONSTER_MAC_TOOLS") or os.path.join(ROOT, "build", "mac-tools")
PROGRAMS = (("ffmpeg", "ffmpeg"), ("deno", "deno"), ("whisper-cpp", "whisper-cli"), ("llama-cpp", "llama-server"))

binaries = []
datas = (
    collect_data_files("yt_dlp_ejs")  # YouTube challenge-solver scripts
    + collect_data_files("cv2", includes=["data/haarcascade_frontalface_default.xml"])  # the fallback face finder
    + [(os.path.join(ROOT, "ytclip", "fonts"), os.path.join("ytclip", "fonts"))]  # caption and banner fonts
)
for folder, name in PROGRAMS:
    program = os.path.join(TOOLS, folder, name)
    if os.path.isfile(program):
        binaries.append((program, folder))
        datas += [(f, folder) for f in glob.glob(os.path.join(TOOLS, folder, "*.txt"))]  # its version and licence
    else:
        print(f"WARNING: {program} isn't there, so ClipMonster.app is built without it")

hiddenimports = (
    collect_submodules("yt_dlp_ejs")
    + collect_submodules("ytclip.intelligence") + collect_submodules("ytclip.vision")
    + collect_submodules("ytclip.audio") + collect_submodules("ytclip.rendering")
    + collect_submodules("ytclip.compliance") + collect_submodules("ytclip.mac")
    + collect_submodules("ytclip.sportsdesk")
    + ["cv2", "anthropic", "keyring.backends.macOS", "PySide6.QtNetwork", "PySide6.QtMultimedia",
       "PySide6.QtMultimediaWidgets",
       "certifi"]  # HTTPS's certificates (pyinstaller-hooks-contrib's hook brings its cacert.pem; mac/app.py)
)
excludes = [
    "tkinter", "matplotlib", "IPython", "pytest", "PIL", "scipy", "pandas",
    # the PC's alone: posting through a browser, the Creator's voices and AI pictures, the HTTPS server's
    # certificate; and faster-whisper, which whisper.cpp stands in for here
    "playwright", "faster_whisper", "ctranslate2", "onnxruntime", "kokoro_onnx", "piper", "espeakng_loader",
    "phonemizer", "torch", "diffusers", "transformers", "cryptography",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick", "PySide6.QtQml",
    "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
    "PySide6.QtBluetooth", "PySide6.QtDesigner", "PySide6.QtSql", "PySide6.QtTest",
]

a = Analysis(
    [os.path.join(SPECPATH, "launch_mac.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME, console=False, upx=False,
          argv_emulation=False, target_arch=os.environ.get("TARGET_ARCH") or None)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME, upx=False)
app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=ICON if os.path.exists(ICON) else None,
    bundle_identifier=BUNDLE_ID,
    version=SHORT,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": SHORT,
        "CFBundleVersion": VERSION,
        # what PySide6 6.12, numpy and OpenCV's Mac builds need (their wheels are for macOS 14): an older Mac
        # is told so by macOS, instead of the app quitting as it starts
        "LSMinimumSystemVersion": "14.0",
        "LSApplicationCategoryType": "public.app-category.video",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,  # dark mode follows the Mac
        # macOS asks once before the app may look for and talk to the PC on the local network
        "NSLocalNetworkUsageDescription": "ClipMonster finds and connects to ClipMonster on your PC over your "
                                          "local network, to use its AI and make videos on it.",
        "NSHumanReadableCopyright": "ClipMonster",
        # videos dropped on the Dock icon or opened with ClipMonster
        "CFBundleDocumentTypes": [{"CFBundleTypeName": "Video", "CFBundleTypeRole": "Viewer",
                                   "LSHandlerRank": "Alternate", "LSItemContentTypes": ["public.movie"]}],
    },
)

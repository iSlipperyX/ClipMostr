# PyInstaller spec: builds dist/ClipMonster Remote.app, the Mac app that runs ClipMonster on a PC.
# Build with:  python packaging/build_mac.py   (on a Mac; it also makes the icon and the .dmg)
#
# The Mac app is only the window and the connection: ytclip.mac, ClipMonster's look (ytclip.gui's theme,
# icons, sidebar and status bars) and the pairing protocol. None of the clipping, AI or posting code comes
# with it, so it stays small.
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, ROOT)
from ytclip import __version__ as VERSION  # noqa: E402
from ytclip.mac import APP_NAME, BUNDLE_ID  # noqa: E402

ICON = os.path.join(SPECPATH, "clipmonster-remote.icns")
SHORT = VERSION.split("+")[0].split("-")[0]

a = Analysis(
    [os.path.join(SPECPATH, "launch_remote.py")],
    pathex=[ROOT],
    hiddenimports=["ytclip.mac.window", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "keyring.backends.macOS"],
    excludes=[
        "tkinter", "matplotlib", "IPython", "pytest", "PIL", "numpy", "cv2", "yt_dlp", "playwright", "anthropic",
        "faster_whisper", "ctranslate2", "onnxruntime", "cryptography", "rich",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick", "PySide6.QtQml",
        "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
        "PySide6.QtBluetooth", "PySide6.QtDesigner", "PySide6.QtSql", "PySide6.QtTest",
        # ClipMonster's own engine: never needed on the Mac
        "ytclip.pipeline", "ytclip.engine", "ytclip.posting", "ytclip.creator", "ytclip.telegram",
        "ytclip.intelligence", "ytclip.vision", "ytclip.audio", "ytclip.rendering", "ytclip.compliance",
        "ytclip.remote.server", "ytclip.remote.host", "ytclip.remote.certs", "ytclip.remote.state",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ClipMonster Remote", console=False, upx=False,
          argv_emulation=False, target_arch=os.environ.get("TARGET_ARCH") or None)
coll = COLLECT(exe, a.binaries, a.datas, name="ClipMonster Remote", upx=False)
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
        "LSMinimumSystemVersion": "12.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,  # dark mode follows the Mac
        # macOS asks once before the app may look for and talk to the PC on the local network
        "NSLocalNetworkUsageDescription": "ClipMonster Remote finds and connects to ClipMonster on your PC over "
                                          "your local network.",
        "NSHumanReadableCopyright": "ClipMonster",
    },
)

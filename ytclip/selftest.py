"""`ytclip self-test`: load every library ClipMonster needs, the way it uses them, and say what works.

The release workflow runs it on each Windows build before publishing (after "says its version"), so a
file the build left out (packaging/slim.py) or a library that didn't make it in fails there, not on your
PC. In the built app every check must pass; from source, a library that isn't installed is skipped.
Nothing is downloaded and no model is loaded: the voices only turn text into sounds (phonemes), which
reads their language files.
"""

from __future__ import annotations

import importlib
import os
import ssl
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PASS, SKIP, FAIL = "pass", "skip", "fail"
SAY = "In 1971, twenty-four students went into a basement at Stanford."

# The app's own modules that pull in the rest: if these import, so does everything they use.
APP_MODULES = (
    "ytclip.cli", "ytclip.pipeline", "ytclip.engine", "ytclip.weekly", "ytclip.telegram.bot",
    "ytclip.telegram.pair",
    "ytclip.creator.orchestrator", "ytclip.creator.voice_engine", "ytclip.creator.compositor",
    "ytclip.posting.service", "ytclip.posting.tiktok.browser", "ytclip.posting.cover",
    "ytclip.intelligence.llama_engine", "ytclip.vision.face_tracker", "ytclip.updater", "ytclip.swap",
    "ytclip.gui.main_window",
)


class Missing(Exception):
    """A library that isn't installed (skipped from source, a failure in the built app)."""


@dataclass
class Result:
    name: str
    status: str
    detail: str


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _need(module: str):
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        if exc.name in (module, module.split(".")[0]):  # not installed, rather than broken inside
            raise Missing(f"{module} isn't installed") from exc
        raise


def check_code() -> str:
    for name in APP_MODULES:
        importlib.import_module(name)
    import ytclip

    where = Path(ytclip.__file__)
    if frozen() and where.suffix != ".pyc":
        raise RuntimeError(f"ClipMonster's code was loaded from {where}, not from the app's own files")
    return f"{len(APP_MODULES)} modules and what they use, from {where.parent.parent.name}"


def check_python_tls() -> str:
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return f"{ssl.OPENSSL_VERSION}, {len(ctx.get_ca_certs()) or 'system'} certificates"


def _qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([sys.argv[0] if sys.argv else "ytclip"])


def check_qt_window() -> str:
    _need("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    app = _qt_app()
    label = QLabel("ClipMonster")
    label.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)  # laid out and drawn, never seen
    label.resize(200, 40)
    label.show()
    app.processEvents()
    label.close()
    label.deleteLater()
    app.processEvents()
    return f"Qt {__import__('PySide6').__version__} on the {app.platformName()} platform"


def check_qt_tls() -> str:
    _need("PySide6")
    from PySide6.QtNetwork import QSslSocket

    _qt_app()
    backends = QSslSocket.availableBackends()
    if not QSslSocket.supportsSsl():
        raise RuntimeError(f"Qt can't make secure connections (backends: {', '.join(backends) or 'none'})")
    if os.name == "nt" and "schannel" not in backends:
        raise RuntimeError(f"Windows' own TLS (Schannel) isn't there (backends: {', '.join(backends)})")
    return f"{QSslSocket.activeBackend()} (for Research's thumbnails)"


def check_qt_video() -> str:
    _need("PySide6")
    from PySide6.QtMultimedia import QMediaFormat, QMediaPlayer

    _qt_app()
    player = QMediaPlayer()
    try:
        if not player.isAvailable():
            raise RuntimeError("Qt's media player has no backend")
        formats = QMediaFormat().supportedFileFormats(QMediaFormat.ConversionMode.Decode)
        if QMediaFormat.FileFormat.MPEG4 not in formats:
            raise RuntimeError("Qt's media player can't play MP4")
    finally:
        player.deleteLater()
    return "plays MP4 (the preview player)"


def check_opencv() -> str:
    cv2 = _need("cv2")
    path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if cv2.CascadeClassifier(str(path)).empty():
        raise RuntimeError(f"OpenCV's face finder ({path.name}) didn't load")
    if not hasattr(cv2, "FaceDetectorYN"):
        raise RuntimeError("OpenCV has no YuNet face detector")
    return f"OpenCV {cv2.__version__}, face finders ready"


def check_ffmpeg() -> str:
    from .ffmpeg import NO_WINDOW, ffmpeg_exe

    exe = ffmpeg_exe()
    out = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, text=True, timeout=60,
                         creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError(f"{exe} didn't run: {out.stderr.strip()[-200:]}")
    return (out.stdout.splitlines() or ["ffmpeg"])[0].split(" Copyright")[0]


def check_whisper() -> str:
    _need("faster_whisper")
    import ctranslate2

    importlib.import_module("faster_whisper.audio")  # PyAV, which reads the audio
    return f"faster-whisper with CTranslate2 {ctranslate2.__version__}"


def check_onnx() -> str:
    ort = _need("onnxruntime")
    return f"onnxruntime {ort.__version__} ({', '.join(ort.get_available_providers())})"


def check_kokoro() -> str:
    _need("kokoro_onnx")
    from kokoro_onnx.tokenizer import Tokenizer

    tok = Tokenizer()
    for lang in ("en-us", "en-gb"):
        if not tok.phonemize(SAY, lang).strip():
            raise RuntimeError(f"Kokoro's eSpeak gave no sounds for {lang}")
    return "reads American and British English"


def check_piper() -> str:
    _need("piper")
    importlib.import_module("piper.voice")
    from piper.phonemize_espeak import EspeakPhonemizer

    p = EspeakPhonemizer()
    for voice in ("en-us", "en"):
        if not any(p.phonemize(voice, SAY)):
            raise RuntimeError(f"Piper's eSpeak gave no sounds for {voice}")
    return "reads American and British English"


def check_playwright() -> str:
    _need("playwright")
    from playwright._impl._driver import compute_driver_executable

    found = compute_driver_executable()
    node = Path(found[0] if isinstance(found, tuple) else found)
    if not node.is_file():
        raise RuntimeError(f"Playwright's driver isn't at {node}")
    return "driver ready (posting drives your Edge or Chrome)"


def check_bundled_tools() -> str:
    """whisper.cpp and llama.cpp's servers next to the exe (only in the built app; optional in a build)."""
    if not frozen():
        raise Missing("only in the built app")
    here = Path(sys.executable).resolve().parent
    have = [name for name, path in (("whisper.cpp", here / "whisper-cpp" / "whisper-cli.exe"),
                                    ("llama.cpp", here / "llama-cpp" / "llama-server.exe")) if path.is_file()]
    if not have:
        raise Missing("neither whisper.cpp nor llama.cpp is in this build")
    return ", ".join(have)


CHECKS: tuple[tuple[str, Callable[[], str], bool], ...] = (  # (name, check, required in the built app)
    ("ClipMonster's code", check_code, True),
    ("Secure connections", check_python_tls, True),
    ("Window", check_qt_window, True),
    ("Window's secure connections", check_qt_tls, True),
    ("Video preview", check_qt_video, True),
    ("OpenCV", check_opencv, True),
    ("ffmpeg", check_ffmpeg, True),
    ("Whisper", check_whisper, True),
    ("onnxruntime", check_onnx, True),
    ("Kokoro voice", check_kokoro, True),
    ("Piper voice", check_piper, True),
    ("Playwright", check_playwright, True),
    ("Graphics card tools", check_bundled_tools, False),
)


def run(checks=None, strict: bool | None = None) -> list[Result]:
    strict = frozen() if strict is None else strict
    results = []
    for name, check, required in CHECKS if checks is None else checks:
        try:
            results.append(Result(name, PASS, check()))
        except Missing as exc:
            results.append(Result(name, FAIL if strict and required else SKIP, str(exc)))
        except Exception as exc:  # noqa: BLE001 - every failure is reported, then the next check runs
            results.append(Result(name, FAIL, f"{type(exc).__name__}: {exc}"))
    return results


def main(argv: list[str] | None = None, out=print) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="ytclip self-test", description="Load every library ClipMonster needs and "
                                "say what works. The release build runs this before it's published.")
    p.add_argument("--strict", action="store_true", help="a library that isn't installed is a failure (always so "
                   "in the built app)")
    args = p.parse_args(argv)
    if os.name != "nt" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no screen to show the window on
    results = run(strict=True if args.strict else None)
    marks = {PASS: "✓", SKIP: "–", FAIL: "✗"}
    for r in results:
        out(f"{marks[r.status]} {r.name}: {r.detail}")
    failed = [r for r in results if r.status == FAIL]
    out("All good." if not failed else f"{len(failed)} of {len(results)} checks failed.")
    return 1 if failed else 0

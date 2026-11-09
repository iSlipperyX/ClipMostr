"""What the Windows build leaves out (ClipMonster.spec): files that come with the libraries ClipMonster uses
but that it never loads: 1,347 files and 183 MB of the app folder in the 3.5.0 build, whose update zip came to 203 MB
(3.3.0's: 412 MB).

Each rule says why its files are never needed. tests/test_packaging.py checks the rules against the file
names of a real build: that each rule still finds its files, and that everything ClipMonster does load stays.
"""

from __future__ import annotations

import os
import re

# The languages ClipMonster's voices speak. Only their eSpeak dictionaries are kept: a voice reads with its
# own language's dictionary. tests/test_packaging.py checks this against the voice list
# (creator/voice_engine.VOICES), so a voice in a new language can't lose its dictionary unnoticed.
LANGUAGES = ("en",)

# Qt's touch-screen keyboard plugin is the only thing that loads Qt Quick, and Qt Quick is the only thing that
# loads these (checked against the DLLs' import tables in the 3.4.0 build).
QT_QUICK = ("qt6virtualkeyboard", "qt6quick", "qt6qml", "qt6qmlmodels", "qt6qmlmeta", "qt6qmlworkerscript",
            "qt6opengl")

RULES: tuple[tuple[str, str], ...] = (  # (why, pattern for the path inside _internal, lower case with /)
    ("Piper's Hebrew and Arabic models (no voice speaks either)",
     r"piper/(hebrew/nakdimon|tashkeel/model)\.onnx"),
    ("eSpeak dictionaries for languages no voice speaks",
     r"(piper|espeakng_loader)/espeak-ng-data/(?!(" + "|".join(LANGUAGES) + r")_dict$)[^/]+_dict"),
    ("Babel's dates and numbers for languages other than English (only its import is used)",
     r"babel/locale-data/(?!root\.dat$|en(_[^/]*)?\.dat$)[^/]+\.dat"),
    ("OpenCV's own FFmpeg (ClipMonster reads and writes video with ffmpeg.exe)",
     r"cv2/opencv_videoio_ffmpeg[^/]*\.dll"),
    ("OpenCV's other face and body finders (one is used)",
     r"cv2/data/haarcascade_(?!frontalface_default\.xml$)[^/]+\.xml"),
    ("Qt's touch-screen keyboard and the Qt Quick it runs on",
     r"pyside6/(plugins/platforminputcontexts/qtvirtualkeyboardplugin|" + "|".join(QT_QUICK) + r")\.dll"),
    ("Qt's software OpenGL (nothing in ClipMonster draws with OpenGL)", r"pyside6/opengl32sw\.dll"),
    ("Qt's own translations (ClipMonster's window is in English)", r"pyside6/translations/[^/]+\.qm"),
    ("Qt's copy of OpenSSL (Qt uses Windows' own TLS instead; Python keeps its own OpenSSL)",
     r"pyside6/plugins/tls/qopensslbackend\.dll|libssl-3-x64\.dll|libcrypto-3-x64\.dll"),
)
_RULES = tuple((why, re.compile(f"(?:{pattern})$")) for why, pattern in RULES)


def unused(dest: str) -> str:
    """Why `dest` (a file's path inside the app's _internal folder) is left out of the build; "" keeps it."""
    path = dest.replace("\\", "/").lower()
    for why, rule in _RULES:
        if rule.match(path):
            return why
    return ""


def slim(toc: list, left_out: dict | None = None) -> list:
    """PyInstaller's list of files to collect ((dest, source, kind) entries) without the unused ones.
    `left_out` gathers {why: {dest: bytes}} for the build's summary."""
    kept = []
    for entry in toc:
        why = unused(entry[0])
        if not why:
            kept.append(entry)
        elif left_out is not None:
            try:
                size = os.path.getsize(entry[1])
            except (OSError, TypeError):
                size = 0
            left_out.setdefault(why, {})[entry[0]] = size
    return kept


def summary(left_out: dict) -> str:
    """'Left out 812 files ClipMonster never loads (148 MB):' and a line for each rule, biggest first."""
    files = sum(len(d) for d in left_out.values())
    total = sum(sum(d.values()) for d in left_out.values())
    lines = [f"Left out {files} files ClipMonster never loads ({total / 1e6:.0f} MB):"]
    for why, d in sorted(left_out.items(), key=lambda kv: -sum(kv[1].values())):
        lines.append(f"  {sum(d.values()) / 1e6:6.1f} MB  {len(d):4d} file{'s' if len(d) != 1 else ' '}  {why}")
    return "\n".join(lines)

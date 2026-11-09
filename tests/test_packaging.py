"""The Windows build: what it leaves out (packaging/slim.py), that both exes share one copy of the code, and the
self-test the release workflow runs on it (ytclip self-test)."""

import sys
from pathlib import Path

import pytest

from ytclip import selftest
from ytclip.creator.voice_engine import VOICES, kokoro_lang

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
import slim  # noqa: E402

# Real paths from the 3.4.0 build's _internal folder, and the rule that leaves each out.
LEFT_OUT = {
    "piper/hebrew/nakdimon.onnx": "Hebrew and Arabic",
    "piper/tashkeel/model.onnx": "Hebrew and Arabic",
    "piper/espeak-ng-data/ru_dict": "eSpeak",
    "espeakng_loader/espeak-ng-data/cmn_dict": "eSpeak",
    "babel/locale-data/fr.dat": "Babel",
    "babel/locale-data/eo.dat": "Babel",
    "cv2/opencv_videoio_ffmpeg4140_64.dll": "OpenCV's own FFmpeg",
    "cv2/data/haarcascade_eye.xml": "face and body",
    "PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll": "keyboard",
    "PySide6/Qt6VirtualKeyboard.dll": "keyboard",
    "PySide6/Qt6Quick.dll": "keyboard",
    "PySide6/Qt6Qml.dll": "keyboard",
    "PySide6/Qt6QmlModels.dll": "keyboard",
    "PySide6/Qt6OpenGL.dll": "keyboard",
    "PySide6/opengl32sw.dll": "software OpenGL",
    "PySide6/translations/qtbase_de.qm": "translations",
    "PySide6/plugins/tls/qopensslbackend.dll": "OpenSSL",
    "libssl-3-x64.dll": "OpenSSL",
    "libcrypto-3-x64.dll": "OpenSSL",
}

# What ClipMonster does load: none of it may match a rule.
KEPT = [
    "piper/espeak-ng-data/en_dict", "piper/espeak-ng-data/phontab", "piper/espeak-ng-data/phondata",
    "piper/espeak-ng-data/phonindex", "piper/espeak-ng-data/intonations", "piper/espeak-ng-data/lang/gmw/en-US",
    "piper/espeak-ng-data/voices/!v/m3", "piper/espeakbridge.pyd", "piper/tashkeel/hint_id_map.json",
    "piper/hebrew/__init__.pyc", "espeakng_loader/espeak-ng-data/en_dict", "espeakng_loader/espeak-ng.dll",
    "babel/locale-data/en.dat", "babel/locale-data/en_GB.dat", "babel/locale-data/en_US_POSIX.dat",
    "babel/locale-data/root.dat", "babel/global.dat",
    "cv2/cv2.pyd", "cv2/data/haarcascade_frontalface_default.xml", "cv2/data/__init__.pyc",
    "PySide6/Qt6Core.dll", "PySide6/Qt6Gui.dll", "PySide6/Qt6Widgets.dll", "PySide6/Qt6Multimedia.dll",
    "PySide6/Qt6MultimediaWidgets.dll", "PySide6/Qt6Network.dll", "PySide6/Qt6Svg.dll", "PySide6/avcodec-63.dll",
    "PySide6/plugins/platforms/qwindows.dll", "PySide6/plugins/tls/qschannelbackend.dll",
    "PySide6/plugins/tls/qcertonlybackend.dll", "PySide6/plugins/multimedia/ffmpegmediaplugin.dll",
    "PySide6/plugins/imageformats/qjpeg.dll", "PySide6/QtWidgets.pyd",
    "libssl-3.dll", "libcrypto-3.dll", "_ssl.pyd", "_hashlib.pyd", "python314.dll", "base_library.zip",
    "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe", "onnxruntime/capi/onnxruntime.dll",
    "ytclip/fonts/Anton-Regular.ttf", "ytclip/__init__.pyc", "playwright/driver/node.exe",
]


@pytest.mark.parametrize("dest, why", LEFT_OUT.items())
def test_unused_files_are_left_out(dest, why):
    assert why in slim.unused(dest)
    assert why in slim.unused(dest.replace("/", "\\").upper())  # however Windows spells it


@pytest.mark.parametrize("dest", KEPT)
def test_what_clipmonster_loads_is_kept(dest):
    assert slim.unused(dest) == ""


def test_every_voice_keeps_its_dictionary():
    spoken = set()
    for v in VOICES.values():
        spoken |= {v.espeak.split("+")[0].split("-")[0], v.piper.split("_")[0], kokoro_lang(v.kokoro).split("-")[0]}
    assert spoken <= set(slim.LANGUAGES), "a voice speaks a language whose eSpeak dictionary the build leaves out"
    for lang in spoken:
        assert not slim.unused(f"piper/espeak-ng-data/{lang}_dict")
        assert not slim.unused(f"espeakng_loader/espeak-ng-data/{lang}_dict")


def test_kokoro_voices_keep_their_accent():
    assert kokoro_lang("bm_george") == kokoro_lang("bf_emma") == "en-gb"
    assert kokoro_lang("am_onyx") == kokoro_lang("af_heart") == "en-us"


def test_slim_tallies_what_it_left_out(tmp_path):
    big = tmp_path / "opengl32sw.dll"
    big.write_bytes(b"x" * 3_000_000)
    toc = [("PySide6/opengl32sw.dll", str(big), "BINARY"), ("PySide6/Qt6Core.dll", str(big), "BINARY"),
           ("piper/espeak-ng-data/ru_dict", str(tmp_path / "gone"), "DATA")]
    left_out = {}
    assert slim.slim(toc, left_out) == [toc[1]]
    assert slim.slim(toc, left_out) == [toc[1]]  # the second exe's list: counted once
    text = slim.summary(left_out)
    assert text.startswith("Left out 2 files ClipMonster never loads (3 MB):")
    assert "3.0 MB     1 file   Qt's software OpenGL" in text.splitlines()[1]


def test_both_exes_share_the_code_and_slim_down():
    spec = (ROOT / "packaging" / "ClipMonster.spec").read_text(encoding="utf-8")
    assert "noarchive=True" in spec and "noarchive=False" not in spec
    assert "a.binaries = slim.slim(a.binaries, left_out)" in spec and "a.datas = slim.slim(a.datas, left_out)" in spec
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "dist/ClipMonster/ytclip.exe self-test" in workflow


def load_build():
    import importlib.util

    spec = importlib.util.spec_from_file_location("clipmonster_build", ROOT / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_update_zip_is_lzma_and_every_updater_unpacks_it(tmp_path):
    import zipfile

    from ytclip import updater

    app = tmp_path / "dist" / "ClipMonster"
    (app / "_internal" / "ytclip").mkdir(parents=True)
    (app / "ClipMonster.exe").write_bytes(b"MZ" + bytes(range(256)) * 400)
    (app / "ytclip.exe").write_bytes(b"MZ" + b"cli" * 5000)
    (app / "_internal" / "ytclip" / "__init__.pyc").write_bytes(b"\x00" * 9000)
    (app / "_internal" / "empty").mkdir()
    out = load_build().make_zip(app, tmp_path / "dist" / "ClipMonster-windows.zip")
    with zipfile.ZipFile(out) as zf:
        files = [i for i in zf.infolist() if not i.is_dir()]
        assert {i.compress_type for i in files} == {zipfile.ZIP_LZMA}
        assert "ClipMonster/_internal/empty/" in zf.namelist() and "ClipMonster/" in zf.namelist()
    assert not list(out.parent.glob("*.part"))
    target = tmp_path / "Programs" / "ClipMonster"
    target.mkdir(parents=True)
    new_app = updater.extract(out, target)  # the updater every installed copy runs
    for f in app.rglob("*"):
        twin = new_app / f.relative_to(app)
        assert twin.is_dir() if f.is_dir() else twin.read_bytes() == f.read_bytes()


# --- ytclip self-test ------------------------------------------------------------------------------------

def checks(*kinds):
    def make(kind):
        def check():
            if kind == "missing":
                raise selftest.Missing("thing isn't installed")
            if kind == "broken":
                raise OSError("DLL load failed")
            return "fine"
        return check
    return tuple((f"{kind} {i}", make(kind), i != 9) for i, kind in enumerate(kinds))


def test_a_missing_library_is_skipped_from_source_but_fails_the_build():
    lenient = selftest.run(checks("ok", "missing"), strict=False)
    assert [r.status for r in lenient] == [selftest.PASS, selftest.SKIP]
    strict = selftest.run(checks("ok", "missing"), strict=True)
    assert [r.status for r in strict] == [selftest.PASS, selftest.FAIL]
    broken = selftest.run(checks("broken"), strict=False)
    assert broken[0].status == selftest.FAIL and broken[0].detail == "OSError: DLL load failed"


def test_self_test_says_what_failed(monkeypatch):
    monkeypatch.setattr(selftest, "CHECKS", checks("ok", "broken"))
    said = []
    assert selftest.main([], out=said.append) == 1
    assert said == ["✓ ok 0: fine", "✗ broken 1: OSError: DLL load failed", "1 of 2 checks failed."]
    monkeypatch.setattr(selftest, "CHECKS", checks("ok", "missing"))
    said.clear()
    assert selftest.main([], out=said.append) == 0 and said[-1] == "All good."
    assert selftest.main(["--strict"], out=said.append) == 1


def test_every_module_of_the_app_loads():
    pytest.importorskip("ytclip.gui.main_window", exc_type=ImportError)  # no Qt Multimedia libraries (Linux CI)
    assert "modules and what they use" in selftest.check_code()


def test_real_checks_that_need_nothing_extra():
    assert "certificates" in selftest.check_python_tls()
    assert selftest.check_ffmpeg().startswith("ffmpeg version")
    with pytest.raises(selftest.Missing):
        selftest.check_bundled_tools()  # only in the built app

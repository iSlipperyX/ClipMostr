"""ClipMonster for Mac's start: `python -m ytclip.mac`, the `clipmonster-mac` command, or the app bundle
(packaging/launch_mac.py)."""

from __future__ import annotations

import sys
from pathlib import Path

from . import APP_NAME, BUNDLE_ID


def self_check(app) -> int:
    """`--self-check`: the window built, and everything the Mac makes clips with found and run, with the version
    said (the build workflow runs it on each .app before it's published). In the built app every part must be
    there; from source, what isn't installed is said and skipped."""
    import ssl
    import subprocess

    from .. import __version__, bundled
    from ..download import js_runtimes
    from ..ffmpeg import ffmpeg_exe
    from . import keychain
    from .proxy import MediaProxy
    from .window import MacWindow

    def starts(program, *args) -> str | None:
        """What `program args` prints, if it starts and ends well (its libraries are there and its signature
        holds); None if it doesn't."""
        try:
            done = subprocess.run([str(program), *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    frozen = bool(getattr(sys, "frozen", False))
    parts, missing = [], []
    w = MacWindow(auto_connect=False)
    try:
        studio_video = w.preview.player is not None
        pc_video = w.link.videos.detail.player.player is not None
        parts.append("window ok")
        parts.append(f"video {'ok' if studio_video and pc_video else 'MISSING'}")
        if not (studio_video and pc_video):
            missing.append("video")
        proxy = MediaProxy(lambda: None)
        proxy.close()
    finally:
        w.quit()
    try:
        exe = ffmpeg_exe()
    except RuntimeError:
        exe = ""
    said = starts(exe, "-hide_banner", "-encoders") if exe else None
    vt = said is not None and "h264_videotoolbox" in said
    own = not frozen or bundled.find("ffmpeg", "ffmpeg") == Path(exe)  # the app's own, not one on the PATH
    if said is None:
        parts.append("ffmpeg MISSING")
    else:
        parts.append("ffmpeg ok" + (" (VideoToolbox)" if vt else "") + ("" if own else f" (but {exe})"))
    if said is None or not own or (frozen and sys.platform == "darwin" and not vt):
        missing.append("ffmpeg")
    runtimes = js_runtimes()
    if frozen:
        deno = bundled.find("deno", "deno")
        works = deno is not None and starts(deno, "--version") is not None
        parts.append("deno ok" if works else "deno MISSING")
        if not works or runtimes.get("deno", {}).get("path") != str(deno):
            missing.append("deno")
    else:
        parts.append(f"YouTube's JavaScript with {', '.join(runtimes) or 'NOTHING (pip install deno)'}")
    for label, folder, name, ask in (("whisper.cpp", "whisper-cpp", "whisper-cli", "-h"),
                                     ("llama.cpp", "llama-cpp", "llama-server", "--version")):
        if not frozen:  # from source they're found as the PC app finds them, if at all
            continue
        found = bundled.find(folder, name)
        if found is None or starts(found, ask) is None:
            parts.append(f"{label} MISSING")
            missing.append(label)
            continue
        version = bundled.find(folder, "VERSION.txt")  # "whisper.cpp v1.9.5 (Metal)"
        parts.append(f"{version.read_text(encoding='utf-8').strip() if version else label} ok")
    parts.append(f"keys in {keychain.where()}")
    certs = https_certificates()
    parts.append(certs or "HTTPS certificates MISSING")
    if frozen and not certs:
        missing.append("HTTPS certificates")
    parts.append(ssl.OPENSSL_VERSION)
    print(f"{APP_NAME} {__version__}: " + ", ".join(parts))
    return 1 if missing else 0


def use_the_apps_certificates() -> None:
    """In the built app, HTTPS trusts the certificates the app brought (certifi's). Its TLS library is python.org's,
    which looks for them in python.org's Python folder on the Mac the app was built on: on most Macs that isn't
    there, and Whisper's and the local AI's model downloads, Gemini and the update check would all fail."""
    import os

    if not getattr(sys, "frozen", False) or os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
    except ImportError:
        return
    if os.path.isfile(certifi.where()):
        os.environ["SSL_CERT_FILE"] = certifi.where()


def https_certificates() -> str:
    """How many certificates HTTPS trusts and where they're from; '' when there are none to check a site with,
    or (in the built app) they aren't the app's own."""
    import ssl

    from .. import bundled

    try:
        count = len(ssl.create_default_context().get_ca_certs())
    except (OSError, ValueError, ssl.SSLError):
        count = 0
    cafile = ssl.get_default_verify_paths().cafile
    if not getattr(sys, "frozen", False):  # (a folder of them is read only as needed, so none are counted)
        return f"{count} HTTPS certificates" if count else "the system's HTTPS certificates"
    own = [p.resolve() for p in bundled.folders()]
    inside = cafile is not None and any(Path(cafile).resolve().is_relative_to(p) for p in own)
    return f"{count} HTTPS certificates of its own" if count and inside else ""


# Where a Mac keeps the command line tools people install (Homebrew, MacPorts, Deno's and Bun's installers,
# nvm and Volta): an app opened from Finder is given only /usr/bin:/bin:/usr/sbin:/sbin
TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "~/.deno/bin", "~/.bun/bin", "~/.volta/bin",
             "~/.local/bin")


def find_tools(environ=None) -> str:
    """The PATH with the folders above in it (those there), and the newest Node nvm installed: so a Node or
    ffmpeg installed on the Mac is found as in Terminal. Returns the new PATH (and sets it)."""
    import glob
    import os

    environ = os.environ if environ is None else environ
    have = [p for p in environ.get("PATH", "").split(os.pathsep) if p]
    extra = [os.path.expanduser(d) for d in TOOL_DIRS]
    nvm = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/v*/bin")),
                 key=lambda p: [int(x) for x in p.split("/v")[-1].split("/")[0].split(".") if x.isdigit()])
    extra += nvm[-1:]
    environ["PATH"] = os.pathsep.join(have + [d for d in extra if d not in have and os.path.isdir(d)])
    return environ["PATH"]


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    from .. import settings as settings_mod
    from ..gui import theme
    from . import keychain

    args = list(argv if argv is not None else sys.argv)
    if sys.platform == "darwin":
        find_tools()
    if getattr(sys, "frozen", False):
        import os

        try:  # an app opened from Finder starts in /, where nothing can be written
            os.chdir(os.path.expanduser("~"))
        except OSError:
            pass
        use_the_apps_certificates()
    settings_mod.keep_secrets_in(keychain.SettingsSecrets())  # API keys in the Keychain, not settings.json
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName(APP_NAME)
    QCoreApplication.setOrganizationDomain(BUNDLE_ID.rsplit(".", 1)[0])
    app = QApplication.instance() or QApplication(args)
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(theme.app_icon())
    app.setQuitOnLastWindowClosed(False)  # closing the window keeps it running, as Mac apps do (Quit ends it)
    theme.apply(app, settings_mod.Settings.load().appearance)
    if "--self-check" in args:
        return self_check(app)
    from .window import MacWindow

    w = MacWindow()
    w.show()
    app.aboutToQuit.connect(w.shutdown)
    if sys.platform == "darwin":
        # clicking the Dock icon brings the window back after it was closed
        app.applicationStateChanged.connect(lambda s: w.isHidden() and s == Qt.ApplicationActive and w.bring_up())
    return app.exec()

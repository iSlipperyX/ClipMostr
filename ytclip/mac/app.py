"""ClipMonster Remote's start: `python -m ytclip.mac`, or the app bundle (packaging/launch_remote.py)."""

from __future__ import annotations

import sys

from . import APP_NAME, BUNDLE_ID


def self_check(app) -> int:
    """`--self-check`: everything the app loads, loaded, and its version said (the build workflow runs it)."""
    from .. import __version__
    from . import keychain
    from .proxy import MediaProxy
    from .window import MainWindow

    w = MainWindow(auto_connect=False)
    have_video = w.videos.detail.player.player is not None
    proxy = MediaProxy(lambda: None)
    proxy.close()
    import ssl

    w.quit()
    print(f"{APP_NAME} {__version__}: window ok, video {'ok' if have_video else 'MISSING'}, keys in "
          f"{keychain.where()}, {ssl.OPENSSL_VERSION}")
    return 0 if have_video else 1


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    from ..gui import theme
    from . import prefs

    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName("ClipMonster")
    QCoreApplication.setOrganizationDomain(BUNDLE_ID.rsplit(".", 1)[0])
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(theme.app_icon())
    app.setQuitOnLastWindowClosed(False)  # the menu bar icon keeps it running on a Mac
    theme.apply(app, prefs.load().get("appearance", "system"))
    if "--self-check" in (argv if argv is not None else sys.argv):
        return self_check(app)
    from .window import MainWindow

    w = MainWindow()
    w.show()
    app.aboutToQuit.connect(w.close_session)
    if sys.platform == "darwin":
        # Clicking the Dock icon brings the window back after it was closed
        app.applicationStateChanged.connect(lambda s: w.isHidden() and s == Qt.ApplicationActive and w._raise())
    return app.exec()

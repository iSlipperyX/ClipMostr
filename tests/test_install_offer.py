"""The window's offer to install ClipMonster, in a copy unzipped by hand before the installer."""

import time

import pytest

from ytclip import updater


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox, QWidget

    from ytclip.gui import main_window

    class Window(QWidget):
        _offer_install = main_window.MainWindow._offer_install
        INSTALL_ASK_AGAIN = main_window.MainWindow.INSTALL_ASK_AGAIN

        def __init__(self):
            super().__init__()
            self.banner = type("Banner", (), {"ready": "an update"})()
            self.warnings, self.closed, self.asked, self.answer = [], False, 0, ""
            self.setup = tmp_path / "ClipMonster-Setup-2.8.0.exe"

        def _download_setup(self):
            return self.setup

        def _warn(self, msg):
            self.warnings.append(msg)

        def close(self):
            self.closed = True
            return True

    win = Window()

    def answer(box):
        win.asked += 1
        next(b for b in box.buttons() if b.text() == win.answer).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", answer)
    monkeypatch.setattr(updater, "needs_installing", lambda: True)
    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path / "ClipMonster")
    runs = []
    monkeypatch.setattr(updater, "run_setup", lambda setup, stop_from=None: runs.append((setup, stop_from)))
    win.runs = runs
    yield win
    win.deleteLater()


def settle(app):
    from PySide6.QtCore import QCoreApplication

    for _ in range(3):
        QCoreApplication.processEvents()


def test_install_now_runs_the_installer_and_closes(window, app, tmp_path):
    window.answer = "Install now"
    assert window._offer_install()
    settle(app)
    assert window.runs == [(window.setup, tmp_path / "ClipMonster")]
    assert window.closed and window.banner.ready is None


def test_not_now_asks_again_in_a_week(window, monkeypatch):
    window.answer = "Not now"
    assert not window._offer_install()
    assert not window._offer_install() and window.asked == 1
    updater.save_state(install_offer=time.time() - 8 * 86400)
    window.answer = "Keep using this copy"
    assert not window._offer_install() and window.asked == 2
    assert not window._offer_install() and window.asked == 2 and updater.load_state()["install_offer"] == "never"
    assert window.runs == []


def test_nothing_to_offer_in_an_installed_copy(window, monkeypatch):
    monkeypatch.setattr(updater, "needs_installing", lambda: False)
    assert not window._offer_install() and window.asked == 0


def test_a_failed_download_keeps_this_copy_running(window):
    window.answer = "Install now"
    window._download_setup = lambda: None
    assert not window._offer_install()
    assert window.runs == [] and not window.closed


def test_an_installer_that_will_not_start_says_why(window, monkeypatch):
    window.answer = "Install now"

    def broken(setup, stop_from=None):
        raise updater.UpdateError("Couldn't start the installer (Access is denied).")

    monkeypatch.setattr(updater, "run_setup", broken)
    assert not window._offer_install()
    assert window.warnings == ["Couldn't start the installer (Access is denied)."] and not window.closed

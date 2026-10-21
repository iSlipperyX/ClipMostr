"""The 'new version available' bar: check, show release notes, download, verify, restart."""

from __future__ import annotations

import os
import shutil
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QTextBrowser, QVBoxLayout)

from .. import __version__, updater
from ..settings import APP_NAME
from . import theme
from .tasks import Task


class UpdateBanner(QFrame):
    def __init__(self, window):
        super().__init__(window)
        self.win = window  # MainWindow: settings, task queue, status line, close()
        self.release: updater.Release | None = None
        self.task: Task | None = None
        self.setObjectName("updateBanner")
        self.setStyleSheet(f"QFrame#updateBanner {{ background: #1E1838; border: 1px solid {theme.ACCENT}; "
                           f"border-radius: 9px; }}")

        self.text = QLabel()
        self.text.setTextFormat(Qt.RichText)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedWidth(220)
        self.notes_btn = QPushButton("What's new")
        self.install_btn = QPushButton("Update now")
        self.install_btn.setObjectName("primary")
        self.skip_btn = QPushButton("Skip this version")
        self.cancel_btn = QPushButton("Cancel")
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("icon")
        self.close_btn.setToolTip("Remind me later")
        self.notes_btn.clicked.connect(self.show_notes)
        self.install_btn.clicked.connect(self.install)
        self.skip_btn.clicked.connect(self.skip)
        self.cancel_btn.clicked.connect(self.cancel)
        self.close_btn.clicked.connect(self.hide)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 8, 6)
        row.addWidget(self.text, 1)
        for w in (self.bar, self.notes_btn, self.skip_btn, self.install_btn, self.cancel_btn, self.close_btn):
            row.addWidget(w)
        self._mode("idle")
        self.hide()

    def _mode(self, mode: str) -> None:
        offer = mode == "offer"
        busy = mode == "busy"
        self.notes_btn.setVisible(offer)
        self.skip_btn.setVisible(offer)
        self.install_btn.setVisible(offer)
        self.bar.setVisible(busy)
        self.cancel_btn.setVisible(busy)
        self.close_btn.setVisible(not busy)

    def _run(self, task: Task) -> None:
        self.task = task
        task.finished.connect(lambda: setattr(self, "task", None))
        task.log.connect(self.win._log)
        task.start()

    # ------------------------------------------------------------ checking
    def check(self, manual: bool = False) -> None:
        if self.task:
            return
        s = self.win.settings
        repo = updater.configured_repo(s.update_repo)
        if not repo:
            if manual:
                QMessageBox.information(self.win, APP_NAME, "Updates aren't set up yet: this build doesn't know "
                                        "which GitHub repository to follow. Builds made by the GitHub release "
                                        "workflow set it automatically, or enter owner/repo in Settings → Updates.")
            return
        task = Task("Checking for updates", lambda t: updater.check(repo, s.include_prereleases))
        task.succeeded.connect(lambda rel: self._checked(rel, manual))
        task.failed.connect(lambda msg: self._check_failed(msg, manual))
        self._run(task)

    def _checked(self, rel: updater.Release | None, manual: bool) -> None:
        updater.save_state(last_check=time.time())
        if rel is None:
            if manual:
                QMessageBox.information(self.win, APP_NAME,
                                        f"You're up to date: ClipMonster {__version__} is the latest version.")
            return
        if not manual and updater.load_state().get("skipped") == rel.version:
            return
        self.release = rel
        beta = " (beta)" if rel.prerelease else ""
        self.text.setText(f"✨ <b>ClipMonster {rel.version}{beta}</b> is available. You have {__version__}.")
        self._mode("offer")
        self.show()

    def _check_failed(self, msg: str, manual: bool) -> None:
        self.win._log(f"Update check failed: {msg}")
        if manual:
            QMessageBox.warning(self.win, APP_NAME, f"Couldn't check for updates:\n\n{msg}")

    # ------------------------------------------------------------ actions
    def show_notes(self) -> None:
        rel = self.release
        if not rel:
            return
        dlg = QDialog(self.win)
        dlg.setWindowTitle(f"What's new in ClipMonster {rel.version}")
        dlg.resize(620, 480)
        lay = QVBoxLayout(dlg)
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setMarkdown(rel.notes or "_No release notes were published for this version._")
        lay.addWidget(view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        if rel.page_url:
            gh = buttons.addButton("Open on GitHub", QDialogButtonBox.ActionRole)
            gh.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(rel.page_url)))
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        dlg.exec()

    def skip(self) -> None:
        if self.release:
            updater.save_state(skipped=self.release.version)
            self.win._notice(f"Skipped version {self.release.version}. You'll be told about the next one.")
        self.hide()

    def cancel(self) -> None:
        if self.task:
            self.task.cancel_requested = True

    def install(self) -> None:
        rel = self.release
        if not rel or self.task:
            return
        target = updater.install_dir()
        if target is None:
            box = QMessageBox(QMessageBox.Information, APP_NAME,
                              "You're running ClipMonster from source code, so it can't replace itself. "
                              "Pull the latest code with git, or download the packaged release.", parent=self.win)
            open_btn = box.addButton("Open release page", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Close)
            box.exec()
            if box.clickedButton() is open_btn and rel.page_url:
                QDesktopServices.openUrl(QUrl(rel.page_url))
            return
        busy = len(self.win.tasks.pending) + (1 if self.win.tasks.busy() else 0)
        if busy and QMessageBox.question(
                self.win, APP_NAME, f"ClipMonster is still working on {busy} task(s). Updating restarts the app "
                "and stops them. Your queue and clip edits are saved. Continue?") != QMessageBox.Yes:
            return

        def blockers() -> None:
            others = updater.other_instances(target)
            if others:
                raise updater.UpdateError("Close the other ClipMonster windows or ytclip commands running from "
                                          f"this folder first: {', '.join(others)}.")

        def run(t: Task):
            blockers()
            zip_path = updater.download(rel, lambda f: t.progress.emit("Downloading", f),
                                        lambda: t.cancel_requested)
            t.progress.emit("Verifying and unpacking", -1.0)
            new_app = updater.extract(zip_path, target, lambda: t.cancel_requested)
            if t.cancel_requested:  # pressed during the last moments of unpacking
                shutil.rmtree(new_app.parent, ignore_errors=True)
                raise updater.UpdateCancelled()
            blockers()  # something may have started while we downloaded
            return new_app

        task = Task(f"Updating to {rel.version}", run)
        task.progress.connect(self._progress)
        task.succeeded.connect(lambda new_app: self._apply(rel, new_app, target))
        task.failed.connect(self._install_failed)
        self.text.setText(f"Downloading ClipMonster {rel.version}…")
        self._mode("busy")
        self._run(task)

    def _progress(self, msg: str, frac: float) -> None:
        if frac < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))
        mb = f" ({frac * (self.release.asset_size or 0) / 1e6:.0f} of {(self.release.asset_size or 0) / 1e6:.0f} MB)" \
            if frac >= 0 and self.release and self.release.asset_size else ""
        self.text.setText(f"{msg} ClipMonster {self.release.version if self.release else ''}…{mb}")

    def _apply(self, rel: updater.Release, new_app, target) -> None:
        self.text.setText(f"✓ Verified ClipMonster {rel.version}. Restarting to finish the update…")
        self._mode("idle")
        self.close_btn.hide()
        updater.save_state(pending=rel.version)
        try:
            updater.launch_swap(new_app, target, os.getpid())
        except OSError as exc:
            self._install_failed(f"Couldn't start the installer: {exc}")
            return
        self.win._log(f"Installing {rel.version}; the app restarts when the swap finishes ({updater.update_log()})")
        QTimer.singleShot(400, self.win.close)

    def _install_failed(self, msg: str) -> None:
        cancelled = "UpdateCancelled" in msg
        colour = theme.MUTED if cancelled else theme.BAD
        text = "Update cancelled." if cancelled else f"Update failed: {msg}"
        self.text.setText(f"<span style='color:{colour}'>{text}</span>")
        self._mode("offer")
        self.install_btn.setText("Try again")
        if not cancelled:
            self.win._log(f"Update failed: {msg}")

    def shutdown(self) -> None:
        """Called when the window closes: stop a running check/download cleanly."""
        task = self.task
        if task and task.isRunning():
            task.cancel_requested = True
            if not task.wait(5000):
                task.terminate()
                task.wait(1000)

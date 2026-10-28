"""The 'new version' bar: check the update channel, download in the background, restart to install,
and go back to the previous version.

With Get updates by themselves on (the default), a new version downloads quietly and the bar only
appears once it's ready: one click restarts into it. Closing the window installs it too, unless
automation is running in the background, which installs it itself between videos (pipeline.py).
"""

from __future__ import annotations

import os
import shutil
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QTextBrowser, QVBoxLayout)

from .. import __version__, updater
from ..settings import APP_NAME, UPDATE_CHANNELS
from . import theme
from .tasks import Task

CHANNEL_NAMES = {k: v.split(" (")[0] for k, v in UPDATE_CHANNELS}
RECHECK_MS = 30 * 60 * 1000  # how often an open window sees whether its channel is due a check


class UpdateBanner(QFrame):
    def __init__(self, window):
        super().__init__(window)
        self.win = window  # MainWindow: settings, task queue, status line, close()
        self.release: updater.Release | None = None
        self.task: Task | None = None
        self.ready = None  # the unpacked new version, when it downloaded by itself
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

        self.timer = QTimer(self, interval=RECHECK_MS)
        self.timer.timeout.connect(self.check_if_due)
        self.timer.start()

    def _mode(self, mode: str) -> None:
        offer = mode in ("offer", "ready")
        busy = mode == "busy"
        self.notes_btn.setVisible(offer and self.release is not None)
        self.skip_btn.setVisible(mode == "offer")
        self.install_btn.setVisible(offer)
        self.bar.setVisible(busy)
        self.cancel_btn.setVisible(busy)
        self.close_btn.setVisible(not busy)

    def _run(self, task: Task) -> None:
        self.task = task
        task.finished.connect(lambda: setattr(self, "task", None))
        task.log.connect(self.win._log)
        task.start()

    @property
    def channel(self) -> str:
        return updater.channel_setting(self.win.settings)

    # ------------------------------------------------------------ checking
    def check_if_due(self) -> None:
        """Every half hour: the channel's own check interval (Dev is checked most often)."""
        s = self.win.settings
        if s.check_updates and updater.install_dir() and not self.task and self.ready is None \
                and updater.due_for_check(updater.CHECK_HOURS[self.channel]):
            self.check(manual=False)

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
        channel = self.channel
        task = Task("Checking for updates", lambda t: updater.check(repo, channel))
        task.succeeded.connect(lambda rel: self._checked(rel, manual))
        task.failed.connect(lambda msg: self._check_failed(msg, manual))
        self._run(task)

    def _checked(self, rel: updater.Release | None, manual: bool) -> None:
        updater.save_state(last_check=time.time())
        if rel is None:
            if manual:
                QMessageBox.information(self.win, APP_NAME, f"You're up to date: ClipMonster {__version__} is the "
                                        f"newest version on the {CHANNEL_NAMES[self.channel]} channel.")
            return
        if not manual and updater.load_state().get("skipped") == rel.version:
            return
        self.release = rel
        if self.win.settings.auto_update and not rel.switch and updater.install_dir() and not manual:
            self._download_quietly(rel)
            return
        self._offer(rel)

    def _offer(self, rel: updater.Release) -> None:
        if rel.switch:
            self.text.setText(f"You're on {__version__}, a {updater.channel_of(__version__)} version. Switch to "
                              f"<b>ClipMonster {rel.label}</b>, the newest on the {CHANNEL_NAMES[self.channel]} "
                              "channel you picked?")
            self.install_btn.setText("Switch now")
        else:
            self.text.setText(f"✨ <b>ClipMonster {rel.label}</b> is available. You have {__version__}.")
            self.install_btn.setText("Update now")
        self._mode("offer")
        self.show()

    def _check_failed(self, msg: str, manual: bool) -> None:
        self.win._log(f"Update check failed: {msg}")
        if manual:
            QMessageBox.warning(self.win, APP_NAME, f"Couldn't check for updates:\n\n{msg}")

    # ------------------------------------------------------------ in the background
    def _download_quietly(self, rel: updater.Release) -> None:
        target = updater.install_dir()
        task = Task(f"Downloading ClipMonster {rel.version}",
                    lambda t: updater.prepare(rel, target, lambda f: t.progress.emit("Downloading", f),
                                              lambda: t.cancel_requested))
        task.succeeded.connect(lambda new_app: self._ready(rel, new_app))
        task.failed.connect(lambda msg: (self.win._log(f"Couldn't download {rel.version} by itself: {msg}"),
                                         self._offer(rel)))
        self._run(task)

    def _ready(self, rel: updater.Release, new_app) -> None:
        self.ready = new_app
        self.text.setText(f"✨ <b>ClipMonster {rel.label}</b> is downloaded and ready. Restart to start using it "
                          "(or it's installed when you close ClipMonster).")
        self.install_btn.setText("Restart now")
        self._mode("ready")
        self.show()

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

    def _confirm_restart(self, what: str) -> bool:
        busy = len(self.win.tasks.pending) + (1 if self.win.tasks.busy() else 0)
        return not busy or QMessageBox.question(
            self.win, APP_NAME, f"ClipMonster is still working on {busy} task(s). {what} restarts the app "
            "and stops them. Your queue and clip edits are saved. Continue?") == QMessageBox.Yes

    def _source_build(self, page_url: str = "") -> bool:
        """Running from source: nothing to replace. Says so; True when that's the case."""
        if updater.install_dir() is not None:
            return False
        box = QMessageBox(QMessageBox.Information, APP_NAME,
                          "You're running ClipMonster from source code, so it can't replace itself. "
                          "Pull the latest code with git, or download the packaged release.", parent=self.win)
        open_btn = box.addButton("Open release page", QMessageBox.ActionRole) if page_url else None
        box.addButton(QMessageBox.Close)
        box.exec()
        if open_btn is not None and box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(page_url))
        return True

    def _install_task(self, title: str, version: str, get_new_app) -> None:
        """Stop automation, check nothing else runs from the app folder, get the new app folder
        (get_new_app(task, target)), then restart into it."""
        target = updater.install_dir()

        def blockers() -> None:
            others = updater.other_instances(target)
            if others:
                raise updater.UpdateError("Close the other ClipMonster windows or ytclip commands running from "
                                          f"this folder first: {', '.join(others)}.")

        def run(t: Task):
            t.progress.emit("Pausing automation", -1.0)
            if not updater.stop_automation():
                raise updater.UpdateError(updater.AUTOMATION_BUSY)
            blockers()
            new_app = get_new_app(t, target)
            if t.cancel_requested:  # pressed during the last moments of unpacking
                shutil.rmtree(updater.staging_dir(), ignore_errors=True)
                updater.save_state(staged="")
                raise updater.UpdateCancelled()
            blockers()  # something may have started while we downloaded
            return new_app

        task = Task(title, run)
        task.progress.connect(self._progress)
        task.succeeded.connect(lambda new_app: self._apply(version, new_app, target))
        task.failed.connect(self._install_failed)
        self.text.setText(f"{title}…")
        self._mode("busy")
        self._run(task)

    def install(self) -> None:
        rel = self.release
        if not rel or self.task or self._source_build(rel.page_url):
            return
        if not self._confirm_restart("Updating"):
            return
        self._install_task(f"Updating to {rel.version}", rel.version,
                           lambda t, target: updater.prepare(rel, target, lambda f: t.progress.emit("Downloading", f),
                                                             lambda: t.cancel_requested))

    def go_back(self) -> None:
        """Settings → Updates → Go back: install the version kept from before the last update."""
        version = updater.previous_version()
        if not version or self.task or self._source_build():
            return
        if QMessageBox.question(
                self.win, APP_NAME, f"Go back to ClipMonster {version}? ClipMonster restarts, and {__version__} "
                "is kept so you can come back to it the same way. You won't be offered "
                f"{__version__} again, only versions after it.") != QMessageBox.Yes:
            return
        if not self._confirm_restart("Going back"):
            return
        self.release = None
        updater.save_state(skipped=__version__)
        self._install_task(f"Going back to {version}", version,
                           lambda t, target: updater.prepare_previous(target))
        self.show()

    def _progress(self, msg: str, frac: float) -> None:
        if frac < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))
        size = self.release.asset_size if self.release else 0
        mb = f" ({frac * size / 1e6:.0f} of {size / 1e6:.0f} MB)" if frac >= 0 and size else ""
        self.text.setText(f"{msg} ClipMonster {self.release.version if self.release else ''}…{mb}")

    def _apply(self, version: str, new_app, target) -> None:
        self.text.setText(f"✓ ClipMonster {version} is ready. Restarting to finish…")
        self._mode("idle")
        self.close_btn.hide()
        updater.save_state(pending=version)
        try:
            updater.launch_swap(new_app, target, os.getpid())
        except OSError as exc:
            self._install_failed(f"Couldn't start the installer: {exc}")
            return
        self.ready = None
        self.win._log(f"Installing {version}; the app restarts when that finishes ({updater.update_log()})")
        QTimer.singleShot(400, self.win.close)

    def _install_failed(self, msg: str) -> None:
        cancelled = "UpdateCancelled" in msg
        colour = theme.MUTED if cancelled else theme.BAD
        text = "Update cancelled." if cancelled else f"Update failed: {msg}"
        self.text.setText(f"<span style='color:{colour}'>{text}</span>")
        self._mode("offer" if self.release else "idle")
        self.install_btn.setText("Try again")
        if not cancelled:
            self.win._log(f"Update failed: {msg}")

    def install_on_exit(self) -> bool:
        """The window is closing with a downloaded update: install it now, with no restart. Left alone
        while automation runs in the background: it installs the update itself between videos (it
        would otherwise have to be stopped, which takes up to a minute and a half). True if started."""
        from ..jobs import JobDB
        from ..pipeline import runner_alive

        rel, new_app, target = self.release, self.ready, updater.install_dir()
        if rel is None or new_app is None or target is None or self.task or updater.setup_running():
            return False  # (ClipMonster-Setup.exe closing the window installs a version itself)
        try:
            db = JobDB()
            try:
                if runner_alive(db):
                    return False
            finally:
                db.close()
            if updater.other_instances(target):
                return False
            updater.save_state(pending=rel.version)
            updater.launch_swap(new_app, target, os.getpid(), relaunch="")
        except Exception as exc:  # closing must never fail
            self.win._log(f"Couldn't install {rel.version} on closing: {exc}")
            return False
        return True

    def shutdown(self) -> None:
        """Called when the window closes: stop a running check/download cleanly, or install a
        downloaded update."""
        self.timer.stop()
        task = self.task
        if task and task.isRunning():
            task.cancel_requested = True
            if not task.wait(5000):
                task.terminate()
                task.wait(1000)
            return
        self.install_on_exit()

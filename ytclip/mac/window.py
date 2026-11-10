"""ClipMonster for Mac's window: the Studio and the Library (ytclip/gui/main_window.py with only its Create
pages: MAC), and under Your PC in the sidebar the PC it's linked to (pc.py).

What the Mac adds to the PC app's window:
* "Make on: This Mac | Your PC" beside the paste box, once a PC is linked: videos added with Your PC picked go to
  the PC to be made start to finish (pc_jobs.py), and show in the Studio's list as they get on there, until their
  clips land in this Mac's Library.
* Your PC's local AI as one of the AIs (pc_ai.py, provider "pc" in ai.py).
* The Mac's menus (the Studio's ⌘E, ⌘O, ⌘V and ⌘, are in them), a menu bar icon, notifications, and closing like
  a Mac app: the window goes, ClipMonster carries on (clips still export, the PC's news still comes) until Quit.
* A check for a newer ClipMonster for Mac (.dmg), instead of the PC's own updater.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QUrl
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox,
                               QPushButton, QWidget)

from .. import __version__, ai
from ..download import is_url, needs_expansion
from ..gui import theme
from ..gui.main_window import MainWindow as StudioWindow
from ..gui.main_window import _label
from ..gui.tasks import Task
from . import APP_NAME, common as c
from . import hosts, prefs
from .pc import PCLink
from .pc_ai import PCAI
from .pc_jobs import ACTIVE, FINISHED, PCJobs

REPO = "iSlipperyX/ClipMostr"
RELEASES = f"https://github.com/{REPO}/releases/latest"
MENU_KEYS = ("Ctrl+E", "Ctrl+O", "Ctrl+V", "Ctrl+,")  # the Studio's own shortcuts that move into the menus
MAC_SHORTCUTS = [
    ("Space", "Play / pause"), ("← / →", "Seek 5 s (⇧: 1 s)"), ("J / L", "Previous / next clip"),
    ("P", "Play selected clip"), ("I / O", "Set clip start / end at playhead"), ("N", "New clip at playhead"),
    ("⌫", "Delete selected clip"), ("V", "Show the export view"), ("⌘E", "Export clips"),
    ("⌘O", "Open video files"), ("⌘V", "Add links from the clipboard"), ("⌘1 – ⌘9", "Go to a page"),
    ("⌘R", "Refresh your PC's pages"), ("⌘,", "Settings"), ("Scroll on timeline", "Zoom (⇧: pan)"),
]
PC_TIP = ("Your PC downloads the video (or gets it from this Mac), finds the clips and exports them with this Mac's "
          "settings, on its graphics card and with its local AI. The clips come back to this Mac's Library.")


def _version_key(v: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", (v or "").split("+")[0].split("-")[0])[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def dmg_name() -> str:
    """The download for this Mac: Apple silicon or Intel."""
    import platform

    return "ClipMonster-mac-intel.dmg" if platform.machine() == "x86_64" else "ClipMonster-mac.dmg"


class _QuitWatch(QObject):
    """What macOS asks of the app as a whole. ⌘Q from the Dock, logging out or shutting down: ClipMonster really
    quits (the window would otherwise only hide), after asking if it's still making something. A video dropped
    on the Dock icon, or opened with ClipMonster in Finder: it's added, as if dropped on the window."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win

    def eventFilter(self, obj, ev):  # noqa: N802
        if ev.type() == QEvent.Quit and not self.win._quitting:
            if not self.win._may_quit():
                ev.ignore()
                return True
            self.win._quitting = True
            if not self.win.isVisible():
                # Qt closes only the windows on screen. One put away with its red button still has to stop
                # what it's making and save the session, or an export dies half-written as the app ends.
                self.win.closeEvent(QCloseEvent())
        elif ev.type() == QEvent.FileOpen:
            path = ev.file() or ev.url().toLocalFile()
            if path:
                self.win.bring_up()
                self.win.add_sources([path])
            return True
        return False


class MacWindow(StudioWindow):
    MAC = True

    def __init__(self, auto_connect: bool = True):
        self._quitting = False
        self._update_note = ""
        self._pc_projects: dict = {}  # a PC video's key -> its row's Project
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1080, 700)
        self.banner.timer.stop()  # the PC app's own updater: a Mac checks for a newer .dmg instead
        self.library.reopen.disconnect()
        self.library.reopen.connect(lambda src: (self.add_sources([src], here=True), self.tabs.setCurrentIndex(0)))

        self.pc_ai = PCAI(self.link)
        ai.use_remote(self.pc_ai)
        self.pc_jobs = PCJobs(self.link, lambda: self.settings, parent=self)
        self.pc_jobs.added.connect(self._pc_add_row)
        self.pc_jobs.changed.connect(self._pc_changed)
        self.pc_jobs.removed.connect(self._pc_removed)
        self.pc_jobs.landed.connect(self._pc_landed)
        self.pc_jobs.failed.connect(self._pc_failed)
        for job in self.pc_jobs.ordered():
            self._pc_add_row(job["key"])

        self.link.changed.connect(self._link_changed)
        self.tabs.currentChanged.connect(self._page_changed)
        self._menus()
        self._quit_watch = _QuitWatch(self)
        QApplication.instance().installEventFilter(self._quit_watch)
        if prefs.load().get("make_on") == "pc":
            self.on_pc.setChecked(True)
        self._page_changed(self.tabs.currentIndex())
        last = hosts.last() if auto_connect else None
        if last is not None:
            self.link.open_host(last)
        self._link_changed()

    # ================================================================== building on the PC app's window

    def _build_sidebar(self, sidebar) -> QWidget:
        brand_row = QHBoxLayout()
        brand_row.setSpacing(9)
        logo = QLabel()
        logo.setPixmap(theme.app_icon_pixmap(56).scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        brand_row.addWidget(logo)
        brand_row.addWidget(_label(APP_NAME, "brand"))
        brand_row.addStretch(1)
        sidebar.top.addLayout(brand_row)
        self.version_label = _label(f"Version {__version__}", "version")
        self.version_label.setWordWrap(True)
        self.version_label.setOpenExternalLinks(True)
        sidebar.bottom.addWidget(self.version_label)
        return sidebar

    def _build_more(self, tabs) -> None:
        """Your PC's pages under the Studio and Library, its bar over them, and Make on beside the paste box."""
        self.link = PCLink(self)
        self.link.add_pages(tabs)
        self.link.bar.hide()
        self.outer.addWidget(self.link.bar)

        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        lab = QLabel("Make on")
        lab.setProperty("muted", True)
        seg = QFrame()
        seg.setObjectName("segbar")
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(3, 3, 3, 3)
        sh.setSpacing(2)
        self.on_mac = QPushButton("This Mac")
        self.on_pc = QPushButton("Your PC")
        self.on_mac.setToolTip("This Mac downloads the video, finds the clips and exports them")
        group = QButtonGroup(box)
        for b in (self.on_mac, self.on_pc):
            b.setObjectName("seg")
            b.setCheckable(True)
            group.addButton(b)
            sh.addWidget(b)
        self.on_mac.setChecked(True)
        self.on_pc.toggled.connect(self._make_on_changed)
        h.addWidget(lab)
        h.addWidget(seg)
        self.make_on = box
        box.hide()
        head = self.header.layout()
        head.insertSpacing(3, 10)
        head.insertWidget(4, box)

    def _shortcuts(self) -> None:
        super()._shortcuts()
        self._studio_keys = []
        for sc in self.findChildren(QShortcut):
            if sc.key().toString() in MENU_KEYS:  # in the menus instead (a key in both would do neither)
                sc.setEnabled(False)
                sc.setParent(None)
                sc.deleteLater()
            else:
                self._studio_keys.append(sc)

    def _menus(self) -> None:
        mb = self.menuBar()

        def act(menu, text, keys, fn, role=None):
            a = QAction(text, self)
            if keys:
                a.setShortcut(QKeySequence(keys))
            a.triggered.connect(lambda _=False: fn())
            if role is not None:
                a.setMenuRole(role)
            menu.addAction(a)
            return a

        file = mb.addMenu("File")
        act(file, "Open Videos…", "Ctrl+O", self._open_files)
        act(file, "Add Links from the Clipboard", "Ctrl+V", self._paste)
        file.addSeparator()
        act(file, "Export Clips", "Ctrl+E", self.export_current)
        act(file, "Export All Ready Videos", "Ctrl+Shift+E", self.export_all)
        act(file, "Open Output Folder", "Ctrl+Shift+O", self._open_output)
        file.addSeparator()
        act(file, "Clean Up…", "", self._clean_up)
        act(file, "Settings…", "Ctrl+,", self._open_settings, QAction.PreferencesRole)
        act(file, f"About {APP_NAME}", "", self._show_help, QAction.AboutRole)
        act(file, f"Quit {APP_NAME}", "Ctrl+Q", self.quit, QAction.QuitRole)

        view = mb.addMenu("View")
        look = view.addMenu("Appearance")
        group = QActionGroup(self)
        for key, label in (("system", "Match the Mac"), ("dark", "Dark"), ("light", "Light")):
            a = QAction(label, self)
            a.setCheckable(True)
            a.setChecked(self.settings.appearance == key)
            a.triggered.connect(lambda _=False, k=key: self._appearance(k))
            group.addAction(a)
            look.addAction(a)
        self.notify_action = QAction("Notifications", self)
        self.notify_action.setCheckable(True)
        self.notify_action.setChecked(bool(prefs.load().get("notify", True)))
        self.notify_action.toggled.connect(lambda on: prefs.set("notify", on))
        view.addAction(self.notify_action)

        go = mb.addMenu("Go")
        pages = [("Studio", 0), ("Library", 1)] + [(self.tabs.tabText(i), i) for i in self.link.pc_rows]
        for n, (label, index) in enumerate(pages):
            act(go, label, f"Ctrl+{n + 1}" if n < 9 else "", lambda i=index: self._go(i))

        pc = mb.addMenu("Your PC")
        act(pc, "Link Your PC…", "", lambda: self._go(self.link.i_link))
        act(pc, "Refresh", "Ctrl+R", self.link.refresh)
        self.pause_action = act(pc, "Pause Automation", "Ctrl+Shift+P", self.link.toggle_pause)
        pc.addSeparator()
        act(pc, "Switch to Another PC…", "", self.link.switch_pc)
        act(pc, "Unlink This Mac…", "", self.link.unlink)
        self.pc_menu = pc

        help_ = mb.addMenu("Help")
        act(help_, "Keyboard Shortcuts", "", self._show_help)
        act(help_, "Check for Updates…", "", lambda: self._check_mac_update(manual=True))
        act(help_, "ClipMonster on GitHub", "", lambda: QDesktopServices.openUrl(QUrl(f"https://github.com/{REPO}")))

    def _go(self, index: int) -> None:
        if self.link.owns(index) and self.tabs.is_hidden(index):  # a PC page before a PC is linked
            index = self.link.i_link
        self.tabs.setCurrentIndex(index)

    def _appearance(self, key: str) -> None:
        self._apply_settings(self.settings.copy(appearance=key))
        QMessageBox.information(self, "Appearance", f"The new look shows the next time {APP_NAME} opens.")

    def _page_changed(self, index: int) -> None:
        on_pc = self.link.owns(index)
        self.header.setVisible(not on_pc)  # the paste box and presets go with the Studio and Library
        self.link.page_changed(index)
        for sc in getattr(self, "_studio_keys", []):  # Space, J, L… belong to the Studio's player
            sc.setEnabled(index == 0)

    # ================================================================== the window the PC's pages talk to

    def say(self, text: str, good: bool = False, bad: bool = False) -> None:
        colour = theme.GOOD if good else theme.BAD if bad else theme.MUTED
        mark = "✓ " if good else "✗ " if bad else ""
        if bad:
            self._log(f"✗ {text}")
        self._notice(f"<span style='color:{colour}'>{c.esc(mark + text)}</span>")

    def bring_up(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def pc_status(self, st: dict) -> None:
        host = st.get("host") or {}
        self.pause_action.setText("Resume Automation" if self.link._paused else "Pause Automation")
        self._footer(str(host.get("version") or ""))
        self._update_make_on()

    def _footer(self, pc_version: str = "") -> None:
        note = f"Version {__version__}"
        if pc_version and _version_key(pc_version)[:2] != _version_key(__version__)[:2]:
            note += (f"<br><span style='color:{theme.WARN}'>Your PC runs {c.esc(pc_version)}: update the older "
                     "one</span>")
        if self._update_note:
            note += "<br>" + self._update_note
        self.version_label.setText(note)

    def _link_changed(self) -> None:
        self._update_make_on()
        if not self.link.linked:
            self._footer()

    # ================================================================== Make on: This Mac | Your PC

    def _on_pc(self) -> bool:
        return self.on_pc.isChecked() and self.link.linked

    def _make_on_changed(self, on_pc: bool) -> None:
        prefs.set("make_on", "pc" if on_pc else "mac")
        self._update_make_on()

    def _update_make_on(self) -> None:
        linked = self.link.linked
        self.make_on.setVisible(linked)
        why = self.pc_jobs.can_send() if linked else ""
        self.on_pc.setToolTip(PC_TIP + (f"\n\nNow: {why}" if why else ""))
        on_pc = self._on_pc()
        self.input.setPlaceholderText(
            "Paste YouTube links, playlists or channels, or drop videos: your PC makes them" if on_pc else
            "Paste YouTube links, playlists or channels (several at once is fine) — or drop video files anywhere")

    def add_sources(self, sources: list[str], here: bool = False) -> None:
        """Videos to make: on this Mac (the Studio, as on the PC), or with Your PC picked, on the PC."""
        if here or not self._on_pc():
            return super().add_sources(sources)
        sources = [s.strip().strip('"') for s in sources if s.strip()]
        sources = [s for s in sources if is_url(s) or Path(s).exists()]  # (a folder is read for its videos)
        if not sources:
            return
        why = self.pc_jobs.can_send()
        if why:
            which = "this video" if len(sources) == 1 else "these videos"
            box = QMessageBox(QMessageBox.Question, APP_NAME, f"{why}\n\nMake {which} on this Mac instead?",
                              QMessageBox.Cancel, self)
            mac = box.addButton("Make on this Mac", QMessageBox.AcceptRole)
            box.setDefaultButton(mac)
            box.exec()
            if box.clickedButton() is mac:
                super().add_sources(sources)
            return
        expand = [s for s in sources if needs_expansion(s)]
        direct = [s for s in sources if s not in expand]
        if direct:
            self._send_to_pc(direct)
        if expand:
            from ..download import expand_sources

            s = self.settings.copy()
            task = Task("Reading playlists", lambda t: expand_sources(expand, s.playlist_limit or None,
                                                                      s.cookies_browser or None, t.log.emit))
            task.succeeded.connect(self._send_to_pc)
            self._wire(task)
            self.tasks.add(task)

    def _send_to_pc(self, sources: list[str]) -> None:
        keys = self.pc_jobs.send(list(sources))
        if keys:
            self._notice(f"Sent {c.plural(len(keys), 'video')} to {c.esc(self.link.name or 'your PC')} to make. "
                         "The clips come back to your Library.")
            self.queue.setCurrentRow(self.projects.index(self._pc_projects[keys[0]]))

    # ================================================================== the PC's videos in the Studio's list

    def _row_job(self, key: str) -> dict:
        job = dict(self.pc_jobs.get(key) or {})
        note = self.pc_jobs.waiting_for(job)
        if note:
            job["note"] = note
        return job

    def _pc_add_row(self, key: str) -> None:
        from ..engine import Project

        job = self.pc_jobs.get(key)
        if job is None or key in self._pc_projects:
            return
        p = Project(job["source"])
        p.status = "remote"
        p.remote = self._row_job(key)  # type: ignore[attr-defined]
        p.pc_key = key  # type: ignore[attr-defined]
        self._pc_projects[key] = p
        self._list_project(p)

    def _pc_changed(self, key: str) -> None:
        p = self._pc_projects.get(key)
        if p is None:
            return
        p.remote = self._row_job(key)  # type: ignore[attr-defined]
        self._refresh_item(p)
        row = self.queue.currentRow()
        if 0 <= row < len(self.projects) and self.projects[row] is p:
            self._show_pc_video(p)

    def _pc_removed(self, key: str) -> None:
        p = self._pc_projects.pop(key, None)
        if p is not None and p in self.projects:
            StudioWindow._remove(self, p)

    def _pc_landed(self, key: str) -> None:
        job = self.pc_jobs.get(key) or {}
        self.library.refresh()
        n, title = int(job.get("clips") or 0), job.get("title") or job.get("name") or "your video"
        url = QUrl.fromLocalFile(job.get("here") or str(self.settings.output_path)).toString()
        self._log(f"✓ {title}: {n} clips made on your PC are in your Library")
        self._notice(f"✓ {c.plural(n, 'clip')} from “{c.esc(c.trim(title, 50))}” came over from your PC — "
                     f"<a style='color:{theme.ACCENT_2}' href='{url}'>open folder</a>")
        if not self.isActiveWindow():
            self.link.notify("Clips ready", f"{c.plural(n, 'clip')} from “{c.trim(title, 80)}”, made on your PC")

    def _pc_failed(self, key: str) -> None:
        job = self.pc_jobs.get(key) or {}
        title = job.get("title") or job.get("name") or "a video"
        self._warn(f"Your PC couldn't make “{c.trim(title, 50)}”: {job.get('error') or 'it stopped'}")

    @staticmethod
    def _pc_words(job: dict) -> str:
        """What a PC video's row says when it's opened."""
        st = job.get("status")
        where = job.get("host_name") or "your PC"
        if job.get("note"):
            return job["note"]
        if st == "waiting":
            return f"Waiting to go to {where}"
        if st == "sending":
            return f"Going to {where}"
        if st == "queued":
            n = int(job.get("waiting") or 0)
            return f"Waiting its turn on {where}" + (f" ({n} ahead)" if n else "")
        if st == "working":
            return f"Being made on {where} · {job.get('stage') or 'starting'}"
        if st == "fetching":
            return f"Made on {where}: its clips are coming over"
        if st == "done":
            return (f"Made on {where}: {c.plural(int(job.get('clips') or 0), 'clip')}, in your Library "
                    "(right-click for its folder)")
        if st == "failed":
            return f"{where} couldn't make it: {job.get('error') or 'it stopped'}"
        return "Stopped"

    def _show_pc_video(self, p) -> None:
        job = p.remote
        name = job.get("title") or job.get("name") or p.source
        self.video_title.setText(self.video_title.fontMetrics().elidedText(name, Qt.ElideRight, 520))
        self.video_meta.setText("   " + self._pc_words(job))
        self.video_meta.setToolTip(job.get("error") or "")

    def load_project(self, p) -> None:
        if p is not None and getattr(p, "remote", None) is not None:
            super().load_project(None)  # nothing to edit here: it's made on the PC
            self._show_pc_video(p)
            return
        super().load_project(p)

    def _queue_menu(self, pos) -> None:
        item = self.queue.itemAt(pos)
        if not item:
            return
        p = self.projects[self.queue.row(item)]
        key = getattr(p, "pc_key", "")
        job = self.pc_jobs.get(key) if key else None
        if job is None:
            return super()._queue_menu(pos)
        st = job["status"]
        menu = QMenu(self)
        if st == "done" and job.get("here"):
            menu.addAction("Open clips folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(job["here"])))
            menu.addAction("Show in Library", lambda: self.tabs.setCurrentWidget(self.library))
        if st in ("waiting", "sending", "queued", "working"):
            menu.addAction("Stop", lambda: self.pc_jobs.stop(key))
        if st in ("failed", "cancelled"):
            again = menu.addAction("Send to your PC again", lambda: self.pc_jobs.send_again(key))
            again.setEnabled(not self.pc_jobs.can_send())
        source = job["source"]
        if st in FINISHED and (is_url(source) or Path(source).is_file()):
            menu.addAction("Open in the Studio on this Mac" if st == "done" else "Make it on this Mac instead",
                           lambda: self.add_sources([source], here=True))
        if is_url(source):
            menu.addAction("Open on YouTube", lambda: QDesktopServices.openUrl(QUrl(source)))
        elif Path(source).is_file():
            from ..gui.library import reveal

            menu.addAction("Show source video", lambda: reveal(Path(source)))
        menu.addSeparator()
        menu.addAction("Remove", lambda: self._remove(p))
        menu.exec(self.queue.mapToGlobal(pos))

    def _remove(self, p) -> None:
        key = getattr(p, "pc_key", "")
        job = self.pc_jobs.get(key) if key else None
        if job is None:
            return super()._remove(p)
        if job["status"] in ("sending", "queued", "working"):
            box = QMessageBox(QMessageBox.Question, APP_NAME, "Stop making this video on your PC and take it off "
                              "the list?", QMessageBox.Cancel, self)
            stop = box.addButton("Stop and remove", QMessageBox.DestructiveRole)
            box.exec()
            if box.clickedButton() is not stop:
                return
        elif job["status"] == "fetching":
            box = QMessageBox(QMessageBox.Question, APP_NAME, "This video's clips are still coming over from your "
                              "PC. Removing it now deletes the ones not here yet from your PC.",
                              QMessageBox.Cancel, self)
            go = box.addButton("Remove", QMessageBox.DestructiveRole)
            box.exec()
            if box.clickedButton() is not go:
                return
        self.pc_jobs.remove(key)  # (its row goes with removed)

    def _clear_finished(self) -> None:
        for key in list(self._pc_projects):
            job = self.pc_jobs.get(key)
            if job is not None and job["status"] in FINISHED:
                self.pc_jobs.remove(key)
        super()._clear_finished()

    def _project_ready(self, p) -> None:
        super()._project_ready(p)
        if p.status == "done" and not self.isActiveWindow():
            self.link.notify("Clips ready", f"{c.plural(len(p.clips), 'clip')} from “{c.trim(p.name, 80)}”")

    # ================================================================== first start, help and updates

    def _startup_updates(self) -> None:
        if not self.settings.setup_done:
            self._open_setup()
        if prefs.load().get("check_updates", True):
            self._check_mac_update(manual=False)

    def _open_setup(self) -> None:
        from .welcome import Welcome

        dlg = Welcome(self.settings, self.link, self)
        if dlg.exec():
            self._apply_settings(dlg.result_settings)
            if dlg.link_pc:
                self._go(self.link.i_link)
        else:
            self._apply_settings(self.settings.copy(setup_done=True))

    def _show_help(self) -> None:
        from ..settings import app_dir

        rows = "".join(f"<tr><td style='padding:3px 14px 3px 0'><b>{k}</b></td><td>{v}</td></tr>"
                       for k, v in MAC_SHORTCUTS)
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        box.setText(f"""
            <h3>{APP_NAME} {__version__} for Mac</h3>
            <p>Paste links or drop videos, let the analysis find the best moments, fine-tune them on the
            timeline, then export clips with captions in any format. Link your PC to use its AI and graphics
            card, and to run its automation from here.</p>
            <p><b>Clip colours:</b> purple = signals, cyan = AI, green = edited by you.</p>
            <table>{rows}</table>
            <p style='color:{theme.MUTED}'>Settings, presets and your session live in {app_dir()}</p>""")
        check = box.addButton("Check for Updates", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is check:
            self._check_mac_update(manual=True)

    def _check_updates_now(self) -> None:
        self._check_mac_update(manual=True)

    def _check_mac_update(self, manual: bool = False) -> None:
        """A newer ClipMonster for Mac on GitHub (its .dmg for this Mac): a link under the version, or a box."""
        want = dmg_name()

        def work():
            req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                         headers={"User-Agent": "ClipMonster-Mac", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)
            tag = str(data.get("tag_name") or "").lstrip("v")
            url = next((a.get("browser_download_url") for a in data.get("assets") or [] if a.get("name") == want), "")
            return tag, url

        def ok(found):
            tag, url = found
            if url and _version_key(tag) > _version_key(__version__):
                self._update_note = (f"<a href='{url}' style='color:{theme.ACCENT_2}'>{APP_NAME} {c.esc(tag)} is out: "
                                     "download</a>")
                self._footer(str(((self.link.status or {}).get("host") or {}).get("version") or ""))
                if manual:
                    box = QMessageBox(QMessageBox.Information, APP_NAME,
                                      f"{APP_NAME} {tag} is out (you have {__version__}). Download it, open it and "
                                      f"drag {APP_NAME} to Applications to replace this one: your settings, linked "
                                      "PC and clips stay.", QMessageBox.Close, self)
                    get = box.addButton("Download", QMessageBox.AcceptRole)
                    box.exec()
                    if box.clickedButton() is get:
                        QDesktopServices.openUrl(QUrl(url))
            elif manual:
                QMessageBox.information(self, APP_NAME, f"You have the newest {APP_NAME} for Mac ({__version__}).")

        def fail(exc):
            if manual:
                QMessageBox.warning(self, APP_NAME, f"Couldn't check for updates ({exc}).")

        self.link.tasks.run(work, ok, fail)

    # ================================================================== closing

    def _busy(self) -> str:
        """What quitting would stop, in words ("" when nothing)."""
        if self.tasks.current is not None or self.tasks.pending:
            return "ClipMonster is still making clips on this Mac."
        if any(j["status"] == "sending" for j in self.pc_jobs.active()):
            return "A video is still going to your PC."
        return ""

    def _may_quit(self) -> bool:
        why = self._busy()
        if not why:
            return True
        box = QMessageBox(QMessageBox.Question, f"Quit {APP_NAME}?", f"{why} Quitting stops it.",
                          QMessageBox.Cancel, self)
        go = box.addButton("Quit", QMessageBox.DestructiveRole)
        box.exec()
        return box.clickedButton() is go

    def quit(self) -> None:
        if not self._quitting and not self._may_quit():
            return
        self._quitting = True
        self.close()

    def shutdown(self) -> None:
        if getattr(self, "_shut", False):
            return
        self._shut = True
        self.pc_jobs.shutdown()
        self.link.shutdown()
        if ai.remote() is self.pc_ai:
            ai.use_remote(None)
        QApplication.instance().removeEventFilter(self._quit_watch)

    def closeEvent(self, e) -> None:  # noqa: N802
        if sys.platform == "darwin" and not self._quitting:
            # like other Mac apps: the window goes, ClipMonster carries on (clips still export, the PC's news still
            # comes) until Quit; the Dock icon or the menu bar icon brings it back
            self.hide()
            e.ignore()
            return
        self.shutdown()
        super().closeEvent(e)
        QApplication.instance().quit()


MainWindow = MacWindow  # (the name the Mac app's tests and self-check know it by)

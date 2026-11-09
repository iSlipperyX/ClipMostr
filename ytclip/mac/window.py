"""The Mac app's window: Connect until a PC is connected, then the sidebar and its pages, a top bar with the
PC's name and Pause, and a line at the bottom for what just happened. A menu bar icon shows how many videos wait
for your OK, and macOS notifications say when one does and when something needs you."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
                               QPushButton, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from .. import __version__
from ..gui import theme
from ..gui.navigation import NavTabs
from . import APP_NAME, common as c
from . import hosts, prefs
from .connect import ConnectPage
from .dashboard import Dashboard
from .inbox import InboxPage
from .pages import AskPage, CategoriesPage, LogPage, PostedPage, RequestsPage, WeekPage
from .proxy import MediaProxy
from .session import Session, Tasks
from .videos import VideosPage

RELEASES = "https://github.com/iSlipperyX/ClipMostr/releases/latest"
LATEST_API = "https://api.github.com/repos/iSlipperyX/ClipMostr/releases/latest"


def _version_key(v: str) -> tuple:
    import re

    nums = [int(x) for x in re.findall(r"\d+", (v or "").split("+")[0].split("-")[0])[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


class MainWindow(QMainWindow):
    def __init__(self, auto_connect: bool = True):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(theme.app_icon())
        self.resize(1280, 820)
        self.setMinimumSize(980, 640)
        self.tasks = Tasks(parent=self)
        self.session: Session | None = None
        self.proxy: MediaProxy | None = None
        self._review_seen: set[str] | None = None
        self._paused = False
        self._quitting = False

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.connect_page = ConnectPage(self.tasks)
        self.connect_page.connect_to.connect(self.open_host)
        self.stack.addWidget(self.connect_page)

        main = QWidget()
        main.setObjectName("page")
        mh = QHBoxLayout(main)
        mh.setContentsMargins(0, 0, 0, 0)
        mh.setSpacing(0)
        self.nav = NavTabs()
        side = self.nav.sidebar
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(theme.app_icon_pixmap(64).scaled(26, 26, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        name = QLabel("ClipMonster")
        name.setObjectName("brand")
        brand.addWidget(logo)
        brand.addWidget(name)
        brand.addStretch(1)
        side.top.addLayout(brand)
        self.footer = QLabel(f"Remote {__version__}")
        self.footer.setObjectName("version")
        self.footer.setWordWrap(True)
        self.footer.setOpenExternalLinks(True)
        side.bottom.addWidget(self.footer)
        mh.addWidget(side)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("toolbar")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(20, 10, 16, 10)
        bh.setSpacing(10)
        self.dot = c.Dot()
        self.pc_label = QLabel("")
        self.pc_label.setStyleSheet("font-weight: 600;")
        self.pc_state = QLabel("")
        self.pc_state.setProperty("muted", True)
        bh.addWidget(self.dot)
        bh.addWidget(self.pc_label)
        bh.addWidget(self.pc_state, 1)
        self.pause_btn = QPushButton("❚❚  Pause")
        self.pause_btn.setToolTip("Nothing new is clipped or made until you resume; posting carries on")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setObjectName("icon")
        self.refresh_btn.setToolTip("Refresh (⌘R)")
        self.refresh_btn.clicked.connect(self.refresh)
        self.pc_btn = QPushButton("PC")
        pc_menu = QMenu(self)
        pc_menu.addAction("Switch to another PC…", self.switch_pc)
        pc_menu.addAction("Unlink this Mac…", self.unlink)
        self.pc_btn.setMenu(pc_menu)
        bh.addWidget(self.pause_btn)
        bh.addWidget(self.refresh_btn)
        bh.addWidget(self.pc_btn)
        right.addWidget(bar)
        hair = QFrame()
        hair.setObjectName("hairline")
        right.addWidget(hair)

        self.offline = QFrame()
        self.offline.setObjectName("banner")
        oh = QHBoxLayout(self.offline)
        oh.setContentsMargins(14, 8, 10, 8)
        self.offline_text = c.WrapLabel("")
        retry = QPushButton("Try again")
        retry.clicked.connect(self.refresh)
        other = QPushButton("Another PC…")
        other.clicked.connect(self.switch_pc)
        oh.addWidget(self.offline_text, 1)
        oh.addWidget(retry)
        oh.addWidget(other)
        self.offline.hide()
        wrap = QVBoxLayout()
        wrap.setContentsMargins(16, 10, 16, 0)
        wrap.addWidget(self.offline)
        right.addLayout(wrap)
        right.addWidget(self.nav, 1)

        sb = QWidget()
        sb.setObjectName("statusbar")
        sh = QHBoxLayout(sb)
        sh.setContentsMargins(20, 6, 20, 8)
        self.notice_label = QLabel("")
        self.notice_label.setTextFormat(Qt.RichText)
        sh.addWidget(self.notice_label, 1)
        right.addWidget(sb)
        mh.addLayout(right, 1)
        self.stack.addWidget(main)
        self.main = main
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(lambda: self.notice_label.setText(""))

        # pages
        self.dashboard = Dashboard()
        self.videos = VideosPage(self)
        self.ask = AskPage(self)
        self.requests = RequestsPage(self)
        self.posted = PostedPage(self)
        self.inbox = InboxPage(self)
        self.categories = CategoriesPage(self)
        self.week = WeekPage(self)
        self.log = LogPage(self)
        self.nav.section("Overview")
        self.i_dash = self.nav.addTab(self.dashboard, "Overview", "automation")
        self.nav.section("Videos")
        self.i_videos = self.nav.addTab(self.videos, "Waiting to post", "queue")
        self.i_posted = self.nav.addTab(self.posted, "Posted", "performance")
        self.i_inbox = self.nav.addTab(self.inbox, "Sports clips", "library")
        self.nav.section("Ask")
        self.i_ask = self.nav.addTab(self.ask, "New video", "creator")
        self.i_requests = self.nav.addTab(self.requests, "Requests", "library")
        self.nav.section("Automation")
        self.i_categories = self.nav.addTab(self.categories, "Categories", "categories")
        self.i_week = self.nav.addTab(self.week, "This week", "research")
        self.i_log = self.nav.addTab(self.log, "Log", "studio")
        self.nav.currentChanged.connect(self._page_changed)
        self.dashboard.open_videos.connect(self._open_videos)
        self.dashboard.open_page.connect(lambda name: self.nav.setCurrentIndex(
            {"posted": self.i_posted, "requests": self.i_requests, "categories": self.i_categories}[name]))
        self.videos.counts.connect(self._review_count)
        self.inbox.counts.connect(self._inbox_count)

        self._menus()
        self._tray()
        self._check_update()
        last = hosts.last() if auto_connect else None
        if last is not None:
            self.open_host(last)
        else:
            self.stack.setCurrentWidget(self.connect_page)

    # --- menus and the menu bar icon -------------------------------------------------------------------------

    def _menus(self) -> None:
        mb = self.menuBar()
        pc = mb.addMenu("PC")
        a = QAction("Refresh", self)
        a.setShortcut(QKeySequence.Refresh if sys.platform != "darwin" else QKeySequence("Ctrl+R"))
        a.triggered.connect(self.refresh)
        pc.addAction(a)
        self.pause_action = QAction("Pause automation", self)
        self.pause_action.setShortcut(QKeySequence("Ctrl+Shift+P"))
        self.pause_action.triggered.connect(self.toggle_pause)
        pc.addAction(self.pause_action)
        pc.addSeparator()
        pc.addAction("Switch to another PC…", self.switch_pc)
        pc.addAction("Unlink this Mac…", self.unlink)
        go = mb.addMenu("Go")
        for i, (label, key) in enumerate((("Overview", "1"), ("Waiting to post", "2"), ("Posted", "3"),
                                          ("Sports clips", "4"), ("New video", "5"), ("Requests", "6"),
                                          ("Categories", "7"), ("This week", "8"), ("Log", "9"))):
            act = QAction(label, self)
            act.setShortcut(QKeySequence(f"Ctrl+{key}"))
            act.triggered.connect(lambda _=False, i=i: self._go(i))
            go.addAction(act)
        view = mb.addMenu("View")
        look = view.addMenu("Appearance")
        cur = prefs.load().get("appearance", "system")
        for key, label in (("system", "Match the Mac"), ("dark", "Dark"), ("light", "Light")):
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(cur == key)
            act.triggered.connect(lambda _=False, k=key: self._appearance(k))
            look.addAction(act)
        self.notify_action = QAction("Notifications", self)
        self.notify_action.setCheckable(True)
        self.notify_action.setChecked(bool(prefs.load().get("notify", True)))
        self.notify_action.toggled.connect(lambda on: prefs.set("notify", on))
        view.addAction(self.notify_action)
        help_ = mb.addMenu("Help")
        help_.addAction("ClipMonster on GitHub", lambda: QDesktopServices.openUrl(QUrl(RELEASES)))

    def _go(self, i: int) -> None:
        if self.session is not None:
            self.nav.setCurrentIndex(i)

    def _appearance(self, key: str) -> None:
        prefs.set("appearance", key)
        QMessageBox.information(self, "Appearance", "The new look shows the next time ClipMonster Remote opens.")

    def _tray(self) -> None:
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(theme.app_icon(), self)
        menu = QMenu()
        self.tray_status = menu.addAction("Not connected")
        self.tray_status.setEnabled(False)
        self.tray_review = menu.addAction("")
        self.tray_review.triggered.connect(lambda: (self._raise(), self._open_videos("review")))
        self.tray_review.setVisible(False)
        self.tray_inbox = menu.addAction("")
        self.tray_inbox.triggered.connect(lambda: (self._raise(), self.nav.setCurrentIndex(self.i_inbox)))
        self.tray_inbox.setVisible(False)
        menu.addSeparator()
        self.tray_pause = menu.addAction("Pause automation", self.toggle_pause)
        menu.addAction(f"Open {APP_NAME}", self._raise)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(APP_NAME)
        self.tray.activated.connect(lambda reason: self._raise() if reason == QSystemTrayIcon.Trigger
                                    and sys.platform != "darwin" else None)
        self.tray.show()

    def _raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def notify(self, title: str, text: str) -> None:
        if self.tray is not None and self.notify_action.isChecked():
            self.tray.showMessage(title, text, theme.app_icon(), 8000)

    # --- connecting ---------------------------------------------------------------------------------------------

    def open_host(self, host: hosts.Host) -> None:
        self.close_session()
        if not host.token():
            self.connect_page.say(f"This Mac's key for {c.esc(host.name)} is gone: link it again.")
            hosts.forget(host.id)
            self.stack.setCurrentWidget(self.connect_page)
            return
        self.session = Session(host, self.tasks, self)
        self.session.status.connect(self._status)
        self.session.videos.connect(self._videos)
        self.session.deliveries.connect(self.inbox.show_deliveries)
        self.session.event.connect(self._event)
        self.session.connection.connect(self._connection)
        self.proxy = MediaProxy(lambda: self.session.client if self.session is not None else None)
        self.pc_label.setText(host.name)
        self.pc_state.setText("Connecting…")
        self.dot.set(theme.MUTED)
        self.stack.setCurrentWidget(self.main)
        self.nav.setCurrentIndex(self.i_dash)
        self._review_seen = None
        self.inbox.connected()
        self.session.start()

    def close_session(self) -> None:
        self.videos.stop()
        self.inbox.disconnect_()
        if self.session is not None:
            self.session.stop()
            self.session.deleteLater()
            self.session = None
        if self.proxy is not None:
            self.proxy.close()
            self.proxy = None

    def switch_pc(self) -> None:
        self.close_session()
        self.connect_page.say("")
        self.stack.setCurrentWidget(self.connect_page)

    def unlink(self) -> None:
        s = self.session
        if s is None:
            return
        ask = QMessageBox(QMessageBox.Warning, "Unlink this Mac?",
                          f"This Mac won't be able to use ClipMonster on {s.host.name} until you link it again with a "
                          "new code.", QMessageBox.Cancel, self)
        go = ask.addButton("Unlink", QMessageBox.DestructiveRole)
        ask.exec()
        if ask.clickedButton() is not go:
            return
        host = s.host

        def done(*_a):
            hosts.forget(host.id)
            self.switch_pc()
            self.connect_page.say(f"Unlinked from {c.esc(host.name)}.", theme.MUTED)

        s.call(lambda cl: cl.post("/api/unlink"), done, done)

    def _connection(self, state: str, text: str) -> None:
        name = self.session.host.name if self.session else "the PC"
        if state == "online":
            self.offline.hide()
            self.dot.set(theme.WARN if self._paused else theme.GOOD)
            return
        if state in ("unlinked", "wrong_pc"):
            host = self.session.host if self.session else None
            self.close_session()
            if host is not None and state == "unlinked":
                hosts.forget(host.id)
            self.connect_page.say(c.esc(text))
            self.stack.setCurrentWidget(self.connect_page)
            return
        self.dot.set(theme.BAD)
        self.pc_state.setText("Not reachable")
        self.offline_text.setText(f"<b>Can't reach {c.esc(name)}.</b> {c.esc(text)} Trying again by itself.")
        self.offline.show()
        if self.tray is not None:
            self.tray_status.setText(f"{name}: not reachable")

    # --- what the PC says ----------------------------------------------------------------------------------------

    def _status(self, st: dict) -> None:
        auto = st.get("automation") or {}
        self._paused = bool(auto.get("paused"))
        host = st.get("host") or {}
        name = host.get("name") or (self.session.host.name if self.session else "")
        self.pc_label.setText(name)
        job = st.get("job")
        if self._paused:
            text = "Automation paused · posting carries on"
        elif job:
            text = ("Making " if job.get("making") else "Clipping ") + f"“{c.trim(job.get('title'), 60)}”"
        else:
            text = c.trim(auto.get("state") or "Running", 80)
        self.pc_state.setText(text)
        self.dot.set(theme.WARN if self._paused else theme.GOOD)
        self.pause_btn.setText("▶  Resume" if self._paused else "❚❚  Pause")
        self.pause_action.setText("Resume automation" if self._paused else "Pause automation")
        self.dashboard.show_status(st)
        remote_v, mine = host.get("version", ""), __version__
        note = f"Remote {mine}"
        if remote_v and _version_key(remote_v)[:2] != _version_key(mine)[:2]:
            note += f"<br><span style='color:{theme.WARN}'>The PC runs {c.esc(remote_v)}: update the older one</span>"
        if getattr(self, "_update_note", ""):
            note += "<br>" + self._update_note
        self.footer.setText(note)
        if self.tray is not None:
            self.tray_status.setText(f"{name}: {text}")
            self.tray_pause.setText("Resume automation" if self._paused else "Pause automation")

    def _videos(self, items: list) -> None:
        self.videos.show_videos(items)
        review = {i["id"]: i for i in items if i.get("state") == "review"}
        if self._review_seen is not None:
            new = [review[k] for k in review if k not in self._review_seen]
            if len(new) == 1:
                self.notify("A video waits for your OK", c.trim(new[0].get("headline"), 120))
            elif new:
                self.notify(f"{len(new)} videos wait for your OK", "Open ClipMonster Remote to watch them.")
        self._review_seen = set(review)

    def _review_count(self, n: int) -> None:
        self.nav.set_badge(self.i_videos, n)
        if self.tray is not None:
            self.tray_review.setText(f"⏸ {c.plural(n, 'video waits', 'videos wait')} for your OK")
            self.tray_review.setVisible(bool(n))
        try:
            from PySide6.QtGui import QGuiApplication

            if hasattr(QGuiApplication, "setBadgeNumber"):
                QGuiApplication.setBadgeNumber(n)  # the Dock icon's badge (Qt 6.5+)
        except Exception:  # noqa: BLE001
            pass

    def _inbox_count(self, n: int) -> None:
        self.nav.set_badge(self.i_inbox, n)
        if self.tray is not None:
            self.tray_inbox.setText(f"🏆 {c.plural(n, 'new sports clip')}")
            self.tray_inbox.setVisible(bool(n))

    def _event(self, e: dict) -> None:
        self.dashboard.add_event(e)
        if e.get("old"):
            return
        kind = e.get("kind")
        if kind == "delivery":  # the inbox says it (once it has the clip), not the news
            if self.session is not None:
                self.session.refresh_deliveries()
            return
        if e.get("alert") or kind in ("error", "problem", "login_needed"):
            self.notify("ClipMonster needs you", e.get("text") or "")
        elif kind in ("posted", "milestone", "hot"):
            self.notify({"posted": "Posted", "milestone": "Milestone", "hot": "Taking off"}[kind], e.get("text") or "")
        if self.session is not None:
            self.session.refresh_videos()

    # --- doing things ---------------------------------------------------------------------------------------------

    def refresh(self) -> None:
        if self.session is None:
            return
        self.session.refresh_status()
        self.session.refresh_videos()
        page = self.nav.currentWidget()
        if hasattr(page, "load"):
            page.load()

    def toggle_pause(self) -> None:
        s = self.session
        if s is None:
            return
        want = not self._paused
        self.notice("Pausing…" if want else "Resuming…")
        s.call(lambda cl: cl.post("/api/automation", {"paused": want}),
               lambda out: (self.notice(out.get("message", ""), good=True), s.refresh_status()),
               lambda e: self.notice(getattr(e, "message", str(e)), bad=True))

    def notice(self, text: str, good: bool = False, bad: bool = False) -> None:
        colour = theme.GOOD if good else theme.BAD if bad else theme.MUTED
        mark = "✓ " if good else "✗ " if bad else ""
        self.notice_label.setText(f"<span style='color:{colour}'>{c.esc(mark + text)}</span>")
        self._notice_timer.start(12000 if bad else 8000)

    def _open_videos(self, filt: str) -> None:
        self.nav.setCurrentIndex(self.i_videos)
        self.videos.set_filter(filt)

    def _page_changed(self, i: int) -> None:
        page = self.nav.widget(i)
        if page is not self.videos:
            self.videos.stop()
        if page is not self.inbox:
            self.inbox.stop()
        if self.session is not None and hasattr(page, "load"):
            page.load()

    def _check_update(self) -> None:
        self._update_note = ""

        def work():
            try:
                req = urllib.request.Request(LATEST_API, headers={"User-Agent": "ClipMonsterRemote",
                                                                  "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.load(r)
            except Exception:  # noqa: BLE001 - offline: no note
                return None
            tag = str(data.get("tag_name") or "").lstrip("v")
            has_mac = any("mac" in str(a.get("name", "")).lower() for a in data.get("assets") or [])
            return tag if has_mac and _version_key(tag) > _version_key(__version__) else None

        def ok(tag):
            if tag:
                self._update_note = (f"<a href='{RELEASES}' style='color:{theme.ACCENT_2}'>Remote {c.esc(tag)} is out: "
                                     "download</a>")
                self.footer.setText(f"Remote {__version__}<br>{self._update_note}")

        if prefs.load().get("check_updates", True):
            self.tasks.run(work, ok, lambda e: None)

    # --- closing ---------------------------------------------------------------------------------------------------

    def quit(self) -> None:
        self._quitting = True
        self.close()

    def closeEvent(self, e):  # noqa: N802
        if self.tray is not None and not self._quitting and sys.platform == "darwin" and self.session is not None:
            # like other Mac apps with a menu bar icon: closing the window keeps the notifications coming
            self.hide()
            e.ignore()
            return
        self.close_session()
        self.tasks.shutdown()
        if self.tray is not None:
            self.tray.hide()
        super().closeEvent(e)
        QApplication.instance().quit()

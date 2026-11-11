"""Your PC: the Windows PC this Mac is linked to, in ClipMonster for Mac's window.

ClipMonster for Mac makes clips by itself (the Studio and Library, ytclip/gui). Linking a PC (its IP address,
port and the authorization code from its Settings → Mac app) adds what the PC does: under Your PC in the
sidebar its automation, the videos waiting to post, the sports clips it sends, requests and the week's
report (the pages in this folder), and the PC lends the Mac its local AI and its processing (pc_ai.py,
pc_jobs.py; remote/studio.py on the PC).

PCLink is all of that the window holds: the connection (session.py), the pages and the bar over them (the PC's
name and state, Pause, Refresh), the menu bar icon and the notifications. The pages talk to it as their
"window": its session, proxy, notice(), notify() and refresh().
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton, QSystemTrayIcon, \
    QVBoxLayout, QWidget

from ..gui import theme
from . import APP_NAME, common as c
from . import hosts, prefs
from .connect import ConnectPage
from .dashboard import Dashboard
from .inbox import InboxPage
from .pages import AskPage, CategoriesPage, LogPage, PostedPage, RequestsPage, WeekPage
from .proxy import MediaProxy
from .session import Session, Tasks
from .sports_page import SportsPage
from .videos import VideosPage


class PCLink(QObject):
    changed = Signal()  # linked or not, reachable or not, or what the PC lends changed
    event = Signal(dict)  # the PC's news, as it comes (pc_jobs.py listens for its videos)

    def __init__(self, win, parent=None):
        super().__init__(parent or win)
        self.win = win
        self.tasks = Tasks(parent=self)
        self.session: Session | None = None
        self.proxy: MediaProxy | None = None
        self.status: dict = {}
        self.state = "unlinked"  # unlinked | connecting | online | offline
        self._paused = False
        self._review_seen: set[str] | None = None
        self._lends = None
        self.tray = None
        self.pc_rows: list[int] = []
        self._build_bar()
        self.connect_page = ConnectPage(self.tasks)
        self.connect_page.connect_to.connect(self.open_host)
        self.dashboard = Dashboard()
        self.videos = VideosPage(self)
        self.posted = PostedPage(self)
        self.inbox = InboxPage(self)
        self.sports = SportsPage(self)
        self.sports.view.open_studio.connect(self._open_in_studio)
        self.ask = AskPage(self)
        self.requests = RequestsPage(self)
        self.categories = CategoriesPage(self)
        self.week = WeekPage(self)
        self.log = LogPage(self)
        self.pages = (self.dashboard, self.videos, self.posted, self.sports, self.inbox, self.ask, self.requests,
                      self.categories, self.week, self.log)
        self.dashboard.open_videos.connect(self._open_videos)
        self.dashboard.open_page.connect(lambda name: self.nav.setCurrentIndex(
            {"posted": self.i_posted, "requests": self.i_requests, "categories": self.i_categories}[name]))
        self.videos.counts.connect(self._review_count)
        self.inbox.counts.connect(self._inbox_count)
        self._make_tray()

    # --- what the window shows ---------------------------------------------------------------------------------

    def _build_bar(self) -> None:
        """The bar over the PC's pages: which PC, what it's doing, Pause, Refresh and the PC menu, and a banner
        under it while the PC can't be reached."""
        self.bar = QWidget()
        v = QVBoxLayout(self.bar)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        row = QHBoxLayout()
        row.setContentsMargins(6, 0, 0, 0)
        row.setSpacing(10)
        self.dot = c.Dot()
        self.pc_label = QLabel("")
        self.pc_label.setStyleSheet("font-weight: 600;")
        self.pc_state = QLabel("")
        self.pc_state.setProperty("muted", True)
        row.addWidget(self.dot)
        row.addWidget(self.pc_label)
        row.addWidget(self.pc_state, 1)
        self.pause_btn = QPushButton("❚❚  Pause")
        self.pause_btn.setToolTip("Nothing new is clipped or made on the PC until you resume; posting carries on")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setObjectName("icon")
        self.refresh_btn.setToolTip("Refresh (⌘R)")
        self.refresh_btn.clicked.connect(self.refresh)
        self.pc_btn = QPushButton("PC")
        menu = QMenu(self.bar)
        menu.addAction("Switch to another PC…", self.switch_pc)
        menu.addAction("Unlink this Mac…", self.unlink)
        self.pc_btn.setMenu(menu)
        row.addWidget(self.pause_btn)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.pc_btn)
        v.addLayout(row)
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
        v.addWidget(self.offline)

    def add_pages(self, nav) -> None:
        """Your PC's rows in the sidebar: Link your PC until one is linked, then its pages."""
        self.nav = nav
        nav.section("Your PC")
        self.i_link = nav.addTab(self.connect_page, "Link your PC", "link")
        self.i_dash = nav.addTab(self.dashboard, "Overview", "automation")
        self.i_videos = nav.addTab(self.videos, "Waiting to post", "queue")
        self.i_posted = nav.addTab(self.posted, "Posted", "performance")
        self.i_sports = nav.addTab(self.sports, "Sports", "sports")
        self.i_inbox = nav.addTab(self.inbox, "Sports clips", "library")
        self.i_ask = nav.addTab(self.ask, "New video", "creator")
        self.i_requests = nav.addTab(self.requests, "Requests", "research")
        self.i_categories = nav.addTab(self.categories, "Categories", "categories")
        self.i_week = nav.addTab(self.week, "This week", "research")
        self.i_log = nav.addTab(self.log, "Log", "studio")
        self.pc_rows = [self.i_dash, self.i_videos, self.i_posted, self.i_sports, self.i_inbox, self.i_ask,
                        self.i_requests, self.i_categories, self.i_week, self.i_log]
        self.all_rows = [self.i_link] + self.pc_rows
        self._show_rows()

    def _show_rows(self) -> None:
        linked = self.session is not None
        self.nav.set_hidden(self.i_link, linked)
        for i in self.pc_rows:
            self.nav.set_hidden(i, not linked)
        if not linked and self.nav.currentIndex() in self.pc_rows:
            self.nav.setCurrentIndex(self.i_link)
        elif linked and self.nav.currentIndex() == self.i_link:
            self.nav.setCurrentIndex(self.i_dash)

    def owns(self, index: int) -> bool:
        return index in getattr(self, "all_rows", [])

    def page_changed(self, index: int) -> None:
        page = self.nav.widget(index)
        if page is not self.videos:
            self.videos.stop()
        if page is not self.inbox:
            self.inbox.stop()
        self.bar.setVisible(self.session is not None and index in self.pc_rows)
        if self.session is not None and hasattr(page, "load"):
            page.load()

    # --- the pages' window -----------------------------------------------------------------------------------

    def notice(self, text: str, good: bool = False, bad: bool = False) -> None:
        self.win.say(text, good=good, bad=bad)

    def notify(self, title: str, text: str) -> None:
        if self.tray is not None and prefs.load().get("notify", True):
            self.tray.showMessage(title, text, theme.app_icon(), 8000)

    def refresh(self) -> None:
        if self.session is None:
            return
        self.session.retry_now()  # (Try again: at once, and back to the usual pace)
        self.session.refresh_videos()
        page = self.nav.currentWidget()
        if page in self.pages and hasattr(page, "load"):
            page.load()

    # --- what the PC lends ----------------------------------------------------------------------------------

    @property
    def linked(self) -> bool:
        return self.session is not None

    @property
    def online(self) -> bool:
        return self.session is not None and self.state == "online"

    @property
    def name(self) -> str:
        if self.session is None:
            return ""
        return (self.status.get("host") or {}).get("name") or self.session.host.name

    def lends(self) -> dict:
        """What the PC can do for this Mac now: {"ai": {ready, model, why}, "studio": bool}, or {} (an older
        PC, or none reachable)."""
        return (self.status.get("lend") or {}) if self.online else {}

    def client(self):
        return self.session.client if self.session is not None else None

    # --- the menu bar icon -------------------------------------------------------------------------------------

    def _make_tray(self) -> None:
        """The menu bar icon: what the PC is doing, the videos waiting for your OK and new sports clips, and
        notifications (macOS shows them as the app's)."""
        if self.tray is not None or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(theme.app_icon(), self)
        menu = QMenu()
        self.tray_status = menu.addAction("No PC linked")
        self.tray_status.setEnabled(False)
        self.tray_review = menu.addAction("")
        self.tray_review.triggered.connect(lambda: (self.win.bring_up(), self._open_videos("review")))
        self.tray_review.setVisible(False)
        self.tray_inbox = menu.addAction("")
        self.tray_inbox.triggered.connect(lambda: (self.win.bring_up(), self.nav.setCurrentIndex(self.i_inbox)))
        self.tray_inbox.setVisible(False)
        menu.addSeparator()
        self.tray_pause = menu.addAction("Pause automation", self.toggle_pause)
        self.tray_pause.setVisible(False)
        menu.addAction(f"Open {APP_NAME}", self.win.bring_up)
        menu.addSeparator()
        menu.addAction(f"Quit {APP_NAME}", self.win.quit)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(APP_NAME)
        self.tray.activated.connect(lambda reason: self.win.bring_up() if reason == QSystemTrayIcon.Trigger
                                    and sys.platform != "darwin" else None)
        self.tray.show()

    def _tray_unlinked(self) -> None:
        if self.tray is not None:
            self.tray_status.setText("No PC linked")
            for act in (self.tray_review, self.tray_inbox, self.tray_pause):
                act.setVisible(False)

    def _drop_tray(self) -> None:
        if self.tray is not None:
            self.tray.hide()
            self.tray.deleteLater()
            self.tray = None

    # --- connecting ------------------------------------------------------------------------------------------

    def open_host(self, host: hosts.Host) -> None:
        self.close_session()
        if not host.token():
            self.connect_page.say(f"This Mac's key for {c.esc(host.name)} is gone: link it again.")
            hosts.forget(host.id)
            self._show_rows()
            return
        self.session = Session(host, self.tasks, self)
        self.session.status.connect(self._status)
        self.session.videos.connect(self._videos)
        self.session.deliveries.connect(self.inbox.show_deliveries)
        self.session.event.connect(self._event)
        self.session.connection.connect(self._connection)
        self.proxy = MediaProxy(lambda: self.session.client if self.session is not None else None)
        self.state = "connecting"
        self.pc_label.setText(host.name)
        self.pc_state.setText("Connecting…")
        self.dot.set(theme.MUTED)
        self._review_seen = None
        if self.tray is not None:
            self.tray_status.setText(f"{host.name}: connecting…")
            self.tray_pause.setVisible(True)
        self._show_rows()
        self.inbox.connected()
        self.session.start()
        self.page_changed(self.nav.currentIndex())
        self.changed.emit()

    def close_session(self) -> None:
        self.videos.stop()
        self.inbox.disconnect_()
        had = self.session is not None
        if self.session is not None:
            self.session.stop()
            self.session.deleteLater()
            self.session = None
        if self.proxy is not None:
            self.proxy.close()
            self.proxy = None
        self.status = {}
        self.state = "unlinked"
        self._lends = None
        self.offline.hide()
        if had:
            self.changed.emit()

    def switch_pc(self) -> None:
        self.close_session()
        self._tray_unlinked()
        self.connect_page.say("")
        self._show_rows()
        self.nav.setCurrentIndex(self.i_link)
        self.win.bring_up()

    def unlink(self) -> None:
        s = self.session
        if s is None:
            return
        ask = QMessageBox(QMessageBox.Warning, "Unlink this Mac?",
                          f"This Mac won't be able to use {s.host.name} (its AI, its processing or its automation) "
                          "until you link it again with a new code.", QMessageBox.Cancel, self.win)
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
        name = self.name or "the PC"
        if state == "online":
            self.offline.hide()
            self.dot.set(theme.WARN if self._paused else theme.GOOD)
            if self.state != "online":
                self.state = "online"
                self.changed.emit()
            return
        if state in ("unlinked", "wrong_pc"):
            host = self.session.host if self.session else None
            self.close_session()
            self._tray_unlinked()
            if host is not None and state == "unlinked":
                hosts.forget(host.id)
            self.connect_page.say(c.esc(text))
            self._show_rows()
            self.nav.setCurrentIndex(self.i_link)
            return
        was = self.state
        self.state = "offline"
        self.dot.set(theme.BAD)
        self.pc_state.setText("Not reachable")
        self.offline_text.setText(f"<b>Can't reach {c.esc(name)}.</b> {c.esc(text)} Trying again by itself.")
        self.offline.show()
        if self.tray is not None:
            self.tray_status.setText(f"{name}: not reachable")
        if was != "offline":
            self.changed.emit()

    # --- what the PC says ----------------------------------------------------------------------------------------

    def _status(self, st: dict) -> None:
        self.status = st
        auto = st.get("automation") or {}
        self._paused = bool(auto.get("paused"))
        name = self.name
        self.pc_label.setText(name)
        job = st.get("job")
        making = (st.get("lend") or {}).get("making")
        if making:
            text = "Making clips for your Mac"
        elif self._paused:
            text = "Automation paused · posting carries on"
        elif job:
            text = ("Making " if job.get("making") else "Clipping ") + f"“{c.trim(job.get('title'), 60)}”"
        else:
            text = c.trim(auto.get("state") or "Running", 80)
        self.pc_state.setText(text)
        self.dot.set(theme.WARN if self._paused else theme.GOOD)
        self.pause_btn.setText("▶  Resume" if self._paused else "❚❚  Pause")
        self.dashboard.show_status(st)
        if self.tray is not None:
            self.tray_status.setText(f"{name}: {text}")
            self.tray_pause.setText("Resume automation" if self._paused else "Pause automation")
        self.win.pc_status(st)
        lends = st.get("lend") or {}
        key = (bool((lends.get("ai") or {}).get("ready")), (lends.get("ai") or {}).get("model"), bool(lends.get("studio")))
        if key != self._lends:
            self._lends = key
            self.changed.emit()

    def _videos(self, items: list) -> None:
        self.videos.show_videos(items)
        review = {i["id"]: i for i in items if i.get("state") == "review"}
        if self._review_seen is not None:
            new = [review[k] for k in review if k not in self._review_seen]
            if len(new) == 1:
                self.notify("A video waits for your OK", c.trim(new[0].get("headline"), 120))
            elif new:
                self.notify(f"{len(new)} videos wait for your OK", f"Open {APP_NAME} to watch them.")
        self._review_seen = set(review)

    def _review_count(self, n: int) -> None:
        self.nav.set_badge(self.i_videos, n)
        if self.tray is not None:
            self.tray_review.setText(f"⏸ {c.plural(n, 'video waits', 'videos wait')} for your OK")
            self.tray_review.setVisible(bool(n))
        try:
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
        self.event.emit(e)
        if e.get("old"):
            return
        kind = e.get("kind")
        if kind == "delivery":  # the inbox says it (once it has the clip), not the news
            if self.session is not None:
                self.session.refresh_deliveries()
            return
        if kind == "studio":  # a video this Mac sent: pc_jobs.py fetches its clips and says so
            return
        if kind == "sports":  # a game's highlights are ready, its clips made, new games found
            self.sports.news(e)
            self.notify("Sports", e.get("text") or "")
            return
        if e.get("alert") or kind in ("error", "problem", "login_needed"):
            self.notify("ClipMonster needs you", e.get("text") or "")
        elif kind in ("posted", "milestone", "hot"):
            self.notify({"posted": "Posted", "milestone": "Milestone", "hot": "Taking off"}[kind], e.get("text") or "")
        if self.session is not None:
            self.session.refresh_videos()

    # --- doing things ---------------------------------------------------------------------------------------------

    def toggle_pause(self) -> None:
        s = self.session
        if s is None:
            return
        want = not self._paused
        self.notice("Pausing…" if want else "Resuming…")
        s.call(lambda cl: cl.post("/api/automation", {"paused": want}),
               lambda out: (self.notice(out.get("message", ""), good=True), s.refresh_status()),
               lambda e: self.notice(getattr(e, "message", str(e)), bad=True))

    def _open_videos(self, filt: str) -> None:
        self.nav.setCurrentIndex(self.i_videos)
        self.videos.set_filter(filt)

    def _open_in_studio(self, url: str, title: str, clips: list) -> None:
        """A game's ticked plays into this Mac's own Studio, as its clips."""
        opener = getattr(self.win, "open_in_studio", None)
        if opener is not None:
            opener(url, title, clips)

    def shutdown(self) -> None:
        self.sports.shutdown()
        self.close_session()
        self.tasks.shutdown()
        self._drop_tray()

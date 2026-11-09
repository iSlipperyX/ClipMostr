"""Overview: what the PC is doing right now, what's waiting, the accounts, and the latest news."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from ..gui import theme
from ..gui.status_bars import NowPanel
from ..steps import Status
from . import common as c

NEWS_KEPT = 12
EVENT_ICONS = {"delivery": "📥", "posted": "✔", "error": "✖", "problem": "⚠", "login_needed": "⚠", "milestone": "🏁", "hot": "🔥",
               "linked": "💻"}


class Dashboard(QScrollArea):
    open_videos = Signal(str)  # a filter for the Videos page ("review", "queued", "")
    open_page = Signal(str)  # "posted" | "requests" | "categories"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body.setObjectName("page")
        self.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(24, 18, 24, 24)
        v.setSpacing(14)

        head = QHBoxLayout()
        self.title = c.title("Overview")
        self.sub = QLabel("")
        self.sub.setProperty("muted", True)
        tv = QVBoxLayout()
        tv.setSpacing(2)
        tv.addWidget(self.title)
        tv.addWidget(self.sub)
        head.addLayout(tv, 1)
        v.addLayout(head)

        self.problems = QFrame()
        self.problems.setObjectName("banner")
        pv = QVBoxLayout(self.problems)
        pv.setContentsMargins(12, 9, 12, 9)
        self.problems_text = c.WrapLabel("")
        pv.addWidget(self.problems_text)
        self.problems.hide()
        v.addWidget(self.problems)

        self.now = NowPanel()
        v.addWidget(self.now)
        self.idle, idle_v = c.panel()
        self.idle_text = c.WrapLabel("")
        idle_v.addWidget(self.idle_text)
        v.addWidget(self.idle)

        cards = QGridLayout()
        cards.setSpacing(10)
        self.c_review = c.StatCard("Wait for your OK")
        self.c_review.clicked.connect(lambda: self.open_videos.emit("review"))
        self.c_queued = c.StatCard("Queued to post")
        self.c_queued.clicked.connect(lambda: self.open_videos.emit("queued"))
        self.c_posted = c.StatCard("Posted today")
        self.c_posted.clicked.connect(lambda: self.open_page.emit("posted"))
        self.c_made = c.StatCard("Made in the last day")
        self.c_made.clicked.connect(lambda: self.open_page.emit("requests"))
        self.c_disk = c.StatCard("Free on the PC")
        for i, card in enumerate((self.c_review, self.c_queued, self.c_posted, self.c_made, self.c_disk)):
            cards.addWidget(card, 0, i)
        v.addLayout(cards)

        self.next_post, nv = c.panel()
        self.next_text = c.WrapLabel("")
        nv.addWidget(self.next_text)
        v.addWidget(self.next_post)

        cols = QHBoxLayout()
        cols.setSpacing(12)
        up, uv = c.panel()
        uh = QHBoxLayout()
        uh.addWidget(c.h2("Up next in automation"), 1)
        self.up_count = QLabel("")
        self.up_count.setProperty("muted", True)
        uh.addWidget(self.up_count)
        uv.addLayout(uh)
        self.up_list = QVBoxLayout()
        self.up_list.setSpacing(6)
        uv.addLayout(self.up_list)
        uv.addStretch(1)
        acc, av = c.panel()
        ah = QHBoxLayout()
        ah.addWidget(c.h2("TikTok accounts"), 1)
        av.addLayout(ah)
        self.acc_list = QVBoxLayout()
        self.acc_list.setSpacing(6)
        av.addLayout(self.acc_list)
        av.addStretch(1)
        cols.addWidget(up, 1)
        cols.addWidget(acc, 1)
        v.addLayout(cols)

        news, nwv = c.panel()
        nwh = QHBoxLayout()
        nwh.addWidget(c.h2("News from the PC"), 1)
        nwv.addLayout(nwh)
        self.news_list = QVBoxLayout()
        self.news_list.setSpacing(5)
        nwv.addLayout(self.news_list)
        self.news_empty = QLabel("Posts going up, problems and milestones show up here while this window is open.")
        self.news_empty.setProperty("muted", True)
        self.news_empty.setWordWrap(True)
        nwv.addWidget(self.news_empty)
        v.addWidget(news)
        v.addStretch(1)
        self.news: list[dict] = []
        self._sig = None

    # --- filling in ---------------------------------------------------------------------------------------

    def show_status(self, st: dict) -> None:
        now = time.time()
        host = st.get("host") or {}
        auto = st.get("automation") or {}
        self.title.setText(host.get("name") or "Your PC")
        bits = [f"ClipMonster {host.get('version', '')}"]
        if auto.get("paused"):
            bits.insert(0, f"<span style='color:{theme.WARN}'>❚❚ Automation is paused</span>")
        else:
            bits.insert(0, f"<span style='color:{theme.GOOD}'>●</span> Automation is running")
        if host.get("time"):
            bits.append(f"PC time {time.strftime('%H:%M', time.gmtime(host['time'] + (host.get('utc_offset') or 0)))}")
        self.sub.setText(" · ".join(bits))
        self.sub.setTextFormat(Qt.RichText)

        problems = st.get("problems") or []
        self.problems.setVisible(bool(problems))
        self.problems_text.setText("<br>".join(f"<span style='color:{theme.WARN}'>⚠</span> {c.esc(p)}"
                                               for p in problems))

        job = st.get("job")
        activity = auto.get("activity")
        if job:
            status = Status.from_json(job.get("status")) if job.get("status") else None
            self.now.show_job(job.get("title") or "", status, job.get("stage") or "", bool(job.get("making")))
            self.idle.hide()
        elif activity:
            self.now.show_activity(activity.get("text") or "", activity.get("frac"))
            self.idle.hide()
        else:
            self.now.idle()
            self.idle.show()
            if auto.get("paused"):
                text = ("<b>Paused.</b> Nothing new is clipped or made until you resume. Posting carries on.")
            elif auto.get("state"):
                text = f"<b>Idle.</b> <span style='color:{theme.MUTED}'>{c.esc(auto['state'])}</span>"
            else:
                text = "<b>Idle.</b> Waiting for the next video to clip or make."
            self.idle_text.setText(text)

        posting = st.get("posting") or {}
        review = int(posting.get("review") or 0)
        self.c_review.set(review, "Tap to watch them" if review else "Nothing waiting",
                          theme.WARN if review else "")
        self.c_queued.set(posting.get("queued", 0) if posting else "–")
        self.c_posted.set(posting.get("today", 0) if posting else "–")
        today = st.get("today") or {}
        made = int(today.get("clips") or 0)
        self.c_made.set(made, " · ".join(b for b in (
            c.plural(today.get("clipped") or 0, "video") + " clipped" if today.get("clipped") else "",
            c.plural(today.get("made") or 0, "Creator video") if today.get("made") else "",
            f"{today['failed']} failed" if today.get("failed") else "") if b), "")
        disk = st.get("disk")
        if disk:
            self.c_disk.set(f"{disk['free'] / c.GB:.0f} GB", disk.get("drive") or "", theme.WARN if disk.get("low") else "")
        else:
            self.c_disk.set("–")

        nxt = posting.get("next") if posting else None
        if nxt:
            self.next_text.setText(f"📤 <b>Next post</b> {c.esc(c.when(nxt.get('at'), now))} on "
                                   f"{c.esc(nxt.get('account'))}: “{c.esc(c.trim(nxt.get('headline'), 120))}”")
            self.next_post.show()
        elif posting is None:
            self.next_text.setText(f"<span style='color:{theme.WARN}'>Posting's library couldn't be read on the PC."
                                   "</span>")
            self.next_post.show()
        else:
            self.next_post.hide()

        sig = (repr((st.get("queue") or {}).get("next")), repr(st.get("accounts")), int(now // 60))
        if sig != self._sig:
            self._sig = sig
            self._fill_up(st.get("queue") or {}, now)
            self._fill_accounts(st.get("accounts") or [], now)

    def _fill_up(self, q: dict, now: float) -> None:
        c.clear(self.up_list)
        bits = []
        if q.get("to_clip"):
            bits.append(f"{q['to_clip']} to clip")
        if q.get("to_make"):
            bits.append(f"{q['to_make']} to make")
        self.up_count.setText(" · ".join(bits))
        items = q.get("next") or []
        if not items:
            lab = QLabel("Nothing waiting. Ask for something on New video.")
            lab.setProperty("muted", True)
            self.up_list.addWidget(lab)
            return
        for j in items:
            icon = "✨" if j.get("making") else "✂"
            later = j.get("next_try") and j["next_try"] > now + 60
            meta = " · ".join(b for b in (j.get("category"), f"from {c.when(j['next_try'], now)}" if later else "",
                                          j.get("stage") if j.get("stage") not in ("", "Waiting") else "") if b)
            lab = c.WrapLabel(f"{icon} {c.esc(c.trim(j.get('title'), 90))}"
                              + (f"<br><span style='color:{theme.MUTED}'>{c.esc(meta)}</span>" if meta else ""))
            self.up_list.addWidget(lab)

    def _fill_accounts(self, accounts: list, now: float) -> None:
        c.clear(self.acc_list)
        if not accounts:
            lab = QLabel("No TikTok accounts yet (Accounts tab on the PC).")
            lab.setProperty("muted", True)
            self.acc_list.addWidget(lab)
            return
        for a in accounts:
            if not a.get("logged_in"):
                mark = f"<span style='color:{theme.WARN}'>⚠ not signed in</span>"
            elif not a.get("autopilot"):
                mark = f"<span style='color:{theme.MUTED}'>posting paused</span>"
            else:
                mark = f"<span style='color:{theme.GOOD}'>●</span>"
            meta = f"{a.get('queued', 0)} queued · {a.get('per_day', 0)} a day"
            if a.get("next_at"):
                meta += f" · next {c.when(a['next_at'], now)}"
            self.acc_list.addWidget(c.WrapLabel(f"<b>{c.esc(a.get('handle'))}</b> {mark}<br>"
                                                f"<span style='color:{theme.MUTED}'>{c.esc(meta)}</span>"))

    def add_event(self, e: dict) -> None:
        self.news.insert(0, e)
        del self.news[NEWS_KEPT:]
        self.news_empty.setVisible(not self.news)
        c.clear(self.news_list)
        for n in self.news:
            icon = EVENT_ICONS.get(n.get("kind"), "•")
            colour = theme.WARN if n.get("alert") else theme.TEXT
            t = time.strftime("%H:%M", time.localtime(n.get("at") or time.time()))
            self.news_list.addWidget(c.WrapLabel(f"<span style='color:{theme.MUTED}'>{t}</span>&nbsp; "
                                                 f"<span style='color:{colour}'>{icon} {c.esc(n.get('text'))}</span>"))

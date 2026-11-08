"""First-run setup: everything ClipMonster needs to run by itself, in one window, about ten minutes.

1. Tools: ffmpeg (built in), Node.js (YouTube downloads), Firefox (the YouTube sign-in), Edge or Chrome
   (posting to TikTok).
2. Keys: the AI (Claude or Gemini) and the YouTube Data API.
3. YouTube sign-in, in ClipMonster's own Firefox profile.
4. TikTok accounts: add each one and log in once.
5. The first category: what it clips, and the account it posts to.
6. The posting schedule.
7. Start: with Windows, and now.

Every step can be skipped, and the whole thing opened again from Settings → General.
"""

from __future__ import annotations

import shutil
from typing import Callable

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QSpinBox, QStackedWidget,
                               QTimeEdit, QVBoxLayout, QWidget)

from .. import categories, youtube_login
from ..settings import Settings
from . import theme
from .posting_state import PostingState

NODE_URL = "https://nodejs.org/en/download"
FIREFOX_URL = "https://www.mozilla.org/firefox/new/"
EDGE_URL = "https://www.microsoft.com/edge/download"
ANTHROPIC_KEY_URL = "https://console.anthropic.com/settings/keys"
GEMINI_KEY_URL = "https://aistudio.google.com/apikey"
YOUTUBE_KEY_URL = "https://console.cloud.google.com/apis/library/youtube.googleapis.com"

STEPS = ("Tools", "Keys", "YouTube sign-in", "TikTok accounts", "First category", "Posting schedule", "Start")


def _link(url: str, text: str) -> str:
    return f"<a href='{url}' style='color:{theme.ACCENT_2}'>{text}</a>"


def _text(html: str) -> QLabel:
    label = QLabel(html)
    label.setWordWrap(True)
    label.setOpenExternalLinks(True)
    label.setTextFormat(Qt.RichText)
    return label


def tools() -> list[tuple[str, bool, str]]:
    """(what, there?, what it's for / where to get it) for each tool ClipMonster relies on."""
    from ..doctor import GOOD, _ffmpeg
    from ..posting.tiktok.login import find_browser

    ff = _ffmpeg()
    node = any(shutil.which(n) for n in ("node", "deno", "bun"))
    firefox = youtube_login.find_firefox() is not None
    browser = find_browser("msedge") or find_browser("chrome")
    return [
        ("ffmpeg", ff.level == GOOD, "Built in: cuts and exports the clips." if ff.level == GOOD else ff.detail),
        ("Node.js", node, "YouTube downloads need it." + ("" if node else " " + _link(NODE_URL, "Get Node.js (LTS)")
                                                           + ", then press Check again.")),
        ("Firefox", firefox, "For signing in to YouTube in a profile of ClipMonster's own." +
         ("" if firefox else " " + _link(FIREFOX_URL, "Get Firefox") + ".")),
        ("Microsoft Edge or Chrome", browser is not None, "Posts to TikTok, with each account's own login." +
         ("" if browser else " " + _link(EDGE_URL, "Get Edge") + ".")),
    ]


class SetupWizard(QDialog):
    def __init__(self, settings: Settings, change: Callable[[str, object], None], posting: PostingState,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set up ClipMonster")
        self.resize(720, 560)
        self.s = settings
        self._change = change  # (name, value): the window's own settings change
        self.posting = posting
        self.start_now = False  # Finish with "Start automation now" ticked
        self.problems: list[str] = []  # for the window to show once this closes
        lay = QVBoxLayout(self)
        self.crumbs = QLabel()
        self.crumbs.setTextFormat(Qt.RichText)
        lay.addWidget(self.crumbs)
        self.pages = QStackedWidget()
        lay.addWidget(self.pages, 1)
        for build in (self._tools, self._keys, self._youtube, self._accounts, self._category, self._schedule,
                      self._start):
            page = QWidget()
            pl = QVBoxLayout(page)
            pl.setContentsMargins(4, 10, 4, 4)
            build(pl)
            pl.addStretch(1)
            self.pages.addWidget(page)
        row = QHBoxLayout()
        self.later = QPushButton("Finish later")
        self.later.setToolTip("What you've filled in is kept. The setup opens again the next time ClipMonster "
                              "starts, or from Settings → General → Set up again")
        self.later.clicked.connect(self.reject)
        row.addWidget(self.later)
        row.addStretch(1)
        self.back = QPushButton("Back")
        self.back.clicked.connect(lambda: self._go(-1))
        self.next = QPushButton("Next")
        self.next.setObjectName("primary")
        self.next.clicked.connect(lambda: self._go(1))
        row.addWidget(self.back)
        row.addWidget(self.next)
        lay.addLayout(row)
        posting.accounts_changed.connect(self._fill_accounts)
        self._go(0)

    # --- moving about ------------------------------------------------------------------------

    def _go(self, delta: int) -> None:
        i = self.pages.currentIndex()
        if delta > 0:
            self._leave(i)
            if i == len(STEPS) - 1:
                self._finish()
                return
        i = max(0, min(len(STEPS) - 1, i + delta))
        self.pages.setCurrentIndex(i)
        self._enter(i)
        self.back.setEnabled(i > 0)
        self.next.setText("Finish" if i == len(STEPS) - 1 else "Next")
        self.crumbs.setText("  ›  ".join(f"<b>{name}</b>" if n == i else
                                         f"<span style='color:{theme.MUTED}'>{name}</span>"
                                         for n, name in enumerate(STEPS)))

    def _enter(self, i: int) -> None:
        if STEPS[i] == "Tools":
            self._check_tools()
        elif STEPS[i] == "YouTube sign-in":
            self._show_sign_in()
        elif STEPS[i] in ("TikTok accounts", "First category"):
            self._fill_accounts()

    def _leave(self, i: int) -> None:
        step = STEPS[i]
        if step == "Keys":
            self._set("ai_provider", self.provider.currentData())
            for name, box in (("ai_api_key", self.claude_key), ("gemini_api_key", self.gemini_key),
                              ("youtube_api_key", self.youtube_key)):
                if box.text().strip() != getattr(self.s, name):
                    self._set(name, box.text().strip())
        elif step == "First category":
            self._save_category()
        elif step == "Posting schedule":
            self._save_schedule()

    def _set(self, name: str, value) -> None:
        self._change(name, value)
        self.s = self.s.apply({name: value})

    # --- 1. tools ---------------------------------------------------------------------------------

    def _tools(self, pl) -> None:
        pl.addWidget(_text("<p style='font-size:14pt'><b>Welcome to ClipMonster</b></p>"
                           "<p>It finds videos, clips and edits them, posts them to TikTok on a schedule, reads how "
                           "they do, keeps a record of each one and deletes its file once it's up. This setup "
                           "gets it ready to do that by itself. First, the programs it uses:</p>"))
        self.tool_rows = QLabel()
        self.tool_rows.setTextFormat(Qt.RichText)
        self.tool_rows.setWordWrap(True)
        self.tool_rows.setOpenExternalLinks(True)
        pl.addWidget(self.tool_rows)
        again = QPushButton("Check again")
        again.clicked.connect(self._check_tools)
        pl.addWidget(again, 0, Qt.AlignLeft)

    def _check_tools(self) -> None:
        rows = []
        for what, ok, note in tools():
            mark = f"<span style='color:{theme.GOOD}'>✓</span>" if ok else f"<span style='color:{theme.WARN}'>✗</span>"
            rows.append(f"<p>{mark} <b>{what}</b>: {note}</p>")
        self.tool_rows.setText("".join(rows))

    # --- 2. keys ------------------------------------------------------------------------------------

    def _keys(self, pl) -> None:
        pl.addWidget(_text("<p style='font-size:12pt'><b>Keys</b></p>"
                           "<p>The AI picks the moments worth clipping, writes hooks and hashtags. "
                           f"Get a Gemini key free at {_link(GEMINI_KEY_URL, 'aistudio.google.com')}, or a Claude "
                           f"key at {_link(ANTHROPIC_KEY_URL, 'console.anthropic.com')}. The YouTube key lets "
                           "ClipMonster read the charts and channels to find videos: "
                           f"{_link(YOUTUBE_KEY_URL, 'turn on YouTube Data API v3')} in a Google Cloud project, "
                           "then make an API key under Credentials. Keys are saved encrypted for your Windows "
                           "account.</p>"))
        form = QFormLayout()
        self.provider = QComboBox()
        self.provider.addItem("Gemini (Google)", "gemini")
        self.provider.addItem("Claude (Anthropic)", "claude")
        self.provider.setCurrentIndex(max(0, self.provider.findData(self.s.ai_provider)))
        form.addRow("AI to use", self.provider)
        self.gemini_key = QLineEdit(self.s.gemini_api_key, echoMode=QLineEdit.Password, placeholderText="AIza…")
        form.addRow("Gemini key", self.gemini_key)
        self.claude_key = QLineEdit(self.s.ai_api_key, echoMode=QLineEdit.Password, placeholderText="sk-ant-…")
        form.addRow("Claude key", self.claude_key)
        self.youtube_key = QLineEdit(self.s.youtube_api_key, echoMode=QLineEdit.Password, placeholderText="AIza…")
        form.addRow("YouTube key", self.youtube_key)
        pl.addLayout(form)
        row = QHBoxLayout()
        test = QPushButton("Test keys")
        test.clicked.connect(self._test_keys)
        self.key_result = QLabel()
        self.key_result.setWordWrap(True)
        row.addWidget(test)
        row.addWidget(self.key_result, 1)
        pl.addLayout(row)

    def _test_keys(self) -> None:
        from PySide6.QtWidgets import QApplication

        from .. import ai

        self._leave(STEPS.index("Keys"))
        self.key_result.setText("Checking…")
        QApplication.processEvents()
        lines, ok = [], True
        for p in [p for p in ai.NAMES if ai.key_for(self.s, p)]:
            try:
                lines.append(f"✓ {ai.check_key(self.s, p)}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"✗ {ai.name(p)}: {exc}")
                ok = False
        if self.s.youtube_api_key:
            try:
                from ..trending import fetch_chart

                fetch_chart(self.s.youtube_api_key, "US", limit=1)  # 1 quota unit of 10,000 a day
                lines.append("✓ YouTube key works")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"✗ YouTube: {exc}")
                ok = False
        self.key_result.setText("\n".join(lines) or "Add a key first.")
        self.key_result.setStyleSheet(f"color: {theme.GOOD if ok and lines else theme.WARN}")

    # --- 3. YouTube sign-in ---------------------------------------------------------------------------

    def _youtube(self, pl) -> None:
        pl.addWidget(_text("<p style='font-size:12pt'><b>Sign in to YouTube</b></p>"
                           "<p>Signed-in downloads keep YouTube from asking this PC to prove it's not a bot. "
                           "ClipMonster opens Firefox with a profile of its own: sign in there with a spare Google "
                           "account (not your main one), then close that window.</p>"))
        row = QHBoxLayout()
        go = QPushButton("🔑  Sign in to YouTube…")
        go.clicked.connect(self._sign_in)
        row.addWidget(go)
        self.sign_state = QLabel()
        row.addWidget(self.sign_state, 1)
        pl.addLayout(row)

    def _show_sign_in(self) -> None:
        on = self.s.cookies_browser == youtube_login.CHOICE and youtube_login.read_sign_in().signed_in
        self.sign_state.setText("✓ Signed in" if on else "Not signed in yet")
        self.sign_state.setStyleSheet(f"color: {theme.GOOD if on else theme.MUTED}")

    def _sign_in(self) -> None:
        from .sign_in_dialog import SignInDialog

        dlg = SignInDialog(self)
        dlg.signed_in.connect(lambda: self._set("cookies_browser", youtube_login.CHOICE))
        dlg.exec()
        self._show_sign_in()

    # --- 4. TikTok accounts ------------------------------------------------------------------------------

    def _accounts(self, pl) -> None:
        pl.addWidget(_text("<p style='font-size:12pt'><b>TikTok accounts</b></p>"
                           "<p>Add each TikTok account clips should post to: link one you have, or make a new one "
                           "on TikTok's own sign-up page. You sign in once in the window that opens (the QR code "
                           "from the TikTok app is quickest); each account keeps its own sign-in on this PC. "
                           "Every category posts to one of them.</p>"))
        self.account_list = QListWidget()
        self.account_list.setMaximumHeight(160)
        pl.addWidget(self.account_list)
        row = QHBoxLayout()
        add = QPushButton("+ Add an account")
        add.setObjectName("primary")
        add.clicked.connect(self._add_account)
        row.addWidget(add)
        self.relogin = QPushButton("Sign in again")
        self.relogin.clicked.connect(self._login_selected)
        row.addWidget(self.relogin)
        row.addStretch(1)
        pl.addLayout(row)

    def _fill_accounts(self) -> None:
        if not hasattr(self, "account_list"):
            return
        self.account_list.clear()
        for a in self.posting.accounts:
            shown = a.label + (f"  ·  @{a.username}" if a.username and a.name.strip() else "")
            self.account_list.addItem(f"{'✓' if a.logged_in else '○'}  {shown}"
                                      + ("" if a.logged_in else "  (not signed in yet)"))
        self.relogin.setEnabled(len(self.posting.accounts) > 0)
        if hasattr(self, "category_account"):
            keep = self.category_account.currentData()
            self.category_account.clear()
            self.category_account.addItem("⚠ No account yet: nothing is picked for it until it has one", "")
            for a in self.posting.accounts:
                self.category_account.addItem(a.label, a.id)
            i = self.category_account.findData(keep) if keep else (1 if len(self.posting.accounts) else 0)
            self.category_account.setCurrentIndex(max(0, i))

    def _add_account(self) -> None:
        self._wizard()

    def _wizard(self, account_id: str = "") -> None:
        from .accounts_view import AccountWizard

        # Categories come on the next page, so this one only adds the account (and its warm-up)
        AccountWizard(self.posting, account_id=account_id, parent=self).exec()
        self.posting.poll()
        self._fill_accounts()

    def _login_selected(self) -> None:
        i = self.account_list.currentRow()
        accounts = list(self.posting.accounts)
        if 0 <= i < len(accounts):
            self._login(accounts[i].id)
        elif accounts:
            self._login(accounts[-1].id)

    def _login(self, account_id: str) -> None:
        self._wizard(account_id)

    # --- 5. first category -----------------------------------------------------------------------------------

    def _category(self, pl) -> None:
        pl.addWidget(_text("<p style='font-size:12pt'><b>Your first category</b></p>"
                           "<p>A category is one account's topic: where its videos come from and how its clips look. "
                           "Channels you list should be ones that welcome clipping (a clipping programme, say); "
                           "a standing request is plain words, like “Warzone clutch plays”, that ClipMonster "
                           "searches for every few hours. More categories, sources and styles are in the Categories "
                           "tab.</p>"))
        form = QFormLayout()
        existing = categories.load(self.s)
        n = existing[0] if existing else None
        self.category_name = QLineEdit(n.name if n else "", placeholderText="e.g. Gaming, Podcasts")
        form.addRow("Name", self.category_name)
        self.category_account = QComboBox()
        form.addRow("Posts to", self.category_account)
        self.category_channels = QPlainTextEdit(n.channels if n else "")
        self.category_channels.setPlaceholderText("@somepodcast  # clipping programme, credit in caption")
        self.category_channels.setFixedHeight(64)
        form.addRow("Channels", self.category_channels)
        self.category_finds = QPlainTextEdit(n.finds if n else "")
        self.category_finds.setPlaceholderText("Warzone clutch plays\nfunny podcast moments")
        self.category_finds.setFixedHeight(64)
        form.addRow("Standing requests", self.category_finds)
        self.category_daily = QSpinBox(minimum=1, maximum=categories.MAX_DAILY, value=n.daily if n else 2)
        self.category_daily.setSuffix(" videos a day")
        form.addRow("Videos to clip", self.category_daily)
        lens = QHBoxLayout()
        self.min_len = QDoubleSpinBox(minimum=5, maximum=600, value=(n.min_len if n and n.min_len else 62), suffix=" s")
        self.max_len = QDoubleSpinBox(minimum=5, maximum=600, value=(n.max_len if n and n.max_len else 90), suffix=" s")
        for box in (self.min_len, self.max_len):
            box.setDecimals(0)
        lens.addWidget(self.min_len)
        lens.addWidget(QLabel("to"))
        lens.addWidget(self.max_len)
        lens.addStretch(1)
        form.addRow("Clip length", lens)
        hint = QLabel("Over a minute is what TikTok's Creator Rewards pay for.")
        hint.setProperty("muted", True)
        form.addRow("", hint)
        pl.addLayout(form)

    def _save_category(self) -> None:
        name = " ".join(self.category_name.text().split())
        if not name:
            return
        aid = self.category_account.currentData() or ""
        a = self.posting.accounts.get(aid)
        found = categories.load(self.s)
        base = found[0] if found else categories.Category(id=categories.new_id(), name=name)
        from dataclasses import replace

        lo, hi = sorted((self.min_len.value(), self.max_len.value()))
        new = replace(base, name=name, account_id=aid, account=a.handle if a else "",
                      channels=self.category_channels.toPlainText().strip(), finds=self.category_finds.toPlainText().strip(),
                      daily=self.category_daily.value(), min_len=lo, max_len=hi)
        self._set("categories", categories.to_settings([new] + found[1:]))

    # --- 6. schedule -------------------------------------------------------------------------------------

    def _schedule(self, pl) -> None:
        pl.addWidget(_text("<p style='font-size:12pt'><b>Posting schedule</b></p>"
                           "<p>Two posts a day, at least three hours apart, is what posting studies support: more "
                           "rarely adds views and looks automated. Each post time moves by a few minutes every day. "
                           "A new TikTok account warms up for two weeks first, posting less, so it doesn't look "
                           "like spam. This applies to every account; each can be changed in the Accounts tab.</p>"))
        form = QFormLayout()
        ps = self.posting.settings
        self.per_day = QSpinBox(minimum=1, maximum=10, value=ps.posts_per_day)
        form.addRow("Posts a day", self.per_day)
        times = QHBoxLayout()
        self.first = QTimeEdit(QTime.fromString(ps.first_post_time, "HH:mm"), displayFormat="HH:mm")
        self.last = QTimeEdit(QTime.fromString(ps.last_post_time, "HH:mm"), displayFormat="HH:mm")
        times.addWidget(self.first)
        times.addWidget(QLabel("to"))
        times.addWidget(self.last)
        times.addStretch(1)
        form.addRow("Between", times)
        self.warm = QCheckBox("Warm up new accounts for two weeks (recommended for accounts made this month)")
        self.warm.setChecked(True)
        form.addRow("", self.warm)
        pl.addLayout(form)

    def _save_schedule(self) -> None:
        ps = self.posting.settings
        ps.posts_per_day = self.per_day.value()
        ps.first_post_time = self.first.time().toString("HH:mm")
        ps.last_post_time = self.last.time().toString("HH:mm")
        try:
            ps.save()
        except OSError:
            pass
        for a in self.posting.accounts:
            a.posts_per_day, a.first_post_time, a.last_post_time = ps.posts_per_day, ps.first_post_time, \
                ps.last_post_time
            if not self.warm.isChecked():
                a.warm_up_start = 0.0
            elif not a.warm_up_start:
                a.start_warm_up()
        self.posting.save()

    # --- 7. start --------------------------------------------------------------------------------------------

    def _start(self, pl) -> None:
        pl.addWidget(_text("<p style='font-size:12pt'><b>Ready</b></p>"
                           "<p>Automation runs in the background, with the window closed too: it finds videos, "
                           "clips them, posts each category's clips to its account on the schedule, reads how they "
                           "do, writes a record of each post to Documents\\ClipMonster\\Posted videos and deletes "
                           "the clip once TikTok shows it. The Queue and Performance tabs show what it's doing; "
                           "updates install themselves.</p>"))
        self.with_windows = QCheckBox("Start with Windows")
        self.with_windows.setChecked(True)
        pl.addWidget(self.with_windows)
        self.go_now = QCheckBox("Start automation now")
        self.go_now.setChecked(True)
        pl.addWidget(self.go_now)

    def _finish(self) -> None:
        from .. import pipeline

        if self.with_windows.isChecked():
            why = pipeline.set_start_with_windows(True)
            if why and "only works on Windows" not in why:
                self.problems.append(f"Start with Windows didn't take: {why}")
        self.start_now = self.go_now.isChecked()
        self._set("setup_done", True)
        self.accept()

    def reject(self) -> None:  # Finish later: what was filled in so far is kept
        i = self.pages.currentIndex()
        if STEPS[i] in ("Keys", "First category", "Posting schedule"):
            self._leave(i)
        super().reject()

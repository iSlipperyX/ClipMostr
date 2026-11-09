"""Performance → Weekly report: the past 7 days on each TikTok account (weekly.py), and the reports
automation sent on Mondays."""

from __future__ import annotations

import datetime as dt
import html
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout

from .. import weekly
from . import theme


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _colour(text: str) -> str:
    if text.startswith(("Best post", "An older video took off")):
        return theme.GOOD
    if text.startswith(("Stats last read", "No stats yet")) or "can't earn" in text or "waiting for your OK" in text:
        return theme.WARN
    return theme.TEXT


def week_html(w: weekly.Week) -> str:
    rows = [f"<p style='font-size:13pt; margin:0 0 2px 0'><b>{_esc(w.headline())}</b></p>",
            f"<p style='color:{theme.MUTED}; margin:0 0 6px 0'>{_esc(w.span())}</p>"]
    for heading, lines in w.sections():
        rows.append(f"<p style='font-size:11.5pt; margin:12px 0 4px 0'><b>{_esc(heading)}</b></p>")
        for text, link in lines:
            body = _esc(text)
            if link:
                body += f" <a href='{html.escape(link, quote=True)}' style='color:{theme.ACCENT}'>watch</a>"
            rows.append(f"<div style='color:{_colour(text)}; margin:3px 0 3px 10px'>• {body}</div>")
    if w.empty:
        rows.append(f"<p style='color:{theme.MUTED}'>Nothing posted or made in these 7 days. Add a TikTok account in "
                    "the Accounts tab to see its views and followers here.</p>")
    return "".join(rows)


def saved_label(path: Path) -> str:
    """'Week to Mon 9 Nov' from week-2026-11-09.json."""
    try:
        day = dt.date.fromisoformat(path.stem.removeprefix("week-"))
    except ValueError:
        return path.stem
    return f"Week to {day.strftime('%a')} {day.day} {day.strftime('%b %Y')}"


class WeeklyDialog(QDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self._week: weekly.Week | None = None
        self.setWindowTitle("Weekly report")
        self.resize(660, 680)
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        head = QLabel("Weekly report")
        head.setObjectName("h2")
        self.which = QComboBox()
        self.which.addItem("The past 7 days", "")
        for path in weekly.saved():
            self.which.addItem(saved_label(path), str(path))
        self.which.currentIndexChanged.connect(lambda _i: self.show_week())
        top.addWidget(head)
        top.addStretch(1)
        top.addWidget(self.which)
        v.addLayout(top)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        v.addWidget(self.view, 1)
        note = QLabel("Automation sends this at 9 am every Monday (Settings → Automation → Weekly report). A week's "
                      "views come from a daily record of every video, kept from the first day of 3.5.0: until it "
                      "reaches back a week, older videos' views are estimates, marked \"about\".")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.MUTED}")
        v.addWidget(note)
        row = QHBoxLayout()
        self.copy_btn = QPushButton("Copy as text")
        self.copy_btn.setToolTip("The report as plain text, to paste anywhere")
        self.copy_btn.clicked.connect(self.copy)
        folder = QPushButton("Open the reports folder")
        folder.clicked.connect(self.open_folder)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(self.copy_btn)
        row.addWidget(folder)
        row.addStretch(1)
        row.addWidget(close)
        v.addLayout(row)
        self.show_week()

    def _live(self) -> weekly.Week:
        from ..jobs import JobDB

        db = JobDB()
        try:
            return weekly.build(self.state.store, self.state.accounts, db)
        finally:
            db.close()

    def show_week(self) -> None:
        path = self.which.currentData()
        try:
            w = weekly.load(Path(path)) if path else self._live()
        except Exception as exc:  # noqa: BLE001 - jobs.db busy for a moment, a stats file mid-write
            w = None
            why = str(exc)
        else:
            why = "the file couldn't be read"
        self._week = w
        self.copy_btn.setEnabled(w is not None)
        if w is None:
            self.view.setHtml(f"<p style='color:{theme.WARN}'>Couldn't show that report: {_esc(why)}.</p>")
            return
        self.view.setHtml(week_html(w))

    def copy(self) -> None:
        if self._week is not None:
            QGuiApplication.clipboard().setText(self._week.text())
            self.copy_btn.setText("Copied")

    def open_folder(self) -> None:
        where = weekly.folder()
        where.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(where)))

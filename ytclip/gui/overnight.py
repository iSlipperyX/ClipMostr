"""The Automation tab's Overnight page: the morning report (report.py) and the health check (doctor.py)."""

from __future__ import annotations

import json
import threading
import time
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from .. import doctor, report
from ..jobs import JobDB
from ..settings import Settings
from . import theme

SPANS = (("Last 12 hours", 12.0), ("Last 24 hours", 24.0), ("Last 7 days", 168.0))
MARKS = {doctor.GOOD: ("✓", theme.GOOD), doctor.WARN: ("⚠", theme.WARN), doctor.BAD: ("✗", theme.BAD)}


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class OvernightPanel(QWidget):
    checked = Signal(list)  # a health check that ran in the background finished (list of doctor.Check)
    open_settings = Signal(str)

    def __init__(self, settings: Callable[[], Settings], db: JobDB, parent=None):
        super().__init__(parent)
        self._settings, self.db = settings, db
        self._sig = None
        self._checking = False
        self._checks: list[doctor.Check] | None = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 6, 0, 0)
        top = QHBoxLayout()
        head = QLabel("What automation did")
        head.setObjectName("h2")
        self.span = QComboBox()
        for label, hours in SPANS:
            self.span.addItem(label, hours)
        self.span.currentIndexChanged.connect(lambda _i: self.refresh(force=True))
        top.addWidget(head)
        top.addStretch(1)
        top.addWidget(self.span)
        v.addLayout(top)
        self.report = QLabel()
        self.report.setTextFormat(Qt.RichText)
        self.report.setWordWrap(True)
        self.report.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.report.setTextInteractionFlags(Qt.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(self.report)
        v.addWidget(scroll, 3)

        row = QHBoxLayout()
        head2 = QLabel("Health check")
        head2.setObjectName("h2")
        self.check_btn = QPushButton("🩺  Check now")
        self.check_btn.setToolTip("Checks ffmpeg, the downloader, your keys (online), folders, free space, TikTok "
                                  "Manager, niches and requests. Automation does the offline part each time it starts")
        self.check_btn.clicked.connect(self.check_now)
        row.addWidget(head2)
        row.addStretch(1)
        row.addWidget(self.check_btn)
        v.addLayout(row)
        self.health = QLabel()
        self.health.setTextFormat(Qt.RichText)
        self.health.setWordWrap(True)
        self.health.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        v.addWidget(self.health, 2)
        self.checked.connect(self._show_checks)
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        try:
            r = report.build(self.db, self._settings(), hours=self.span.currentData())
        except Exception as exc:  # jobs.db busy for a moment: keep what's shown
            if force:
                self.report.setText(f"<span style='color:{theme.MUTED}'>Couldn't read the report: "
                                    f"{_esc(str(exc))}</span>")
            return
        lines = r.lines()
        if lines != self._sig or force:
            self._sig = lines
            rows = [f"<p style='font-size:12pt'><b>{_esc(lines[0])}</b></p>"]
            for line in lines[1:]:
                colour = (theme.BAD if line.startswith("Failed") else
                          theme.WARN if line.startswith(("YouTube", "Paused", "Stopped", "Rejected")) else theme.TEXT)
                rows.append(f"<div style='color:{colour}; margin:3px 0'>• {_esc(line)}</div>")
            self.report.setText("".join(rows))
        if self._checks is None:
            self._show_saved()

    def _show_saved(self) -> None:
        """What automation's own check found when it last started."""
        raw = self.db.meta("health")
        at = self.db.meta_float("health_at")
        if not raw:
            self.health.setText(f"<span style='color:{theme.MUTED}'>Automation checks itself each time it starts. "
                                "Press Check now for a full check.</span>")
            return
        try:
            found = [doctor.Check(**d) for d in json.loads(raw)]
        except (ValueError, TypeError):
            found = []
        when = time.strftime("%a %H:%M", time.localtime(at)) if at else "its last start"
        if not found:
            self.health.setText(f"<span style='color:{theme.GOOD}'>✓ All good when automation started "
                                f"({when}).</span>")
            return
        self.health.setText(f"<span style='color:{theme.MUTED}'>When automation started ({when}):</span><br>"
                            + self._rows(found))

    @staticmethod
    def _rows(checks: list[doctor.Check]) -> str:
        out = []
        for c in checks:
            mark, colour = MARKS[c.level]
            fix = f"<br><span style='color:{theme.MUTED}'>&nbsp;&nbsp;&nbsp;&nbsp;Fix: {_esc(c.fix)}</span>" if c.fix else ""
            out.append(f"<div style='margin:3px 0'><span style='color:{colour}'>{mark}</span> <b>{_esc(c.name)}</b>: "
                       f"{_esc(c.detail)}{fix}</div>")
        return "".join(out)

    def check_now(self, online: bool = True) -> None:
        if self._checking:
            return
        self._checking = True
        self.check_btn.setEnabled(False)
        self.check_btn.setText("Checking…")
        s = self._settings()

        def work() -> None:
            try:
                found = doctor.check(s, self.db, online=online)
            except Exception as exc:
                found = [doctor.Check("Health check", doctor.BAD, f"Couldn't run: {exc}")]
            self.checked.emit(found)

        threading.Thread(target=work, daemon=True).start()

    def _show_checks(self, checks: list) -> None:
        self._checking = False
        self._checks = checks
        self.check_btn.setEnabled(True)
        self.check_btn.setText("🩺  Check now")
        bad = sum(c.level == doctor.BAD for c in checks)
        warn = sum(c.level == doctor.WARN for c in checks)
        summary = ("All good." if not (bad or warn) else
                   f"{bad} problem{'s' if bad != 1 else ''}, {warn} warning{'s' if warn != 1 else ''}.")
        self.health.setText(f"<b>{summary}</b>" + self._rows(sorted(checks, key=lambda c: (c.level == doctor.GOOD,
                                                                                        c.level == doctor.WARN))))

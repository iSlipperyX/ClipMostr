"""The Creator tab: an original video from a prompt (or your own script), made on this PC.

On the left, what to make: the prompt or your script, the category (its folder and TikTok account), the
voice (▶ plays a sample), the length, the look and where the pictures come from. On the right, the
storyboard: "Write storyboard" fills it in so you can read and change every line, picture and camera move
before anything is rendered; "Make video" renders what's there (writing it first when it's empty). The
steps and a bar show where it is; when it's done the video plays here and goes to its account's queue.
"Add to automation queue" leaves the making to automation, in turn with the clips.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QRadioButton,
                               QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import categories, steps
from ..settings import CREATOR_STYLES, CREATOR_VISUALS, Settings, work_dir
from . import theme
from .status_bars import LabeledBar, StepChips

COLUMNS = ("#", "Narration", "Picture", "Camera", "Sound")
EXAMPLES = ("3 stoic habits that will change your life", "The unsolved mystery of the Voynich Manuscript",
            "Dark psychology tricks people use on you without you noticing")


class Worker(QThread):
    """Runs one step of the Creator (write, make, preview) off the window's thread."""

    log = Signal(str)
    progress = Signal(str, float)  # message, 0-1 or -1 when it can't tell
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[["Worker"], Any]):
        super().__init__()
        self.fn = fn
        self.cancel_requested = False

    def run(self) -> None:
        from ..ffmpeg import Cancelled

        try:
            self.done.emit(self.fn(self))
        except Cancelled:
            self.failed.emit("Cancelled")
        except Exception as exc:  # noqa: BLE001
            self.log.emit(traceback.format_exc().strip().splitlines()[-1])
            self.failed.emit(str(exc) or exc.__class__.__name__)


class CreatorView(QWidget):
    notice = Signal(str)
    open_settings = Signal(str)

    def __init__(self, settings: Callable[[], Settings], change: Callable[[str, Any], None] | None = None,
                 parent=None):
        super().__init__(parent)
        self._settings = settings
        self._change = change
        self.board = None  # director.Storyboard on show
        self.worker: Worker | None = None
        self.tracker: steps.Tracker | None = None
        self.result = None
        self._filling = False
        self._player = None
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_right())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([420, 760])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)
        self.show_board(None)
        self.reload()

    # ------------------------------------------------------------ building
    def _build_left(self) -> QWidget:
        from ..creator import voice_engine

        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(4, 4, 10, 4)
        v.setSpacing(10)
        head = QLabel("Make a video")
        head.setObjectName("h2")
        v.addWidget(head)
        sub = QLabel("An original video from an idea: the script, the voice, the pictures, the music and the captions "
                     "are all made on this PC.")
        sub.setWordWrap(True)
        sub.setProperty("muted", True)
        v.addWidget(sub)
        modes = QHBoxLayout()
        self.mode_ai = QRadioButton("Write it for me")
        self.mode_script = QRadioButton("Use my script")
        self.mode_ai.setChecked(True)
        self.mode_ai.toggled.connect(self._mode_changed)
        modes.addWidget(self.mode_ai)
        modes.addWidget(self.mode_script)
        modes.addStretch(1)
        v.addLayout(modes)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("What's it about? For example:\n" + "\n".join(EXAMPLES))
        self.prompt.setFixedHeight(92)
        v.addWidget(self.prompt)
        self.script = QPlainTextEdit()
        self.script.setPlaceholderText("Paste your narration. The first sentence is the hook; a blank line starts a "
                                       "new scene.")
        self.script.setVisible(False)
        self.script.setMinimumHeight(150)
        v.addWidget(self.script)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        self.category = QComboBox()
        self.category.setToolTip("Its folder in Automation, and the TikTok account it's queued for")
        form.addRow("Category", self.category)
        voice_row = QHBoxLayout()
        self.voice = QComboBox()
        for prof in voice_engine.VOICES.values():
            self.voice.addItem(prof.label, prof.key)
            self.voice.setItemData(self.voice.count() - 1, prof.blurb, Qt.ToolTipRole)
        self.play_voice = QPushButton("▶")
        self.play_voice.setFixedWidth(36)
        self.play_voice.setToolTip("Hear this voice")
        self.play_voice.clicked.connect(self.preview_voice)
        voice_row.addWidget(self.voice, 1)
        voice_row.addWidget(self.play_voice)
        form.addRow("Voice", voice_row)
        length_row = QHBoxLayout()
        self.seconds = QSpinBox()
        self.seconds.setRange(20, 180)
        self.seconds.setSuffix(" s")
        self.rewards = QCheckBox("Over a minute (Creator Rewards)")
        self.rewards.setToolTip("TikTok's Creator Rewards only pays for videos over one minute: kept at 62-85 s")
        self.rewards.toggled.connect(self._rewards_changed)
        length_row.addWidget(self.seconds)
        length_row.addWidget(self.rewards, 1)
        form.addRow("Length", length_row)
        self.style = QComboBox()
        for key, label in CREATOR_STYLES:
            self.style.addItem(label, key)
        form.addRow("Look", self.style)
        self.visuals = QComboBox()
        for key, label in CREATOR_VISUALS:
            self.visuals.addItem(label, key)
        form.addRow("Pictures", self.visuals)
        v.addLayout(form)
        self.ready = QLabel()
        self.ready.setWordWrap(True)
        self.ready.setProperty("muted", True)
        self.ready.setTextFormat(Qt.RichText)
        self.ready.setOpenExternalLinks(False)
        self.ready.linkActivated.connect(lambda _l: self.open_settings.emit("Creator"))
        v.addWidget(self.ready)
        buttons = QHBoxLayout()
        self.write_btn = QPushButton("Write storyboard")
        self.write_btn.setToolTip("The script and scenes only, to read and change before anything is made")
        self.write_btn.clicked.connect(self.write)
        self.make_btn = QPushButton("Make video")
        self.make_btn.setObjectName("primary")
        self.make_btn.clicked.connect(self.make)
        buttons.addWidget(self.write_btn)
        buttons.addWidget(self.make_btn, 1)
        v.addLayout(buttons)
        self.queue_btn = QPushButton("Add to automation queue")
        self.queue_btn.setToolTip("Automation makes it in turn, alongside the clips (start it in the Automation tab)")
        self.queue_btn.clicked.connect(self.add_to_queue)
        v.addWidget(self.queue_btn)
        v.addStretch(1)
        # What's picked here sticks until the app restarts; untouched boxes follow Settings → Creator.
        for w in (self.category, self.voice, self.style, self.visuals):
            w.activated.connect(lambda _i, w=w: w.setProperty("touched", True))
        self.visuals.activated.connect(lambda _i: self._readiness())
        self.seconds.editingFinished.connect(lambda: self.seconds.setProperty("touched", True))
        self.voice.currentIndexChanged.connect(lambda _i: self._count())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(box)
        scroll.setMinimumWidth(380)
        return scroll

    def _build_right(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 4, 4, 4)
        v.setSpacing(8)
        self.status_panel = QFrame()
        self.status_panel.setObjectName("panel")
        sp = QVBoxLayout(self.status_panel)
        sp.setContentsMargins(12, 8, 12, 9)
        sp.setSpacing(5)
        top = QHBoxLayout()
        self.status_title = QLabel()
        self.status_title.setStyleSheet("font-weight:600;")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel)
        top.addWidget(self.status_title, 1)
        top.addWidget(self.cancel_btn)
        sp.addLayout(top)
        self.chips = StepChips()
        sp.addWidget(self.chips)
        self.bar = LabeledBar()
        sp.addWidget(self.bar)
        self.detail = QLabel()
        self.detail.setProperty("muted", True)
        sp.addWidget(self.detail)
        self.status_panel.setVisible(False)
        v.addWidget(self.status_panel)
        title_row = QHBoxLayout()
        label = QLabel("Storyboard")
        label.setObjectName("h2")
        self.title = QLineEdit()
        self.title.setPlaceholderText("The video's title")
        self.title.textEdited.connect(self._title_edited)
        self.count = QLabel()
        self.count.setProperty("muted", True)
        title_row.addWidget(label)
        title_row.addWidget(self.title, 1)
        title_row.addWidget(self.count)
        v.addLayout(title_row)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("panel")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.Stretch)
        head.setSectionResizeMode(2, QHeaderView.Interactive)
        self.table.setColumnWidth(2, 340)
        self.table.setStyleSheet("QTableWidget::item { padding: 6px 4px; }")
        head.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        head.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._cell_edited)
        self.empty = QLabel("Write a storyboard to see the script scene by scene: what the narrator says, the picture, "
                            "the camera move and the sound. Double-click anything to change it.")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setProperty("muted", True)
        v.addWidget(self.table, 3)
        v.addWidget(self.empty, 3)
        self.done_panel = QFrame()
        self.done_panel.setObjectName("panel")
        dp = QHBoxLayout(self.done_panel)
        dp.setContentsMargins(12, 8, 12, 8)
        self.done_text = QLabel()
        self.done_text.setWordWrap(True)
        self.done_text.setTextFormat(Qt.RichText)
        self.play_btn = QPushButton("▶  Play")
        self.play_btn.clicked.connect(self.play_result)
        self.folder_btn = QPushButton("Show in folder")
        self.folder_btn.clicked.connect(self.show_folder)
        dp.addWidget(self.done_text, 1)
        dp.addWidget(self.play_btn)
        dp.addWidget(self.folder_btn)
        self.done_panel.setVisible(False)
        v.addWidget(self.done_panel)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(400)
        self.log_box.setFixedHeight(110)
        self.log_box.setPlaceholderText("What it's doing shows here.")
        v.addWidget(self.log_box)
        return box

    # ------------------------------------------------------------ state
    def reload(self) -> None:
        """Pick up the settings and categories again (they may have changed in another tab)."""
        s = self._settings()
        keep = self.category.currentData() if self.category.property("touched") else None
        self.category.blockSignals(True)
        self.category.clear()
        for c in categories.load(s):
            account = f"  ·  {c.account}" if c.account else "  ·  no account yet"
            self.category.addItem(f"{c.name}{account}", c.id)
        self.category.addItem("No category (just save it)", "")
        i = self.category.findData(keep) if keep is not None else -1
        self.category.setCurrentIndex(i if i >= 0 else 0)
        self.category.blockSignals(False)
        for combo, value in ((self.voice, s.creator_voice), (self.style, s.creator_style),
                             (self.visuals, s.creator_visuals)):
            if combo.property("touched"):
                continue
            j = combo.findData(value)
            if j >= 0:
                combo.setCurrentIndex(j)
        if not self.seconds.property("touched"):
            self.seconds.setValue(int(s.creator_duration))
            self.rewards.setChecked(bool(s.creator_rewards_length))
        self._readiness()

    def _readiness(self) -> None:
        """One line on what's ready here, linking to Settings → Creator."""
        from ..creator import director, visual_generator, voice_engine

        s = self._settings()
        engines = voice_engine.pick_engines(s)
        voice = voice_engine.ENGINE_NAMES.get(engines[0], "") if engines else ""
        writer = director.script_writers(s)
        pics = self.visuals.currentData() or s.creator_visuals
        pic_text = visual_generator.BACKEND_NAMES.get(pics, "the best available")
        bits = [f"Voice: <b>{voice or 'none installed'}</b>", f"Pictures: <b>{pic_text}</b>",
                "Script: <b>" + ("the local AI" if writer == ["local"] else ", ".join(writer) if writer else
                                 "your own (no AI ready)") + "</b>"]
        self.ready.setText(" · ".join(bits) + " · <a href='settings'>Creator settings</a>")
        self.ready.setToolTip("" if writer else director.no_writer_text(s))

    def _mode_changed(self) -> None:
        mine = self.mode_script.isChecked()
        self.script.setVisible(mine)
        self.prompt.setPlaceholderText("Optional: a word on what it's about (for the hashtags and the look)" if mine
                                       else "What's it about? For example:\n" + "\n".join(EXAMPLES))
        self.prompt.setFixedHeight(48 if mine else 92)

    def _rewards_changed(self, on: bool) -> None:
        if on and not 62 <= self.seconds.value() <= 85:
            self.seconds.setValue(65)

    def request(self):
        from ..creator.orchestrator import CreateRequest

        return CreateRequest(prompt=self.prompt.toPlainText().strip(),
                             script=self.script.toPlainText() if self.mode_script.isChecked() else "",
                             title=self.title.text().strip() if self.board is not None else "",
                             category=self.category.currentData() or "", voice=self.voice.currentData() or "",
                             seconds=float(self.seconds.value()), visuals=self.visuals.currentData() or "",
                             style=self.style.currentData() or "", rewards=self.rewards.isChecked(),
                             storyboard=self.board.to_json() if self.board is not None else None)

    def _check_input(self) -> str:
        if self.mode_script.isChecked() and not self.script.toPlainText().strip() and self.board is None:
            return "Paste your script first."
        if not self.mode_script.isChecked() and not self.prompt.toPlainText().strip() and self.board is None:
            return "Say what the video is about first."
        return ""

    # ------------------------------------------------------------ the storyboard table
    def show_board(self, board) -> None:
        self.board = board
        self._filling = True
        self.table.setRowCount(0)
        if board is not None:
            self.title.setText(board.title)
            self.table.setRowCount(len(board.scenes))
            for r, sc in enumerate(board.scenes):
                cells = [str(sc.scene_id), sc.speech_text, sc.visual_prompt, sc.camera_motion.replace("_", " "),
                         "" if sc.sfx_cue == "none" else sc.sfx_cue.replace("_", " ")]
                for c, text in enumerate(cells):
                    it = QTableWidgetItem(text)
                    if c == 0:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    if c in (3, 4):
                        it.setToolTip("Camera: " + ", ".join(m.replace("_", " ") for m in _motions()) if c == 3 else
                                      "Sound: " + ", ".join(x.replace("_", " ") for x in _cues()))
                    self.table.setItem(r, c, it)
            self.table.resizeRowsToContents()
        self._filling = False
        self.table.setVisible(board is not None)
        self.empty.setVisible(board is None)
        self._count()

    def _count(self) -> None:
        if self.board is None:
            self.count.setText("")
            return
        from ..creator import voice_engine

        wpm = voice_engine.learned_wpm(self.voice.currentData() or "")
        words = self.board.word_count
        self.count.setText(f"{len(self.board.scenes)} scenes · {words} words · about {words / wpm * 60:.0f} s")

    def _cell_edited(self, item: QTableWidgetItem) -> None:
        if self._filling or self.board is None:
            return
        r, c = item.row(), item.column()
        if r >= len(self.board.scenes):
            return
        sc = self.board.scenes[r]
        text = item.text().strip()
        if c == 1:
            sc.speech_text = text
        elif c == 2:
            sc.visual_prompt = text
        elif c == 3:
            sc.camera_motion = text.replace(" ", "_").lower()
        elif c == 4:
            sc.sfx_cue = text.replace(" ", "_").lower() or "none"
        self.table.resizeRowToContents(r)
        self._count()

    def _title_edited(self, text: str) -> None:
        if self.board is not None:
            self.board.title = text.strip() or self.board.title

    # ------------------------------------------------------------ running
    def _start(self, fn, title: str, plan: list[str] | None) -> None:
        self.worker = Worker(fn)
        self.tracker = steps.Tracker(plan)
        self.worker.log.connect(self._log)
        self.worker.progress.connect(self._progress)
        self.status_title.setText(title)
        self.chips.setVisible(bool(plan))
        self.chips.show_status(None)
        self.bar.set_fraction(None)
        self.detail.setText("Starting")
        self.status_panel.setVisible(True)
        self.done_panel.setVisible(False)
        self._busy(True)
        self.worker.start()

    def _busy(self, on: bool) -> None:
        for w in (self.write_btn, self.make_btn, self.queue_btn, self.play_voice):
            w.setEnabled(not on)
        self.cancel_btn.setVisible(on)

    def _log(self, text: str) -> None:
        self.log_box.appendPlainText(text)

    def _progress(self, msg: str, frac: float) -> None:
        status = self.tracker.update(msg, frac) if self.tracker else None
        if status is not None and status.plan:
            self.chips.show_status(status)
            self.bar.set_status(status)
        else:
            self.bar.set_fraction(None if frac < 0 else frac)
        self.detail.setText(msg + (f" {frac:.0%}" if frac >= 0 and "%" not in msg else ""))

    def _orchestrator(self, w: Worker):
        from ..creator.orchestrator import VideoCreatorOrchestrator

        return VideoCreatorOrchestrator(self._settings(), log=w.log.emit,
                                        progress=lambda m, f: w.progress.emit(m, -1.0 if f is None else float(f)),
                                        cancelled=lambda: w.cancel_requested)

    def write(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        self.board = None
        problem = self._check_input()
        if problem:
            self.notice.emit(problem)
            return
        req = self.request()

        def run(w: Worker):
            return self._orchestrator(w).storyboard(req)

        self._start(run, "✍ Writing the storyboard", ["script"])
        self.worker.done.connect(self._written)
        self.worker.failed.connect(self._failed)

    def _written(self, board) -> None:
        self._finish()
        self.show_board(board)
        self._log(f"Storyboard: \"{board.title}\", {len(board.scenes)} scenes. Change anything, then Make video.")

    def make(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        problem = self._check_input()
        if problem:
            self.notice.emit(problem)
            return
        req = self.request()

        def run(w: Worker):
            return self._orchestrator(w).create(req)

        self._start(run, f"✨ Making {req.label()}", list(steps.CREATOR_PLAN))
        self.worker.done.connect(self._made)
        self.worker.failed.connect(self._failed)

    def _made(self, result) -> None:
        self._finish()
        self.result = result
        self.show_board(result.storyboard)
        c = result.checks
        where = (f"queued for <b>{_esc(result.account)}</b>" if result.account else
                 "in the Automation folder" if self.request().post else "saved")
        warn = "".join(f"<br><span style='color:{theme.WARN}'>⚠ {_esc(w)}</span>" for w in result.warnings)
        self.done_text.setText(f"<b>✓ {_esc(result.storyboard.title)}</b> · {result.duration:.0f} s · "
                               f"{c.get('lufs')} LUFS · {c.get('size_mb')} MB · {where}{warn}")
        self.done_panel.setVisible(True)
        self.notice.emit(f"Made \"{result.storyboard.title}\" ({result.duration:.0f} s).")

    def _failed(self, error: str) -> None:
        self._finish()
        if error == "Cancelled":
            self._log("Cancelled.")
            return
        self._log(f"✗ {error}")
        self.detail.setText(error)
        self.status_panel.setVisible(True)
        self.status_title.setText("✗ Couldn't finish")
        self.notice.emit(error)

    def _finish(self) -> None:
        self._busy(False)
        self.status_panel.setVisible(False)

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel_requested = True
            self.detail.setText("Stopping at the next step…")

    def add_to_queue(self) -> None:
        from ..creator.orchestrator import queue
        from ..jobs import JobDB

        problem = self._check_input()
        if problem:
            self.notice.emit(problem)
            return
        req = self.request()
        db = JobDB()
        try:
            queue(db, req, req.category)
        finally:
            db.close()
        self._log(f"Queued \"{req.label()}\" for automation to make.")
        self.notice.emit("Added to automation's queue: it's made in turn while automation runs.")

    # ------------------------------------------------------------ playing
    def _play(self, path: Path, video: bool) -> None:
        if video:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

        if self._player is None:
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()

    def play_result(self) -> None:
        if self.result is not None:
            self._play(self.result.video, True)

    def show_folder(self) -> None:
        if self.result is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.result.folder)))

    def preview_voice(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        from ..creator import voice_engine

        key = self.voice.currentData() or ""
        out = work_dir() / f"voice-sample-{key}.wav"

        def run(w: Worker):
            return voice_engine.preview(self._settings(), key, out, log=w.log.emit,
                                        progress=lambda m, f: w.progress.emit(m, -1.0 if f is None else float(f)))

        self._start(run, "▶ Making a voice sample", None)
        self.worker.done.connect(lambda track: (self._finish(), self._play(track.path, False),
                                                self._log(f"{track.voice} ({track.engine}).")))
        self.worker.failed.connect(self._failed)


def _motions() -> tuple[str, ...]:
    from ..creator.director import MOTIONS

    return MOTIONS


def _cues() -> tuple[str, ...]:
    from ..creator.director import SFX_CUES

    return SFX_CUES


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

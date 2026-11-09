"""Videos: everything waiting to post, newest needs first. Pick one to watch it (streamed from the PC), change
its caption, and OK it, post it now, try it again or delete it from the PC."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSlider,
                               QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from ..gui import theme
from . import common as c
from .paths import cache_dir

ITEM = Qt.UserRole + 1
ROW_H = 86
THUMB_W, THUMB_H = 42, 74
FILTERS = (("", "All"), ("review", "Needs your OK"), ("queued", "Queued"), ("failed", "Didn't post"),
           ("library", "Not queued"))


# --- pictures -------------------------------------------------------------------------------------------------


class Thumbs(QObject):
    """Each video's picture, from the PC once and then from a cache on the Mac."""

    ready = Signal(str)

    def __init__(self, session_getter, parent=None, kind: str = "v"):
        super().__init__(parent)
        self._session = session_getter
        self.kind = kind  # v: posting's videos; d: clips sent to the Macs (inbox.py)
        self.pix: dict[str, QPixmap] = {}
        self._asked: set[str] = set()

    def folder(self) -> Path:
        p = cache_dir() / "thumbs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def key(self, v: dict) -> str:
        s = self._session()
        host = s.host.id if s else ""
        kind = "" if self.kind == "v" else f"{self.kind}:"
        return hashlib.sha1(f"{host}:{kind}{v['id']}:{v.get('thumb_v', 0)}".encode()).hexdigest()[:20]

    def get(self, v: dict) -> QPixmap | None:
        k = self.key(v)
        if k in self.pix:
            return self.pix[k]
        f = self.folder() / f"{k}.jpg"
        if f.is_file():
            pm = QPixmap(str(f))
            if not pm.isNull():
                self.pix[k] = pm
                return pm
        if k not in self._asked and v.get("has_thumb"):
            self._asked.add(k)
            s = self._session()
            if s is not None:
                def ok(data, k=k, vid=v["id"]):
                    pm = QPixmap()
                    if data and pm.loadFromData(data):
                        self.pix[k] = pm
                        try:
                            (self.folder() / f"{k}.jpg").write_bytes(data)
                        except OSError:
                            pass
                        self.ready.emit(vid)

                def path(cl, vid):
                    return cl.video_path(vid, "thumb") if self.kind == "v" else cl.delivery_path(vid, "thumb")

                s.call(lambda cl, vid=v["id"]: cl.fetch(path(cl, vid), 2 * 1024 * 1024), ok, lambda _e: None)
        return None


def _rounded(p: QPainter, pm: QPixmap | None, r: QRect, radius: float = 6) -> None:
    path = QPainterPath()
    path.addRoundedRect(QRectF(r), radius, radius)
    p.save()
    p.setClipPath(path)
    p.fillRect(r, QColor(theme.PANEL_2))
    if pm is not None and not pm.isNull():
        scaled = pm.scaled(r.size() * 2, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(2.0)
        sw, sh = scaled.width() / 2, scaled.height() / 2
        p.drawPixmap(int(r.x() - (sw - r.width()) / 2), int(r.y() - (sh - r.height()) / 2), scaled)
    else:
        p.setPen(QColor(theme.MUTED))
        p.drawText(r, Qt.AlignCenter, "▶")
    p.restore()


class _Delegate(QStyledItemDelegate):
    def __init__(self, thumbs: Thumbs, parent=None):
        super().__init__(parent)
        self.thumbs = thumbs

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), ROW_H)

    def paint(self, p: QPainter, option, index) -> None:
        v = index.data(ITEM) or {}
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(6, 3, -6, -3)
        if option.state & QStyle.State_Selected:
            p.setPen(Qt.NoPen)
            p.setBrush(_css(theme.soft(alpha=0.24 if theme.is_dark() else 0.14)))
            p.drawRoundedRect(QRectF(r), 8, 8)
        elif option.state & QStyle.State_MouseOver:
            col = QColor(theme.TEXT)
            col.setAlphaF(0.05)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(r), 8, 8)
        thumb = QRect(r.x() + 8, r.y() + (r.height() - THUMB_H) // 2, THUMB_W, THUMB_H)
        _rounded(p, self.thumbs.get(v), thumb)
        x = thumb.right() + 12
        right = r.right() - 10
        # the state chip
        label, _ = c.STATES.get(v.get("state"), ("", "MUTED"))
        f = QFont(option.font)
        cf = QFont(f)
        cf.setPointSizeF(max(7.0, f.pointSizeF() * 0.82))
        cf.setWeight(QFont.DemiBold)
        p.setFont(cf)
        cw = p.fontMetrics().horizontalAdvance(label) + 16
        chip = QRect(right - cw, r.y() + 12, cw, 18)
        col = QColor(c.state_colour(v.get("state", "")))
        bg = QColor(col)
        bg.setAlphaF(0.16)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(chip), 9, 9)
        p.setPen(col)
        p.drawText(chip, Qt.AlignCenter, label)
        # headline and the line under it
        hf = QFont(f)
        hf.setWeight(QFont.DemiBold)
        p.setFont(hf)
        p.setPen(QColor(theme.TEXT))
        hr = QRect(x, r.y() + 10, chip.left() - 8 - x, 22)
        p.drawText(hr, Qt.AlignLeft | Qt.AlignVCenter, p.fontMetrics().elidedText(v.get("headline", ""),
                                                                                  Qt.ElideRight, hr.width()))
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        now = time.time()
        if v.get("state") == "review":
            when = "Waits for your OK"
        elif v.get("state") == "failed":
            when = c.trim(v.get("last_error") or "Didn't post", 60)
        elif v.get("planned_at"):
            when = c.when(v["planned_at"], now)
        else:
            when = ""
        meta = " · ".join(b for b in (v.get("account_handle") or "no account", v.get("category"), when) if b)
        mr = QRect(x, r.y() + 33, right - x, 20)
        p.drawText(mr, Qt.AlignLeft | Qt.AlignVCenter, p.fontMetrics().elidedText(meta, Qt.ElideRight, mr.width()))
        extra = " · ".join(b for b in (c.length(v.get("duration")), "AI-made" if v.get("ai_generated") else "",
                                        v.get("part"), f"score {v['score']:.0f}" if v.get("score") else "") if b)
        er = QRect(x, r.y() + 53, right - x, 18)
        p.drawText(er, Qt.AlignLeft | Qt.AlignVCenter, p.fontMetrics().elidedText(extra, Qt.ElideRight, er.width()))
        p.restore()


def _css(css: str) -> QColor:
    if css.startswith("rgba("):
        r, g, b, a = (x.strip() for x in css[5:-1].split(","))
        col = QColor(int(r), int(g), int(b))
        col.setAlphaF(float(a))
        return col
    return QColor(css)


# --- the player ---------------------------------------------------------------------------------------------------


class Player(QWidget):
    """The video, streamed from the PC through the Mac-only proxy. Without Qt Multimedia it shows the picture."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.stack = QStackedWidget()
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignCenter)
        self.picture.setStyleSheet(f"background: #000; border-radius: {theme.RADIUS}px; color: {theme.MUTED};")
        self.stack.addWidget(self.picture)
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget

            self.video = QVideoWidget()
            self.video.setStyleSheet("background: #000;")
            self.audio = QAudioOutput(self)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.player.positionChanged.connect(self._pos)
            self.player.durationChanged.connect(self._dur)
            self.player.playbackStateChanged.connect(self._state)
            self.player.errorOccurred.connect(self._error)
            self.stack.addWidget(self.video)
        except Exception:  # noqa: BLE001 - no multimedia: pictures only
            self.player = None
        self.stack.setFixedSize(270, 480)  # 9:16, like the phone it's made for
        v.addWidget(self.stack, 0, Qt.AlignHCenter)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.play_btn = QPushButton("▶  Play")
        self.play_btn.setMinimumWidth(92)
        self.play_btn.clicked.connect(self.toggle)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(lambda ms: self.player and self.player.setPosition(ms))
        self.time = QLabel("0:00")
        self.time.setProperty("muted", True)
        self.mute = QPushButton("🔊")
        self.mute.setObjectName("flat")
        self.mute.setCheckable(True)
        self.mute.toggled.connect(self._mute)
        bar.addWidget(self.play_btn)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.time)
        bar.addWidget(self.mute)
        v.addLayout(bar)
        self.url = ""
        self._dur_ms = 0
        if self.player is None:
            for w in (self.play_btn, self.slider, self.mute):
                w.setEnabled(False)
            self.time.setText("Playing videos needs Qt Multimedia")

    def show_picture(self, pm: QPixmap | None, text: str = "") -> None:
        self.stack.setCurrentWidget(self.picture)
        if pm is not None and not pm.isNull():
            ratio = self.devicePixelRatioF() or 1.0
            scaled = pm.scaled(self.stack.size() * ratio, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(ratio)
            self.picture.setPixmap(scaled)
        else:
            self.picture.setPixmap(QPixmap())
            self.picture.setText(text)

    def load(self, url: str, pm: QPixmap | None) -> None:
        self.stop()
        self.url = url
        self.show_picture(pm, "▶")
        self.slider.setRange(0, 0)
        self.time.setText("0:00" if self.player else self.time.text())
        self.play_btn.setText("▶  Play")

    def toggle(self) -> None:
        if self.player is None or not self.url:
            return
        from PySide6.QtMultimedia import QMediaPlayer

        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            return
        if self.player.source().toString() != self.url:
            self.player.setSource(QUrl(self.url))
        self.stack.setCurrentWidget(self.video)
        self.player.play()

    def stop(self) -> None:
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        self.stack.setCurrentWidget(self.picture)

    def _mute(self, on: bool) -> None:
        if self.player is not None:
            self.audio.setMuted(on)
        self.mute.setText("🔇" if on else "🔊")

    def _pos(self, ms: int) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        self.time.setText(f"{c.length(ms / 1000) or '0:00'} / {c.length(self._dur_ms / 1000) or '0:00'}")

    def _dur(self, ms: int) -> None:
        self._dur_ms = ms
        self.slider.setRange(0, ms)

    def _state(self, state) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        self.play_btn.setText("❚❚  Pause" if state == QMediaPlayer.PlayingState else "▶  Play")

    def _error(self, *_a) -> None:
        if self.player is not None and self.player.errorString():
            self.time.setText(c.trim(f"Can't play it: {self.player.errorString()}", 60))


# --- one video ---------------------------------------------------------------------------------------------------


class Detail(QScrollArea):
    act = Signal(str, str, dict)  # action, video id, extra (caption/hashtags)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumWidth(340)
        body = QWidget()
        body.setObjectName("page")
        self.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(16, 14, 20, 18)
        v.setSpacing(10)
        self.empty = c.WrapLabel("Pick a video to watch it, OK it, post it now or delete it.", muted=True)
        v.addWidget(self.empty)
        self.box = QWidget()
        b = QVBoxLayout(self.box)
        b.setContentsMargins(0, 0, 0, 0)
        b.setSpacing(10)
        self.player = Player()
        b.addWidget(self.player)
        top = QHBoxLayout()
        self.headline = c.WrapLabel("")
        self.headline.setStyleSheet("font-size: 13pt; font-weight: 700;")
        self.chip = c.Chip()
        top.addWidget(self.headline, 1)
        top.addWidget(self.chip, 0, Qt.AlignTop)
        b.addLayout(top)
        self.meta = c.WrapLabel("", muted=True)
        b.addWidget(self.meta)
        self.state_text = c.WrapLabel("")
        b.addWidget(self.state_text)
        self.source = c.WrapLabel("", muted=True)
        b.addWidget(self.source)
        self.warnings = c.WrapLabel("")
        b.addWidget(self.warnings)

        acts = QHBoxLayout()
        acts.setSpacing(8)
        self.ok_btn = QPushButton("✓  OK to post")
        self.ok_btn.setObjectName("primary")
        self.ok_btn.clicked.connect(lambda: self._do("ok"))
        self.now_btn = QPushButton("🚀  Post now")
        self.now_btn.clicked.connect(self._post_now)
        self.retry_btn = QPushButton("↻  Try again")
        self.retry_btn.clicked.connect(lambda: self._do("retry"))
        self.del_btn = QPushButton("🗑  Delete…")
        self.del_btn.clicked.connect(self._delete)
        self.tt_btn = QPushButton("Open on TikTok")
        self.tt_btn.clicked.connect(lambda: self.v and QDesktopServices.openUrl(QUrl(self.v.get("tiktok_url"))))
        for btn in (self.ok_btn, self.now_btn, self.retry_btn, self.del_btn, self.tt_btn):
            acts.addWidget(btn)
        acts.addStretch(1)
        b.addLayout(acts)

        b.addWidget(c.h2("Caption"))
        self.caption = QPlainTextEdit()
        self.caption.setFixedHeight(84)
        self.caption.textChanged.connect(self._changed)
        b.addWidget(self.caption)
        b.addWidget(c.h2("Hashtags"))
        self.hashtags = QLineEdit()
        self.hashtags.setPlaceholderText("#fyp")
        self.hashtags.textChanged.connect(self._changed)
        b.addWidget(self.hashtags)
        srow = QHBoxLayout()
        self.save_btn = QPushButton("Save caption")
        self.save_btn.clicked.connect(self._save)
        self.save_note = QLabel("")
        self.save_note.setProperty("muted", True)
        srow.addWidget(self.save_btn)
        srow.addWidget(self.save_note, 1)
        b.addLayout(srow)
        b.addStretch(1)
        v.addWidget(self.box)
        v.addStretch(1)
        self.box.hide()
        self.v: dict | None = None
        self._loading = False

    def show_video(self, v: dict | None, url: str = "", pm: QPixmap | None = None, keep_player: bool = False) -> None:
        same = self.v is not None and v is not None and v["id"] == self.v["id"]
        dirty = same and self.save_btn.isEnabled()
        self.v = v
        self.empty.setVisible(v is None)
        self.box.setVisible(v is not None)
        if v is None:
            self.player.stop()
            return
        if not (same and keep_player):
            self.player.load(url, pm)
        now = time.time()
        self.headline.setText(c.esc(v.get("headline")))
        label, _ = c.STATES.get(v.get("state"), ("", "MUTED"))
        self.chip.set(label, c.state_colour(v.get("state", "")))
        self.meta.setText(" · ".join(c.esc(b) for b in (
            v.get("account_handle") or "no account yet", v.get("category"), v.get("part"), c.length(v.get("duration")),
            f"score {v['score']:.0f}" if v.get("score") else "", c.size(v.get("size")),
            "AI-generated, labelled as such" if v.get("ai_generated") else "") if b))
        st = v.get("state")
        if st == "review":
            text = (f"<span style='color:{theme.WARN}'>⏸ <b>Waiting for your OK.</b></span> Watch it, then OK it to post "
                    "when its time comes" + (f" ({c.esc(c.when(v['planned_at'], now))})" if v.get("planned_at") else "")
                    + ", or delete it.")
        elif st == "queued":
            text = (f"🕓 Posts <b>{c.esc(c.when(v['planned_at'], now))}</b>" if v.get("planned_at") else
                    "🕓 In the queue") + (f" on {c.esc(v['account_handle'])}" if v.get("account_handle") else "")
        elif st == "posting":
            text = "⏫ Posting now…"
        elif st == "failed":
            text = f"<span style='color:{theme.BAD}'>✖ Didn't post:</span> {c.esc(v.get('last_error'))}"
            if v.get("interrupted"):
                text += ("<br>It may be on TikTok already: check the account, then retry it on the PC if it isn't.")
        elif st == "posted":
            text = f"✔ Posted {c.esc(c.when(v.get('posted_at'), now))}"
        else:
            text = "📚 In the library, not queued" + ("" if v.get("account_handle") else
                                                     " (its category has no TikTok account)")
        if v.get("state") in ("queued", "review") and not v.get("signed_in") and v.get("account_handle"):
            text += (f"<br><span style='color:{theme.WARN}'>⚠ {c.esc(v['account_handle'])} isn't signed in to TikTok "
                     "on the PC.</span>")
        self.state_text.setText(text)
        if v.get("prompt") == "your script":
            src = "✨ Made from your own script"
        elif v.get("prompt"):
            src = f"✨ Made from “{c.esc(c.trim(v['prompt'], 200))}”"
        elif v.get("source_title"):
            src = f"From “{c.esc(c.trim(v['source_title'], 120))}”" + (
                f" ({c.esc(v['source_channel'])})" if v.get("source_channel") else "")
        else:
            src = ""
        self.source.setText(src)
        self.source.setVisible(bool(src))
        warns = v.get("warnings") or []
        self.warnings.setText("<br>".join(f"<span style='color:{theme.WARN}'>⚠</span> {c.esc(w)}" for w in warns))
        self.warnings.setVisible(bool(warns))
        can = v.get("can") or {}
        self.ok_btn.setVisible(bool(can.get("ok")))
        self.now_btn.setVisible(bool(can.get("post_now")))
        self.retry_btn.setVisible(bool(can.get("retry")))
        self.del_btn.setVisible(bool(can.get("delete")))
        self.tt_btn.setVisible(bool(v.get("tiktok_url")))
        if not dirty:
            self._loading = True
            self.caption.setPlainText(v.get("caption") or "")
            self.hashtags.setText(v.get("hashtags") or "")
            self._loading = False
            self.save_btn.setEnabled(False)
            self.save_note.setText("")
        editable = bool(can.get("edit"))
        self.caption.setReadOnly(not editable)
        self.hashtags.setReadOnly(not editable)
        self.save_btn.setVisible(editable)

    def _changed(self) -> None:
        if not self._loading and self.v is not None:
            changed = (self.caption.toPlainText().strip() != (self.v.get("caption") or "").strip()
                       or " ".join(self.hashtags.text().split()) != (self.v.get("hashtags") or ""))
            self.save_btn.setEnabled(changed)
            self.save_note.setText("Not saved yet" if changed else "")

    def _save(self) -> None:
        if self.v is not None:
            self.save_btn.setEnabled(False)
            self.save_note.setText("Saving…")
            self.act.emit("edit", self.v["id"], {"caption": self.caption.toPlainText(), "hashtags": self.hashtags.text()})

    def _do(self, action: str) -> None:
        if self.v is not None:
            self.act.emit(action, self.v["id"], {})

    def _post_now(self) -> None:
        if self.v is None:
            return
        ask = QMessageBox(QMessageBox.Question, "Post it now?",
                          f"Post “{c.trim(self.v.get('headline'), 80)}” to {self.v.get('account_handle')} now, ahead of "
                          "its schedule?", QMessageBox.Cancel, self)
        go = ask.addButton("Post now", QMessageBox.AcceptRole)
        ask.exec()
        if ask.clickedButton() is go:
            self._do("post-now")

    def _delete(self) -> None:
        if self.v is None:
            return
        ask = QMessageBox(QMessageBox.Warning, "Delete this video?",
                          f"Delete “{c.trim(self.v.get('headline'), 80)}” from the PC? It won't be posted, and its "
                          "files go the way Clean up clears them (to the Recycle Bin if the PC is set to).",
                          QMessageBox.Cancel, self)
        go = ask.addButton("Delete", QMessageBox.DestructiveRole)
        ask.exec()
        if ask.clickedButton() is go:
            self.player.stop()
            self._do("delete")

    def saved(self, note: str) -> None:
        self.save_note.setText(note)


# --- the page ---------------------------------------------------------------------------------------------------


class VideosPage(QWidget):
    counts = Signal(int)  # videos waiting for your OK (the sidebar's badge)

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window_ = window
        self.thumbs = Thumbs(lambda: window.session)
        self.thumbs.ready.connect(self._thumb_ready)
        self.items: list[dict] = []
        self.filter = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(24, 16, 24, 10)
        head.addWidget(c.title("Videos"))
        head.addSpacing(16)
        self.seg = QFrame()
        self.seg.setObjectName("segbar")
        sh = QHBoxLayout(self.seg)
        sh.setContentsMargins(3, 3, 3, 3)
        sh.setSpacing(2)
        self.seg_btns: dict[str, QPushButton] = {}
        for key, label in FILTERS:
            btn = QPushButton(label)
            btn.setObjectName("seg")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=key: self.set_filter(k))
            sh.addWidget(btn)
            self.seg_btns[key] = btn
        head.addWidget(self.seg)
        head.addStretch(1)
        self.count = QLabel("")
        self.count.setProperty("muted", True)
        head.addWidget(self.count)
        v.addLayout(head)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(18, 0, 6, 12)
        self.list = QListWidget()
        self.list.setItemDelegate(_Delegate(self.thumbs, self.list))
        self.list.setMouseTracking(True)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setUniformItemSizes(True)
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setStyleSheet("QListWidget { background: transparent; border: none; } "
                                "QListWidget::item { border: none; background: transparent; }")
        self.list.currentItemChanged.connect(self._picked)
        lv.addWidget(self.list)
        self.empty = c.WrapLabel("", muted=True)
        self.empty.setAlignment(Qt.AlignCenter)
        lv.addWidget(self.empty)
        self.detail = Detail()
        self.detail.act.connect(self._act)
        split.addWidget(left)
        split.addWidget(self.detail)
        split.setSizes([460, 560])
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 5)
        v.addWidget(split, 1)
        self.set_filter("")

    def _thumb_ready(self, vid: str) -> None:
        self.list.viewport().update()
        v = self.detail.v
        if v is not None and v["id"] == vid and self.detail.player.stack.currentWidget() is self.detail.player.picture:
            self.detail.player.show_picture(self.thumbs.get(v))

    def set_filter(self, key: str) -> None:
        self.filter = key
        for k, btn in self.seg_btns.items():
            btn.setChecked(k == key)
        self._fill()

    def show_videos(self, items: list) -> None:
        self.items = items
        self.counts.emit(sum(1 for i in items if i.get("state") == "review"))
        self._fill()

    def _fill(self) -> None:
        cur = self.list.currentItem()
        cur_id = (cur.data(ITEM) or {}).get("id") if cur is not None else (self.detail.v or {}).get("id")
        shown = [i for i in self.items if not self.filter or i.get("state") == self.filter
                 or (self.filter == "queued" and i.get("state") == "posting")]
        n = {k: sum(1 for i in self.items if i.get("state") == k) for k, _ in FILTERS if k}
        for k, label in FILTERS:
            self.seg_btns[k].setText(f"{label}  {n[k]}" if k and n.get(k) else label)
        self.count.setText(c.plural(len(self.items), "video"))
        self.list.blockSignals(True)
        self.list.clear()
        pick = None
        for it in shown:
            li = QListWidgetItem()
            li.setData(ITEM, it)
            li.setSizeHint(QSize(100, ROW_H))
            self.list.addItem(li)
            if it["id"] == cur_id:
                pick = li
        self.list.blockSignals(False)
        self.empty.setVisible(not shown)
        self.list.setVisible(bool(shown))
        if not shown:
            self.empty.setText("Nothing waiting to post. New clips and Creator videos show up here as automation "
                               "makes them." if not self.filter else "No videos like that right now.")
        if pick is not None:
            self.list.blockSignals(True)
            self.list.setCurrentItem(pick)
            self.list.blockSignals(False)
            self._show(pick.data(ITEM), keep_player=True)
        elif shown:  # what was picked isn't on this list (another filter, or it's gone): the first one
            self.list.setCurrentRow(0)
        else:
            self.detail.show_video(None)

    def select(self, video_id: str) -> None:
        for i in range(self.list.count()):
            if (self.list.item(i).data(ITEM) or {}).get("id") == video_id:
                self.list.setCurrentRow(i)
                return

    def _picked(self, li, _old=None) -> None:
        if li is not None:
            self._show(li.data(ITEM))

    def _show(self, v: dict, keep_player: bool = False) -> None:
        proxy = self.window_.proxy
        url = proxy.url(v["id"]) if proxy is not None and v.get("exists") else ""
        self.detail.show_video(v, url, self.thumbs.get(v), keep_player=keep_player)

    def _act(self, action: str, vid: str, extra: dict) -> None:
        s = self.window_.session
        if s is None:
            return
        words = {"ok": "OK'ing…", "post-now": "Asking the PC to post it…", "retry": "Queuing it again…",
                 "delete": "Deleting it from the PC…", "edit": "Saving…"}
        self.window_.notice(words.get(action, "…"))

        def ok(out):
            self.window_.notice(out.get("message") or "Done.", good=True)
            if action == "edit":
                self.detail.saved("Saved")
            if out.get("video"):
                self.items = [out["video"] if i["id"] == vid else i for i in self.items]
                self._fill()
            if action == "delete":
                self.items = [i for i in self.items if i["id"] != vid]
                self.detail.show_video(None)
                self._fill()
            s.refresh_videos()
            s.refresh_status()

        def fail(exc):
            self.window_.notice(getattr(exc, "message", str(exc)), bad=True)
            if action == "edit":
                self.detail.saved("Not saved")
                self.detail.save_btn.setEnabled(True)

        s.call(lambda cl: cl.post(cl.video_path(vid, action), extra or None, timeout=60.0), ok, fail)

    def stop(self) -> None:
        self.detail.player.stop()

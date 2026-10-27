"""Build settings editors straight from settings.SCHEMA."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontDatabase
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
                               QPushButton, QSlider, QSpinBox, QWidget)

from ..download import VIDEO_EXTS
from ..fonts import BUNDLED
from ..settings import Field, Settings
from . import qt_fonts

OnChange = Callable[[str, Any], None]


class Editor:
    """A widget plus get/set for one settings field."""

    def __init__(self, widget: QWidget, get: Callable[[], Any], set_: Callable[[Any], None]):
        self.widget, self.get, self.set = widget, get, set_


def make_editor(f: Field, value: Any, on_change: OnChange) -> Editor:
    emit = lambda *_: on_change(f.name, editor.get())  # noqa: E731

    if f.kind == "bool":
        w = QCheckBox()
        w.setChecked(bool(value))
        w.toggled.connect(emit)
        editor = Editor(w, w.isChecked, lambda v: w.setChecked(bool(v)))

    elif f.kind == "int":
        w = QSpinBox()
        w.setRange(int(f.lo), int(f.hi))
        w.setSingleStep(int(f.step) or 1)
        w.setValue(int(value))
        w.valueChanged.connect(emit)
        editor = Editor(w, w.value, lambda v: w.setValue(int(v)))

    elif f.kind == "float" and f.hi <= 1 and f.step < 1:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 100)
        label = QLabel()
        label.setFixedWidth(34)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(slider, 1)
        row.addWidget(label)

        def set_slider(v: float) -> None:
            slider.blockSignals(True)
            slider.setValue(int(round(float(v) * 100)))
            slider.blockSignals(False)
            label.setText(f"{float(v):.2f}")

        def on_slide(v: int) -> None:
            label.setText(f"{v / 100:.2f}")
            emit()

        set_slider(value)
        slider.valueChanged.connect(on_slide)
        editor = Editor(box, lambda: slider.value() / 100, set_slider)

    elif f.kind == "float":
        w = QDoubleSpinBox()
        w.setRange(f.lo, f.hi)
        w.setSingleStep(f.step)
        w.setDecimals(1 if f.step >= 0.5 else 2)
        w.setValue(float(value))
        w.valueChanged.connect(emit)
        editor = Editor(w, w.value, lambda v: w.setValue(float(v)))

    elif f.kind == "choice":
        w = QComboBox()
        for data, text in f.choices:
            w.addItem(text, data)

        def set_choice(v: Any) -> None:
            i = w.findData(v)
            if i < 0:
                w.addItem(str(v), v)
                i = w.count() - 1
            w.blockSignals(True)
            w.setCurrentIndex(i)
            w.blockSignals(False)

        set_choice(value)
        w.currentIndexChanged.connect(emit)
        editor = Editor(w, w.currentData, set_choice)

    elif f.kind == "multi":  # several of f.choices, stored as "id, id"
        w = QListWidget()
        w.setFixedHeight(150)
        for data, text in f.choices:
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, data)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            w.addItem(it)

        def checked() -> str:
            return ", ".join(str(w.item(i).data(Qt.UserRole)) for i in range(w.count())
                             if w.item(i).checkState() == Qt.Checked)

        def set_checked(v: Any) -> None:
            chosen = {x.strip() for x in str(v).split(",") if x.strip()}
            for i in range(w.count()):
                it = w.item(i)
                it.setCheckState(Qt.Checked if str(it.data(Qt.UserRole)) in chosen else Qt.Unchecked)

        set_checked(value)
        w.itemChanged.connect(emit)
        editor = Editor(w, checked, set_checked)

    elif f.kind == "text":
        w = QPlainTextEdit()
        w.setFixedHeight(90)
        w.setPlaceholderText(f.placeholder)
        w.setPlainText(str(value))
        w.textChanged.connect(emit)
        editor = Editor(w, w.toPlainText, lambda v: w.setPlainText(str(v)))

    elif f.kind == "path":
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        line = QLineEdit(str(value))
        btn = QPushButton("Browse…")
        row.addWidget(line, 1)
        row.addWidget(btn)

        def browse() -> None:
            d = QFileDialog.getExistingDirectory(box, f.label, line.text())
            if d:
                line.setText(d)

        btn.clicked.connect(browse)
        line.editingFinished.connect(emit)
        line.textChanged.connect(lambda *_: None)
        btn.clicked.connect(emit)
        editor = Editor(box, line.text, lambda v: line.setText(str(v)))

    elif f.kind == "file":
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        line = QLineEdit(str(value))
        line.setPlaceholderText("No file chosen")
        btn = QPushButton("Choose…")
        row.addWidget(line, 1)
        row.addWidget(btn)

        def choose() -> None:
            exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
            path, _ = QFileDialog.getOpenFileName(box, f.label, line.text(), f"Videos ({exts});;All files (*)")
            if path:
                line.setText(path)
                emit()

        btn.clicked.connect(choose)
        line.editingFinished.connect(emit)
        editor = Editor(box, line.text, lambda v: line.setText(str(v)))

    elif f.kind == "font":
        # Bundled fonts first, each drawn in itself; any installed font can be picked or typed below them.
        w = QComboBox()
        w.setEditable(True)
        w.setInsertPolicy(QComboBox.NoInsert)
        w.setMaxVisibleItems(18)
        for name in BUNDLED:
            w.addItem(name, name)
            w.setItemData(w.count() - 1, qt_fonts.qfont(name, 15), Qt.FontRole)
        w.insertSeparator(w.count())
        for family in QFontDatabase.families():
            if family not in BUNDLED and not QFontDatabase.isPrivateFamily(family):
                w.addItem(family, family)

        def set_font(v: Any) -> None:
            w.blockSignals(True)
            i = w.findText(str(v))
            if i >= 0:
                w.setCurrentIndex(i)
            else:
                w.setEditText(str(v))
            w.blockSignals(False)

        set_font(value)
        w.currentTextChanged.connect(emit)
        editor = Editor(w, lambda: w.currentText().strip(), set_font)
        if not f.help:
            editor.widget.setToolTip("Fonts at the top ship with ClipMonster and look the same on every PC")

    elif f.kind == "color":
        w = QPushButton()
        w.setFixedWidth(90)
        state = {"v": str(value)}

        def paint() -> None:
            w.setText(state["v"].upper())
            w.setStyleSheet(f"background:{state['v']}; color:{'#000' if QColor(state['v']).lightness() > 140 else '#fff'};")

        def pick() -> None:
            c = QColorDialog.getColor(QColor(state["v"]), w, f.label)
            if c.isValid():
                state["v"] = c.name()
                paint()
                emit()

        def set_colour(v: Any) -> None:
            state["v"] = str(v)
            paint()

        paint()
        w.clicked.connect(pick)
        editor = Editor(w, lambda: state["v"], set_colour)

    else:  # str / secret
        w = QLineEdit(str(value))
        w.setMinimumWidth(170)
        if f.kind == "secret":
            w.setEchoMode(QLineEdit.Password)
            w.setPlaceholderText(f.placeholder or "Paste key")
        w.textChanged.connect(emit)
        editor = Editor(w, w.text, lambda v: w.setText(str(v)))

    if f.help:
        editor.widget.setToolTip(f.help)
    return editor


def build_form(fields: list[Field], settings: Settings, on_change: OnChange,
               compact: bool = False) -> tuple[QWidget, dict[str, Editor]]:
    page = QWidget()
    form = QFormLayout(page)
    form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    if compact:  # narrow side panels: long labels go above their control
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    form.setVerticalSpacing(9)
    editors: dict[str, Editor] = {}
    for f in fields:
        ed = make_editor(f, getattr(settings, f.name), on_change)
        label = QLabel(f.label)
        if f.help:
            label.setToolTip(f.help)
        form.addRow(label, ed.widget)
        editors[f.name] = ed
    return page, editors


def refresh(editors: dict[str, Editor], settings: Settings) -> None:
    """Push settings values into existing editors without firing change callbacks."""
    for name, ed in editors.items():
        ed.widget.blockSignals(True)
        for child in ed.widget.findChildren(QWidget):
            child.blockSignals(True)
        try:
            ed.set(getattr(settings, name))
        finally:
            ed.widget.blockSignals(False)
            for child in ed.widget.findChildren(QWidget):
                child.blockSignals(False)

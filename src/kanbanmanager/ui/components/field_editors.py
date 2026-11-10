"""
Editors for custom field values (Qt side of core.custom_fields)

``create_field_editor(field, context)`` returns a ``FieldEditor`` for a field
definition. Built-in types have dedicated editors; any other registered type
gets a text box that round-trips through the type's ``to_text`` / ``parse``,
so new field types work without UI code. To give a type its own editor::

    register_field_editor("phone", PhoneEditor)   # PhoneEditor(field, context, parent)

``FieldsPanel`` lays out the editors of a board's fields (the task editor's
Fields tab and the task template editor use it).
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from functools import partial
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QDate, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import custom_fields as cf
from kanbanmanager.core.custom_fields import FieldValueError
from kanbanmanager.ui.colors import alpha
from kanbanmanager.ui.components.flow_layout import FlowLayout

NO_DATE = QDate(1900, 1, 1)


@dataclass
class EditorContext:
    people: List[str] = dc_field(default_factory=list)   # names offered by person fields


def swatch(color: str, size: int = 12) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(QColor(color))
    return QIcon(pix)


class FieldEditor(QWidget):
    """Base editor: ``value()`` returns the canonical value (raising FieldValueError)."""

    changed = Signal()

    def __init__(self, field: Dict[str, Any], context: EditorContext, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.field = field
        self.context = context
        self.ftype = cf.type_of(field)
        self.config = field.get("config") or {}
        self.layout_ = QHBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(6)

    def raw(self) -> Any:
        raise NotImplementedError

    def value(self) -> Any:
        return self.ftype.validate(self.raw(), self.config)

    def set_value(self, value: Any) -> None:
        raise NotImplementedError

    def error(self) -> Optional[str]:
        try:
            self.value()
            return None
        except FieldValueError as exc:
            return str(exc)

    def _emit(self, *_args) -> None:
        self.changed.emit()


class TextLineEditor(FieldEditor):
    """One-line text; also the generic editor for types without their own."""

    def __init__(self, field, context, parent=None):
        super().__init__(field, context, parent)
        self._kept: Any = None  # value of a field whose type is unavailable (read-only)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(self._placeholder())
        self.edit.textChanged.connect(self._emit)
        self.layout_.addWidget(self.edit, 1)
        if not self.ftype.editable:
            self.edit.setReadOnly(True)
            self.edit.setToolTip("This field's type is not available in this version; its value is kept as is.")

    def _placeholder(self) -> str:
        return {"url": "https://…", "email": "name@example.com"}.get(self.ftype.key, "")

    def raw(self) -> Any:
        return self.edit.text()

    def value(self) -> Any:
        if not self.ftype.editable:
            return self._kept
        return self.ftype.parse(self.edit.text(), self.config)

    def set_value(self, value: Any) -> None:
        self._kept = value
        self.edit.setText(self.ftype.to_text(value, self.config) if value is not None else "")


class MultilineEditor(FieldEditor):
    def __init__(self, field, context, parent=None):
        super().__init__(field, context, parent)
        self.edit = QPlainTextEdit()
        self.edit.setFixedHeight(64)
        self.edit.textChanged.connect(self._emit)
        self.layout_.addWidget(self.edit, 1)

    def raw(self) -> Any:
        return self.edit.toPlainText()

    def set_value(self, value: Any) -> None:
        self.edit.setPlainText(value or "")


def _text_editor(field, context, parent=None) -> FieldEditor:
    if (field.get("config") or {}).get("multiline"):
        return MultilineEditor(field, context, parent)
    return TextLineEditor(field, context, parent)


class NumberEditor(TextLineEditor):
    """Number / currency / percent: a text box (so "empty" is possible) with the unit beside it."""

    def __init__(self, field, context, parent=None):
        super().__init__(field, context, parent)
        self.edit.setMaximumWidth(160)
        key = self.ftype.key
        prefix = self.config.get("symbol") if key == "currency" else ""
        suffix = "%" if key == "percent" else (self.config.get("unit") or "")
        if prefix:
            lbl = QLabel(prefix)
            lbl.setStyleSheet("color: #94A3B8;")
            self.layout_.insertWidget(0, lbl)
        if suffix:
            lbl = QLabel(suffix)
            lbl.setStyleSheet("color: #94A3B8;")
            self.layout_.addWidget(lbl)
        lo, hi = self.config.get("min"), self.config.get("max")
        if lo is not None or hi is not None:
            self.edit.setPlaceholderText(f"{'' if lo is None else lo} … {'' if hi is None else hi}")
        self.layout_.addStretch()


class SelectEditor(FieldEditor):
    def __init__(self, field, context, parent=None):
        super().__init__(field, context, parent)
        self.combo = QComboBox()
        self.combo.addItem("—", None)
        for opt in self.config.get("options", []):
            self.combo.addItem(swatch(opt["color"]), opt["label"], opt["label"])
        self.combo.currentIndexChanged.connect(self._emit)
        self.layout_.addWidget(self.combo, 1)

    def raw(self) -> Any:
        return self.combo.currentData()

    def set_value(self, value: Any) -> None:
        idx = self.combo.findData(value)
        if idx < 0 and value:
            self.combo.addItem(f"⚠ {value} (no longer an option)", value)
            idx = self.combo.count() - 1
        self.combo.setCurrentIndex(max(0, idx))


class MultiSelectEditor(FieldEditor):
    """Toggle chips, one per option."""

    def __init__(self, field, context, parent=None):
        super().__init__(field, context, parent)
        self.host = QWidget()
        self.layout_.addWidget(self.host, 1)
        self.flow = FlowLayout(self.host, h_spacing=4, v_spacing=4)
        self.buttons: Dict[str, QPushButton] = {}
        for opt in self.config.get("options", []):
            btn = QPushButton(opt["label"])
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            c = opt["color"]
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: #94A3B8; border: 1px solid #334155;"
                f" border-radius: 10px; padding: 2px 10px; }}"
                f"QPushButton:checked {{ background-color: {alpha(c, 0x33)}; color: {c}; border-color: {c};"
                f" font-weight: 600; }}")
            btn.toggled.connect(self._emit)
            self.flow.addWidget(btn)
            self.buttons[opt["label"]] = btn
        if not self.buttons:
            self.flow.addWidget(QLabel("No options defined - add some under 🧩 Fields"))

    def raw(self) -> Any:
        return [label for label, btn in self.buttons.items() if btn.isChecked()] or None

    def set_value(self, value: Any) -> None:
        chosen = {v.lower() for v in (value or [])}
        for label, btn in self.buttons.items():
            btn.setChecked(label.lower() in chosen)


class CheckboxEditor(FieldEditor):
    def __init__(self, field, context, parent=None):
        super().__init__(field, context, parent)
        self.box = QCheckBox()
        self.box.toggled.connect(self._emit)
        self.layout_.addWidget(self.box)
        self.layout_.addStretch()

    def raw(self) -> Any:
        return True if self.box.isChecked() else None

    def set_value(self, value: Any) -> None:
        self.box.setChecked(value is True)


class DateEditor(FieldEditor):
    def __init__(self, field, context, parent=None):
        super().__init__(field, context, parent)
        self.edit = QDateEdit()
        self.edit.setCalendarPopup(True)
        self.edit.setDisplayFormat("MMM d, yyyy")
        self.edit.setMinimumDate(NO_DATE)
        self.edit.setSpecialValueText("—")  # shown at the minimum date = no value
        self.edit.setDate(NO_DATE)
        self.edit.dateChanged.connect(self._emit)
        self.layout_.addWidget(self.edit)
        today = QToolButton()
        today.setText("Today")
        today.clicked.connect(self._set_today)
        self.layout_.addWidget(today)
        clear = QToolButton()
        clear.setText("✕")
        clear.setToolTip("Clear")
        clear.clicked.connect(self._clear)
        self.layout_.addWidget(clear)
        self.layout_.addStretch()

    def _set_today(self, _checked: bool = False) -> None:
        self.edit.setDate(QDate.currentDate())

    def _clear(self, _checked: bool = False) -> None:
        self.edit.setDate(NO_DATE)

    def raw(self) -> Any:
        d = self.edit.date()
        return None if d == NO_DATE else d.toPython().isoformat()

    def set_value(self, value: Any) -> None:
        d = QDate.fromString(value, "yyyy-MM-dd") if value else NO_DATE
        self.edit.setDate(d if d.isValid() else NO_DATE)


class RatingEditor(FieldEditor):
    def __init__(self, field, context, parent=None):
        super().__init__(field, context, parent)
        self._value = 0
        self.stars: List[QToolButton] = []
        for n in range(1, int(self.config.get("max", 5)) + 1):
            btn = QToolButton()
            btn.setAutoRaise(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(f"{n} of {self.config.get('max', 5)} - click again to clear")
            btn.clicked.connect(partial(self._click, n))
            self.layout_.addWidget(btn)
            self.stars.append(btn)
        self.layout_.addStretch()
        self._paint()

    def _click(self, n: int, _checked: bool = False) -> None:
        self._value = 0 if self._value == n else n
        self._paint()
        self.changed.emit()

    def _paint(self) -> None:
        for i, btn in enumerate(self.stars, 1):
            on = i <= self._value
            btn.setText("★" if on else "☆")
            btn.setStyleSheet(f"QToolButton {{ color: {'#FBBF24' if on else '#475569'}; font-size: 18px;"
                              " border: none; padding: 0 1px; }")

    def raw(self) -> Any:
        return self._value or None

    def set_value(self, value: Any) -> None:
        self._value = int(value or 0)
        self._paint()


class PersonEditor(FieldEditor):
    def __init__(self, field, context, parent=None):
        super().__init__(field, context, parent)
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.NoInsert)
        self.combo.addItem("")
        for name in context.people:
            self.combo.addItem(name)
        self.combo.lineEdit().setPlaceholderText("Nobody")
        self.combo.editTextChanged.connect(self._emit)
        self.layout_.addWidget(self.combo, 1)

    def raw(self) -> Any:
        return self.combo.currentText()

    def set_value(self, value: Any) -> None:
        self.combo.setEditText(value or "")


class LinkEditor(TextLineEditor):
    """URL / email with a button that opens it."""

    def __init__(self, field, context, parent=None):
        super().__init__(field, context, parent)
        self.open_btn = QToolButton()
        self.open_btn.setText("↗" if self.ftype.key == "url" else "✉")
        self.open_btn.setToolTip("Open link" if self.ftype.key == "url" else "Write an email")
        self.open_btn.clicked.connect(self._open)
        self.layout_.addWidget(self.open_btn)
        self.edit.textChanged.connect(self._update_button)
        self._update_button()

    def _update_button(self, *_args) -> None:
        self.open_btn.setEnabled(bool(self.edit.text().strip()) and self.error() is None)

    def _open(self, _checked: bool = False) -> None:
        try:
            value = self.value()
        except FieldValueError:
            return
        if value:
            QDesktopServices.openUrl(QUrl(value if self.ftype.key == "url" else f"mailto:{value}"))


_EDITORS: Dict[str, Callable[..., FieldEditor]] = {
    "text": _text_editor,
    "number": NumberEditor,
    "currency": NumberEditor,
    "percent": NumberEditor,
    "select": SelectEditor,
    "multi_select": MultiSelectEditor,
    "checkbox": CheckboxEditor,
    "date": DateEditor,
    "rating": RatingEditor,
    "person": PersonEditor,
    "url": LinkEditor,
    "email": LinkEditor,
}


def register_field_editor(type_key: str, factory: Callable[..., FieldEditor]) -> None:
    """Use ``factory(field, context, parent)`` to edit values of ``type_key`` fields."""
    _EDITORS[type_key] = factory


def create_field_editor(field: Dict[str, Any], context: Optional[EditorContext] = None,
                        parent: Optional[QWidget] = None) -> FieldEditor:
    ftype = cf.type_of(field)
    factory = _EDITORS.get(ftype.key) if ftype.editable else None
    editor = (factory or TextLineEditor)(field, context or EditorContext(), parent)
    return editor


# ---------------------------------------------------------------------------
# A form with one editor per field
# ---------------------------------------------------------------------------
class FieldsPanel(QWidget):
    """Editors for a list of field definitions, with inline validation messages."""

    changed = Signal()
    manage_requested = Signal()

    def __init__(self, context: Optional[EditorContext] = None, parent: Optional[QWidget] = None,
                 empty_text: str = "This board has no custom fields yet.", show_manage: bool = True):
        super().__init__(parent)
        self.context = context or EditorContext()
        self.fields: List[Dict[str, Any]] = []
        self.editors: Dict[int, FieldEditor] = {}
        self.error_labels: Dict[int, QLabel] = {}
        self._empty_text = empty_text
        from kanbanmanager.ui.styles import CHECKBOX_QSS

        self.setStyleSheet(CHECKBOX_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(self.scroll, 1)
        self.host = QWidget()
        self.form = QFormLayout(self.host)
        self.form.setLabelAlignment(Qt.AlignRight | Qt.AlignTop)
        self.form.setHorizontalSpacing(14)
        self.form.setVerticalSpacing(4)
        self.scroll.setWidget(self.host)

        self.empty = QLabel(empty_text)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: #64748B; padding: 24px;")
        outer.addWidget(self.empty)
        foot = QHBoxLayout()
        outer.addLayout(foot)
        self.hint = QLabel("Fields are defined per board. * = required.")
        self.hint.setStyleSheet("color: #64748B; font-size: 11px;")
        foot.addWidget(self.hint, 1)
        self.manage_btn = QPushButton("🧩 Manage Fields…")
        self.manage_btn.clicked.connect(self.manage_requested.emit)
        self.manage_btn.setVisible(show_manage)
        foot.addWidget(self.manage_btn)

    def set_fields(self, fields: List[Dict[str, Any]], values: Optional[Dict[int, Any]] = None) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.fields = list(fields)
        self.editors, self.error_labels = {}, {}
        for f in self.fields:
            name = QLabel(f"{f['name']}{' *' if f.get('required') else ''}")
            name.setToolTip(f.get("description") or f"{f.get('type_label', '')} field")
            name.setStyleSheet("color: #CBD5E1; font-weight: 600; padding-top: 4px;")
            editor = create_field_editor(f, self.context)
            editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            if f.get("description"):
                editor.setToolTip(f["description"])
            self.form.addRow(name, editor)
            err = QLabel("")
            err.setStyleSheet("color: #F87171; font-size: 11px;")
            err.setVisible(False)
            self.form.addRow("", err)
            editor.set_value((values or {}).get(f["id"]))
            editor.changed.connect(partial(self._on_changed, f["id"]))
            self.editors[f["id"]] = editor
            self.error_labels[f["id"]] = err
        self.scroll.setVisible(bool(self.fields))
        self.empty.setVisible(not self.fields)
        self.hint.setVisible(bool(self.fields))

    def _on_changed(self, field_id: int) -> None:
        msg = self.editors[field_id].error()
        self.error_labels[field_id].setText(f"⚠ {msg}" if msg else "")
        self.error_labels[field_id].setVisible(bool(msg))
        self.changed.emit()

    def set_values(self, values: Dict[int, Any], only_given: bool = False) -> None:
        for fid, editor in self.editors.items():
            if fid in values or not only_given:
                editor.set_value(values.get(fid))

    def raw_values(self) -> Dict[int, Any]:
        """Current values (None for invalid or empty ones)."""
        out = {}
        for fid, editor in self.editors.items():
            try:
                out[fid] = editor.value()
            except FieldValueError:
                out[fid] = None
        return out

    def values(self) -> Dict[int, Any]:
        """Validated values of every field (None = empty); raises FieldValueError."""
        out = {}
        for f in self.fields:
            try:
                out[f["id"]] = self.editors[f["id"]].value()
            except FieldValueError as exc:
                raise FieldValueError(f"{f['name']}: {exc}") from None
        return out

    def errors(self) -> List[str]:
        out = []
        for f in self.fields:
            msg = self.editors[f["id"]].error()
            if msg:
                out.append(f"{f['name']}: {msg}")
        return out

    def missing_required(self) -> List[str]:
        return cf.missing_required(self.fields, self.raw_values())

    def filled_count(self) -> int:
        return sum(1 for f in self.fields if not cf.is_empty(f, self.raw_values().get(f["id"])))

    def focus_field(self, name: str) -> None:
        for f in self.fields:
            if f["name"] == name:
                self.scroll.ensureWidgetVisible(self.editors[f["id"]])
                self.editors[f["id"]].setFocus()

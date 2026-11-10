"""
Custom Fields Manager: define a board's fields (name, type, settings, default, required, on cards)
"""
from __future__ import annotations

import json
from functools import partial
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import custom_fields as cf
from kanbanmanager.core.custom_fields import FieldValueError
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.components.field_editors import EditorContext, FieldEditor, create_field_editor, swatch
from kanbanmanager.ui.styles import CHECKBOX_QSS

NEW_ID = -1


class OptionsEditor(QWidget):
    """Options of a choice field: rename in place, recolour, reorder, add, remove.
    Each option keeps its id so renames are applied to existing values."""

    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        add_row = QHBoxLayout()
        layout.addLayout(add_row)
        self.new_input = QLineEdit()
        self.new_input.setPlaceholderText("New option, then Enter")
        self.new_input.returnPressed.connect(self.add_from_input)
        add_row.addWidget(self.new_input, 1)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.add_from_input)
        add_row.addWidget(add_btn)
        body = QHBoxLayout()
        layout.addLayout(body)
        self.list = QListWidget()
        self.list.setMinimumHeight(170)
        self.list.setToolTip("Double-click an option to rename it")
        self.list.itemChanged.connect(self._emit)
        self.list.model().rowsMoved.connect(self._emit)
        self.list.model().rowsRemoved.connect(self._emit)
        self.list.model().rowsInserted.connect(self._emit)
        body.addWidget(self.list, 1)
        side = QVBoxLayout()
        body.addLayout(side)
        for text, tip, slot in (("▲", "Move up", partial(self.move, -1)), ("▼", "Move down", partial(self.move, 1)),
                                ("🎨", "Colour", self.recolor), ("✕", "Remove", self.remove)):
            btn = QToolButton()
            btn.setText(text)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            side.addWidget(btn)
        side.addStretch()

    def _emit(self, *_args) -> None:
        self.changed.emit()

    def set_options(self, options: List[Dict[str, str]]) -> None:
        self.list.clear()
        for opt in options:
            self._add_item(opt.get("label", ""), opt.get("color") or cf.OPTION_PALETTE[self.list.count() % 10],
                           opt.get("id"))

    def _add_item(self, label: str, color: str, oid: Optional[str]) -> None:
        item = QListWidgetItem(swatch(color), label)
        item.setData(Qt.UserRole, oid)
        item.setData(Qt.UserRole + 1, color)
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        self.list.addItem(item)

    def add_from_input(self, _checked: bool = False) -> None:
        label = " ".join(self.new_input.text().split())
        if not label:
            return
        if any(self.list.item(i).text().lower() == label.lower() for i in range(self.list.count())):
            self.new_input.selectAll()
            return
        self._add_item(label, cf.OPTION_PALETTE[self.list.count() % len(cf.OPTION_PALETTE)], None)
        self.new_input.clear()

    def move(self, step: int, _checked: bool = False) -> None:
        row = self.list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(target, item)
        self.list.setCurrentRow(target)

    def recolor(self, _checked: bool = False) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        color = QColorDialog.getColor(item.data(Qt.UserRole + 1), self, "Option colour")
        if color.isValid():
            item.setData(Qt.UserRole + 1, color.name().upper())
            item.setIcon(swatch(color.name()))
            self.changed.emit()

    def remove(self, _checked: bool = False) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)

    def options(self) -> List[Dict[str, Any]]:
        out = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            opt = {"label": item.text(), "color": item.data(Qt.UserRole + 1)}
            if item.data(Qt.UserRole):
                opt["id"] = item.data(Qt.UserRole)
            out.append(opt)
        return out


class TypeSettings(QWidget):
    """The settings form for one field type (rebuilt when the type changes)."""

    changed = Signal()  # settings that affect how values are entered (options, unit, scale ...)

    def __init__(self, type_key: str, config: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.type_key = type_key
        self.ftype = cf.get_field_type(type_key)
        config = self.ftype.normalize_config(config)
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.widgets: Dict[str, QWidget] = {}
        self.options_editor: Optional[OptionsEditor] = None
        self.json_edit: Optional[QPlainTextEdit] = None
        k = type_key
        if self.ftype.has_options:
            self.options_editor = OptionsEditor()
            self.options_editor.set_options(config.get("options", []))
            self.options_editor.changed.connect(self.changed.emit)
            self.form.addRow("Options:", self.options_editor)
        elif k == "text":
            self._check("multiline", "Several lines", config)
            self._spin("max_length", "Max length:", config, 0, 10000, " characters", special="no limit")
        elif k in ("number", "currency"):
            if k == "currency":
                self._line("symbol", "Symbol:", config, "$, €, £ …")
            else:
                self._line("unit", "Unit:", config, "pts, kg, h … (optional)")
            self._spin("decimals", "Decimals:", config, 0, 6 if k == "number" else 4)
            self._line("min", "Minimum:", config, "none")
            self._line("max", "Maximum:", config, "none")
            self._check("show_total", "Show the total in column headers", config)
        elif k == "rating":
            self._spin("max", "Stars:", config, 3, 10)
        elif self.ftype.default_config():
            # A registered type without a dedicated settings form: edit its config as JSON
            self.json_edit = QPlainTextEdit(json.dumps(config, indent=2, ensure_ascii=False))
            self.json_edit.setFixedHeight(90)
            self.form.addRow("Settings (JSON):", self.json_edit)
        else:
            note = QLabel("No settings for this type.")
            note.setStyleSheet("color: #64748B;")
            self.form.addRow(note)

    def _check(self, key: str, text: str, config: Dict[str, Any]) -> None:
        box = QCheckBox(text)
        box.setChecked(bool(config.get(key)))
        box.toggled.connect(self.changed.emit)
        self.form.addRow("", box)
        self.widgets[key] = box

    def _spin(self, key: str, label: str, config: Dict[str, Any], lo: int, hi: int, suffix: str = "",
              special: str = "") -> None:
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setSuffix(suffix)
        if special:
            spin.setSpecialValueText(special)
        spin.setValue(int(config.get(key) or lo))
        spin.valueChanged.connect(self.changed.emit)
        self.form.addRow(label, spin)
        self.widgets[key] = spin

    def _line(self, key: str, label: str, config: Dict[str, Any], placeholder: str) -> None:
        edit = QLineEdit("" if config.get(key) is None else str(config.get(key)))
        edit.setPlaceholderText(placeholder)
        edit.setMaximumWidth(180)
        edit.editingFinished.connect(self.changed.emit)
        self.form.addRow(label, edit)
        self.widgets[key] = edit

    def config(self) -> Dict[str, Any]:
        """Settings as entered (raises FieldValueError for unreadable input)."""
        if self.options_editor is not None:
            return self.ftype.normalize_config({"options": self.options_editor.options()})
        if self.json_edit is not None:
            try:
                raw = json.loads(self.json_edit.toPlainText() or "{}")
            except ValueError as exc:
                raise FieldValueError(f"Settings are not valid JSON: {exc}") from None
            return self.ftype.normalize_config(raw if isinstance(raw, dict) else {})
        raw: Dict[str, Any] = {}
        for key, w in self.widgets.items():
            if isinstance(w, QCheckBox):
                raw[key] = w.isChecked()
            elif isinstance(w, QSpinBox):
                raw[key] = w.value()
            else:
                text = w.text().strip()
                if key in ("min", "max"):
                    if text:
                        try:
                            raw[key] = float(text.replace(",", ""))
                        except ValueError:
                            raise FieldValueError(f"{key.capitalize()}imum must be a number") from None
                    else:
                        raw[key] = None
                else:
                    raw[key] = text
        return self.ftype.normalize_config(raw)


class CustomFieldsDialog(QDialog):
    """List of a board's fields on the left, the selected field's settings on the right."""

    def __init__(self, service: DatabaseService, board_id: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.changed = False
        self.fields: List[Dict[str, Any]] = []
        self.current: Optional[Dict[str, Any]] = None   # field being edited (id NEW_ID = not saved yet)
        self.type_settings: Optional[TypeSettings] = None
        self.default_editor: Optional[FieldEditor] = None
        board = service.get_board(board_id)
        self.setWindowTitle(f"Custom Fields - {board.name if board else ''}")
        self.setMinimumSize(860, 600)
        self.setStyleSheet(CHECKBOX_QSS)
        self._init_ui()
        self.reload()

    # UI -----------------------------------------------------------------------
    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        intro = QLabel("Add your own fields to every task on this board - story points, severity, customer, "
                       "links… Values can be shown on cards, filtered, totalled and used in automation rules.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #94A3B8;")
        root.addWidget(intro)

        body = QHBoxLayout()
        root.addLayout(body, 1)
        left = QVBoxLayout()
        body.addLayout(left, 2)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_row_changed)
        left.addWidget(self.list, 1)
        lbtns = QHBoxLayout()
        left.addLayout(lbtns)
        self.add_btn = QPushButton("＋ Add Field")
        self.add_btn.setObjectName("PrimaryButton")
        self.add_btn.clicked.connect(self.new_field)
        lbtns.addWidget(self.add_btn)
        self.up_btn = QToolButton()
        self.up_btn.setText("▲")
        self.up_btn.setToolTip("Move up")
        self.up_btn.clicked.connect(partial(self.move_selected, -1))
        lbtns.addWidget(self.up_btn)
        self.down_btn = QToolButton()
        self.down_btn.setText("▼")
        self.down_btn.setToolTip("Move down")
        self.down_btn.clicked.connect(partial(self.move_selected, 1))
        lbtns.addWidget(self.down_btn)
        lbtns.addStretch()
        self.delete_btn = QPushButton("🗑 Delete")
        self.delete_btn.setObjectName("DangerButton")
        self.delete_btn.clicked.connect(self.delete_selected)
        lbtns.addWidget(self.delete_btn)

        self.editor_frame = QFrame()
        self.editor_frame.setObjectName("FieldEditorFrame")
        self.editor_frame.setStyleSheet("#FieldEditorFrame { border: 1px solid #334155; border-radius: 8px; }")
        body.addWidget(self.editor_frame, 3)
        ed = QVBoxLayout(self.editor_frame)
        ed.setContentsMargins(14, 12, 14, 12)
        self.form = QFormLayout()
        ed.addLayout(self.form)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. Story points")
        self.name_input.setMaxLength(cf.MAX_FIELD_NAME)
        self.form.addRow("Name:", self.name_input)
        self.type_combo = QComboBox()
        for t in cf.field_types():
            self.type_combo.addItem(f"{t.icon}  {t.label}", t.key)
            self.type_combo.setItemData(self.type_combo.count() - 1, t.description, Qt.ToolTipRole)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self.form.addRow("Type:", self.type_combo)
        self.type_help = QLabel("")
        self.type_help.setWordWrap(True)
        self.type_help.setStyleSheet("color: #64748B; font-size: 11px;")
        self.form.addRow("", self.type_help)
        self.desc_input = QLineEdit()
        self.desc_input.setPlaceholderText("Help text shown in the task editor (optional)")
        self.form.addRow("Description:", self.desc_input)
        self.settings_holder = QVBoxLayout()
        ed.addLayout(self.settings_holder)
        flags = QHBoxLayout()
        ed.addLayout(flags)
        self.required_chk = QCheckBox("Required")
        self.required_chk.setToolTip("The task editor won't save a task without a value. Tasks created "
                                     "elsewhere (quick capture, automation) are flagged on their card.")
        flags.addWidget(self.required_chk)
        self.card_chk = QCheckBox("Show on cards")
        flags.addWidget(self.card_chk)
        flags.addStretch()
        self.default_row = QFormLayout()
        ed.addLayout(self.default_row)
        self.usage_lbl = QLabel("")
        self.usage_lbl.setStyleSheet("color: #64748B; font-size: 11px;")
        ed.addWidget(self.usage_lbl)
        ed.addStretch()
        ebtns = QHBoxLayout()
        ed.addLayout(ebtns)
        self.error_lbl = QLabel("")
        self.error_lbl.setWordWrap(True)
        self.error_lbl.setStyleSheet("color: #F87171;")
        ebtns.addWidget(self.error_lbl, 1)
        self.revert_btn = QPushButton("Revert")
        self.revert_btn.clicked.connect(self._revert)
        ebtns.addWidget(self.revert_btn)
        self.save_btn = QPushButton("Save Field")
        self.save_btn.setObjectName("PrimaryButton")
        self.save_btn.clicked.connect(self.save_current)
        ebtns.addWidget(self.save_btn)

        bottom = QHBoxLayout()
        root.addLayout(bottom)
        bottom.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)

    # Data ---------------------------------------------------------------------
    def reload(self, select_id: Optional[int] = None) -> None:
        self.fields = self.service.get_custom_fields(self.board_id)
        self.list.blockSignals(True)
        self.list.clear()
        for f in self.fields:
            flags = " *" if f["required"] else ""
            item = QListWidgetItem(f"{f['icon']}  {f['name']}{flags}")
            item.setToolTip(f"{f['type_label']}" + (f" - {f['description']}" if f["description"] else ""))
            item.setData(Qt.UserRole, f["id"])
            self.list.addItem(item)
        self.list.blockSignals(False)
        if not self.fields:
            self.new_field()
            return
        ids = [f["id"] for f in self.fields]
        row = ids.index(select_id) if select_id in ids else 0
        self.list.setCurrentRow(row)
        self._on_row_changed(row)

    def _on_row_changed(self, row: int) -> None:
        if 0 <= row < len(self.fields):
            self._edit(dict(self.fields[row]))

    def new_field(self, _checked: bool = False) -> None:
        self.list.blockSignals(True)
        self.list.clearSelection()
        self.list.setCurrentRow(-1)
        self.list.blockSignals(False)
        self._edit({"id": NEW_ID, "name": "", "field_type": "text", "config": {}, "description": "",
                    "required": False, "default": None, "show_on_card": True})
        self.name_input.setFocus()

    def _edit(self, f: Dict[str, Any]) -> None:
        self.current = f
        self.error_lbl.setText("")
        self.name_input.setText(f["name"])
        self.desc_input.setText(f.get("description") or "")
        self.required_chk.setChecked(bool(f.get("required")))
        self.card_chk.setChecked(bool(f.get("show_on_card", True)))
        self.type_combo.blockSignals(True)
        idx = self.type_combo.findData(f["field_type"])
        if idx < 0:  # type not installed: show it, but it can't be configured
            self.type_combo.addItem(f"❔ {f['field_type']} (unavailable)", f["field_type"])
            idx = self.type_combo.count() - 1
        self.type_combo.setCurrentIndex(idx)
        self.type_combo.blockSignals(False)
        self._build_settings(f["field_type"], f.get("config") or {})
        self._build_default(f.get("default"))
        is_new = f["id"] == NEW_ID
        self.delete_btn.setEnabled(not is_new)
        self.up_btn.setEnabled(not is_new)
        self.down_btn.setEnabled(not is_new)
        self.save_btn.setText("Add Field" if is_new else "Save Field")
        if is_new:
            self.usage_lbl.setText("")
        else:
            n = self.service.count_field_values(f["id"])
            self.usage_lbl.setText(f"Used on {n} task{'s' if n != 1 else ''}")

    def _build_settings(self, type_key: str, config: Dict[str, Any]) -> None:
        if self.type_settings is not None:
            self.settings_holder.removeWidget(self.type_settings)
            self.type_settings.deleteLater()
        self.type_settings = TypeSettings(type_key, config)
        self.type_settings.changed.connect(self._refresh_default)
        self.settings_holder.addWidget(self.type_settings)
        self.type_help.setText(cf.get_field_type(type_key).description)

    def _build_default(self, value: Any) -> None:
        while self.default_row.rowCount():
            self.default_row.removeRow(0)
        self.default_editor = None
        try:
            config = self.type_settings.config() if self.type_settings else {}
        except FieldValueError:
            config = {}
        definition = {"id": 0, "name": "Default", "field_type": self.type_combo.currentData(), "config": config}
        if not cf.type_of(definition).editable:
            return
        people = self.service.get_people()
        self.default_editor = create_field_editor(definition, EditorContext(people=people))
        self.default_editor.set_value(value)
        self.default_editor.setToolTip("Given to new tasks on this board (optional)")
        self.default_row.addRow("Default value:", self.default_editor)

    def _refresh_default(self) -> None:
        """Settings changed (e.g. an option was added): rebuild the default editor, keeping its value."""
        try:
            value = self.default_editor.value() if self.default_editor else None
        except FieldValueError:
            value = None
        self._build_default(value)

    def _on_type_changed(self, _index: int) -> None:
        keep = self.current["config"] if self.current and self.type_combo.currentData() == \
            self.current["field_type"] else {}
        self._build_settings(self.type_combo.currentData(), keep)
        self._build_default(None)

    def _revert(self, _checked: bool = False) -> None:
        if self.current and self.current["id"] != NEW_ID:
            self._edit(next(dict(f) for f in self.fields if f["id"] == self.current["id"]))
        else:
            self.new_field()

    def form_values(self) -> Dict[str, Any]:
        """The edited definition (raises FieldValueError)."""
        name = cf.normalize_name(self.name_input.text())
        config = self.type_settings.config()
        type_key = self.type_combo.currentData()
        definition = {"id": 0, "name": name, "field_type": type_key, "config": config}
        default = None
        if self.default_editor is not None:
            try:
                default = self.default_editor.value()
            except FieldValueError as exc:
                raise FieldValueError(f"Default value: {exc}") from None
            if default is not None:
                try:
                    default = cf.validate_value(definition, default)  # e.g. options changed since
                except FieldValueError as exc:
                    raise FieldValueError(f"Default value: {exc}") from None
        return {"name": name, "field_type": type_key, "config": config, "description": self.desc_input.text(),
                "required": self.required_chk.isChecked(), "show_on_card": self.card_chk.isChecked(),
                "default": default}

    def save_current(self, _checked: bool = False, confirm: bool = True) -> Optional[Dict[str, Any]]:
        """Create/update the field being edited. Returns it, or None (invalid / cancelled)."""
        if self.current is None:
            return None
        try:
            values = self.form_values()
            if self.current["id"] == NEW_ID:
                saved = self.service.create_custom_field(self.board_id, **values)
            else:
                if values["field_type"] != self.current["field_type"] or values["config"] != self.current["config"]:
                    impact = self.service.preview_field_change(self.current["id"], values["field_type"],
                                                               values["config"])
                    if impact["lost"] and confirm and not self._confirm_loss(impact):
                        return None
                saved = self.service.update_custom_field(self.current["id"], **values)
        except (FieldValueError, ValueError) as exc:
            self.error_lbl.setText(f"⚠ {exc}")
            return None
        self.changed = True
        self.reload(select_id=saved["id"])
        return saved

    def _confirm_loss(self, impact: Dict[str, Any]) -> bool:
        lost = impact["lost"]
        shown = "\n".join(f"• {title}: {old}" for title, old in lost[:8]) + ("\n…" if len(lost) > 8 else "")
        reply = QMessageBox.warning(
            self, "Some values don't fit",
            f"{len(lost)} of {impact['values']} existing value{'s' if impact['values'] != 1 else ''} can't be "
            f"kept with the new settings and will be cleared:\n\n{shown}\n\nThe change is recorded in each "
            "task's history. Continue?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        return reply == QMessageBox.Yes

    def move_selected(self, step: int, _checked: bool = False) -> None:
        row = self.list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < len(self.fields):
            return
        ids = [f["id"] for f in self.fields]
        ids.insert(target, ids.pop(row))
        self.service.reorder_custom_fields(self.board_id, ids)
        self.changed = True
        self.reload(select_id=ids[target])

    def delete_selected(self, _checked: bool = False, confirm: bool = True) -> bool:
        if not self.current or self.current["id"] == NEW_ID:
            return False
        n = self.service.count_field_values(self.current["id"])
        if confirm:
            reply = QMessageBox.question(
                self, "Delete Field",
                f"Delete the field '{self.current['name']}'?"
                + (f"\n\nIts values on {n} task{'s' if n != 1 else ''} are removed (each task's history keeps "
                   "a note of the old value)." if n else ""),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return False
        self.service.delete_custom_field(self.current["id"])
        self.changed = True
        self.reload()
        return True

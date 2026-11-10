"""
Template dialogs: the new-board template gallery, saving a board as a template,
and managing / editing task templates
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QTime, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
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
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import templates as tpl
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.enums import Priority
from kanbanmanager.ui.colors import tint
from kanbanmanager.ui.components.field_editors import EditorContext, FieldsPanel
from kanbanmanager.ui.components.markdown_editor import MarkdownEditor
from kanbanmanager.ui.styles import CHECKBOX_QSS


def _pills(names: List[str], color: str = "#64748B") -> str:
    if not names:
        return "<span style='color:#64748B'>none</span>"
    return " ".join(f"<span style='background-color:{tint(color, 0.35)}; color:#E2E8F0;'>&nbsp;{html.escape(n)}&nbsp;"
                    f"</span>" for n in names)


# ---------------------------------------------------------------------------
# New board from a template
# ---------------------------------------------------------------------------
class NewBoardDialog(QDialog):
    """Pick a built-in or saved template, preview what it contains, name the board, create it."""

    def __init__(self, service: DatabaseService, workspace_id: int, parent: Optional[QWidget] = None,
                 preselect: Optional[str] = None):
        super().__init__(parent)
        self.service = service
        self.workspace_id = workspace_id
        self.templates: List[Dict[str, Any]] = []
        self.created_board_id: Optional[int] = None
        self.problems: List[str] = []
        self.color: Optional[str] = None
        self._name_edited = False
        self.setWindowTitle("Create New Board")
        self.setMinimumSize(900, 580)
        self.setStyleSheet(CHECKBOX_QSS)
        self._init_ui()
        self.reload(preselect or f"{tpl.BUILTIN_PREFIX}standard")

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        head = QLabel("Start from a template")
        head.setStyleSheet("font-size: 16px; font-weight: 800;")
        root.addWidget(head)

        body = QHBoxLayout()
        root.addLayout(body, 1)
        left = QVBoxLayout()
        body.addLayout(left, 2)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_row_changed)
        self.list.itemDoubleClicked.connect(self._create_from_item)
        left.addWidget(self.list, 1)
        lbtns = QHBoxLayout()
        left.addLayout(lbtns)
        self.import_btn = QPushButton("Import…")
        self.import_btn.setToolTip(f"Add a template from a {tpl.TEMPLATE_FILE_SUFFIX} file")
        self.import_btn.clicked.connect(self.import_template)
        lbtns.addWidget(self.import_btn)
        self.export_btn = QPushButton("Export…")
        self.export_btn.setToolTip("Save the selected template to a file you can share")
        self.export_btn.clicked.connect(self.export_template)
        lbtns.addWidget(self.export_btn)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setObjectName("DangerButton")
        self.delete_btn.setToolTip("Delete a template you saved (built-in ones can't be deleted)")
        self.delete_btn.clicked.connect(self.delete_template)
        lbtns.addWidget(self.delete_btn)

        right = QVBoxLayout()
        body.addLayout(right, 3)
        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.RichText)
        self.preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.preview.setStyleSheet("padding: 4px;")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self.preview)
        right.addWidget(scroll, 1)
        form = QFormLayout()
        right.addLayout(form)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Board name")
        self.name_input.textEdited.connect(self._on_name_edited)
        form.addRow("Board name *", self.name_input)
        color_row = QHBoxLayout()
        form.addRow("Colour:", color_row)
        self.color_btn = QPushButton("")
        self.color_btn.setFixedSize(40, 24)
        self.color_btn.clicked.connect(self.pick_color)
        color_row.addWidget(self.color_btn)
        color_row.addStretch()
        self.samples_chk = QCheckBox("Include the template's sample tasks")
        self.samples_chk.setChecked(True)
        form.addRow("", self.samples_chk)

        bottom = QHBoxLayout()
        root.addLayout(bottom)
        bottom.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        self.create_btn = QPushButton("Create Board")
        self.create_btn.setObjectName("PrimaryButton")
        self.create_btn.clicked.connect(self.create_board)
        bottom.addWidget(self.create_btn)

    def reload(self, select: Any = None) -> None:
        self.templates = self.service.get_board_templates()
        self.list.blockSignals(True)
        self.list.clear()
        self._rows: List[Optional[int]] = []  # list row -> template index (None for headers)
        category = None
        select_row = -1
        for i, t in enumerate(self.templates):
            if t["category"] != category:
                category = t["category"]
                header = QListWidgetItem(category.upper())
                header.setFlags(Qt.NoItemFlags)
                header.setForeground(Qt.gray)
                self.list.addItem(header)
                self._rows.append(None)
            item = QListWidgetItem(f"{t['icon']}  {t['name']}")
            item.setToolTip(t["description"])
            self.list.addItem(item)
            self._rows.append(i)
            if t["id"] == select:
                select_row = self.list.count() - 1
        self.list.blockSignals(False)
        if select_row < 0:
            select_row = next((r for r, idx in enumerate(self._rows) if idx is not None), -1)
        self.list.setCurrentRow(select_row)
        self._on_row_changed(select_row)

    def selected(self) -> Optional[Dict[str, Any]]:
        row = self.list.currentRow()
        if 0 <= row < len(self._rows) and self._rows[row] is not None:
            return self.templates[self._rows[row]]
        return None

    def select_template(self, template_id: Any) -> bool:
        for row, idx in enumerate(self._rows):
            if idx is not None and self.templates[idx]["id"] == template_id:
                self.list.setCurrentRow(row)
                return True
        return False

    def _on_name_edited(self, _text: str) -> None:
        self._name_edited = True

    def _on_row_changed(self, _row: int) -> None:
        t = self.selected()
        self.create_btn.setEnabled(t is not None)
        self.export_btn.setEnabled(t is not None)
        self.delete_btn.setEnabled(t is not None and not t["builtin"])
        if t is None:
            self.preview.setText("")
            return
        info = tpl.describe_board_template(t["data"])
        fields = ", ".join(f"{html.escape(n)} <span style='color:#64748B'>({html.escape(k)})</span>"
                           for n, k in info["fields"]) or "<span style='color:#64748B'>none</span>"
        parts = [
            f"<div style='font-size:18px; font-weight:800;'>{t['icon']} {html.escape(t['name'])}</div>",
            f"<p style='color:#94A3B8'>{html.escape(t['description'] or '')}</p>",
            f"<p><b>Columns</b><br>{_pills(info['columns'], t['color'])}</p>",
            f"<p><b>Labels</b><br>{_pills(info['labels'])}</p>",
            f"<p><b>Custom fields</b><br>{fields}</p>",
        ]
        if info["task_templates"]:
            parts.append(f"<p><b>Task templates</b><br>{_pills(info['task_templates'], '#8B5CF6')}</p>")
        if info["rules"]:
            parts.append("<p><b>Automation rules</b><br>" + "<br>".join(f"⚡ {html.escape(r)}" for r in info["rules"])
                         + "</p>")
        if not t["builtin"]:
            who = f" by {html.escape(t['created_by'])}" if t.get("created_by") else ""
            parts.append(f"<p style='color:#64748B'>Saved template{who}</p>")
        self.preview.setText("".join(parts))
        self.samples_chk.setEnabled(info["tasks"] > 0)
        self.samples_chk.setText(f"Include the template's {info['tasks']} sample task{'s' if info['tasks'] != 1 else ''}"
                                 if info["tasks"] else "No sample tasks in this template")
        if not self._name_edited:
            self.name_input.setText(t["name"] if t["id"] != f"{tpl.BUILTIN_PREFIX}standard" else "")
        self.color = t["color"]
        self._paint_color()

    def _paint_color(self) -> None:
        self.color_btn.setStyleSheet(f"background-color: {self.color}; border: 1px solid #475569; border-radius: 4px;")

    def pick_color(self, _checked: bool = False) -> None:
        c = QColorDialog.getColor(self.color or "#3B82F6", self, "Board colour")
        if c.isValid():
            self.color = c.name()
            self._paint_color()

    def _create_from_item(self, _item: QListWidgetItem) -> None:
        if self.selected() is not None and self.name_input.text().strip():
            self.create_board()

    def create_board(self, _checked: bool = False) -> Optional[int]:
        t = self.selected()
        name = self.name_input.text().strip()
        if t is None:
            return None
        if not name:
            QMessageBox.warning(self, "Board name", "Give the new board a name.")
            self.name_input.setFocus()
            return None
        try:
            board = self.service.create_board_from_template(
                self.workspace_id, t, name=name, color=self.color,
                include_sample_tasks=self.samples_chk.isChecked() and self.samples_chk.isEnabled())
        except (tpl.TemplateError, ValueError) as exc:
            QMessageBox.critical(self, "Create Board", f"Could not create the board:\n{exc}")
            return None
        self.created_board_id = board.id
        self.problems = list(getattr(board, "template_problems", []) or [])
        if self.problems:
            QMessageBox.warning(self, "Board created", "The board was created, but some parts of the template "
                                "could not be applied:\n\n" + "\n".join(self.problems))
        self.accept()
        return board.id

    def import_template(self, _checked: bool = False, path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import Board Template", "",
                                                  f"Board templates (*{tpl.TEMPLATE_FILE_SUFFIX} *.json)")
        if not path:
            return None
        try:
            data = tpl.read_template_file(path)
            saved = self.service.save_board_template(data["name"], data["data"], description=data["description"],
                                                     icon=data["icon"], color=data["color"],
                                                     category=data["category"])
        except (tpl.TemplateError, ValueError) as exc:
            QMessageBox.warning(self, "Import Template", str(exc))
            return None
        self.reload(saved["id"])
        return saved

    def export_template(self, _checked: bool = False, path: Optional[str] = None) -> Optional[Path]:
        t = self.selected()
        if t is None:
            return None
        if path is None:
            safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in t["name"]).strip() or "template"
            path, _ = QFileDialog.getSaveFileName(self, "Export Board Template", f"{safe}{tpl.TEMPLATE_FILE_SUFFIX}",
                                                  f"Board templates (*{tpl.TEMPLATE_FILE_SUFFIX})")
        if not path:
            return None
        return tpl.export_template_file(t, path)

    def delete_template(self, _checked: bool = False, confirm: bool = True) -> bool:
        t = self.selected()
        if t is None or t["builtin"]:
            return False
        if confirm and QMessageBox.question(self, "Delete Template", f"Delete the template '{t['name']}'? Boards "
                                            "created from it are not affected.",
                                            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return False
        self.service.delete_board_template(t["id"])
        self.reload()
        return True


class SaveBoardTemplateDialog(QDialog):
    """Save the structure of a board (optionally with its open tasks) as a template."""

    def __init__(self, service: DatabaseService, board_id: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.saved: Optional[Dict[str, Any]] = None
        board = service.get_board(board_id)
        self.setWindowTitle("Save Board as Template")
        self.setMinimumWidth(480)
        self.setStyleSheet(CHECKBOX_QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        intro = QLabel("A template keeps the board's columns, WIP limits, labels, custom fields and task "
                       "templates. Use it from <b>New Board</b> - or export it to share.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #94A3B8;")
        root.addWidget(intro)
        form = QFormLayout()
        root.addLayout(form)
        self.name_input = QLineEdit(f"{board.name} template" if board else "")
        form.addRow("Name *", self.name_input)
        self.icon_input = QLineEdit("📋")
        self.icon_input.setMaxLength(4)
        self.icon_input.setFixedWidth(58)
        self.icon_input.setAlignment(Qt.AlignCenter)
        form.addRow("Icon:", self.icon_input)
        self.desc_input = QPlainTextEdit(board.description if board else "")
        self.desc_input.setFixedHeight(70)
        form.addRow("Description:", self.desc_input)
        n_rules = len(service.get_automation_rules(board_id))
        open_tasks = [t for t in service.get_tasks_for_board(board_id) if not t.is_completed]
        self.rules_chk = QCheckBox(f"Include automation rules and policies ({n_rules} rule{'s' if n_rules != 1 else ''})")
        self.rules_chk.setChecked(True)
        form.addRow("", self.rules_chk)
        self.tasks_chk = QCheckBox(f"Include the {len(open_tasks)} open task{'s' if len(open_tasks) != 1 else ''} "
                                   "as samples (no dates, comments or attachments)")
        self.tasks_chk.setEnabled(bool(open_tasks))
        form.addRow("", self.tasks_chk)
        btns = QHBoxLayout()
        root.addLayout(btns)
        btns.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("Save Template")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self.save)
        btns.addWidget(save)

    def save(self, _checked: bool = False) -> Optional[Dict[str, Any]]:
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Template name", "Give the template a name.")
            return None
        try:
            self.saved = self.service.save_board_as_template(
                self.board_id, name, description=self.desc_input.toPlainText(),
                include_tasks=self.tasks_chk.isChecked(), include_automation=self.rules_chk.isChecked(),
                icon=self.icon_input.text().strip() or "📋")
        except (tpl.TemplateError, ValueError) as exc:
            QMessageBox.warning(self, "Save Template", str(exc))
            return None
        self.accept()
        return self.saved


# ---------------------------------------------------------------------------
# Task templates
# ---------------------------------------------------------------------------
class TaskTemplateEditor(QDialog):
    """Create or edit one task template."""

    def __init__(self, service: DatabaseService, board_id: int, template: Optional[Dict[str, Any]] = None,
                 parent: Optional[QWidget] = None, is_new: Optional[bool] = None):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.template = template or {"name": "", "icon": "📋", "data": {}, "board_id": board_id}
        self.is_new = is_new if is_new is not None else not self.template.get("id")
        self.saved: Optional[Dict[str, Any]] = None
        self.setWindowTitle("New Task Template" if self.is_new else f"Edit Template - {self.template['name']}")
        self.setMinimumSize(760, 600)
        self.setStyleSheet(CHECKBOX_QSS)
        self.fields = service.get_custom_fields(board_id)
        self._other_fields: Dict[str, Any] = {}  # values for fields this board doesn't have (kept)
        self._init_ui()
        self._load(tpl.normalize_task_template(self.template.get("data")))

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        top = QFormLayout()
        root.addLayout(top)
        name_row = QHBoxLayout()
        top.addRow("Template name *", name_row)
        self.icon_input = QLineEdit(self.template.get("icon") or "📋")
        self.icon_input.setMaxLength(4)
        self.icon_input.setFixedWidth(58)
        self.icon_input.setAlignment(Qt.AlignCenter)
        self.icon_input.setToolTip("Icon (an emoji)")
        name_row.addWidget(self.icon_input)
        self.name_input = QLineEdit(self.template.get("name") or "")
        self.name_input.setPlaceholderText("e.g. Bug report")
        name_row.addWidget(self.name_input, 1)
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("This board only", self.board_id)
        self.scope_combo.addItem("All boards", None)
        self.scope_combo.setCurrentIndex(0 if self.template.get("board_id") else 1)
        name_row.addWidget(self.scope_combo)
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Title of new tasks, e.g. 'Bug: ' or 'Status report - week {week}'")
        top.addRow("Task title:", self.title_input)
        ph = QLabel(f"Placeholders: {tpl.PLACEHOLDERS}")
        ph.setStyleSheet("color: #64748B; font-size: 11px;")
        top.addRow("", ph)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # Details ----------------------------------------------------------------
        details = QWidget()
        dl = QHBoxLayout(details)
        grid = QFormLayout()
        dl.addLayout(grid, 3)
        label_box = QVBoxLayout()
        dl.addLayout(label_box, 2)
        self.column_combo = QComboBox()
        self.column_combo.addItem("(where it is added)", None)
        for c in self.service.get_columns_for_board(self.board_id):
            self.column_combo.addItem(c.name, c.name)
        grid.addRow("Column:", self.column_combo)
        self.priority_combo = QComboBox()
        self.priority_combo.addItem("(default: Medium)", None)
        for p in Priority:
            self.priority_combo.addItem(f"{p.icon_char} {p.display_name}", p.value)
        grid.addRow("Priority:", self.priority_combo)
        self.assignee_combo = QComboBox()
        self.assignee_combo.setEditable(True)
        self.assignee_combo.setInsertPolicy(QComboBox.NoInsert)
        self.assignee_combo.addItem("")
        self.assignee_combo.addItem("@me")
        for name in self.service.get_people():
            self.assignee_combo.addItem(name)
        self.assignee_combo.lineEdit().setPlaceholderText("Unassigned  (@me = whoever uses the template)")
        grid.addRow("Assignee:", self.assignee_combo)
        self.hours_spin = QDoubleSpinBox()
        self.hours_spin.setMaximumWidth(140)
        self.hours_spin.setRange(0, 999)
        self.hours_spin.setSingleStep(0.5)
        self.hours_spin.setSuffix(" h")
        grid.addRow("Estimate:", self.hours_spin)
        due_row = QHBoxLayout()
        grid.addRow("Due:", due_row)
        self.due_chk = QCheckBox("in")
        due_row.addWidget(self.due_chk)
        self.due_spin = QSpinBox()
        self.due_spin.setRange(0, 3650)
        due_row.addWidget(self.due_spin)
        self.working_chk = QCheckBox("working days, at")
        self.working_chk.setChecked(True)
        due_row.addWidget(self.working_chk)
        self.due_time = QTimeEdit(QTime(17, 0))
        self.due_time.setDisplayFormat("HH:mm")
        due_row.addWidget(self.due_time)
        due_row.addStretch()
        start_row = QHBoxLayout()
        grid.addRow("Start:", start_row)
        self.start_chk = QCheckBox("in")
        start_row.addWidget(self.start_chk)
        self.start_spin = QSpinBox()
        self.start_spin.setRange(0, 3650)
        start_row.addWidget(self.start_spin)
        start_row.addWidget(QLabel("days"))
        start_row.addStretch()
        self.milestone_chk = QCheckBox("◆ Milestone")
        grid.addRow("", self.milestone_chk)
        label_box.addWidget(QLabel("Labels:"))
        self.labels_list = QListWidget()
        for lbl in self.service.get_labels_for_board(self.board_id):
            item = QListWidgetItem(f"🏷 {lbl.name}")
            item.setData(Qt.UserRole, lbl.name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.labels_list.addItem(item)
        label_box.addWidget(self.labels_list, 1)
        self.tabs.addTab(details, "Details")

        # Description & checklist -----------------------------------------------------
        self.desc_editor = MarkdownEditor(placeholder="Description of new tasks (Markdown). Placeholders work here too.")
        self.tabs.addTab(self.desc_editor, "Description")
        check = QWidget()
        cl = QVBoxLayout(check)
        cl.addWidget(QLabel("Checklist items, one per line:"))
        self.checklist_edit = QPlainTextEdit()
        cl.addWidget(self.checklist_edit, 1)
        self.tabs.addTab(check, "Checklist")

        # Field values --------------------------------------------------------------------
        self.fields_panel = FieldsPanel(EditorContext(people=self.service.get_people() + ["{user}"]),
                                        empty_text="This board has no custom fields.", show_manage=False)
        self.fields_panel.set_fields([dict(f, required=False) for f in self.fields])
        self.tabs.addTab(self.fields_panel, "Field values")

        self.summary = QLabel("")
        self.summary.setStyleSheet("color: #94A3B8;")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.error_lbl = QLabel("")
        self.error_lbl.setStyleSheet("color: #F87171;")
        root.addWidget(self.error_lbl)
        btns = QHBoxLayout()
        root.addLayout(btns)
        btns.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("Save Template")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self.save)
        btns.addWidget(save)
        for w in (self.priority_combo, self.column_combo):
            w.currentIndexChanged.connect(self._update_summary)
        for w in (self.due_chk, self.start_chk, self.milestone_chk, self.working_chk):
            w.toggled.connect(self._update_summary)
        for w in (self.due_spin, self.start_spin):
            w.valueChanged.connect(self._update_summary)
        self.hours_spin.valueChanged.connect(self._update_summary)
        self.assignee_combo.editTextChanged.connect(self._update_summary)
        self.checklist_edit.textChanged.connect(self._update_summary)
        self.labels_list.itemChanged.connect(self._update_summary)
        self.fields_panel.changed.connect(self._update_summary)

    def _load(self, d: Dict[str, Any]) -> None:
        self.title_input.setText(d["title"])
        self.desc_editor.set_markdown(d["description"])
        idx = self.column_combo.findData(d["column"])
        if d["column"] and idx < 0:
            self.column_combo.addItem(f"{d['column']} (not on this board)", d["column"])
            idx = self.column_combo.count() - 1
        self.column_combo.setCurrentIndex(max(0, idx))
        self.priority_combo.setCurrentIndex(max(0, self.priority_combo.findData(d["priority"])))
        self.assignee_combo.setCurrentText(d["assignee"])
        self.hours_spin.setValue(d["estimated_hours"])
        self.due_chk.setChecked(d["due_in_days"] is not None)
        self.due_spin.setValue(max(0, d["due_in_days"] or 0))
        self.working_chk.setChecked(d["working_days"])
        hh, mm = (int(x) for x in d["due_time"].split(":"))
        self.due_time.setTime(QTime(hh, mm))
        self.start_chk.setChecked(d["start_in_days"] is not None)
        self.start_spin.setValue(max(0, d["start_in_days"] or 0))
        self.milestone_chk.setChecked(d["is_milestone"])
        have = {self.labels_list.item(i).data(Qt.UserRole).lower() for i in range(self.labels_list.count())}
        for name in d["labels"]:
            if name.lower() not in have:
                item = QListWidgetItem(f"🏷 {name}  (created when used)")
                item.setData(Qt.UserRole, name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                self.labels_list.addItem(item)
        wanted = {n.lower() for n in d["labels"]}
        for i in range(self.labels_list.count()):
            item = self.labels_list.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole).lower() in wanted else Qt.Unchecked)
        self.checklist_edit.setPlainText("\n".join(d["checklist"]))
        by_name = {f["name"].lower(): f["id"] for f in self.fields}
        self.fields_panel.set_values({by_name[k.lower()]: v for k, v in d["fields"].items() if k.lower() in by_name})
        self._other_fields = {k: v for k, v in d["fields"].items() if k.lower() not in by_name}
        self._update_summary()

    def data(self) -> Dict[str, Any]:
        names = {f["id"]: f["name"] for f in self.fields}
        fields = dict(self._other_fields)  # values for fields of other boards (global templates) are kept
        fields.update({names[k]: v for k, v in self.fields_panel.raw_values().items() if v is not None})
        return tpl.normalize_task_template({
            "title": self.title_input.text(),
            "description": self.desc_editor.markdown(),
            "priority": self.priority_combo.currentData(),
            "labels": [self.labels_list.item(i).data(Qt.UserRole) for i in range(self.labels_list.count())
                       if self.labels_list.item(i).checkState() == Qt.Checked],
            "checklist": self.checklist_edit.toPlainText().splitlines(),
            "estimated_hours": self.hours_spin.value(),
            "assignee": self.assignee_combo.currentText(),
            "due_in_days": self.due_spin.value() if self.due_chk.isChecked() else None,
            "due_time": self.due_time.time().toString("HH:mm"),
            "working_days": self.working_chk.isChecked(),
            "start_in_days": self.start_spin.value() if self.start_chk.isChecked() else None,
            "fields": fields,
            "column": self.column_combo.currentData(),
            "is_milestone": self.milestone_chk.isChecked(),
        })

    def _update_summary(self, *_args) -> None:
        self.summary.setText("Sets: " + tpl.describe_task_template(self.data()))

    def save(self, _checked: bool = False) -> Optional[Dict[str, Any]]:
        errors = self.fields_panel.errors()
        if errors:
            self.tabs.setCurrentWidget(self.fields_panel)
            self.error_lbl.setText("⚠ " + "; ".join(errors))
            return None
        try:
            self.saved = self.service.save_task_template(
                self.name_input.text(), self.data(), board_id=self.scope_combo.currentData(),
                icon=self.icon_input.text().strip() or "📋",
                template_id=None if self.is_new else self.template.get("id"))
        except ValueError as exc:
            self.error_lbl.setText(f"⚠ {exc}")
            return None
        self.accept()
        return self.saved


class TaskTemplatesDialog(QDialog):
    """The task templates available on a board (its own and the "all boards" ones)."""

    def __init__(self, service: DatabaseService, board_id: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.templates: List[Dict[str, Any]] = []
        self.changed = False
        self.use_requested: Optional[Dict[str, Any]] = None  # template chosen with "New Task"
        board = service.get_board(board_id)
        self.setWindowTitle(f"Task Templates - {board.name if board else ''}")
        self.setMinimumSize(640, 420)
        self.setStyleSheet(CHECKBOX_QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        intro = QLabel("Templates pre-fill new tasks: title, description, checklist, labels, field values, "
                       "assignee and due date. Use them from a column's ⋮ menu or the task editor.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #94A3B8;")
        root.addWidget(intro)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self.edit_selected)
        self.list.currentRowChanged.connect(self._update_buttons)
        root.addWidget(self.list, 1)
        btns = QHBoxLayout()
        root.addLayout(btns)
        self.new_btn = QPushButton("＋ New")
        self.new_btn.setObjectName("PrimaryButton")
        self.new_btn.clicked.connect(self.new_template)
        btns.addWidget(self.new_btn)
        self.edit_btn = QPushButton("Edit")
        self.edit_btn.clicked.connect(self.edit_selected)
        btns.addWidget(self.edit_btn)
        self.dup_btn = QPushButton("Duplicate")
        self.dup_btn.clicked.connect(self.duplicate_selected)
        btns.addWidget(self.dup_btn)
        self.del_btn = QPushButton("Delete")
        self.del_btn.setObjectName("DangerButton")
        self.del_btn.clicked.connect(self.delete_selected)
        btns.addWidget(self.del_btn)
        btns.addStretch()
        self.use_btn = QPushButton("New Task from Template")
        self.use_btn.clicked.connect(self.use_selected)
        btns.addWidget(self.use_btn)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        self.reload()

    def reload(self, select_id: Optional[int] = None) -> None:
        self.templates = self.service.get_task_templates(self.board_id)
        self.list.clear()
        for t in self.templates:
            scope = "" if t["board_id"] else "  ·  all boards"
            item = QListWidgetItem(f"{t['icon']}  {t['name']}{scope}\n      {tpl.describe_task_template(t['data'])}")
            item.setData(Qt.UserRole, t["id"])
            self.list.addItem(item)
        ids = [t["id"] for t in self.templates]
        self.list.setCurrentRow(ids.index(select_id) if select_id in ids else (0 if ids else -1))
        self._update_buttons()

    def _update_buttons(self, *_args) -> None:
        has = self.selected() is not None
        for b in (self.edit_btn, self.dup_btn, self.del_btn, self.use_btn):
            b.setEnabled(has)

    def selected(self) -> Optional[Dict[str, Any]]:
        row = self.list.currentRow()
        return self.templates[row] if 0 <= row < len(self.templates) else None

    def _run_editor(self, template: Optional[Dict[str, Any]], is_new: bool) -> Optional[Dict[str, Any]]:
        dlg = TaskTemplateEditor(self.service, self.board_id, template, self, is_new=is_new)
        saved = dlg.saved if dlg.exec() else None
        dlg.deleteLater()
        if saved:
            self.changed = True
            self.reload(saved["id"])
        return saved

    def new_template(self, _checked: bool = False) -> Optional[Dict[str, Any]]:
        return self._run_editor(None, True)

    def edit_selected(self, *_args) -> Optional[Dict[str, Any]]:
        t = self.selected()
        return self._run_editor(t, False) if t else None

    def duplicate_selected(self, _checked: bool = False) -> Optional[Dict[str, Any]]:
        t = self.selected()
        if t is None:
            return None
        names = {x["name"].lower() for x in self.templates}
        base = re.sub(r" \(copy(?: \d+)?\)$", "", t["name"])  # a copy of a copy is "X (copy 2)"
        n, name = 2, f"{base} (copy)"
        while name.lower() in names:
            name = f"{base} (copy {n})"
            n += 1
        saved = self.service.save_task_template(name, t["data"], board_id=t["board_id"], icon=t["icon"])
        self.changed = True
        self.reload(saved["id"])
        return saved

    def delete_selected(self, _checked: bool = False, confirm: bool = True) -> bool:
        t = self.selected()
        if t is None:
            return False
        where = "every board" if t["board_id"] is None else "this board"
        if confirm and QMessageBox.question(self, "Delete Template", f"Delete '{t['name']}' (used on {where})?",
                                            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return False
        self.service.delete_task_template(t["id"])
        self.changed = True
        self.reload()
        return True

    def use_selected(self, _checked: bool = False) -> None:
        self.use_requested = self.selected()
        if self.use_requested:
            self.accept()

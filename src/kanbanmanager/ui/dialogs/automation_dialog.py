"""
Board Automations Dialog: Rules, Policies (auto-archive & stale tasks) and History

Changes apply immediately (like toggling a setting); the rule editor saves on OK.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import automation as au
from kanbanmanager.core.signals import app_signals
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.enums import Priority
from kanbanmanager.ui.styles import CHECKBOX_QSS

STATUS_COLORS = {"success": "#10B981", "partial": "#F59E0B", "failed": "#EF4444", "skipped": "#94A3B8"}


def _local(iso: Optional[str]) -> str:
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone()
    return dt.strftime("%b %d, %H:%M")


# ---------------------------------------------------------------------------
# Parameter widgets (built from the engine's Param specs)
# ---------------------------------------------------------------------------
class ParamWidgets:
    def __init__(self, ctx: au.BoardContext, people: List[str]):
        self.ctx = ctx
        self.people = people

    def build(self, param: au.Param, value: Any, allow_trigger_actor: bool = False) -> QWidget:
        kind = param.kind
        if kind in ("column", "priority", "label"):
            box = QComboBox()
            if param.optional:
                box.addItem({"column": "any column", "priority": "any priority", "label": "any label"}[kind], None)
            if kind == "column":
                for cid, col in sorted(self.ctx.columns.items(), key=lambda kv: kv[1]["position"]):
                    box.addItem(col["name"], cid)
            elif kind == "priority":
                for p in Priority:
                    box.addItem(f"{p.icon_char} {p.display_name}", p.value)
            else:
                for lid, name in sorted(self.ctx.labels.items(), key=lambda kv: kv[1].lower()):
                    box.addItem(f"🏷 {name}", lid)
            idx = box.findData(value)
            if value not in (None, "") and idx < 0:
                box.addItem("⚠ (deleted)", value)  # keep the broken reference visible
                idx = box.count() - 1
            box.setCurrentIndex(max(0, idx))
            return box
        if kind in ("field", "template"):
            box = QComboBox()
            if param.optional:
                box.addItem("any field" if kind == "field" else "any template", None)
            if kind == "field":
                for fid, f in sorted(self.ctx.fields.items(), key=lambda kv: kv[1].get("position", 0)):
                    box.addItem(f"{f.get('icon', '🧩')} {f['name']}", fid)
            else:
                for tid, name in sorted(self.ctx.templates.items(), key=lambda kv: kv[1].lower()):
                    box.addItem(f"📋 {name}", tid)
            idx = box.findData(value)
            if value not in (None, "") and idx < 0:
                box.addItem("⚠ (deleted)", value)
                idx = box.count() - 1
            if box.count() == 0:
                box.addItem("(none on this board yet)", None)
            box.setCurrentIndex(max(0, idx))
            return box
        if kind == "person":
            box = QComboBox()
            box.setEditable(True)
            box.setInsertPolicy(QComboBox.NoInsert)
            if param.optional:
                box.addItem("anyone", None)
            if allow_trigger_actor:
                box.addItem("👤 the person who triggered it", au.ACTOR_FOR_TRIGGER)
            for name in self.people:
                box.addItem(name, name)
            idx = box.findData(value)
            if idx >= 0:
                box.setCurrentIndex(idx)
            elif value:
                box.setEditText(str(value))
            return box
        if kind == "int":
            spin = QSpinBox()
            spin.setRange(param.minimum, param.maximum)
            spin.setSuffix(param.suffix)
            spin.setValue(int(value if value not in (None, "") else (param.default or param.minimum)))
            return spin
        if kind == "multiline":
            edit = QPlainTextEdit(str(value or ""))
            edit.setFixedHeight(56)
            edit.setPlaceholderText(param.label + (" - placeholders: " + au.PLACEHOLDERS
                                                   if param.key == "text" else ""))
            return edit
        edit = QLineEdit(str(value or ""))
        edit.setPlaceholderText(param.label)
        return edit

    @staticmethod
    def read(widget: QWidget) -> Any:
        if isinstance(widget, QComboBox):
            if widget.isEditable():
                data = widget.currentData()
                text = widget.currentText().strip()
                if data is not None and widget.itemText(widget.currentIndex()) == text:
                    return data
                return text or None
            return widget.currentData()
        if isinstance(widget, QSpinBox):
            return widget.value()
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText()
        if isinstance(widget, QLineEdit):
            return widget.text()
        return None

    @staticmethod
    def connect(widget: QWidget, slot) -> None:
        if isinstance(widget, QComboBox):
            widget.currentIndexChanged.connect(slot)
            if widget.isEditable():
                widget.editTextChanged.connect(slot)
        elif isinstance(widget, QSpinBox):
            widget.valueChanged.connect(slot)
        elif isinstance(widget, (QLineEdit, QPlainTextEdit)):
            widget.textChanged.connect(slot)


class _Row(QFrame):
    changed = Signal()
    removed = Signal(object)

    def __init__(self, params: ParamWidgets, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.params = params
        self.setObjectName("RuleRow")
        self.setStyleSheet("#RuleRow { background: #0F172A; border: 1px solid #334155; border-radius: 6px; }")
        self.layout_ = QHBoxLayout(self)
        self.layout_.setContentsMargins(6, 4, 6, 4)
        self.layout_.setSpacing(6)
        self.kind_combo = QComboBox()
        self.layout_.addWidget(self.kind_combo)
        self.param_holder = QHBoxLayout()
        self.layout_.addLayout(self.param_holder, 1)
        self.remove_btn = QToolButton()
        self.remove_btn.setText("✕")
        self.remove_btn.setToolTip("Remove")
        self.remove_btn.clicked.connect(lambda: self.removed.emit(self))
        self.layout_.addWidget(self.remove_btn)
        self._widgets: Dict[str, QWidget] = {}

    def _clear_params(self) -> None:
        while self.param_holder.count():
            item = self.param_holder.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._widgets = {}

    def _add_param(self, key: str, widget: QWidget, label: str = "") -> None:
        if label:
            lbl = QLabel(label)
            lbl.setStyleSheet("color: #94A3B8; border: none;")
            self.param_holder.addWidget(lbl)
        self.param_holder.addWidget(widget, 1)
        self.params.connect(widget, self.changed.emit)
        self._widgets[key] = widget


class ConditionRow(_Row):
    def __init__(self, params: ParamWidgets, cond: Optional[Dict[str, Any]] = None, parent=None):
        super().__init__(params, parent)
        for f, spec in au.CONDITIONS.items():
            self.kind_combo.addItem(spec.label, f)
        self.subject_holder = QHBoxLayout()
        self.layout_.insertLayout(1, self.subject_holder)
        self.subject_widget: Optional[QWidget] = None
        self.op_combo = QComboBox()
        self.layout_.insertWidget(2, self.op_combo)
        cond = cond or {"field": "priority", "op": "is"}
        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(cond.get("field"))))
        self._rebuild(cond)
        self.kind_combo.currentIndexChanged.connect(lambda: (self._rebuild({}), self.changed.emit()))
        self.op_combo.currentIndexChanged.connect(lambda: (self._rebuild_value(None), self.changed.emit()))

    def _spec(self) -> au.ConditionSpec:
        return au.CONDITIONS[self.kind_combo.currentData()]

    def _rebuild(self, cond: Dict[str, Any]) -> None:
        spec = self._spec()
        if self.subject_widget is not None:
            self.subject_holder.removeWidget(self.subject_widget)
            self.subject_widget.deleteLater()
            self.subject_widget = None
        if spec.subject is not None:
            self.subject_widget = self.params.build(spec.subject, cond.get(spec.subject.key))
            self.subject_holder.addWidget(self.subject_widget)
            self.params.connect(self.subject_widget, self.changed.emit)
        self.op_combo.blockSignals(True)
        self.op_combo.clear()
        for op, label in spec.ops:
            self.op_combo.addItem(label, op)
        self.op_combo.setCurrentIndex(max(0, self.op_combo.findData(cond.get("op"))))
        self.op_combo.blockSignals(False)
        self._rebuild_value(cond.get("value"))

    def _rebuild_value(self, value: Any) -> None:
        self._clear_params()
        spec = self._spec()
        if spec.value is not None and self.op_combo.currentData() not in spec.valueless_ops:
            self._add_param("value", self.params.build(spec.value, value))

    def value(self) -> Dict[str, Any]:
        out = {"field": self.kind_combo.currentData(), "op": self.op_combo.currentData()}
        spec = self._spec()
        if spec.subject is not None and self.subject_widget is not None:
            out[spec.subject.key] = self.params.read(self.subject_widget)
        if "value" in self._widgets:
            out["value"] = self.params.read(self._widgets["value"])
        return out


class ActionRow(_Row):
    def __init__(self, params: ParamWidgets, action: Optional[Dict[str, Any]] = None, parent=None):
        super().__init__(params, parent)
        for t, spec in au.ACTIONS.items():
            self.kind_combo.addItem(spec.label, t)
        action = action or {"type": "add_comment"}
        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(action.get("type"))))
        self._rebuild(action)
        self.kind_combo.currentIndexChanged.connect(lambda: (self._rebuild({}), self.changed.emit()))

    def _rebuild(self, action: Dict[str, Any]) -> None:
        self._clear_params()
        spec = au.ACTIONS[self.kind_combo.currentData()]
        for p in spec.params:
            widget = self.params.build(p, action.get(p.key, p.default), allow_trigger_actor=spec.type == "assign")
            self._add_param(p.key, widget, p.label if p.kind == "int" else "")

    def value(self) -> Dict[str, Any]:
        out = {"type": self.kind_combo.currentData()}
        for key, w in self._widgets.items():
            out[key] = self.params.read(w)
        return out


# ---------------------------------------------------------------------------
# Rule editor
# ---------------------------------------------------------------------------
class RuleEditorDialog(QDialog):
    def __init__(self, service: DatabaseService, board_id: int, ctx: au.BoardContext,
                 rule: Optional[Dict[str, Any]] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.ctx = ctx
        self.rule = rule or {}
        self.saved_rule: Optional[Dict[str, Any]] = None
        self.params = ParamWidgets(ctx, service.get_people())
        self.setWindowTitle("Edit Rule" if rule and rule.get("id") else "New Automation Rule")
        self.setStyleSheet(CHECKBOX_QSS)
        self.resize(760, 640)
        self._init_ui()
        self._load()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Name:"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. Escalate overdue bugs")
        top.addWidget(self.name_input, 1)
        self.enabled_chk = QCheckBox("Enabled")
        self.enabled_chk.setChecked(True)
        top.addWidget(self.enabled_chk)
        layout.addLayout(top)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)

        when = QGroupBox("WHEN")
        wl = QHBoxLayout(when)
        self.trigger_combo = QComboBox()
        for t, spec in au.TRIGGERS.items():
            self.trigger_combo.addItem(("⏰ " if spec.scheduled else "⚡ ") + spec.label, t)
        self.trigger_combo.setToolTip("⚡ fires immediately when something happens · ⏰ is checked every few minutes")
        wl.addWidget(self.trigger_combo)
        self.trigger_params = QHBoxLayout()
        wl.addLayout(self.trigger_params, 1)
        bl.addWidget(when)
        self._trigger_widgets: Dict[str, QWidget] = {}

        self.if_box = QGroupBox("IF (all must match - optional)")
        il = QVBoxLayout(self.if_box)
        self.cond_list = QVBoxLayout()
        il.addLayout(self.cond_list)
        add_cond = QPushButton("+ Add condition")
        add_cond.clicked.connect(lambda: self._add_condition(None))
        il.addWidget(add_cond, 0, Qt.AlignLeft)
        bl.addWidget(self.if_box)

        then = QGroupBox("THEN")
        tl = QVBoxLayout(then)
        self.action_list = QVBoxLayout()
        tl.addLayout(self.action_list)
        add_act = QPushButton("+ Add action")
        add_act.clicked.connect(lambda: self._add_action(None))
        tl.addWidget(add_act, 0, Qt.AlignLeft)
        hint = QLabel(f"Comments & notifications can use placeholders: {au.PLACEHOLDERS}")
        hint.setStyleSheet("color: #64748B; font-size: 11px;")
        hint.setWordWrap(True)
        tl.addWidget(hint)
        bl.addWidget(then)
        bl.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        self.summary_lbl = QLabel("")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setStyleSheet("color: #CBD5E1; background: #0F172A; border: 1px solid #334155;"
                                       " border-radius: 6px; padding: 8px;")
        layout.addWidget(self.summary_lbl)
        self.problems_lbl = QLabel("")
        self.problems_lbl.setWordWrap(True)
        self.problems_lbl.setStyleSheet("color: #F87171;")
        layout.addWidget(self.problems_lbl)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.trigger_combo.currentIndexChanged.connect(lambda: (self._build_trigger_params({}), self._update()))
        self.name_input.textChanged.connect(self._update)
        self.enabled_chk.toggled.connect(self._update)

    def _load(self) -> None:
        r = self.rule
        self.name_input.setText(r.get("name", ""))
        self.enabled_chk.setChecked(r.get("is_enabled", True))
        trigger = r.get("trigger") or {"type": "task_moved"}
        self.trigger_combo.blockSignals(True)
        self.trigger_combo.setCurrentIndex(max(0, self.trigger_combo.findData(trigger.get("type"))))
        self.trigger_combo.blockSignals(False)
        self._build_trigger_params(trigger)
        for c in r.get("conditions") or []:
            self._add_condition(c)
        for a in r.get("actions") or [{"type": "add_comment"}]:
            self._add_action(a)
        self._update()

    def _build_trigger_params(self, trigger: Dict[str, Any]) -> None:
        while self.trigger_params.count():
            item = self.trigger_params.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._trigger_widgets = {}
        spec = au.TRIGGERS[self.trigger_combo.currentData()]
        for p in spec.params:
            lbl = QLabel(p.label)
            lbl.setStyleSheet("color: #94A3B8;")
            self.trigger_params.addWidget(lbl)
            w = self.params.build(p, trigger.get(p.key, p.default))
            self.params.connect(w, self._update)
            self.trigger_params.addWidget(w, 1)
            self._trigger_widgets[p.key] = w
        self.trigger_params.addStretch()

    def _add_condition(self, cond: Optional[Dict[str, Any]]) -> None:
        row = ConditionRow(self.params, cond)
        row.changed.connect(self._update)
        row.removed.connect(self._remove_row)
        self.cond_list.addWidget(row)
        self._update()

    def _add_action(self, action: Optional[Dict[str, Any]]) -> None:
        row = ActionRow(self.params, action)
        row.changed.connect(self._update)
        row.removed.connect(self._remove_row)
        self.action_list.addWidget(row)
        self._update()

    def _remove_row(self, row: QWidget) -> None:
        row.setParent(None)
        row.deleteLater()
        self._update()

    def _rows(self, layout: QVBoxLayout) -> List[_Row]:
        return [layout.itemAt(i).widget() for i in range(layout.count())
                if isinstance(layout.itemAt(i).widget(), _Row) and layout.itemAt(i).widget().parent() is not None]

    def value(self) -> Dict[str, Any]:
        trigger = {"type": self.trigger_combo.currentData()}
        for key, w in self._trigger_widgets.items():
            v = self.params.read(w)
            if v not in (None, ""):
                trigger[key] = v
        return {
            "name": self.name_input.text().strip(),
            "is_enabled": self.enabled_chk.isChecked(),
            "trigger": trigger,
            "conditions": [r.value() for r in self._rows(self.cond_list)],
            "actions": [r.value() for r in self._rows(self.action_list)],
        }

    def problems(self) -> List[str]:
        data = self.value()
        problems = au.validate_rule(data, self.ctx)
        if not data["name"]:
            problems.insert(0, "Give the rule a name")
        return problems

    def _update(self) -> None:
        data = self.value()
        self.summary_lbl.setText("📋 " + au.describe_rule(data, self.ctx))
        problems = self.problems()
        self.problems_lbl.setText("\n".join(f"• {p}" for p in problems))
        self.problems_lbl.setVisible(bool(problems))

    def save(self) -> None:
        problems = self.problems()
        if problems:
            self.problems_lbl.setText("\n".join(f"• {p}" for p in problems))
            self.problems_lbl.setVisible(True)
            return
        self.saved_rule = self.service.save_automation_rule(self.board_id, self.value(), self.rule.get("id"))
        self.accept()


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------
class AutomationDialog(QDialog):
    def __init__(self, service: DatabaseService, board_id: int, engine: Optional[au.AutomationEngine] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.engine = engine or au.AutomationEngine(service)
        self.changed = False  # something that affects the board happened (e.g. "Run now")
        board = service.get_board(board_id)
        self.setWindowTitle(f"Automations - {board.name if board else ''}")
        self.setStyleSheet(CHECKBOX_QSS)
        self.resize(980, 620)
        self._loading = False
        self._init_ui()
        self.reload()

    def ctx(self) -> au.BoardContext:
        return self.engine.board_context(self.board_id)

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # Rules ---------------------------------------------------------------
        rules_tab = QWidget()
        rl = QVBoxLayout(rules_tab)
        intro = QLabel("Rules react to what happens on this board (⚡) or to time passing (⏰). "
                       "Changes made by rules show up in each task's history as '⚙ Automation'.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #94A3B8;")
        rl.addWidget(intro)
        self.rules_table = QTableWidget(0, 5)
        self.rules_table.setHorizontalHeaderLabels(["On", "Rule", "What it does", "Runs", "Last run"])
        hdr = self.rules_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.rules_table.setSelectionMode(QTableWidget.SingleSelection)
        self.rules_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.rules_table.setWordWrap(True)
        self.rules_table.itemDoubleClicked.connect(lambda _i: self.edit_selected())
        self.rules_table.itemChanged.connect(self._on_rule_item_changed)
        rl.addWidget(self.rules_table, 1)
        self.rules_empty = QLabel("No rules yet. Start from a template - they are easy to tweak.")
        self.rules_empty.setStyleSheet("color: #64748B; padding: 8px;")
        rl.addWidget(self.rules_empty)
        btns = QHBoxLayout()
        rl.addLayout(btns)  # attach first: a parentless layout + self-capturing lambda double-frees in PySide6
        new_btn = QPushButton("+ New Rule")
        new_btn.setObjectName("PrimaryButton")
        new_btn.clicked.connect(self.new_rule)
        btns.addWidget(new_btn)
        tmpl_btn = QPushButton("✨ From Template")
        tmpl_menu = QMenu(tmpl_btn)
        for i, t in enumerate(au.RULE_TEMPLATES):
            tmpl_menu.addAction(t["name"], lambda i=i: self.new_from_template(i))
        tmpl_btn.setMenu(tmpl_menu)
        btns.addWidget(tmpl_btn)
        btns.addStretch()
        self.edit_btn = QPushButton("Edit")
        self.edit_btn.clicked.connect(self.edit_selected)
        self.dup_btn = QPushButton("Duplicate")
        self.dup_btn.clicked.connect(self.duplicate_selected)
        self.del_btn = QPushButton("Delete")
        self.del_btn.setObjectName("DangerButton")
        self.del_btn.clicked.connect(lambda: self.delete_selected())
        for b in (self.edit_btn, self.dup_btn, self.del_btn):
            btns.addWidget(b)
        self.tabs.addTab(rules_tab, "⚡ Rules")

        # Policies ----------------------------------------------------------
        pol_tab = QWidget()
        pl = QVBoxLayout(pol_tab)
        arch = QGroupBox("Auto-archiving")
        al = QFormLayout(arch)
        self.auto_chk = QCheckBox("Archive completed tasks")
        self.auto_days = QSpinBox()
        self.auto_days.setRange(1, 3650)
        self.auto_days.setSuffix(" days after completion")
        al.addRow(self.auto_chk, self.auto_days)
        pl.addWidget(arch)

        stale = QGroupBox("Stale tasks (no activity: no edits, moves, comments or logged time)")
        sl = QFormLayout(stale)
        self.stale_chk = QCheckBox("Flag open tasks with no activity for")
        self.stale_days = QSpinBox()
        self.stale_days.setRange(1, 3650)
        self.stale_days.setSuffix(" days")
        sl.addRow(self.stale_chk, self.stale_days)
        self.stale_label_chk = QCheckBox("Add label")
        self.stale_label = QLineEdit()
        self.stale_label.setPlaceholderText("Stale")
        sl.addRow(self.stale_label_chk, self.stale_label)
        self.stale_comment_chk = QCheckBox("Post a comment asking whether it is still needed")
        sl.addRow("", self.stale_comment_chk)
        self.stale_notify_chk = QCheckBox("Show a notification")
        sl.addRow("", self.stale_notify_chk)
        self.stale_move_chk = QCheckBox("Move to column")
        self.stale_move_combo = QComboBox()
        sl.addRow(self.stale_move_chk, self.stale_move_combo)
        self.stale_exclude = QListWidget()
        self.stale_exclude.setMaximumHeight(90)
        sl.addRow("Never flag tasks in:", self.stale_exclude)
        self.archive_stale_chk = QCheckBox("Archive open tasks with no activity for")
        self.archive_stale_days = QSpinBox()
        self.archive_stale_days.setRange(1, 3650)
        self.archive_stale_days.setSuffix(" days")
        sl.addRow(self.archive_stale_chk, self.archive_stale_days)
        pl.addWidget(stale)
        for w in (self.auto_days, self.stale_days, self.archive_stale_days, self.stale_label, self.stale_move_combo):
            w.setMaximumWidth(240)  # compact inputs; the descriptions carry the meaning
        self.stale_exclude.setMaximumWidth(240)

        run_row = QHBoxLayout()
        note = QLabel("Policies and ⏰ rules are checked every few minutes and at startup. "
                      "Archived tasks can be restored from 🗄 Archived.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #64748B; font-size: 11px;")
        run_row.addWidget(note, 1)
        self.run_btn = QPushButton("▶ Run Now")
        self.run_btn.setToolTip("Check time-based rules and policies on all boards now")
        self.run_btn.clicked.connect(self.run_now)
        run_row.addWidget(self.run_btn)
        pl.addLayout(run_row)
        self.run_result = QLabel("")
        self.run_result.setStyleSheet("color: #34D399;")
        pl.addWidget(self.run_result)
        pl.addStretch()
        self.tabs.addTab(pol_tab, "🗄 Policies")

        for w in (self.auto_chk, self.stale_chk, self.stale_label_chk, self.stale_comment_chk, self.stale_notify_chk,
                  self.stale_move_chk, self.archive_stale_chk):
            w.toggled.connect(self._save_policies)
        for w in (self.auto_days, self.stale_days, self.archive_stale_days):
            w.valueChanged.connect(self._save_policies)
        self.stale_label.editingFinished.connect(self._save_policies)
        self.stale_move_combo.currentIndexChanged.connect(self._save_policies)
        self.stale_exclude.itemChanged.connect(self._save_policies)

        # History -------------------------------------------------------------
        hist_tab = QWidget()
        hl = QVBoxLayout(hist_tab)
        self.history = QTableWidget(0, 5)
        self.history.setHorizontalHeaderLabels(["When", "Rule / policy", "Task", "Result", "Details"])
        h = self.history.horizontalHeader()
        for i in range(4):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.Stretch)
        self.history.verticalHeader().setVisible(False)
        self.history.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history.setSelectionBehavior(QTableWidget.SelectRows)
        self.history.itemDoubleClicked.connect(self._open_history_task)
        hl.addWidget(self.history, 1)
        hh = QLabel("Double-click a row to open its task. History is kept for 90 days.")
        hh.setStyleSheet("color: #64748B; font-size: 11px;")
        hl.addWidget(hh)
        self.tabs.addTab(hist_tab, "🕘 History")
        self.tabs.currentChanged.connect(lambda i: self.reload_history() if i == 2 else None)

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.accept)
        layout.addWidget(close)

    # ------------------------------------------------------------------
    def reload(self) -> None:
        self.reload_rules()
        self.load_policies()
        self.reload_history()

    def reload_rules(self) -> None:
        ctx = self.ctx()
        self.rules = self.service.get_automation_rules(self.board_id)
        self.rules_table.blockSignals(True)
        self.rules_table.setRowCount(len(self.rules))
        for r, rule in enumerate(self.rules):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            on.setCheckState(Qt.Checked if rule["is_enabled"] else Qt.Unchecked)
            on.setData(Qt.UserRole, rule["id"])
            self.rules_table.setItem(r, 0, on)
            problems = au.validate_rule(rule, ctx)
            name = QTableWidgetItem(("⚠ " if problems else "") + rule["name"])
            if problems:
                name.setForeground(QColor("#F87171"))
                name.setToolTip("This rule needs attention:\n" + "\n".join(problems))
            self.rules_table.setItem(r, 1, name)
            summary = QTableWidgetItem(au.describe_rule(rule, ctx))
            summary.setToolTip(summary.text())
            self.rules_table.setItem(r, 2, summary)
            self.rules_table.setItem(r, 3, QTableWidgetItem(str(rule["run_count"])))
            self.rules_table.setItem(r, 4, QTableWidgetItem(_local(rule["last_run_at"]) or "never"))
        self.rules_table.blockSignals(False)
        self.rules_table.resizeRowsToContents()
        self.rules_empty.setVisible(not self.rules)

    def _on_rule_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self.service.set_automation_rule_enabled(item.data(Qt.UserRole), item.checkState() == Qt.Checked)

    def _selected_rule(self) -> Optional[Dict[str, Any]]:
        row = self.rules_table.currentRow()
        return self.rules[row] if 0 <= row < len(self.rules) else None

    def _edit(self, rule: Optional[Dict[str, Any]]) -> bool:
        dlg = RuleEditorDialog(self.service, self.board_id, self.ctx(), rule, self)
        accepted = bool(dlg.exec())
        dlg.deleteLater()
        if accepted:
            self.reload_rules()
        return accepted

    def new_rule(self) -> None:
        self._edit(None)

    def new_from_template(self, index: int) -> None:
        data = au.instantiate_template(au.RULE_TEMPLATES[index], self.service, self.board_id)
        self._edit(data)

    def edit_selected(self) -> None:
        rule = self._selected_rule()
        if rule:
            self._edit(rule)

    def duplicate_selected(self) -> None:
        rule = self._selected_rule()
        if rule:
            copy = {k: v for k, v in rule.items() if k != "id"}
            copy["name"] = f"{rule['name']} (copy)"
            copy["is_enabled"] = False  # a duplicate starts disabled so it cannot double-fire
            self.service.save_automation_rule(self.board_id, copy)
            self.reload_rules()

    def delete_selected(self, confirm: bool = True) -> None:
        rule = self._selected_rule()
        if not rule:
            return
        if confirm and QMessageBox.question(self, "Delete Rule", f"Delete the rule '{rule['name']}'?",
                                            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.service.delete_automation_rule(rule["id"])
        self.reload_rules()

    # Policies ------------------------------------------------------------
    def load_policies(self) -> None:
        self._loading = True
        p = au.normalize_policies(self.service.get_board_policies(self.board_id))
        ctx = self.ctx()
        self.auto_chk.setChecked(p["auto_archive"]["enabled"])
        self.auto_days.setValue(p["auto_archive"]["days"])
        s = p["stale"]
        self.stale_chk.setChecked(s["enabled"])
        self.stale_days.setValue(s["days"])
        self.stale_label_chk.setChecked(bool(s.get("label")))
        self.stale_label.setText(s.get("label") or "Stale")
        self.stale_comment_chk.setChecked(bool(s.get("comment")))
        self.stale_notify_chk.setChecked(bool(s.get("notify")))
        self.stale_move_combo.clear()
        columns = sorted(ctx.columns.items(), key=lambda kv: kv[1]["position"])
        for cid, col in columns:
            self.stale_move_combo.addItem(col["name"], cid)
        move_to = s.get("move_to_column_id")
        self.stale_move_chk.setChecked(bool(move_to))
        if move_to:
            self.stale_move_combo.setCurrentIndex(max(0, self.stale_move_combo.findData(move_to)))
        self.stale_exclude.clear()
        excluded = {int(c) for c in s.get("exclude_column_ids") or []}
        for cid, col in columns:
            if col["is_done_column"]:
                continue  # completed tasks are never stale anyway
            item = QListWidgetItem(col["name"])
            item.setData(Qt.UserRole, cid)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if cid in excluded else Qt.Unchecked)
            self.stale_exclude.addItem(item)
        self.archive_stale_chk.setChecked(p["archive_stale"]["enabled"])
        self.archive_stale_days.setValue(p["archive_stale"]["days"])
        self._loading = False
        self._update_policy_enablement()

    def policies_value(self) -> Dict[str, Any]:
        excluded = [self.stale_exclude.item(i).data(Qt.UserRole) for i in range(self.stale_exclude.count())
                    if self.stale_exclude.item(i).checkState() == Qt.Checked]
        return {
            "auto_archive": {"enabled": self.auto_chk.isChecked(), "days": self.auto_days.value()},
            "stale": {
                "enabled": self.stale_chk.isChecked(),
                "days": self.stale_days.value(),
                "label": (self.stale_label.text().strip() or "Stale") if self.stale_label_chk.isChecked() else "",
                "comment": self.stale_comment_chk.isChecked(),
                "notify": self.stale_notify_chk.isChecked(),
                "move_to_column_id": self.stale_move_combo.currentData() if self.stale_move_chk.isChecked() else None,
                "exclude_column_ids": excluded,
            },
            "archive_stale": {"enabled": self.archive_stale_chk.isChecked(), "days": self.archive_stale_days.value()},
        }

    def _update_policy_enablement(self) -> None:
        self.auto_days.setEnabled(self.auto_chk.isChecked())
        on = self.stale_chk.isChecked()
        for w in (self.stale_days, self.stale_label_chk, self.stale_comment_chk, self.stale_notify_chk,
                  self.stale_move_chk, self.stale_exclude):
            w.setEnabled(on)
        self.stale_label.setEnabled(on and self.stale_label_chk.isChecked())
        self.stale_move_combo.setEnabled(on and self.stale_move_chk.isChecked())
        self.archive_stale_days.setEnabled(self.archive_stale_chk.isChecked())

    def _save_policies(self, *_args) -> None:
        if self._loading:
            return
        self._update_policy_enablement()
        self.service.set_board_policies(self.board_id, self.policies_value())

    def run_now(self) -> None:
        summary = self.engine.run_scheduled()
        self.changed = self.changed or summary.changed
        self.run_result.setText("✓ " + (summary.describe() or "Nothing to do right now."))
        self.reload_rules()
        self.reload_history()

    # History -------------------------------------------------------------
    def reload_history(self) -> None:
        runs = self.service.get_automation_runs(self.board_id)
        self.history.setRowCount(len(runs))
        for r, run in enumerate(runs):
            values = [_local(run["created_at"]), run["rule_name"], run["task_title"], run["status"], run["message"]]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, run["task_id"])
                if c == 3:
                    item.setForeground(QColor(STATUS_COLORS.get(run["status"], "#CBD5E1")))
                if c == 4:
                    item.setToolTip(v)
                self.history.setItem(r, c, item)

    def _open_history_task(self, item: QTableWidgetItem) -> None:
        task_id = item.data(Qt.UserRole)
        task = self.service.get_task(task_id) if task_id else None
        if task is not None and not task.is_archived:
            app_signals.task_edit_requested.emit(task_id)

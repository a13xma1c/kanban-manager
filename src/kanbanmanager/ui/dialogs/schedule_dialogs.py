"""
Scheduling Dialogs: Auto-schedule preview, Baselines & variance, Schedule settings,
and per-task Dependency editing (type & lag).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import List, Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import scheduling as sch
from kanbanmanager.core.schedule_engine import AUTO_SCHEDULE_ACTOR, ScheduleEngine
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.enums import DependencyType
from kanbanmanager.ui.styles import CHECKBOX_QSS

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEP_TYPES = [DependencyType.FINISH_TO_START, DependencyType.START_TO_START, DependencyType.FINISH_TO_FINISH,
             DependencyType.START_TO_FINISH]


def _d(value: Optional[date]) -> str:
    return value.strftime("%a %b %d") if value else "-"


def _table(headers: List[str], stretch: int = 0) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    for c in range(len(headers)):
        t.horizontalHeader().setSectionResizeMode(c, QHeaderView.Stretch if c == stretch
                                                   else QHeaderView.ResizeToContents)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    return t


# ---------------------------------------------------------------------------
# Auto-schedule
# ---------------------------------------------------------------------------
class AutoScheduleDialog(QDialog):
    """Preview ASAP dates from the dependency network; apply the ones you tick."""

    def __init__(self, engine: ScheduleEngine, board_id: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.engine = engine
        self.board_id = board_id
        self.proposal: Optional[sch.ScheduleProposal] = None
        self.applied = 0
        self.setWindowTitle("Auto-schedule")
        self.setStyleSheet(CHECKBOX_QSS)
        self.resize(900, 560)

        layout = QVBoxLayout(self)
        intro = QLabel("Dates are proposed from each task's dependencies and duration (its current dates, or its "
                       "estimate ÷ hours per day). Completed and 📌 locked tasks never move. Nothing changes until "
                       "you apply.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #94A3B8;")
        layout.addWidget(intro)

        opts = QHBoxLayout()
        layout.addLayout(opts)
        self.keep_chk = QCheckBox("Don't move tasks earlier than currently planned")
        self.keep_chk.setChecked(True)
        self.keep_chk.setToolTip("Off: pack every task as early as dependencies and the project start allow")
        self.keep_chk.toggled.connect(self.recompute)
        opts.addWidget(self.keep_chk)
        self.unscheduled_chk = QCheckBox("Give dates to tasks that have none")
        self.unscheduled_chk.setChecked(True)
        self.unscheduled_chk.toggled.connect(self.recompute)
        opts.addWidget(self.unscheduled_chk)
        opts.addStretch()

        self.table = _table(["Apply", "Task", "Currently", "Proposed", "Shift", "Why"], stretch=1)
        layout.addWidget(self.table, 1)
        self.blocked_lbl = QLabel("")
        self.blocked_lbl.setWordWrap(True)
        self.blocked_lbl.setStyleSheet("color: #FB923C;")
        layout.addWidget(self.blocked_lbl)
        self.summary_lbl = QLabel("")
        self.summary_lbl.setStyleSheet("color: #CBD5E1;")
        layout.addWidget(self.summary_lbl)

        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        buttons.addStretch()
        self.apply_btn = QPushButton("Apply Selected")
        self.apply_btn.setObjectName("PrimaryButton")
        self.apply_btn.clicked.connect(self.apply)
        buttons.addWidget(self.apply_btn)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.recompute()

    def recompute(self) -> None:
        before = self.engine.analyze(self.board_id)
        self.proposal = self.engine.auto_schedule(self.board_id, keep_planned_starts=self.keep_chk.isChecked(),
                                                  include_unscheduled=self.unscheduled_chk.isChecked())
        changes = self.proposal.changes
        self.table.setRowCount(len(changes))
        for r, ch in enumerate(changes):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            tick.setCheckState(Qt.Checked)
            self.table.setItem(r, 0, tick)
            current = "not scheduled" if ch.is_new else (
                _d(ch.old_start) if ch.old_start == ch.old_finish else f"{_d(ch.old_start)} → {_d(ch.old_finish)}")
            proposed = _d(ch.new_start) if ch.new_start == ch.new_finish else f"{_d(ch.new_start)} → {_d(ch.new_finish)}"
            shift = "new" if ch.is_new else f"{ch.shift_days:+d}d"
            for c, v in enumerate([ch.title, current, proposed, shift, ch.reason], start=1):
                item = QTableWidgetItem(v)
                if c == 4 and not ch.is_new:
                    item.setForeground(QColor("#F87171" if ch.shift_days > 0 else "#34D399"))
                self.table.setItem(r, c, item)
        self.blocked_lbl.setText("\n".join(f"⚠ {b}" for b in self.proposal.blocked))
        self.blocked_lbl.setVisible(bool(self.proposal.blocked))
        old = before.project_finish_date
        new = self.proposal.project_finish
        if not changes:
            self.summary_lbl.setText("✓ The plan already respects every dependency - nothing to change.")
        else:
            self.summary_lbl.setText(f"{len(changes)} task{'s' if len(changes) != 1 else ''} would change. "
                                     f"Project finish: {_d(old)} → {_d(new)}")
        self.apply_btn.setEnabled(bool(changes))

    def selected_changes(self) -> List[sch.DateChange]:
        return [ch for r, ch in enumerate(self.proposal.changes)
                if self.table.item(r, 0).checkState() == Qt.Checked]

    def apply(self) -> None:
        chosen = self.selected_changes()
        self.applied = self.engine.apply(self.board_id, chosen, actor=AUTO_SCHEDULE_ACTOR)
        self.accept()


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------
STATUS_TEXT = {
    "late": ("Late", "#F87171"), "early": ("Early", "#34D399"), "on_track": ("On track", "#94A3B8"),
    "new": ("New since baseline", "#60A5FA"), "removed": ("Removed / archived", "#94A3B8"),
    "unscheduled": ("No dates now", "#FBBF24"),
}


class BaselinesDialog(QDialog):
    def __init__(self, engine: ScheduleEngine, board_id: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.engine = engine
        self.service = engine.service
        self.board_id = board_id
        self.setWindowTitle("Project Baselines")
        self.resize(980, 620)

        layout = QVBoxLayout(self)
        intro = QLabel("A baseline freezes today's plan. Compare against it later to see what slipped; the Gantt "
                       "can draw it as ghost bars under the current plan.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #94A3B8;")
        layout.addWidget(intro)

        row = QHBoxLayout()
        layout.addLayout(row)
        self.list = _table(["Baseline", "Saved", "By", "Tasks"], stretch=0)
        self.list.setSelectionMode(QTableWidget.SingleSelection)
        self.list.setMaximumHeight(170)
        self.list.itemSelectionChanged.connect(self._render_comparison)
        row.addWidget(self.list, 1)
        btns = QVBoxLayout()
        row.addLayout(btns)
        self.save_btn = QPushButton("📸 Save Baseline...")
        self.save_btn.setObjectName("PrimaryButton")
        self.save_btn.clicked.connect(self.save_new)
        btns.addWidget(self.save_btn)
        self.show_btn = QPushButton("Show in Gantt")
        self.show_btn.clicked.connect(self.show_selected)
        btns.addWidget(self.show_btn)
        self.rename_btn = QPushButton("Rename...")
        self.rename_btn.clicked.connect(self.rename_selected)
        btns.addWidget(self.rename_btn)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setObjectName("DangerButton")
        self.delete_btn.clicked.connect(self.delete_selected)
        btns.addWidget(self.delete_btn)
        btns.addStretch()

        self.summary_lbl = QLabel("")
        self.summary_lbl.setTextFormat(Qt.RichText)
        self.summary_lbl.setWordWrap(True)
        layout.addWidget(self.summary_lbl)
        self.variance = _table(["Task", "Baseline", "Current", "Start Δ", "Finish Δ", "Status"], stretch=0)
        layout.addWidget(self.variance, 1)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.accept)
        layout.addWidget(close)
        self.reload()

    def reload(self, select_id: Optional[int] = None) -> None:
        self.baselines = self.service.get_baselines(self.board_id)
        shown = sch.normalize_schedule_settings(self.service.get_schedule_settings(self.board_id)).get("baseline_id")
        self.list.setRowCount(len(self.baselines))
        for r, b in enumerate(self.baselines):
            saved = datetime.fromisoformat(b["created_at"]).replace(tzinfo=timezone.utc).astimezone()
            values = [("👁 " if b["id"] == shown else "") + b["name"], saved.strftime("%b %d, %Y %H:%M"),
                      b["created_by"] or "-", str(b["task_count"])]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, b["id"])
                self.list.setItem(r, c, item)
        target = select_id or (self.baselines[0]["id"] if self.baselines else None)
        for r, b in enumerate(self.baselines):
            if b["id"] == target:
                self.list.selectRow(r)
        for w in (self.show_btn, self.rename_btn, self.delete_btn):
            w.setEnabled(bool(self.baselines))
        self._render_comparison()

    def selected_id(self) -> Optional[int]:
        items = self.list.selectedItems()
        return items[0].data(Qt.UserRole) if items else None

    def save_new(self, name: Optional[str] = None) -> Optional[dict]:
        if name is None:
            name, ok = QInputDialog.getText(self, "Save Baseline", "Name:",
                                            text=f"Baseline {date.today():%b %d, %Y}")
            if not ok:
                return None
        if not (name or "").strip():
            return None
        baseline = self.engine.save_baseline(self.board_id, name)
        self.reload(baseline["id"])
        return baseline

    def show_selected(self) -> None:
        settings = sch.normalize_schedule_settings(self.service.get_schedule_settings(self.board_id))
        settings["baseline_id"] = self.selected_id()
        self.service.set_schedule_settings(self.board_id, settings)
        self.reload(self.selected_id())

    def rename_selected(self) -> None:
        bid = self.selected_id()
        if bid is None:
            return
        current = next(b["name"] for b in self.baselines if b["id"] == bid)
        name, ok = QInputDialog.getText(self, "Rename Baseline", "Name:", text=current)
        if ok and name.strip():
            self.service.rename_baseline(bid, name)
            self.reload(bid)

    def delete_selected(self, confirm: bool = True) -> None:
        bid = self.selected_id()
        if bid is None:
            return
        if confirm and QMessageBox.question(self, "Delete Baseline", "Delete this baseline?",
                                            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.service.delete_baseline(bid)
        self.reload()

    def _render_comparison(self) -> None:
        bid = self.selected_id()
        cmp = self.engine.compare(self.board_id, bid) if bid else None
        if cmp is None:
            self.summary_lbl.setText("Save a baseline to start tracking schedule variance.")
            self.variance.setRowCount(0)
            return
        parts = []
        if cmp.finish_var is not None:
            tone = "#F87171" if cmp.finish_var > 0 else "#34D399"
            parts.append(f"Project finish: baseline <b>{_d(cmp.baseline_finish)}</b>, now <b>{_d(cmp.current_finish)}</b>"
                         f" (<span style='color:{tone}'>{cmp.finish_var:+d} working days</span>)")
        for key in ("late", "early", "on_track", "new", "unscheduled", "removed"):
            n = cmp.count(key)
            if n:
                label, color = STATUS_TEXT[key]
                parts.append(f"<span style='color:{color}'>{n} {label.lower()}</span>")
        self.summary_lbl.setText(" &nbsp;·&nbsp; ".join(parts))
        self.variance.setRowCount(len(cmp.rows))

        def span(a: Optional[date], b: Optional[date], milestone: bool) -> str:
            if a is None:
                return "-"
            return _d(a) if milestone or a == b else f"{_d(a)} → {_d(b)}"

        for r, v in enumerate(cmp.rows):
            label, color = STATUS_TEXT[v.status]
            values = [("◆ " if v.is_milestone else "") + v.title,
                      span(v.baseline_start, v.baseline_finish, v.is_milestone),
                      span(v.current_start, v.current_finish, v.is_milestone),
                      "-" if v.start_var is None else f"{v.start_var:+d}d",
                      "-" if v.finish_var is None else f"{v.finish_var:+d}d", label]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if c in (3, 4) and text not in ("-", "+0d"):
                    item.setForeground(QColor("#F87171" if text.startswith("+") else "#34D399"))
                if c == 5:
                    item.setForeground(QColor(color))
                self.variance.setItem(r, c, item)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class ScheduleSettingsDialog(QDialog):
    def __init__(self, service: DatabaseService, board_id: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.settings = sch.normalize_schedule_settings(service.get_schedule_settings(board_id))
        self.setWindowTitle("Schedule Settings")
        self.setStyleSheet(CHECKBOX_QSS)
        form = QFormLayout(self)

        days_row = QHBoxLayout()
        form.addRow("Working days:", days_row)
        self.day_boxes = []
        for i, name in enumerate(DAY_NAMES):
            box = QCheckBox(name)
            box.setChecked(i in self.settings["working_days"])
            days_row.addWidget(box)
            self.day_boxes.append(box)
        self.hours = QDoubleSpinBox()
        self.hours.setRange(0.5, 24)
        self.hours.setSingleStep(0.5)
        self.hours.setSuffix(" h")
        self.hours.setValue(self.settings["hours_per_day"])
        self.hours.setToolTip("Used to turn estimates into durations for tasks without both dates")
        form.addRow("Hours per working day:", self.hours)

        start_row = QHBoxLayout()
        form.addRow("Project start:", start_row)
        self.start_chk = QCheckBox("Fixed date")
        self.start_edit = QDateEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("ddd, MMM d yyyy")
        start = sch._to_date(self.settings.get("project_start"))
        self.start_chk.setChecked(start is not None)
        qd = start or date.today()
        self.start_edit.setDate(QDate(qd.year, qd.month, qd.day))
        self.start_edit.setEnabled(start is not None)
        self.start_chk.toggled.connect(self.start_edit.setEnabled)
        start_row.addWidget(self.start_chk)
        start_row.addWidget(self.start_edit)
        hint = QLabel("Otherwise tasks without dates are scheduled from today.")
        hint.setStyleSheet("color: #64748B; font-size: 11px;")
        form.addRow("", hint)

        self.cascade_chk = QCheckBox("Cascade date changes to dependent tasks")
        self.cascade_chk.setChecked(self.settings["cascade"])
        self.cascade_chk.setToolTip("When a task moves later, its dependents (and theirs) move so no dependency "
                                    "is violated. Locked and completed tasks never move.")
        form.addRow("", self.cascade_chk)
        self.pull_chk = QCheckBox("Also pull dependents earlier when a task moves earlier")
        self.pull_chk.setChecked(self.settings["cascade_pull"])
        self.pull_chk.setEnabled(self.settings["cascade"])
        self.cascade_chk.toggled.connect(self.pull_chk.setEnabled)
        form.addRow("", self.pull_chk)

        self.error_lbl = QLabel("")
        self.error_lbl.setStyleSheet("color: #F87171;")
        form.addRow(self.error_lbl)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def value(self) -> dict:
        s = dict(self.settings)
        s["working_days"] = [i for i, b in enumerate(self.day_boxes) if b.isChecked()]
        s["hours_per_day"] = self.hours.value()
        s["project_start"] = self.start_edit.date().toPython().isoformat() if self.start_chk.isChecked() else None
        s["cascade"] = self.cascade_chk.isChecked()
        s["cascade_pull"] = self.pull_chk.isChecked() and self.cascade_chk.isChecked()
        return s

    def save(self) -> None:
        value = self.value()
        if not value["working_days"]:
            self.error_lbl.setText("Choose at least one working day.")
            return
        self.service.set_schedule_settings(self.board_id, value)
        self.accept()


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
class DependencyDialog(QDialog):
    """Edit a task's predecessors: type (FS/SS/FF/SF), lag/lead, add and remove."""

    def __init__(self, service: DatabaseService, board_id: int, task_id: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.task_id = task_id
        task = service.get_task(task_id)
        self.setWindowTitle(f"Dependencies - {task.title if task else ''}")
        self.resize(720, 420)
        layout = QVBoxLayout(self)
        intro = QLabel("This task depends on (predecessors). Lag is in working days; negative lag is a lead "
                       "(e.g. start-to-start -1 = may start a day before the predecessor starts).")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #94A3B8;")
        layout.addWidget(intro)
        self.table = _table(["Predecessor", "Type", "Lag (days)", ""], stretch=0)
        layout.addWidget(self.table, 1)

        add_row = QHBoxLayout()
        layout.addLayout(add_row)
        add_row.addWidget(QLabel("Add predecessor:"))
        self.add_combo = QComboBox()
        add_row.addWidget(self.add_combo, 1)
        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(self.add_selected)
        add_row.addWidget(self.add_btn)
        self.error_lbl = QLabel("")
        self.error_lbl.setStyleSheet("color: #F87171;")
        layout.addWidget(self.error_lbl)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.accept)
        layout.addWidget(close)
        self.reload()

    def reload(self) -> None:
        deps = [d for d in self.service.get_board_dependencies(self.board_id) if d["task_id"] == self.task_id]
        titles = {t.id: t.title for t in self.service.get_tasks_for_board(self.board_id)}
        self.table.setRowCount(len(deps))
        for r, d in enumerate(deps):
            pred = d["depends_on_task_id"]
            self.table.setItem(r, 0, QTableWidgetItem(titles.get(pred, f"#{pred} (archived)")))
            combo = QComboBox()
            for kind in DEP_TYPES:
                combo.addItem(f"{kind.short} - {kind.display_name}", kind.value)
            current = DependencyType(d["dependency_type"]).scheduling_type.value
            combo.setCurrentIndex(max(0, combo.findData(current)))
            combo.currentIndexChanged.connect(self._make_type_handler(pred, combo))
            self.table.setCellWidget(r, 1, combo)
            spin = QSpinBox()
            spin.setRange(-60, 365)
            spin.setValue(int(d.get("lag_days") or 0))
            spin.valueChanged.connect(self._make_lag_handler(pred))
            self.table.setCellWidget(r, 2, spin)
            remove = QPushButton("Remove")
            remove.clicked.connect(self._make_remove_handler(pred))
            self.table.setCellWidget(r, 3, remove)
        linked = {d["depends_on_task_id"] for d in deps}
        self.add_combo.clear()
        for tid, title in sorted(titles.items(), key=lambda kv: kv[1].lower()):
            if tid != self.task_id and tid not in linked:
                self.add_combo.addItem(title, tid)
        self.add_btn.setEnabled(self.add_combo.count() > 0)

    # One handler per row, bound to that row's predecessor (cell widgets sit directly in the table,
    # so they are owned by Qt from the start - see tests/test_qt_ownership.py for why that matters)
    def _make_type_handler(self, pred: int, combo: QComboBox):
        def handler(_index: int) -> None:
            self.service.update_dependency(self.task_id, pred, dependency_type=combo.currentData())
        return handler

    def _make_lag_handler(self, pred: int):
        def handler(value: int) -> None:
            self.service.update_dependency(self.task_id, pred, lag_days=value)
        return handler

    def _make_remove_handler(self, pred: int):
        def handler(_checked: bool = False) -> None:
            self.service.remove_dependency(self.task_id, pred)
            self.reload()
        return handler

    def add_selected(self) -> None:
        pred = self.add_combo.currentData()
        if pred is None:
            return
        ok, err = self.service.add_dependency(task_id=self.task_id, depends_on_task_id=pred)
        self.error_lbl.setText("" if ok else (err or "Could not add the dependency"))
        self.reload()

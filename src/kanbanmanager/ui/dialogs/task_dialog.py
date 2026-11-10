"""
Comprehensive Task Editor Dialog with Rich Markdown Descriptions, Attachments,
Comments & Audit Trail, Due Dates, Reminders, Recurrence, Labels, and Dependencies
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
from functools import partial
from typing import Optional, Dict, Any, List

from PySide6.QtCore import Qt, QDate, QTime, QDateTime, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QComboBox,
    QDoubleSpinBox,
    QCheckBox,
    QPushButton,
    QDateEdit,
    QDateTimeEdit,
    QListWidget,
    QListWidgetItem,
    QTabWidget,
    QWidget,
    QFrame,
    QMessageBox,
    QSpinBox,
    QFileDialog,
    QMenu,
)

from kanbanmanager.config import IMAGE_EXTENSIONS
from kanbanmanager.timefmt import format_duration
from kanbanmanager.core.markdown_utils import attachment_refs, remove_attachment_refs, rewrite_attachment_refs
from kanbanmanager.enums import Priority, RecurrenceFrequency, DependencyType
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.components.activity_panel import ActivityPanel
from kanbanmanager.ui.components.attachments_panel import AttachmentsPanel
from kanbanmanager.ui.components.markdown_editor import MarkdownEditor
from kanbanmanager.ui.components.time_panel import TimePanel
from kanbanmanager.ui.components.field_editors import EditorContext, FieldsPanel
from kanbanmanager.core import templates as tpl


class TaskDialog(QDialog):
    """
    Dialog for creating and editing tasks with full feature set:
    Markdown/WYSIWYG description with inline images, Attachments,
    Comments & History, Labels, Priorities, Due Dates & Reminders,
    Recurring rules, Checklist items, and Dependency graph management.

    Form fields and attachment changes are applied together on Save;
    comments are posted immediately (see ``external_changes``).
    """

    def __init__(
        self,
        board_id: int,
        task_id: Optional[int] = None,
        default_column_id: Optional[int] = None,
        service: Optional[DatabaseService] = None,
        parent: Optional[QWidget] = None,
        tracker=None,
        template: Optional[Dict[str, Any]] = None,
    ):
        """``template``: a task template ({"name", "data"}) to pre-fill a new task with."""
        super().__init__(parent)
        self.tracker = tracker  # shared TimeTracker (optional; enables live timers in the Time tab)
        self.template_name: Optional[str] = None  # template the form was filled from (new tasks)
        self.fields_changed = False  # the board's field definitions were edited from here
        self.board_id = board_id
        self.task_id = task_id
        self.default_column_id = default_column_id
        self.service = service or DatabaseService()

        self.setWindowTitle("Edit Task" if self.task_id else "Create New Task")
        self.setMinimumSize(760, 620)
        self.resize(1200, 760)  # all nine tabs visible at the default size (tests/test_fields_templates_ui.py)

        self._checklist_items: List[Dict[str, Any]] = []
        # True when something was persisted without Save (e.g. a comment was posted)
        self.external_changes = False

        self._init_ui()
        self._load_data()
        if template and not self.task_id:
            self.apply_template(template)
        self.finished.connect(self._on_finished)

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 18, 20, 18)
        main_layout.setSpacing(14)

        # Title Input
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        main_layout.addLayout(title_box)
        title_row = QHBoxLayout()
        title_box.addLayout(title_row)
        title_lbl = QLabel("Task Title *")
        title_lbl.setStyleSheet("font-weight: 600; color: #F8FAFC;")
        title_row.addWidget(title_lbl)
        title_row.addStretch()
        self.template_btn = QPushButton("📋 From Template")
        self.template_btn.setToolTip("Fill this task in from one of the board's task templates")
        self.template_menu = QMenu(self.template_btn)
        self.template_menu.aboutToShow.connect(self._fill_template_menu)
        self.template_btn.setMenu(self.template_menu)
        self.template_btn.setVisible(not self.task_id)
        title_row.addWidget(self.template_btn)

        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("e.g. Implement drag-and-drop workflow")
        title_box.addWidget(self.title_input)
        self.template_note = QLabel("")
        self.template_note.setWordWrap(True)
        self.template_note.setStyleSheet("color: #94A3B8; font-size: 11px;")
        self.template_note.setVisible(False)
        title_box.addWidget(self.template_note)

        # Tab Widget for Sections
        self.tabs = QTabWidget()

        # ---------------------------------------------------------------------
        # Tab 1: General Details
        # ---------------------------------------------------------------------
        tab_general = QWidget()
        gen_layout = QVBoxLayout(tab_general)
        gen_layout.setSpacing(12)

        # Row: Column + Priority + Estimated Hours
        top_grid = QGridLayout()
        top_grid.setSpacing(10)

        top_grid.addWidget(QLabel("Column:"), 0, 0)
        self.column_combo = QComboBox()
        top_grid.addWidget(self.column_combo, 0, 1)

        top_grid.addWidget(QLabel("Priority:"), 0, 2)
        self.priority_combo = QComboBox()
        for p in Priority:
            self.priority_combo.addItem(f"{p.icon_char} {p.display_name}", p.value)
        top_grid.addWidget(self.priority_combo, 0, 3)

        top_grid.addWidget(QLabel("Est. Hours:"), 1, 0)
        self.hours_spin = QDoubleSpinBox()
        self.hours_spin.setRange(0.0, 999.0)
        self.hours_spin.setSingleStep(0.5)
        top_grid.addWidget(self.hours_spin, 1, 1)

        top_grid.addWidget(QLabel("Assignee:"), 1, 2)
        self.assignee_combo = QComboBox()
        self.assignee_combo.setEditable(True)  # pick a teammate or type a new name
        self.assignee_combo.setInsertPolicy(QComboBox.NoInsert)
        self.assignee_combo.lineEdit().setPlaceholderText("Unassigned")
        self.assignee_combo.setToolTip("Who is responsible. Used by the Workload view for capacity planning.")
        top_grid.addWidget(self.assignee_combo, 1, 3)

        gen_layout.addLayout(top_grid)

        # Description (Markdown, editable visually or as source)
        gen_layout.addWidget(QLabel("Description & Notes:"))
        self.desc_editor = MarkdownEditor(
            placeholder="Add notes, acceptance criteria, checklists... Paste or drop images to attach them."
        )
        self.desc_editor.setMinimumHeight(220)
        gen_layout.addWidget(self.desc_editor, 1)

        # Labels selection
        gen_layout.addWidget(QLabel("Labels:"))
        self.labels_list = QListWidget()
        self.labels_list.setMaximumHeight(80)
        gen_layout.addWidget(self.labels_list)

        self.tabs.addTab(tab_general, "General")

        # ---------------------------------------------------------------------
        # Tab: Custom fields (defined per board)
        # ---------------------------------------------------------------------
        self.fields_panel = FieldsPanel(EditorContext(people=self.service.get_people()))
        self._fields_tab = self.tabs.addTab(self.fields_panel, "Fields")
        self.fields_panel.changed.connect(self._update_tab_titles)
        self.fields_panel.manage_requested.connect(self._manage_fields)

        # ---------------------------------------------------------------------
        # Tab 2: Dates, Due Dates & Reminders
        # ---------------------------------------------------------------------
        tab_dates = QWidget()
        dates_layout = QVBoxLayout(tab_dates)
        dates_layout.setSpacing(14)

        dates_grid = QGridLayout()
        dates_grid.setSpacing(12)

        # Start Date
        self.has_start_chk = QCheckBox("Set Start Date:")
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDate(QDate.currentDate())
        self.start_date_edit.setEnabled(False)
        self.has_start_chk.toggled.connect(self.start_date_edit.setEnabled)
        dates_grid.addWidget(self.has_start_chk, 0, 0)
        dates_grid.addWidget(self.start_date_edit, 0, 1)

        # Due Date
        self.has_due_chk = QCheckBox("Set Due Date && Time:")  # "&&" = literal ampersand
        self.due_datetime_edit = QDateTimeEdit()
        self.due_datetime_edit.setCalendarPopup(True)
        self.due_datetime_edit.setDateTime(QDateTime.currentDateTime().addDays(1))
        self.due_datetime_edit.setEnabled(False)
        self.has_due_chk.toggled.connect(self.due_datetime_edit.setEnabled)
        dates_grid.addWidget(self.has_due_chk, 1, 0)
        dates_grid.addWidget(self.due_datetime_edit, 1, 1)

        dates_layout.addLayout(dates_grid)

        # Scheduling flags (used by the Gantt, critical path and auto-scheduling)
        self.milestone_chk = QCheckBox("◆ Milestone - a point in time (zero duration) on its due date")
        self.milestone_chk.setToolTip("Milestones appear as diamonds in the Gantt chart")
        dates_layout.addWidget(self.milestone_chk)
        self.lock_chk = QCheckBox("📌 Lock dates - auto-scheduling and cascading never move this task")
        dates_layout.addWidget(self.lock_chk)

        # Reminder settings
        rem_frame = QFrame()
        rem_frame.setStyleSheet("background-color: #1E293B; border: 1px solid #334155; border-radius: 6px; padding: 10px;")
        rem_layout = QVBoxLayout(rem_frame)
        rem_layout.setSpacing(8)

        self.has_reminder_chk = QCheckBox("⏰ Enable Scheduled Reminder Notification")
        rem_layout.addWidget(self.has_reminder_chk)

        rem_row = QHBoxLayout()
        rem_row.addWidget(QLabel("Remind me:"))
        self.rem_offset_combo = QComboBox()
        self.rem_offset_combo.addItem("At time of due date", 0)
        self.rem_offset_combo.addItem("15 minutes before due date", 15)
        self.rem_offset_combo.addItem("1 hour before due date", 60)
        self.rem_offset_combo.addItem("1 day before due date", 1440)
        rem_row.addWidget(self.rem_offset_combo)
        rem_layout.addLayout(rem_row)

        dates_layout.addWidget(rem_frame)
        dates_layout.addStretch()

        self.tabs.addTab(tab_dates, "Dates && Reminders")  # "&&" = literal ampersand (not a mnemonic)

        # ---------------------------------------------------------------------
        # Tab 3: Checklist / Subtasks
        # ---------------------------------------------------------------------
        tab_check = QWidget()
        check_layout = QVBoxLayout(tab_check)
        check_layout.setSpacing(10)

        add_check_row = QHBoxLayout()
        self.check_item_input = QLineEdit()
        self.check_item_input.setPlaceholderText("Add subtask / checklist item...")
        self.check_item_input.returnPressed.connect(self._add_checklist_item)
        add_check_row.addWidget(self.check_item_input, 1)

        add_check_btn = QPushButton("Add")
        add_check_btn.clicked.connect(self._add_checklist_item)
        add_check_row.addWidget(add_check_btn)
        check_layout.addLayout(add_check_row)

        self.checklist_widget = QListWidget()
        check_layout.addWidget(self.checklist_widget, 1)

        del_check_btn = QPushButton("Remove Selected Subtask")
        del_check_btn.clicked.connect(self._remove_checklist_item)
        check_layout.addWidget(del_check_btn)

        self.tabs.addTab(tab_check, "Checklist")

        # ---------------------------------------------------------------------
        # Tab 4: Recurrence
        # ---------------------------------------------------------------------
        tab_rec = QWidget()
        rec_layout = QVBoxLayout(tab_rec)
        rec_layout.setSpacing(12)

        self.is_recurring_chk = QCheckBox("🔁 Repeat this task automatically")
        rec_layout.addWidget(self.is_recurring_chk)

        self.rec_options_frame = QFrame()
        self.rec_options_frame.setEnabled(False)
        self.is_recurring_chk.toggled.connect(self.rec_options_frame.setEnabled)

        rec_opts_layout = QVBoxLayout(self.rec_options_frame)
        rec_opts_layout.setSpacing(10)

        freq_row = QHBoxLayout()
        freq_row.addWidget(QLabel("Frequency:"))
        self.rec_freq_combo = QComboBox()
        for f in RecurrenceFrequency:
            self.rec_freq_combo.addItem(f.display_name, f.value)
        freq_row.addWidget(self.rec_freq_combo)
        rec_opts_layout.addLayout(freq_row)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("Repeat every:"))
        self.rec_interval_spin = QSpinBox()
        self.rec_interval_spin.setRange(1, 99)
        self.rec_interval_spin.setValue(1)
        interval_row.addWidget(self.rec_interval_spin)
        rec_opts_layout.addLayout(interval_row)

        rec_layout.addWidget(self.rec_options_frame)
        rec_layout.addStretch()

        self.tabs.addTab(tab_rec, "Recurring Rules")

        # ---------------------------------------------------------------------
        # Tab 5: Dependencies
        # ---------------------------------------------------------------------
        tab_dep = QWidget()
        dep_layout = QVBoxLayout(tab_dep)
        dep_layout.setSpacing(10)

        dep_info = QLabel("Specify prerequisite tasks that must be completed before this task can proceed:")
        dep_info.setWordWrap(True)
        dep_info.setStyleSheet("color: #94A3B8; font-size: 11px;")
        dep_layout.addWidget(dep_info)

        self.dep_list = QListWidget()
        dep_layout.addWidget(self.dep_list, 1)

        self.tabs.addTab(tab_dep, "Dependencies")

        # ---------------------------------------------------------------------
        # Tab 6: Attachments
        # ---------------------------------------------------------------------
        self.attachments_panel = AttachmentsPanel(self.service, self.task_id)
        self._attachments_tab = self.tabs.addTab(self.attachments_panel, "Attachments")
        self.attachments_panel.changed.connect(self._update_tab_titles)
        self.attachments_panel.insert_inline_requested.connect(self._insert_inline_image)
        self.attachments_panel.attachment_removed.connect(self._on_attachment_removed)

        self.desc_editor.set_resource_resolver(self.attachments_panel.resolve)
        self.desc_editor.image_requested.connect(self._choose_inline_image)
        self.desc_editor.image_pasted.connect(self._on_image_pasted)
        self.desc_editor.files_dropped.connect(self._on_files_dropped)

        # ---------------------------------------------------------------------
        # Tab 7: Activity - comments + audit trail
        # ---------------------------------------------------------------------
        self.time_panel = TimePanel(self.service, self.task_id, tracker=self.tracker)
        self._time_tab = self.tabs.addTab(self.time_panel, "Time")
        self.time_panel.time_changed.connect(self._on_time_changed)
        self.hours_spin.valueChanged.connect(self.time_panel.set_estimate)

        self.activity_panel = ActivityPanel(self.service, self.task_id)
        self._activity_tab = self.tabs.addTab(self.activity_panel, "Activity")
        self.activity_panel.comments_changed.connect(self._on_comments_changed)
        self._update_tab_titles()

        main_layout.addWidget(self.tabs, 1)

        # ---------------------------------------------------------------------
        # Bottom Buttons: Cancel | Delete | Save
        # ---------------------------------------------------------------------
        btn_row = QHBoxLayout()

        if self.task_id:
            del_btn = QPushButton("🗑 Delete Task")
            del_btn.setObjectName("DangerButton")
            del_btn.clicked.connect(self._delete_task)
            btn_row.addWidget(del_btn)
        save_tpl_btn = QPushButton("📋 Save as Template…")
        save_tpl_btn.setToolTip("Reuse this task's content (title, description, checklist, labels, fields) "
                                "for new tasks")
        save_tpl_btn.clicked.connect(self.save_as_template)
        btn_row.addWidget(save_tpl_btn)

        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save Task")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._save_task)
        btn_row.addWidget(save_btn)

        main_layout.addLayout(btn_row)

    def _load_data(self) -> None:
        # Load board columns
        columns = self.service.get_columns_for_board(self.board_id)
        self.column_combo.clear()
        for col in columns:
            self.column_combo.addItem(col.name, col.id)

        if self.default_column_id:
            idx = self.column_combo.findData(self.default_column_id)
            if idx >= 0:
                self.column_combo.setCurrentIndex(idx)

        # Assignees: team members, people already assigned, and you
        self.assignee_combo.clear()
        self.assignee_combo.addItem("")
        for name in self.service.get_people():
            self.assignee_combo.addItem(name)
        self.assignee_combo.setCurrentIndex(0)

        # Load board labels
        labels = self.service.get_labels_for_board(self.board_id)
        self.labels_list.clear()
        for lbl in labels:
            item = QListWidgetItem(f"🏷 {lbl.name}")
            item.setData(Qt.UserRole, lbl.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.labels_list.addItem(item)

        # Custom fields (new tasks start with each field's default)
        self.board_fields = self.service.get_custom_fields(self.board_id)
        self.fields_panel.set_fields(self.board_fields, {f["id"]: f["default"] for f in self.board_fields})
        self._update_tab_titles()

        # Load board tasks for dependency picker
        all_board_tasks = self.service.get_tasks_for_board(self.board_id)
        self.dep_list.clear()
        for t in all_board_tasks:
            if self.task_id and t.id == self.task_id:
                continue  # Cannot depend on self
            item = QListWidgetItem(f"#{t.id}: {t.title}")
            item.setData(Qt.UserRole, t.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.dep_list.addItem(item)

        if not self.task_id:
            return

        # Load existing task data
        task = self.service.get_task(self.task_id)
        if not task:
            return
        self._update_tab_titles()

        self.title_input.setText(task.title)
        self.desc_editor.set_markdown(task.description or "")

        col_idx = self.column_combo.findData(task.column_id)
        if col_idx >= 0:
            self.column_combo.setCurrentIndex(col_idx)

        pri_idx = self.priority_combo.findData(task.priority)
        if pri_idx >= 0:
            self.priority_combo.setCurrentIndex(pri_idx)

        self.hours_spin.setValue(task.estimated_hours or 0.0)
        self.assignee_combo.setCurrentText(task.assignee or "")

        # Dates
        if task.start_date:
            self.has_start_chk.setChecked(True)
            self.start_date_edit.setDate(QDate(task.start_date.year, task.start_date.month, task.start_date.day))

        if task.due_date:
            self.has_due_chk.setChecked(True)
            self.due_datetime_edit.setDateTime(task.due_date)
        self.milestone_chk.setChecked(bool(task.is_milestone))
        self.lock_chk.setChecked(bool(task.dates_locked))

        self.fields_panel.set_values(task.field_value_map)

        # Labels check
        task_label_ids = {lbl.id for lbl in task.labels}
        for i in range(self.labels_list.count()):
            item = self.labels_list.item(i)
            if item.data(Qt.UserRole) in task_label_ids:
                item.setCheckState(Qt.Checked)

        # Checklist
        self._checklist_items = list(task.checklist)
        self._refresh_checklist_widget()

        # Recurrence
        if task.is_recurring:
            self.is_recurring_chk.setChecked(True)
            rule = task.recurrence_rule
            freq_idx = self.rec_freq_combo.findData(rule.get("frequency", "DAILY"))
            if freq_idx >= 0:
                self.rec_freq_combo.setCurrentIndex(freq_idx)
            self.rec_interval_spin.setValue(rule.get("interval", 1))

        # Dependencies
        prereq_ids = {p.depends_on_task_id for p in task.prerequisites}
        for i in range(self.dep_list.count()):
            item = self.dep_list.item(i)
            if item.data(Qt.UserRole) in prereq_ids:
                item.setCheckState(Qt.Checked)

    def _add_checklist_item(self) -> None:
        text = self.check_item_input.text().strip()
        if not text:
            return
        new_id = len(self._checklist_items) + 1
        self._checklist_items.append({"id": new_id, "text": text, "done": False})
        self.check_item_input.clear()
        self._refresh_checklist_widget()

    def _remove_checklist_item(self) -> None:
        row = self.checklist_widget.currentRow()
        if row >= 0 and row < len(self._checklist_items):
            del self._checklist_items[row]
            self._refresh_checklist_widget()

    def _refresh_checklist_widget(self) -> None:
        self.checklist_widget.clear()
        for idx, item in enumerate(self._checklist_items):
            list_item = QListWidgetItem(item.get("text", ""))
            list_item.setFlags(list_item.flags() | Qt.ItemIsUserCheckable)
            list_item.setCheckState(Qt.Checked if item.get("done") else Qt.Unchecked)
            self.checklist_widget.addItem(list_item)

    def _save_task(self) -> None:
        title = self.title_input.text().strip()
        if not title:
            QMessageBox.warning(self, "Validation Error", "Task title cannot be empty.")
            return
        field_errors = self.fields_panel.errors()
        missing = self.fields_panel.missing_required()
        if field_errors or missing:
            lines = field_errors + [f"{name}: required" for name in missing if
                                    not any(e.startswith(f"{name}:") for e in field_errors)]
            self.tabs.setCurrentIndex(self._fields_tab)
            self.fields_panel.focus_field((missing or [e.split(":")[0] for e in field_errors])[0])
            QMessageBox.warning(self, "Check the fields", "Please fix these fields first:\n\n" + "\n".join(lines))
            return
        field_values = self.fields_panel.values()

        column_id = self.column_combo.currentData()
        priority = self.priority_combo.currentData()
        estimated_hours = self.hours_spin.value()
        assignee = self.assignee_combo.currentText().strip() or None
        desc = self.desc_editor.markdown()

        start_date_val = self.start_date_edit.date().toPython() if self.has_start_chk.isChecked() else None
        due_date_val = self.due_datetime_edit.dateTime().toPython() if self.has_due_chk.isChecked() else None
        is_milestone = self.milestone_chk.isChecked()
        if is_milestone:
            # A milestone is a single point in time: both dates are its date
            if due_date_val is None and start_date_val is not None:
                due_date_val = datetime.combine(start_date_val, datetime.min.time()).replace(hour=17)
            start_date_val = due_date_val.date() if due_date_val else None

        # Gather labels (labels a template brought along are created now)
        selected_label_ids = []
        for i in range(self.labels_list.count()):
            item = self.labels_list.item(i)
            if item.checkState() == Qt.Checked:
                if item.data(Qt.UserRole) is None:
                    lbl = self.service.get_label_by_name(self.board_id, item.data(Qt.UserRole + 1),
                                                         create_color=item.data(Qt.UserRole + 2) or "#64748B")
                    selected_label_ids.append(lbl.id)
                else:
                    selected_label_ids.append(item.data(Qt.UserRole))

        # Gather checklist
        for i in range(self.checklist_widget.count()):
            item = self.checklist_widget.item(i)
            if i < len(self._checklist_items):
                self._checklist_items[i]["done"] = (item.checkState() == Qt.Checked)

        # Recurrence rule
        is_rec = self.is_recurring_chk.isChecked()
        rec_rule = {}
        if is_rec:
            rec_rule = {
                "frequency": self.rec_freq_combo.currentData(),
                "interval": self.rec_interval_spin.value(),
            }

        attachment_errors: List[str] = []
        if self.task_id:
            # Attachments first, so inline references to new files can point at real ids
            mapping, attachment_errors = self.attachments_panel.commit(self.task_id)
            desc = rewrite_attachment_refs(desc, mapping)
            self.service.update_task(
                task_id=self.task_id,
                title=title,
                assignee=assignee,
                is_milestone=is_milestone,
                dates_locked=self.lock_chk.isChecked(),
                description=desc,
                column_id=column_id,
                priority=priority,
                estimated_hours=estimated_hours,
                start_date=start_date_val,
                due_date=due_date_val,
                label_ids=selected_label_ids,
                checklist=self._checklist_items,
                is_recurring=is_rec,
                recurrence_rule=rec_rule,
                field_values=field_values,
            )
            saved_task_id = self.task_id
        else:
            # Create new
            task = self.service.create_task(
                board_id=self.board_id,
                column_id=column_id,
                title=title,
                assignee=assignee,
                is_milestone=is_milestone,
                dates_locked=self.lock_chk.isChecked(),
                description=desc,
                priority=priority,
                estimated_hours=estimated_hours,
                start_date=start_date_val,
                due_date=due_date_val,
                label_ids=selected_label_ids,
                checklist=self._checklist_items,
                is_recurring=is_rec,
                recurrence_rule=rec_rule,
                field_values=field_values,  # every field: an emptied default stays empty
                origin=f"from template '{self.template_name}'" if self.template_name else None,
            )
            saved_task_id = task.id
            mapping, attachment_errors = self.attachments_panel.commit(saved_task_id)
            final_desc = rewrite_attachment_refs(desc, mapping)
            if final_desc != desc:
                # Pointing staged refs at their new ids is bookkeeping, not a user edit
                with self.service.activity_paused():
                    self.service.update_task(saved_task_id, description=final_desc)

        # Update Dependencies
        for i in range(self.dep_list.count()):
            item = self.dep_list.item(i)
            prereq_id = item.data(Qt.UserRole)
            if item.checkState() == Qt.Checked:
                success, err = self.service.add_dependency(task_id=saved_task_id, depends_on_task_id=prereq_id)
                if not success and err:
                    QMessageBox.warning(self, "Dependency Warning", f"Could not add dependency: {err}")
            else:
                self.service.remove_dependency(task_id=saved_task_id, depends_on_task_id=prereq_id)

        # Create scheduled reminder if requested
        if self.has_reminder_chk.isChecked() and due_date_val:
            offset = self.rem_offset_combo.currentData()
            rem_time = due_date_val - timedelta(minutes=offset)
            self.service.create_reminder(
                task_id=saved_task_id,
                remind_at=rem_time,
                offset_minutes=offset,
                message=f"Reminder: '{title}' is due at {due_date_val.strftime('%H:%M %b %d')}",
            )

        if attachment_errors:
            QMessageBox.warning(
                self,
                "Some attachments were not saved",
                "The task was saved, but these files could not be attached:\n\n" + "\n".join(attachment_errors),
            )

        self.accept()

    # -------------------------------------------------------------------------
    # Custom fields & templates
    # -------------------------------------------------------------------------
    def _manage_fields(self) -> None:
        from kanbanmanager.ui.dialogs.custom_fields_dialog import CustomFieldsDialog

        current = self.fields_panel.raw_values()
        dlg = CustomFieldsDialog(self.service, self.board_id, self)
        dlg.exec()
        if dlg.changed:
            self.fields_changed = True
            self.board_fields = self.service.get_custom_fields(self.board_id)
            defaults = {} if self.task_id else {f["id"]: f["default"] for f in self.board_fields}
            defaults.update({k: v for k, v in current.items() if v is not None})
            self.fields_panel.set_fields(self.board_fields, defaults)
            self._update_tab_titles()
        dlg.deleteLater()

    def _fill_template_menu(self) -> None:
        self.template_menu.clear()
        templates = self.service.get_task_templates(self.board_id)
        for t in templates:
            scope = "" if t["board_id"] else "   (all boards)"
            act = self.template_menu.addAction(f"{t['icon']}  {t['name']}{scope}".replace("&", "&&"))
            act.setToolTip(tpl.describe_task_template(t["data"]))
            act.triggered.connect(partial(self._apply_template_clicked, t))
        if not templates:
            none = self.template_menu.addAction("No templates on this board yet")
            none.setEnabled(False)
        self.template_menu.addSeparator()
        self.template_menu.addAction("Manage Templates…", self._manage_templates)

    def _apply_template_clicked(self, template: Dict[str, Any], _checked: bool = False) -> None:
        has_content = self.title_input.text().strip() or self.desc_editor.markdown().strip()
        if has_content:
            reply = QMessageBox.question(self, "Use Template", f"Replace what you've entered with the "
                                         f"'{template['name']}' template?", QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
        self.apply_template(template)

    def _manage_templates(self) -> None:
        from kanbanmanager.ui.dialogs.template_dialogs import TaskTemplatesDialog

        dlg = TaskTemplatesDialog(self.service, self.board_id, self)
        dlg.exec()
        dlg.deleteLater()

    def apply_template(self, template: Dict[str, Any]) -> List[str]:
        """Fill the form from a task template (nothing is saved until Save). Returns warnings."""
        data = template.get("data", template)
        kwargs, warnings = tpl.expand_task_template(self.service, data, self.board_id, create_labels=False)
        norm = tpl.normalize_task_template(data)
        self.template_name = template.get("name")
        self.title_input.setText(kwargs["raw_title"] if kwargs["raw_title"].strip() else "")
        self.desc_editor.set_markdown(kwargs["description"])
        idx = self.priority_combo.findData(kwargs["priority"])
        if idx >= 0:
            self.priority_combo.setCurrentIndex(idx)
        self.hours_spin.setValue(float(kwargs["estimated_hours"] or 0))
        self.assignee_combo.setCurrentText(kwargs["assignee"] or "")
        if norm["column"]:
            col_idx = self.column_combo.findText(norm["column"], Qt.MatchFixedString)
            if col_idx >= 0:
                self.column_combo.setCurrentIndex(col_idx)
        self.has_due_chk.setChecked(kwargs["due_date"] is not None)
        if kwargs["due_date"] is not None:
            self.due_datetime_edit.setDateTime(kwargs["due_date"])
        self.has_start_chk.setChecked(kwargs["start_date"] is not None)
        if kwargs["start_date"] is not None:
            d = kwargs["start_date"]
            self.start_date_edit.setDate(QDate(d.year, d.month, d.day))
        self.milestone_chk.setChecked(bool(kwargs["is_milestone"]))
        # Labels: tick the template's labels; ones the board lacks are added (created on Save)
        wanted = set(kwargs["label_ids"])
        for i in range(self.labels_list.count()):
            item = self.labels_list.item(i)
            if item.data(Qt.UserRole) is not None:
                item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in wanted else Qt.Unchecked)
        for n, name in enumerate(kwargs.get("new_labels", [])):
            item = QListWidgetItem(f"🏷 {name}  (new)")
            item.setData(Qt.UserRole, None)
            item.setData(Qt.UserRole + 1, name)
            item.setData(Qt.UserRole + 2, tpl.LABEL_PALETTE[n % len(tpl.LABEL_PALETTE)])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.labels_list.addItem(item)
        self._checklist_items = list(kwargs["checklist"])
        self._refresh_checklist_widget()
        self.fields_panel.set_values(kwargs["field_values"], only_given=True)
        self._update_tab_titles()
        note = f"📋 Filled in from the '{template.get('name', 'template')}' template - edit anything before saving."
        if warnings:
            note += "  ⚠ " + "; ".join(warnings)
        self.template_note.setText(note)
        self.template_note.setVisible(True)
        self._place_title_cursor = True
        self.title_input.setFocus()
        self.title_input.deselect()
        self.title_input.setCursorPosition(len(self.title_input.text()))
        return warnings

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if getattr(self, "_place_title_cursor", False):
            # Showing focuses the title with everything selected; a template's "Bug: " is meant to be
            # kept, so put the cursor after it (once the focus has settled)
            self._place_title_cursor = False
            QTimer.singleShot(0, self, self._title_cursor_to_end)

    def _title_cursor_to_end(self) -> None:
        self.title_input.setFocus()
        self.title_input.deselect()
        self.title_input.setCursorPosition(len(self.title_input.text()))

    def template_data(self) -> Dict[str, Any]:
        """The form's current content as task template data."""
        board_fields = {f["id"]: f["name"] for f in self.board_fields}
        labels = []
        for i in range(self.labels_list.count()):
            item = self.labels_list.item(i)
            if item.checkState() == Qt.Checked:
                labels.append(item.data(Qt.UserRole + 1) if item.data(Qt.UserRole) is None
                              else item.text().replace("🏷 ", "", 1))
        for i in range(self.checklist_widget.count()):
            if i < len(self._checklist_items):
                self._checklist_items[i]["done"] = False
        return tpl.normalize_task_template({
            "title": self.title_input.text(),
            "description": tpl._strip_attachment_refs(self.desc_editor.markdown()),
            "priority": self.priority_combo.currentData(),
            "labels": labels,
            "checklist": [i.get("text", "") for i in self._checklist_items],
            "estimated_hours": self.hours_spin.value(),
            "assignee": self.assignee_combo.currentText().strip(),
            "fields": {board_fields[k]: v for k, v in self.fields_panel.raw_values().items()
                       if v is not None and k in board_fields},
            "is_milestone": self.milestone_chk.isChecked(),
        })

    def save_as_template(self, _checked: bool = False) -> Optional[Dict[str, Any]]:
        from kanbanmanager.ui.dialogs.template_dialogs import TaskTemplateEditor

        data = self.template_data()
        name = (self.title_input.text().strip() or "New template")[:60]
        dlg = TaskTemplateEditor(self.service, self.board_id, {"name": name, "data": data, "icon": "📋",
                                                               "board_id": self.board_id}, self, is_new=True)
        saved = dlg.saved if dlg.exec() else None
        dlg.deleteLater()
        return saved

    # -------------------------------------------------------------------------
    # Rich content helpers
    # -------------------------------------------------------------------------
    def _update_tab_titles(self) -> None:
        if hasattr(self, "fields_panel") and hasattr(self, "_fields_tab"):
            filled = self.fields_panel.filled_count()
            flag = " ⚠" if self.fields_panel.missing_required() or self.fields_panel.errors() else ""
            self.tabs.setTabText(self._fields_tab, (f"Fields ({filled})" if filled else "Fields") + flag)
        if not hasattr(self, "activity_panel"):
            return
        n_att = self.attachments_panel.count()
        self.tabs.setTabText(self._attachments_tab, f"Attachments ({n_att})" if n_att else "Attachments")
        logged = self.time_panel.logged_seconds()
        self.tabs.setTabText(self._time_tab, f"Time ({format_duration(logged)})" if logged else "Time")
        n_com = self.activity_panel.comment_count()
        self.tabs.setTabText(self._activity_tab, f"Activity ({n_com})" if n_com else "Activity")
        self.tabs.setTabToolTip(
            self._activity_tab, f"Comments ({n_com}) and the full change history of this task"
        )

    def _on_time_changed(self) -> None:
        self.external_changes = True
        self._update_tab_titles()

    def _on_finished(self, _result: int) -> None:
        self.attachments_panel.cleanup()
        self.time_panel.detach()

    def _on_comments_changed(self) -> None:
        self.external_changes = True
        self._update_tab_titles()

    def _insert_inline_image(self, ref: str, alt: str) -> None:
        self.desc_editor.insert_image(ref, alt)
        self.tabs.setCurrentIndex(0)

    def _choose_inline_image(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(IMAGE_EXTENSIONS))
        path, _ = QFileDialog.getOpenFileName(self, "Insert Image", "", f"Images ({patterns})")
        if path:
            for ref, name in self.attachments_panel.add_files([path]):
                self.desc_editor.insert_image(ref, name.rsplit(".", 1)[0])

    def _on_image_pasted(self, image: QImage) -> None:
        staged = self.attachments_panel.stage_image(image)
        if staged:
            self.desc_editor.insert_image(staged[0], "screenshot")

    def _on_files_dropped(self, paths: List[str]) -> None:
        for ref, name in self.attachments_panel.add_files(paths):
            if any(name.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
                self.desc_editor.insert_image(ref, name.rsplit(".", 1)[0])

    def _on_attachment_removed(self, ref: str) -> None:
        md = self.desc_editor.markdown()
        if ref in attachment_refs(md):
            self.desc_editor.replace_markdown(remove_attachment_refs(md, ref))

    def has_unsaved_rich_changes(self) -> bool:
        return self.attachments_panel.has_changes() or self.desc_editor.is_modified()

    def reject(self) -> None:
        if self.has_unsaved_rich_changes():
            reply = QMessageBox.question(
                self,
                "Discard Changes?",
                "You have unsaved changes to the description or attachments. Discard them?",
                QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if reply != QMessageBox.Discard:
                return
        super().reject()

    def _delete_task(self) -> None:
        if not self.task_id:
            return
        reply = QMessageBox.question(
            self,
            "Delete Task",
            "Are you sure you want to permanently delete this task?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.service.delete_task(self.task_id)
            self.accept()

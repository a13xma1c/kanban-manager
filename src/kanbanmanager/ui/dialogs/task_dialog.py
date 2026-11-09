"""
Comprehensive Task Editor Dialog with Rich Markdown Descriptions, Attachments,
Comments & Audit Trail, Due Dates, Reminders, Recurrence, Labels, and Dependencies
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List

from PySide6.QtCore import Qt, QDate, QTime, QDateTime
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
)

from kanbanmanager.config import IMAGE_EXTENSIONS
from kanbanmanager.core.markdown_utils import attachment_refs, remove_attachment_refs, rewrite_attachment_refs
from kanbanmanager.enums import Priority, RecurrenceFrequency, DependencyType
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.components.activity_panel import ActivityPanel
from kanbanmanager.ui.components.attachments_panel import AttachmentsPanel
from kanbanmanager.ui.components.markdown_editor import MarkdownEditor


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
    ):
        super().__init__(parent)
        self.board_id = board_id
        self.task_id = task_id
        self.default_column_id = default_column_id
        self.service = service or DatabaseService()

        self.setWindowTitle("Edit Task" if self.task_id else "Create New Task")
        self.setMinimumSize(760, 620)
        self.resize(960, 760)

        self._checklist_items: List[Dict[str, Any]] = []
        # True when something was persisted without Save (e.g. a comment was posted)
        self.external_changes = False

        self._init_ui()
        self._load_data()
        self.finished.connect(lambda _r: self.attachments_panel.cleanup())

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 18, 20, 18)
        main_layout.setSpacing(14)

        # Title Input
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title_lbl = QLabel("Task Title *")
        title_lbl.setStyleSheet("font-weight: 600; color: #F8FAFC;")
        title_box.addWidget(title_lbl)

        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("e.g. Implement drag-and-drop workflow")
        title_box.addWidget(self.title_input)
        main_layout.addLayout(title_box)

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
        self.has_due_chk = QCheckBox("Set Due Date & Time:")
        self.due_datetime_edit = QDateTimeEdit()
        self.due_datetime_edit.setCalendarPopup(True)
        self.due_datetime_edit.setDateTime(QDateTime.currentDateTime().addDays(1))
        self.due_datetime_edit.setEnabled(False)
        self.has_due_chk.toggled.connect(self.due_datetime_edit.setEnabled)
        dates_grid.addWidget(self.has_due_chk, 1, 0)
        dates_grid.addWidget(self.due_datetime_edit, 1, 1)

        dates_layout.addLayout(dates_grid)

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

        # Load board labels
        labels = self.service.get_labels_for_board(self.board_id)
        self.labels_list.clear()
        for lbl in labels:
            item = QListWidgetItem(f"🏷 {lbl.name}")
            item.setData(Qt.UserRole, lbl.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.labels_list.addItem(item)

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

        self.title_input.setText(task.title)
        self.desc_editor.set_markdown(task.description or "")

        col_idx = self.column_combo.findData(task.column_id)
        if col_idx >= 0:
            self.column_combo.setCurrentIndex(col_idx)

        pri_idx = self.priority_combo.findData(task.priority)
        if pri_idx >= 0:
            self.priority_combo.setCurrentIndex(pri_idx)

        self.hours_spin.setValue(task.estimated_hours or 0.0)

        # Dates
        if task.start_date:
            self.has_start_chk.setChecked(True)
            self.start_date_edit.setDate(QDate(task.start_date.year, task.start_date.month, task.start_date.day))

        if task.due_date:
            self.has_due_chk.setChecked(True)
            self.due_datetime_edit.setDateTime(task.due_date)

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

        column_id = self.column_combo.currentData()
        priority = self.priority_combo.currentData()
        estimated_hours = self.hours_spin.value()
        desc = self.desc_editor.markdown()

        start_date_val = self.start_date_edit.date().toPython() if self.has_start_chk.isChecked() else None
        due_date_val = self.due_datetime_edit.dateTime().toPython() if self.has_due_chk.isChecked() else None

        # Gather labels
        selected_label_ids = []
        for i in range(self.labels_list.count()):
            item = self.labels_list.item(i)
            if item.checkState() == Qt.Checked:
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
            )
            saved_task_id = self.task_id
        else:
            # Create new
            task = self.service.create_task(
                board_id=self.board_id,
                column_id=column_id,
                title=title,
                description=desc,
                priority=priority,
                estimated_hours=estimated_hours,
                start_date=start_date_val,
                due_date=due_date_val,
                label_ids=selected_label_ids,
                checklist=self._checklist_items,
                is_recurring=is_rec,
                recurrence_rule=rec_rule,
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
    # Rich content helpers
    # -------------------------------------------------------------------------
    def _update_tab_titles(self) -> None:
        n_att = self.attachments_panel.count()
        self.tabs.setTabText(self._attachments_tab, f"Attachments ({n_att})" if n_att else "Attachments")
        n_com = self.activity_panel.comment_count()
        self.tabs.setTabText(self._activity_tab, f"Activity ({n_com})" if n_com else "Activity")
        self.tabs.setTabToolTip(
            self._activity_tab, f"Comments ({n_com}) and the full change history of this task"
        )

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

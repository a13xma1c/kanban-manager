"""
Interactive Task Card Widget with Full Drag-and-Drop Support
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Dict, Any, List

from PySide6.QtCore import Qt, QPoint, QMimeData, QByteArray, QDataStream, QIODevice, Signal
from PySide6.QtGui import (
    QDrag,
    QPixmap,
    QPainter,
    QColor,
    QFont,
    QCursor,
    QIcon,
    QAction,
    QLinearGradient,
)
from PySide6.QtWidgets import (
    QMessageBox,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QMenu,
    QProgressBar,
    QSizePolicy,
    QApplication,
)

from kanbanmanager.enums import Priority
from kanbanmanager.core.markdown_utils import first_line_snippet
from kanbanmanager.core.signals import app_signals
from kanbanmanager.ui.components.activity_panel import avatar_color, initials
from kanbanmanager.ui.components.flow_layout import FlowLayout
from kanbanmanager.ui.colors import alpha, chip_style
from kanbanmanager.core import custom_fields as cf


MIME_TASK_ID = "application/x-kanban-task-id"


def confirm_task_deletion(parent: Optional[QWidget], task: Dict[str, Any]) -> bool:
    """Ask before deleting a task for good (``app_signals.task_deleted`` means: confirmed)."""
    extras = [f"{n} {what}{'s' if n != 1 else ''}" for n, what in
              ((task.get("comment_count", 0), "comment"), (task.get("attachment_count", 0), "attachment")) if n]
    detail = f"\n\nIts {' and '.join(extras)} and logged time go with it." if extras else ""
    return QMessageBox.question(
        parent, "Delete Task", f"Permanently delete '{task.get('title', '')}'?{detail}\n\n"
        "Archive it instead to keep it out of the way but restorable.",
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes


class TaskCardWidget(QFrame):
    """
    Card representing a task in a Kanban column.
    Supports dragging, status toggling, label pills, priority indicators,
    due date warnings, and dependency badges.
    """

    def __init__(self, task_data: Dict[str, Any], parent: Optional[QWidget] = None,
                 fields: Optional[List[Dict[str, Any]]] = None):
        """``fields``: the board's custom field definitions (values shown as chips)."""
        super().__init__(parent)
        self.task_data = task_data
        self.fields = fields or []
        self.task_id = task_data.get("id", 0)
        self.column_id = task_data.get("column_id", 0)
        self.board_id = task_data.get("board_id", 0)

        self._drag_start_pos: Optional[QPoint] = None

        self.setObjectName("TaskCardWidget")
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.setFocusPolicy(Qt.StrongFocus)   # reachable with Tab; vim keys select it too
        self.setAccessibleName(self._describe())

        self._init_ui()

    def _describe(self) -> str:
        """What a screen reader announces for this card."""
        t = self.task_data
        parts = [f"Task: {t.get('title', '')}"]
        if t.get("is_completed"):
            parts.append("completed")
        if t.get("priority"):
            try:
                parts.append(f"priority {Priority(t['priority']).display_name}")
            except ValueError:
                pass
        if t.get("due_date"):
            parts.append(("overdue, was due " if t.get("is_overdue") else "due ") + t["due_date"][:10])
        if t.get("assignee"):
            parts.append(f"assigned to {t['assignee']}")
        if t.get("is_blocked"):
            parts.append("blocked by prerequisites")
        return ", ".join(parts)

    def set_selected(self, selected: bool) -> None:
        """The keyboard-navigation highlight (styled via TaskCardWidget[selected="true"])."""
        if (self.property("selected") == "true") == selected:
            return
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    @property
    def is_selected(self) -> bool:
        return self.property("selected") == "true"

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        app_signals.card_focused.emit(self.task_id)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            app_signals.task_edit_requested.emit(self.task_id)
        else:
            super().keyPressEvent(event)

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # ---------------------------------------------------------------------
        # Top Row: Labels + Priority Badge
        # ---------------------------------------------------------------------
        # Labels + priority wrap (like the badges below) so cards fit narrow columns, e.g. in Split View
        top_row = FlowLayout(h_spacing=4, v_spacing=4)

        # Labels container
        labels = self.task_data.get("labels", [])
        if labels:
            for lbl in labels[:3]:  # Show first 3 labels
                lbl_pill = QLabel(lbl.get("name", ""))
                color = lbl.get("color", "#3B82F6")
                lbl_pill.setStyleSheet(chip_style(color))
                top_row.addWidget(lbl_pill)
            if len(labels) > 3:
                more_lbl = QLabel(f"+{len(labels) - 3}")
                more_lbl.setStyleSheet("color: #94A3B8; font-size: 10px;")
                top_row.addWidget(more_lbl)

        # Priority badge
        pri_val = self.task_data.get("priority", Priority.MEDIUM.value)
        try:
            pri_enum = Priority(pri_val)
        except ValueError:
            pri_enum = Priority.MEDIUM

        pri_badge = QLabel(f"{pri_enum.icon_char} {pri_enum.display_name}")
        pri_badge.setStyleSheet(
            f"color: {pri_enum.color}; background-color: {alpha(pri_enum.color, 0x18)}; "
            f"border: 1px solid {alpha(pri_enum.color, 0x44)}; border-radius: 4px; "
            f"padding: 2px 6px; font-size: 10px; font-weight: bold;"
        )
        top_row.addWidget(pri_badge)
        layout.addLayout(top_row)

        # ---------------------------------------------------------------------
        # Middle: Title & Checkbox
        # ---------------------------------------------------------------------
        title_row = QHBoxLayout()
        title_row.setSpacing(6)

        # Complete check button
        self.check_btn = QPushButton("✓" if self.task_data.get("is_completed") else "○")
        self.check_btn.setFixedSize(22, 22)
        self.check_btn.setAccessibleName("Mark incomplete" if self.task_data.get("is_completed") else "Mark complete")
        self.check_btn.setToolTip("Mark incomplete" if self.task_data.get("is_completed") else "Mark complete")
        if self.task_data.get("is_completed"):
            self.check_btn.setStyleSheet(
                "background-color: #10B981; color: #FFFFFF; border-radius: 11px; font-weight: bold; border: none;"
            )
        else:
            self.check_btn.setStyleSheet(
                "background-color: transparent; color: #64748B; border: 1px solid #475569; border-radius: 11px;"
            )
        self.check_btn.clicked.connect(self._on_toggle_completed)
        title_row.addWidget(self.check_btn)

        # Title Label
        title_text = self.task_data.get("title", "")
        self.title_lbl = QLabel(title_text)
        self.title_lbl.setWordWrap(True)
        if self.task_data.get("is_completed"):
            self.title_lbl.setStyleSheet("color: #64748B; text-decoration: line-through; font-size: 13px;")
        else:
            self.title_lbl.setStyleSheet("color: #F8FAFC; font-weight: 600; font-size: 13px;")
        title_row.addWidget(self.title_lbl, 1)

        layout.addLayout(title_row)

        # Description snippet (Markdown rendered down to its first line of plain text)
        snippet = first_line_snippet(self.task_data.get("description", ""))
        if snippet:
            desc_lbl = QLabel(snippet)
            desc_lbl.setTextFormat(Qt.PlainText)
            desc_lbl.setWordWrap(True)  # never let a long snippet widen the card past its column
            desc_lbl.setStyleSheet("color: #94A3B8; font-size: 11px;")
            layout.addWidget(desc_lbl)

        # ---------------------------------------------------------------------
        # Custom fields shown on cards (and a warning for required ones left empty)
        # ---------------------------------------------------------------------
        self.field_chips: List[QLabel] = []
        if self.fields:
            values = self.task_data.get("field_values") or {}
            field_row = FlowLayout(h_spacing=4, v_spacing=4)
            for f in self.fields:
                if not f.get("show_on_card"):
                    continue
                for chip in cf.value_chips(f, values.get(f["id"])):
                    pill = QLabel(chip.text)
                    pill.setTextFormat(Qt.PlainText)
                    pill.setToolTip(chip.tooltip)
                    if chip.color:
                        pill.setStyleSheet(chip_style(chip.color))
                    else:
                        pill.setStyleSheet("color: #CBD5E1; background-color: #263244; border: 1px solid #334155; "
                                           "border-radius: 4px; padding: 1px 6px; font-size: 10px;")
                    field_row.addWidget(pill)
                    self.field_chips.append(pill)
            missing = cf.missing_required(self.fields, values)
            if missing and not self.task_data.get("is_completed"):
                warn = QLabel(f"⚠ {len(missing)} required field{'s' if len(missing) != 1 else ''}")
                warn.setToolTip("Missing: " + ", ".join(missing))
                warn.setStyleSheet(chip_style("#F97316"))
                field_row.addWidget(warn)
                self.field_chips.append(warn)
            if self.field_chips:
                layout.addLayout(field_row)
            else:
                field_row.deleteLater()

        # ---------------------------------------------------------------------
        # Checklist Progress
        # ---------------------------------------------------------------------
        checklist = self.task_data.get("checklist", [])
        if checklist:
            total_items = len(checklist)
            done_items = sum(1 for item in checklist if item.get("done"))
            progress_pct = int((done_items / total_items) * 100) if total_items > 0 else 0

            check_row = QHBoxLayout()
            check_row.setSpacing(6)

            pbar = QProgressBar()
            pbar.setFixedHeight(5)
            pbar.setTextVisible(False)
            pbar.setRange(0, 100)
            pbar.setValue(progress_pct)
            pbar_color = "#10B981" if progress_pct == 100 else "#3B82F6"
            pbar.setStyleSheet(
                f"QProgressBar {{ background-color: #334155; border: none; border-radius: 2px; }} "
                f"QProgressBar::chunk {{ background-color: {pbar_color}; border-radius: 2px; }}"
            )
            check_row.addWidget(pbar, 1)

            check_count_lbl = QLabel(f"☑ {done_items}/{total_items}")
            check_count_lbl.setStyleSheet("color: #94A3B8; font-size: 10px; font-weight: 500;")
            check_row.addWidget(check_count_lbl)

            layout.addLayout(check_row)

        # ---------------------------------------------------------------------
        # Bottom Badges: Due Date, Blocked, Recurring, Hours
        # ---------------------------------------------------------------------
        # Badges wrap onto a second line instead of widening the card past its column
        bottom_row = FlowLayout(h_spacing=6, v_spacing=4)

        # Due Date Badge
        due_iso = self.task_data.get("due_date")
        if due_iso:
            try:
                due_dt = datetime.fromisoformat(due_iso)
                due_str = due_dt.strftime("%b %d")
                is_overdue = self.task_data.get("is_overdue", False)
                
                due_badge = QLabel(f"📅 {due_str}")
                if is_overdue:
                    due_badge.setStyleSheet(
                        "color: #EF4444; background-color: rgba(239, 68, 68, 34); border: 1px solid rgba(239, 68, 68, 102); "
                        "border-radius: 4px; padding: 1px 5px; font-size: 10px; font-weight: bold;"
                    )
                else:
                    due_badge.setStyleSheet(
                        "color: #38BDF8; background-color: rgba(2, 132, 199, 34); border: 1px solid rgba(2, 132, 199, 68); "
                        "border-radius: 4px; padding: 1px 5px; font-size: 10px;"
                    )
                bottom_row.addWidget(due_badge)
            except Exception:
                pass

        # Dependency Indicators
        if self.task_data.get("is_blocked"):
            blocked_badge = QLabel("🔒 Blocked")
            blocked_badge.setToolTip("Waiting on unfinished prerequisite tasks")
            blocked_badge.setStyleSheet(
                "color: #EF4444; background-color: rgba(239, 68, 68, 34); border: 1px solid rgba(239, 68, 68, 102); "
                "border-radius: 4px; padding: 1px 5px; font-size: 10px; font-weight: bold;"
            )
            bottom_row.addWidget(blocked_badge)
        elif self.task_data.get("prerequisites"):
            prereq_badge = QLabel(f"🔗 {len(self.task_data['prerequisites'])} prereqs")
            prereq_badge.setStyleSheet(
                "color: #10B981; background-color: rgba(16, 185, 129, 34); border: 1px solid rgba(16, 185, 129, 68); "
                "border-radius: 4px; padding: 1px 5px; font-size: 10px;"
            )
            bottom_row.addWidget(prereq_badge)

        # Recurring Indicator
        if self.task_data.get("is_recurring"):
            rec_badge = QLabel("🔁 Recurring")
            rec_badge.setStyleSheet(
                "color: #A855F7; background-color: rgba(168, 85, 247, 34); border: 1px solid rgba(168, 85, 247, 68); "
                "border-radius: 4px; padding: 1px 5px; font-size: 10px;"
            )
            bottom_row.addWidget(rec_badge)


        # Rich-content counters
        for key, icon, tip in (
            ("attachment_count", "📎", "attachment"),
            ("comment_count", "💬", "comment"),
        ):
            n = self.task_data.get(key, 0)
            if n:
                counter = QLabel(f"{icon} {n}")
                counter.setToolTip(f"{n} {tip}{'s' if n != 1 else ''}")
                counter.setStyleSheet("color: #94A3B8; font-size: 10px;")
                bottom_row.addWidget(counter)

        # Scheduling flags
        if self.task_data.get("is_milestone"):
            ms = QLabel("◆ Milestone")
            ms.setToolTip("A point in time on the project schedule")
            ms.setStyleSheet(
                "color: #FBBF24; background-color: #422006; border: 1px solid #92400E; "
                "border-radius: 4px; padding: 1px 5px; font-size: 10px; font-weight: bold;"
            )
            bottom_row.addWidget(ms)
        if self.task_data.get("dates_locked"):
            lock = QLabel("📌")
            lock.setToolTip("Dates locked: auto-scheduling and cascading never move this task")
            lock.setStyleSheet("font-size: 10px;")
            bottom_row.addWidget(lock)

        # Live timer indicator
        if self.task_data.get("timer_running"):
            live = QLabel("● Tracking")
            live.setToolTip("A timer is running on this task")
            live.setStyleSheet(
                "color: #F87171; background-color: #450A0A; border: 1px solid #7F1D1D; "
                "border-radius: 4px; padding: 1px 5px; font-size: 10px; font-weight: bold;"
            )
            bottom_row.addWidget(live)

        # Time: logged vs estimate ("⏱ 2.5/5h"), or just the estimate
        est_hours = float(self.task_data.get("estimated_hours") or 0.0)
        spent = float(self.task_data.get("spent_hours") or 0.0)
        if est_hours > 0 or spent > 0:
            if spent > 0 and est_hours > 0:
                text = f"⏱ {spent:.1f}/{est_hours:g}h"
                tip = f"{spent:.2f}h logged of {est_hours:g}h estimated"
            elif spent > 0:
                text, tip = f"⏱ {spent:.1f}h", f"{spent:.2f}h logged (no estimate)"
            else:
                text, tip = f"⏱ {est_hours:.1f}h", f"{est_hours:g}h estimated"
            hrs_lbl = QLabel(text)
            hrs_lbl.setToolTip(tip)
            over = est_hours > 0 and spent > est_hours
            hrs_lbl.setStyleSheet(f"color: {'#F87171' if over else '#64748B'}; font-size: 10px;")
            bottom_row.addWidget(hrs_lbl)

        # Assignee avatar
        assignee = (self.task_data.get("assignee") or "").strip()
        if assignee:
            avatar = QLabel(initials(assignee))
            avatar.setFixedSize(20, 20)
            avatar.setAlignment(Qt.AlignCenter)
            avatar.setToolTip(f"Assigned to {assignee}")
            avatar.setStyleSheet(
                f"background-color: {avatar_color(assignee)}; color: white; border-radius: 10px; "
                "font-size: 9px; font-weight: 700;"
            )
            bottom_row.addWidget(avatar)

        layout.addLayout(bottom_row)

    def _on_toggle_completed(self) -> None:
        """Trigger completion toggle signal."""
        app_signals.task_completed_toggled.emit(self.task_id, not self.task_data.get("is_completed", False))

    def _delete(self, _checked: bool = False) -> None:
        if confirm_task_deletion(self, self.task_data):
            app_signals.task_deleted.emit(self.task_id)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not (event.buttons() & Qt.LeftButton) or not self._drag_start_pos:
            return

        if (event.position().toPoint() - self._drag_start_pos).manhattanLength() < QApplication.startDragDistance():
            return

        # Prepare Qt Drag
        drag = QDrag(self)
        mime_data = QMimeData()

        # Encode task_id and source column_id
        payload = f"{self.task_id}:{self.column_id}".encode("utf-8")
        mime_data.setData(MIME_TASK_ID, QByteArray(payload))
        drag.setMimeData(mime_data)

        # Generate drag preview pixmap
        pixmap = QPixmap(self.size())
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setOpacity(0.85)
        self.render(painter, QPoint(0, 0))  # PySide6 needs the offset for the QPainter overload
        painter.end()

        drag.setPixmap(pixmap)
        drag.setHotSpot(event.position().toPoint())

        self.setGraphicsEffect(None)
        drag.exec(Qt.MoveAction)
        self._drag_start_pos = None

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            app_signals.task_edit_requested.emit(self.task_id)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)

        edit_act = menu.addAction("✏ Edit Task")
        edit_act.triggered.connect(lambda: app_signals.task_edit_requested.emit(self.task_id))

        toggle_act = menu.addAction("✓ Mark Complete" if not self.task_data.get("is_completed") else "○ Mark Incomplete")
        toggle_act.triggered.connect(self._on_toggle_completed)

        menu.addSeparator()
        if self.task_data.get("timer_running"):
            menu.addAction("■ Stop Timer", app_signals.timer_stop_requested.emit)
        else:
            menu.addAction("▶ Start Stopwatch", lambda: app_signals.timer_start_requested.emit(self.task_id, "stopwatch"))
            menu.addAction("🍅 Start Pomodoro", lambda: app_signals.timer_start_requested.emit(self.task_id, "pomodoro"))

        menu.addSeparator()
        menu.addAction("📋 Save as Template…", lambda: app_signals.task_template_save_requested.emit(self.task_id))
        menu.addSeparator()

        menu.addAction("🗄 Archive Task", lambda: app_signals.task_archive_requested.emit(self.task_id))
        del_act = menu.addAction("🗑 Delete Task")
        del_act.triggered.connect(self._delete)

        menu.exec(event.globalPos())

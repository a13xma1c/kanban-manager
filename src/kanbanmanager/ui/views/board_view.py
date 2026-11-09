"""
Kanban Board View with Drag-and-Drop Columns and Workflow Enforcement
"""
from __future__ import annotations

from typing import Optional, Dict, Any, List

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QScrollArea,
    QPushButton,
    QFrame,
    QLabel,
    QMessageBox,
)

from kanbanmanager.core.signals import app_signals
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.components.column_widget import KanbanColumnWidget


class BoardView(QWidget):
    """
    Main Kanban Board View containing columns and task cards.
    Handles horizontal layout, column creation, and task filtering.
    """

    task_add_requested = Signal(int)  # column_id
    column_add_requested = Signal(int)  # board_id
    column_edit_requested = Signal(int)  # column_id

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.current_board_id: Optional[int] = None
        self.current_filters: Dict[str, Any] = {}
        self.column_widgets: Dict[int, KanbanColumnWidget] = {}
        self.running_task_id: Optional[int] = None  # task with a live timer (badge on its card)

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(0)

        # Horizontal Scroll Area for Kanban Columns
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("background: transparent;")

        self.columns_container = QWidget()
        self.columns_layout = QHBoxLayout(self.columns_container)
        self.columns_layout.setContentsMargins(0, 0, 0, 0)
        self.columns_layout.setSpacing(16)
        self.columns_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.scroll_area.setWidget(self.columns_container)
        layout.addWidget(self.scroll_area)

    def load_board(self, board_id: int) -> None:
        """Load columns and tasks for the given board ID."""
        self.current_board_id = board_id
        self.refresh()

    def apply_filters(self, filters: Dict[str, Any]) -> None:
        """Apply active search & filter criteria to cards."""
        self.current_filters = filters
        self.refresh()

    def refresh(self) -> None:
        """Reload board columns and filter/populate tasks."""
        if not self.current_board_id:
            return

        board = self.service.get_board(self.current_board_id)
        if not board:
            return

        # Fetch all tasks matching filters
        search = self.current_filters.get("search")
        priority = self.current_filters.get("priority")
        label_id = self.current_filters.get("label_id")
        is_completed = self.current_filters.get("is_completed")

        label_ids = [label_id] if label_id is not None else None
        tasks = self.service.get_tasks_for_board(
            board_id=self.current_board_id,
            search_query=search,
            priority=priority,
            label_ids=label_ids,
            is_completed=is_completed,
        )

        # Filter by due date if specified
        due_filter = self.current_filters.get("due_filter", "all")
        if due_filter == "overdue":
            tasks = [t for t in tasks if t.is_overdue]
        elif due_filter == "has_due":
            tasks = [t for t in tasks if t.due_date is not None]

        # Group tasks by column
        tasks_by_col: Dict[int, List[Dict[str, Any]]] = {c.id: [] for c in board.columns}
        for t in tasks:
            if t.column_id in tasks_by_col:
                data = t.to_dict()
                data["timer_running"] = t.id == self.running_task_id
                tasks_by_col[t.column_id].append(data)

        # Clear existing column widgets
        while self.columns_layout.count() > 0:
            item = self.columns_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.column_widgets.clear()

        # Build column widgets
        for col in board.columns:
            col_data = col.to_dict()
            col_tasks = tasks_by_col.get(col.id, [])
            col_data["task_count"] = len(col_tasks)

            col_widget = KanbanColumnWidget(col_data, self.columns_container)
            col_widget.task_add_requested.connect(self.task_add_requested.emit)
            col_widget.column_edit_requested.connect(self.column_edit_requested.emit)
            col_widget.column_delete_requested.connect(self._delete_column)

            col_widget.set_tasks(col_tasks)
            self.column_widgets[col.id] = col_widget
            self.columns_layout.addWidget(col_widget)

        # Add Column Button at end of columns
        add_col_btn = QPushButton("➕ Add Column")
        add_col_btn.setFixedSize(140, 42)
        add_col_btn.setStyleSheet(
            "QPushButton { background-color: #1E293B; color: #94A3B8; border: 2px dashed #334155; border-radius: 8px; font-weight: 600; } "
            "QPushButton:hover { background-color: #334155; color: #F8FAFC; border-color: #475569; }"
        )
        add_col_btn.clicked.connect(lambda: self.column_add_requested.emit(self.current_board_id))
        self.columns_layout.addWidget(add_col_btn)

    def _delete_column(self, column_id: int) -> None:
        col = self.service.get_columns_for_board(self.current_board_id)
        reply = QMessageBox.question(
            self,
            "Delete Column",
            "Are you sure you want to delete this column? All tasks inside will also be removed.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.service.delete_column(column_id)
            self.refresh()
            app_signals.board_refresh_requested.emit()

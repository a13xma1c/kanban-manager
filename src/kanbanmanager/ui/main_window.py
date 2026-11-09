"""
Main Application Window for KanbanManager
"""
from __future__ import annotations

from typing import Optional, Dict, Any

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QStackedWidget,
    QStatusBar,
    QMessageBox,
    QSystemTrayIcon,
)

from kanbanmanager.config import APP_NAME, APP_VERSION
from kanbanmanager.enums import ViewMode
from kanbanmanager.core.signals import app_signals
from kanbanmanager.core.recurrence import RecurrenceEngine
from kanbanmanager.core.reminder_engine import ReminderEngine
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.styles import get_stylesheet
from kanbanmanager.ui.components.workspace_sidebar import WorkspaceSidebar
from kanbanmanager.ui.components.filter_bar import FilterBar
from kanbanmanager.ui.components.stats_bar import StatsBar
from kanbanmanager.ui.views.board_view import BoardView
from kanbanmanager.ui.views.calendar_view import CalendarView
from kanbanmanager.ui.views.timeline_view import TimelineView
from kanbanmanager.ui.views.analytics_view import AnalyticsView
from kanbanmanager.ui.dialogs.task_dialog import TaskDialog
from kanbanmanager.ui.dialogs.board_dialog import BoardDialog
from kanbanmanager.ui.dialogs.column_dialog import ColumnDialog
from kanbanmanager.ui.dialogs.workspace_dialog import WorkspaceDialog
from kanbanmanager.ui.dialogs.backup_dialog import BackupDialog


class MainWindow(QMainWindow):
    """
    Main Application Window orchestrating workspaces, boards,
    views (Board, Calendar, Timeline, Analytics), and background engines.
    """

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.current_board_id: Optional[int] = None

        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1280, 800)
        self.setMinimumSize(960, 600)
        self.setStyleSheet(get_stylesheet())

        self._init_ui()
        self._connect_signals()

        # Start background reminder engine
        self.reminder_engine = ReminderEngine(self.service, self)
        self.reminder_engine.start(interval_ms=15000)

        # Initial data load
        self.sidebar.refresh()
        if self.sidebar.current_board_id and not self.current_board_id:
            self._on_board_selected(self.sidebar.current_board_id)

    def _init_ui(self) -> None:
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        root_layout = QHBoxLayout(central_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Left Sidebar
        self.sidebar = WorkspaceSidebar(self.service, self)
        root_layout.addWidget(self.sidebar)

        # Right Main Container
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # Top Filter & Search Bar
        self.filter_bar = FilterBar(self)
        right_layout.addWidget(self.filter_bar)

        # Board Stats Summary Bar
        self.stats_bar = StatsBar(self)
        right_layout.addWidget(self.stats_bar)

        # Stacked Views (Board, Calendar, Timeline, Analytics)
        self.view_stack = QStackedWidget()

        self.board_view = BoardView(self.service, self)
        self.calendar_view = CalendarView(self.service, self)
        self.timeline_view = TimelineView(self.service, self)
        self.analytics_view = AnalyticsView(self.service, self)

        self.view_stack.addWidget(self.board_view)      # Index 0
        self.view_stack.addWidget(self.calendar_view)   # Index 1
        self.view_stack.addWidget(self.timeline_view)   # Index 2
        self.view_stack.addWidget(self.analytics_view)  # Index 3

        right_layout.addWidget(self.view_stack, 1)
        root_layout.addWidget(right_container, 1)

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready", 3000)

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+N"), self, self._open_new_task_dialog)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.filter_bar.search_input.setFocus())

    def _connect_signals(self) -> None:
        # Sidebar Actions
        self.sidebar.new_workspace_requested.connect(self._open_new_workspace_dialog)
        self.sidebar.new_board_requested.connect(self._open_new_board_dialog)
        self.sidebar.edit_workspace_requested.connect(self._open_edit_workspace_dialog)
        self.sidebar.edit_board_requested.connect(self._open_edit_board_dialog)
        self.sidebar.backup_manager_requested.connect(self._open_backup_dialog)
        self.sidebar.demo_board_requested.connect(self.create_demo_board)

        # App Signals
        app_signals.board_selected.connect(self._on_board_selected)
        app_signals.view_mode_changed.connect(self._on_view_mode_changed)
        app_signals.filter_changed.connect(self._on_filter_changed)
        app_signals.task_edit_requested.connect(self._open_edit_task_dialog)
        app_signals.task_moved.connect(self._on_task_moved)
        app_signals.task_completed_toggled.connect(self._on_task_completed_toggled)
        app_signals.task_deleted.connect(self._on_task_deleted)
        app_signals.backup_restored.connect(self._on_backup_restored)
        app_signals.board_refresh_requested.connect(self.refresh_active_view)
        app_signals.notification_requested.connect(self._show_notification)

        # Board View Specifics
        self.board_view.task_add_requested.connect(self._open_new_task_in_column)
        self.board_view.column_add_requested.connect(self._open_new_column_dialog)
        self.board_view.column_edit_requested.connect(self._open_edit_column_dialog)

    @Slot(int)
    def _on_board_selected(self, board_id: int) -> None:
        self.current_board_id = board_id
        board = self.service.get_board(board_id)
        if board:
            self.setWindowTitle(f"{APP_NAME} - {board.name}")
            labels = [lbl.to_dict() for lbl in board.labels]
            self.filter_bar.set_board_labels(labels)

        self.refresh_active_view()

    @Slot(str)
    def _on_view_mode_changed(self, mode: str) -> None:
        if mode == ViewMode.BOARD.value:
            self.view_stack.setCurrentIndex(0)
        elif mode == ViewMode.CALENDAR.value:
            self.view_stack.setCurrentIndex(1)
        elif mode == ViewMode.TIMELINE.value:
            self.view_stack.setCurrentIndex(2)
        elif mode == ViewMode.ANALYTICS.value:
            self.view_stack.setCurrentIndex(3)

        self.refresh_active_view()

    @Slot(dict)
    def _on_filter_changed(self, filters: Dict[str, Any]) -> None:
        if self.view_stack.currentIndex() == 0:
            self.board_view.apply_filters(filters)
        else:
            self.refresh_active_view()

    def refresh_active_view(self) -> None:
        if not self.current_board_id:
            return

        # Update stats
        tasks = self.service.get_tasks_for_board(self.current_board_id)
        self.stats_bar.update_stats([t.to_dict() for t in tasks])

        curr_idx = self.view_stack.currentIndex()
        if curr_idx == 0:
            self.board_view.load_board(self.current_board_id)
        elif curr_idx == 1:
            self.calendar_view.load_board(self.current_board_id)
        elif curr_idx == 2:
            self.timeline_view.load_board(self.current_board_id)
        elif curr_idx == 3:
            self.analytics_view.load_board(self.current_board_id)

    @Slot(int, int)
    def _on_task_moved(self, task_id: int, target_column_id: int) -> None:
        success, err = self.service.move_task(task_id, target_column_id)
        if not success:
            QMessageBox.warning(self, "Workflow Rule Alert", f"Cannot move task:\n{err}")
        else:
            self.status_bar.showMessage("Task moved successfully", 2000)

        self.refresh_active_view()

    @Slot(int, bool)
    def _on_task_completed_toggled(self, task_id: int, is_completed: bool) -> None:
        task = self.service.toggle_task_completion(task_id)
        if task and task.is_completed and task.is_recurring:
            # Handle recurring task generation
            new_id = RecurrenceEngine.handle_recurring_task_completion(self.service, task_id)
            if new_id:
                self.status_bar.showMessage("Completed recurring task & created next occurrence", 3000)

        self.refresh_active_view()

    @Slot(int)
    def _on_task_deleted(self, task_id: int) -> None:
        self.service.delete_task(task_id)
        self.status_bar.showMessage("Task deleted", 2000)
        self.refresh_active_view()

    @Slot()
    def _on_backup_restored(self) -> None:
        self.sidebar.refresh()
        self.refresh_active_view()

    @Slot(str, str, str)
    def _show_notification(self, title: str, message: str, severity: str) -> None:
        self.status_bar.showMessage(f"🔔 {title}: {message}", 6000)

    # -------------------------------------------------------------------------
    # Dialog Openers
    # -------------------------------------------------------------------------
    def _open_new_task_dialog(self) -> None:
        if not self.current_board_id:
            return
        dlg = TaskDialog(board_id=self.current_board_id, service=self.service, parent=self)
        if dlg.exec():
            self.refresh_active_view()

    def _open_new_task_in_column(self, column_id: int) -> None:
        if not self.current_board_id:
            return
        dlg = TaskDialog(
            board_id=self.current_board_id,
            default_column_id=column_id,
            service=self.service,
            parent=self,
        )
        if dlg.exec():
            self.refresh_active_view()

    def _open_edit_task_dialog(self, task_id: int) -> None:
        if not self.current_board_id:
            return
        dlg = TaskDialog(
            board_id=self.current_board_id,
            task_id=task_id,
            service=self.service,
            parent=self,
        )
        # Comments are saved immediately, so refresh even if the dialog was cancelled
        if dlg.exec() or dlg.external_changes:
            self.refresh_active_view()

    def _open_new_workspace_dialog(self) -> None:
        dlg = WorkspaceDialog(service=self.service, parent=self)
        if dlg.exec():
            self.sidebar.refresh()

    def _open_edit_workspace_dialog(self, workspace_id: int) -> None:
        dlg = WorkspaceDialog(workspace_id=workspace_id, service=self.service, parent=self)
        if dlg.exec():
            self.sidebar.refresh()

    def _open_new_board_dialog(self, workspace_id: int) -> None:
        dlg = BoardDialog(workspace_id=workspace_id, service=self.service, parent=self)
        if dlg.exec():
            self.sidebar.refresh()

    def _open_edit_board_dialog(self, board_id: int) -> None:
        board = self.service.get_board(board_id)
        if not board:
            return
        dlg = BoardDialog(workspace_id=board.workspace_id, board_id=board_id, service=self.service, parent=self)
        if dlg.exec():
            self.sidebar.refresh()
            self._on_board_selected(board_id)

    def _open_new_column_dialog(self, board_id: int) -> None:
        dlg = ColumnDialog(board_id=board_id, service=self.service, parent=self)
        if dlg.exec():
            self.refresh_active_view()

    def _open_edit_column_dialog(self, column_id: int) -> None:
        if not self.current_board_id:
            return
        dlg = ColumnDialog(board_id=self.current_board_id, column_id=column_id, service=self.service, parent=self)
        if dlg.exec():
            self.refresh_active_view()

    def create_demo_board(self, workspace_id: int = 0) -> Optional[int]:
        """Add the Feature Tour demo board to a workspace and open it."""
        ws_id = workspace_id or self.sidebar.current_workspace_id
        if not ws_id:
            workspaces = self.service.get_workspaces()
            ws_id = workspaces[0].id if workspaces else self.service.create_workspace("Demo Workspace").id
        try:
            board = self.service.create_demo_board(ws_id)
        except Exception as exc:  # disk full, permissions on the attachment store, ...
            QMessageBox.critical(self, "Demo Board", f"Could not create the demo board:\n{exc}")
            return None
        self.sidebar.current_board_id = board.id
        self.sidebar.current_workspace_id = ws_id
        self.sidebar.refresh()
        self.status_bar.showMessage("✨ Demo board created - open any card to explore", 5000)
        return board.id

    def _open_backup_dialog(self) -> None:
        dlg = BackupDialog(parent=self)
        dlg.exec()

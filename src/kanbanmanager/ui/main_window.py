"""
Main Application Window for KanbanManager
"""
from __future__ import annotations

from typing import Optional, Dict, Any

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import QApplication
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
from kanbanmanager.core.time_tracker import TimeTracker
from kanbanmanager.core.backup_manager import BackupManager
from kanbanmanager.core.automation import AutomationEngine, normalize_policies
from kanbanmanager.config import AUTOMATION_SCHEDULE_INTERVAL_MS
from kanbanmanager.timefmt import format_duration
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.styles import get_stylesheet
from kanbanmanager.ui.components.workspace_sidebar import WorkspaceSidebar
from kanbanmanager.ui.components.filter_bar import FilterBar
from kanbanmanager.ui.components.stats_bar import StatsBar
from kanbanmanager.ui.views.board_view import BoardView
from kanbanmanager.ui.views.calendar_view import CalendarView
from kanbanmanager.ui.views.gantt_view import GanttView
from kanbanmanager.core.schedule_engine import ScheduleEngine
from kanbanmanager.ui.views.analytics_view import AnalyticsView
from kanbanmanager.ui.views.workload_view import WorkloadView
from kanbanmanager.ui.components.timer_widget import TimerWidget
from kanbanmanager.ui.dialogs.timesheet_dialog import TimesheetDialog
from kanbanmanager.ui.dialogs.automation_dialog import AutomationDialog
from kanbanmanager.ui.dialogs.quick_capture import QuickCaptureDialog, QuickCaptureSettingsDialog, load_settings
from kanbanmanager.ui.views.focus_view import FocusView
from kanbanmanager.ui.views.split_view import SplitView
from kanbanmanager.ui.global_hotkey import GlobalHotkey
from kanbanmanager.ui.ipc import CommandServer, server_name
from kanbanmanager.core import capture as capture_core
from kanbanmanager.ui.dialogs.archive_dialog import ArchivedTasksDialog
from kanbanmanager.ui.dialogs.custom_fields_dialog import CustomFieldsDialog
from kanbanmanager.ui.dialogs.calendar_sync_dialog import CalendarSyncDialog
from kanbanmanager.ui.dialogs.import_export_dialog import ImportExportDialog
from kanbanmanager.ui.calendar_feed import CalendarFeedServer, SyncRunner, load_feed_settings
from kanbanmanager.ui import theme
from kanbanmanager.ui.dialogs.theme_dialog import ThemeDialog
from kanbanmanager.ui.keyboard import KeyboardNavigator, ShortcutHelpDialog
from kanbanmanager.ui.command_palette import Command, CommandPalette
from functools import partial
from kanbanmanager.ui.dialogs.template_dialogs import (
    NewBoardDialog,
    SaveBoardTemplateDialog,
    TaskTemplateEditor,
    TaskTemplatesDialog,
)
from kanbanmanager.ui.dialogs.task_dialog import TaskDialog
from kanbanmanager.ui.dialogs.board_dialog import BoardDialog
from kanbanmanager.ui.dialogs.column_dialog import ColumnDialog
from kanbanmanager.ui.dialogs.workspace_dialog import WorkspaceDialog
from kanbanmanager.ui.dialogs.backup_dialog import BackupDialog


class MainWindow(QMainWindow):
    """
    Main Application Window orchestrating workspaces, boards,
    views (Board, Calendar, Timeline, Analytics, Workload), the shared
    time tracker, and background engines.
    """

    PAGE_BOARD, PAGE_FOCUS, PAGE_SPLIT = 0, 1, 2

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None,
                 desktop_integration: bool = False):
        """``desktop_integration``: register the system-wide hotkey and the single-instance socket
        (the real app does; tests and embedded uses don't)."""
        super().__init__(parent)
        self.service = service or DatabaseService()
        theme.apply_appearance(self.service)  # before any widget gets its style sheet
        self.current_board_id: Optional[int] = None
        self.desktop_integration = desktop_integration
        self.capture_dialog: Optional[QuickCaptureDialog] = None
        self.global_hotkey = GlobalHotkey(self)
        self.command_server: Optional[CommandServer] = None
        self._capture_shortcut: Optional[QShortcut] = None

        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1280, 800)
        self.setMinimumSize(1000, 600)  # narrowest width at which every toolbar control fits
        self.setStyleSheet(get_stylesheet())

        # One live timer for the whole app (stopwatch / Pomodoro)
        self.time_tracker = TimeTracker(self.service, self)

        # Workflow automation: event rules run as changes happen; time-based rules & policies on a timer
        self.automation = AutomationEngine(self.service, notify=self._on_automation_notice)
        self.automation.attach()
        # Scheduling: cascading date shifts when tasks move (plus CPM / auto-schedule / baselines in the Gantt)
        self.scheduler = ScheduleEngine(self.service, notify=self._on_automation_notice)
        self.scheduler.attach()
        self._automation_timer = QTimer(self)
        self._automation_timer.setInterval(AUTOMATION_SCHEDULE_INTERVAL_MS)
        self._automation_timer.timeout.connect(self.run_automations)
        # Calendar integration: the live feed and background sync of subscriptions (real app only)
        self.feed_server = CalendarFeedServer(self.service, self)
        self.sync_runner = SyncRunner(self.service, self)
        self.sync_runner.finished.connect(self._on_sync_finished)
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(60 * 1000)
        self._sync_timer.timeout.connect(self.run_calendar_sync)

        self._init_ui()
        self._connect_signals()
        self.navigator = KeyboardNavigator(self)   # vim-style keys (see ui.keyboard)

        # Start background reminder engine
        self.reminder_engine = ReminderEngine(self.service, self)
        self.reminder_engine.start(interval_ms=15000)

        # Initial data load
        self.sidebar.refresh()
        if self.sidebar.current_board_id and not self.current_board_id:
            self._on_board_selected(self.sidebar.current_board_id)

        # Resume a timer left running when the app was last closed (or crashed)
        restored = self.time_tracker.restore()
        if restored:
            self.status_bar.showMessage(f"⏱ {restored}", 8000)

        self._automation_timer.start()
        QTimer.singleShot(2000, self.run_automations)  # catch up on anything due while the app was closed

        # Quick capture: in-app shortcut always; system-wide hotkey + socket for the real app
        self.apply_capture_settings()
        if desktop_integration:
            self.command_server = CommandServer(server_name(self.service.db.db_path), self)
            self.command_server.command_received.connect(self.handle_command)
            self.command_server.start()
            feed = load_feed_settings(self.service)
            if feed["enabled"]:
                ok, message = self.feed_server.start(feed)
                if not ok:
                    self.status_bar.showMessage(f"📡 Calendar feed: {message}", 8000)
            self._sync_timer.start()
            QTimer.singleShot(5000, self.run_calendar_sync)
        self.sidebar.set_inbox_count(self.service.count_inbox_items())

    def _init_ui(self) -> None:
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        root_layout = QHBoxLayout(central_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Left Sidebar
        self.sidebar = WorkspaceSidebar(self.service, self)
        root_layout.addWidget(self.sidebar)

        # Right side: board page (filters + stats + views) | My Focus | Split View
        self.pages = QStackedWidget()
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self.pages.addWidget(right_container)                     # PAGE_BOARD
        self.focus_view = FocusView(self.service, self, scheduler=self.scheduler)
        self.pages.addWidget(self.focus_view)                     # PAGE_FOCUS
        self.split_view = SplitView(self.service, self)
        self.pages.addWidget(self.split_view)                     # PAGE_SPLIT

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
        self.timeline_view = GanttView(self.service, self, engine=self.scheduler)
        self.analytics_view = AnalyticsView(self.service, self)
        self.workload_view = WorkloadView(self.service, self)

        self.view_stack.addWidget(self.board_view)      # Index 0
        self.view_stack.addWidget(self.calendar_view)   # Index 1
        self.view_stack.addWidget(self.timeline_view)   # Index 2
        self.view_stack.addWidget(self.analytics_view)  # Index 3
        self.view_stack.addWidget(self.workload_view)   # Index 4

        right_layout.addWidget(self.view_stack, 1)
        root_layout.addWidget(self.pages, 1)

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready", 3000)
        self.timer_widget = TimerWidget(self.time_tracker, self)
        self.status_bar.addPermanentWidget(self.timer_widget)

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+N"), self, self._open_new_task_dialog)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.filter_bar.search_input.setFocus())
        QShortcut(QKeySequence("Ctrl+Shift+E"), self, self.open_import_export)
        QShortcut(QKeySequence("Ctrl+K"), self, self.open_command_palette)
        QShortcut(QKeySequence("Ctrl+P"), self, self.open_command_palette)

    def _connect_signals(self) -> None:
        # Sidebar Actions
        self.sidebar.new_workspace_requested.connect(self._open_new_workspace_dialog)
        self.sidebar.new_board_requested.connect(self._open_new_board_dialog)
        self.sidebar.edit_workspace_requested.connect(self._open_edit_workspace_dialog)
        self.sidebar.edit_board_requested.connect(self._open_edit_board_dialog)
        self.sidebar.backup_manager_requested.connect(self._open_backup_dialog)
        self.sidebar.demo_board_requested.connect(self.create_demo_board)
        self.sidebar.no_board_available.connect(self.clear_board)

        # App Signals
        app_signals.board_selected.connect(self._on_board_selected)
        app_signals.view_mode_changed.connect(self._on_view_mode_changed)
        app_signals.filter_changed.connect(self._on_filter_changed)
        app_signals.task_edit_requested.connect(self._open_edit_task_dialog)
        app_signals.task_moved.connect(self._on_task_moved)
        app_signals.task_dropped.connect(self._on_task_moved)
        app_signals.task_completed_toggled.connect(self._on_task_completed_toggled)
        app_signals.task_deleted.connect(self._on_task_deleted)
        app_signals.backup_restored.connect(self._on_backup_restored)
        app_signals.board_refresh_requested.connect(self.refresh_active_view)
        app_signals.notification_requested.connect(self._show_notification)

        # Board View Specifics
        self.board_view.task_add_requested.connect(self._open_new_task_in_column)
        self.board_view.column_add_requested.connect(self._open_new_column_dialog)
        self.board_view.column_edit_requested.connect(self._open_edit_column_dialog)

        # Time tracking & workload
        app_signals.timer_start_requested.connect(self.start_timer)
        app_signals.timer_stop_requested.connect(self.stop_timer)
        self.time_tracker.state_changed.connect(self._on_timer_state_changed)
        self.time_tracker.entry_logged.connect(self._on_time_logged)
        self.time_tracker.phase_finished.connect(self._on_phase_finished)
        self.timer_widget.open_task_requested.connect(self._open_edit_task_dialog)
        self.timer_widget.stop_requested.connect(self.stop_timer)
        self.workload_view.timesheet_requested.connect(self.open_timesheet_dialog)

        # Cross-workspace pages & quick capture
        self.sidebar.focus_requested.connect(lambda: self.show_focus("focus"))
        self.sidebar.inbox_requested.connect(lambda: self.show_focus("inbox"))
        self.sidebar.split_requested.connect(self.show_split)
        self.focus_view.open_board_requested.connect(self.open_board)
        self.focus_view.capture_requested.connect(self.show_quick_capture)
        self.focus_view.capture_settings_requested.connect(self.open_capture_settings)
        self.focus_view.inbox_changed.connect(self._update_inbox_count)
        self.split_view.open_board_requested.connect(self.open_board)
        self.split_view.task_add_requested.connect(self.open_new_task_on_board)
        self.split_view.column_edit_requested.connect(self._open_column_on_board)
        self.global_hotkey.activated.connect(self.show_quick_capture)

        # Automation & archive
        app_signals.task_archive_requested.connect(self.archive_task)
        app_signals.automations_requested.connect(self.open_automation_dialog)
        app_signals.archived_tasks_requested.connect(self.open_archive_dialog)
        self.stats_bar.automations_requested.connect(lambda: self.open_automation_dialog(self.current_board_id))
        self.stats_bar.archive_requested.connect(lambda: self.open_archive_dialog(self.current_board_id))

        # Custom fields & templates
        app_signals.custom_fields_requested.connect(self.open_custom_fields)
        app_signals.task_templates_requested.connect(self.open_task_templates)
        app_signals.board_template_save_requested.connect(self.save_board_as_template)
        app_signals.task_template_save_requested.connect(self.save_task_as_template)
        app_signals.task_from_template_requested.connect(self.new_task_from_template)
        self.stats_bar.fields_requested.connect(lambda: self.open_custom_fields(self.current_board_id))
        self.stats_bar.templates_requested.connect(lambda: self.open_task_templates(self.current_board_id))

        # Import / export & calendars
        app_signals.appearance_requested.connect(self.open_appearance)
        app_signals.command_palette_requested.connect(self.open_command_palette)
        app_signals.import_export_requested.connect(self.open_import_export)
        app_signals.calendar_sync_requested.connect(self.open_calendar_sync)
        self.analytics_view.message.connect(self._show_status)

    @Slot(int)
    def _on_board_selected(self, board_id: int) -> None:
        self.current_board_id = board_id
        self.pages.setCurrentIndex(self.PAGE_BOARD)
        self.sidebar.set_active_page("board")
        board = self.service.get_board(board_id)
        if board:
            self.setWindowTitle(f"{APP_NAME} - {board.name}")
            labels = [lbl.to_dict() for lbl in board.labels]
            self.filter_bar.set_board_labels(labels)
            self._refresh_board_fields()

        self.refresh_active_view()

    def clear_board(self) -> None:
        """Nothing to show: the open board was deleted and no other board exists."""
        self.current_board_id = None
        self.board_view.clear()
        self.view_stack.setCurrentIndex(0)
        self.filter_bar.set_view_mode(ViewMode.BOARD.value)
        self.filter_bar.set_board_labels([])
        self.filter_bar.set_board_fields([], {})
        self.stats_bar.update_stats([])
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")

    def _refresh_board_fields(self) -> None:
        """Field filter choices for the current board (people in use for person fields)."""
        if not self.current_board_id:
            return
        fields = self.service.get_custom_fields(self.current_board_id)
        in_use = {f["id"]: self.service.field_values_in_use(f["id"]) for f in fields if f["field_type"] == "person"}
        self.filter_bar.set_board_fields(fields, in_use)

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
        elif mode == ViewMode.WORKLOAD.value:
            self.view_stack.setCurrentIndex(4)
        self.filter_bar.set_view_mode(mode)

        self.refresh_active_view()

    @Slot(dict)
    def _on_filter_changed(self, filters: Dict[str, Any]) -> None:
        if self.view_stack.currentIndex() == 0:
            self.board_view.apply_filters(filters)
        else:
            self.refresh_active_view()

    def refresh_active_view(self) -> None:
        self._update_inbox_count()
        page = self.pages.currentIndex()
        if page == self.PAGE_FOCUS:
            self.focus_view.running_task_id = self.time_tracker.task_id
            self.focus_view.refresh()
            return
        if page == self.PAGE_SPLIT:
            self.split_view.running_task_id = self.time_tracker.task_id
            self.split_view.refresh()
            return
        if not self.current_board_id:
            return

        # Update stats
        tasks = self.service.get_tasks_for_board(self.current_board_id)
        self.stats_bar.update_stats([t.to_dict() for t in tasks])
        self._update_automation_counts()

        curr_idx = self.view_stack.currentIndex()
        if curr_idx == 0:
            self.board_view.running_task_id = self.time_tracker.task_id
            self.board_view.load_board(self.current_board_id, tasks=tasks)  # one query for both
        elif curr_idx == 1:
            self.calendar_view.load_board(self.current_board_id)
        elif curr_idx == 2:
            self.timeline_view.load_board(self.current_board_id)
        elif curr_idx == 3:
            self.analytics_view.load_board(self.current_board_id)
        elif curr_idx == 4:
            self.workload_view.load_board(self.current_board_id)

    @Slot(int, int)
    def _on_task_moved(self, task_id: int, target_column_id: int, position: int = 0) -> None:
        success, err = self.service.move_task(task_id, target_column_id, position)
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
        self.time_tracker.sync()  # its running timer (if any) went with it
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
        self._run_task_dialog(TaskDialog(board_id=self.current_board_id, service=self.service, parent=self,
                                         tracker=self.time_tracker))

    def _open_new_task_in_column(self, column_id: int) -> None:
        if not self.current_board_id:
            return
        self._run_task_dialog(TaskDialog(
            board_id=self.current_board_id,
            default_column_id=column_id,
            service=self.service,
            parent=self,
            tracker=self.time_tracker,
        ))

    def _run_task_dialog(self, dlg: TaskDialog) -> bool:
        accepted = bool(dlg.exec())
        fields_changed = dlg.fields_changed
        dlg.deleteLater()
        if fields_changed:
            self._refresh_board_fields()
        if accepted or fields_changed:
            self.refresh_active_view()
        return accepted

    def _open_edit_task_dialog(self, task_id: int) -> None:
        task = self.service.get_task(task_id)
        if task is None:
            return
        # The task may live on another board (workload view, timer widget), so use its own board
        dlg = TaskDialog(
            board_id=task.board_id,
            task_id=task_id,
            service=self.service,
            parent=self,
            tracker=self.time_tracker,
        )
        # Comments and time entries are saved immediately, so refresh even if the dialog was cancelled
        accepted = dlg.exec()
        changed = accepted or dlg.external_changes or dlg.fields_changed
        if dlg.fields_changed:
            self._refresh_board_fields()
        dlg.deleteLater()
        if changed:
            self.time_tracker.sync()
            self.refresh_active_view()

    def _open_new_workspace_dialog(self) -> None:
        dlg = WorkspaceDialog(service=self.service, parent=self)
        if dlg.exec():
            self.sidebar.refresh()

    def _open_edit_workspace_dialog(self, workspace_id: int) -> None:
        dlg = WorkspaceDialog(workspace_id=workspace_id, service=self.service, parent=self)
        if dlg.exec():
            self.sidebar.refresh()

    def _open_new_board_dialog(self, workspace_id: int) -> Optional[int]:
        """New board from a template (the gallery starts on the standard layout)."""
        dlg = NewBoardDialog(self.service, workspace_id, self)
        board_id = dlg.created_board_id if dlg.exec() else None
        dlg.deleteLater()
        if board_id:
            self.sidebar.current_workspace_id = workspace_id
            self.sidebar.refresh()
            self.open_board(board_id)
            self.status_bar.showMessage("📋 Board created", 4000)
        return board_id

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

    # -------------------------------------------------------------------------
    # Time tracking
    # -------------------------------------------------------------------------
    @Slot(int, str)
    def start_timer(self, task_id: int, mode: str = "stopwatch") -> None:
        try:
            if mode == "pomodoro":
                self.time_tracker.start_pomodoro(task_id)
            else:
                self.time_tracker.start_stopwatch(task_id)
        except ValueError as exc:
            QMessageBox.warning(self, "Timer", str(exc))

    @Slot()
    def stop_timer(self) -> None:
        title = self.time_tracker.task_title
        elapsed = self.time_tracker.elapsed_seconds()
        logged = self.time_tracker.stop()
        if logged:
            self.status_bar.showMessage(f"⏱ Logged {format_duration(logged)} on '{title}'", 5000)
        elif elapsed:
            self.status_bar.showMessage(f"⏱ Timer stopped after {format_duration(elapsed)} - too short to log", 5000)

    def _on_timer_state_changed(self) -> None:
        # Only the "tracking" badges depend on this; avoid rebuilding otherwise
        running = self.time_tracker.task_id
        page = self.pages.currentIndex()
        if page == self.PAGE_FOCUS and self.focus_view.running_task_id != running:
            self.refresh_active_view()
        elif page == self.PAGE_SPLIT and self.split_view.running_task_id != running:
            self.refresh_active_view()
        elif (page == self.PAGE_BOARD and self.view_stack.currentIndex() == 0
              and self.board_view.running_task_id != running):
            self.refresh_active_view()

    def _on_time_logged(self, task_id: int, seconds: int) -> None:
        self.refresh_active_view()  # spent hours on cards, workload "logged"

    def _on_phase_finished(self, phase: str, message: str) -> None:
        self.status_bar.showMessage(f"🍅 {message}", 15000)
        QApplication.beep()
        QApplication.alert(self)  # flash the taskbar entry if the window is in the background

    # -------------------------------------------------------------------------
    # Automation & archive
    # -------------------------------------------------------------------------
    def _update_automation_counts(self) -> None:
        if not self.current_board_id:
            return
        rules = self.service.get_automation_rules(self.current_board_id, enabled_only=True)
        policies = normalize_policies(self.service.get_board_policies(self.current_board_id))
        self.stats_bar.set_automation_counts(
            len(rules), sum(1 for p in policies.values() if p["enabled"]),
            self.service.count_archived_tasks(self.current_board_id),
            fields=len(self.service.get_custom_fields(self.current_board_id)),
            templates=len(self.service.get_task_templates(self.current_board_id)))

    def _on_automation_notice(self, message: str) -> None:
        self.status_bar.showMessage(f"⚡ {message}", 10000)

    @Slot()
    def run_automations(self):
        """Evaluate time-based rules and policies (also runs every few minutes)."""
        summary = self.automation.run_scheduled()
        if summary.changed or summary.failures:
            self.time_tracker.sync()
            self.refresh_active_view()
            text = summary.describe()
            if text:
                current = self.status_bar.currentMessage()
                self.status_bar.showMessage(f"⚡ Automation: {text}" + (f"  ·  {current}" if current else ""), 10000)
        return summary

    @Slot(int)
    def archive_task(self, task_id: int) -> None:
        task = self.service.get_task(task_id)
        if task is None:
            return
        if self.service.archive_task(task_id, reason="Archived manually"):
            self.status_bar.showMessage(f"🗄 Archived '{task.title}' - restore it from 🗄 Archived", 6000)
        self.refresh_active_view()

    @Slot(int)
    def open_automation_dialog(self, board_id: Optional[int] = None) -> None:
        board_id = board_id or self.current_board_id
        if not board_id:
            return
        dlg = AutomationDialog(self.service, board_id, engine=self.automation, parent=self)
        dlg.exec()
        dlg.deleteLater()
        self.refresh_active_view()

    @Slot(int)
    def open_archive_dialog(self, board_id: Optional[int] = None) -> None:
        board_id = board_id or self.current_board_id
        if not board_id:
            return
        dlg = ArchivedTasksDialog(self.service, board_id, parent=self)
        dlg.exec()
        changed = dlg.changed
        dlg.deleteLater()
        if changed:
            self.time_tracker.sync()
            self.refresh_active_view()

    # -------------------------------------------------------------------------
    # Custom fields & templates
    # -------------------------------------------------------------------------
    @Slot(int)
    def open_custom_fields(self, board_id: Optional[int] = None) -> bool:
        board_id = board_id or self.current_board_id
        if not board_id:
            return False
        dlg = CustomFieldsDialog(self.service, board_id, self)
        dlg.exec()
        changed = dlg.changed
        dlg.deleteLater()
        if changed:
            self._refresh_board_fields()
            self.refresh_active_view()
        return changed

    @Slot(int)
    def open_task_templates(self, board_id: Optional[int] = None) -> None:
        board_id = board_id or self.current_board_id
        if not board_id:
            return
        dlg = TaskTemplatesDialog(self.service, board_id, self)
        dlg.exec()
        use, changed = dlg.use_requested, dlg.changed
        dlg.deleteLater()
        if changed:
            self._update_automation_counts()
            self.refresh_active_view()
        if use:
            self.new_task_from_template(board_id, 0, use["id"])

    @Slot(int, int, int)
    def new_task_from_template(self, board_id: int, column_id: int, template_id: int) -> bool:
        """Open the task editor pre-filled from a template (nothing is saved until Save)."""
        template = self.service.get_task_template(template_id)
        if template is None:
            return False
        from kanbanmanager.core.templates import normalize_task_template, resolve_column

        column = column_id or resolve_column(self.service, board_id, normalize_task_template(template["data"])["column"])
        return self._run_task_dialog(TaskDialog(board_id=board_id, default_column_id=column, service=self.service,
                                                parent=self, tracker=self.time_tracker, template=template))

    @Slot(int)
    def save_board_as_template(self, board_id: Optional[int] = None) -> Optional[dict]:
        board_id = board_id or self.current_board_id
        if not board_id:
            return None
        dlg = SaveBoardTemplateDialog(self.service, board_id, self)
        saved = dlg.saved if dlg.exec() else None
        dlg.deleteLater()
        if saved:
            self.status_bar.showMessage(f"💾 Saved template '{saved['name']}' - pick it under New Board", 6000)
        return saved

    @Slot(int)
    def save_task_as_template(self, task_id: int) -> Optional[dict]:
        task = self.service.get_task(task_id)
        if task is None:
            return None
        data = self.service.task_template_from_task(task_id)
        dlg = TaskTemplateEditor(self.service, task.board_id, {"name": task.title[:60], "icon": "📋", "data": data,
                                                                "board_id": task.board_id}, self, is_new=True)
        saved = dlg.saved if dlg.exec() else None
        dlg.deleteLater()
        if saved:
            self.status_bar.showMessage(f"📋 Saved task template '{saved['name']}'", 5000)
            self.refresh_active_view()
        return saved

    # -------------------------------------------------------------------------
    # Command palette & keyboard help
    # -------------------------------------------------------------------------
    def palette_commands(self, query: str) -> list:
        """What Ctrl+K offers: boards, views, pages, actions - and tasks once you type."""
        from kanbanmanager.ui.command_palette import rank

        commands = []
        workspaces = self.service.get_workspaces()
        for ws in workspaces:
            for b in ws.boards:
                if not b.is_archived:
                    commands.append(Command(f"Go to {b.name}", partial(self.open_board, b.id), "📋", "Boards",
                                            subtitle=ws.name if len(workspaces) > 1 else "", keywords="board open"))
        for label, mode, key in (("Board", ViewMode.BOARD, "1"), ("Calendar", ViewMode.CALENDAR, "2"),
                                 ("Gantt", ViewMode.TIMELINE, "3"), ("Analytics", ViewMode.ANALYTICS, "4"),
                                 ("Workload", ViewMode.WORKLOAD, "5")):
            commands.append(Command(f"View: {label}", partial(self._on_view_mode_changed, mode.value), "🗔", "Views",
                                    shortcut=key, keywords="view switch show"))
        commands += [
            Command("My Focus", partial(self.show_focus, "focus"), "🎯", "Pages"),
            Command("Inbox", partial(self.show_focus, "inbox"), "📥", "Pages", keywords="triage capture"),
            Command("Split View", self.show_split, "🗂", "Pages", keywords="multi board side"),
            Command("New Task", self._open_new_task_dialog, "➕", "Actions", shortcut="Ctrl+N", keywords="create add"),
            Command("Quick Capture", self.show_quick_capture, "⚡", "Actions", shortcut="Ctrl+Shift+Space",
                    keywords="inbox jot"),
            Command("New Board…", partial(self._open_new_board_dialog, self.sidebar.current_workspace_id or
                                          (workspaces[0].id if workspaces else 0)), "📋", "Actions",
                    keywords="create template"),
            Command("Import / Export…", self.open_import_export, "⇅", "Actions", shortcut="Ctrl+Shift+E",
                    keywords="csv excel pdf markdown trello spreadsheet report picture"),
            Command("Calendar Sync…", self.open_calendar_sync, "📅", "Actions", keywords="ics caldav feed subscribe"),
            Command("Custom Fields…", self.open_custom_fields, "🧩", "Actions", keywords="field story points"),
            Command("Task Templates…", self.open_task_templates, "📋", "Actions", keywords="template preset"),
            Command("Automations…", self.open_automation_dialog, "⚡", "Actions", keywords="rules policies"),
            Command("Archived Tasks…", self.open_archive_dialog, "🗄", "Actions", keywords="restore archive"),
            Command("Timesheets…", self.open_timesheet_dialog, "⏱", "Actions", keywords="time export hours"),
            Command("Appearance…", self.open_appearance, "🎨", "Actions", keywords="theme dark light colors font"),
            Command("Keyboard Shortcuts", self.open_shortcut_help, "⌨", "Actions", shortcut="?",
                    keywords="help keys vim"),
            Command("Backups & Data…", self._open_backup_dialog, "💾", "Actions", keywords="restore snapshot json"),
        ]
        # an empty palette is a browser: show everything; once you type, keep the best few
        ranked = rank(query, commands, limit=9 if query.strip() else len(commands))
        if len(query.strip()) >= 2:
            for t in self.service.search_tasks(query, limit=6):
                subtitle = t["board_name"] + (" · done" if t["is_completed"] else "")
                ranked.append(Command(t["title"], partial(app_signals.task_edit_requested.emit, t["id"]),
                                      "✓" if t["is_completed"] else "○", "Tasks", subtitle=subtitle))
        return ranked

    @Slot()
    def open_command_palette(self, query: str = "") -> CommandPalette:
        palette = CommandPalette(self.palette_commands, self)
        palette.finished.connect(palette.deleteLater)
        palette.open(query)
        palette.move(self.mapToGlobal(self.rect().center()).x() - palette.width() // 2,
                     self.mapToGlobal(self.rect().topLeft()).y() + 120)
        return palette

    def open_shortcut_help(self) -> None:
        dlg = ShortcutHelpDialog(self)
        dlg.exec()
        dlg.deleteLater()

    # -------------------------------------------------------------------------
    # Appearance
    # -------------------------------------------------------------------------
    @Slot()
    def open_appearance(self) -> bool:
        dlg = ThemeDialog(self.service, self)
        accepted = bool(dlg.exec())
        dlg.deleteLater()
        if accepted:
            self.status_bar.showMessage(f"🎨 Theme: {theme.current().label}", 4000)
        return accepted

    # -------------------------------------------------------------------------
    # Import / export & calendar sync
    # -------------------------------------------------------------------------
    def _show_status(self, text: str) -> None:
        self.status_bar.showMessage(text, 6000)

    @Slot(int)
    def open_import_export(self, board_id: int = 0) -> bool:
        board_id = board_id or self.current_board_id
        dlg = ImportExportDialog(self.service, board_id, self, filters=dict(self.board_view.current_filters))
        dlg.calendar_requested.connect(self._calendar_from_import)
        created: list = []
        dlg.board_created.connect(created.append)
        dlg.exec()
        changed = dlg.changed
        dlg.deleteLater()
        if changed:
            self.sidebar.refresh()
            if created:
                self.open_board(created[-1])
            self.refresh_active_view()
        return changed

    def _calendar_from_import(self, tab: str) -> None:
        self.open_calendar_sync(self.current_board_id or 0, tab)

    @Slot(int)
    def open_calendar_sync(self, board_id: int = 0, tab: str = "") -> bool:
        dlg = CalendarSyncDialog(self.service, self.feed_server, self.sync_runner, board_id or self.current_board_id,
                                 self)
        if tab == "files":
            dlg.tabs.setCurrentIndex(2)
        elif tab == "feed":
            dlg.tabs.setCurrentIndex(1)
        dlg.exec()
        changed = dlg.changed
        dlg.deleteLater()
        if changed:
            self.refresh_active_view()
        return changed

    def run_calendar_sync(self) -> bool:
        """Sync the subscriptions that are due (in the background)."""
        if self.sync_runner.busy:
            return False
        return self.sync_runner.run(None)

    def _on_sync_finished(self, reports) -> None:
        changed = [r for r in reports if any(r.counts.values())]
        failed = [r for r in reports if r.status == "failed"]
        if changed:
            self.time_tracker.sync()
            self.refresh_active_view()
        parts = [f"{r.name}: {r.describe()}" for r in changed + failed]
        if parts:
            self.status_bar.showMessage("🔄 Calendar sync - " + "  ·  ".join(parts), 8000)

    # -------------------------------------------------------------------------
    # My Focus / Inbox / Split View
    # -------------------------------------------------------------------------
    def show_focus(self, tab: str = "focus") -> None:
        self.pages.setCurrentIndex(self.PAGE_FOCUS)
        self.sidebar.set_active_page(tab)
        self.focus_view.show_tab(tab)
        self.setWindowTitle(f"{APP_NAME} - {'Inbox' if tab == 'inbox' else 'My Focus'}")
        self.refresh_active_view()

    def show_split(self) -> None:
        self.pages.setCurrentIndex(self.PAGE_SPLIT)
        self.sidebar.set_active_page("split")
        self.setWindowTitle(f"{APP_NAME} - Split View")
        boards = [b.id for ws in self.service.get_workspaces() for b in ws.boards if not b.is_archived]
        defaults = [self.current_board_id] + [b for b in boards if b != self.current_board_id]
        self.split_view.running_task_id = self.time_tracker.task_id
        self.split_view.load(defaults)
        self._update_inbox_count()

    def open_board(self, board_id: int) -> None:
        self.sidebar.current_board_id = board_id
        self.sidebar.select_board(board_id)
        self._on_board_selected(board_id)

    def open_new_task_on_board(self, board_id: int, column_id: int) -> None:
        self._run_task_dialog(TaskDialog(board_id=board_id, default_column_id=column_id, service=self.service,
                                         parent=self, tracker=self.time_tracker))

    def _open_column_on_board(self, board_id: int, column_id: int) -> None:
        dlg = ColumnDialog(board_id=board_id, column_id=column_id, service=self.service, parent=self)
        if dlg.exec():
            self.refresh_active_view()
        dlg.deleteLater()

    def _update_inbox_count(self) -> None:
        self.sidebar.set_inbox_count(self.service.count_inbox_items())

    # -------------------------------------------------------------------------
    # Quick capture
    # -------------------------------------------------------------------------
    def apply_capture_settings(self) -> str:
        """(Re)install the in-app shortcut and, for the real app, the system-wide hotkey."""
        settings = load_settings(self.service)
        if self._capture_shortcut is not None:
            self._capture_shortcut.setParent(None)
            self._capture_shortcut.deleteLater()
        self._capture_shortcut = QShortcut(QKeySequence(settings["hotkey"]), self)
        self._capture_shortcut.setContext(Qt.ApplicationShortcut)
        self._capture_shortcut.activated.connect(self.show_quick_capture)
        self.global_hotkey.stop()
        if self.desktop_integration and settings["global"]:
            ok, _message = self.global_hotkey.start(settings["hotkey"])
            if ok:
                self._capture_shortcut.setEnabled(False)  # the global one also fires in-app
        elif not settings["global"]:
            self.global_hotkey.status = "System-wide shortcut is turned off (works inside the app only)"
        else:
            self.global_hotkey.status = "In-app only (no desktop integration)"
        return self.global_hotkey.status

    def show_quick_capture(self, text: str = "") -> QuickCaptureDialog:
        if self.capture_dialog is None:
            self.capture_dialog = QuickCaptureDialog(self.service, None)  # top-level: usable with the window hidden
            self.capture_dialog.captured.connect(self._on_captured)
        self.capture_dialog.summon(text if isinstance(text, str) else "")
        return self.capture_dialog

    def _on_captured(self, saved: dict) -> None:
        where = f"board '{saved['board_name']}'" if saved["kind"] == "task" else "the Inbox"
        self.status_bar.showMessage(f"⚡ Captured '{saved['title']}' to {where}", 5000)
        self.refresh_active_view()

    def open_capture_settings(self) -> None:
        dlg = QuickCaptureSettingsDialog(self.service, self.global_hotkey.status, self)
        if dlg.exec():
            status = self.apply_capture_settings()
            self.status_bar.showMessage(f"⚡ Quick capture: {status}", 8000)
        dlg.deleteLater()

    def handle_command(self, payload: dict) -> None:
        """Commands from another process (``kanbanmanager --capture`` / a second launch)."""
        if payload.get("cmd") == "show":
            self.showNormal()
            self.raise_()
            self.activateWindow()
            return
        text = (payload.get("text") or "").strip()
        if text:
            try:
                saved = capture_core.commit_capture(self.service, text, source="command_line")
            except ValueError:
                return
            self._on_captured({k: v for k, v in saved.items() if k != "result"})
        else:
            self.show_quick_capture()

    def open_timesheet_dialog(self) -> None:
        dlg = TimesheetDialog(self.service, board_id=self.current_board_id, parent=self)
        dlg.exec()
        dlg.deleteLater()

    def _ask_about_running_timer(self) -> str:
        """Ask what to do with a running timer on exit: 'stop', 'keep' or 'cancel'."""
        box = QMessageBox(self)
        box.setWindowTitle("Timer Running")
        box.setIcon(QMessageBox.Question)
        box.setText(f"A timer is running on '{self.time_tracker.task_title}' ({self.time_tracker.display_time()}).")
        box.setInformativeText("Stop it and log the time, or keep it running until you reopen KanbanManager?")
        stop_btn = box.addButton("Stop && Log", QMessageBox.AcceptRole)
        keep_btn = box.addButton("Keep Running", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(stop_btn)
        box.exec()
        clicked = box.clickedButton()
        return "stop" if clicked == stop_btn else "keep" if clicked == keep_btn else "cancel"

    def closeEvent(self, event) -> None:
        """Don't silently leave a timer running (or lose it) when the app closes."""
        if self.time_tracker.is_tracking_work:
            choice = self._ask_about_running_timer()
            if choice == "cancel":
                event.ignore()
                return
            if choice == "stop":
                self.stop_timer()
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Stop everything that runs in the background - timers, engines, servers, the sync
        thread and the keyboard filter - without asking anything. Safe to call twice."""
        if getattr(self, "_is_shut_down", False):
            return
        self._is_shut_down = True
        self.time_tracker.shutdown()
        self.navigator.detach()
        self.automation.detach()
        self.scheduler.detach()
        self.reminder_engine.stop()
        self._automation_timer.stop()
        self._sync_timer.stop()
        self.feed_server.stop()
        self.sync_runner.wait(5000)
        self.global_hotkey.stop()
        if self.command_server is not None:
            self.command_server.stop()
        if self.capture_dialog is not None:
            self.capture_dialog.close()

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
        dlg = BackupDialog(BackupManager(self.service.db), parent=self)  # this window's database
        dlg.exec()

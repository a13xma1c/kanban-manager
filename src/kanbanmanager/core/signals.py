"""
Global Event Bus and Qt Signals for KanbanManager
"""
from PySide6.QtCore import QObject, Signal


class AppSignals(QObject):
    """Central event bus for coordinating updates across widgets and background tasks."""

    # Workspace Signals
    workspace_selected = Signal(int)  # workspace_id
    workspace_created = Signal(int)   # workspace_id
    workspace_updated = Signal(int)   # workspace_id
    workspace_deleted = Signal(int)   # workspace_id

    # Board Signals
    board_selected = Signal(int)      # board_id
    board_created = Signal(int)       # board_id
    board_updated = Signal(int)       # board_id
    board_deleted = Signal(int)       # board_id
    board_refresh_requested = Signal()

    # Column Signals
    column_created = Signal(int)      # column_id
    column_updated = Signal(int)      # column_id
    column_deleted = Signal(int)      # column_id
    columns_reordered = Signal(int)   # board_id

    # Task Signals
    task_created = Signal(int)        # task_id
    task_updated = Signal(int)        # task_id
    task_deleted = Signal(int)        # task_id
    task_moved = Signal(int, int)     # task_id, target_column_id
    task_completed_toggled = Signal(int, bool)  # task_id, is_completed
    task_edit_requested = Signal(int) # task_id

    # Label & Filter Signals
    labels_changed = Signal(int)      # board_id
    filter_changed = Signal(dict)     # filter criteria dict

    # View Mode Signals
    view_mode_changed = Signal(str)   # 'BOARD', 'CALENDAR', 'TIMELINE', 'ANALYTICS'

    # Reminder & Notification Signals
    reminder_triggered = Signal(dict) # reminder payload dict
    notification_requested = Signal(str, str, str)  # title, message, severity ('info', 'warning', 'error', 'success')

    # Backup & System Signals
    backup_restored = Signal()
    status_message = Signal(str, int)  # message, timeout_ms


# Global singleton signals instance
app_signals = AppSignals()

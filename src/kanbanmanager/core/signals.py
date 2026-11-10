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
    task_dropped = Signal(int, int, int)  # task_id, target_column_id, position (drag & drop, any board)
    task_completed_toggled = Signal(int, bool)  # task_id, is_completed
    task_edit_requested = Signal(int) # task_id

    # Time tracking
    timer_start_requested = Signal(int, str)  # task_id, "stopwatch" | "pomodoro"
    timer_stop_requested = Signal()

    # Workflow automation & archive
    task_archive_requested = Signal(int)       # task_id
    automations_requested = Signal(int)        # board_id
    archived_tasks_requested = Signal(int)     # board_id

    # Custom fields & templates
    custom_fields_requested = Signal(int)          # board_id: open the field manager
    task_templates_requested = Signal(int)         # board_id: open the task template manager
    board_template_save_requested = Signal(int)    # board_id: save the board as a template
    task_template_save_requested = Signal(int)     # task_id: save a task as a template
    task_from_template_requested = Signal(int, int, int)  # board_id, column_id, template_id

    # Import / export & calendar sync
    import_export_requested = Signal(int)          # board_id (0 = none): open the import/export centre
    calendar_sync_requested = Signal(int)          # board_id (0 = none): open calendar sync

    # Appearance & keyboard
    appearance_requested = Signal()                # open the theme customizer
    command_palette_requested = Signal()           # open the command palette
    card_focused = Signal(int)                     # a task card received keyboard focus (task_id)

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

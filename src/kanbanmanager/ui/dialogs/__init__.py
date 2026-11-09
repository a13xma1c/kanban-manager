"""
Dialogs Package for KanbanManager
"""
from kanbanmanager.ui.dialogs.task_dialog import TaskDialog
from kanbanmanager.ui.dialogs.board_dialog import BoardDialog
from kanbanmanager.ui.dialogs.column_dialog import ColumnDialog
from kanbanmanager.ui.dialogs.workspace_dialog import WorkspaceDialog
from kanbanmanager.ui.dialogs.backup_dialog import BackupDialog

__all__ = [
    "TaskDialog",
    "BoardDialog",
    "ColumnDialog",
    "WorkspaceDialog",
    "BackupDialog",
]

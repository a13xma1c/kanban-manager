"""
Core Application Logic Package

Exports are resolved lazily, so importing a pure module such as
``kanbanmanager.core.custom_fields`` (which the database layer uses) does not
pull in the backup manager and, through it, the database service.
"""
from importlib import import_module

_EXPORTS = {
    "app_signals": "kanbanmanager.core.signals",
    "AppSignals": "kanbanmanager.core.signals",
    "RecurrenceEngine": "kanbanmanager.core.recurrence",
    "BackupManager": "kanbanmanager.core.backup_manager",
    "ReminderEngine": "kanbanmanager.core.reminder_engine",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        return getattr(import_module(_EXPORTS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

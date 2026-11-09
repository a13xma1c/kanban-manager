"""
Background Reminder and Due Date Notification Engine
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Set

from PySide6.QtCore import QObject, QTimer, Slot

from kanbanmanager.core.signals import app_signals
from kanbanmanager.database.service import DatabaseService


class ReminderEngine(QObject):
    """Background engine monitoring task due dates and reminders."""

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_reminders_and_due_dates)
        self._notified_overdue_task_ids: Set[int] = set()

    def start(self, interval_ms: int = 30000) -> None:
        """Start periodic reminder polling (default 30 seconds)."""
        self.check_reminders_and_due_dates()
        self.timer.start(interval_ms)

    def stop(self) -> None:
        self.timer.stop()

    @Slot()
    def check_reminders_and_due_dates(self) -> None:
        """Query DB for triggered reminders and overdue tasks."""
        now = datetime.now()

        # 1. Check explicit scheduled reminders
        try:
            pending = self.service.get_pending_reminders(cutoff_time=now)
            for r in pending:
                self.service.mark_reminder_triggered(r["id"])
                app_signals.reminder_triggered.emit(r)
                app_signals.notification_requested.emit(
                    "Task Reminder",
                    f"{r['task_title']}: {r['message']}",
                    "warning",
                )
        except Exception as e:
            # Prevent background timer crash
            pass

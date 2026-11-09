"""
Board Statistics and WIP Limits Summary Bar
"""
from __future__ import annotations

from typing import Optional, Dict, Any, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QFrame,
)


class StatsBar(QFrame):
    """
    Sub-header strip displaying summary statistics for the active board:
    Total tasks, completed rate, active WIP count, overdue count, blocked count.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("StatsBarWidget")
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(16)

        self.total_lbl = QLabel("Total: 0")
        self.total_lbl.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.total_lbl)

        self.wip_lbl = QLabel("In Progress: 0")
        self.wip_lbl.setStyleSheet("color: #38BDF8; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.wip_lbl)

        self.completed_lbl = QLabel("Completed: 0 (0%)")
        self.completed_lbl.setStyleSheet("color: #10B981; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.completed_lbl)

        self.overdue_lbl = QLabel("Overdue: 0")
        self.overdue_lbl.setStyleSheet("color: #EF4444; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.overdue_lbl)

        self.blocked_lbl = QLabel("Blocked: 0")
        self.blocked_lbl.setStyleSheet("color: #F59E0B; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.blocked_lbl)

        layout.addStretch()

    def update_stats(self, tasks: List[Dict[str, Any]]) -> None:
        total = len(tasks)
        completed = sum(1 for t in tasks if t.get("is_completed"))
        overdue = sum(1 for t in tasks if t.get("is_overdue"))
        blocked = sum(1 for t in tasks if t.get("is_blocked"))
        in_progress = total - completed

        pct = int((completed / total) * 100) if total > 0 else 0

        self.total_lbl.setText(f"Total Tasks: {total}")
        self.wip_lbl.setText(f"Active Tasks: {in_progress}")
        self.completed_lbl.setText(f"Completed: {completed} ({pct}%)")
        self.overdue_lbl.setText(f"⚠️ Overdue: {overdue}")
        self.blocked_lbl.setText(f"🔒 Blocked: {blocked}")

"""
Board Analytics, Velocity Metrics, and Workflow Bottleneck Analysis View
"""
from __future__ import annotations

from typing import Optional, Dict, Any, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QProgressBar,
    QFrame,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
)

from kanbanmanager.database.service import DatabaseService


class AnalyticsCard(QFrame):
    """Reusable metric summary card widget."""

    def __init__(self, title: str, value: str, subtitle: str = "", color: str = "#3B82F6", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            f"AnalyticsCard {{ background-color: #1E293B; border: 1px solid #334155; border-radius: 8px; padding: 12px; }} "
            f"AnalyticsCard:hover {{ border-color: {color}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        t_lbl = QLabel(title)
        t_lbl.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 600; text-transform: uppercase;")
        layout.addWidget(t_lbl)

        v_lbl = QLabel(value)
        v_lbl.setStyleSheet(f"color: {color}; font-size: 22px; font-weight: 800;")
        layout.addWidget(v_lbl)

        if subtitle:
            s_lbl = QLabel(subtitle)
            s_lbl.setStyleSheet("color: #64748B; font-size: 11px;")
            layout.addWidget(s_lbl)


class AnalyticsView(QWidget):
    """
    Analytics Dashboard showing task distribution, WIP utilization,
    completion rate, bottleneck alerts, and recent activity logs.
    """

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.current_board_id: Optional[int] = None

        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(16)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setStyleSheet("background: transparent;")

        container = QWidget()
        self.content_layout = QVBoxLayout(container)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(16)

        scroll_area.setWidget(container)
        main_layout.addWidget(scroll_area)

    def load_board(self, board_id: int) -> None:
        self.current_board_id = board_id
        self.refresh()

    def refresh(self) -> None:
        if not self.current_board_id:
            return

        # Clear layout
        while self.content_layout.count() > 0:
            item = self.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        board = self.service.get_board(self.current_board_id)
        if not board:
            return

        tasks = self.service.get_tasks_for_board(self.current_board_id)
        total_tasks = len(tasks)
        completed_tasks = sum(1 for t in tasks if t.is_completed)
        overdue_tasks = sum(1 for t in tasks if t.is_overdue)
        blocked_tasks = sum(1 for t in tasks if t.is_blocked)
        total_est = sum(t.estimated_hours or 0 for t in tasks)

        # ---------------------------------------------------------------------
        # Top KPI Cards Grid
        # ---------------------------------------------------------------------
        kpi_grid = QGridLayout()
        kpi_grid.setSpacing(12)

        pct = int((completed_tasks / total_tasks) * 100) if total_tasks > 0 else 0

        card1 = AnalyticsCard("Completion Rate", f"{pct}%", f"{completed_tasks} of {total_tasks} tasks completed", "#10B981")
        card2 = AnalyticsCard("Active WIP", f"{total_tasks - completed_tasks}", "Tasks currently in workflow", "#3B82F6")
        card3 = AnalyticsCard("Overdue Alerts", f"{overdue_tasks}", "Tasks past due date", "#EF4444" if overdue_tasks > 0 else "#64748B")
        card4 = AnalyticsCard("Blocked Tasks", f"{blocked_tasks}", "Tasks waiting on prerequisites", "#F59E0B" if blocked_tasks > 0 else "#64748B")

        kpi_grid.addWidget(card1, 0, 0)
        kpi_grid.addWidget(card2, 0, 1)
        kpi_grid.addWidget(card3, 0, 2)
        kpi_grid.addWidget(card4, 0, 3)

        self.content_layout.addLayout(kpi_grid)

        # ---------------------------------------------------------------------
        # Column WIP & Distribution
        # ---------------------------------------------------------------------
        col_frame = QFrame()
        col_frame.setStyleSheet("background-color: #1E293B; border: 1px solid #334155; border-radius: 8px; padding: 14px;")
        col_layout = QVBoxLayout(col_frame)
        col_layout.setSpacing(10)

        col_title = QLabel("📊 Column Task Distribution & WIP Limit Utilization")
        col_title.setStyleSheet("font-size: 14px; font-weight: 700; color: #F8FAFC;")
        col_layout.addWidget(col_title)

        for col in board.columns:
            col_tasks_count = len(col.tasks)
            row = QHBoxLayout()
            row.setSpacing(12)

            name_lbl = QLabel(f"{col.name}")
            name_lbl.setFixedWidth(140)
            name_lbl.setStyleSheet("color: #F8FAFC; font-weight: 600;")
            row.addWidget(name_lbl)

            pbar = QProgressBar()
            pbar.setFixedHeight(12)
            pbar.setTextVisible(False)
            pbar.setRange(0, max(1, total_tasks))
            pbar.setValue(col_tasks_count)

            bar_color = col.color or "#3B82F6"
            pbar.setStyleSheet(
                f"QProgressBar {{ background-color: #334155; border-radius: 6px; }} "
                f"QProgressBar::chunk {{ background-color: {bar_color}; border-radius: 6px; }}"
            )
            row.addWidget(pbar, 1)

            count_str = f"{col_tasks_count} tasks"
            if col.wip_limit > 0:
                count_str += f" (WIP limit: {col.wip_limit})"
                if col_tasks_count >= col.wip_limit:
                    count_str += " ⚠️ AT LIMIT"
            
            c_lbl = QLabel(count_str)
            c_lbl.setStyleSheet("color: #94A3B8; font-size: 12px; font-weight: 500;")
            row.addWidget(c_lbl)

            col_layout.addLayout(row)

        self.content_layout.addWidget(col_frame)

        # ---------------------------------------------------------------------
        # Recent Activity Logs
        # ---------------------------------------------------------------------
        act_frame = QFrame()
        act_frame.setStyleSheet("background-color: #1E293B; border: 1px solid #334155; border-radius: 8px; padding: 14px;")
        act_layout = QVBoxLayout(act_frame)
        act_layout.setSpacing(10)

        act_title = QLabel("🕒 Recent Board Activity History")
        act_title.setStyleSheet("font-size: 14px; font-weight: 700; color: #F8FAFC;")
        act_layout.addWidget(act_title)

        logs = self.service.get_board_activities(self.current_board_id, limit=15)
        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["Timestamp", "By", "Action", "Details"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        table.setRowCount(len(logs))
        table.setStyleSheet("QTableWidget { background: #0F172A; border: none; } QHeaderView::section { background: #1E293B; color: #94A3B8; }")

        for r, log in enumerate(logs):
            ts = log.get("timestamp", "")
            if ts:
                ts = ts.replace("T", " ")[:19]
            table.setItem(r, 0, QTableWidgetItem(ts))
            table.setItem(r, 1, QTableWidgetItem(log.get("actor", "")))
            table.setItem(r, 2, QTableWidgetItem(log.get("action_type", "")))
            table.setItem(r, 3, QTableWidgetItem(log.get("details", "")))

        act_layout.addWidget(table)
        self.content_layout.addWidget(act_frame)

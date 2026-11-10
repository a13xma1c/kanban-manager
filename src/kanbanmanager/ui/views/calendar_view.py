"""
Interactive Month & Week Calendar View for Scheduled Tasks and Due Dates
"""
from __future__ import annotations

import calendar
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QFrame,
    QSizePolicy,
    QToolButton,
    QMenu,
    QFileDialog,
)

from kanbanmanager.core.signals import app_signals
from kanbanmanager.database.service import DatabaseService


class CalendarDayCell(QFrame):
    """Represents a single day cell in the monthly calendar grid."""

    task_clicked = Signal(int)
    day_double_clicked = Signal(date)

    def __init__(self, cell_date: date, is_current_month: bool, is_today: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cell_date = cell_date
        self.is_current_month = is_current_month
        self.is_today = is_today

        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(110)

        bg_color = "#182234" if is_current_month else "#0F172A"
        border_color = "#3B82F6" if is_today else "#1E293B"
        self.setStyleSheet(
            f"CalendarDayCell {{ background-color: {bg_color}; border: 1px solid {border_color}; border-radius: 6px; }} "
            f"CalendarDayCell:hover {{ border-color: #475569; }}"
        )

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(6, 6, 6, 6)
        self.layout.setSpacing(4)

        # Day Number Header
        header_row = QHBoxLayout()
        header_row.setSpacing(4)

        num_lbl = QLabel(str(cell_date.day))
        if is_today:
            num_lbl.setStyleSheet(
                "background-color: #3B82F6; color: #FFFFFF; font-weight: bold; "
                "border-radius: 10px; padding: 2px 6px; font-size: 11px;"
            )
        elif is_current_month:
            num_lbl.setStyleSheet("color: #F8FAFC; font-weight: 600; font-size: 11px;")
        else:
            num_lbl.setStyleSheet("color: #475569; font-size: 11px;")

        header_row.addWidget(num_lbl)
        header_row.addStretch()
        self.layout.addLayout(header_row)

        self.tasks_container = QVBoxLayout()
        self.tasks_container.setSpacing(3)
        self.layout.addLayout(self.tasks_container)
        self.layout.addStretch()

    def add_task(self, task: Dict[str, Any]) -> None:
        title = task.get("title", "")
        is_completed = task.get("is_completed", False)
        is_overdue = task.get("is_overdue", False)
        task_id = task.get("id", 0)

        chip = QPushButton(f"{'✓ ' if is_completed else ''}{title}")
        chip.setFixedHeight(22)
        chip.setCursor(Qt.PointingHandCursor)
        chip.setToolTip(title)
        # long titles are clipped instead of widening their day (all days stay the same width)
        chip.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        chip.setMinimumWidth(0)

        if is_completed:
            chip.setStyleSheet(
                "QPushButton { background-color: rgba(16, 185, 129, 34); color: #10B981; border: 1px solid rgba(16, 185, 129, 68); "
                "border-radius: 4px; text-align: left; padding: 1px 4px; font-size: 10px; } "
                "QPushButton:hover { background-color: rgba(16, 185, 129, 68); }"
            )
        elif is_overdue:
            chip.setStyleSheet(
                "QPushButton { background-color: rgba(239, 68, 68, 34); color: #EF4444; border: 1px solid rgba(239, 68, 68, 102); "
                "border-radius: 4px; text-align: left; padding: 1px 4px; font-size: 10px; font-weight: bold; } "
                "QPushButton:hover { background-color: rgba(239, 68, 68, 68); }"
            )
        else:
            chip.setStyleSheet(
                "QPushButton { background-color: rgba(59, 130, 246, 34); color: #38BDF8; border: 1px solid rgba(59, 130, 246, 68); "
                "border-radius: 4px; text-align: left; padding: 1px 4px; font-size: 10px; } "
                "QPushButton:hover { background-color: rgba(59, 130, 246, 68); }"
            )

        chip.clicked.connect(lambda: app_signals.task_edit_requested.emit(task_id))
        self.tasks_container.addWidget(chip)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.day_double_clicked.emit(self.cell_date)
        super().mouseDoubleClickEvent(event)


class CalendarView(QWidget):
    """
    Month Calendar View displaying tasks scheduled by due date.
    Supports navigation across months, status filtering, and task editing.
    """

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.current_board_id: Optional[int] = None
        self.current_date = date.today()

        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(12)

        # ---------------------------------------------------------------------
        # Month Navigation Bar
        # ---------------------------------------------------------------------
        nav_row = QHBoxLayout()

        self.prev_btn = QPushButton("◀ Prev")
        self.prev_btn.clicked.connect(self._prev_month)
        nav_row.addWidget(self.prev_btn)

        self.today_btn = QPushButton("Today")
        self.today_btn.clicked.connect(self._go_today)
        nav_row.addWidget(self.today_btn)

        self.next_btn = QPushButton("Next ▶")
        self.next_btn.clicked.connect(self._next_month)
        nav_row.addWidget(self.next_btn)

        nav_row.addStretch()

        self.month_title = QLabel()
        self.month_title.setStyleSheet("font-size: 18px; font-weight: 800; color: #F8FAFC;")
        nav_row.addWidget(self.month_title)

        nav_row.addStretch()
        main_layout.addLayout(nav_row)

        self.sync_btn = QPushButton("🔄 Calendar Sync…")
        self.sync_btn.setToolTip("Subscribe to calendars, publish a live feed, or sync with CalDAV")
        self.sync_btn.clicked.connect(self._request_sync)
        nav_row.addWidget(self.sync_btn)
        self.export_btn = QToolButton()
        self.export_btn.setText("⬇ Export")
        self.export_btn.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(self.export_btn)
        export_menu.addAction("Calendar file (.ics)…").triggered.connect(self._request_sync)
        export_menu.addSeparator()
        export_menu.addAction("This month as PDF…").triggered.connect(self._export_pdf)
        export_menu.addAction("This month as PNG…").triggered.connect(self._export_png)
        self.export_btn.setMenu(export_menu)
        nav_row.addWidget(self.export_btn)

        # ---------------------------------------------------------------------
        # Weekday Header Row
        # ---------------------------------------------------------------------
        days_header = QHBoxLayout()
        days_header.setSpacing(8)
        weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for name in weekday_names:
            lbl = QLabel(name)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("color: #94A3B8; font-weight: 700; font-size: 12px; padding: 4px;")
            days_header.addWidget(lbl, 1)
        main_layout.addLayout(days_header)

        # ---------------------------------------------------------------------
        # Calendar Grid
        # ---------------------------------------------------------------------
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setSpacing(8)
        for col in range(7):
            self.grid_layout.setColumnStretch(col, 1)

        main_layout.addWidget(self.grid_container, 1)

    def load_board(self, board_id: int) -> None:
        self.current_board_id = board_id
        self.refresh()

    def _request_sync(self, _checked: bool = False) -> None:
        app_signals.calendar_sync_requested.emit(self.current_board_id or 0)

    def export_month(self, fmt: str, path: Optional[str] = None):
        from kanbanmanager.ui.reports import export_widget

        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Export Calendar", f"{self.current_date:%Y-%m} calendar.{fmt}",
                                                  "PDF (*.pdf)" if fmt == "pdf" else "PNG (*.png)")
        if not path:
            return None
        return export_widget(self, path, fmt, title=self.current_date.strftime("%B %Y"))

    def _export_pdf(self, _checked: bool = False) -> None:
        self.export_month("pdf")

    def _export_png(self, _checked: bool = False) -> None:
        self.export_month("png")

    def refresh(self) -> None:
        if not self.current_board_id:
            return

        # Update title
        month_str = self.current_date.strftime("%B %Y")
        self.month_title.setText(month_str)

        # Clear existing grid cells
        while self.grid_layout.count() > 0:
            item = self.grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Fetch board tasks
        tasks = self.service.get_tasks_for_board(self.current_board_id)

        # Group tasks by due date
        tasks_by_date: Dict[date, List[Dict[str, Any]]] = {}
        for t in tasks:
            if t.due_date:
                d = t.due_date.date()
                if d not in tasks_by_date:
                    tasks_by_date[d] = []
                tasks_by_date[d].append(t.to_dict())

        # Generate calendar month matrix
        year = self.current_date.year
        month = self.current_date.month
        cal = calendar.Calendar(firstweekday=0)  # Monday first
        month_days = cal.monthdatescalendar(year, month)

        today = date.today()

        for row_idx, week in enumerate(month_days):
            for col_idx, day in enumerate(week):
                is_curr_month = (day.month == month)
                is_today = (day == today)

                cell = CalendarDayCell(day, is_curr_month, is_today, self.grid_container)

                # Add tasks for this day
                day_tasks = tasks_by_date.get(day, [])
                for t in day_tasks:
                    cell.add_task(t)

                self.grid_layout.addWidget(cell, row_idx, col_idx)

    def _prev_month(self) -> None:
        first = self.current_date.replace(day=1)
        prev_month_last_day = first - timedelta(days=1)
        self.current_date = prev_month_last_day.replace(day=1)
        self.refresh()

    def _next_month(self) -> None:
        first = self.current_date.replace(day=28)
        next_month_day = first + timedelta(days=5)
        self.current_date = next_month_day.replace(day=1)
        self.refresh()

    def _go_today(self) -> None:
        self.current_date = date.today()
        self.refresh()

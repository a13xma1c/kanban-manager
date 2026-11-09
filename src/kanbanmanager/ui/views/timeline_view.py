"""
Interactive Timeline and Gantt Chart View with Dependency Connector Links
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List, Tuple

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QPainterPath
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QScrollArea,
    QLabel,
    QPushButton,
    QFrame,
    QSizePolicy,
)

from kanbanmanager.core.signals import app_signals
from kanbanmanager.database.service import DatabaseService


class TimelineCanvas(QWidget):
    """
    Custom painting canvas for Gantt timeline task bars and dependency arrow lines.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.tasks: List[Dict[str, Any]] = []
        self.dependencies: List[Dict[str, Any]] = []
        self.start_date = date.today() - timedelta(days=7)
        self.end_date = date.today() + timedelta(days=21)
        self.day_width = 36
        self.row_height = 42
        self.header_height = 40
        self.task_bar_rects: Dict[int, QRectF] = {}

        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(
        self,
        tasks: List[Dict[str, Any]],
        dependencies: List[Dict[str, Any]],
    ) -> None:
        self.tasks = tasks
        self.dependencies = dependencies

        # Determine date range based on tasks
        min_date = date.today() - timedelta(days=7)
        max_date = date.today() + timedelta(days=21)

        for t in self.tasks:
            if t.get("start_date"):
                try:
                    s = datetime.fromisoformat(t["start_date"]).date() if isinstance(t["start_date"], str) else t["start_date"]
                    min_date = min(min_date, s)
                except Exception:
                    pass
            if t.get("due_date"):
                try:
                    due_iso = t["due_date"]
                    d = datetime.fromisoformat(due_iso).date() if isinstance(due_iso, str) else due_iso.date()
                    max_date = max(max_date, d)
                except Exception:
                    pass

        self.start_date = min_date - timedelta(days=3)
        self.end_date = max_date + timedelta(days=7)

        total_days = (self.end_date - self.start_date).days + 1
        canvas_width = max(800, total_days * self.day_width + 220)
        canvas_height = max(500, len(self.tasks) * self.row_height + self.header_height + 40)

        self.setFixedSize(canvas_width, canvas_height)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Background
        painter.fillRect(self.rect(), QColor("#0F172A"))

        total_days = (self.end_date - self.start_date).days + 1
        x_offset = 220  # Left sidebar margin for task names
        today = date.today()

        # ---------------------------------------------------------------------
        # Grid Columns & Date Headers
        # ---------------------------------------------------------------------
        font = QFont("sans-serif", 9)
        painter.setFont(font)

        for i in range(total_days):
            d = self.start_date + timedelta(days=i)
            x = x_offset + i * self.day_width

            # Alternating column or today column highlight
            if d == today:
                painter.fillRect(int(x), 0, self.day_width, self.height(), QColor("#1E3A8A44"))
            elif d.weekday() >= 5:  # Weekend
                painter.fillRect(int(x), 0, self.day_width, self.height(), QColor("#1E293B33"))

            # Header cell
            header_rect = QRectF(x, 0, self.day_width, self.header_height)
            painter.setPen(QColor("#334155"))
            painter.drawLine(int(x), 0, int(x), self.height())

            # Day Text
            painter.setPen(QColor("#60A5FA" if d == today else "#94A3B8"))
            day_str = str(d.day)
            day_name = d.strftime("%a")[0]
            painter.drawText(header_rect, Qt.AlignCenter, f"{day_str}\n{day_name}")

        # Header horizontal bottom line
        painter.setPen(QColor("#334155"))
        painter.drawLine(0, self.header_height, self.width(), self.header_height)
        painter.drawLine(x_offset, 0, x_offset, self.height())

        # Today marker vertical line
        if self.start_date <= today <= self.end_date:
            today_idx = (today - self.start_date).days
            today_x = x_offset + today_idx * self.day_width + self.day_width / 2
            painter.setPen(QPen(QColor("#3B82F6"), 2, Qt.DashLine))
            painter.drawLine(int(today_x), 0, int(today_x), self.height())

        # ---------------------------------------------------------------------
        # Task Rows & Bars
        # ---------------------------------------------------------------------
        self.task_bar_rects.clear()

        for idx, task in enumerate(self.tasks):
            y = self.header_height + idx * self.row_height

            # Row background separator
            painter.setPen(QColor("#1E293B"))
            painter.drawLine(0, y + self.row_height, self.width(), y + self.row_height)

            # Left task title
            title_rect = QRectF(10, y + 4, x_offset - 20, self.row_height - 8)
            painter.setPen(QColor("#F8FAFC"))
            title = task.get("title", "")
            if len(title) > 28:
                title = title[:25] + "..."
            painter.drawText(title_rect, Qt.AlignVCenter | Qt.AlignLeft, title)

            # Compute task bar date span
            t_start = today
            if task.get("start_date"):
                try:
                    s_val = task["start_date"]
                    t_start = datetime.fromisoformat(s_val).date() if isinstance(s_val, str) else s_val
                except Exception:
                    pass

            t_end = t_start + timedelta(days=2)
            if task.get("due_date"):
                try:
                    due_iso = task["due_date"]
                    t_end = datetime.fromisoformat(due_iso).date() if isinstance(due_iso, str) else due_iso.date()
                except Exception:
                    pass

            if t_end < t_start:
                t_end = t_start + timedelta(days=1)

            start_idx = max(0, (t_start - self.start_date).days)
            duration_days = max(1, (t_end - t_start).days)

            bar_x = x_offset + start_idx * self.day_width + 2
            bar_w = max(24, duration_days * self.day_width - 4)
            bar_y = y + 8
            bar_h = self.row_height - 16

            bar_rect = QRectF(bar_x, bar_y, bar_w, bar_h)
            self.task_bar_rects[task["id"]] = bar_rect

            # Draw Task Bar
            is_completed = task.get("is_completed", False)
            is_blocked = task.get("is_blocked", False)

            if is_completed:
                bar_color = QColor("#10B981")
            elif is_blocked:
                bar_color = QColor("#EF4444")
            else:
                bar_color = QColor("#3B82F6")

            # Rounded bar
            painter.setBrush(QBrush(bar_color))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(bar_rect, 6, 6)

            # Bar inner text
            painter.setPen(QColor("#FFFFFF"))
            font_bar = QFont("sans-serif", 8, QFont.Bold)
            painter.setFont(font_bar)
            painter.drawText(bar_rect.adjusted(6, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, task.get("title", ""))

        # ---------------------------------------------------------------------
        # Dependency Connector Arrows
        # ---------------------------------------------------------------------
        for dep in self.dependencies:
            prereq_id = dep.get("depends_on_task_id")
            dep_id = dep.get("task_id")

            if prereq_id in self.task_bar_rects and dep_id in self.task_bar_rects:
                r1 = self.task_bar_rects[prereq_id]
                r2 = self.task_bar_rects[dep_id]

                p1 = QPointF(r1.right(), r1.center().y())
                p2 = QPointF(r2.left(), r2.center().y())

                pen = QPen(QColor("#F59E0B"), 2, Qt.SolidLine)
                painter.setPen(pen)

                # Draw cubic bezier or elbow path
                path = QPainterPath()
                path.moveTo(p1)
                mid_x = (p1.x() + p2.x()) / 2
                path.lineTo(mid_x, p1.y())
                path.lineTo(mid_x, p2.y())
                path.lineTo(p2.x(), p2.y())
                painter.drawPath(path)

                # Arrow head at target
                painter.setBrush(QBrush(QColor("#F59E0B")))
                arrow = QPainterPath()
                arrow.moveTo(p2.x(), p2.y())
                arrow.lineTo(p2.x() - 6, p2.y() - 4)
                arrow.lineTo(p2.x() - 6, p2.y() + 4)
                arrow.closeSubpath()
                painter.drawPath(arrow)

        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            pos = event.position()
            for task_id, rect in self.task_bar_rects.items():
                if rect.contains(pos):
                    app_signals.task_edit_requested.emit(task_id)
                    break
        super().mousePressEvent(event)


class TimelineView(QWidget):
    """
    Timeline / Gantt View displaying tasks along a date timeline with dependency links.
    """

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.current_board_id: Optional[int] = None

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        # Header controls
        header_row = QHBoxLayout()
        title_lbl = QLabel("📈 Project Timeline & Dependency Graph")
        title_lbl.setStyleSheet("font-size: 16px; font-weight: 800; color: #F8FAFC;")
        header_row.addWidget(title_lbl)

        header_row.addStretch()

        legend_lbl = QLabel("■ Normal   ■ Completed   ■ Blocked   —► Dependency")
        legend_lbl.setStyleSheet("color: #94A3B8; font-size: 11px;")
        header_row.addWidget(legend_lbl)

        layout.addLayout(header_row)

        # Scroll Area for Gantt Canvas
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setStyleSheet("background-color: #0F172A; border: 1px solid #1E293B; border-radius: 8px;")

        self.canvas = TimelineCanvas()
        self.scroll_area.setWidget(self.canvas)

        layout.addWidget(self.scroll_area, 1)

    def load_board(self, board_id: int) -> None:
        self.current_board_id = board_id
        self.refresh()

    def refresh(self) -> None:
        if not self.current_board_id:
            return

        tasks = self.service.get_tasks_for_board(self.current_board_id)
        task_dicts = [t.to_dict() for t in tasks]

        deps = self.service.get_board_dependencies(self.current_board_id)
        self.canvas.set_data(task_dicts, deps)

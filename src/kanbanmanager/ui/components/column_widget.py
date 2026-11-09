"""
Kanban Column Widget with Drag-and-Drop Drop Target and WIP Limit Enforcement
"""
from __future__ import annotations

from typing import Optional, Dict, Any, List

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QFrame,
    QMenu,
    QSizePolicy,
)

from kanbanmanager.core.signals import app_signals
from kanbanmanager.ui.components.task_card import TaskCardWidget, MIME_TASK_ID


class DropAreaWidget(QWidget):
    """Container widget inside column that handles drag tracking and drop indicator lines."""

    def __init__(self, column_widget: "KanbanColumnWidget", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.column_widget = column_widget
        self.setAcceptDrops(True)
        self.drop_index: Optional[int] = None

        self.cards_layout = QVBoxLayout(self)
        self.cards_layout.setContentsMargins(6, 6, 6, 6)
        self.cards_layout.setSpacing(8)
        self.cards_layout.addStretch()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(MIME_TASK_ID):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if not event.mimeData().hasFormat(MIME_TASK_ID):
            event.ignore()
            return

        event.acceptProposedAction()
        pos_y = event.position().toPoint().y()

        # Calculate target insert index based on children card rects
        new_index = 0
        for i in range(self.cards_layout.count() - 1):  # exclude stretch
            item = self.cards_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                mid_y = w.y() + w.height() / 2
                if pos_y > mid_y:
                    new_index = i + 1
                else:
                    break

        if self.drop_index != new_index:
            self.drop_index = new_index
            self.update()

    def dragLeaveEvent(self, event) -> None:
        self.drop_index = None
        self.update()
        event.accept()

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasFormat(MIME_TASK_ID):
            event.ignore()
            return

        payload = event.mimeData().data(MIME_TASK_ID).data().decode("utf-8")
        try:
            task_id_str, source_col_str = payload.split(":")
            task_id = int(task_id_str)
            target_pos = self.drop_index if self.drop_index is not None else 0
            
            # Request task move
            self.column_widget.request_task_move(task_id, target_pos)
            event.acceptProposedAction()
        except Exception:
            event.ignore()
        finally:
            self.drop_index = None
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.drop_index is not None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            pen = QPen(QColor("#3B82F6"), 3, Qt.SolidLine)
            painter.setPen(pen)

            # Determine Y line for drop indicator
            if self.drop_index == 0:
                y = 4
            elif self.drop_index >= self.cards_layout.count() - 1:
                last_item = self.cards_layout.itemAt(self.cards_layout.count() - 2)
                y = (last_item.widget().y() + last_item.widget().height() + 4) if last_item and last_item.widget() else 4
            else:
                item = self.cards_layout.itemAt(self.drop_index)
                y = (item.widget().y() - 4) if item and item.widget() else 4

            painter.drawLine(10, int(y), self.width() - 10, int(y))
            painter.end()


class KanbanColumnWidget(QFrame):
    """
    Column container representing a stage in the Kanban workflow.
    Displays column header, WIP limits, card count, add task button, and cards list.
    """

    task_add_requested = Signal(int)  # column_id
    column_edit_requested = Signal(int)  # column_id
    column_delete_requested = Signal(int)  # column_id

    def __init__(self, column_data: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.column_data = column_data
        self.column_id = column_data.get("id", 0)
        self.board_id = column_data.get("board_id", 0)

        self.setObjectName("ColumnContainer")
        self.setFixedWidth(310)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ---------------------------------------------------------------------
        # Column Header
        # ---------------------------------------------------------------------
        header_frame = QFrame()
        header_frame.setObjectName("ColumnHeader")
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(12, 10, 10, 10)
        header_layout.setSpacing(8)

        # Accent Color Bar
        color_hex = self.column_data.get("color", "#64748B")
        accent_dot = QLabel("●")
        accent_dot.setStyleSheet(f"color: {color_hex}; font-size: 14px;")
        header_layout.addWidget(accent_dot)

        # Column Name
        col_name = self.column_data.get("name", "Column")
        self.title_lbl = QLabel(col_name)
        self.title_lbl.setStyleSheet("font-weight: 700; font-size: 14px; color: #F8FAFC;")
        header_layout.addWidget(self.title_lbl)

        # WIP / Count Badge
        wip_limit = self.column_data.get("wip_limit", 0)
        task_count = self.column_data.get("task_count", 0)
        
        self.count_badge = QLabel()
        self._update_count_badge(task_count, wip_limit)
        header_layout.addWidget(self.count_badge)

        header_layout.addStretch()

        # Add Task Quick Button
        add_btn = QPushButton("+")
        add_btn.setFixedSize(24, 24)
        add_btn.setToolTip("Add task to this column")
        add_btn.setStyleSheet(
            "QPushButton { background-color: #334155; color: #F8FAFC; border-radius: 4px; font-weight: bold; border: none; } "
            "QPushButton:hover { background-color: #3B82F6; }"
        )
        add_btn.clicked.connect(lambda: self.task_add_requested.emit(self.column_id))
        header_layout.addWidget(add_btn)

        # Column Options Menu Button
        opt_btn = QPushButton("⋮")
        opt_btn.setFixedSize(24, 24)
        opt_btn.setToolTip("Column settings")
        opt_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #94A3B8; border: none; font-size: 16px; border-radius: 4px; } "
            "QPushButton:hover { background-color: #334155; color: #F8FAFC; }"
        )
        opt_btn.clicked.connect(self._show_column_menu)
        header_layout.addWidget(opt_btn)

        main_layout.addWidget(header_frame)

        # ---------------------------------------------------------------------
        # Scrollable Cards Area
        # ---------------------------------------------------------------------
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("background: transparent;")

        self.drop_area = DropAreaWidget(self)
        self.scroll_area.setWidget(self.drop_area)

        main_layout.addWidget(self.scroll_area)

    def _update_count_badge(self, count: int, wip_limit: int) -> None:
        if wip_limit > 0:
            text = f"{count}/{wip_limit}"
            if count >= wip_limit:
                style = (
                    "background-color: #EF444422; color: #EF4444; border: 1px solid #EF444466; "
                    "border-radius: 10px; padding: 2px 8px; font-size: 11px; font-weight: bold;"
                )
            else:
                style = (
                    "background-color: #334155; color: #94A3B8; border-radius: 10px; "
                    "padding: 2px 8px; font-size: 11px; font-weight: bold;"
                )
        else:
            text = str(count)
            style = (
                "background-color: #334155; color: #94A3B8; border-radius: 10px; "
                "padding: 2px 8px; font-size: 11px; font-weight: bold;"
            )

        self.count_badge.setText(text)
        self.count_badge.setStyleSheet(style)

    def set_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        """Populate column with task cards."""
        # Clear existing card widgets
        while self.drop_area.cards_layout.count() > 1:
            item = self.drop_area.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        wip_limit = self.column_data.get("wip_limit", 0)
        self._update_count_badge(len(tasks), wip_limit)

        for t in tasks:
            card = TaskCardWidget(t, self.drop_area)
            self.drop_area.cards_layout.insertWidget(self.drop_area.cards_layout.count() - 1, card)

    def request_task_move(self, task_id: int, target_position: int) -> None:
        """Called when a card is dropped into this column."""
        app_signals.task_moved.emit(task_id, self.column_id)

    def _show_column_menu(self) -> None:
        menu = QMenu(self)
        
        add_act = menu.addAction("➕ Add Task")
        add_act.triggered.connect(lambda: self.task_add_requested.emit(self.column_id))

        edit_act = menu.addAction("✏ Edit Column (WIP & Workflow)")
        edit_act.triggered.connect(lambda: self.column_edit_requested.emit(self.column_id))

        menu.addSeparator()

        del_act = menu.addAction("🗑 Delete Column")
        del_act.triggered.connect(lambda: self.column_delete_requested.emit(self.column_id))

        menu.exec(self.mapToGlobal(self.rect().topRight()))

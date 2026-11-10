"""
Kanban Column Widget with Drag-and-Drop Drop Target and WIP Limit Enforcement
"""
from __future__ import annotations

from datetime import date
from functools import partial
from typing import Optional, Dict, Any, List, Tuple

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

from kanbanmanager.core import custom_fields as cf
from kanbanmanager.core.signals import app_signals
from kanbanmanager.ui.components.task_card import TaskCardWidget, MIME_TASK_ID


_NOT_SHOWN = frozenset({"position", "updated_at"})  # task keys a card doesn't display


def card_day() -> int:
    """Cards word their due dates relative to today, so a new day means new cards."""
    return date.today().toordinal()


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

    def __init__(self, column_data: Dict[str, Any], parent: Optional[QWidget] = None, width: int = 310,
                 fields: Optional[List[Dict[str, Any]]] = None, templates: Optional[List[Dict[str, Any]]] = None):
        """``fields``: the board's custom fields (card chips, header totals);
        ``templates``: task templates offered in the column menu."""
        super().__init__(parent)
        self.column_data = column_data
        self.column_id = column_data.get("id", 0)
        self.board_id = column_data.get("board_id", 0)
        self.task_ids: List[int] = []
        self.fields = fields or []
        self.templates = templates or []
        # Card widgets by task id, with the data they were built from: refreshing the board
        # reuses every card whose task didn't change instead of rebuilding all of them
        self._cards: Dict[int, Tuple[Any, TaskCardWidget]] = {}

        self.setObjectName("ColumnContainer")
        self.setFixedWidth(width)
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
        add_btn.setAccessibleName(f"Add task to {self.column_data.get('name', 'column')}")
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
        opt_btn.setAccessibleName(f"Menu for column {self.column_data.get('name', '')}")
        opt_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #94A3B8; border: none; font-size: 16px; border-radius: 4px; } "
            "QPushButton:hover { background-color: #334155; color: #F8FAFC; }"
        )
        opt_btn.clicked.connect(self._show_column_menu)
        header_layout.addWidget(opt_btn)

        main_layout.addWidget(header_frame)

        # Totals of numeric custom fields ("Σ 13 pts"), shown under the header when any
        self.totals_lbl = QLabel("")
        self.totals_lbl.setObjectName("ColumnTotals")
        self.totals_lbl.setStyleSheet("#ColumnTotals { background: transparent; color: #94A3B8; font-size: 11px;"
                                      " font-weight: 600; padding: 0 12px 6px 14px; }")
        self.totals_lbl.setVisible(False)
        main_layout.addWidget(self.totals_lbl)

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
                    "background-color: rgba(239, 68, 68, 34); color: #EF4444; border: 1px solid rgba(239, 68, 68, 102); "
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

    def set_tasks(self, tasks: List[Dict[str, Any]], keep_hidden: bool = False) -> None:
        """Show these tasks' cards, in this order. Cards of unchanged tasks are reused.
        ``keep_hidden``: cards of tasks not listed are hidden rather than destroyed (the board
        is filtered, so they'll very likely be back)."""
        layout = self.drop_area.cards_layout
        while layout.count() > 1:  # detach the cards; the trailing stretch stays
            layout.takeAt(0)

        day = card_day()
        wanted: List[TaskCardWidget] = []
        for t in tasks:
            # Everything the card shows. Not its position or timestamp: moving one card
            # renumbers its neighbours, which needn't rebuild them.
            signature = (day, {k: v for k, v in t.items() if k not in _NOT_SHOWN})
            entry = self._cards.get(t.get("id"))
            if entry is not None and entry[0] == signature:
                card = entry[1]
                card.task_data = t
            else:
                if entry is not None:
                    self._discard(entry[1])
                card = TaskCardWidget(t, self.drop_area, fields=self.fields)
                self._cards[t.get("id")] = (signature, card)
            wanted.append(card)

        listed = {t.get("id") for t in tasks}
        for task_id, (_sig, card) in list(self._cards.items()):
            if task_id not in listed:
                if keep_hidden:
                    card.hide()
                else:
                    self._discard(card)
                    del self._cards[task_id]

        # Showing many cards one by one relayouts the column for each; hide the container
        # while doing it and the column is laid out once
        reveal = [card for card in wanted if card.isHidden()]
        batch = len(reveal) > 8 and self.drop_area.isVisible()
        if batch:
            self.drop_area.setUpdatesEnabled(False)
            self.drop_area.hide()
        for index, card in enumerate(wanted):
            layout.insertWidget(index, card)
        for card in reveal:
            card.show()
        if batch:
            self.drop_area.show()
            self.drop_area.setUpdatesEnabled(True)

        wip_limit = self.column_data.get("wip_limit", 0)
        self.column_data["task_count"] = len(tasks)
        self._update_count_badge(len(tasks), wip_limit)
        self.task_ids = [t.get("id") for t in tasks]
        self._update_totals(tasks)

    @staticmethod
    def _discard(card: TaskCardWidget) -> None:
        card.hide()
        card.deleteLater()

    def _update_totals(self, tasks: List[Dict[str, Any]]) -> None:
        parts, tips = [], []
        for f in self.fields:
            total = cf.column_total(f, [(t.get("field_values") or {}).get(f["id"]) for t in tasks])
            if total is not None:
                parts.append(total)
                tips.append(f"{f['name']}: {total}")
        self.totals_lbl.setText("Σ " + "  ·  ".join(parts) if parts else "")
        self.totals_lbl.setToolTip("Totals in this column\n" + "\n".join(tips) if tips else "")
        self.totals_lbl.setVisible(bool(parts))

    def cards(self) -> List[TaskCardWidget]:
        lay = self.drop_area.cards_layout
        return [lay.itemAt(i).widget() for i in range(lay.count())
                if isinstance(lay.itemAt(i).widget(), TaskCardWidget)]

    def request_task_move(self, task_id: int, target_position: int) -> None:
        """Called when a card is dropped into this column (from this board or another one)."""
        if task_id in self.task_ids and self.task_ids.index(task_id) < target_position:
            target_position -= 1  # the card itself is counted above its drop position
        app_signals.task_dropped.emit(task_id, self.column_id, target_position)

    def request_from_template(self, template_id: int, _checked: bool = False) -> None:
        app_signals.task_from_template_requested.emit(self.board_id, self.column_id, template_id)

    def _manage_templates(self, _checked: bool = False) -> None:
        app_signals.task_templates_requested.emit(self.board_id)

    def _show_column_menu(self) -> None:
        menu = QMenu(self)
        
        add_act = menu.addAction("➕ Add Task")
        add_act.triggered.connect(lambda: self.task_add_requested.emit(self.column_id))
        tpl_menu = menu.addMenu("📋 New from Template")
        for t in self.templates:
            act = tpl_menu.addAction(f"{t['icon']}  {t['name']}".replace("&", "&&"))
            act.triggered.connect(partial(self.request_from_template, t["id"]))
        if not self.templates:
            tpl_menu.addAction("No templates yet").setEnabled(False)
        tpl_menu.addSeparator()
        tpl_menu.addAction("Manage Templates…").triggered.connect(self._manage_templates)

        edit_act = menu.addAction("✏ Edit Column (WIP & Workflow)")
        edit_act.triggered.connect(lambda: self.column_edit_requested.emit(self.column_id))

        menu.addSeparator()

        del_act = menu.addAction("🗑 Delete Column")
        del_act.triggered.connect(lambda: self.column_delete_requested.emit(self.column_id))

        menu.exec(self.mapToGlobal(self.rect().topRight()))

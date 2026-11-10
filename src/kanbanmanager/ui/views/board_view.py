"""
Kanban Board View with Drag-and-Drop Columns and Workflow Enforcement
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, Dict, Any, List

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QScrollArea,
    QPushButton,
    QFrame,
    QLabel,
    QMessageBox,
)

from kanbanmanager.core import custom_fields as cf
from kanbanmanager.core.signals import app_signals
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.components.column_widget import KanbanColumnWidget
from kanbanmanager.ui.components.task_card import confirm_task_deletion


def filter_tasks(tasks: List[Any], filters: Dict[str, Any], fields: List[Dict[str, Any]],
                 today: Optional[date] = None) -> List[Any]:
    """Apply the filter bar's due-date and custom-field filters (search, priority, labels and
    status are applied by the database query)."""
    today = today or date.today()
    due_filter = filters.get("due_filter", "all")
    if due_filter == "overdue":
        tasks = [t for t in tasks if t.is_overdue]
    elif due_filter == "today":
        tasks = [t for t in tasks if t.due_date is not None and t.due_date.date() == today]
    elif due_filter == "this_week":
        monday = today - timedelta(days=today.weekday())
        tasks = [t for t in tasks if t.due_date is not None and monday <= t.due_date.date() <= monday + timedelta(days=6)]
    elif due_filter == "has_due":
        tasks = [t for t in tasks if t.due_date is not None]
    spec = filters.get("field_filter")
    if spec:
        fid, _, key = str(spec).partition("|")
        definition = next((f for f in fields if str(f["id"]) == fid), None)
        if definition is not None:
            ftype = cf.type_of(definition)
            config = definition.get("config") or {}
            tasks = [t for t in tasks if ftype.matches_filter(t.field_value_map.get(definition["id"]), key, config)]
    return tasks


class BoardView(QWidget):
    """
    Main Kanban Board View containing columns and task cards.
    Handles horizontal layout, column creation, and task filtering.
    """

    task_add_requested = Signal(int)  # column_id
    column_add_requested = Signal(int)  # board_id
    column_edit_requested = Signal(int)  # column_id

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.current_board_id: Optional[int] = None
        self.current_filters: Dict[str, Any] = {}
        self.column_widgets: Dict[int, KanbanColumnWidget] = {}
        self.running_task_id: Optional[int] = None  # task with a live timer (badge on its card)
        self.selected_task_id: Optional[int] = None  # keyboard-navigation selection

        self._init_ui()
        app_signals.card_focused.connect(self._on_card_focused)

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(0)

        # Horizontal Scroll Area for Kanban Columns
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("background: transparent;")

        self.columns_container = QWidget()
        self.columns_layout = QHBoxLayout(self.columns_container)
        self.columns_layout.setContentsMargins(0, 0, 0, 0)
        self.columns_layout.setSpacing(16)
        self.columns_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.scroll_area.setWidget(self.columns_container)
        layout.addWidget(self.scroll_area)

    def load_board(self, board_id: int, tasks: Optional[List[Any]] = None) -> None:
        """Load columns and tasks for the given board ID. ``tasks``: the board's open tasks,
        if the caller already has them (used when no database-side filter is active)."""
        if board_id != self.current_board_id:
            self._clear_columns()
        self.current_board_id = board_id
        self.refresh(tasks)

    def apply_filters(self, filters: Dict[str, Any]) -> None:
        """Apply active search & filter criteria to cards."""
        self.current_filters = filters
        self.refresh()

    def _db_filters(self) -> Dict[str, Any]:
        f = self.current_filters
        return {"search_query": f.get("search") or None, "priority": f.get("priority") or None,
                "label_ids": [f["label_id"]] if f.get("label_id") is not None else None,
                "is_completed": f.get("is_completed")}

    def is_filtered(self) -> bool:
        f = self.current_filters
        return (any(v is not None for v in self._db_filters().values())
                or f.get("due_filter", "all") not in (None, "", "all") or bool(f.get("field_filter")))

    def refresh(self, tasks: Optional[List[Any]] = None) -> None:
        """Reload columns and cards. Column widgets and the cards of unchanged tasks are
        reused, so a refresh costs little more than the database query."""
        if not self.current_board_id:
            return
        columns = self.service.get_columns_for_board(self.current_board_id)
        if not columns and self.service.get_board(self.current_board_id) is None:
            return

        db_filters = self._db_filters()
        if tasks is None or any(v is not None for v in db_filters.values()):
            tasks = self.service.get_tasks_for_board(board_id=self.current_board_id, **db_filters)
        fields = self.service.get_custom_fields(self.current_board_id)
        templates = self.service.get_task_templates(self.current_board_id)
        tasks = filter_tasks(tasks, self.current_filters, fields)

        tasks_by_col: Dict[int, List[Dict[str, Any]]] = {c.id: [] for c in columns}
        for t in tasks:
            if t.column_id in tasks_by_col:
                data = t.to_dict()
                data["timer_running"] = t.id == self.running_task_id
                tasks_by_col[t.column_id].append(data)

        # Detach everything from the row (widgets stay alive), then lay the columns out again
        while self.columns_layout.count() > 0:
            self.columns_layout.takeAt(0)
        keep_hidden = self.is_filtered()
        widgets: Dict[int, KanbanColumnWidget] = {}
        for col in columns:
            col_data = col.to_dict(include_count=False)
            signature = (col_data, fields)
            col_widget = self.column_widgets.pop(col.id, None)
            if col_widget is not None and col_widget.signature != signature:
                col_widget.hide()
                col_widget.deleteLater()
                col_widget = None
            if col_widget is None:
                col_widget = KanbanColumnWidget(dict(col_data), self.columns_container, fields=fields,
                                                templates=templates)
                col_widget.signature = signature
                col_widget.task_add_requested.connect(self.task_add_requested.emit)
                col_widget.column_edit_requested.connect(self.column_edit_requested.emit)
                col_widget.column_delete_requested.connect(self._delete_column)
            col_widget.templates = templates
            col_widget.set_tasks(tasks_by_col.get(col.id, []), keep_hidden=keep_hidden)
            widgets[col.id] = col_widget
            self.columns_layout.addWidget(col_widget)
        self._clear_columns()  # columns that no longer exist
        self.column_widgets = widgets

        self._apply_selection()
        self.columns_layout.addWidget(self._add_column_button())
        self._add_col_btn.show()

    def clear(self) -> None:
        """Show no board at all."""
        self.current_board_id = None
        self.selected_task_id = None
        self._clear_columns()
        if getattr(self, "_add_col_btn", None) is not None:
            self.columns_layout.removeWidget(self._add_col_btn)
            self._add_col_btn.hide()

    def _clear_columns(self) -> None:
        for col_widget in self.column_widgets.values():
            self.columns_layout.removeWidget(col_widget)
            col_widget.hide()
            col_widget.deleteLater()
        self.column_widgets = {}

    def _add_column_button(self) -> QPushButton:
        if getattr(self, "_add_col_btn", None) is None:
            self._add_col_btn = QPushButton("➕ Add Column")
            self._add_col_btn.setFixedSize(140, 42)
            self._add_col_btn.setStyleSheet(
                "QPushButton { background-color: #1E293B; color: #94A3B8; border: 2px dashed #334155; border-radius: 8px; font-weight: 600; } "
                "QPushButton:hover { background-color: #334155; color: #F8FAFC; border-color: #475569; }"
            )
            self._add_col_btn.clicked.connect(lambda: self.column_add_requested.emit(self.current_board_id))
        return self._add_col_btn

    def _delete_column(self, column_id: int) -> None:
        col = self.service.get_columns_for_board(self.current_board_id)
        reply = QMessageBox.question(
            self,
            "Delete Column",
            "Are you sure you want to delete this column? All tasks inside will also be removed.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.service.delete_column(column_id)
            self.refresh()
            app_signals.board_refresh_requested.emit()

    # -------------------------------------------------------------------------
    # Keyboard navigation (vim-style selection; see ui.keyboard for the keys)
    # -------------------------------------------------------------------------
    def _columns(self) -> List[KanbanColumnWidget]:
        return list(self.column_widgets.values())

    def _locate(self, task_id: Optional[int]):
        """(column index, row index, card) of a task, or None."""
        if task_id is None:
            return None
        for c, col in enumerate(self._columns()):
            for r, card in enumerate(col.cards()):
                if card.task_id == task_id:
                    return c, r, card
        return None

    def selected_card(self):
        found = self._locate(self.selected_task_id)
        return found[2] if found else None

    def _apply_selection(self) -> None:
        found = self._locate(self.selected_task_id)
        if found is None:
            self.selected_task_id = None
        for col in self._columns():
            for card in col.cards():
                card.set_selected(found is not None and card.task_id == self.selected_task_id)

    def select_task(self, task_id: Optional[int]) -> bool:
        found = self._locate(task_id)
        self.selected_task_id = task_id if found else None
        self._apply_selection()
        if found is None:
            return False
        c, _r, card = found
        column = self._columns()[c]
        self.scroll_area.ensureWidgetVisible(column, 40, 0)
        column.scroll_area.ensureWidgetVisible(card, 0, 40)
        card.setFocus(Qt.OtherFocusReason)  # keyboard focus follows the selection
        return True

    def clear_selection(self) -> None:
        self.selected_task_id = None
        self._apply_selection()

    def _on_card_focused(self, task_id: int) -> None:
        if self._locate(task_id) is not None and task_id != self.selected_task_id:
            self.select_task(task_id)

    def select_move(self, d_row: int = 0, d_col: int = 0) -> bool:
        """Move the selection (j/k/h/l). Without one, selects the first card."""
        columns = self._columns()
        found = self._locate(self.selected_task_id)
        if found is None:
            for col in columns:
                if col.cards():
                    return self.select_task(col.cards()[0].task_id)
            return False
        c, r, _card = found
        if d_col:
            step = 1 if d_col > 0 else -1
            nc = c + step
            while 0 <= nc < len(columns) and not columns[nc].cards():
                nc += step  # skip empty columns
            if not 0 <= nc < len(columns):
                return False
            cards = columns[nc].cards()
            return self.select_task(cards[min(r, len(cards) - 1)].task_id)
        cards = columns[c].cards()
        nr = max(0, min(len(cards) - 1, r + d_row))
        return self.select_task(cards[nr].task_id)

    def select_edge(self, last: bool) -> bool:
        found = self._locate(self.selected_task_id)
        columns = self._columns()
        cards = columns[found[0]].cards() if found else next((c.cards() for c in columns if c.cards()), [])
        if not cards:
            return False
        return self.select_task(cards[-1 if last else 0].task_id)

    # Actions on the selection ---------------------------------------------------
    def open_selected(self) -> bool:
        if self.selected_task_id is None:
            return False
        app_signals.task_edit_requested.emit(self.selected_task_id)
        return True

    def toggle_selected(self) -> bool:
        card = self.selected_card()
        if card is None:
            return False
        app_signals.task_completed_toggled.emit(card.task_id, not card.task_data.get("is_completed", False))
        return True

    def archive_selected(self) -> bool:
        if self.selected_task_id is None:
            return False
        app_signals.task_archive_requested.emit(self.selected_task_id)
        return True

    def delete_selected(self, confirm: bool = True) -> bool:
        card = self.selected_card()
        if card is None:
            return False
        if confirm and not confirm_task_deletion(self, card.task_data):
            return False
        found = self._locate(card.task_id)
        cards = self._columns()[found[0]].cards()
        neighbour = cards[found[1] + 1].task_id if found[1] + 1 < len(cards) else             (cards[found[1] - 1].task_id if found[1] else None)
        self.selected_task_id = neighbour
        app_signals.task_deleted.emit(card.task_id)
        return True

    def move_selected(self, d_row: int = 0, d_col: int = 0) -> bool:
        """Move the selected *task* (Shift+J/K/H/L)."""
        found = self._locate(self.selected_task_id)
        if found is None:
            return False
        c, r, card = found
        columns = self._columns()
        if d_col:
            nc = c + (1 if d_col > 0 else -1)
            if not 0 <= nc < len(columns):
                return False
            target, position = columns[nc].column_id, min(r, len(columns[nc].cards()))
        else:
            cards = columns[c].cards()
            position = max(0, min(len(cards) - 1, r + d_row))
            if position == r:
                return False
            target = columns[c].column_id
        app_signals.task_dropped.emit(card.task_id, target, position)
        return True

    def bump_priority(self, delta: int) -> bool:
        card = self.selected_card()
        if card is None:
            return False
        from kanbanmanager.enums import Priority

        order = [Priority.LOW.value, Priority.MEDIUM.value, Priority.HIGH.value, Priority.URGENT.value]
        current = card.task_data.get("priority")
        index = order.index(current) if current in order else 1
        new = order[max(0, min(len(order) - 1, index + delta))]
        if new == current:
            return False
        self.service.update_task(card.task_id, priority=new)
        app_signals.board_refresh_requested.emit()
        return True

    def timer_selected(self) -> bool:
        card = self.selected_card()
        if card is None:
            return False
        if card.task_data.get("timer_running"):
            app_signals.timer_stop_requested.emit()
        else:
            app_signals.timer_start_requested.emit(card.task_id, "stopwatch")
        return True

    def add_task_near_selection(self) -> None:
        found = self._locate(self.selected_task_id)
        if found is not None:
            self.task_add_requested.emit(self._columns()[found[0]].column_id)
        elif self.current_board_id:
            column = self.service.default_capture_column(self.current_board_id)
            if column:
                self.task_add_requested.emit(column)

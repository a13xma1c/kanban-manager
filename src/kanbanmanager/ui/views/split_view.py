"""
Multi-Board Split View: up to four boards side by side (or stacked)

Each pane shows one board's columns with compact cards and its own board
picker. Cards can be dragged *between* boards: the move goes through the same
service call as any drop (WIP limits apply; labels are matched on the target
board; comments, attachments, time and history travel with the task).
The layout (which boards, orientation) is remembered.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core.signals import app_signals
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.components.column_widget import KanbanColumnWidget

SETTINGS_KEY = "split_view"
MAX_PANES = 4
PANE_COLUMN_WIDTH = 250


class BoardPane(QFrame):
    close_requested = Signal(object)              # pane
    board_changed = Signal()
    task_add_requested = Signal(int, int)         # board_id, column_id
    column_edit_requested = Signal(int, int)      # board_id, column_id
    open_board_requested = Signal(int)            # board_id

    def __init__(self, service: DatabaseService, board_id: Optional[int], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.running_task_id: Optional[int] = None
        self.column_widgets: Dict[int, KanbanColumnWidget] = {}
        self.setObjectName("BoardPane")
        self.setStyleSheet("#BoardPane { border: 1px solid #1F2937; border-radius: 10px; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        head = QHBoxLayout()
        layout.addLayout(head)
        self.board_combo = QComboBox()
        self.board_combo.setMinimumWidth(200)
        self.board_combo.activated.connect(self._on_board_picked)
        head.addWidget(self.board_combo, 1)
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet("color: #64748B; font-size: 11px;")
        head.addWidget(self.count_lbl)
        self.open_btn = QToolButton()
        self.open_btn.setText("↗")
        self.open_btn.setToolTip("Open this board on its own")
        self.open_btn.clicked.connect(self._open_board)
        head.addWidget(self.open_btn)
        self.close_btn = QToolButton()
        self.close_btn.setText("✕")
        self.close_btn.setToolTip("Close this pane")
        self.close_btn.clicked.connect(self._close)
        head.addWidget(self.close_btn)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet("background: transparent;")  # same as the board view: no label boxes
        layout.addWidget(self.scroll, 1)
        self.columns_host = QWidget()
        self.columns_layout = QHBoxLayout(self.columns_host)
        self.columns_layout.setContentsMargins(0, 0, 0, 0)
        self.columns_layout.setSpacing(10)
        self.columns_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.scroll.setWidget(self.columns_host)

    def _open_board(self, _checked: bool = False) -> None:
        if self.board_id:
            self.open_board_requested.emit(self.board_id)

    def _close(self, _checked: bool = False) -> None:
        self.close_requested.emit(self)

    def _on_board_picked(self, _index: int) -> None:
        self.board_id = self.board_combo.currentData()
        self.refresh()
        self.board_changed.emit()

    def fill_boards(self) -> None:
        self.board_combo.blockSignals(True)
        self.board_combo.clear()
        workspaces = self.service.get_workspaces()
        for ws in workspaces:
            for b in ws.boards:
                if not b.is_archived:
                    suffix = f"  ·  {ws.name}" if len(workspaces) > 1 else ""
                    self.board_combo.addItem(f"📋 {b.name}{suffix}", b.id)
        idx = self.board_combo.findData(self.board_id)
        if idx < 0 and self.board_combo.count():
            idx = 0
            self.board_id = self.board_combo.itemData(0)
        self.board_combo.setCurrentIndex(idx)
        self.board_combo.blockSignals(False)

    def refresh(self) -> None:
        self.fill_boards()
        while self.columns_layout.count():
            item = self.columns_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.column_widgets.clear()
        board = self.service.get_board(self.board_id) if self.board_id else None
        if board is None:
            self.count_lbl.setText("")
            return
        by_col: Dict[int, List[dict]] = {c.id: [] for c in board.columns}
        tasks = self.service.get_tasks_for_board(board.id)
        for t in tasks:
            if t.column_id in by_col:
                data = t.to_dict()
                data["timer_running"] = t.id == self.running_task_id
                by_col[t.column_id].append(data)
        fields = self.service.get_custom_fields(board.id)
        templates = self.service.get_task_templates(board.id)
        for col in board.columns:
            data = col.to_dict()
            data["task_count"] = len(by_col[col.id])
            widget = KanbanColumnWidget(data, self.columns_host, width=PANE_COLUMN_WIDTH, fields=fields,
                                        templates=templates)
            widget.task_add_requested.connect(self._make_add_handler())
            widget.column_edit_requested.connect(self._make_edit_handler())
            widget.column_delete_requested.connect(self._delete_column)
            widget.set_tasks(by_col[col.id])
            self.column_widgets[col.id] = widget
            self.columns_layout.addWidget(widget)
        self.count_lbl.setText(f"{len(tasks)} task{'s' if len(tasks) != 1 else ''}")

    # Handlers report the board this pane currently shows (columns are parented to an attached layout,
    # so they are Qt-owned from the start - see tests/test_qt_ownership.py)
    def _make_add_handler(self):
        def handler(column_id: int) -> None:
            self.task_add_requested.emit(self.board_id, column_id)
        return handler

    def _make_edit_handler(self):
        def handler(column_id: int) -> None:
            self.column_edit_requested.emit(self.board_id, column_id)
        return handler

    def _delete_column(self, column_id: int) -> None:
        if QMessageBox.question(self, "Delete Column",
                                "Delete this column? All tasks inside will also be removed.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.service.delete_column(column_id)
            app_signals.board_refresh_requested.emit()


class SplitView(QWidget):
    task_add_requested = Signal(int, int)
    column_edit_requested = Signal(int, int)
    open_board_requested = Signal(int)

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.panes: List[BoardPane] = []
        self.running_task_id: Optional[int] = None
        self.orientation = Qt.Horizontal

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        top = QHBoxLayout()
        layout.addLayout(top)
        title = QLabel("🗂 Split View")
        title.setStyleSheet("font-size: 16px; font-weight: 800;")
        top.addWidget(title)
        hint = QLabel("Drag cards between boards to move them")
        hint.setStyleSheet("color: #64748B;")
        top.addWidget(hint)
        top.addStretch()
        self.add_btn = QPushButton("➕ Add Board")
        self.add_btn.clicked.connect(self._add_clicked)
        top.addWidget(self.add_btn)
        self.orient_btn = QPushButton("⬍ Stack vertically")
        self.orient_btn.clicked.connect(self.toggle_orientation)
        top.addWidget(self.orient_btn)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter, 1)
        self._loaded = False

    # ------------------------------------------------------------------
    def load(self, default_board_ids: List[int]) -> None:
        """Restore the saved layout (first time), otherwise just refresh."""
        if not self._loaded:
            saved = self.service.get_setting(SETTINGS_KEY, {}) or {}
            boards = [b for b in saved.get("boards", []) if self.service.get_board(b) is not None]
            boards = boards or [b for b in default_board_ids if b][:2]
            if saved.get("orientation") == "vertical":
                self.set_orientation(Qt.Vertical, persist=False)
            for board_id in boards[:MAX_PANES]:
                self.add_pane(board_id, persist=False)
            if not self.panes:
                self.add_pane(None, persist=False)
            self._loaded = True
        self.refresh()

    def refresh(self) -> None:
        for pane in self.panes:
            pane.running_task_id = self.running_task_id
            pane.refresh()
        self.add_btn.setEnabled(len(self.panes) < MAX_PANES)
        for pane in self.panes:
            pane.close_btn.setEnabled(len(self.panes) > 1)

    def board_ids(self) -> List[Optional[int]]:
        return [p.board_id for p in self.panes]

    def _add_clicked(self, _checked: bool = False) -> None:
        shown = set(self.board_ids())
        all_boards = [b.id for ws in self.service.get_workspaces() for b in ws.boards if not b.is_archived]
        nxt = next((b for b in all_boards if b not in shown), all_boards[0] if all_boards else None)
        self.add_pane(nxt)

    def add_pane(self, board_id: Optional[int], persist: bool = True) -> Optional[BoardPane]:
        if len(self.panes) >= MAX_PANES:
            return None
        pane = BoardPane(self.service, board_id)
        self.splitter.addWidget(pane)
        pane.close_requested.connect(self.remove_pane)
        pane.board_changed.connect(self._persist)
        pane.task_add_requested.connect(self.task_add_requested.emit)
        pane.column_edit_requested.connect(self.column_edit_requested.emit)
        pane.open_board_requested.connect(self.open_board_requested.emit)
        self.panes.append(pane)
        pane.running_task_id = self.running_task_id
        pane.refresh()
        sizes = [1000] * len(self.panes)
        self.splitter.setSizes(sizes)
        if persist:
            self._persist()
        self.refresh()
        return pane

    def remove_pane(self, pane: BoardPane) -> None:
        if pane not in self.panes or len(self.panes) <= 1:
            return
        self.panes.remove(pane)
        pane.setParent(None)
        pane.deleteLater()
        self._persist()
        self.refresh()

    def toggle_orientation(self, _checked: bool = False) -> None:
        self.set_orientation(Qt.Vertical if self.orientation == Qt.Horizontal else Qt.Horizontal)

    def set_orientation(self, orientation, persist: bool = True) -> None:
        self.orientation = orientation
        self.splitter.setOrientation(orientation)
        self.orient_btn.setText("⬌ Side by side" if orientation == Qt.Vertical else "⬍ Stack vertically")
        if persist:
            self._persist()

    def _persist(self) -> None:
        self.service.set_setting(SETTINGS_KEY, {
            "boards": [b for b in self.board_ids() if b],
            "orientation": "vertical" if self.orientation == Qt.Vertical else "horizontal",
        })

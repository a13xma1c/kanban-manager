"""
Archived Tasks Dialog: browse, search, restore or permanently delete archived tasks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.database.service import DatabaseService


def _local(iso: Optional[str]) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone().strftime("%b %d, %Y %H:%M")


class ArchivedTasksDialog(QDialog):
    def __init__(self, service: DatabaseService, board_id: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.changed = False
        self.tasks: List[Dict[str, Any]] = []
        board = service.get_board(board_id)
        self.setWindowTitle(f"Archived Tasks - {board.name if board else ''}")
        self.resize(860, 520)

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 Filter archived tasks...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._render)
        top.addWidget(self.search, 1)
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet("color: #94A3B8;")
        top.addWidget(self.count_lbl)
        layout.addLayout(top)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Task", "Column", "Assignee", "Archived", "Reason"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in (1, 2, 3):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(lambda _i: self.restore_selected())
        layout.addWidget(self.table, 1)

        self.empty_lbl = QLabel("Nothing archived on this board. Archive a task from its right-click menu, "
                                "or let an auto-archiving policy do it (⚡ Automations → Policies).")
        self.empty_lbl.setWordWrap(True)
        self.empty_lbl.setStyleSheet("color: #64748B; padding: 10px;")
        layout.addWidget(self.empty_lbl)

        btns = QHBoxLayout()
        layout.addLayout(btns)  # attach first: a parentless layout + self-capturing lambda double-frees in PySide6
        hint = QLabel("Archived tasks keep their history, comments, attachments and time.")
        hint.setStyleSheet("color: #64748B; font-size: 11px;")
        btns.addWidget(hint)
        btns.addStretch()
        self.restore_btn = QPushButton("↩ Restore")
        self.restore_btn.setObjectName("PrimaryButton")
        self.restore_btn.clicked.connect(self.restore_selected)
        self.delete_btn = QPushButton("🗑 Delete Permanently")
        self.delete_btn.setObjectName("DangerButton")
        self.delete_btn.clicked.connect(lambda: self.delete_selected())
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        for b in (self.restore_btn, self.delete_btn, close_btn):
            btns.addWidget(b)
        self.reload()

    def reload(self) -> None:
        self.tasks = self.service.get_archived_tasks(self.board_id)
        self._render()

    def _visible(self) -> List[Dict[str, Any]]:
        needle = self.search.text().strip().lower()
        return [t for t in self.tasks if not needle or needle in t["title"].lower()
                or needle in (t.get("archive_reason") or "").lower()]

    def _render(self) -> None:
        rows = self._visible()
        self.table.setRowCount(len(rows))
        for r, t in enumerate(rows):
            values = [t["title"], t.get("column_name", ""), t.get("assignee") or "-", _local(t.get("archived_at")),
                      t.get("archive_reason") or "Archived manually"]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, t["id"])
                item.setToolTip(v)
                self.table.setItem(r, c, item)
        self.count_lbl.setText(f"{len(rows)} of {len(self.tasks)} archived")
        self.empty_lbl.setVisible(not self.tasks)
        self.table.setVisible(bool(self.tasks))
        self._update_buttons()

    def selected_ids(self) -> List[int]:
        return sorted({item.data(Qt.UserRole) for item in self.table.selectedItems()})

    def _update_buttons(self) -> None:
        has = bool(self.selected_ids())
        self.restore_btn.setEnabled(has)
        self.delete_btn.setEnabled(has)

    def restore_selected(self) -> None:
        ids = self.selected_ids()
        for task_id in ids:
            self.service.restore_task(task_id)
        if ids:
            self.changed = True
            self.reload()

    def delete_selected(self, confirm: bool = True) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        if confirm and QMessageBox.question(
            self, "Delete Permanently",
            f"Permanently delete {len(ids)} archived task{'s' if len(ids) != 1 else ''}? "
            "Their comments, attachments, time entries and history are deleted too. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        for task_id in ids:
            self.service.delete_task(task_id)
        self.changed = True
        self.reload()

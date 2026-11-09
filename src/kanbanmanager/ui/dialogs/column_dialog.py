"""
Custom Column & Workflow Rule Editor Dialog
"""
from __future__ import annotations

from typing import Optional, Dict, Any, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QComboBox,
    QSpinBox,
    QCheckBox,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QWidget,
    QColorDialog,
    QMessageBox,
)

from kanbanmanager.enums import ColumnType
from kanbanmanager.database.service import DatabaseService


class ColumnDialog(QDialog):
    """
    Dialog for configuring column properties, WIP limits,
    done status, and allowed workflow transitions.
    """

    def __init__(
        self,
        board_id: int,
        column_id: Optional[int] = None,
        service: Optional[DatabaseService] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.board_id = board_id
        self.column_id = column_id
        self.service = service or DatabaseService()

        self.setWindowTitle("Edit Column Workflow" if self.column_id else "Add New Column")
        self.setMinimumSize(460, 420)

        self.selected_color = "#3B82F6"

        self._init_ui()
        self._load_data()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        grid = QGridLayout()
        grid.setSpacing(12)

        # Name
        grid.addWidget(QLabel("Column Name *"), 0, 0)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. In Review / QA")
        grid.addWidget(self.name_input, 0, 1)

        # Column Type
        grid.addWidget(QLabel("Column Type:"), 1, 0)
        self.type_combo = QComboBox()
        for ct in ColumnType:
            self.type_combo.addItem(ct.display_name, ct.value)
        grid.addWidget(self.type_combo, 1, 1)

        # WIP Limit
        grid.addWidget(QLabel("WIP Limit (0 = Unlimited):"), 2, 0)
        self.wip_spin = QSpinBox()
        self.wip_spin.setRange(0, 99)
        self.wip_spin.setValue(0)
        grid.addWidget(self.wip_spin, 2, 1)

        # Color Accent Picker
        grid.addWidget(QLabel("Color Accent:"), 3, 0)
        color_row = QHBoxLayout()
        self.color_preview = QLabel("   ")
        self.color_preview.setFixedSize(32, 24)
        self._update_color_preview()
        color_row.addWidget(self.color_preview)

        self.pick_color_btn = QPushButton("Choose Color...")
        self.pick_color_btn.clicked.connect(self._pick_color)
        color_row.addWidget(self.pick_color_btn)
        color_row.addStretch()
        grid.addLayout(color_row, 3, 1)

        layout.addLayout(grid)

        # Is Done Column
        self.is_done_chk = QCheckBox("Mark tasks moved here as completed (Done column)")
        layout.addWidget(self.is_done_chk)

        # Allowed Workflow Transitions
        layout.addWidget(QLabel("Allowed Next Workflow Steps (Leave all unchecked for unrestricted transitions):"))
        self.transitions_list = QListWidget()
        self.transitions_list.setMaximumHeight(120)
        layout.addWidget(self.transitions_list)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save Column")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._save_column)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)

    def _update_color_preview(self) -> None:
        self.color_preview.setStyleSheet(
            f"background-color: {self.selected_color}; border: 1px solid #475569; border-radius: 4px;"
        )

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(self.selected_color, self, "Select Column Accent Color")
        if color.isValid():
            self.selected_color = color.name()
            self._update_color_preview()

    def _load_data(self) -> None:
        # Load other columns on board for transitions list
        all_cols = self.service.get_columns_for_board(self.board_id)
        self.transitions_list.clear()

        for c in all_cols:
            if self.column_id and c.id == self.column_id:
                continue
            item = QListWidgetItem(f"➔ Can move to: {c.name}")
            item.setData(Qt.UserRole, c.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.transitions_list.addItem(item)

        if not self.column_id:
            return

        col = None
        for c in all_cols:
            if c.id == self.column_id:
                col = c
                break

        if not col:
            return

        self.name_input.setText(col.name)
        self.wip_spin.setValue(col.wip_limit)
        self.selected_color = col.color or "#3B82F6"
        self._update_color_preview()
        self.is_done_chk.setChecked(col.is_done_column)

        idx = self.type_combo.findData(col.column_type)
        if idx >= 0:
            self.type_combo.setCurrentIndex(idx)

        # Set allowed transitions
        allowed = set(col.allowed_transitions)
        for i in range(self.transitions_list.count()):
            item = self.transitions_list.item(i)
            if item.data(Qt.UserRole) in allowed:
                item.setCheckState(Qt.Checked)

    def _save_column(self) -> None:
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Column name cannot be empty.")
            return

        wip = self.wip_spin.value()
        col_type = self.type_combo.currentData()
        is_done = self.is_done_chk.isChecked()

        allowed_trans = []
        for i in range(self.transitions_list.count()):
            item = self.transitions_list.item(i)
            if item.checkState() == Qt.Checked:
                allowed_trans.append(item.data(Qt.UserRole))

        if self.column_id:
            self.service.update_column(
                column_id=self.column_id,
                name=name,
                color=self.selected_color,
                column_type=col_type,
                wip_limit=wip,
                is_done_column=is_done,
                allowed_transitions=allowed_trans,
            )
        else:
            self.service.create_column(
                board_id=self.board_id,
                name=name,
                color=self.selected_color,
                column_type=col_type,
                wip_limit=wip,
                is_done_column=is_done,
                allowed_transitions=allowed_trans,
            )

        self.accept()

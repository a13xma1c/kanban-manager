"""
Board Creation and Settings Dialog
"""
from __future__ import annotations

from typing import Optional, Dict, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QTextEdit,
    QPushButton,
    QWidget,
    QColorDialog,
    QMessageBox,
)

from kanbanmanager.database.service import DatabaseService


class BoardDialog(QDialog):
    """Dialog for creating and editing boards."""

    def __init__(
        self,
        workspace_id: int,
        board_id: Optional[int] = None,
        service: Optional[DatabaseService] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.workspace_id = workspace_id
        self.board_id = board_id
        self.service = service or DatabaseService()

        self.setWindowTitle("Edit Board Settings" if self.board_id else "Create New Board")
        self.setMinimumSize(420, 320)

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
        grid.addWidget(QLabel("Board Name *"), 0, 0)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. Sprint 24 / Q4 Goals")
        grid.addWidget(self.name_input, 0, 1)

        # Description
        grid.addWidget(QLabel("Description:"), 1, 0)
        self.desc_input = QTextEdit()
        self.desc_input.setMaximumHeight(80)
        grid.addWidget(self.desc_input, 1, 1)

        # Color
        grid.addWidget(QLabel("Color Theme:"), 2, 0)
        color_row = QHBoxLayout()
        self.color_preview = QLabel("   ")
        self.color_preview.setFixedSize(32, 24)
        self._update_color_preview()
        color_row.addWidget(self.color_preview)

        self.pick_color_btn = QPushButton("Choose Color...")
        self.pick_color_btn.clicked.connect(self._pick_color)
        color_row.addWidget(self.pick_color_btn)
        color_row.addStretch()
        grid.addLayout(color_row, 2, 1)

        layout.addLayout(grid)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save Board")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._save_board)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)

    def _update_color_preview(self) -> None:
        self.color_preview.setStyleSheet(
            f"background-color: {self.selected_color}; border: 1px solid #475569; border-radius: 4px;"
        )

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(self.selected_color, self, "Select Board Theme Color")
        if color.isValid():
            self.selected_color = color.name()
            self._update_color_preview()

    def _load_data(self) -> None:
        if not self.board_id:
            return

        board = self.service.get_board(self.board_id)
        if not board:
            return

        self.name_input.setText(board.name)
        self.desc_input.setPlainText(board.description or "")
        self.selected_color = board.color or "#3B82F6"
        self._update_color_preview()

    def _save_board(self) -> None:
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Board name cannot be empty.")
            return

        desc = self.desc_input.toPlainText()

        if self.board_id:
            self.service.update_board(
                board_id=self.board_id,
                name=name,
                description=desc,
                color=self.selected_color,
            )
        else:
            self.service.create_board(
                workspace_id=self.workspace_id,
                name=name,
                description=desc,
                color=self.selected_color,
            )

        self.accept()

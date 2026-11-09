"""
Local Database Backup, Restore, and JSON Data Exchange Dialog
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QFileDialog,
    QMessageBox,
    QFrame,
    QWidget,
)

from kanbanmanager.core.backup_manager import BackupManager
from kanbanmanager.core.signals import app_signals


class BackupDialog(QDialog):
    """
    Dialog for managing SQLite database snapshots, restores,
    and structured JSON export/import.
    """

    def __init__(self, backup_manager: Optional[BackupManager] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.backup_mgr = backup_manager or BackupManager()

        self.setWindowTitle("Local Storage & Data Backups")
        self.setMinimumSize(600, 480)

        self._init_ui()
        self._refresh_backups()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(16)

        # Header Info
        header_lbl = QLabel("💾 Database Snapshot & Backup Manager")
        header_lbl.setStyleSheet("font-size: 16px; font-weight: 800; color: #F8FAFC;")
        layout.addWidget(header_lbl)

        desc_lbl = QLabel(
            "Create full local SQLite database snapshots, restore from previous backups, "
            "or export/import data in portable JSON format."
        )
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet("color: #94A3B8; font-size: 12px;")
        layout.addWidget(desc_lbl)

        # ---------------------------------------------------------------------
        # SQLite Snapshot Section
        # ---------------------------------------------------------------------
        snap_header = QHBoxLayout()
        snap_title = QLabel("LOCAL BACKUP SNAPSHOTS")
        snap_title.setStyleSheet("font-size: 11px; font-weight: 700; color: #64748B;")
        snap_header.addWidget(snap_title)
        snap_header.addStretch()

        create_snap_btn = QPushButton("➕ Create Instant Backup")
        create_snap_btn.setObjectName("PrimaryButton")
        create_snap_btn.clicked.connect(self._create_snapshot)
        snap_header.addWidget(create_snap_btn)

        layout.addLayout(snap_header)

        # Backups Table
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Filename", "Size", "Created Date", "Action"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(
            "QTableWidget { background-color: #1E293B; border: 1px solid #334155; border-radius: 6px; } "
            "QHeaderView::section { background-color: #0F172A; color: #94A3B8; padding: 6px; }"
        )
        layout.addWidget(self.table, 1)

        # ---------------------------------------------------------------------
        # JSON Export / Import Section
        # ---------------------------------------------------------------------
        json_frame = QFrame()
        json_frame.setStyleSheet("background-color: #182234; border: 1px solid #293548; border-radius: 8px; padding: 12px;")
        json_layout = QHBoxLayout(json_frame)
        json_layout.setSpacing(12)

        json_lbl = QLabel("📁 Portable JSON Data:")
        json_lbl.setStyleSheet("color: #F8FAFC; font-weight: 600;")
        json_layout.addWidget(json_lbl)

        export_json_btn = QPushButton("Export JSON...")
        export_json_btn.clicked.connect(self._export_json)
        json_layout.addWidget(export_json_btn)

        import_json_btn = QPushButton("Import JSON...")
        import_json_btn.clicked.connect(self._import_json)
        json_layout.addWidget(import_json_btn)

        json_layout.addStretch()
        layout.addWidget(json_frame)

        # Close button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _refresh_backups(self) -> None:
        backups = self.backup_mgr.list_backups()
        self.table.setRowCount(len(backups))

        for row, b in enumerate(backups):
            self.table.setItem(row, 0, QTableWidgetItem(b["filename"]))
            self.table.setItem(row, 1, QTableWidgetItem(f"{b['size_kb']} KB"))
            self.table.setItem(row, 2, QTableWidgetItem(b["modified"]))

            restore_btn = QPushButton("Restore")
            restore_btn.setStyleSheet(
                "QPushButton { background-color: #334155; color: #F8FAFC; border-radius: 4px; padding: 2px 8px; } "
                "QPushButton:hover { background-color: #EF4444; }"
            )
            backup_path = b["path"]
            restore_btn.clicked.connect(lambda _, p=backup_path: self._restore_snapshot(p))
            self.table.setCellWidget(row, 3, restore_btn)

    def _create_snapshot(self) -> None:
        try:
            dest = self.backup_mgr.create_database_backup()
            QMessageBox.information(self, "Backup Created", f"Successfully created snapshot at:\n{dest.name}")
            self._refresh_backups()
        except Exception as e:
            QMessageBox.critical(self, "Backup Error", f"Failed to create backup: {e}")

    def _restore_snapshot(self, backup_path: str) -> None:
        reply = QMessageBox.question(
            self,
            "Confirm Database Restore",
            "Restoring this backup will replace current board data. A safety snapshot will be saved first.\n\nProceed with restore?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            try:
                self.backup_mgr.restore_database_backup(backup_path)
                QMessageBox.information(self, "Restore Successful", "Database has been restored successfully.")
                app_signals.backup_restored.emit()
                self.accept()
            except Exception as e:
                QMessageBox.critical(self, "Restore Error", f"Failed to restore database: {e}")

    def _export_json(self) -> None:
        dest, _ = QFileDialog.getSaveFileName(self, "Export Kanban Data to JSON", "kanban_export.json", "JSON Files (*.json)")
        if dest:
            try:
                self.backup_mgr.export_to_json(dest)
                QMessageBox.information(self, "Export Complete", f"Data exported successfully to:\n{dest}")
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Failed to export data: {e}")

    def _import_json(self) -> None:
        src, _ = QFileDialog.getOpenFileName(self, "Import Kanban Data from JSON", "", "JSON Files (*.json)")
        if src:
            try:
                self.backup_mgr.import_from_json(src)
                QMessageBox.information(self, "Import Complete", "Data imported successfully!")
                app_signals.backup_restored.emit()
                self.accept()
            except Exception as e:
                QMessageBox.critical(self, "Import Error", f"Failed to import data: {e}")

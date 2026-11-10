"""
Workspace & Board Navigation Sidebar
"""
from __future__ import annotations

from typing import Optional, List, Dict, Any

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QMessageBox,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QFrame,
    QMenu,
    QHeaderView,
)

from kanbanmanager.core.signals import app_signals
from kanbanmanager.database.service import DatabaseService


class WorkspaceSidebar(QFrame):
    """
    Left-hand sidebar displaying Workspaces and their child Boards.
    Provides creation, switching, editing, and backup management controls.
    """

    new_workspace_requested = Signal()
    new_board_requested = Signal(int)  # workspace_id
    edit_workspace_requested = Signal(int)
    edit_board_requested = Signal(int)
    backup_manager_requested = Signal()
    demo_board_requested = Signal(int)  # workspace_id (0 = current/first)
    focus_requested = Signal()
    inbox_requested = Signal()
    split_requested = Signal()
    no_board_available = Signal()  # the open board is gone and no other board exists

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.setObjectName("SidebarWidget")
        self.setFixedWidth(260)

        self.current_workspace_id: Optional[int] = None
        self.current_board_id: Optional[int] = None

        self._init_ui()
        self.refresh()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(12)

        # ---------------------------------------------------------------------
        # App Title & Logo Header
        # ---------------------------------------------------------------------
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        logo_lbl = QLabel("🗂")
        logo_lbl.setStyleSheet("font-size: 20px;")
        header_layout.addWidget(logo_lbl)

        title_lbl = QLabel("KanbanManager")
        title_lbl.setStyleSheet("font-size: 16px; font-weight: 800; color: #F8FAFC;")
        header_layout.addWidget(title_lbl)

        header_layout.addStretch()
        layout.addLayout(header_layout)

        # ---------------------------------------------------------------------
        # Cross-workspace pages: My Focus, Global Inbox, Split View
        # ---------------------------------------------------------------------
        nav = QVBoxLayout()
        layout.addLayout(nav)
        nav.setSpacing(2)
        nav_style = (
            "QPushButton { text-align: left; background: transparent; color: #CBD5E1; border: none;"
            " border-radius: 6px; padding: 7px 10px; font-weight: 600; }"
            "QPushButton:hover { background-color: #1E293B; }"
            "QPushButton:checked { background-color: #1D4ED8; color: #FFFFFF; }"
        )
        self.focus_btn = QPushButton("🎯  My Focus")
        self.focus_btn.setToolTip("Everything assigned to you, across all workspaces")
        self.inbox_btn = QPushButton("📥  Inbox")
        self.inbox_btn.setToolTip("Quick-captured items waiting to be sorted into boards")
        self.split_btn = QPushButton("🗂  Split View")
        self.split_btn.setToolTip("Several boards side by side; drag cards between them")
        for btn, signal in ((self.focus_btn, self.focus_requested), (self.inbox_btn, self.inbox_requested),
                            (self.split_btn, self.split_requested)):
            btn.setCheckable(True)
            btn.setStyleSheet(nav_style)
            btn.clicked.connect(signal.emit)
            nav.addWidget(btn)

        # ---------------------------------------------------------------------
        # Workspaces Header + Add Button
        # ---------------------------------------------------------------------
        ws_header = QHBoxLayout()
        ws_title = QLabel("WORKSPACES")
        ws_title.setStyleSheet("font-size: 11px; font-weight: 700; color: #64748B; letter-spacing: 0.5px;")
        ws_header.addWidget(ws_title)
        ws_header.addStretch()

        add_ws_btn = QPushButton("+")
        add_ws_btn.setFixedSize(22, 22)
        add_ws_btn.setToolTip("Create new workspace")
        add_ws_btn.setStyleSheet(
            "QPushButton { background-color: #1E293B; color: #94A3B8; border-radius: 4px; font-weight: bold; border: 1px solid #334155; } "
            "QPushButton:hover { background-color: #3B82F6; color: #FFFFFF; border-color: #2563EB; }"
        )
        add_ws_btn.clicked.connect(self.new_workspace_requested.emit)
        ws_header.addWidget(add_ws_btn)

        layout.addLayout(ws_header)

        # ---------------------------------------------------------------------
        # Tree of Workspaces and Boards
        # ---------------------------------------------------------------------
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(16)
        self.tree.setStyleSheet("QTreeWidget { background: transparent; border: none; }")
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        layout.addWidget(self.tree, 1)

        # ---------------------------------------------------------------------
        # Bottom Utility Actions: Backups & Settings
        # ---------------------------------------------------------------------
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #1F2937;")
        layout.addWidget(sep)

        demo_btn = QPushButton("✨ Add Demo Board")
        demo_btn.setToolTip("Add a Feature Tour board showing rich descriptions, attachments, comments and history")
        demo_btn.setStyleSheet(
            "QPushButton { text-align: left; background-color: #1E293B; color: #C4B5FD; padding: 8px 12px; border: 1px solid #334155; border-radius: 6px; font-weight: 600; } "
            "QPushButton:hover { background-color: #334155; }"
        )
        demo_btn.clicked.connect(lambda: self.demo_board_requested.emit(self.current_workspace_id or 0))
        layout.addWidget(demo_btn)

        theme_btn = QPushButton("🎨 Appearance")
        theme_btn.setToolTip("Themes, your own colours and text size")
        theme_btn.setStyleSheet(
            "QPushButton { text-align: left; background-color: #1E293B; color: #E2E8F0; padding: 8px 12px; border: 1px solid #334155; border-radius: 6px; font-weight: 600; } "
            "QPushButton:hover { background-color: #334155; }"
        )
        theme_btn.clicked.connect(app_signals.appearance_requested.emit)
        layout.addWidget(theme_btn)

        io_btn = QPushButton("⇅ Import / Export")
        io_btn.setToolTip("Spreadsheets, PDF reports, board pictures, calendars, Trello (Ctrl+Shift+E)")
        io_btn.setStyleSheet(
            "QPushButton { text-align: left; background-color: #1E293B; color: #E2E8F0; padding: 8px 12px; border: 1px solid #334155; border-radius: 6px; font-weight: 600; } "
            "QPushButton:hover { background-color: #334155; }"
        )
        io_btn.clicked.connect(self._request_import_export)
        layout.addWidget(io_btn)

        backup_btn = QPushButton("💾 Backups && Data")  # "&&" = a literal ampersand (not a mnemonic)
        backup_btn.setStyleSheet(
            "QPushButton { text-align: left; background-color: #1E293B; color: #E2E8F0; padding: 8px 12px; border: 1px solid #334155; border-radius: 6px; font-weight: 600; } "
            "QPushButton:hover { background-color: #334155; }"
        )
        backup_btn.clicked.connect(self.backup_manager_requested.emit)
        layout.addWidget(backup_btn)

    def _request_import_export(self, _checked: bool = False) -> None:
        app_signals.import_export_requested.emit(self.current_board_id or 0)

    def set_inbox_count(self, count: int) -> None:
        self.inbox_btn.setText(f"📥  Inbox  ({count})" if count else "📥  Inbox")

    def set_active_page(self, page: str) -> None:
        """Highlight the active cross-workspace page ('focus', 'inbox', 'split') or none ('board')."""
        for name, btn in (("focus", self.focus_btn), ("inbox", self.inbox_btn), ("split", self.split_btn)):
            btn.setChecked(name == page)
        if page != "board":
            self.tree.clearSelection()

    def refresh(self) -> None:
        """Reload workspaces and boards into the tree widget."""
        self.tree.clear()
        workspaces = self.service.get_workspaces()

        selected_item = None

        for ws in workspaces:
            ws_item = QTreeWidgetItem(self.tree)
            ws_item.setText(0, f"📁 {ws.name}")
            ws_item.setData(0, Qt.UserRole, {"type": "workspace", "id": ws.id})
            ws_item.setExpanded(True)

            for b in ws.boards:
                b_item = QTreeWidgetItem(ws_item)
                b_item.setText(0, f"📋 {b.name}")
                b_item.setData(0, Qt.UserRole, {"type": "board", "id": b.id, "workspace_id": ws.id})

                if self.current_board_id == b.id:
                    selected_item = b_item

        if selected_item:
            self.tree.setCurrentItem(selected_item)
            if self.current_board_id:
                app_signals.board_selected.emit(self.current_board_id)
            return
        # The remembered board is gone (deleted, restored away) or none was open yet: open the
        # first board of any workspace - or tell the window there is nothing left to show
        for i in range(self.tree.topLevelItemCount()):
            ws_item = self.tree.topLevelItem(i)
            if ws_item.childCount() > 0:
                first_board = ws_item.child(0)
                self.tree.setCurrentItem(first_board)
                data = first_board.data(0, Qt.UserRole)
                self.current_board_id = data["id"]
                self.current_workspace_id = data["workspace_id"]
                app_signals.board_selected.emit(self.current_board_id)
                return
        had_board = self.current_board_id is not None
        self.current_board_id = None
        if had_board:
            self.no_board_available.emit()

    def select_board(self, board_id: int) -> None:
        """Programmatically select a board item in the tree."""
        self.current_board_id = board_id
        for i in range(self.tree.topLevelItemCount()):
            ws_item = self.tree.topLevelItem(i)
            for j in range(ws_item.childCount()):
                b_item = ws_item.child(j)
                data = b_item.data(0, Qt.UserRole)
                if data and data.get("id") == board_id:
                    self.tree.setCurrentItem(b_item)
                    return

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        data = item.data(0, Qt.UserRole)
        if not data:
            return

        if data["type"] == "board":
            self.current_board_id = data["id"]
            self.current_workspace_id = data.get("workspace_id")
            app_signals.board_selected.emit(self.current_board_id)
        elif data["type"] == "workspace":
            self.current_workspace_id = data["id"]
            app_signals.workspace_selected.emit(self.current_workspace_id)

    def _on_tree_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if not item:
            return

        data = item.data(0, Qt.UserRole)
        if not data:
            return

        menu = QMenu(self)
        if data["type"] == "workspace":
            ws_id = data["id"]
            add_b_act = menu.addAction("➕ New Board in Workspace…")
            add_b_act.triggered.connect(lambda: self.new_board_requested.emit(ws_id))

            demo_act = menu.addAction("✨ Add Demo Board")
            demo_act.triggered.connect(lambda: self.demo_board_requested.emit(ws_id))

            edit_ws_act = menu.addAction("✏ Edit Workspace")
            edit_ws_act.triggered.connect(lambda: self.edit_workspace_requested.emit(ws_id))

            menu.addSeparator()

            del_ws_act = menu.addAction("🗑 Delete Workspace")
            del_ws_act.triggered.connect(lambda: self._delete_workspace(ws_id))

        elif data["type"] == "board":
            b_id = data["id"]
            edit_b_act = menu.addAction("✏ Edit Board Settings")
            edit_b_act.triggered.connect(lambda: self.edit_board_requested.emit(b_id))
            menu.addAction("🧩 Custom Fields...", lambda: app_signals.custom_fields_requested.emit(b_id))
            menu.addAction("📋 Task Templates...", lambda: app_signals.task_templates_requested.emit(b_id))
            menu.addAction("⚡ Automations...", lambda: app_signals.automations_requested.emit(b_id))
            menu.addAction("🗄 Archived Tasks...", lambda: app_signals.archived_tasks_requested.emit(b_id))
            menu.addAction("💾 Save as Board Template...",
                           lambda: app_signals.board_template_save_requested.emit(b_id))
            menu.addSeparator()
            menu.addAction("⇅ Import / Export...", lambda: app_signals.import_export_requested.emit(b_id))
            menu.addAction("📅 Calendar Sync...", lambda: app_signals.calendar_sync_requested.emit(b_id))

            menu.addSeparator()

            del_b_act = menu.addAction("🗑 Delete Board")
            del_b_act.triggered.connect(lambda: self._delete_board(b_id))

        menu.exec(self.tree.mapToGlobal(pos))

    def _confirm_delete(self, title: str, what: str) -> bool:
        return QMessageBox.question(
            self, title, f"{what}\n\nThis can't be undone (except by restoring a backup).",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    @staticmethod
    def _count(n: int, noun: str) -> str:
        return f"{n} {noun}{'' if n == 1 else 's'}"

    def _delete_workspace(self, workspace_id: int) -> bool:
        ws = self.service.get_workspace(workspace_id)
        if ws is None:
            return False
        board_ids = [b.id for b in ws.boards]
        tasks = self.service.count_tasks(board_ids)
        if not self._confirm_delete("Delete Workspace", f"Delete the workspace '{ws.name}' with its "
                                    f"{self._count(len(board_ids), 'board')} and {self._count(tasks, 'task')}?"):
            return False
        self.service.delete_workspace(workspace_id)
        self.refresh()
        return True

    def _delete_board(self, board_id: int) -> bool:
        board = self.service.get_board(board_id)
        if board is None:
            return False
        tasks = self.service.count_tasks([board_id])
        if not self._confirm_delete("Delete Board",
                                    f"Delete the board '{board.name}' and its {self._count(tasks, 'task')}?"):
            return False
        self.service.delete_board(board_id)
        self.refresh()
        return True

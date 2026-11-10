"""
The everyday management UI: workspace, board and column dialogs, the sidebar's
context actions (with confirmation before anything is deleted), what the window
shows when the open board disappears, and the Backups & Data dialog.
"""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog, QFileDialog, QMessageBox

from kanbanmanager.core.backup_manager import BackupManager
from kanbanmanager.core.signals import app_signals
from kanbanmanager.ui import MainWindow
from kanbanmanager.ui.dialogs.backup_dialog import BackupDialog
from kanbanmanager.ui.dialogs.board_dialog import BoardDialog
from kanbanmanager.ui.dialogs.column_dialog import ColumnDialog
from kanbanmanager.ui.dialogs.workspace_dialog import WorkspaceDialog
from support import flush_qt, make_board


@pytest.fixture
def service(empty_service):
    return empty_service


@pytest.fixture
def warnings(monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2] if len(a) > 2 else "") or
                        QMessageBox.Ok)
    return shown


@pytest.fixture
def answer(monkeypatch):
    """Answer every QMessageBox.question with ``answer.value`` (default Yes); records the texts."""
    class Answer:
        value = QMessageBox.Yes
        asked: list = []

    a = Answer()
    a.asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **k: a.asked.append(args[2]) or a.value)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args, **k: pytest.fail(f"error dialog: {args[2]}"))
    return a


# ---------------------------------------------------------------------------
# Workspace / board / column dialogs
# ---------------------------------------------------------------------------
def test_workspace_dialog_creates_edits_and_validates(qapp, service, warnings, monkeypatch):
    dlg = WorkspaceDialog(service=service)
    assert dlg.windowTitle() == "Create New Workspace"
    dlg._save_workspace()
    assert warnings and not service.get_workspaces(), "a name is required"
    dlg.name_input.setText("  Engineering  ")
    dlg.desc_input.setPlainText("Platform team")
    monkeypatch.setattr(QColorDialog, "getColor", lambda *a, **k: QColor("#10b981"))
    dlg._pick_color()
    dlg._save_workspace()
    assert dlg.result() == WorkspaceDialog.Accepted
    ws = service.get_workspaces()[0]
    assert (ws.name, ws.description, ws.color.upper()) == ("Engineering", "Platform team", "#10B981")

    edit = WorkspaceDialog(workspace_id=ws.id, service=service)
    assert edit.windowTitle() == "Edit Workspace"
    assert edit.name_input.text() == "Engineering" and edit.selected_color.upper() == "#10B981"
    monkeypatch.setattr(QColorDialog, "getColor", lambda *a, **k: QColor())  # cancelled picker
    edit._pick_color()
    assert edit.selected_color.upper() == "#10B981", "cancelling the picker keeps the colour"
    edit.name_input.setText("Eng")
    edit._save_workspace()
    assert service.get_workspace(ws.id).name == "Eng"


def test_board_dialog_creates_edits_and_validates(qapp, service, warnings):
    ws = service.create_workspace("W")
    dlg = BoardDialog(ws.id, service=service)
    dlg._save_board()
    assert warnings
    dlg.name_input.setText("Roadmap")
    dlg.desc_input.setPlainText("Q3 goals")
    dlg._save_board()
    board = service.get_workspaces()[0].boards[0]
    assert (board.name, board.description) == ("Roadmap", "Q3 goals")
    assert [c.name for c in service.get_columns_for_board(board.id)][:2] == ["Backlog", "To Do"]

    edit = BoardDialog(ws.id, board.id, service=service)
    assert edit.windowTitle() == "Edit Board Settings" and edit.desc_input.toPlainText() == "Q3 goals"
    edit.name_input.setText("Roadmap 2")
    edit._save_board()
    assert service.get_board(board.id).name == "Roadmap 2"
    assert BoardDialog(ws.id, 9999, service=service).name_input.text() == "", "a missing board loads nothing"


def test_column_dialog_round_trips_wip_done_type_and_transitions(qapp, service, warnings):
    built = make_board(service, "Flow")
    backlog, todo, doing, review, done = built.columns
    dlg = ColumnDialog(built.id, doing, service=service)
    assert dlg.windowTitle() == "Edit Column Workflow"
    assert dlg.name_input.text() == "In Progress" and dlg.wip_spin.value() == 3
    ids = [dlg.transitions_list.item(i).data(Qt.UserRole) for i in range(dlg.transitions_list.count())]
    assert doing not in ids and set(ids) == {backlog, todo, review, done}, "a column can't transition to itself"
    # only allow In Progress -> In Review
    for i in range(dlg.transitions_list.count()):
        item = dlg.transitions_list.item(i)
        item.setCheckState(Qt.Checked if item.data(Qt.UserRole) == review else Qt.Unchecked)
    dlg.wip_spin.setValue(1)
    dlg._save_column()
    col = next(c for c in service.get_columns_for_board(built.id) if c.id == doing)
    assert col.allowed_transitions == [review] and col.wip_limit == 1

    again = ColumnDialog(built.id, doing, service=service)
    checked = [again.transitions_list.item(i).data(Qt.UserRole) for i in range(again.transitions_list.count())
               if again.transitions_list.item(i).checkState() == Qt.Checked]
    assert checked == [review], "saved transitions are shown checked"

    # the rules are enforced
    task = service.create_task(built.id, doing, "Work")
    ok, err = service.move_task(task.id, done, 0)
    assert not ok and "not allowed" in err
    assert service.move_task(task.id, review, 0)[0]
    other = service.create_task(built.id, todo, "More")
    service.move_task(other.id, doing, 0)
    third = service.create_task(built.id, todo, "Even more")
    ok, err = service.move_task(third.id, doing, 0)
    assert not ok and "WIP" in err

    new = ColumnDialog(built.id, service=service)
    assert new.windowTitle() == "Add New Column"
    new._save_column()
    assert warnings, "a name is required"
    new.name_input.setText("Shipped")
    new.is_done_chk.setChecked(True)
    new.type_combo.setCurrentIndex(new.type_combo.findData("DONE"))
    new._save_column()
    shipped = service.get_columns_for_board(built.id)[-1]
    assert (shipped.name, shipped.is_done_column, shipped.column_type) == ("Shipped", True, "DONE")


# ---------------------------------------------------------------------------
# Sidebar management and deleting safely
# ---------------------------------------------------------------------------
@pytest.fixture
def window(qapp, service):
    ws = service.create_workspace("Work")
    first = make_board(service, "First", tasks=3, workspace_id=ws.id)
    second = make_board(service, "Second", tasks=2, workspace_id=ws.id)
    win = MainWindow(service=service)
    win.resize(1300, 800)
    win.show()
    win.open_board(first.id)
    flush_qt()
    yield win, ws, first, second


def test_deleting_a_board_asks_first_and_moves_to_another_board(window, service, answer):
    win, ws, first, second = window
    answer.value = QMessageBox.No
    win.sidebar._delete_board(first.id)
    assert service.get_board(first.id) is not None, "No keeps the board"
    assert "First" in answer.asked[-1] and "3 tasks" in answer.asked[-1], "the question says what will be lost"

    answer.value = QMessageBox.Yes
    win.sidebar._delete_board(first.id)
    flush_qt()
    assert service.get_board(first.id) is None
    assert win.current_board_id == second.id, "the window moves to a board that still exists"
    assert [c.task_data["title"] for col in win.board_view._columns() for c in col.cards()] == \
        ["Task 0000", "Task 0001"]


def test_deleting_the_last_board_leaves_an_empty_window(window, service, answer):
    win, ws, first, second = window
    win.sidebar._delete_board(second.id)
    win.sidebar._delete_board(first.id)
    flush_qt()
    assert win.current_board_id is None
    assert win.board_view._columns() == [], "no ghost columns of a deleted board"
    assert "First" not in win.windowTitle()
    # actions that need a board do nothing instead of failing
    win._open_new_task_dialog()
    win.refresh_active_view()
    assert win.palette_commands("new")


def test_deleting_a_workspace_asks_and_counts_what_goes(window, service, answer):
    win, ws, first, second = window
    other = service.create_workspace("Elsewhere")
    kept = make_board(service, "Kept", tasks=1, workspace_id=other.id)
    win.sidebar.refresh()
    answer.value = QMessageBox.No
    win.sidebar._delete_workspace(ws.id)
    assert service.get_workspace(ws.id) is not None
    assert "2 boards" in answer.asked[-1] and "5 tasks" in answer.asked[-1]
    answer.value = QMessageBox.Yes
    win.sidebar._delete_workspace(ws.id)
    flush_qt()
    assert service.get_workspace(ws.id) is None
    assert win.current_board_id == kept.id, "boards in later workspaces are found too"


def test_workspace_and_board_dialogs_from_the_window(window, service, monkeypatch):
    win, ws, first, second = window

    def fill_and_accept(dlg):
        dlg.name_input.setText("Renamed")
        (dlg._save_workspace if isinstance(dlg, WorkspaceDialog) else dlg._save_board)()
        return dlg.result()

    monkeypatch.setattr(WorkspaceDialog, "exec", fill_and_accept)
    monkeypatch.setattr(BoardDialog, "exec", fill_and_accept)
    win._open_edit_workspace_dialog(ws.id)
    win._open_edit_board_dialog(first.id)
    flush_qt()
    assert service.get_workspace(ws.id).name == "Renamed" and service.get_board(first.id).name == "Renamed"
    assert win.windowTitle().endswith("Renamed")
    names = [win.sidebar.tree.topLevelItem(0).text(0)] + \
        [win.sidebar.tree.topLevelItem(0).child(i).text(0) for i in range(2)]
    assert any("Renamed" in n for n in names)
    win._open_new_workspace_dialog()
    assert len(service.get_workspaces()) == 2


def test_column_dialogs_from_the_window_refresh_the_board(window, service, monkeypatch):
    win, ws, first, second = window

    def rename(dlg):
        dlg.name_input.setText("Ideas")
        dlg._save_column()
        return dlg.result()

    monkeypatch.setattr(ColumnDialog, "exec", rename)
    win._open_edit_column_dialog(first.columns[0])
    flush_qt()
    assert win.board_view._columns()[0].title_lbl.text() == "Ideas"
    win._open_new_column_dialog(first.id)
    flush_qt()
    assert [c.title_lbl.text() for c in win.board_view._columns()][-1] == "Ideas"
    assert len(win.board_view._columns()) == 6


# ---------------------------------------------------------------------------
# Backups & data
# ---------------------------------------------------------------------------
def test_backup_dialog_snapshots_restores_and_moves_json(qapp, service, tmp_path, answer, monkeypatch):
    built = make_board(service, "Data", tasks=2)
    manager = BackupManager(service.db)
    manager.backup_dir = tmp_path / "backups"
    dlg = BackupDialog(manager)
    assert dlg.table.rowCount() == 0
    dlg._create_snapshot()
    assert dlg.table.rowCount() == 1
    snapshot = manager.list_backups()[0]["path"]

    service.create_task(built.id, built.columns[0], "Added after the snapshot")
    restored = []
    app_signals.backup_restored.connect(lambda: restored.append(True))
    answer.value = QMessageBox.No
    dlg._restore_snapshot(snapshot)
    assert len(service.get_tasks_for_board(built.id)) == 3 and not restored
    answer.value = QMessageBox.Yes
    dlg._restore_snapshot(snapshot)
    assert [t.title for t in service.get_tasks_for_board(built.id)] == ["Task 0000", "Task 0001"]
    assert restored and len(manager.list_backups()) >= 2, "a safety snapshot is taken first"

    out = tmp_path / "export.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    BackupDialog(manager)._export_json()
    data = json.loads(out.read_text())
    assert any(b["name"] == "Data" for ws in data["workspaces"] for b in ws["boards"])
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(out), ""))
    BackupDialog(manager)._import_json()
    assert sum(1 for ws in service.get_workspaces() for b in ws.boards if b.name == "Data") == 2
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    BackupDialog(manager)._export_json()  # cancelled: nothing happens


def test_the_windows_backup_dialog_works_on_the_windows_database(window, service, monkeypatch):
    win, *_ = window
    seen = []
    monkeypatch.setattr(BackupDialog, "exec", lambda dlg: seen.append(dlg.backup_mgr.db.db_path) or 0)
    win._open_backup_dialog()
    assert seen == [service.db.db_path]

"""
Unit Tests for Local Storage, SQLite Backups, and JSON Export/Import
"""
import pytest

from kanbanmanager import BackupManager
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager import DatabaseService


@pytest.fixture
def test_setup(tmp_path):
    db_file = tmp_path / "live_kanban.db"
    backup_dir = tmp_path / "backups"
    mgr = DatabaseManager(db_file)
    mgr.init_database(seed_defaults=True)

    backup_mgr = BackupManager(mgr)
    backup_mgr.backup_dir = backup_dir
    backup_mgr.backup_dir.mkdir(parents=True, exist_ok=True)

    service = DatabaseService(mgr)
    return mgr, service, backup_mgr, tmp_path


def test_sqlite_snapshot_and_restore(test_setup):
    mgr, service, backup_mgr, tmp_path = test_setup

    # Create a unique task in live DB
    ws = service.get_workspaces()[0]
    board = ws.boards[0]
    col = board.columns[0]
    task = service.create_task(board.id, col.id, title="Pre-Backup Task")

    # Create backup snapshot
    snap_path = backup_mgr.create_database_backup()
    assert snap_path.exists()
    assert snap_path.stat().st_size > 0

    # Modify live DB by creating another task
    service.create_task(board.id, col.id, title="Post-Backup Extra Task")
    tasks_before_restore = service.get_tasks_for_board(board.id)
    assert any(t.title == "Post-Backup Extra Task" for t in tasks_before_restore)

    # Restore from snapshot
    success = backup_mgr.restore_database_backup(snap_path)
    assert success is True

    # After restore, Pre-Backup Task should exist, Post-Backup Extra Task should NOT
    tasks_after_restore = service.get_tasks_for_board(board.id)
    assert any(t.title == "Pre-Backup Task" for t in tasks_after_restore)
    assert not any(t.title == "Post-Backup Extra Task" for t in tasks_after_restore)


def test_json_export_and_import(test_setup):
    mgr, service, backup_mgr, tmp_path = test_setup

    # Export to JSON
    json_dest = tmp_path / "kanban_export.json"
    backup_mgr.export_to_json(json_dest)
    assert json_dest.exists()

    # Create new empty database
    new_db_file = tmp_path / "imported_kanban.db"
    new_mgr = DatabaseManager(new_db_file)
    new_mgr.init_database(seed_defaults=False)
    new_backup_mgr = BackupManager(new_mgr)
    new_service = DatabaseService(new_mgr)

    # Import JSON into new DB
    success = new_backup_mgr.import_from_json(json_dest)
    assert success is True

    # Verify workspaces, boards, and tasks were restored in the new DB
    workspaces = new_service.get_workspaces()
    assert len(workspaces) > 0
    assert len(workspaces[0].boards) > 0

    imported_board = workspaces[0].boards[0]
    imported_tasks = new_service.get_tasks_for_board(imported_board.id)
    assert len(imported_tasks) > 0

"""
Unit Tests for Local Storage, SQLite Backups, and JSON Export/Import
"""
import pytest

from kanbanmanager import BackupManager
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager import DatabaseService
from support import seeded_manager


@pytest.fixture
def test_setup(tmp_path):
    db_file = tmp_path / "live_kanban.db"
    backup_dir = tmp_path / "backups"
    mgr = seeded_manager(db_file)

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


# ---------------------------------------------------------------------------
# Data safety: WAL mode, open connections, bad backup files
# ---------------------------------------------------------------------------
import sqlite3  # noqa: E402


def _titles(path):
    con = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in con.execute("SELECT title FROM tasks"))
    finally:
        con.close()


@pytest.fixture
def live(tmp_path):
    mgr = seeded_manager(tmp_path / "live.db", seed=False)
    service = DatabaseService(mgr)
    ws = service.create_workspace("W")
    board = service.create_board(ws.id, "B")
    col = service.get_columns_for_board(board.id)[0].id
    service.create_task(board.id, col, "Before the snapshot")
    backups = BackupManager(mgr)
    backups.backup_dir = tmp_path / "backups"
    return mgr, service, backups, board.id, col


def test_the_safety_snapshot_holds_work_still_in_the_wal(live):
    mgr, service, backups, board, col = live
    snapshot = backups.create_database_backup()
    service.create_task(board, col, "Precious work after the snapshot")
    assert (mgr.db_path.parent / f"{mgr.db_path.name}-wal").exists(), "recent commits live in the WAL"
    backups.restore_database_backup(snapshot)
    safety = sorted(backups.backup_dir.glob("pre_restore_safety_*.db"))
    assert safety, "a safety snapshot is taken before restoring"
    assert _titles(safety[-1]) == ["Before the snapshot", "Precious work after the snapshot"], \
        "was: a raw copy of the main file, missing everything not yet checkpointed"
    assert [t.title for t in service.get_tasks_for_board(board)] == ["Before the snapshot"]
    # and undoing the restore from the safety snapshot brings the work back
    backups.restore_database_backup(safety[-1])
    assert "Precious work after the snapshot" in [t.title for t in service.get_tasks_for_board(board)]


def test_restore_with_another_connection_open_is_consistent(live):
    mgr, service, backups, board, col = live
    snapshot = backups.create_database_backup()
    service.create_task(board, col, "Later")
    other = sqlite3.connect(mgr.db_path)  # e.g. a background sync thread's connection
    other.execute("SELECT count(*) FROM tasks").fetchall()
    try:
        backups.restore_database_backup(snapshot)
    finally:
        other.close()
    assert [t.title for t in service.get_tasks_for_board(board)] == ["Before the snapshot"]
    con = sqlite3.connect(mgr.db_path)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


@pytest.mark.parametrize("content", [b"", b"this is not a database at all" * 100, b"SQLite format 3\x00" + b"\x00" * 50])
def test_a_bad_backup_file_is_refused_and_the_live_data_survives(live, tmp_path, content):
    mgr, service, backups, board, col = live
    bad = tmp_path / "bad.db"
    bad.write_bytes(content)
    with pytest.raises(ValueError, match="not a KanbanManager backup"):
        backups.restore_database_backup(bad)
    assert [t.title for t in service.get_tasks_for_board(board)] == ["Before the snapshot"]


def test_a_database_of_another_app_is_refused(live, tmp_path):
    mgr, service, backups, board, col = live
    foreign = tmp_path / "foreign.db"
    con = sqlite3.connect(foreign)
    con.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="not a KanbanManager backup"):
        backups.restore_database_backup(foreign)
    assert [t.title for t in service.get_tasks_for_board(board)] == ["Before the snapshot"]


def test_restoring_a_missing_file_changes_nothing(live, tmp_path):
    mgr, service, backups, board, col = live
    with pytest.raises(FileNotFoundError):
        backups.restore_database_backup(tmp_path / "nope.db")
    assert not list(backups.backup_dir.glob("pre_restore_safety_*.db")) if backups.backup_dir.exists() else True
    assert [t.title for t in service.get_tasks_for_board(board)] == ["Before the snapshot"]


def test_listing_backups_newest_first(live):
    mgr, service, backups, board, col = live
    first = backups.create_database_backup(backups.backup_dir / "kanban_backup_20240101_000000.db")
    second = backups.create_database_backup(backups.backup_dir / "kanban_backup_20250101_000000.db")
    import os
    os.utime(first, (1_700_000_000, 1_700_000_000))
    listed = backups.list_backups()
    assert [b["filename"] for b in listed][:2] == [second.name, first.name]
    assert all(b["size_kb"] >= 0 and b["modified"] for b in listed)

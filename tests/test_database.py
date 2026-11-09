"""
Unit Tests for Database Models and Service Operations
"""
import pytest
from datetime import datetime

from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager import DatabaseService
from kanbanmanager import Priority, ColumnType


@pytest.fixture
def test_service(tmp_path):
    db_file = tmp_path / "test_kanban.db"
    mgr = DatabaseManager(db_file)
    mgr.init_database(seed_defaults=False)
    return DatabaseService(mgr)


def test_workspace_crud(test_service):
    # Create
    ws = test_service.create_workspace(name="Engineering", description="Dev Projects", color="#3B82F6")
    assert ws.id is not None
    assert ws.name == "Engineering"

    # Read
    fetched = test_service.get_workspace(ws.id)
    assert fetched is not None
    assert fetched.name == "Engineering"

    # Update
    updated = test_service.update_workspace(ws.id, name="Core Engineering", color="#10B981")
    assert updated.name == "Core Engineering"
    assert updated.color == "#10B981"

    # Delete
    deleted = test_service.delete_workspace(ws.id)
    assert deleted is True
    assert test_service.get_workspace(ws.id) is None


def test_board_and_column_crud(test_service):
    ws = test_service.create_workspace(name="Product")
    board = test_service.create_board(workspace_id=ws.id, name="Sprint 1")
    assert board.id is not None

    cols = test_service.get_columns_for_board(board.id)
    assert len(cols) == 5  # Default columns created
    assert cols[0].name == "Backlog"

    # Add custom column
    custom_col = test_service.create_column(
        board_id=board.id,
        name="QA Testing",
        wip_limit=2,
        column_type=ColumnType.REVIEW.value,
    )
    assert custom_col.id is not None
    assert custom_col.wip_limit == 2

    # Update column
    updated_col = test_service.update_column(custom_col.id, wip_limit=4)
    assert updated_col.wip_limit == 4


def test_task_lifecycle_and_labels(test_service):
    ws = test_service.create_workspace(name="Operations")
    board = test_service.create_board(workspace_id=ws.id, name="Main Board")
    cols = test_service.get_columns_for_board(board.id)
    todo_col = cols[1]

    # Create label
    lbl = test_service.create_label(board.id, name="Security", color="#EF4444")

    # Create task
    task = test_service.create_task(
        board_id=board.id,
        column_id=todo_col.id,
        title="Audit user permissions",
        description="Verify RBAC policies",
        priority=Priority.HIGH.value,
        estimated_hours=3.5,
        due_date=datetime.now(),
        label_ids=[lbl.id],
        checklist=[{"id": 1, "text": "Check admin roles", "done": False}],
    )
    assert task.id is not None
    assert len(task.labels) == 1
    assert task.labels[0].name == "Security"
    assert len(task.checklist) == 1

    # Toggle completion
    completed_task = test_service.toggle_task_completion(task.id)
    assert completed_task.is_completed is True
    assert completed_task.completed_at is not None

    # Delete task
    assert test_service.delete_task(task.id) is True
    assert test_service.get_task(task.id) is None

"""
Unit Tests for Custom Column Workflows, WIP Limits, and Transition Rules
"""
import pytest

from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager import DatabaseService


@pytest.fixture
def test_service(tmp_path):
    db_file = tmp_path / "test_workflow.db"
    mgr = DatabaseManager(db_file)
    mgr.init_database(seed_defaults=False)
    return DatabaseService(mgr)


def test_wip_limit_enforcement(test_service):
    ws = test_service.create_workspace(name="Dev")
    board = test_service.create_board(workspace_id=ws.id, name="Kanban")
    
    col_todo = test_service.create_column(board.id, name="To Do", wip_limit=0)
    col_wip = test_service.create_column(board.id, name="In Progress", wip_limit=1)

    t1 = test_service.create_task(board.id, col_todo.id, title="Task 1")
    t2 = test_service.create_task(board.id, col_todo.id, title="Task 2")

    # Move Task 1 to In Progress (Allowed, WIP count becomes 1 == limit)
    success, err = test_service.move_task(t1.id, col_wip.id)
    assert success is True
    assert err is None

    # Move Task 2 to In Progress (Should be rejected because WIP limit is reached)
    success, err = test_service.move_task(t2.id, col_wip.id)
    assert success is False
    assert "WIP limit" in err


def test_workflow_transition_rules(test_service):
    ws = test_service.create_workspace(name="Dev")
    board = test_service.create_board(workspace_id=ws.id, name="Strict Workflow")

    col_backlog = test_service.create_column(board.id, name="Backlog")
    col_dev = test_service.create_column(board.id, name="Development")
    col_done = test_service.create_column(board.id, name="Done", is_done_column=True)

    # Restrict Backlog so it can ONLY transition to Development (cannot jump straight to Done)
    test_service.update_column(col_backlog.id, allowed_transitions=[col_dev.id])

    t = test_service.create_task(board.id, col_backlog.id, title="Strict Task")

    # Direct move to Done should fail
    success, err = test_service.move_task(t.id, col_done.id)
    assert success is False
    assert "not allowed" in err

    # Move to Development should succeed
    success, err = test_service.move_task(t.id, col_dev.id)
    assert success is True

    # Move from Development to Done should succeed and mark task completed
    success, err = test_service.move_task(t.id, col_done.id)
    assert success is True
    updated_t = test_service.get_task(t.id)
    assert updated_t.is_completed is True

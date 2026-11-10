"""
Unit Tests for Task Dependencies, Cycle Prevention, and Blocked Status
"""
import pytest

from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager import DatabaseService


@pytest.fixture
def test_service(tmp_path):
    db_file = tmp_path / "test_dependencies.db"
    mgr = DatabaseManager(db_file)
    mgr.init_database(seed_defaults=False)
    return DatabaseService(mgr)


def test_dependency_blocked_status(test_service):
    ws = test_service.create_workspace(name="Projects")
    board = test_service.create_board(workspace_id=ws.id, name="Sprint")
    col = test_service.create_column(board.id, name="To Do")

    # Task A (Prerequisite) and Task B (Dependent)
    task_a = test_service.create_task(board.id, col.id, title="Database Migration")
    task_b = test_service.create_task(board.id, col.id, title="Deploy API")

    # Add dependency: Task B depends on Task A
    success, err = test_service.add_dependency(task_id=task_b.id, depends_on_task_id=task_a.id)
    assert success is True
    assert err is None

    # Check Task B status -> Should be blocked because Task A is not completed
    task_b_fetched = test_service.get_task(task_b.id)
    assert task_b_fetched.is_blocked is True

    # Complete Task A
    test_service.toggle_task_completion(task_a.id)

    # Check Task B status -> Should no longer be blocked
    task_b_fetched = test_service.get_task(task_b.id)
    assert task_b_fetched.is_blocked is False


def test_circular_dependency_prevention(test_service):
    ws = test_service.create_workspace(name="Projects")
    board = test_service.create_board(workspace_id=ws.id, name="Dependencies")
    col = test_service.create_column(board.id, name="To Do")

    t1 = test_service.create_task(board.id, col.id, title="Task 1")
    t2 = test_service.create_task(board.id, col.id, title="Task 2")
    t3 = test_service.create_task(board.id, col.id, title="Task 3")

    # t1 depends on t2
    test_service.add_dependency(task_id=t1.id, depends_on_task_id=t2.id)
    # t2 depends on t3
    test_service.add_dependency(task_id=t2.id, depends_on_task_id=t3.id)

    # Now attempting to make t3 depend on t1 would create cycle: t1 -> t2 -> t3 -> t1
    success, err = test_service.add_dependency(task_id=t3.id, depends_on_task_id=t1.id)
    assert success is False
    assert "circular dependency" in err.lower()

    # Self-dependency should also be rejected
    success, err = test_service.add_dependency(task_id=t1.id, depends_on_task_id=t1.id)
    assert success is False


def test_dependency_on_a_deleted_task_is_refused_cleanly(test_service):
    """Found by the model-based test: this used to raise an IntegrityError."""
    ws = test_service.create_workspace(name="W")
    board = test_service.create_board(workspace_id=ws.id, name="B")
    col = test_service.get_columns_for_board(board.id)[0].id
    a = test_service.create_task(board.id, col, "A")
    b = test_service.create_task(board.id, col, "B")
    test_service.delete_task(b.id)
    assert test_service.add_dependency(a.id, b.id) == (False, "Task not found")
    assert test_service.add_dependency(b.id, a.id) == (False, "Task not found")
    assert test_service.get_task(a.id).to_dict()["prerequisites"] == []

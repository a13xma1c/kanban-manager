"""
End-to-End Integration Test for Project Management Features
"""
import pytest
from datetime import datetime, timedelta

from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager import DatabaseService
from kanbanmanager import BackupManager
from kanbanmanager import RecurrenceEngine
from kanbanmanager import Priority, RecurrenceFrequency


@pytest.fixture
def e2e_env(tmp_path):
    db_file = tmp_path / "e2e_kanban.db"
    mgr = DatabaseManager(db_file)
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr)
    backup_mgr = BackupManager(mgr)
    backup_mgr.backup_dir = tmp_path / "backups"
    backup_mgr.backup_dir.mkdir(parents=True, exist_ok=True)
    return mgr, service, backup_mgr, tmp_path


def test_full_project_management_flow(e2e_env):
    mgr, service, backup_mgr, tmp_path = e2e_env

    # 1. Multiple Workspaces & Boards
    ws1 = service.create_workspace(name="Engineering", color="#6366F1")
    ws2 = service.create_workspace(name="Marketing", color="#EC4899")

    board1 = service.create_board(workspace_id=ws1.id, name="Platform V2")
    board2 = service.create_board(workspace_id=ws1.id, name="Mobile App")
    board3 = service.create_board(workspace_id=ws2.id, name="Campaign Q4")

    assert len(service.get_workspaces()) == 2
    assert len(service.get_boards_for_workspace(ws1.id)) == 2
    assert len(service.get_boards_for_workspace(ws2.id)) == 1

    # 2. Custom Columns and Workflows
    cols = service.get_columns_for_board(board1.id)
    backlog_col, todo_col, inprog_col, review_col, done_col = cols

    # Configure WIP limit on In Progress
    service.update_column(inprog_col.id, wip_limit=2)

    # 3. Labels and Priorities
    label_feature = service.create_label(board1.id, name="Feature", color="#3B82F6")
    label_bug = service.create_label(board1.id, name="Critical Bug", color="#EF4444")

    # 4. Due Dates and Reminders
    due_tomorrow = datetime.now() + timedelta(days=1)
    task1 = service.create_task(
        board_id=board1.id,
        column_id=todo_col.id,
        title="Setup Auth Microservice",
        description="OAuth2 & JWT implementation",
        priority=Priority.URGENT.value,
        estimated_hours=8.0,
        due_date=due_tomorrow,
        label_ids=[label_feature.id],
        checklist=[
            {"id": 1, "text": "Token signing", "done": True},
            {"id": 2, "text": "Refresh token rotation", "done": False},
        ],
    )
    reminder = service.create_reminder(
        task_id=task1.id,
        remind_at=due_tomorrow - timedelta(hours=1),
        offset_minutes=60,
    )
    assert reminder.id is not None

    # 5. Task Dependencies
    task2 = service.create_task(
        board_id=board1.id,
        column_id=todo_col.id,
        title="Integrate Client Login Screen",
        priority=Priority.HIGH.value,
    )
    # task2 depends on task1 (client login requires auth microservice)
    success, _ = service.add_dependency(task_id=task2.id, depends_on_task_id=task1.id)
    assert success is True

    # Task2 is blocked because Task1 is not yet complete
    t2_fetched = service.get_task(task2.id)
    assert t2_fetched.is_blocked is True

    # 6. Drag-and-Drop / Move task
    # Move task1 to In Progress
    ok, err = service.move_task(task1.id, inprog_col.id)
    assert ok is True

    # 7. Recurring Tasks
    rec_rule = {"frequency": RecurrenceFrequency.WEEKDAYS.value}
    rec_task = service.create_task(
        board_id=board1.id,
        column_id=backlog_col.id,
        title="Daily Standup Meeting",
        is_recurring=True,
        recurrence_rule=rec_rule,
        due_date=datetime.now(),
    )
    # Complete recurring task
    service.toggle_task_completion(rec_task.id)
    next_task_id = RecurrenceEngine.handle_recurring_task_completion(service, rec_task.id)
    assert next_task_id is not None
    next_task = service.get_task(next_task_id)
    assert next_task.title == "Daily Standup Meeting"
    assert next_task.is_completed is False

    # Complete Task1 (the dependency prerequisite)
    service.move_task(task1.id, done_col.id)
    t1_done = service.get_task(task1.id)
    assert t1_done.is_completed is True

    # Now task2 is unblocked
    t2_unblocked = service.get_task(task2.id)
    assert t2_unblocked.is_blocked is False

    # 8. Local Data Storage, Backup & JSON Export
    snap = backup_mgr.create_database_backup()
    assert snap.exists()

    json_path = tmp_path / "e2e_export.json"
    backup_mgr.export_to_json(json_path)
    assert json_path.exists()

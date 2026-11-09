"""
Unit Tests for Recurring Task Calculations and Auto-Spawning
"""
from datetime import datetime
import pytest

from kanbanmanager import RecurrenceEngine
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager import DatabaseService
from kanbanmanager import RecurrenceFrequency


@pytest.fixture
def test_service(tmp_path):
    db_file = tmp_path / "test_recurrence.db"
    mgr = DatabaseManager(db_file)
    mgr.init_database(seed_defaults=False)
    return DatabaseService(mgr)


def test_calculate_next_due_date_daily():
    base = datetime(2026, 11, 10, 12, 0, 0)
    rule = {"frequency": RecurrenceFrequency.DAILY.value, "interval": 1}
    next_dt = RecurrenceEngine.calculate_next_due_date(base, rule, from_datetime=base)
    assert next_dt == datetime(2026, 11, 11, 12, 0, 0)


def test_calculate_next_due_date_weekdays():
    # Friday -> should advance to Monday
    friday = datetime(2026, 11, 13, 10, 0, 0)
    assert friday.weekday() == 4  # Friday
    rule = {"frequency": RecurrenceFrequency.WEEKDAYS.value}
    next_dt = RecurrenceEngine.calculate_next_due_date(friday, rule, from_datetime=friday)
    assert next_dt.weekday() == 0  # Monday
    assert next_dt.day == 16


def test_calculate_next_due_date_monthly():
    base = datetime(2026, 1, 15, 9, 0, 0)
    rule = {"frequency": RecurrenceFrequency.MONTHLY.value, "interval": 1}
    next_dt = RecurrenceEngine.calculate_next_due_date(base, rule, from_datetime=base)
    assert next_dt == datetime(2026, 2, 15, 9, 0, 0)


def test_recurring_task_completion_spawn(test_service):
    ws = test_service.create_workspace(name="Schedules")
    board = test_service.create_board(workspace_id=ws.id, name="Daily Ops")
    cols = test_service.get_columns_for_board(board.id)
    backlog_col = cols[0]

    rule = {"frequency": RecurrenceFrequency.DAILY.value, "interval": 1}
    initial_due = datetime(2026, 11, 8, 10, 0, 0)

    task = test_service.create_task(
        board_id=board.id,
        column_id=backlog_col.id,
        title="Daily Standup Notes",
        is_recurring=True,
        recurrence_rule=rule,
        due_date=initial_due,
        checklist=[{"id": 1, "text": "Post update", "done": True}],
    )

    # Spawn next recurring instance upon task completion
    new_task_id = RecurrenceEngine.handle_recurring_task_completion(test_service, task.id)
    assert new_task_id is not None

    new_task = test_service.get_task(new_task_id)
    assert new_task.title == "Daily Standup Notes"
    assert new_task.is_completed is False
    assert new_task.due_date > initial_due
    # Checklist items reset to not done
    assert new_task.checklist[0]["done"] is False

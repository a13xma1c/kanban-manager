"""
Tests for time entries, the stopwatch/Pomodoro engine, workload & capacity
planning, timesheet exports, and the related migrations/exports.
"""
import csv
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from PySide6.QtCore import QCoreApplication

from kanbanmanager import BackupManager, DatabaseService
from kanbanmanager.core import timesheet as ts
from kanbanmanager.core.time_tracker import FOCUS, IDLE, LONG_BREAK, POMODORO, SHORT_BREAK, STOPWATCH, TimeTracker
from kanbanmanager.core.workload import DAY, UNASSIGNED, WEEK, build_periods, compute_workload
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.enums import ActivityAction as A
from kanbanmanager.timefmt import format_duration, parse_duration


@pytest.fixture(scope="module")
def qcore():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def env(tmp_path):
    mgr = DatabaseManager(tmp_path / "time.db")
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr, actor="Tess")
    ws = service.create_workspace("W")
    board = service.create_board(ws.id, "B")
    col = service.get_columns_for_board(board.id)[0]
    task = service.create_task(board.id, col.id, "Build it", estimated_hours=2)
    return mgr, service, board, col, task


class Clock:
    def __init__(self, start=datetime(2024, 1, 8, 9, 0, 0)):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def actions(service, task_id):
    return [e["action_type"] for e in service.get_task_activity(task_id)]


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,seconds", [
    ("1h 30m", 5400), ("1h30", 5400), ("90m", 5400), ("45 min", 2700), ("1.5", 5400), ("1,5", 5400),
    ("1:30", 5400), ("2", 7200), ("2 hours 15 minutes", 8100), ("0", None), ("", None), ("abc", None),
    ("1h foo", None), ("0:00", None),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


def test_format_duration():
    assert [format_duration(s) for s in (12, 60, 90 * 60, 3600, 3630)] == ["12s", "1m", "1h 30m", "1h", "1h 1m"]


# ---------------------------------------------------------------------------
# Time entries (service)
# ---------------------------------------------------------------------------
def test_timer_entries_update_spent_hours_and_audit(env):
    _, service, board, col, task = env
    t0 = datetime(2024, 1, 8, 9, 0)
    entry = service.start_time_entry(task.id, started_at=t0)
    assert service.get_running_time_entry()["id"] == entry["id"]
    assert service.get_time_entries(task_id=task.id) == [], "running entries are not listed by default"

    other = service.create_task(board.id, col.id, "Other")
    service.start_time_entry(other.id, started_at=t0 + timedelta(minutes=30))  # switching tasks stops the first
    first = service.get_time_entries(task_id=task.id)
    assert len(first) == 1 and first[0]["duration_seconds"] > 0

    assert service.stop_time_entry(ended_at=t0 + timedelta(minutes=30, seconds=10), min_seconds=30) is None
    assert service.get_running_time_entry() is None, "too-short entries are discarded"

    service.add_time_entry(task.id, 3600, note="Pairing")
    assert service.get_task(task.id).spent_hours == pytest.approx(1 + first[0]["duration_seconds"] / 3600)
    logs = service.get_task_activity(task.id)
    assert [l["action_type"] for l in logs].count(A.TIME_LOGGED.value) == 2
    assert logs[-1]["details"] == "Logged 1h (manual): Pairing"


def test_manual_entry_validation_update_and_delete(env):
    _, service, _, _, task = env
    with pytest.raises(ValueError):
        service.add_time_entry(task.id, 0)
    with pytest.raises(ValueError):
        service.add_time_entry(task.id, 25 * 3600)
    e = service.add_time_entry(task.id, 1800)
    updated = service.update_time_entry(e["id"], duration_seconds=2700, note="fixed")
    assert updated["duration_seconds"] == 2700 and updated["note"] == "fixed"
    assert service.get_task(task.id).spent_hours == pytest.approx(0.75)
    assert service.delete_time_entry(e["id"]) is True
    assert service.get_task(task.id).spent_hours == 0
    assert actions(service, task.id)[-2:] == [A.TIME_ENTRY_UPDATED.value, A.TIME_ENTRY_DELETED.value]


def test_time_entry_queries(env):
    _, service, board, col, task = env
    other_board = service.create_board(board.workspace_id, "B2")
    other_task = service.create_task(other_board.id, service.get_columns_for_board(other_board.id)[0].id, "T2")
    service.add_time_entry(task.id, 600, started_at=datetime(2024, 1, 1, 10))
    service.add_time_entry(task.id, 600, started_at=datetime(2024, 1, 5, 10), user="Bob")
    service.add_time_entry(other_task.id, 600, started_at=datetime(2024, 1, 5, 11))
    assert len(service.get_time_entries(board_ids=[board.id])) == 2
    assert len(service.get_time_entries(start=datetime(2024, 1, 2), end=datetime(2024, 1, 6))) == 2
    assert [e["user"] for e in service.get_time_entries(user="Bob")] == ["Bob"]
    assert service.get_time_entries(board_ids=[]) == []
    e = service.get_time_entries(board_ids=[other_board.id])[0]
    assert (e["task_title"], e["board_name"], e["workspace_name"]) == ("T2", "B2", "W")


def test_deleting_task_removes_its_time(env):
    _, service, _, _, task = env
    service.start_time_entry(task.id)
    service.delete_task(task.id)
    assert service.get_running_time_entry() is None


def test_team_members_people_and_settings(env):
    _, service, board, col, task = env
    m = service.upsert_team_member("Ann", 24, [0, 1, 2])
    assert m == {"id": m["id"], "name": "Ann", "weekly_capacity_hours": 24.0, "working_days": [0, 1, 2]}
    assert service.upsert_team_member("Ann", 30)["working_days"] == [0, 1, 2], "partial update keeps days"
    with pytest.raises(ValueError):
        service.upsert_team_member("Ann", 200)
    with pytest.raises(ValueError):
        service.upsert_team_member("  ")
    service.update_task(task.id, assignee="Zed")
    assert service.get_people() == ["Ann", "Tess", "Zed"]
    assert service.delete_team_member("Ann") and not service.delete_team_member("Ann")

    assert service.get_setting("pomodoro", {"x": 1}) == {"x": 1}
    service.set_setting("pomodoro", {"focus_minutes": 50})
    assert service.get_setting("pomodoro") == {"focus_minutes": 50}


def test_assignee_changes_are_audited(env):
    _, service, _, _, task = env
    service.update_task(task.id, assignee="Ann")
    service.update_task(task.id, assignee="  Ann  ")  # whitespace only: no change
    service.update_task(task.id, assignee="Bob")
    service.update_task(task.id, assignee="")
    details = [e["details"] for e in service.get_task_activity(task.id) if e["field_name"] == "assignee"]
    assert details == ["Assigned to Ann", "Reassigned from Ann to Bob", "Unassigned (was Bob)"]
    assert service.get_task(task.id).assignee is None


def test_legacy_database_gets_assignee_column_and_spent_hours_backfill(tmp_path):
    db = tmp_path / "old.db"
    mgr = DatabaseManager(db)
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr)
    ws = service.create_workspace("W")
    b = service.create_board(ws.id, "B")
    t = service.create_task(b.id, service.get_columns_for_board(b.id)[0].id, "Legacy")
    mgr.engine.dispose()
    con = sqlite3.connect(db)
    con.executescript(
        """
        DROP TABLE time_entries; DROP TABLE team_members; DROP TABLE app_settings;
        ALTER TABLE tasks DROP COLUMN assignee;
        UPDATE tasks SET spent_hours = 1.5;
        """
    )
    con.commit()
    con.close()

    upgraded = DatabaseManager(db)
    upgraded.init_database(seed_defaults=False)
    upgraded.init_database(seed_defaults=False)  # idempotent: no second backfill entry
    service = DatabaseService(upgraded)
    entries = service.get_time_entries(task_id=t.id)
    assert len(entries) == 1 and entries[0]["duration_seconds"] == 5400 and entries[0]["source"] == "MANUAL"
    assert service.get_task(t.id).assignee is None
    service.update_task(t.id, assignee="Ann")
    assert service.get_task(t.id).assignee == "Ann"


# ---------------------------------------------------------------------------
# Timer engine
# ---------------------------------------------------------------------------
def test_stopwatch_pause_resume_and_short_discard(qcore, env):
    _, service, _, _, task = env
    clock = Clock()
    tr = TimeTracker(service, now=clock)
    tr.start_stopwatch(task.id)
    assert (tr.mode, tr.is_tracking_work) == (STOPWATCH, True)
    clock.advance(600)
    tr.pause()
    assert tr.paused and not tr.is_tracking_work
    clock.advance(3600)  # paused time is not counted
    tr.resume()
    clock.advance(300)
    assert tr.display_time() == "15:00"
    assert tr.stop() == 300
    assert [e["duration_seconds"] for e in service.get_time_entries(task_id=task.id)] == [600, 300]
    assert tr.mode == IDLE and tr.last_task_id == task.id

    tr.start_stopwatch(task.id)
    clock.advance(10)
    assert tr.stop() == 0, "an accidental 10 s run is not logged"
    assert len(service.get_time_entries(task_id=task.id)) == 2


def test_pomodoro_cycle(qcore, env):
    _, service, _, _, task = env
    clock = Clock()
    tr = TimeTracker(service, now=clock)
    tr.save_settings({"focus_minutes": 25, "short_break_minutes": 5, "long_break_minutes": 15,
                      "sessions_before_long_break": 2, "auto_start_breaks": True})
    messages = []
    tr.phase_finished.connect(lambda phase, msg: messages.append(phase))

    tr.start_pomodoro(task.id)
    assert (tr.mode, tr.phase, tr.display_time()) == (POMODORO, FOCUS, "25:00")
    clock.advance(25 * 60 + 4)  # the tick arrives a little late
    tr._on_tick()
    assert tr.phase == SHORT_BREAK and not tr.paused and messages == [FOCUS]
    assert [e["duration_seconds"] for e in service.get_time_entries(task_id=task.id)] == [1500], \
        "exactly the focus length is logged"
    clock.advance(5 * 60)
    tr._on_tick()
    assert tr.phase == FOCUS and tr.paused, "the next focus waits for the user"
    assert service.get_running_time_entry() is None, "nothing is tracked while waiting"

    tr.resume()
    clock.advance(25 * 60)
    tr._on_tick()
    assert tr.phase == LONG_BREAK, "every 2nd break is long"
    tr.skip_break()
    assert tr.phase == FOCUS and tr.is_tracking_work
    tr.stop()
    assert sum(e["duration_seconds"] for e in service.get_time_entries(task_id=task.id)) == 3000


def test_pomodoro_without_auto_breaks_and_discard(qcore, env):
    _, service, _, _, task = env
    clock = Clock()
    tr = TimeTracker(service, now=clock)
    tr.save_settings({"focus_minutes": 1, "auto_start_breaks": False})
    tr.start_pomodoro(task.id)
    clock.advance(61)
    tr._on_tick()
    assert tr.phase == SHORT_BREAK and tr.paused
    tr.start_stopwatch(task.id)
    clock.advance(120)
    tr.discard()
    assert tr.mode == IDLE
    assert [e["duration_seconds"] for e in service.get_time_entries(task_id=task.id)] == [60]


def test_restore_after_restart(qcore, env):
    _, service, _, _, task = env
    clock = Clock()
    TimeTracker(service, now=clock).start_stopwatch(task.id)  # app "closes" while tracking
    clock.advance(2 * 3600)
    tr = TimeTracker(service, now=clock)
    assert "Resumed" in tr.restore()
    assert tr.mode == STOPWATCH and tr.display_time() == "2:00:00"
    tr.stop()

    TimeTracker(service, now=clock).start_pomodoro(task.id)
    clock.advance(3 * 3600)  # the focus session ended long ago
    tr = TimeTracker(service, now=clock)
    assert "finished while" in tr.restore()
    assert tr.mode == IDLE
    assert service.get_time_entries(task_id=task.id)[-1]["duration_seconds"] == 25 * 60
    assert TimeTracker(service, now=clock).restore() is None


def test_sync_drops_timer_of_deleted_task(qcore, env):
    _, service, _, _, task = env
    tr = TimeTracker(service, now=Clock())
    tr.start_stopwatch(task.id)
    service.delete_task(task.id)
    tr.sync()
    assert tr.mode == IDLE and tr.last_task_id is None


# ---------------------------------------------------------------------------
# Workload
# ---------------------------------------------------------------------------
TODAY = date(2024, 6, 5)  # Wednesday


def _task(id, assignee, est, due=None, spent=0, start=None, done=False):
    return dict(id=id, title=f"T{id}", board_name="B", assignee=assignee, estimated_hours=est, spent_hours=spent,
                due_date=due, start_date=start, is_completed=done)


def test_workload_distribution_capacity_and_overload():
    members = {"Ann": {"weekly_capacity_hours": 40, "working_days": [0, 1, 2, 3, 4]}}
    tasks = [
        _task(1, "Ann", 10, "2024-06-11T17:00:00", spent=2),   # 8h left over Wed..Tue = 5 working days
        _task(2, "Ann", 30, "2024-06-07T17:00:00"),            # 30h due Friday -> overload this week
        _task(3, "Ann", 99, "2024-06-06", done=True),          # completed: ignored
        _task(4, "Ann", 5, "2024-06-20", spent=6),             # over-logged: nothing left
    ]
    res = compute_workload(tasks, members, TODAY, 2, WEEK, TODAY)
    ann = res.row("Ann")
    this_week, next_week = ann.cells
    assert this_week.capacity == 24, "only Wed-Fri remain this week"
    assert this_week.planned == pytest.approx(8 * 3 / 5 + 30)
    assert next_week.planned == pytest.approx(8 * 2 / 5)
    assert this_week.status == "over" and next_week.status == "ok"
    assert [(r.name, c.start) for r, c in res.overloads()] == [("Ann", date(2024, 6, 3))]


def test_workload_overdue_unscheduled_unassigned_and_defaults():
    tasks = [
        _task(1, "Bob", 4, "2024-06-01T10:00:00"),  # overdue -> today
        _task(2, "Bob", 6, None, spent=1),          # no due date -> unscheduled
        _task(3, "", 3, "2024-06-06"),              # unassigned
        _task(4, "", 0, "2024-06-06"),              # no estimate
    ]
    res = compute_workload(tasks, {}, TODAY, 5, DAY, TODAY)
    bob = res.row("Bob")
    assert bob.weekly_capacity == 40 and not bob.is_member, "non-members get the default capacity"
    assert bob.cells[0].planned == 4 and bob.cells[0].allocations[0].is_overdue
    assert bob.unscheduled_hours == 5
    assert [c.capacity for c in bob.cells] == [8, 8, 8, 0, 0], "weekend has no capacity"
    unassigned = res.rows[-1]
    assert unassigned.name == UNASSIGNED and unassigned.unestimated_tasks == 1
    assert unassigned.cells[0].planned + unassigned.cells[1].planned == pytest.approx(3)


def test_workload_custom_working_days_logged_time_and_start_dates():
    members = {"Eve": {"weekly_capacity_hours": 16, "working_days": [0, 1]}, "Idle": {"weekly_capacity_hours": 20,
                                                                                    "working_days": [0, 1, 2, 3, 4]}}
    tasks = [_task(1, "Eve", 8, "2024-06-14", start="2024-06-10")]  # only Mon/Tue next week are workdays
    entries = [{"user": "Eve", "started_at": "2024-06-04T10:00:00", "duration_seconds": 5400}]
    res = compute_workload(tasks, members, TODAY, 2, WEEK, TODAY, time_entries=entries)
    eve = res.row("Eve")
    assert eve.cells[0].capacity == 0, "Eve's Mon/Tue this week are already past"
    assert eve.cells[0].planned == 0 and eve.cells[1].planned == 8
    assert eve.cells[0].logged == pytest.approx(1.5)
    assert res.row("Idle") is not None, "team members are listed even without work"


def test_build_periods():
    assert build_periods(TODAY, 2, WEEK) == [(date(2024, 6, 3), date(2024, 6, 9)), (date(2024, 6, 10), date(2024, 6, 16))]
    assert build_periods(TODAY, 1, DAY) == [(TODAY, TODAY)]


# ---------------------------------------------------------------------------
# Timesheets
# ---------------------------------------------------------------------------
def test_preset_ranges():
    wed = date(2024, 6, 5)
    assert ts.preset_range(ts.PRESET_THIS_WEEK, wed) == (date(2024, 6, 3), date(2024, 6, 9))
    assert ts.preset_range(ts.PRESET_LAST_WEEK, wed) == (date(2024, 5, 27), date(2024, 6, 2))
    assert ts.preset_range(ts.PRESET_THIS_MONTH, wed) == (date(2024, 6, 1), date(2024, 6, 30))
    assert ts.preset_range(ts.PRESET_LAST_MONTH, date(2024, 3, 15)) == (date(2024, 2, 1), date(2024, 2, 29))
    assert ts.preset_range(ts.PRESET_LAST_30, wed) == (date(2024, 5, 7), wed)


def _entries(service, task):
    from kanbanmanager.timefmt import local_to_utc
    service.add_time_entry(task.id, 3600, started_at=local_to_utc(datetime(2024, 6, 3, 9)), note="=HYPERLINK(x)")
    service.add_time_entry(task.id, 1800, started_at=local_to_utc(datetime(2024, 6, 4, 14)), user="Bob")
    service.add_time_entry(task.id, 900, started_at=local_to_utc(datetime(2024, 6, 10, 9)))  # outside range
    start, end = ts.local_range_to_utc(date(2024, 6, 3), date(2024, 6, 9))
    return ts.build_rows(service.get_time_entries(start=start, end=end))


def test_timesheet_rows_grouping_and_csv(env, tmp_path):
    _, service, _, _, task = env
    rows = _entries(service, task)
    assert [(r.day, r.start.hour, r.person) for r in rows] == [(date(2024, 6, 3), 9, "Tess"), (date(2024, 6, 4), 14, "Bob")]
    assert ts.group_rows(rows, ts.GROUP_PERSON) == [(("Bob",), 1800), (("Tess",), 3600)]
    assert ts.group_rows(rows, ts.GROUP_DAY)[0] == (("2024-06-03", "Monday"), 3600)

    path = ts.write_entries_csv(rows, tmp_path / "out" / "ts.csv")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM so Excel detects UTF-8"
    table = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    assert table[0][:5] == ["Date", "Start", "End", "Duration", "Hours"]
    assert table[1][12] == "'=HYPERLINK(x)", "formula injection is neutralised"
    assert table[-1][:5] == ["Total", "", "", "1h 30m", "1.50"]

    summary = list(csv.reader(ts.write_summary_csv(rows, date(2024, 6, 3), date(2024, 6, 9), tmp_path / "s.csv")
                              .read_text(encoding="utf-8-sig").splitlines()))
    assert summary[0][3] == "Mon 2024-06-03" and summary[0][-1] == "Total" and len(summary[0]) == 3 + 7 + 1
    assert summary[-1][-1] == "1.50"

    html = ts.build_report_html(rows, date(2024, 6, 3), date(2024, 6, 9), "Board: B")
    assert "Tess - 1h" in html and "Bob - 30m" in html and "&lt;" not in html.split("<body>")[0]
    assert "No time was logged" in ts.build_report_html([], date(2024, 6, 3), date(2024, 6, 9), "x")


# ---------------------------------------------------------------------------
# JSON export / import
# ---------------------------------------------------------------------------
def test_json_roundtrip_time_assignee_and_team(env, tmp_path):
    mgr, service, _, _, task = env
    service.upsert_team_member("Ann", 20, [0, 2])
    service.update_task(task.id, assignee="Ann")
    service.add_time_entry(task.id, 2700, note="spike")
    service.start_time_entry(task.id)  # running: not exported
    dest = BackupManager(mgr).export_to_json(tmp_path / "x.json")
    data = json.loads(dest.read_text())
    exported_task = data["workspaces"][0]["boards"][0]["tasks"][0]
    assert exported_task["assignee"] == "Ann" and len(exported_task["time_entries"]) == 1

    target = DatabaseManager(tmp_path / "new" / "k.db")
    target.init_database(seed_defaults=False)
    BackupManager(target).import_from_json(dest)
    svc = DatabaseService(target)
    t = svc.get_tasks_for_board(svc.get_workspaces()[0].boards[0].id)[0]
    assert t.assignee == "Ann" and t.spent_hours == pytest.approx(0.75)
    assert svc.get_time_entries(task_id=t.id)[0]["note"] == "spike"
    assert svc.get_team_members()[0]["working_days"] == [0, 2]


def test_legacy_export_spent_hours_become_an_entry(env, tmp_path):
    _, service, *_ = env
    legacy = {"version": "1.1", "workspaces": [{"name": "Old", "boards": [{
        "name": "Old B", "columns": [{"name": "To Do"}], "labels": [],
        "tasks": [{"temp_id": 1, "column_name": "To Do", "title": "Has time", "spent_hours": 2.5}],
    }]}]}
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy))
    BackupManager(service.db).import_from_json(path)
    board = [b for ws in service.get_workspaces() for b in ws.boards if b.name == "Old B"][0]
    task = service.get_tasks_for_board(board.id)[0]
    assert task.spent_hours == pytest.approx(2.5)
    assert service.get_time_entries(task_id=task.id)[0]["duration_seconds"] == 9000

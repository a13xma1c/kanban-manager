"""
Tests for the working-day calendar, CPM analysis (all dependency types, lags,
milestones, locked/completed tasks, cycles), auto-scheduling, cascading date
shifts, baselines & variance, and the service/engine integration.
"""
from datetime import date, datetime, timedelta

import pytest

from kanbanmanager import DatabaseService
from kanbanmanager.core import scheduling as sch
from kanbanmanager.core.schedule_engine import CASCADE_ACTOR, ScheduleEngine
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.enums import ActivityAction as A, DependencyType

MON = date(2024, 6, 3)  # a Monday; the week of Jun 3-7 2024, weekend Jun 8-9


def d(offset: int) -> date:
    return MON + timedelta(days=offset)


@pytest.fixture
def cal():
    return sch.WorkCalendar([0, 1, 2, 3, 4], date(2024, 5, 1))


def task(cal, tid, start=None, finish=None, **kw):
    dur = kw.pop("duration", None)
    if dur is None:
        dur = 0 if kw.get("is_milestone") else (cal.span(start, finish) if start and finish else 1)
    return sch.SchedTask(tid, f"T{tid}", dur, start, finish, **kw)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
def test_calendar_round_trips_and_snaps(cal):
    for i in range(-80, 400):
        day = cal.day(i)
        assert cal.is_working(day) and cal.index_start(day) == i
    sat = d(5)
    assert cal.day(cal.index_start(sat)) == d(7), "a weekend start snaps to Monday"
    assert cal.day(cal.index_end(sat) - 1) == d(4), "a weekend finish snaps to Friday"
    assert cal.span(d(0), d(4)) == 5 and cal.span(d(0), d(7)) == 6
    assert cal.add_working_days(d(3), 3) == d(8)
    four_day = sch.WorkCalendar([0, 1, 2, 3], date(2024, 5, 1))
    assert four_day.add_working_days(d(3), 1) == d(7), "Friday is not a working day"


# ---------------------------------------------------------------------------
# CPM
# ---------------------------------------------------------------------------
def test_cpm_float_critical_path_and_conflicts(cal):
    a = task(cal, 1, d(0), d(1))                      # Mon-Tue
    b = task(cal, 2, d(3), d(4))                      # Thu-Fri (gap Wed -> A has 1 day float)
    c = task(cal, 3, d(1), d(1))                      # SS+1 after A, finishes early -> float
    m = task(cal, 4, d(4), d(4), is_milestone=True)   # milestone after B
    e = task(cal, 5, d(0), d(2))                      # FF with B, planned to finish too early
    deps = [sch.SchedDep(1, 2), sch.SchedDep(1, 3, sch.SS, 1), sch.SchedDep(2, 4), sch.SchedDep(2, 5, sch.FF)]
    r = sch.analyze([a, b, c, m, e], deps, cal)
    flt = {tid: r.tasks[tid].total_float for tid in r.tasks}
    assert flt == {1: 1, 2: 0, 3: 3, 4: 0, 5: 0}
    assert r.critical_ids == [2, 4, 5] or set(r.critical_ids) == {2, 4, 5}
    assert r.project_finish_date == d(4) and r.duration_days == 5
    assert r.start_date(4) == r.finish_date(4) == d(4), "milestones are a single date"
    assert r.start_date(5) == d(2), "forecast pushes the FF successor to finish with B"
    assert [(x.pred, x.succ, x.kind, x.days) for x in r.conflicts] == [(2, 5, sch.FF, 2)]
    assert "finishes before 'T2' finishes - 2 days too early" in r.conflicts[0].describe({2: "T2", 5: "T5"})


def test_milestones_sit_at_the_end_of_their_day(cal):
    """A milestone on Wednesday happens at the end of Wednesday: its FS successor can start Thursday,
    not Wednesday, and a standalone milestone keeps its planned date."""
    m = task(cal, 1, d(2), d(2), is_milestone=True)
    same_day = task(cal, 2, d(2), d(2))
    next_day = task(cal, 3, d(3), d(3))
    r = sch.analyze([m, same_day, next_day], [sch.SchedDep(1, 2), sch.SchedDep(1, 3)], cal)
    assert r.start_date(1) == r.finish_date(1) == d(2)
    assert [(c.succ, c.days) for c in r.conflicts] == [(2, 1)]
    assert r.start_date(2) == d(3), "the forecast moves the same-day successor to Thursday"


@pytest.mark.parametrize("kind,lag,expected_start", [
    (sch.FS, 0, d(3)),    # after A finishes Wed -> Thu
    (sch.FS, 2, d(7)),    # +2 working days -> Mon (weekend skipped)
    (sch.FS, -1, d(2)),   # lead: may start the day A finishes
    (sch.SS, 1, d(1)),    # a day after A starts
    (sch.FF, 0, d(1)),    # B (2 days) must finish with A on Wed -> starts Tue
    (sch.SF, 0, d(-4)),   # B can't finish before A starts (Mon): it ends Fri May 31, so starts Thu
])
def test_dependency_types_and_lags(cal, kind, lag, expected_start):
    a = task(cal, 1, d(0), d(2), locked=True)         # Mon-Wed, fixed
    b = task(cal, 2, duration=2)                      # unscheduled, 2 days
    proposal = sch.auto_schedule([a, b], [sch.SchedDep(1, 2, kind, lag)], cal, project_start=d(-10),
                                 keep_planned_starts=False)
    change = next(c for c in proposal.changes if c.task_id == 2)
    assert change.new_start == expected_start, (kind, lag)
    assert cal.span(change.new_start, change.new_finish) == 2


def test_locked_and_completed_tasks_are_fixed(cal):
    a = task(cal, 1, d(0), d(3))
    b = task(cal, 2, d(2), d(2), locked=True)            # locked, violates FS
    c = task(cal, 3, d(1), d(1), is_completed=True)      # completed, violates FS - history, no conflict
    r = sch.analyze([a, b, c], [sch.SchedDep(1, 2), sch.SchedDep(1, 3)], cal)
    assert r.start_date(2) == d(2) and r.tasks[2].driver == "locked"
    assert r.tasks[1].total_float < 0, "a locked successor makes its predecessor late (negative float)"
    assert [(x.succ, x.days) for x in r.conflicts] == [(2, 2)]
    proposal = sch.auto_schedule([a, b, c], [sch.SchedDep(1, 2), sch.SchedDep(1, 3)], cal)
    assert proposal.changes == [] and "locked" in proposal.blocked[0]


def test_cycles_are_reported_not_scheduled(cal):
    a, b, c = task(cal, 1, d(0), d(0)), task(cal, 2, d(1), d(1)), task(cal, 3, d(2), d(2))
    deps = [sch.SchedDep(1, 2), sch.SchedDep(2, 1), sch.SchedDep(2, 3)]
    r = sch.analyze([a, b, c], deps, cal)
    assert r.cycle_ids == [1, 2, 3] and r.tasks == {}
    assert any("cycle" in x for x in sch.auto_schedule([a, b, c], deps, cal).blocked)


def test_build_inputs_durations(cal):
    rows = [
        {"id": 1, "title": "both", "start_date": "2024-06-03", "due_date": "2024-06-05T17:00:00"},
        {"id": 2, "title": "estimate", "start_date": "2024-06-03", "estimated_hours": 20},
        {"id": 3, "title": "due only", "due_date": "2024-06-07T09:30:00", "estimated_hours": 12},
        {"id": 4, "title": "nothing"},
        {"id": 5, "title": "ms", "due_date": "2024-06-06T12:00:00", "is_milestone": True},
    ]
    deps = [{"task_id": 2, "depends_on_task_id": 1, "dependency_type": "BLOCKS", "lag_days": 1},
            {"task_id": 9, "depends_on_task_id": 1}]  # dangling (archived task): dropped
    tasks, sdeps = sch.build_inputs(rows, deps, {"hours_per_day": 8}, cal)
    by = {t.id: t for t in tasks}
    assert (by[1].duration, by[2].duration, by[3].duration, by[4].duration, by[5].duration) == (3, 3, 2, 1, 0)
    assert by[2].finish == d(2) and by[3].start == d(3) and by[3].due_time == (9, 30)
    assert not by[4].scheduled and by[5].start == by[5].finish == d(3)
    assert sdeps == [sch.SchedDep(1, 2, sch.FS, 1)], "legacy BLOCKS schedules as finish-to-start"


def test_normalize_settings():
    s = sch.normalize_schedule_settings({"working_days": [9, 1, 1], "hours_per_day": 0.1, "bogus": 1})
    assert s["working_days"] == [1] and s["hours_per_day"] == 0.5 and "bogus" not in s
    assert sch.normalize_schedule_settings({"hours_per_day": 0})["hours_per_day"] == 8.0, "0 = unset -> default"
    assert sch.normalize_schedule_settings({"working_days": []})["working_days"] == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Auto-schedule & cascade
# ---------------------------------------------------------------------------
def test_auto_schedule_modes(cal):
    a = task(cal, 1, d(0), d(1))
    b = task(cal, 2, d(7), d(8))             # planned later than needed
    c = task(cal, 3, duration=3)             # no dates
    deps = [sch.SchedDep(1, 2), sch.SchedDep(2, 3)]
    keep = sch.auto_schedule([a, b, c], deps, cal, project_start=d(0))
    assert [(x.task_id, x.new_start) for x in keep.changes] == [(3, d(9))], "B stays; C follows B"
    pack = sch.auto_schedule([a, b, c], deps, cal, project_start=d(0), keep_planned_starts=False)
    moved = {x.task_id: (x.new_start, x.shift_days) for x in pack.changes}
    assert moved[2] == (d(2), -3) and moved[3][0] == d(4)
    assert pack.project_finish == d(8)
    none = sch.auto_schedule([a, b, c], deps, cal, project_start=d(0), include_unscheduled=False)
    assert none.changes == []


def test_cascade_push_and_pull(cal):
    a = task(cal, 1, d(0), d(4))             # moved: now ends Fri
    b = task(cal, 2, d(2), d(3))             # FS: too early now
    c = task(cal, 3, d(7), d(7))             # FS after B: pushed transitively
    x = task(cal, 4, d(14), d(14))           # FS after A with lots of slack: untouched by push
    deps = [sch.SchedDep(1, 2), sch.SchedDep(2, 3), sch.SchedDep(1, 4)]
    push = sch.cascade([a, b, c, x], deps, cal, [1])
    assert [(ch.task_id, ch.new_start, ch.new_finish) for ch in push.changes] == [(2, d(7), d(8)), (3, d(9), d(9))]
    pull = sch.cascade([a, b, c, x], deps, cal, [1], pull=True)
    assert {ch.task_id: ch.new_start for ch in pull.changes}[4] == d(7), "pull closes the gap"
    locked = sch.cascade([a, task(cal, 2, d(2), d(3), locked=True), c], deps[:2], cal, [1])
    assert locked.changes == [] and "locked" in locked.blocked[0]


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------
def test_baseline_comparison(cal):
    planned = [task(cal, 1, d(0), d(1)), task(cal, 2, d(2), d(3)), task(cal, 3, d(4), d(4), is_milestone=True),
               task(cal, 9, d(0), d(0))]
    entries = sch.baseline_entries(planned)
    now = [task(cal, 1, d(0), d(2)), task(cal, 2, d(3), d(7)), task(cal, 3, d(4), d(4), is_milestone=True),
           task(cal, 5, d(1), d(1)), task(cal, 6)]
    cmp = sch.compare_baseline(entries, now, cal)
    status = {r.task_id: (r.status, r.start_var, r.finish_var) for r in cmp.rows}
    assert status[1] == ("late", 0, 1) and status[2] == ("late", 1, 2)
    assert status[3] == ("on_track", 0, 0) and status[5][0] == "new" and status[9][0] == "removed"
    assert 6 not in status, "unscheduled new tasks are not listed"
    assert cmp.finish_var == 1 and cmp.count("late") == 2
    assert [r.status for r in cmp.rows][:2] == ["late", "late"], "worst first"


# ---------------------------------------------------------------------------
# Service & engine integration
# ---------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path):
    mgr = DatabaseManager(tmp_path / "sched.db")
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr, actor="Ann")
    ws = service.create_workspace("W")
    board = service.create_board(ws.id, "B")
    col = service.get_columns_for_board(board.id)[1]
    notes = []
    engine = ScheduleEngine(service, notify=notes.append)
    engine.attach()
    return service, board, col, engine, notes


def dt(day: date, hour: int = 17) -> datetime:
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


def test_cascade_on_date_change_keeps_times_and_is_audited(env):
    service, board, col, engine, notes = env
    design = service.create_task(board.id, col.id, "Design", start_date=d(0), due_date=dt(d(1)))
    build = service.create_task(board.id, col.id, "Build", start_date=d(2), due_date=dt(d(4), 12))
    ship = service.create_task(board.id, col.id, "Ship", start_date=d(7), due_date=dt(d(7), 9), is_milestone=True)
    service.add_dependency(build.id, design.id)
    service.add_dependency(ship.id, build.id)
    service.update_task(design.id, due_date=dt(d(3)))  # slips 2 days
    b, s = service.get_task(build.id), service.get_task(ship.id)
    assert (b.start_date, b.due_date) == (d(4), dt(d(8), 12)), "duration and 12:00 due time kept"
    # a milestone sits at the end of its day: right after Build finishes on Tuesday
    assert (s.start_date, s.due_date) == (d(8), dt(d(8), 9))
    assert notes == ["Shifted 2 dependent tasks: 'Build', 'Ship'"]
    assert {e["actor"] for e in service.get_task_activity(build.id)[-2:]} == {CASCADE_ACTOR}


def test_cascade_respects_settings_and_new_dependencies(env):
    service, board, col, engine, notes = env
    a = service.create_task(board.id, col.id, "A", start_date=d(0), due_date=dt(d(2)))
    b = service.create_task(board.id, col.id, "B", start_date=d(1), due_date=dt(d(1)))
    service.add_dependency(b.id, a.id)  # new dependency that B violates -> pushed
    assert service.get_task(b.id).start_date == d(3)
    service.set_schedule_settings(board.id, {"cascade": False})
    service.update_task(a.id, due_date=dt(d(7)))
    assert service.get_task(b.id).start_date == d(3), "cascading disabled"
    service.set_schedule_settings(board.id, {"cascade": True, "cascade_pull": True})
    service.update_task(a.id, due_date=dt(d(0)), start_date=d(0))
    assert service.get_task(b.id).start_date == d(1), "pulled back next to A"


def test_update_dependency_type_and_lag(env):
    service, board, col, engine, notes = env
    a = service.create_task(board.id, col.id, "A", start_date=d(0), due_date=dt(d(1)))
    b = service.create_task(board.id, col.id, "B", start_date=d(2), due_date=dt(d(2)))
    service.add_dependency(b.id, a.id)
    assert service.update_dependency(b.id, a.id, lag_days=2)
    assert service.get_task(b.id).start_date == d(4), "lag change cascades"
    assert service.update_dependency(b.id, a.id, dependency_type=DependencyType.START_TO_START.value)
    deps = service.get_board_dependencies(board.id)
    assert (deps[0]["dependency_type"], deps[0]["lag_days"]) == ("START_TO_START", 2)
    log = [e["details"] for e in service.get_task_activity(b.id) if e["action_type"] == A.DEPENDENCY_UPDATED.value]
    assert log == ["Changed the dependency on 'A' from finish-to-start to finish-to-start +2d",
                   "Changed the dependency on 'A' from finish-to-start +2d to start-to-start +2d"]
    assert service.get_task(b.id).is_blocked is False, "start-to-start does not block starting"
    with pytest.raises(ValueError):
        service.update_dependency(b.id, a.id, dependency_type="SIDEWAYS")


def test_engine_auto_schedule_apply_and_baselines(env):
    service, board, col, engine, notes = env
    a = service.create_task(board.id, col.id, "A", start_date=d(0), due_date=dt(d(1)))
    b = service.create_task(board.id, col.id, "B", estimated_hours=16)
    locked = service.create_task(board.id, col.id, "Launch", start_date=d(2), due_date=dt(d(2)),
                                 is_milestone=True, dates_locked=True)
    service.add_dependency(b.id, a.id)
    baseline = engine.save_baseline(board.id, "v1")
    assert baseline["task_count"] == 2, "B had no dates yet"

    service.set_schedule_settings(board.id, {"project_start": d(0).isoformat()})
    proposal = engine.auto_schedule(board.id)
    assert [(c.title, c.new_start, c.new_finish) for c in proposal.changes] == [("B", d(2), d(3))]
    assert engine.apply(board.id, proposal.changes) == 1
    assert service.get_task(b.id).start_date == d(2)
    assert service.get_task_activity(b.id)[-1]["actor"].endswith("Auto-schedule")

    service.add_dependency(locked.id, b.id)  # B finishes Thu, locked milestone is Wed -> conflict, can't move
    assert service.get_task(locked.id).due_date == dt(d(2)), "locked tasks never move"
    assert any("locked" in n for n in notes)

    cmp = engine.compare(board.id, baseline["id"])
    assert {r.title: r.status for r in cmp.rows} == {"A": "on_track", "Launch": "on_track", "B": "new"}
    assert service.rename_baseline(baseline["id"], "Kick-off") and service.get_baselines(board.id)[0]["name"] == "Kick-off"
    service.set_schedule_settings(board.id, {"baseline_id": baseline["id"]})
    assert service.delete_baseline(baseline["id"])
    assert service.get_schedule_settings(board.id)["baseline_id"] is None, "shown baseline reference cleared"


def test_milestone_and_lock_flags_are_audited_and_exposed(env):
    service, board, col, engine, notes = env
    t = service.create_task(board.id, col.id, "Release", due_date=dt(d(4)))
    service.update_task(t.id, is_milestone=True, dates_locked=True)
    details = [e["details"] for e in service.get_task_activity(t.id)][-2:]
    assert details == ["Marked as a milestone", "Locked its dates (auto-scheduling won't move it)"]
    data = service.get_task(t.id).to_dict()
    assert data["is_milestone"] and data["dates_locked"]


def test_legacy_database_gets_scheduling_columns(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    mgr = DatabaseManager(db)
    mgr.init_database(seed_defaults=False)
    s = DatabaseService(mgr)
    ws = s.create_workspace("W")
    b = s.create_board(ws.id, "B")
    c = s.get_columns_for_board(b.id)[0]
    t1, t2 = s.create_task(b.id, c.id, "one"), s.create_task(b.id, c.id, "two")
    s.add_dependency(t2.id, t1.id)
    mgr.engine.dispose()
    con = sqlite3.connect(db)
    con.executescript("""
        DROP TABLE baseline_entries; DROP TABLE project_baselines;
        ALTER TABLE tasks DROP COLUMN is_milestone; ALTER TABLE tasks DROP COLUMN dates_locked;
        ALTER TABLE boards DROP COLUMN schedule_json; ALTER TABLE task_dependencies DROP COLUMN lag_days;
    """)
    con.commit()
    con.close()
    upgraded = DatabaseManager(db)
    upgraded.init_database(seed_defaults=False)
    s = DatabaseService(upgraded)
    assert s.get_board_dependencies(b.id)[0]["lag_days"] == 0
    assert s.get_task(t1.id).to_dict()["is_milestone"] is False and s.get_baselines(b.id) == []

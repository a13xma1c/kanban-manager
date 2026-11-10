"""
Tests for the workflow automation engine: event triggers, conditions, actions,
loop protection, scheduled triggers, auto-archiving & stale-task policies,
archive behaviour, portability, and the demo content.
"""
import json
from datetime import datetime, timedelta

import pytest

from kanbanmanager import BackupManager, DatabaseService
from kanbanmanager.core import automation as au
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.database.models import utc_now
from kanbanmanager.enums import ActivityAction as A
from support import seeded_manager


class Clock:
    """Shifts both the UTC and local clocks the engine uses."""

    def __init__(self):
        self.offset = timedelta(0)

    def utc(self):
        return utc_now() + self.offset

    def local(self):
        return datetime.now() + self.offset


@pytest.fixture
def env(tmp_path):
    mgr = DatabaseManager(tmp_path / "auto.db")
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr, actor="Ann")
    ws = service.create_workspace("W")
    board = service.create_board(ws.id, "B")
    cols = service.get_columns_for_board(board.id)  # Backlog, To Do, In Progress, In Review, Done
    labels = {l.name: l.id for l in service.get_labels_for_board(board.id)}
    clock = Clock()
    notes = []
    engine = au.AutomationEngine(service, notify=notes.append, now=clock.utc, local_now=clock.local)
    engine.attach()
    return service, board, cols, labels, engine, clock, notes


def rule(service, board, trigger, actions, conditions=None, name="Rule"):
    return service.save_automation_rule(board.id, {"name": name, "trigger": trigger, "actions": actions,
                                                   "conditions": conditions or []})


def history(service, task_id):
    return [(e["actor"], e["details"]) for e in service.get_task_activity(task_id)]


# ---------------------------------------------------------------------------
# Events & actions
# ---------------------------------------------------------------------------
def test_move_trigger_runs_actions_and_is_audited(env):
    service, board, cols, labels, engine, _, notes = env
    rule(service, board, {"type": "task_moved", "to_column_id": cols[3].id}, [
        {"type": "add_comment", "text": "{actor} moved '{title}' to {column}"},
        {"type": "assign", "person": au.ACTOR_FOR_TRIGGER},
        {"type": "add_label", "label_id": labels["Testing"]},
        {"type": "notify", "text": "{title} needs review"},
    ], name="Review")
    t = service.create_task(board.id, cols[2].id, "Login page")
    service.move_task(t.id, cols[1].id)  # other column: no match
    assert service.get_comments(t.id) == []
    service.move_task(t.id, cols[3].id)

    task = service.get_task(t.id)
    assert task.assignee == "Ann" and [l.name for l in task.labels] == ["Testing"]
    assert service.get_comments(t.id)[0]["body"] == "Ann moved 'Login page' to In Review"
    assert service.get_comments(t.id)[0]["author"] == "⚙ Automation: Review"
    assert notes == ["Login page needs review"]
    actors = {a for a, _ in history(service, t.id)}
    assert "⚙ Automation: Review" in actors
    run = service.get_automation_runs(board.id)[0]
    assert run["status"] == "success" and run["rule_name"] == "Review" and "posted a comment" in run["message"]
    assert service.get_automation_rules(board.id)[0]["run_count"] == 1


def test_created_completed_priority_label_comment_and_time_triggers(env):
    service, board, cols, labels, engine, _, notes = env
    rule(service, board, {"type": "task_created", "column_id": cols[0].id}, [{"type": "set_priority",
                                                                             "priority": "LOW"}])
    rule(service, board, {"type": "task_completed"}, [{"type": "notify", "text": "done: {title}"}])
    rule(service, board, {"type": "priority_changed", "to_priority": "URGENT"}, [{"type": "notify", "text": "urgent"}])
    rule(service, board, {"type": "label_added", "label_id": labels["Bug"]},
         [{"type": "add_checklist", "items": "Reproduce\nFix"}])
    rule(service, board, {"type": "comment_added", "contains": "blocked"}, [{"type": "notify", "text": "help!"}])
    rule(service, board, {"type": "time_logged"}, [{"type": "move_to_column", "column_id": cols[2].id}],
         conditions=[{"field": "column", "op": "is", "value": cols[1].id}])

    a = service.create_task(board.id, cols[0].id, "In backlog")
    b = service.create_task(board.id, cols[1].id, "In todo")
    assert service.get_task(a.id).priority == "LOW" and service.get_task(b.id).priority == "MEDIUM"

    service.update_task(b.id, label_ids=[labels["Bug"]])
    service.update_task(b.id, label_ids=[labels["Bug"]])  # no change -> no second run
    assert [i["text"] for i in service.get_task(b.id).checklist] == ["Reproduce", "Fix"]

    service.update_task(b.id, priority="URGENT")
    service.add_comment(b.id, "I'm blocked on the API")
    service.add_comment(b.id, "all good")
    service.add_time_entry(b.id, 600)
    assert service.get_task(b.id).column_id == cols[2].id, "time logged in To Do starts work"

    service.move_task(b.id, cols[4].id)  # completing by moving into Done counts
    assert notes == ["urgent", "help!", "done: In todo"]


def test_conditions(env):
    service, board, cols, labels, engine, _, notes = env
    rule(service, board, {"type": "task_completed"}, [{"type": "notify", "text": "{title}"}], conditions=[
        {"field": "priority", "op": "at_least", "value": "HIGH"},
        {"field": "label", "op": "lacks", "value": labels["Design"]},
        {"field": "assignee", "op": "is_set"},
        {"field": "title", "op": "not_contains", "value": "spike"},
        {"field": "due_date", "op": "is_empty"},
        {"field": "blocked", "op": "is_false"},
    ])
    def done(title, **kw):
        t = service.create_task(board.id, cols[1].id, title, **kw)
        service.toggle_task_completion(t.id)
    done("match", priority="URGENT", assignee="Ann")
    done("low priority", priority="LOW", assignee="Ann")
    done("unassigned", priority="HIGH")
    done("research spike", priority="HIGH", assignee="Ann")
    done("designed", priority="HIGH", assignee="Ann", label_ids=[labels["Design"]])
    done("has due", priority="HIGH", assignee="Ann", due_date=datetime.now())
    assert notes == ["match"]


def test_failed_actions_are_reported_not_raised(env):
    service, board, cols, labels, engine, _, _ = env
    service.update_column(cols[2].id, wip_limit=1)
    service.create_task(board.id, cols[2].id, "Already in progress")
    rule(service, board, {"type": "task_created", "column_id": cols[1].id}, [
        {"type": "move_to_column", "column_id": cols[2].id},  # WIP limit reached
        {"type": "add_comment", "text": "hello"},
        {"type": "add_label", "label_id": 999999},            # deleted label
    ], name="Partly broken")
    t = service.create_task(board.id, cols[1].id, "New")  # must not raise
    run = service.get_automation_runs(board.id)[0]
    assert run["status"] == "partial"
    assert "WIP limit" in run["message"] and "label no longer exists" in run["message"]
    assert service.get_task(t.id).column_id == cols[1].id and len(service.get_comments(t.id)) == 1


def test_rules_triggering_each_other_stop_instead_of_looping(env):
    service, board, cols, labels, engine, _, _ = env
    rule(service, board, {"type": "task_moved", "to_column_id": cols[2].id},
         [{"type": "move_to_column", "column_id": cols[3].id}], name="Ping")
    rule(service, board, {"type": "task_moved", "to_column_id": cols[3].id},
         [{"type": "move_to_column", "column_id": cols[2].id}], name="Pong")
    t = service.create_task(board.id, cols[1].id, "Hot potato")
    service.move_task(t.id, cols[2].id)  # must terminate
    runs = service.get_automation_runs(board.id)
    assert [r["status"] for r in runs].count("skipped") >= 1
    assert sum(r["status"] == "success" for r in runs) == 2, "each rule acted once in the chain"
    # The next user action starts a fresh chain
    service.move_task(t.id, cols[1].id)
    service.move_task(t.id, cols[2].id)
    assert sum(r["status"] == "success" for r in service.get_automation_runs(board.id)) == 4


def test_disabled_rules_and_paused_activity_do_not_fire(env):
    service, board, cols, labels, engine, _, notes = env
    r = rule(service, board, {"type": "task_created"}, [{"type": "notify", "text": "x"}])
    service.set_automation_rule_enabled(r["id"], False)
    service.create_task(board.id, cols[0].id, "quiet")
    service.set_automation_rule_enabled(r["id"], True)
    with service.activity_paused():  # imports / internal fix-ups publish no events
        service.create_task(board.id, cols[0].id, "imported")
    assert notes == []
    service.create_task(board.id, cols[0].id, "loud")
    assert notes == ["x"]


def test_over_estimate_fires_once_per_task(env):
    service, board, cols, labels, engine, _, notes = env
    rule(service, board, {"type": "over_estimate"}, [{"type": "notify", "text": "{title} over"}])
    t = service.create_task(board.id, cols[2].id, "Sized", estimated_hours=1)
    service.add_time_entry(t.id, 1800)
    assert notes == []
    service.add_time_entry(t.id, 2400)
    service.add_time_entry(t.id, 600)
    assert notes == ["Sized over"]


# ---------------------------------------------------------------------------
# Scheduled rules & policies
# ---------------------------------------------------------------------------
def test_due_soon_and_overdue_fire_once_per_due_date(env):
    service, board, cols, labels, engine, clock, notes = env
    rule(service, board, {"type": "due_soon", "hours": 24}, [{"type": "notify", "text": "soon: {title}"}])
    rule(service, board, {"type": "overdue"}, [{"type": "set_priority", "priority": "URGENT"}])
    t = service.create_task(board.id, cols[1].id, "Report", due_date=datetime.now() + timedelta(hours=30))
    engine.run_scheduled()
    assert notes == []
    clock.offset = timedelta(hours=10)
    engine.run_scheduled()
    engine.run_scheduled()
    assert notes == ["soon: Report"]
    clock.offset = timedelta(hours=40)
    summary = engine.run_scheduled()
    assert summary.rules_fired == 1 and service.get_task(t.id).priority == "URGENT"
    # A new due date is a new situation
    service.update_task(t.id, due_date=datetime.now() + timedelta(hours=50))
    clock.offset = timedelta(hours=40)
    engine.run_scheduled()
    assert notes == ["soon: Report", "soon: Report"]


def test_auto_archive_and_stale_policies(env):
    service, board, cols, labels, engine, clock, notes = env
    service.set_board_policies(board.id, {
        "auto_archive": {"enabled": True, "days": 14},
        "stale": {"enabled": True, "days": 7, "label": "Stale", "comment": True, "notify": True,
                  "move_to_column_id": cols[0].id, "exclude_column_ids": [cols[3].id]},
        "archive_stale": {"enabled": True, "days": 30},
    })
    idle = service.create_task(board.id, cols[1].id, "Idle")
    busy = service.create_task(board.id, cols[1].id, "Busy")
    in_review = service.create_task(board.id, cols[3].id, "Waiting on review")
    done = service.create_task(board.id, cols[4].id, "Shipped")

    clock.offset = timedelta(days=8)
    service.add_comment(busy.id, "still on it")  # real activity (now-ish, i.e. 8 days before the clock)
    summary = engine.run_scheduled()
    assert summary.flagged_stale == 2 and summary.archived == 0, "idle + busy (comment was 8 days 'ago')"
    idle_task = service.get_task(idle.id)
    assert [l.name for l in idle_task.labels] == ["Stale"] and idle_task.column_id == cols[0].id
    assert "No activity for 8 days" in service.get_comments(idle.id)[-1]["body"]
    assert notes and "2 stale tasks" in notes[-1]
    assert not service.get_task(in_review.id).labels, "excluded column"
    assert engine.run_scheduled().flagged_stale == 0, "flagged once per stale period"

    clock.offset = timedelta(days=15)
    assert engine.run_scheduled().archived == 1
    assert service.get_task(done.id).is_archived

    clock.offset = timedelta(days=31)
    summary = engine.run_scheduled()
    # The stale flag (label/comment/move by automation) must not count as activity
    archived = {t["title"]: t["archive_reason"] for t in service.get_archived_tasks(board.id)}
    assert {"Idle", "Busy", "Waiting on review"} <= set(archived)
    assert archived["Idle"].startswith("Auto-archived: no activity for 31 days")
    runs = {r["source"] for r in service.get_automation_runs(board.id)}
    assert {"stale", "auto_archive", "archive_stale"} <= runs


def test_normalize_policies():
    p = au.normalize_policies({"stale": {"enabled": 1, "days": 0, "bogus": True}})
    assert p["stale"]["enabled"] is True and p["stale"]["days"] == 14 and "bogus" not in p["stale"]
    assert p["auto_archive"] == au.DEFAULT_POLICIES["auto_archive"]


# ---------------------------------------------------------------------------
# Archive semantics
# ---------------------------------------------------------------------------
def test_archived_tasks_leave_views_wip_and_positions(env):
    service, board, cols, labels, engine, _, _ = env
    service.update_column(cols[2].id, wip_limit=1)
    a = service.create_task(board.id, cols[2].id, "A")
    b = service.create_task(board.id, cols[1].id, "B")
    ok, err = service.move_task(b.id, cols[2].id)
    assert not ok and "WIP" in err
    assert service.archive_task(a.id, reason="done elsewhere")
    assert not service.archive_task(a.id), "already archived"
    assert [t.title for t in service.get_tasks_for_board(board.id)] == ["B"]
    assert len(service.get_tasks_for_board(board.id, include_archived=True)) == 2
    ok, _ = service.move_task(b.id, cols[2].id)
    assert ok, "archived tasks don't count towards WIP limits"
    assert service.count_archived_tasks(board.id) == 1

    assert service.restore_task(a.id)
    restored = service.get_task(a.id)
    assert not restored.is_archived and restored.position == 1, "restored at the bottom of its column"
    assert [e["action_type"] for e in service.get_task_activity(a.id)][-2:] == \
        [A.TASK_ARCHIVED.value, A.TASK_RESTORED.value]


def test_reminders_skip_archived_tasks(env):
    service, board, cols, *_ = env
    t = service.create_task(board.id, cols[1].id, "R", due_date=datetime.now())
    service.create_reminder(t.id, datetime.now() - timedelta(minutes=1))
    assert len(service.get_pending_reminders()) == 1
    service.archive_task(t.id)
    assert service.get_pending_reminders() == []


# ---------------------------------------------------------------------------
# Rule text, validation, templates, portability
# ---------------------------------------------------------------------------
def test_describe_and_validate(env):
    service, board, cols, labels, engine, _, _ = env
    ctx = engine.board_context(board.id)
    data = {"name": "x", "trigger": {"type": "task_moved", "to_column_id": cols[4].id},
            "conditions": [{"field": "priority", "op": "at_least", "value": "HIGH"}],
            "actions": [{"type": "add_label", "label_id": labels["Bug"]}, {"type": "assign", "person": ""}]}
    assert au.describe_rule(data, ctx) == ("When a task is moved to 'Done', if priority is at least High "
                                          "→ add label 'Bug', assign to")
    problems = au.validate_rule(data, ctx)
    assert problems == ["Action 2 (assign to): 'person' is required"]
    data["actions"][0]["label_id"] = 424242
    assert "the label no longer exists" in " ".join(au.validate_rule(data, ctx))
    with pytest.raises(ValueError):
        service.save_automation_rule(board.id, {"name": "", "trigger": {"type": "overdue"}, "actions": [{}]})


def test_every_template_instantiates_to_a_valid_rule(env):
    service, board, cols, labels, engine, _, _ = env
    ctx_before = engine.board_context(board.id)
    for template in au.RULE_TEMPLATES:
        data = au.instantiate_template(template, service, board.id)
        ctx = engine.board_context(board.id)  # templates may create labels
        assert au.validate_rule(data, ctx) == [], template["name"]
        service.save_automation_rule(board.id, data)
    assert "Over estimate" in engine.board_context(board.id).labels.values()
    assert len(ctx_before.labels) < len(engine.board_context(board.id).labels)


def test_export_import_remaps_rule_references(env, tmp_path):
    service, board, cols, labels, engine, _, _ = env
    rule(service, board, {"type": "task_moved", "to_column_id": cols[3].id},
         [{"type": "add_label", "label_id": labels["Bug"]}],
         conditions=[{"field": "column", "op": "is_not", "value": cols[0].id}], name="Portable")
    service.set_board_policies(board.id, {"stale": {"enabled": True, "days": 9, "move_to_column_id": cols[0].id,
                                                    "exclude_column_ids": [cols[3].id]}})
    t = service.create_task(board.id, cols[1].id, "Gone")
    service.archive_task(t.id, reason="old")
    dest = BackupManager(service.db).export_to_json(tmp_path / "x.json")
    assert json.loads(dest.read_text())["workspaces"][0]["boards"][0]["automation_rules"][0]["trigger"] \
        == {"type": "task_moved", "to_column_id": "In Review"}

    target = DatabaseManager(tmp_path / "new" / "k.db")
    target.init_database(seed_defaults=False)
    BackupManager(target).import_from_json(dest)
    svc = DatabaseService(target)
    nb = svc.get_workspaces()[0].boards[0]
    new_cols = {c.name: c.id for c in svc.get_columns_for_board(nb.id)}
    new_labels = {l.name: l.id for l in svc.get_labels_for_board(nb.id)}
    r = svc.get_automation_rules(nb.id)[0]
    assert r["trigger"]["to_column_id"] == new_cols["In Review"]
    assert r["actions"][0]["label_id"] == new_labels["Bug"]
    assert r["conditions"][0]["value"] == new_cols["Backlog"]
    pol = au.normalize_policies(svc.get_board_policies(nb.id))["stale"]
    assert pol["days"] == 9 and pol["move_to_column_id"] == new_cols["Backlog"]
    assert pol["exclude_column_ids"] == [new_cols["In Review"]]
    archived = svc.get_archived_tasks(nb.id)
    assert [a["title"] for a in archived] == ["Gone"] and archived[0]["archive_reason"] == "old"


def test_legacy_database_gets_archive_and_policy_columns(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    mgr = DatabaseManager(db)
    mgr.init_database(seed_defaults=False)
    s = DatabaseService(mgr)
    ws = s.create_workspace("W")
    b = s.create_board(ws.id, "B")
    s.create_task(b.id, s.get_columns_for_board(b.id)[0].id, "Old task")
    mgr.engine.dispose()
    con = sqlite3.connect(db)
    con.executescript("""
        DROP TABLE automation_runs; DROP TABLE automation_rules;
        DROP INDEX idx_tasks_board;  -- that release had no such index (it covers is_archived)
        ALTER TABLE tasks DROP COLUMN is_archived; ALTER TABLE tasks DROP COLUMN archived_at;
        ALTER TABLE tasks DROP COLUMN archive_reason; ALTER TABLE boards DROP COLUMN policies_json;
    """)
    con.commit()
    con.close()
    upgraded = DatabaseManager(db)
    upgraded.init_database(seed_defaults=False)
    s = DatabaseService(upgraded)
    assert [t.title for t in s.get_tasks_for_board(b.id)] == ["Old task"]
    assert s.get_board_policies(b.id) == {} and s.get_automation_rules(b.id) == []


# ---------------------------------------------------------------------------
# Demo board
# ---------------------------------------------------------------------------
def test_demo_board_automation_showcase(tmp_path):
    mgr = seeded_manager(tmp_path / "demo.db")
    service = DatabaseService(mgr)
    board = [b for b in service.get_workspaces()[0].boards if b.name == "Feature Tour"][0]
    engine = au.AutomationEngine(service)
    engine.attach()
    ctx = engine.board_context(board.id)
    rules = service.get_automation_rules(board.id)
    assert len(rules) == 4 and all(au.validate_rule(r, ctx) == [] for r in rules)
    assert au.normalize_policies(service.get_board_policies(board.id))["auto_archive"]["enabled"]

    summary = engine.run_scheduled()
    assert summary.archived == 1 and summary.flagged_stale >= 1
    assert [t["title"] for t in service.get_archived_tasks(board.id)] == ["Set up the CI pipeline"]
    charts = [t for t in service.get_tasks_for_board(board.id) if t.title.startswith("Evaluate charting")][0]
    assert "Stale" in [l.name for l in charts.labels]
    assert engine.run_scheduled().changed is False, "nothing left to do on a second run"

    # The 'Ask for review' rule fires when the user moves something to In Review
    crash = [t for t in service.get_tasks_for_board(board.id) if t.title.startswith("Crash")][0]
    review = next(c for c in service.get_columns_for_board(board.id) if c.name == "In Review")
    service.update_column(review.id, wip_limit=0)
    service.move_task(crash.id, review.id)
    assert service.get_comments(crash.id)[-1]["body"].startswith("👀")

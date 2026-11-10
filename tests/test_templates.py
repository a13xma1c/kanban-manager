"""
Tests for board and task templates: the task template format, placeholders and
date offsets, creating tasks from templates, scopes, the built-in board
templates, saving a board as a template (and back), template files, and the
demo board's fields and templates.
"""
import json
from datetime import date, datetime

import pytest

from kanbanmanager import DatabaseService
from kanbanmanager.core import automation as au
from kanbanmanager.core import custom_fields as cf
from kanbanmanager.core import templates as tpl
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.database.models import TaskTemplate
from support import seeded_manager

MONDAY = datetime(2031, 1, 6, 9, 30)  # a Monday, ISO week 2


@pytest.fixture
def env(tmp_path):
    mgr = DatabaseManager(tmp_path / "tpl.db")
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr, actor="Ann")
    ws = service.create_workspace("W")
    board = service.create_board(ws.id, "Team")
    cols = service.get_columns_for_board(board.id)
    return service, ws, board, cols


def test_placeholders_and_working_day_offsets():
    values = tpl.placeholder_values(MONDAY, "Ann", "Team")
    assert tpl.expand_placeholders("Report {date} ({weekday}, week {week}, {month} {year}) by {user} on {board} {x}",
                                   values) == \
        "Report 2031-01-06 (Monday, week 2, January 2031) by Ann on Team {x}"
    assert values["today"] == "Jan 6"
    friday = date(2031, 1, 10)
    assert tpl.add_days(friday, 1) == date(2031, 1, 13), "skips the weekend"
    assert tpl.add_days(friday, 1, working_days=False) == date(2031, 1, 11)
    assert tpl.add_days(date(2031, 1, 11), 0) == date(2031, 1, 13), "day 0 on a Saturday = Monday"
    assert tpl.add_days(date(2031, 1, 13), -1) == friday


def test_normalize_and_describe_task_templates():
    d = tpl.normalize_task_template({"title": "X", "priority": "BOGUS", "labels": ["Bug", "bug", " "],
                                     "checklist": ["a", {"text": "b"}, ""], "due_in_days": "3", "due_time": "25:00",
                                     "estimated_hours": "-2", "fields": {"Sev": "High", "Empty": ""}})
    assert d["priority"] is None and d["labels"] == ["Bug"] and d["checklist"] == ["a", "b"]
    assert d["due_in_days"] == 3 and d["due_time"] == "17:00" and d["estimated_hours"] == 0
    assert d["fields"] == {"Sev": "High"} and d["working_days"] is True
    assert tpl.describe_task_template({"priority": "HIGH", "labels": ["Bug"], "checklist": ["a", "b"],
                                       "assignee": "@me", "due_in_days": 2}) == \
        "High · Bug · 2 checklist items · assigned to you · due in 2 working days"
    assert tpl.describe_task_template({}) == "Empty template"


def test_creating_tasks_from_templates(env):
    service, _ws, board, cols = env
    service.create_custom_field(board.id, "Severity", "select", {"options": ["Critical", "Minor"]}, default="Minor")
    service.create_custom_field(board.id, "Reporter", "person")
    data = {"title": "Status - week {week}", "description": "By {user} on {today}", "priority": "HIGH",
            "labels": ["Bug", "Brand new"], "checklist": ["Draft for {board}", "Send"], "estimated_hours": 1.5,
            "assignee": "@me", "due_in_days": 2, "due_time": "10:30", "start_in_days": 0, "column": "to do",
            "fields": {"severity": "critical", "Reporter": "{user}", "Missing": 1}}
    saved = service.save_task_template("Status", data, board_id=board.id, icon="📝")
    task = service.create_task_from_template(saved["id"], board.id, now=MONDAY)
    assert task.title == "Status - week 2" and task.description == "By Ann on Jan 6"
    assert task.priority == "HIGH" and task.estimated_hours == 1.5 and task.assignee == "Ann"
    assert task.due_date == datetime(2031, 1, 8, 10, 30) and task.start_date == date(2031, 1, 6)
    assert task.column_id == cols[1].id, "column by name, case-insensitive"
    assert [i["text"] for i in task.checklist] == ["Draft for Team", "Send"]
    assert sorted(l.name for l in task.labels) == ["Brand new", "Bug"], "missing label created"
    assert task.custom_fields == {"Severity": "Critical", "Reporter": "Ann"}
    assert task.template_warnings == ["This board has no field 'Missing' (value skipped)"]
    first = service.get_task_activity(task.id)[0]["details"]
    assert first == "Created task 'Status - week 2' in column 'To Do' (from template 'Status')"

    # Column given explicitly wins; an empty template still gets defaults and a title
    blank = service.create_task_from_template({"name": "Blank", "data": {}}, board.id, column_id=cols[3].id)
    assert blank.title == "Untitled task" and blank.column_id == cols[3].id
    assert blank.custom_fields == {"Severity": "Minor"}
    # A value that no longer fits is skipped with a warning rather than failing
    odd = service.create_task_from_template({"name": "Odd", "data": {"fields": {"Severity": "Huge"}}}, board.id)
    assert odd.custom_fields == {"Severity": "Minor"} and "'Huge' is not an option" in odd.template_warnings[0]

    # Preview mode (the task editor) doesn't create labels
    kwargs, _ = tpl.expand_task_template(service, {"labels": ["Nope"], "title": "Bug: "}, board.id,
                                         create_labels=False)
    assert kwargs["new_labels"] == ["Nope"] and kwargs["raw_title"] == "Bug: " and kwargs["title"] == "Bug:"
    assert "Nope" not in [l.name for l in service.get_labels_for_board(board.id)]


def test_task_template_scopes_names_and_cleanup(env):
    service, ws, board, _cols = env
    other = service.create_board(ws.id, "Other")
    mine = service.save_task_template("Bug", {"title": "Bug: "}, board_id=board.id)
    glob = service.save_task_template("Weekly", {"title": "W"})
    service.save_task_template("Bug", {"title": "Other bug"}, board_id=other.id)  # same name elsewhere: fine
    with pytest.raises(ValueError, match="already a template named 'Bug' on this board"):
        service.save_task_template("bug", {}, board_id=board.id)
    with pytest.raises(ValueError, match="needs a name"):
        service.save_task_template("  ", {})
    assert [t["name"] for t in service.get_task_templates(board.id)] == ["Bug", "Weekly"], "board's own first"
    assert [t["name"] for t in service.get_task_templates(board.id, include_global=False)] == ["Bug"]
    assert [t["name"] for t in service.get_task_templates(None)] == ["Weekly"]
    updated = service.save_task_template("Bug report", {"title": "Bug - "}, board_id=board.id, template_id=mine["id"])
    assert updated["id"] == mine["id"] and updated["data"]["title"] == "Bug - "
    assert service.delete_task_template(glob["id"]) and not service.delete_task_template(glob["id"])
    service.delete_board(board.id)
    with service.db.session_scope() as s:
        assert [t.name for t in s.query(TaskTemplate)] == ["Bug"], "board templates go with their board"


def test_template_from_task_captures_content_not_history(env):
    service, _ws, board, cols = env
    service.create_custom_field(board.id, "Points", "number")
    task = service.create_task(board.id, cols[0].id, "Release notes", description="Hi ![x](attachment:7) there",
                               priority="LOW", checklist=[{"id": 1, "text": "Draft", "done": True}],
                               field_values={"Points": 2}, due_date=datetime(2031, 1, 1))
    data = service.task_template_from_task(task.id)
    assert data["title"] == "Release notes" and "attachment:" not in data["description"]
    assert data["checklist"] == ["Draft"] and data["fields"] == {"Points": 2} and data["due_in_days"] is None


def test_every_builtin_board_template_instantiates_cleanly(env):
    service, ws, _board, _cols = env
    builtins = tpl.builtin_board_templates()
    assert [b["id"] for b in builtins][:2] == ["builtin:standard", "builtin:simple"]
    for b in builtins:
        board = service.create_board_from_template(ws.id, b["id"])
        assert board.template_problems == [], b["name"]
        assert [c.name for c in board.columns] == [c["name"] for c in b["data"]["columns"]]
        assert any(c.is_done_column for c in board.columns), b["name"]
        fields = service.get_custom_fields(board.id)
        assert [f["name"] for f in fields] == [f["name"] for f in b["data"]["fields"]]
        assert all(cf.is_known_type(f["field_type"]) for f in fields)
        engine = au.AutomationEngine(service)
        ctx = engine.board_context(board.id)
        for rule in service.get_automation_rules(board.id):
            assert au.validate_rule(rule, ctx) == [], (b["name"], rule["name"])
        assert len(service.get_tasks_for_board(board.id)) == len(b["data"]["tasks"])
        assert len(service.get_task_templates(board.id, include_global=False)) == len(b["data"]["task_templates"])
        log = service.get_board_activities(board.id)
        assert any(a["details"] == f"Created from the '{b['name']}' template" for a in log)
    standard = service.create_board_from_template(ws.id, "builtin:standard", name="S")
    plain = service.create_board(ws.id, "Plain")
    assert [(c.name, c.wip_limit, c.column_type) for c in standard.columns] == \
        [(c.name, c.wip_limit, c.column_type) for c in plain.columns], "standard = a plain new board"
    assert sorted(l.name for l in standard.labels) == sorted(l.name for l in plain.labels)


def test_bug_tracker_template_escalates_critical_bugs(env):
    service, ws, _board, _cols = env
    engine = au.AutomationEngine(service)
    engine.attach()
    board = service.create_board_from_template(ws.id, "builtin:bugs", name="Bugs", include_sample_tasks=False)
    assert service.get_tasks_for_board(board.id) == []
    crash = next(t for t in service.get_task_templates(board.id) if t["name"] == "Crash report")
    task = service.create_task_from_template(crash, board.id, title="Crash: on save")
    assert task.custom_fields == {"Severity": "Critical", "Reported by": "Ann"}
    minor = service.create_task(board.id, board.columns[0].id, "Typo", field_values={"Severity": "Minor"})
    service.update_task(minor.id, field_values={"Severity": "Critical"})
    assert service.get_task(minor.id).priority == "URGENT"


def test_save_board_as_template_and_recreate(env):
    service, ws, board, cols = env
    service.update_column(cols[2].id, wip_limit=4)
    sev = service.create_custom_field(board.id, "Severity", "select", {"options": ["A", "B"]}, required=True)
    service.save_task_template("Bug", {"title": "Bug: ", "fields": {"Severity": "A"}}, board_id=board.id)
    tt = service.get_task_templates(board.id)[0]
    service.save_automation_rule(board.id, {
        "name": "R", "trigger": {"type": "field_changed", "field_id": sev["id"]},
        "actions": [{"type": "create_from_template", "template_id": tt["id"], "column_id": cols[3].id}]})
    service.set_board_policies(board.id, {"stale": {"enabled": True, "days": 5, "exclude_column_ids": [cols[4].id]}})
    service.set_schedule_settings(board.id, {"hours_per_day": 6, "baseline_id": 99, "cascade": False})
    service.create_task(board.id, cols[1].id, "Open one", field_values={"Severity": "B"},
                        description="See ![img](attachment:3)")
    done = service.create_task(board.id, cols[4].id, "Done one")
    assert done.is_completed

    saved = service.save_board_as_template(board.id, "Team setup", description="Our way", include_tasks=True)
    assert saved["builtin"] is False and saved["color"] == board.color
    data = saved["data"]
    assert [t["title"] for t in data["tasks"]] == ["Open one"], "open tasks only"
    assert "attachment:" not in data["tasks"][0]["description"]
    assert data["schedule_settings"] == {"hours_per_day": 6, "cascade": False}
    assert data["automation_rules"][0]["trigger"]["field_id"] == "Severity"

    new = service.create_board_from_template(ws.id, saved["id"], name="Copy")
    assert new.template_problems == []
    assert [(c.name, c.wip_limit, c.column_type) for c in new.columns] == \
        [(c.name, c.wip_limit, c.column_type) for c in service.get_columns_for_board(board.id)]
    nf = service.get_custom_fields(new.id)
    assert [(f["name"], f["required"]) for f in nf] == [("Severity", True)]
    rule = service.get_automation_rules(new.id)[0]
    ncols = {c.name: c.id for c in new.columns}
    ntt = service.get_task_templates(new.id, include_global=False)[0]
    assert rule["trigger"]["field_id"] == nf[0]["id"]
    assert rule["actions"][0] == {"type": "create_from_template", "template_id": ntt["id"],
                                  "column_id": ncols["In Review"]}
    stale = service.get_board_policies(new.id)["stale"]
    assert stale["exclude_column_ids"] == [ncols["Done"]]
    assert service.get_schedule_settings(new.id) == {"hours_per_day": 6, "cascade": False}
    assert {t.title: t.custom_fields for t in service.get_tasks_for_board(new.id)} == {"Open one": {"Severity": "B"}}

    lean = service.save_board_as_template(board.id, "Lean", include_tasks=False, include_automation=False)
    assert lean["data"]["tasks"] == [] and lean["data"]["automation_rules"] == [] and lean["data"]["policies"] == {}
    assert not service.delete_board_template("builtin:standard")
    assert service.delete_board_template(lean["id"])
    assert [t["name"] for t in service.get_board_templates() if not t["builtin"]] == ["Team setup"]


def test_template_files(env, tmp_path):
    service, ws, board, _cols = env
    service.create_custom_field(board.id, "Points", "number", {"show_total": True})
    saved = service.save_board_as_template(board.id, "Shared", description="For friends", icon="🎁")
    path = tpl.export_template_file(saved, tmp_path / f"shared{tpl.TEMPLATE_FILE_SUFFIX}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["format"] == tpl.TEMPLATE_FORMAT and raw["name"] == "Shared" and raw["icon"] == "🎁"
    back = tpl.read_template_file(path)
    assert back["data"] == saved["data"] and back["description"] == "For friends"
    board2 = service.create_board_from_template(ws.id, back, name="From file")
    assert [f["name"] for f in service.get_custom_fields(board2.id)] == ["Points"]

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"workspaces": []}))
    with pytest.raises(tpl.TemplateError, match="not a KanbanManager board template"):
        tpl.read_template_file(bad)
    raw["version"] = tpl.TEMPLATE_VERSION + 1
    bad.write_text(json.dumps(raw))
    with pytest.raises(tpl.TemplateError, match="newer version"):
        tpl.read_template_file(bad)
    bad.write_text("{not json")
    with pytest.raises(tpl.TemplateError, match="Could not read"):
        tpl.read_template_file(bad)
    with pytest.raises(tpl.TemplateError, match="at least one column"):
        tpl.normalize_board_template({"columns": []})


def test_unknown_field_types_in_templates_are_reported_not_fatal(env):
    service, ws, _board, _cols = env
    board = service.create_board_from_template(ws.id, {"name": "Odd", "data": {
        "columns": [{"name": "Only"}], "fields": [{"name": "Mood", "field_type": "emotion"},
                                                   {"name": "Ok", "field_type": "text"}]}})
    assert [f["name"] for f in service.get_custom_fields(board.id)] == ["Ok"]
    assert board.template_problems == ["field 'Mood' has an unknown type (emotion)"]


def test_demo_board_has_fields_values_and_templates(tmp_path):
    mgr = seeded_manager(tmp_path / "demo.db")
    service = DatabaseService(mgr)
    demo = next(b for ws in service.get_workspaces() for b in ws.boards if b.name == DEMO_BOARD_NAME)
    fields = {f["name"]: f for f in service.get_custom_fields(demo.id)}
    assert list(fields) == ["Story points", "Component", "Customer impact", "Spec", "Reviewer"]
    assert fields["Story points"]["config"]["show_total"] and not fields["Reviewer"]["show_on_card"]
    tasks = {t.title: t for t in service.get_tasks_for_board(demo.id)}
    onboarding = tasks["Design the new onboarding flow"]
    assert onboarding.custom_fields["Story points"] == 8 and onboarding.custom_fields["Customer impact"] == 4
    assert "Changed Story points from 5 pts to 8 pts" in [a["details"] for a in service.get_task_activity(onboarding.id)]
    names = [t["name"] for t in service.get_task_templates(demo.id)]
    assert names == ["Bug report", "Weekly status report"]
    status = service.create_task_from_template(service.get_task_templates(demo.id)[1], demo.id)
    assert status.title.startswith("Status report - week ") and status.assignee == service.actor
    assert status.custom_fields == {"Story points": 1, "Component": "Docs"}

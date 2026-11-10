"""
Tests for typed custom fields: the extensible type registry (validation, parsing,
formatting, filters, comparisons, conversion), the service API (definitions,
values, audit trail, events, search, option renames, type changes, deletion,
cross-board moves, recurrence), the legacy free-form migration, JSON
export/import (1.6) and automation (field triggers, conditions and actions).
"""
import json
import re
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from kanbanmanager import BackupManager, DatabaseService
from kanbanmanager.core import automation as au
from kanbanmanager.core import custom_fields as cf
from kanbanmanager.core.custom_fields import FieldType, FieldValueError
from kanbanmanager.core.recurrence import RecurrenceEngine
from kanbanmanager.database import events as ev
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.database.models import CustomField, Task, TaskFieldValue
from support import seeded_manager


@pytest.fixture
def env(tmp_path):
    mgr = DatabaseManager(tmp_path / "fields.db")
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr, actor="Ann")
    ws = service.create_workspace("W")
    board = service.create_board(ws.id, "B")
    cols = service.get_columns_for_board(board.id)  # Backlog, To Do, In Progress, In Review, Done
    return service, ws, board, cols


def t(key):
    return cf.get_field_type(key)


def history(service, task_id):
    return [a["details"] for a in service.get_task_activity(task_id)]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
def test_every_builtin_type_round_trips_through_text():
    samples = {
        "text": ("Acme Corp", {}), "number": (5, {"unit": "pts"}), "currency": (1200.5, {"symbol": "€"}),
        "percent": (45, {}), "select": ("High", {"options": ["High", "Low"]}),
        "multi_select": (["Web", "iOS"], {"options": ["Web", "iOS", "Android"]}), "checkbox": (True, {}),
        "date": ("2031-06-05", {}), "rating": (4, {"max": 5}), "person": ("Priya Sharma", {}),
        "url": ("https://example.com/spec", {}), "email": ("sam@example.com", {}),
    }
    assert {ft.key for ft in cf.field_types()} >= set(samples)
    for key, (value, config) in samples.items():
        ftype = t(key)
        config = ftype.normalize_config(config)
        assert ftype.validate(value, config) == value, key
        assert ftype.parse(ftype.to_text(value, config), config) == value, key
        assert ftype.format(value, config), key
        assert ftype.chips("F", value, config), key
        assert not ftype.is_empty(value) and ftype.is_empty(None), key


def test_number_currency_percent_rating_parsing_and_limits():
    num = t("number")
    cfg = num.normalize_config({"unit": "pts", "min": 0, "max": 100})
    assert num.parse("5 pts", cfg) == 5 and num.parse("  ", cfg) is None
    assert num.parse("1,234", {**cfg, "max": None}) == 1234
    with pytest.raises(FieldValueError, match="at most 100"):
        num.parse("101", cfg)
    with pytest.raises(FieldValueError, match="not a number"):
        num.parse("lots", cfg)
    assert num.validate(2.345, num.normalize_config({"decimals": 2})) == 2.35
    assert num.validate(2.6, cfg) == 3, "0 decimals stores an int"
    assert num.format(1500, cfg) == "1,500 pts"
    with pytest.raises(FieldValueError):
        num.validate(True, cfg)

    cur = t("currency")
    ccfg = cur.normalize_config({"symbol": "$"})
    assert cur.parse("$1,200.50", ccfg) == 1200.5
    assert cur.parse("-$5", ccfg) == -5.0 and cur.format(-5.0, ccfg) == "-$5.00"
    assert t("percent").parse("45%", {}) == 45
    with pytest.raises(FieldValueError):
        t("percent").parse("120", t("percent").normalize_config({}))

    rating = t("rating")
    rcfg = rating.normalize_config({"max": 5})
    assert rating.parse("★★★", rcfg) == 3 and rating.parse("4/5", rcfg) == 4 and rating.parse("0", rcfg) is None
    assert rating.format(2, rcfg) == "★★☆☆☆"
    with pytest.raises(FieldValueError, match="1 to 5"):
        rating.parse("6", rcfg)
    assert rating.normalize_config({"max": 50})["max"] == 10


def test_choice_checkbox_date_link_and_text_types():
    sel = t("select")
    cfg = sel.normalize_config({"options": ["High", {"label": "Low", "color": "#00ff00"}, "high", "", "A,B"]})
    assert [o["label"] for o in cfg["options"]] == ["High", "Low", "A B"], "deduped, commas removed"
    assert len({o["id"] for o in cfg["options"]}) == 3 and cfg["options"][1]["color"] == "#00FF00"
    assert sel.parse("high", cfg) == "High", "case-insensitive, canonical label"
    with pytest.raises(FieldValueError, match=re.escape("(High, Low, A B)")):
        sel.parse("Medium", cfg)

    multi = t("multi_select")
    mcfg = multi.normalize_config({"options": ["Web", "iOS", "Android"]})
    assert multi.parse("android, web", mcfg) == ["Web", "Android"], "kept in option order"
    assert multi.parse("", mcfg) is None and multi.validate([], mcfg) is None

    box = t("checkbox")
    assert box.parse("yes", {}) is True and box.parse("no", {}) is None and box.validate(False, {}) is None
    with pytest.raises(FieldValueError):
        box.parse("maybe", {})

    d = t("date")
    assert d.parse("2031-06-05", {}) == "2031-06-05"
    assert d.parse("tomorrow", {}) == (date.today() + timedelta(days=1)).isoformat()
    assert d.validate(datetime(2031, 1, 2, 15, 0), {}) == "2031-01-02"
    with pytest.raises(FieldValueError):
        d.parse("someday", {})

    url = t("url")
    assert url.parse("example.com/x", {}) == "https://example.com/x"
    assert url.chips("Spec", "https://www.figma.com/file/1", {})[0].text == "🔗 figma.com"
    for bad in ("not a url", "javascript:alert(1)", "http://"):
        with pytest.raises(FieldValueError):
            url.parse(bad, {})
    assert t("email").parse("Sam@Example.COM", {}) == "Sam@example.com"
    with pytest.raises(FieldValueError):
        t("email").parse("sam@", {})

    text = t("text")
    assert text.validate("  two\nlines ", text.normalize_config({})) == "two lines"
    assert text.validate("two\nlines", text.normalize_config({"multiline": True})) == "two\nlines"
    with pytest.raises(FieldValueError, match="longer than 3"):
        text.validate("abcd", text.normalize_config({"max_length": 3}))


def test_filters_comparisons_totals_and_conversion():
    sel_def = {"id": 1, "name": "Sev", "field_type": "select",
               "config": t("select").normalize_config({"options": ["Critical", "Minor"]})}
    ftype = cf.type_of(sel_def)
    keys = [k for k, _ in ftype.filter_choices(sel_def["config"])]
    assert keys == ["eq:Critical", "eq:Minor", "set", "empty"]
    assert ftype.matches_filter("Critical", "eq:critical", sel_def["config"])
    assert ftype.matches_filter(None, "empty", sel_def["config"])
    assert t("rating").matches_filter(4, "gte:3", {}) and not t("rating").matches_filter(2, "gte:3", {})
    assert t("multi_select").matches_filter(["Web"], "has:web", {})
    assert t("date").matches_filter((date.today() - timedelta(days=1)).isoformat(), "past", {})
    assert t("checkbox").matches_filter(None, "false", {})
    assert t("person").filter_choices({}, ["Zed", "amy", None])[:2] == [("eq:amy", "is amy"), ("eq:Zed", "is Zed")]

    num = t("number")
    assert num.compare(5, "gt", "3", {}) and num.compare(5, "lt", "10 pts", {"unit": "pts"})
    assert num.compare(None, "is_empty", "", {}) and not num.compare(5, "is", "4", {})
    assert ftype.compare("Critical", "is", "critical", sel_def["config"])
    assert t("text").compare("Acme Corp", "contains", "acme", {})
    assert t("multi_select").compare(["Web", "iOS"], "contains", "ios", {})

    assert cf.column_total({"field_type": "number", "config": {"unit": "pts", "show_total": True}},
                           [3, None, 5]) == "8 pts"
    assert cf.column_total({"field_type": "number", "config": {"show_total": False}}, [3]) is None

    num_def = {"name": "N", "field_type": "number", "config": num.normalize_config({})}
    text_def = {"name": "T", "field_type": "text", "config": {}}
    assert cf.convert_value(5, num_def, text_def) == "5"
    assert cf.convert_value("7", text_def, num_def) == 7
    with pytest.raises(FieldValueError):
        cf.convert_value("seven", text_def, num_def)
    multi_def = {"name": "M", "field_type": "multi_select", "config": {"options": [{"id": "o1", "label": "Minor"}]}}
    assert cf.convert_value("Minor", sel_def, multi_def) == ["Minor"]

    renamed, removed = cf.option_changes(
        {"options": [{"id": "o1", "label": "A"}, {"id": "o2", "label": "B"}]},
        {"options": [{"id": "o1", "label": "Alpha"}]})
    assert renamed == {"A": "Alpha"} and removed == ["B"]
    assert cf.remap_choice_value(t("multi_select"), ["A", "B"], renamed, removed) == ["Alpha"]
    assert cf.infer_type([1, 2.5])[0] == "number" and cf.infer_type([True])[0] == "checkbox"
    assert cf.infer_type([["a"], ["b", "a"]]) == ("multi_select", t("multi_select").normalize_config(
        {"options": ["a", "b"]}))


class PhoneField(FieldType):
    key, label, icon = "test_phone", "Phone", "📞"

    def validate(self, value, config):
        text = self._text(value)
        if text and not re.fullmatch(r"\+?[\d ()-]{6,}", text):
            raise FieldValueError("is not a phone number")
        return text


def test_registering_a_new_field_type_makes_it_usable_everywhere(env):
    service, _ws, board, cols = env
    with pytest.raises(FieldValueError):
        service.create_custom_field(board.id, "Phone", "test_phone")
    cf.register_field_type(PhoneField())
    try:
        with pytest.raises(ValueError, match="already registered"):
            cf.register_field_type(PhoneField())
        f = service.create_custom_field(board.id, "Phone", "test_phone")
        assert f["type_label"] == "Phone" and f["icon"] == "📞"
        task = service.create_task(board.id, cols[0].id, "Call", field_values={"Phone": "+44 20 7946 0000"})
        assert task.custom_fields == {"Phone": "+44 20 7946 0000"}
        with pytest.raises(FieldValueError, match="Phone: is not a phone number"):
            service.update_task(task.id, field_values={f["id"]: "call me"})
    finally:
        cf.unregister_field_type("test_phone")
    # Type no longer installed: values are kept untouched and shown as text, but read-only
    fields = service.get_custom_fields(board.id)
    assert fields[0]["type_label"] == "Unknown (test_phone)"
    assert not cf.type_of(fields[0]).editable
    assert service.get_task(task.id).custom_fields == {"Phone": "+44 20 7946 0000"}
    with pytest.raises(ValueError):
        cf.register_field_type(type("Bad", (FieldType,), {"key": "Bad Key"})())


# ---------------------------------------------------------------------------
# Service: definitions & values
# ---------------------------------------------------------------------------
def test_field_definitions_are_validated(env):
    service, _ws, board, _cols = env
    service.create_custom_field(board.id, "Story points", "number", {"unit": "pts"})
    with pytest.raises(FieldValueError, match="already has a field named 'Story points'"):
        service.create_custom_field(board.id, "  story   POINTS ", "text")
    with pytest.raises(FieldValueError, match="Unknown field type"):
        service.create_custom_field(board.id, "X", "hologram")
    with pytest.raises(FieldValueError, match="needs a name"):
        service.create_custom_field(board.id, "   ", "text")
    with pytest.raises(FieldValueError, match="Default value"):
        service.create_custom_field(board.id, "Sev", "select", {"options": ["A"]}, default="B")
    sev = service.create_custom_field(board.id, "Sev", "select", {"options": ["A", "B"]}, default="b",
                                      required=True, show_on_card=False, description="How bad")
    assert sev["default"] == "B" and sev["required"] and not sev["show_on_card"] and sev["position"] == 1
    board_log = [a["details"] for a in service.get_board_activities(board.id)]
    assert "Added custom field 'Sev' (Dropdown)" in board_log


def test_values_defaults_audit_trail_events_and_search(env):
    service, _ws, board, cols = env
    points = service.create_custom_field(board.id, "Story points", "number", {"unit": "pts"})
    sev = service.create_custom_field(board.id, "Severity", "select", {"options": ["High", "Low"]}, default="Low")
    events = []
    service.add_event_listener(events.append)

    a = service.create_task(board.id, cols[0].id, "A", field_values={"story points": "3 pts"})
    assert a.custom_fields == {"Story points": 3, "Severity": "Low"}, "by name (any case) + default"
    assert a.to_dict()["field_values"] == {points["id"]: 3, sev["id"]: "Low"}
    b = service.create_task(board.id, cols[0].id, "B", field_values={sev["id"]: None})
    assert b.custom_fields == {}, "an explicit None keeps the default out"
    with pytest.raises(FieldValueError, match="no custom field 'Nope'"):
        service.create_task(board.id, cols[0].id, "C", field_values={"Nope": 1})
    with pytest.raises(FieldValueError, match="Severity: 'Mid' is not an option"):
        service.create_task(board.id, cols[0].id, "C", field_values={"Severity": "Mid"})
    assert [x.title for x in service.get_tasks_for_board(board.id)] == ["A", "B"], "failed creations saved nothing"

    events.clear()
    service.update_task(a.id, field_values={points["id"]: 8, sev["id"]: "High"})
    service.update_task(a.id, field_values={points["id"]: 8})  # no-op
    service.set_task_field_values(a.id, {"Severity": None})
    assert history(service, a.id)[-3:] == ["Changed Story points from 3 pts to 8 pts",
                                           "Changed Severity from Low to High", "Cleared Severity (was High)"]
    changed = [e for e in events if e.type == ev.FIELD_CHANGED]
    assert [(e.data["field_name"], e.data["from"], e.data["to"]) for e in changed] == [
        ("Story points", 3, 8), ("Severity", "Low", "High"), ("Severity", "High", None)]
    assert service.get_task(a.id).custom_fields == {"Story points": 8}

    service.update_task(b.id, field_values={"Story points": 13})
    assert [x.title for x in service.get_tasks_for_board(board.id, search_query="13")] == ["B"]
    assert service.field_values_in_use(points["id"]) == [8, 13] or \
        sorted(service.field_values_in_use(points["id"])) == [8, 13]


def test_option_renames_removals_and_type_changes_update_values(env):
    service, _ws, board, cols = env
    sev = service.create_custom_field(board.id, "Severity", "select", {"options": ["High", "Low"]}, default="Low")
    x = service.create_task(board.id, cols[0].id, "X", field_values={"Severity": "High"})
    y = service.create_task(board.id, cols[0].id, "Y", field_values={"Severity": "Low"})
    opts = service.get_custom_field(sev["id"])["config"]["options"]
    high, low = opts
    service.update_custom_field(sev["id"], config={"options": [{**high, "label": "P1"}]})  # rename + remove
    assert service.get_task(x.id).custom_fields == {"Severity": "P1"}
    assert service.get_task(y.id).custom_fields == {}
    assert history(service, x.id)[-1] == "Changed Severity from High to P1 (field settings changed)"
    assert history(service, y.id)[-1] == "Cleared Severity (was Low) (field settings changed)"
    assert service.get_custom_field(sev["id"])["default"] is None, "the default option was removed"

    points = service.create_custom_field(board.id, "Points", "text")
    service.update_task(x.id, field_values={"Points": "5"})
    service.update_task(y.id, field_values={"Points": "lots"})
    preview = service.preview_field_change(points["id"], "number")
    assert preview == {"values": 2, "changed": 1, "lost": [("Y", "lots")]}
    service.update_custom_field(points["id"], field_type="number", name="Story points")
    assert service.get_task(x.id).custom_fields == {"Severity": "P1", "Story points": 5}
    assert "Story points" not in service.get_task(y.id).custom_fields
    board_log = [a["details"] for a in service.get_board_activities(board.id)]
    assert "Custom field 'Points': renamed to 'Story points', type Text → Number" in board_log
    with pytest.raises(ValueError, match="Unknown field settings"):
        service.update_custom_field(points["id"], colour="red")


def test_deleting_and_reordering_fields(env):
    service, _ws, board, cols = env
    a = service.create_custom_field(board.id, "A", "text")
    service.create_custom_field(board.id, "B", "checkbox")
    c = service.create_custom_field(board.id, "C", "number")
    task = service.create_task(board.id, cols[0].id, "T", field_values={"A": "hello", "B": True})
    service.reorder_custom_fields(board.id, [c["id"], a["id"]])
    assert [f["name"] for f in service.get_custom_fields(board.id)] == ["C", "A", "B"]
    assert service.count_field_values(a["id"]) == 1
    assert service.delete_custom_field(a["id"])
    assert [(f["name"], f["position"]) for f in service.get_custom_fields(board.id)] == [("C", 0), ("B", 1)]
    assert service.get_task(task.id).custom_fields == {"B": True}
    assert history(service, task.id)[-1] == "Field 'A' was deleted (was hello)"
    with service.db.session_scope() as s:
        assert s.query(TaskFieldValue).filter_by(field_id=a["id"]).count() == 0
    service.delete_task(task.id)
    service.delete_board(board.id)
    with service.db.session_scope() as s:
        assert s.query(CustomField).count() == 0 and s.query(TaskFieldValue).count() == 0


def test_cross_board_move_carries_field_values(env):
    service, ws, board, cols = env
    other = service.create_board(ws.id, "Other")
    ocols = service.get_columns_for_board(other.id)
    service.create_custom_field(board.id, "Severity", "select", {"options": [{"label": "Critical", "color": "#DC2626"}]})
    service.create_custom_field(board.id, "Points", "number")
    service.create_custom_field(board.id, "Customer", "text")
    service.create_custom_field(other.id, "severity", "select", {"options": ["Minor"]})
    service.create_custom_field(other.id, "Points", "checkbox")
    task = service.create_task(board.id, cols[0].id, "Bug", field_values={"Severity": "Critical", "Points": 5,
                                                                       "Customer": "Acme"})
    ok, err = service.move_task(task.id, ocols[1].id, 0)
    assert ok, err
    moved = service.get_task(task.id)
    assert moved.board_id == other.id
    assert moved.custom_fields == {"severity": "Critical", "Customer": "Acme"}
    target = {f["name"]: f for f in service.get_custom_fields(other.id)}
    assert [o["label"] for o in target["severity"]["config"]["options"]] == ["Minor", "Critical"]
    assert target["severity"]["config"]["options"][1]["color"] == "#DC2626", "option keeps its colour"
    assert target["Customer"]["field_type"] == "text", "missing field created on the target board"
    move_log = next(a["details"] for a in reversed(service.get_task_activity(task.id)) if a["action_type"] == "TASK_MOVED")
    assert "Added field 'Customer' to this board" in move_log and "Dropped Points '5'" in move_log
    assert {fv.field.board_id for fv in moved.field_values} == {other.id}


def test_recurring_copies_keep_field_values_and_assignee(env):
    service, _ws, board, cols = env
    service.create_custom_field(board.id, "Minutes", "number", default=30)
    service.create_custom_field(board.id, "Room", "text", default="Main")
    task = service.create_task(board.id, cols[1].id, "Sync", due_date=datetime(2031, 1, 6, 10), is_recurring=True,
                               recurrence_rule={"frequency": "WEEKLY", "interval": 1}, assignee="Priya",
                               field_values={"Minutes": 45, "Room": None})
    service.toggle_task_completion(task.id)
    new_id = RecurrenceEngine.handle_recurring_task_completion(service, task.id)
    nxt = service.get_task(new_id)
    assert nxt.custom_fields == {"Minutes": 45}, "same values; the emptied Room stays empty"
    assert nxt.assignee == "Priya"


def test_legacy_free_form_custom_fields(env, tmp_path):
    service, _ws, board, cols = env
    task = service.create_task(board.id, cols[0].id, "T", custom_fields={"Customer": "Acme", "Seats": 12})
    fields = {f["name"]: f for f in service.get_custom_fields(board.id)}
    assert fields["Customer"]["field_type"] == "text" and fields["Seats"]["field_type"] == "number"
    assert service.get_task(task.id).custom_fields == {"Customer": "Acme", "Seats": 12}
    service.update_task(task.id, custom_fields={"Seats": 15, "Plan": "Pro"})
    assert service.get_task(task.id).custom_fields == {"Customer": "Acme", "Seats": 15, "Plan": "Pro"}

    # Databases from before typed fields: the JSON column is migrated on startup (once)
    path = tmp_path / "legacy.db"
    seeded_manager(path)
    con = sqlite3.connect(path)
    con.execute("""UPDATE tasks SET custom_fields_json='{"Customer":"Globex","Seats":3,"VIP":true}' WHERE id=1""")
    con.execute("""UPDATE tasks SET custom_fields_json='{"Customer":"Initech","Seats":4.5}' WHERE id=2""")
    con.commit()
    con.close()
    for _ in range(2):
        mgr2 = DatabaseManager(path)
        mgr2.init_database(seed_defaults=True)
    legacy = DatabaseService(mgr2)
    one, two = legacy.get_task(1), legacy.get_task(2)
    assert one.custom_fields == {"Customer": "Globex", "Seats": 3, "VIP": True}
    assert two.custom_fields == {"Customer": "Initech", "Seats": 4.5}
    types = {f["name"]: f["field_type"] for f in legacy.get_custom_fields(one.board_id)}
    assert types == {"Customer": "text", "Seats": "number", "VIP": "checkbox"}
    with mgr2.session_scope() as s:
        assert {t.custom_fields_json for t in s.query(Task)} == {"{}"}


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------
def test_json_export_import_round_trips_fields_templates_and_rules(env, tmp_path):
    service, ws, board, cols = env
    sev = service.create_custom_field(board.id, "Severity", "select", {"options": ["Critical", "Minor"]},
                                      required=True, default="Minor")
    service.create_custom_field(board.id, "Points", "number", {"unit": "pts", "show_total": True})
    tt = service.save_task_template("Bug", {"title": "Bug: ", "fields": {"Severity": "Critical"}}, board_id=board.id)
    service.save_task_template("Everywhere", {"title": "G"})
    service.save_board_template("Saved", {"columns": [{"name": "Only"}]})
    service.save_automation_rule(board.id, {
        "name": "Crit", "trigger": {"type": "field_changed", "field_id": sev["id"], "to_value": "Critical"},
        "conditions": [{"field": "custom_field", "field_id": sev["id"], "op": "is", "value": "Critical"}],
        "actions": [{"type": "create_from_template", "template_id": tt["id"]}]})
    service.create_task(board.id, cols[0].id, "Empty sev", field_values={"Severity": None, "Points": 2})
    service.create_task(board.id, cols[1].id, "Crit", field_values={"Severity": "Critical"})

    path = BackupManager(service.db).export_to_json(tmp_path / "out.json")
    data = json.loads(path.read_text())
    assert data["version"] == "1.6"
    b_data = data["workspaces"][0]["boards"][0]
    assert [f["name"] for f in b_data["custom_fields"]] == ["Severity", "Points"]
    assert b_data["task_templates"][0]["name"] == "Bug" and data["task_templates"][0]["name"] == "Everywhere"
    assert data["board_templates"][0]["name"] == "Saved"
    rule = b_data["automation_rules"][0]
    assert rule["trigger"]["field_id"] == "Severity" and rule["actions"][0]["template_id"] == "Bug"

    mgr = DatabaseManager(tmp_path / "in.db")
    mgr.init_database(seed_defaults=False)
    BackupManager(mgr).import_from_json(path)
    imp = DatabaseService(mgr)
    nb = imp.get_workspaces()[0].boards[0]
    fields = imp.get_custom_fields(nb.id)
    assert [(f["name"], f["required"], f["default"]) for f in fields] == [("Severity", True, "Minor"),
                                                                          ("Points", False, None)]
    tasks = {x.title: x.custom_fields for x in imp.get_tasks_for_board(nb.id)}
    assert tasks == {"Empty sev": {"Points": 2}, "Crit": {"Severity": "Critical"}}, "no defaults added on import"
    r = imp.get_automation_rules(nb.id)[0]
    ids = {f["name"]: f["id"] for f in fields}
    bug = next(x for x in imp.get_task_templates(nb.id) if x["name"] == "Bug")
    assert r["trigger"]["field_id"] == ids["Severity"] and r["conditions"][0]["field_id"] == ids["Severity"]
    assert r["actions"][0]["template_id"] == bug["id"]
    assert [x["name"] for x in imp.get_task_templates(None)] == ["Everywhere"]
    assert [x["name"] for x in imp.get_board_templates() if not x["builtin"]] == ["Saved"]
    BackupManager(mgr).import_from_json(path)  # importing again doesn't duplicate templates
    assert [x["name"] for x in imp.get_board_templates() if not x["builtin"]] == ["Saved"]


def test_import_keeps_labels_and_column_types_and_reads_older_files(env, tmp_path):
    """Regression: imports duplicated every default label and lost column types (CUSTOM)."""
    service, ws, board, cols = env
    service.create_label(board.id, "Customer", "#123456")
    service.delete_label(next(l.id for l in service.get_labels_for_board(board.id) if l.name == "Testing"))
    path = BackupManager(service.db).export_to_json(tmp_path / "out.json")
    data = json.loads(path.read_text())
    # An older (1.5) file: free-form custom fields and no field definitions
    b_data = data["workspaces"][0]["boards"][0]
    b_data.pop("custom_fields")
    b_data["tasks"] = [{"temp_id": 1, "column_name": "To Do", "title": "Old", "labels": ["Bug"],
                        "custom_fields": {"Customer": "Acme", "Seats": 3}}]
    data["version"] = "1.5"
    path.write_text(json.dumps(data))
    mgr = DatabaseManager(tmp_path / "in.db")
    mgr.init_database(seed_defaults=False)
    BackupManager(mgr).import_from_json(path)
    imp = DatabaseService(mgr)
    nb = imp.get_workspaces()[0].boards[0]
    labels = [(l.name, l.color) for l in imp.get_labels_for_board(nb.id)]
    assert sorted(labels) == sorted((l.name, l.color) for l in service.get_labels_for_board(board.id)), \
        "exactly the exported labels: no duplicated defaults, none missing or re-added"
    assert [c.column_type for c in imp.get_columns_for_board(nb.id)] == [c.column_type for c in cols]
    old = imp.get_tasks_for_board(nb.id)[0]
    assert old.custom_fields == {"Customer": "Acme", "Seats": 3} and [l.name for l in old.labels] == ["Bug"]


# ---------------------------------------------------------------------------
# Automation
# ---------------------------------------------------------------------------
def test_automation_field_trigger_condition_and_actions(env):
    service, _ws, board, cols = env
    engine = au.AutomationEngine(service)
    engine.attach()
    sev = service.create_custom_field(board.id, "Severity", "select", {"options": ["Critical", "Minor"]})
    owner = service.create_custom_field(board.id, "Owner", "person")
    tt = service.save_task_template("Postmortem", {"title": "Postmortem: {board}", "labels": ["Documentation"]},
                                    board_id=board.id)
    service.save_automation_rule(board.id, {
        "name": "Critical", "trigger": {"type": "field_changed", "field_id": sev["id"], "to_value": "critical"},
        "conditions": [{"field": "custom_field", "field_id": owner["id"], "op": "is_empty"}],
        "actions": [{"type": "set_priority", "priority": "URGENT"},
                    {"type": "set_field", "field_id": owner["id"], "value": "{actor}"},
                    {"type": "create_from_template", "template_id": tt["id"], "column_id": cols[0].id}]})
    task = service.create_task(board.id, cols[1].id, "Outage")
    service.update_task(task.id, field_values={"Severity": "Minor"})
    assert service.get_task(task.id).priority == "MEDIUM", "to_value didn't match"
    service.update_task(task.id, field_values={"Severity": "Critical"})
    updated = service.get_task(task.id)
    assert updated.priority == "URGENT" and updated.custom_fields["Owner"] == "Ann"
    created = [x for x in service.get_tasks_for_board(board.id) if x.title == "Postmortem: B"]
    assert len(created) == 1 and created[0].column_id == cols[0].id
    assert "Created task 'Postmortem: B' in column 'Backlog' (from template 'Postmortem')" in history(
        service, created[0].id)[0]
    run = service.get_automation_runs(board.id)[0]
    assert run["status"] == "success" and "set 'Owner' to Ann" in run["message"]

    ctx = engine.board_context(board.id)
    rule = service.get_automation_rules(board.id)[0]
    text = au.describe_rule(rule, ctx)
    assert "custom field 'Owner' is empty" in text and "create a task from a template 'Postmortem'" in text
    assert au.validate_rule(rule, ctx) == []
    service.delete_custom_field(owner["id"])
    service.delete_task_template(tt["id"])
    problems = au.validate_rule(rule, engine.board_context(board.id))
    assert "Condition 1: the custom field no longer exists" in problems
    assert any("the task template no longer exists" in p for p in problems)

    # set_field with a value that doesn't fit fails that action only
    service.save_automation_rule(board.id, {"name": "Bad", "trigger": {"type": "task_created"},
                                            "actions": [{"type": "set_field", "field_id": sev["id"], "value": "Huge"},
                                                        {"type": "set_priority", "priority": "LOW"}]})
    fresh = service.create_task(board.id, cols[0].id, "Fresh")
    assert service.get_task(fresh.id).priority == "LOW"
    bad = next(r for r in service.get_automation_runs(board.id) if r["rule_name"] == "Bad")
    assert bad["status"] == "partial" and "'Huge' is not an option" in bad["message"]

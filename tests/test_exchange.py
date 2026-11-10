"""
Tests for import/export tools: the task table and its CSV / Excel (XLSX) writers
(checked with openpyxl when available), Markdown, reading CSV/TSV/XLSX, column
mapping and presets, value parsing (dates, priorities, estimates), the import
plan/run/undo cycle, Trello board import, and the command-line tools.
"""
import csv
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from kanbanmanager import DatabaseService
from kanbanmanager.core import exchange as x
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from support import seeded_manager

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def env(tmp_path):
    mgr = seeded_manager(tmp_path / "x.db")
    service = DatabaseService(mgr, actor="Ann")
    demo = next(b for ws in service.get_workspaces() for b in ws.boards if b.name == DEMO_BOARD_NAME)
    return service, demo


def write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.write_text(text, encoding=encoding)
    return path


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def test_task_table_csv_and_xlsx(env, tmp_path):
    service, demo = env
    col = service.get_columns_for_board(demo.id)[1]
    service.create_task(demo.id, col.id, "=HYPERLINK(\"evil\")", due_date=datetime(2031, 2, 3, 17, 0),
                        field_values={"Story points": 13, "Component": "Data"},
                        checklist=[{"id": 1, "text": "one", "done": True}, {"id": 2, "text": "two", "done": False}])
    headers, rows = x.task_table(service, [demo.id])
    assert headers[:4] == ["ID", "Board", "Column", "Title"]
    assert headers[-5:] == ["Story points", "Component", "Customer impact", "Spec", "Reviewer"]
    row = next(r for r in rows if r[3].startswith("=HYPERLINK"))
    by = dict(zip(headers, row))
    assert by["Due date"] == datetime(2031, 2, 3, 17, 0) and by["Story points"] == 13 and by["Component"] == "Data"
    assert by["Checklist"] == "[x] one\n[ ] two" and by["Status"] == "Open"
    without_done = x.task_table(service, [demo.id], include_completed=False)[1]
    assert len(without_done) == len(rows) - 2

    csv_path = x.write_csv(headers, rows, tmp_path / "t.csv")
    raw = csv_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM so Excel reads UTF-8"
    parsed = list(csv.reader(raw.decode("utf-8-sig").splitlines(True)))
    evil = next(r for r in parsed if "HYPERLINK" in r[3])
    assert evil[3].startswith("'="), "formula injection neutralised"
    assert evil[headers.index("Due date")] == "2031-02-03 17:00"

    xlsx_path = x.write_xlsx(headers, rows, tmp_path / "t.xlsx", sheet="Feature/Tour")
    with zipfile.ZipFile(xlsx_path) as z:
        assert {"[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml", "xl/styles.xml"} <= \
            set(z.namelist())
    back = x.read_table(xlsx_path)
    assert back.headers == headers and len(back.rows) == len(rows)
    r = next(r for r in back.rows if "HYPERLINK" in r[3])
    assert r[headers.index("Due date")] == "2031-02-03 17:00" and r[headers.index("Story points")] == "13"
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb.active
    assert ws.title == "Feature Tour" and ws.freeze_panes == "A2" and ws.auto_filter.ref.startswith("A1:")
    cell = next(c for c in ws["D"] if c.value and "HYPERLINK" in str(c.value))
    assert cell.data_type == "s", "text stays text in Excel (no formula)"
    due = ws.cell(row=cell.row, column=headers.index("Due date") + 1)
    assert due.value == datetime(2031, 2, 3, 17, 0) and due.number_format == "yyyy-mm-dd hh:mm"
    assert ws.cell(row=cell.row, column=headers.index("Story points") + 1).value == 13


def test_board_markdown(env):
    service, demo = env
    md = x.board_markdown(service, demo.id)
    assert md.startswith(f"# {DEMO_BOARD_NAME}\n")
    assert "## In Progress (1 · WIP limit 3)" in md
    assert "- [ ] **Crash when importing large CSV files** — Urgent · due " in md
    assert "`Bug`" in md and "Story points: 5 pts" in md and "@Marcus Chen" in md
    assert "- [x] **Publish the Q3 performance report**" in md
    assert "attachment:" not in md and "*(image: " in md
    assert "Stream rows instead" in md  # descriptions included
    assert "Stream rows instead" not in x.board_markdown(service, demo.id, include_descriptions=False)


# ---------------------------------------------------------------------------
# Reading & mapping
# ---------------------------------------------------------------------------
def test_reading_csv_variants(tmp_path):
    semi = x.read_table(write(tmp_path / "a.csv", "Name;Due;Tags\nA;05.06.2031;x, y\n", "cp1252"))
    assert semi.delimiter == ";" and semi.rows == [["A", "05.06.2031", "x, y"]]
    bom = x.read_table(write(tmp_path / "b.csv", "\ufeffTitle,Note\n\"Multi\nline\",\"a, b\"\n\n"))
    assert bom.headers == ["Title", "Note"] and bom.rows == [["Multi\nline", "a, b"]]
    tsv = x.read_table(write(tmp_path / "c.tsv", "Title\tStatus\nX\tDone\n"))
    assert tsv.delimiter == "\t" and tsv.rows == [["X", "Done"]]
    latin = x.read_table(tmp_path / "d.csv") if (tmp_path / "d.csv").write_bytes("Title\nCaf\xe9\n".encode("cp1252")) \
        else None
    assert latin.rows == [["Café"]] and latin.encoding == "cp1252"
    with pytest.raises(x.ExchangeError, match="empty"):
        x.read_table(write(tmp_path / "e.csv", "\n\n"))
    with pytest.raises(x.ExchangeError):
        x.read_table(tmp_path / "missing.csv")
    (tmp_path / "f.xlsx").write_bytes(b"PK\x03\x04not really a zip")
    with pytest.raises(x.ExchangeError, match="Excel"):
        x.read_table(tmp_path / "f.xlsx")


def test_guess_mapping_and_presets():
    jira = x.Table(["Summary", "Issue key", "Status", "Priority", "Assignee", "Labels", "Labels", "Due date",
                    "Original Estimate", "Description", "Story Points", "Sprint"], [])
    m = x.guess_mapping(jira, preset="Jira")
    assert [m[i] for i in range(len(jira.headers))] == [
        "title", "ignore", "column", "priority", "assignee", "labels", "labels", "due", "estimate_seconds",
        "description", "new_field", "new_field"]
    asana = x.Table(["Task ID", "Name", "Section/Column", "Assignee", "Due Date", "Tags", "Notes", "Completed At"], [])
    m = x.guess_mapping(asana, preset="Asana")
    assert [m[i] for i in range(8)] == ["ignore", "title", "column", "assignee", "due", "labels", "description",
                                        "completed"]
    fields = [{"id": 7, "name": "Story points"}]
    generic = x.Table(["Card Name", "List", "story points", "Whatever", "Title"], [["A", "B", "3", "x", "T"]])
    m = x.guess_mapping(generic, fields)
    assert m == {0: "title", 1: "column", 2: "field:7", 3: "ignore", 4: "ignore"}, "first title column wins"
    nameless = x.Table(["Column 1", "Column 2"], [["123", "Buy milk"]])
    assert x.guess_mapping(nameless) == {0: "ignore", 1: "title"}, "falls back to a text column"


def test_value_parsing():
    p = x.parse_datetime_text
    assert p("2031-06-05") == (datetime(2031, 6, 5), False)
    assert p("2031-06-05T14:30:00") == (datetime(2031, 6, 5, 14, 30), True)
    utc, has_time = p("2031-06-05T14:30:00Z")
    assert has_time and utc == datetime(2031, 6, 5, 14, 30).replace(tzinfo=__import__("datetime").timezone.utc) \
        .astimezone().replace(tzinfo=None)
    assert p("03/04/2031") == (datetime(2031, 3, 4), False)
    assert p("03/04/2031", "dmy") == (datetime(2031, 4, 3), False)
    assert p("25/04/2031") == (datetime(2031, 4, 25), False), "day > 12 must be day-first"
    assert p("05.06.31 9:15 PM", "dmy") == (datetime(2031, 6, 5, 21, 15), True)
    assert p("Jun 5, 2031") == (datetime(2031, 6, 5), False)
    assert p("5 June 2031") == (datetime(2031, 6, 5), False)
    assert p("05/Jun/31 3:45 PM") == (datetime(2031, 6, 5, 15, 45), True)
    assert p("Thu, 5 Jun 2031") == (datetime(2031, 6, 5), False)
    assert p("2031/6/5") == (datetime(2031, 6, 5), False)
    assert p("sometime") == (None, False) and p("31/31/2031") == (None, False)
    assert x.guess_date_order(["01/02/2031", "14/02/2031"]) == "dmy"
    assert x.guess_date_order(["02/14/2031"]) == "mdy"
    assert [x.parse_priority(v) for v in ("Highest", "P2", "normal", "Trivial", "!!!", "3", "wat")] == \
        ["URGENT", "HIGH", "MEDIUM", "LOW", "URGENT", "MEDIUM", None]
    assert [x.parse_hours(v) for v in ("2.5", "1h 30m", "45m", "2d", "1,5", "x")] == [2.5, 1.5, 0.75, 16.0, 1.5, None]
    assert x.parse_bool("Resolved") is True and x.parse_bool("open") is False and x.parse_bool("maybe") is None
    assert x.split_list("a, b; a | c\nd") == ["a", "b", "c", "d"]


# ---------------------------------------------------------------------------
# Import plan / run / undo
# ---------------------------------------------------------------------------
JIRA_CSV = """Summary,Issue key,Status,Priority,Assignee,Labels,Labels,Due date,Original Estimate,Description,Story Points,Component
Login fails on Safari,WEB-1,In Progress,Highest,Priya Sharma,bug,safari,05/Jun/31 3:45 PM,7200,Steps: open the page,5,Frontend
Add dark mode,WEB-2,Selected for Development,Medium,,ux,,,,"Users asked, a lot",8,Design system
Old ticket,WEB-3,Done,Low,Marcus Chen,,,01/Jun/31,3600,,1,Backend
Crash when importing large CSV files,WEB-4,In Progress,High,,,,,,,,
,WEB-5,To Do,Low,,,,,,,,
Login fails on Safari,WEB-6,In Progress,High,,,,,,,,
"""


def test_spreadsheet_import_plan_run_and_undo(env, tmp_path):
    service, demo = env
    table = x.read_table(write(tmp_path / "jira.csv", JIRA_CSV))
    fields = service.get_custom_fields(demo.id)
    mapping = x.guess_mapping(table, fields, preset="Jira")
    assert mapping[10] == "field:" + str(next(f["id"] for f in fields if f["name"] == "Story points"))
    assert mapping[11] == "field:" + str(next(f["id"] for f in fields if f["name"] == "Component"))
    opts = x.ImportOptions(board_id=demo.id, mapping=mapping)
    plans = x.plan_rows(service, table, opts)
    by_line = {p.line: p for p in plans}
    safari = by_line[1]
    assert (safari.title, safari.priority, safari.labels, safari.assignee, safari.estimate) == (
        "Login fails on Safari", "URGENT", ["bug", "safari"], "Priya Sharma", 2.0)
    assert safari.due == datetime(2031, 6, 5, 15, 45) and safari.column_id is not None
    assert by_line[2].new_column == "Selected for Development"
    assert by_line[3].completed and by_line[3].due == datetime(2031, 6, 1, 17, 0)
    assert by_line[4].skip == "already on the board"
    assert by_line[5].skip == "no title"
    assert by_line[6].skip == "duplicate row"

    before_labels = {l.name for l in service.get_labels_for_board(demo.id)}
    result = x.run_import(service, table, opts)
    assert result.describe() == "3 tasks imported, 3 skipped, 1 column added, 2 labels added", "bug = Bug"
    tasks = {t.title: t for t in service.get_tasks_for_board(demo.id) if t.id in result.created}
    dark = tasks["Add dark mode"]
    assert dark.custom_fields == {"Story points": 8, "Component": "Design system"}, "new option added"
    comp = next(f for f in service.get_custom_fields(demo.id) if f["name"] == "Component")
    assert "Design system" in [o["label"] for o in comp["config"]["options"]]
    old = tasks["Old ticket"]
    assert old.is_completed and old.column_id == next(c.id for c in service.get_columns_for_board(demo.id)
                                                       if c.is_done_column)
    assert service.get_task_activity(old.id)[0]["details"].endswith("(imported from jira.csv)")

    assert x.undo_import(service, result) == 3
    assert not any(t.id in result.created for t in service.get_tasks_for_board(demo.id, include_archived=True))
    assert "Selected for Development" not in [c.name for c in service.get_columns_for_board(demo.id)]
    assert {l.name for l in service.get_labels_for_board(demo.id)} == before_labels


def test_import_options_and_new_fields(env, tmp_path):
    service, demo = env
    board = service.create_board(demo.workspace_id, "Plain", labels_def=[])
    table = x.read_table(write(tmp_path / "t.csv", "Title,Status,Customer,Checklist,Done,Comment\n"
                                                  "A,Waiting,Acme,[x] call; [ ] email,no,First note\n"
                                                  "B,,Globex,,yes,\n"))
    mapping = x.guess_mapping(table)
    mapping[2] = x.NEW_FIELD
    opts = x.ImportOptions(board_id=board.id, mapping=mapping, create_columns=False, create_labels=False)
    result = x.run_import(service, table, opts)
    assert len(result.created) == 2 and result.columns_created == []
    tasks = {t.title: t for t in service.get_tasks_for_board(board.id)}
    todo = service.default_capture_column(board.id)
    assert tasks["A"].column_id == todo, "unknown status -> default column when not creating columns"
    assert tasks["A"].custom_fields == {"Customer": "Acme"}
    assert tasks["A"].checklist == [{"id": 1, "text": "call", "done": True}, {"id": 2, "text": "email", "done": False}]
    assert [c["body"] for c in service.get_comments(tasks["A"].id)] == ["First note"]
    assert tasks["B"].is_completed
    assert len(result.fields_created) == 1
    x.undo_import(service, result)
    assert service.get_custom_fields(board.id) == [], "the field it created goes too"


# ---------------------------------------------------------------------------
# Trello
# ---------------------------------------------------------------------------
TRELLO = {
    "name": "Launch", "desc": "Trello board",
    "lists": [{"id": "l1", "name": "Ideas", "pos": 1}, {"id": "l2", "name": "Doing", "pos": 2},
              {"id": "l3", "name": "Done", "pos": 3}, {"id": "l4", "name": "Old", "pos": 4, "closed": True}],
    "labels": [{"id": "g", "name": "", "color": "green"}, {"id": "r", "name": "Urgent", "color": "red_dark"}],
    "members": [{"id": "m1", "fullName": "Priya Sharma"}],
    "customFields": [{"id": "cf1", "name": "Effort", "type": "number"},
                     {"id": "cf2", "name": "Team", "type": "list",
                      "options": [{"id": "o1", "value": {"text": "Web"}, "color": "blue"}]}],
    "checklists": [{"id": "c1", "idCard": "k1", "name": "Steps", "pos": 1,
                    "checkItems": [{"name": "Draft", "state": "complete", "pos": 1},
                                   {"name": "Review", "state": "incomplete", "pos": 2}]}],
    "cards": [
        {"id": "k1", "name": "Write copy", "desc": "Hero text", "idList": "l2", "pos": 1, "idLabels": ["g", "r"],
         "idMembers": ["m1"], "due": "2031-06-05T15:00:00.000Z", "dueComplete": False,
         "customFieldItems": [{"idCustomField": "cf1", "value": {"number": "2.5"}},
                              {"idCustomField": "cf2", "idValue": "o1"}],
         "attachments": [{"name": "Brief", "url": "https://example.com/brief"}]},
        {"id": "k2", "name": "Ship", "idList": "l3", "pos": 1, "dueComplete": True, "due": "2031-06-01T10:00:00Z"},
        {"id": "k3", "name": "Archived idea", "idList": "l1", "pos": 2, "closed": True},
        {"id": "k4", "name": "In a closed list", "idList": "l4", "pos": 1},
    ],
    "actions": [{"type": "commentCard", "date": "2031-05-01T09:00:00.000Z", "memberCreator": {"fullName": "Bo"},
                 "data": {"card": {"id": "k1"}, "text": "Looks good"}}],
}


def test_trello_import(env, tmp_path):
    service, demo = env
    path = tmp_path / "trello.json"
    path.write_text(json.dumps(TRELLO), encoding="utf-8")
    data = x.read_trello(path)
    board, result = x.import_trello(service, data, demo.workspace_id)
    assert board.name == "Launch" and [c.name for c in board.columns] == ["Ideas", "Doing", "Done"]
    assert [c.is_done_column for c in board.columns] == [False, False, True]
    assert result.describe().startswith("2 tasks imported, 2 skipped")
    tasks = {t.title: t for t in service.get_tasks_for_board(board.id)}
    copy = tasks["Write copy"]
    assert sorted(l.name for l in copy.labels) == ["Green", "Urgent"]
    assert {l.name: l.color for l in copy.labels}["Urgent"] == "#EB5A46"
    assert copy.assignee == "Priya Sharma" and copy.description.startswith("Hero text")
    assert "[Brief](https://example.com/brief)" in copy.description
    assert copy.checklist == [{"id": 1, "text": "Draft", "done": True}, {"id": 2, "text": "Review", "done": False}]
    assert copy.custom_fields == {"Effort": 2.5, "Team": "Web"}
    comments = service.get_comments(copy.id)
    assert comments[0]["author"] == "Bo" and comments[0]["body"] == "Looks good"
    assert comments[0]["created_at"].startswith("2031-05-01T09:00")
    assert tasks["Ship"].is_completed
    with_archive, result2 = x.import_trello(service, data, demo.workspace_id, name="Launch (all)",
                                            include_archived=True)
    assert [c.name for c in with_archive.columns][-1] == "Old"
    assert "Archived idea" in [t["title"] for t in service.get_archived_tasks(with_archive.id)]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"workspaces": []}))
    with pytest.raises(x.ExchangeError, match="not a Trello board export"):
        x.read_trello(bad)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def _cli(tmp_path, *args):
    env = dict(os.environ, PYTHONPATH=str(SRC), KANBAN_DATA_DIR=str(tmp_path / "data"), QT_QPA_PLATFORM="offscreen")
    return subprocess.run([sys.executable, "-m", "kanbanmanager", *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.slow
def test_command_line_entry_point(tmp_path):
    """The real ``python -m kanbanmanager`` process: tools run without a window and report
    through the exit code (behaviour in detail: tests/test_cli.py, in-process)."""
    data = tmp_path / "data"
    data.mkdir()
    DatabaseManager(data / "kanban.db").init_database(seed_defaults=True)
    proc = _cli(tmp_path, "--export", "csv", "--board", "feature")
    assert proc.returncode == 0 and proc.stdout.startswith("Exported 13 tasks"), proc.stderr
    assert (tmp_path / "Feature Tour.csv").read_bytes().startswith(b"\xef\xbb\xbfID,")
    missing = _cli(tmp_path, "--export", "md", "--board", "nope")
    assert missing.returncode != 0 and "No board named 'nope'" in missing.stderr
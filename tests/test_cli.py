"""
The command-line tools (``kanbanmanager --export / --import / --sync``), run
in-process through the same argument parser the app uses - fast, and measured
by coverage.  ``test_exchange.py`` keeps one real subprocess run for the entry
point itself.
"""
from __future__ import annotations

import json
import zipfile

import pytest

from kanbanmanager import cli
from kanbanmanager.app import _parse_args
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.database.service import DatabaseService
from support import seeded_manager


@pytest.fixture
def db(tmp_path):
    return seeded_manager(tmp_path / "cli.db")


@pytest.fixture
def run(db, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def _run(*argv):
        code = cli.run(_parse_args(list(argv)), db_manager=db)
        out, err = capsys.readouterr()
        return code, out, err
    return _run


def test_tools_are_only_wanted_when_asked_for():
    assert not cli.wants_tools(_parse_args([]))
    assert not cli.wants_tools(_parse_args(["--capture", "x"]))
    for argv in (["--export", "csv"], ["--import", "a.csv"], ["--sync"], ["--sync", "Work"]):
        assert cli.wants_tools(_parse_args(argv)), argv
    assert _parse_args(["--sync"]).sync == "" and _parse_args(["--sync", "Work"]).sync == "Work"


@pytest.mark.parametrize("fmt, name, magic", [
    ("csv", "Feature Tour.csv", b"\xef\xbb\xbfID,"),
    ("xlsx", "Feature Tour.xlsx", b"PK"),
    ("md", "Feature Tour.md", b"# "),
    ("ics", "Feature Tour.ics", b"BEGIN:VCALENDAR"),
    ("pdf", "Feature Tour report.pdf", b"%PDF"),
    ("svg", "Feature Tour.svg", b"<?xml"),
    ("png", "Feature Tour.png", b"\x89PNG"),
])
def test_every_export_format(run, tmp_path, fmt, name, magic):
    code, out, err = run("--export", fmt, "--board", "feature")  # a name prefix is enough
    assert code == 0, err
    assert out.startswith("Exported ") and name in out
    assert (tmp_path / name).read_bytes().startswith(magic)


def test_export_details_and_options(run, tmp_path, db):
    code, out, _ = run("--export", "csv", "--board", DEMO_BOARD_NAME, "--output", "sub/dir/tasks.csv")
    assert code == 0 and "13 tasks" in out and (tmp_path / "sub/dir/tasks.csv").exists()
    code, out, _ = run("--export", "xlsx")  # no board: every board
    assert code == 0 and (tmp_path / "kanban.xlsx").exists()
    with zipfile.ZipFile(tmp_path / "kanban.xlsx") as z:
        assert "xl/worksheets/sheet1.xml" in z.namelist()
    code, out, _ = run("--export", "ics", "--events", "--completed", "--output", "all.ics")
    text = (tmp_path / "all.ics").read_text()
    assert code == 0 and "BEGIN:VEVENT" in text and "BEGIN:VTODO" not in text
    code, out, _ = run("--export", "ics", "--mine", "--output", "mine.ics")
    assert code == 0 and "calendar item" in out
    code, out, _ = run("--export", "json")
    backup = json.loads((tmp_path / "kanban-backup.json").read_text())
    assert code == 0 and "full backup" in out and backup["workspaces"]
    code, out, _ = run("--export", "pdf", "--board", DEMO_BOARD_NAME, "--days", "7", "--output", "week.pdf")
    assert code == 0 and (tmp_path / "week.pdf").read_bytes().startswith(b"%PDF")


def test_board_lookup_errors(run, db):
    code, _out, err = run("--export", "md")
    assert code == 2 and "needs --board" in err
    with pytest.raises(SystemExit, match="No board named 'nope'"):
        run("--export", "md", "--board", "nope")
    service = DatabaseService(db)
    ws = service.get_workspaces()[0]
    service.create_board(ws.id, "Feature Requests")
    with pytest.raises(SystemExit, match="matches several boards"):
        run("--export", "md", "--board", "feature")
    demo = next(b for b in ws.boards if b.name == DEMO_BOARD_NAME)
    code, out, _ = run("--export", "md", "--board", str(demo.id))
    assert code == 0, "boards can be given by id"


def test_imports_spreadsheets_skipping_duplicates(run, tmp_path, db):
    run("--export", "csv", "--board", DEMO_BOARD_NAME, "--output", "tour.csv")
    code, out, _ = run("--import", "tour.csv", "--board", "Product Roadmap")
    assert code == 0 and "13 tasks imported" in out
    code, out, _ = run("--import", "tour.csv", "--board", "Product Roadmap")
    assert "0 tasks imported, 13 skipped" in out
    code, _out, err = run("--import", "tour.csv")
    assert code == 2 and "needs --board" in err
    (tmp_path / "odd.csv").write_text("Foo,Bar\n1,2\n", encoding="utf-8")
    code, _out, err = run("--import", "odd.csv", "--board", "Product Roadmap")
    assert code == 2 and "titles" in err
    code, _out, err = run("--import", "missing.csv", "--board", "Product Roadmap")
    assert code == 1 and "Import failed" in err


def test_imports_calendars_trello_and_backups(run, tmp_path, db):
    (tmp_path / "cal.ics").write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\nBEGIN:VTODO\r\nUID:1@t\r\nSUMMARY:From ICS\r\n"
        "DUE:20310101T100000Z\r\nEND:VTODO\r\nEND:VCALENDAR\r\n", encoding="utf-8")
    code, out, _ = run("--import", "cal.ics", "--board", "Product Roadmap")
    service = DatabaseService(db)
    roadmap = next(b for ws in service.get_workspaces() for b in ws.boards if b.name == "Product Roadmap")
    assert code == 0 and "From ICS" in [t.title for t in service.get_tasks_for_board(roadmap.id)]

    trello = {"name": "Trello Board", "lists": [{"id": "l1", "name": "Todo", "closed": False}],
              "cards": [{"id": "c1", "name": "Card one", "idList": "l1", "closed": False, "desc": "",
                         "idLabels": [], "idMembers": []}], "labels": [], "checklists": [], "actions": [],
              "members": []}
    (tmp_path / "trello.json").write_text(json.dumps(trello), encoding="utf-8")
    code, out, _ = run("--import", "trello.json")
    assert code == 0 and "Created board 'Trello Board'" in out

    run("--export", "json", "--output", "backup.json")
    before = sum(len(ws.boards) for ws in service.get_workspaces())
    code, out, _ = run("--import", "backup.json")
    assert code == 0 and "Imported the backup" in out
    assert sum(len(ws.boards) for ws in service.get_workspaces()) == 2 * before


def test_sync_runs_every_or_one_named_subscription(run, tmp_path, db):
    code, out, _ = run("--sync")
    assert code == 0 and out.startswith("No calendar subscriptions")
    service = DatabaseService(db)
    roadmap = next(b for ws in service.get_workspaces() for b in ws.boards if b.name == "Product Roadmap")
    feed = tmp_path / "feed.ics"
    feed.write_text("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//T//EN\r\nBEGIN:VTODO\r\nUID:f1@t\r\n"
                    "SUMMARY:Feed item\r\nEND:VTODO\r\nEND:VCALENDAR\r\n", encoding="utf-8")
    service.save_calendar_subscription({"name": "Team feed", "url": feed.as_uri(), "board_id": roadmap.id})
    code, out, _ = run("--sync", "team feed")
    assert code == 0 and out == "Team feed: 1 new\n"
    code, _out, err = run("--sync", "Other")
    assert code == 2 and "No calendar subscription named 'Other'" in err
    feed.unlink()
    code, out, _ = run("--sync")
    assert code == 1, "a failed sync is reported in the exit code"

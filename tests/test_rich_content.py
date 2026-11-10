"""
Tests for Markdown helpers, Attachments, Comments, the Audit Trail, schema
migration, backup/export of rich content, and the demo board.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from kanbanmanager import BackupManager, DatabaseService
from kanbanmanager.config import get_current_user
from kanbanmanager.core.markdown_utils import (
    attachment_refs,
    first_line_snippet,
    markdown_to_plain,
    parse_attachment_url,
    remove_attachment_refs,
    rewrite_attachment_refs,
    tidy_qt_markdown,
)
from kanbanmanager.database.attachments import AttachmentError
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.database.models import ActivityLog, Attachment, utc_now
from kanbanmanager.enums import ActivityAction as A
from support import seeded_manager


@pytest.fixture
def env(tmp_path):
    mgr = DatabaseManager(tmp_path / "rich.db")
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr, actor="Ada")
    ws = service.create_workspace("Eng")
    board = service.create_board(ws.id, "Board")
    cols = service.get_columns_for_board(board.id)
    return mgr, service, board, cols, tmp_path


def actions(service, task_id):
    return [e["action_type"] for e in service.get_task_activity(task_id)]


def make_file(path, content=b"hello"):
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------
def test_markdown_snippets_and_plain_text():
    md = "![banner](attachment:3)\n\n## Summary\n\nThe **importer** crashes on [big files](https://x.y).\n"
    assert first_line_snippet(md) == "The importer crashes on big files."
    assert first_line_snippet("## Only a heading") == "Only a heading"
    assert first_line_snippet("") == ""
    assert len(first_line_snippet("word " * 50, limit=40)) <= 40
    plain = markdown_to_plain("- [x] done *item*\n| a | b |\n|---|---|\n| `c` | d |")
    assert plain == "done item\na b\nc d"
    # underscores inside identifiers are not emphasis
    assert markdown_to_plain("use snake_case_name") == "use snake_case_name"


def test_attachment_reference_helpers():
    md = "See ![shot](attachment:new-1) and [log](attachment:7) and ![x](https://e.com/a.png)"
    assert attachment_refs(md) == {"new-1", "7"}
    assert rewrite_attachment_refs(md, {"new-1": "42"}) == md.replace("attachment:new-1", "attachment:42")
    removed = remove_attachment_refs(md, "new-1")
    assert "attachment:new-1" not in removed and "attachment:7" in removed
    assert remove_attachment_refs(md, "7").count("attachment:7") == 0 and "log" in remove_attachment_refs(md, "7")
    assert parse_attachment_url("attachment:12") == "12"
    assert parse_attachment_url("attachment:../../etc/passwd") is None
    assert parse_attachment_url("https://example.com") is None


def test_tidy_qt_markdown_fixes_code_fences():
    assert tidy_qt_markdown("a\n\n```\ncode\n\n```\nb\n\n") == "a\n\n```\ncode\n```\n\nb"
    assert tidy_qt_markdown("```py\nx\n  ```\n\n") == "```py\nx\n```"


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
def test_attachment_lifecycle_dedup_and_file_cleanup(env):
    mgr, service, board, cols, tmp = env
    t1 = service.create_task(board.id, cols[0].id, "One")
    t2 = service.create_task(board.id, cols[0].id, "Two")
    src = make_file(tmp / "spec.pdf", b"%PDF same bytes")

    a1 = service.add_attachment(t1.id, src)
    a2 = service.add_attachment(t2.id, src, filename="copy.pdf")
    assert a1["filename"] == "spec.pdf" and a1["content_type"] == "application/pdf"
    assert a1["file_exists"] and a1["uploaded_by"] == "Ada"
    assert a1["stored_name"] == a2["stored_name"]  # identical content is stored once
    blob = mgr.attachment_store.path_for(a1["stored_name"])
    assert blob.read_bytes() == b"%PDF same bytes"

    assert service.get_task(t1.id).to_dict()["attachment_count"] == 1
    assert A.ATTACHMENT_ADDED.value in actions(service, t1.id)

    assert service.delete_attachment(a1["id"]) is True
    assert blob.exists(), "blob still referenced by task Two"
    assert A.ATTACHMENT_REMOVED.value in actions(service, t1.id)
    service.delete_attachment(a2["id"])
    assert not blob.exists()


def test_attachment_validation(env):
    mgr, service, board, cols, tmp = env
    t = service.create_task(board.id, cols[0].id, "T")
    with pytest.raises(AttachmentError):
        service.add_attachment(t.id, tmp / "missing.txt")
    mgr.attachment_store.max_bytes = 10
    with pytest.raises(AttachmentError, match="limit"):
        service.add_attachment(t.id, make_file(tmp / "big.bin", b"x" * 11))
    assert service.get_attachments(t.id) == []
    with pytest.raises(AttachmentError):
        mgr.attachment_store.path_for("../../outside.txt")


@pytest.mark.parametrize("deleter", ["task", "column", "board", "workspace"])
def test_cascading_deletes_remove_attachment_files(env, deleter):
    mgr, service, board, cols, tmp = env
    t = service.create_task(board.id, cols[1].id, "T")
    att = service.add_attachment(t.id, make_file(tmp / f"{deleter}.txt", deleter.encode()))
    blob = mgr.attachment_store.path_for(att["stored_name"])
    assert blob.exists()
    {
        "task": lambda: service.delete_task(t.id),
        "column": lambda: service.delete_column(cols[1].id),
        "board": lambda: service.delete_board(board.id),
        "workspace": lambda: service.delete_workspace(board.workspace_id),
    }[deleter]()
    assert not blob.exists()


def test_missing_blob_is_reported_not_fatal(env):
    mgr, service, board, cols, tmp = env
    t = service.create_task(board.id, cols[0].id, "T")
    att = service.add_attachment(t.id, make_file(tmp / "a.txt"))
    mgr.attachment_store.path_for(att["stored_name"]).unlink()
    info = service.get_attachments(t.id)[0]
    assert info["file_exists"] is False
    assert service.get_attachment_path(att["id"]) is None


# ---------------------------------------------------------------------------
# Comments & audit trail
# ---------------------------------------------------------------------------
def test_comment_crud_is_audited(env):
    _, service, board, cols, _ = env
    t = service.create_task(board.id, cols[0].id, "T")
    c = service.add_comment(t.id, "  Looks **good**  ")
    assert c["body"] == "Looks **good**" and c["author"] == "Ada" and not c["is_edited"]
    with pytest.raises(ValueError):
        service.add_comment(t.id, "   ")

    edited = service.update_comment(c["id"], "Looks great")
    assert edited["is_edited"] and edited["body"] == "Looks great"
    assert service.get_task(t.id).to_dict()["comment_count"] == 1

    assert service.delete_comment(c["id"]) is True
    assert service.get_comments(t.id) == []
    log = service.get_task_activity(t.id)
    assert [e["action_type"] for e in log][-3:] == [
        A.COMMENT_ADDED.value, A.COMMENT_EDITED.value, A.COMMENT_DELETED.value,
    ]
    deleted = log[-1]
    assert deleted["old_value"] == "Looks great" and deleted["actor"] == "Ada"


def test_update_task_records_field_level_changes(env):
    _, service, board, cols, _ = env
    bug = service.create_label(board.id, "Sec", "#EF4444")
    due = datetime(2030, 5, 1, 9, 30, 15, 987654)
    t = service.create_task(
        board.id, cols[0].id, "Draft", description="old", estimated_hours=2, due_date=due,
        checklist=[{"id": 1, "text": "a", "done": False}],
    )
    start = len(service.get_task_activity(t.id))

    service.update_task(
        t.id,
        title="Final",
        description="new",
        priority="URGENT",
        column_id=cols[2].id,
        estimated_hours=4,
        due_date=None,
        label_ids=[bug.id],
        checklist=[{"id": 1, "text": "a", "done": True}, {"id": 2, "text": "b", "done": False}],
        is_recurring=True,
        recurrence_rule={"frequency": "WEEKLY", "interval": 1},
    )
    entries = {e["field_name"]: e for e in service.get_task_activity(t.id)[start:]}
    assert entries["title"]["old_value"] == "Draft" and entries["title"]["new_value"] == "Final"
    assert entries["description"]["old_value"] == "old"
    assert entries["priority"]["details"] == "Changed priority from Medium to Urgent"
    assert entries["column"]["action_type"] == A.TASK_MOVED.value
    assert entries["column"]["new_value"] == cols[2].name
    assert entries["estimated_hours"]["details"] == "Changed estimate from 2h to 4h"
    assert entries["due_date"]["details"].startswith("Removed due date")
    assert entries["labels"]["details"] == "Added label Sec"
    assert "checked 'a'" in entries["checklist"]["details"] and "added 'b'" in entries["checklist"]["details"]
    assert entries["recurrence"]["details"] == "Set to repeat: Weekly"
    assert all(e["actor"] == "Ada" for e in entries.values())


def test_noop_update_logs_nothing(env):
    """Saving an unchanged task (e.g. dates round-tripped through Qt with lost microseconds) adds no history."""
    _, service, board, cols, _ = env
    due = datetime(2030, 5, 1, 9, 30, 15, 987654)
    t = service.create_task(board.id, cols[0].id, "Same", description="d", due_date=due, estimated_hours=1.5)
    before = len(service.get_task_activity(t.id))
    service.update_task(
        t.id, title="Same", description="d", due_date=due.replace(microsecond=987000),
        estimated_hours=1.5000001, label_ids=[], checklist=[], recurrence_rule={}, is_recurring=False,
    )
    assert len(service.get_task_activity(t.id)) == before


def test_moves_completion_and_dependencies_are_audited(env):
    _, service, board, cols, _ = env
    a = service.create_task(board.id, cols[0].id, "A")
    b = service.create_task(board.id, cols[0].id, "B")

    service.move_task(a.id, cols[0].id, 1)  # reorder within the column: not history
    assert actions(service, a.id) == [A.TASK_CREATED.value]
    service.move_task(a.id, cols[1].id)
    moved = service.get_task_activity(a.id)[-1]
    assert (moved["old_value"], moved["new_value"]) == (cols[0].name, cols[1].name)

    service.toggle_task_completion(a.id)
    service.toggle_task_completion(a.id)
    assert actions(service, a.id)[-2:] == [A.TASK_COMPLETED.value, A.TASK_REOPENED.value]

    service.add_dependency(b.id, a.id)
    service.add_dependency(b.id, a.id)  # duplicate: no second entry
    service.remove_dependency(b.id, a.id)
    assert actions(service, b.id)[1:] == [A.DEPENDENCY_ADDED.value, A.DEPENDENCY_REMOVED.value]


def test_activity_paused_suppresses_logging(env):
    _, service, board, cols, _ = env
    t = service.create_task(board.id, cols[0].id, "T")
    with service.activity_paused():
        service.update_task(t.id, title="Quiet")
        service.add_comment(t.id, "hi")
    assert actions(service, t.id) == [A.TASK_CREATED.value]
    service.update_task(t.id, title="Loud")
    assert actions(service, t.id)[-1] == A.TASK_UPDATED.value


def test_current_user_override(monkeypatch):
    monkeypatch.setenv("KANBAN_USER", "Grace Hopper")
    assert get_current_user() == "Grace Hopper"
    assert DatabaseService(DatabaseManager(":memory:")).actor == "Grace Hopper"


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------
def test_legacy_database_is_migrated(tmp_path):
    db = tmp_path / "legacy.db"
    mgr = seeded_manager(db)
    mgr.engine.dispose()
    # Recreate activity_logs exactly as the previous release defined it
    con = sqlite3.connect(db)
    con.executescript(
        """
        DROP TABLE activity_logs;
        DROP TABLE comments;
        DROP TABLE attachments;
        CREATE TABLE activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, board_id INTEGER NOT NULL, task_id INTEGER,
            action_type VARCHAR(50) NOT NULL, details TEXT, timestamp DATETIME NOT NULL);
        INSERT INTO activity_logs (board_id, task_id, action_type, details, timestamp)
            VALUES (1, 1, 'TASK_CREATED', 'legacy row', '2024-01-01 10:00:00');
        """
    )
    con.commit()
    con.close()

    upgraded = DatabaseManager(db)
    upgraded.init_database(seed_defaults=True)
    service = DatabaseService(upgraded, actor="Ada")
    legacy = service.get_task_activity(1)
    assert legacy[0]["details"] == "legacy row" and legacy[0]["actor"] == ""
    service.add_comment(1, "works after upgrade")
    assert service.get_task_activity(1)[-1]["actor"] == "Ada"
    upgraded.init_database(seed_defaults=True)  # idempotent


# ---------------------------------------------------------------------------
# Backups & JSON export
# ---------------------------------------------------------------------------
def test_snapshot_restore_brings_back_attachment_files(env):
    mgr, service, board, cols, tmp = env
    backups = BackupManager(mgr)
    backups.backup_dir = tmp / "backups"
    t = service.create_task(board.id, cols[0].id, "T")
    att = service.add_attachment(t.id, make_file(tmp / "keep.txt", b"precious"))
    snap = backups.create_database_backup()
    assert (snap.parent / "attachments" / att["stored_name"]).exists()

    service.delete_attachment(att["id"])  # row and blob are gone from the live data
    assert service.get_attachments(t.id) == []

    backups.restore_database_backup(snap)
    restored = service.get_attachments(t.id)
    assert len(restored) == 1 and restored[0]["file_exists"]
    assert mgr.attachment_store.path_for(restored[0]["stored_name"]).read_bytes() == b"precious"


def test_json_roundtrip_preserves_rich_content(env, tmp_path):
    mgr, service, board, cols, tmp = env
    backups = BackupManager(mgr)
    t = service.create_task(board.id, cols[0].id, "Rich")
    img = service.add_attachment(t.id, make_file(tmp / "shot.png", b"\x89PNG fake"))
    service.update_task(t.id, description=f"Look: ![shot](attachment:{img['id']})")
    service.add_comment(t.id, "First!")
    original_history = service.get_task_activity(t.id)

    dest = backups.export_to_json(tmp / "export.json")
    exported = json.loads(dest.read_text())
    assert exported["version"] == "1.6"

    # Shift ids in the target DB so a stale attachment id in the description would be detectable
    new_mgr = DatabaseManager(tmp_path / "imported" / "k.db")
    new_mgr.init_database(seed_defaults=False)
    new_service = DatabaseService(new_mgr, actor="Importer")
    filler_ws = new_service.create_workspace("Filler")
    filler_board = new_service.create_board(filler_ws.id, "F")
    filler_task = new_service.create_task(filler_board.id, new_service.get_columns_for_board(filler_board.id)[0].id, "f")
    for i in range(3):
        new_service.add_attachment_bytes(filler_task.id, f"{i}".encode(), f"f{i}.txt")

    BackupManager(new_mgr).import_from_json(dest)
    imported_board = [b for ws in new_service.get_workspaces() for b in ws.boards if b.name == "Board"][0]
    task = [x for x in new_service.get_tasks_for_board(imported_board.id) if x.title == "Rich"][0]

    atts = new_service.get_attachments(task.id)
    assert len(atts) == 1 and atts[0]["file_exists"]
    assert new_mgr.attachment_store.path_for(atts[0]["stored_name"]).read_bytes() == b"\x89PNG fake"
    assert task.description == f"Look: ![shot](attachment:{atts[0]['id']})"

    comments = new_service.get_comments(task.id)
    assert [c["body"] for c in comments] == ["First!"] and comments[0]["author"] == "Ada"

    history = new_service.get_task_activity(task.id)
    assert [(h["action_type"], h["actor"]) for h in history] == [
        (h["action_type"], h["actor"]) for h in original_history
    ], "history is replayed verbatim, not re-logged by the importer"


def test_import_of_legacy_json_without_rich_content(env, tmp_path):
    _, service, *_ = env
    legacy = {
        "version": "1.0",
        "workspaces": [{"name": "Old", "boards": [{
            "name": "B",
            "columns": [{"name": "To Do", "position": 0}],
            "labels": [],
            "tasks": [{"temp_id": 1, "column_name": "To Do", "title": "Old task", "description": "plain"}],
            "dependencies": [],
        }]}],
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy))
    BackupManager(service.db).import_from_json(path)
    board = [b for ws in service.get_workspaces() for b in ws.boards if b.name == "B"][0]
    task = service.get_tasks_for_board(board.id)[0]
    assert actions(service, task.id) == [A.TASK_CREATED.value]
    assert service.get_comments(task.id) == [] and service.get_attachments(task.id) == []


# ---------------------------------------------------------------------------
# Demo board
# ---------------------------------------------------------------------------
def _check_demo_board(service, mgr, board_id):
    tasks = service.get_tasks_for_board(board_id)
    assert len(tasks) == 13
    with mgr.session_scope() as session:
        for task in tasks:
            atts = {str(a.id) for a in session.query(Attachment).filter(Attachment.task_id == task.id)}
            # every inline image points at an attachment of the same task, and the file exists
            assert attachment_refs(task.description) <= atts, task.title
            for a in session.query(Attachment).filter(Attachment.task_id == task.id):
                assert mgr.attachment_store.exists(a.stored_name)
            logs = (
                session.query(ActivityLog).filter(ActivityLog.task_id == task.id)
                .order_by(ActivityLog.timestamp, ActivityLog.id).all()
            )
            assert logs[0].action_type == A.TASK_CREATED.value, task.title
            assert all(log.actor for log in logs)
            assert all(log.timestamp <= utc_now() + timedelta(minutes=1) for log in logs)
    by_title = {t.title: t.to_dict() for t in tasks}
    assert by_title["Migrate attachment storage to the CDN"]["is_blocked"] is True
    assert by_title["Publish the Q3 performance report"]["is_completed"] is True
    assert sum(d["comment_count"] for d in by_title.values()) >= 10
    assert sum(d["attachment_count"] for d in by_title.values()) >= 10
    # positions within each column are contiguous
    by_col = {}
    for t in tasks:
        by_col.setdefault(t.column_id, []).append(t.position)
    assert all(sorted(p) == list(range(len(p))) for p in by_col.values())


def test_fresh_database_seeds_demo_board(tmp_path):
    mgr = DatabaseManager(tmp_path / "fresh.db")  # the real seeding path, not the test cache
    mgr.init_database(seed_defaults=True)
    service = DatabaseService(mgr)
    boards = service.get_workspaces()[0].boards
    assert [b.name for b in boards] == ["Product Roadmap", DEMO_BOARD_NAME]
    _check_demo_board(service, mgr, boards[1].id)


def test_demo_board_can_be_added_on_demand(env):
    mgr, service, board, *_ = env
    demo = service.create_demo_board(board.workspace_id)
    assert demo.name == DEMO_BOARD_NAME and demo.position == 1
    assert len(demo.columns) == 5
    _check_demo_board(service, mgr, demo.id)
    # the "yours" comment is authored by the current actor so it can be edited
    authors = {c["author"] for t in service.get_tasks_for_board(demo.id) for c in service.get_comments(t.id)}
    assert "Ada" in authors


@pytest.mark.parametrize("weekday_offset", range(7))
def test_demo_time_entries_are_consistent_on_every_weekday(tmp_path, monkeypatch, weekday_offset):
    """The demo is generated relative to 'now'; it must be realistic whichever day it is built on."""
    from datetime import timezone

    import kanbanmanager.database.demo_board as demo

    fake_local = datetime(2024, 6, 3, 11, 30) + timedelta(days=weekday_offset)  # Monday + offset

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_local if tz is None else fake_local.astimezone(tz)

    fake_utc = fake_local.astimezone(timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(demo, "datetime", FrozenDatetime)
    monkeypatch.setattr(demo, "utc_now", lambda: fake_utc)

    mgr = DatabaseManager(tmp_path / f"demo{weekday_offset}.db")
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr)
    ws = service.create_workspace("W")
    board = service.create_demo_board(ws.id)

    entries = service.get_time_entries(board_ids=[board.id])
    assert len(entries) >= 10
    tasks = {t.id: t for t in service.get_tasks_for_board(board.id)}
    by_person = {}
    for e in entries:
        start, end = datetime.fromisoformat(e["started_at"]), datetime.fromisoformat(e["ended_at"])
        assert start >= tasks[e["task_id"]].created_at, "time logged before the task existed"
        assert end <= fake_utc, "time logged in the future"
        by_person.setdefault(e["user"], []).append((start, end))
    for person, spans in by_person.items():
        spans.sort()
        for (s1, e1), (s2, _e2) in zip(spans, spans[1:]):
            assert s2 >= e1, f"{person} has overlapping time entries"
    for t in tasks.values():
        assert actions(service, t.id)[0] == A.TASK_CREATED.value
        logged = sum(e["duration_seconds"] for e in entries if e["task_id"] == t.id)
        assert abs((t.spent_hours or 0) - logged / 3600) < 1e-6

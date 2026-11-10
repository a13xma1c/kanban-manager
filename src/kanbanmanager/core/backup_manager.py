"""
Local Data Storage, Backup, Restore, and JSON Export/Import Manager
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from kanbanmanager.config import DEFAULT_DB_PATH, DEFAULT_BACKUP_DIR
from kanbanmanager.core.markdown_utils import rewrite_attachment_refs
from kanbanmanager.core.automation import local_policies, local_rule, portable_policies, portable_rule
from kanbanmanager.database.attachments import AttachmentError, AttachmentStore
from kanbanmanager.database.connection import get_db_manager, DatabaseManager
from kanbanmanager.database.models import (
    Workspace,
    Board,
    BoardColumn,
    Task,
    Label,
    TaskDependency,
    Reminder,
    ActivityLog,
    Comment,
    Attachment,
    TimeEntry,
    TeamMember,
    ProjectBaseline,
    BaselineEntry,
    InboxItem,
    TaskTemplate,
    BoardTemplate,
    utc_now,
)
from kanbanmanager.core import custom_fields as cf
from kanbanmanager.database.service import DatabaseService

# 1.1 adds comments, attachments and per-task history; 1.2 adds assignees, time entries, team capacity;
# 1.3 adds automation rules, board policies and archived tasks;
# 1.4 adds dependency lag/types, milestones, locked dates, schedule settings and baselines;
# 1.5 adds the global Inbox (quick-captured items);
# 1.6 adds typed custom fields (definitions per board, values by field name), task templates
#     (per board and global) and saved board templates
EXPORT_FORMAT_VERSION = "1.6"
ATTACHMENT_MIRROR_DIRNAME = "attachments"


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class BackupManager:
    """Manages SQLite database snapshots and structured JSON export/import."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or get_db_manager()
        self.backup_dir = DEFAULT_BACKUP_DIR
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # SQLITE SNAPSHOT BACKUP & RESTORE
    # -------------------------------------------------------------------------
    def create_database_backup(self, custom_path: Optional[Path | str] = None) -> Path:
        """Create a full, consistent SQLite database snapshot."""
        if custom_path:
            dest = Path(custom_path)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = self.backup_dir / f"kanban_backup_{timestamp}.db"

        dest.parent.mkdir(parents=True, exist_ok=True)

        # Use SQLite backup API for zero-corruption live snapshot
        src_conn = sqlite3.connect(str(self.db.db_path))
        dst_conn = sqlite3.connect(str(dest))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        # Attachment files are not inside the DB; mirror them next to the snapshot
        self.mirror_attachments(dest.parent / ATTACHMENT_MIRROR_DIRNAME)
        return dest

    def mirror_attachments(self, mirror_root: Path) -> int:
        """Copy every referenced attachment blob into ``mirror_root`` (incremental).

        Blobs are content-addressed and immutable, so one mirror can be shared by
        all snapshots in a folder and existing files never need re-copying.
        """
        with self.db.session_scope() as session:
            names = {n for (n,) in session.query(Attachment.stored_name).distinct().all()}
        copied = 0
        for name in names:
            try:
                if self.db.attachment_store.copy_blob_to(name, mirror_root):
                    copied += 1
            except (AttachmentError, OSError):
                continue  # a missing/unreadable blob must not fail the whole backup
        return copied

    def _restore_attachment_blobs(self, mirror_root: Path) -> int:
        """After a DB restore, bring back any referenced blobs missing from the live store."""
        if not mirror_root.is_dir():
            return 0
        mirror = AttachmentStore(mirror_root)
        with self.db.session_scope() as session:
            names = {n for (n,) in session.query(Attachment.stored_name).distinct().all()}
        restored = 0
        for name in names:
            try:
                if not self.db.attachment_store.exists(name) and mirror.copy_blob_to(name, self.db.attachment_store.root):
                    restored += 1
            except (AttachmentError, OSError):
                continue
        return restored

    def list_backups(self) -> List[Dict[str, Any]]:
        """List all automatic and manual backups in the backup directory."""
        results = []
        if not self.backup_dir.exists():
            return results

        for p in sorted(self.backup_dir.glob("*.db"), key=os.path.getmtime, reverse=True):
            stat = p.stat()
            results.append(
                {
                    "filename": p.name,
                    "path": str(p),
                    "size_bytes": stat.st_size,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        return results

    REQUIRED_TABLES = frozenset({"workspaces", "boards", "board_columns", "tasks"})

    def validate_backup(self, backup_path: Path | str) -> None:
        """Raise ValueError unless the file is an intact KanbanManager database."""
        src = Path(backup_path)
        tables: set = set()
        ok = False
        try:
            con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            try:
                ok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            finally:
                con.close()
        except sqlite3.DatabaseError:
            ok = False
        if not ok or not self.REQUIRED_TABLES <= tables:
            raise ValueError(f"'{src.name}' is not a KanbanManager backup (or it is damaged)")

    def restore_database_backup(self, backup_path: Path | str) -> bool:
        """Replace the live database with a snapshot.

        The snapshot is checked first, the current data is saved as a safety snapshot, and
        the pages are copied *into* the live database with SQLite's backup API - which, unlike
        copying files, is correct in WAL mode (recent commits live in the -wal file) and with
        other connections open. Raises FileNotFoundError / ValueError and changes nothing
        when the file can't be used."""
        src = Path(backup_path)
        if not src.exists():
            raise FileNotFoundError(f"Backup file not found at {src}")
        self.validate_backup(src)

        # Safety: snapshot the current data first (through SQLite, so nothing in the WAL is
        # missed). If that fails, don't restore: the user could lose their current work.
        if self.db.db_path.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            self.create_database_backup(self.backup_dir / f"pre_restore_safety_{stamp}.db")

        self.db.engine.dispose()
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        dst_conn = sqlite3.connect(str(self.db.db_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        self.db.engine.dispose()  # no pooled connection may keep pages from before

        # Re-initialize database schema/engine (also migrates snapshots from older versions)
        self.db.init_database(seed_defaults=False)
        self._restore_attachment_blobs(src.parent / ATTACHMENT_MIRROR_DIRNAME)
        return True

    # -------------------------------------------------------------------------
    # JSON EXPORT & IMPORT
    # -------------------------------------------------------------------------
    def export_to_json(
        self,
        export_path: Path | str,
        workspace_id: Optional[int] = None,
        include_attachment_files: bool = True,
    ) -> Path:
        """Export workspace(s), boards, columns, labels, tasks, dependencies, comments,
        attachments (file contents base64-encoded unless disabled) and task history to JSON."""
        dest = Path(export_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        store = self.db.attachment_store

        data: Dict[str, Any] = {
            "version": EXPORT_FORMAT_VERSION,
            "exported_at": datetime.now().isoformat(),
            "workspaces": [],
        }

        with self.db.session_scope() as session:
            board_names = {b.id: b.name for b in session.query(Board)}
            data["inbox"] = []
            for item in session.query(InboxItem).order_by(InboxItem.created_at):
                entry = item.to_dict()
                entry.pop("id", None)
                entry["board_hint"] = board_names.get(entry.pop("board_hint_id"))
                data["inbox"].append(entry)
            data["task_templates"] = [
                {"name": t.name, "icon": t.icon, "data": t.data}
                for t in session.query(TaskTemplate).filter(TaskTemplate.board_id.is_(None))
                .order_by(TaskTemplate.position, TaskTemplate.id)
            ]
            data["board_templates"] = [
                {k: v for k, v in t.to_dict().items() if k not in ("id", "builtin", "created_at")}
                for t in session.query(BoardTemplate).order_by(BoardTemplate.name)
            ]
            data["team_members"] = [m.to_dict() for m in session.query(TeamMember).order_by(TeamMember.name)]
            for m in data["team_members"]:
                m.pop("id", None)
            ws_query = session.query(Workspace)
            if workspace_id:
                ws_query = ws_query.filter(Workspace.id == workspace_id)
            workspaces = ws_query.all()

            for ws in workspaces:
                ws_data = {
                    "name": ws.name,
                    "description": ws.description,
                    "color": ws.color,
                    "icon": ws.icon,
                    "boards": [],
                }
                for board in ws.boards:
                    board_data = {
                        "name": board.name,
                        "description": board.description,
                        "color": board.color,
                        "icon": board.icon,
                        "position": board.position,
                        "columns": [],
                        "labels": [],
                        "tasks": [],
                        "dependencies": [],
                        "automation_rules": [],
                        "policies": {},
                        "custom_fields": [
                            {k: v for k, v in f.to_dict().items()
                             if k in ("name", "field_type", "config", "description", "required", "default",
                                      "show_on_card")}
                            for f in board.custom_fields
                        ],
                        "task_templates": [
                            {"name": t.name, "icon": t.icon, "data": t.data} for t in board.task_templates
                        ],
                    }
                    col_id_map = {}
                    for col in board.columns:
                        col_id_map[col.id] = col.name
                        board_data["columns"].append(
                            {
                                "name": col.name,
                                "position": col.position,
                                "wip_limit": col.wip_limit,
                                "color": col.color,
                                "column_type": col.column_type,
                                "is_done_column": col.is_done_column,
                            }
                        )

                    for lbl in board.labels:
                        board_data["labels"].append(
                            {
                                "name": lbl.name,
                                "color": lbl.color,
                            }
                        )

                    for task in board.tasks:
                        board_data["tasks"].append(
                            {
                                "temp_id": task.id,
                                "column_name": task.column.name if task.column else "",
                                "title": task.title,
                                "description": task.description,
                                "position": task.position,
                                "priority": task.priority,
                                "estimated_hours": task.estimated_hours,
                                "spent_hours": task.spent_hours,
                                "start_date": task.start_date.isoformat() if task.start_date else None,
                                "due_date": task.due_date.isoformat() if task.due_date else None,
                                "is_completed": task.is_completed,
                                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                                "checklist": task.checklist,
                                "custom_fields": task.custom_fields,
                                "is_recurring": task.is_recurring,
                                "recurrence_rule": task.recurrence_rule,
                                "labels": [lbl.name for lbl in task.labels],
                                "assignee": task.assignee,
                                "is_archived": bool(task.is_archived),
                                "is_milestone": bool(task.is_milestone),
                                "dates_locked": bool(task.dates_locked),
                                "archived_at": task.archived_at.isoformat() if task.archived_at else None,
                                "archive_reason": task.archive_reason,
                                "time_entries": [
                                    {
                                        "user": e.user,
                                        "started_at": e.started_at.isoformat() if e.started_at else None,
                                        "ended_at": e.ended_at.isoformat() if e.ended_at else None,
                                        "duration_seconds": e.duration_seconds,
                                        "source": e.source,
                                        "note": e.note,
                                    }
                                    for e in task.time_entries
                                    if e.ended_at is not None  # running timers are not portable
                                ],
                                "comments": [
                                    {
                                        "author": c.author,
                                        "body": c.body,
                                        "created_at": c.created_at.isoformat() if c.created_at else None,
                                        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                                    }
                                    for c in task.comments
                                ],
                                "attachments": [
                                    self._export_attachment(store, a, include_attachment_files)
                                    for a in task.attachments
                                ],
                                "activity": [
                                    {
                                        "action_type": log.action_type,
                                        "details": log.details,
                                        "actor": log.actor,
                                        "field_name": log.field_name,
                                        "old_value": log.old_value,
                                        "new_value": log.new_value,
                                        "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                                    }
                                    for log in sorted(task.activity_logs, key=lambda l: (l.timestamp, l.id))
                                ],
                            }
                        )

                    deps = (
                        session.query(TaskDependency)
                        .join(Task, Task.id == TaskDependency.task_id)
                        .filter(Task.board_id == board.id)
                        .all()
                    )
                    for d in deps:
                        board_data["dependencies"].append(
                            {
                                "task_id": d.task_id,
                                "depends_on_task_id": d.depends_on_task_id,
                                "dependency_type": d.dependency_type,
                                "lag_days": int(d.lag_days or 0),
                            }
                        )

                    col_names = {c.id: c.name for c in board.columns}
                    label_names = {l.id: l.name for l in board.labels}
                    field_names = {f.id: f.name for f in board.custom_fields}
                    template_names = {t.id: t.name for t in board.task_templates}
                    board_data["automation_rules"] = [
                        portable_rule(r.to_dict(), col_names, label_names, field_names, template_names)
                        for r in board.automation_rules
                    ]
                    board_data["policies"] = portable_policies(board.policies, col_names)
                    schedule = dict(board.schedule_settings)
                    shown = schedule.pop("baseline_id", None)
                    board_data["schedule_settings"] = schedule
                    board_data["baselines"] = [
                        {
                            "name": bl.name,
                            "created_by": bl.created_by,
                            "created_at": bl.created_at.isoformat() if bl.created_at else None,
                            "shown": bl.id == shown,
                            "entries": [
                                {
                                    "task_temp_id": e.task_id,
                                    "title": e.title,
                                    "start_date": e.start_date.isoformat(),
                                    "finish_date": e.finish_date.isoformat(),
                                    "duration_days": e.duration_days,
                                    "is_milestone": bool(e.is_milestone),
                                }
                                for e in bl.entries
                            ],
                        }
                        for bl in board.baselines
                    ]

                    ws_data["boards"].append(board_data)
                data["workspaces"].append(ws_data)

        with open(dest, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        return dest

    @staticmethod
    def _export_attachment(store: AttachmentStore, att: Attachment, include_file: bool) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "temp_id": att.id,
            "filename": att.filename,
            "content_type": att.content_type,
            "size_bytes": att.size_bytes,
            "checksum": att.checksum,
            "uploaded_by": att.uploaded_by,
            "created_at": att.created_at.isoformat() if att.created_at else None,
        }
        if include_file:
            try:
                entry["data_base64"] = base64.b64encode(store.path_for(att.stored_name).read_bytes()).decode("ascii")
            except (AttachmentError, OSError):
                entry["data_base64"] = None  # file missing on disk; metadata still exported
        return entry

    def import_from_json(self, import_path: Path | str, target_workspace_id: Optional[int] = None) -> bool:
        """Import data from a structured JSON file."""
        src = Path(import_path)
        if not src.exists():
            raise FileNotFoundError(f"JSON import file not found at {src}")

        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)

        service = DatabaseService(self.db)

        for m_data in data.get("team_members", []) or []:
            if m_data.get("name"):
                existing = {m["name"] for m in service.get_team_members()}
                if m_data["name"] not in existing:  # never overwrite capacity configured locally
                    service.upsert_team_member(
                        m_data["name"],
                        float(m_data.get("weekly_capacity_hours", 40.0)),
                        list(m_data.get("working_days") or [0, 1, 2, 3, 4]),
                    )

        for ws_data in data.get("workspaces", []):
            if target_workspace_id:
                ws = service.get_workspace(target_workspace_id)
                if not ws:
                    ws = service.create_workspace(
                        name=ws_data.get("name", "Imported Workspace"),
                        description=ws_data.get("description", ""),
                        color=ws_data.get("color", "#6366F1"),
                        icon=ws_data.get("icon", "folder"),
                    )
            else:
                ws = service.create_workspace(
                    name=ws_data.get("name", "Imported Workspace"),
                    description=ws_data.get("description", ""),
                    color=ws_data.get("color", "#6366F1"),
                    icon=ws_data.get("icon", "folder"),
                )

            for b_data in ws_data.get("boards", []):
                # Labels come from the file (not the default palette on top of them, which
                # duplicated every default label on import)
                board = service.create_board(
                    workspace_id=ws.id,
                    name=b_data.get("name", "Imported Board"),
                    description=b_data.get("description", ""),
                    color=b_data.get("color", "#3B82F6"),
                    icon=b_data.get("icon", "layout"),
                    columns_def=b_data.get("columns"),
                    labels_def=b_data.get("labels") if "labels" in b_data else None,
                )

                # Re-fetch created columns
                cols = service.get_columns_for_board(board.id)
                col_by_name = {c.name: c for c in cols}
                created_labels = {lbl.name: lbl for lbl in service.get_labels_for_board(board.id)}
                fields = self._import_fields(service, board.id, b_data.get("custom_fields") or [])

                # Create tasks & map temp IDs
                id_map: Dict[int, int] = {}
                for t_data in b_data.get("tasks", []):
                    col = col_by_name.get(t_data.get("column_name")) or (cols[0] if cols else None)
                    if not col:
                        continue

                    lbl_ids = [created_labels[name].id for name in t_data.get("labels", []) if name in created_labels]

                    start_d = None
                    if t_data.get("start_date"):
                        try:
                            start_d = datetime.fromisoformat(t_data["start_date"]).date()
                        except Exception:
                            pass

                    due_d = None
                    if t_data.get("due_date"):
                        try:
                            due_d = datetime.fromisoformat(t_data["due_date"])
                        except Exception:
                            pass

                    # Files exported with history replay that history verbatim instead of
                    # logging the import itself as a fresh creation by the importing user.
                    with service.activity_paused() if "activity" in t_data else nullcontext():
                        new_task = service.create_task(
                            board_id=board.id,
                            column_id=col.id,
                            title=t_data.get("title", "Untitled Task"),
                            description=t_data.get("description", ""),
                            priority=t_data.get("priority", "MEDIUM"),
                            estimated_hours=float(t_data.get("estimated_hours", 0.0)),
                            start_date=start_d,
                            due_date=due_d,
                            label_ids=lbl_ids,
                            checklist=t_data.get("checklist", []),
                            **self._import_field_values(fields, t_data.get("custom_fields") or {}),
                            is_recurring=bool(t_data.get("is_recurring", False)),
                            recurrence_rule=t_data.get("recurrence_rule", {}),
                            assignee=t_data.get("assignee"),
                            is_milestone=bool(t_data.get("is_milestone")),
                            dates_locked=bool(t_data.get("dates_locked")),
                        )

                        if t_data.get("is_completed"):
                            service.update_task(new_task.id, is_completed=True, completed_at=datetime.now())

                        self._import_rich_content(service, board.id, new_task.id, t_data)

                    if t_data.get("is_archived"):
                        with service.activity_paused():
                            service.archive_task(new_task.id, reason=t_data.get("archive_reason"))
                        archived_at = _parse_dt(t_data.get("archived_at"))
                        if archived_at:
                            with self.db.session_scope() as session:
                                session.get(Task, new_task.id).archived_at = archived_at

                    old_id = t_data.get("temp_id")
                    if old_id:
                        id_map[old_id] = new_task.id

                for tt in b_data.get("task_templates") or []:
                    try:
                        service.save_task_template(tt.get("name"), tt.get("data") or {}, board_id=board.id,
                                                   icon=tt.get("icon") or "📋")
                    except ValueError:
                        continue

                # Automation (by column/label/field/template name; references to missing ones stay
                # empty and show as problems)
                label_ids = {l.name: l.id for l in service.get_labels_for_board(board.id)}
                column_ids = {c.name: c.id for c in cols}
                field_ids = {f["name"]: f["id"] for f in service.get_custom_fields(board.id)}
                template_ids = {t["name"]: t["id"] for t in service.get_task_templates(board.id, include_global=False)}
                for r_data in b_data.get("automation_rules", []) or []:
                    try:
                        service.save_automation_rule(board.id, local_rule(r_data, column_ids, label_ids, field_ids,
                                                                          template_ids))
                    except ValueError:
                        continue
                if b_data.get("policies"):
                    service.set_board_policies(board.id, local_policies(b_data["policies"], column_ids))

                # Recreate dependencies (already present in replayed history, if exported)
                replayed = any("activity" in t for t in b_data.get("tasks", []))
                with service.activity_paused() if replayed else nullcontext():
                    self._import_dependencies(service, b_data, id_map)
                self._import_schedule(service, board.id, b_data, id_map)

        self._import_inbox(service, data.get("inbox") or [])
        self._import_templates(service, data)
        return True

    @staticmethod
    def _import_fields(service: DatabaseService, board_id: int, defs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Custom field definitions of an imported board (invalid or unknown-type ones are skipped)."""
        with service.activity_paused():
            for f in defs:
                if not cf.is_known_type(f.get("field_type") or ""):
                    continue
                try:
                    service.create_custom_field(board_id, f.get("name"), f.get("field_type"), config=f.get("config"),
                                                required=bool(f.get("required")), default=f.get("default"),
                                                show_on_card=bool(f.get("show_on_card", True)),
                                                description=f.get("description") or "")
                except (cf.FieldValueError, ValueError):
                    continue
        return service.get_custom_fields(board_id)

    @staticmethod
    def _import_field_values(fields: List[Dict[str, Any]], values: Dict[str, Any]) -> Dict[str, Any]:
        """create_task() arguments for a task's exported values: the board's fields get exactly
        their exported value (no defaults), values that don't fit are dropped, and names without
        a definition (files from before 1.6) become fields of their own."""
        by_name = {f["name"].lower(): f for f in fields}
        given = {str(k).lower(): v for k, v in values.items()}
        field_values: Dict[int, Any] = {}
        for f in fields:
            try:
                field_values[f["id"]] = cf.validate_value(f, given.get(f["name"].lower()))
            except cf.FieldValueError:
                field_values[f["id"]] = None
        legacy = {k: v for k, v in values.items() if str(k).lower() not in by_name}
        return {"field_values": field_values, "custom_fields": legacy}

    @staticmethod
    def _import_templates(service: DatabaseService, data: Dict[str, Any]) -> None:
        for tt in data.get("task_templates") or []:
            try:
                service.save_task_template(tt.get("name"), tt.get("data") or {}, icon=tt.get("icon") or "📋")
            except ValueError:
                continue  # a global template of that name exists already
        existing = {t["name"].lower() for t in service.get_board_templates() if not t.get("builtin")}
        for bt in data.get("board_templates") or []:
            name = (bt.get("name") or "").strip()
            if not name or name.lower() in existing:
                continue
            try:
                service.save_board_template(name, bt.get("data") or {}, description=bt.get("description") or "",
                                            icon=bt.get("icon") or "📋", color=bt.get("color") or "#3B82F6",
                                            category=bt.get("category") or "My templates")
            except ValueError:
                continue

    def _import_inbox(self, service: DatabaseService, items: List[Dict[str, Any]]) -> None:
        boards = {b.name: b.id for ws in service.get_workspaces() for b in ws.boards}
        with self.db.session_scope() as session:
            for e in items:
                if not (e.get("title") or "").strip():
                    continue
                item = InboxItem(title=e["title"][:255], raw_text=e.get("raw_text") or e["title"],
                                 notes=e.get("notes") or None, priority=e.get("priority"),
                                 due_date=_parse_dt(e.get("due_date")), assignee=e.get("assignee") or None,
                                 board_hint_id=boards.get(e.get("board_hint")), source=e.get("source") or "import",
                                 captured_by=e.get("captured_by") or None,
                                 created_at=_parse_dt(e.get("created_at")) or utc_now())
                item.labels = e.get("labels") or []
                session.add(item)

    @staticmethod
    def _import_dependencies(service: DatabaseService, b_data: Dict[str, Any], id_map: Dict[int, int]) -> None:
        for dep_data in b_data.get("dependencies", []):
            old_t_id = dep_data.get("task_id")
            old_dep_on_id = dep_data.get("depends_on_task_id")
            if old_t_id in id_map and old_dep_on_id in id_map:
                service.add_dependency(
                    task_id=id_map[old_t_id],
                    depends_on_task_id=id_map[old_dep_on_id],
                    dependency_type=dep_data.get("dependency_type", "FINISH_TO_START"),
                    lag_days=int(dep_data.get("lag_days") or 0),
                )

    def _import_schedule(self, service: DatabaseService, board_id: int, b_data: Dict[str, Any],
                         id_map: Dict[int, int]) -> None:
        """Schedule settings and baselines (task references re-linked to the imported tasks)."""
        settings = dict(b_data.get("schedule_settings") or {})
        with self.db.session_scope() as session:
            for bl_data in b_data.get("baselines", []) or []:
                baseline = ProjectBaseline(board_id=board_id, name=bl_data.get("name") or "Baseline",
                                           created_by=bl_data.get("created_by"),
                                           created_at=_parse_dt(bl_data.get("created_at")) or utc_now())
                session.add(baseline)
                session.flush()
                for e in bl_data.get("entries", []) or []:
                    try:
                        start = datetime.fromisoformat(e["start_date"]).date()
                        finish = datetime.fromisoformat(e["finish_date"]).date()
                    except (KeyError, TypeError, ValueError):
                        continue
                    session.add(BaselineEntry(
                        baseline_id=baseline.id, task_id=id_map.get(e.get("task_temp_id")),
                        title=e.get("title") or "", start_date=start, finish_date=finish,
                        duration_days=int(e.get("duration_days") or 0), is_milestone=bool(e.get("is_milestone")),
                    ))
                if bl_data.get("shown"):
                    settings["baseline_id"] = baseline.id
        if settings:
            service.set_schedule_settings(board_id, settings)

    def _import_rich_content(self, service: DatabaseService, board_id: int, task_id: int, t_data: Dict[str, Any]) -> None:
        """Recreate comments, attachments and history for an imported task (format >= 1.1)."""
        store = self.db.attachment_store
        ref_map: Dict[str, str] = {}
        for a_data in t_data.get("attachments", []) or []:
            payload = a_data.get("data_base64")
            if not payload:
                continue  # exported without file contents; nothing to restore
            try:
                blob = store.store_bytes(base64.b64decode(payload), a_data.get("filename") or "attachment")
            except (AttachmentError, binascii.Error, ValueError):
                continue
            with self.db.session_scope() as session:
                att = Attachment(
                    task_id=task_id,
                    filename=a_data.get("filename") or "attachment",
                    stored_name=blob.stored_name,
                    content_type=a_data.get("content_type") or "application/octet-stream",
                    size_bytes=blob.size_bytes,
                    checksum=blob.checksum,
                    uploaded_by=a_data.get("uploaded_by"),
                    created_at=_parse_dt(a_data.get("created_at")) or datetime.now(),
                )
                session.add(att)
                session.flush()
                if a_data.get("temp_id") is not None:
                    ref_map[str(a_data["temp_id"])] = str(att.id)

        with self.db.session_scope() as session:
            entries = t_data.get("time_entries")
            for e_data in entries or []:
                start = _parse_dt(e_data.get("started_at"))
                seconds = int(e_data.get("duration_seconds") or 0)
                if start is None or seconds <= 0:
                    continue
                session.add(
                    TimeEntry(
                        task_id=task_id,
                        user=e_data.get("user"),
                        started_at=start,
                        ended_at=_parse_dt(e_data.get("ended_at")) or start + timedelta(seconds=seconds),
                        duration_seconds=seconds,
                        source=e_data.get("source") or "MANUAL",
                        note=e_data.get("note"),
                    )
                )
            task = session.get(Task, task_id)
            if entries is not None and task is not None:
                session.flush()
                service._recompute_spent_hours(session, task_id)
            elif task is not None and t_data.get("spent_hours"):
                # Older exports only carried a number; keep it as one manual entry
                seconds = int(round(float(t_data["spent_hours"]) * 3600))
                if seconds > 0:
                    now = utc_now()
                    session.add(TimeEntry(task_id=task_id, user=None, started_at=now - timedelta(seconds=seconds),
                                          ended_at=now, duration_seconds=seconds, source="MANUAL",
                                          note="Time spent recorded before time tracking"))
                    session.flush()
                    service._recompute_spent_hours(session, task_id)
            if ref_map:
                if task and task.description:
                    task.description = rewrite_attachment_refs(task.description, ref_map)
            for c_data in t_data.get("comments", []) or []:
                if not (c_data.get("body") or "").strip():
                    continue
                session.add(
                    Comment(
                        task_id=task_id,
                        author=c_data.get("author") or "",
                        body=c_data["body"],
                        created_at=_parse_dt(c_data.get("created_at")) or datetime.now(),
                        updated_at=_parse_dt(c_data.get("updated_at")),
                    )
                )
            for l_data in t_data.get("activity", []) or []:
                if not l_data.get("action_type"):
                    continue
                session.add(
                    ActivityLog(
                        board_id=board_id,
                        task_id=task_id,
                        action_type=l_data["action_type"],
                        details=l_data.get("details"),
                        actor=l_data.get("actor"),
                        field_name=l_data.get("field_name"),
                        old_value=l_data.get("old_value"),
                        new_value=l_data.get("new_value"),
                        timestamp=_parse_dt(l_data.get("timestamp")) or datetime.now(),
                    )
                )

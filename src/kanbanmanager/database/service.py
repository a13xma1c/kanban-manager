"""
High-Level Database Service and Repository Layer
"""
from __future__ import annotations

import functools
import json
import logging
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Dict, Any, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import set_committed_value

from kanbanmanager.config import (
    DEFAULT_WEEKLY_CAPACITY_HOURS,
    DEFAULT_WORKING_DAYS,
    get_current_user,
)
from kanbanmanager.database.attachments import AttachmentError, guess_content_type
from kanbanmanager.database.audit import diff_snapshots, field_change_details as from_audit_field_details, snapshot_task
from kanbanmanager.database.connection import DatabaseManager, get_db_manager
from kanbanmanager.database.events import DomainEvent, event_type_for
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
    AppSetting,
    AutomationRule,
    AutomationRun,
    ProjectBaseline,
    BaselineEntry,
    InboxItem,
    CustomField,
    TaskFieldValue,
    TaskTemplate,
    BoardTemplate,
    CalendarSubscription,
    ExternalLink,
    SyncRun,
    utc_now,
)
from kanbanmanager.core import custom_fields as cf
from kanbanmanager.core.custom_fields import FieldValueError
from kanbanmanager.timefmt import format_duration
from kanbanmanager.enums import Priority, ColumnType, DependencyType, ActivityAction, TimeEntrySource


def _dependency_text(kind: DependencyType, lag_days: int) -> str:
    """'finish-to-start', 'start-to-start +2d', 'finish-to-start -1d (lead)'."""
    name = kind.scheduling_type.display_name.lower().replace(" ", "-")
    lag = int(lag_days or 0)
    if lag > 0:
        return f"{name} +{lag}d"
    if lag < 0:
        return f"{name} {lag}d (lead)"
    return name


def _snippet(text: str, limit: int = 80) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


log = logging.getLogger(__name__)


def _publishing(method):
    """Publish events queued during a public call once the outermost call has returned
    (i.e. after its transaction committed). Failed calls publish nothing."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self._call_depth += 1
        succeeded = False
        try:
            result = method(self, *args, **kwargs)
            succeeded = True
            return result
        finally:
            self._call_depth -= 1
            if self._call_depth == 0:
                events, self._pending_events = self._pending_events, []
                if succeeded:
                    for event in events:
                        for listener in list(self._listeners):
                            try:
                                listener(event)
                            except Exception:  # a broken listener must never break the user's action
                                log.exception("Event listener failed for %s", event.type)

    return wrapper


def _publishes_events(cls):
    for name, attr in list(vars(cls).items()):
        if not name.startswith("_") and callable(attr) and not isinstance(attr, (staticmethod, classmethod)):
            setattr(cls, name, _publishing(attr))
    return cls


# Everything Task.to_dict() and the cards read, loaded in one query per relationship
# (selectin) instead of one per task - and without the row explosion of joining three
# collections at once.
_TASK_LOAD_OPTIONS = (
    selectinload(Task.labels),
    selectinload(Task.prerequisites).joinedload(TaskDependency.prerequisite_task),
    selectinload(Task.dependents).joinedload(TaskDependency.dependent_task),
)


@_publishes_events
class DatabaseService:
    """Service providing high-level CRUD operations for the Kanban application."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None, actor: Optional[str] = None):
        self.db = db_manager or get_db_manager()
        # Name recorded as comment author / audit-trail actor
        self.actor = actor or get_current_user()
        self._record_activity = True
        self._listeners: List[Any] = []
        self._pending_events: List[DomainEvent] = []
        self._call_depth = 0

    # -------------------------------------------------------------------------
    # EVENTS
    # -------------------------------------------------------------------------
    def add_event_listener(self, listener) -> None:
        """``listener(DomainEvent)`` is called after every committed, audited change."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_event_listener(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    @contextmanager
    def acting_as(self, actor: str) -> Iterator[None]:
        """Attribute changes to another actor (e.g. an automation rule) within the block."""
        previous = self.actor
        self.actor = actor
        try:
            yield
        finally:
            self.actor = previous

    # -------------------------------------------------------------------------
    # AUDIT TRAIL HELPERS
    # -------------------------------------------------------------------------
    @contextmanager
    def activity_paused(self) -> Iterator[None]:
        """Suppress automatic activity logging (used for imports and internal fix-ups)."""
        previous = self._record_activity
        self._record_activity = False
        try:
            yield
        finally:
            self._record_activity = previous

    def _log(
        self,
        session: Session,
        board_id: int,
        task_id: Optional[int],
        action: ActivityAction,
        details: str,
        field_name: Optional[str] = None,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
        event_data: Optional[Dict[str, Any]] = None,
        publish: bool = True,
    ) -> None:
        if not self._record_activity:
            return
        event_type = event_type_for(action, field_name) if publish else None
        if event_type:
            data = {"field": field_name, "old": old_value, "new": new_value}
            data.update(event_data or {})
            self._pending_events.append(DomainEvent(event_type, board_id, task_id, self.actor, data))
        session.add(
            ActivityLog(
                board_id=board_id,
                task_id=task_id,
                action_type=action.value,
                details=details,
                actor=self.actor,
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
            )
        )

    def _attachment_names_for(self, session: Session, task_filter) -> Set[str]:
        rows = session.query(Attachment.stored_name).join(Task, Task.id == Attachment.task_id).filter(task_filter).all()
        return {name for (name,) in rows}

    def _cleanup_attachment_files(self, stored_names: Iterable[str]) -> None:
        """Delete blobs that are no longer referenced by any attachment row (after commit)."""
        names = set(stored_names)
        if not names:
            return
        with self.db.session_scope() as session:
            still_used = {
                n for (n,) in session.query(Attachment.stored_name).filter(Attachment.stored_name.in_(names)).all()
            }
        for name in names - still_used:
            try:
                self.db.attachment_store.remove(name)
            except OSError:
                pass  # a locked/undeletable file must not break the delete itself

    # -------------------------------------------------------------------------
    # WORKSPACES
    # -------------------------------------------------------------------------
    def get_workspaces(self, include_archived: bool = False) -> List[Workspace]:
        with self.db.session_scope() as session:
            q = session.query(Workspace).options(
                selectinload(Workspace.boards).options(selectinload(Board.columns), selectinload(Board.labels)),
            )
            if not include_archived:
                q = q.filter(Workspace.is_archived == False)
            return q.order_by(Workspace.created_at).all()

    def get_workspace(self, workspace_id: int) -> Optional[Workspace]:
        with self.db.session_scope() as session:
            ws = (
                session.query(Workspace)
                .options(
                    joinedload(Workspace.boards).joinedload(Board.columns),
                    joinedload(Workspace.boards).joinedload(Board.labels),
                )
                .filter(Workspace.id == workspace_id)
                .first()
            )
            if ws:
                for b in ws.boards:
                    _ = b.columns
                    _ = b.labels
            return ws

    def create_workspace(self, name: str, description: str = "", color: str = "#6366F1", icon: str = "folder") -> Workspace:
        with self.db.session_scope() as session:
            ws = Workspace(name=name, description=description, color=color, icon=icon)
            session.add(ws)
            session.flush()
            session.refresh(ws)
            return ws

    def update_workspace(self, workspace_id: int, **kwargs) -> Optional[Workspace]:
        with self.db.session_scope() as session:
            ws = session.query(Workspace).filter(Workspace.id == workspace_id).first()
            if not ws:
                return None
            for key, value in kwargs.items():
                if hasattr(ws, key):
                    setattr(ws, key, value)
            ws.updated_at = datetime.now()
            session.flush()
            session.refresh(ws)
            return ws

    def delete_workspace(self, workspace_id: int) -> bool:
        with self.db.session_scope() as session:
            ws = session.query(Workspace).filter(Workspace.id == workspace_id).first()
            if not ws:
                return False
            board_ids = [b.id for b in ws.boards]
            blobs = self._attachment_names_for(session, Task.board_id.in_(board_ids)) if board_ids else set()
            session.delete(ws)
        self._cleanup_attachment_files(blobs)
        return True

    # -------------------------------------------------------------------------
    # BOARDS
    # -------------------------------------------------------------------------
    def get_boards_for_workspace(self, workspace_id: int, include_archived: bool = False) -> List[Board]:
        with self.db.session_scope() as session:
            q = session.query(Board).filter(Board.workspace_id == workspace_id)
            if not include_archived:
                q = q.filter(Board.is_archived == False)
            return q.order_by(Board.position, Board.id).all()

    def get_board(self, board_id: int) -> Optional[Board]:
        with self.db.session_scope() as session:
            # A fixed handful of queries however big the board is (one per relationship level),
            # with everything the UI reads loaded before the session closes
            return (
                session.query(Board)
                .options(
                    selectinload(Board.columns).selectinload(BoardColumn.tasks).options(*_TASK_LOAD_OPTIONS),
                    selectinload(Board.labels),
                )
                .filter(Board.id == board_id)
                .first()
            )

    def create_board(
        self,
        workspace_id: int,
        name: str,
        description: str = "",
        color: str = "#3B82F6",
        icon: str = "layout",
        columns_def: Optional[List[Dict[str, Any]]] = None,
        labels_def: Optional[List[Dict[str, Any]]] = None,
    ) -> Board:
        """``columns_def``/``labels_def`` default to the standard workflow and label palette.
        Column dicts take "type" or "column_type" (the JSON export's key)."""
        with self.db.session_scope() as session:
            # Determine position
            max_pos = session.query(Board).filter(Board.workspace_id == workspace_id).count()
            board = Board(
                workspace_id=workspace_id,
                name=name,
                description=description,
                color=color,
                icon=icon,
                position=max_pos,
            )
            session.add(board)
            session.flush()

            # Create default columns if none supplied
            from kanbanmanager.config import DEFAULT_COLUMNS, DEFAULT_LABEL_PALETTE

            cols = columns_def or DEFAULT_COLUMNS
            for i, c in enumerate(cols):
                col = BoardColumn(
                    board_id=board.id,
                    name=c["name"],
                    position=i,
                    wip_limit=c.get("wip_limit", 0),
                    color=c.get("color", "#64748B"),
                    column_type=c.get("type") or c.get("column_type") or ColumnType.CUSTOM.value,
                    is_done_column=c.get("is_done_column", False),
                )
                session.add(col)

            # Labels: as given (an empty list = none), else the default palette
            if labels_def is None:
                labels_def = [{"name": n, "color": c} for n, c in DEFAULT_LABEL_PALETTE]
            seen: Set[str] = set()
            for l_def in labels_def:
                lbl_name = (l_def.get("name") or "").strip()
                if lbl_name and lbl_name.lower() not in seen:
                    seen.add(lbl_name.lower())
                    session.add(Label(board_id=board.id, name=lbl_name, color=l_def.get("color") or "#3B82F6"))

            session.flush()
            session.refresh(board)
            _ = board.columns
            _ = board.labels
            return board

    def update_board(self, board_id: int, **kwargs) -> Optional[Board]:
        with self.db.session_scope() as session:
            board = session.query(Board).filter(Board.id == board_id).first()
            if not board:
                return None
            for key, value in kwargs.items():
                if hasattr(board, key):
                    setattr(board, key, value)
            board.updated_at = datetime.now()
            session.flush()
            session.refresh(board)
            return board

    def count_tasks(self, board_ids: Iterable[int], include_archived: bool = True) -> int:
        """How many tasks the boards hold (e.g. for "delete this board?" questions)."""
        ids = list(board_ids)
        if not ids:
            return 0
        with self.db.session_scope() as session:
            q = session.query(func.count(Task.id)).filter(Task.board_id.in_(ids))
            if not include_archived:
                q = q.filter(Task.is_archived == False)
            return int(q.scalar() or 0)

    def delete_board(self, board_id: int) -> bool:
        with self.db.session_scope() as session:
            board = session.query(Board).filter(Board.id == board_id).first()
            if not board:
                return False
            blobs = self._attachment_names_for(session, Task.board_id == board_id)
            session.delete(board)
        self._cleanup_attachment_files(blobs)
        return True

    # -------------------------------------------------------------------------
    # COLUMNS
    # -------------------------------------------------------------------------
    def get_columns_for_board(self, board_id: int) -> List[BoardColumn]:
        with self.db.session_scope() as session:
            return (
                session.query(BoardColumn)
                .filter(BoardColumn.board_id == board_id)
                .order_by(BoardColumn.position, BoardColumn.id)
                .all()
            )

    def create_column(
        self,
        board_id: int,
        name: str,
        color: str = "#64748B",
        column_type: str = ColumnType.CUSTOM.value,
        wip_limit: int = 0,
        is_done_column: bool = False,
        allowed_transitions: Optional[List[int]] = None,
    ) -> BoardColumn:
        with self.db.session_scope() as session:
            max_pos = session.query(BoardColumn).filter(BoardColumn.board_id == board_id).count()
            col = BoardColumn(
                board_id=board_id,
                name=name,
                position=max_pos,
                color=color,
                column_type=column_type,
                wip_limit=wip_limit,
                is_done_column=is_done_column,
                allowed_transitions_json=json.dumps(allowed_transitions or []),
            )
            session.add(col)
            session.flush()
            session.refresh(col)
            return col

    def update_column(self, column_id: int, **kwargs) -> Optional[BoardColumn]:
        with self.db.session_scope() as session:
            col = session.query(BoardColumn).filter(BoardColumn.id == column_id).first()
            if not col:
                return None
            for key, value in kwargs.items():
                if key == "allowed_transitions" and isinstance(value, list):
                    col.allowed_transitions_json = json.dumps(value)
                elif hasattr(col, key):
                    setattr(col, key, value)
            session.flush()
            session.refresh(col)
            return col

    def delete_column(self, column_id: int) -> bool:
        with self.db.session_scope() as session:
            col = session.query(BoardColumn).filter(BoardColumn.id == column_id).first()
            if not col:
                return False
            board_id = col.board_id
            blobs = self._attachment_names_for(session, Task.column_id == column_id)
            session.delete(col)
            session.flush()
            # Re-index remaining columns
            remaining = (
                session.query(BoardColumn)
                .filter(BoardColumn.board_id == board_id)
                .order_by(BoardColumn.position)
                .all()
            )
            for idx, c in enumerate(remaining):
                c.position = idx
        self._cleanup_attachment_files(blobs)
        return True

    def reorder_columns(self, board_id: int, column_ids: List[int]) -> None:
        with self.db.session_scope() as session:
            for idx, col_id in enumerate(column_ids):
                col = session.query(BoardColumn).filter(BoardColumn.id == col_id, BoardColumn.board_id == board_id).first()
                if col:
                    col.position = idx

    # -------------------------------------------------------------------------
    # TASKS
    # -------------------------------------------------------------------------
    def get_task(self, task_id: int) -> Optional[Task]:
        with self.db.session_scope() as session:
            task = (
                session.query(Task)
                .options(
                    joinedload(Task.labels),
                    joinedload(Task.prerequisites).joinedload(TaskDependency.prerequisite_task),
                    joinedload(Task.dependents).joinedload(TaskDependency.dependent_task),
                    joinedload(Task.reminders),
                )
                .filter(Task.id == task_id)
                .first()
            )
            return task

    def get_tasks_for_board(
        self,
        board_id: int,
        search_query: Optional[str] = None,
        priority: Optional[str] = None,
        label_ids: Optional[List[int]] = None,
        is_completed: Optional[bool] = None,
        include_archived: bool = False,
    ) -> List[Task]:
        with self.db.session_scope() as session:
            q = (
                session.query(Task)
                .options(*_TASK_LOAD_OPTIONS)
                .filter(Task.board_id == board_id)
            )
            if not include_archived:
                q = q.filter(Task.is_archived == False)

            if is_completed is not None:
                q = q.filter(Task.is_completed == is_completed)
            if priority:
                q = q.filter(Task.priority == priority)
            if search_query:
                term = f"%{search_query.strip()}%"
                in_fields = (session.query(TaskFieldValue.id)
                             .filter(TaskFieldValue.task_id == Task.id, TaskFieldValue.value_json.ilike(term))
                             .exists())
                q = q.filter((Task.title.ilike(term)) | (Task.description.ilike(term)) | in_fields)

            tasks = q.order_by(Task.position, Task.id).all()

            if label_ids:
                label_set = set(label_ids)
                tasks = [t for t in tasks if any(lbl.id in label_set for lbl in t.labels)]

            return tasks

    def create_task(
        self,
        board_id: int,
        column_id: int,
        title: str,
        description: str = "",
        priority: str = Priority.MEDIUM.value,
        estimated_hours: float = 0.0,
        start_date: Optional[date] = None,
        due_date: Optional[datetime] = None,
        label_ids: Optional[List[int]] = None,
        checklist: Optional[List[Dict[str, Any]]] = None,
        custom_fields: Optional[Dict[str, Any]] = None,
        is_recurring: bool = False,
        recurrence_rule: Optional[Dict[str, Any]] = None,
        assignee: Optional[str] = None,
        is_milestone: bool = False,
        dates_locked: bool = False,
        origin: Optional[str] = None,
        field_values: Optional[Dict[Any, Any]] = None,
    ) -> Task:
        """``field_values``: custom field values by field id (or name); fields not given get their
        default. ``custom_fields`` is the older by-name form (unknown names become text fields).
        Raises FieldValueError for a value that doesn't fit its field."""
        with self.db.session_scope() as session:
            values = self._resolve_field_values(session, board_id, field_values, custom_fields)
            for f in self._board_fields(session, board_id):
                if f.id not in values and f.default is not None:
                    values[f.id] = f.default
            # Check target column if done column
            target_col = session.query(BoardColumn).filter(BoardColumn.id == column_id).first()
            is_done = target_col.is_done_column if target_col else False

            max_pos = session.query(Task).filter(Task.column_id == column_id, Task.is_archived == False).count()

            task = Task(
                board_id=board_id,
                column_id=column_id,
                title=title,
                description=description,
                position=max_pos,
                priority=priority,
                estimated_hours=estimated_hours,
                start_date=start_date,
                due_date=due_date,
                is_completed=is_done,
                completed_at=datetime.now() if is_done else None,
                checklist_json=json.dumps(checklist or []),
                custom_fields_json="{}",
                is_recurring=is_recurring,
                recurrence_rule_json=json.dumps(recurrence_rule or {}),
                assignee=(assignee or "").strip() or None,
                is_milestone=bool(is_milestone),
                dates_locked=bool(dates_locked),
            )

            if label_ids:
                labels = session.query(Label).filter(Label.id.in_(label_ids)).all()
                task.labels = labels

            session.add(task)
            self._apply_field_values(session, task, values)
            session.flush()

            self._log(
                session,
                board_id,
                task.id,
                ActivityAction.TASK_CREATED,
                f"Created task '{title}' in column '{target_col.name if target_col else ''}'"
                + (f" ({origin})" if origin else ""),
                field_name="column",
                new_value=target_col.name if target_col else None,
                event_data={"column_id": column_id},
            )

            session.flush()
            # A brand-new task has no dependencies, comments or attachments: say so instead of
            # reloading it (five fewer queries per task - it adds up in imports)
            for name in ("prerequisites", "dependents"):
                set_committed_value(task, name, [])
            for name in ("comment_count", "attachment_count"):
                set_committed_value(task, name, 0)
            _ = task.labels
            _ = [fv.field for fv in task.field_values]
            return task

    def update_task(self, task_id: int, label_ids: Optional[List[int]] = None, **kwargs) -> Optional[Task]:
        with self.db.session_scope() as session:
            task = session.query(Task).options(joinedload(Task.labels)).filter(Task.id == task_id).first()
            if not task:
                return None

            def column_name(col_id: int) -> str:
                col = session.get(BoardColumn, col_id)
                return col.name if col else ""

            before = snapshot_task(task, column_name(task.column_id))

            if "assignee" in kwargs:
                kwargs["assignee"] = (kwargs["assignee"] or "").strip() or None
            field_values = kwargs.pop("field_values", None)
            legacy_fields = kwargs.pop("custom_fields", None)
            if field_values or legacy_fields:
                values = self._resolve_field_values(session, task.board_id, field_values, legacy_fields)
                self._apply_field_values(session, task, values)

            for key, value in kwargs.items():
                if key == "checklist" and isinstance(value, list):
                    task.checklist_json = json.dumps(value)
                elif key == "recurrence_rule" and isinstance(value, dict):
                    task.recurrence_rule_json = json.dumps(value)
                elif hasattr(task, key):
                    setattr(task, key, value)

            if label_ids is not None:
                labels = session.query(Label).filter(Label.id.in_(label_ids)).all()
                task.labels = labels

            after = snapshot_task(task, column_name(task.column_id))
            for change in diff_snapshots(before, after):
                self._log(
                    session,
                    task.board_id,
                    task.id,
                    change.action,
                    change.details,
                    field_name=change.field_name,
                    old_value=change.old_value,
                    new_value=change.new_value,
                    event_data=change.data if change.data is not None
                    else self._event_data_for(change.field_name, before, after),
                )

            task.updated_at = datetime.now()
            session.flush()
            session.refresh(task)
            _ = task.labels
            _ = task.prerequisites
            _ = task.dependents
            _ = [fv.field for fv in task.field_values]
            return task

    @staticmethod
    def _event_data_for(field: str, before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
        """Machine-readable values for automation (the log itself stores display text)."""
        if field == "column":
            return {"from_column_id": before["column_id"], "to_column_id": after["column_id"]}
        if field in ("priority", "assignee"):
            return {"from": before[field], "to": after[field]}
        if field == "labels":
            return {"added": [l for l in after["labels"] if l not in before["labels"]],
                    "removed": [l for l in before["labels"] if l not in after["labels"]]}
        return {}

    def delete_task(self, task_id: int) -> bool:
        blobs: Set[str] = set()
        with self.db.session_scope() as session:
            task = session.query(Task).filter(Task.id == task_id).first()
            if not task:
                return False
            col_id = task.column_id
            board_id = task.board_id
            title = task.title
            blobs = self._attachment_names_for(session, Task.id == task_id)
            session.delete(task)
            session.flush()

            # Re-index column tasks
            col_tasks = (
                session.query(Task)
                .filter(Task.column_id == col_id, Task.is_archived == False)
                .order_by(Task.position)
                .all()
            )
            for idx, t in enumerate(col_tasks):
                t.position = idx

            # Board-level record survives; the task's own history is removed with it
            self._log(session, board_id, None, ActivityAction.TASK_DELETED, f"Deleted task '{title}'")
        self._cleanup_attachment_files(blobs)
        return True

    def move_task(
        self,
        task_id: int,
        target_column_id: int,
        target_position: int = 0,
    ) -> Tuple[bool, Optional[str]]:
        """
        Move a task to a target column at a specific position.
        Enforces WIP limits and workflow transition constraints.
        Returns (success: bool, error_message: Optional[str]).
        """
        with self.db.session_scope() as session:
            task = session.query(Task).filter(Task.id == task_id).first()
            if not task:
                return False, "Task not found"

            source_column = session.query(BoardColumn).filter(BoardColumn.id == task.column_id).first()
            target_column = session.query(BoardColumn).filter(BoardColumn.id == target_column_id).first()

            if not target_column:
                return False, "Target column not found"

            if target_column.board_id != task.board_id:
                return self._move_to_other_board(session, task, source_column, target_column, target_position)

            # Check workflow allowed transitions if moving cross-column
            if source_column.id != target_column.id:
                allowed = source_column.allowed_transitions
                if allowed and target_column.id not in allowed:
                    return False, f"Workflow transition from '{source_column.name}' to '{target_column.name}' is not allowed"

                # Check WIP limit on target column
                if target_column.wip_limit > 0:
                    current_count = (
                        session.query(Task)
                        .filter(Task.column_id == target_column.id, Task.is_archived == False)
                        .count()
                    )
                    if current_count >= target_column.wip_limit:
                        return False, f"Target column '{target_column.name}' has reached its WIP limit of {target_column.wip_limit} tasks"

            old_col_id = task.column_id
            old_col_name = source_column.name if source_column else ""

            # Remove from old column ordering
            old_tasks = (
                session.query(Task)
                .filter(Task.column_id == old_col_id, Task.id != task.id, Task.is_archived == False)
                .order_by(Task.position)
                .all()
            )
            for idx, t in enumerate(old_tasks):
                t.position = idx

            # Adjust new column positions
            new_tasks = (
                session.query(Task)
                .filter(Task.column_id == target_column_id, Task.id != task.id, Task.is_archived == False)
                .order_by(Task.position)
                .all()
            )

            target_pos = max(0, min(target_position, len(new_tasks)))
            new_tasks.insert(target_pos, task)

            for idx, t in enumerate(new_tasks):
                t.position = idx
                if t.id == task.id:
                    t.column_id = target_column_id

            # If target column is marked as done, auto-mark completed
            was_completed = bool(task.is_completed)
            if target_column.is_done_column:
                task.is_completed = True
                task.completed_at = datetime.now()
            elif source_column.is_done_column and not target_column.is_done_column:
                # Moved out of done column
                task.is_completed = False
                task.completed_at = None

            task.updated_at = datetime.now()

            # Reordering inside a column is not a meaningful history event
            if old_col_id != target_column_id:
                self._log(
                    session,
                    task.board_id,
                    task.id,
                    ActivityAction.TASK_MOVED,
                    f"Moved '{task.title}' from '{old_col_name}' to '{target_column.name}'",
                    field_name="column",
                    old_value=old_col_name,
                    new_value=target_column.name,
                    event_data={"from_column_id": old_col_id, "to_column_id": target_column_id},
                )
            # Completing by moving into a Done column is a completion in its own right
            if task.is_completed and not was_completed:
                self._log(session, task.board_id, task.id, ActivityAction.TASK_COMPLETED, "Marked as complete",
                          field_name="is_completed", old_value="Open", new_value="Complete")
            elif was_completed and not task.is_completed:
                self._log(session, task.board_id, task.id, ActivityAction.TASK_REOPENED, "Reopened task",
                          field_name="is_completed", old_value="Complete", new_value="Open")

            return True, None

    def _move_to_other_board(self, session: Session, task: Task, source_column: BoardColumn,
                             target_column: BoardColumn, target_position: int) -> Tuple[bool, Optional[str]]:
        """Move a task (with its comments, attachments, time and history) to another board.

        Workflow transition rules are per board and don't apply; the target WIP limit does.
        Labels are matched by name on the target board and created there if missing.
        """
        if target_column.wip_limit > 0:
            count = (session.query(Task)
                     .filter(Task.column_id == target_column.id, Task.is_archived == False).count())
            if count >= target_column.wip_limit:
                return False, (f"Target column '{target_column.name}' has reached its WIP limit of "
                               f"{target_column.wip_limit} tasks")
        old_board = session.get(Board, task.board_id)
        new_board = session.get(Board, target_column.board_id)

        # Labels: same names on the new board (created there if needed)
        target_labels = {l.name.lower(): l for l in session.query(Label).filter(Label.board_id == new_board.id)}
        new_labels = []
        for lbl in task.labels:
            match = target_labels.get(lbl.name.lower())
            if match is None:
                match = Label(board_id=new_board.id, name=lbl.name, color=lbl.color)
                session.add(match)
                target_labels[lbl.name.lower()] = match
            new_labels.append(match)
        task.labels = new_labels
        field_notes = self._carry_field_values(session, task, new_board)

        # Close the gap in the old column, make room in the new one
        old_col_id = task.column_id
        for idx, t in enumerate(session.query(Task)
                                .filter(Task.column_id == old_col_id, Task.id != task.id, Task.is_archived == False)
                                .order_by(Task.position).all()):
            t.position = idx
        siblings = (session.query(Task)
                    .filter(Task.column_id == target_column.id, Task.id != task.id, Task.is_archived == False)
                    .order_by(Task.position).all())
        pos = max(0, min(target_position, len(siblings)))
        siblings.insert(pos, task)
        for idx, t in enumerate(siblings):
            t.position = idx

        was_completed = bool(task.is_completed)
        task.board_id = new_board.id
        task.column_id = target_column.id
        if target_column.is_done_column and not was_completed:
            task.is_completed, task.completed_at = True, datetime.now()
        elif was_completed and not target_column.is_done_column:
            task.is_completed, task.completed_at = False, None
        task.updated_at = datetime.now()
        session.flush()

        self._log(session, old_board.id, None, ActivityAction.TASK_MOVED,
                  f"Moved '{task.title}' to board '{new_board.name}'", field_name="board",
                  old_value=old_board.name, new_value=new_board.name)
        self._log(session, new_board.id, task.id, ActivityAction.TASK_MOVED,
                  f"Moved from board '{old_board.name}' ({source_column.name}) to '{new_board.name}' "
                  f"({target_column.name})" + (f". {'; '.join(field_notes)}" if field_notes else ""),
                  field_name="column",
                  old_value=f"{old_board.name} / {source_column.name}", new_value=target_column.name,
                  event_data={"from_column_id": old_col_id, "to_column_id": target_column.id,
                              "from_board_id": old_board.id, "to_board_id": new_board.id})
        if task.is_completed and not was_completed:
            self._log(session, new_board.id, task.id, ActivityAction.TASK_COMPLETED, "Marked as complete",
                      field_name="is_completed", old_value="Open", new_value="Complete")
        elif was_completed and not task.is_completed:
            self._log(session, new_board.id, task.id, ActivityAction.TASK_REOPENED, "Reopened task",
                      field_name="is_completed", old_value="Complete", new_value="Open")
        return True, None

    def toggle_task_completion(self, task_id: int) -> Optional[Task]:
        with self.db.session_scope() as session:
            task = session.query(Task).filter(Task.id == task_id).first()
            if not task:
                return None
            task.is_completed = not task.is_completed
            task.completed_at = datetime.now() if task.is_completed else None
            task.updated_at = datetime.now()
            if task.is_completed:
                self._log(session, task.board_id, task.id, ActivityAction.TASK_COMPLETED, "Marked as complete",
                          field_name="is_completed", old_value="Open", new_value="Complete")
            else:
                self._log(session, task.board_id, task.id, ActivityAction.TASK_REOPENED, "Reopened task",
                          field_name="is_completed", old_value="Complete", new_value="Open")
            session.flush()
            session.refresh(task)
            return task

    # -------------------------------------------------------------------------
    # DEPENDENCIES
    # -------------------------------------------------------------------------
    def add_dependency(
        self,
        task_id: int,
        depends_on_task_id: int,
        dependency_type: str = DependencyType.FINISH_TO_START.value,
        lag_days: int = 0,
    ) -> Tuple[bool, Optional[str]]:
        """
        Add a dependency: `task_id` depends on `depends_on_task_id` (task_id is blocked by depends_on_task_id).
        Checks for self-reference and circular dependency cycles.
        """
        if task_id == depends_on_task_id:
            return False, "A task cannot depend on itself"

        with self.db.session_scope() as session:
            # Either task may have been deleted meanwhile (another window, a calendar sync)
            if session.get(Task, task_id) is None or session.get(Task, depends_on_task_id) is None:
                return False, "Task not found"
            # Check if dependency already exists
            existing = (
                session.query(TaskDependency)
                .filter(TaskDependency.task_id == task_id, TaskDependency.depends_on_task_id == depends_on_task_id)
                .first()
            )
            if existing:
                return True, "Dependency already exists"

            # Check cycle: would adding (task_id -> depends_on_task_id) introduce a cycle?
            # i.e., can we reach task_id starting from depends_on_task_id?
            if self._has_path(session, start_id=depends_on_task_id, target_id=task_id):
                return False, "Cannot add dependency: this would create a circular dependency cycle"

            dep = TaskDependency(
                task_id=task_id,
                depends_on_task_id=depends_on_task_id,
                dependency_type=dependency_type,
                lag_days=int(lag_days or 0),
            )
            session.add(dep)

            task = session.get(Task, task_id)
            prereq = session.get(Task, depends_on_task_id)
            if task and prereq:
                kind = DependencyType(dependency_type)
                detail = (f"Marked as blocked by #{prereq.id} '{prereq.title}'"
                          if kind.scheduling_type == DependencyType.FINISH_TO_START and not lag_days
                          else f"Now depends on #{prereq.id} '{prereq.title}' ({_dependency_text(kind, lag_days)})")
                self._log(session, task.board_id, task.id, ActivityAction.DEPENDENCY_ADDED, detail,
                          field_name="dependency", new_value=prereq.title,
                          event_data={"pred_id": prereq.id, "succ_id": task.id})
            return True, None

    def update_dependency(
        self,
        task_id: int,
        depends_on_task_id: int,
        dependency_type: Optional[str] = None,
        lag_days: Optional[int] = None,
    ) -> bool:
        """Change the type and/or lag (working days; negative = lead) of an existing dependency."""
        with self.db.session_scope() as session:
            dep = (
                session.query(TaskDependency)
                .filter(TaskDependency.task_id == task_id, TaskDependency.depends_on_task_id == depends_on_task_id)
                .first()
            )
            if dep is None:
                return False
            old_text = _dependency_text(DependencyType(dep.dependency_type), dep.lag_days)
            if dependency_type is not None:
                DependencyType(dependency_type)  # validate
                dep.dependency_type = dependency_type
            if lag_days is not None:
                dep.lag_days = int(lag_days)
            new_text = _dependency_text(DependencyType(dep.dependency_type), dep.lag_days)
            if new_text != old_text:
                task, prereq = session.get(Task, task_id), session.get(Task, depends_on_task_id)
                self._log(session, task.board_id, task.id, ActivityAction.DEPENDENCY_UPDATED,
                          f"Changed the dependency on '{prereq.title}' from {old_text} to {new_text}",
                          field_name="dependency", old_value=old_text, new_value=new_text,
                          event_data={"pred_id": prereq.id, "succ_id": task.id})
            return True

    def remove_dependency(self, task_id: int, depends_on_task_id: int) -> bool:
        with self.db.session_scope() as session:
            dep = (
                session.query(TaskDependency)
                .filter(TaskDependency.task_id == task_id, TaskDependency.depends_on_task_id == depends_on_task_id)
                .first()
            )
            if dep:
                task = session.get(Task, task_id)
                prereq = session.get(Task, depends_on_task_id)
                session.delete(dep)
                if task and prereq:
                    self._log(session, task.board_id, task.id, ActivityAction.DEPENDENCY_REMOVED,
                              f"No longer blocked by #{prereq.id} '{prereq.title}'",
                              field_name="dependency", old_value=prereq.title)
                return True
            return False

    def _has_path(self, session: Session, start_id: int, target_id: int) -> bool:
        """Check if target_id is reachable from start_id via existing dependency edges."""
        visited: Set[int] = set()
        queue = [start_id]

        while queue:
            curr = queue.pop(0)
            if curr == target_id:
                return True
            if curr in visited:
                continue
            visited.add(curr)

            # Find all tasks that 'curr' depends on
            prereqs = session.query(TaskDependency.depends_on_task_id).filter(TaskDependency.task_id == curr).all()
            for (p_id,) in prereqs:
                if p_id not in visited:
                    queue.append(p_id)

        return False

    def get_board_dependencies(self, board_id: int) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            deps = (
                session.query(TaskDependency)
                .join(Task, Task.id == TaskDependency.task_id)
                .filter(Task.board_id == board_id)
                .all()
            )
            return [
                {
                    "id": d.id,
                    "task_id": d.task_id,
                    "depends_on_task_id": d.depends_on_task_id,
                    "dependency_type": d.dependency_type,
                    "lag_days": int(d.lag_days or 0),
                }
                for d in deps
            ]

    # -------------------------------------------------------------------------
    # LABELS
    # -------------------------------------------------------------------------
    def get_labels_for_board(self, board_id: int) -> List[Label]:
        with self.db.session_scope() as session:
            return session.query(Label).filter(Label.board_id == board_id).order_by(Label.name).all()

    def create_label(self, board_id: int, name: str, color: str = "#3B82F6") -> Label:
        with self.db.session_scope() as session:
            lbl = Label(board_id=board_id, name=name, color=color)
            session.add(lbl)
            session.flush()
            session.refresh(lbl)
            return lbl

    def update_label(self, label_id: int, name: str, color: str) -> Optional[Label]:
        with self.db.session_scope() as session:
            lbl = session.query(Label).filter(Label.id == label_id).first()
            if lbl:
                lbl.name = name
                lbl.color = color
                session.flush()
                session.refresh(lbl)
                return lbl
            return None

    def delete_label(self, label_id: int) -> bool:
        with self.db.session_scope() as session:
            lbl = session.query(Label).filter(Label.id == label_id).first()
            if lbl:
                session.delete(lbl)
                return True
            return False

    # -------------------------------------------------------------------------
    # REMINDERS
    # -------------------------------------------------------------------------
    def get_pending_reminders(self, cutoff_time: Optional[datetime] = None) -> List[Dict[str, Any]]:
        cutoff = cutoff_time or datetime.now()
        with self.db.session_scope() as session:
            reminders = (
                session.query(Reminder)
                .join(Task, Task.id == Reminder.task_id)
                .filter(
                    Reminder.is_triggered == False,
                    Reminder.is_dismissed == False,
                    Reminder.remind_at <= cutoff,
                    Task.is_completed == False,
                    Task.is_archived == False,
                )
                .all()
            )
            results = []
            for r in reminders:
                results.append(
                    {
                        "id": r.id,
                        "task_id": r.task_id,
                        "task_title": r.task.title if r.task else "Task",
                        "board_id": r.task.board_id if r.task else None,
                        "remind_at": r.remind_at,
                        "message": r.message or f"Task '{r.task.title}' is due soon!",
                    }
                )
            return results

    def create_reminder(
        self,
        task_id: int,
        remind_at: datetime,
        offset_minutes: int = 0,
        message: Optional[str] = None,
    ) -> Reminder:
        with self.db.session_scope() as session:
            r = Reminder(
                task_id=task_id,
                remind_at=remind_at,
                offset_minutes=offset_minutes,
                message=message,
            )
            session.add(r)
            task = session.get(Task, task_id)
            if task:
                self._log(session, task.board_id, task.id, ActivityAction.REMINDER_SET,
                          f"Scheduled a reminder for {remind_at.strftime('%b %d, %Y %H:%M')}",
                          field_name="reminder", new_value=remind_at.strftime("%b %d, %Y %H:%M"))
            session.flush()
            session.refresh(r)
            return r

    def mark_reminder_triggered(self, reminder_id: int) -> None:
        with self.db.session_scope() as session:
            r = session.query(Reminder).filter(Reminder.id == reminder_id).first()
            if r:
                r.is_triggered = True

    def dismiss_reminder(self, reminder_id: int) -> None:
        with self.db.session_scope() as session:
            r = session.query(Reminder).filter(Reminder.id == reminder_id).first()
            if r:
                r.is_dismissed = True

    # -------------------------------------------------------------------------
    # ACTIVITY LOGS
    # -------------------------------------------------------------------------
    def get_board_activities(self, board_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            logs = (
                session.query(ActivityLog)
                .filter(ActivityLog.board_id == board_id)
                .order_by(ActivityLog.timestamp.desc())
                .limit(limit)
                .all()
            )
            return [l.to_dict() for l in logs]

    def get_task_activity(self, task_id: int) -> List[Dict[str, Any]]:
        """Full audit trail for one task, oldest first."""
        with self.db.session_scope() as session:
            logs = (
                session.query(ActivityLog)
                .filter(ActivityLog.task_id == task_id)
                .order_by(ActivityLog.timestamp, ActivityLog.id)
                .all()
            )
            return [l.to_dict() for l in logs]

    # -------------------------------------------------------------------------
    # COMMENTS
    # -------------------------------------------------------------------------
    def get_comments(self, task_id: int) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            comments = (
                session.query(Comment)
                .filter(Comment.task_id == task_id)
                .order_by(Comment.created_at, Comment.id)
                .all()
            )
            return [c.to_dict() for c in comments]

    def add_comment(self, task_id: int, body: str, author: Optional[str] = None,
                    created_at: Optional[datetime] = None) -> Dict[str, Any]:
        """``created_at`` (naive UTC) keeps the original time of an imported comment."""
        body = (body or "").strip()
        if not body:
            raise ValueError("Comment cannot be empty")
        with self.db.session_scope() as session:
            task = session.get(Task, task_id)
            if not task:
                raise ValueError(f"Task {task_id} not found")
            comment = Comment(task_id=task_id, author=author or self.actor, body=body)
            if created_at is not None:
                comment.created_at = created_at
            session.add(comment)
            session.flush()
            self._log(session, task.board_id, task.id, ActivityAction.COMMENT_ADDED,
                      f"Commented: {_snippet(body)}", field_name="comment", new_value=body,
                      event_data={"author": comment.author})
            return comment.to_dict()

    def update_comment(self, comment_id: int, body: str) -> Optional[Dict[str, Any]]:
        body = (body or "").strip()
        if not body:
            raise ValueError("Comment cannot be empty")
        with self.db.session_scope() as session:
            comment = session.get(Comment, comment_id)
            if not comment:
                return None
            if comment.body == body:
                return comment.to_dict()
            old = comment.body
            comment.body = body
            comment.updated_at = utc_now()
            self._log(session, comment.task.board_id, comment.task_id, ActivityAction.COMMENT_EDITED,
                      f"Edited a comment: {_snippet(body)}", field_name="comment", old_value=old, new_value=body)
            session.flush()
            return comment.to_dict()

    def delete_comment(self, comment_id: int) -> bool:
        with self.db.session_scope() as session:
            comment = session.get(Comment, comment_id)
            if not comment:
                return False
            self._log(session, comment.task.board_id, comment.task_id, ActivityAction.COMMENT_DELETED,
                      f"Deleted a comment by {comment.author}: {_snippet(comment.body)}",
                      field_name="comment", old_value=comment.body)
            session.delete(comment)
            return True

    # -------------------------------------------------------------------------
    # ATTACHMENTS
    # -------------------------------------------------------------------------
    def _attachment_dict(self, att: Attachment) -> Dict[str, Any]:
        data = att.to_dict()
        store = self.db.attachment_store
        try:
            path = store.path_for(att.stored_name)
            data["path"] = str(path)
            data["file_exists"] = path.is_file()
        except AttachmentError:
            data["path"] = ""
            data["file_exists"] = False
        return data

    def get_attachments(self, task_id: int) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            rows = (
                session.query(Attachment)
                .filter(Attachment.task_id == task_id)
                .order_by(Attachment.created_at, Attachment.id)
                .all()
            )
            return [self._attachment_dict(a) for a in rows]

    def get_attachment(self, attachment_id: int) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            att = session.get(Attachment, attachment_id)
            return self._attachment_dict(att) if att else None

    def get_attachment_path(self, attachment_id: int) -> Optional[Path]:
        info = self.get_attachment(attachment_id)
        if not info or not info["file_exists"]:
            return None
        return Path(info["path"])

    def add_attachment(self, task_id: int, source_path: Path | str, filename: Optional[str] = None) -> Dict[str, Any]:
        """Copy a file into the attachment store and link it to a task.

        Raises AttachmentError if the file is missing or exceeds the size limit.
        """
        src = Path(source_path)
        name = (filename or src.name).strip() or "attachment"
        with self.db.session_scope() as session:
            task = session.get(Task, task_id)
            if not task:
                raise AttachmentError(f"Task {task_id} not found")
        blob = self.db.attachment_store.store_file(src, name)
        try:
            return self._create_attachment_row(task_id, name, blob)
        except Exception:
            self._cleanup_attachment_files([blob.stored_name])
            raise

    def add_attachment_bytes(self, task_id: int, data: bytes, filename: str) -> Dict[str, Any]:
        blob = self.db.attachment_store.store_bytes(data, filename)
        try:
            return self._create_attachment_row(task_id, filename, blob)
        except Exception:
            self._cleanup_attachment_files([blob.stored_name])
            raise

    def _create_attachment_row(self, task_id: int, filename: str, blob, uploaded_by: Optional[str] = None,
                               created_at: Optional[datetime] = None) -> Dict[str, Any]:
        with self.db.session_scope() as session:
            task = session.get(Task, task_id)
            if not task:
                raise AttachmentError(f"Task {task_id} not found")
            att = Attachment(
                task_id=task_id,
                filename=filename,
                stored_name=blob.stored_name,
                content_type=guess_content_type(filename),
                size_bytes=blob.size_bytes,
                checksum=blob.checksum,
                uploaded_by=uploaded_by or self.actor,
            )
            if created_at:
                att.created_at = created_at
            session.add(att)
            session.flush()
            self._log(session, task.board_id, task.id, ActivityAction.ATTACHMENT_ADDED,
                      f"Attached '{filename}'", field_name="attachment", new_value=filename)
            return self._attachment_dict(att)

    def delete_attachment(self, attachment_id: int) -> bool:
        with self.db.session_scope() as session:
            att = session.get(Attachment, attachment_id)
            if not att:
                return False
            stored_name = att.stored_name
            self._log(session, att.task.board_id, att.task_id, ActivityAction.ATTACHMENT_REMOVED,
                      f"Removed attachment '{att.filename}'", field_name="attachment", old_value=att.filename)
            session.delete(att)
        self._cleanup_attachment_files([stored_name])
        return True

    # -------------------------------------------------------------------------
    # DEMO CONTENT
    # -------------------------------------------------------------------------
    def create_demo_board(self, workspace_id: int) -> Board:
        """Create the guided-tour demo board (rich descriptions, attachments, comments, history)."""
        from kanbanmanager.database.demo_board import build_demo_board

        with self.db.session_scope() as session:
            board = build_demo_board(session, workspace_id, self.db.attachment_store, you=self.actor)
            session.flush()
            session.refresh(board)
            _ = board.columns
            _ = board.labels
            return board

    # -------------------------------------------------------------------------
    # TIME TRACKING
    # -------------------------------------------------------------------------
    @staticmethod
    def _recompute_spent_hours(session: Session, task_id: int) -> None:
        total = sum(
            int(d or 0)
            for (d,) in session.query(TimeEntry.duration_seconds)
            .filter(TimeEntry.task_id == task_id, TimeEntry.ended_at.isnot(None))
            .all()
        )
        task = session.get(Task, task_id)
        if task is not None:
            task.spent_hours = total / 3600.0

    def _time_entry_dict(self, entry: TimeEntry) -> Dict[str, Any]:
        data = entry.to_dict()
        task = entry.task
        data["task_title"] = task.title if task else ""
        data["board_id"] = task.board_id if task else None
        data["board_name"] = task.board.name if task and task.board else ""
        data["workspace_name"] = task.board.workspace.name if task and task.board and task.board.workspace else ""
        data["labels"] = [l.name for l in task.labels] if task else []
        data["assignee"] = (task.assignee or "") if task else ""
        return data

    def get_running_time_entry(self, user: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The open (running) entry for ``user`` (default: current actor), if any."""
        with self.db.session_scope() as session:
            entry = (
                session.query(TimeEntry)
                .filter(TimeEntry.ended_at.is_(None), TimeEntry.user == (user or self.actor))
                .order_by(TimeEntry.started_at.desc())
                .first()
            )
            return self._time_entry_dict(entry) if entry else None

    def start_time_entry(
        self,
        task_id: int,
        source: str = TimeEntrySource.STOPWATCH.value,
        planned_seconds: Optional[int] = None,
        started_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Start tracking time on a task. Any timer already running for this user is stopped first
        (one person can only work on one thing at a time)."""
        running = self.get_running_time_entry()
        if running:
            self.stop_time_entry(running["id"])
        with self.db.session_scope() as session:
            if session.get(Task, task_id) is None:
                raise ValueError(f"Task {task_id} not found")
            entry = TimeEntry(
                task_id=task_id,
                user=self.actor,
                started_at=started_at or utc_now(),
                source=source,
                planned_seconds=planned_seconds,
            )
            session.add(entry)
            session.flush()
            return self._time_entry_dict(entry)

    def stop_time_entry(
        self,
        entry_id: Optional[int] = None,
        ended_at: Optional[datetime] = None,
        min_seconds: int = 0,
        note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Finish a running entry (default: the current user's). Entries shorter than
        ``min_seconds`` are discarded and None is returned."""
        with self.db.session_scope() as session:
            q = session.query(TimeEntry).filter(TimeEntry.ended_at.is_(None))
            q = q.filter(TimeEntry.id == entry_id) if entry_id else q.filter(TimeEntry.user == self.actor)
            entry = q.order_by(TimeEntry.started_at.desc()).first()
            if entry is None:
                return None
            end = ended_at or utc_now()
            seconds = max(0, int((end - entry.started_at).total_seconds()))
            if seconds < min_seconds:
                session.delete(entry)
                return None
            entry.ended_at = end
            entry.duration_seconds = seconds
            if note:
                entry.note = note
            self._recompute_spent_hours(session, entry.task_id)
            source = TimeEntrySource(entry.source).display_name if entry.source in TimeEntrySource._value2member_map_ else entry.source
            self._log(session, entry.task.board_id, entry.task_id, ActivityAction.TIME_LOGGED,
                      f"Logged {format_duration(seconds)} ({source.lower()})",
                      field_name="time", new_value=str(seconds))
            session.flush()
            return self._time_entry_dict(entry)

    def discard_running_time_entry(self, entry_id: int) -> bool:
        with self.db.session_scope() as session:
            entry = session.get(TimeEntry, entry_id)
            if entry is None or entry.ended_at is not None:
                return False
            session.delete(entry)
            return True

    def add_time_entry(
        self,
        task_id: int,
        duration_seconds: int,
        started_at: Optional[datetime] = None,
        note: str = "",
        user: Optional[str] = None,
        source: str = TimeEntrySource.MANUAL.value,
    ) -> Dict[str, Any]:
        """Log time after the fact. ``started_at`` is naive UTC (defaults to now - duration)."""
        duration_seconds = int(duration_seconds or 0)
        if duration_seconds <= 0:
            raise ValueError("Duration must be positive")
        if duration_seconds > 24 * 3600:
            raise ValueError("A single entry cannot exceed 24 hours")
        start = started_at or (utc_now() - timedelta(seconds=duration_seconds))
        with self.db.session_scope() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise ValueError(f"Task {task_id} not found")
            entry = TimeEntry(
                task_id=task_id,
                user=user or self.actor,
                started_at=start,
                ended_at=start + timedelta(seconds=duration_seconds),
                duration_seconds=duration_seconds,
                source=source,
                note=(note or "").strip() or None,
            )
            session.add(entry)
            session.flush()
            self._recompute_spent_hours(session, task_id)
            self._log(session, task.board_id, task_id, ActivityAction.TIME_LOGGED,
                      f"Logged {format_duration(duration_seconds)} (manual)"
                      + (f": {_snippet(note, 60)}" if note and note.strip() else ""),
                      field_name="time", new_value=str(duration_seconds))
            return self._time_entry_dict(entry)

    def update_time_entry(
        self,
        entry_id: int,
        duration_seconds: Optional[int] = None,
        started_at: Optional[datetime] = None,
        note: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            entry = session.get(TimeEntry, entry_id)
            if entry is None or entry.ended_at is None:
                return None
            old = int(entry.duration_seconds or 0)
            if started_at is not None:
                entry.started_at = started_at
            if duration_seconds is not None:
                if int(duration_seconds) <= 0:
                    raise ValueError("Duration must be positive")
                entry.duration_seconds = int(duration_seconds)
            entry.ended_at = entry.started_at + timedelta(seconds=int(entry.duration_seconds))
            if note is not None:
                entry.note = note.strip() or None
            self._recompute_spent_hours(session, entry.task_id)
            if old != entry.duration_seconds:
                details = f"Changed a time entry from {format_duration(old)} to {format_duration(entry.duration_seconds)}"
            else:
                details = "Edited a time entry"
            self._log(session, entry.task.board_id, entry.task_id, ActivityAction.TIME_ENTRY_UPDATED, details,
                      field_name="time", old_value=str(old), new_value=str(entry.duration_seconds))
            session.flush()
            return self._time_entry_dict(entry)

    def delete_time_entry(self, entry_id: int) -> bool:
        with self.db.session_scope() as session:
            entry = session.get(TimeEntry, entry_id)
            if entry is None:
                return False
            task_id, board_id = entry.task_id, entry.task.board_id
            seconds, running = int(entry.duration_seconds or 0), entry.ended_at is None
            session.delete(entry)
            session.flush()
            self._recompute_spent_hours(session, task_id)
            if not running:
                self._log(session, board_id, task_id, ActivityAction.TIME_ENTRY_DELETED,
                          f"Deleted a time entry of {format_duration(seconds)}",
                          field_name="time", old_value=str(seconds))
            return True

    def get_time_entries(
        self,
        task_id: Optional[int] = None,
        board_ids: Optional[List[int]] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        user: Optional[str] = None,
        include_running: bool = False,
    ) -> List[Dict[str, Any]]:
        """Time entries, oldest first. ``start``/``end`` are naive-UTC bounds on ``started_at`` (end exclusive)."""
        with self.db.session_scope() as session:
            q = session.query(TimeEntry).options(
                joinedload(TimeEntry.task).joinedload(Task.board).joinedload(Board.workspace),
                joinedload(TimeEntry.task).joinedload(Task.labels),
            )
            if task_id is not None:
                q = q.filter(TimeEntry.task_id == task_id)
            if board_ids is not None:
                if not board_ids:
                    return []
                q = q.join(Task, Task.id == TimeEntry.task_id).filter(Task.board_id.in_(board_ids))
            if start is not None:
                q = q.filter(TimeEntry.started_at >= start)
            if end is not None:
                q = q.filter(TimeEntry.started_at < end)
            if user is not None:
                q = q.filter(TimeEntry.user == user)
            if not include_running:
                q = q.filter(TimeEntry.ended_at.isnot(None))
            return [self._time_entry_dict(e) for e in q.order_by(TimeEntry.started_at, TimeEntry.id).all()]

    # -------------------------------------------------------------------------
    # TEAM & CAPACITY
    # -------------------------------------------------------------------------
    def get_team_members(self) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            return [m.to_dict() for m in session.query(TeamMember).order_by(TeamMember.name).all()]

    def upsert_team_member(
        self,
        name: str,
        weekly_capacity_hours: Optional[float] = None,
        working_days: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("Name is required")
        if weekly_capacity_hours is not None and not (0 <= weekly_capacity_hours <= 168):
            raise ValueError("Weekly capacity must be between 0 and 168 hours")
        with self.db.session_scope() as session:
            member = session.query(TeamMember).filter(TeamMember.name == name).first()
            if member is None:
                member = TeamMember(name=name, weekly_capacity_hours=DEFAULT_WEEKLY_CAPACITY_HOURS)
                member.working_days = list(DEFAULT_WORKING_DAYS)
                session.add(member)
            if weekly_capacity_hours is not None:
                member.weekly_capacity_hours = float(weekly_capacity_hours)
            if working_days is not None:
                member.working_days = working_days
            session.flush()
            return member.to_dict()

    def delete_team_member(self, name: str) -> bool:
        with self.db.session_scope() as session:
            member = session.query(TeamMember).filter(TeamMember.name == name).first()
            if member is None:
                return False
            session.delete(member)  # tasks keep their assignee text; capacity falls back to the default
            return True

    def get_people(self) -> List[str]:
        """Everyone who can be assigned: team members, existing assignees, and the current user."""
        with self.db.session_scope() as session:
            names = {n for (n,) in session.query(TeamMember.name).all()}
            names |= {n for (n,) in session.query(Task.assignee).filter(Task.assignee.isnot(None)).distinct().all()}
        names.add(self.actor)
        return sorted((n for n in names if n and n.strip()), key=str.lower)

    # -------------------------------------------------------------------------
    # SETTINGS
    # -------------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.db.session_scope() as session:
            row = session.get(AppSetting, key)
            if row is None:
                return default
            try:
                return json.loads(row.value)
            except (TypeError, ValueError):
                return default

    def set_setting(self, key: str, value: Any) -> None:
        with self.db.session_scope() as session:
            row = session.get(AppSetting, key)
            if row is None:
                session.add(AppSetting(key=key, value=json.dumps(value)))
            else:
                row.value = json.dumps(value)

    # -------------------------------------------------------------------------
    # ARCHIVE
    # -------------------------------------------------------------------------
    def archive_task(self, task_id: int, reason: Optional[str] = None) -> bool:
        """Hide a task from every view (restorable). Returns False if missing or already archived."""
        with self.db.session_scope() as session:
            task = session.get(Task, task_id)
            if task is None or task.is_archived:
                return False
            col_id = task.column_id
            task.is_archived = True
            task.archived_at = utc_now()
            task.archive_reason = (reason or "").strip()[:200] or None
            session.flush()
            remaining = (
                session.query(Task)
                .filter(Task.column_id == col_id, Task.is_archived == False)
                .order_by(Task.position)
                .all()
            )
            for idx, t in enumerate(remaining):
                t.position = idx
            self._log(session, task.board_id, task.id, ActivityAction.TASK_ARCHIVED,
                      "Archived task" + (f" ({task.archive_reason})" if task.archive_reason else ""),
                      field_name="archived", old_value="Active", new_value="Archived")
            return True

    def restore_task(self, task_id: int) -> bool:
        """Bring an archived task back, at the bottom of its column (or the first column if it is gone)."""
        with self.db.session_scope() as session:
            task = session.get(Task, task_id)
            if task is None or not task.is_archived:
                return False
            column = session.get(BoardColumn, task.column_id)
            if column is None:
                column = (session.query(BoardColumn).filter(BoardColumn.board_id == task.board_id)
                          .order_by(BoardColumn.position).first())
                task.column_id = column.id
            task.position = (
                session.query(Task).filter(Task.column_id == task.column_id, Task.is_archived == False).count()
            )
            task.is_archived = False
            task.archived_at = None
            task.archive_reason = None
            self._log(session, task.board_id, task.id, ActivityAction.TASK_RESTORED,
                      f"Restored from the archive to '{column.name}'",
                      field_name="archived", old_value="Archived", new_value="Active")
            return True

    def get_archived_tasks(self, board_id: int) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            tasks = (
                session.query(Task)
                .options(joinedload(Task.labels), joinedload(Task.column))
                .filter(Task.board_id == board_id, Task.is_archived == True)
                .order_by(Task.archived_at.desc(), Task.id.desc())
                .all()
            )
            out = []
            for t in tasks:
                data = t.to_dict()
                data["column_name"] = t.column.name if t.column else ""
                out.append(data)
            return out

    def count_archived_tasks(self, board_id: int) -> int:
        with self.db.session_scope() as session:
            return session.query(Task).filter(Task.board_id == board_id, Task.is_archived == True).count()

    # -------------------------------------------------------------------------
    # AUTOMATION RULES, RUN HISTORY & POLICIES
    # -------------------------------------------------------------------------
    def get_automation_rules(self, board_id: Optional[int] = None, enabled_only: bool = False) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            q = session.query(AutomationRule)
            if board_id is not None:
                q = q.filter(AutomationRule.board_id == board_id)
            if enabled_only:
                q = q.filter(AutomationRule.is_enabled == True)
            return [r.to_dict() for r in q.order_by(AutomationRule.board_id, AutomationRule.position,
                                                    AutomationRule.id).all()]

    def get_automation_rule(self, rule_id: int) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            rule = session.get(AutomationRule, rule_id)
            return rule.to_dict() if rule else None

    def save_automation_rule(self, board_id: int, data: Dict[str, Any], rule_id: Optional[int] = None) -> Dict[str, Any]:
        """Create or update a rule from a dict with name, is_enabled, trigger, conditions, actions."""
        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("A rule needs a name")
        if not (data.get("trigger") or {}).get("type"):
            raise ValueError("A rule needs a trigger")
        if not data.get("actions"):
            raise ValueError("A rule needs at least one action")
        with self.db.session_scope() as session:
            if rule_id is None:
                rule = AutomationRule(
                    board_id=board_id,
                    position=session.query(AutomationRule).filter(AutomationRule.board_id == board_id).count(),
                )
                session.add(rule)
            else:
                rule = session.get(AutomationRule, rule_id)
                if rule is None:
                    raise ValueError(f"Rule {rule_id} not found")
            rule.name = name[:120]
            rule.is_enabled = bool(data.get("is_enabled", True))
            rule.trigger_json = json.dumps(data["trigger"])
            rule.conditions_json = json.dumps(data.get("conditions") or [])
            rule.actions_json = json.dumps(data["actions"])
            session.flush()
            return rule.to_dict()

    def set_automation_rule_enabled(self, rule_id: int, enabled: bool) -> None:
        with self.db.session_scope() as session:
            rule = session.get(AutomationRule, rule_id)
            if rule:
                rule.is_enabled = bool(enabled)

    def delete_automation_rule(self, rule_id: int) -> bool:
        with self.db.session_scope() as session:
            rule = session.get(AutomationRule, rule_id)
            if rule is None:
                return False
            session.delete(rule)
            return True

    def record_automation_run(
        self,
        board_id: int,
        source: str,
        rule_name: str,
        trigger: str,
        status: str,
        message: str = "",
        rule_id: Optional[int] = None,
        task_id: Optional[int] = None,
        task_title: Optional[str] = None,
        dedupe_key: Optional[str] = None,
    ) -> None:
        with self.db.session_scope() as session:
            session.add(AutomationRun(
                board_id=board_id, rule_id=rule_id, source=source, rule_name=rule_name[:120], task_id=task_id,
                task_title=(task_title or "")[:255] or None, trigger=trigger, status=status, message=message,
                dedupe_key=dedupe_key,
            ))
            if rule_id is not None and status in ("success", "partial"):
                rule = session.get(AutomationRule, rule_id)
                if rule is not None:
                    rule.run_count = int(rule.run_count or 0) + 1
                    rule.last_run_at = utc_now()

    def has_automation_run(self, source: str, dedupe_key: str, rule_id: Optional[int] = None) -> bool:
        with self.db.session_scope() as session:
            q = session.query(AutomationRun.id).filter(AutomationRun.source == source,
                                                       AutomationRun.dedupe_key == dedupe_key,
                                                       AutomationRun.status != "skipped")
            if rule_id is not None:
                q = q.filter(AutomationRun.rule_id == rule_id)
            return q.first() is not None

    def get_automation_runs(self, board_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            runs = (
                session.query(AutomationRun)
                .filter(AutomationRun.board_id == board_id)
                .order_by(AutomationRun.created_at.desc(), AutomationRun.id.desc())
                .limit(limit)
                .all()
            )
            return [r.to_dict() for r in runs]

    def prune_automation_runs(self, older_than: datetime) -> int:
        with self.db.session_scope() as session:
            return (session.query(AutomationRun).filter(AutomationRun.created_at < older_than)
                    .delete(synchronize_session=False))

    def get_board_policies(self, board_id: int) -> Dict[str, Any]:
        with self.db.session_scope() as session:
            board = session.get(Board, board_id)
            return board.policies if board else {}

    def set_board_policies(self, board_id: int, policies: Dict[str, Any]) -> None:
        with self.db.session_scope() as session:
            board = session.get(Board, board_id)
            if board is not None:
                board.policies = policies

    def get_last_activity_times(self, board_id: int, exclude_actor_prefix: Optional[str] = None) -> Dict[int, datetime]:
        """Latest audit-trail timestamp per (unarchived) task, optionally ignoring some actors
        (automation's own changes must not count as activity). Falls back to created_at."""
        from sqlalchemy import func as sa_func, or_

        with self.db.session_scope() as session:
            q = (session.query(ActivityLog.task_id, sa_func.max(ActivityLog.timestamp))
                 .filter(ActivityLog.board_id == board_id, ActivityLog.task_id.isnot(None)))
            if exclude_actor_prefix:
                q = q.filter(or_(ActivityLog.actor.is_(None), ~ActivityLog.actor.startswith(exclude_actor_prefix)))
            latest = {task_id: ts for task_id, ts in q.group_by(ActivityLog.task_id).all()}
            result = {}
            for task_id, created in (session.query(Task.id, Task.created_at)
                                     .filter(Task.board_id == board_id, Task.is_archived == False).all()):
                result[task_id] = max(filter(None, [latest.get(task_id), created]))
            return result

    def get_label_by_name(self, board_id: int, name: str, create_color: Optional[str] = None) -> Optional[Label]:
        """Find a board label case-insensitively; optionally create it when missing."""
        with self.db.session_scope() as session:
            for lbl in session.query(Label).filter(Label.board_id == board_id).all():
                if lbl.name.lower() == (name or "").strip().lower():
                    return lbl
        if create_color:
            return self.create_label(board_id, name.strip(), create_color)
        return None

    # -------------------------------------------------------------------------
    # SCHEDULING SETTINGS & BASELINES
    # -------------------------------------------------------------------------
    def get_schedule_settings(self, board_id: int) -> Dict[str, Any]:
        with self.db.session_scope() as session:
            board = session.get(Board, board_id)
            return board.schedule_settings if board else {}

    def set_schedule_settings(self, board_id: int, settings: Dict[str, Any]) -> None:
        with self.db.session_scope() as session:
            board = session.get(Board, board_id)
            if board is not None:
                board.schedule_settings = settings

    def create_baseline(self, board_id: int, name: str, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Save a snapshot of planned dates. ``entries``: task_id, title, start_date, finish_date,
        duration_days, is_milestone (see core.scheduling.baseline_entries)."""
        name = (name or "").strip()
        if not name:
            raise ValueError("A baseline needs a name")
        with self.db.session_scope() as session:
            baseline = ProjectBaseline(board_id=board_id, name=name[:120], created_by=self.actor)
            session.add(baseline)
            session.flush()
            for e in entries:
                session.add(BaselineEntry(
                    baseline_id=baseline.id, task_id=e.get("task_id"), title=(e.get("title") or "")[:255],
                    start_date=e["start_date"], finish_date=e["finish_date"],
                    duration_days=int(e.get("duration_days") or 0), is_milestone=bool(e.get("is_milestone")),
                ))
            self._log(session, board_id, None, ActivityAction.BASELINE_SAVED,
                      f"Saved baseline '{baseline.name}' ({len(entries)} tasks)",
                      field_name="baseline", new_value=baseline.name)
            session.flush()
            return baseline.to_dict()

    def get_baselines(self, board_id: int) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            rows = (session.query(ProjectBaseline).filter(ProjectBaseline.board_id == board_id)
                    .order_by(ProjectBaseline.created_at.desc(), ProjectBaseline.id.desc()).all())
            return [b.to_dict() for b in rows]

    def get_baseline(self, baseline_id: int) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            baseline = session.get(ProjectBaseline, baseline_id)
            return baseline.to_dict(include_entries=True) if baseline else None

    def rename_baseline(self, baseline_id: int, name: str) -> bool:
        name = (name or "").strip()
        if not name:
            raise ValueError("A baseline needs a name")
        with self.db.session_scope() as session:
            baseline = session.get(ProjectBaseline, baseline_id)
            if baseline is None:
                return False
            baseline.name = name[:120]
            return True

    def delete_baseline(self, baseline_id: int) -> bool:
        with self.db.session_scope() as session:
            baseline = session.get(ProjectBaseline, baseline_id)
            if baseline is None:
                return False
            board = session.get(Board, baseline.board_id)
            if board is not None and board.schedule_settings.get("baseline_id") == baseline_id:
                settings = board.schedule_settings
                settings["baseline_id"] = None
                board.schedule_settings = settings
            session.delete(baseline)
            return True

    # -------------------------------------------------------------------------
    # GLOBAL INBOX & FOCUS
    # -------------------------------------------------------------------------
    def add_inbox_item(
        self,
        title: str,
        raw_text: str = "",
        notes: str = "",
        priority: Optional[str] = None,
        due_date: Optional[datetime] = None,
        labels: Optional[List[str]] = None,
        assignee: Optional[str] = None,
        board_hint_id: Optional[int] = None,
        source: str = "quick_capture",
    ) -> Dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("Nothing to capture")
        with self.db.session_scope() as session:
            if board_hint_id is not None and session.get(Board, board_hint_id) is None:
                board_hint_id = None
            item = InboxItem(title=title[:255], raw_text=raw_text or title, notes=(notes or "").strip() or None,
                             priority=priority, due_date=due_date, assignee=(assignee or "").strip() or None,
                             board_hint_id=board_hint_id, source=source, captured_by=self.actor)
            item.labels = labels or []
            session.add(item)
            session.flush()
            return item.to_dict()

    def get_inbox_items(self) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            rows = session.query(InboxItem).order_by(InboxItem.created_at.desc(), InboxItem.id.desc()).all()
            return [r.to_dict() for r in rows]

    def count_inbox_items(self) -> int:
        with self.db.session_scope() as session:
            return session.query(InboxItem).count()

    def update_inbox_item(self, item_id: int, **fields) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            item = session.get(InboxItem, item_id)
            if item is None:
                return None
            for key, value in fields.items():
                if key == "labels":
                    item.labels = value
                elif key == "title":
                    if not (value or "").strip():
                        raise ValueError("Title cannot be empty")
                    item.title = value.strip()[:255]
                elif hasattr(item, key):
                    setattr(item, key, value)
            session.flush()
            return item.to_dict()

    def delete_inbox_item(self, item_id: int) -> bool:
        with self.db.session_scope() as session:
            item = session.get(InboxItem, item_id)
            if item is None:
                return False
            session.delete(item)
            return True

    def default_capture_column(self, board_id: int) -> Optional[int]:
        """Where captured work lands on a board: the 'To Do' column, else the first open column."""
        cols = self.get_columns_for_board(board_id)
        for col_type in ("TODO", "BACKLOG"):
            match = next((c for c in cols if c.column_type == col_type and not c.is_done_column), None)
            if match:
                return match.id
        match = next((c for c in cols if not c.is_done_column), None)
        return (match or (cols[0] if cols else None)).id if cols else None

    def triage_inbox_item(self, item_id: int, board_id: int, column_id: Optional[int] = None,
                          title: Optional[str] = None) -> Task:
        """Turn an inbox item into a task on a board (labels matched/created by name) and remove it."""
        with self.db.session_scope() as session:
            item = session.get(InboxItem, item_id)
            if item is None:
                raise ValueError(f"Inbox item {item_id} not found")
            data = item.to_dict()
        column_id = column_id or self.default_capture_column(board_id)
        if column_id is None:
            raise ValueError("That board has no columns")
        label_ids = []
        for name in data["labels"]:
            lbl = self.get_label_by_name(board_id, name, create_color="#64748B")
            if lbl is not None:
                label_ids.append(lbl.id)
        captured = datetime.fromisoformat(data["created_at"]) if data["created_at"] else None
        task = self.create_task(
            board_id=board_id, column_id=column_id, title=(title or data["title"]).strip() or data["title"],
            description=data["notes"], priority=data["priority"] or Priority.MEDIUM.value,
            due_date=datetime.fromisoformat(data["due_date"]) if data["due_date"] else None,
            label_ids=label_ids, assignee=data["assignee"] or None,
            origin=f"from the Inbox, captured {captured:%b %d}" if captured else "from the Inbox",
        )
        self.delete_inbox_item(item_id)
        return task

    def get_focus_tasks(self, person: Optional[str] = None, include_unassigned: bool = False) -> List[Dict[str, Any]]:
        """Open tasks across every workspace and board assigned to ``person`` (default: you).

        Each dict is Task.to_dict() plus board/workspace names and colors.
        """
        person = person or self.actor
        with self.db.session_scope() as session:
            q = (session.query(Task)
                 .options(joinedload(Task.labels), joinedload(Task.board).joinedload(Board.workspace),
                          joinedload(Task.prerequisites).joinedload(TaskDependency.prerequisite_task))
                 .join(Board, Board.id == Task.board_id).join(Workspace, Workspace.id == Board.workspace_id)
                 .filter(Task.is_completed == False, Task.is_archived == False,
                         Board.is_archived == False, Workspace.is_archived == False))
            if include_unassigned:
                q = q.filter((Task.assignee == person) | (Task.assignee.is_(None)))
            else:
                q = q.filter(Task.assignee == person)
            out = []
            for t in q.all():
                data = t.to_dict()
                data["board_name"] = t.board.name
                data["board_color"] = t.board.color
                data["workspace_name"] = t.board.workspace.name
                data["workspace_id"] = t.board.workspace_id
                out.append(data)
            return out

    # -------------------------------------------------------------------------
    # CUSTOM FIELDS (types: core.custom_fields)
    # -------------------------------------------------------------------------
    @staticmethod
    def _board_fields(session: Session, board_id: int) -> List[CustomField]:
        return (session.query(CustomField).filter(CustomField.board_id == board_id)
                .order_by(CustomField.position, CustomField.id).all())

    def _resolve_field_values(self, session: Session, board_id: int, field_values: Optional[Dict[Any, Any]],
                              legacy: Optional[Dict[str, Any]] = None) -> Dict[int, Any]:
        """Validated values by field id. ``field_values`` keys are field ids or names (unknown ones
        raise); ``legacy`` (the old free-form ``custom_fields`` dict) creates missing fields."""
        if not field_values and not legacy:
            return {}
        fields = self._board_fields(session, board_id)
        by_id = {f.id: f for f in fields}
        by_name = {f.name.lower(): f for f in fields}
        raw: Dict[int, Any] = {}
        for key, value in (field_values or {}).items():
            f = by_id.get(key) if isinstance(key, int) else None
            if f is None and isinstance(key, str):
                f = by_id.get(int(key)) if key.strip().isdigit() else by_name.get(" ".join(key.split()).lower())
            if f is None:
                raise FieldValueError(f"This board has no custom field '{key}'")
            raw[f.id] = value
        for name, value in (legacy or {}).items():
            f = by_name.get(" ".join(str(name).split()).lower())
            if f is None:
                if value in (None, "", []):
                    continue
                ftype, config = cf.infer_type([value])
                f = CustomField(board_id=board_id, name=cf.normalize_name(name), field_type=ftype,
                                position=len(by_id), show_on_card=False)
                f.config = config
                session.add(f)
                session.flush()
                by_id[f.id] = f
                by_name[f.name.lower()] = f
                self._log(session, board_id, None, ActivityAction.BOARD_UPDATED,
                          f"Added custom field '{f.name}' ({cf.get_field_type(ftype).label})",
                          field_name="custom_fields", new_value=f.name)
            raw[f.id] = cf.legacy_value(value, f.field_type)
        out: Dict[int, Any] = {}
        for fid, value in raw.items():
            definition = by_id[fid].to_dict()
            out[fid] = cf.validate_value(definition, value) if cf.type_of(definition).editable else value
        return out

    @staticmethod
    def _apply_field_values(session: Session, task: Task, values: Dict[int, Any]) -> None:
        if not values:
            return
        existing = {fv.field_id: fv for fv in task.field_values}
        for fid, value in values.items():
            fv = existing.get(fid)
            if value is None:
                if fv is not None:
                    task.field_values.remove(fv)
                continue
            if fv is None:
                task.field_values.append(TaskFieldValue(field=session.get(CustomField, fid),
                                                        value_json=json.dumps(value, ensure_ascii=False)))
            elif fv.value != value:
                fv.value = value

    def _carry_field_values(self, session: Session, task: Task, new_board: Board) -> List[str]:
        """Re-point a moving task's values at the same-named fields of the new board, creating
        missing fields (and choice options) there. Values that can't be converted are dropped."""
        notes: List[str] = []
        if not task.field_values:
            return notes
        targets = {f.name.lower(): f for f in self._board_fields(session, new_board.id)}
        for fv in list(task.field_values):
            src = fv.field
            if src is None:
                continue
            value = fv.value
            dst = targets.get(src.name.lower())
            if dst is None:
                dst = CustomField(board_id=new_board.id, name=src.name, field_type=src.field_type,
                                  config_json=src.config_json, description=src.description or "",
                                  required=src.required, default_json=src.default_json,
                                  show_on_card=src.show_on_card, position=len(targets))
                session.add(dst)
                session.flush()
                targets[dst.name.lower()] = dst
                notes.append(f"Added field '{dst.name}' to this board")
                fv.field = dst
                continue
            sd, dd = src.to_dict(), dst.to_dict()
            dtype = cf.type_of(dd)
            if dtype.has_options and dtype.editable and value is not None:
                text = value if isinstance(value, list) else cf.type_of(sd).to_text(value, sd["config"])
                labels = text if isinstance(text, list) else (
                    [x.strip() for x in text.split(",")] if dd["field_type"] == "multi_select" else [text])
                colors = {o["label"]: o["color"] for o in sd["config"].get("options", [])}
                merged = cf.merge_options(dd["config"], labels, colors)
                if merged != dd["config"]:
                    dst.config = merged
                    dd = dst.to_dict()
            try:
                new_value = cf.convert_value(value, sd, dd) if dtype.editable else value
            except FieldValueError:
                new_value = None
            if new_value is None or dtype.is_empty(new_value):
                notes.append(f"Dropped {src.name} '{cf.format_value(sd, value)}' (doesn't fit this board's field)")
                task.field_values.remove(fv)
            else:
                fv.field = dst
                fv.value = new_value
        return notes

    def get_custom_fields(self, board_id: int) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            return [f.to_dict() for f in self._board_fields(session, board_id)]

    def get_custom_field(self, field_id: int) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            f = session.get(CustomField, field_id)
            return f.to_dict() if f else None

    @staticmethod
    def _check_field_name(session: Session, board_id: int, name: Any, exclude_id: Optional[int] = None) -> str:
        name = cf.normalize_name(name)
        clash = (session.query(CustomField)
                 .filter(CustomField.board_id == board_id, func.lower(CustomField.name) == name.lower(),
                         CustomField.id != (exclude_id or 0)).first())
        if clash:
            raise FieldValueError(f"This board already has a field named '{clash.name}'")
        return name

    def create_custom_field(self, board_id: int, name: str, field_type: str = "text",
                            config: Optional[Dict[str, Any]] = None, required: bool = False, default: Any = None,
                            show_on_card: bool = True, description: str = "") -> Dict[str, Any]:
        """Add a field to a board. Raises FieldValueError for a bad name, type, config or default."""
        if not cf.is_known_type(field_type):
            raise FieldValueError(f"Unknown field type '{field_type}'")
        ftype = cf.get_field_type(field_type)
        with self.db.session_scope() as session:
            if session.get(Board, board_id) is None:
                raise ValueError("Board not found")
            name = self._check_field_name(session, board_id, name)
            config = ftype.normalize_config(config)
            definition = {"name": "Default value", "field_type": field_type, "config": config}
            default = cf.validate_value(definition, default) if default is not None else None
            position = session.query(CustomField).filter(CustomField.board_id == board_id).count()
            f = CustomField(board_id=board_id, name=name, field_type=field_type, required=bool(required),
                            show_on_card=bool(show_on_card), description=(description or "").strip(),
                            position=position)
            f.config = config
            f.default = default
            session.add(f)
            session.flush()
            self._log(session, board_id, None, ActivityAction.BOARD_UPDATED,
                      f"Added custom field '{name}' ({ftype.label})", field_name="custom_fields", new_value=name)
            return f.to_dict()

    def _plan_field_change(self, f: CustomField, new_def: Dict[str, Any]) -> List[Tuple[TaskFieldValue, Any, str, str]]:
        """(value row, new value or None, old text, new text) for every value that changes when
        field ``f`` gets ``new_def`` (type and config)."""
        old_def = f.to_dict()
        same_type = new_def["field_type"] == old_def["field_type"]
        ntype = cf.type_of(new_def)
        renamed, removed = (cf.option_changes(old_def["config"], new_def["config"])
                            if same_type and ntype.has_options else ({}, []))
        plan = []
        for fv in f.values:
            value = fv.value
            try:
                if same_type:
                    moved = cf.remap_choice_value(ntype, value, renamed, removed) if ntype.has_options else value
                    new_value = cf.validate_value(new_def, moved) if moved is not None and ntype.editable else moved
                else:
                    new_value = cf.convert_value(value, old_def, new_def)
            except FieldValueError:
                new_value = None
            if new_value is not None and ntype.is_empty(new_value):
                new_value = None
            if same_type and new_value == value:
                continue
            old_text = cf.format_value(old_def, value)
            new_text = cf.format_value(new_def, new_value) if new_value is not None else ""
            plan.append((fv, new_value, old_text, new_text))
        return plan

    def preview_field_change(self, field_id: int, field_type: Optional[str] = None,
                             config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """What changing a field's type/config would do to existing values:
        {"values": n in use, "changed": n converted, "lost": [(task title, old text)]}."""
        with self.db.session_scope() as session:
            f = session.get(CustomField, field_id)
            if f is None:
                return {"values": 0, "changed": 0, "lost": []}
            new_type = field_type or f.field_type
            ntype = cf.get_field_type(new_type)
            base = config if config is not None else (f.config if new_type == f.field_type else {})
            new_def = {"id": f.id, "name": f.name, "field_type": new_type, "config": ntype.normalize_config(base)}
            plan = self._plan_field_change(f, new_def)
            return {
                "values": len(f.values),
                "changed": sum(1 for _fv, v, _o, _n in plan if v is not None),
                "lost": [(fv.task.title if fv.task else "?", old) for fv, v, old, _n in plan if v is None],
            }

    def update_custom_field(self, field_id: int, **changes) -> Optional[Dict[str, Any]]:
        """Change name, field_type, config, required, default, show_on_card or description.
        Existing values follow option renames and type changes; values that no longer fit are
        cleared (see ``preview_field_change``). Each affected task records the change."""
        allowed = {"name", "field_type", "config", "required", "default", "show_on_card", "description"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unknown field settings: {', '.join(sorted(unknown))}")
        with self.db.session_scope() as session:
            f = session.get(CustomField, field_id)
            if f is None:
                return None
            old_def = f.to_dict()
            new_type = changes.get("field_type") or f.field_type
            if new_type != f.field_type and not cf.is_known_type(new_type):
                raise FieldValueError(f"Unknown field type '{new_type}'")
            ntype = cf.get_field_type(new_type)
            name = self._check_field_name(session, f.board_id, changes["name"], f.id) if "name" in changes else f.name
            if "config" in changes or new_type != f.field_type:
                base = changes.get("config", old_def["config"] if new_type == f.field_type else {})
                config = ntype.normalize_config(base)
            else:
                config = old_def["config"]
            new_def = {"id": f.id, "name": name, "field_type": new_type, "config": config}

            # Default value: as given, else carried over if it still fits
            if "default" in changes:
                default = (cf.validate_value({**new_def, "name": "Default value"}, changes["default"])
                           if changes["default"] is not None else None)
            elif old_def["default"] is not None and (new_type != f.field_type or config != old_def["config"]):
                try:
                    default = (cf.convert_value(old_def["default"], old_def, new_def)
                               if new_type != f.field_type else cf.validate_value(new_def, old_def["default"]))
                except FieldValueError:
                    default = None
            else:
                default = old_def["default"]

            plan = self._plan_field_change(f, new_def) if (new_type != f.field_type
                                                           or config != old_def["config"]) else []
            for fv, new_value, old_text, new_text in plan:
                task = fv.task
                if new_value is None:
                    session.delete(fv)
                else:
                    fv.value = new_value
                if task is not None and old_text != new_text:
                    self._log(session, task.board_id, task.id, ActivityAction.TASK_UPDATED,
                              from_audit_field_details(name, old_text, new_text) + " (field settings changed)",
                              field_name="custom_field", old_value=old_text or None, new_value=new_text or None,
                              publish=False)

            summary = []
            if name != f.name:
                summary.append(f"renamed to '{name}'")
            if new_type != f.field_type:
                summary.append(f"type {cf.get_field_type(f.field_type).label} → {ntype.label}")
            elif config != old_def["config"]:
                summary.append("settings changed")
            for key, label in (("required", "required"), ("show_on_card", "shown on cards")):
                if key in changes and bool(changes[key]) != old_def[key]:
                    summary.append(f"{'' if changes[key] else 'not '}{label}")
            f.name, f.field_type = name, new_type
            f.config = config
            f.default = default
            for key in ("required", "show_on_card"):
                if key in changes:
                    setattr(f, key, bool(changes[key]))
            if "description" in changes:
                f.description = (changes["description"] or "").strip()
            if summary:
                self._log(session, f.board_id, None, ActivityAction.BOARD_UPDATED,
                          f"Custom field '{old_def['name']}': {', '.join(summary)}", field_name="custom_fields",
                          old_value=old_def["name"], new_value=name)
            session.flush()
            return f.to_dict()

    def delete_custom_field(self, field_id: int) -> bool:
        """Delete a field and its values (each task with a value records the removal)."""
        with self.db.session_scope() as session:
            f = session.get(CustomField, field_id)
            if f is None:
                return False
            definition = f.to_dict()
            for fv in f.values:
                if fv.task is not None and not cf.is_empty(definition, fv.value):
                    text = cf.format_value(definition, fv.value)
                    self._log(session, fv.task.board_id, fv.task_id, ActivityAction.TASK_UPDATED,
                              f"Field '{f.name}' was deleted (was {text[:60]})", field_name="custom_field",
                              old_value=text, publish=False)
            self._log(session, f.board_id, None, ActivityAction.BOARD_UPDATED,
                      f"Deleted custom field '{f.name}' ({len(f.values)} value{'s' if len(f.values) != 1 else ''})",
                      field_name="custom_fields", old_value=f.name)
            board_id = f.board_id
            session.delete(f)
            session.flush()
            for idx, other in enumerate(self._board_fields(session, board_id)):
                other.position = idx
            return True

    def reorder_custom_fields(self, board_id: int, field_ids: List[int]) -> None:
        with self.db.session_scope() as session:
            fields = {f.id: f for f in self._board_fields(session, board_id)}
            ordered = [fields[i] for i in field_ids if i in fields]
            ordered += [f for f in fields.values() if f not in ordered]
            for idx, f in enumerate(ordered):
                f.position = idx

    def count_field_values(self, field_id: int) -> int:
        with self.db.session_scope() as session:
            return session.query(TaskFieldValue).filter(TaskFieldValue.field_id == field_id).count()

    def field_values_in_use(self, field_id: int) -> List[Any]:
        """Distinct non-empty values of a field on its board's (unarchived) tasks."""
        with self.db.session_scope() as session:
            rows = (session.query(TaskFieldValue.value_json).join(Task, Task.id == TaskFieldValue.task_id)
                    .filter(TaskFieldValue.field_id == field_id, Task.is_archived == False).distinct().all())
        out = []
        for (raw,) in rows:
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if value not in (None, "", []) and value not in out:
                out.append(value)
        return out

    def set_task_field_values(self, task_id: int, values: Dict[Any, Any]) -> Optional[Task]:
        """Set some custom field values of a task (by field id or name; None clears)."""
        return self.update_task(task_id, field_values=values)

    # -------------------------------------------------------------------------
    # TASK TEMPLATES (format & expansion: core.templates)
    # -------------------------------------------------------------------------
    def get_task_templates(self, board_id: Optional[int] = None, include_global: bool = True) -> List[Dict[str, Any]]:
        """Templates of a board (plus the "all boards" ones), or only the global ones (board_id None)."""
        with self.db.session_scope() as session:
            q = session.query(TaskTemplate)
            if board_id is None:
                q = q.filter(TaskTemplate.board_id.is_(None))
            elif include_global:
                q = q.filter((TaskTemplate.board_id == board_id) | TaskTemplate.board_id.is_(None))
            else:
                q = q.filter(TaskTemplate.board_id == board_id)
            rows = q.all()
            rows.sort(key=lambda t: (t.board_id is None, t.position, t.name.lower()))
            return [t.to_dict() for t in rows]

    def get_task_template(self, template_id: int) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            t = session.get(TaskTemplate, template_id)
            return t.to_dict() if t else None

    def save_task_template(self, name: str, data: Dict[str, Any], board_id: Optional[int] = None, icon: str = "📋",
                           template_id: Optional[int] = None) -> Dict[str, Any]:
        """Create or update a task template (``board_id`` None = available on every board)."""
        from kanbanmanager.core import templates as tpl

        name = " ".join(str(name or "").split())[:120]
        if not name:
            raise ValueError("A template needs a name")
        data = tpl.normalize_task_template(data)
        with self.db.session_scope() as session:
            clash = (session.query(TaskTemplate)
                     .filter(func.lower(TaskTemplate.name) == name.lower(), TaskTemplate.id != (template_id or 0),
                             TaskTemplate.board_id.is_(None) if board_id is None else TaskTemplate.board_id == board_id)
                     .first())
            if clash:
                raise ValueError(f"There is already a template named '{clash.name}'"
                                 + (" on this board" if board_id else " for all boards"))
            if template_id:
                t = session.get(TaskTemplate, template_id)
                if t is None:
                    raise ValueError("Template not found")
                created = False
            else:
                position = session.query(TaskTemplate).filter(
                    TaskTemplate.board_id.is_(None) if board_id is None else TaskTemplate.board_id == board_id).count()
                t = TaskTemplate(position=position, created_by=self.actor)
                session.add(t)
                created = True
            t.name, t.board_id, t.icon = name, board_id, (icon or "📋")[:16]
            t.data = data
            t.updated_at = utc_now()
            session.flush()
            if board_id:
                self._log(session, board_id, None, ActivityAction.BOARD_UPDATED,
                          f"{'Added' if created else 'Updated'} task template '{name}'", field_name="task_templates",
                          new_value=name)
            return t.to_dict()

    def delete_task_template(self, template_id: int) -> bool:
        with self.db.session_scope() as session:
            t = session.get(TaskTemplate, template_id)
            if t is None:
                return False
            if t.board_id:
                self._log(session, t.board_id, None, ActivityAction.BOARD_UPDATED,
                          f"Deleted task template '{t.name}'", field_name="task_templates", old_value=t.name)
            session.delete(t)
            return True

    def task_template_from_task(self, task_id: int) -> Dict[str, Any]:
        """Template data capturing a task's content (see core.templates.template_from_task)."""
        from kanbanmanager.core import templates as tpl

        return tpl.template_from_task(self, task_id)

    def create_task_from_template(self, template: Any, board_id: int, column_id: Optional[int] = None,
                                  title: Optional[str] = None, now: Optional[datetime] = None) -> Task:
        """Create a task from a template (id or template dict) on ``board_id``."""
        from kanbanmanager.core import templates as tpl

        if not isinstance(template, dict):
            template = self.get_task_template(int(template))
            if template is None:
                raise ValueError("Template not found")
        return tpl.create_task_from_template(self, template, board_id, column_id=column_id, title=title, now=now)

    # -------------------------------------------------------------------------
    # BOARD TEMPLATES (built-ins & format: core.templates)
    # -------------------------------------------------------------------------
    def get_board_templates(self) -> List[Dict[str, Any]]:
        """Built-in templates followed by the ones you saved."""
        from kanbanmanager.core import templates as tpl

        with self.db.session_scope() as session:
            saved = [t.to_dict() for t in session.query(BoardTemplate).order_by(BoardTemplate.name)]
        return tpl.builtin_board_templates() + saved

    def get_board_template(self, template_id: Any) -> Optional[Dict[str, Any]]:
        from kanbanmanager.core import templates as tpl

        if isinstance(template_id, str) and template_id.startswith(tpl.BUILTIN_PREFIX):
            return next((t for t in tpl.builtin_board_templates() if t["id"] == template_id), None)
        with self.db.session_scope() as session:
            t = session.get(BoardTemplate, int(template_id))
            return t.to_dict() if t else None

    def save_board_template(self, name: str, data: Dict[str, Any], description: str = "", icon: str = "📋",
                            color: str = "#3B82F6", category: str = "My templates",
                            template_id: Optional[int] = None) -> Dict[str, Any]:
        from kanbanmanager.core import templates as tpl

        name = " ".join(str(name or "").split())[:120]
        if not name:
            raise ValueError("A template needs a name")
        data = tpl.normalize_board_template(data)
        with self.db.session_scope() as session:
            if template_id:
                t = session.get(BoardTemplate, template_id)
                if t is None:
                    raise ValueError("Template not found")
            else:
                t = BoardTemplate(created_by=self.actor)
                session.add(t)
            t.name, t.description, t.icon = name, (description or "").strip(), (icon or "📋")[:16]
            t.color, t.category = color or "#3B82F6", (category or "My templates")[:60]
            t.data = data
            t.updated_at = utc_now()
            session.flush()
            return t.to_dict()

    def delete_board_template(self, template_id: Any) -> bool:
        """Delete a saved template (built-ins can't be deleted)."""
        if isinstance(template_id, str) and not template_id.isdigit():
            return False
        with self.db.session_scope() as session:
            t = session.get(BoardTemplate, int(template_id))
            if t is None:
                return False
            session.delete(t)
            return True

    def save_board_as_template(self, board_id: int, name: str, description: str = "", include_tasks: bool = False,
                               include_automation: bool = True, icon: str = "📋",
                               category: str = "My templates") -> Dict[str, Any]:
        """Capture a board's structure (columns, labels, fields, task templates, automation and
        schedule settings; optionally its open tasks as samples) as a reusable template."""
        from kanbanmanager.core import templates as tpl

        board = self.get_board(board_id)
        if board is None:
            raise ValueError("Board not found")
        data = tpl.board_to_template(self, board_id, include_tasks=include_tasks,
                                     include_automation=include_automation)
        return self.save_board_template(name, data, description=description, icon=icon, color=board.color,
                                        category=category)

    def create_board_from_template(self, workspace_id: int, template: Any, name: Optional[str] = None,
                                   color: Optional[str] = None, description: Optional[str] = None,
                                   include_sample_tasks: bool = True) -> Board:
        """New board from a template (id - "builtin:<key>" or a saved one - or a template dict)."""
        from kanbanmanager.core import templates as tpl

        if not isinstance(template, dict):
            found = self.get_board_template(template)
            if found is None:
                raise ValueError("Template not found")
            template = found
        return tpl.instantiate_board_template(self, workspace_id, template, name=name, color=color,
                                              description=description, include_sample_tasks=include_sample_tasks)

    # -------------------------------------------------------------------------
    # CALENDAR SUBSCRIPTIONS & EXTERNAL LINKS (sync logic: core.calendar_sync)
    # -------------------------------------------------------------------------
    SUBSCRIPTION_FIELDS = ("name", "kind", "url", "username", "password", "board_id", "column_id", "direction",
                           "interval_minutes", "enabled", "options")

    def get_calendar_subscriptions(self, include_secret: bool = False) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            return [s.to_dict(include_secret) for s in
                    session.query(CalendarSubscription).order_by(CalendarSubscription.name, CalendarSubscription.id)]

    def get_calendar_subscription(self, sub_id: int, include_secret: bool = False) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            sub = session.get(CalendarSubscription, sub_id)
            return sub.to_dict(include_secret) if sub else None

    def save_calendar_subscription(self, data: Dict[str, Any], sub_id: Optional[int] = None) -> Dict[str, Any]:
        """Create or update a subscription. ``password`` None keeps the stored one ("" clears it)."""
        unknown = set(data) - set(self.SUBSCRIPTION_FIELDS)
        if unknown:
            raise ValueError(f"Unknown subscription settings: {', '.join(sorted(unknown))}")
        with self.db.session_scope() as session:
            if sub_id is None:
                sub = CalendarSubscription()  # added once valid (queries below would autoflush it)
            else:
                sub = session.get(CalendarSubscription, sub_id)
                if sub is None:
                    raise ValueError("Subscription not found")
            merged = {**(sub.to_dict(True) if sub_id else {}), **data}
            name = " ".join(str(merged.get("name") or "").split())
            url = str(merged.get("url") or "").strip()
            if not name:
                raise ValueError("Give the calendar a name")
            if not url:
                raise ValueError("Enter the calendar's address (URL)")
            if merged.get("kind", "ics") not in ("ics", "caldav"):
                raise ValueError("Unknown calendar type")
            if merged.get("direction", "pull") not in ("pull", "push", "two_way"):
                raise ValueError("Unknown sync direction")
            if merged.get("kind", "ics") == "ics" and merged.get("direction", "pull") != "pull":
                raise ValueError("iCalendar feeds are read-only: use CalDAV to sync both ways")
            board = session.get(Board, merged.get("board_id"))
            if board is None:
                raise ValueError("Choose the board that receives the calendar's items")
            column_id = merged.get("column_id")
            if column_id and session.get(BoardColumn, column_id) is None:
                column_id = None
            sub.name, sub.url, sub.kind = name[:120], url, merged.get("kind", "ics")
            sub.username = (merged.get("username") or "").strip() or None
            if "password" in data and data["password"] is not None:
                sub.password = data["password"] or None
            sub.board_id, sub.column_id = board.id, column_id
            sub.direction = merged.get("direction", "pull")
            sub.interval_minutes = max(0, min(24 * 60, int(merged.get("interval_minutes") or 0)))
            sub.enabled = bool(merged.get("enabled", True))
            sub.options = dict(merged.get("options") or {})
            if sub_id is None:
                session.add(sub)
            session.flush()
            return sub.to_dict()

    def delete_calendar_subscription(self, sub_id: int) -> bool:
        """Forget a subscription (its tasks stay on the board, unlinked)."""
        with self.db.session_scope() as session:
            sub = session.get(CalendarSubscription, sub_id)
            if sub is None:
                return False
            session.query(ExternalLink).filter(ExternalLink.subscription_id == sub_id).delete()
            session.delete(sub)
            return True

    def set_subscription_status(self, sub_id: int, status: str, message: str) -> None:
        with self.db.session_scope() as session:
            sub = session.get(CalendarSubscription, sub_id)
            if sub is not None:
                sub.last_sync_at, sub.last_status, sub.last_message = utc_now(), status, message

    def get_external_links(self, source: str) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            return [l.to_dict() for l in session.query(ExternalLink).filter(ExternalLink.source == source)]

    def get_external_link_for_task(self, task_id: int, source: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as session:
            q = session.query(ExternalLink).filter(ExternalLink.task_id == task_id)
            if source:
                q = q.filter(ExternalLink.source == source)
            link = q.first()
            return link.to_dict() if link else None

    def save_external_link(self, source: str, uid: str, subscription_id: Optional[int] = None, **fields) -> Dict[str, Any]:
        """Create or update the link (source, uid); fields: task_id, href, etag, local_hash,
        remote_hash, remote_data."""
        allowed = {"task_id", "href", "etag", "local_hash", "remote_hash", "remote_data"}
        with self.db.session_scope() as session:
            link = session.query(ExternalLink).filter(ExternalLink.source == source, ExternalLink.uid == uid).first()
            if link is None:
                link = ExternalLink(source=source, uid=uid[:500], subscription_id=subscription_id)
                session.add(link)
            for k, v in fields.items():
                if k in allowed:
                    setattr(link, k, v)
            link.last_synced_at = utc_now()
            session.flush()
            return link.to_dict()

    def delete_external_link(self, source: str, uid: str) -> bool:
        with self.db.session_scope() as session:
            n = session.query(ExternalLink).filter(ExternalLink.source == source, ExternalLink.uid == uid).delete()
            return bool(n)

    def record_sync_run(self, subscription_id: int, started_at: datetime, status: str, counts: Dict[str, int],
                        message: str) -> Dict[str, Any]:
        with self.db.session_scope() as session:
            run = SyncRun(subscription_id=subscription_id, started_at=started_at, finished_at=utc_now(),
                          status=status, message=message,
                          **{k: int(counts.get(k, 0)) for k in ("created", "updated", "removed", "pushed",
                                                                "conflicts")})
            session.add(run)
            session.flush()
            # keep the last 50 runs per subscription
            old = (session.query(SyncRun.id).filter(SyncRun.subscription_id == subscription_id)
                   .order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).offset(50).all())
            if old:
                session.query(SyncRun).filter(SyncRun.id.in_([r[0] for r in old])).delete(synchronize_session=False)
            return run.to_dict()

    def get_sync_runs(self, subscription_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        with self.db.session_scope() as session:
            return [r.to_dict() for r in session.query(SyncRun).filter(SyncRun.subscription_id == subscription_id)
                    .order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(limit)]

    def instance_id(self) -> str:
        """A random id for this database, used in iCalendar UIDs of exported tasks."""
        value = self.get_setting("instance_id")
        if not value:
            import uuid

            value = uuid.uuid4().hex[:12]
            self.set_setting("instance_id", value)
        return value

    def log_task_note(self, task_id: int, details: str, field_name: str = "sync") -> None:
        """Add an informational entry to a task's history (not an edit; publishes no event)."""
        with self.db.session_scope() as session:
            task = session.get(Task, task_id)
            if task is not None:
                self._log(session, task.board_id, task.id, ActivityAction.TASK_UPDATED, details,
                          field_name=field_name, publish=False)

    def get_flow_history(self, board_id: int) -> List[Dict[str, Any]]:
        """Chronological task creations, moves, completions and archive events on a board
        (the raw material of the cumulative flow, burn-up and cycle-time charts)."""
        wanted = [a.value for a in (ActivityAction.TASK_CREATED, ActivityAction.TASK_MOVED,
                                    ActivityAction.TASK_COMPLETED, ActivityAction.TASK_REOPENED,
                                    ActivityAction.TASK_ARCHIVED, ActivityAction.TASK_RESTORED)]
        with self.db.session_scope() as session:
            rows = (session.query(ActivityLog.task_id, ActivityLog.action_type, ActivityLog.field_name,
                                  ActivityLog.old_value, ActivityLog.new_value, ActivityLog.timestamp)
                    .filter(ActivityLog.board_id == board_id, ActivityLog.task_id.isnot(None),
                            ActivityLog.action_type.in_(wanted))
                    .order_by(ActivityLog.timestamp, ActivityLog.id).all())
            return [{"task_id": r[0], "action": r[1], "field": r[2], "old": r[3], "new": r[4], "at": r[5]}
                    for r in rows]

    def search_tasks(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Open-first title search across every board (the command palette's task list)."""
        query = (query or "").strip()
        if not query:
            return []
        with self.db.session_scope() as session:
            rows = (session.query(Task, Board.name, Workspace.name)
                    .join(Board, Board.id == Task.board_id).join(Workspace, Workspace.id == Board.workspace_id)
                    .filter(Task.title.ilike(f"%{query}%"), Task.is_archived == False,
                            Board.is_archived == False, Workspace.is_archived == False)
                    .order_by(Task.is_completed, Task.updated_at.desc()).limit(limit).all())
            return [{"id": t.id, "title": t.title, "board_id": t.board_id, "board_name": board,
                     "workspace_name": ws, "is_completed": bool(t.is_completed)} for t, board, ws in rows]

"""
High-Level Database Service and Repository Layer
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Dict, Any, Set, Tuple

from sqlalchemy.orm import Session, joinedload

from kanbanmanager.config import (
    DEFAULT_WEEKLY_CAPACITY_HOURS,
    DEFAULT_WORKING_DAYS,
    get_current_user,
)
from kanbanmanager.database.attachments import AttachmentError, guess_content_type
from kanbanmanager.database.audit import diff_snapshots, snapshot_task
from kanbanmanager.database.connection import DatabaseManager, get_db_manager
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
    utc_now,
)
from kanbanmanager.timefmt import format_duration
from kanbanmanager.enums import Priority, ColumnType, DependencyType, ActivityAction, TimeEntrySource


def _snippet(text: str, limit: int = 80) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class DatabaseService:
    """Service providing high-level CRUD operations for the Kanban application."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None, actor: Optional[str] = None):
        self.db = db_manager or get_db_manager()
        # Name recorded as comment author / audit-trail actor
        self.actor = actor or get_current_user()
        self._record_activity = True

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
    ) -> None:
        if not self._record_activity:
            return
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
                joinedload(Workspace.boards).joinedload(Board.columns),
                joinedload(Workspace.boards).joinedload(Board.labels),
            )
            if not include_archived:
                q = q.filter(Workspace.is_archived == False)
            workspaces = q.order_by(Workspace.created_at).all()
            # Eager load while session is open
            for ws in workspaces:
                for b in ws.boards:
                    _ = b.columns
                    _ = b.labels
            return workspaces

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
            board = (
                session.query(Board)
                .options(
                    joinedload(Board.columns).joinedload(BoardColumn.tasks).joinedload(Task.labels),
                    joinedload(Board.labels),
                )
                .filter(Board.id == board_id)
                .first()
            )
            if board:
                # Eagerly load all relationships before session closes
                for col in board.columns:
                    for t in col.tasks:
                        _ = t.labels
                        _ = t.prerequisites
                        _ = t.dependents
            return board

    def create_board(
        self,
        workspace_id: int,
        name: str,
        description: str = "",
        color: str = "#3B82F6",
        icon: str = "layout",
        columns_def: Optional[List[Dict[str, Any]]] = None,
    ) -> Board:
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
                    column_type=c.get("type", ColumnType.CUSTOM.value),
                    is_done_column=c.get("is_done_column", False),
                )
                session.add(col)

            # Create default labels
            for lbl_name, lbl_color in DEFAULT_LABEL_PALETTE:
                lbl = Label(board_id=board.id, name=lbl_name, color=lbl_color)
                session.add(lbl)

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
    ) -> List[Task]:
        with self.db.session_scope() as session:
            q = (
                session.query(Task)
                .options(
                    joinedload(Task.labels),
                    joinedload(Task.prerequisites).joinedload(TaskDependency.prerequisite_task),
                    joinedload(Task.dependents).joinedload(TaskDependency.dependent_task),
                )
                .filter(Task.board_id == board_id)
            )

            if is_completed is not None:
                q = q.filter(Task.is_completed == is_completed)
            if priority:
                q = q.filter(Task.priority == priority)
            if search_query:
                term = f"%{search_query.strip()}%"
                q = q.filter((Task.title.ilike(term)) | (Task.description.ilike(term)))

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
    ) -> Task:
        with self.db.session_scope() as session:
            # Check target column if done column
            target_col = session.query(BoardColumn).filter(BoardColumn.id == column_id).first()
            is_done = target_col.is_done_column if target_col else False

            max_pos = session.query(Task).filter(Task.column_id == column_id).count()

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
                custom_fields_json=json.dumps(custom_fields or {}),
                is_recurring=is_recurring,
                recurrence_rule_json=json.dumps(recurrence_rule or {}),
                assignee=(assignee or "").strip() or None,
            )

            if label_ids:
                labels = session.query(Label).filter(Label.id.in_(label_ids)).all()
                task.labels = labels

            session.add(task)
            session.flush()

            self._log(
                session,
                board_id,
                task.id,
                ActivityAction.TASK_CREATED,
                f"Created task '{title}' in column '{target_col.name if target_col else ''}'",
                field_name="column",
                new_value=target_col.name if target_col else None,
            )

            session.flush()
            session.refresh(task)
            _ = task.labels
            _ = task.prerequisites
            _ = task.dependents
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

            for key, value in kwargs.items():
                if key == "checklist" and isinstance(value, list):
                    task.checklist_json = json.dumps(value)
                elif key == "custom_fields" and isinstance(value, dict):
                    task.custom_fields_json = json.dumps(value)
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
                )

            task.updated_at = datetime.now()
            session.flush()
            session.refresh(task)
            _ = task.labels
            _ = task.prerequisites
            _ = task.dependents
            return task

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
            col_tasks = session.query(Task).filter(Task.column_id == col_id).order_by(Task.position).all()
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

            # Check workflow allowed transitions if moving cross-column
            if source_column.id != target_column.id:
                allowed = source_column.allowed_transitions
                if allowed and target_column.id not in allowed:
                    return False, f"Workflow transition from '{source_column.name}' to '{target_column.name}' is not allowed"

                # Check WIP limit on target column
                if target_column.wip_limit > 0:
                    current_count = session.query(Task).filter(Task.column_id == target_column.id).count()
                    if current_count >= target_column.wip_limit:
                        return False, f"Target column '{target_column.name}' has reached its WIP limit of {target_column.wip_limit} tasks"

            old_col_id = task.column_id
            old_col_name = source_column.name if source_column else ""

            # Remove from old column ordering
            old_tasks = (
                session.query(Task)
                .filter(Task.column_id == old_col_id, Task.id != task.id)
                .order_by(Task.position)
                .all()
            )
            for idx, t in enumerate(old_tasks):
                t.position = idx

            # Adjust new column positions
            new_tasks = (
                session.query(Task)
                .filter(Task.column_id == target_column_id, Task.id != task.id)
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
                )

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
    ) -> Tuple[bool, Optional[str]]:
        """
        Add a dependency: `task_id` depends on `depends_on_task_id` (task_id is blocked by depends_on_task_id).
        Checks for self-reference and circular dependency cycles.
        """
        if task_id == depends_on_task_id:
            return False, "A task cannot depend on itself"

        with self.db.session_scope() as session:
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
            )
            session.add(dep)

            task = session.get(Task, task_id)
            prereq = session.get(Task, depends_on_task_id)
            if task and prereq:
                self._log(session, task.board_id, task.id, ActivityAction.DEPENDENCY_ADDED,
                          f"Marked as blocked by #{prereq.id} '{prereq.title}'",
                          field_name="dependency", new_value=prereq.title)
            return True, None

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

    def add_comment(self, task_id: int, body: str, author: Optional[str] = None) -> Dict[str, Any]:
        body = (body or "").strip()
        if not body:
            raise ValueError("Comment cannot be empty")
        with self.db.session_scope() as session:
            task = session.get(Task, task_id)
            if not task:
                raise ValueError(f"Task {task_id} not found")
            comment = Comment(task_id=task_id, author=author or self.actor, body=body)
            session.add(comment)
            session.flush()
            self._log(session, task.board_id, task.id, ActivityAction.COMMENT_ADDED,
                      f"Commented: {_snippet(body)}", field_name="comment", new_value=body)
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

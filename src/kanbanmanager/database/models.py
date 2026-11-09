"""
SQLAlchemy Database Models for KanbanManager
"""
from __future__ import annotations

import json
from datetime import datetime, date, timezone
from pathlib import PurePath
from typing import List, Optional, Dict, Any


def utc_now() -> datetime:
    """Return naive UTC datetime for SQLite compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

from sqlalchemy import (
    Column as SAColumn,
    Integer,
    String,
    Text,
    Boolean,
    Float,
    DateTime,
    Date,
    ForeignKey,
    Table,
    UniqueConstraint,
    Index,
    select,
    func,
    inspect as sa_inspect,
)
from sqlalchemy.orm import declarative_base, relationship, column_property

from kanbanmanager.config import IMAGE_EXTENSIONS
from kanbanmanager.enums import Priority, ColumnType, RecurrenceFrequency, DependencyType

Base = declarative_base()

# Task-to-Label Many-to-Many association table
task_labels = Table(
    "task_labels",
    Base.metadata,
    SAColumn("task_id", Integer, ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    SAColumn("label_id", Integer, ForeignKey("labels.id", ondelete="CASCADE"), primary_key=True),
)


class Workspace(Base):
    __tablename__ = "workspaces"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    name = SAColumn(String(120), nullable=False)
    description = SAColumn(Text, nullable=True, default="")
    color = SAColumn(String(20), nullable=False, default="#6366F1")
    icon = SAColumn(String(50), nullable=True, default="folder")
    is_archived = SAColumn(Boolean, default=False, nullable=False)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)
    updated_at = SAColumn(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    boards = relationship(
        "Board",
        back_populates="workspace",
        cascade="all, delete-orphan",
        order_by="Board.position",
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "color": self.color,
            "icon": self.icon,
            "is_archived": self.is_archived,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "boards": [b.to_dict(include_details=False) for b in self.boards if not b.is_archived],
        }


class Board(Base):
    __tablename__ = "boards"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    workspace_id = SAColumn(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name = SAColumn(String(120), nullable=False)
    description = SAColumn(Text, nullable=True, default="")
    color = SAColumn(String(20), nullable=False, default="#3B82F6")
    icon = SAColumn(String(50), nullable=True, default="layout")
    position = SAColumn(Integer, default=0, nullable=False)
    is_archived = SAColumn(Boolean, default=False, nullable=False)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)
    updated_at = SAColumn(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    workspace = relationship("Workspace", back_populates="boards")
    columns = relationship(
        "BoardColumn",
        back_populates="board",
        cascade="all, delete-orphan",
        order_by="BoardColumn.position",
    )
    tasks = relationship(
        "Task",
        back_populates="board",
        cascade="all, delete-orphan",
        order_by="Task.position",
    )
    labels = relationship(
        "Label",
        back_populates="board",
        cascade="all, delete-orphan",
    )
    activity_logs = relationship(
        "ActivityLog",
        back_populates="board",
        cascade="all, delete-orphan",
        order_by="ActivityLog.timestamp.desc()",
    )

    def to_dict(self, include_details: bool = True) -> Dict[str, Any]:
        data = {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "description": self.description or "",
            "color": self.color,
            "icon": self.icon,
            "position": self.position,
            "is_archived": self.is_archived,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_details:
            data["columns"] = [col.to_dict() for col in self.columns]
            data["labels"] = [lbl.to_dict() for lbl in self.labels]
        return data


class BoardColumn(Base):
    __tablename__ = "board_columns"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    board_id = SAColumn(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    name = SAColumn(String(100), nullable=False)
    position = SAColumn(Integer, default=0, nullable=False)
    wip_limit = SAColumn(Integer, default=0, nullable=False)  # 0 = unlimited
    color = SAColumn(String(20), default="#64748B", nullable=False)
    column_type = SAColumn(String(30), default=ColumnType.CUSTOM.value, nullable=False)
    is_collapsed = SAColumn(Boolean, default=False, nullable=False)
    is_done_column = SAColumn(Boolean, default=False, nullable=False)
    allowed_transitions_json = SAColumn(Text, default="[]", nullable=False)  # JSON array of allowed target column IDs

    board = relationship("Board", back_populates="columns")
    tasks = relationship(
        "Task",
        back_populates="column",
        cascade="all, delete-orphan",
        order_by="Task.position",
    )

    @property
    def allowed_transitions(self) -> List[int]:
        try:
            return json.loads(self.allowed_transitions_json or "[]")
        except Exception:
            return []

    @allowed_transitions.setter
    def allowed_transitions(self, val: List[int]) -> None:
        self.allowed_transitions_json = json.dumps(val or [])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "board_id": self.board_id,
            "name": self.name,
            "position": self.position,
            "wip_limit": self.wip_limit,
            "color": self.color,
            "column_type": self.column_type,
            "is_collapsed": self.is_collapsed,
            "is_done_column": self.is_done_column,
            "allowed_transitions": self.allowed_transitions,
            "task_count": len(self.tasks),
        }


class Label(Base):
    __tablename__ = "labels"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    board_id = SAColumn(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    name = SAColumn(String(50), nullable=False)
    color = SAColumn(String(20), nullable=False, default="#3B82F6")
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)

    board = relationship("Board", back_populates="labels")
    tasks = relationship("Task", secondary=task_labels, back_populates="labels")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "board_id": self.board_id,
            "name": self.name,
            "color": self.color,
        }


class Task(Base):
    __tablename__ = "tasks"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    board_id = SAColumn(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    column_id = SAColumn(Integer, ForeignKey("board_columns.id", ondelete="CASCADE"), nullable=False)
    title = SAColumn(String(255), nullable=False)
    description = SAColumn(Text, nullable=True, default="")
    position = SAColumn(Integer, default=0, nullable=False)
    priority = SAColumn(String(20), default=Priority.MEDIUM.value, nullable=False)
    estimated_hours = SAColumn(Float, nullable=True, default=0.0)
    spent_hours = SAColumn(Float, nullable=True, default=0.0)
    start_date = SAColumn(Date, nullable=True)
    due_date = SAColumn(DateTime, nullable=True)
    is_completed = SAColumn(Boolean, default=False, nullable=False)
    completed_at = SAColumn(DateTime, nullable=True)
    
    # Checklist as JSON array: [{"id": 1, "text": "...", "done": bool}]
    checklist_json = SAColumn(Text, default="[]", nullable=False)
    
    # Custom fields as JSON dict: {"Custom Field": "Value"}
    custom_fields_json = SAColumn(Text, default="{}", nullable=False)

    # Recurrence configuration
    is_recurring = SAColumn(Boolean, default=False, nullable=False)
    recurrence_rule_json = SAColumn(Text, default="{}", nullable=False)
    parent_recurring_task_id = SAColumn(Integer, nullable=True)

    # Person responsible (free-form display name; capacity lives in TeamMember)
    assignee = SAColumn(String(120), nullable=True)

    created_at = SAColumn(DateTime, default=utc_now, nullable=False)
    updated_at = SAColumn(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    board = relationship("Board", back_populates="tasks")
    column = relationship("BoardColumn", back_populates="tasks")
    labels = relationship("Label", secondary=task_labels, back_populates="tasks")
    reminders = relationship("Reminder", back_populates="task", cascade="all, delete-orphan")
    activity_logs = relationship("ActivityLog", back_populates="task", cascade="all, delete-orphan")
    comments = relationship(
        "Comment",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Comment.created_at",
    )
    attachments = relationship(
        "Attachment",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Attachment.created_at",
    )
    time_entries = relationship(
        "TimeEntry",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TimeEntry.started_at",
    )

    # Dependencies: tasks that this task depends on (this task is blocked by)
    prerequisites = relationship(
        "TaskDependency",
        foreign_keys="TaskDependency.task_id",
        back_populates="dependent_task",
        cascade="all, delete-orphan",
    )
    # Dependencies: tasks that depend on this task (this task blocks them)
    dependents = relationship(
        "TaskDependency",
        foreign_keys="TaskDependency.depends_on_task_id",
        back_populates="prerequisite_task",
        cascade="all, delete-orphan",
    )

    @property
    def checklist(self) -> List[Dict[str, Any]]:
        try:
            return json.loads(self.checklist_json or "[]")
        except Exception:
            return []

    @checklist.setter
    def checklist(self, val: List[Dict[str, Any]]) -> None:
        self.checklist_json = json.dumps(val or [])

    @property
    def custom_fields(self) -> Dict[str, Any]:
        try:
            return json.loads(self.custom_fields_json or "{}")
        except Exception:
            return {}

    @custom_fields.setter
    def custom_fields(self, val: Dict[str, Any]) -> None:
        self.custom_fields_json = json.dumps(val or {})

    @property
    def recurrence_rule(self) -> Dict[str, Any]:
        try:
            return json.loads(self.recurrence_rule_json or "{}")
        except Exception:
            return {}

    @recurrence_rule.setter
    def recurrence_rule(self, val: Dict[str, Any]) -> None:
        self.recurrence_rule_json = json.dumps(val or {})

    @property
    def is_overdue(self) -> bool:
        if self.is_completed or not self.due_date:
            return False
        return self.due_date < utc_now()

    def _loaded_int(self, attr: str) -> int:
        """Read a column_property without triggering a lazy load on a detached instance."""
        if attr in sa_inspect(self).unloaded:
            return 0
        return int(getattr(self, attr) or 0)

    @property
    def is_blocked(self) -> bool:
        """Returns True if any prerequisite task is not completed."""
        for dep in self.prerequisites:
            if dep.prerequisite_task and not dep.prerequisite_task.is_completed:
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "board_id": self.board_id,
            "column_id": self.column_id,
            "title": self.title,
            "description": self.description or "",
            "position": self.position,
            "priority": self.priority,
            "estimated_hours": self.estimated_hours,
            "spent_hours": self.spent_hours,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "is_completed": self.is_completed,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "checklist": self.checklist,
            "custom_fields": self.custom_fields,
            "is_recurring": self.is_recurring,
            "recurrence_rule": self.recurrence_rule,
            "labels": [lbl.to_dict() for lbl in self.labels],
            "is_overdue": self.is_overdue,
            "is_blocked": self.is_blocked,
            "assignee": self.assignee or "",
            "comment_count": self._loaded_int("comment_count"),
            "attachment_count": self._loaded_int("attachment_count"),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "prerequisites": [
                {
                    "dependency_id": p.id,
                    "task_id": p.depends_on_task_id,
                    "title": p.prerequisite_task.title if p.prerequisite_task else "",
                    "is_completed": p.prerequisite_task.is_completed if p.prerequisite_task else False,
                    "dependency_type": p.dependency_type,
                }
                for p in self.prerequisites
            ],
            "dependents": [
                {
                    "dependency_id": d.id,
                    "task_id": d.task_id,
                    "title": d.dependent_task.title if d.dependent_task else "",
                    "is_completed": d.dependent_task.is_completed if d.dependent_task else False,
                    "dependency_type": d.dependency_type,
                }
                for d in self.dependents
            ],
        }


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    task_id = SAColumn(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    depends_on_task_id = SAColumn(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    dependency_type = SAColumn(String(30), default=DependencyType.FINISH_TO_START.value, nullable=False)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dependency"),
        Index("idx_task_dep_pair", "task_id", "depends_on_task_id"),
    )

    dependent_task = relationship("Task", foreign_keys=[task_id], back_populates="prerequisites")
    prerequisite_task = relationship("Task", foreign_keys=[depends_on_task_id], back_populates="dependents")


class Reminder(Base):
    __tablename__ = "reminders"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    task_id = SAColumn(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    remind_at = SAColumn(DateTime, nullable=False)
    offset_minutes = SAColumn(Integer, default=0, nullable=False)  # 0=on time, 15=15m before, etc.
    message = SAColumn(String(255), nullable=True)
    is_triggered = SAColumn(Boolean, default=False, nullable=False)
    is_dismissed = SAColumn(Boolean, default=False, nullable=False)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)

    task = relationship("Task", back_populates="reminders")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "remind_at": self.remind_at.isoformat() if self.remind_at else None,
            "offset_minutes": self.offset_minutes,
            "message": self.message or "",
            "is_triggered": self.is_triggered,
            "is_dismissed": self.is_dismissed,
        }


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    board_id = SAColumn(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    task_id = SAColumn(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    action_type = SAColumn(String(50), nullable=False)
    details = SAColumn(Text, nullable=True)
    timestamp = SAColumn(DateTime, default=utc_now, nullable=False)
    # Structured audit-trail fields (added in schema v2; nullable for legacy rows)
    actor = SAColumn(String(120), nullable=True)
    field_name = SAColumn(String(50), nullable=True)
    old_value = SAColumn(Text, nullable=True)
    new_value = SAColumn(Text, nullable=True)

    board = relationship("Board", back_populates="activity_logs")
    task = relationship("Task", back_populates="activity_logs")

    __table_args__ = (Index("idx_activity_task_time", "task_id", "timestamp"),)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "board_id": self.board_id,
            "task_id": self.task_id,
            "action_type": self.action_type,
            "details": self.details or "",
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "actor": self.actor or "",
            "field_name": self.field_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
        }


class Comment(Base):
    """A Markdown-formatted discussion comment on a task."""

    __tablename__ = "comments"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    task_id = SAColumn(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    author = SAColumn(String(120), nullable=False, default="")
    body = SAColumn(Text, nullable=False, default="")
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)
    updated_at = SAColumn(DateTime, nullable=True)  # set only when the comment is edited

    task = relationship("Task", back_populates="comments")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "author": self.author or "",
            "body": self.body or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "is_edited": self.updated_at is not None,
        }


class Attachment(Base):
    """Metadata for a file attached to a task.

    The file itself lives in the content-addressed AttachmentStore next to the
    database; ``stored_name`` is its path relative to that store.
    """

    __tablename__ = "attachments"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    task_id = SAColumn(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = SAColumn(String(255), nullable=False)
    stored_name = SAColumn(String(255), nullable=False, index=True)
    content_type = SAColumn(String(120), nullable=False, default="application/octet-stream")
    size_bytes = SAColumn(Integer, nullable=False, default=0)
    checksum = SAColumn(String(64), nullable=False)
    uploaded_by = SAColumn(String(120), nullable=True)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)

    task = relationship("Task", back_populates="attachments")

    @property
    def is_image(self) -> bool:
        return PurePath(self.filename or "").suffix.lower() in IMAGE_EXTENSIONS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "filename": self.filename,
            "stored_name": self.stored_name,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "checksum": self.checksum,
            "uploaded_by": self.uploaded_by or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "is_image": self.is_image,
        }


class TimeEntry(Base):
    """A span of time worked on a task. ``ended_at`` is NULL while a timer is running.

    Timestamps are naive UTC, like the rest of the audit data.
    """

    __tablename__ = "time_entries"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    task_id = SAColumn(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    user = SAColumn(String(120), nullable=True)
    started_at = SAColumn(DateTime, nullable=False, index=True)
    ended_at = SAColumn(DateTime, nullable=True)
    duration_seconds = SAColumn(Integer, nullable=False, default=0)
    source = SAColumn(String(20), nullable=False, default="STOPWATCH")
    planned_seconds = SAColumn(Integer, nullable=True)  # Pomodoro focus length, for crash recovery
    note = SAColumn(Text, nullable=True)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)

    task = relationship("Task", back_populates="time_entries")

    @property
    def is_running(self) -> bool:
        return self.ended_at is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "user": self.user or "",
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": int(self.duration_seconds or 0),
            "source": self.source,
            "planned_seconds": self.planned_seconds,
            "note": self.note or "",
            "is_running": self.is_running,
        }


class TeamMember(Base):
    """A person whose capacity is planned in the workload view."""

    __tablename__ = "team_members"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    name = SAColumn(String(120), nullable=False, unique=True)
    weekly_capacity_hours = SAColumn(Float, nullable=False, default=40.0)
    working_days_json = SAColumn(Text, nullable=False, default="[0, 1, 2, 3, 4]")
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)

    @property
    def working_days(self) -> List[int]:
        try:
            days = json.loads(self.working_days_json or "[]")
            return sorted({int(d) for d in days if 0 <= int(d) <= 6})
        except Exception:
            return [0, 1, 2, 3, 4]

    @working_days.setter
    def working_days(self, val: List[int]) -> None:
        self.working_days_json = json.dumps(sorted(set(val or [])))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "weekly_capacity_hours": float(self.weekly_capacity_hours or 0.0),
            "working_days": self.working_days,
        }


class AppSetting(Base):
    """Small key/value store for preferences (kept in the DB so they travel with backups)."""

    __tablename__ = "app_settings"

    key = SAColumn(String(80), primary_key=True)
    value = SAColumn(Text, nullable=False, default="")


# Aggregate counts used by task cards; loaded with the Task row via correlated subqueries
Task.comment_count = column_property(
    select(func.count(Comment.id)).where(Comment.task_id == Task.id).correlate_except(Comment).scalar_subquery()
)
Task.attachment_count = column_property(
    select(func.count(Attachment.id)).where(Attachment.task_id == Task.id).correlate_except(Attachment).scalar_subquery()
)

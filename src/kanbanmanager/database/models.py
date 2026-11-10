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
    # Auto-archiving & stale-task policies (see core.automation.DEFAULT_POLICIES)
    policies_json = SAColumn(Text, default="{}", nullable=False)
    # Working calendar, cascading and compared baseline (see core.scheduling.DEFAULT_SCHEDULE_SETTINGS)
    schedule_json = SAColumn(Text, default="{}", nullable=False)

    workspace = relationship("Workspace", back_populates="boards")
    automation_rules = relationship(
        "AutomationRule",
        back_populates="board",
        cascade="all, delete-orphan",
        order_by="AutomationRule.position",
    )
    automation_runs = relationship("AutomationRun", cascade="all, delete-orphan")
    baselines = relationship("ProjectBaseline", back_populates="board", cascade="all, delete-orphan",
                             order_by="ProjectBaseline.created_at")
    custom_fields = relationship("CustomField", back_populates="board", cascade="all, delete-orphan",
                                 order_by="CustomField.position")
    task_templates = relationship("TaskTemplate", back_populates="board", cascade="all, delete-orphan",
                                  order_by="TaskTemplate.position")
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

    @property
    def schedule_settings(self) -> Dict[str, Any]:
        try:
            return json.loads(self.schedule_json or "{}")
        except Exception:
            return {}

    @schedule_settings.setter
    def schedule_settings(self, val: Dict[str, Any]) -> None:
        self.schedule_json = json.dumps(val or {})

    @property
    def policies(self) -> Dict[str, Any]:
        try:
            return json.loads(self.policies_json or "{}")
        except Exception:
            return {}

    @policies.setter
    def policies(self, val: Dict[str, Any]) -> None:
        self.policies_json = json.dumps(val or {})

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

    def to_dict(self, include_count: bool = True) -> Dict[str, Any]:
        """``include_count``: count the column's open tasks (reads ``tasks``; leave it out
        for columns loaded without their tasks)."""
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
            **({"task_count": sum(1 for t in self.tasks if not t.is_archived)} if include_count else {}),
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

    # Archived tasks are hidden from every view but kept (restorable, still in history)
    is_archived = SAColumn(Boolean, default=False, nullable=False, server_default="0")
    archived_at = SAColumn(DateTime, nullable=True)
    archive_reason = SAColumn(String(200), nullable=True)

    # Scheduling: milestones have zero duration; locked tasks are never moved by
    # auto-scheduling or cascading date shifts
    is_milestone = SAColumn(Boolean, default=False, nullable=False, server_default="0")
    dates_locked = SAColumn(Boolean, default=False, nullable=False, server_default="0")

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
    # Custom field values; always loaded with the task (one extra IN query per batch of tasks)
    field_values = relationship(
        "TaskFieldValue",
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="selectin",
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

    def _field_value_rows(self) -> List["TaskFieldValue"]:
        if "field_values" in sa_inspect(self).unloaded:  # detached without them: never lazy-load
            return []
        return [fv for fv in self.field_values if fv.field is not None]

    @property
    def field_value_map(self) -> Dict[int, Any]:
        """Custom field values by field id."""
        return {fv.field_id: fv.value for fv in self._field_value_rows()}

    @property
    def custom_fields(self) -> Dict[str, Any]:
        """Custom field values by field name (``custom_fields_json`` is the pre-typed-fields legacy
        store; the migration moves its contents into real fields)."""
        return {fv.field.name: fv.value for fv in sorted(self._field_value_rows(), key=lambda r: r.field.position)}

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
        """True if a finish-to-start prerequisite is not completed yet.

        Start-to-start, finish-to-finish and start-to-finish links constrain the
        schedule but don't prevent starting the task, so they don't block it.
        """
        for dep in self.prerequisites:
            try:
                kind = DependencyType(dep.dependency_type).scheduling_type
            except ValueError:
                kind = DependencyType.FINISH_TO_START
            if kind != DependencyType.FINISH_TO_START:
                continue
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
            "field_values": self.field_value_map,
            "is_recurring": self.is_recurring,
            "recurrence_rule": self.recurrence_rule,
            "labels": [lbl.to_dict() for lbl in self.labels],
            "is_overdue": self.is_overdue,
            "is_blocked": self.is_blocked,
            "assignee": self.assignee or "",
            "is_archived": bool(self.is_archived),
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "archive_reason": self.archive_reason or "",
            "is_milestone": bool(self.is_milestone),
            "dates_locked": bool(self.dates_locked),
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
                    "lag_days": int(p.lag_days or 0),
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
    lag_days = SAColumn(Integer, default=0, nullable=False, server_default="0")  # working days; negative = lead
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


class AutomationRule(Base):
    """'When <trigger> and <conditions> then <actions>' for one board.

    trigger/conditions/actions are JSON; their vocabulary lives in core.automation.
    """

    __tablename__ = "automation_rules"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    board_id = SAColumn(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    name = SAColumn(String(120), nullable=False)
    is_enabled = SAColumn(Boolean, default=True, nullable=False)
    position = SAColumn(Integer, default=0, nullable=False)
    trigger_json = SAColumn(Text, default="{}", nullable=False)
    conditions_json = SAColumn(Text, default="[]", nullable=False)
    actions_json = SAColumn(Text, default="[]", nullable=False)
    run_count = SAColumn(Integer, default=0, nullable=False)
    last_run_at = SAColumn(DateTime, nullable=True)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)
    updated_at = SAColumn(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    board = relationship("Board", back_populates="automation_rules")

    @staticmethod
    def _load(raw: str, default):
        try:
            value = json.loads(raw or "")
            return value if isinstance(value, type(default)) else default
        except Exception:
            return default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "board_id": self.board_id,
            "name": self.name,
            "is_enabled": bool(self.is_enabled),
            "position": self.position,
            "trigger": self._load(self.trigger_json, {}),
            "conditions": self._load(self.conditions_json, []),
            "actions": self._load(self.actions_json, []),
            "run_count": int(self.run_count or 0),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
        }


class AutomationRun(Base):
    """Execution history of rules and policies (also used to avoid firing twice)."""

    __tablename__ = "automation_runs"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    board_id = SAColumn(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    rule_id = SAColumn(Integer, ForeignKey("automation_rules.id", ondelete="SET NULL"), nullable=True)
    source = SAColumn(String(40), nullable=False, default="rule")  # 'rule' or a policy name
    rule_name = SAColumn(String(120), nullable=False, default="")
    task_id = SAColumn(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    task_title = SAColumn(String(255), nullable=True)
    trigger = SAColumn(String(40), nullable=False, default="")
    status = SAColumn(String(20), nullable=False, default="success")  # success | partial | failed | skipped
    message = SAColumn(Text, nullable=True)
    dedupe_key = SAColumn(String(200), nullable=True, index=True)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False, index=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "board_id": self.board_id,
            "rule_id": self.rule_id,
            "source": self.source,
            "rule_name": self.rule_name,
            "task_id": self.task_id,
            "task_title": self.task_title or "",
            "trigger": self.trigger,
            "status": self.status,
            "message": self.message or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ProjectBaseline(Base):
    """A saved snapshot of a board's planned dates, to compare the current plan against."""

    __tablename__ = "project_baselines"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    board_id = SAColumn(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    name = SAColumn(String(120), nullable=False)
    created_by = SAColumn(String(120), nullable=True)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)

    board = relationship("Board", back_populates="baselines")
    entries = relationship("BaselineEntry", back_populates="baseline", cascade="all, delete-orphan")

    def to_dict(self, include_entries: bool = False) -> Dict[str, Any]:
        data = {
            "id": self.id,
            "board_id": self.board_id,
            "name": self.name,
            "created_by": self.created_by or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "task_count": len(self.entries),
        }
        if include_entries:
            data["entries"] = [e.to_dict() for e in self.entries]
        return data


class BaselineEntry(Base):
    __tablename__ = "baseline_entries"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    baseline_id = SAColumn(Integer, ForeignKey("project_baselines.id", ondelete="CASCADE"), nullable=False,
                           index=True)
    task_id = SAColumn(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    title = SAColumn(String(255), nullable=False)
    start_date = SAColumn(Date, nullable=False)
    finish_date = SAColumn(Date, nullable=False)
    duration_days = SAColumn(Integer, nullable=False, default=0)
    is_milestone = SAColumn(Boolean, nullable=False, default=False)

    baseline = relationship("ProjectBaseline", back_populates="entries")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "start_date": self.start_date.isoformat(),
            "finish_date": self.finish_date.isoformat(),
            "duration_days": self.duration_days,
            "is_milestone": bool(self.is_milestone),
        }


class CustomField(Base):
    """A board-defined task field ("Story points", "Severity" ...). ``field_type`` is a key of the
    extensible registry in core.custom_fields; ``config_json`` holds type settings (options, unit ...)."""

    __tablename__ = "custom_fields"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    board_id = SAColumn(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    name = SAColumn(String(60), nullable=False)
    field_type = SAColumn(String(40), nullable=False, default="text")
    config_json = SAColumn(Text, nullable=False, default="{}")
    description = SAColumn(Text, nullable=False, default="")
    required = SAColumn(Boolean, nullable=False, default=False)
    default_json = SAColumn(Text, nullable=True)          # JSON value given to new tasks (NULL = none)
    show_on_card = SAColumn(Boolean, nullable=False, default=True)
    position = SAColumn(Integer, nullable=False, default=0)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)

    __table_args__ = (UniqueConstraint("board_id", "name", name="uq_custom_field_name"),)

    board = relationship("Board", back_populates="custom_fields")
    values = relationship("TaskFieldValue", back_populates="field", cascade="all, delete-orphan",
                          passive_deletes=True)

    @property
    def config(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.config_json or "{}")
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @config.setter
    def config(self, val: Dict[str, Any]) -> None:
        self.config_json = json.dumps(val or {}, ensure_ascii=False)

    @property
    def default(self) -> Any:
        try:
            return json.loads(self.default_json) if self.default_json else None
        except Exception:
            return None

    @default.setter
    def default(self, val: Any) -> None:
        self.default_json = None if val is None else json.dumps(val, ensure_ascii=False)

    def to_dict(self) -> Dict[str, Any]:
        from kanbanmanager.core.custom_fields import get_field_type

        ftype = get_field_type(self.field_type)
        return {
            "id": self.id,
            "board_id": self.board_id,
            "name": self.name,
            "field_type": self.field_type,
            "type_label": ftype.label,
            "icon": ftype.icon,
            "config": self.config,
            "description": self.description or "",
            "required": bool(self.required),
            "default": self.default,
            "show_on_card": bool(self.show_on_card),
            "position": self.position,
        }


class TaskFieldValue(Base):
    """The value of one custom field on one task (JSON; see core.custom_fields)."""

    __tablename__ = "task_field_values"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    task_id = SAColumn(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    field_id = SAColumn(Integer, ForeignKey("custom_fields.id", ondelete="CASCADE"), nullable=False, index=True)
    value_json = SAColumn(Text, nullable=False, default="null")
    updated_at = SAColumn(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (UniqueConstraint("task_id", "field_id", name="uq_task_field_value"),)

    task = relationship("Task", back_populates="field_values")
    field = relationship("CustomField", back_populates="values", lazy="joined")

    @property
    def value(self) -> Any:
        try:
            return json.loads(self.value_json)
        except Exception:
            return None

    @value.setter
    def value(self, val: Any) -> None:
        self.value_json = json.dumps(val, ensure_ascii=False)


class TaskTemplate(Base):
    """A reusable task preset (title pattern, description, checklist, labels, field values ...).
    ``board_id`` NULL = available on every board. See core.templates for the data format."""

    __tablename__ = "task_templates"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    board_id = SAColumn(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=True, index=True)
    name = SAColumn(String(120), nullable=False)
    icon = SAColumn(String(16), nullable=False, default="📋")
    data_json = SAColumn(Text, nullable=False, default="{}")
    position = SAColumn(Integer, nullable=False, default=0)
    created_by = SAColumn(String(120), nullable=True)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)
    updated_at = SAColumn(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    board = relationship("Board", back_populates="task_templates")

    @property
    def data(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.data_json or "{}")
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @data.setter
    def data(self, val: Dict[str, Any]) -> None:
        self.data_json = json.dumps(val or {}, ensure_ascii=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "board_id": self.board_id,
            "name": self.name,
            "icon": self.icon or "📋",
            "data": self.data,
            "position": self.position,
            "created_by": self.created_by or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class BoardTemplate(Base):
    """A board blueprint saved by the user (built-in templates live in core.templates).
    ``data_json`` uses the portable template format (columns, labels, fields, task templates,
    automation, sample tasks), so templates can be exported and shared as files."""

    __tablename__ = "board_templates"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    name = SAColumn(String(120), nullable=False)
    description = SAColumn(Text, nullable=False, default="")
    icon = SAColumn(String(16), nullable=False, default="📋")
    color = SAColumn(String(20), nullable=False, default="#3B82F6")
    category = SAColumn(String(60), nullable=False, default="My templates")
    data_json = SAColumn(Text, nullable=False, default="{}")
    created_by = SAColumn(String(120), nullable=True)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)
    updated_at = SAColumn(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    @property
    def data(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.data_json or "{}")
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @data.setter
    def data(self, val: Dict[str, Any]) -> None:
        self.data_json = json.dumps(val or {}, ensure_ascii=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "icon": self.icon or "📋",
            "color": self.color,
            "category": self.category or "My templates",
            "data": self.data,
            "created_by": self.created_by or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "builtin": False,
        }


class InboxItem(Base):
    """A quickly captured thought waiting to be triaged into a board (the Global Inbox).

    The raw text is kept; the parsed hints (due date, priority, labels, assignee,
    target board) are stored so triage can apply them.
    """

    __tablename__ = "inbox_items"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    raw_text = SAColumn(Text, nullable=False, default="")
    title = SAColumn(String(255), nullable=False)
    notes = SAColumn(Text, nullable=True)
    priority = SAColumn(String(20), nullable=True)
    due_date = SAColumn(DateTime, nullable=True)          # local time, like Task.due_date
    labels_json = SAColumn(Text, nullable=False, default="[]")
    assignee = SAColumn(String(120), nullable=True)
    board_hint_id = SAColumn(Integer, ForeignKey("boards.id", ondelete="SET NULL"), nullable=True)
    source = SAColumn(String(40), nullable=False, default="quick_capture")
    captured_by = SAColumn(String(120), nullable=True)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)

    @property
    def labels(self) -> List[str]:
        try:
            return [str(x) for x in json.loads(self.labels_json or "[]")]
        except Exception:
            return []

    @labels.setter
    def labels(self, val: List[str]) -> None:
        self.labels_json = json.dumps(list(val or []))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "raw_text": self.raw_text or "",
            "title": self.title,
            "notes": self.notes or "",
            "priority": self.priority,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "labels": self.labels,
            "assignee": self.assignee or "",
            "board_hint_id": self.board_hint_id,
            "source": self.source,
            "captured_by": self.captured_by or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CalendarSubscription(Base):
    """A calendar this app syncs with: an iCalendar feed URL (read) or a CalDAV calendar
    (read/write). Items are mapped to tasks on ``board_id`` (see core.calendar_sync)."""

    __tablename__ = "calendar_subscriptions"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    name = SAColumn(String(120), nullable=False)
    kind = SAColumn(String(20), nullable=False, default="ics")           # ics | caldav
    url = SAColumn(Text, nullable=False)
    username = SAColumn(String(200), nullable=True)
    password = SAColumn(Text, nullable=True)
    board_id = SAColumn(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    column_id = SAColumn(Integer, ForeignKey("board_columns.id", ondelete="SET NULL"), nullable=True)
    direction = SAColumn(String(20), nullable=False, default="pull")     # pull | push | two_way
    interval_minutes = SAColumn(Integer, nullable=False, default=30)     # 0 = manual only
    enabled = SAColumn(Boolean, nullable=False, default=True)
    options_json = SAColumn(Text, nullable=False, default="{}")
    last_sync_at = SAColumn(DateTime, nullable=True)                    # naive UTC
    last_status = SAColumn(String(20), nullable=True)                   # success | partial | failed
    last_message = SAColumn(Text, nullable=True)
    created_at = SAColumn(DateTime, default=utc_now, nullable=False)

    @property
    def options(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.options_json or "{}")
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @options.setter
    def options(self, val: Dict[str, Any]) -> None:
        self.options_json = json.dumps(val or {})

    def to_dict(self, include_secret: bool = False) -> Dict[str, Any]:
        data = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "url": self.url,
            "username": self.username or "",
            "has_password": bool(self.password),
            "board_id": self.board_id,
            "column_id": self.column_id,
            "direction": self.direction,
            "interval_minutes": int(self.interval_minutes or 0),
            "enabled": bool(self.enabled),
            "options": self.options,
            "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "last_status": self.last_status,
            "last_message": self.last_message or "",
        }
        if include_secret:
            data["password"] = self.password or ""
        return data


class ExternalLink(Base):
    """Sync state of one task <-> one item elsewhere (a calendar entry, an imported card ...).

    ``source`` identifies where ("sub:<id>" for a calendar subscription, "ics-file",
    "trello:<board>" ...). ``task_id`` becomes NULL when the task is deleted, which tells
    the next sync to delete the remote copy. ``local_hash`` / ``remote_hash`` are the
    states both sides had at the last sync, to tell who changed what since.
    """

    __tablename__ = "external_links"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    source = SAColumn(String(80), nullable=False)
    subscription_id = SAColumn(Integer, ForeignKey("calendar_subscriptions.id", ondelete="CASCADE"), nullable=True,
                               index=True)
    uid = SAColumn(String(500), nullable=False)
    task_id = SAColumn(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    href = SAColumn(Text, nullable=True)
    etag = SAColumn(String(200), nullable=True)
    local_hash = SAColumn(String(64), nullable=True)
    remote_hash = SAColumn(String(64), nullable=True)
    remote_data = SAColumn(Text, nullable=True)          # last remote iCalendar text (to write back faithfully)
    last_synced_at = SAColumn(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("source", "uid", name="uq_external_link"),)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "source": self.source, "subscription_id": self.subscription_id, "uid": self.uid,
            "task_id": self.task_id, "href": self.href, "etag": self.etag, "local_hash": self.local_hash,
            "remote_hash": self.remote_hash, "remote_data": self.remote_data,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
        }


class SyncRun(Base):
    """History of calendar sync runs."""

    __tablename__ = "sync_runs"

    id = SAColumn(Integer, primary_key=True, autoincrement=True)
    subscription_id = SAColumn(Integer, ForeignKey("calendar_subscriptions.id", ondelete="CASCADE"), nullable=False,
                               index=True)
    started_at = SAColumn(DateTime, default=utc_now, nullable=False)
    finished_at = SAColumn(DateTime, nullable=True)
    status = SAColumn(String(20), nullable=False, default="success")
    created = SAColumn(Integer, nullable=False, default=0)      # local tasks created
    updated = SAColumn(Integer, nullable=False, default=0)      # local tasks updated
    removed = SAColumn(Integer, nullable=False, default=0)      # local tasks archived/deleted
    pushed = SAColumn(Integer, nullable=False, default=0)       # remote items created/updated/deleted
    conflicts = SAColumn(Integer, nullable=False, default=0)
    message = SAColumn(Text, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "subscription_id": self.subscription_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "status": self.status, "created": self.created, "updated": self.updated, "removed": self.removed,
            "pushed": self.pushed, "conflicts": self.conflicts, "message": self.message or "",
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

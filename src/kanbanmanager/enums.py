"""
KanbanManager Application Models and Enums
"""
from enum import Enum


class Priority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"

    @property
    def display_name(self) -> str:
        return {
            Priority.LOW: "Low",
            Priority.MEDIUM: "Medium",
            Priority.HIGH: "High",
            Priority.URGENT: "Urgent",
        }[self]

    @property
    def color(self) -> str:
        return {
            Priority.LOW: "#10B981",     # Emerald Green
            Priority.MEDIUM: "#3B82F6",  # Blue
            Priority.HIGH: "#F59E0B",    # Amber/Orange
            Priority.URGENT: "#EF4444",  # Crimson Red
        }[self]

    @property
    def icon_char(self) -> str:
        return {
            Priority.LOW: "●",
            Priority.MEDIUM: "▲",
            Priority.HIGH: "◆",
            Priority.URGENT: "★",
        }[self]


class ColumnType(str, Enum):
    BACKLOG = "BACKLOG"
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    DONE = "DONE"
    CUSTOM = "CUSTOM"

    @property
    def display_name(self) -> str:
        return {
            ColumnType.BACKLOG: "Backlog",
            ColumnType.TODO: "To Do",
            ColumnType.IN_PROGRESS: "In Progress",
            ColumnType.REVIEW: "In Review",
            ColumnType.DONE: "Done",
            ColumnType.CUSTOM: "Custom",
        }[self]


class RecurrenceFrequency(str, Enum):
    DAILY = "DAILY"
    WEEKDAYS = "WEEKDAYS"
    WEEKLY = "WEEKLY"
    BIWEEKLY = "BIWEEKLY"
    MONTHLY = "MONTHLY"
    CUSTOM = "CUSTOM"

    @property
    def display_name(self) -> str:
        return {
            RecurrenceFrequency.DAILY: "Daily",
            RecurrenceFrequency.WEEKDAYS: "Every Weekday (Mon-Fri)",
            RecurrenceFrequency.WEEKLY: "Weekly",
            RecurrenceFrequency.BIWEEKLY: "Bi-weekly",
            RecurrenceFrequency.MONTHLY: "Monthly",
            RecurrenceFrequency.CUSTOM: "Custom Interval",
        }[self]


class DependencyType(str, Enum):
    FINISH_TO_START = "FINISH_TO_START"  # Prerequisite must be done before dependent can start
    BLOCKS = "BLOCKS"                    # Task blocks another task
    BLOCKED_BY = "BLOCKED_BY"            # Task is blocked by another task

    @property
    def display_name(self) -> str:
        return {
            DependencyType.FINISH_TO_START: "Finish to Start",
            DependencyType.BLOCKS: "Blocks",
            DependencyType.BLOCKED_BY: "Blocked by",
        }[self]


class ViewMode(str, Enum):
    BOARD = "BOARD"
    CALENDAR = "CALENDAR"
    TIMELINE = "TIMELINE"
    ANALYTICS = "ANALYTICS"
    WORKLOAD = "WORKLOAD"


class TimeEntrySource(str, Enum):
    STOPWATCH = "STOPWATCH"
    POMODORO = "POMODORO"
    MANUAL = "MANUAL"

    @property
    def display_name(self) -> str:
        return {
            TimeEntrySource.STOPWATCH: "Stopwatch",
            TimeEntrySource.POMODORO: "Pomodoro",
            TimeEntrySource.MANUAL: "Manual",
        }[self]


class ActivityAction(str, Enum):
    """Audit-trail action types recorded in ActivityLog.action_type."""
    TASK_CREATED = "TASK_CREATED"
    TASK_UPDATED = "TASK_UPDATED"
    TASK_MOVED = "TASK_MOVED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_REOPENED = "TASK_REOPENED"
    TASK_DELETED = "TASK_DELETED"
    COMMENT_ADDED = "COMMENT_ADDED"
    COMMENT_EDITED = "COMMENT_EDITED"
    COMMENT_DELETED = "COMMENT_DELETED"
    ATTACHMENT_ADDED = "ATTACHMENT_ADDED"
    ATTACHMENT_REMOVED = "ATTACHMENT_REMOVED"
    DEPENDENCY_ADDED = "DEPENDENCY_ADDED"
    DEPENDENCY_REMOVED = "DEPENDENCY_REMOVED"
    REMINDER_SET = "REMINDER_SET"
    TIME_LOGGED = "TIME_LOGGED"
    TIME_ENTRY_UPDATED = "TIME_ENTRY_UPDATED"
    TIME_ENTRY_DELETED = "TIME_ENTRY_DELETED"

    @property
    def is_comment_action(self) -> bool:
        return self in (
            ActivityAction.COMMENT_ADDED,
            ActivityAction.COMMENT_EDITED,
            ActivityAction.COMMENT_DELETED,
        )

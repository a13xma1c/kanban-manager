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
    START_TO_START = "START_TO_START"    # Dependent can start once the prerequisite has started
    FINISH_TO_FINISH = "FINISH_TO_FINISH"  # Dependent can finish once the prerequisite has finished
    START_TO_FINISH = "START_TO_FINISH"  # Dependent can finish once the prerequisite has started
    BLOCKS = "BLOCKS"                    # Legacy alias of finish-to-start
    BLOCKED_BY = "BLOCKED_BY"            # Legacy alias of finish-to-start

    @property
    def display_name(self) -> str:
        return {
            DependencyType.FINISH_TO_START: "Finish to Start",
            DependencyType.START_TO_START: "Start to Start",
            DependencyType.FINISH_TO_FINISH: "Finish to Finish",
            DependencyType.START_TO_FINISH: "Start to Finish",
            DependencyType.BLOCKS: "Blocks",
            DependencyType.BLOCKED_BY: "Blocked by",
        }[self]

    @property
    def short(self) -> str:
        return {
            DependencyType.START_TO_START: "SS",
            DependencyType.FINISH_TO_FINISH: "FF",
            DependencyType.START_TO_FINISH: "SF",
        }.get(self, "FS")

    @property
    def scheduling_type(self) -> "DependencyType":
        """The legacy 'blocks' types schedule like finish-to-start."""
        return DependencyType.FINISH_TO_START if self in (DependencyType.BLOCKS, DependencyType.BLOCKED_BY) else self


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
    TASK_ARCHIVED = "TASK_ARCHIVED"
    TASK_RESTORED = "TASK_RESTORED"
    DEPENDENCY_UPDATED = "DEPENDENCY_UPDATED"
    BASELINE_SAVED = "BASELINE_SAVED"
    TIME_LOGGED = "TIME_LOGGED"
    TIME_ENTRY_UPDATED = "TIME_ENTRY_UPDATED"
    TIME_ENTRY_DELETED = "TIME_ENTRY_DELETED"
    BOARD_UPDATED = "BOARD_UPDATED"   # board structure: custom fields, templates

    @property
    def is_comment_action(self) -> bool:
        return self in (
            ActivityAction.COMMENT_ADDED,
            ActivityAction.COMMENT_EDITED,
            ActivityAction.COMMENT_DELETED,
        )

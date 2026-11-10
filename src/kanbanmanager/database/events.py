"""
Domain Events

Every user-visible change the service records in the audit trail is also
published as a DomainEvent *after* its transaction commits. The automation
engine subscribes to these. Changes made while activity logging is paused
(imports, internal fix-ups) produce no history and therefore no events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from kanbanmanager.enums import ActivityAction

# Event types (stable strings; used in automation rule JSON)
TASK_CREATED = "task_created"
TASK_MOVED = "task_moved"
TASK_COMPLETED = "task_completed"
TASK_REOPENED = "task_reopened"
TASK_UPDATED = "task_updated"
PRIORITY_CHANGED = "priority_changed"
ASSIGNEE_CHANGED = "assignee_changed"
LABELS_CHANGED = "labels_changed"
DUE_DATE_CHANGED = "due_date_changed"
COMMENT_ADDED = "comment_added"
ATTACHMENT_ADDED = "attachment_added"
TIME_LOGGED = "time_logged"
TASK_ARCHIVED = "task_archived"
TASK_RESTORED = "task_restored"
TASK_DELETED = "task_deleted"
DEPENDENCY_ADDED = "dependency_added"
DEPENDENCY_UPDATED = "dependency_updated"
FIELD_CHANGED = "field_changed"     # a custom field value; data: field_id, field_name, from, to

_FIELD_EVENTS = {
    "priority": PRIORITY_CHANGED,
    "assignee": ASSIGNEE_CHANGED,
    "labels": LABELS_CHANGED,
    "due_date": DUE_DATE_CHANGED,
    "custom_field": FIELD_CHANGED,
}


@dataclass(frozen=True)
class DomainEvent:
    type: str
    board_id: int
    task_id: Optional[int]
    actor: str
    data: Dict[str, Any] = field(default_factory=dict)


def event_type_for(action: ActivityAction, field_name: Optional[str]) -> Optional[str]:
    """Map an audit-trail entry to the event it represents (None = not published)."""
    if action == ActivityAction.TASK_CREATED:
        return TASK_CREATED
    if action == ActivityAction.TASK_MOVED:
        return TASK_MOVED
    if action == ActivityAction.TASK_COMPLETED:
        return TASK_COMPLETED
    if action == ActivityAction.TASK_REOPENED:
        return TASK_REOPENED
    if action == ActivityAction.TASK_UPDATED:
        return _FIELD_EVENTS.get(field_name or "", TASK_UPDATED)
    if action == ActivityAction.COMMENT_ADDED:
        return COMMENT_ADDED
    if action == ActivityAction.ATTACHMENT_ADDED:
        return ATTACHMENT_ADDED
    if action == ActivityAction.TIME_LOGGED:
        return TIME_LOGGED
    if action == ActivityAction.TASK_ARCHIVED:
        return TASK_ARCHIVED
    if action == ActivityAction.TASK_RESTORED:
        return TASK_RESTORED
    if action == ActivityAction.TASK_DELETED:
        return TASK_DELETED
    if action == ActivityAction.DEPENDENCY_ADDED:
        return DEPENDENCY_ADDED
    if action == ActivityAction.DEPENDENCY_UPDATED:
        return DEPENDENCY_UPDATED
    return None

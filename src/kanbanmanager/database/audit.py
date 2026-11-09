"""
Audit-Trail Change Detection for Tasks

``snapshot_task`` captures the user-visible state of a task; ``diff_snapshots``
compares two snapshots and yields one ``Change`` per modified field, with
human-readable details plus structured old/new values for the activity log.

Values are normalised before comparison so that a no-op save from the editor
(e.g. a due date round-tripped through QDateTime, which drops microseconds)
never produces a spurious history entry.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from kanbanmanager.enums import ActivityAction, Priority, RecurrenceFrequency


@dataclass(frozen=True)
class Change:
    action: ActivityAction
    field_name: str
    details: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None


def _fmt_datetime(value: Optional[datetime]) -> Optional[str]:
    return value.strftime("%b %d, %Y %H:%M") if value else None


def _fmt_date(value: Optional[date]) -> Optional[str]:
    return value.strftime("%b %d, %Y") if value else None


def _fmt_hours(value: float) -> str:
    return f"{value:g}h"


def priority_display(value: Optional[str]) -> str:
    try:
        return Priority(value).display_name
    except ValueError:
        return value or ""


def recurrence_display(is_recurring: bool, rule: Dict[str, Any]) -> str:
    if not is_recurring or not rule.get("frequency"):
        return "Does not repeat"
    try:
        name = RecurrenceFrequency(rule["frequency"]).display_name
    except ValueError:
        name = str(rule["frequency"])
    interval = int(rule.get("interval", 1) or 1)
    return f"{name} (every {interval})" if interval > 1 else name


def snapshot_task(task, column_name: str = "") -> Dict[str, Any]:
    """Capture the audited fields of a Task ORM instance (must be session-bound)."""
    rule = task.recurrence_rule or {}
    return {
        "title": task.title or "",
        "description": task.description or "",
        "priority": task.priority,
        "column_id": task.column_id,
        "column_name": column_name,
        "estimated_hours": round(float(task.estimated_hours or 0.0), 2),
        "spent_hours": round(float(task.spent_hours or 0.0), 2),
        "start_date": task.start_date,
        "due_date": task.due_date.replace(microsecond=0) if task.due_date else None,
        "is_completed": bool(task.is_completed),
        "labels": sorted(lbl.name for lbl in task.labels),
        "checklist": [
            {"text": str(item.get("text", "")), "done": bool(item.get("done"))} for item in (task.checklist or [])
        ],
        "is_recurring": bool(task.is_recurring),
        # occurrences_count is engine bookkeeping, not a user edit
        "recurrence": {k: v for k, v in rule.items() if k != "occurrences_count"} if task.is_recurring else {},
        "custom_fields": json.dumps(task.custom_fields or {}, sort_keys=True),
    }


def _checklist_changes(old: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> List[str]:
    parts: List[str] = []
    old_by_text = {i["text"]: i for i in old}
    new_by_text = {i["text"]: i for i in new}
    for text in new_by_text:
        if text not in old_by_text:
            parts.append(f"added '{text}'")
    for text in old_by_text:
        if text not in new_by_text:
            parts.append(f"removed '{text}'")
    for text, item in new_by_text.items():
        prev = old_by_text.get(text)
        if prev and prev["done"] != item["done"]:
            parts.append(f"{'checked' if item['done'] else 'unchecked'} '{text}'")
    if not parts and [i["text"] for i in old] != [i["text"] for i in new]:
        parts.append("reordered items")
    return parts


def _progress(items: List[Dict[str, Any]]) -> str:
    return f"{sum(1 for i in items if i['done'])}/{len(items)} done"


def diff_snapshots(old: Dict[str, Any], new: Dict[str, Any]) -> List[Change]:
    changes: List[Change] = []
    U = ActivityAction.TASK_UPDATED

    if old["title"] != new["title"]:
        changes.append(Change(U, "title", f"Renamed task from '{old['title']}' to '{new['title']}'", old["title"], new["title"]))

    if old["description"] != new["description"]:
        if not old["description"]:
            details = "Added a description"
        elif not new["description"]:
            details = "Cleared the description"
        else:
            details = "Edited the description"
        changes.append(Change(U, "description", details, old["description"], new["description"]))

    if old["column_id"] != new["column_id"]:
        changes.append(
            Change(
                ActivityAction.TASK_MOVED,
                "column",
                f"Moved from '{old['column_name']}' to '{new['column_name']}'",
                old["column_name"],
                new["column_name"],
            )
        )

    if old["priority"] != new["priority"]:
        o, n = priority_display(old["priority"]), priority_display(new["priority"])
        changes.append(Change(U, "priority", f"Changed priority from {o} to {n}", o, n))

    for key, label in (("estimated_hours", "estimate"), ("spent_hours", "time spent")):
        if old[key] != new[key]:
            o, n = _fmt_hours(old[key]), _fmt_hours(new[key])
            changes.append(Change(U, key, f"Changed {label} from {o} to {n}", o, n))

    for key, label, fmt in (("start_date", "start date", _fmt_date), ("due_date", "due date", _fmt_datetime)):
        if old[key] != new[key]:
            o, n = fmt(old[key]), fmt(new[key])
            if o is None:
                details = f"Set {label} to {n}"
            elif n is None:
                details = f"Removed {label} (was {o})"
            else:
                details = f"Changed {label} from {o} to {n}"
            changes.append(Change(U, key, details, o, n))

    if old["is_completed"] != new["is_completed"]:
        if new["is_completed"]:
            changes.append(Change(ActivityAction.TASK_COMPLETED, "is_completed", "Marked as complete", "Open", "Complete"))
        else:
            changes.append(Change(ActivityAction.TASK_REOPENED, "is_completed", "Reopened task", "Complete", "Open"))

    if old["labels"] != new["labels"]:
        added = [l for l in new["labels"] if l not in old["labels"]]
        removed = [l for l in old["labels"] if l not in new["labels"]]
        parts = []
        if added:
            parts.append(f"added label{'s' if len(added) > 1 else ''} {', '.join(added)}")
        if removed:
            parts.append(f"removed label{'s' if len(removed) > 1 else ''} {', '.join(removed)}")
        details = "; ".join(parts)
        changes.append(
            Change(U, "labels", details[:1].upper() + details[1:], ", ".join(old["labels"]), ", ".join(new["labels"]))
        )

    if old["checklist"] != new["checklist"]:
        parts = _checklist_changes(old["checklist"], new["checklist"])
        details = "Checklist: " + "; ".join(parts) if parts else "Updated the checklist"
        changes.append(Change(U, "checklist", details, _progress(old["checklist"]), _progress(new["checklist"])))

    if old["is_recurring"] != new["is_recurring"] or old["recurrence"] != new["recurrence"]:
        o = recurrence_display(old["is_recurring"], old["recurrence"])
        n = recurrence_display(new["is_recurring"], new["recurrence"])
        if o != n:
            if not new["is_recurring"]:
                details = "Stopped repeating"
            elif not old["is_recurring"]:
                details = f"Set to repeat: {n}"
            else:
                details = f"Changed recurrence from {o} to {n}"
            changes.append(Change(U, "recurrence", details, o, n))

    if old["custom_fields"] != new["custom_fields"]:
        changes.append(Change(U, "custom_fields", "Updated custom fields", old["custom_fields"], new["custom_fields"]))

    return changes

"""
Calendar integration: iCalendar export/import, feed subscriptions and CalDAV sync
(pure Python + urllib, no Qt).

Tasks map to iCalendar like this::

    title        SUMMARY            due date      DUE (to-dos) / DTSTART (events)
    description  DESCRIPTION        start date    DTSTART
    labels       CATEGORIES         done          STATUS:COMPLETED + COMPLETED
    priority     PRIORITY 1/3/5/9   recurrence    RRULE (simple rules both ways)

* ``export_calendar`` builds a VCALENDAR of to-dos or events (all-day or timed)
  for boards or a person, optionally with alarms. Used for .ics files, the
  live feed calendar apps can subscribe to, and the command line.
* ``import_calendar`` turns events/to-dos into tasks. Re-importing the same file
  updates the tasks it created instead of duplicating them (matched by UID).
* ``CalendarSync`` syncs a *subscription*:

  - **iCalendar feed** (any http(s)/webcal/file URL, e.g. Google Calendar's
    secret address): read-only. New items become tasks, changed items update
    them, items that disappear are archived (or deleted / kept).
  - **CalDAV calendar** (Nextcloud, iCloud, Fastmail, Radicale, Baïkal ...):
    pull, push or two-way. Both sides' states at the last sync are stored as
    hashes of the synced fields, so the engine knows who changed what:
    changes on one side are copied to the other; if both changed, the newest
    wins (or the configured side); deletions propagate. Items written back keep
    every property this app doesn't manage (attendees, alarms, X- props ...).
    Writes use ETags (If-Match / If-None-Match) so a concurrent edit on the
    server is never overwritten blindly.

Changes made by a sync are attributed to "🔄 Calendar sync: <name>".
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from kanbanmanager.core import ical
from kanbanmanager.core.ical import Component, ICalError
from kanbanmanager.enums import ColumnType, Priority

log = logging.getLogger(__name__)

SYNC_ACTOR_PREFIX = "🔄 Calendar sync"
DEFAULT_DUE_TIME = time(17, 0)
DEFAULT_START_TIME = time(9, 0)
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
HTTP_TIMEOUT = 20

DEFAULT_OPTIONS: Dict[str, Any] = {
    "include_events": True,        # feeds: turn events into tasks (to-dos always)
    "include_todos": True,
    "days_past": 7,                # events: only occurrences in this window
    "days_future": 60,
    "import_completed": False,     # create tasks for items already completed remotely
    "remote_deleted": "archive",   # archive | delete | keep
    "conflict": "newest",          # newest | remote | local
    "push_completed": False,       # push new local tasks that are already done
    "push_undated": True,          # push open tasks without a due date (CalDAV to-dos)
    "component": "VTODO",          # CalDAV: sync as to-dos or events
}

PRIORITY_TO_ICAL = {Priority.URGENT.value: 1, Priority.HIGH.value: 3, Priority.MEDIUM.value: 5, Priority.LOW.value: 9}


def priority_from_ical(value: Optional[str]) -> Optional[str]:
    try:
        n = int(str(value or "0").strip())
    except ValueError:
        return None
    if n <= 0:
        return None
    if n <= 2:
        return Priority.URGENT.value
    if n <= 4:
        return Priority.HIGH.value
    if n == 5:
        return Priority.MEDIUM.value
    return Priority.LOW.value


def task_uid(instance: str, task_id: int) -> str:
    return f"kanban-task-{task_id}-{instance}@kanbanmanager"


def _strip_markdown_images(text: str) -> str:
    return re.sub(r"!\[[^\]]*\]\(attachment:[^)]*\)\s*", "", text or "").strip()


# ---------------------------------------------------------------------------
# Normalised item (the fields that are synced)
# ---------------------------------------------------------------------------
@dataclass
class Item:
    """A calendar entry in the app's terms."""
    uid: str
    kind: str = "VTODO"                     # VTODO | VEVENT
    title: str = ""
    description: str = ""
    start: Optional[date] = None
    due: Optional[datetime] = None          # naive local
    completed: bool = False
    completed_at: Optional[datetime] = None  # naive local
    priority: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    rrule: Optional[str] = None
    last_modified: Optional[datetime] = None  # naive UTC
    in_progress: bool = False
    component: Optional[Component] = None   # the parsed entry (remote items)

    def state(self) -> Dict[str, Any]:
        """What is compared between syncs (both sides are reduced to this). Events carry no
        separate start date (an event starts at the task's due time)."""
        return {
            "title": " ".join(self.title.split()),
            "description": _strip_markdown_images(self.description).replace("\r\n", "\n").strip(),
            "start": self.start.isoformat() if self.start and self.kind == "VTODO" else None,
            "due": self.due.replace(second=0, microsecond=0).isoformat() if self.due else None,
            "completed": bool(self.completed),
            "priority": self.priority or Priority.MEDIUM.value,   # no PRIORITY = the default
            "labels": sorted({l.strip().lower() for l in self.labels if l.strip()}),
            "rrule": _normal_rrule(self.rrule),
        }

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(self.state(), sort_keys=True).encode()).hexdigest()


def _normal_rrule(rrule: Optional[str]) -> Optional[str]:
    """Rules the app can express compare by meaning ("FREQ=WEEKLY;WKST=MO" == "FREQ=WEEKLY")."""
    if not rrule:
        return None
    mapped = ical.recurrence_from_rrule(rrule)
    return ical.rrule_from_recurrence(mapped) if mapped else rrule.upper()


def item_from_task(task: Dict[str, Any], uid: str, kind: str = "VTODO") -> Item:
    due = datetime.fromisoformat(task["due_date"]) if task.get("due_date") else None
    start = date.fromisoformat(task["start_date"][:10]) if task.get("start_date") else None
    rule = task.get("recurrence_rule") or {}
    return Item(
        uid=uid, kind=kind, title=task.get("title") or "", description=task.get("description") or "",
        start=start, due=due, completed=bool(task.get("is_completed")),
        completed_at=datetime.fromisoformat(task["completed_at"]) if task.get("completed_at") else None,
        priority=task.get("priority"), labels=[l["name"] for l in task.get("labels") or []],
        rrule=ical.rrule_from_recurrence(rule) if task.get("is_recurring") and rule else None,
    )


def _in_progress_column(task: Dict[str, Any]) -> bool:
    return task.get("_column_type") == ColumnType.IN_PROGRESS.value


# ---------------------------------------------------------------------------
# Task -> iCalendar
# ---------------------------------------------------------------------------
def fill_component(comp: Component, item: Item, task: Optional[Dict[str, Any]] = None,
                   details: Optional[str] = None, event_style: str = "all_day", alarm_minutes: int = 0,
                   now: Optional[datetime] = None) -> Component:
    """Write ``item`` into ``comp`` (a new or a previously synced VTODO/VEVENT), replacing only
    the properties this app manages and keeping the rest."""
    if now is None:
        now_utc = datetime.now(timezone.utc)
    else:
        now_utc = (now if now.tzinfo else now.astimezone()).astimezone(timezone.utc)
    managed = ["DTSTAMP", "SUMMARY", "DESCRIPTION", "DTSTART", "DUE", "DTEND", "DURATION", "STATUS", "COMPLETED",
               "PERCENT-COMPLETE", "PRIORITY", "CATEGORIES", "RRULE", "LAST-MODIFIED", "TRANSP"]
    for name in managed:
        comp.remove(name)
    if comp.get("UID") is None:
        comp.props.insert(0, ical.Prop("UID", ical.escape_text(item.uid)))
    comp.set("DTSTAMP", now_utc.strftime("%Y%m%dT%H%M%SZ"))
    if task is not None:
        if task.get("created_at") and comp.get("CREATED") is None:
            comp.set("CREATED", ical.format_naive_utc(datetime.fromisoformat(task["created_at"])))
        if task.get("updated_at"):
            comp.set("LAST-MODIFIED", ical.format_utc(datetime.fromisoformat(task["updated_at"])))
    title = item.title
    if item.kind == "VEVENT" and item.completed:
        title = f"✓ {title}"
    comp.set_text("SUMMARY", title)
    description = _strip_markdown_images(item.description)
    if details:
        description = f"{description}\n\n{details}".strip()
    if description:
        comp.set_text("DESCRIPTION", description)
    all_day = event_style == "all_day"
    if item.kind == "VEVENT":
        if item.due is None:
            raise ValueError("an event needs a date")
        if all_day:
            first = item.start if item.start and item.start <= item.due.date() else item.due.date()
            comp.set("DTSTART", ical.format_date(first), {"VALUE": "DATE"})
            comp.set("DTEND", ical.format_date(item.due.date() + timedelta(days=1)), {"VALUE": "DATE"})
        else:  # the event starts at the due time (so it reads back as the same due time)
            hours = float((task or {}).get("estimated_hours") or 0)
            length = timedelta(hours=min(max(hours, 0.5), 8)) if hours else timedelta(minutes=30)
            comp.set("DTSTART", ical.format_utc(item.due))
            comp.set("DTEND", ical.format_utc(item.due + length))
        comp.set("TRANSP", "TRANSPARENT")  # tasks don't make you busy
        comp.set("STATUS", "CONFIRMED")
    else:
        if item.due is not None:
            comp.set("DUE", ical.format_utc(item.due))
            if item.start is not None:  # DTSTART must not be after DUE and must be a DATE-TIME too
                start_at = datetime.combine(item.start, DEFAULT_START_TIME)
                if start_at > item.due:
                    start_at = datetime.combine(item.start, time(0, 0))
                if start_at <= item.due:
                    comp.set("DTSTART", ical.format_utc(start_at))
        elif item.start is not None:
            comp.set("DTSTART", ical.format_date(item.start), {"VALUE": "DATE"})
        if item.completed:
            comp.set("STATUS", "COMPLETED")
            comp.set("COMPLETED", ical.format_utc(item.completed_at or datetime.now()))
            comp.set("PERCENT-COMPLETE", "100")
        else:
            comp.set("STATUS", "IN-PROCESS" if item.in_progress else "NEEDS-ACTION")
            checklist = (task or {}).get("checklist") or []
            if checklist:
                done = sum(1 for c in checklist if c.get("done"))
                comp.set("PERCENT-COMPLETE", str(int(done * 100 / len(checklist))))
    if item.priority in PRIORITY_TO_ICAL:
        comp.set("PRIORITY", str(PRIORITY_TO_ICAL[item.priority]))
    if item.labels:
        comp.set("CATEGORIES", ",".join(ical.escape_text(l) for l in item.labels))
    if item.rrule:
        comp.set("RRULE", item.rrule)
    if alarm_minutes and item.due is not None and not item.completed:
        comp.children = [c for c in comp.children if c.name != "VALARM" or c.text("X-KANBAN") != "1"]
        alarm = Component("VALARM")
        alarm.set("ACTION", "DISPLAY")
        alarm.set_text("DESCRIPTION", item.title)
        alarm.set("TRIGGER", ical.format_duration(-timedelta(minutes=alarm_minutes)),
                  {"RELATED": "END"} if item.kind == "VEVENT" else {})
        alarm.set("X-KANBAN", "1")
        comp.children.append(alarm)
    return comp


def _details(task: Dict[str, Any]) -> str:
    parts = [f"Board: {task.get('_board_name', '')}", f"Column: {task.get('_column_name', '')}"]
    if task.get("assignee"):
        parts.append(f"Assignee: {task['assignee']}")
    for name, value in (task.get("_field_texts") or {}).items():
        parts.append(f"{name}: {value}")
    return " · ".join(p for p in parts if not p.endswith(": "))


def collect_tasks(service, board_ids: Optional[Iterable[int]] = None, person: Optional[str] = None,
                  include_completed: bool = False) -> List[Dict[str, Any]]:
    """Task dicts (plus _board_name, _column_name, _column_type, _field_texts) to export."""
    from kanbanmanager.core import custom_fields as cf

    boards = [b for ws in service.get_workspaces() for b in ws.boards if not b.is_archived]
    if board_ids is not None:
        wanted = set(board_ids)
        boards = [b for b in boards if b.id in wanted]
    out = []
    for board in boards:
        columns = {c.id: c for c in board.columns}
        fields = service.get_custom_fields(board.id)
        for t in service.get_tasks_for_board(board.id):
            if person and (t.assignee or "") != person:
                continue
            if t.is_completed and not include_completed:
                continue
            data = t.to_dict()
            col = columns.get(t.column_id)
            data["_board_name"], data["_board_id"] = board.name, board.id
            data["_column_name"] = col.name if col else ""
            data["_column_type"] = col.column_type if col else ""
            values = t.field_value_map
            data["_field_texts"] = {f["name"]: cf.format_value(f, values[f["id"]]) for f in fields
                                    if f["id"] in values and not cf.is_empty(f, values[f["id"]])}
            out.append(data)
    return out


def export_calendar(service, board_ids: Optional[Iterable[int]] = None, person: Optional[str] = None,
                    kind: str = "VTODO", include_completed: bool = False, include_undated: bool = False,
                    event_style: str = "all_day", alarm_minutes: int = 0, name: Optional[str] = None,
                    details: bool = True, refresh_minutes: Optional[int] = None,
                    now: Optional[datetime] = None) -> str:
    """An iCalendar document of tasks: ``kind`` "VTODO" (to-dos) or "VEVENT" (events on the
    due date). Events need a due date; to-dos without one are included with ``include_undated``."""
    instance = service.instance_id()
    cal = ical.new_calendar(name or "KanbanManager", refresh_minutes)
    for task in collect_tasks(service, board_ids, person, include_completed):
        if task.get("due_date") is None and (kind == "VEVENT" or not include_undated):
            continue
        link = service.get_external_link_for_task(task["id"])
        uid = link["uid"] if link else task_uid(instance, task["id"])
        item = item_from_task(task, uid, kind)
        item.in_progress = _in_progress_column(task)
        comp = fill_component(Component(kind), item, task, details=_details(task) if details else None,
                              event_style=event_style, alarm_minutes=alarm_minutes, now=now)
        comp.set_text("X-KANBAN-BOARD", task["_board_name"])
        comp.set_text("X-KANBAN-COLUMN", task["_column_name"])
        comp.set("X-KANBAN-TASK-ID", str(task["id"]))
        cal.children.append(comp)
    return ical.serialize(cal)


# ---------------------------------------------------------------------------
# iCalendar -> tasks
# ---------------------------------------------------------------------------
def _local_due(value, all_day_time: time = DEFAULT_DUE_TIME) -> Optional[datetime]:
    if value is None:
        return None
    value = ical.to_local(value)
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0)
    return datetime.combine(value, all_day_time)


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    value = ical.to_local(value)
    return value.date() if isinstance(value, datetime) else value


def _utc_naive(value) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def item_from_component(comp: Component, now: Optional[datetime] = None, days_past: int = 7,
                        days_future: int = 60) -> Optional[Item]:
    """An Item for a VTODO/VEVENT, or None when it's outside the window (recurring events:
    the next occurrence in the window) or cancelled."""
    kind = comp.name
    uid = (comp.text("UID") or "").strip()
    if kind not in ("VTODO", "VEVENT") or not uid:
        return None
    status = (comp.text("STATUS") or "").strip().upper()
    if status == "CANCELLED":
        return None
    now = now or datetime.now()
    summary = (comp.text("SUMMARY") or "").strip() or "(no title)"
    done_event = kind == "VEVENT" and summary.startswith("✓ ")  # how completed tasks are written as events
    if done_event:
        summary = summary[2:].strip() or "(no title)"
    item = Item(uid=uid, kind=kind, title=summary, description=(comp.text("DESCRIPTION") or "").strip(),
                component=comp, completed=done_event)
    item.labels = [c for p in comp.get_all("CATEGORIES") for c in ical.split_list(p.value)]
    item.priority = priority_from_ical(comp.text("PRIORITY"))
    lm = comp.get("LAST-MODIFIED") or comp.get("DTSTAMP")
    if lm is not None:
        try:
            item.last_modified = _utc_naive(ical.parse_date_value(lm.value, lm.params))
        except ICalError:
            pass
    rrule = comp.text("RRULE")
    if kind == "VTODO":
        start = ical.prop_date(comp, "DTSTART")
        due = ical.prop_date(comp, "DUE")
        if due is None and start is not None and comp.get("DURATION") is not None:
            due = start + ical.parse_duration(comp.text("DURATION"))
        item.start = _as_date(start)
        item.due = _local_due(due)
        item.completed = status == "COMPLETED" or comp.get("COMPLETED") is not None
        item.in_progress = status == "IN-PROCESS"
        done_at = ical.prop_date(comp, "COMPLETED")
        item.completed_at = _local_due(done_at) if done_at is not None else None
        item.rrule = rrule
        return item
    # VEVENT
    start = ical.prop_date(comp, "DTSTART")
    if start is None:
        return None
    end = ical.prop_date(comp, "DTEND")
    if end is None and comp.get("DURATION") is not None:
        end = start + ical.parse_duration(comp.text("DURATION"))
    all_day = not isinstance(start, datetime)
    length = (end - start) if end is not None else (timedelta(days=1) if all_day else timedelta(0))
    if rrule:
        # the next occurrence (or the current one) within the window
        exdates = []
        for p in comp.get_all("EXDATE"):
            for v in p.value.split(","):
                try:
                    exdates.append(ical.parse_date_value(v, p.params))
                except ICalError:
                    pass
        if all_day:
            lo, hi = (now - timedelta(days=days_past)).date(), (now + timedelta(days=days_future)).date()
        else:
            tz = start.tzinfo
            base = now.astimezone(tz) if tz else now
            lo, hi = base - timedelta(days=days_past), base + timedelta(days=days_future)
        today = now.date() if all_day else (now.astimezone(start.tzinfo) if start.tzinfo else now)
        try:
            occ = ical.occurrences(start, rrule, lo, hi, exdates, limit=400)
        except ICalError:
            occ = []
        upcoming = [o for o in occ if (o + length if not all_day else o + length - timedelta(days=1)) >= today]
        if not upcoming:
            return None
        start = upcoming[0]
        end = start + length
        item.rrule = rrule
    else:
        last = (end - timedelta(days=1)) if all_day and end is not None else (end or start)
        first_local = _as_date(start)
        last_local = _as_date(last) if all_day else _as_date(start)
        if last_local < (now - timedelta(days=days_past)).date() or first_local > (now + timedelta(days=days_future)).date():
            return None
    if all_day:
        last_day = (end - timedelta(days=1)) if end is not None and end > start else start
        item.start = start
        item.due = datetime.combine(last_day, DEFAULT_DUE_TIME)
    else:
        item.due = _local_due(start)
        item.start = item.due.date()
    return item


@dataclass
class ParseResult:
    items: List[Item]
    skipped: int = 0
    warnings: List[str] = field(default_factory=list)
    name: str = ""


def parse_items(text: str, now: Optional[datetime] = None, include_events: bool = True, include_todos: bool = True,
                days_past: int = 7, days_future: int = 60) -> ParseResult:
    """Items of an iCalendar document. Raises ICalError for unreadable data."""
    cal = ical.parse_calendar(text)
    result = ParseResult(items=[], name=cal.text("X-WR-CALNAME") or cal.text("NAME") or "")
    seen = {}
    for comp in cal.children:
        if comp.name not in ("VTODO", "VEVENT"):
            continue
        if (comp.name == "VEVENT" and not include_events) or (comp.name == "VTODO" and not include_todos):
            result.skipped += 1
            continue
        if comp.get("RECURRENCE-ID") is not None:
            result.skipped += 1  # an edited single occurrence of a series: the series itself is imported
            continue
        try:
            item = item_from_component(comp, now, days_past, days_future)
        except (ICalError, ValueError) as exc:
            result.warnings.append(f"Skipped '{comp.text('SUMMARY') or comp.text('UID')}': {exc}")
            continue
        if item is None:
            result.skipped += 1
            continue
        if item.uid in seen:
            result.skipped += 1
            continue
        seen[item.uid] = True
        result.items.append(item)
    return result


def _task_state_item(task, uid: str, kind: str) -> Item:
    return item_from_task(task.to_dict() if hasattr(task, "to_dict") else task, uid, kind)


def _done_column(service, board_id: int) -> Optional[int]:
    col = next((c for c in service.get_columns_for_board(board_id) if c.is_done_column), None)
    return col.id if col else None


def _in_progress_column_id(service, board_id: int) -> Optional[int]:
    col = next((c for c in service.get_columns_for_board(board_id)
                if c.column_type == ColumnType.IN_PROGRESS.value and not c.is_done_column), None)
    return col.id if col else None


def apply_item(service, board_id: int, item: Item, task_id: Optional[int] = None,
               column_id: Optional[int] = None, origin: Optional[str] = None) -> int:
    """Create or update a task from an item; returns the task id."""
    label_ids = []
    for name in item.labels:
        lbl = service.get_label_by_name(board_id, name, create_color="#64748B")
        if lbl is not None and lbl.id not in label_ids:
            label_ids.append(lbl.id)
    recurrence = ical.recurrence_from_rrule(item.rrule) if item.rrule else None
    fields: Dict[str, Any] = {
        "title": item.title[:255], "description": item.description, "start_date": item.start,
        "due_date": item.due, "is_recurring": recurrence is not None, "recurrence_rule": recurrence or {},
    }
    if item.priority:
        fields["priority"] = item.priority
    if task_id is None:
        target = column_id or service.default_capture_column(board_id)
        if item.completed:
            target = _done_column(service, board_id) or target
        elif item.in_progress:
            target = _in_progress_column_id(service, board_id) or target
        task = service.create_task(board_id=board_id, column_id=target, label_ids=label_ids,
                                   priority=fields.pop("priority", Priority.MEDIUM.value), origin=origin,
                                   **{k: v for k, v in fields.items()})
        if item.completed and not task.is_completed:
            service.update_task(task.id, is_completed=True, completed_at=item.completed_at or datetime.now())
        return task.id
    task = service.get_task(task_id)
    if task is None:
        raise ValueError("task not found")
    if _strip_markdown_images(task.description or "") == _strip_markdown_images(item.description):
        fields.pop("description")  # same text: keep the local inline images (calendars don't get them)
    if item.kind == "VEVENT" and not item.start:
        fields.pop("start_date")
    service.update_task(task_id, label_ids=label_ids, **fields)
    if item.completed != bool(task.is_completed):
        if item.completed:
            done = _done_column(service, task.board_id)
            if done and task.column_id != done:
                service.move_task(task_id, done, 10 ** 6)
            if not service.get_task(task_id).is_completed:
                service.update_task(task_id, is_completed=True, completed_at=item.completed_at or datetime.now())
        else:
            target = column_id or service.default_capture_column(task.board_id)
            current = service.get_task(task_id)
            if target and current.column_id != target and any(
                    c.id == current.column_id and c.is_done_column for c in service.get_columns_for_board(task.board_id)):
                service.move_task(task_id, target, 0)
            if service.get_task(task_id).is_completed:
                service.update_task(task_id, is_completed=False, completed_at=None)
    return task_id


@dataclass
class ImportSummary:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    task_ids: List[int] = field(default_factory=list)      # tasks created (for "undo")
    warnings: List[str] = field(default_factory=list)
    name: str = ""

    def describe(self) -> str:
        parts = [f"{self.created} created"]
        if self.updated:
            parts.append(f"{self.updated} updated")
        if self.unchanged:
            parts.append(f"{self.unchanged} unchanged")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return ", ".join(parts)


def import_calendar(service, text: str, board_id: int, column_id: Optional[int] = None,
                    include_events: bool = True, include_todos: bool = True, include_completed: bool = False,
                    days_past: int = 30, days_future: int = 365, now: Optional[datetime] = None,
                    source: str = "ics-file") -> ImportSummary:
    """Import an .ics document into a board. Items imported before (same UID, task still there)
    are updated instead of duplicated."""
    parsed = parse_items(text, now, include_events, include_todos, days_past, days_future)
    summary = ImportSummary(skipped=parsed.skipped, warnings=list(parsed.warnings), name=parsed.name)
    links = {l["uid"]: l for l in service.get_external_links(source)}
    for item in parsed.items:
        link = links.get(item.uid)
        task = service.get_task(link["task_id"]) if link and link.get("task_id") else None
        if task is not None and task.board_id != board_id:
            task = None
        try:
            if task is None:
                if item.completed and not include_completed:
                    summary.skipped += 1
                    continue
                task_id = apply_item(service, board_id, item, column_id=column_id,
                                     origin="imported from a calendar")
                summary.created += 1
                summary.task_ids.append(task_id)
            else:
                before = _task_state_item(task, item.uid, item.kind).hash()
                if before == item.hash():
                    summary.unchanged += 1
                    continue
                task_id = apply_item(service, board_id, item, task_id=task.id, column_id=column_id)
                summary.updated += 1
        except Exception as exc:  # a bad item must not abort the rest
            log.exception("calendar import failed for %s", item.uid)
            summary.warnings.append(f"Could not import '{item.title}': {exc}")
            continue
        service.save_external_link(source, item.uid, task_id=task_id, remote_hash=item.hash())
    return summary


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class SyncError(Exception):
    """A sync problem shown to the user."""


class PreconditionFailed(SyncError):
    """The remote item changed since we last saw it (HTTP 412)."""


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes

    def text(self) -> str:
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", self.headers.get("content-type", ""), re.I)
        if m:
            charset = m.group(1)
        try:
            return self.body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            return self.body.decode("latin-1")


Transport = Callable[[str, str, Dict[str, str], Optional[bytes]], Response]


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    low = url.lower()
    if low.startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    if low.startswith("webcals://"):
        return "https://" + url[len("webcals://"):]
    return url


def http_transport(username: Optional[str] = None, password: Optional[str] = None) -> Transport:
    """urllib-based transport with Basic auth (sent pre-emptively) and Digest fallback."""
    def request(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Response:
        url = normalize_url(url)
        hdrs = {"User-Agent": "KanbanManager/1.0", **headers}
        if username:
            token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
            hdrs.setdefault("Authorization", f"Basic {token}")
        handlers: List[Any] = []
        if username:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, url, username, password or "")
            handlers.append(urllib.request.HTTPDigestAuthHandler(mgr))
        opener = urllib.request.build_opener(*handlers, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                data = resp.read(MAX_DOWNLOAD_BYTES + 1)
                if len(data) > MAX_DOWNLOAD_BYTES:
                    raise SyncError("The calendar is too large (over 20 MB)")
                return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, data)
        except urllib.error.HTTPError as exc:
            return Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b"")
        except urllib.error.URLError as exc:
            raise SyncError(f"Could not reach {urllib.parse.urlsplit(url).netloc or url}: {exc.reason}") from None
        except (TimeoutError, OSError) as exc:
            raise SyncError(f"Network error: {exc}") from None
    return request


def fetch_text(url: str, username: Optional[str] = None, password: Optional[str] = None,
               transport: Optional[Transport] = None) -> str:
    """Download a calendar from http(s)/webcal or read it from a file path / file:// URL."""
    url = (url or "").strip()
    if url.lower().startswith("file://"):
        path = Path(urllib.request.url2pathname(urllib.parse.urlsplit(url).path))
        return _read_file(path)
    if "://" not in url:
        return _read_file(Path(url).expanduser())
    resp = (transport or http_transport(username, password))("GET", url, {"Accept": "text/calendar"}, None)
    if resp.status == 401:
        raise SyncError("The server refused the user name or password (401)")
    if resp.status == 404:
        raise SyncError("The calendar address was not found (404)")
    if resp.status >= 400:
        raise SyncError(f"The server answered {resp.status}")
    return resp.text()


def _read_file(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SyncError(f"Could not read {path}: {exc.strerror or exc}") from None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# ---------------------------------------------------------------------------
# CalDAV
# ---------------------------------------------------------------------------
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "cs": "http://calendarserver.org/ns/"}


def _propfind_body(props: str) -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:" '
            f'xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/">'
            f'<d:prop>{props}</d:prop></d:propfind>').encode()


@dataclass
class RemoteEntry:
    href: str
    etag: Optional[str]
    text: str


class CalDAVClient:
    """The handful of CalDAV operations sync needs (RFC 4791 / RFC 4918)."""

    def __init__(self, url: str, username: Optional[str] = None, password: Optional[str] = None,
                 transport: Optional[Transport] = None):
        self.url = normalize_url(url)
        if not self.url.endswith("/"):
            self.url += "/"
        self.http = transport or http_transport(username, password)

    def _abs(self, href: str) -> str:
        return urllib.parse.urljoin(self.url, href)

    def _multistatus(self, resp: Response, what: str) -> ET.Element:
        if resp.status == 401:
            raise SyncError("The server refused the user name or password (401)")
        if resp.status == 404:
            raise SyncError("Calendar not found (404) - check the address")
        if resp.status != 207:
            raise SyncError(f"{what} failed: the server answered {resp.status}")
        try:
            return ET.fromstring(resp.body)
        except ET.ParseError as exc:
            raise SyncError(f"{what}: unreadable server response ({exc})") from None

    def propfind(self, url: str, props: str, depth: str = "0") -> ET.Element:
        resp = self.http("PROPFIND", url, {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
                         _propfind_body(props))
        return self._multistatus(resp, "Looking up the calendar")

    @staticmethod
    def _responses(root: ET.Element):
        for r in root.findall("d:response", NS):
            href = (r.findtext("d:href", default="", namespaces=NS) or "").strip()
            props = {}
            for ps in r.findall("d:propstat", NS):
                status = ps.findtext("d:status", default="", namespaces=NS)
                if " 200 " not in f" {status} ":
                    continue
                prop = ps.find("d:prop", NS)
                if prop is not None:
                    for child in prop:
                        props[child.tag] = child
            yield href, props

    def _calendar_info(self, href: str, props: Dict[str, ET.Element]) -> Optional[Dict[str, Any]]:
        rt = props.get("{DAV:}resourcetype")
        if rt is None or rt.find("c:calendar", NS) is None:
            return None
        comps = props.get("{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
        names = [c.get("name", "").upper() for c in comps.findall("c:comp", NS)] if comps is not None else \
            ["VEVENT", "VTODO"]
        name_el = props.get("{DAV:}displayname")
        return {"url": self._abs(href), "href": href, "name": (name_el.text or "").strip() if name_el is not None
                else href.rstrip("/").rsplit("/", 1)[-1], "components": names}

    CAL_PROPS = "<d:resourcetype/><d:displayname/><c:supported-calendar-component-set/><cs:getctag/>"

    def discover(self) -> List[Dict[str, Any]]:
        """Calendars reachable from the URL: the URL itself if it is a calendar, otherwise the
        calendars in the user's calendar home (via current-user-principal)."""
        root = self.propfind(self.url, self.CAL_PROPS + "<d:current-user-principal/><c:calendar-home-set/>")
        for href, props in self._responses(root):
            info = self._calendar_info(href or self.url, props)
            if info:
                return [info]
            home = props.get("{urn:ietf:params:xml:ns:caldav}calendar-home-set")
            principal = props.get("{DAV:}current-user-principal")
            home_href = home.findtext("d:href", default="", namespaces=NS) if home is not None else ""
            if not home_href and principal is not None:
                p_href = principal.findtext("d:href", default="", namespaces=NS)
                if p_href:
                    proot = self.propfind(self._abs(p_href), "<c:calendar-home-set/>")
                    for _h, pprops in self._responses(proot):
                        h = pprops.get("{urn:ietf:params:xml:ns:caldav}calendar-home-set")
                        if h is not None:
                            home_href = h.findtext("d:href", default="", namespaces=NS) or ""
            if home_href:
                hroot = self.propfind(self._abs(home_href), self.CAL_PROPS, depth="1")
                found = [i for h, p in self._responses(hroot) if (i := self._calendar_info(h, p))]
                return found
        return []

    def list_entries(self, component: str = "VTODO") -> List[RemoteEntry]:
        body = (f'<?xml version="1.0" encoding="utf-8"?><c:calendar-query xmlns:d="DAV:" '
                f'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/><c:calendar-data/></d:prop>'
                f'<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="{component}"/></c:comp-filter>'
                f'</c:filter></c:calendar-query>').encode()
        resp = self.http("REPORT", self.url, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, body)
        root = self._multistatus(resp, "Reading the calendar")
        out = []
        for href, props in self._responses(root):
            data = props.get("{urn:ietf:params:xml:ns:caldav}calendar-data")
            if data is None or not (data.text or "").strip():
                continue
            etag = props.get("{DAV:}getetag")
            out.append(RemoteEntry(href=href, etag=(etag.text or "").strip() if etag is not None else None,
                                   text=data.text))
        return out

    def put(self, href: str, text: str, etag: Optional[str] = None, create: bool = False) -> Optional[str]:
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if create:
            headers["If-None-Match"] = "*"
        elif etag:
            headers["If-Match"] = etag
        resp = self.http("PUT", self._abs(href), headers, text.encode("utf-8"))
        if resp.status == 412:
            raise PreconditionFailed("changed on the server meanwhile")
        if resp.status == 401:
            raise SyncError("The server refused the user name or password (401)")
        if resp.status not in (200, 201, 204):
            raise SyncError(f"Saving to the calendar failed: the server answered {resp.status}")
        return resp.headers.get("etag")

    def delete(self, href: str, etag: Optional[str] = None) -> None:
        headers = {"If-Match": etag} if etag else {}
        resp = self.http("DELETE", self._abs(href), headers, None)
        if resp.status == 412:
            raise PreconditionFailed("changed on the server meanwhile")
        if resp.status not in (200, 204, 404):
            raise SyncError(f"Deleting from the calendar failed: the server answered {resp.status}")


# ---------------------------------------------------------------------------
# Sync engine
# ---------------------------------------------------------------------------
@dataclass
class SyncReport:
    subscription_id: int
    name: str
    status: str = "success"      # success | partial | failed
    counts: Dict[str, int] = field(default_factory=lambda: {"created": 0, "updated": 0, "removed": 0,
                                                             "pushed": 0, "conflicts": 0})
    messages: List[str] = field(default_factory=list)

    def describe(self) -> str:
        c = self.counts
        parts = []
        for key, label in (("created", "new"), ("updated", "updated"), ("removed", "removed"),
                           ("pushed", "sent to the calendar"), ("conflicts", "conflicts")):
            if c.get(key):
                parts.append(f"{c[key]} {label}")
        text = ", ".join(parts) or "no changes"
        if self.status == "failed":
            return f"failed: {self.messages[-1] if self.messages else 'unknown error'}"
        if self.status == "partial":
            text += f" ({len(self.messages)} problem{'s' if len(self.messages) != 1 else ''})"
        return text


def options_for(sub: Dict[str, Any]) -> Dict[str, Any]:
    opts = dict(DEFAULT_OPTIONS)
    opts.update({k: v for k, v in (sub.get("options") or {}).items() if k in DEFAULT_OPTIONS})
    return opts


class CalendarSync:
    """Runs subscriptions. ``transport_factory(username, password)`` can be replaced in tests."""

    def __init__(self, service, transport_factory: Optional[Callable[..., Transport]] = None,
                 now: Optional[Callable[[], datetime]] = None):
        self.service = service
        self.transport_factory = transport_factory or http_transport
        self._now = now or datetime.now

    # Public ------------------------------------------------------------------
    def due_subscriptions(self) -> List[Dict[str, Any]]:
        """Enabled subscriptions whose interval has passed since their last sync."""
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        out = []
        for sub in self.service.get_calendar_subscriptions():
            if not sub["enabled"] or sub["interval_minutes"] <= 0:
                continue
            last = datetime.fromisoformat(sub["last_sync_at"]) if sub["last_sync_at"] else None
            if last is None or now_utc - last >= timedelta(minutes=sub["interval_minutes"]):
                out.append(sub)
        return out

    def sync_due(self) -> List[SyncReport]:
        return [self.sync(sub["id"]) for sub in self.due_subscriptions()]

    def sync(self, sub_id: int) -> SyncReport:
        sub = self.service.get_calendar_subscription(sub_id, include_secret=True)
        if sub is None:
            raise ValueError("Subscription not found")
        report = SyncReport(sub_id, sub["name"])
        started = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            with self.service.acting_as(f"{SYNC_ACTOR_PREFIX}: {sub['name']}"):
                if sub["kind"] == "caldav":
                    self._sync_caldav(sub, report)
                else:
                    self._sync_feed(sub, report)
        except (SyncError, ICalError) as exc:
            report.status = "failed"
            report.messages.append(str(exc))
        except Exception as exc:  # never let a sync take the app down
            log.exception("Calendar sync failed")
            report.status = "failed"
            report.messages.append(f"Unexpected error: {exc}")
        if report.status != "failed" and report.messages:
            report.status = "partial"
        self.service.record_sync_run(sub_id, started, report.status, report.counts,
                                     "\n".join(report.messages) or report.describe())
        self.service.set_subscription_status(sub_id, report.status, report.describe())
        return report

    # Shared -------------------------------------------------------------------
    @staticmethod
    def source(sub: Dict[str, Any]) -> str:
        return f"sub:{sub['id']}"

    def _links(self, sub) -> Dict[str, Dict[str, Any]]:
        return {l["uid"]: l for l in self.service.get_external_links(self.source(sub))}

    def _task(self, task_id: Optional[int], board_id: int):
        if not task_id:
            return None
        task = self.service.get_task(task_id)
        if task is None or task.board_id != board_id:
            return None
        return task

    def _pull(self, sub, item: Item, link: Optional[Dict[str, Any]], report: SyncReport,
              href: Optional[str] = None, etag: Optional[str] = None, raw: Optional[str] = None) -> None:
        task = self._task(link.get("task_id") if link else None, sub["board_id"])
        new = task is None
        task_id = apply_item(self.service, sub["board_id"], item, task_id=None if new else task.id,
                             column_id=sub["column_id"], origin=f"from the calendar '{sub['name']}'")
        report.counts["created" if new else "updated"] += 1
        task = self.service.get_task(task_id)
        self.service.save_external_link(self.source(sub), item.uid, subscription_id=sub["id"], task_id=task_id,
                                        href=href, etag=etag, remote_hash=item.hash(), remote_data=raw,
                                        local_hash=_task_state_item(task, item.uid, item.kind).hash())

    def _remove_local(self, sub, task, opts, report: SyncReport, why: str) -> None:
        policy = opts["remote_deleted"]
        if policy == "keep":
            self.service.log_task_note(task.id, f"{why} - kept here (the sync no longer links them)")
            return
        if policy == "delete":
            self.service.delete_task(task.id)
        else:
            self.service.archive_task(task.id, reason=why)
        report.counts["removed"] += 1

    # iCalendar feed (read-only) --------------------------------------------------
    def _sync_feed(self, sub, report: SyncReport) -> None:
        opts = options_for(sub)
        text = fetch_text(sub["url"], sub["username"] or None, sub.get("password") or None,
                          transport=self.transport_factory(sub["username"] or None, sub.get("password") or None)
                          if "://" in sub["url"] and not sub["url"].lower().startswith("file://") else None)
        parsed = parse_items(text, self._now(), opts["include_events"], opts["include_todos"],
                             int(opts["days_past"]), int(opts["days_future"]))
        report.messages.extend(parsed.warnings)
        links = self._links(sub)
        remote = {i.uid: i for i in parsed.items}
        for uid, item in remote.items():
            link = links.get(uid)
            task = self._task(link.get("task_id") if link else None, sub["board_id"])
            if link and task is None:
                continue  # deleted here on purpose: don't bring it back
            if link is None and item.completed and not opts["import_completed"]:
                continue
            if link is not None and link.get("remote_hash") == item.hash():
                continue  # unchanged in the calendar (local edits are kept)
            try:
                self._pull(sub, item, link, report, raw=None)
            except Exception as exc:
                log.exception("feed item failed")
                report.messages.append(f"'{item.title}': {exc}")
        for uid, link in links.items():
            if uid in remote:
                continue
            task = self._task(link.get("task_id"), sub["board_id"])
            if task is not None and not task.is_archived:
                self._remove_local(sub, task, opts, report, f"Removed from the calendar '{sub['name']}'")
            self.service.delete_external_link(self.source(sub), uid)

    # CalDAV (pull / push / two-way) -------------------------------------------------
    def _sync_caldav(self, sub, report: SyncReport) -> None:
        opts = options_for(sub)
        kind = "VEVENT" if opts["component"] == "VEVENT" else "VTODO"
        can_pull = sub["direction"] in ("pull", "two_way")
        can_push = sub["direction"] in ("push", "two_way")
        client = CalDAVClient(sub["url"], sub["username"] or None, sub.get("password") or None,
                              transport=self.transport_factory(sub["username"] or None, sub.get("password") or None))
        entries = client.list_entries(kind)
        remote: Dict[str, Tuple[Item, RemoteEntry]] = {}
        for entry in entries:
            try:
                cal = ical.parse_calendar(entry.text)
            except ICalError as exc:
                report.messages.append(f"Unreadable entry {entry.href}: {exc}")
                continue
            comp = next((c for c in cal.children if c.name == kind and c.get("RECURRENCE-ID") is None), None)
            if comp is None:
                continue
            item = item_from_component(comp, self._now(), days_past=36500, days_future=36500)
            if item is not None:
                remote[item.uid] = (item, entry)
        links = self._links(sub)
        source = self.source(sub)

        # 1. Items known on both sides, or only remotely
        for uid, (item, entry) in remote.items():
            link = links.get(uid)
            try:
                if link is None:
                    if not can_pull or (item.completed and not opts["import_completed"]):
                        continue
                    self._pull(sub, item, None, report, entry.href, entry.etag, entry.text)
                    continue
                task = self._task(link.get("task_id"), sub["board_id"])
                if task is None:
                    if can_push and link.get("task_id") is None:  # deleted here: delete there too
                        client.delete(entry.href, entry.etag)
                        report.counts["pushed"] += 1
                        self.service.delete_external_link(source, uid)
                    continue
                local_item = _task_state_item(task, uid, kind)
                local_changed = link.get("local_hash") != local_item.hash()
                remote_changed = link.get("remote_hash") != item.hash()
                if local_changed and remote_changed:
                    winner = self._resolve(opts, task, item)
                    report.counts["conflicts"] += 1
                    side = "the calendar's" if winner == "remote" else "this board's"
                    self.service.log_task_note(task.id, f"Sync conflict with '{sub['name']}': both sides changed, "
                                                        f"kept {side} version")
                    if winner == "remote" and can_pull:
                        self._pull(sub, item, link, report, entry.href, entry.etag, entry.text)
                    elif winner == "local" and can_push:
                        self._push(client, sub, task, uid, kind, report, link, entry)
                elif remote_changed and can_pull:
                    self._pull(sub, item, link, report, entry.href, entry.etag, entry.text)
                elif local_changed and can_push:
                    self._push(client, sub, task, uid, kind, report, link, entry)
                elif remote_changed or entry.etag != link.get("etag"):
                    # nothing to copy (direction), but remember the server's latest version
                    self.service.save_external_link(source, uid, subscription_id=sub["id"], etag=entry.etag,
                                                    href=entry.href, remote_data=entry.text,
                                                    remote_hash=item.hash() if can_pull else link.get("remote_hash"))
            except PreconditionFailed:
                report.messages.append(f"'{item.title}' changed on the server during the sync - it will be "
                                       "synced next time")
            except SyncError as exc:
                report.messages.append(f"'{item.title}': {exc}")

        # 2. Linked items that are gone remotely
        for uid, link in links.items():
            if uid in remote:
                continue
            task = self._task(link.get("task_id"), sub["board_id"])
            try:
                if task is None:
                    self.service.delete_external_link(source, uid)
                elif can_push and not can_pull:
                    self._push(client, sub, task, uid, kind, report, None, None)  # push-only: recreate it
                elif can_pull:
                    if not task.is_archived:
                        self._remove_local(sub, task, opts, report, f"Deleted in the calendar '{sub['name']}'")
                    self.service.delete_external_link(source, uid)
            except SyncError as exc:
                report.messages.append(f"'{task.title if task else uid}': {exc}")

        # 3. New local tasks
        if can_push:
            linked = {l.get("task_id") for l in self.service.get_external_links(source)}
            for t in self.service.get_tasks_for_board(sub["board_id"]):
                if t.id in linked or (t.is_completed and not opts["push_completed"]):
                    continue
                if t.due_date is None and (kind == "VEVENT" or not opts["push_undated"]):
                    continue
                if self.service.get_external_link_for_task(t.id) is not None:
                    continue  # linked to another calendar already
                uid = f"kanban-task-{t.id}-{uuid.uuid4().hex[:8]}@kanbanmanager"
                try:
                    self._push(client, sub, t, uid, kind, report, None, None, create=True)
                except SyncError as exc:
                    report.messages.append(f"'{t.title}': {exc}")

    @staticmethod
    def _resolve(opts: Dict[str, Any], task, item: Item) -> str:
        if opts["conflict"] in ("remote", "local"):
            return opts["conflict"]
        local_time = task.updated_at
        if local_time is not None:
            local_time = local_time.astimezone(timezone.utc).replace(tzinfo=None) if local_time.tzinfo else \
                local_time.astimezone().astimezone(timezone.utc).replace(tzinfo=None)
        if item.last_modified and local_time and item.last_modified > local_time:
            return "remote"
        return "local" if local_time else "remote"

    def _push(self, client: CalDAVClient, sub, task, uid: str, kind: str, report: SyncReport,
              link: Optional[Dict[str, Any]], entry: Optional[RemoteEntry], create: bool = False) -> None:
        data = task.to_dict()
        columns = {c.id: c for c in self.service.get_columns_for_board(task.board_id)}
        col = columns.get(task.column_id)
        data["_column_type"] = col.column_type if col else ""
        item = item_from_task(data, uid, kind)
        item.in_progress = _in_progress_column(data)
        if kind == "VEVENT" and item.due is None:
            return
        # Write into the server's latest version, keeping what we don't manage
        base_text = entry.text if entry is not None else (link or {}).get("remote_data")
        cal, comp = None, None
        if base_text:
            try:
                cal = ical.parse_calendar(base_text)
                comp = next((c for c in cal.children if c.name == kind and c.get("RECURRENCE-ID") is None), None)
            except ICalError:
                cal = None
        if cal is None or comp is None:
            cal = ical.new_calendar()
            comp = Component(kind)
            cal.children.append(comp)
        fill_component(comp, item, data, event_style="timed")  # timed: reads back as the same due time
        seq = comp.text("SEQUENCE")
        comp.set("SEQUENCE", str(int(seq) + 1) if seq and seq.isdigit() else "0")
        text = ical.serialize(cal)
        href = entry.href if entry is not None else (link or {}).get("href") if link and not create else None
        if href is None:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", uid)
            href = f"{safe}.ics"
        etag = client.put(href, text, etag=entry.etag if entry is not None else None,
                          create=entry is None)
        report.counts["pushed"] += 1
        self.service.save_external_link(self.source(sub), uid, subscription_id=sub["id"], task_id=task.id,
                                        href=href, etag=etag, remote_data=text, remote_hash=item.hash(),
                                        local_hash=item.hash())


def check_connection(kind: str, url: str, username: Optional[str] = None, password: Optional[str] = None,
                    transport: Optional[Transport] = None) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """(ok, message, calendars found) for the subscription editor's "Test" button."""
    try:
        if kind == "caldav":
            found = CalDAVClient(url, username, password, transport).discover()
            if not found:
                return False, "Connected, but no calendars were found at this address", []
            if len(found) == 1:
                c = found[0]
                return True, f"Found the calendar '{c['name']}' ({', '.join(c['components'])})", found
            return True, f"Found {len(found)} calendars - pick one", found
        text = fetch_text(url, username, password, transport)
        parsed = parse_items(text)
        name = f" '{parsed.name}'" if parsed.name else ""
        return True, f"Read the calendar{name}: {len(parsed.items)} upcoming item{'s' if len(parsed.items) != 1 else ''}", []
    except (SyncError, ICalError) as exc:
        return False, str(exc), []

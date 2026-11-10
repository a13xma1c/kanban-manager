"""
Board & Task Templates (pure Python, no Qt)

**Task templates** are reusable task presets stored per board (or for every
board). Their data::

    {"title": "Bug: ", "description": "## Steps to reproduce ...", "priority": "HIGH",
     "labels": ["Bug"], "checklist": ["Reproduce", "Fix", "Verify"], "estimated_hours": 2,
     "assignee": "@me", "due_in_days": 3, "due_time": "17:00", "start_in_days": null,
     "working_days": true, "fields": {"Severity": "Major"}, "column": "To Do", "is_milestone": false}

Titles, descriptions and checklist items may use placeholders, filled in when
the template is used: {date} {today} {weekday} {week} {month} {year} {user} {board}.
Labels and fields are referenced by *name*, so a template works on any board
(missing labels are created; values for fields the board doesn't have are
skipped with a warning).

**Board templates** are blueprints for new boards: columns (with WIP limits
and types), labels, custom fields, task templates, automation rules and
policies, schedule settings and optionally sample tasks. Built-in ones are
defined below; boards can be saved as templates and templates can be
exported to / imported from ``.kbtemplate.json`` files to share them.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kanbanmanager.config import DEFAULT_COLUMNS, DEFAULT_LABEL_PALETTE
from kanbanmanager.core import custom_fields as cf
from kanbanmanager.core.custom_fields import FieldValueError
from kanbanmanager.enums import ActivityAction, ColumnType, Priority

BUILTIN_PREFIX = "builtin:"
TEMPLATE_FORMAT = "kanbanmanager.board-template"
TEMPLATE_VERSION = 1
TEMPLATE_FILE_SUFFIX = ".kbtemplate.json"
PLACEHOLDERS = "{date} {today} {weekday} {week} {month} {year} {user} {board}"
LABEL_PALETTE = ["#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6", "#EC4899", "#06B6D4", "#64748B"]
_PRIORITIES = {p.value for p in Priority}
_COLUMN_TYPES = {c.value for c in ColumnType}


class TemplateError(ValueError):
    """A template (file) that can't be used; the message is shown to the user."""


# ---------------------------------------------------------------------------
# Placeholders & dates
# ---------------------------------------------------------------------------
def placeholder_values(now: datetime, user: str, board: str) -> Dict[str, str]:
    return {
        "date": now.strftime("%Y-%m-%d"),
        "today": f"{now:%b} {now.day}",
        "weekday": now.strftime("%A"),
        "week": str(now.isocalendar()[1]),
        "month": now.strftime("%B"),
        "year": str(now.year),
        "user": user or "",
        "board": board or "",
    }


def expand_placeholders(text: str, values: Dict[str, str]) -> str:
    """Replace known {placeholders}; anything else in braces is left alone."""
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), text or "")


def add_days(start: date, days: int, working_days: bool = True) -> date:
    """``start`` + ``days`` (working days skip Saturday/Sunday; day 0 on a weekend = next Monday)."""
    if not working_days:
        return start + timedelta(days=days)
    d = start
    while d.weekday() >= 5:
        d += timedelta(days=1)
    step = 1 if days >= 0 else -1
    remaining = abs(days)
    while remaining:
        d += timedelta(days=step)
        if d.weekday() < 5:
            remaining -= 1
    return d


# ---------------------------------------------------------------------------
# Task templates
# ---------------------------------------------------------------------------
def _opt_int(value: Any, lo: int = -3650, hi: int = 3650) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return None


def normalize_task_template(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    d = data or {}
    labels: List[str] = []
    for name in d.get("labels") or []:
        name = " ".join(str(name).split())
        if name and name.lower() not in (x.lower() for x in labels):
            labels.append(name)
    checklist = []
    for item in d.get("checklist") or []:
        text = item.get("text") if isinstance(item, dict) else item
        text = " ".join(str(text or "").split())
        if text:
            checklist.append(text)
    due_time = str(d.get("due_time") or "17:00")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", due_time):
        due_time = "17:00"
    try:
        hours = max(0.0, min(9999.0, float(d.get("estimated_hours") or 0)))
    except (TypeError, ValueError):
        hours = 0.0
    fields = {}
    for name, value in (d.get("fields") or {}).items():
        name = " ".join(str(name).split())
        if name and value not in (None, "", []):
            fields[name] = value
    column = " ".join(str(d.get("column") or "").split()) or None
    return {
        "title": str(d.get("title") or "")[:255],
        "description": str(d.get("description") or ""),
        "priority": d.get("priority") if d.get("priority") in _PRIORITIES else None,
        "labels": labels,
        "checklist": checklist,
        "estimated_hours": hours,
        "assignee": " ".join(str(d.get("assignee") or "").split()),
        "due_in_days": _opt_int(d.get("due_in_days")),
        "due_time": due_time,
        "start_in_days": _opt_int(d.get("start_in_days")),
        "working_days": bool(d.get("working_days", True)),
        "fields": fields,
        "column": column,
        "is_milestone": bool(d.get("is_milestone")),
    }


def _strip_attachment_refs(markdown: str) -> str:
    """Attachments don't travel with templates: drop inline references to them."""
    from kanbanmanager.core.markdown_utils import attachment_refs, remove_attachment_refs

    for ref in attachment_refs(markdown or ""):
        markdown = remove_attachment_refs(markdown, ref)
    return markdown or ""


def template_from_task(service, task_id: int) -> Dict[str, Any]:
    """Template data with a task's content (no dates, attachments or history)."""
    task = service.get_task(task_id)
    if task is None:
        raise TemplateError("Task not found")
    fields = {f["name"]: task.field_value_map[f["id"]] for f in service.get_custom_fields(task.board_id)
              if f["id"] in task.field_value_map}
    return normalize_task_template({
        "title": task.title,
        "description": _strip_attachment_refs(task.description or ""),
        "priority": task.priority,
        "labels": [lbl.name for lbl in task.labels],
        "checklist": [item.get("text", "") for item in task.checklist],
        "estimated_hours": task.estimated_hours or 0,
        "assignee": task.assignee or "",
        "fields": fields,
        "is_milestone": bool(task.is_milestone),
    })


def describe_task_template(data: Dict[str, Any]) -> str:
    """One line summarising what a template sets ("High · Bug · 3 checklist items · due in 3 days")."""
    d = normalize_task_template(data)
    parts = []
    if d["priority"]:
        parts.append(Priority(d["priority"]).display_name)
    if d["labels"]:
        parts.append(", ".join(d["labels"]))
    if d["checklist"]:
        parts.append(f"{len(d['checklist'])} checklist item{'s' if len(d['checklist']) != 1 else ''}")
    if d["fields"]:
        parts.append(f"{len(d['fields'])} field value{'s' if len(d['fields']) != 1 else ''}")
    if d["estimated_hours"]:
        parts.append(f"{d['estimated_hours']:g}h")
    if d["assignee"]:
        parts.append("assigned to you" if d["assignee"].lower() in ("@me", "me") else f"→ {d['assignee']}")
    if d["due_in_days"] is not None:
        n = d["due_in_days"]
        unit = "working day" if d["working_days"] else "day"
        parts.append("due today" if n == 0 else f"due in {n} {unit}{'s' if abs(n) != 1 else ''}")
    return " · ".join(parts) or "Empty template"


def expand_task_template(service, data: Dict[str, Any], board_id: int, now: Optional[datetime] = None,
                         user: Optional[str] = None, create_labels: bool = True) -> Tuple[Dict[str, Any], List[str]]:
    """Keyword arguments for ``create_task`` (minus board/column) from template data, and warnings.

    With ``create_labels`` False, labels missing on the board are returned by name in
    ``new_labels`` instead of being created (used to preview a template in the editor)."""
    d = normalize_task_template(data)
    now = now or datetime.now()
    user = user or service.actor
    board = service.get_board(board_id)
    values = placeholder_values(now, user, board.name if board else "")
    warnings: List[str] = []

    label_ids, new_labels = [], []
    existing = {lbl.name.lower(): lbl for lbl in (board.labels if board else [])}
    for i, name in enumerate(d["labels"]):
        lbl = existing.get(name.lower())
        if lbl is None and create_labels:
            lbl = service.get_label_by_name(board_id, name, create_color=LABEL_PALETTE[i % len(LABEL_PALETTE)])
        if lbl is not None:
            label_ids.append(lbl.id)
        else:
            new_labels.append(name)

    fields = {f["name"].lower(): f for f in service.get_custom_fields(board_id)}
    field_values: Dict[int, Any] = {}
    for name, value in d["fields"].items():
        definition = fields.get(name.lower())
        if definition is None:
            warnings.append(f"This board has no field '{name}' (value skipped)")
            continue
        if isinstance(value, str):
            value = expand_placeholders(value, values)
        try:
            field_values[definition["id"]] = cf.validate_value(definition, value)
        except FieldValueError as exc:
            warnings.append(str(exc))

    due = start = None
    if d["due_in_days"] is not None:
        hh, mm = (int(x) for x in d["due_time"].split(":"))
        due = datetime.combine(add_days(now.date(), d["due_in_days"], d["working_days"]), time(hh, mm))
    if d["start_in_days"] is not None:
        start = add_days(now.date(), d["start_in_days"], d["working_days"])
    if d["is_milestone"] and due is not None:
        start = due.date()
    if start is not None and due is not None and start > due.date():
        start = due.date()

    assignee = d["assignee"]
    if assignee.lower() in ("@me", "me"):
        assignee = user
    kwargs = {
        "title": expand_placeholders(d["title"], values).strip() or "Untitled task",
        "description": expand_placeholders(d["description"], values),
        "priority": d["priority"] or Priority.MEDIUM.value,
        "estimated_hours": d["estimated_hours"],
        "start_date": start,
        "due_date": due,
        "label_ids": label_ids,
        "checklist": [{"id": i + 1, "text": expand_placeholders(t, values), "done": False}
                      for i, t in enumerate(d["checklist"])],
        "assignee": assignee or None,
        "is_milestone": d["is_milestone"],
        "field_values": field_values,
    }
    if not create_labels:
        kwargs["new_labels"] = new_labels
        kwargs["raw_title"] = expand_placeholders(d["title"], values).lstrip()  # "Bug: " keeps its space
    return kwargs, warnings


def resolve_column(service, board_id: int, column_name: Optional[str], column_id: Optional[int] = None) -> Optional[int]:
    columns = service.get_columns_for_board(board_id)
    if column_id and any(c.id == column_id for c in columns):
        return column_id
    if column_name:
        match = next((c for c in columns if c.name.lower() == column_name.lower()), None)
        if match is not None:
            return match.id
    return service.default_capture_column(board_id)


def create_task_from_template(service, template: Dict[str, Any], board_id: int, column_id: Optional[int] = None,
                              title: Optional[str] = None, now: Optional[datetime] = None):
    """Create (and return) a task from a template dict ({"name", "data"} or bare data).
    Warnings (skipped field values) are attached as ``task.template_warnings``."""
    data = template.get("data", template) if isinstance(template, dict) else {}
    kwargs, warnings = expand_task_template(service, data, board_id, now=now)
    if title:
        kwargs["title"] = title
    column = resolve_column(service, board_id, normalize_task_template(data)["column"], column_id)
    if column is None:
        raise TemplateError("The board has no columns")
    name = template.get("name") if isinstance(template, dict) and "data" in template else None
    task = service.create_task(board_id=board_id, column_id=column,
                               origin=f"from template '{name}'" if name else "from a template", **kwargs)
    task.template_warnings = warnings
    return task


# ---------------------------------------------------------------------------
# Board templates
# ---------------------------------------------------------------------------
def _column(name: str, color: str, ctype: str = "CUSTOM", wip: int = 0, done: bool = False) -> Dict[str, Any]:
    return {"name": name, "color": color, "column_type": ctype, "wip_limit": wip, "is_done_column": done}


def _field(name: str, ftype: str, config: Optional[Dict[str, Any]] = None, required: bool = False,
           show: bool = True, default: Any = None, description: str = "") -> Dict[str, Any]:
    return {"name": name, "field_type": ftype, "config": config or {}, "required": required,
            "show_on_card": show, "default": default, "description": description}


def _opts(*labels_colors) -> Dict[str, Any]:
    return {"options": [{"label": lc[0], "color": lc[1]} if isinstance(lc, tuple) else {"label": lc}
                        for lc in labels_colors]}


def normalize_board_template(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validated template data (raises TemplateError for something unusable)."""
    d = data or {}
    if d.get("format") not in (None, TEMPLATE_FORMAT):
        raise TemplateError("This is not a KanbanManager board template")
    columns = []
    for c in d.get("columns") or []:
        name = " ".join(str(c.get("name") or "").split())[:100]
        if not name or name.lower() in (x["name"].lower() for x in columns):
            continue
        ctype = c.get("column_type") or c.get("type") or "CUSTOM"
        columns.append({
            "name": name, "color": c.get("color") or "#64748B",
            "column_type": ctype if ctype in _COLUMN_TYPES else "CUSTOM",
            "wip_limit": max(0, int(c.get("wip_limit") or 0)), "is_done_column": bool(c.get("is_done_column")),
        })
    if not columns:
        raise TemplateError("A board template needs at least one column")
    labels = []
    for lbl in d.get("labels") or []:
        name = " ".join(str(lbl.get("name") or "").split())[:60]
        if name and name.lower() not in (x["name"].lower() for x in labels):
            labels.append({"name": name, "color": lbl.get("color") or "#3B82F6"})
    fields = []
    for f in d.get("fields") or []:
        try:
            name = cf.normalize_name(f.get("name"))
        except FieldValueError:
            continue
        if name.lower() in (x["name"].lower() for x in fields):
            continue
        ftype = f.get("field_type") or "text"
        fields.append({"name": name, "field_type": ftype, "config": f.get("config") or {},
                       "required": bool(f.get("required")), "show_on_card": bool(f.get("show_on_card", True)),
                       "default": f.get("default"), "description": str(f.get("description") or "")})
    task_templates = []
    for t in d.get("task_templates") or []:
        name = " ".join(str(t.get("name") or "").split())[:120]
        if name:
            task_templates.append({"name": name, "icon": str(t.get("icon") or "📋")[:16],
                                   "data": normalize_task_template(t.get("data"))})
    tasks = []
    for t in d.get("tasks") or []:
        norm = normalize_task_template(t)
        if norm["title"].strip():
            tasks.append(norm)
    schedule = {k: v for k, v in (d.get("schedule_settings") or {}).items() if k not in ("baseline_id", "project_start")}
    return {
        "format": TEMPLATE_FORMAT,
        "version": TEMPLATE_VERSION,
        "columns": columns,
        "labels": labels,
        "fields": fields,
        "task_templates": task_templates,
        "automation_rules": [r for r in d.get("automation_rules") or [] if isinstance(r, dict)],
        "policies": d.get("policies") or {},
        "schedule_settings": schedule,
        "tasks": tasks,
    }


def describe_board_template(data: Dict[str, Any]) -> Dict[str, Any]:
    """Counts for previews: columns, labels, fields, task templates, rules, sample tasks."""
    return {
        "columns": [c["name"] for c in data.get("columns", [])],
        "labels": [l["name"] for l in data.get("labels", [])],
        "fields": [(f["name"], cf.get_field_type(f["field_type"]).label) for f in data.get("fields", [])],
        "task_templates": [t["name"] for t in data.get("task_templates", [])],
        "rules": [r.get("name", "") for r in data.get("automation_rules", [])],
        "tasks": len(data.get("tasks", [])),
    }


# The standard template is exactly what a plain "new board" gets
_STANDARD_LABELS = [{"name": n, "color": c} for n, c in DEFAULT_LABEL_PALETTE]
_STANDARD_COLUMNS = [_column(c["name"], c["color"], c["type"], c.get("wip_limit", 0), c.get("is_done_column", False))
                     for c in DEFAULT_COLUMNS]

_BUG_REPORT = """## Steps to reproduce
1. Go to …
2. Click …

## Expected
…

## Actual
…

## Environment / logs
…
"""

_BUILTINS: List[Dict[str, Any]] = [
    {
        "key": "standard", "name": "Standard Kanban", "icon": "📋", "color": "#3B82F6", "category": "Basics",
        "description": "Backlog → To Do → In Progress → In Review → Done, with WIP limits and the usual labels. "
                       "The classic starting point.",
        "data": {
            "columns": _STANDARD_COLUMNS,
            "labels": _STANDARD_LABELS,
        },
    },
    {
        "key": "simple", "name": "Simple To-Do", "icon": "✅", "color": "#10B981", "category": "Basics",
        "description": "Three columns and nothing else - for a quick list you don't want to configure.",
        "data": {
            "columns": [_column("To Do", "#3B82F6", "TODO"), _column("Doing", "#F59E0B", "IN_PROGRESS"),
                        _column("Done", "#10B981", "DONE", done=True)],
            "labels": [],
        },
    },
    {
        "key": "sprint", "name": "Software Sprint", "icon": "🏃", "color": "#6366F1", "category": "Software",
        "description": "Scrum-style sprint board: story points with column totals, components, pull request "
                       "links, and templates for bugs, user stories and spikes. Moving a card to Code Review "
                       "asks for a review.",
        "data": {
            "columns": [_column("Backlog", "#64748B", "BACKLOG"), _column("Sprint", "#3B82F6", "TODO"),
                        _column("In Progress", "#F59E0B", "IN_PROGRESS", 3),
                        _column("Code Review", "#8B5CF6", "REVIEW", 2), _column("Done", "#10B981", "DONE", done=True)],
            "labels": [{"name": "Bug", "color": "#EF4444"}, {"name": "Feature", "color": "#3B82F6"},
                       {"name": "Tech debt", "color": "#F59E0B"}, {"name": "Spike", "color": "#8B5CF6"},
                       {"name": "Documentation", "color": "#06B6D4"}],
            "fields": [
                _field("Story points", "number", {"unit": "pts", "min": 0, "max": 100, "show_total": True},
                       description="Relative effort (Fibonacci: 1, 2, 3, 5, 8, 13)"),
                _field("Component", "select", _opts(("Frontend", "#3B82F6"), ("Backend", "#10B981"),
                                                    ("API", "#8B5CF6"), ("Infrastructure", "#F59E0B"),
                                                    ("Docs", "#06B6D4"))),
                _field("Pull request", "url", show=True, description="Link to the code review"),
                _field("Acceptance reviewed", "checkbox", show=False),
            ],
            "task_templates": [
                {"name": "Bug report", "icon": "🐞", "data": {
                    "title": "Bug: ", "description": _BUG_REPORT, "priority": "HIGH", "labels": ["Bug"],
                    "checklist": ["Reproduce", "Write a failing test", "Fix", "Verify on staging"],
                    "fields": {"Story points": 2}}},
                {"name": "User story", "icon": "📖", "data": {
                    "title": "As a … I want … so that …", "labels": ["Feature"],
                    "description": "## Acceptance criteria\n- [ ] …\n- [ ] …\n\n## Notes\n",
                    "checklist": ["Acceptance criteria agreed", "Implemented", "Tested", "Documented"],
                    "fields": {"Story points": 3}}},
                {"name": "Spike", "icon": "🔬", "data": {
                    "title": "Spike: ", "labels": ["Spike"], "estimated_hours": 4, "due_in_days": 3,
                    "description": "**Question:** \n\n**Time box:** 4 hours\n\n**Findings:**\n",
                    "fields": {"Story points": 1}}},
            ],
            "automation_rules": [
                {"name": "Ask for review in Code Review", "is_enabled": True,
                 "trigger": {"type": "task_moved", "to_column_id": "Code Review"}, "conditions": [],
                 "actions": [{"type": "add_comment", "text": "👀 {actor} moved this to {column} - ready for review."}]},
                {"name": "Bugs start at high priority", "is_enabled": True,
                 "trigger": {"type": "label_added", "label_id": "Bug"}, "conditions": [],
                 "actions": [{"type": "set_priority", "priority": "HIGH"}]},
            ],
            "tasks": [
                {"title": "Set up the sprint board", "column": "Sprint", "priority": "LOW",
                 "description": "Estimate the backlog in **story points** - the column headers add them up.",
                 "fields": {"Story points": 1, "Component": "Docs"}, "labels": ["Documentation"]},
                {"title": "Login page remembers the last user", "column": "Backlog", "labels": ["Feature"],
                 "fields": {"Story points": 5, "Component": "Frontend"}},
            ],
        },
    },
    {
        "key": "bugs", "name": "Bug Tracker", "icon": "🐞", "color": "#EF4444", "category": "Software",
        "description": "Triage → Confirmed → Fixing → Verifying → Closed. Severity (required), environments, "
                       "reproducibility and reporter fields; critical bugs are escalated to Urgent automatically.",
        "data": {
            "columns": [_column("Triage", "#64748B", "BACKLOG"), _column("Confirmed", "#3B82F6", "TODO"),
                        _column("Fixing", "#F59E0B", "IN_PROGRESS", 4), _column("Verifying", "#8B5CF6", "REVIEW"),
                        _column("Closed", "#10B981", "DONE", done=True)],
            "labels": [{"name": "Regression", "color": "#EF4444"}, {"name": "Crash", "color": "#DC2626"},
                       {"name": "UI", "color": "#EC4899"}, {"name": "Performance", "color": "#F59E0B"},
                       {"name": "Security", "color": "#7C3AED"}],
            "fields": [
                _field("Severity", "select", _opts(("Critical", "#DC2626"), ("Major", "#F97316"),
                                                   ("Minor", "#EAB308"), ("Trivial", "#64748B")), required=True),
                _field("Environment", "multi_select", _opts("Windows", "macOS", "Linux", "Web", "iOS", "Android")),
                _field("Reproducible", "checkbox"),
                _field("Reported by", "person", show=False),
                _field("Version", "text", show=False, description="Where it was found, e.g. 2.4.1"),
                _field("Ticket", "url", show=False),
            ],
            "task_templates": [
                {"name": "New bug", "icon": "🐞", "data": {
                    "title": "", "description": _BUG_REPORT, "priority": "MEDIUM", "column": "Triage",
                    "fields": {"Reported by": "{user}"}}},
                {"name": "Crash report", "icon": "💥", "data": {
                    "title": "Crash: ", "description": _BUG_REPORT, "priority": "HIGH", "labels": ["Crash"],
                    "column": "Triage", "fields": {"Severity": "Critical", "Reported by": "{user}"}}},
            ],
            "automation_rules": [
                {"name": "Critical bugs are urgent", "is_enabled": True,
                 "trigger": {"type": "field_changed", "field_id": "Severity", "to_value": "Critical"},
                 "conditions": [],
                 "actions": [{"type": "set_priority", "priority": "URGENT"},
                             {"type": "notify", "text": "🔥 Critical bug: '{title}'"}]},
                {"name": "Verify fixes", "is_enabled": True,
                 "trigger": {"type": "task_moved", "to_column_id": "Verifying"}, "conditions": [],
                 "actions": [{"type": "add_checklist", "items": "Verified on the reported environment\n"
                                                                "Regression test added"}]},
            ],
            "tasks": [
                {"title": "Crash: app closes when pasting a large image", "column": "Confirmed",
                 "priority": "URGENT", "labels": ["Crash"],
                 "fields": {"Severity": "Critical", "Environment": ["Windows"], "Reproducible": True,
                            "Version": "2.4.1"}},
                {"title": "Tooltip is cut off in dark mode", "column": "Triage", "priority": "LOW", "labels": ["UI"],
                 "fields": {"Severity": "Trivial", "Environment": ["macOS", "Linux"]}},
            ],
        },
    },
    {
        "key": "content", "name": "Content Calendar", "icon": "📰", "color": "#EC4899", "category": "Marketing",
        "description": "Plan, write and publish content: channels, publish dates, authors and word counts, "
                       "with templates for blog posts and the monthly newsletter.",
        "data": {
            "columns": [_column("Ideas", "#64748B", "BACKLOG"), _column("Drafting", "#F59E0B", "IN_PROGRESS", 4),
                        _column("Editing", "#8B5CF6", "REVIEW", 3), _column("Scheduled", "#3B82F6", "TODO"),
                        _column("Published", "#10B981", "DONE", done=True)],
            "labels": [{"name": "Evergreen", "color": "#10B981"}, {"name": "Announcement", "color": "#F59E0B"},
                       {"name": "Guest post", "color": "#8B5CF6"}],
            "fields": [
                _field("Channel", "multi_select", _opts(("Blog", "#3B82F6"), ("Newsletter", "#10B981"),
                                                        ("LinkedIn", "#0EA5E9"), ("YouTube", "#EF4444"),
                                                        ("Podcast", "#8B5CF6"))),
                _field("Publish date", "date"),
                _field("Author", "person"),
                _field("Word count", "number", {"unit": "words", "show_total": True}, show=False),
                _field("Published URL", "url", show=False),
            ],
            "task_templates": [
                {"name": "Blog post", "icon": "✍️", "data": {
                    "title": "Blog: ", "column": "Ideas", "due_in_days": 10,
                    "checklist": ["Outline", "First draft", "Images", "SEO review", "Schedule"],
                    "fields": {"Channel": ["Blog"], "Author": "{user}"}}},
                {"name": "Monthly newsletter", "icon": "📨", "data": {
                    "title": "Newsletter - {month} {year}", "column": "Drafting", "due_in_days": 5,
                    "checklist": ["Pick the top 3 stories", "Write the intro", "Test send", "Schedule"],
                    "fields": {"Channel": ["Newsletter"], "Author": "{user}"}}},
            ],
            "tasks": [
                {"title": "10 tips for faster onboarding", "column": "Drafting", "labels": ["Evergreen"],
                 "fields": {"Channel": ["Blog", "LinkedIn"], "Word count": 1200}, "due_in_days": 4},
            ],
        },
    },
    {
        "key": "hiring", "name": "Hiring Pipeline", "icon": "🧑‍💼", "color": "#0EA5E9", "category": "People",
        "description": "Track candidates from application to offer: role, source, rating, contact details and "
                       "salary expectations. Moving someone to Offer adds the offer checklist.",
        "data": {
            "columns": [_column("Applied", "#64748B", "BACKLOG"), _column("Phone screen", "#3B82F6", "TODO"),
                        _column("Interviews", "#F59E0B", "IN_PROGRESS", 6), _column("Offer", "#8B5CF6", "REVIEW"),
                        _column("Hired", "#10B981", "DONE", done=True)],
            "labels": [{"name": "Referral", "color": "#10B981"}, {"name": "Senior", "color": "#8B5CF6"},
                       {"name": "Remote", "color": "#06B6D4"}],
            "fields": [
                _field("Role", "select", _opts("Engineer", "Designer", "Product Manager", "Support"), required=True),
                _field("Rating", "rating", {"max": 5}),
                _field("Email", "email", show=False),
                _field("Source", "select", _opts("LinkedIn", "Referral", "Careers page", "Agency"), show=False),
                _field("Salary expectation", "currency", {"symbol": "$", "decimals": 0}, show=False),
                _field("Resume", "url", show=False),
                _field("Next interview", "date"),
            ],
            "task_templates": [
                {"name": "Candidate", "icon": "👤", "data": {
                    "title": "Candidate: ", "column": "Applied",
                    "checklist": ["Review resume", "Phone screen", "Technical interview", "Team interview",
                                  "References"]}},
            ],
            "automation_rules": [
                {"name": "Offer checklist", "is_enabled": True,
                 "trigger": {"type": "task_moved", "to_column_id": "Offer"}, "conditions": [],
                 "actions": [{"type": "add_checklist",
                              "items": "Approve the salary band\nSend the offer letter\nAgree on a start date"}]},
            ],
        },
    },
    {
        "key": "gtd", "name": "Personal (GTD)", "icon": "🧘", "color": "#84CC16", "category": "Personal",
        "description": "Getting-Things-Done lists: Inbox, Next, Doing, Waiting and Done, with context, energy "
                       "and time needed - plus a weekly review template.",
        "data": {
            "columns": [_column("Inbox", "#64748B", "BACKLOG"), _column("Next", "#3B82F6", "TODO"),
                        _column("Doing", "#F59E0B", "IN_PROGRESS", 2), _column("Waiting", "#8B5CF6", "CUSTOM"),
                        _column("Done", "#10B981", "DONE", done=True)],
            "labels": [{"name": "Home", "color": "#10B981"}, {"name": "Work", "color": "#3B82F6"},
                       {"name": "Errands", "color": "#F59E0B"}],
            "fields": [
                _field("Context", "select", _opts("@home", "@work", "@phone", "@computer", "@errands")),
                _field("Energy", "select", _opts(("High", "#EF4444"), ("Medium", "#F59E0B"), ("Low", "#10B981")),
                       show=False),
                _field("Minutes", "number", {"unit": "min", "show_total": True}),
                _field("Waiting for", "person", show=True),
            ],
            "task_templates": [
                {"name": "Weekly review", "icon": "🔁", "data": {
                    "title": "Weekly review - week {week}", "column": "Next", "due_in_days": 0,
                    "assignee": "@me", "fields": {"Minutes": 45, "Context": "@computer"},
                    "checklist": ["Empty the inbox", "Review the calendar", "Review Waiting", "Plan next week"]}},
            ],
        },
    },
]


def builtin_board_templates() -> List[Dict[str, Any]]:
    """Built-in templates in the same shape as saved ones (id "builtin:<key>", builtin True)."""
    out = []
    for t in _BUILTINS:
        out.append({
            "id": BUILTIN_PREFIX + t["key"], "name": t["name"], "description": t["description"], "icon": t["icon"],
            "color": t["color"], "category": t["category"], "builtin": True,
            "data": normalize_board_template(copy.deepcopy(t["data"])),
        })
    return out


def board_to_template(service, board_id: int, include_tasks: bool = False,
                      include_automation: bool = True) -> Dict[str, Any]:
    """A board's structure as template data."""
    from kanbanmanager.core.automation import portable_policies, portable_rule

    board = service.get_board(board_id)
    if board is None:
        raise TemplateError("Board not found")
    columns = {c.id: c.name for c in board.columns}
    labels = {l.id: l.name for l in board.labels}
    fields = service.get_custom_fields(board_id)
    field_names = {f["id"]: f["name"] for f in fields}
    templates = service.get_task_templates(board_id, include_global=False)
    template_names = {t["id"]: t["name"] for t in templates}
    data: Dict[str, Any] = {
        "columns": [{"name": c.name, "color": c.color, "column_type": c.column_type, "wip_limit": c.wip_limit,
                     "is_done_column": c.is_done_column} for c in board.columns],
        "labels": [{"name": l.name, "color": l.color} for l in sorted(board.labels, key=lambda l: l.name.lower())],
        "fields": [{k: f[k] for k in ("name", "field_type", "config", "required", "default", "show_on_card",
                                      "description")} for f in fields],
        "task_templates": [{"name": t["name"], "icon": t["icon"], "data": t["data"]} for t in templates],
        "schedule_settings": service.get_schedule_settings(board_id),
        "automation_rules": [],
        "policies": {},
        "tasks": [],
    }
    if include_automation:
        data["automation_rules"] = [portable_rule(r, columns, labels, field_names, template_names)
                                    for r in service.get_automation_rules(board_id)]
        data["policies"] = portable_policies(service.get_board_policies(board_id), columns)
    if include_tasks:
        for t in service.get_tasks_for_board(board_id):
            if t.is_completed:
                continue
            values = t.field_value_map
            data["tasks"].append({
                "title": t.title, "column": columns.get(t.column_id), "priority": t.priority,
                "description": _strip_attachment_refs(t.description or ""),
                "labels": [l.name for l in t.labels], "checklist": [i.get("text", "") for i in t.checklist],
                "estimated_hours": t.estimated_hours or 0, "assignee": t.assignee or "",
                "fields": {field_names[fid]: v for fid, v in values.items() if fid in field_names},
                "is_milestone": bool(t.is_milestone),
            })
    return normalize_board_template(data)


def instantiate_board_template(service, workspace_id: int, template: Dict[str, Any], name: Optional[str] = None,
                               color: Optional[str] = None, description: Optional[str] = None,
                               include_sample_tasks: bool = True, now: Optional[datetime] = None):
    """Create a board from a template dict ({"name", "data", ...} or bare data) and return it."""
    from kanbanmanager.core.automation import local_policies, local_rule

    data = normalize_board_template(template.get("data", template))
    tpl_name = template.get("name") if "data" in template else None
    board = service.create_board(
        workspace_id=workspace_id,
        name=(name or tpl_name or "New Board").strip(),
        description=description if description is not None else (template.get("description") or ""),
        color=color or template.get("color") or "#3B82F6",
        columns_def=[dict(c) for c in data["columns"]],
        labels_def=data["labels"],
    )
    problems: List[str] = []
    for f in data["fields"]:
        if not cf.is_known_type(f["field_type"]):
            problems.append(f"field '{f['name']}' has an unknown type ({f['field_type']})")
            continue
        try:
            service.create_custom_field(board.id, f["name"], f["field_type"], config=f["config"],
                                        required=f["required"], default=f["default"],
                                        show_on_card=f["show_on_card"], description=f["description"])
        except (FieldValueError, ValueError) as exc:
            problems.append(f"field '{f['name']}': {exc}")
    for t in data["task_templates"]:
        try:
            service.save_task_template(t["name"], t["data"], board_id=board.id, icon=t["icon"])
        except ValueError as exc:
            problems.append(f"task template '{t['name']}': {exc}")

    column_ids = {c.name: c.id for c in service.get_columns_for_board(board.id)}
    label_ids = {l.name: l.id for l in service.get_labels_for_board(board.id)}
    field_ids = {f["name"]: f["id"] for f in service.get_custom_fields(board.id)}
    template_ids = {t["name"]: t["id"] for t in service.get_task_templates(board.id, include_global=False)}
    for r in data["automation_rules"]:
        try:
            service.save_automation_rule(board.id, local_rule(r, column_ids, label_ids, field_ids, template_ids))
        except ValueError as exc:
            problems.append(f"rule '{r.get('name', '')}': {exc}")
    if data["policies"]:
        service.set_board_policies(board.id, local_policies(data["policies"], column_ids))
    if data["schedule_settings"]:
        service.set_schedule_settings(board.id, data["schedule_settings"])

    if include_sample_tasks:
        for t in data["tasks"]:
            try:
                create_task_from_template(service, {"name": tpl_name, "data": t} if tpl_name else t, board.id,
                                          now=now)
            except (FieldValueError, TemplateError, ValueError) as exc:
                problems.append(f"sample task '{t['title']}': {exc}")
    if tpl_name:
        with service.db.session_scope() as session:
            service._log(session, board.id, None, ActivityAction.BOARD_UPDATED,
                         f"Created from the '{tpl_name}' template" + (f" ({'; '.join(problems)})" if problems else ""),
                         field_name="template", new_value=tpl_name)
    created = service.get_board(board.id)
    created.template_problems = problems
    return created


# ---------------------------------------------------------------------------
# Template files
# ---------------------------------------------------------------------------
def export_template_file(template: Dict[str, Any], path: Path | str) -> Path:
    """Write a board template (dict from the service) to a shareable JSON file."""
    dest = Path(path)
    payload = {
        "format": TEMPLATE_FORMAT, "version": TEMPLATE_VERSION,
        "name": template.get("name") or "Board template", "description": template.get("description") or "",
        "icon": template.get("icon") or "📋", "color": template.get("color") or "#3B82F6",
        "category": template.get("category") or "My templates",
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "template": normalize_board_template(template.get("data", {})),
    }
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest


def read_template_file(path: Path | str) -> Dict[str, Any]:
    """{"name", "description", "icon", "color", "category", "data"} from a template file."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TemplateError(f"Could not read the template file: {exc}") from None
    if not isinstance(raw, dict) or raw.get("format") != TEMPLATE_FORMAT:
        raise TemplateError("This file is not a KanbanManager board template")
    if int(raw.get("version") or 1) > TEMPLATE_VERSION:
        raise TemplateError("This template was made by a newer version of KanbanManager")
    return {
        "name": " ".join(str(raw.get("name") or "Imported template").split())[:120],
        "description": str(raw.get("description") or ""),
        "icon": str(raw.get("icon") or "📋")[:16],
        "color": str(raw.get("color") or "#3B82F6"),
        "category": str(raw.get("category") or "Imported")[:60],
        "data": normalize_board_template(raw.get("template") or {}),
    }

"""
Workflow Automation Engine (pure Python, no Qt)

Rules - "When <trigger>, if <conditions>, then <actions>" - per board:

* **Event triggers** fire from DomainEvents the service publishes after every
  committed change (task created/moved/completed, priority/assignee/label
  changes, comments, logged time ...).
* **Scheduled triggers** (due soon, overdue, no activity for N days) are
  evaluated by ``run_scheduled()``, which the UI calls periodically. Each
  fires once per task and situation (e.g. once per due date).

Policies - per-board settings evaluated by the same scheduler:

* auto-archive tasks completed more than N days ago,
* flag stale tasks (no activity for N days): add a label, comment, move, notify,
* archive tasks that have been stale for M days.

Changes made by automation are attributed to "⚙ Automation: <rule>" in the
audit trail. They do not count as activity for staleness, so flagging a
task does not reset its stale clock. Rules may trigger other rules, but a
chain is limited in depth and each rule acts at most once per task per chain
(loop protection). Every execution is recorded in the run history.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from kanbanmanager.config import (
    AUTOMATION_ACTOR_PREFIX,
    AUTOMATION_MAX_CHAIN_DEPTH,
    AUTOMATION_RUN_RETENTION_DAYS,
)
from kanbanmanager.core import custom_fields as cf
from kanbanmanager.database import events as ev
from kanbanmanager.database.models import utc_now
from kanbanmanager.enums import Priority

log = logging.getLogger(__name__)

ACTOR_FOR_TRIGGER = "__actor__"  # assign action: "the person who triggered the rule"
PRIORITY_ORDER = [Priority.LOW.value, Priority.MEDIUM.value, Priority.HIGH.value, Priority.URGENT.value]


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Param:
    key: str
    kind: str                    # column | priority | label | person | int | text | multiline | field | template
    label: str
    optional: bool = False       # optional params mean "any" when left empty
    default: Any = None
    minimum: int = 0
    maximum: int = 9999
    suffix: str = ""


@dataclass(frozen=True)
class TriggerSpec:
    type: str
    label: str
    params: Tuple[Param, ...] = ()
    scheduled: bool = False
    events: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionSpec:
    type: str
    label: str
    params: Tuple[Param, ...] = ()


@dataclass(frozen=True)
class ConditionSpec:
    field: str
    label: str
    ops: Tuple[Tuple[str, str], ...]   # (op, label)
    value: Optional[Param] = None      # None: ops need no value
    valueless_ops: Tuple[str, ...] = ()
    subject: Optional[Param] = None    # chosen before the operator (e.g. which custom field)


TRIGGERS: Dict[str, TriggerSpec] = {t.type: t for t in [
    TriggerSpec("task_created", "A task is created",
                (Param("column_id", "column", "in column", optional=True),), events=(ev.TASK_CREATED,)),
    TriggerSpec("task_moved", "A task is moved",
                (Param("from_column_id", "column", "from", optional=True),
                 Param("to_column_id", "column", "to", optional=True)), events=(ev.TASK_MOVED,)),
    TriggerSpec("task_completed", "A task is completed", events=(ev.TASK_COMPLETED,)),
    TriggerSpec("task_reopened", "A task is reopened", events=(ev.TASK_REOPENED,)),
    TriggerSpec("priority_changed", "Priority changes",
                (Param("to_priority", "priority", "to", optional=True),), events=(ev.PRIORITY_CHANGED,)),
    TriggerSpec("assignee_changed", "The assignee changes",
                (Param("to_person", "person", "to", optional=True),), events=(ev.ASSIGNEE_CHANGED,)),
    TriggerSpec("label_added", "A label is added",
                (Param("label_id", "label", "named", optional=True),), events=(ev.LABELS_CHANGED,)),
    TriggerSpec("field_changed", "A custom field changes",
                (Param("field_id", "field", "field", optional=True),
                 Param("to_value", "text", "to", optional=True)), events=(ev.FIELD_CHANGED,)),
    TriggerSpec("comment_added", "A comment is posted",
                (Param("contains", "text", "containing", optional=True),), events=(ev.COMMENT_ADDED,)),
    TriggerSpec("time_logged", "Time is logged on a task", events=(ev.TIME_LOGGED,)),
    TriggerSpec("over_estimate", "Logged time exceeds the estimate", events=(ev.TIME_LOGGED,)),
    TriggerSpec("due_soon", "A due date is approaching",
                (Param("hours", "int", "within", default=24, minimum=1, maximum=24 * 30, suffix=" hours"),),
                scheduled=True),
    TriggerSpec("overdue", "A task becomes overdue", scheduled=True),
    TriggerSpec("stale", "A task has no activity",
                (Param("days", "int", "for", default=7, minimum=1, maximum=365, suffix=" days"),), scheduled=True),
]}

CONDITIONS: Dict[str, ConditionSpec] = {c.field: c for c in [
    ConditionSpec("priority", "Priority", (("is", "is"), ("is_not", "is not"), ("at_least", "is at least"),
                                           ("at_most", "is at most")), Param("value", "priority", "")),
    ConditionSpec("column", "Column", (("is", "is"), ("is_not", "is not")), Param("value", "column", "")),
    ConditionSpec("label", "Labels", (("has", "include"), ("lacks", "do not include")), Param("value", "label", "")),
    ConditionSpec("assignee", "Assignee", (("is", "is"), ("is_not", "is not"), ("is_set", "is set"),
                                           ("is_empty", "is empty")), Param("value", "person", ""),
                  valueless_ops=("is_set", "is_empty")),
    ConditionSpec("title", "Title", (("contains", "contains"), ("not_contains", "does not contain")),
                  Param("value", "text", "")),
    ConditionSpec("due_date", "Due date", (("is_set", "is set"), ("is_empty", "is not set")),
                  valueless_ops=("is_set", "is_empty")),
    ConditionSpec("blocked", "Blocked by a dependency", (("is_true", "yes"), ("is_false", "no")),
                  valueless_ops=("is_true", "is_false")),
    ConditionSpec("completed", "Completed", (("is_true", "yes"), ("is_false", "no")),
                  valueless_ops=("is_true", "is_false")),
    ConditionSpec("custom_field", "Custom field", cf.COMPARE_OPS, Param("value", "text", "value"),
                  valueless_ops=cf.VALUELESS_OPS, subject=Param("field_id", "field", "field")),
]}

ACTIONS: Dict[str, ActionSpec] = {a.type: a for a in [
    ActionSpec("move_to_column", "Move to column", (Param("column_id", "column", "column"),)),
    ActionSpec("set_priority", "Set priority", (Param("priority", "priority", "priority"),)),
    ActionSpec("add_label", "Add label", (Param("label_id", "label", "label"),)),
    ActionSpec("remove_label", "Remove label", (Param("label_id", "label", "label"),)),
    ActionSpec("assign", "Assign to", (Param("person", "person", "person"),)),
    ActionSpec("unassign", "Unassign"),
    ActionSpec("set_due_date", "Set due date",
               (Param("days", "int", "in", default=3, minimum=0, maximum=365, suffix=" days"),)),
    ActionSpec("add_comment", "Post a comment", (Param("text", "multiline", "comment"),)),
    ActionSpec("add_checklist", "Add checklist items", (Param("items", "multiline", "one item per line"),)),
    ActionSpec("mark_complete", "Mark complete"),
    ActionSpec("reopen", "Reopen"),
    ActionSpec("archive", "Archive the task"),
    ActionSpec("notify", "Show a notification", (Param("text", "text", "message"),)),
    ActionSpec("set_field", "Set a custom field",
               (Param("field_id", "field", "field"), Param("value", "text", "value (empty clears it)", optional=True))),
    ActionSpec("create_from_template", "Create a task from a template",
               (Param("template_id", "template", "template"), Param("column_id", "column", "in column", optional=True))),
]}

PLACEHOLDERS = "{title} {assignee} {column} {priority} {due} {board} {actor} {days}"

DEFAULT_POLICIES: Dict[str, Dict[str, Any]] = {
    "auto_archive": {"enabled": False, "days": 14},
    "stale": {"enabled": False, "days": 14, "label": "Stale", "comment": True, "notify": True,
              "move_to_column_id": None, "exclude_column_ids": []},
    "archive_stale": {"enabled": False, "days": 60},
}

POLICY_NAMES = {
    "auto_archive": "Auto-archive completed tasks",
    "stale": "Flag stale tasks",
    "archive_stale": "Archive stale tasks",
}

STALE_LABEL_COLOR = "#F97316"


def normalize_policies(raw: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Defaults merged with stored values (unknown keys dropped, numbers clamped)."""
    merged: Dict[str, Dict[str, Any]] = {}
    for name, defaults in DEFAULT_POLICIES.items():
        value = dict(defaults)
        stored = (raw or {}).get(name) or {}
        value.update({k: v for k, v in stored.items() if k in defaults})
        value["enabled"] = bool(value["enabled"])
        value["days"] = max(1, min(3650, int(value.get("days") or defaults["days"])))
        merged[name] = value
    return merged


# ---------------------------------------------------------------------------
# Board context (ids -> names) and task snapshots
# ---------------------------------------------------------------------------
@dataclass
class BoardContext:
    board_id: int
    name: str
    columns: Dict[int, Dict[str, Any]]         # id -> {"name", "column_type", "is_done_column", "position"}
    labels: Dict[int, str]                     # id -> name
    fields: Dict[int, Dict[str, Any]] = field(default_factory=dict)   # custom field id -> definition
    templates: Dict[int, str] = field(default_factory=dict)           # task template id -> name

    def column_name(self, column_id: Any) -> str:
        col = self.columns.get(_as_int(column_id))
        return col["name"] if col else f"(deleted column #{column_id})"

    def label_name(self, label_id: Any) -> str:
        return self.labels.get(_as_int(label_id), f"(deleted label #{label_id})")

    def field_name(self, field_id: Any) -> str:
        f = self.fields.get(_as_int(field_id))
        return f["name"] if f else f"(deleted field #{field_id})"

    def template_name(self, template_id: Any) -> str:
        return self.templates.get(_as_int(template_id), f"(deleted template #{template_id})")


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class TaskView:
    id: int
    board_id: int
    title: str
    priority: str
    column_id: int
    assignee: str
    label_ids: List[int]
    label_names: List[str]
    due_date: Optional[datetime]
    is_completed: bool
    completed_at: Optional[datetime]
    is_blocked: bool
    is_archived: bool
    estimated_hours: float
    spent_hours: float
    checklist: List[Dict[str, Any]]
    field_values: Dict[int, Any] = field(default_factory=dict)


@dataclass
class RunSummary:
    archived: int = 0
    flagged_stale: int = 0
    rules_fired: int = 0
    failures: int = 0
    messages: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.archived or self.flagged_stale or self.rules_fired)

    def describe(self) -> str:
        parts = []
        if self.rules_fired:
            parts.append(f"{self.rules_fired} rule run{'s' if self.rules_fired != 1 else ''}")
        if self.flagged_stale:
            parts.append(f"flagged {self.flagged_stale} stale task{'s' if self.flagged_stale != 1 else ''}")
        if self.archived:
            parts.append(f"archived {self.archived} task{'s' if self.archived != 1 else ''}")
        if self.failures:
            parts.append(f"{self.failures} failed (see Automations → History)")
        return ", ".join(parts)


class ActionError(Exception):
    pass


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def _safe_format(template: str, values: Dict[str, Any]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
                  template or "")


RULE_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "Escalate overdue tasks",
        "trigger": {"type": "overdue"},
        "conditions": [],
        "actions": [
            {"type": "set_priority", "priority": Priority.URGENT.value},
            {"type": "add_comment", "text": "⏰ '{title}' is overdue (was due {due}). Escalated to Urgent."},
            {"type": "notify", "text": "'{title}' is overdue"},
        ],
    },
    {
        "name": "Remind me a day before the due date",
        "trigger": {"type": "due_soon", "hours": 24},
        "conditions": [],
        "actions": [{"type": "notify", "text": "'{title}' is due {due}"}],
    },
    {
        "name": "Triage new bugs",
        "trigger": {"type": "label_added", "label_name": "Bug"},
        "conditions": [],
        "actions": [
            {"type": "set_priority", "priority": Priority.HIGH.value},
            {"type": "add_checklist", "items": "Reproduce the bug\nFind the root cause\nAdd a regression test"},
        ],
    },
    {
        "name": "Start work when time is logged",
        "trigger": {"type": "time_logged"},
        "conditions": [{"field": "column", "op": "is", "value_column_type": "TODO"}],
        "actions": [{"type": "move_to_column", "column_type": "IN_PROGRESS"}],
    },
    {
        "name": "Ask for review when moved to review",
        "trigger": {"type": "task_moved", "to_column_type": "REVIEW"},
        "conditions": [],
        "actions": [{"type": "add_comment", "text": "👀 {actor} moved this to {column} - ready for review."}],
    },
    {
        "name": "Flag time overruns",
        "trigger": {"type": "over_estimate"},
        "conditions": [],
        "actions": [
            {"type": "add_label", "label_name": "Over estimate", "label_color": "#EF4444"},
            {"type": "add_comment", "text": "⚠ Logged time has exceeded the estimate - please re-estimate."},
        ],
    },
    {
        "name": "Unassign completed tasks",
        "trigger": {"type": "task_completed"},
        "conditions": [{"field": "assignee", "op": "is_set"}],
        "actions": [{"type": "add_comment", "text": "✅ Completed by {actor}."}, {"type": "unassign"}],
    },
]


def instantiate_template(template: Dict[str, Any], service, board_id: int) -> Dict[str, Any]:
    """Turn a template into concrete rule data for a board: column types -> column ids,
    label names -> label ids (creating missing labels)."""
    columns = service.get_columns_for_board(board_id)

    def column_for(col_type: str) -> Optional[int]:
        match = next((c for c in columns if c.column_type == col_type), None)
        return match.id if match else None

    def label_for(name: str, color: Optional[str]) -> Optional[int]:
        lbl = service.get_label_by_name(board_id, name, create_color=color or "#64748B")
        return lbl.id if lbl else None

    def resolve(item: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for key, value in item.items():
            if key.endswith("column_type"):
                out[key.replace("column_type", "column_id") if key != "value_column_type" else "value"] = \
                    column_for(value)
            elif key == "label_name":
                out["label_id"] = label_for(value, item.get("label_color"))
            elif key != "label_color":
                out[key] = value
        return out

    return {
        "name": template["name"],
        "is_enabled": True,
        "trigger": resolve(template["trigger"]),
        "conditions": [resolve(c) for c in template.get("conditions", [])],
        "actions": [resolve(a) for a in template.get("actions", [])],
    }


# ---------------------------------------------------------------------------
# Describing & validating rules
# ---------------------------------------------------------------------------
def _param_text(param: Param, value: Any, ctx: BoardContext) -> str:
    if value in (None, ""):
        return ""
    if param.kind == "column":
        return f"'{ctx.column_name(value)}'"
    if param.kind == "label":
        return f"'{ctx.label_name(value)}'"
    if param.kind == "priority":
        try:
            return Priority(value).display_name
        except ValueError:
            return str(value)
    if param.kind == "person":
        return "the person who triggered it" if value == ACTOR_FOR_TRIGGER else str(value)
    if param.kind == "int":
        return f"{value}{param.suffix}"
    if param.kind == "field":
        return f"'{ctx.field_name(value)}'"
    if param.kind == "template":
        return f"'{ctx.template_name(value)}'"
    text = " ".join(str(value).split())
    return f"\"{text[:40]}{'…' if len(text) > 40 else ''}\""


def describe_trigger(trigger: Dict[str, Any], ctx: BoardContext) -> str:
    spec = TRIGGERS.get(trigger.get("type", ""))
    if spec is None:
        return f"Unknown trigger '{trigger.get('type')}'"
    parts = [spec.label]
    for p in spec.params:
        text = _param_text(p, trigger.get(p.key), ctx)
        if text:
            parts.append(f"{p.label} {text}")
    return " ".join(parts)


def describe_condition(cond: Dict[str, Any], ctx: BoardContext) -> str:
    spec = CONDITIONS.get(cond.get("field", ""))
    if spec is None:
        return f"unknown condition '{cond.get('field')}'"
    op_label = dict(spec.ops).get(cond.get("op"), cond.get("op", ""))
    subject = f"{spec.label.lower()} {_param_text(spec.subject, cond.get(spec.subject.key), ctx)}" \
        if spec.subject is not None else spec.label.lower()
    if cond.get("op") in spec.valueless_ops or spec.value is None:
        return f"{subject} {op_label}"
    return f"{subject} {op_label} {_param_text(spec.value, cond.get('value'), ctx)}"


def describe_action(action: Dict[str, Any], ctx: BoardContext) -> str:
    spec = ACTIONS.get(action.get("type", ""))
    if spec is None:
        return f"unknown action '{action.get('type')}'"
    texts = [_param_text(p, action.get(p.key), ctx) for p in spec.params]
    return " ".join([spec.label.lower()] + [t for t in texts if t])


def describe_rule(rule: Dict[str, Any], ctx: BoardContext) -> str:
    trigger_text = describe_trigger(rule.get("trigger") or {}, ctx)
    text = f"When {trigger_text[:1].lower()}{trigger_text[1:]}"  # keep column/label names' case
    conds = rule.get("conditions") or []
    if conds:
        text += ", if " + " and ".join(describe_condition(c, ctx) for c in conds)
    acts = rule.get("actions") or []
    text += " → " + ", ".join(describe_action(a, ctx) for a in acts) if acts else " → (no actions)"
    return text


def validate_rule(rule: Dict[str, Any], ctx: BoardContext) -> List[str]:
    """Human-readable problems (missing required values, references to deleted columns/labels)."""
    problems: List[str] = []

    def check(param: Param, value: Any, where: str) -> None:
        if value in (None, ""):
            if not param.optional:
                problems.append(f"{where}: '{param.label}' is required")
            return
        if param.kind == "column" and _as_int(value) not in ctx.columns:
            problems.append(f"{where}: the column no longer exists")
        if param.kind == "label" and _as_int(value) not in ctx.labels:
            problems.append(f"{where}: the label no longer exists")
        if param.kind == "field" and _as_int(value) not in ctx.fields:
            problems.append(f"{where}: the custom field no longer exists")
        if param.kind == "template" and _as_int(value) not in ctx.templates:
            problems.append(f"{where}: the task template no longer exists")

    trigger = rule.get("trigger") or {}
    t_spec = TRIGGERS.get(trigger.get("type", ""))
    if t_spec is None:
        problems.append("Unknown trigger")
    else:
        for p in t_spec.params:
            check(p, trigger.get(p.key, p.default), "Trigger")
    for i, cond in enumerate(rule.get("conditions") or [], 1):
        c_spec = CONDITIONS.get(cond.get("field", ""))
        if c_spec is None or cond.get("op") not in dict(c_spec.ops):
            problems.append(f"Condition {i}: invalid")
            continue
        if c_spec.subject is not None:
            check(c_spec.subject, cond.get(c_spec.subject.key), f"Condition {i}")
        if c_spec.value is not None and cond.get("op") not in c_spec.valueless_ops:
            check(c_spec.value, cond.get("value"), f"Condition {i}")
    if not rule.get("actions"):
        problems.append("No actions")
    for i, action in enumerate(rule.get("actions") or [], 1):
        a_spec = ACTIONS.get(action.get("type", ""))
        if a_spec is None:
            problems.append(f"Action {i}: unknown")
            continue
        for p in a_spec.params:
            check(p, action.get(p.key, p.default), f"Action {i} ({a_spec.label.lower()})")
    return problems


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class AutomationEngine:
    def __init__(
        self,
        service,
        notify: Optional[Callable[[str], None]] = None,
        now: Callable[[], datetime] = utc_now,
        local_now: Callable[[], datetime] = datetime.now,
    ):
        self.service = service
        self.notify = notify or (lambda message: None)
        self._now = now              # naive UTC, for audit timestamps
        self._local_now = local_now  # naive local time, for due dates / completed_at
        self._depth = 0
        self._fired: set = set()

    # Wiring ---------------------------------------------------------------
    def attach(self) -> None:
        self.service.add_event_listener(self.handle_event)

    def detach(self) -> None:
        self.service.remove_event_listener(self.handle_event)

    # Context helpers -----------------------------------------------------
    def board_context(self, board_id: int) -> Optional[BoardContext]:
        board = self.service.get_board(board_id)
        if board is None:
            return None
        return BoardContext(
            board_id=board.id,
            name=board.name,
            columns={c.id: {"name": c.name, "column_type": c.column_type, "is_done_column": c.is_done_column,
                            "position": c.position} for c in board.columns},
            labels={l.id: l.name for l in board.labels},
            fields={f["id"]: f for f in self.service.get_custom_fields(board.id)},
            templates={t["id"]: t["name"] for t in self.service.get_task_templates(board.id)},
        )

    def task_view(self, task_id: int) -> Optional[TaskView]:
        t = self.service.get_task(task_id)
        if t is None:
            return None
        return TaskView(
            id=t.id, board_id=t.board_id, title=t.title, priority=t.priority, column_id=t.column_id,
            assignee=(t.assignee or ""), label_ids=[l.id for l in t.labels], label_names=[l.name for l in t.labels],
            due_date=t.due_date, is_completed=bool(t.is_completed), completed_at=t.completed_at,
            is_blocked=t.is_blocked, is_archived=bool(t.is_archived), estimated_hours=float(t.estimated_hours or 0),
            spent_hours=float(t.spent_hours or 0), checklist=list(t.checklist),
            field_values=t.field_value_map,
        )

    # Matching -------------------------------------------------------------
    @staticmethod
    def trigger_matches(trigger: Dict[str, Any], event: ev.DomainEvent, task: TaskView, ctx: BoardContext) -> bool:
        t = trigger.get("type")
        spec = TRIGGERS.get(t or "")
        if spec is None or spec.scheduled or event.type not in spec.events:
            return False
        d = event.data
        if t == "task_created":
            return not trigger.get("column_id") or _as_int(trigger["column_id"]) == _as_int(d.get("column_id"))
        if t == "task_moved":
            if trigger.get("from_column_id") and _as_int(trigger["from_column_id"]) != _as_int(d.get("from_column_id")):
                return False
            if trigger.get("to_column_id") and _as_int(trigger["to_column_id"]) != _as_int(d.get("to_column_id")):
                return False
            return True
        if t == "priority_changed":
            return not trigger.get("to_priority") or trigger["to_priority"] == d.get("to")
        if t == "assignee_changed":
            return not trigger.get("to_person") or trigger["to_person"] == (d.get("to") or "")
        if t == "label_added":
            added = d.get("added") or []
            if not added:
                return False
            return not trigger.get("label_id") or ctx.label_name(trigger["label_id"]) in added
        if t == "comment_added":
            needle = (trigger.get("contains") or "").strip().lower()
            return not needle or needle in (d.get("new") or "").lower()
        if t == "over_estimate":
            return task.estimated_hours > 0 and task.spent_hours > task.estimated_hours
        if t == "field_changed":
            if trigger.get("field_id") and _as_int(trigger["field_id"]) != _as_int(d.get("field_id")):
                return False
            wanted = (trigger.get("to_value") or "").strip()
            if not wanted:
                return True
            definition = ctx.fields.get(_as_int(d.get("field_id")))
            if definition is None:
                return False
            return cf.type_of(definition).compare(d.get("to"), "is", wanted, definition.get("config") or {})
        return True  # completed / reopened / time_logged

    @staticmethod
    def conditions_match(conditions: Sequence[Dict[str, Any]], task: TaskView, ctx: BoardContext) -> bool:
        for c in conditions or []:
            field_name, op, value = c.get("field"), c.get("op"), c.get("value")
            if field_name == "priority":
                rank = PRIORITY_ORDER.index(task.priority) if task.priority in PRIORITY_ORDER else 1
                target = PRIORITY_ORDER.index(value) if value in PRIORITY_ORDER else 1
                ok = {"is": task.priority == value, "is_not": task.priority != value,
                      "at_least": rank >= target, "at_most": rank <= target}.get(op, False)
            elif field_name == "column":
                ok = (task.column_id == _as_int(value)) if op == "is" else (task.column_id != _as_int(value))
            elif field_name == "label":
                has = _as_int(value) in task.label_ids
                ok = has if op == "has" else not has
            elif field_name == "assignee":
                ok = {"is": task.assignee == (value or ""), "is_not": task.assignee != (value or ""),
                      "is_set": bool(task.assignee), "is_empty": not task.assignee}.get(op, False)
            elif field_name == "title":
                contains = (value or "").lower() in task.title.lower()
                ok = contains if op == "contains" else not contains
            elif field_name == "due_date":
                ok = (task.due_date is not None) == (op == "is_set")
            elif field_name == "blocked":
                ok = task.is_blocked == (op == "is_true")
            elif field_name == "completed":
                ok = task.is_completed == (op == "is_true")
            elif field_name == "custom_field":
                definition = ctx.fields.get(_as_int(c.get("field_id")))
                ok = definition is not None and cf.type_of(definition).compare(
                    task.field_values.get(definition["id"]), op, str(value or ""), definition.get("config") or {})
            else:
                ok = False
            if not ok:
                return False
        return True

    # Events -------------------------------------------------------------------
    def handle_event(self, event: ev.DomainEvent) -> None:
        if event.task_id is None or event.type in (ev.TASK_DELETED, ev.TASK_ARCHIVED):
            return
        rules = [r for r in self.service.get_automation_rules(event.board_id, enabled_only=True)
                 if event.type in TRIGGERS.get((r.get("trigger") or {}).get("type", ""), TriggerSpec("", "")).events]
        if not rules:
            return
        ctx = self.board_context(event.board_id)
        if ctx is None:
            return
        top_level = self._depth == 0
        if top_level:
            self._fired = set()
        self._depth += 1
        try:
            for rule in rules:
                task = self.task_view(event.task_id)
                if task is None or task.is_archived:
                    return
                if not self.trigger_matches(rule["trigger"], event, task, ctx):
                    continue
                if not self.conditions_match(rule.get("conditions"), task, ctx):
                    continue
                key = (rule["id"], task.id)
                trigger_type = rule["trigger"]["type"]
                if key in self._fired or self._depth > AUTOMATION_MAX_CHAIN_DEPTH:
                    self.service.record_automation_run(
                        ctx.board_id, "rule", rule["name"], trigger_type, "skipped",
                        "Skipped to prevent an automation loop (rules triggering each other).",
                        rule_id=rule["id"], task_id=task.id, task_title=task.title)
                    continue
                if trigger_type == "over_estimate":
                    dedupe = f"over_estimate:{task.id}"
                    if self.service.has_automation_run("rule", dedupe, rule["id"]):
                        continue
                else:
                    dedupe = None
                self._fired.add(key)
                self._run_rule(rule, task, ctx, event.actor, trigger_type, dedupe)
        finally:
            self._depth -= 1
            if top_level:
                self._fired = set()

    # Scheduler ----------------------------------------------------------------
    def run_scheduled(self) -> RunSummary:
        """Evaluate time-based rules and policies on every board. Safe to call often."""
        summary = RunSummary()
        boards = [b for ws in self.service.get_workspaces() for b in ws.boards if not b.is_archived]
        for board in boards:
            try:
                self._run_board_schedule(board.id, summary)
            except Exception:
                log.exception("Scheduled automation failed for board %s", board.id)
                summary.failures += 1
        self.service.prune_automation_runs(self._now() - timedelta(days=AUTOMATION_RUN_RETENTION_DAYS))
        return summary

    def _run_board_schedule(self, board_id: int, summary: RunSummary) -> None:
        ctx = self.board_context(board_id)
        if ctx is None:
            return
        rules = [r for r in self.service.get_automation_rules(board_id, enabled_only=True)
                 if TRIGGERS.get(r["trigger"].get("type", ""), TriggerSpec("", "")).scheduled]
        policies = normalize_policies(self.service.get_board_policies(board_id))
        if not rules and not any(p["enabled"] for p in policies.values()):
            return
        now, local_now = self._now(), self._local_now()
        last_activity = self.service.get_last_activity_times(board_id, exclude_actor_prefix=AUTOMATION_ACTOR_PREFIX)
        task_ids = [t.id for t in self.service.get_tasks_for_board(board_id)]

        for rule in rules:
            trigger = rule["trigger"]
            t_type = trigger["type"]
            for task_id in task_ids:
                task = self.task_view(task_id)
                if task is None or task.is_archived or task.is_completed:
                    continue
                key = None
                if t_type == "due_soon" and task.due_date is not None:
                    hours = int(trigger.get("hours") or 24)
                    if local_now <= task.due_date <= local_now + timedelta(hours=hours):
                        key = f"due_soon:{task.id}:{task.due_date.isoformat()}"
                elif t_type == "overdue" and task.due_date is not None and task.due_date < local_now:
                    key = f"overdue:{task.id}:{task.due_date.isoformat()}"
                elif t_type == "stale":
                    seen = last_activity.get(task.id)
                    if seen is not None and seen <= now - timedelta(days=int(trigger.get("days") or 7)):
                        key = f"stale:{task.id}:{seen.isoformat()}"
                if key is None or not self.conditions_match(rule.get("conditions"), task, ctx):
                    continue
                if self.service.has_automation_run("rule", key, rule["id"]):
                    continue  # already handled this situation
                status = self._run_rule(rule, task, ctx, None, t_type, key)
                summary.rules_fired += 1
                summary.failures += status == "failed"

        self._apply_policies(ctx, policies, task_ids, last_activity, summary)

    # Policies ------------------------------------------------------------------
    def _apply_policies(self, ctx: BoardContext, policies: Dict[str, Dict[str, Any]], task_ids: List[int],
                        last_activity: Dict[int, datetime], summary: RunSummary) -> None:
        now, local_now = self._now(), self._local_now()
        auto, stale, archive_stale = policies["auto_archive"], policies["stale"], policies["archive_stale"]
        stale_hits: List[str] = []
        for task_id in task_ids:
            task = self.task_view(task_id)
            if task is None or task.is_archived:
                continue
            seen = last_activity.get(task.id)
            idle_days = (now - seen).days if seen else 0

            if auto["enabled"] and task.is_completed and task.completed_at is not None \
                    and task.completed_at <= local_now - timedelta(days=auto["days"]):
                reason = f"completed more than {auto['days']} days ago"
                self._policy_archive(ctx, "auto_archive", task, reason, summary)
                continue
            if task.is_completed or seen is None:
                continue
            if archive_stale["enabled"] and seen <= now - timedelta(days=archive_stale["days"]):
                reason = f"no activity for {idle_days} days"
                self._policy_archive(ctx, "archive_stale", task, reason, summary)
                continue
            if stale["enabled"] and seen <= now - timedelta(days=stale["days"]) \
                    and task.column_id not in {_as_int(c) for c in stale.get("exclude_column_ids") or []}:
                key = f"stale:{task.id}:{seen.isoformat()}"
                if self.service.has_automation_run("stale", key):
                    continue
                self._flag_stale(ctx, stale, task, idle_days, key, summary)
                stale_hits.append(task.title)
        if stale_hits and stale.get("notify"):
            shown = ", ".join(f"'{t}'" for t in stale_hits[:3]) + (" ..." if len(stale_hits) > 3 else "")
            self.notify(f"{len(stale_hits)} stale task{'s' if len(stale_hits) != 1 else ''} on {ctx.name}: {shown}")

    def _policy_archive(self, ctx: BoardContext, policy: str, task: TaskView, reason: str,
                        summary: RunSummary) -> None:
        with self.service.acting_as(f"{AUTOMATION_ACTOR_PREFIX}: {POLICY_NAMES[policy]}"):
            ok = self.service.archive_task(task.id, reason=f"Auto-archived: {reason}")
        if ok:
            summary.archived += 1
            self.service.record_automation_run(ctx.board_id, policy, POLICY_NAMES[policy], "schedule", "success",
                                               f"Archived ({reason})", task_id=task.id, task_title=task.title)

    def _flag_stale(self, ctx: BoardContext, cfg: Dict[str, Any], task: TaskView, idle_days: int, key: str,
                    summary: RunSummary) -> None:
        done: List[str] = []
        errors: List[str] = []
        with self.service.acting_as(f"{AUTOMATION_ACTOR_PREFIX}: {POLICY_NAMES['stale']}"):
            label_name = (cfg.get("label") or "").strip()
            if label_name:
                lbl = self.service.get_label_by_name(ctx.board_id, label_name, create_color=STALE_LABEL_COLOR)
                current = self.task_view(task.id)
                if lbl is not None and lbl.id not in current.label_ids:
                    self.service.update_task(task.id, label_ids=current.label_ids + [lbl.id])
                    done.append(f"added label '{lbl.name}'")
            if cfg.get("comment"):
                self.service.add_comment(
                    task.id, f"🕸 No activity for {idle_days} days. Is this still needed? "
                             "Update it, or it may be archived automatically.")
                done.append("posted a comment")
            target = _as_int(cfg.get("move_to_column_id"))
            if target and target != task.column_id:
                if target in ctx.columns:
                    ok, err = self.service.move_task(task.id, target, 10 ** 6)
                    (done if ok else errors).append(f"moved to '{ctx.column_name(target)}'" if ok else str(err))
                else:
                    errors.append("the 'move to' column no longer exists")
        summary.flagged_stale += 1
        status = "failed" if errors and not done else ("partial" if errors else "success")
        summary.failures += status == "failed"
        message = f"No activity for {idle_days} days: " + ", ".join(done or ["flagged"])
        if errors:
            message += "; failed: " + "; ".join(errors)
        self.service.record_automation_run(ctx.board_id, "stale", POLICY_NAMES["stale"], "schedule", status,
                                           message, task_id=task.id, task_title=task.title, dedupe_key=key)

    # Executing rules ---------------------------------------------------------
    def _run_rule(self, rule: Dict[str, Any], task: TaskView, ctx: BoardContext, trigger_actor: Optional[str],
                  trigger_type: str, dedupe_key: Optional[str]) -> str:
        done: List[str] = []
        errors: List[str] = []
        values = self._placeholders(task, ctx, trigger_actor, rule)
        with self.service.acting_as(f"{AUTOMATION_ACTOR_PREFIX}: {rule['name']}"):
            for action in rule.get("actions") or []:
                try:
                    done.append(self._execute(action, task.id, ctx, values, trigger_actor))
                except ActionError as exc:
                    errors.append(str(exc))
                except Exception as exc:  # never let one broken action take down the rest
                    log.exception("Automation action failed")
                    errors.append(f"{action.get('type')}: {exc}")
        status = "failed" if errors and not done else ("partial" if errors else "success")
        message = "; ".join(done) if done else ""
        if errors:
            message = (message + "; " if message else "") + "failed: " + "; ".join(errors)
        self.service.record_automation_run(ctx.board_id, "rule", rule["name"], trigger_type, status, message,
                                           rule_id=rule["id"], task_id=task.id, task_title=task.title,
                                           dedupe_key=dedupe_key)
        return status

    def _placeholders(self, task: TaskView, ctx: BoardContext, actor: Optional[str], rule: Dict[str, Any]):
        due = task.due_date.strftime("%b %d, %H:%M") if task.due_date else "(no due date)"
        try:
            priority = Priority(task.priority).display_name
        except ValueError:
            priority = task.priority
        return {
            "title": task.title, "assignee": task.assignee or "nobody", "column": ctx.column_name(task.column_id),
            "priority": priority, "due": due, "board": ctx.name, "actor": actor or "Automation",
            "days": (rule.get("trigger") or {}).get("days", ""),
            "rule": rule.get("name", ""),
        }

    def _execute(self, action: Dict[str, Any], task_id: int, ctx: BoardContext, values: Dict[str, Any],
                 trigger_actor: Optional[str]) -> str:
        s = self.service
        kind = action.get("type")
        task = self.task_view(task_id)
        if task is None:
            raise ActionError("the task no longer exists")
        if kind == "move_to_column":
            col = _as_int(action.get("column_id"))
            if col not in ctx.columns:
                raise ActionError("move: the column no longer exists")
            if col == task.column_id:
                return f"already in '{ctx.column_name(col)}'"
            ok, err = s.move_task(task_id, col, 10 ** 6)
            if not ok:
                raise ActionError(f"move: {err}")
            return f"moved to '{ctx.column_name(col)}'"
        if kind == "set_priority":
            value = action.get("priority")
            if value not in PRIORITY_ORDER:
                raise ActionError("set priority: invalid priority")
            s.update_task(task_id, priority=value)
            return f"set priority to {Priority(value).display_name}"
        if kind in ("add_label", "remove_label"):
            lbl = _as_int(action.get("label_id"))
            if lbl not in ctx.labels:
                raise ActionError(f"{kind.replace('_', ' ')}: the label no longer exists")
            ids = list(task.label_ids)
            if kind == "add_label" and lbl not in ids:
                ids.append(lbl)
            elif kind == "remove_label" and lbl in ids:
                ids.remove(lbl)
            else:
                return f"label '{ctx.label_name(lbl)}' already {'present' if kind == 'add_label' else 'absent'}"
            s.update_task(task_id, label_ids=ids)
            verb = "added" if kind == "add_label" else "removed"
            return f"{verb} label '{ctx.label_name(lbl)}'"
        if kind == "assign":
            person = action.get("person")
            if person == ACTOR_FOR_TRIGGER:
                if not trigger_actor or trigger_actor.startswith(AUTOMATION_ACTOR_PREFIX):
                    raise ActionError("assign: nobody to assign (the trigger was not a person)")
                person = trigger_actor
            if not person:
                raise ActionError("assign: no person chosen")
            s.update_task(task_id, assignee=person)
            return f"assigned to {person}"
        if kind == "unassign":
            s.update_task(task_id, assignee=None)
            return "unassigned"
        if kind == "set_due_date":
            days = int(action.get("days") or 0)
            due = (self._local_now() + timedelta(days=days)).replace(hour=17, minute=0, second=0, microsecond=0)
            s.update_task(task_id, due_date=due)
            return f"due date set to {due:%b %d}"
        if kind == "add_comment":
            body = _safe_format(action.get("text") or "", values).strip()
            if not body:
                raise ActionError("comment: empty text")
            s.add_comment(task_id, body)
            return "posted a comment"
        if kind == "add_checklist":
            items = [line.strip() for line in (action.get("items") or "").splitlines() if line.strip()]
            existing = {i.get("text") for i in task.checklist}
            new = [i for i in items if i not in existing]
            if not new:
                return "checklist items already present"
            next_id = max([int(i.get("id") or 0) for i in task.checklist] + [0]) + 1
            checklist = task.checklist + [{"id": next_id + n, "text": text, "done": False} for n, text in enumerate(new)]
            s.update_task(task_id, checklist=checklist)
            return f"added {len(new)} checklist item{'s' if len(new) != 1 else ''}"
        if kind == "mark_complete":
            if task.is_completed:
                return "already complete"
            s.update_task(task_id, is_completed=True, completed_at=self._local_now())
            return "marked complete"
        if kind == "reopen":
            if not task.is_completed:
                return "already open"
            s.update_task(task_id, is_completed=False, completed_at=None)
            return "reopened"
        if kind == "archive":
            s.archive_task(task_id, reason=f"rule '{values['rule']}'")
            return "archived"
        if kind == "notify":
            message = _safe_format(action.get("text") or "{title}", values)
            self.notify(message)
            return "notified"
        if kind == "set_field":
            definition = ctx.fields.get(_as_int(action.get("field_id")))
            if definition is None:
                raise ActionError("set field: the custom field no longer exists")
            ftype = cf.type_of(definition)
            text = _safe_format(str(action.get("value") or ""), values).strip()
            try:
                new_value = ftype.parse(text, definition.get("config") or {})
            except cf.FieldValueError as exc:
                raise ActionError(f"set field '{definition['name']}': {exc}") from None
            if task.field_values.get(definition["id"]) == new_value:
                return f"'{definition['name']}' already {'empty' if new_value is None else 'set'}"
            s.update_task(task_id, field_values={definition["id"]: new_value})
            if new_value is None:
                return f"cleared '{definition['name']}'"
            return f"set '{definition['name']}' to {cf.format_value(definition, new_value)}"
        if kind == "create_from_template":
            template_id = _as_int(action.get("template_id"))
            template = s.get_task_template(template_id) if template_id in ctx.templates else None
            if template is None:
                raise ActionError("create task: the template no longer exists")
            column = _as_int(action.get("column_id"))
            if column is not None and column not in ctx.columns:
                raise ActionError("create task: the column no longer exists")
            created = s.create_task_from_template(template, ctx.board_id, column_id=column)
            return f"created '{created.title}' from template '{template['name']}'"
        raise ActionError(f"unknown action '{kind}'")


# ---------------------------------------------------------------------------
# Portability (JSON export/import): ids are database-specific, names are not
# ---------------------------------------------------------------------------
_COLUMN_KEYS = ("column_id", "from_column_id", "to_column_id")


def _map_refs(rule: Dict[str, Any], column: Callable[[Any], Any], label: Callable[[Any], Any],
              field_ref: Optional[Callable[[Any], Any]] = None,
              template_ref: Optional[Callable[[Any], Any]] = None) -> Dict[str, Any]:
    def item(d: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(d)
        for k in _COLUMN_KEYS:
            if out.get(k) not in (None, ""):
                out[k] = column(out[k])
        if out.get("label_id") not in (None, ""):
            out["label_id"] = label(out["label_id"])
        if field_ref is not None and out.get("field_id") not in (None, ""):
            out["field_id"] = field_ref(out["field_id"])
        if template_ref is not None and out.get("template_id") not in (None, ""):
            out["template_id"] = template_ref(out["template_id"])
        if out.get("field") == "column" and out.get("value") not in (None, ""):
            out["value"] = column(out["value"])
        if out.get("field") == "label" and out.get("value") not in (None, ""):
            out["value"] = label(out["value"])
        return out

    return {
        **{k: v for k, v in rule.items() if k in ("name", "is_enabled")},
        "trigger": item(rule.get("trigger") or {}),
        "conditions": [item(c) for c in rule.get("conditions") or []],
        "actions": [item(a) for a in rule.get("actions") or []],
    }


def portable_rule(rule: Dict[str, Any], columns: Dict[int, str], labels: Dict[int, str],
                  fields: Optional[Dict[int, str]] = None, templates: Optional[Dict[int, str]] = None) -> Dict[str, Any]:
    """Rule with column/label (and custom field / task template) ids replaced by names."""
    return _map_refs(rule, lambda v: columns.get(_as_int(v)), lambda v: labels.get(_as_int(v)),
                     (lambda v: fields.get(_as_int(v))) if fields is not None else None,
                     (lambda v: templates.get(_as_int(v))) if templates is not None else None)


def local_rule(rule: Dict[str, Any], column_ids: Dict[str, int], label_ids: Dict[str, int],
               field_ids: Optional[Dict[str, int]] = None,
               template_ids: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """The reverse of ``portable_rule`` for a board (names missing there become empty references)."""
    return _map_refs(rule, lambda v: column_ids.get(v), lambda v: label_ids.get(v),
                     (lambda v: field_ids.get(v)) if field_ids is not None else None,
                     (lambda v: template_ids.get(v)) if template_ids is not None else None)


def portable_policies(policies: Dict[str, Any], columns: Dict[int, str]) -> Dict[str, Any]:
    p = normalize_policies(policies)
    stale = p["stale"]
    stale["move_to_column_id"] = columns.get(_as_int(stale.get("move_to_column_id")))
    stale["exclude_column_ids"] = [columns[c] for c in map(_as_int, stale.get("exclude_column_ids") or [])
                                   if c in columns]
    return p


def local_policies(policies: Dict[str, Any], column_ids: Dict[str, int]) -> Dict[str, Any]:
    p = normalize_policies(policies)
    stale = p["stale"]
    stale["move_to_column_id"] = column_ids.get(stale.get("move_to_column_id"))
    stale["exclude_column_ids"] = [column_ids[n] for n in stale.get("exclude_column_ids") or [] if n in column_ids]
    return p

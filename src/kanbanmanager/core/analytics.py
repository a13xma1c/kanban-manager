"""
Board analytics (pure Python, no Qt): the numbers behind the charts.

Task history is reconstructed from the audit trail (creations, moves,
completions, archiving), so charts cover the past as well as the present:

* ``cumulative_flow`` - tasks per column at the end of each day (CFD)
* ``burnup``          - total scope vs. completed work per day (task count or
                        the sum of a number field such as story points)
* ``throughput``      - tasks completed per week
* ``cycle_times``     - days from starting work to done (and lead time from
                        creation), with average / 85th percentile
* ``breakdown``       - open tasks (or a number field's sum) by column,
                        priority, label, assignee or a dropdown field

Audit timestamps are UTC; everything here is in local dates.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kanbanmanager.enums import ActivityAction as A, ColumnType, Priority

OTHER = "Other"
PALETTE = ["#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6", "#EC4899", "#06B6D4", "#84CC16", "#F97316",
           "#64748B"]
PERIODS = {"Last 14 days": 14, "Last 30 days": 30, "Last 90 days": 90, "Last 6 months": 182, "Last year": 365}


def _local(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def days_between(start: date, end: date) -> List[date]:
    n = (end - start).days
    return [start + timedelta(days=i) for i in range(max(0, n) + 1)]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
@dataclass
class TaskStory:
    id: int
    title: str
    created: datetime                                   # local
    moves: List[Tuple[datetime, str]] = field(default_factory=list)   # (local time, column name), first = start
    completed_at: Optional[datetime] = None              # local (current state)
    archived_at: Optional[datetime] = None               # local, if archived now
    is_completed: bool = False
    values: Dict[int, Any] = field(default_factory=dict)

    def column_at(self, moment: datetime) -> Optional[str]:
        current = None
        for when, col in self.moves:
            if when <= moment:
                current = col
            else:
                break
        return current

    def exists_at(self, moment: datetime) -> bool:
        if self.created > moment:
            return False
        return self.archived_at is None or self.archived_at > moment


@dataclass
class BoardData:
    board_id: int
    name: str
    columns: List[Dict[str, Any]]                        # {"id", "name", "color", "type", "done", "wip"}
    stories: List[TaskStory]
    tasks: List[Any]                                     # current (unarchived) Task objects
    fields: List[Dict[str, Any]]

    @property
    def done_names(self) -> set:
        return {c["name"] for c in self.columns if c["done"]}


def load_board(service, board_id: int) -> BoardData:
    board = service.get_board(board_id)
    if board is None:
        raise ValueError("Board not found")
    columns = [{"id": c.id, "name": c.name, "color": c.color or "#64748B", "type": c.column_type,
                "done": bool(c.is_done_column), "wip": int(c.wip_limit or 0)} for c in board.columns]
    col_names = {c["id"]: c["name"] for c in columns}
    all_tasks = service.get_tasks_for_board(board_id, include_archived=True)
    history = defaultdict(list)
    for h in service.get_flow_history(board_id):
        history[h["task_id"]].append(h)
    stories = []
    for t in all_tasks:
        created = _local(t.created_at) or datetime.now()
        moves: List[Tuple[datetime, str]] = []
        archived_at = None
        for h in history.get(t.id, []):
            when = _local(h["at"])
            if h["action"] == A.TASK_CREATED.value and h["field"] == "column" and h["new"]:
                created = min(created, when)
                moves.insert(0, (when, h["new"])) if not moves else moves.append((when, h["new"]))
            elif h["action"] == A.TASK_MOVED.value and h["field"] == "column" and h["new"]:
                if not moves:  # arrived from another board (or history starts with a move)
                    moves.append((when, h["new"]))
                    created = min(created, when) if h["old"] and " / " in (h["old"] or "") else created
                else:
                    moves.append((when, h["new"]))
            elif h["action"] == A.TASK_ARCHIVED.value:
                archived_at = when
            elif h["action"] == A.TASK_RESTORED.value:
                archived_at = None
        if not moves:
            moves = [(created, col_names.get(t.column_id, OTHER))]
        elif moves[0][0] > created:
            # the history starts later than the task: it was in the first column recorded
            moves.insert(0, (created, moves[0][1]))
        moves.sort(key=lambda m: m[0])
        if t.is_archived and archived_at is None:
            archived_at = _local(t.archived_at) if t.archived_at else datetime.now()
        if not t.is_archived:
            archived_at = None
        stories.append(TaskStory(
            id=t.id, title=t.title, created=created, moves=moves,
            completed_at=t.completed_at if t.is_completed else None, archived_at=archived_at,
            is_completed=bool(t.is_completed), values=t.field_value_map))
    return BoardData(board.id, board.name, columns, stories, [t for t in all_tasks if not t.is_archived],
                     service.get_custom_fields(board_id))


def default_range(days: int, today: Optional[date] = None) -> Tuple[date, date]:
    today = today or date.today()
    return today - timedelta(days=days - 1), today


# ---------------------------------------------------------------------------
# Cumulative flow
# ---------------------------------------------------------------------------
@dataclass
class FlowSeries:
    days: List[date]
    series: List[Tuple[str, str, List[int]]]    # (column, color, counts) - first = bottom band (done)


def cumulative_flow(data: BoardData, start: date, end: date) -> FlowSeries:
    """Tasks per column at the end of each day. Bands are ordered done-first (bottom) so the
    chart reads like a classic CFD; columns that no longer exist are grouped as "Other"."""
    days = days_between(start, end)
    known = {c["name"] for c in data.columns}
    counts: Dict[str, List[int]] = {c["name"]: [0] * len(days) for c in data.columns}
    counts[OTHER] = [0] * len(days)
    for i, d in enumerate(days):
        moment = datetime.combine(d, datetime.max.time())
        for s in data.stories:
            if not s.exists_at(moment):
                continue
            col = s.column_at(moment)
            counts[col if col in known else OTHER][i] += 1
    ordered = list(reversed(data.columns))
    series = [(c["name"], c["color"], counts[c["name"]]) for c in ordered]
    if any(counts[OTHER]):
        series.append((OTHER, "#475569", counts[OTHER]))
    return FlowSeries(days, series)


# ---------------------------------------------------------------------------
# Burn-up
# ---------------------------------------------------------------------------
@dataclass
class BurnUp:
    days: List[date]
    scope: List[float]
    done: List[float]
    unit: str                       # "tasks" or the field's unit/name


def _weight(story: TaskStory, field_def: Optional[Dict[str, Any]]) -> float:
    if field_def is None:
        return 1.0
    v = story.values.get(field_def["id"])
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def burnup(data: BoardData, start: date, end: date, field_id: Optional[int] = None) -> BurnUp:
    field_def = next((f for f in data.fields if f["id"] == field_id), None)
    days = days_between(start, end)
    scope, done = [], []
    done_cols = data.done_names
    for d in days:
        moment = datetime.combine(d, datetime.max.time())
        s_total = d_total = 0.0
        for s in data.stories:
            if not s.exists_at(moment):
                continue
            w = _weight(s, field_def)
            s_total += w
            finished = (s.completed_at is not None and s.completed_at <= moment) or \
                (s.column_at(moment) in done_cols and s.is_completed)
            if finished:
                d_total += w
        scope.append(round(s_total, 2))
        done.append(round(d_total, 2))
    unit = "tasks"
    if field_def is not None:
        unit = (field_def.get("config") or {}).get("unit") or field_def["name"]
    return BurnUp(days, scope, done, unit)


# ---------------------------------------------------------------------------
# Throughput & cycle time
# ---------------------------------------------------------------------------
def throughput(data: BoardData, weeks: int = 12, today: Optional[date] = None) -> List[Tuple[date, int]]:
    """(Monday, tasks completed that week) for the last ``weeks`` weeks, oldest first."""
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    mondays = [this_monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    counts = Counter()
    for s in data.stories:
        if s.completed_at is None:
            continue
        d = s.completed_at.date()
        counts[d - timedelta(days=d.weekday())] += 1
    return [(m, counts.get(m, 0)) for m in mondays]


@dataclass
class CycleTime:
    task_id: int
    title: str
    completed: date
    cycle_days: float     # started -> done
    lead_days: float      # created -> done


def cycle_times(data: BoardData, start: date, end: date) -> List[CycleTime]:
    """Completed tasks in [start, end]. Work starts when a task first enters an "in progress"
    column (or any column after Backlog/To Do); without such a move, cycle = lead time."""
    waiting = {c["name"] for c in data.columns if c["type"] in (ColumnType.BACKLOG.value, ColumnType.TODO.value)
               and not c["done"]}
    done_cols = data.done_names
    out = []
    for s in data.stories:
        if s.completed_at is None or not (start <= s.completed_at.date() <= end):
            continue
        started = next((when for when, col in s.moves if col not in waiting and col not in done_cols), None)
        if started is None or started > s.completed_at:
            started = s.created
        cycle = max(0.0, (s.completed_at - started).total_seconds() / 86400)
        lead = max(0.0, (s.completed_at - s.created).total_seconds() / 86400)
        out.append(CycleTime(s.id, s.title, s.completed_at.date(), round(cycle, 2), round(lead, 2)))
    out.sort(key=lambda c: c.completed)
    return out


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[int(k)] if lo == hi else ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------
BREAKDOWNS = [("column", "Column"), ("priority", "Priority"), ("label", "Label"), ("assignee", "Assignee")]


def breakdown(data: BoardData, by: str, open_only: bool = True, sum_field_id: Optional[int] = None
              ) -> List[Tuple[str, float, str]]:
    """(label, value, color) slices: task counts (or the sum of a number field) grouped by
    column / priority / label / assignee / "field:<id>" (dropdown or multi-select)."""
    tasks = [t for t in data.tasks if not (open_only and t.is_completed)]
    weight_field = next((f for f in data.fields if f["id"] == sum_field_id), None)

    def weight(t) -> float:
        if weight_field is None:
            return 1.0
        v = t.field_value_map.get(weight_field["id"])
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

    totals: Dict[str, float] = defaultdict(float)
    colors: Dict[str, str] = {}
    order: List[str] = []

    def add(key: str, w: float, color: Optional[str] = None) -> None:
        if key not in totals:
            order.append(key)
        totals[key] += w
        if color:
            colors[key] = color

    if by == "column":
        for c in data.columns:
            order.append(c["name"])
            totals[c["name"]] += 0
            colors[c["name"]] = c["color"]
        names = {c["id"]: c["name"] for c in data.columns}
        for t in tasks:
            add(names.get(t.column_id, OTHER), weight(t))
    elif by == "priority":
        for p in (Priority.URGENT, Priority.HIGH, Priority.MEDIUM, Priority.LOW):
            order.append(p.display_name)
            totals[p.display_name] += 0
            colors[p.display_name] = p.color
        for t in tasks:
            try:
                add(Priority(t.priority).display_name, weight(t))
            except ValueError:
                add(OTHER, weight(t))
    elif by == "label":
        for t in tasks:
            if not t.labels:
                add("(no label)", weight(t), "#475569")
            for l in t.labels:
                add(l.name, weight(t), l.color)
    elif by == "assignee":
        for t in tasks:
            add(t.assignee or "(unassigned)", weight(t), "#475569" if not t.assignee else None)
    elif by.startswith("field:"):
        f = next((f for f in data.fields if f"field:{f['id']}" == by), None)
        if f is None:
            return []
        for o in (f.get("config") or {}).get("options", []):
            order.append(o["label"])
            totals[o["label"]] += 0
            colors[o["label"]] = o["color"]
        for t in tasks:
            v = t.field_value_map.get(f["id"])
            values = v if isinstance(v, list) else ([v] if v not in (None, "") else [])
            if not values:
                add("(empty)", weight(t), "#475569")
            for item in values:
                add(str(item), weight(t))
    else:
        raise ValueError(f"unknown breakdown '{by}'")
    out = []
    for i, key in enumerate(dict.fromkeys(order)):
        out.append((key, round(totals[key], 2), colors.get(key) or PALETTE[i % len(PALETTE)]))
    if by in ("label", "assignee"):
        out.sort(key=lambda x: -x[1])
    return out


def breakdown_options(data: BoardData) -> List[Tuple[str, str]]:
    """(key, label) of the available breakdowns (incl. the board's dropdown fields)."""
    return BREAKDOWNS + [(f"field:{f['id']}", f["name"]) for f in data.fields
                         if f["field_type"] in ("select", "multi_select")]


def number_fields(data: BoardData) -> List[Dict[str, Any]]:
    return [f for f in data.fields if f["field_type"] in ("number", "currency", "rating")]


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------
def kpis(data: BoardData, start: date, end: date) -> Dict[str, Any]:
    tasks = data.tasks
    total = len(tasks)
    done = sum(1 for t in tasks if t.is_completed)
    cycles = cycle_times(data, start, end)
    period_days = max(1, (end - start).days + 1)
    return {
        "total": total, "open": total - done, "done": done,
        "completion_pct": int(done * 100 / total) if total else 0,
        "overdue": sum(1 for t in tasks if t.is_overdue), "blocked": sum(1 for t in tasks if t.is_blocked),
        "completed_in_period": len(cycles),
        "per_week": round(len(cycles) * 7 / period_days, 1),
        "avg_cycle": round(sum(c.cycle_days for c in cycles) / len(cycles), 1) if cycles else None,
        "p85_cycle": round(percentile([c.cycle_days for c in cycles], 85), 1) if cycles else None,
        "wip_over": [c["name"] for c in data.columns if c["wip"] and
                     sum(1 for t in tasks if t.column_id == c["id"]) > c["wip"]],
    }

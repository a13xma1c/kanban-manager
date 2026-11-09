"""
Workload & Capacity Planning (pure Python, no Qt)

For every person, each open task's *remaining* effort (estimate minus time
already logged) is spread evenly over that person's working days between the
task's start date (or today, whichever is later) and its due date:

* overdue tasks put all remaining effort on today;
* tasks without a due date are listed as *unscheduled* (they cannot be placed);
* tasks without an estimate are counted but cannot be planned.

Capacity is the person's weekly capacity divided over their working days, and
only counts days from today onward (past days cannot take new work). Time
actually logged in each period is reported alongside for comparison.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from kanbanmanager.config import DEFAULT_WEEKLY_CAPACITY_HOURS, DEFAULT_WORKING_DAYS

WEEK = "week"
DAY = "day"

UNASSIGNED = ""  # row key for tasks nobody owns

# Utilisation thresholds
BUSY_THRESHOLD = 0.8
OVER_THRESHOLD = 1.0


@dataclass
class Allocation:
    task_id: int
    title: str
    board_name: str
    hours: float                  # hours placed in this period (or remaining, when unscheduled)
    remaining_hours: float        # total remaining effort of the task
    due: Optional[date]
    is_overdue: bool = False


@dataclass
class Cell:
    start: date
    end: date                     # inclusive
    capacity: float = 0.0
    planned: float = 0.0
    logged: float = 0.0
    allocations: List[Allocation] = field(default_factory=list)

    @property
    def utilization(self) -> Optional[float]:
        if self.capacity <= 0:
            return None
        return self.planned / self.capacity

    @property
    def status(self) -> str:
        """'idle', 'ok', 'busy', 'over' or 'past' (no capacity left in the period)."""
        if self.planned <= 0.005:
            return "past" if self.capacity <= 0 else "idle"
        if self.capacity <= 0:
            return "over"
        u = self.planned / self.capacity
        if u > OVER_THRESHOLD + 1e-6:
            return "over"
        if u >= BUSY_THRESHOLD:
            return "busy"
        return "ok"


@dataclass
class PersonRow:
    name: str                     # UNASSIGNED ("") for the unassigned row
    weekly_capacity: float
    working_days: List[int]
    is_member: bool
    cells: List[Cell]
    unscheduled: List[Allocation] = field(default_factory=list)
    unestimated_tasks: int = 0

    @property
    def display_name(self) -> str:
        return self.name or "Unassigned"

    @property
    def planned(self) -> float:
        return sum(c.planned for c in self.cells)

    @property
    def capacity(self) -> float:
        return sum(c.capacity for c in self.cells)

    @property
    def logged(self) -> float:
        return sum(c.logged for c in self.cells)

    @property
    def unscheduled_hours(self) -> float:
        return sum(a.hours for a in self.unscheduled)


@dataclass
class WorkloadResult:
    granularity: str
    periods: List[Tuple[date, date]]
    rows: List[PersonRow]
    today: date

    def overloads(self) -> List[Tuple[PersonRow, Cell]]:
        return [(r, c) for r in self.rows if r.name != UNASSIGNED for c in r.cells if c.status == "over"]

    def row(self, name: str) -> Optional[PersonRow]:
        return next((r for r in self.rows if r.name == name), None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def build_periods(start: date, count: int, granularity: str) -> List[Tuple[date, date]]:
    if granularity == WEEK:
        first = week_start(start)
        return [(first + timedelta(weeks=i), first + timedelta(weeks=i, days=6)) for i in range(count)]
    return [(start + timedelta(days=i), start + timedelta(days=i)) for i in range(count)]


def _as_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def utc_iso_to_local_date(value: Any) -> Optional[date]:
    """Time entries are stored as naive UTC; bucket them by the user's local calendar day."""
    if not value:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=timezone.utc).astimezone().date()


def _daterange(a: date, b: date) -> Iterable[date]:
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------
def compute_workload(
    tasks: List[Dict[str, Any]],
    members: Dict[str, Dict[str, Any]],
    period_start: date,
    periods: int = 4,
    granularity: str = WEEK,
    today: Optional[date] = None,
    time_entries: Optional[List[Dict[str, Any]]] = None,
) -> WorkloadResult:
    """Compute planned load vs. capacity per person and period.

    ``tasks``: dicts with id, title, board_name, assignee, estimated_hours, spent_hours,
    start_date, due_date, is_completed (Task.to_dict() plus board_name).
    ``members``: name -> {"weekly_capacity_hours": float, "working_days": [0..6]}.
    ``time_entries``: finished entries (user, started_at in naive UTC, duration_seconds).
    """
    today = today or date.today()
    spans = build_periods(period_start, periods, granularity)
    range_start, range_end = spans[0][0], spans[-1][1]

    def profile(name: str) -> Tuple[float, List[int], bool]:
        m = members.get(name)
        if m is not None:
            return float(m.get("weekly_capacity_hours", 0.0)), list(m.get("working_days") or []), True
        if name == UNASSIGNED:
            return 0.0, list(DEFAULT_WORKING_DAYS), False
        return DEFAULT_WEEKLY_CAPACITY_HOURS, list(DEFAULT_WORKING_DAYS), False

    rows: Dict[str, PersonRow] = {}

    def row_for(name: str) -> PersonRow:
        if name not in rows:
            weekly, days, is_member = profile(name)
            per_day = weekly / len(days) if days else 0.0
            cells = []
            for s, e in spans:
                cap_days = sum(1 for d in _daterange(s, e) if d.weekday() in days and d >= today)
                cells.append(Cell(start=s, end=e, capacity=round(per_day * cap_days, 4)))
            rows[name] = PersonRow(name, weekly, days, is_member, cells)
        return rows[name]

    def cell_index(d: date) -> Optional[int]:
        if d < range_start or d > range_end:
            return None
        if granularity == WEEK:
            return (week_start(d) - range_start).days // 7
        return (d - range_start).days

    for name in members:
        row_for(name)

    for t in tasks:
        if t.get("is_completed"):
            continue
        person = (t.get("assignee") or "").strip()
        estimate = float(t.get("estimated_hours") or 0.0)
        row = row_for(person)
        if estimate <= 0:
            row.unestimated_tasks += 1
            continue
        remaining = max(0.0, estimate - float(t.get("spent_hours") or 0.0))
        if remaining <= 0.005:
            continue
        due = _as_date(t.get("due_date"))
        base = dict(task_id=t["id"], title=t.get("title", ""), board_name=t.get("board_name", ""),
                    remaining_hours=remaining, due=due)
        if due is None:
            row.unscheduled.append(Allocation(hours=remaining, **base))
            continue

        overdue = due < today
        begin = today if overdue else max(_as_date(t.get("start_date")) or today, today)
        end = today if overdue else due
        begin = min(begin, end)
        work_days = [d for d in _daterange(begin, end) if d.weekday() in row.working_days] or [end]
        per_day = remaining / len(work_days)

        placed: Dict[int, float] = defaultdict(float)
        for d in work_days:
            idx = cell_index(d)
            if idx is not None:
                placed[idx] += per_day
        for idx, hours in placed.items():
            cell = row.cells[idx]
            cell.planned += hours
            cell.allocations.append(Allocation(hours=hours, is_overdue=overdue, **base))

    for e in time_entries or []:
        user = (e.get("user") or "").strip()
        if not user:
            continue
        idx = cell_index(utc_iso_to_local_date(e.get("started_at")))
        if idx is None:
            continue
        row_for(user).cells[idx].logged += int(e.get("duration_seconds") or 0) / 3600.0

    for r in rows.values():
        for c in r.cells:
            c.planned = round(c.planned, 4)
            c.logged = round(c.logged, 4)
            c.allocations.sort(key=lambda a: (-a.hours, a.title))

    # Rows: people with work or capacity first (alphabetical), unassigned last; drop empty non-members
    def has_content(r: PersonRow) -> bool:
        return r.is_member or r.planned > 0 or r.logged > 0 or r.unscheduled or r.unestimated_tasks > 0

    ordered = sorted((r for r in rows.values() if r.name != UNASSIGNED and has_content(r)),
                     key=lambda r: r.name.lower())
    unassigned = rows.get(UNASSIGNED)
    if unassigned and has_content(unassigned):
        ordered.append(unassigned)
    return WorkloadResult(granularity=granularity, periods=spans, rows=ordered, today=today)

"""
Project Scheduling: Critical Path Method, Auto-Scheduling, Cascading Shifts, Baselines
(pure Python, no Qt)

Model
-----
* Time is measured in **working days** of a board calendar (default Mon-Fri).
  Internally a task occupies the half-open working-day interval [ES, EF):
  a task planned Mon..Wed has ES = index(Mon), EF = index(Wed) + 1, duration 3.
  A milestone has duration 0 and sits at the *end* of its day (ES = EF).
* Duration: milestones 0; tasks with both dates use their span; otherwise the
  estimate divided by the board's hours per day (rounded up); otherwise 1 day.
* Dependencies (predecessor P -> successor S, lag L in working days):
    FS: S.ES >= P.EF + L      SS: S.ES >= P.ES + L
    FF: S.EF >= P.EF + L      SF: S.EF >= P.ES + L

Analysis (CPM)
--------------
The *planned* dates are what the user set. The forward pass forecasts each
task no earlier than its planned start and no earlier than its predecessors
allow; the backward pass computes the latest dates that keep the forecast
project finish. Total float = LS - ES; tasks with float <= 0 are critical.
Planned dates that violate a dependency are reported as conflicts. Completed
and locked tasks keep their dates (a locked task that violates a dependency
gets negative float on its predecessors).

Auto-scheduling proposes ASAP dates from the network; cascading pushes
(optionally pulls) dependent tasks after a date change. Both return proposals
for the UI/engine to apply, never writing to the database themselves.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from kanbanmanager.enums import DependencyType

FS, SS, FF, SF = "FS", "SS", "FF", "SF"

DEFAULT_SCHEDULE_SETTINGS: Dict[str, Any] = {
    "working_days": [0, 1, 2, 3, 4],
    "hours_per_day": 8.0,
    "project_start": None,      # ISO date; default: today for unscheduled work
    "cascade": True,            # shift dependents when a task's dates change
    "cascade_pull": False,      # also pull dependents earlier to stay tightly linked
    "baseline_id": None,        # baseline shown in the Gantt
}


def normalize_schedule_settings(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    s = dict(DEFAULT_SCHEDULE_SETTINGS)
    s.update({k: v for k, v in (raw or {}).items() if k in DEFAULT_SCHEDULE_SETTINGS})
    days = sorted({int(d) for d in (s.get("working_days") or []) if 0 <= int(d) <= 6})
    s["working_days"] = days or [0, 1, 2, 3, 4]
    try:
        s["hours_per_day"] = min(24.0, max(0.5, float(s.get("hours_per_day") or 8.0)))
    except (TypeError, ValueError):
        s["hours_per_day"] = 8.0
    s["cascade"] = bool(s["cascade"])
    s["cascade_pull"] = bool(s["cascade_pull"])
    return s


def dep_kind(dependency_type: Optional[str]) -> str:
    try:
        return DependencyType(dependency_type or "FINISH_TO_START").scheduling_type.short
    except ValueError:
        return FS


# ---------------------------------------------------------------------------
# Working-day calendar
# ---------------------------------------------------------------------------
class WorkCalendar:
    """Working-day arithmetic relative to an origin date (O(1) per call)."""

    def __init__(self, working_days: Iterable[int] = (0, 1, 2, 3, 4), origin: Optional[date] = None):
        self.working_days: Set[int] = set(working_days) or {0, 1, 2, 3, 4}
        self.per_week = len(self.working_days)
        origin = origin or date.today()
        self.origin = origin - timedelta(days=origin.weekday())  # a Monday

    def is_working(self, d: date) -> bool:
        return d.weekday() in self.working_days

    def _count(self, a: date, b: date) -> int:
        """Working days in [a, b) for a <= b."""
        days = (b - a).days
        weeks, rem = divmod(days, 7)
        n = weeks * self.per_week
        base = a + timedelta(days=weeks * 7)
        for k in range(rem):
            if (base + timedelta(days=k)).weekday() in self.working_days:
                n += 1
        return n

    def index_start(self, d: date) -> int:
        """Index of the first working day at or after ``d``."""
        return self._count(self.origin, d) if d >= self.origin else -self._count(d, self.origin)

    def index_end(self, d: date) -> int:
        """Exclusive end index for work finishing on ``d`` (or the last working day before it)."""
        return self.index_start(d + timedelta(days=1))

    def day(self, i: int) -> date:
        """The working day with index ``i``."""
        if i >= 0:
            weeks, _ = divmod(i, self.per_week)
            d = self.origin + timedelta(days=weeks * 7)
            idx = weeks * self.per_week
        else:
            weeks = (-i) // self.per_week + 1
            d = self.origin - timedelta(days=weeks * 7)
            idx = -weeks * self.per_week
        while True:
            if d.weekday() in self.working_days:
                if idx == i:
                    return d
                idx += 1
            d += timedelta(days=1)

    def add_working_days(self, d: date, n: int) -> date:
        return self.day(self.index_start(d) + n)

    def span(self, start: date, finish: date) -> int:
        """Working days from ``start`` to ``finish`` inclusive (>= 0)."""
        return max(0, self.index_end(finish) - self.index_start(start))

    def next_working(self, d: date) -> date:
        return self.day(self.index_start(d))


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass
class SchedTask:
    id: int
    title: str
    duration: int
    start: Optional[date]        # planned (None = unscheduled)
    finish: Optional[date]
    is_milestone: bool = False
    is_completed: bool = False
    locked: bool = False
    assignee: str = ""
    due_time: Optional[Tuple[int, int]] = None  # keep the due time-of-day when shifting

    @property
    def scheduled(self) -> bool:
        return self.start is not None and self.finish is not None


@dataclass(frozen=True)
class SchedDep:
    pred: int
    succ: int
    kind: str = FS
    lag: int = 0


def _to_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def build_inputs(tasks: Sequence[Dict[str, Any]], dependencies: Sequence[Dict[str, Any]],
                 settings: Dict[str, Any], calendar: WorkCalendar) -> Tuple[List[SchedTask], List[SchedDep]]:
    """Turn Task.to_dict() rows and dependency rows into scheduling inputs."""
    hpd = float(settings.get("hours_per_day") or 8.0)
    out: List[SchedTask] = []
    for t in tasks:
        start, due_raw = _to_date(t.get("start_date")), t.get("due_date")
        finish = _to_date(due_raw)
        due_time = None
        if isinstance(due_raw, str) and "T" in due_raw:
            dt = datetime.fromisoformat(due_raw)
            due_time = (dt.hour, dt.minute)
        elif isinstance(due_raw, datetime):
            due_time = (due_raw.hour, due_raw.minute)
        milestone = bool(t.get("is_milestone"))
        if milestone:
            duration = 0
            when = finish or start
            start = finish = when
        else:
            if start and finish and finish < start:
                finish = start
            if start and finish:
                duration = max(1, calendar.span(start, finish))
            else:
                est = float(t.get("estimated_hours") or 0.0)
                duration = max(1, math.ceil(est / hpd)) if est > 0 else 1
                if finish and not start:
                    end = calendar.index_end(finish)
                    start = calendar.day(end - duration)
                elif start and not finish:
                    finish = calendar.day(calendar.index_start(start) + duration - 1)
        out.append(SchedTask(
            id=t["id"], title=t.get("title", ""), duration=duration, start=start, finish=finish,
            is_milestone=milestone, is_completed=bool(t.get("is_completed")), locked=bool(t.get("dates_locked")),
            assignee=t.get("assignee") or "", due_time=due_time,
        ))
    ids = {t.id for t in out}
    deps = [SchedDep(d["depends_on_task_id"], d["task_id"], dep_kind(d.get("dependency_type")),
                     int(d.get("lag_days") or 0))
            for d in dependencies if d["task_id"] in ids and d["depends_on_task_id"] in ids]
    return out, deps


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class TaskSchedule:
    id: int
    duration: int
    es: int
    ef: int
    ls: int = 0
    lf: int = 0
    planned: bool = True          # had dates (False = placed by the network / project start)
    is_critical: bool = False
    driver: Optional[str] = None  # what determined the forecast start

    @property
    def total_float(self) -> int:
        return self.ls - self.es


@dataclass
class Conflict:
    pred: int
    succ: int
    kind: str
    lag: int
    days: int                     # how many working days the successor is too early

    def describe(self, titles: Dict[int, str]) -> str:
        what = {FS: "starts before '{p}' finishes", SS: "starts before '{p}' starts",
                FF: "finishes before '{p}' finishes", SF: "finishes before '{p}' starts"}[self.kind]
        lag = f" (+{self.lag}d lag)" if self.lag > 0 else (f" ({self.lag}d lead)" if self.lag < 0 else "")
        return (f"'{titles.get(self.succ, self.succ)}' " + what.format(p=titles.get(self.pred, self.pred))
                + lag + f" - {self.days} day{'s' if self.days != 1 else ''} too early")


@dataclass
class ScheduleResult:
    calendar: WorkCalendar
    tasks: Dict[int, TaskSchedule]
    inputs: Dict[int, SchedTask]
    deps: List[SchedDep]
    order: List[int]
    project_start: Optional[int]
    project_finish: Optional[int]
    conflicts: List[Conflict] = field(default_factory=list)
    cycle_ids: List[int] = field(default_factory=list)

    def start_date(self, task_id: int) -> date:
        t = self.tasks[task_id]
        # a milestone sits at the end of its day: its start and finish are the same date
        return self.calendar.day(t.es) if t.duration > 0 else self.finish_date(task_id)

    def finish_date(self, task_id: int) -> date:
        return self.calendar.day(self.tasks[task_id].ef - 1)

    def late_finish_date(self, task_id: int) -> date:
        return self.calendar.day(self.tasks[task_id].lf - 1)

    @property
    def critical_ids(self) -> List[int]:
        return [tid for tid in self.order if tid in self.tasks and self.tasks[tid].is_critical]

    @property
    def project_start_date(self) -> Optional[date]:
        return self.calendar.day(self.project_start) if self.project_start is not None else None

    @property
    def project_finish_date(self) -> Optional[date]:
        return self.calendar.day(self.project_finish - 1) if self.project_finish is not None else None

    @property
    def duration_days(self) -> int:
        if self.project_start is None or self.project_finish is None:
            return 0
        return self.project_finish - self.project_start

    def critical_path(self) -> List[int]:
        """One critical chain from project start to finish (ordered)."""
        crit = set(self.critical_ids)
        if not crit:
            return []
        preds = defaultdict(list)
        for d in self.deps:
            if d.pred in crit and d.succ in crit:
                preds[d.succ].append(d.pred)
        end = max(crit, key=lambda i: (self.tasks[i].ef, -self.tasks[i].es))
        chain = [end]
        seen = {end}
        while preds.get(chain[-1]):
            nxt = max((p for p in preds[chain[-1]] if p not in seen), key=lambda i: self.tasks[i].ef, default=None)
            if nxt is None:
                break
            chain.append(nxt)
            seen.add(nxt)
        return list(reversed(chain))


# ---------------------------------------------------------------------------
# CPM core
# ---------------------------------------------------------------------------
def _topological(ids: Iterable[int], deps: Sequence[SchedDep]) -> Tuple[List[int], List[int]]:
    ids = list(ids)
    indeg = {i: 0 for i in ids}
    succs = defaultdict(list)
    for d in deps:
        if d.pred in indeg and d.succ in indeg:
            indeg[d.succ] += 1
            succs[d.pred].append(d.succ)
    queue = deque(sorted(i for i, n in indeg.items() if n == 0))
    order = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for s in succs[n]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    cyclic = sorted(i for i in ids if i not in set(order))
    return order, cyclic


def _required_start(kind: str, lag: int, pred_es: int, pred_ef: int, succ_duration: int) -> int:
    """Earliest ES of a successor allowed by one dependency."""
    if kind == SS:
        return pred_es + lag
    if kind == FF:
        return pred_ef + lag - succ_duration
    if kind == SF:
        return pred_es + lag - succ_duration
    return pred_ef + lag


def _latest_finish(kind: str, lag: int, succ_ls: int, succ_lf: int, pred_duration: int) -> int:
    """Latest EF of a predecessor allowed by one dependency."""
    if kind == SS:
        return succ_ls - lag + pred_duration
    if kind == FF:
        return succ_lf - lag
    if kind == SF:
        return succ_lf - lag + pred_duration
    return succ_ls - lag


def analyze(tasks: Sequence[SchedTask], deps: Sequence[SchedDep], calendar: WorkCalendar,
            default_start: Optional[date] = None) -> ScheduleResult:
    """CPM on the current plan (see module docs)."""
    inputs = {t.id: t for t in tasks if t.scheduled or not t.is_completed}
    deps = [d for d in deps if d.pred in inputs and d.succ in inputs]
    order, cyclic = _topological(inputs, deps)
    default_idx = calendar.index_start(default_start or date.today())
    preds: Dict[int, List[SchedDep]] = defaultdict(list)
    succs: Dict[int, List[SchedDep]] = defaultdict(list)
    for d in deps:
        preds[d.succ].append(d)
        succs[d.pred].append(d)

    titles = {t.id: t.title for t in inputs.values()}
    result: Dict[int, TaskSchedule] = {}
    for tid in order:
        t = inputs[tid]
        planned_es = calendar.index_start(t.start) if t.scheduled else None
        if t.is_milestone and t.scheduled:
            planned_es = calendar.index_end(t.start)  # end of the milestone's day
        fixed = t.is_completed or t.locked
        es = planned_es if planned_es is not None else default_idx
        driver = "planned start" if planned_es is not None else "project start"
        if not fixed:
            for d in preds[tid]:
                p = result.get(d.pred)
                if p is None:
                    continue
                req = _required_start(d.kind, d.lag, p.es, p.ef, t.duration)
                if req > es:
                    es, driver = req, f"after '{titles[d.pred]}' ({d.kind}{'+%d' % d.lag if d.lag else ''})"
        elif t.locked and not t.is_completed:
            driver = "locked"
        result[tid] = TaskSchedule(id=tid, duration=t.duration, es=es, ef=es + t.duration,
                                   planned=planned_es is not None, driver=driver)

    if result:
        project_start = min(r.es for r in result.values())
        project_finish = max(r.ef for r in result.values())
    else:
        project_start = project_finish = None

    for tid in reversed(order):
        r = result[tid]
        lf = project_finish
        for d in succs[tid]:
            s = result.get(d.succ)
            if s is not None:
                lf = min(lf, _latest_finish(d.kind, d.lag, s.ls, s.lf, r.duration))
        r.lf = lf
        r.ls = lf - r.duration
        r.is_critical = (r.ls - r.es) <= 0 and not inputs[tid].is_completed

    conflicts: List[Conflict] = []
    for d in deps:
        p, s = inputs[d.pred], inputs[d.succ]
        if not (p.scheduled and s.scheduled) or d.pred not in result or d.succ not in result:
            continue
        p_es = calendar.index_end(p.start) if p.is_milestone else calendar.index_start(p.start)
        p_ef = p_es + p.duration
        s_es = calendar.index_end(s.start) if s.is_milestone else calendar.index_start(s.start)
        req = _required_start(d.kind, d.lag, p_es, p_ef, s.duration)
        if s_es < req and not s.is_completed:
            conflicts.append(Conflict(d.pred, d.succ, d.kind, d.lag, req - s_es))

    return ScheduleResult(calendar=calendar, tasks=result, inputs=inputs, deps=list(deps), order=order,
                          project_start=project_start, project_finish=project_finish, conflicts=conflicts,
                          cycle_ids=cyclic)


# ---------------------------------------------------------------------------
# Proposals (auto-schedule & cascade)
# ---------------------------------------------------------------------------
@dataclass
class DateChange:
    task_id: int
    title: str
    old_start: Optional[date]
    old_finish: Optional[date]
    new_start: date
    new_finish: date
    reason: str
    shift_days: int = 0           # working days the start moves (+ later / - earlier)

    @property
    def is_new(self) -> bool:
        return self.old_start is None


@dataclass
class ScheduleProposal:
    changes: List[DateChange]
    blocked: List[str]            # conflicts that could not be resolved (locked/completed tasks)
    project_finish: Optional[date]


def _dates_for(calendar: WorkCalendar, es: int, duration: int) -> Tuple[date, date]:
    if duration <= 0:
        d = calendar.day(es - 1)
        return d, d
    return calendar.day(es), calendar.day(es + duration - 1)


def auto_schedule(tasks: Sequence[SchedTask], deps: Sequence[SchedDep], calendar: WorkCalendar,
                  project_start: Optional[date] = None, keep_planned_starts: bool = True,
                  include_unscheduled: bool = True) -> ScheduleProposal:
    """ASAP dates from the dependency network.

    Completed and locked tasks keep their dates. With ``keep_planned_starts``
    no task moves earlier than currently planned; otherwise every movable task
    is packed as early as the network and ``project_start`` allow.
    """
    inputs = {t.id: t for t in tasks}
    deps = [d for d in deps if d.pred in inputs and d.succ in inputs]
    order, cyclic = _topological(inputs, deps)
    floor = calendar.index_start(project_start or date.today())
    preds: Dict[int, List[SchedDep]] = defaultdict(list)
    for d in deps:
        preds[d.succ].append(d)
    titles = {t.id: t.title for t in inputs.values()}
    placed: Dict[int, Tuple[int, int]] = {}
    changes: List[DateChange] = []
    blocked: List[str] = []

    for tid in order:
        t = inputs[tid]
        planned_es = None
        if t.scheduled:
            planned_es = calendar.index_end(t.start) if t.is_milestone else calendar.index_start(t.start)
        if t.is_completed or t.locked or (not t.scheduled and not include_unscheduled):
            if planned_es is not None:
                placed[tid] = (planned_es, planned_es + t.duration)
                if t.locked and not t.is_completed:
                    for d in preds[tid]:
                        if d.pred in placed:
                            req = _required_start(d.kind, d.lag, *placed[d.pred], t.duration)
                            if req > planned_es:
                                blocked.append(f"'{t.title}' is locked but must move {req - planned_es} working "
                                               f"day(s) later because of '{titles[d.pred]}'")
            continue
        lower = planned_es if (keep_planned_starts and planned_es is not None) else floor
        # a milestone's index is the end of its day: give it the same floor semantics
        if t.is_milestone and lower == floor:
            lower = floor + 1
        es, reason = lower, ("planned start" if lower == planned_es else "project start")
        for d in preds[tid]:
            if d.pred not in placed:
                continue
            req = _required_start(d.kind, d.lag, *placed[d.pred], t.duration)
            if req > es:
                es, reason = req, f"after '{titles[d.pred]}' ({d.kind}{'%+d' % d.lag if d.lag else ''})"
        placed[tid] = (es, es + t.duration)
        new_start, new_finish = _dates_for(calendar, es, t.duration)
        if (new_start, new_finish) != (t.start, t.finish):
            shift = es - planned_es if planned_es is not None else 0
            changes.append(DateChange(tid, t.title, t.start, t.finish, new_start, new_finish, reason, shift))

    for tid in cyclic:
        blocked.append(f"'{titles[tid]}' is part of a dependency cycle and was not scheduled")
    finish = max((ef for _, ef in placed.values()), default=None)
    return ScheduleProposal(changes, blocked, calendar.day(finish - 1) if finish is not None else None)


def cascade(tasks: Sequence[SchedTask], deps: Sequence[SchedDep], calendar: WorkCalendar,
            changed_ids: Iterable[int], pull: bool = False) -> ScheduleProposal:
    """After ``changed_ids`` moved, shift their (transitive) dependents so no dependency is violated.

    Push-only by default: dependents only move later. With ``pull`` they also
    move earlier to the tightest position their predecessors allow. Durations
    are kept; completed, locked and unscheduled dependents are never moved.
    """
    inputs = {t.id: t for t in tasks}
    deps = [d for d in deps if d.pred in inputs and d.succ in inputs]
    order, _ = _topological(inputs, deps)
    succs: Dict[int, List[SchedDep]] = defaultdict(list)
    preds: Dict[int, List[SchedDep]] = defaultdict(list)
    for d in deps:
        succs[d.pred].append(d)
        preds[d.succ].append(d)
    affected: Set[int] = set()
    queue = deque(i for i in changed_ids if i in inputs)
    while queue:
        n = queue.popleft()
        for d in succs[n]:
            if d.succ not in affected:
                affected.add(d.succ)
                queue.append(d.succ)
    affected -= set(changed_ids)

    def current(t: SchedTask) -> Optional[Tuple[int, int]]:
        if not t.scheduled:
            return None
        es = calendar.index_end(t.start) if t.is_milestone else calendar.index_start(t.start)
        return es, es + t.duration

    titles = {t.id: t.title for t in inputs.values()}
    position = {tid: current(t) for tid, t in inputs.items()}
    changes: List[DateChange] = []
    blocked: List[str] = []
    for tid in order:
        if tid not in affected:
            continue
        t = inputs[tid]
        pos = position[tid]
        if pos is None:
            continue
        req, reason = None, ""
        for d in preds[tid]:
            p = position.get(d.pred)
            if p is None:
                continue
            r = _required_start(d.kind, d.lag, p[0], p[1], t.duration)
            if req is None or r > req:
                req, reason = r, f"after '{titles[d.pred]}' ({d.kind}{'%+d' % d.lag if d.lag else ''})"
        if req is None or req == pos[0] or (req < pos[0] and not pull):
            continue
        if t.is_completed or t.locked:
            if req > pos[0]:
                blocked.append(f"'{t.title}' is {'completed' if t.is_completed else 'locked'} and was not moved "
                               f"({req - pos[0]} working day(s) too early)")
            continue
        position[tid] = (req, req + t.duration)
        new_start, new_finish = _dates_for(calendar, req, t.duration)
        changes.append(DateChange(tid, t.title, t.start, t.finish, new_start, new_finish, reason, req - pos[0]))
    finish = max((p[1] for p in position.values() if p), default=None)
    return ScheduleProposal(changes, blocked, calendar.day(finish - 1) if finish is not None else None)


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------
def baseline_entries(tasks: Sequence[SchedTask]) -> List[Dict[str, Any]]:
    """Snapshot of planned dates (only tasks that have dates)."""
    return [
        {"task_id": t.id, "title": t.title, "start_date": t.start, "finish_date": t.finish,
         "duration_days": t.duration, "is_milestone": t.is_milestone}
        for t in tasks if t.scheduled
    ]


@dataclass
class Variance:
    task_id: Optional[int]
    title: str
    baseline_start: Optional[date]
    baseline_finish: Optional[date]
    current_start: Optional[date]
    current_finish: Optional[date]
    start_var: Optional[int]      # working days, + later than baseline
    finish_var: Optional[int]
    status: str                   # on_track | late | early | new | removed | unscheduled
    is_milestone: bool = False


@dataclass
class BaselineComparison:
    rows: List[Variance]
    baseline_finish: Optional[date]
    current_finish: Optional[date]
    finish_var: Optional[int]

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r.status == status)


def compare_baseline(entries: Sequence[Dict[str, Any]], tasks: Sequence[SchedTask],
                     calendar: WorkCalendar) -> BaselineComparison:
    current = {t.id: t for t in tasks}
    seen: Set[int] = set()
    rows: List[Variance] = []

    def var(a: date, b: date) -> int:
        """Signed working days from baseline date a to current date b."""
        return calendar.index_start(b) - calendar.index_start(a)

    for e in entries:
        b_start, b_finish = _to_date(e["start_date"]), _to_date(e["finish_date"])
        tid = e.get("task_id")
        t = current.get(tid) if tid is not None else None
        if t is None:
            rows.append(Variance(tid, e["title"], b_start, b_finish, None, None, None, None, "removed",
                                 bool(e.get("is_milestone"))))
            continue
        seen.add(t.id)
        if not t.scheduled:
            rows.append(Variance(t.id, t.title, b_start, b_finish, None, None, None, None, "unscheduled",
                                 t.is_milestone))
            continue
        sv, fv = var(b_start, t.start), var(b_finish, t.finish)
        status = "late" if fv > 0 else ("early" if fv < 0 else "on_track")
        rows.append(Variance(t.id, t.title, b_start, b_finish, t.start, t.finish, sv, fv, status, t.is_milestone))
    for t in tasks:
        if t.id not in seen and t.scheduled:
            rows.append(Variance(t.id, t.title, None, None, t.start, t.finish, None, None, "new", t.is_milestone))

    b_fin = max((_to_date(e["finish_date"]) for e in entries), default=None)
    c_fin = max((t.finish for t in tasks if t.scheduled), default=None)
    f_var = var(b_fin, c_fin) if b_fin and c_fin else None
    order = {"late": 0, "unscheduled": 1, "removed": 2, "new": 3, "early": 4, "on_track": 5}
    rows.sort(key=lambda r: (order[r.status], -(r.finish_var or 0), r.title.lower()))
    return BaselineComparison(rows, b_fin, c_fin, f_var)


# ---------------------------------------------------------------------------
# Convenience for the app
# ---------------------------------------------------------------------------
def calendar_for(settings: Dict[str, Any], dates: Iterable[Optional[date]] = ()) -> WorkCalendar:
    known = [d for d in dates if d]
    origin = min(known + [date.today()]) - timedelta(days=30)
    return WorkCalendar(settings.get("working_days") or [0, 1, 2, 3, 4], origin)


def load_board_schedule(service, board_id: int) -> Tuple[Dict[str, Any], WorkCalendar, List[SchedTask],
                                                          List[SchedDep]]:
    """Settings, calendar and scheduling inputs for a board (non-archived tasks)."""
    settings = normalize_schedule_settings(service.get_schedule_settings(board_id))
    rows = [t.to_dict() for t in service.get_tasks_for_board(board_id)]
    deps = service.get_board_dependencies(board_id)
    dates = [_to_date(r.get("start_date")) for r in rows] + [_to_date(r.get("due_date")) for r in rows]
    calendar = calendar_for(settings, dates)
    tasks, sdeps = build_inputs(rows, deps, settings, calendar)
    return settings, calendar, tasks, sdeps


def project_start_for(settings: Dict[str, Any]) -> date:
    raw = settings.get("project_start")
    try:
        return _to_date(raw) or date.today()
    except ValueError:
        return date.today()


def due_datetime(task: SchedTask, finish: date) -> datetime:
    """Due date-time for a new finish date, keeping the task's time of day (default 17:00)."""
    hour, minute = task.due_time or (17, 0)
    return datetime.combine(finish, datetime.min.time()).replace(hour=hour, minute=minute)

"""
Schedule Engine: cascading date shifts and applying schedule proposals

Listens to the service's DomainEvents. When a task's dates (or milestone flag,
or a dependency) change, dependents that would now violate a dependency are
shifted (see core.scheduling.cascade), if the board enables cascading. Shifts
are recorded in the audit trail as "⚙ Automation: Cascading schedule"; the
whole chain is computed at once, so the engine ignores the events its own
updates produce.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable, Iterator, Optional, Sequence

from kanbanmanager.config import AUTOMATION_ACTOR_PREFIX
from kanbanmanager.core import scheduling as sch
from kanbanmanager.database import events as ev

log = logging.getLogger(__name__)

CASCADE_ACTOR = f"{AUTOMATION_ACTOR_PREFIX}: Cascading schedule"
AUTO_SCHEDULE_ACTOR = f"{AUTOMATION_ACTOR_PREFIX}: Auto-schedule"

_DATE_FIELDS = {"start_date", "due_date", "is_milestone", "estimated_hours"}


class ScheduleEngine:
    def __init__(self, service, notify: Optional[Callable[[str], None]] = None):
        self.service = service
        self.notify = notify or (lambda message: None)
        self._suspended = 0
        self.last_cascade: Optional[sch.ScheduleProposal] = None

    def attach(self) -> None:
        self.service.add_event_listener(self.handle_event)

    def detach(self) -> None:
        self.service.remove_event_listener(self.handle_event)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Apply a consistent batch of date changes without cascading after each one."""
        self._suspended += 1
        try:
            yield
        finally:
            self._suspended -= 1

    # ------------------------------------------------------------------
    def handle_event(self, event: ev.DomainEvent) -> None:
        if self._suspended or event.actor in (CASCADE_ACTOR, AUTO_SCHEDULE_ACTOR):
            return
        if event.type in (ev.DEPENDENCY_ADDED, ev.DEPENDENCY_UPDATED):
            changed = event.data.get("pred_id")
        elif event.type == ev.DUE_DATE_CHANGED or (
                event.type == ev.TASK_UPDATED and event.data.get("field") in _DATE_FIELDS):
            changed = event.task_id
        else:
            return
        if changed is None:
            return
        settings = sch.normalize_schedule_settings(self.service.get_schedule_settings(event.board_id))
        if not settings["cascade"]:
            return
        try:
            self.cascade_from(event.board_id, [changed], pull=settings["cascade_pull"])
        except Exception:  # never break the user's edit because of a scheduling problem
            log.exception("Cascading schedule failed")

    def cascade_from(self, board_id: int, task_ids: Sequence[int], pull: bool = False) -> sch.ScheduleProposal:
        _settings, calendar, tasks, deps = sch.load_board_schedule(self.service, board_id)
        proposal = sch.cascade(tasks, deps, calendar, task_ids, pull=pull)
        self.last_cascade = proposal
        if proposal.changes:
            self.apply(board_id, proposal.changes, tasks, actor=CASCADE_ACTOR)
            names = ", ".join(f"'{c.title}'" for c in proposal.changes[:3])
            more = f" and {len(proposal.changes) - 3} more" if len(proposal.changes) > 3 else ""
            self.notify(f"Shifted {len(proposal.changes)} dependent task"
                        f"{'s' if len(proposal.changes) != 1 else ''}: {names}{more}")
        if proposal.blocked:
            self.notify("Schedule conflict: " + proposal.blocked[0])
        return proposal

    def apply(self, board_id: int, changes: Sequence[sch.DateChange], tasks: Optional[Sequence[sch.SchedTask]] = None,
              actor: str = AUTO_SCHEDULE_ACTOR) -> int:
        """Write proposed dates (keeping each task's due time of day). Returns the number applied."""
        if tasks is None:
            _s, _c, tasks, _d = sch.load_board_schedule(self.service, board_id)
        by_id = {t.id: t for t in tasks}
        applied = 0
        with self.suspended(), self.service.acting_as(actor):
            for change in changes:
                task = by_id.get(change.task_id)
                if task is None:
                    continue
                self.service.update_task(change.task_id, start_date=change.new_start,
                                         due_date=sch.due_datetime(task, change.new_finish))
                applied += 1
        return applied

    # ------------------------------------------------------------------
    def auto_schedule(self, board_id: int, keep_planned_starts: bool = True,
                      include_unscheduled: bool = True) -> sch.ScheduleProposal:
        settings, calendar, tasks, deps = sch.load_board_schedule(self.service, board_id)
        return sch.auto_schedule(tasks, deps, calendar, project_start=sch.project_start_for(settings),
                                 keep_planned_starts=keep_planned_starts, include_unscheduled=include_unscheduled)

    def analyze(self, board_id: int) -> sch.ScheduleResult:
        settings, calendar, tasks, deps = sch.load_board_schedule(self.service, board_id)
        return sch.analyze(tasks, deps, calendar, default_start=sch.project_start_for(settings))

    def save_baseline(self, board_id: int, name: str) -> dict:
        _settings, _calendar, tasks, _deps = sch.load_board_schedule(self.service, board_id)
        return self.service.create_baseline(board_id, name, sch.baseline_entries(tasks))

    def compare(self, board_id: int, baseline_id: int) -> Optional[sch.BaselineComparison]:
        baseline = self.service.get_baseline(baseline_id)
        if baseline is None:
            return None
        _settings, calendar, tasks, _deps = sch.load_board_schedule(self.service, board_id)
        return sch.compare_baseline(baseline["entries"], tasks, calendar)

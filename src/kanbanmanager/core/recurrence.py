"""
Recurring Task Rule Calculation and Generator Engine
"""
from __future__ import annotations

import calendar
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List

from kanbanmanager.enums import RecurrenceFrequency


class RecurrenceEngine:
    """Calculates next recurrence dates and manages spawning recurring task instances."""

    @staticmethod
    def calculate_next_due_date(
        base_datetime: datetime,
        rule: Dict[str, Any],
        from_datetime: Optional[datetime] = None,
    ) -> Optional[datetime]:
        """
        Calculate the next due date given a base datetime and recurrence rule.
        rule format:
        {
            "frequency": "DAILY" | "WEEKDAYS" | "WEEKLY" | "BIWEEKLY" | "MONTHLY" | "CUSTOM",
            "interval": 1,
            "days_of_week": [0, 1, 2, 3, 4],  # 0=Monday, 6=Sunday
            "end_type": "never" | "after_count" | "end_date",
            "max_occurrences": 10,
            "occurrences_count": 0,
            "end_date": "2026-12-31"
        }
        """
        if not rule or not rule.get("frequency"):
            return None

        freq = rule.get("frequency", RecurrenceFrequency.DAILY.value)
        interval = max(1, int(rule.get("interval", 1)))
        from_dt = from_datetime or datetime.now()

        # Check end criteria
        end_type = rule.get("end_type", "never")
        end_day: Optional[date] = None
        if end_type == "after_count":
            max_count = int(rule.get("max_occurrences", 0))
            current_count = int(rule.get("occurrences_count", 0))
            if max_count > 0 and current_count >= max_count:
                return None
        elif end_type == "end_date" and rule.get("end_date"):
            try:
                end_day = date.fromisoformat(str(rule["end_date"])[:10])
            except ValueError:
                end_day = None

        # Monthly rules keep their day of the month: the 31st falls on Feb 28, then Mar 31 again
        anchor_day = int(rule.get("month_day") or base_datetime.day)
        next_dt = base_datetime

        if freq == RecurrenceFrequency.DAILY.value:
            next_dt = base_datetime + timedelta(days=interval)
            while next_dt <= from_dt:
                next_dt += timedelta(days=interval)

        elif freq == RecurrenceFrequency.WEEKDAYS.value:
            next_dt = base_datetime + timedelta(days=1)
            while next_dt <= from_dt or next_dt.weekday() >= 5:  # 5=Sat, 6=Sun
                next_dt += timedelta(days=1)

        elif freq == RecurrenceFrequency.WEEKLY.value or freq == RecurrenceFrequency.BIWEEKLY.value:
            week_interval = 2 if freq == RecurrenceFrequency.BIWEEKLY.value else interval
            next_dt = RecurrenceEngine._next_weekly(base_datetime, rule.get("days_of_week"), week_interval, from_dt)

        elif freq == RecurrenceFrequency.MONTHLY.value:
            next_dt = RecurrenceEngine._add_months(base_datetime, interval, anchor_day)
            while next_dt <= from_dt:
                next_dt = RecurrenceEngine._add_months(next_dt, interval, anchor_day)

        elif freq == RecurrenceFrequency.CUSTOM.value:
            unit = rule.get("custom_unit", "days")  # "days", "weeks", "months"
            if unit == "weeks":
                next_dt = base_datetime + timedelta(weeks=interval)
                while next_dt <= from_dt:
                    next_dt += timedelta(weeks=interval)
            elif unit == "months":
                next_dt = RecurrenceEngine._add_months(base_datetime, interval, anchor_day)
                while next_dt <= from_dt:
                    next_dt = RecurrenceEngine._add_months(next_dt, interval, anchor_day)
            else:  # days
                next_dt = base_datetime + timedelta(days=interval)
                while next_dt <= from_dt:
                    next_dt += timedelta(days=interval)

        if end_day is not None and next_dt.date() > end_day:
            return None  # the series is over: the next occurrence would fall after its end date
        return next_dt

    @staticmethod
    def _next_weekly(base: datetime, days_of_week: Any, every_weeks: int, after: datetime) -> datetime:
        """The first selected weekday after ``base`` (and after ``after``) in a week that is a
        whole number of ``every_weeks`` weeks from ``base``'s week."""
        days = sorted({int(d) for d in days_of_week if 0 <= int(d) <= 6}) if isinstance(days_of_week, list) else []
        days = days or [base.weekday()]
        base_monday = base.date() - timedelta(days=base.weekday())
        candidate = base + timedelta(days=1)
        if after > candidate:  # skip whole cycles (keeps the week phase)
            cycles = (after - candidate).days // (7 * every_weeks)
            candidate += timedelta(weeks=cycles * every_weeks)
        # what's left of the gap to ``after`` (under one cycle) plus one full cycle always holds a match
        for _ in range(14 * every_weeks + 7):
            monday = candidate.date() - timedelta(days=candidate.weekday())
            in_active_week = ((monday - base_monday).days // 7) % every_weeks == 0
            if in_active_week and candidate.weekday() in days and candidate > after:
                return candidate
            candidate += timedelta(days=1)
        return base + timedelta(weeks=every_weeks)  # not reached: some active week has a selected day

    @staticmethod
    def _add_months(source_date: datetime, months: int, day: Optional[int] = None) -> datetime:
        """``months`` later on ``day`` (default: the same day), clamped to the month's length."""
        month = source_date.month - 1 + months
        year = source_date.year + month // 12
        month = month % 12 + 1
        day = min(day or source_date.day, calendar.monthrange(year, month)[1])
        return source_date.replace(year=year, month=month, day=day)

    @staticmethod
    def handle_recurring_task_completion(service, task_id: int) -> Optional[int]:
        """
        When a recurring task is completed, spawn the next task instance with updated due date.
        Returns the new task_id if created, or None.
        """
        task = service.get_task(task_id)
        if not task or not task.is_recurring:
            return None

        rule = task.recurrence_rule
        if not rule:
            return None

        base_due = task.due_date or datetime.now()
        next_due = RecurrenceEngine.calculate_next_due_date(base_due, rule)
        if not next_due:
            return None

        # Update occurrences count in rule
        updated_rule = dict(rule)
        updated_rule["occurrences_count"] = int(rule.get("occurrences_count", 0)) + 1
        if rule.get("frequency") == RecurrenceFrequency.MONTHLY.value or rule.get("custom_unit") == "months":
            updated_rule.setdefault("month_day", base_due.day)  # remember the 31st across short months

        # Calculate new start_date if original had one
        new_start_date = None
        if task.start_date and task.due_date:
            duration = task.due_date.date() - task.start_date
            new_start_date = next_due.date() - duration

        # Reset checklist items for the new instance
        new_checklist = []
        for item in task.checklist:
            new_checklist.append({"id": item.get("id"), "text": item.get("text", ""), "done": False})

        # Fetch first column / backlog or original column
        board = service.get_board(task.board_id)
        target_col_id = task.column_id
        if board and board.columns:
            # Place in first non-done column (usually Backlog or To Do)
            for c in board.columns:
                if not c.is_done_column:
                    target_col_id = c.id
                    break

        label_ids = [lbl.id for lbl in task.labels]

        new_task = service.create_task(
            board_id=task.board_id,
            column_id=target_col_id,
            title=task.title,
            description=task.description or "",
            priority=task.priority,
            estimated_hours=task.estimated_hours,
            start_date=new_start_date,
            due_date=next_due,
            label_ids=label_ids,
            checklist=new_checklist,
            # Same field values (an empty one stays empty rather than getting the field's default)
            field_values={f["id"]: task.field_value_map.get(f["id"]) for f in service.get_custom_fields(task.board_id)},
            is_recurring=True,
            recurrence_rule=updated_rule,
            assignee=task.assignee,
        )

        return new_task.id

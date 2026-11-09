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
        if end_type == "after_count":
            max_count = int(rule.get("max_occurrences", 0))
            current_count = int(rule.get("occurrences_count", 0))
            if max_count > 0 and current_count >= max_count:
                return None
        elif end_type == "end_date" and rule.get("end_date"):
            try:
                end_dt = datetime.fromisoformat(rule["end_date"])
                if from_dt >= end_dt:
                    return None
            except Exception:
                pass

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
            days = rule.get("days_of_week")
            if not days or not isinstance(days, list):
                days = [base_datetime.weekday()]

            # Sort days
            days = sorted(list(set(days)))
            found = False
            candidate = base_datetime + timedelta(days=1)
            
            # Search within next 52 weeks
            for _ in range(365):
                if candidate.weekday() in days and candidate > from_dt:
                    next_dt = candidate
                    found = True
                    break
                candidate += timedelta(days=1)

            if not found:
                next_dt = base_datetime + timedelta(weeks=week_interval)

        elif freq == RecurrenceFrequency.MONTHLY.value:
            # Advance month by interval
            next_dt = RecurrenceEngine._add_months(base_datetime, interval)
            while next_dt <= from_dt:
                next_dt = RecurrenceEngine._add_months(next_dt, interval)

        elif freq == RecurrenceFrequency.CUSTOM.value:
            unit = rule.get("custom_unit", "days")  # "days", "weeks", "months"
            if unit == "weeks":
                next_dt = base_datetime + timedelta(weeks=interval)
                while next_dt <= from_dt:
                    next_dt += timedelta(weeks=interval)
            elif unit == "months":
                next_dt = RecurrenceEngine._add_months(base_datetime, interval)
                while next_dt <= from_dt:
                    next_dt = RecurrenceEngine._add_months(next_dt, interval)
            else:  # days
                next_dt = base_datetime + timedelta(days=interval)
                while next_dt <= from_dt:
                    next_dt += timedelta(days=interval)

        return next_dt

    @staticmethod
    def _add_months(source_date: datetime, months: int) -> datetime:
        month = source_date.month - 1 + months
        year = source_date.year + month // 12
        month = month % 12 + 1
        day = min(source_date.day, calendar.monthrange(year, month)[1])
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
            custom_fields=task.custom_fields,
            is_recurring=True,
            recurrence_rule=updated_rule,
        )

        return new_task.id

"""
Unit Tests for Recurring Task Calculations and Auto-Spawning
"""
from datetime import datetime
import pytest

from kanbanmanager import RecurrenceEngine
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager import DatabaseService
from kanbanmanager import RecurrenceFrequency


@pytest.fixture
def test_service(tmp_path):
    db_file = tmp_path / "test_recurrence.db"
    mgr = DatabaseManager(db_file)
    mgr.init_database(seed_defaults=False)
    return DatabaseService(mgr)


def test_calculate_next_due_date_daily():
    base = datetime(2026, 11, 10, 12, 0, 0)
    rule = {"frequency": RecurrenceFrequency.DAILY.value, "interval": 1}
    next_dt = RecurrenceEngine.calculate_next_due_date(base, rule, from_datetime=base)
    assert next_dt == datetime(2026, 11, 11, 12, 0, 0)


def test_calculate_next_due_date_weekdays():
    # Friday -> should advance to Monday
    friday = datetime(2026, 11, 13, 10, 0, 0)
    assert friday.weekday() == 4  # Friday
    rule = {"frequency": RecurrenceFrequency.WEEKDAYS.value}
    next_dt = RecurrenceEngine.calculate_next_due_date(friday, rule, from_datetime=friday)
    assert next_dt.weekday() == 0  # Monday
    assert next_dt.day == 16


def test_calculate_next_due_date_monthly():
    base = datetime(2026, 1, 15, 9, 0, 0)
    rule = {"frequency": RecurrenceFrequency.MONTHLY.value, "interval": 1}
    next_dt = RecurrenceEngine.calculate_next_due_date(base, rule, from_datetime=base)
    assert next_dt == datetime(2026, 2, 15, 9, 0, 0)


def test_recurring_task_completion_spawn(test_service):
    ws = test_service.create_workspace(name="Schedules")
    board = test_service.create_board(workspace_id=ws.id, name="Daily Ops")
    cols = test_service.get_columns_for_board(board.id)
    backlog_col = cols[0]

    rule = {"frequency": RecurrenceFrequency.DAILY.value, "interval": 1}
    initial_due = datetime(2026, 11, 8, 10, 0, 0)

    task = test_service.create_task(
        board_id=board.id,
        column_id=backlog_col.id,
        title="Daily Standup Notes",
        is_recurring=True,
        recurrence_rule=rule,
        due_date=initial_due,
        checklist=[{"id": 1, "text": "Post update", "done": True}],
    )

    # Spawn next recurring instance upon task completion
    new_task_id = RecurrenceEngine.handle_recurring_task_completion(test_service, task.id)
    assert new_task_id is not None

    new_task = test_service.get_task(new_task_id)
    assert new_task.title == "Daily Standup Notes"
    assert new_task.is_completed is False
    assert new_task.due_date > initial_due
    # Checklist items reset to not done
    assert new_task.checklist[0]["done"] is False


# ---------------------------------------------------------------------------
# Every rule type, end conditions, and the bugs these tests pinned down
# ---------------------------------------------------------------------------
from datetime import date, timedelta  # noqa: E402

MONDAY = datetime(2026, 11, 9, 9, 0)


def nxt(base, rule, now=None):
    return RecurrenceEngine.calculate_next_due_date(base, rule, from_datetime=now or base)


def series(base, rule, n):
    out, d = [], base
    for _ in range(n):
        d = nxt(d, rule)
        out.append(d)
    return out


def test_biweekly_and_every_n_weeks_skip_the_weeks_in_between():
    assert nxt(MONDAY, {"frequency": "BIWEEKLY"}) == MONDAY + timedelta(weeks=2), "was: the next Monday"
    rule = {"frequency": "WEEKLY", "interval": 2, "days_of_week": [0, 2]}  # Mon + Wed, every other week
    assert [d.date().isoformat() for d in series(MONDAY, rule, 4)] == \
        ["2026-11-11", "2026-11-23", "2026-11-25", "2026-12-07"]
    assert nxt(MONDAY, {"frequency": "CUSTOM", "custom_unit": "weeks", "interval": 3}) == MONDAY + timedelta(weeks=3)


def test_weekly_on_chosen_days_and_the_default_day():
    rule = {"frequency": "WEEKLY", "days_of_week": [1, 4]}  # Tue, Fri
    assert [d.strftime("%a %d") for d in series(MONDAY, rule, 4)] == ["Tue 10", "Fri 13", "Tue 17", "Fri 20"]
    assert nxt(MONDAY, {"frequency": "WEEKLY"}) == MONDAY + timedelta(weeks=1), "default: the same weekday"
    assert nxt(MONDAY, {"frequency": "WEEKLY", "days_of_week": [9, "x"][:1]}) == MONDAY + timedelta(weeks=1)


def test_monthly_rules_keep_their_day_of_the_month():
    jan31 = datetime(2026, 1, 31, 9)
    rule = {"frequency": "MONTHLY", "month_day": 31}
    assert [d.date().isoformat() for d in series(jan31, rule, 4)] == \
        ["2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"], "was: stuck on the 28th after February"
    quarterly = {"frequency": "CUSTOM", "custom_unit": "months", "interval": 3, "month_day": 30}
    assert [d.date().isoformat() for d in series(datetime(2025, 11, 30, 9), quarterly, 2)] == \
        ["2026-02-28", "2026-05-30"]
    assert nxt(datetime(2024, 1, 31), {"frequency": "MONTHLY"}) == datetime(2024, 2, 29), "leap years"


def test_end_date_is_checked_against_the_next_occurrence():
    rule = {"frequency": "DAILY", "end_type": "end_date", "end_date": "2026-12-31"}
    assert nxt(datetime(2026, 12, 30, 9), rule, datetime(2026, 12, 29)) == datetime(2026, 12, 31, 9), \
        "the end date itself is included"
    assert nxt(datetime(2026, 12, 31, 9), rule, datetime(2026, 12, 29)) is None, "was: Jan 1 was created"
    assert nxt(datetime(2026, 12, 31, 9), {**rule, "end_date": "garbage"}) is not None


def test_count_limit_and_rules_that_do_not_recur():
    rule = {"frequency": "DAILY", "end_type": "after_count", "max_occurrences": 2, "occurrences_count": 2}
    assert nxt(MONDAY, rule) is None
    assert nxt(MONDAY, {**rule, "occurrences_count": 1}) == MONDAY + timedelta(days=1)
    assert nxt(MONDAY, {}) is None and nxt(MONDAY, {"frequency": ""}) is None


def test_catching_up_from_an_old_due_date_lands_in_the_future():
    old = datetime(2020, 3, 2, 9)  # a Monday years ago
    now = datetime(2026, 11, 10, 12)
    for rule in ({"frequency": "DAILY"}, {"frequency": "WEEKDAYS"}, {"frequency": "BIWEEKLY"},
                 {"frequency": "MONTHLY"}, {"frequency": "CUSTOM", "custom_unit": "weeks", "interval": 3}):
        d = nxt(old, rule, now)
        assert now < d <= now + timedelta(days=31), rule
        assert d.time() == old.time(), "the time of day is kept"
    d = nxt(old, {"frequency": "BIWEEKLY"}, now)
    assert (d.date() - old.date()).days % 14 == 0, "the bi-weekly rhythm is kept while catching up"
    assert nxt(old, {"frequency": "WEEKDAYS"}, now).weekday() < 5


def test_spawned_monthly_task_remembers_its_day(test_service):
    ws = test_service.create_workspace(name="W")
    board = test_service.create_board(workspace_id=ws.id, name="B")
    col = test_service.get_columns_for_board(board.id)[0]
    year = date.today().year + 1  # in the future: completing spawns the next one after *now*
    task = test_service.create_task(board.id, col.id, "Invoice", is_recurring=True, due_date=datetime(year, 1, 31, 9),
                                    recurrence_rule={"frequency": "MONTHLY", "interval": 1})
    second = test_service.get_task(RecurrenceEngine.handle_recurring_task_completion(test_service, task.id))
    feb_end = 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28
    assert second.due_date.date() == date(year, 2, feb_end) and second.recurrence_rule["month_day"] == 31
    third = test_service.get_task(RecurrenceEngine.handle_recurring_task_completion(test_service, second.id))
    assert third.due_date.date() == date(year, 3, 31)
    assert third.recurrence_rule["occurrences_count"] == 2


def test_spawned_tasks_keep_duration_labels_and_land_in_an_open_column(test_service):
    ws = test_service.create_workspace(name="W")
    board = test_service.create_board(workspace_id=ws.id, name="B")
    cols = test_service.get_columns_for_board(board.id)
    label = test_service.create_label(board.id, "Ops", "#EF4444")
    due = datetime.combine(date.today() + timedelta(days=14), datetime.min.time()).replace(hour=17)
    task = test_service.create_task(board.id, cols[-1].id, "Backup check", is_recurring=True,
                                    start_date=due.date() - timedelta(days=3), due_date=due,
                                    recurrence_rule={"frequency": "WEEKLY"}, label_ids=[label.id],
                                    assignee="Ann", priority="HIGH")
    new = test_service.get_task(RecurrenceEngine.handle_recurring_task_completion(test_service, task.id))
    assert new.column_id == cols[0].id, "not the done column"
    assert new.due_date == due + timedelta(weeks=1), "a week later, same time"
    assert new.start_date == new.due_date.date() - timedelta(days=3), "the same 3-day span"
    assert [lbl.name for lbl in new.labels] == ["Ops"] and new.assignee == "Ann" and new.priority == "HIGH"
    plain = test_service.create_task(board.id, cols[0].id, "One-off")
    assert RecurrenceEngine.handle_recurring_task_completion(test_service, plain.id) is None
    assert RecurrenceEngine.handle_recurring_task_completion(test_service, 99999) is None


hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st  # noqa: E402

_rules = st.one_of(
    st.fixed_dictionaries({"frequency": st.sampled_from(["DAILY", "WEEKDAYS", "BIWEEKLY", "MONTHLY"]),
                           "interval": st.integers(1, 4)}),
    st.fixed_dictionaries({"frequency": st.just("WEEKLY"), "interval": st.integers(1, 4),
                           "days_of_week": st.lists(st.integers(0, 6), min_size=1, max_size=7)}),
    st.fixed_dictionaries({"frequency": st.just("CUSTOM"), "interval": st.integers(1, 6),
                           "custom_unit": st.sampled_from(["days", "weeks", "months"])}),
)


@settings(max_examples=200, deadline=None)
@given(st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2090, 1, 1)), _rules,
       st.integers(-400, 400))
def test_next_occurrence_properties(base, rule, offset_days):
    now = base + timedelta(days=offset_days)
    d = nxt(base, rule, now)
    assert d is not None and d > base and d > now, "always strictly after the base and now"
    assert d.time() == base.time()
    if rule["frequency"] == "WEEKDAYS":
        assert d.weekday() < 5
    if rule["frequency"] == "WEEKLY":
        assert d.weekday() in set(rule["days_of_week"])
        weeks = ((d.date() - timedelta(days=d.weekday())) - (base.date() - timedelta(days=base.weekday()))).days // 7
        assert weeks % rule["interval"] == 0, "only in the active weeks"
    if rule["frequency"] == "MONTHLY" or rule.get("custom_unit") == "months":
        import calendar
        assert d.day == min(base.day, calendar.monthrange(d.year, d.month)[1])

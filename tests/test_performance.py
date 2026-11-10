"""
Performance regression tests.

Most checks here are *deterministic*: the number of SQL statements must not grow
with the number of tasks (N+1 guards), unchanged cards are reused instead of
rebuilt, layouts cache their measurements, the board loads once at start-up and
re-applying the current theme touches nothing.  A few wall-clock budgets (marked
``perf``) use generous limits scaled by ``KANBAN_PERF_FACTOR`` (default 1.0;
raise it on slow CI machines) so they only catch order-of-magnitude regressions.
"""
from __future__ import annotations

import os
import time

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QLabel, QWidget

from kanbanmanager.ui import theme as th
from kanbanmanager.ui.components.flow_layout import FlowLayout
from kanbanmanager.ui.components.task_card import TaskCardWidget
from kanbanmanager.ui.views.board_view import BoardView
from support import count_queries, flush_qt, make_board

PERF_FACTOR = float(os.environ.get("KANBAN_PERF_FACTOR", "1.0"))


def budget(seconds: float) -> float:
    return seconds * PERF_FACTOR


@pytest.fixture
def service(empty_service):
    return empty_service


def _queries(service, fn):
    with count_queries(service.db) as log:
        fn()
    return log


# ---------------------------------------------------------------------------
# N+1 guards: statement counts independent of board size
# ---------------------------------------------------------------------------
def test_get_board_query_count_does_not_grow_with_tasks(service):
    small = make_board(service, "Small", tasks=4, dependencies=2)
    large = make_board(service, "Large", tasks=60, dependencies=30)
    q_small = _queries(service, lambda: service.get_board(small.id))
    q_large = _queries(service, lambda: service.get_board(large.id))
    assert q_large.count == q_small.count, q_large
    assert q_large.count <= 12, q_large
    board = service.get_board(large.id)  # detached, but everything the UI reads is loaded
    blocked = [t for c in board.columns for t in c.tasks if t.is_blocked]
    assert blocked and all(t.labels is not None for c in board.columns for t in c.tasks)


def test_get_tasks_for_board_query_count_does_not_grow(service):
    small = make_board(service, "Small", tasks=4, dependencies=2)
    large = make_board(service, "Large", tasks=60, dependencies=30)
    q_small = _queries(service, lambda: [t.to_dict() for t in service.get_tasks_for_board(small.id)])
    q_large = _queries(service, lambda: [t.to_dict() for t in service.get_tasks_for_board(large.id)])
    assert q_large.count == q_small.count, q_large


def test_board_view_refresh_query_count_does_not_grow(qapp, service):
    small = make_board(service, "Small", tasks=5, dependencies=2)
    large = make_board(service, "Large", tasks=80, dependencies=40)
    view = BoardView(service)
    counts = []
    for built in (small, large):
        view.load_board(built.id)
        counts.append(_queries(service, view.refresh))
    assert counts[1].count == counts[0].count, counts[1]
    assert counts[1].count <= 12, counts[1]


def test_main_window_refresh_query_count_does_not_grow(qapp, service):
    from kanbanmanager.ui import MainWindow

    small = make_board(service, "Small", tasks=5, dependencies=2)
    large = make_board(service, "Large", tasks=80, dependencies=40)
    win = MainWindow(service=service)
    counts = []
    for built in (small, large):
        win.open_board(built.id)
        counts.append(_queries(service, win.refresh_active_view))
    assert counts[1].count == counts[0].count, counts[1]


HOT_INDEXES = {"idx_tasks_board", "idx_tasks_column", "idx_columns_board", "idx_labels_board",
               "idx_boards_workspace", "idx_task_dep_prereq", "idx_task_labels_label", "idx_reminders_task",
               "idx_activity_board_time"}


def _index_names(path):
    import sqlite3
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    finally:
        con.close()


def test_hot_foreign_keys_are_indexed_and_used(service):
    assert HOT_INDEXES <= _index_names(service.db.db_path)
    from sqlalchemy import text
    with service.db.engine.connect() as con:
        plans = {
            "tasks of a board": "SELECT * FROM tasks WHERE board_id = 1 AND is_archived = 0",
            "cards of a column": "SELECT * FROM tasks WHERE column_id = 1 ORDER BY position",
            "what a task blocks": "SELECT * FROM task_dependencies WHERE depends_on_task_id = 1",
            "tasks with a label": "SELECT * FROM task_labels WHERE label_id = 1",
        }
        for what, sql in plans.items():
            plan = " ".join(str(r[-1]) for r in con.execute(text("EXPLAIN QUERY PLAN " + sql)))
            assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, f"{what}: {plan}"


def test_older_databases_get_the_indexes_on_upgrade(tmp_path):
    import sqlite3
    from kanbanmanager.database.connection import DatabaseManager

    path = tmp_path / "old.db"
    DatabaseManager(path).init_database(seed_defaults=False)
    con = sqlite3.connect(path)
    for name in HOT_INDEXES:
        con.execute(f"DROP INDEX {name}")
    con.commit()
    con.close()
    assert not HOT_INDEXES & _index_names(path)
    DatabaseManager(path).init_database(seed_defaults=False)
    assert HOT_INDEXES <= _index_names(path)


# ---------------------------------------------------------------------------
# Start-up does the work once
# ---------------------------------------------------------------------------
def test_startup_loads_the_board_once(qapp, service, monkeypatch):
    from kanbanmanager.ui import MainWindow

    make_board(service, "Only", tasks=6)
    loads = []
    original = BoardView.refresh
    monkeypatch.setattr(BoardView, "refresh", lambda self, *a, **k: loads.append(self.current_board_id) or original(self, *a, **k))
    win = MainWindow(service=service)
    flush_qt()
    assert len(loads) == 1, f"board view refreshed {len(loads)} times while starting"
    assert win.current_board_id == loads[0]


# ---------------------------------------------------------------------------
# Card reuse
# ---------------------------------------------------------------------------
def _cards(view):
    return {card.task_id: card for col in view._columns() for card in col.cards()}


def test_refresh_reuses_cards_of_unchanged_tasks(qapp, service):
    built = make_board(service, "Reuse", tasks=12)
    view = BoardView(service)
    view.load_board(built.id)
    before = _cards(view)
    assert len(before) == 12
    changed, moved, deleted = built.tasks[0], built.tasks[1], built.tasks[2]
    service.update_task(changed, title="Renamed")
    service.move_task(moved, built.columns[4], 0)
    service.delete_task(deleted)
    view.refresh()
    flush_qt()
    after = _cards(view)
    assert set(after) == set(before) - {deleted}
    untouched = set(before) - {changed, moved, deleted}
    assert all(after[i] is before[i] for i in untouched), "unchanged cards are the same widgets"
    assert after[changed] is not before[changed] and after[changed].task_data["title"] == "Renamed"
    assert after[moved].column_id == built.columns[4]
    assert view._columns()[4].cards()[0].task_id == moved, "the moved card sits where it was dropped"
    # every card is shown in database order in its column
    for col in view._columns():
        ids = [c.task_id for c in col.cards()]
        assert ids == col.task_ids
        assert all(c.isVisibleTo(view) for c in col.cards())


def test_filtering_hides_and_restores_the_same_cards(qapp, service):
    built = make_board(service, "Filter", tasks=15)
    view = BoardView(service)
    view.resize(1600, 900)
    view.show()
    view.load_board(built.id)
    before = _cards(view)
    view.apply_filters({"search": "Task 0001"})
    flush_qt()
    shown = [c for c in view.findChildren(TaskCardWidget) if c.isVisibleTo(view)]
    assert [c.task_data["title"] for c in shown] == ["Task 0001"]
    assert sum(len(col.cards()) for col in view._columns()) == 1
    view.apply_filters({})
    flush_qt()
    after = _cards(view)
    assert all(after[i] is before[i] for i in before), "clearing the filter brings back the same widgets"
    assert len([c for c in view.findChildren(TaskCardWidget) if c.isVisibleTo(view)]) == 15


def test_cards_rebuild_when_the_day_changes(qapp, service, monkeypatch):
    """Due-date wording ("today", "overdue") depends on the date, so a new day rebuilds."""
    import kanbanmanager.ui.components.column_widget as cw

    built = make_board(service, "Days", tasks=3, with_dates=True)
    view = BoardView(service)
    view.load_board(built.id)
    before = _cards(view)
    real = cw.card_day

    monkeypatch.setattr(cw, "card_day", lambda: real() + 1)
    view.refresh()
    after = _cards(view)
    assert all(after[i] is not before[i] for i in before)


def test_column_widgets_are_reused_until_the_column_changes(qapp, service):
    built = make_board(service, "Cols", tasks=4)
    view = BoardView(service)
    view.load_board(built.id)
    first = dict(view.column_widgets)
    view.refresh()
    assert all(view.column_widgets[k] is first[k] for k in first)
    service.update_column(built.columns[0], name="Ideas")
    view.refresh()
    assert view.column_widgets[built.columns[0]] is not first[built.columns[0]]
    assert view.column_widgets[built.columns[0]].title_lbl.text() == "Ideas"
    assert view.column_widgets[built.columns[1]] is first[built.columns[1]]
    service.create_column(built.id, "Extra")
    view.refresh()
    assert len(view._columns()) == 6 and view._columns()[-1].title_lbl.text() == "Extra"


# ---------------------------------------------------------------------------
# Layout and theming
# ---------------------------------------------------------------------------
def test_flow_layout_caches_height_for_width(qapp, monkeypatch):
    host = QWidget()
    layout = FlowLayout(host)
    for i in range(6):
        layout.addWidget(QLabel(f"badge {i}"))
    calls = []
    original = FlowLayout._do_layout
    monkeypatch.setattr(FlowLayout, "_do_layout", lambda self, r, test_only: calls.append(test_only) or original(self, r, test_only))
    h = layout.heightForWidth(120)
    for _ in range(20):
        assert layout.heightForWidth(120) == h
    assert calls.count(True) == 1, "repeated measurements at one width are cached"
    narrow = layout.heightForWidth(40)
    assert narrow > h, "narrower means more lines"
    layout.addWidget(QLabel("one more"))
    layout.invalidate()
    layout.heightForWidth(120)
    assert calls.count(True) == 3, "adding an item invalidates the cache"
    layout.setGeometry(QRect(0, 0, 120, 200))
    host.deleteLater()


def test_reapplying_the_current_theme_touches_no_widgets(qapp, monkeypatch):
    th.install()
    label = QLabel("x")
    label.setStyleSheet("color: #F8FAFC;")
    touched = []
    monkeypatch.setattr(th, "_original_set_style_sheet",
                        lambda w, s, real=th._original_set_style_sheet: touched.append(w) or real(w, s))
    th.set_theme(th.builtin_theme("dark"), 100)
    assert touched == [], "nothing changes, nothing is restyled"
    th.set_theme(th.builtin_theme("light"), 100)
    assert label in touched
    label.deleteLater()


# ---------------------------------------------------------------------------
# Wall-clock budgets (order-of-magnitude guards)
# ---------------------------------------------------------------------------
@pytest.mark.perf
@pytest.mark.slow
def test_large_board_budgets(qapp, service):
    from kanbanmanager.ui import MainWindow

    built = make_board(service, "Big", tasks=300, dependencies=40, with_dates=True)
    win = MainWindow(service=service)
    win.resize(1400, 850)
    win.show()
    flush_qt()

    t = time.perf_counter()
    win.open_board(built.id)
    flush_qt()
    open_s = time.perf_counter() - t

    t = time.perf_counter()
    win.refresh_active_view()
    flush_qt()
    refresh_s = time.perf_counter() - t

    t = time.perf_counter()
    for text in ("T", "Ta", "Task 00"):
        win.board_view.apply_filters({"search": text})
    flush_qt()
    search_s = (time.perf_counter() - t) / 3

    views = {}
    for mode in ("CALENDAR", "TIMELINE", "ANALYTICS", "WORKLOAD", "BOARD"):
        t = time.perf_counter()
        win._on_view_mode_changed(mode)
        flush_qt()
        views[mode] = time.perf_counter() - t

    report = (f"open {open_s:.2f}s, refresh {refresh_s:.2f}s, search {search_s:.2f}s/keystroke, "
              + ", ".join(f"{k.lower()} {v:.2f}s" for k, v in views.items()))
    print(report)
    assert open_s < budget(4.0), report
    assert refresh_s < budget(1.0), report
    assert search_s < budget(1.0), report
    assert max(views.values()) < budget(4.0), report


@pytest.mark.perf
@pytest.mark.slow
def test_bulk_service_operations_budget(service):
    t = time.perf_counter()
    built = make_board(service, "Bulk", tasks=200)
    create_s = time.perf_counter() - t
    t = time.perf_counter()
    for task_id in built.tasks[:50]:
        service.move_task(task_id, built.columns[3], 0)
    move_s = time.perf_counter() - t
    t = time.perf_counter()
    for _ in range(10):
        service.search_tasks("Task 01", limit=20)
    search_s = time.perf_counter() - t
    report = f"create 200: {create_s:.2f}s, move 50: {move_s:.2f}s, search x10: {search_s:.2f}s"
    print(report)
    assert create_s < budget(6.0), report
    assert move_s < budget(3.0), report
    assert search_s < budget(1.0), report

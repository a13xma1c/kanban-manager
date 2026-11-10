"""
UI Integration Tests for MainWindow, Views, and Widgets
"""
import pytest
from PySide6.QtWidgets import QApplication

from kanbanmanager import DatabaseService
from kanbanmanager.ui import MainWindow
from kanbanmanager import ViewMode
from support import seeded_manager


@pytest.fixture
def main_window(qapp, tmp_path):
    db_file = tmp_path / "ui_test.db"
    mgr = seeded_manager(db_file)
    service = DatabaseService(mgr)

    win = MainWindow(service=service)
    win.show()
    return win


def test_main_window_init_and_views(main_window):
    assert main_window.current_board_id is not None
    assert main_window.view_stack.count() == 5

    # Test View Switching
    main_window._on_view_mode_changed(ViewMode.BOARD.value)
    assert main_window.view_stack.currentIndex() == 0

    main_window._on_view_mode_changed(ViewMode.CALENDAR.value)
    assert main_window.view_stack.currentIndex() == 1

    main_window._on_view_mode_changed(ViewMode.TIMELINE.value)
    assert main_window.view_stack.currentIndex() == 2

    main_window._on_view_mode_changed(ViewMode.ANALYTICS.value)
    assert main_window.view_stack.currentIndex() == 3

    main_window._on_view_mode_changed(ViewMode.WORKLOAD.value)
    assert main_window.view_stack.currentIndex() == 4


def test_filter_bar_and_search(main_window):
    main_window._on_view_mode_changed(ViewMode.BOARD.value)
    main_window.filter_bar.search_input.setText("Architecture")
    
    # Verify filter applied without error
    main_window.filter_bar.clear_filters()
    assert main_window.filter_bar.search_input.text() == ""


def test_task_interaction(main_window):
    tasks = main_window.service.get_tasks_for_board(main_window.current_board_id)
    assert len(tasks) > 0

    first_task = tasks[0]
    initial_completed = first_task.is_completed

    # Toggle task completion
    main_window._on_task_completed_toggled(first_task.id, not initial_completed)
    updated = main_window.service.get_task(first_task.id)
    assert updated.is_completed != initial_completed


@pytest.mark.parametrize("window_width", ["minimum", 1280, 1600])
def test_filter_bar_controls_fit_without_overlap(main_window, window_width):
    """Search, filters and view switcher must never overlap or be squashed, down to the minimum window width."""
    if window_width == "minimum":
        window_width = main_window.minimumWidth()
    main_window.resize(window_width, 800)
    QApplication.processEvents()
    bar = main_window.filter_bar
    widgets = [
        bar.search_input, bar.priority_combo, bar.label_combo, bar.due_combo, bar.status_combo,
        bar.reset_btn, bar.btn_board, bar.btn_calendar, bar.btn_timeline, bar.btn_analytics, bar.btn_workload,
    ]
    rects = [w.geometry().translated(w.parentWidget().mapTo(bar, w.parentWidget().rect().topLeft())) for w in widgets]
    for i, a in enumerate(rects):
        assert bar.rect().contains(a), f"{widgets[i].__class__.__name__} sticks out of the filter bar"
        for j in range(i + 1, len(rects)):
            assert not a.intersects(rects[j]), f"widgets {i} and {j} overlap at {window_width}px"

    for btn in (bar.btn_board, bar.btn_calendar, bar.btn_timeline, bar.btn_analytics, bar.btn_workload):
        assert btn.width() >= btn.sizeHint().width(), f"'{btn.text()}' is truncated"
    for combo in (bar.priority_combo, bar.label_combo, bar.due_combo, bar.status_combo):
        assert combo.width() >= combo.fontMetrics().horizontalAdvance(combo.itemText(0)) + 40

    # The stats bar below must not truncate its labels either (it switches to compact wording)
    main_window.stats_bar.set_automation_counts(4, 2, 13)
    QApplication.processEvents()
    stats = main_window.stats_bar
    for w in (stats.total_lbl, stats.wip_lbl, stats.completed_lbl, stats.overdue_lbl, stats.blocked_lbl,
              stats.automations_btn, stats.archive_btn):
        assert w.width() >= w.sizeHint().width() - 1, f"'{w.text()}' is truncated at {window_width}px"

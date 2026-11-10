"""
UI tests for the Gantt view (rendering model, drag to move/resize/link, context
actions), the scheduling dialogs, milestone editing, and main-window cascading.
"""
from datetime import timedelta

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QPushButton, QSpinBox

from kanbanmanager import DatabaseService
from kanbanmanager.core import scheduling as sch
from kanbanmanager.core.schedule_engine import ScheduleEngine
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.enums import ViewMode
from kanbanmanager.ui import MainWindow
from kanbanmanager.ui.components.task_card import TaskCardWidget
from kanbanmanager.ui.dialogs.schedule_dialogs import (
    AutoScheduleDialog,
    BaselinesDialog,
    DependencyDialog,
    ScheduleSettingsDialog,
)
from kanbanmanager.ui.dialogs.task_dialog import TaskDialog
from kanbanmanager.ui.views.gantt_view import GanttView
from support import seeded_manager


@pytest.fixture
def demo(qapp, tmp_path):
    mgr = seeded_manager(tmp_path / "gantt.db")
    service = DatabaseService(mgr, actor="Ann")
    board = [b for b in service.get_workspaces()[0].boards if b.name == DEMO_BOARD_NAME][0]
    engine = ScheduleEngine(service)
    engine.attach()
    tasks = {t.title: t for t in service.get_tasks_for_board(board.id)}
    return service, board, engine, tasks


def gantt(service, board, engine):
    view = GanttView(service, engine=engine)
    view.resize(1400, 800)
    view.show()
    view.load_board(board.id)
    QApplication.processEvents()
    return view


def bar_point(view, title, where="body"):
    i = next(i for i, r in enumerate(view.model.rows) if r.task.title == title)
    rect = view.model.bar_rect(i)
    if where == "end":
        return QPoint(int(rect.right() - 2), int(rect.center().y()))
    if where == "handle":
        return QPoint(int(rect.right() + 9), int(rect.center().y()))
    return QPoint(int(rect.left() + 6), int(rect.center().y()))


def drag(widget, start, end):
    QTest.mousePress(widget, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(widget, end)
    QTest.mouseRelease(widget, Qt.LeftButton, Qt.NoModifier, end)


# ---------------------------------------------------------------------------
# Rendering model
# ---------------------------------------------------------------------------
def test_gantt_model_critical_path_conflicts_and_baseline(demo):
    service, board, engine, tasks = demo
    view = gantt(service, board, engine)
    titles = [r.task.title for r in view.model.rows]
    assert "Publish the Q3 performance report" not in titles, "completed tasks without dates are hidden"
    assert view.table.rowCount() == len(view.model.rows)
    critical = {r.task.title for r in view.model.rows if r.critical}
    assert {"Crash when importing large CSV files", "Migrate attachment storage to the CDN",
            "🏁 General availability"} <= critical
    assert (tasks["Crash when importing large CSV files"].id, tasks["Write API docs for webhooks"].id) \
        in view.model.conflicts
    assert any(r.baseline for r in view.model.rows), "the demo shows its baseline"
    text = view.summary_lbl.text()
    assert "critical" in text and "dependency conflict" in text and "vs baseline: finish +1 days" in text
    placed = [r for r in view.model.rows if r.placed_by_schedule]
    assert placed and all(view.table.item(view.model.row_index(r.task.id), 1).text().startswith("~")
                          for r in placed)
    view.grab()  # paints header, bars, links, baselines without errors


def test_zoom_and_display_options(demo):
    service, board, engine, _ = demo
    view = gantt(service, board, engine)
    day_width = view.canvas.width()
    view.zoom_combo.setCurrentIndex(2)  # months
    assert view.model.day_w == 5 and view.canvas.width() < day_width
    view.critical_chk.setChecked(False)
    assert view.model.show_critical is False
    view.baseline_combo.setCurrentIndex(0)  # (none)
    assert sch.normalize_schedule_settings(service.get_schedule_settings(board.id))["baseline_id"] is None
    assert not any(r.baseline for r in view.model.rows)


# ---------------------------------------------------------------------------
# Mouse interaction
# ---------------------------------------------------------------------------
def test_drag_moves_task_and_dependents_cascade(demo):
    service, board, engine, tasks = demo
    view = gantt(service, board, engine)
    crash = tasks["Crash when importing large CSV files"]
    cdn_before = service.get_task(tasks["Migrate attachment storage to the CDN"].id)
    start = bar_point(view, crash.title)
    drag(view.canvas, start, start + QPoint(view.model.day_w * 7, 0))   # one calendar week later
    moved = service.get_task(crash.id)
    cal = view.model.calendar
    assert moved.start_date == crash.start_date + timedelta(days=7)
    assert cal.span(moved.start_date, moved.due_date.date()) == cal.span(crash.start_date, crash.due_date.date())
    cdn = service.get_task(cdn_before.id)
    assert cdn.start_date > cdn_before.start_date, "the critical successor cascaded"
    beta = service.get_task(tasks["🚀 Public beta"].id)
    assert beta.due_date == tasks["🚀 Public beta"].due_date, "locked milestone never moves"


def test_drag_right_edge_resizes(demo):
    service, board, engine, tasks = demo
    view = gantt(service, board, engine)
    docs = tasks["Write API docs for webhooks"]
    p = bar_point(view, docs.title, "end")
    drag(view.canvas, p, p + QPoint(view.model.day_w * 3, 0))
    updated = service.get_task(docs.id)
    assert updated.start_date == docs.start_date and updated.due_date.date() > docs.due_date.date()
    assert updated.due_date.time() == docs.due_date.time(), "due time of day kept"


def test_drag_handle_links_tasks_and_locked_bars_do_not_move(demo):
    service, board, engine, tasks = demo
    view = gantt(service, board, engine)
    sync, docs = tasks["Weekly team sync"], tasks["Write API docs for webhooks"]
    drag(view.canvas, bar_point(view, docs.title, "handle"), bar_point(view, sync.title))
    assert any(d["task_id"] == sync.id and d["depends_on_task_id"] == docs.id
               for d in service.get_board_dependencies(board.id))
    beta = tasks["🚀 Public beta"]
    p = bar_point(view, beta.title)
    drag(view.canvas, p, p + QPoint(view.model.day_w * 5, 0))
    assert service.get_task(beta.id).due_date == beta.due_date


def test_context_actions(demo):
    service, board, engine, tasks = demo
    view = gantt(service, board, engine)
    sync = tasks["Weekly team sync"]
    view.toggle_milestone(sync.id)
    t = service.get_task(sync.id)
    assert t.is_milestone and t.start_date == t.due_date.date()
    view.toggle_milestone(sync.id)
    assert not service.get_task(sync.id).is_milestone
    view.toggle_lock(sync.id)
    assert service.get_task(sync.id).dates_locked
    view.toggle_lock(sync.id)
    view.clear_dates(sync.id)
    assert service.get_task(sync.id).due_date is None
    row = next(r for r in view.model.rows if r.task.id == sync.id)
    assert row.placed_by_schedule, "now shown where the schedule would place it"


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------
def test_auto_schedule_dialog_applies_only_ticked_changes(demo):
    service, board, engine, tasks = demo
    dlg = AutoScheduleDialog(engine, board.id)
    titles = [c.title for c in dlg.proposal.changes]
    assert "Write API docs for webhooks" in titles and len(titles) >= 2
    for r, ch in enumerate(dlg.proposal.changes):
        if ch.title != "Write API docs for webhooks":
            dlg.table.item(r, 0).setCheckState(Qt.Unchecked)
    dlg.apply()
    assert dlg.applied == 1
    assert engine.analyze(board.id).conflicts == [], "the conflict is fixed"
    assert service.get_task(tasks["👋 Start here: take the feature tour"].id).due_date is None, "unticked: unchanged"
    dlg2 = AutoScheduleDialog(engine, board.id)
    dlg2.unscheduled_chk.setChecked(False)
    assert dlg2.proposal.changes == [] and "nothing to change" in dlg2.summary_lbl.text()


def test_baselines_dialog(demo):
    service, board, engine, _ = demo
    dlg = BaselinesDialog(engine, board.id)
    assert dlg.list.rowCount() == 1 and "+1 working days" in dlg.summary_lbl.text()
    assert dlg.variance.rowCount() >= 5
    new = dlg.save_new("After re-plan")
    assert dlg.list.rowCount() == 2 and new["task_count"] >= 8
    assert "+0 working days" in dlg.summary_lbl.text()
    dlg.show_selected()
    assert sch.normalize_schedule_settings(service.get_schedule_settings(board.id))["baseline_id"] == new["id"]
    dlg.delete_selected(confirm=False)
    assert dlg.list.rowCount() == 1


def test_schedule_settings_dialog(demo):
    service, board, engine, _ = demo
    dlg = ScheduleSettingsDialog(service, board.id)
    for box in dlg.day_boxes:
        box.setChecked(False)
    dlg.save()
    assert dlg.result() == 0 and "at least one" in dlg.error_lbl.text()
    for i in (0, 1, 2, 3):
        dlg.day_boxes[i].setChecked(True)
    dlg.hours.setValue(6)
    dlg.cascade_chk.setChecked(False)
    dlg.save()
    s = sch.normalize_schedule_settings(service.get_schedule_settings(board.id))
    assert s["working_days"] == [0, 1, 2, 3] and s["hours_per_day"] == 6 and s["cascade"] is False
    assert s["baseline_id"] is not None, "other settings are preserved"


def test_dependency_dialog_edits_type_lag_and_links(demo):
    service, board, engine, tasks = demo
    launch = tasks["Prepare launch screenshots & store listing"]
    dlg = DependencyDialog(service, board.id, launch.id)
    assert dlg.table.rowCount() == 1
    combo: QComboBox = dlg.table.cellWidget(0, 1)
    spin: QSpinBox = dlg.table.cellWidget(0, 2)
    assert combo.currentData() == "START_TO_START" and spin.value() == 2
    combo.setCurrentIndex(combo.findData("FINISH_TO_START"))
    spin.setValue(0)
    dep = next(d for d in service.get_board_dependencies(board.id) if d["task_id"] == launch.id)
    assert (dep["dependency_type"], dep["lag_days"]) == ("FINISH_TO_START", 0)
    dlg.add_combo.setCurrentIndex(dlg.add_combo.findText("Weekly team sync"))
    dlg.add_selected()
    assert dlg.table.rowCount() == 2
    remove: QPushButton = dlg.table.cellWidget(1, 3)
    remove.click()
    assert dlg.table.rowCount() == 1


def test_task_dialog_milestone_checkbox_and_card_badges(demo):
    service, board, engine, tasks = demo
    sync = tasks["Weekly team sync"]
    dlg = TaskDialog(board_id=board.id, task_id=sync.id, service=service)
    assert not dlg.milestone_chk.isChecked()
    dlg.milestone_chk.setChecked(True)
    dlg.lock_chk.setChecked(True)
    dlg._save_task()
    t = service.get_task(sync.id)
    assert t.is_milestone and t.dates_locked and t.start_date == t.due_date.date()
    card = TaskCardWidget(t.to_dict())
    texts = [l.text() for l in card.findChildren(QLabel)]
    assert "◆ Milestone" in texts and "📌" in texts


def test_main_window_gantt_and_cascade_feedback(demo):
    service, board, fixture_engine, tasks = demo
    fixture_engine.detach()  # the window attaches its own scheduler
    win = MainWindow(service=service)
    win.sidebar.select_board(board.id)
    win._on_board_selected(board.id)
    win._on_view_mode_changed(ViewMode.TIMELINE.value)
    assert win.view_stack.currentWidget() is win.timeline_view
    assert isinstance(win.timeline_view, GanttView) and win.filter_bar.btn_timeline.text() == "📈 Gantt"
    crash = tasks["Crash when importing large CSV files"]
    win.timeline_view.move_task(crash.id, crash.start_date + timedelta(days=7),
                                crash.due_date.date() + timedelta(days=7))
    assert "Shifted" in win.status_bar.currentMessage()
    win.time_tracker.shutdown()

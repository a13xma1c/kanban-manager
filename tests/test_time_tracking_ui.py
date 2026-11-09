"""
UI tests for the Time tab, status-bar timer, Workload view, Team dialog,
Timesheet dialog, and main-window timer integration.
"""
import os
from datetime import date, datetime, timedelta

import pytest
from PySide6.QtCore import QEvent, QCoreApplication
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel

from kanbanmanager import DatabaseService
from kanbanmanager.core.time_tracker import TimeTracker
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.enums import ViewMode
from kanbanmanager.ui import MainWindow
from kanbanmanager.ui.components.task_card import TaskCardWidget
from kanbanmanager.ui.components.time_panel import TimePanel, manual_start_time
from kanbanmanager.ui.components.timer_widget import TimerWidget
from kanbanmanager.ui.dialogs.task_dialog import TaskDialog
from kanbanmanager.ui.dialogs.timesheet_dialog import TimesheetDialog
from kanbanmanager.ui.views.workload_view import TeamDialog, WorkloadView


@pytest.fixture(scope="module")
def qapp():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    return QApplication.instance() or QApplication([])


@pytest.fixture
def demo(qapp, tmp_path):
    mgr = DatabaseManager(tmp_path / "tt_ui.db")
    mgr.init_database(seed_defaults=True)
    service = DatabaseService(mgr, actor="Marcus Chen")
    board = [b for b in service.get_workspaces()[0].boards if b.name == DEMO_BOARD_NAME][0]
    tasks = {t.title: t for t in service.get_tasks_for_board(board.id)}
    return service, board, tasks


class Clock:
    def __init__(self):
        self.t = datetime(2030, 1, 7, 9, 0)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def flush():
    for _ in range(3):
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


# ---------------------------------------------------------------------------
# Time tab
# ---------------------------------------------------------------------------
def test_time_panel_manual_logging_and_own_entries_only(demo):
    service, board, tasks = demo
    crash = tasks["Crash when importing large CSV files"]
    panel = TimePanel(service, crash.id)
    changes = []
    panel.time_changed.connect(lambda: changes.append(1))
    assert "Logged 2h 30m of 5h estimate" in panel.summary_lbl.text()

    panel.duration_input.setText("soon")
    panel.log_manual()
    assert "Enter a duration" in panel.error_lbl.text() and not changes

    panel.duration_input.setText("45m")
    panel.note_input.setText("Regression test")
    panel.log_manual()
    assert changes and panel.duration_input.text() == ""
    assert "Logged 3h 15m of 5h" in panel.summary_lbl.text()
    newest = service.get_time_entries(task_id=crash.id)[-1]
    assert (newest["duration_seconds"], newest["note"], newest["user"]) == (2700, "Regression test", "Marcus Chen")

    # Own entry (row 0 = newest) can be deleted; someone else's cannot
    panel.table.selectRow(0)
    panel.delete_selected(confirm=False)
    assert len(service.get_time_entries(task_id=crash.id)) == 2
    onboarding = TimePanel(service, tasks["Design the new onboarding flow"].id)  # Elena's entries
    onboarding.table.selectRow(0)
    before = len(service.get_time_entries(task_id=onboarding.task_id))
    onboarding.delete_selected(confirm=False)
    assert len(service.get_time_entries(task_id=onboarding.task_id)) == before


def test_manual_start_time():
    now = datetime(2024, 6, 5, 15, 0)
    assert manual_start_time(date(2024, 6, 5), 3600, now) == manual_start_time(date(2024, 6, 5), 3600, now)
    from kanbanmanager.timefmt import utc_to_local
    assert utc_to_local(manual_start_time(date(2024, 6, 5), 3600, now)) == datetime(2024, 6, 5, 14, 0)
    assert utc_to_local(manual_start_time(date(2024, 6, 1), 3600, now)) == datetime(2024, 6, 1, 9, 0)


def test_time_panel_live_timer(demo):
    service, board, tasks = demo
    task = tasks["Write API docs for webhooks"]
    clock = Clock()
    tracker = TimeTracker(service, now=clock)
    panel = TimePanel(service, task.id, tracker=tracker)
    assert panel.stopwatch_btn.isVisibleTo(panel) and not panel.stop_btn.isVisibleTo(panel)
    panel._start("stopwatch")
    assert panel.stop_btn.isVisibleTo(panel) and not panel.stopwatch_btn.isVisibleTo(panel)
    clock.advance(125)
    tracker._on_tick()
    assert panel.live_lbl.text() == "02:05"
    before = panel.logged_seconds()
    panel.stop_btn.click()
    assert service.get_time_entries(task_id=task.id)[-1]["duration_seconds"] == 125
    assert panel.logged_seconds() == before + 125, "the tab reloads when its timer logs time"
    assert not panel.stop_btn.isVisibleTo(panel)
    panel.detach()
    tracker.start_stopwatch(task.id)  # detached panel must not react any more
    assert panel.stopwatch_btn.isVisibleTo(panel)
    tracker.stop()


def test_task_dialog_assignee_and_time_tab(demo):
    service, board, tasks = demo
    task = tasks["✏️ Your turn: edit this card"]
    dlg = TaskDialog(board_id=board.id, task_id=task.id, service=service)
    assert dlg.assignee_combo.currentText() == ""
    assert dlg.assignee_combo.findText("Elena Rossi") >= 0
    dlg.assignee_combo.setCurrentText("Priya Sharma")
    dlg.time_panel.duration_input.setText("1h")
    dlg.time_panel.log_manual()
    assert dlg.external_changes and dlg.tabs.tabText(dlg._time_tab) == "Time (1h)"
    dlg._save_task()
    assert service.get_task(task.id).assignee == "Priya Sharma"
    assert service.get_task_activity(task.id)[-1]["details"] == "Assigned to Priya Sharma"


# ---------------------------------------------------------------------------
# Status-bar widget
# ---------------------------------------------------------------------------
def test_timer_widget_states(demo):
    service, board, tasks = demo
    task = tasks["Write API docs for webhooks"]
    clock = Clock()
    tracker = TimeTracker(service, now=clock)
    tracker.save_settings({"focus_minutes": 1, "short_break_minutes": 1})
    w = TimerWidget(tracker)
    assert w.task_btn.text() == "No timer running" and not w.primary_btn.isVisibleTo(w)

    tracker.start_pomodoro(task.id)
    assert w.icon_lbl.text() == "🍅" and w.time_lbl.text() == "01:00" and w.primary_btn.text() == "⏸"
    clock.advance(61)
    tracker._on_tick()
    assert w.icon_lbl.text() == "☕" and "Short break" in w.task_btn.text()
    assert w.skip_action.isVisible()
    w._primary_action()
    assert w.primary_btn.text() == "▶ Start break"
    tracker.stop()
    assert w.primary_btn.text() == "▶ Resume" and w.primary_btn.isVisibleTo(w)
    w._primary_action()  # resume last task as a stopwatch
    assert tracker.mode == "stopwatch" and tracker.task_id == task.id
    tracker.discard()


# ---------------------------------------------------------------------------
# Workload view & team dialog
# ---------------------------------------------------------------------------
def test_workload_view_detail_and_reassign(demo):
    service, board, tasks = demo
    view = WorkloadView(service)
    view.load_board(board.id)
    names = [r.name for r in view.result.rows]
    assert {"Elena Rossi", "Marcus Chen", "Priya Sharma"} <= set(names)
    assert view.grid.rowCount() == len(names) and view.grid.columnCount() == view.WEEKS_SHOWN + 2
    assert "people" in view.summary_lbl.text()

    r = names.index("Elena Rossi")
    view._on_cell_clicked(r, 0)  # person column: everything planned for Elena
    titles = {view.detail.item(i, 0).text() for i in range(view.detail.rowCount())}
    assert "Prepare launch screenshots & store listing" in titles

    launch = tasks["Prepare launch screenshots & store listing"]
    view.reassign(launch.id, "Priya Sharma")
    assert service.get_task(launch.id).assignee == "Priya Sharma"
    assert service.get_task_activity(launch.id)[-1]["details"] == "Reassigned from Elena Rossi to Priya Sharma"
    priya = view.result.row("Priya Sharma")
    assert any(a.task_id == launch.id for c in priya.cells for a in c.allocations)

    view.gran_combo.setCurrentIndex(1)  # days
    assert view.grid.columnCount() == view.DAYS_SHOWN + 2 and view._selected is None
    view.scope_combo.setCurrentIndex(1)  # whole workspace includes the Product Roadmap board
    assert view.result.row("") is not None


def test_team_dialog_saves_capacity(demo):
    service, *_ = demo
    dlg = TeamDialog(service, service.get_people())
    row = next(r for r in range(dlg.table.rowCount()) if dlg.table.item(r, 0).text() == "Marcus Chen")
    dlg.table.cellWidget(row, 1).setValue(20)
    dlg.table.cellWidget(row, 2 + 4).findChild(QCheckBox).setChecked(False)  # not on Fridays
    dlg._save()
    marcus = next(m for m in service.get_team_members() if m["name"] == "Marcus Chen")
    assert marcus["weekly_capacity_hours"] == 20 and marcus["working_days"] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Timesheet dialog
# ---------------------------------------------------------------------------
def test_timesheet_dialog_filters_and_exports(demo, tmp_path):
    service, board, tasks = demo
    dlg = TimesheetDialog(service, board_id=board.id)
    dlg.preset_combo.setCurrentText("Last 30 days")
    total_everyone = sum(r.seconds for r in dlg.rows)
    assert total_everyone > 0 and dlg.person_combo.count() >= 4  # Everyone + 3 people

    dlg.person_combo.setCurrentIndex(dlg.person_combo.findData("Elena Rossi"))
    assert {r.person for r in dlg.rows} == {"Elena Rossi"}
    for idx in range(dlg.group_combo.count()):
        dlg.group_combo.setCurrentIndex(idx)
        assert dlg.table.rowCount() > 0

    pdf = dlg.export("pdf", str(tmp_path / "t.pdf"))
    assert pdf.read_bytes()[:4] == b"%PDF" and pdf.stat().st_size > 1000
    assert dlg.export("csv", str(tmp_path / "t.csv")).exists()
    assert dlg.export("summary", str(tmp_path / "s.csv")).exists()
    assert "Saved" in dlg.note_lbl.text()

    dlg.preset_combo.setCurrentText("This week")
    dlg.start_edit.setDate(dlg.start_edit.date().addDays(-400))  # custom range
    assert dlg.preset_combo.currentText() == "Custom range"
    dlg.start_edit.setDate(dlg.end_edit.date().addDays(5))  # start after end: end follows
    assert dlg.end_edit.date() >= dlg.start_edit.date()
    assert dlg.default_filename("summary").endswith("_summary.csv")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
def test_main_window_timer_flow_card_badge_and_close_prompt(demo, monkeypatch):
    service, board, tasks = demo
    win = MainWindow(service=service)
    win.show()
    win.sidebar.select_board(board.id)
    win._on_board_selected(board.id)
    docs = tasks["Write API docs for webhooks"]

    win.start_timer(docs.id, "stopwatch")
    flush()
    card = next(c for c in win.findChildren(TaskCardWidget) if c.task_id == docs.id and c.isVisible())
    assert card.task_data["timer_running"]
    assert any(l.text() == "● Tracking" for l in card.findChildren(QLabel))
    assert "Write API docs" in win.timer_widget.task_btn.text()

    # Closing while tracking asks first; Cancel keeps the window open and the timer running
    monkeypatch.setattr(win, "_ask_about_running_timer", lambda: "cancel")
    event = QCloseEvent()
    win.closeEvent(event)
    assert not event.isAccepted() and win.time_tracker.is_tracking_work

    # "Keep running" leaves the entry open so the next start resumes it
    monkeypatch.setattr(win, "_ask_about_running_timer", lambda: "keep")
    win.closeEvent(QCloseEvent())
    assert service.get_running_time_entry() is not None
    restored = TimeTracker(service)
    assert "Resumed" in restored.restore()
    restored.discard()
    win.time_tracker.sync()

    win.start_timer(docs.id, "stopwatch")
    monkeypatch.setattr(win, "_ask_about_running_timer", lambda: "stop")
    win.closeEvent(QCloseEvent())
    assert service.get_running_time_entry() is None and not win.time_tracker.is_active

    # Stopping from the status bar reports what happened (here: too short to be logged)
    win.start_timer(docs.id, "stopwatch")
    win.timer_widget.stop_btn.click()
    assert not win.time_tracker.is_active
    assert "too short to log" in win.status_bar.currentMessage()

    win._on_view_mode_changed(ViewMode.WORKLOAD.value)
    assert win.view_stack.currentWidget() is win.workload_view and win.filter_bar.btn_workload.isChecked()
    assert win.workload_view.result is not None


def test_busy_cards_wrap_badges_instead_of_overflowing(demo):
    """Cards with many badges (due, blocked, tracking, counters, time, avatar) must stay inside their column."""
    service, board, tasks = demo
    win = MainWindow(service=service)
    win.resize(1280, 800)
    win.show()
    win.sidebar.select_board(board.id)
    win._on_board_selected(board.id)
    win.start_timer(tasks["Crash when importing large CSV files"].id, "stopwatch")
    flush()
    cards = [c for c in win.findChildren(TaskCardWidget) if c.isVisible()]
    assert len(cards) >= 8
    from PySide6.QtWidgets import QScrollArea

    for card in cards:
        # The column's own scroll area is what clips: measure against its visible viewport
        parent = card.parentWidget()
        while not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        viewport = parent.viewport()
        for child in card.findChildren(QLabel):
            if child.isVisible():
                right = child.mapTo(viewport, child.rect().topRight()).x()
                assert right <= viewport.width(), (
                    f"'{child.text()}' on '{card.task_data['title']}' is clipped by its column"
                )
    win.time_tracker.discard()


def test_timer_widget_menu_button_has_no_dropdown_arrow_and_titles_keep_ampersands(demo):
    service, board, tasks = demo
    tracker = TimeTracker(service, now=Clock())
    w = TimerWidget(tracker)
    w.show()
    flush()
    # Qt paints a drop-down arrow in the bottom-right corner of buttons with a menu. It fits inside
    # the padding (no size change), so check the rendered pixels: that corner must be empty.
    img = w.menu_btn.grab().toImage()
    arrow_pixels = [
        (x, y)
        for x in range(img.width() - 12, img.width())
        for y in range(img.height() // 2, img.height())
        if img.pixelColor(x, y).lightness() > 150
    ]
    assert arrow_pixels == [], "the ⚙ button shows a drop-down arrow"

    # "&" in a task title must be shown literally, not turned into a shortcut underline
    col = service.get_columns_for_board(board.id)[0]
    task = service.create_task(board.id, col.id, "Design & build")
    tracker.start_stopwatch(task.id)
    assert w.task_btn.text().endswith("Design && build")
    tracker.discard()

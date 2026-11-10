"""
Main-window handlers that glue dialogs, signals and the service together:
refused moves, recurring completions, template flows, the dialogs that report
back whether anything changed, quick-capture commands and failure paths.
Dialogs are stubbed at ``exec`` so nothing blocks.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from PySide6.QtWidgets import QDialog, QMessageBox

from kanbanmanager.core.signals import app_signals
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.ui import MainWindow
from kanbanmanager.ui.dialogs import template_dialogs as td
from kanbanmanager.ui.dialogs.archive_dialog import ArchivedTasksDialog
from kanbanmanager.ui.dialogs.calendar_sync_dialog import CalendarSyncDialog
from kanbanmanager.ui.dialogs.custom_fields_dialog import CustomFieldsDialog
from kanbanmanager.ui.dialogs.import_export_dialog import ImportExportDialog
from kanbanmanager.ui.dialogs.task_dialog import TaskDialog
from kanbanmanager.ui.dialogs.theme_dialog import ThemeDialog
from support import flush_qt


@pytest.fixture
def win(seeded_service):
    window = MainWindow(service=seeded_service)
    window.resize(1400, 850)
    window.show()
    demo = next(b for ws in seeded_service.get_workspaces() for b in ws.boards if b.name == DEMO_BOARD_NAME)
    window.open_board(demo.id)
    flush_qt()
    return window


@pytest.fixture
def service(seeded_service):
    return seeded_service


def titles(win):
    return [c.task_data["title"] for col in win.board_view._columns() for c in col.cards()]


def test_a_refused_move_explains_why_and_changes_nothing(win, service, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]) or QMessageBox.Ok)
    columns = service.get_columns_for_board(win.current_board_id)
    review = next(c for c in columns if c.name == "In Review")
    service.update_column(review.id, wip_limit=1)
    backlog_task = win.board_view._columns()[0].cards()[0].task_id
    win._on_task_moved(backlog_task, review.id, 0)
    assert warned and "WIP limit" in warned[0]
    assert service.get_task(backlog_task).column_id == columns[0].id
    todo = columns[1]
    win._on_task_moved(backlog_task, todo.id, 0)
    assert service.get_task(backlog_task).column_id == todo.id
    assert win.status_bar.currentMessage() == "Task moved successfully"


def test_completing_a_recurring_task_creates_the_next_one(win, service):
    col = win.board_view._columns()[0].column_id
    due = datetime.now().replace(microsecond=0) + timedelta(days=2)
    task = service.create_task(win.current_board_id, col, "Water plants", is_recurring=True, due_date=due,
                               recurrence_rule={"frequency": "WEEKLY"})
    win._on_task_completed_toggled(task.id, True)
    flush_qt()
    waters = [t for t in service.get_tasks_for_board(win.current_board_id) if t.title == "Water plants"]
    assert sorted(t.due_date for t in waters) == [due, due + timedelta(weeks=1)]
    assert "next occurrence" in win.status_bar.currentMessage()
    assert titles(win).count("Water plants") == 2


def test_delete_and_archive_signals(win, service):
    first = win.board_view._columns()[0].cards()[0].task_id
    app_signals.task_archive_requested.emit(first)
    flush_qt()
    assert any(t["id"] == first for t in service.get_archived_tasks(win.current_board_id))
    second = win.board_view._columns()[0].cards()[0].task_id
    app_signals.task_deleted.emit(second)  # already confirmed by whoever emitted it
    flush_qt()
    assert service.get_task(second) is None and win.status_bar.currentMessage() == "Task deleted"


def test_template_flows(win, service, monkeypatch):
    board = win.current_board_id
    template = service.get_task_templates(board)[0]
    opened = []
    monkeypatch.setattr(TaskDialog, "exec", lambda dlg: opened.append((dlg.template_name, dlg.default_column_id)) or 0)
    assert win.new_task_from_template(board, 0, template["id"]) is False, "cancelled: nothing saved"
    assert opened[0][0] == template["name"] and opened[0][1], "filled from the template, column resolved"
    assert win.new_task_from_template(board, 0, 987654) is False, "a missing template opens nothing"

    def use_first(dlg):
        dlg.use_requested = template
        dlg.changed = True
        return 0

    monkeypatch.setattr(td.TaskTemplatesDialog, "exec", use_first)
    win.open_task_templates()
    assert len(opened) == 2, "'Use' opens the editor from the template"

    def save_board(dlg):
        dlg.saved = {"name": "My layout"}
        return QDialog.Accepted

    monkeypatch.setattr(td.SaveBoardTemplateDialog, "exec", save_board)
    assert win.save_board_as_template() == {"name": "My layout"}
    assert "My layout" in win.status_bar.currentMessage()

    task_id = win.board_view._columns()[0].cards()[0].task_id
    captured = []

    def save_task(dlg):
        captured.append(dlg)
        dlg.saved = {"name": "From card"}
        return QDialog.Accepted

    monkeypatch.setattr(td.TaskTemplateEditor, "exec", save_task)
    assert win.save_task_as_template(task_id) == {"name": "From card"}
    assert win.save_task_as_template(987654) is None


@pytest.mark.parametrize("opener, dialog_cls", [
    ("open_custom_fields", CustomFieldsDialog),
    ("open_archive_dialog", ArchivedTasksDialog),
    ("open_import_export", ImportExportDialog),
    ("open_calendar_sync", CalendarSyncDialog),
])
def test_dialogs_that_report_changes_refresh_the_board(win, service, monkeypatch, opener, dialog_cls):
    refreshed = []
    monkeypatch.setattr(type(win), "refresh_active_view", lambda self: refreshed.append(True))

    def changed(dlg):
        dlg.changed = True
        return 0

    monkeypatch.setattr(dialog_cls, "exec", changed)
    getattr(win, opener)()
    assert refreshed, opener
    refreshed.clear()
    monkeypatch.setattr(dialog_cls, "exec", lambda dlg: 0)
    getattr(win, opener)()
    assert not refreshed, f"{opener}: no change, no refresh"


def test_import_dialog_can_open_a_new_board_and_the_calendar_tabs(win, service, monkeypatch):
    new_board = service.create_board(service.get_workspaces()[0].id, "Imported")

    def imported(dlg):
        dlg.board_created.emit(new_board.id)
        dlg.changed = True
        return 0

    monkeypatch.setattr(ImportExportDialog, "exec", imported)
    win.open_import_export()
    assert win.current_board_id == new_board.id
    tabs = []
    monkeypatch.setattr(CalendarSyncDialog, "exec", lambda dlg: tabs.append(dlg.tabs.currentIndex()) or 0)
    win.open_calendar_sync(tab="files")
    win.open_calendar_sync(tab="feed")
    win._calendar_from_import("files")
    assert tabs == [2, 1, 2]


def test_appearance_result_is_announced(win, monkeypatch):
    monkeypatch.setattr(ThemeDialog, "exec", lambda dlg: QDialog.Accepted)
    assert win.open_appearance() is True and "Theme: Dark" in win.status_bar.currentMessage()
    monkeypatch.setattr(ThemeDialog, "exec", lambda dlg: QDialog.Rejected)
    assert win.open_appearance() is False


def test_commands_from_other_processes(win, service):
    win.handle_command({"cmd": "capture", "text": "Book flights !high"})
    assert any(i["title"] == "Book flights" for i in service.get_inbox_items())
    win.handle_command({"cmd": "capture", "text": "   "})  # no text: the capture window opens
    assert win.capture_dialog is not None and win.capture_dialog.isVisible()
    win.capture_dialog.close()
    win.handle_command({"cmd": "show"})
    win.handle_command({"cmd": "capture", "text": "!high"})  # nothing to capture: ignored
    assert sum(1 for i in service.get_inbox_items() if i["title"] == "Book flights") == 1


def test_capture_settings_are_reapplied(win, monkeypatch):
    from kanbanmanager.ui.dialogs.quick_capture import QuickCaptureSettingsDialog
    monkeypatch.setattr(QuickCaptureSettingsDialog, "exec", lambda dlg: QDialog.Accepted)
    win.open_capture_settings()
    assert "Quick capture:" in win.status_bar.currentMessage()
    assert win._capture_shortcut is not None and win._capture_shortcut.isEnabled()


def test_a_failing_demo_board_is_reported_not_raised(win, service, monkeypatch):
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: errors.append(a[2]))
    monkeypatch.setattr(type(service), "create_demo_board", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert win.create_demo_board() is None
    assert errors and "disk full" in errors[0]


def test_demo_board_goes_to_a_new_workspace_when_there_is_none(qapp, empty_service):
    window = MainWindow(service=empty_service)
    board_id = window.create_demo_board()
    assert board_id and empty_service.get_workspaces()[0].name == "Demo Workspace"
    flush_qt()
    assert window.current_board_id == board_id


def test_sync_results_are_summarised(win):
    class Report:
        def __init__(self, name, status, counts, text):
            self.name, self.status, self.counts, self._text = name, status, counts, text

        def describe(self):
            return self._text

    win._on_sync_finished([Report("Team", "success", {"created": 2}, "2 new"),
                           Report("Broken", "failed", {}, "server said no")])
    message = win.status_bar.currentMessage()
    assert "Team: 2 new" in message and "Broken: server said no" in message


def test_closing_a_window_stops_its_background_work(qapp, seeded_service):
    first = MainWindow(service=seeded_service)
    listeners = list(seeded_service._listeners)
    assert first.automation.handle_event in listeners and first.scheduler.handle_event in listeners
    first.close()
    assert first.automation.handle_event not in seeded_service._listeners, \
        "otherwise a second window on the same database would run every rule twice"
    assert not first._automation_timer.isActive() and not first.reminder_engine.timer.isActive()
    second = MainWindow(service=seeded_service)
    assert len(seeded_service._listeners) == len(listeners)
    second.shutdown()
    second.shutdown()  # twice is fine

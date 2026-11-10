"""
UI tests for My Focus / Global Inbox, the Quick Capture window, Multi-Board
Split View (including dragging cards between boards), and main-window wiring.
"""
from datetime import date, datetime, timedelta

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QScrollArea

from kanbanmanager import DatabaseService
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.ui import MainWindow
from kanbanmanager.ui.components.task_card import TaskCardWidget
from kanbanmanager.ui.dialogs.quick_capture import QuickCaptureDialog, QuickCaptureSettingsDialog, load_settings
from kanbanmanager.ui.views.focus_view import FocusView, TriageDialog, group_tasks
from kanbanmanager.ui.views.split_view import SplitView
from support import seeded_manager


@pytest.fixture
def env(qapp, tmp_path):
    mgr = seeded_manager(tmp_path / "focus.db")
    service = DatabaseService(mgr, actor="Ann")
    boards = {b.name: b for ws in service.get_workspaces() for b in ws.boards}
    return service, boards


def tree_titles(tree):
    out = []
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        out.append(item.text(0))
    return out


# ---------------------------------------------------------------------------
# Focus
# ---------------------------------------------------------------------------
def test_group_tasks_buckets():
    now = datetime(2024, 6, 5, 14)
    tasks = [
        {"id": 1, "title": "late", "due_date": "2024-06-04T17:00:00", "priority": "LOW"},
        {"id": 2, "title": "earlier today", "due_date": "2024-06-05T09:00:00", "priority": "LOW"},
        {"id": 3, "title": "tonight", "due_date": "2024-06-05T20:00:00", "priority": "LOW"},
        {"id": 4, "title": "b-soon", "due_date": "2024-06-08T17:00:00", "priority": "LOW"},
        {"id": 5, "title": "a-soon", "due_date": "2024-06-08T17:00:00", "priority": "URGENT"},
        {"id": 6, "title": "far", "due_date": "2024-07-01T17:00:00", "priority": "LOW"},
        {"id": 7, "title": "whenever", "due_date": None, "priority": "LOW"},
    ]
    g = group_tasks(tasks, now)
    assert [t["id"] for t in g["overdue"]] == [1, 2]
    assert [t["id"] for t in g["today"]] == [3]
    assert [t["id"] for t in g["week"]] == [5, 4], "same due: higher priority first"
    assert [t["id"] for t in g["later"]] == [6] and [t["id"] for t in g["nodate"]] == [7]


def test_focus_view_lists_reschedules_and_completes(env, monkeypatch):
    service, boards = env
    view = FocusView(service)
    view.refresh()
    view.person_combo.setCurrentIndex(view.person_combo.findData("Marcus Chen"))
    titles = tree_titles(view.tree)
    assert any(t.startswith("🗓 Next 7 days") for t in titles)
    crash_row = next(t for t in titles if t.startswith("Crash when importing"))
    assert "🔥" in crash_row, "critical-path tasks are flagged"
    assert "<b>4</b> open tasks across <b>1</b> board" in view.summary.text()

    crash = next(t for t in view.tasks if t["title"].startswith("Crash"))
    view.reschedule(crash["id"], date.today() + timedelta(days=30))
    moved = service.get_task(crash["id"])
    assert moved.due_date.date() == date.today() + timedelta(days=30)
    assert moved.due_date.time().hour == 12, "time of day kept"

    completed = []
    from kanbanmanager.core.signals import app_signals

    def on_toggled(task_id, _done):
        completed.append(task_id)

    app_signals.task_completed_toggled.connect(on_toggled)
    try:
        row = next(view.tree.topLevelItem(i) for i in range(view.tree.topLevelItemCount())
                   if view.tree.topLevelItem(i).data(0, Qt.UserRole) == crash["id"])
        row.setCheckState(0, Qt.Checked)
        assert completed == [], "deferred: the tree must not be rebuilt inside itemChanged"
        QCoreApplication.processEvents()
        assert completed == [crash["id"]]
    finally:
        app_signals.task_completed_toggled.disconnect(on_toggled)  # only ours: the signal is global

    view.search.setText("charting")
    assert [t for t in tree_titles(view.tree) if not t[0] in "⏰📅🗓🔭📭"] == \
        ["Evaluate charting libraries for the analytics view"]
    view.person_combo.setCurrentIndex(0)  # me
    assert all("Ann" in (t.get("assignee") or "Ann") for t in view.tasks)


def test_inbox_tab_capture_triage_and_delete(env):
    service, boards = env
    view = FocusView(service)
    view.refresh()
    assert view.tabs.tabText(1) == "📥 Inbox (2)"
    view.inbox_input.setText("Book venue next fri !high #Events")
    saved = view.add_from_input()
    assert saved["kind"] == "inbox" and view.tabs.tabText(1) == "📥 Inbox (3)"
    view.inbox_input.setText("Update roadmap slide >roadmap")
    assert view.add_from_input()["kind"] == "task", "'>board' goes straight to the board"

    venue = next(i for i in range(view.inbox_tree.topLevelItemCount())
                 if view.inbox_tree.topLevelItem(i).text(0) == "Book venue")
    view.inbox_tree.topLevelItem(venue).setSelected(True)
    assert "#Events" in view.inbox_tree.topLevelItem(venue).text(1)

    def accept(dlg: TriageDialog):
        dlg.board_combo.setCurrentIndex(dlg.board_combo.findData(boards[DEMO_BOARD_NAME].id))
        dlg.title_edit.setText("Book the venue")
        dlg.send()
        return True

    created = view.send_selected(dialog_exec=accept)
    task = service.get_task(created[0])
    assert task.title == "Book the venue" and task.priority == "HIGH" and task.board_id == boards[DEMO_BOARD_NAME].id
    assert [l.name for l in task.labels] == ["Events"]
    assert service.count_inbox_items() == 2

    view.inbox_tree.selectAll()
    view.delete_selected(confirm=False)
    assert service.count_inbox_items() == 0 and not view.inbox_empty.isHidden()


# ---------------------------------------------------------------------------
# Quick Capture window & settings
# ---------------------------------------------------------------------------
def test_quick_capture_window(env):
    service, boards = env
    dlg = QuickCaptureDialog(service)
    captured = []
    dlg.captured.connect(captured.append)
    dlg.summon("Ship notes fri 3pm !high #Docs @elena >feature // mention CDN")
    assert dlg.isVisible() and dlg.destination.currentData() == boards[DEMO_BOARD_NAME].id
    preview = dlg.preview.text()
    assert "Ship notes" in preview and "High" in preview and "#Docs" in preview and "Elena Rossi" in preview
    saved = dlg.save(keep_open=True)
    task = service.get_task(saved["id"])
    assert (task.title, task.assignee, task.description) == ("Ship notes", "Elena Rossi", "mention CDN")
    assert dlg.input.text() == "" and "Feature Tour" in dlg.status.text() and captured

    # A destination picked by hand wins over '>board'
    dlg.destination.setCurrentIndex(0)
    dlg._on_destination_picked(0)
    dlg.input.setText("Someday idea >feature")
    assert dlg.destination.currentData() is None
    assert dlg.save(keep_open=True)["kind"] == "inbox"
    dlg.input.setText("")
    assert dlg.save() is None and "EF4444" in dlg.input.styleSheet()

    # Until the user picks, the destination follows '>board' both ways
    dlg.summon()
    dlg.input.setText("Typo >feature")
    assert dlg.destination.currentData() == boards[DEMO_BOARD_NAME].id
    dlg.input.setText("Typo")
    assert dlg.destination.currentData() is None


def test_quick_capture_settings(env):
    service, _ = env
    dlg = QuickCaptureSettingsDialog(service, "status text")
    from PySide6.QtGui import QKeySequence
    dlg.sequence.setKeySequence(QKeySequence("Shift+K"))
    dlg.global_chk.setChecked(False)
    dlg.save()
    assert load_settings(service) == {"hotkey": "Shift+K", "global": False}
    dlg.sequence.setKeySequence(QKeySequence("K"))
    dlg.save()
    assert "modifier" in dlg.error_lbl.text() and load_settings(service)["hotkey"] == "Shift+K"


# ---------------------------------------------------------------------------
# Split view
# ---------------------------------------------------------------------------
def test_split_view_panes_persist_and_cards_fit(env):
    service, boards = env
    view = SplitView(service)
    view.resize(1500, 800)
    view.show()
    view.load([boards["Product Roadmap"].id, boards[DEMO_BOARD_NAME].id])
    assert view.board_ids() == [boards["Product Roadmap"].id, boards[DEMO_BOARD_NAME].id]
    QApplication.processEvents()
    for card in (c for c in view.findChildren(TaskCardWidget) if c.isVisible()):
        parent = card.parentWidget()
        while not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        vp = parent.viewport()
        for child in card.findChildren(type(card.title_lbl)):
            if child.isVisible():
                assert child.mapTo(vp, child.rect().topRight()).x() <= vp.width(), \
                    f"'{child.text()}' is clipped in a split-view column"
    view.add_pane(boards["Product Roadmap"].id)
    view.toggle_orientation()
    view.remove_pane(view.panes[0])
    assert view.board_ids() == [boards[DEMO_BOARD_NAME].id, boards["Product Roadmap"].id]
    saved = service.get_setting("split_view")
    assert saved == {"boards": view.board_ids(), "orientation": "vertical"}
    restored = SplitView(service)
    restored.load([])
    assert restored.board_ids() == view.board_ids() and restored.orientation == Qt.Vertical
    for _ in range(4):
        restored.add_pane(None)
    assert len(restored.panes) == 4 and not restored.add_btn.isEnabled()


def test_drag_between_boards_in_split_view_and_drop_position(env):
    service, boards = env
    win = MainWindow(service=service)
    win.resize(1500, 850)
    win.show()
    win.show_split()
    split = win.split_view
    roadmap, demo = boards["Product Roadmap"], boards[DEMO_BOARD_NAME]
    assert split.board_ids()[:2] == [win.current_board_id, demo.id] or demo.id in split.board_ids()
    assert any(p.board_id == roadmap.id for p in split.panes)
    demo_pane = next(p for p in split.panes if p.board_id == demo.id)
    task = service.get_tasks_for_board(roadmap.id)[0]
    todo = next(c for c in service.get_columns_for_board(demo.id) if c.name == "To Do")
    before = [t.id for t in service.get_tasks_for_board(demo.id) if t.column_id == todo.id]

    # What the drop area does when a card from the other pane is released at index 1
    demo_pane.column_widgets[todo.id].request_task_move(task.id, 1)
    moved = service.get_task(task.id)
    assert moved.board_id == demo.id and moved.column_id == todo.id
    after = [t.id for t in service.get_tasks_for_board(demo.id) if t.column_id == todo.id]
    assert after == before[:1] + [task.id] + before[1:], "dropped where the indicator was"
    assert task.id in demo_pane.column_widgets[todo.id].task_ids, "panes refreshed after the move"
    assert task.id not in [t.id for t in service.get_tasks_for_board(roadmap.id)]

    # Reordering within a column: a card dragged down is counted above its drop position
    col = demo_pane.column_widgets[todo.id]
    first = col.task_ids[0]
    col.request_task_move(first, 3)
    assert [t.id for t in service.get_tasks_for_board(demo.id) if t.column_id == todo.id].index(first) == 2
    win.time_tracker.shutdown()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
def test_main_window_pages_commands_and_capture(env):
    service, boards = env
    win = MainWindow(service=service)
    assert win.sidebar.inbox_btn.text() == "📥  Inbox  (2)"
    win.show_focus("inbox")
    assert win.pages.currentWidget() is win.focus_view and win.focus_view.tabs.currentIndex() == 1
    assert win.sidebar.inbox_btn.isChecked() and not win.sidebar.focus_btn.isChecked()
    win.open_board(boards["Product Roadmap"].id)
    assert win.pages.currentIndex() == win.PAGE_BOARD and win.current_board_id == boards["Product Roadmap"].id
    assert not win.sidebar.inbox_btn.isChecked()

    win.handle_command({"cmd": "capture", "text": "From another app tomorrow"})
    assert "From another app" in win.status_bar.currentMessage()
    assert win.sidebar.inbox_btn.text() == "📥  Inbox  (3)"
    win.handle_command({"cmd": "capture", "text": ""})
    assert win.capture_dialog is not None and win.capture_dialog.isVisible()
    win.capture_dialog.close()
    assert not win.global_hotkey.active, "no system-wide grab without desktop integration"
    assert "In-app only" in win.global_hotkey.status
    assert win._capture_shortcut.key().toString() == "Ctrl+Shift+Space"
    win.time_tracker.shutdown()

"""
Mouse-level interactions that the view tests don't reach: drag and drop on the
board (synthesised drag events - offscreen Qt can't run a real drag), the
card / column / sidebar context menus, the Markdown editor's source mode,
time entries, attachments, the timer menu and the theme colour pickers.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QByteArray, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QContextMenuEvent, QDrag, QDragEnterEvent, QDragMoveEvent, QDropEvent, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QColorDialog, QDialog, QFileDialog, QMessageBox

from kanbanmanager.core.signals import app_signals
from kanbanmanager.core.time_tracker import TimeTracker
from kanbanmanager.ui.components.attachments_panel import AttachmentsPanel
from kanbanmanager.ui.components.markdown_editor import LinkDialog, MarkdownEditor
from kanbanmanager.ui.components.task_card import MIME_TASK_ID
from kanbanmanager.ui.components.time_panel import TimeEntryDialog, TimePanel
from kanbanmanager.ui.components.timer_widget import PomodoroSettingsDialog, TimerWidget
from kanbanmanager.ui.components.workspace_sidebar import WorkspaceSidebar
from kanbanmanager.ui.dialogs.theme_dialog import ThemeDialog
from kanbanmanager.ui.views.board_view import BoardView
from support import MenuCatcher, flush_qt, make_board, recording


@pytest.fixture
def menus(monkeypatch):
    from kanbanmanager.ui.components import column_widget, task_card, time_panel, workspace_sidebar
    return MenuCatcher().install(monkeypatch, column_widget, task_card, time_panel, workspace_sidebar)


@pytest.fixture
def board(qapp, empty_service):
    built = make_board(empty_service, "Drag", tasks=6)
    view = BoardView(empty_service)
    view.resize(1700, 900)
    view.show()
    view.load_board(built.id)
    flush_qt()
    return empty_service, built, view


def _mime(task_id, column_id):
    md = QMimeData()
    md.setData(MIME_TASK_ID, QByteArray(f"{task_id}:{column_id}".encode()))
    return md


def _drag(widget, mime, y, kind):
    pos = QPointF(20, y)
    if kind == "enter":
        ev = QDragEnterEvent(pos, Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
    elif kind == "move":
        ev = QDragMoveEvent(pos, Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
    else:
        ev = QDropEvent(pos, Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(widget, ev)
    return ev


# ---------------------------------------------------------------------------
# Drag and drop
# ---------------------------------------------------------------------------
def test_drop_position_follows_the_pointer_and_paints_an_indicator(board):
    service, built, view = board
    backlog, todo = view._columns()[0], view._columns()[1]
    area = todo.drop_area
    cards = todo.cards()  # Task 0001, Task 0006 in "To Do"? (round-robin over 5 columns)
    mime = _mime(built.tasks[0], backlog.column_id)
    assert _drag(area, QMimeData(), 5, "enter").isAccepted() is False, "other drags are refused"
    assert _drag(area, mime, 5, "enter").isAccepted()
    _drag(area, mime, 1, "move")
    assert area.drop_index == 0
    below_last = cards[-1].geometry().bottom() + 5
    _drag(area, mime, below_last, "move")
    assert area.drop_index == len(cards)
    area.grab()  # paints the indicator line (top, middle and end positions)
    _drag(area, mime, cards[0].geometry().center().y() + 5, "move")
    assert area.drop_index == 1
    area.grab()
    with recording(app_signals.task_dropped) as drops:
        _drag(area, mime, cards[0].geometry().center().y() + 5, "drop")
    assert drops == [(built.tasks[0], todo.column_id, 1)]
    assert area.drop_index is None, "the indicator goes away"
    area.dragLeaveEvent(QDragEnterEvent(QPointF(0, 0), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier))


def test_dropping_lower_in_the_same_column_accounts_for_the_card_itself(board):
    service, built, view = board
    todo = view._columns()[1]
    first = todo.cards()[0]
    with recording(app_signals.task_dropped) as drops:
        todo.request_task_move(first.task_id, 2)  # dropped below the 2nd card
        todo.request_task_move(first.task_id, 0)
    assert drops == [(first.task_id, todo.column_id, 1), (first.task_id, todo.column_id, 0)]


def test_a_broken_drop_payload_is_ignored(board):
    service, built, view = board
    md = QMimeData()
    md.setData(MIME_TASK_ID, QByteArray(b"garbage"))
    with recording(app_signals.task_dropped) as drops:
        ev = _drag(view._columns()[0].drop_area, md, 5, "drop")
    assert drops == [] and not ev.isAccepted()


def test_dragging_a_card_carries_its_task_and_column(board, monkeypatch):
    service, built, view = board
    card = view._columns()[0].cards()[0]
    started = []
    monkeypatch.setattr(QDrag, "exec", lambda drag, *a: started.append(drag.mimeData().data(MIME_TASK_ID).data())
                        or Qt.MoveAction)
    QTest.mousePress(card, Qt.LeftButton, Qt.NoModifier, QPoint(30, 30))
    QTest.mouseMove(card, QPoint(31, 31))  # under the drag distance: nothing yet
    assert started == []
    from PySide6.QtGui import QMouseEvent  # QTest moves don't carry buttons: send a real one
    ev = QMouseEvent(QMouseEvent.MouseMove, QPointF(80, 80), QPointF(80, 80), Qt.NoButton, Qt.LeftButton,
                     Qt.NoModifier)
    QApplication.sendEvent(card, ev)
    assert started == [f"{card.task_id}:{card.column_id}".encode()]
    with recording(app_signals.task_edit_requested) as edits:
        QTest.mouseDClick(card, Qt.LeftButton, Qt.NoModifier, QPoint(30, 30))
    assert edits == [(card.task_id,)]
    with recording(app_signals.task_completed_toggled) as toggles:
        card.check_btn.click()
    assert toggles == [(card.task_id, True)]


# ---------------------------------------------------------------------------
# Context menus
# ---------------------------------------------------------------------------
def test_card_context_menu_actions(board, menus, monkeypatch):
    service, built, view = board
    card = view._columns()[0].cards()[0]
    card.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), card.mapToGlobal(QPoint(5, 5))))
    assert menus.texts()[:2] == ["✏ Edit Task", "✓ Mark Complete"]
    expectations = [
        ("✏ Edit", app_signals.task_edit_requested, (card.task_id,)),
        ("✓ Mark", app_signals.task_completed_toggled, (card.task_id, True)),
        ("▶ Start Stopwatch", app_signals.timer_start_requested, (card.task_id, "stopwatch")),
        ("🍅 Start Pomodoro", app_signals.timer_start_requested, (card.task_id, "pomodoro")),
        ("📋 Save as Template", app_signals.task_template_save_requested, (card.task_id,)),
        ("🗄 Archive", app_signals.task_archive_requested, (card.task_id,)),
    ]
    for text, signal, args in expectations:
        with recording(signal) as calls:
            menus.trigger(text)
        assert calls == [args], text
    # deleting asks first (it takes comments, attachments and logged time with it)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    with recording(app_signals.task_deleted) as calls:
        menus.trigger("🗑 Delete")
    assert calls == [] and card.task_data["title"] in asked[0] and "Archive it instead" in asked[0]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    with recording(app_signals.task_deleted) as calls:
        menus.trigger("🗑 Delete")
    assert calls == [(card.task_id,)]
    card.task_data["timer_running"] = True
    card.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), QPoint(5, 5)))
    with recording(app_signals.timer_stop_requested) as calls:
        menus.trigger("■ Stop Timer")
    assert calls == [()]


def test_column_menu_actions(board, menus, monkeypatch):
    service, built, view = board
    column = view._columns()[0]
    column._show_column_menu()
    assert menus.find("No templates yet") is not None and not menus.find("No templates yet").isEnabled()
    with recording(column.task_add_requested, column.column_edit_requested) as calls:
        menus.trigger("➕ Add Task")
        menus.trigger("✏ Edit Column")
    assert calls == [(column.column_id,), (column.column_id,)]
    with recording(app_signals.task_templates_requested) as calls:
        menus.trigger("Manage Templates")
    assert calls == [(built.id,)]

    template = service.save_task_template("Bug & fix", {"title": "Bug: {date}"}, board_id=built.id, icon="🐞")
    view.refresh()
    column = view._columns()[0]
    column._show_column_menu()
    assert menus.find("🐞  Bug && fix") is not None, "ampersands are escaped in menu texts"
    with recording(app_signals.task_from_template_requested) as calls:
        menus.trigger("🐞")
    assert calls == [(built.id, column.column_id, template["id"])]

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    column._show_column_menu()
    menus.trigger("🗑 Delete Column")
    assert len(service.get_columns_for_board(built.id)) == 5, "No keeps the column"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    column._show_column_menu()
    menus.trigger("🗑 Delete Column")
    flush_qt()
    assert len(service.get_columns_for_board(built.id)) == 4 and len(view._columns()) == 4


def test_sidebar_context_menus(qapp, empty_service, menus, monkeypatch):
    ws = empty_service.create_workspace("Team")
    built = make_board(empty_service, "Plans", workspace_id=ws.id)
    sidebar = WorkspaceSidebar(empty_service)
    sidebar.resize(260, 700)
    sidebar.show()
    sidebar.refresh()
    flush_qt()
    ws_item = sidebar.tree.topLevelItem(0)
    board_item = ws_item.child(0)

    sidebar._on_tree_context_menu(sidebar.tree.visualItemRect(ws_item).center())
    for text, signal in (("➕ New Board", sidebar.new_board_requested), ("✨ Add Demo", sidebar.demo_board_requested),
                         ("✏ Edit Workspace", sidebar.edit_workspace_requested)):
        with recording(signal) as calls:
            menus.trigger(text)
        assert calls == [(ws.id,)], text

    sidebar._on_tree_context_menu(sidebar.tree.visualItemRect(board_item).center())
    for text, signal in (
        ("✏ Edit Board", sidebar.edit_board_requested), ("🧩 Custom Fields", app_signals.custom_fields_requested),
        ("📋 Task Templates", app_signals.task_templates_requested),
        ("⚡ Automations", app_signals.automations_requested),
        ("🗄 Archived Tasks", app_signals.archived_tasks_requested),
        ("💾 Save as Board Template", app_signals.board_template_save_requested),
        ("⇅ Import / Export", app_signals.import_export_requested),
        ("📅 Calendar Sync", app_signals.calendar_sync_requested),
    ):
        with recording(signal) as calls:
            menus.trigger(text)
        assert calls == [(built.id,)], text

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    menus.trigger("🗑 Delete Board")
    assert empty_service.get_board(built.id) is None
    sidebar._on_tree_context_menu(QPoint(5, 690))  # empty space: no menu
    with recording(app_signals.workspace_selected) as calls:
        sidebar._on_item_clicked(sidebar.tree.topLevelItem(0), 0)
    assert calls == [(ws.id,)]


# ---------------------------------------------------------------------------
# Markdown editor: source mode, links, paste and drop
# ---------------------------------------------------------------------------
def _select(edit, start, end):
    from PySide6.QtGui import QTextCursor
    cur = edit.textCursor()
    cur.setPosition(start)
    cur.setPosition(end, QTextCursor.KeepAnchor)
    edit.setTextCursor(cur)


@pytest.mark.parametrize("toggle, wrapped", [("toggle_bold", "**word**"), ("toggle_italic", "*word*"),
                                             ("toggle_strike", "~~word~~"), ("toggle_code", "`word`")])
def test_source_mode_wraps_and_unwraps_selections(qapp, toggle, wrapped):
    ed = MarkdownEditor()
    ed.set_mode(MarkdownEditor.MARKDOWN)
    ed.set_markdown("a word here")
    _select(ed.source, 2, 6)
    getattr(ed, toggle)()
    assert ed.markdown() == f"a {wrapped} here"
    _select(ed.source, 2, 2 + len(wrapped))
    getattr(ed, toggle)()
    assert ed.markdown() == "a word here", "toggling again removes the markers"
    ed.set_markdown("")
    getattr(ed, toggle)()
    assert ed.markdown().strip() and ed.source.textCursor().hasSelection(), "a placeholder is inserted, selected"


def test_source_mode_line_prefixes_toggle_and_replace_each_other(qapp):
    ed = MarkdownEditor()
    ed.set_mode(MarkdownEditor.MARKDOWN)
    ed.set_markdown("one\ntwo")
    _select(ed.source, 0, 7)
    ed.toggle_bullets()
    assert ed.markdown() == "- one\n- two"
    _select(ed.source, 0, len(ed.markdown()))
    ed.toggle_tasks()
    assert ed.markdown() == "- [ ] one\n- [ ] two", "a list style replaces the other"
    _select(ed.source, 0, len(ed.markdown()))
    ed.toggle_tasks()
    assert ed.markdown() == "one\ntwo"
    _select(ed.source, 0, 7)
    ed.toggle_quote()
    assert ed.markdown() == "> one\n> two"
    _select(ed.source, 0, len(ed.markdown()))
    ed.toggle_quote()
    assert ed.markdown() == "one\ntwo"
    ed.toggle_heading(3)
    assert ed.markdown().startswith("### ")
    _select(ed.source, 0, 0)
    ed.toggle_code_block()
    assert "```" in ed.markdown()


def test_links_in_both_modes(qapp, monkeypatch):
    answers = iter([("Docs", "https://docs.example"), ("", ""), ("Site", "https://example.com")])

    def fake_exec(dlg):
        text, url = next(answers)
        dlg.text_input.setText(text)
        dlg.url_input.setText(url)
        return QDialog.Accepted

    monkeypatch.setattr(LinkDialog, "exec", fake_exec)
    ed = MarkdownEditor()
    ed.set_markdown("see")
    from PySide6.QtGui import QTextCursor
    cur = ed.rich.textCursor()
    cur.movePosition(QTextCursor.End)
    ed.rich.setTextCursor(cur)
    ed.insert_link()
    assert ed.markdown().strip() == "see[Docs](https://docs.example)"
    ed.insert_link()  # empty URL: nothing inserted
    assert ed.markdown().strip() == "see[Docs](https://docs.example)"
    ed.set_mode(MarkdownEditor.MARKDOWN)
    cur = ed.source.textCursor()
    cur.movePosition(QTextCursor.End)
    ed.source.setTextCursor(cur)
    ed.insert_link()
    assert ed.markdown().endswith("[Site](https://example.com)")
    monkeypatch.setattr(LinkDialog, "exec", lambda dlg: QDialog.Rejected)
    ed.insert_link()
    assert LinkDialog("", "").values() == ("", "")
    assert LinkDialog("", "https://x.io").values() == ("https://x.io", "https://x.io"), "the URL is the fallback text"


def test_pasted_images_and_dropped_files_are_reported_not_embedded(qapp, tmp_path):
    ed = MarkdownEditor()
    img_md = QMimeData()
    image = QImage(4, 4, QImage.Format_RGB32)
    image.fill(QColor("#ff0000"))
    img_md.setImageData(image)
    with recording(ed.rich.image_pasted) as pasted:
        assert ed.rich.canInsertFromMimeData(img_md)
        ed.rich.insertFromMimeData(img_md)
    assert len(pasted) == 1 and pasted[0][0].width() == 4
    f = tmp_path / "notes.txt"
    f.write_text("x")
    file_md = QMimeData()
    file_md.setUrls([QUrl.fromLocalFile(str(f))])
    with recording(ed.rich.files_dropped) as dropped:
        ed.rich.insertFromMimeData(file_md)
    assert dropped == [([str(f)],)]
    text_md = QMimeData()
    text_md.setText("plain paste")
    ed.rich.insertFromMimeData(text_md)
    assert "plain paste" in ed.markdown()


# ---------------------------------------------------------------------------
# Time entries, attachments, timer menu
# ---------------------------------------------------------------------------
def test_time_entries_can_be_edited_and_deleted_only_by_their_owner(qapp, empty_service, menus, monkeypatch):
    built = make_board(empty_service, "Time", tasks=1)
    task_id = built.tasks[0]
    empty_service.add_time_entry(task_id, 1800, note="mine")
    empty_service.add_time_entry(task_id, 600, user="Bob", note="his")
    panel = TimePanel(empty_service, task_id, TimeTracker(empty_service))
    panel.show()
    flush_qt()
    assert panel.table.rowCount() == 2
    row_of = {panel.table.item(r, 0).data(Qt.UserRole): r for r in range(panel.table.rowCount())}
    entries = {e["note"]: e for e in empty_service.get_time_entries(task_id=task_id)}

    bob_row = row_of[entries["his"]["id"]]
    panel._context_menu(panel.table.visualItemRect(panel.table.item(bob_row, 0)).center())
    assert not menus.find("✏ Edit").isEnabled() and menus.find("Logged by Bob") is not None

    mine_row = row_of[entries["mine"]["id"]]
    panel._context_menu(panel.table.visualItemRect(panel.table.item(mine_row, 0)).center())
    assert menus.find("✏ Edit").isEnabled()

    def edit(dlg):
        dlg.duration_edit.setText("not a duration")
        dlg._validate()
        assert dlg.error_lbl.text() and dlg.result() != QDialog.Accepted
        dlg.duration_edit.setText("1h 15m")
        dlg.note_edit.setText("edited")
        dlg._validate()
        return dlg.result()

    monkeypatch.setattr(TimeEntryDialog, "exec", edit)
    with recording(panel.time_changed) as changed:
        menus.trigger("✏ Edit")
    entry = next(e for e in empty_service.get_time_entries(task_id=task_id) if e["id"] == entries["mine"]["id"])
    assert (entry["duration_seconds"], entry["note"]) == (4500, "edited") and changed

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    panel.table.selectRow(row_of[entries["mine"]["id"]])
    panel.delete_selected()
    assert len(empty_service.get_time_entries(task_id=task_id)) == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    panel.table.selectRow([r for r in range(2) if panel.table.item(r, 0).data(Qt.UserRole) ==
                           entries["mine"]["id"]][0])
    panel.delete_selected()
    assert [e["note"] for e in empty_service.get_time_entries(task_id=task_id)] == ["his"]
    panel.table.selectRow(0)
    panel.delete_selected(confirm=False)  # Bob's: not ours to delete
    assert len(empty_service.get_time_entries(task_id=task_id)) == 1


def test_time_entry_dialog_keeps_the_time_of_day_when_the_date_changes(qapp):
    start = datetime(2026, 3, 2, 14, 30)
    dlg = TimeEntryDialog({"started_at": start.isoformat(), "duration_seconds": 3600, "note": "n"})
    dlg.date_edit.setDate(dlg.date_edit.date().addDays(2))
    values = dlg.values()
    assert values["duration_seconds"] == 3600 and values["note"] == "n"
    assert values["started_at"] - start == timedelta(days=2)


def test_attachments_drop_remove_save_and_inline(qapp, empty_service, tmp_path, monkeypatch):
    built = make_board(empty_service, "Files", tasks=1)
    panel = AttachmentsPanel(empty_service, built.tasks[0])
    panel.show()
    photo = tmp_path / "photo.png"
    img = QImage(8, 8, QImage.Format_RGB32)
    img.fill(QColor("#00ff00"))
    img.save(str(photo))
    doc = tmp_path / "spec.pdf"
    doc.write_bytes(b"%PDF-1.4 fake")
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(photo)), QUrl.fromLocalFile(str(doc))])
    viewport = panel.list.viewport()  # item views take drags on their viewport
    enter = QDragEnterEvent(QPointF(5, 5), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(viewport, enter)
    assert enter.isAccepted()
    move = QDragMoveEvent(QPointF(6, 6), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(viewport, move)
    assert move.isAccepted()
    QApplication.sendEvent(viewport, QDropEvent(QPointF(5, 5), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier))
    flush_qt()
    assert sorted(e["filename"] for e in panel.entries()) == ["photo.png", "spec.pdf"] and panel.has_changes()
    mapping, errors = panel.commit(built.tasks[0])
    assert len(mapping) == 2 and not errors
    panel.reload()
    stored = {e["filename"]: e for e in panel.entries()}
    assert all(e["file_exists"] for e in stored.values())

    panel.list.setCurrentRow([i for i in range(panel.list.count())
                              if panel.list.item(i).text().startswith("photo")][0])
    assert panel.inline_btn.isEnabled() and panel.save_btn.isEnabled()
    with recording(panel.insert_inline_requested) as inline:
        panel._insert_inline()
    assert inline and inline[0][1] == "photo"
    dest = tmp_path / "copy.png"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(dest), ""))
    panel._save_as()
    assert dest.read_bytes() == photo.read_bytes()

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    panel.remove_selected()
    assert len(panel.entries()) == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    with recording(panel.attachment_removed) as gone:
        panel.remove_selected()
    assert gone and len(panel.entries()) == 1
    _mapping, errors = panel.commit(built.tasks[0])
    assert not errors and [a["filename"] for a in empty_service.get_attachments(built.tasks[0])] == ["spec.pdf"]


def test_pomodoro_settings_from_the_timer_menu(qapp, empty_service, monkeypatch):
    tracker = TimeTracker(empty_service)
    widget = TimerWidget(tracker)
    monkeypatch.setattr(PomodoroSettingsDialog, "exec", lambda dlg: (dlg.focus.setValue(50),
                                                                    dlg.sessions.setValue(3), dlg._save())[-1])
    widget._open_settings()
    cfg = tracker.settings()
    assert (cfg["focus_minutes"], cfg["sessions_before_long_break"]) == (50, 3)
    built = make_board(empty_service, "Timer", tasks=1)
    tracker.start_stopwatch(built.tasks[0])
    widget.refresh()
    widget._discard()
    assert tracker.task_id is None and not empty_service.get_time_entries(task_id=built.tasks[0])
    tracker.shutdown()


def test_theme_colour_pickers_switch_to_the_custom_theme(qapp, empty_service, monkeypatch):
    dlg = ThemeDialog(empty_service)
    for swatch in dlg.swatches.values():
        swatch.grab()  # every preview paints
    monkeypatch.setattr(QColorDialog, "getColor", lambda *a, **k: QColor("#123456"))
    dlg.pick_color("background")
    assert dlg.selected_key == "custom" and dlg._custom["background"] == "#123456"
    before = dict(dlg._custom)
    monkeypatch.setattr(QColorDialog, "getColor", lambda *a, **k: QColor())
    dlg.pick_color("text")
    assert dlg._custom == before, "a cancelled picker changes nothing"
    dlg.reset()
    assert dlg.selected_key == "dark" and dlg.scale_slider.value() == 100
    dlg.reject()

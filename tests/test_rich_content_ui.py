"""
UI tests for the Markdown/WYSIWYG editor, attachments panel, activity panel,
task dialog integration, task card badges, and demo-board creation.
"""
import os
from datetime import datetime

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from kanbanmanager import DatabaseService
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.enums import ActivityAction as A
from kanbanmanager.ui import MainWindow
from kanbanmanager.ui.components.activity_panel import (
    FILTER_ALL,
    FILTER_COMMENTS,
    FILTER_HISTORY,
    ActivityPanel,
    build_timeline,
    humanize_timestamp,
)
from kanbanmanager.ui.components.markdown_editor import MarkdownEditor
from kanbanmanager.ui.components.task_card import TaskCardWidget
from kanbanmanager.ui.dialogs.task_dialog import TaskDialog


@pytest.fixture(scope="module")
def qapp():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    return QApplication.instance() or QApplication([])


@pytest.fixture
def seeded(qapp, tmp_path):
    mgr = DatabaseManager(tmp_path / "ui_rich.db")
    mgr.init_database(seed_defaults=True)
    service = DatabaseService(mgr, actor="Ada")
    demo = [b for b in service.get_workspaces()[0].boards if b.name == DEMO_BOARD_NAME][0]
    tasks = {t.title: t for t in service.get_tasks_for_board(demo.id)}
    return service, demo, tasks, tmp_path


def png(path, w=120, h=80, color="#3B82F6"):
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor(color))
    img.save(str(path))
    return path


# ---------------------------------------------------------------------------
# Editor
# ---------------------------------------------------------------------------
def test_editor_returns_untouched_text_verbatim(qapp):
    ed = MarkdownEditor()
    original = "Plain legacy text\nwith a soft break   \n\n\n"
    ed.set_markdown(original)
    assert ed.markdown() == original and not ed.is_modified()
    ed.set_mode(MarkdownEditor.MARKDOWN)
    ed.set_mode(MarkdownEditor.VISUAL)
    assert ed.markdown() == original
    ed.rich.textCursor().insertText("x")
    ed.rich.undo()
    assert not ed.is_modified(), "an undone edit is not a change"


def test_editor_visual_formatting_produces_markdown(qapp):
    ed = MarkdownEditor()
    ed.set_markdown("hello world\n\nnext")
    cur = ed.rich.textCursor()
    cur.setPosition(0)
    cur.setPosition(5, QTextCursor.KeepAnchor)
    ed.rich.setTextCursor(cur)
    ed.toggle_bold()
    assert ed.markdown().startswith("**hello** world")
    cur.setPosition(6)
    cur.setPosition(11, QTextCursor.KeepAnchor)
    ed.rich.setTextCursor(cur)
    ed.toggle_code()
    assert "`world`" in ed.markdown()

    cur = ed.rich.textCursor()
    cur.movePosition(QTextCursor.End)
    ed.rich.setTextCursor(cur)
    for toggle, expected in (
        (ed.toggle_tasks, "- [ ] next"),
        (ed.toggle_bullets, "- next"),
        (ed.toggle_numbers, "1.  next"),
        (ed.toggle_quote, "> next"),
    ):
        if toggle == ed.toggle_quote:
            ed.toggle_numbers()  # leave the list first
        toggle()
        assert ed.markdown().endswith(expected), (expected, ed.markdown())
    ed.toggle_quote()
    ed.toggle_heading(2)
    assert ed.markdown().endswith("## next")
    ed.toggle_heading(2)
    ed.toggle_code_block()
    assert ed.markdown().endswith("```\nnext\n```")
    assert ed.is_modified()


def test_editor_task_checkbox_click_and_markdown_mode(qapp):
    ed = MarkdownEditor()
    ed.resize(500, 300)
    ed.show()
    ed.set_markdown("- [ ] one\n- [x] two")
    qapp.processEvents()
    layout = ed.rich.document().documentLayout()
    rect = layout.blockBoundingRect(ed.rich.document().begin())
    x = next(x for x in range(0, 80) if layout.blockWithMarkerAt(QPointF(x, rect.center().y())).isValid())
    QTest.mouseClick(ed.rich.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(x + 1, int(rect.center().y())))
    assert ed.markdown().startswith("- [x] one")

    ed.set_markdown("a\nb")
    ed.set_mode(MarkdownEditor.MARKDOWN)
    cur = ed.source.textCursor()
    cur.select(QTextCursor.Document)
    ed.source.setTextCursor(cur)
    ed.toggle_numbers()
    assert ed.markdown() == "1. a\n2. b"
    ed.insert_image("9", "pic")
    assert "![pic](attachment:9)" in ed.markdown()


def test_mode_switch_keeps_cursor_on_the_same_block(qapp):
    ed = MarkdownEditor()
    ed.set_markdown("# Title\n\nfirst para\n\n- item **bold** end")
    cur = ed.rich.textCursor()
    cur.movePosition(QTextCursor.End)
    ed.rich.setTextCursor(cur)
    ed.set_mode(MarkdownEditor.MARKDOWN)
    QTest.keyClicks(ed.source, "!")
    assert ed.markdown().endswith("- item **bold** end!")
    ed.set_mode(MarkdownEditor.VISUAL)
    QTest.keyClicks(ed.rich, "?")
    assert ed.markdown().endswith("- item **bold** end!?")
    assert ed.markdown().startswith("# Title")


def test_editor_resolves_inline_attachment_images(qapp, tmp_path):
    path = png(tmp_path / "wide.png", 2000, 100)
    ed = MarkdownEditor()
    ed.set_resource_resolver(lambda ref: str(path) if ref == "5" else None)
    ed.set_markdown("![x](attachment:5)")
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QTextDocument
    img = ed.rich.loadResource(QTextDocument.ImageResource, QUrl("attachment:5"))
    assert isinstance(img, QImage) and 0 < img.width() <= 560, "large images are scaled for display"
    missing = ed.rich.loadResource(QTextDocument.ImageResource, QUrl("attachment:6"))
    assert isinstance(missing, QImage) and not missing.isNull(), "placeholder instead of a broken image"


# ---------------------------------------------------------------------------
# Task dialog integration
# ---------------------------------------------------------------------------
def test_opening_and_saving_unchanged_task_adds_no_history(seeded):
    service, demo, tasks, _ = seeded
    task = tasks["Design the new onboarding flow"]  # has dates, labels, checklist, deps on board
    before = service.get_task_activity(task.id)
    dlg = TaskDialog(board_id=demo.id, task_id=task.id, service=service)
    assert dlg.attachments_panel.count() == 3
    assert dlg.activity_panel.comment_count() == 4
    dlg._save_task()
    assert service.get_task_activity(task.id) == before
    assert service.get_task(task.id).description == task.description


def test_new_task_with_inline_pasted_image(seeded, monkeypatch):
    service, demo, _, tmp = seeded
    dlg = TaskDialog(board_id=demo.id, service=service)
    dlg.title_input.setText("Screenshot bug")
    dlg.desc_editor.set_mode(MarkdownEditor.MARKDOWN)
    dlg.desc_editor.source.setPlainText("Steps **below**\n")
    dlg.desc_editor.source.document().setModified(True)
    img = QImage(50, 40, QImage.Format_RGB32)
    img.fill(QColor("red"))
    dlg._on_image_pasted(img)
    (tmp / "notes.txt").write_text("n")
    with_warning = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: with_warning.append(a))
    dlg._on_files_dropped([str(tmp / "notes.txt"), str(tmp / "does-not-exist.txt")])
    assert len(with_warning) == 1, "missing files are reported, the rest are still attached"
    assert "attachment:new-1" in dlg.desc_editor.markdown()
    assert dlg.attachments_panel.count() == 2
    dlg._save_task()

    task = [t for t in service.get_tasks_for_board(demo.id) if t.title == "Screenshot bug"][0]
    atts = service.get_attachments(task.id)
    assert {a["filename"].split("_")[0] for a in atts} == {"pasted", "notes.txt"}
    image = next(a for a in atts if a["is_image"])
    assert f"(attachment:{image['id']})" in task.description and "new-" not in task.description
    assert actions_of(service, task.id) == [A.TASK_CREATED.value, A.ATTACHMENT_ADDED.value, A.ATTACHMENT_ADDED.value]
    dlg.done(0)
    assert dlg.attachments_panel._tmpdir is None, "temporary paste files are cleaned up"


def actions_of(service, task_id):
    return [e["action_type"] for e in service.get_task_activity(task_id)]


def test_removing_inline_attachment_updates_description_and_files(seeded, monkeypatch):
    service, demo, tasks, _ = seeded
    task = tasks["Design the new onboarding flow"]
    wire = next(a for a in service.get_attachments(task.id) if "wireframe" in a["filename"])
    assert f"attachment:{wire['id']}" in task.description
    dlg = TaskDialog(board_id=demo.id, task_id=task.id, service=service)
    panel = dlg.attachments_panel
    panel.list.clearSelection()
    for i in range(panel.list.count()):
        if panel.list.item(i).data(Qt.UserRole) == str(wire["id"]):
            panel.list.item(i).setSelected(True)
    panel.remove_selected(confirm=False)
    assert f"attachment:{wire['id']}" not in dlg.desc_editor.markdown()
    assert service.get_attachment(wire["id"]) is not None, "nothing is deleted before Save"
    dlg._save_task()
    assert service.get_attachment(wire["id"]) is None
    saved = service.get_task(task.id)
    assert f"attachment:{wire['id']}" not in saved.description
    recent = [e["action_type"] for e in service.get_task_activity(task.id)[-2:]]
    assert recent == [A.ATTACHMENT_REMOVED.value, A.TASK_UPDATED.value]


def test_cancel_with_staged_changes_asks_first(seeded, monkeypatch):
    service, demo, tasks, tmp = seeded
    task = tasks["✏️ Your turn: edit this card"]
    dlg = TaskDialog(board_id=demo.id, task_id=task.id, service=service)
    dlg.attachments_panel.add_files([str(png(tmp / "x.png"))])
    closed = []
    dlg.finished.connect(closed.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    dlg.reject()
    assert closed == [], "answering Cancel keeps the dialog open"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard)
    dlg.reject()
    assert closed == [0]
    assert service.get_attachments(task.id) == []


# ---------------------------------------------------------------------------
# Activity panel
# ---------------------------------------------------------------------------
def test_activity_panel_comment_flow(seeded, monkeypatch):
    service, demo, tasks, _ = seeded
    task = tasks["✏️ Your turn: edit this card"]
    panel = ActivityPanel(service, task.id)
    changes = []
    panel.comments_changed.connect(lambda: changes.append(1))
    panel.comment_input.setPlainText("Ship it **today**")
    panel.post_comment()
    assert panel.comment_input.toPlainText() == ""
    comment = service.get_comments(task.id)[-1]
    assert comment["author"] == "Ada"

    from kanbanmanager.ui.components import activity_panel as ap
    monkeypatch.setattr(ap.CommentEditDialog, "exec", lambda self: True)
    monkeypatch.setattr(ap.CommentEditDialog, "body", lambda self: "Ship it tomorrow")
    panel.edit_comment(comment["id"])
    assert service.get_comments(task.id)[-1]["is_edited"]
    panel.delete_comment(comment["id"], confirm=False)
    assert service.get_comments(task.id) == []
    assert len(changes) == 3

    panel.set_filter(FILTER_HISTORY)
    kinds = [i["data"]["action_type"] for i in panel.timeline_items()]
    assert kinds[-3:] == [A.COMMENT_ADDED.value, A.COMMENT_EDITED.value, A.COMMENT_DELETED.value]


def test_activity_panel_without_task_shows_placeholder(seeded):
    service, *_ = seeded
    panel = ActivityPanel(service, None)
    assert panel.placeholder.isVisibleTo(panel) and not panel.composer.isVisibleTo(panel)


def test_timeline_filters_and_ordering():
    comments = [{"id": 1, "created_at": "2024-01-02T10:00:00", "author": "A", "body": "hi"}]
    activity = [
        {"action_type": A.TASK_CREATED.value, "timestamp": "2024-01-01T09:00:00"},
        {"action_type": A.COMMENT_ADDED.value, "timestamp": "2024-01-02T10:00:00"},
        {"action_type": A.COMMENT_DELETED.value, "timestamp": "2024-01-03T10:00:00"},
    ]
    all_items = build_timeline(comments, activity, FILTER_ALL)
    assert [i["kind"] for i in all_items] == ["history", "comment", "history"]
    assert len(build_timeline(comments, activity, FILTER_COMMENTS)) == 1
    assert len(build_timeline(comments, activity, FILTER_HISTORY)) == 3


def test_humanize_timestamp():
    now = datetime(2024, 6, 10, 12, 0, 0)
    assert humanize_timestamp(datetime(2024, 6, 10, 11, 59, 30), now) == "just now"
    assert humanize_timestamp(datetime(2024, 6, 10, 11, 0, 0), now) == "1 hour ago"
    assert humanize_timestamp(datetime(2024, 6, 9, 10, 0, 0), now) == "yesterday"
    assert humanize_timestamp(datetime(2024, 6, 6, 12, 0, 0), now) == "4 days ago"
    assert "2024" in humanize_timestamp(datetime(2024, 1, 1), now)


# ---------------------------------------------------------------------------
# Cards & main window
# ---------------------------------------------------------------------------
def test_task_card_shows_badges_and_markdown_free_snippet(seeded):
    service, demo, tasks, _ = seeded
    data = service.get_task(tasks["Crash when importing large CSV files"].id).to_dict()
    card = TaskCardWidget(data)
    texts = [lbl.text() for lbl in card.findChildren(type(card.title_lbl))]
    assert "📎 3" in texts and "💬 3" in texts
    assert any(t.startswith("Importing a CSV with more than") for t in texts)
    assert not any("##" in t or "**" in t for t in texts)


def test_main_window_adds_demo_board_and_refreshes_after_comment(seeded, monkeypatch):
    service, demo, tasks, _ = seeded
    win = MainWindow(service=service)
    ws_id = demo.workspace_id
    new_board_id = win.create_demo_board(ws_id)
    boards = service.get_boards_for_workspace(ws_id)
    assert [b.name for b in boards].count(DEMO_BOARD_NAME) == 2
    assert win.current_board_id == new_board_id

    # A comment posted and then "Cancel" still refreshes the board (counts change)
    refreshed = []
    monkeypatch.setattr(win, "refresh_active_view", lambda: refreshed.append(1))

    def fake_exec(self):
        self.activity_panel.comment_input.setPlainText("via dialog")
        self.activity_panel.post_comment()
        return 0

    monkeypatch.setattr(TaskDialog, "exec", fake_exec)
    some_task = service.get_tasks_for_board(new_board_id)[0]
    win._open_edit_task_dialog(some_task.id)
    assert refreshed, "board refreshed although the dialog was cancelled"
    win.reminder_engine.stop()

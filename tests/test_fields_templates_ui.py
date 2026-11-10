"""
UI tests for custom fields and templates: value editors, the task editor's
Fields tab (validation, required fields, defaults) and template support, the
field manager, card chips and column totals, the field filter (and the due-date
filters), the new-board template gallery, task template dialogs, automation
rule rows with a custom field, and main-window wiring.
"""
from datetime import date, datetime, time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from kanbanmanager import DatabaseService
from kanbanmanager.core import automation as au
from kanbanmanager.core import custom_fields as cf
from kanbanmanager.core import templates as tpl
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.ui import MainWindow
from kanbanmanager.ui.components.field_editors import (
    CheckboxEditor, EditorContext, FieldsPanel, MultiSelectEditor, RatingEditor, TextLineEditor, create_field_editor,
    register_field_editor,
)
from kanbanmanager.ui.components import column_widget as column_widget_module
from kanbanmanager.ui.components.task_card import TaskCardWidget
from kanbanmanager.ui.dialogs.automation_dialog import ConditionRow, ParamWidgets
from kanbanmanager.ui.dialogs.custom_fields_dialog import NEW_ID, CustomFieldsDialog
from kanbanmanager.ui.dialogs.task_dialog import TaskDialog
from kanbanmanager.ui.dialogs.template_dialogs import (
    NewBoardDialog, SaveBoardTemplateDialog, TaskTemplateEditor, TaskTemplatesDialog,
)
from kanbanmanager.ui.views.board_view import BoardView, filter_tasks
from support import seeded_manager


@pytest.fixture
def env(qapp, tmp_path):
    mgr = seeded_manager(tmp_path / "ui.db")
    service = DatabaseService(mgr, actor="Ann")
    demo = next(b for ws in service.get_workspaces() for b in ws.boards if b.name == DEMO_BOARD_NAME)
    tasks = {t.title: t for t in service.get_tasks_for_board(demo.id)}
    return service, demo, tasks


@pytest.fixture
def no_dialogs(monkeypatch):
    """Record message boxes instead of showing them."""
    shown = []

    def box(kind, answer):
        def show(parent, title, text, *args, **kwargs):
            shown.append((kind, title, text))
            return answer
        return show

    monkeypatch.setattr(QMessageBox, "warning", box("warning", QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "question", box("question", QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, "information", box("information", QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "critical", box("critical", QMessageBox.Ok))
    return shown


def definition(fid, name, ftype, config=None, **kw):
    return {"id": fid, "name": name, "field_type": ftype, "config": cf.get_field_type(ftype).normalize_config(config),
            "required": False, "show_on_card": True, "description": "", "default": None, **kw}


# ---------------------------------------------------------------------------
# Editors
# ---------------------------------------------------------------------------
def test_every_editor_round_trips_its_value(qapp):
    fields = [
        definition(1, "Text", "text"), definition(2, "Notes", "text", {"multiline": True}),
        definition(3, "Points", "number", {"unit": "pts"}), definition(4, "Budget", "currency"),
        definition(5, "Done %", "percent"), definition(6, "Sev", "select", {"options": ["A", "B"]}),
        definition(7, "Tags", "multi_select", {"options": ["X", "Y", "Z"]}), definition(8, "Ok", "checkbox"),
        definition(9, "When", "date"), definition(10, "Stars", "rating", {"max": 4}),
        definition(11, "Who", "person"), definition(12, "Link", "url"), definition(13, "Mail", "email"),
    ]
    values = {1: "hello", 2: "two\nlines", 3: 5, 4: 12.5, 5: 40, 6: "B", 7: ["X", "Z"], 8: True,
              9: "2031-03-04", 10: 3, 11: "Priya", 12: "https://example.com", 13: "a@b.co"}
    panel = FieldsPanel(EditorContext(people=["Priya"]))
    panel.set_fields(fields, values)
    assert panel.values() == values and panel.errors() == [] and panel.filled_count() == 13
    panel.set_values({})
    assert panel.values() == {f["id"]: None for f in fields}

    rating = panel.editors[10]
    assert isinstance(rating, RatingEditor)
    rating.stars[1].click()
    assert rating.value() == 2
    rating.stars[1].click()
    assert rating.value() is None, "clicking the same star again clears it"
    multi = panel.editors[7]
    assert isinstance(multi, MultiSelectEditor)
    multi.buttons["Y"].setChecked(True)
    assert multi.value() == ["Y"]
    assert isinstance(panel.editors[8], CheckboxEditor)

    number = panel.editors[3]
    number.edit.setText("many")
    assert panel.errors() == ["Points: 'many' is not a number"]
    assert panel.error_labels[3].isVisibleTo(panel) and "not a number" in panel.error_labels[3].text()
    number.edit.setText("7")
    assert not panel.error_labels[3].isVisibleTo(panel)


def test_unknown_types_get_a_read_only_editor_and_new_types_can_register_one(qapp):
    orphan = {"id": 1, "name": "Mood", "field_type": "emotion", "config": {}}
    editor = create_field_editor(orphan)
    assert isinstance(editor, TextLineEditor) and editor.edit.isReadOnly()
    editor.set_value({"happy": True})
    assert editor.value() == {"happy": True}, "kept untouched"

    class Phone(cf.FieldType):
        key, label = "test_ui_phone", "Phone"

    class PhoneEditor(TextLineEditor):
        pass

    cf.register_field_type(Phone())
    try:
        phone = {"id": 2, "name": "Phone", "field_type": "test_ui_phone", "config": {}}
        assert type(create_field_editor(phone)) is TextLineEditor, "generic editor without UI code"
        register_field_editor("test_ui_phone", PhoneEditor)
        assert type(create_field_editor(phone)) is PhoneEditor
    finally:
        cf.unregister_field_type("test_ui_phone")


# ---------------------------------------------------------------------------
# Task editor
# ---------------------------------------------------------------------------
def test_task_dialog_fields_tab_required_defaults_and_history(env, no_dialogs):
    service, demo, tasks = env
    sev = service.create_custom_field(demo.id, "Severity", "select", {"options": ["Critical", "Minor"]},
                                      required=True)
    points_id = next(f["id"] for f in service.get_custom_fields(demo.id) if f["name"] == "Story points")
    service.update_custom_field(points_id, default=1)

    dlg = TaskDialog(board_id=demo.id, service=service)
    assert dlg.fields_panel.raw_values()[points_id] == 1, "new tasks start with defaults"
    assert dlg.tabs.tabText(dlg._fields_tab) == "Fields (1) ⚠"
    dlg.title_input.setText("Needs severity")
    dlg._save_task()
    assert no_dialogs[-1][1] == "Check the fields" and "Severity: required" in no_dialogs[-1][2]
    assert dlg.tabs.currentIndex() == dlg._fields_tab and dlg.result() == 0
    assert "Needs severity" not in [t.title for t in service.get_tasks_for_board(demo.id)]
    dlg.fields_panel.editors[sev["id"]].set_value("Minor")
    dlg.fields_panel.editors[points_id].set_value(None)  # clearing the default sticks
    dlg.fields_panel.editors[points_id].edit.setText("abc")
    dlg._save_task()
    assert "Story points: 'abc' is not a number" in no_dialogs[-1][2]
    dlg.fields_panel.editors[points_id].edit.setText("")
    dlg._save_task()
    assert dlg.result() == 1
    created = next(t for t in service.get_tasks_for_board(demo.id) if t.title == "Needs severity")
    assert created.custom_fields == {"Severity": "Minor"}

    onboarding = tasks["Design the new onboarding flow"]
    dlg = TaskDialog(board_id=demo.id, task_id=onboarding.id, service=service)
    assert dlg.tabs.tabText(dlg._fields_tab) == "Fields (5) ⚠", "5 values, required Severity missing"
    dlg.fields_panel.editors[sev["id"]].set_value("Critical")
    dlg.fields_panel.editors[points_id].edit.setText("13")
    dlg._save_task()
    assert service.get_task(onboarding.id).custom_fields["Story points"] == 13
    log = [a["details"] for a in service.get_task_activity(onboarding.id)]
    assert log[-2:] == ["Changed Story points from 8 pts to 13 pts", "Set Severity to Critical"]
    assert "Severity" not in [f["name"] for f in TaskDialog(board_id=next(
        b.id for ws in service.get_workspaces() for b in ws.boards if b.name == "Product Roadmap"),
        service=service).fields_panel.fields], "fields are per board"


def test_task_dialog_tabs_all_fit_at_the_default_size(env):
    service, demo, tasks = env
    dlg = TaskDialog(board_id=demo.id, task_id=tasks["Crash when importing large CSV files"].id, service=service)
    dlg.show()
    QApplication.processEvents()
    bar = dlg.tabs.tabBar()
    needed = sum(bar.tabSizeHint(i).width() for i in range(bar.count()))
    assert bar.count() == 9 and needed <= dlg.tabs.width(), f"tabs need {needed}px of {dlg.tabs.width()}px"
    dlg.close()


def test_task_dialog_applies_templates_and_saves_new_labels(env, no_dialogs):
    service, demo, _tasks = env
    bug = next(t for t in service.get_task_templates(demo.id) if t["name"] == "Bug report")
    dlg = TaskDialog(board_id=demo.id, service=service, template=bug)
    assert dlg.title_input.text() == "Bug: " and dlg.priority_combo.currentData() == "HIGH"
    checked = [dlg.labels_list.item(i).text() for i in range(dlg.labels_list.count())
               if dlg.labels_list.item(i).checkState() == Qt.Checked]
    assert checked == ["🏷 Bug"]
    assert [i["text"] for i in dlg._checklist_items] == ["Reproduce", "Write a failing test", "Fix", "Verify"]
    points_id = next(f["id"] for f in dlg.board_fields if f["name"] == "Story points")
    assert dlg.fields_panel.raw_values()[points_id] == 2
    assert "Filled in from the 'Bug report' template" in dlg.template_note.text()

    # The menu lists the board's templates; applying another one over typed content asks first
    dlg._fill_template_menu()
    names = [a.text() for a in dlg.template_menu.actions() if a.text()]
    assert names[:2] == ["🐞  Bug report", "📝  Weekly status report"] and names[-1] == "Manage Templates…"
    fresh = service.save_task_template("Onboard", {"title": "Welcome {user}", "labels": ["Brand new"],
                                                   "due_in_days": 1, "assignee": "@me"}, board_id=demo.id)
    dlg._apply_template_clicked(fresh)
    assert no_dialogs[-1][0] == "question" and dlg.title_input.text() == "Welcome Ann"
    new_label = [dlg.labels_list.item(i) for i in range(dlg.labels_list.count())
                 if dlg.labels_list.item(i).text().startswith("🏷 Brand new")]
    assert new_label and new_label[0].checkState() == Qt.Checked
    assert dlg.has_due_chk.isChecked() and dlg.assignee_combo.currentText() == "Ann"
    dlg._save_task()
    task = next(t for t in service.get_tasks_for_board(demo.id) if t.title == "Welcome Ann")
    assert "Brand new" in [l.name for l in task.labels], "the template's missing label is created on save"
    assert service.get_task_activity(task.id)[0]["details"].endswith("(from template 'Onboard')")


def test_saving_a_task_as_a_template(env, monkeypatch, no_dialogs):
    service, demo, tasks = env
    crash = tasks["Crash when importing large CSV files"]
    dlg = TaskDialog(board_id=demo.id, task_id=crash.id, service=service)
    data = dlg.template_data()
    assert data["title"] == crash.title and "attachment:" not in data["description"]
    assert data["fields"] == {"Story points": 5, "Component": "Data", "Customer impact": 5}
    assert data["labels"] == ["Bug"]

    def fake_exec(editor):
        editor.name_input.setText("Crash template")
        editor.scope_combo.setCurrentIndex(1)  # all boards
        return bool(editor.save())

    monkeypatch.setattr(TaskTemplateEditor, "exec", fake_exec)
    saved = dlg.save_as_template()
    assert saved["name"] == "Crash template" and saved["board_id"] is None
    assert saved["data"]["fields"]["Component"] == "Data"


# ---------------------------------------------------------------------------
# Field manager
# ---------------------------------------------------------------------------
def test_field_manager_creates_edits_and_deletes_fields(env, no_dialogs):
    service, demo, tasks = env
    board = service.create_board(demo.workspace_id, "Fresh")
    col = service.get_columns_for_board(board.id)[0]
    dlg = CustomFieldsDialog(service, board.id)
    assert dlg.current["id"] == NEW_ID, "an empty board starts with a new field"
    dlg.name_input.setText("Severity")
    dlg.type_combo.setCurrentIndex(dlg.type_combo.findData("select"))
    opts = dlg.type_settings.options_editor
    for label in ("Critical", "Minor", "critical"):
        opts.new_input.setText(label)
        opts.add_from_input()
    assert [o["label"] for o in opts.options()] == ["Critical", "Minor"], "duplicates ignored"
    assert dlg.default_editor.combo.count() == 3, "default editor follows the options (— + 2)"
    dlg.default_editor.set_value("Minor")
    dlg.required_chk.setChecked(True)
    saved = dlg.save_current()
    assert saved["name"] == "Severity" and saved["default"] == "Minor" and saved["required"] and dlg.changed
    task = service.create_task(board.id, col.id, "T", field_values={"Severity": "Critical"})

    # Rename an option in place: the value follows
    opts = dlg.type_settings.options_editor
    opts.list.item(0).setText("P1")
    dlg.save_current()
    assert service.get_task(task.id).custom_fields == {"Severity": "P1"}

    # A duplicate name is refused with a message
    dlg.new_field()
    dlg.name_input.setText("severity")
    assert dlg.save_current() is None and "already has a field named" in dlg.error_lbl.text()

    # Type change that loses values: cancel keeps everything, confirming applies it
    dlg.reload(saved["id"])
    dlg.type_combo.setCurrentIndex(dlg.type_combo.findData("number"))
    QMessageBox.warning = lambda *a, **k: QMessageBox.Cancel
    assert dlg.save_current() is None
    assert service.get_custom_field(saved["id"])["field_type"] == "select"
    assert dlg.save_current(confirm=False)["field_type"] == "number"
    assert service.get_task(task.id).custom_fields == {}

    # Number settings + reorder + delete
    dlg.type_settings.widgets["unit"].setText("pts")
    dlg.type_settings.widgets["show_total"].setChecked(True)
    dlg.type_settings.widgets["min"].setText("oops")
    assert dlg.save_current() is None and "must be a number" in dlg.error_lbl.text()
    dlg.type_settings.widgets["min"].setText("0")
    assert dlg.save_current()["config"] == {"decimals": 0, "min": 0, "max": None, "unit": "pts", "show_total": True}
    dlg.new_field()
    dlg.name_input.setText("Link")
    dlg.type_combo.setCurrentIndex(dlg.type_combo.findData("url"))
    dlg.save_current()
    dlg.move_selected(-1)
    assert [f["name"] for f in service.get_custom_fields(board.id)] == ["Link", "Severity"]
    assert dlg.delete_selected(confirm=False)
    assert [f["name"] for f in service.get_custom_fields(board.id)] == ["Severity"]


# ---------------------------------------------------------------------------
# Board: chips, totals, menus, filters
# ---------------------------------------------------------------------------
def test_cards_columns_and_board_filters(env, monkeypatch):
    service, demo, tasks = env
    service.create_custom_field(demo.id, "Severity", "select", {"options": [{"label": "Critical", "color": "#DC2626"}]},
                                required=True)
    view = BoardView(service)
    view.resize(1600, 900)
    view.show()
    view.load_board(demo.id)
    QApplication.processEvents()
    cards = {c.task_data["title"]: c for c in view.findChildren(TaskCardWidget)}
    chips = [c.text() for c in cards["Design the new onboarding flow"].field_chips]
    assert chips[:4] == ["8 pts", "Frontend", "★★★★", "🔗 figma.com"], chips
    assert "⚠ 1 required field" in chips and not any("Marcus" in c for c in chips), "Reviewer is hidden"
    assert "#DC2626" not in cards["Design the new onboarding flow"].field_chips[1].styleSheet()
    done_card = cards["Publish the Q3 performance report"]
    assert not any(c.text().startswith("⚠") for c in done_card.field_chips), "no nagging on completed tasks"

    backlog = next(w for w in view.column_widgets.values() if w.column_data["name"] == "Backlog")
    expected = sum(t.custom_fields.get("Story points", 0) for t in tasks.values()
                   if t.column_id == backlog.column_id and not t.is_archived)
    assert backlog.totals_lbl.text() == f"Σ {expected} pts" and backlog.totals_lbl.isVisibleTo(backlog)

    # The column asks for a task from a template (via a stand-in for the global signal bus, so
    # main windows left over from other tests don't open a modal editor)
    got = []

    class Bus:
        class task_from_template_requested:
            @staticmethod
            def emit(*args):
                got.append(args)

    monkeypatch.setattr(column_widget_module, "app_signals", Bus)
    assert [t["name"] for t in backlog.templates] == ["Bug report", "Weekly status report"]
    backlog.request_from_template(backlog.templates[0]["id"])
    assert got == [(demo.id, backlog.column_id, backlog.templates[0]["id"])]

    # Field filter: "Component: is Frontend"
    comp = next(f for f in service.get_custom_fields(demo.id) if f["name"] == "Component")
    view.apply_filters({"field_filter": f"{comp['id']}|eq:Frontend"})
    shown = {c.task_data["title"] for c in view.findChildren(TaskCardWidget) if c.isVisibleTo(view)}
    QApplication.processEvents()
    shown = {t for col in view.column_widgets.values() for t in col.task_ids}
    assert shown == {t.id for t in tasks.values() if t.custom_fields.get("Component") == "Frontend"
                     and not t.is_archived}
    view.close()


def test_due_date_filters_today_and_this_week(env):
    """Regression: the "Due Today" / "Due This Week" filters used to show everything."""
    service, demo, _tasks = env
    today = date(2031, 1, 8)  # a Wednesday
    col = service.get_columns_for_board(demo.id)[0]
    mk = lambda title, d: service.create_task(demo.id, col.id, title, due_date=datetime.combine(d, time(17)))
    a, b, c = mk("today", today), mk("sunday", date(2031, 1, 12)), mk("next monday", date(2031, 1, 13))
    tasks = [service.get_task(x.id) for x in (a, b, c)]
    assert [t.title for t in filter_tasks(tasks, {"due_filter": "today"}, [], today)] == ["today"]
    assert [t.title for t in filter_tasks(tasks, {"due_filter": "this_week"}, [], today)] == ["today", "sunday"]
    assert len(filter_tasks(tasks, {"due_filter": "all"}, [], today)) == 3


# ---------------------------------------------------------------------------
# Template dialogs
# ---------------------------------------------------------------------------
def test_new_board_dialog_gallery(env, tmp_path, no_dialogs):
    service, demo, _tasks = env
    dlg = NewBoardDialog(service, demo.workspace_id)
    assert dlg.selected()["id"] == "builtin:standard" and dlg.name_input.text() == ""
    dlg.create_board()
    assert dlg.created_board_id is None and no_dialogs[-1][1] == "Board name"
    assert dlg.select_template("builtin:sprint")
    assert dlg.name_input.text() == "Software Sprint" and "Story points" in dlg.preview.text()
    assert dlg.samples_chk.isEnabled() and "2 sample tasks" in dlg.samples_chk.text()
    assert not dlg.delete_btn.isEnabled(), "built-ins can't be deleted"
    dlg.name_input.setText("Sprint 42")
    dlg.samples_chk.setChecked(False)
    board_id = dlg.create_board()
    board = service.get_board(board_id)
    assert board.name == "Sprint 42" and [c.name for c in board.columns][3] == "Code Review"
    assert service.get_tasks_for_board(board_id) == [] and len(service.get_custom_fields(board_id)) == 4

    # Save that board as a template, export it, delete it, import it back
    save = SaveBoardTemplateDialog(service, board_id)
    save.name_input.setText("Our sprint")
    saved = save.save()
    gallery = NewBoardDialog(service, demo.workspace_id, preselect=saved["id"])
    assert gallery.selected()["name"] == "Our sprint" and gallery.delete_btn.isEnabled()
    path = gallery.export_template(path=str(tmp_path / f"sprint{tpl.TEMPLATE_FILE_SUFFIX}"))
    assert gallery.delete_template(confirm=False)
    assert all(t["name"] != "Our sprint" for t in service.get_board_templates())
    imported = gallery.import_template(path=str(path))
    assert imported["name"] == "Our sprint" and gallery.selected()["id"] == imported["id"]
    (tmp_path / "junk.json").write_text("{}")
    assert gallery.import_template(path=str(tmp_path / "junk.json")) is None
    assert "not a KanbanManager board template" in no_dialogs[-1][2]


def test_task_templates_dialog_and_editor(env, no_dialogs):
    service, demo, _tasks = env
    dlg = TaskTemplatesDialog(service, demo.id)
    assert dlg.list.count() == 2 and dlg.selected()["name"] == "Bug report"
    assert "High · Bug · 4 checklist items · 1 field value" in dlg.list.item(0).text()
    copy = dlg.duplicate_selected()
    assert copy["name"] == "Bug report (copy)" and dlg.selected()["id"] == copy["id"]
    assert dlg.duplicate_selected()["name"] == "Bug report (copy 2)"
    assert dlg.delete_selected(confirm=False)

    status = next(t for t in service.get_task_templates(demo.id) if t["name"] == "Weekly status report")
    editor = TaskTemplateEditor(service, demo.id, status)
    assert editor.data() == tpl.normalize_task_template(status["data"]), "loads and reads back unchanged"
    assert editor.column_combo.currentData() == "To Do" and editor.due_chk.isChecked()
    editor.due_spin.setValue(5)
    editor.working_chk.setChecked(False)
    editor.checklist_edit.setPlainText("One\n\nTwo")
    editor.fields_panel.editors[next(f["id"] for f in editor.fields if f["name"] == "Component")].set_value("Backend")
    assert "due in 5 days" in editor.summary.text()
    saved = editor.save()
    assert saved["id"] == status["id"] and saved["data"]["checklist"] == ["One", "Two"]
    assert saved["data"]["fields"]["Component"] == "Backend" and saved["data"]["working_days"] is False

    # Values for fields of other boards (global templates) survive editing on this board
    glob = service.save_task_template("Global", {"title": "G", "fields": {"Elsewhere": 3, "Component": "Docs"}})
    editor = TaskTemplateEditor(service, demo.id, glob)
    assert editor.data()["fields"] == {"Elsewhere": 3, "Component": "Docs"}
    new = TaskTemplateEditor(service, demo.id)
    new.name_input.setText("Bug report")
    assert new.save() is None and "already a template named 'Bug report'" in new.error_lbl.text()


def test_condition_rows_pick_a_custom_field(env):
    service, demo, _tasks = env
    ctx = au.AutomationEngine(service).board_context(demo.id)
    comp = next(f for f in ctx.fields.values() if f["name"] == "Component")
    row = ConditionRow(ParamWidgets(ctx, []), {"field": "custom_field", "field_id": comp["id"], "op": "is",
                                              "value": "Docs"})
    assert row.value() == {"field": "custom_field", "field_id": comp["id"], "op": "is", "value": "Docs"}
    row.op_combo.setCurrentIndex(row.op_combo.findData("is_empty"))
    assert row.value() == {"field": "custom_field", "field_id": comp["id"], "op": "is_empty"}
    row.kind_combo.setCurrentIndex(row.kind_combo.findData("priority"))
    assert "field_id" not in row.value() and row.subject_widget is None


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
def test_main_window_fields_and_templates_wiring(env, monkeypatch):
    service, demo, _tasks = env
    win = MainWindow(service=service)
    win.resize(win.minimumWidth(), 800)
    win.show()
    win.open_board(demo.id)
    QApplication.processEvents()
    bar, stats = win.filter_bar, win.stats_bar
    assert bar.field_combo.isVisibleTo(bar) and bar.field_combo.count() > 10
    widgets = [bar.search_input, bar.priority_combo, bar.label_combo, bar.due_combo, bar.status_combo,
               bar.field_combo, bar.reset_btn]
    rects = [w.geometry() for w in widgets]
    for i, a in enumerate(rects):
        for j in range(i + 1, len(rects)):
            if widgets[i].parentWidget() is widgets[j].parentWidget():
                assert not a.intersects(rects[j]), f"{i} and {j} overlap at the minimum width"
    for w in (stats.fields_btn, stats.templates_btn, stats.automations_btn, stats.archive_btn):
        assert w.width() >= w.sizeHint().width() - 1, f"'{w.text()}' is truncated"
    win.resize(1600, 800)
    QApplication.processEvents()
    assert stats.fields_btn.text() == "🧩 Fields (5)" and stats.templates_btn.text() == "📋 Templates (2)"

    opened = []
    monkeypatch.setattr(TaskDialog, "exec", lambda dlg: opened.append((dlg.template_name, dlg.default_column_id)) or 0)
    bug = service.get_task_templates(demo.id)[0]
    assert win.new_task_from_template(demo.id, 0, bug["id"]) is False  # (the stubbed editor was cancelled)
    todo = next(c.id for c in service.get_columns_for_board(demo.id) if c.name == "To Do")
    assert opened == [("Bug report", todo)], "no column given: the capture column"

    monkeypatch.setattr(CustomFieldsDialog, "exec", lambda dlg: dlg.delete_selected(confirm=False) and 0)
    assert win.open_custom_fields(demo.id) is True
    assert len(service.get_custom_fields(demo.id)) == 4
    assert all("Story points" not in bar.field_combo.itemText(i) for i in range(bar.field_combo.count()))
    win.time_tracker.shutdown()
    win.close()
    win.deleteLater()  # a deleted window stops listening to the global signals other tests emit
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

"""
UI tests for the Automations dialog (rules, rule editor, policies, history),
the Archived Tasks dialog, and main-window integration.
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QSpinBox

from kanbanmanager import DatabaseService
from kanbanmanager.core import automation as au
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.ui import MainWindow
from kanbanmanager.ui.dialogs.archive_dialog import ArchivedTasksDialog
from kanbanmanager.ui.dialogs.automation_dialog import (
    ActionRow,
    AutomationDialog,
    ConditionRow,
    RuleEditorDialog,
)
from support import seeded_manager


@pytest.fixture
def demo(qapp, tmp_path):
    mgr = seeded_manager(tmp_path / "auto_ui.db")
    service = DatabaseService(mgr, actor="Ann")
    board = [b for b in service.get_workspaces()[0].boards if b.name == DEMO_BOARD_NAME][0]
    engine = au.AutomationEngine(service)
    engine.attach()
    return service, board, engine


def cols(service, board):
    return {c.name: c.id for c in service.get_columns_for_board(board.id)}


def test_rule_editor_builds_and_saves_a_rule(demo):
    service, board, engine = demo
    dlg = RuleEditorDialog(service, board.id, engine.board_context(board.id))
    assert dlg.problems()  # no name yet
    dlg.name_input.setText("Review handoff")
    dlg.trigger_combo.setCurrentIndex(dlg.trigger_combo.findData("task_moved"))
    to_box = dlg._trigger_widgets["to_column_id"]
    to_box.setCurrentIndex(to_box.findData(cols(service, board)["In Review"]))

    dlg._add_condition({"field": "priority", "op": "at_least", "value": "HIGH"})
    action = dlg._rows(dlg.action_list)[0]
    action.kind_combo.setCurrentIndex(action.kind_combo.findData("assign"))
    person = action._widgets["person"]
    person.setCurrentIndex(person.findData(au.ACTOR_FOR_TRIGGER))

    value = dlg.value()
    assert value["trigger"] == {"type": "task_moved", "to_column_id": cols(service, board)["In Review"]}
    assert value["conditions"] == [{"field": "priority", "op": "at_least", "value": "HIGH"}]
    assert value["actions"] == [{"type": "assign", "person": au.ACTOR_FOR_TRIGGER}]
    assert "When a task is moved to 'In Review', if priority is at least High → assign to the person who " \
           "triggered it" in dlg.summary_lbl.text()
    assert dlg.problems() == []
    dlg.save()
    saved = [r for r in service.get_automation_rules(board.id) if r["name"] == "Review handoff"][0]
    assert saved["actions"] == value["actions"]


def test_rule_editor_blocks_invalid_rules_and_edits_existing(demo):
    service, board, engine = demo
    existing = service.get_automation_rules(board.id)[0]
    dlg = RuleEditorDialog(service, board.id, engine.board_context(board.id), existing)
    assert dlg.name_input.text() == existing["name"] and dlg.value()["actions"] == existing["actions"]
    for row in dlg._rows(dlg.action_list):
        dlg._remove_row(row)
    dlg.save()
    assert dlg.result() == 0 and "No actions" in dlg.problems_lbl.text()
    assert service.get_automation_rule(existing["id"])["actions"] == existing["actions"], "nothing saved"


def test_condition_and_action_rows_switch_parameter_widgets(demo):
    service, board, engine = demo
    from kanbanmanager.ui.dialogs.automation_dialog import ParamWidgets
    params = ParamWidgets(engine.board_context(board.id), service.get_people())
    cond = ConditionRow(params, {"field": "assignee", "op": "is_empty"})
    assert cond.value() == {"field": "assignee", "op": "is_empty"}
    cond.op_combo.setCurrentIndex(cond.op_combo.findData("is"))
    assert "value" in cond.value()
    act = ActionRow(params, {"type": "set_due_date", "days": 5})
    assert isinstance(act._widgets["days"], QSpinBox) and act.value() == {"type": "set_due_date", "days": 5}
    act.kind_combo.setCurrentIndex(act.kind_combo.findData("archive"))
    assert act.value() == {"type": "archive"}


def test_automation_dialog_rules_tab(demo, monkeypatch):
    service, board, engine = demo
    dlg = AutomationDialog(service, board.id, engine=engine)
    assert dlg.rules_table.rowCount() == 4
    assert "Escalate" in dlg.rules_table.item(0, 1).text()
    assert dlg.rules_table.item(0, 2).text().startswith("When a task becomes overdue")

    dlg.rules_table.item(0, 0).setCheckState(Qt.Unchecked)  # toggle off
    assert service.get_automation_rules(board.id)[0]["is_enabled"] is False

    dlg.rules_table.selectRow(1)
    dlg.duplicate_selected()
    copies = [r for r in service.get_automation_rules(board.id) if r["name"].endswith("(copy)")]
    assert len(copies) == 1 and copies[0]["is_enabled"] is False
    dlg.rules_table.selectRow(dlg.rules_table.rowCount() - 1)
    dlg.delete_selected(confirm=False)
    assert len(service.get_automation_rules(board.id)) == 4

    # From template: the editor opens pre-filled; saving adds the rule
    monkeypatch.setattr(RuleEditorDialog, "exec", lambda self: self.save() or self.result())
    dlg.new_from_template(1)
    assert any(r["name"] == au.RULE_TEMPLATES[1]["name"] for r in service.get_automation_rules(board.id))

    # A rule pointing at a deleted column is flagged
    service.delete_column(cols(service, board)["In Review"])
    dlg.reload_rules()
    flagged = [dlg.rules_table.item(r, 1).text() for r in range(dlg.rules_table.rowCount())
               if dlg.rules_table.item(r, 1).text().startswith("⚠")]
    assert flagged == ["⚠ Ask for review when moved to review"]


def test_automation_dialog_policies_save_immediately_and_run_now(demo):
    service, board, engine = demo
    dlg = AutomationDialog(service, board.id, engine=engine)
    assert dlg.auto_chk.isChecked() and dlg.auto_days.value() == 14
    assert dlg.stale_chk.isChecked() and dlg.stale_label.text() == "Stale"
    dlg.archive_stale_chk.setChecked(True)
    dlg.archive_stale_days.setValue(45)
    dlg.stale_move_chk.setChecked(True)
    dlg.stale_move_combo.setCurrentIndex(dlg.stale_move_combo.findData(cols(service, board)["Backlog"]))
    dlg.stale_exclude.item(0).setCheckState(Qt.Checked)
    saved = au.normalize_policies(service.get_board_policies(board.id))
    assert saved["archive_stale"] == {"enabled": True, "days": 45}
    assert saved["stale"]["move_to_column_id"] == cols(service, board)["Backlog"]
    assert saved["stale"]["exclude_column_ids"] == [dlg.stale_exclude.item(0).data(Qt.UserRole)]

    dlg.stale_chk.setChecked(False)
    assert not dlg.stale_days.isEnabled(), "sub-options follow the main switch"

    dlg.run_now()
    assert dlg.changed and "archived 1 task" in dlg.run_result.text()
    dlg.tabs.setCurrentIndex(2)
    assert dlg.history.rowCount() >= 1
    assert any(dlg.history.item(r, 3).text() == "success" for r in range(dlg.history.rowCount()))


def test_archived_tasks_dialog(demo):
    service, board, engine = demo
    engine.run_scheduled()  # archives "Set up the CI pipeline"
    extra = service.get_tasks_for_board(board.id)[0]
    service.archive_task(extra.id, reason="Archived manually")
    dlg = ArchivedTasksDialog(service, board.id)
    assert dlg.table.rowCount() == 2
    dlg.search.setText("ci pipeline")
    assert dlg.table.rowCount() == 1 and "completed more than 14 days" in dlg.table.item(0, 4).text()
    dlg.table.selectRow(0)
    dlg.restore_selected()
    assert dlg.changed and service.count_archived_tasks(board.id) == 1
    dlg.search.clear()
    dlg.table.selectRow(0)
    dlg.delete_selected(confirm=False)
    assert service.count_archived_tasks(board.id) == 0 and service.get_task(extra.id) is None
    assert dlg.empty_lbl.isVisibleTo(dlg)


def test_main_window_archive_counts_and_scheduled_run(demo, monkeypatch):
    service, board, fixture_engine = demo
    fixture_engine.detach()  # the window brings its own engine; two would double-fire
    win = MainWindow(service=service)
    win.resize(1400, 800)
    win.show()
    win.sidebar.select_board(board.id)
    win._on_board_selected(board.id)
    QApplication.processEvents()
    assert win.stats_bar.automations_btn.text() == "⚡ Automations (6 active)"  # 4 rules + 2 policies
    assert win.stats_bar.archive_btn.text() == "🗄 Archived"

    summary = win.run_automations()
    assert summary.archived == 1 and "Automation:" in win.status_bar.currentMessage()
    assert win.stats_bar.archive_btn.text() == "🗄 Archived (1)"

    task = service.get_tasks_for_board(board.id)[0]
    win.archive_task(task.id)
    assert service.get_task(task.id).is_archived and "Archived" in win.status_bar.currentMessage()
    assert win.stats_bar.archive_btn.text() == "🗄 Archived (2)"

    opened = []
    monkeypatch.setattr(AutomationDialog, "exec", lambda self: opened.append("auto") or 0)
    monkeypatch.setattr(ArchivedTasksDialog, "exec", lambda self: opened.append("archive") or 0)
    win.stats_bar.automations_btn.click()
    win.stats_bar.archive_btn.click()
    assert opened == ["auto", "archive"]

    # Event rules run through the window's own engine when the user acts
    review = cols(service, board)["In Review"]
    service.update_column(review, wip_limit=0)
    target = [t for t in service.get_tasks_for_board(board.id) if t.title.startswith("Crash")][0]
    before = len(service.get_comments(target.id))
    win._on_task_moved(target.id, review)
    comments = service.get_comments(target.id)
    assert len(comments) == before + 1 and comments[-1]["author"].startswith("⚙ Automation")
    win.time_tracker.shutdown()

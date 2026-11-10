"""
Guards against a PySide6 memory-corruption pattern that crashes the process on exit.

A widget whose signal is connected to a lambda capturing ``self`` must not be
added to a layout that is not yet attached to a widget. PySide6 then loses
track of ownership, and destroying the parent frees the widget twice
("malloc_consolidate(): unaligned fastbin chunk", segfault or bus error at
exit). Attaching the layout first (or adding the widget to an attached layout)
avoids it. A static audit catches the pattern in code; a subprocess test
constructs and destroys every dialog/panel and checks the interpreter exits
cleanly.
"""
import ast
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
LAYOUTS = {"QHBoxLayout", "QVBoxLayout", "QGridLayout", "QFormLayout", "FlowLayout"}


def _audit_file(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    findings = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        nodes = sorted((n for n in ast.walk(fn) if hasattr(n, "lineno")), key=lambda n: (n.lineno, n.col_offset))
        created, attached, added, lambda_widgets = {}, {}, {}, set()
        for n in nodes:
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                f = n.value.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                if name in LAYOUTS and not n.value.args:
                    for t in n.targets:
                        created[ast.unparse(t)] = n.lineno
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                meth, owner = n.func.attr, ast.unparse(n.func.value)
                if meth in ("addLayout", "insertLayout", "setLayout") and n.args:
                    arg = n.args[-1] if meth == "insertLayout" else n.args[0]
                    attached.setdefault(ast.unparse(arg), n.lineno)
                if meth in ("addWidget", "insertWidget", "addRow") and owner in created and owner not in attached:
                    for a in n.args:
                        if not isinstance(a, ast.Constant):
                            added.setdefault(ast.unparse(a), (owner, n.lineno))
                if meth == "connect" and n.args and isinstance(n.args[0], ast.Lambda):
                    if any(isinstance(x, ast.Name) and x.id == "self" for x in ast.walk(n.args[0])):
                        if isinstance(n.func.value, ast.Attribute):
                            lambda_widgets.add(ast.unparse(n.func.value.value))
        for loop in (n for n in ast.walk(fn) if isinstance(n, ast.For)):
            if isinstance(loop.target, ast.Name) and isinstance(loop.iter, (ast.Tuple, ast.List)):
                if loop.target.id in added:
                    for elt in loop.iter.elts:
                        added.setdefault(ast.unparse(elt), added[loop.target.id])
        for w in sorted(lambda_widgets & set(added)):
            layout, line = added[w]
            findings.append(f"{path.relative_to(SRC)}:{line}: {w} (self-lambda) added to unattached '{layout}'")
    return findings


def test_no_self_lambda_widgets_in_unattached_layouts():
    findings = [f for p in sorted((SRC / "kanbanmanager" / "ui").rglob("*.py")) for f in _audit_file(p)]
    assert findings == [], "Attach these layouts before adding widgets:\n" + "\n".join(findings)


def test_audit_detects_the_pattern(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(textwrap.dedent("""
        class W:
            def _init_ui(self):
                layout = QVBoxLayout(self)
                row = QHBoxLayout()
                self.btn = QPushButton("x")
                self.btn.clicked.connect(lambda: self.go())
                for b in (self.btn,):
                    row.addWidget(b)
                layout.addLayout(row)
    """))
    global SRC
    saved, SRC = SRC, tmp_path
    try:
        assert len(_audit_file(bad)) == 1
    finally:
        SRC = saved


TEARDOWN_SCRIPT = textwrap.dedent("""
    import gc, os, sys
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from kanbanmanager.database.connection import DatabaseManager
    from kanbanmanager.database.service import DatabaseService
    from kanbanmanager.core.automation import AutomationEngine
    from kanbanmanager.core.time_tracker import TimeTracker
    from kanbanmanager.ui.components.activity_panel import ActivityPanel
    from kanbanmanager.ui.components.attachments_panel import AttachmentsPanel
    from kanbanmanager.ui.components.time_panel import TimePanel
    from kanbanmanager.ui.dialogs.archive_dialog import ArchivedTasksDialog
    from kanbanmanager.ui.dialogs.automation_dialog import AutomationDialog, RuleEditorDialog
    from kanbanmanager.ui.dialogs.task_dialog import TaskDialog
    from kanbanmanager.ui.dialogs.timesheet_dialog import TimesheetDialog
    from kanbanmanager.ui.main_window import MainWindow
    from kanbanmanager.ui.views.workload_view import TeamDialog, WorkloadView
    from kanbanmanager.ui.views.gantt_view import GanttView
    from kanbanmanager.core.schedule_engine import ScheduleEngine
    from kanbanmanager.ui.dialogs.schedule_dialogs import (
        AutoScheduleDialog, BaselinesDialog, DependencyDialog, ScheduleSettingsDialog)
    from kanbanmanager.ui.views.focus_view import FocusView, TriageDialog
    from kanbanmanager.ui.views.split_view import SplitView
    from kanbanmanager.ui.dialogs.quick_capture import QuickCaptureDialog, QuickCaptureSettingsDialog
    from kanbanmanager.ui.dialogs.custom_fields_dialog import CustomFieldsDialog
    from kanbanmanager.ui.dialogs.template_dialogs import (
        NewBoardDialog, SaveBoardTemplateDialog, TaskTemplateEditor, TaskTemplatesDialog)
    from kanbanmanager.ui.components.field_editors import EditorContext, FieldsPanel
    from kanbanmanager.ui.dialogs.calendar_sync_dialog import CalendarSyncDialog, SubscriptionDialog
    from kanbanmanager.ui.dialogs.import_export_dialog import ImportExportDialog, SpreadsheetImportWizard
    from kanbanmanager.ui.views.analytics_view import AnalyticsView
    from kanbanmanager.ui.calendar_feed import CalendarFeedServer, SyncRunner
    from kanbanmanager.ui.charts import Chart, ChartWidget
    from kanbanmanager.ui.command_palette import Command, CommandPalette
    from kanbanmanager.ui.dialogs.theme_dialog import ThemeDialog
    from kanbanmanager.ui.keyboard import ShortcutHelpDialog
    from kanbanmanager.core import exchange

    m = DatabaseManager(sys.argv[1]); m.init_database(True); s = DatabaseService(m)
    board = [b for b in s.get_workspaces()[0].boards if b.name == "Feature Tour"][0]
    task = s.get_tasks_for_board(board.id)[0]
    eng = AutomationEngine(s)
    sched = ScheduleEngine(s)

    def gantt():
        g = GanttView(s, engine=sched); g.load_board(board.id); return g

    def focus():
        f = FocusView(s, scheduler=sched); f.refresh(); return f

    def split():
        v = SplitView(s); v.load([board.id]); return v

    def capture():
        d = QuickCaptureDialog(s); d.summon("x tomorrow !high"); return d

    def fields_panel():
        p = FieldsPanel(EditorContext(people=["A"])); p.set_fields(s.get_custom_fields(board.id)); return p

    def fields_dialog():
        d = CustomFieldsDialog(s, board.id)
        for row in range(d.list.count()):  # every type's settings form and default editor
            d.list.setCurrentRow(row)
        d.new_field(); return d

    def analytics():
        v = AnalyticsView(s); v.load_board(board.id); return v

    def wizard():
        t = exchange.Table(["Title", "Status", "Due"], [["A", "Doing", "2031-01-01"]], source="t.csv")
        w = SpreadsheetImportWizard(s, t, board.id); w.show_preview(); return w

    def sync_dialog():
        return CalendarSyncDialog(s, CalendarFeedServer(s), SyncRunner(s), board.id)

    def new_board():
        d = NewBoardDialog(s, board.workspace_id)
        for t in s.get_board_templates():
            d.select_template(t["id"])
        return d
    factories = [
        lambda: TaskDialog(board.id, task.id, service=s, tracker=TimeTracker(s)),
        lambda: AttachmentsPanel(s, task.id),
        lambda: ActivityPanel(s, task.id),
        lambda: TimePanel(s, task.id, TimeTracker(s)),
        lambda: WorkloadView(s),
        lambda: TeamDialog(s, s.get_people()),
        lambda: TimesheetDialog(s, board.id),
        lambda: AutomationDialog(s, board.id, engine=eng),
        lambda: RuleEditorDialog(s, board.id, eng.board_context(board.id), s.get_automation_rules(board.id)[0]),
        lambda: ArchivedTasksDialog(s, board.id),
        gantt,
        lambda: AutoScheduleDialog(sched, board.id),
        lambda: BaselinesDialog(sched, board.id),
        lambda: ScheduleSettingsDialog(s, board.id),
        lambda: DependencyDialog(s, board.id, task.id),
        focus,
        split,
        capture,
        lambda: QuickCaptureSettingsDialog(s, "status"),
        lambda: TriageDialog(s, s.get_inbox_items()[:1]),
        fields_panel,
        fields_dialog,
        new_board,
        lambda: SaveBoardTemplateDialog(s, board.id),
        lambda: TaskTemplateEditor(s, board.id, s.get_task_templates(board.id)[0]),
        lambda: TaskTemplatesDialog(s, board.id),
        lambda: TaskDialog(board.id, service=s, template=s.get_task_templates(board.id)[0]),
        analytics,
        wizard,
        sync_dialog,
        lambda: SubscriptionDialog(s, None, board_id=board.id),
        lambda: ImportExportDialog(s, board.id),
        lambda: ChartWidget(Chart("donut", "x", slices=[("a", 1, "#3B82F6")])),
        lambda: ThemeDialog(s),
        lambda: ShortcutHelpDialog(),
        lambda: CommandPalette(lambda q: [Command("A", lambda: None)]),
    ]

    def build():
        for make in factories:
            for _ in range(2):
                w = make(); w.show(); app.processEvents()
        win = MainWindow(service=s); win.show(); app.processEvents(); win.close()

    build(); gc.collect()
    print("clean")
""")


@pytest.mark.slow
@pytest.mark.parametrize("attempt", range(3))
def test_widgets_are_destroyed_without_crashing(tmp_path, attempt):
    script = tmp_path / "teardown.py"
    script.write_text(TEARDOWN_SCRIPT)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(SRC), KANBAN_DATA_DIR=str(tmp_path))
    proc = subprocess.run([sys.executable, str(script), str(tmp_path / "k.db")], env=env,
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0 and "clean" in proc.stdout, (
        f"exit code {proc.returncode}\n{proc.stderr[-2000:]}"
    )

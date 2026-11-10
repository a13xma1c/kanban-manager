"""
Tests for analytics, charts and exports (vector SVG/PDF, PNG), the PDF board
report, board / Gantt / calendar pictures, the Analytics view, the Import /
Export centre and spreadsheet wizard, the Calendar Sync dialog (subscriptions
against a local CalDAV/feed server, the live feed, .ics files) and the live
feed's HTTP server.
"""
import socket
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from caldav_server import PASSWORD, TASKS, USER, Server
from kanbanmanager import DatabaseService
from kanbanmanager.core import analytics as an
from kanbanmanager.core import exchange as x
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.database.models import ActivityLog, Task
from kanbanmanager.enums import ViewMode
from kanbanmanager.ui import MainWindow
from kanbanmanager.ui.calendar_feed import CalendarFeedServer, SyncRunner, load_feed_settings, save_feed_settings
from kanbanmanager.ui.charts import Chart, ChartWidget, export_chart
from kanbanmanager.ui.dialogs.calendar_sync_dialog import CalendarSyncDialog, SubscriptionDialog
from kanbanmanager.ui.dialogs.import_export_dialog import ImportExportDialog, SpreadsheetImportWizard
from kanbanmanager.ui.reports import build_charts, export_board_snapshot, export_gantt, write_board_report
from kanbanmanager.ui.views.analytics_view import AnalyticsView
from kanbanmanager.ui.views.gantt_view import GanttView
from support import seeded_manager


@pytest.fixture
def env(qapp, tmp_path):
    mgr = seeded_manager(tmp_path / "r.db")
    service = DatabaseService(mgr, actor="Ann")
    demo = next(b for ws in service.get_workspaces() for b in ws.boards if b.name == DEMO_BOARD_NAME)
    return service, demo


@pytest.fixture
def boxes(monkeypatch):
    shown = []

    def box(answer):
        def show(parent, title, text, *args, **kwargs):
            shown.append((title, text))
            return answer
        return show
    for name, answer in (("warning", QMessageBox.Yes), ("question", QMessageBox.Yes),
                         ("information", QMessageBox.Ok), ("critical", QMessageBox.Ok)):
        monkeypatch.setattr(QMessageBox, name, box(answer))
    return shown


def pdf_text(path: Path) -> str:
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument()
    assert doc.load(str(path)) == QPdfDocument.Error.None_, f"not a readable PDF: {path}"
    text = " ".join(doc.getAllText(i).text() for i in range(doc.pageCount()))
    n = doc.pageCount()
    doc.close()
    return f"[{n} pages] " + " ".join(text.split())  # table cells wrap: compare with single spaces


def wait_for(predicate, ms: int = 10000) -> bool:
    loop_end = datetime.now() + timedelta(milliseconds=ms)
    while datetime.now() < loop_end:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 50)
        if predicate():
            return True
    return predicate()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
def _utc(local: datetime) -> datetime:
    return local.astimezone().astimezone(timezone.utc).replace(tzinfo=None)


def test_history_reconstruction_cfd_burnup_throughput_cycle_time(qapp, tmp_path):
    mgr = DatabaseManager(tmp_path / "a.db")
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr, actor="Ann")
    ws = service.create_workspace("W")
    board = service.create_board(ws.id, "Flow")
    cols = {c.name: c for c in service.get_columns_for_board(board.id)}
    service.create_custom_field(board.id, "Points", "number")
    a = service.create_task(board.id, cols["To Do"].id, "A", field_values={"Points": 3})
    b = service.create_task(board.id, cols["Backlog"].id, "B", field_values={"Points": 5})
    service.move_task(a.id, cols["In Progress"].id)
    service.move_task(a.id, cols["Done"].id)
    service.move_task(b.id, cols["To Do"].id)
    # rewrite the timeline: A created Jan 1, started Jan 3, done Jan 6; B created Jan 2, planned Jan 5
    day = lambda d, h=10: _utc(datetime(2031, 1, d, h))
    with mgr.session_scope() as s:
        logs = s.query(ActivityLog).filter(ActivityLog.board_id == board.id, ActivityLog.task_id.isnot(None)) \
            .order_by(ActivityLog.id).all()
        stamps = {(a.id, "TASK_CREATED"): [day(1)], (b.id, "TASK_CREATED"): [day(2)],
                  (a.id, "TASK_MOVED"): [day(3), day(6)], (b.id, "TASK_MOVED"): [day(5)],
                  (a.id, "TASK_COMPLETED"): [day(6)]}
        for log in logs:
            queue = stamps.get((log.task_id, log.action_type))
            if queue:
                log.timestamp = queue.pop(0)
        s.get(Task, a.id).created_at = day(1)
        s.get(Task, b.id).created_at = day(2)
        s.get(Task, a.id).completed_at = datetime(2031, 1, 6, 10)
    data = an.load_board(service, board.id)
    flow = an.cumulative_flow(data, date(2031, 1, 1), date(2031, 1, 7))
    bands = {name: counts for name, _c, counts in flow.series}
    assert [n for n, _c, _v in flow.series][0] == "Done", "done is the bottom band"
    assert bands["To Do"] == [1, 1, 0, 0, 1, 1, 1]
    assert bands["Backlog"] == [0, 1, 1, 1, 0, 0, 0]
    assert bands["In Progress"] == [0, 0, 1, 1, 1, 0, 0]
    assert bands["Done"] == [0, 0, 0, 0, 0, 1, 1]
    bu = an.burnup(data, date(2031, 1, 1), date(2031, 1, 7), field_id=next(f["id"] for f in data.fields))
    assert bu.scope == [3, 8, 8, 8, 8, 8, 8] and bu.done == [0, 0, 0, 0, 0, 3, 3] and bu.unit == "Points"
    assert an.burnup(data, date(2031, 1, 1), date(2031, 1, 2)).scope == [1, 2]
    cycles = an.cycle_times(data, date(2031, 1, 1), date(2031, 1, 31))
    assert [(c.title, c.cycle_days, c.lead_days) for c in cycles] == [("A", 3.0, 5.0)]
    tp = an.throughput(data, weeks=3, today=date(2031, 1, 15))
    assert tp == [(date(2030, 12, 30), 0), (date(2031, 1, 6), 1), (date(2031, 1, 13), 0)], "weeks start on Monday"
    k = an.kpis(data, date(2031, 1, 1), date(2031, 1, 14))
    assert (k["done"], k["completed_in_period"], k["avg_cycle"]) == (1, 1, 3.0)
    assert an.percentile([1, 2, 3, 4], 50) == 2.5 and an.percentile([], 85) is None
    by_points = an.breakdown(data, "column", open_only=False, sum_field_id=next(f["id"] for f in data.fields))
    assert dict((n, v) for n, v, _c in by_points)["Done"] == 3


def test_charts_export_as_vectors_and_png(env, tmp_path):
    service, demo = env
    charts = build_charts(service, demo.id, 30)
    assert {"flow", "burnup", "throughput", "cycle", "columns", "priority", "assignee", "label", "field"} <= set(charts)
    for key, chart in charts.items():
        svg = export_chart(chart, tmp_path / f"{key}.svg")
        text = svg.read_text(encoding="utf-8")
        assert "<svg" in text and "<image" not in text, f"{key}: vector, no embedded bitmap"
        assert f"<title>{chart.title}</title>" in text
    pdf = export_chart(charts["flow"], tmp_path / "flow.pdf")
    assert "Cumulative" in pdf_text(pdf)
    png = export_chart(charts["priority"], tmp_path / "p.png")
    img = QImage(str(png))
    assert (img.width(), img.height()) == (1920, 1080)
    empty = Chart("bars", "Nothing", labels=[date.today()], series=[("x", "#000000", [0])])
    assert empty.is_empty
    export_chart(empty, tmp_path / "empty.svg")
    assert "No data for this period yet" in (tmp_path / "empty.svg").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        export_chart(empty, tmp_path / "x.gif")


def test_board_report_pdf(env, tmp_path):
    service, demo = env
    points = next(f["id"] for f in service.get_custom_fields(demo.id) if f["name"] == "Story points")
    path = write_board_report(service, demo.id, tmp_path / "report.pdf", days=30, points_field_id=points,
                              include_descriptions=True)
    text = pdf_text(path)
    pages = int(text.split(" pages]")[0][1:])
    assert pages >= 4
    for needle in ("Feature Tour", "Cumulative", "Burn-up", "Throughput", "Cycle time", "Open tasks by priority",
                   "Story points by Component", "Crash when importing large CSV files", "Backlog", "Page 1 of",
                   "Stream rows instead"):
        assert needle in text, needle
    short = write_board_report(service, demo.id, tmp_path / "short.pdf", include_charts=False,
                               include_completed=False)
    short_text = pdf_text(short)
    assert "Cumulative" not in short_text and "Publish the Q3 performance report" not in short_text


def test_board_gantt_and_calendar_pictures(env, tmp_path):
    service, demo = env
    png = export_board_snapshot(service, demo.id, tmp_path / "board.png")
    img = QImage(str(png))
    assert img.width() > 2 * 5 * 300 and img.height() > 2 * 900, "every column, full height (2x)"
    svg = export_board_snapshot(service, demo.id, tmp_path / "board.svg")
    text = svg.read_text(encoding="utf-8")
    assert "Public beta" in text and "Add Column" not in text, "the last card of the longest column is included"
    filtered = export_board_snapshot(service, demo.id, tmp_path / "f.svg", filters={"search": "crash"})
    ftext = filtered.read_text(encoding="utf-8")
    assert "Crash when importing" in ftext and "Public beta" not in ftext
    assert pdf_text(export_board_snapshot(service, demo.id, tmp_path / "board.pdf")).count("Backlog") >= 1

    gantt = GanttView(service)
    gantt.resize(1300, 700)
    gantt.show()
    gantt.load_board(demo.id)
    out = gantt.export_chart("svg", str(tmp_path / "gantt.svg"))
    gtext = out.read_text(encoding="utf-8")
    assert "Design the new onboarding flow" in gtext and "Task" in gtext
    assert QImage(str(export_gantt(gantt, tmp_path / "g.png"))).width() == 2 * (300 + gantt.canvas.width())
    gantt.close()

    win = MainWindow(service=service)
    win.show()
    win.open_board(demo.id)
    win._on_view_mode_changed(ViewMode.CALENDAR.value)
    pdf = win.calendar_view.export_month("pdf", str(tmp_path / "month.pdf"))
    assert "Mon" in pdf_text(pdf)
    win.time_tracker.shutdown()
    win.close()
    win.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_analytics_view(env, tmp_path):
    service, demo = env
    view = AnalyticsView(service)
    view.resize(1300, 900)
    view.show()
    view.load_board(demo.id)
    QApplication.processEvents()
    assert view.points_combo.currentText() == "Sum of Story points", "story points picked by default"
    assert list(view.charts)[:4] == ["flow", "burnup", "throughput", "cycle"]
    assert view.charts["burnup"].chart.subtitle.endswith("(pts) - the gap is what's left")
    view.points_combo.setCurrentIndex(0)
    assert "(tasks)" in view.charts["burnup"].chart.subtitle
    view.period_combo.setCurrentIndex(view.period_combo.findData(90))
    assert len(view.charts["flow"].chart.labels) == 90
    messages = []
    view.message.connect(messages.append)
    report = view.export_report(path=str(tmp_path / "r.pdf"))
    assert report.exists() and messages[-1] == "⬇ Saved the report to r.pdf"
    files = view.export_all_charts("svg", folder=str(tmp_path))
    assert len(files) == len(view.charts) and all(f.suffix == ".svg" for f in files)
    assert isinstance(view.charts["flow"], ChartWidget)
    view.close()


# ---------------------------------------------------------------------------
# Import / export centre
# ---------------------------------------------------------------------------
def test_import_export_dialog_exports(env, tmp_path):
    service, demo = env
    dlg = ImportExportDialog(service, demo.id)
    assert dlg.export_tasks("xlsx", str(tmp_path / "t.xlsx")).exists()
    assert "✅ Saved 13 tasks" in dlg.status.text()
    dlg.completed_chk.setChecked(False)
    dlg.export_tasks("csv", str(tmp_path / "t.csv"))
    assert len(x.read_table(tmp_path / "t.csv").rows) == 11
    dlg.scope_combo.setCurrentIndex(dlg.scope_combo.findData("all"))
    dlg.completed_chk.setChecked(True)
    dlg.export_tasks("csv", str(tmp_path / "all.csv"))
    assert len(x.read_table(tmp_path / "all.csv").rows) == 13 + 4
    assert dlg.export_markdown(str(tmp_path / "b.md")).read_text(encoding="utf-8").startswith("# Feature Tour")
    assert "Feature Tour" in pdf_text(dlg.export_report(str(tmp_path / "rep.pdf")))
    assert QImage(str(dlg.export_snapshot("png", str(tmp_path / "b.png")))).width() > 1000
    assert dlg.export_json(str(tmp_path / "b.json")).read_text(encoding="utf-8").lstrip().startswith("{")
    requests = []
    dlg.calendar_requested.connect(requests.append)
    dlg.cal_btn.click()
    assert requests == ["files"]
    none = ImportExportDialog(service, None)
    assert not none.report_btn.isEnabled() and none.scope_combo.count() == 1


def test_spreadsheet_wizard_and_undo(env, tmp_path, boxes):
    service, demo = env
    path = tmp_path / "asana.csv"
    path.write_text("Task ID,Name,Section/Column,Assignee,Due Date,Tags,Notes\n"
                    "1,Plan launch,Doing,Priya Sharma,2031-03-01,launch,Big one\n"
                    "2,Draft blog,Ideas,,2031-03-05,content;launch,\n", encoding="utf-8")
    dlg = ImportExportDialog(service, demo.id)

    def drive(wizard: SpreadsheetImportWizard) -> bool:
        assert wizard.target_board_id == demo.id
        wizard.preset_combo.setCurrentText("Asana")
        assert wizard.mapping() == {0: "ignore", 1: "title", 2: "column", 3: "assignee", 4: "due", 5: "labels",
                                    6: "description"}
        saved = wizard.combos[1].currentIndex()
        wizard.combos[1].setCurrentIndex(0)          # no title column -> refused
        wizard.next()
        assert boxes[-1][1] == "Choose which column holds the task titles." and wizard.stack.currentIndex() == 0
        wizard.combos[1].setCurrentIndex(saved)
        wizard.next()                                 # preview
        assert wizard.stack.currentIndex() == 1 and wizard.next_btn.text() == "Import"
        assert "<b>2</b> tasks will be created" in wizard.summary.text()
        assert "new columns: Doing, Ideas" in wizard.summary.text()
        assert wizard.preview.item(0, 2).text() == "＋ Doing"
        wizard.next()                                 # import
        return wizard.result() == QDialog.Accepted

    result = dlg.import_sheet(str(path), run=drive)
    assert len(result.created) == 2 and dlg.undo_btn.isEnabled()
    titles = {t.title for t in service.get_tasks_for_board(demo.id)}
    assert {"Plan launch", "Draft blog"} <= titles
    assert dlg.undo_last() == 2 and not dlg.undo_btn.isEnabled()
    assert "Doing" not in [c.name for c in service.get_columns_for_board(demo.id)]
    assert dlg.import_sheet(str(tmp_path / "nope.csv")) is None and "Could not read" in boxes[-1][1]


def test_trello_and_json_imports_from_the_dialog(env, tmp_path, boxes):
    import json as _json

    from test_exchange import TRELLO

    service, demo = env
    path = tmp_path / "t.json"
    path.write_text(_json.dumps(TRELLO), encoding="utf-8")
    dlg = ImportExportDialog(service, demo.id)
    made = []
    dlg.board_created.connect(made.append)
    board_id = dlg.import_trello(str(path))
    assert made == [board_id] and service.get_board(board_id).name == "Launch"
    assert "Created the board 'Launch'" in dlg.status.text()
    backup = tmp_path / "backup.json"
    dlg.export_json(str(backup))
    before = len(service.get_workspaces())
    assert dlg.import_json(str(backup)) and len(service.get_workspaces()) == before * 2
    assert dlg.import_trello(str(backup)) is None and "not a Trello board export" in boxes[-1][1]


# ---------------------------------------------------------------------------
# Calendar sync UI & live feed
# ---------------------------------------------------------------------------
def test_calendar_sync_dialog_subscriptions(env, boxes, monkeypatch):
    service, demo = env
    with Server() as srv:
        srv.state.feed = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:f1@x\r\nSUMMARY:From the feed\r\n"
                          "END:VTODO\r\nEND:VCALENDAR\r\n")
        runner = SyncRunner(service)
        dlg = CalendarSyncDialog(service, CalendarFeedServer(service), runner, demo.id)
        assert dlg.table.rowCount() == 0 and not dlg.sync_btn.isEnabled()

        def fill(editor: SubscriptionDialog) -> int:
            editor.name_input.setText("Team feed")
            editor.url_input.setText(srv.base + "/feed.ics")
            assert not editor.direction_combo.isEnabled(), "feeds are read-only"
            assert editor.test_connection() and "1 upcoming item" in editor.test_lbl.text()
            return 1 if editor.save() else 0

        monkeypatch.setattr(SubscriptionDialog, "exec", fill)
        saved = dlg.add_subscription()
        assert saved["name"] == "Team feed" and saved["board_id"] == demo.id
        assert wait_for(lambda: not runner.busy and "From the feed" in
                        [t.title for t in service.get_tasks_for_board(demo.id)])
        assert wait_for(lambda: "Team feed: 1 new" in dlg.sync_status.text())
        assert dlg.table.rowCount() == 1 and dlg.table.item(0, 0).text() == "✅"
        assert "1 new" in dlg.table.item(0, 5).text()
        monkeypatch.setattr(QDialog, "exec", lambda self: 0)
        assert "1 created" in dlg.show_history()

        def caldav(editor: SubscriptionDialog) -> int:
            editor.kind_combo.setCurrentIndex(editor.kind_combo.findData("caldav"))
            assert editor.direction_combo.isEnabled()
            editor.name_input.setText("")
            editor.url_input.setText(srv.base + "/")
            editor.user_input.setText(USER)
            editor.password_input.setText(PASSWORD)
            assert editor.test_connection() and not editor.calendars_combo.isHidden(), "pick one of several"
            assert editor.calendars_combo.count() == 2 and editor.url_input.text() == srv.base + TASKS
            assert editor.name_input.text() == "Tasks", "named after the calendar"
            return 1 if editor.save() else 0

        monkeypatch.setattr(SubscriptionDialog, "exec", caldav)
        cal = dlg.add_subscription()
        assert cal["kind"] == "caldav" and cal["direction"] == "two_way" and cal["has_password"]
        assert wait_for(lambda: not runner.busy and any(h.startswith(TASKS + "kanban-task")
                                                        for h in srv.state.items), 15000)
        dlg.table.selectRow(0)
        assert dlg.delete_selected(confirm=False) and dlg.table.rowCount() == 1
        dlg.close()


def test_live_feed_tab_and_http_server(env, tmp_path, boxes):
    service, demo = env
    server = CalendarFeedServer(service)
    dlg = CalendarSyncDialog(service, server, SyncRunner(service), demo.id)
    port = free_port()
    dlg.feed_enabled.setChecked(True)
    dlg.feed_port.setValue(port)
    dlg.feed_kind.setCurrentIndex(dlg.feed_kind.findData("VTODO"))
    assert dlg.apply_feed() and server.running and server.port == port
    assert dlg.feed_table.rowCount() == 2 + 2 and "webcal://127.0.0.1:" in dlg.feed_table.item(0, 1).text()
    token = load_feed_settings(service)["token"]
    results = {}

    def fetch(key, url, method="GET"):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5) as r:
                results[key] = (r.status, r.headers.get("Content-Type"), r.read())
        except urllib.error.HTTPError as exc:
            results[key] = (exc.code, None, b"")
        except Exception as exc:  # pragma: no cover
            results[key] = (repr(exc), None, b"")

    base = f"http://127.0.0.1:{port}"
    jobs = {"all": f"{base}/{token}/all.ics", "board": f"{base}/{token}/board-{demo.id}.ics",
            "mine": f"{base}/{token}/mine.ics", "bad": f"{base}/wrong/all.ics", "missing": f"{base}/{token}/x.ics"}
    threads = [threading.Thread(target=fetch, args=(k, u)) for k, u in jobs.items()]
    threads.append(threading.Thread(target=fetch, args=("post", jobs["all"], "POST")))
    threads.append(threading.Thread(target=fetch, args=("head", jobs["all"], "HEAD")))
    for t in threads:
        t.start()
    assert wait_for(lambda: len(results) == len(threads))
    assert results["all"][0] == 200 and results["all"][1].startswith("text/calendar")
    assert b"BEGIN:VTODO" in results["all"][2] and b"Feature Tour" in results["board"][2]
    assert b"X-WR-CALNAME:My tasks" in results["mine"][2]
    assert results["bad"][0] == 404 and results["missing"][0] == 404 and results["post"][0] == 405
    assert results["head"][0] == 200 and results["head"][2] == b""

    dlg.regenerate_token(confirm=False)
    assert load_feed_settings(service)["token"] != token and server.running
    dlg.feed_enabled.setChecked(False)
    dlg.apply_feed()
    assert not server.running
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        ok, message = server.start(save_feed_settings(service, {"port": blocker.getsockname()[1], "enabled": True}))
        assert not ok and "Could not open port" in message
    finally:
        blocker.close()
    server.stop()


def test_calendar_files_tab(env, tmp_path, boxes):
    service, demo = env
    other = service.create_board(demo.workspace_id, "From ics")
    dlg = CalendarSyncDialog(service, CalendarFeedServer(service), SyncRunner(service), demo.id)
    out = dlg.export_file(path=str(tmp_path / "t.ics"))
    assert out.read_text(encoding="utf-8").startswith("BEGIN:VCALENDAR") and "✅ Exported" in dlg.files_status.text()
    dlg.import_board.setCurrentIndex(dlg.import_board.findData(other.id))
    summary = dlg.import_file(path=str(out))
    assert summary.created == len(service.get_tasks_for_board(other.id)) > 0 and dlg.undo_btn.isEnabled()
    assert dlg.undo_import() == summary.created and service.get_tasks_for_board(other.id) == []
    bad = tmp_path / "bad.ics"
    bad.write_text("nope")
    assert dlg.import_file(path=str(bad)) is None and "❌" in dlg.files_status.text()


def test_main_window_integration_wiring(env, monkeypatch):
    service, demo = env
    win = MainWindow(service=service)
    win.show()
    win.open_board(demo.id)
    assert not win._sync_timer.isActive() and not win.feed_server.running, "tests: no background integration"
    opened = []
    monkeypatch.setattr(ImportExportDialog, "exec", lambda dlg: opened.append(("io", dlg.board_id)) or 0)
    monkeypatch.setattr(CalendarSyncDialog, "exec", lambda dlg: opened.append(("cal", dlg.tabs.currentIndex())) or 0)
    win.open_import_export()
    win.open_calendar_sync(0, "files")
    win.open_calendar_sync(0, "feed")
    assert opened == [("io", demo.id), ("cal", 2), ("cal", 1)]
    from kanbanmanager.core.calendar_sync import SyncReport

    report = SyncReport(1, "Work")
    report.counts["created"] = 2
    win._on_sync_finished([report])
    assert win.status_bar.currentMessage() == "🔄 Calendar sync - Work: 2 new"
    win.time_tracker.shutdown()
    win.close()
    win.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

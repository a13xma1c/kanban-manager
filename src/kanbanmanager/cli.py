"""
Command-line tools (no window): export, import and calendar sync - handy for
scripts, cron jobs and publishing files to a shared folder.

    kanbanmanager --export xlsx --board "Sprint 42" --output sprint.xlsx
    kanbanmanager --export pdf  --board "Sprint 42" --days 90        # board report
    kanbanmanager --export png  --board "Sprint 42"                  # board picture (also svg)
    kanbanmanager --export ics  --events --output ~/Public/tasks.ics # all boards' due dates
    kanbanmanager --export md|csv|json
    kanbanmanager --import issues.csv --board "Sprint 42"            # csv, tsv, xlsx, .ics, Trello .json
    kanbanmanager --sync                                             # every calendar subscription
    kanbanmanager --sync "Work calendar"
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

EXPORT_FORMATS = ("csv", "xlsx", "md", "json", "ics", "pdf", "png", "svg")
_APP = None  # the QApplication tools need (fonts, painting); kept alive while they run


def add_arguments(parser) -> None:
    g = parser.add_argument_group("tools (run without opening the window)")
    g.add_argument("--export", choices=EXPORT_FORMATS, metavar="FORMAT",
                   help="export: " + ", ".join(EXPORT_FORMATS) + " (pdf = board report, png/svg = board picture)")
    g.add_argument("--output", metavar="PATH", help="where to write the export (default: current folder)")
    g.add_argument("--board", metavar="NAME", help="board name or id (default: all boards where that makes sense)")
    g.add_argument("--days", type=int, default=30, metavar="N", help="report period in days (pdf)")
    g.add_argument("--events", action="store_true", help="ics: events on the due date instead of to-dos")
    g.add_argument("--mine", action="store_true", help="ics: only tasks assigned to you")
    g.add_argument("--completed", action="store_true", help="ics: include completed tasks")
    g.add_argument("--import", dest="import_path", metavar="PATH",
                   help="import a csv/tsv/xlsx spreadsheet, an .ics calendar or a Trello .json export")
    g.add_argument("--sync", nargs="?", const="", default=None, metavar="NAME",
                   help="run calendar subscriptions (all, or the one named)")


def wants_tools(args) -> bool:
    return bool(args.export or args.import_path or args.sync is not None)


def _board(service, text: Optional[str]):
    if not text:
        return None
    boards = [b for ws in service.get_workspaces() for b in ws.boards if not b.is_archived]
    if text.isdigit():
        match = [b for b in boards if b.id == int(text)]
    else:
        match = [b for b in boards if b.name.lower() == text.lower()] or \
            [b for b in boards if b.name.lower().startswith(text.lower())]
    if not match:
        raise SystemExit(f"No board named '{text}'. Boards: " + ", ".join(b.name for b in boards))
    if len(match) > 1:
        raise SystemExit(f"'{text}' matches several boards: " + ", ".join(b.name for b in match))
    return match[0]


def _safe(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in text).strip() or "kanban"


def run(args, db_manager=None) -> int:
    """Run the requested tool; returns the exit code. ``db_manager``: the database to work on
    (default: the app's own)."""
    global _APP
    previous_handler = None
    if args.export in ("pdf", "png", "svg"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no display needed
        from PySide6.QtCore import qInstallMessageHandler

        def quiet(_mode, _context, message):  # the offscreen plugin warns about window hints
            if "propagateSizeHints" not in message:
                print(message, file=sys.stderr)
        previous_handler = qInstallMessageHandler(quiet)
    from PySide6.QtWidgets import QApplication

    _APP = QApplication.instance() or QApplication(sys.argv[:1])
    from kanbanmanager.database.connection import get_db_manager
    from kanbanmanager.database.service import DatabaseService

    db = db_manager or get_db_manager()
    db.init_database(seed_defaults=False)
    service = DatabaseService(db)
    try:
        board = _board(service, args.board)
        code = 0
        if args.export:
            code = max(code, _export(service, board, args))
        if args.import_path:
            code = max(code, _import(service, board, args))
        if args.sync is not None:
            code = max(code, _sync(service, args.sync))
        return code
    finally:
        if previous_handler is not None or args.export in ("pdf", "png", "svg"):
            from PySide6.QtCore import qInstallMessageHandler
            qInstallMessageHandler(previous_handler)


def _export(service, board, args) -> int:
    from kanbanmanager.core import calendar_sync as cs
    from kanbanmanager.core import exchange as x

    fmt = args.export
    needs_board = fmt in ("md", "pdf", "png", "svg")
    if needs_board and board is None:
        print(f"--export {fmt} needs --board", file=sys.stderr)
        return 2
    stem = _safe(board.name) if board else "kanban"
    default_name = {"pdf": f"{stem} report.pdf", "json": "kanban-backup.json"}.get(fmt, f"{stem}.{fmt}")
    out = Path(args.output).expanduser() if args.output else Path.cwd() / default_name
    board_ids = [board.id] if board else None
    if fmt in ("csv", "xlsx"):
        headers, rows = x.task_table(service, board_ids)
        (x.write_xlsx(headers, rows, out, sheet=board.name if board else "Tasks") if fmt == "xlsx"
         else x.write_csv(headers, rows, out))
        detail = f"{len(rows)} task{'s' if len(rows) != 1 else ''}"
    elif fmt == "md":
        out.write_text(x.board_markdown(service, board.id), encoding="utf-8")
        detail = "Markdown"
    elif fmt == "json":
        from kanbanmanager.core.backup_manager import BackupManager

        BackupManager(service.db).export_to_json(out)
        detail = "full backup"
    elif fmt == "ics":
        text = cs.export_calendar(service, board_ids, person=service.actor if args.mine else None,
                                  kind="VEVENT" if args.events else "VTODO", include_completed=args.completed)
        out.write_text(text, encoding="utf-8", newline="")
        n = text.count("BEGIN:VEVENT") + text.count("BEGIN:VTODO")
        detail = f"{n} calendar item{'s' if n != 1 else ''}"
    elif fmt == "pdf":
        from kanbanmanager.ui.reports import write_board_report

        points = next((f["id"] for f in service.get_custom_fields(board.id)
                       if f["field_type"] == "number" and "point" in f["name"].lower()), None)
        write_board_report(service, board.id, out, days=args.days, points_field_id=points)
        detail = "board report"
    else:
        from kanbanmanager.ui.reports import export_board_snapshot

        export_board_snapshot(service, board.id, out, fmt)
        detail = "board picture"
    print(f"Exported {detail} to {out}")
    return 0


def _import(service, board, args) -> int:
    from kanbanmanager.core import calendar_sync as cs
    from kanbanmanager.core import exchange as x

    path = Path(args.import_path).expanduser()
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and "cards" in data and "lists" in data:
                ws = service.get_board(board.id).workspace_id if board else service.get_workspaces()[0].id
                new_board, result = x.import_trello(service, data, ws)
                print(f"Created board '{new_board.name}': {result.describe()}")
            else:
                from kanbanmanager.core.backup_manager import BackupManager

                BackupManager(service.db).import_from_json(path)
                print("Imported the backup")
            return 0
        if board is None:
            print("--import needs --board (the board that receives the tasks)", file=sys.stderr)
            return 2
        if suffix in (".ics", ".ical"):
            summary = cs.import_calendar(service, cs.fetch_text(str(path)), board.id)
            print(f"Imported into '{board.name}': {summary.describe()}")
            for w in summary.warnings:
                print(f"Note: {w}", file=sys.stderr)
            return 0
        table = x.read_table(path)
        mapping = x.guess_mapping(table, service.get_custom_fields(board.id))
        if "title" not in mapping.values():
            print("Could not tell which column holds the titles - use the Import wizard in the app",
                  file=sys.stderr)
            return 2
        result = x.run_import(service, table, x.ImportOptions(board_id=board.id, mapping=mapping))
        print(f"Imported into '{board.name}': {result.describe()}")
        for w in result.warnings:
            print(f"Note: {w}", file=sys.stderr)
        return 0
    except (x.ExchangeError, cs.SyncError, ValueError, OSError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1


def _sync(service, name: str) -> int:
    from kanbanmanager.core import calendar_sync as cs

    subs = service.get_calendar_subscriptions()
    if name:
        subs = [s for s in subs if s["name"].lower() == name.lower()]
        if not subs:
            print(f"No calendar subscription named '{name}'", file=sys.stderr)
            return 2
    if not subs:
        print("No calendar subscriptions yet (add one under Calendar Sync in the app)")
        return 0
    engine = cs.CalendarSync(service)
    code = 0
    for sub in subs:
        report = engine.sync(sub["id"])
        print(f"{sub['name']}: {report.describe()}")
        for m in report.messages:
            print(f"  {m}", file=sys.stderr)
        if report.status == "failed":
            code = 1
    return code

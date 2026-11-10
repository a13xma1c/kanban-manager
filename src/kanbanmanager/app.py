"""
Application Entry Point for KanbanManager

    kanbanmanager                       start (or bring the running window forward)
    kanbanmanager --capture             open Quick Capture (in the running app, or standalone)
    kanbanmanager --capture "TEXT"      capture TEXT right away (see core.capture for the syntax)

``--capture`` is what to bind to a system keyboard shortcut where apps can't
register global hotkeys themselves (Wayland, macOS).

Tools that run without opening the window (see kanbanmanager.cli)::

    kanbanmanager --export xlsx|csv|md|json|ics|pdf|png|svg [--board NAME] [--output PATH]
    kanbanmanager --import FILE [--board NAME]
    kanbanmanager --sync [NAME]
"""
import argparse
import sys
from typing import List, Optional

from PySide6.QtWidgets import QApplication

from kanbanmanager.config import APP_NAME


def _run_event_loop(app: QApplication) -> int:
    """Qt's main loop. (Tests replace this: once a process has run ``exec()`` to its end, Qt
    treats the application as closing and nested event loops stop working.)"""
    return app.exec()


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kanbanmanager", description="KanbanManager desktop app")
    parser.add_argument("--capture", nargs="?", const="", default=None, metavar="TEXT",
                        help="quick-capture TEXT, or open the capture window when no text is given")
    from kanbanmanager import cli

    cli.add_arguments(parser)
    args, unknown = parser.parse_known_args(argv)  # Qt may add its own arguments
    args.words = [w for w in unknown if not w.startswith("-")]
    return args


def capture_main(argv: Optional[List[str]] = None, db_manager=None) -> int:
    """``kanbanmanager --capture [TEXT]``. ``db_manager``: the database (default: the app's)."""
    from kanbanmanager.core import capture
    from kanbanmanager.database.connection import get_db_manager
    from kanbanmanager.database.service import DatabaseService
    from kanbanmanager.ui.ipc import send_command, server_name

    args = _parse_args(sys.argv[1:] if argv is None else argv)
    # `kanbanmanager --capture TEXT` or `kanbanmanager-capture TEXT`
    text = (args.capture if args.capture else " ".join(args.words)).strip()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    QApplication.setApplicationName(APP_NAME)
    db_manager = db_manager or get_db_manager()
    if send_command(server_name(db_manager.db_path), {"cmd": "capture", "text": text}):
        return 0  # the running app takes it from here

    db_manager.init_database(seed_defaults=False)
    service = DatabaseService(db_manager)
    if text:
        try:
            saved = capture.commit_capture(service, text, source="command_line")
        except ValueError as exc:
            print(f"Nothing captured: {exc}", file=sys.stderr)
            return 2
        for warning in saved["result"].warnings:
            print(f"Note: {warning}", file=sys.stderr)
        where = f"board '{saved['board_name']}'" if saved["kind"] == "task" else "the Inbox"
        print(f"Captured '{saved['title']}' to {where}")
        return 0

    from kanbanmanager.ui import theme
    from kanbanmanager.ui.dialogs.quick_capture import QuickCaptureDialog
    from kanbanmanager.ui.styles import get_stylesheet

    theme.apply_appearance(service)
    app.setStyleSheet(theme.apply(get_stylesheet()))
    dialog = QuickCaptureDialog(service)
    dialog.summon()
    dialog.finished.connect(app.quit)
    _run_event_loop(app)
    return 0


def main() -> int:
    """Initialize application, database, and start the Qt event loop."""
    args = _parse_args(sys.argv[1:])
    if args.capture is not None:
        return capture_main()
    from kanbanmanager import cli

    if cli.wants_tools(args):
        return cli.run(args)

    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName("KanbanManager")
    app = QApplication.instance() or QApplication(sys.argv)
    window = launch()
    if window is None:
        return 0  # the running instance came to the front instead
    return _run_event_loop(app)


def launch(db_manager=None, desktop_integration: bool = True):
    """Open the main window on ``db_manager`` (default: the app's database), seeding a new
    database first. Returns None when another instance is already running on it - that one
    is brought to the front instead."""
    from kanbanmanager.database.connection import get_db_manager
    from kanbanmanager.database.service import DatabaseService
    from kanbanmanager.ui.ipc import send_command, server_name

    db_manager = db_manager or get_db_manager()
    # Single instance: a second launch brings the existing window forward
    if send_command(server_name(db_manager.db_path), {"cmd": "show"}, timeout_ms=500):
        return None

    # Initialize SQLite Database & seed defaults
    db_manager.init_database(seed_defaults=True)

    from kanbanmanager.ui.main_window import MainWindow

    window = MainWindow(service=DatabaseService(db_manager), desktop_integration=desktop_integration)
    window.show()
    return window


if __name__ == "__main__":
    sys.exit(main())

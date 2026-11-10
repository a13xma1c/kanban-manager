"""
Start-up: ``launch()`` (what ``kanbanmanager`` runs) with the single-instance
socket, the commands a second launch sends, and ``capture_main`` (what
``kanbanmanager --capture`` runs) - all in-process.
"""
from __future__ import annotations

import time

import pytest
from PySide6.QtWidgets import QApplication

from kanbanmanager import app as entry
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.dialogs.quick_capture import QuickCaptureDialog
from support import flush_qt


def wait_for(condition, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        flush_qt(1)
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.fixture
def db(tmp_path):
    return DatabaseManager(tmp_path / f"app{time.time_ns()}.db")


def test_launch_seeds_a_new_database_and_opens_the_window(qapp, db):
    window = entry.launch(db)
    assert window is not None and window.isVisible()
    service = DatabaseService(db)
    assert [b.name for b in service.get_workspaces()[0].boards] == ["Product Roadmap", "Feature Tour"]
    assert window.current_board_id is not None and window.board_view._columns()
    assert window.command_server is not None, "the real app listens for a second launch"
    window.shutdown()


def test_a_second_launch_hands_over_to_the_running_window(qapp, db):
    first = entry.launch(db)
    shown = []
    first.showNormal = lambda: shown.append(True)  # what "show" does (raising is a no-op offscreen)
    assert entry.launch(db) is None, "the second launch finds the first and stops"
    assert wait_for(lambda: shown), "and the first one comes to the front"

    from kanbanmanager.ui.ipc import send_command, server_name
    assert send_command(server_name(db.db_path), {"cmd": "capture", "text": "Call the bank tomorrow !high"})
    service = DatabaseService(db)
    assert wait_for(lambda: any(i["title"] == "Call the bank" for i in service.get_inbox_items()))
    assert "Captured 'Call the bank'" in first.status_bar.currentMessage()
    first.shutdown()
    assert entry.launch(db) is not None, "after it closes, a launch opens a window again"


def test_capture_from_the_command_line(qapp, db, capsys):
    db.init_database(seed_defaults=True)
    assert entry.capture_main(["--capture", "Renew passport next month !high"], db_manager=db) == 0
    assert "Captured 'Renew passport' to the Inbox" in capsys.readouterr().out
    assert entry.capture_main(["--capture", "Fix the roadmap >product"], db_manager=db) == 0
    assert "to board 'Product Roadmap'" in capsys.readouterr().out
    assert entry.capture_main(["--capture", "x >nosuchboard"], db_manager=db) == 0
    assert "No board matches 'nosuchboard'" in capsys.readouterr().err
    assert entry.capture_main(["--capture", "!high"], db_manager=db) == 2, "nothing left to capture"
    assert "Nothing captured" in capsys.readouterr().err


def test_capture_goes_to_the_running_app_when_there_is_one(qapp, db, monkeypatch):
    sent = []
    import kanbanmanager.ui.ipc as ipc
    monkeypatch.setattr(ipc, "send_command", lambda name, payload, **k: sent.append(payload) or True)
    assert entry.capture_main(["--capture", "Ping Ann"], db_manager=db) == 0
    assert sent == [{"cmd": "capture", "text": "Ping Ann"}]


def test_capture_without_text_opens_the_capture_window(qapp, db, monkeypatch):
    seen = []

    def event_loop(app):
        # Never run the real app.exec() here: after it returns, Qt treats the (shared)
        # application as closing and every later test's event loop would stop working.
        dialog = next(w for w in QApplication.topLevelWidgets() if isinstance(w, QuickCaptureDialog)
                      and w.isVisible())
        seen.append(dialog.windowTitle())
        dialog.reject()  # the user closes it
        return 0

    monkeypatch.setattr(entry, "_run_event_loop", event_loop)
    assert entry.capture_main(["--capture"], db_manager=db) == 0
    assert len(seen) == 1, "the capture window was open while the loop ran"


def test_main_runs_the_tools_or_the_window(qapp, db, monkeypatch, capsys):
    import sys
    from kanbanmanager.database import connection
    monkeypatch.setattr(connection, "get_db_manager", lambda: db)
    monkeypatch.setattr(sys, "argv", ["kanbanmanager", "--export", "csv", "--output", str(db.db_path.parent / "t.csv")])
    assert entry.main() == 0 and "Exported" in capsys.readouterr().out
    loops = []
    monkeypatch.setattr(entry, "_run_event_loop", lambda app: loops.append(app) or 0)
    monkeypatch.setattr(sys, "argv", ["kanbanmanager"])
    assert entry.main() == 0 and loops == [QApplication.instance()]
    window = next(w for w in QApplication.topLevelWidgets() if type(w).__name__ == "MainWindow" and w.isVisible())
    assert window.service.db.db_path == db.db_path
    assert entry.main() == 0 and len(loops) == 1, "a second start hands over to the first window"


def test_words_after_the_options_are_the_capture_text():
    args = entry._parse_args(["Buy", "milk", "-platformtheme", "x"])  # Qt's own flags are ignored
    assert args.words == ["Buy", "milk", "x"] and args.capture is None

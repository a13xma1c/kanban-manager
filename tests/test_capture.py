"""
Tests for the quick-capture language, saving captures, the Global Inbox, the
cross-workspace focus query, cross-board moves, single-instance IPC, the
command line, and the X11 global hotkey (against a real Xvfb server).
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from kanbanmanager import BackupManager, DatabaseService
from kanbanmanager.core import capture as cap
from kanbanmanager.core.automation import AutomationEngine
from kanbanmanager.database.connection import DatabaseManager

SRC = Path(__file__).resolve().parents[1] / "src"
NOW = datetime(2024, 6, 5, 14, 0)  # a Wednesday afternoon
PEOPLE = ["Marcus Chen", "Priya Sharma", "Elena Rossi", "Ann"]
BOARDS = [(1, "Product Roadmap"), (2, "Feature Tour"), (3, "Marketing Plan"), (4, "Mobile App")]


def parse(text):
    return cap.parse_capture(text, NOW, PEOPLE, BOARDS, me="Ann")


@pytest.fixture(scope="module")
def qcore():
    return QCoreApplication.instance() or QCoreApplication([])


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def test_parse_everything():
    r = parse("Fix login timeout tomorrow 3pm !high #Bug @marcus >roadmap // check the SSO flow")
    assert (r.title, r.due, r.priority, r.labels, r.assignee, r.board_id, r.notes) == (
        "Fix login timeout", datetime(2024, 6, 6, 15, 0), "HIGH", ["Bug"], "Marcus Chen", 1, "check the SSO flow")


@pytest.mark.parametrize("text,title,due", [
    ("Plan monday standup tomorrow", "Plan monday standup", datetime(2024, 6, 6, 17)),   # last date wins
    ('"Monday report" fri', "Monday report", datetime(2024, 6, 7, 17)),                 # quotes are literal
    ("call mom at 9", "call mom", datetime(2024, 6, 6, 9)),                              # 9am passed -> tomorrow
    ("call mom 16:30", "call mom", datetime(2024, 6, 5, 16, 30)),                        # still today
    ("lunch at 1", "lunch", datetime(2024, 6, 6, 13)),                                   # "at 1" = 13:00
    ("ship next fri", "ship", datetime(2024, 6, 14, 17)),                                # Friday of next week
    ("ship fri", "ship", datetime(2024, 6, 7, 17)),
    ("report wed", "report", datetime(2024, 6, 5, 17)),                                  # today is Wednesday
    ("pay rent eom", "pay rent", datetime(2024, 6, 30, 17)),
    ("wrap up eow", "wrap up", datetime(2024, 6, 7, 17)),
    ("hike weekend", "hike", datetime(2024, 6, 8, 17)),
    ("review in 2 weeks", "review", datetime(2024, 6, 19, 17)),
    ("renew in a month", "renew", datetime(2024, 7, 5, 17)),
    ("sync next week", "sync", datetime(2024, 6, 10, 17)),
    ("deck jun 3", "deck", datetime(2025, 6, 3, 17)),                                    # passed -> next year
    ("deck 5 june 2026 noon", "deck", datetime(2026, 6, 5, 12)),
    ("release 2024-07-01 9:15am", "release", datetime(2024, 7, 1, 9, 15)),
    ("standup tonight", "standup", datetime(2024, 6, 5, 20)),
    ("no date here", "no date here", None),
])
def test_dates_and_times(text, title, due):
    r = parse(text)
    assert (r.title, r.due) == (title, due)


def test_markers_people_boards_and_edge_cases():
    assert parse("x !1").priority == "URGENT" and parse("x !4").priority == "LOW"
    assert parse("x !!!").priority == "URGENT" and parse("x !!").priority == "HIGH"
    assert parse("x !nonsense").title == "x !nonsense"
    assert parse("x #a #A #b").labels == ["a", "b"], "case-insensitive de-duplication"
    assert parse("x @pri").assignee == "Priya Sharma" and parse("x @me").assignee == "Ann"
    assert parse("x @newperson").assignee == "Newperson"
    assert parse("taxes >feature").board_name == "Feature Tour"
    r = parse("taxes >m")
    assert r.board_id is None and "several boards" in r.warnings[0]
    assert "No board matches" in parse("taxes >zzz").warnings[0]
    assert parse("a > b").title == "a > b", "a bare '>' is text"
    r = parse("// just notes")
    assert r.title == "just notes" and r.is_valid
    assert not parse("   ").is_valid and not parse("!high #x").is_valid
    assert cap.describe_due(datetime(2024, 6, 6, 15), NOW) == "Tomorrow 15:00"
    assert cap.describe_due(datetime(2025, 6, 3, 17), NOW) == "Tue Jun 3 2025 17:00"


# ---------------------------------------------------------------------------
# Saving, Inbox, triage
# ---------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path):
    mgr = DatabaseManager(tmp_path / "cap.db")
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr, actor="Ann")
    ws1 = service.create_workspace("Work")
    ws2 = service.create_workspace("Home")
    work = service.create_board(ws1.id, "Product Roadmap")
    home = service.create_board(ws2.id, "House")
    return service, work, home


def test_commit_capture_to_board_or_inbox(env):
    service, work, home = env
    saved = cap.commit_capture(service, "Fix crash tomorrow !urgent #Bug #Hotfix @me >roadmap // stack trace attached")
    assert saved["kind"] == "task" and saved["board_name"] == "Product Roadmap"
    task = service.get_task(saved["id"])
    todo = next(c for c in service.get_columns_for_board(work.id) if c.column_type == "TODO")
    assert task.column_id == todo.id and task.priority == "URGENT" and task.assignee == "Ann"
    assert sorted(l.name for l in task.labels) == ["Bug", "Hotfix"], "missing labels are created"
    assert task.description == "stack trace attached"
    assert service.get_task_activity(task.id)[0]["details"].endswith("(via Quick Capture)")

    inboxed = cap.commit_capture(service, "Buy paint next week #Shopping")
    assert inboxed["kind"] == "inbox" and service.count_inbox_items() == 1
    explicit = cap.commit_capture(service, "Fix fence", board_id=home.id)
    assert explicit["kind"] == "task" and explicit["board_name"] == "House"
    with pytest.raises(ValueError):
        cap.commit_capture(service, "  ")


def test_inbox_crud_and_triage(env):
    service, work, home = env
    item = service.add_inbox_item("Paint the fence", raw_text="Paint the fence fri !high #Garden @ann",
                                  priority="HIGH", due_date=datetime(2030, 6, 7, 17), labels=["Garden"],
                                  assignee="Ann", board_hint_id=home.id)
    assert service.get_inbox_items()[0]["board_hint_id"] == home.id
    service.update_inbox_item(item["id"], title="Paint the fence white")
    with pytest.raises(ValueError):
        service.update_inbox_item(item["id"], title=" ")
    task = service.triage_inbox_item(item["id"], home.id)
    assert service.count_inbox_items() == 0
    t = service.get_task(task.id)
    assert (t.title, t.priority, t.assignee, t.due_date) == ("Paint the fence white", "HIGH", "Ann",
                                                            datetime(2030, 6, 7, 17))
    assert [l.name for l in t.labels] == ["Garden"]
    assert "from the Inbox, captured" in service.get_task_activity(t.id)[0]["details"]
    other = service.add_inbox_item("Call plumber")
    col = service.get_columns_for_board(work.id)[0].id
    assert service.triage_inbox_item(other["id"], work.id, col, title="Call the plumber").column_id == col
    assert service.delete_inbox_item(999) is False
    with pytest.raises(ValueError):
        service.add_inbox_item("")


def test_focus_query_spans_workspaces(env):
    service, work, home = env
    wc, hc = service.get_columns_for_board(work.id), service.get_columns_for_board(home.id)
    service.create_task(work.id, wc[1].id, "Mine at work", assignee="Ann")
    service.create_task(home.id, hc[1].id, "Mine at home", assignee="Ann", due_date=datetime.now())
    service.create_task(home.id, hc[1].id, "Unassigned chore")
    service.create_task(work.id, wc[1].id, "Marcus's", assignee="Marcus")
    done = service.create_task(work.id, wc[1].id, "Finished", assignee="Ann")
    service.toggle_task_completion(done.id)
    archived = service.create_task(work.id, wc[1].id, "Archived", assignee="Ann")
    service.archive_task(archived.id)
    mine = service.get_focus_tasks()
    assert sorted(t["title"] for t in mine) == ["Mine at home", "Mine at work"]
    assert {t["workspace_name"] for t in mine} == {"Work", "Home"}
    assert len(service.get_focus_tasks(include_unassigned=True)) == 3
    assert [t["title"] for t in service.get_focus_tasks("Marcus")] == ["Marcus's"]


# ---------------------------------------------------------------------------
# Cross-board moves
# ---------------------------------------------------------------------------
def test_move_task_to_another_board(env):
    service, work, home = env
    wc, hc = service.get_columns_for_board(work.id), service.get_columns_for_board(home.id)
    bug = next(l for l in service.get_labels_for_board(work.id) if l.name == "Bug")
    custom = service.create_label(work.id, "Only at work", "#123456")
    t = service.create_task(work.id, wc[1].id, "Travels", label_ids=[bug.id, custom.id])
    service.add_comment(t.id, "comes along")
    service.add_time_entry(t.id, 600)
    service.create_task(home.id, hc[1].id, "Already there")
    stay = service.create_task(work.id, wc[1].id, "Stays")

    ok, err = service.move_task(t.id, hc[1].id, 0)
    assert ok, err
    moved = service.get_task(t.id)
    assert moved.board_id == home.id and moved.column_id == hc[1].id and moved.position == 0
    assert {l.name: l.board_id for l in moved.labels} == {"Bug": home.id, "Only at work": home.id}
    assert next(l for l in moved.labels if l.name == "Only at work").color == "#123456"
    assert [x.title for x in service.get_tasks_for_board(home.id)] == ["Travels", "Already there"]
    assert service.get_task(stay.id).position == 0, "gap closed on the old board"
    assert len(service.get_comments(t.id)) == 1 and service.get_task(t.id).spent_hours == pytest.approx(600 / 3600)
    entry = service.get_task_activity(t.id)[-1]
    assert entry["board_id"] == home.id and "from board 'Product Roadmap'" in entry["details"]
    assert any("to board 'House'" in a["details"] for a in service.get_board_activities(work.id))

    service.move_task(t.id, hc[4].id)  # into Done on that board
    assert service.get_task(t.id).is_completed
    service.update_column(wc[2].id, wip_limit=1)
    service.create_task(work.id, wc[2].id, "Fills WIP")
    ok, err = service.move_task(t.id, wc[2].id)
    assert not ok and "WIP limit" in err


def test_cross_board_move_triggers_target_board_automation(env):
    service, work, home = env
    engine = AutomationEngine(service)
    engine.attach()
    hc = service.get_columns_for_board(home.id)
    service.save_automation_rule(home.id, {"name": "Welcome", "trigger": {"type": "task_moved",
                                                                        "to_column_id": hc[1].id},
                                           "actions": [{"type": "add_comment", "text": "Welcome to {board}!"}]})
    t = service.create_task(work.id, service.get_columns_for_board(work.id)[1].id, "Visitor")
    service.move_task(t.id, hc[1].id)
    assert service.get_comments(t.id)[-1]["body"] == "Welcome to House!"


# ---------------------------------------------------------------------------
# JSON export of the Inbox
# ---------------------------------------------------------------------------
def test_inbox_is_exported_and_imported(env, tmp_path):
    service, work, home = env
    service.add_inbox_item("Later", labels=["x"], board_hint_id=home.id, due_date=datetime(2030, 1, 2, 17))
    dest = BackupManager(service.db).export_to_json(tmp_path / "x.json")
    assert json.loads(dest.read_text())["inbox"][0]["board_hint"] == "House"
    target = DatabaseManager(tmp_path / "n" / "k.db")
    target.init_database(seed_defaults=False)
    BackupManager(target).import_from_json(dest)
    s2 = DatabaseService(target)
    item = s2.get_inbox_items()[0]
    house = next(b.id for ws in s2.get_workspaces() for b in ws.boards if b.name == "House")
    assert (item["title"], item["labels"], item["board_hint_id"]) == ("Later", ["x"], house)


# ---------------------------------------------------------------------------
# IPC, command line, global hotkey
# ---------------------------------------------------------------------------
def test_ipc_round_trip_and_stale_socket(qcore, tmp_path):
    from kanbanmanager.ui.ipc import CommandServer, send_command, server_name

    db = tmp_path / f"ipc{time.time_ns()}.db"
    name = server_name(db)
    assert name == server_name(db) and name != server_name(tmp_path / "other.db"), "one socket per database"
    assert send_command(name, {"cmd": "show"}, timeout_ms=200) is False, "nobody listening"
    server = CommandServer(name)
    received = []
    server.command_received.connect(received.append)
    assert server.start()
    assert CommandServer(name).start() is False, "a second instance doesn't steal the socket"
    assert send_command(name, {"cmd": "capture", "text": "hello"})
    for _ in range(20):
        if received:
            break
        wait(50)
    assert received == [{"cmd": "capture", "text": "hello"}]
    assert send_command(name, {"cmd": "bogus"})
    wait(200)
    assert len(received) == 1, "unknown commands are ignored"
    server.stop()


@pytest.mark.slow
def test_command_line_capture_without_running_app(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(SRC), KANBAN_DATA_DIR=str(tmp_path))
    run = subprocess.run([sys.executable, "-m", "kanbanmanager", "--capture", "Renew passport next month !high"],
                         env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0 and "Captured 'Renew passport' to the Inbox" in run.stdout, run.stderr
    warn = subprocess.run([sys.executable, "-m", "kanbanmanager", "--capture", "x >nosuchboard"],
                          env=env, capture_output=True, text=True, timeout=120)
    assert "No board matches 'nosuchboard'" in warn.stderr
    service = DatabaseService(DatabaseManager(tmp_path / "kanban.db"))
    items = service.get_inbox_items()
    assert [i["title"] for i in items] == ["x", "Renew passport"] and items[1]["priority"] == "HIGH"


needs_x = pytest.mark.skipif(not (shutil.which("Xvfb") and shutil.which("xdotool")),
                             reason="needs Xvfb and xdotool")


@pytest.fixture
def xserver():
    display = f":{90 + os.getpid() % 9}"
    proc = subprocess.Popen(["Xvfb", display, "-screen", "0", "800x600x24"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    old = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = display
    os.environ.pop("WAYLAND_DISPLAY", None)
    os.environ["XDG_SESSION_TYPE"] = "x11"
    yield display
    if old is None:
        os.environ.pop("DISPLAY", None)
    else:
        os.environ["DISPLAY"] = old
    proc.terminate()
    proc.wait(5)


@needs_x
def test_x11_global_hotkey(qcore, xserver):
    from kanbanmanager.ui.global_hotkey import GlobalHotkey

    hits = []
    hk = GlobalHotkey()
    hk.activated.connect(lambda: hits.append(1))
    ok, message = hk.start("Ctrl+Shift+Space")
    assert ok, message

    def press(keys):
        subprocess.run(["xdotool", "key", keys], env=dict(os.environ), check=True)
        wait(250)

    press("ctrl+shift+space")
    assert hits == [1]
    press("ctrl+space")
    assert hits == [1], "other combinations are not captured"
    press("Num_Lock")
    press("ctrl+shift+space")
    assert hits == [1, 1], "works with NumLock on"
    rival = GlobalHotkey()
    assert rival.start("Ctrl+Shift+Space") == (False, "Another application already uses this shortcut")
    hk.stop()
    press("ctrl+shift+space")
    assert len(hits) == 2
    assert rival.start("Ctrl+Shift+Space")[0], "free again after stop()"
    rival.stop()


def test_hotkey_parsing_and_unsupported_sessions(qcore, monkeypatch):
    from kanbanmanager.ui import global_hotkey as gh

    assert gh.parse_hotkey("Ctrl+Shift+Space") == ({"ctrl", "shift"}, "space")
    assert gh.parse_hotkey("meta+alt+k") == ({"super", "alt"}, "k")
    assert gh.parse_hotkey("Ctrl+F5") == ({"ctrl"}, "f5")
    for bad in ("", "Space", "Ctrl+Shift", "Ctrl+Hyper+K", "Ctrl+§"):
        with pytest.raises(ValueError):
            gh.parse_hotkey(bad)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    ok, msg = gh.GlobalHotkey().start()
    assert not ok and "kanbanmanager --capture" in msg

"""
Tests for calendar integration: the iCalendar reader/writer (folding, escaping,
parameters, dates & time zones, durations, RRULE expansion and mapping), .ics
export (to-dos, all-day and timed events, alarms, filters) and import
(time zones, recurring and multi-day events, updates instead of duplicates),
feed subscriptions, and two-way CalDAV sync against a local test server.
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from caldav_server import EVENTS, PASSWORD, TASKS, USER, Server
from kanbanmanager import DatabaseService
from kanbanmanager.core import calendar_sync as cs
from kanbanmanager.core import ical
from kanbanmanager.core.ical import Component, ICalError
from kanbanmanager.database.connection import DatabaseManager
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from support import seeded_manager

NOW = datetime(2031, 1, 8, 12, 0)   # a Wednesday


@pytest.fixture
def env(tmp_path):
    mgr = DatabaseManager(tmp_path / "cal.db")
    mgr.init_database(seed_defaults=False)
    service = DatabaseService(mgr, actor="Ann")
    ws = service.create_workspace("W")
    board = service.create_board(ws.id, "Team")
    cols = {c.name: c for c in service.get_columns_for_board(board.id)}
    return service, ws, board, cols


def history(service, task_id):
    return [(a["actor"], a["details"]) for a in service.get_task_activity(task_id)]


def vcal(*components: str, extra: str = "") -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n" + extra + "".join(components) + \
        "END:VCALENDAR\r\n"


def vtodo(uid: str, summary: str, **props) -> str:
    lines = [f"UID:{uid}", f"SUMMARY:{summary}"] + [f"{k.replace('_', '-')}:{v}" for k, v in props.items()]
    return "BEGIN:VTODO\r\n" + "\r\n".join(lines) + "\r\nEND:VTODO\r\n"


# ---------------------------------------------------------------------------
# iCalendar format
# ---------------------------------------------------------------------------
def test_text_escaping_folding_and_parsing_round_trip():
    text = 'Ünïcødé, commas; semicolons \\ backslash\nnew line ' + "界" * 40
    cal = ical.new_calendar("Test")
    todo = Component("VTODO")
    todo.set_text("SUMMARY", text)
    todo.set("X-PARAM", "v", {"X-NAME": "a;b:c", "LANG": "en"})
    cal.children.append(todo)
    out = ical.serialize(cal)
    assert all(len(line.encode("utf-8")) <= 75 for line in out.split("\r\n")), "folded at 75 octets"
    assert "\n" not in out.replace("\r\n", ""), "CRLF line ends only"
    back = ical.parse_calendar(out).first("VTODO")
    assert back.text("SUMMARY") == text
    assert back.get("X-PARAM").params == {"X-NAME": "a;b:c", "LANG": "en"}
    # LF-only, BOM, tab continuation, lowercase names
    loose = "\ufeffbegin:vcalendar\nbegin:vevent\nuid:1\nsummary:Hel\n\tlo\nend:vevent\nend:vcalendar\n"
    assert ical.parse_calendar(loose).first("VEVENT").text("SUMMARY") == "Hello"
    assert ical.split_list("a\\,b,c, ,d") == ["a,b", "c", "d"]
    for bad in ("", "BEGIN:VCALENDAR\r\n", "BEGIN:VCALENDAR\r\nEND:VEVENT\r\n", "no colon here",
                "BEGIN:VEVENT\r\nEND:VEVENT\r\n"):
        with pytest.raises(ICalError):
            ical.parse_calendar(bad)


def test_dates_time_zones_and_durations():
    assert ical.parse_date_value("20310106") == date(2031, 1, 6)
    assert ical.parse_date_value("20310106T090000Z") == datetime(2031, 1, 6, 9, tzinfo=timezone.utc)
    berlin = ical.parse_date_value("20310106T090000", {"TZID": "Europe/Berlin"})
    assert berlin.utcoffset() == timedelta(hours=1)
    assert ical.parse_date_value("20310106T090000", {"TZID": "Pacific Standard Time"}).utcoffset() == \
        timedelta(hours=-8), "Windows zone names"
    assert ical.parse_date_value("20310106T0900", {"TZID": "Nowhere/Special"}) == datetime(2031, 1, 6, 9), \
        "unknown zone: floating"
    local = datetime(2031, 1, 6, 17, 0)
    assert ical.to_local(ical.parse_date_value(ical.format_utc(local))) == local
    with pytest.raises(ICalError):
        ical.parse_date_value("2031-01-06")
    assert ical.parse_duration("P1W") == timedelta(weeks=1)
    assert ical.parse_duration("-PT15M") == timedelta(minutes=-15)
    assert ical.format_duration(timedelta(days=1, hours=2, minutes=3)) == "P1DT2H3M"
    assert ical.format_duration(timedelta(minutes=-30)) == "-PT30M"
    with pytest.raises(ICalError):
        ical.parse_duration("PT")


def test_rrule_expansion_and_mapping():
    occ = ical.occurrences
    assert occ(date(2031, 1, 6), "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4", date(2031, 1, 1), date(2031, 12, 1)) == \
        [date(2031, 1, 6), date(2031, 1, 8), date(2031, 1, 13), date(2031, 1, 15)]
    assert occ(date(2031, 1, 31), "FREQ=MONTHLY", date(2031, 1, 1), date(2031, 5, 1)) == \
        [date(2031, 1, 31), date(2031, 3, 31)], "months without a 31st are skipped"
    assert occ(date(2031, 1, 31), "FREQ=MONTHLY;BYDAY=-1FR", date(2031, 1, 1), date(2031, 3, 31)) == \
        [date(2031, 1, 31), date(2031, 2, 28), date(2031, 3, 28)]
    assert occ(datetime(2031, 1, 6, 9), "FREQ=DAILY;INTERVAL=2;UNTIL=20310112T235959Z", datetime(2031, 1, 1),
               datetime(2031, 2, 1), exdates=[datetime(2031, 1, 8, 9)]) == \
        [datetime(2031, 1, 6, 9), datetime(2031, 1, 10, 9), datetime(2031, 1, 12, 9)]
    assert occ(date(2030, 2, 14), "FREQ=YEARLY", date(2031, 1, 1), date(2033, 1, 1)) == \
        [date(2031, 2, 14), date(2032, 2, 14)]
    assert occ(date(2031, 1, 6), None, date(2031, 1, 1), date(2031, 1, 31)) == [date(2031, 1, 6)]

    assert ical.recurrence_from_rrule("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR") == {"frequency": "WEEKDAYS", "interval": 1}
    assert ical.recurrence_from_rrule("FREQ=WEEKLY;INTERVAL=2") == {"frequency": "BIWEEKLY", "interval": 1}
    assert ical.recurrence_from_rrule("FREQ=DAILY;INTERVAL=3;COUNT=5") == {
        "frequency": "CUSTOM", "interval": 3, "custom_unit": "days", "end_type": "after_count", "max_occurrences": 5}
    assert ical.recurrence_from_rrule("FREQ=MONTHLY;BYDAY=2TU") is None, "can't be expressed: stays one task"
    for rule in ({"frequency": "DAILY"}, {"frequency": "WEEKDAYS"}, {"frequency": "BIWEEKLY"},
                 {"frequency": "MONTHLY", "interval": 2}, {"frequency": "WEEKLY", "days_of_week": [0, 3]}):
        rr = ical.rrule_from_recurrence(rule)
        back = ical.recurrence_from_rrule(rr)
        assert back["frequency"] in (rule["frequency"], "CUSTOM"), (rule, rr, back)
    assert ical.rrule_from_recurrence({"frequency": "WEEKLY", "end_type": "after_count", "max_occurrences": 5,
                                       "occurrences_count": 2}) == "FREQ=WEEKLY;COUNT=3"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def test_export_todos_events_and_filters(env):
    service, _ws, board, cols = env
    service.create_custom_field(board.id, "Points", "number", {"unit": "pts"})
    due = datetime(2031, 1, 10, 16, 30)
    a = service.create_task(board.id, cols["In Progress"].id, "Ship it, finally; really", priority="URGENT",
                            due_date=due, start_date=date(2031, 1, 6), assignee="Ann",
                            description="Notes ![pic](attachment:4) here", field_values={"Points": 3},
                            label_ids=[l.id for l in service.get_labels_for_board(board.id) if l.name == "Bug"],
                            checklist=[{"id": 1, "text": "a", "done": True}, {"id": 2, "text": "b", "done": False}])
    service.create_task(board.id, cols["To Do"].id, "Weekly sync", due_date=datetime(2031, 1, 9, 10),
                        is_recurring=True, recurrence_rule={"frequency": "WEEKLY", "interval": 1}, assignee="Bo")
    service.create_task(board.id, cols["Done"].id, "Finished", due_date=datetime(2031, 1, 2, 9))
    service.create_task(board.id, cols["Backlog"].id, "Someday")

    cal = ical.parse_calendar(cs.export_calendar(service, [board.id], alarm_minutes=15, now=NOW))
    todos = {t.text("SUMMARY"): t for t in cal.walk("VTODO")}
    assert sorted(todos) == ["Ship it, finally; really", "Weekly sync"], "open, dated tasks by default"
    t = todos["Ship it, finally; really"]
    assert t.text("UID") == cs.task_uid(service.instance_id(), a.id)
    assert t.text("DUE") == ical.format_utc(due) and t.text("STATUS") == "IN-PROCESS"
    assert t.text("PRIORITY") == "1" and ical.split_list(t.get("CATEGORIES").value) == ["Bug"]
    assert t.text("PERCENT-COMPLETE") == "50"
    assert "attachment:" not in t.text("DESCRIPTION") and "Notes" in t.text("DESCRIPTION")
    assert "Assignee: Ann" in t.text("DESCRIPTION") and "Points: 3 pts" in t.text("DESCRIPTION")
    assert t.first("VALARM").text("TRIGGER") == "-PT15M"
    assert ical.to_local(ical.prop_date(t, "DTSTART")).date() <= due.date()
    assert todos["Weekly sync"].text("RRULE") == "FREQ=WEEKLY"

    everything = ical.parse_calendar(cs.export_calendar(service, [board.id], include_completed=True,
                                                        include_undated=True, details=False))
    by_title = {t.text("SUMMARY"): t for t in everything.walk("VTODO")}
    assert by_title["Finished"].text("STATUS") == "COMPLETED" and by_title["Finished"].get("COMPLETED")
    assert by_title["Someday"].get("DUE") is None
    assert "Board:" not in (by_title["Ship it, finally; really"].text("DESCRIPTION") or "")
    mine = ical.parse_calendar(cs.export_calendar(service, [board.id], person="Bo"))
    assert [t.text("SUMMARY") for t in mine.walk("VTODO")] == ["Weekly sync"]

    events = ical.parse_calendar(cs.export_calendar(service, [board.id], kind="VEVENT", include_completed=True))
    ev = {e.text("SUMMARY"): e for e in events.walk("VEVENT")}
    assert set(ev) == {"Ship it, finally; really", "Weekly sync", "✓ Finished"}
    ship = ev["Ship it, finally; really"]
    assert ship.get("DTSTART").params == {"VALUE": "DATE"} and ship.text("DTSTART") == "20310106"
    assert ship.text("DTEND") == "20310111", "all-day through the due date (DTEND is exclusive)"
    assert ship.text("TRANSP") == "TRANSPARENT"
    timed = ical.parse_calendar(cs.export_calendar(service, [board.id], kind="VEVENT", event_style="timed"))
    ship = next(e for e in timed.walk("VEVENT") if e.text("SUMMARY").startswith("Ship"))
    assert ship.text("DTSTART") == ical.format_utc(due)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
GOOGLE_LIKE = vcal(
    "BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\nBEGIN:STANDARD\r\nDTSTART:19701025T030000\r\nTZOFFSETFROM:+0200\r\n"
    "TZOFFSETTO:+0100\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n",
    "BEGIN:VEVENT\r\nUID:standup@example.com\r\nSUMMARY:Stand-up\r\nDTSTART;TZID=Europe/Berlin:20301201T093000\r\n"
    "DTEND;TZID=Europe/Berlin:20301201T094500\r\nRRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR\r\nEXDATE;TZID=Europe/Berlin:20310108T093000\r\n"
    "END:VEVENT\r\n",
    "BEGIN:VEVENT\r\nUID:conf@example.com\r\nSUMMARY:Conference\\, Berlin\r\nDTSTART;VALUE=DATE:20310120\r\n"
    "DTEND;VALUE=DATE:20310123\r\nCATEGORIES:Travel,Events\r\nDESCRIPTION:Line 1\\nLine 2\r\nEND:VEVENT\r\n",
    "BEGIN:VEVENT\r\nUID:old@example.com\r\nSUMMARY:Long ago\r\nDTSTART:20200101T100000Z\r\nEND:VEVENT\r\n",
    "BEGIN:VEVENT\r\nUID:cancel@example.com\r\nSUMMARY:Cancelled\r\nSTATUS:CANCELLED\r\nDTSTART:20310110T100000Z\r\n"
    "END:VEVENT\r\n",
    vtodo("todo-1@x", "Buy milk", DUE="20310109", PRIORITY="1"),
    vtodo("todo-2@x", "Done already", STATUS="COMPLETED", COMPLETED="20310101T100000Z"),
    extra="X-WR-CALNAME:Work\r\n",
)


def test_import_calendar_maps_events_and_todos_and_updates_on_reimport(env):
    service, _ws, board, cols = env
    summary = cs.import_calendar(service, GOOGLE_LIKE, board.id, column_id=cols["To Do"].id, now=NOW,
                                 days_past=7, days_future=60)
    assert summary.name == "Work" and summary.describe() == "3 created, 3 skipped"
    tasks = {t.title: t for t in service.get_tasks_for_board(board.id)}
    assert set(tasks) == {"Stand-up", "Conference, Berlin", "Buy milk"}
    standup = tasks["Stand-up"]
    berlin_next = datetime(2031, 1, 10, 9, 30, tzinfo=ical.zone("Europe/Berlin"))  # Wed the 8th is excluded
    assert standup.due_date == berlin_next.astimezone().replace(tzinfo=None)
    assert standup.is_recurring and standup.recurrence_rule["frequency"] == "WEEKLY"
    conf = tasks["Conference, Berlin"]
    assert conf.start_date == date(2031, 1, 20) and conf.due_date == datetime(2031, 1, 22, 17, 0)
    assert sorted(l.name for l in conf.labels) == ["Events", "Travel"] and conf.description == "Line 1\nLine 2"
    milk = tasks["Buy milk"]
    assert milk.priority == "URGENT" and milk.due_date == datetime(2031, 1, 9, 17, 0)
    assert milk.column_id == cols["To Do"].id
    assert history(service, milk.id)[0][1].endswith("(imported from a calendar)")

    changed = GOOGLE_LIKE.replace("SUMMARY:Buy milk", "SUMMARY:Buy oat milk")
    again = cs.import_calendar(service, changed, board.id, now=NOW, days_past=7, days_future=60)
    assert (again.created, again.updated, again.unchanged) == (0, 1, 2)
    assert service.get_task(milk.id).title == "Buy oat milk"
    with_done = cs.import_calendar(service, GOOGLE_LIKE, board.id, now=NOW, include_completed=True)
    done = next(t for t in service.get_tasks_for_board(board.id) if t.title == "Done already")
    assert with_done.task_ids == [done.id] and done.is_completed and done.column_id == cols["Done"].id
    with pytest.raises(ICalError):
        cs.import_calendar(service, "not a calendar", board.id)


def test_export_then_import_round_trips_tasks(env):
    service, ws, board, cols = env
    service.create_task(board.id, cols["To Do"].id, "Round trip", due_date=datetime(2031, 2, 3, 11, 15),
                        start_date=date(2031, 2, 1), priority="HIGH",
                        label_ids=[l.id for l in service.get_labels_for_board(board.id) if l.name in ("Bug", "Design")],
                        is_recurring=True, recurrence_rule={"frequency": "WEEKDAYS", "interval": 1})
    text = cs.export_calendar(service, [board.id], details=False)
    other = service.create_board(ws.id, "Copy")
    cs.import_calendar(service, text, other.id, now=NOW, days_future=999)
    t = service.get_tasks_for_board(other.id)[0]
    assert (t.title, t.due_date, t.start_date, t.priority) == ("Round trip", datetime(2031, 2, 3, 11, 15),
                                                               date(2031, 2, 1), "HIGH")
    assert sorted(l.name for l in t.labels) == ["Bug", "Design"]
    assert t.recurrence_rule == {"frequency": "WEEKDAYS", "interval": 1}


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------
def test_subscription_settings_are_validated(env):
    service, _ws, board, _cols = env
    with pytest.raises(ValueError, match="read-only"):
        service.save_calendar_subscription({"name": "G", "url": "https://x/y.ics", "board_id": board.id,
                                            "direction": "two_way"})
    with pytest.raises(ValueError, match="name"):
        service.save_calendar_subscription({"name": " ", "url": "https://x", "board_id": board.id})
    with pytest.raises(ValueError, match="board"):
        service.save_calendar_subscription({"name": "G", "url": "https://x", "board_id": 999})
    sub = service.save_calendar_subscription({"name": "G", "url": "https://x/y.ics", "board_id": board.id,
                                              "password": "pw", "interval_minutes": 99999})
    assert sub["has_password"] and "password" not in sub and sub["interval_minutes"] == 1440
    upd = service.save_calendar_subscription({"name": "G2", "password": None}, sub_id=sub["id"])
    assert upd["name"] == "G2" and upd["has_password"], "None keeps the stored password"
    assert service.get_calendar_subscription(sub["id"], include_secret=True)["password"] == "pw"
    engine = cs.CalendarSync(service)
    assert [s["id"] for s in engine.due_subscriptions()] == [sub["id"]], "never synced yet"
    service.set_subscription_status(sub["id"], "success", "ok")
    assert engine.due_subscriptions() == []
    service.save_calendar_subscription({"interval_minutes": 0}, sub_id=sub["id"])
    assert engine.due_subscriptions() == []


def test_feed_subscription_pull_updates_removals_and_local_deletes(env, tmp_path):
    service, _ws, board, cols = env
    with Server() as srv:
        srv.state.feed = vcal(vtodo("a@x", "Alpha", DUE="20310110T100000Z"), vtodo("b@x", "Beta"),
                              vtodo("c@x", "Gamma"))
        sub = service.save_calendar_subscription({"name": "Team feed", "url": srv.base.replace("http", "webcal", 1)
                                                  .replace("webcal", "http") + "/feed.ics", "board_id": board.id,
                                                  "column_id": cols["To Do"].id})
        engine = cs.CalendarSync(service, now=lambda: NOW)
        report = engine.sync(sub["id"])
        assert report.status == "success" and report.counts["created"] == 3, report.messages
        tasks = {t.title: t for t in service.get_tasks_for_board(board.id)}
        assert tasks["Alpha"].column_id == cols["To Do"].id
        assert history(service, tasks["Alpha"].id)[0] == (
            "🔄 Calendar sync: Team feed", "Created task 'Alpha' in column 'To Do' (from the calendar 'Team feed')")
        assert engine.sync(sub["id"]).describe() == "no changes"

        # local edit kept while the feed doesn't change that item; feed changes win when it does
        service.update_task(tasks["Beta"].id, title="Beta (mine)")
        service.delete_task(tasks["Gamma"].id)
        srv.state.feed = vcal(vtodo("a@x", "Alpha v2", DUE="20310110T100000Z"), vtodo("b@x", "Beta"),
                              vtodo("c@x", "Gamma v2"), vtodo("d@x", "Delta"))
        report = engine.sync(sub["id"])
        assert report.counts == {"created": 1, "updated": 1, "removed": 0, "pushed": 0, "conflicts": 0}
        titles = sorted(t.title for t in service.get_tasks_for_board(board.id))
        assert titles == ["Alpha v2", "Beta (mine)", "Delta"], "deleted Gamma isn't brought back, even if changed"

        srv.state.feed = vcal(vtodo("b@x", "Beta"), vtodo("d@x", "Delta"))
        report = engine.sync(sub["id"])
        assert report.counts["removed"] == 1
        archived = [t["title"] for t in service.get_archived_tasks(board.id)]
        assert archived == ["Alpha v2"]
        runs = service.get_sync_runs(sub["id"])
        assert len(runs) == 4 and runs[0]["removed"] == 1

        # errors: wrong credentials, unreachable, bad data -> failed run, nothing changed
        srv.state.feed_auth = True
        failed = engine.sync(sub["id"])
        assert failed.status == "failed" and "401" in failed.describe()
        assert service.get_calendar_subscription(sub["id"])["last_status"] == "failed"
        srv.state.feed_auth = False
        srv.state.feed = "garbage"
        assert engine.sync(sub["id"]).status == "failed"
    ok, message, _ = cs.check_connection("ics", "http://127.0.0.1:9/nothing.ics")
    assert not ok and "Could not reach" in message

    # a local file works as a feed too
    path = tmp_path / "cal.ics"
    path.write_text(vcal(vtodo("f@x", "From a file")), encoding="utf-8")
    fsub = service.save_calendar_subscription({"name": "File", "url": str(path), "board_id": board.id})
    assert cs.CalendarSync(service).sync(fsub["id"]).counts["created"] == 1
    assert cs.normalize_url("webcal://example.com/a.ics") == "https://example.com/a.ics"


# ---------------------------------------------------------------------------
# CalDAV
# ---------------------------------------------------------------------------
def caldav_sub(service, srv, board, direction="two_way", **options):
    return service.save_calendar_subscription({
        "name": "Nextcloud", "kind": "caldav", "url": srv.base + TASKS, "username": USER, "password": PASSWORD,
        "board_id": board.id, "direction": direction, "options": options})


def remote_todo(srv, uid, summary, **props):
    return srv.state.put(f"{TASKS}{uid}.ics", vcal(vtodo(uid, summary, **props)))


def test_caldav_discovery_and_connection_check(env):
    with Server() as srv:
        ok, message, found = cs.check_connection("caldav", srv.base + "/", USER, PASSWORD)
        assert ok and message == "Found 2 calendars - pick one"
        assert [(c["name"], c["components"]) for c in found] == [("Tasks", ["VTODO"]), ("Events", ["VEVENT"])]
        assert found[0]["url"] == srv.base + TASKS
        ok, message, found = cs.check_connection("caldav", srv.base + TASKS, USER, PASSWORD)
        assert ok and len(found) == 1 and "Tasks" in message
        ok, message, _ = cs.check_connection("caldav", srv.base + TASKS, USER, "wrong")
        assert not ok and "401" in message


def test_caldav_two_way_sync(env):
    service, _ws, board, cols = env
    with Server() as srv:
        local = service.create_task(board.id, cols["To Do"].id, "Local task", due_date=datetime(2031, 1, 15, 12),
                                    priority="HIGH")
        service.create_task(board.id, cols["Done"].id, "Old and done")
        remote_todo(srv, "r1@server", "Remote task", DUE="20310120T090000Z", CATEGORIES="Ops",
                    ATTENDEE="mailto:bob@example.com", X_SERVER_PROP="keep me")
        sub = caldav_sub(service, srv, board)
        engine = cs.CalendarSync(service, now=lambda: NOW)

        first = engine.sync(sub["id"])
        assert first.status == "success", first.messages
        assert first.counts["created"] == 1 and first.counts["pushed"] == 1
        remote_task = next(t for t in service.get_tasks_for_board(board.id) if t.title == "Remote task")
        assert [l.name for l in remote_task.labels] == ["Ops"]
        pushed = [h for h in srv.state.texts() if "kanban-task" in h]
        assert len(pushed) == 1, "done tasks aren't pushed"
        text = srv.state.items[pushed[0]][1]
        todo = ical.parse_calendar(text).first("VTODO")
        assert todo.text("SUMMARY") == "Local task" and todo.text("PRIORITY") == "3"
        assert todo.text("DUE") == ical.format_utc(datetime(2031, 1, 15, 12))
        put = next(r for r in srv.state.requests if r[0] == "PUT")
        assert put[2].get("If-None-Match") == "*", "creating never overwrites"
        assert engine.sync(sub["id"]).describe() == "no changes", "stable: nothing ping-pongs"

        # local change -> pushed (If-Match), unknown remote properties kept
        service.update_task(remote_task.id, title="Remote task (edited here)", priority="LOW")
        srv.state.requests.clear()
        report = engine.sync(sub["id"])
        assert report.counts["pushed"] == 1
        put = next(r for r in srv.state.requests if r[0] == "PUT")
        assert put[2].get("If-Match", "").startswith('"')
        todo = ical.parse_calendar(srv.state.items[f"{TASKS}r1@server.ics"][1]).first("VTODO")
        assert todo.text("SUMMARY") == "Remote task (edited here)" and todo.text("PRIORITY") == "9"
        assert todo.text("ATTENDEE") == "mailto:bob@example.com" and todo.text("X-SERVER-PROP") == "keep me"
        assert todo.text("SEQUENCE") == "0"

        # remote change -> pulled (incl. completion -> Done column)
        href = f"{TASKS}r1@server.ics"
        srv.state.put(href, srv.state.items[href][1].replace("STATUS:NEEDS-ACTION", "STATUS:COMPLETED\r\n"
                                                             "COMPLETED:20310109T100000Z"))
        report = engine.sync(sub["id"])
        assert report.counts["updated"] == 1
        t = service.get_task(remote_task.id)
        assert t.is_completed and t.column_id == cols["Done"].id
        moves = [(a, d) for a, d in history(service, t.id) if d.startswith("Moved")]
        assert moves[-1] == ("🔄 Calendar sync: Nextcloud", "Moved 'Remote task (edited here)' from 'To Do' to 'Done'")

        # both changed -> conflict; the newer side wins (the server's LAST-MODIFIED is in the future)
        service.update_task(local.id, title="Local (here)")
        lhref = next(h for h in srv.state.texts() if "kanban-task" in h)
        srv.state.put(lhref, srv.state.items[lhref][1].replace("SUMMARY:Local task", "SUMMARY:Local (server)")
                      .replace("LAST-MODIFIED:", "LAST-MODIFIED:29990101T000000Z\r\nX-OLD-LM:"))
        report = engine.sync(sub["id"])
        assert report.counts["conflicts"] == 1
        assert service.get_task(local.id).title == "Local (server)"
        assert any("Sync conflict with 'Nextcloud'" in d for _a, d in history(service, local.id))

        # deleted here -> deleted there; deleted there -> archived here
        service.delete_task(local.id)
        srv.state.requests.clear()
        engine.sync(sub["id"])
        assert any(r[0] == "DELETE" for r in srv.state.requests) and lhref not in srv.state.items
        del srv.state.items[href]
        report = engine.sync(sub["id"])
        assert report.counts["removed"] == 1
        assert "Remote task (edited here)" in [a["title"] for a in service.get_archived_tasks(board.id)]
        assert service.get_external_links(cs.CalendarSync.source(sub)) == []


def test_caldav_push_only_pull_only_and_server_conflicts(env):
    service, ws, board, cols = env
    with Server() as srv:
        remote_todo(srv, "r9@server", "Server only")
        service.create_task(board.id, cols["To Do"].id, "Mine")
        push = caldav_sub(service, srv, board, direction="push")
        engine = cs.CalendarSync(service, now=lambda: NOW)
        report = engine.sync(push["id"])
        assert report.counts["created"] == 0 and report.counts["pushed"] == 1, "push-only doesn't import"
        mine_href = next(h for h in srv.state.texts() if "kanban-task" in h)
        del srv.state.items[mine_href]
        assert engine.sync(push["id"]).counts["pushed"] == 1 and mine_href in srv.state.items, "push-only restores"

        other = service.create_board(ws.id, "Pull")
        pull = caldav_sub(service, srv, other, direction="pull")
        report = engine.sync(pull["id"])
        assert report.counts["created"] == 2 and report.counts["pushed"] == 0
        pulled = {t.title: t for t in service.get_tasks_for_board(other.id)}
        service.update_task(pulled["Server only"].id, title="Renamed locally")
        service.create_task(other.id, other.columns[0].id, "Never leaves")
        srv.state.requests.clear()
        assert engine.sync(pull["id"]).counts["pushed"] == 0
        assert not any(r[0] in ("PUT", "DELETE") for r in srv.state.requests), "pull-only never writes"

        # the server changes an item between our listing and our write -> 412, retried next time
        t = next(t for t in service.get_tasks_for_board(board.id) if t.title == "Mine")
        service.update_task(t.id, title="Mine v2")
        real = engine.transport_factory

        def racing(username, password):
            http = real(username, password)

            def request(method, url, headers, body):
                if method == "PUT":
                    srv.state.put(url.replace(srv.base, ""), srv.state.items[url.replace(srv.base, "")][1])
                return http(method, url, headers, body)
            return request

        engine.transport_factory = racing
        report = engine.sync(push["id"])
        assert report.status == "partial" and "changed on the server during the sync" in report.messages[0]
        engine.transport_factory = real
        assert engine.sync(push["id"]).counts["pushed"] == 1
        assert "SUMMARY:Mine v2" in srv.state.items[mine_href][1]


def test_caldav_events_mode_round_trips_without_churn(env):
    service, _ws, board, cols = env
    with Server() as srv:
        service.create_task(board.id, cols["To Do"].id, "Demo day", due_date=datetime(2031, 1, 17, 15, 0),
                            start_date=date(2031, 1, 13))
        done = service.create_task(board.id, cols["To Do"].id, "Retro", due_date=datetime(2031, 1, 10, 11, 0))
        service.create_task(board.id, cols["To Do"].id, "No date")
        sub = service.save_calendar_subscription({
            "name": "Events", "kind": "caldav", "url": srv.base + EVENTS, "username": USER, "password": PASSWORD,
            "board_id": board.id, "direction": "two_way", "options": {"component": "VEVENT"}})
        engine = cs.CalendarSync(service, now=lambda: NOW)
        first = engine.sync(sub["id"])
        assert first.counts["pushed"] == 2, "undated tasks can't be events"
        assert engine.sync(sub["id"]).describe() == "no changes"
        service.toggle_task_completion(done.id)
        assert engine.sync(sub["id"]).counts["pushed"] == 1
        retro = next(t for h, t in srv.state.texts(EVENTS).items() if "Retro" in t)
        assert "SUMMARY:✓ Retro" in retro
        assert engine.sync(sub["id"]).describe() == "no changes"
        assert service.get_task(done.id).is_completed


def test_demo_board_exports(tmp_path):
    mgr = seeded_manager(tmp_path / "demo.db")
    service = DatabaseService(mgr)
    demo = next(b for ws in service.get_workspaces() for b in ws.boards if b.name == DEMO_BOARD_NAME)
    cal = ical.parse_calendar(cs.export_calendar(service, [demo.id]))
    titles = [t.text("SUMMARY") for t in cal.walk("VTODO")]
    assert "Weekly team sync" in titles and "🚀 Public beta" in titles
    sync = next(t for t in cal.walk("VTODO") if t.text("SUMMARY") == "Weekly team sync")
    assert sync.text("RRULE") == "FREQ=WEEKLY"
    json.dumps(titles)  # plain strings


def test_exports_parse_with_a_third_party_library(tmp_path):
    """Interoperability: our output is valid for an independent iCalendar implementation."""
    icalendar = pytest.importorskip("icalendar")
    mgr = seeded_manager(tmp_path / "i.db")
    service = DatabaseService(mgr)
    for kind, style in (("VTODO", "all_day"), ("VEVENT", "all_day"), ("VEVENT", "timed")):
        text = cs.export_calendar(service, kind=kind, event_style=style, alarm_minutes=15, include_completed=True,
                                  include_undated=True)
        cal = icalendar.Calendar.from_ical(text)
        comps = [c for c in cal.walk() if c.name == kind]
        assert comps and all(c.errors == [] for c in comps), (kind, style)
        assert all(str(c.get("uid")).endswith("@kanbanmanager") for c in comps)

"""
Demo Board: a guided tour of rich content, attachments, comments and history

``build_demo_board`` creates a "Feature Tour" board whose cards showcase:
  * Markdown descriptions (headings, lists, task lists, tables, code, quotes,
    links) with inline images referencing attachments (``attachment:<id>``)
  * image and file attachments
  * comment threads from several teammates (plus one by *you*, so editing and
    deleting your own comments can be tried)
  * a realistic, back-dated audit trail consistent with each card's state

The story is scripted, so rows are written directly with explicit actors and
timestamps rather than through DatabaseService (which always stamps "now").
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from kanbanmanager.config import DEFAULT_COLUMNS, DEFAULT_LABEL_PALETTE, get_current_user
from kanbanmanager.database import demo_images
from kanbanmanager.database.attachments import AttachmentStore, guess_content_type
from kanbanmanager.database.models import (
    ActivityLog,
    Attachment,
    AutomationRule,
    BaselineEntry,
    Board,
    BoardColumn,
    Comment,
    CustomField,
    Label,
    ProjectBaseline,
    Task,
    TaskDependency,
    TaskFieldValue,
    TaskTemplate,
    TeamMember,
    TimeEntry,
    utc_now,
)
from kanbanmanager.enums import ActivityAction as A, Priority, RecurrenceFrequency, TimeEntrySource
from kanbanmanager.timefmt import format_duration

DEMO_BOARD_NAME = "Feature Tour"
DEMO_BOARD_DESCRIPTION = (
    "Demo board: Markdown descriptions, file & image attachments, comments and a full audit trail. "
    "Open any card to explore."
)

PRIYA = "Priya Sharma"
MARCUS = "Marcus Chen"
ELENA = "Elena Rossi"

BACKLOG, TODO, IN_PROGRESS, IN_REVIEW, DONE = range(5)

# Weekly capacity and working days (0 = Monday) for the workload view
DEMO_TEAM = {
    PRIYA: (32.0, [0, 1, 2, 3]),
    MARCUS: (40.0, [0, 1, 2, 3, 4]),
    ELENA: (16.0, [0, 1]),  # part-time
}


def _working_day(offset: int, hour: int = 17, minute: int = 0) -> datetime:
    """Local date-time ``offset`` working days (Mon-Fri) from today; 0 = today, or the next
    working day when today is a weekend."""
    d = datetime.now().date()
    while d.weekday() >= 5:
        d += timedelta(days=1)
    step = 1 if offset >= 0 else -1
    remaining = abs(offset)
    while remaining:
        d += timedelta(days=step)
        if d.weekday() < 5:
            remaining -= 1
    return datetime.combine(d, time(hour, minute))


class _DemoBuilder:
    def __init__(self, session: Session, store: AttachmentStore, you: str):
        self.s = session
        self.store = store
        self.you = you
        self.now = utc_now()
        self.board: Optional[Board] = None
        self.cols: List[BoardColumn] = []
        self.labels: Dict[str, Label] = {}
        self._positions: Dict[int, int] = {}
        self._booked: Dict[str, List[tuple]] = {}  # person -> [(start, end)] of logged work (UTC)

    # ------------------------------------------------------------------
    def ago(self, days: float = 0, hours: float = 0) -> datetime:
        return self.now - timedelta(days=days, hours=hours)

    def create_board(self, workspace_id: int, position: int) -> Board:
        self.board = Board(
            workspace_id=workspace_id,
            name=DEMO_BOARD_NAME,
            description=DEMO_BOARD_DESCRIPTION,
            color="#8B5CF6",
            icon="sparkles",
            position=position,
        )
        self.s.add(self.board)
        self.s.flush()
        for i, c in enumerate(DEFAULT_COLUMNS):
            col = BoardColumn(
                board_id=self.board.id,
                name=c["name"],
                position=i,
                wip_limit=c.get("wip_limit", 0),
                color=c.get("color", "#64748B"),
                column_type=c.get("type", "CUSTOM"),
                is_done_column=c.get("is_done_column", False),
            )
            self.s.add(col)
            self.cols.append(col)
        for name, color in DEFAULT_LABEL_PALETTE:
            lbl = Label(board_id=self.board.id, name=name, color=color)
            self.s.add(lbl)
            self.labels[name] = lbl
        self.s.flush()
        return self.board

    def task(self, col: int, title: str, created: datetime, creator: str, created_in: Optional[int] = None,
             **fields) -> Task:
        """Create a task in its *current* column; the history records it as created in ``created_in``."""
        column = self.cols[col]
        origin = self.cols[created_in if created_in is not None else col]
        pos = self._positions.get(col, 0)
        self._positions[col] = pos + 1
        labels = [self.labels[n] for n in fields.pop("labels", [])]
        is_done = column.is_done_column
        t = Task(
            board_id=self.board.id,
            column_id=column.id,
            title=title,
            position=pos,
            is_completed=is_done,
            labels=labels,
            created_at=created,
            updated_at=created,
            **fields,
        )
        self.s.add(t)
        self.s.flush()
        self.log(t, A.TASK_CREATED, creator, created, f"Created task '{title}' in column '{origin.name}'",
                 "column", None, origin.name)
        return t

    def log(self, task: Task, action: A, actor: str, when: datetime, details: str,
            field: Optional[str] = None, old: Optional[str] = None, new: Optional[str] = None) -> None:
        self.s.add(ActivityLog(board_id=self.board.id, task_id=task.id, action_type=action.value, details=details,
                               actor=actor, timestamp=when, field_name=field, old_value=old, new_value=new))

    def move(self, task: Task, actor: str, when: datetime, src: int, dst: int) -> None:
        a, b = self.cols[src].name, self.cols[dst].name
        self.log(task, A.TASK_MOVED, actor, when, f"Moved '{task.title}' from '{a}' to '{b}'", "column", a, b)

    def comment(self, task: Task, author: str, when: datetime, body: str, edited: Optional[datetime] = None) -> None:
        self.s.add(Comment(task_id=task.id, author=author, body=body, created_at=when, updated_at=edited))
        flat = " ".join(body.split())
        self.log(task, A.COMMENT_ADDED, author, when, f"Commented: {flat[:79] + '…' if len(flat) > 80 else flat}",
                 "comment", None, body)

    def track(self, task: Task, user: str, days_ago: int, local_hour: float, minutes: int,
              source: TimeEntrySource = TimeEntrySource.STOPWATCH, note: str = "") -> None:
        """Log work in a (local) working-hours slot ``days_ago`` *working* days back."""
        day = datetime.now().date()
        remaining = days_ago
        while remaining > 0 or day.weekday() >= 5:
            day -= timedelta(days=1)
            if day.weekday() < 5:
                remaining -= 1
        slot = time(int(local_hour), int((local_hour % 1) * 60))

        def to_utc(d):  # stored as naive UTC
            return datetime.combine(d, slot).astimezone(timezone.utc).replace(tzinfo=None)

        start = to_utc(day)
        while start < task.created_at:  # work can't predate the task; move to the next working day
            day += timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
            start = to_utc(day)
        seconds = minutes * 60
        # Nobody works on two things at once: move to the first free slot after any overlap
        booked = self._booked.setdefault(user, [])
        moved = True
        while moved:
            moved = False
            for b_start, b_end in booked:
                if start < b_end and b_start < start + timedelta(seconds=seconds):
                    start = b_end + timedelta(minutes=10)
                    moved = True
        if start + timedelta(seconds=seconds) >= self.now:  # never in the future
            start = min(b for b, _ in booked) - timedelta(seconds=seconds + 600) if booked else \
                self.now - timedelta(seconds=seconds + 300)
        booked.append((start, start + timedelta(seconds=seconds)))
        self.s.add(TimeEntry(task_id=task.id, user=user, started_at=start, ended_at=start + timedelta(seconds=seconds),
                             duration_seconds=seconds, source=source.value, note=note or None,
                             planned_seconds=seconds if source == TimeEntrySource.POMODORO else None))
        self.log(task, A.TIME_LOGGED, user, start + timedelta(seconds=seconds),
                 f"Logged {format_duration(seconds)} ({source.display_name.lower()})", "time", None, str(seconds))

    def finish_time(self) -> None:
        """Set each task's spent hours from its time entries."""
        self.s.flush()
        for task in self.s.query(Task).filter(Task.board_id == self.board.id):
            total = sum(e.duration_seconds for e in self.s.query(TimeEntry).filter(TimeEntry.task_id == task.id))
            task.spent_hours = total / 3600.0

    def field(self, name: str, field_type: str, config: Optional[dict] = None, **kw) -> CustomField:
        from kanbanmanager.core.custom_fields import get_field_type

        f = CustomField(board_id=self.board.id, name=name, field_type=field_type,
                        position=self.s.query(CustomField).filter(CustomField.board_id == self.board.id).count(), **kw)
        f.config = get_field_type(field_type).normalize_config(config)
        self.s.add(f)
        self.s.flush()
        return f

    def set_field(self, task: Task, f: CustomField, value) -> None:
        from kanbanmanager.core.custom_fields import validate_value

        self.s.add(TaskFieldValue(task_id=task.id, field_id=f.id,
                                  value_json=json.dumps(validate_value(f.to_dict(), value), ensure_ascii=False)))

    def attach(self, task: Task, filename: str, data: bytes, author: str, when: datetime, log: bool = True) -> Attachment:
        blob = self.store.store_bytes(data, filename)
        att = Attachment(task_id=task.id, filename=filename, stored_name=blob.stored_name,
                         content_type=guess_content_type(filename), size_bytes=blob.size_bytes,
                         checksum=blob.checksum, uploaded_by=author, created_at=when)
        self.s.add(att)
        self.s.flush()
        if log:
            self.log(task, A.ATTACHMENT_ADDED, author, when, f"Attached '{filename}'", "attachment", None, filename)
        return att


# ---------------------------------------------------------------------------
# Sample file contents
# ---------------------------------------------------------------------------
_CRASH_LOG = """2024-09-12 10:41:07,113 INFO  importer: Starting CSV import (sample_import.csv, 61,204 rows)
2024-09-12 10:41:07,118 DEBUG importer: Detected delimiter ',' and header row
2024-09-12 10:41:15,902 WARN  ui: Event loop blocked for 8784 ms
2024-09-12 10:41:19,447 ERROR importer: Import failed
Traceback (most recent call last):
  File "importer.py", line 88, in import_rows
    rows = list(reader)
MemoryError
2024-09-12 10:41:19,451 CRITICAL app: Unhandled exception, shutting down
"""


def _sample_csv(rows: int = 400) -> bytes:
    lines = ["id,title,priority,column,estimate_hours"]
    pri = ["LOW", "MEDIUM", "HIGH", "URGENT"]
    cols = ["Backlog", "To Do", "In Progress", "Done"]
    for i in range(1, rows + 1):
        lines.append(f"{i},Imported task {i},{pri[i % 4]},{cols[i % 4]},{(i % 8) + 0.5}")
    return ("\n".join(lines) + "\n").encode()


_Q3_CSV = b"""metric,q2,q3,change
tasks_completed,1240,1530,+23%
avg_cycle_time_days,4.2,3.1,-26%
overdue_rate,9%,6%,-3pts
weekly_active_boards,812,958,+18%
"""

_WEBHOOK_JSON = json.dumps(
    {
        "event": "task.moved",
        "delivered_at": "2024-09-14T09:30:00Z",
        "board": {"id": 42, "name": "Product Roadmap"},
        "task": {"id": 1337, "title": "Ship webhooks", "from_column": "In Review", "to_column": "Done"},
        "actor": {"name": "Marcus Chen"},
    },
    indent=2,
).encode()

_RESEARCH_NOTES = b"""# Onboarding research notes

Interviewed 8 new users (4 teams, 4 individuals).

* 6/8 were unsure what to do after signing up
* 5/8 wanted a template instead of an empty board
* Most abandoned sessions happened on the empty-board screen
"""

_MEETING_TEMPLATE = b"""# Team sync - {date}

## Wins
-

## Blockers
-

## Action items
- [ ] owner - task
"""


# ---------------------------------------------------------------------------
# Story
# ---------------------------------------------------------------------------
def build_demo_board(
    session: Session,
    workspace_id: int,
    store: AttachmentStore,
    you: Optional[str] = None,
    position: Optional[int] = None,
) -> Board:
    """Create the demo board in ``workspace_id`` and return it (session is flushed, not committed)."""
    you = you or get_current_user()
    if position is None:
        position = session.query(Board).filter(Board.workspace_id == workspace_id).count()

    b = _DemoBuilder(session, store, you)
    b.create_board(workspace_id, position)
    for name, (capacity, days) in DEMO_TEAM.items():  # keep any capacity the user already set
        if session.query(TeamMember).filter(TeamMember.name == name).first() is None:
            member = TeamMember(name=name, weekly_capacity_hours=capacity)
            member.working_days = days
            session.add(member)

    # -- 1. Welcome --------------------------------------------------------
    welcome = b.task(TODO, "👋 Start here: take the feature tour", b.ago(days=7), PRIYA,
                     priority=Priority.HIGH.value, labels=["Documentation"], estimated_hours=0.5, assignee=PRIYA)
    banner = b.attach(welcome, "welcome_banner.png", demo_images.welcome_banner(), PRIYA, b.ago(days=7), log=True)
    welcome.description = f"""![Feature tour banner](attachment:{banner.id})

# Welcome to the Feature Tour

This board is a **hands-on demo** of rich task content. Every card has a little story - open them to explore.

## Things to try

- [x] Open this card (you just did!)
- [ ] Switch the description editor between **Visual** and **Markdown** and watch the formatting carry over
- [ ] Open the **Attachments** tab - images get a preview, other files open in their default app
- [ ] Read the thread in the **Activity** tab, then post a comment of your own
- [ ] Set the activity filter to **History** to see the complete audit trail
- [ ] Open the **Fields** tab: story points, component and impact are *custom fields* - add your own
  from **🧩 Fields** in the bar above the board
- [ ] Create a card from a template: a column's **⋮** menu → **New from template**

## Formatting cheat sheet

| You type | You get |
|---|---|
| `**bold**` | **bold** |
| `*italic*` | *italic* |
| `~~strike~~` | ~~strike~~ |
| `` `code` `` | `code` |
| `- [ ] item` | a checkbox |

> **Tip:** paste a screenshot straight into the description editor. It is saved as an attachment and shown inline.

New to Markdown? The [CommonMark tutorial](https://commonmark.org/help/) takes ten minutes.
"""
    b.log(welcome, A.TASK_UPDATED, PRIYA, b.ago(days=7) + timedelta(minutes=4), "Added a description",
          "description", "", welcome.description)
    b.comment(welcome, PRIYA, b.ago(days=6, hours=22),
              "Welcome aboard! 🎉 Every card on this board demonstrates something different. "
              "Start with **Design the new onboarding flow** - it has the richest history.")
    b.comment(welcome, MARCUS, b.ago(days=6, hours=20),
              "Pro tip: comments support Markdown too - `code`, **bold**, lists and [links](https://commonmark.org).")

    # -- 2. Onboarding flow (richest history) ------------------------------
    onboarding = b.task(IN_REVIEW, "Design the new onboarding flow", b.ago(days=12), PRIYA, created_in=BACKLOG,
                        priority=Priority.HIGH.value, estimated_hours=12.0, assignee=ELENA)
    onboarding.labels = [b.labels["Design"], b.labels["Feature"]]
    onboarding.due_date = _working_day(3)
    onboarding.start_date = _working_day(-8).date()
    onboarding.checklist = [
        {"id": 1, "text": "Synthesise user research", "done": True},
        {"id": 2, "text": "Low-fi wireframes", "done": True},
        {"id": 3, "text": "Hi-fi mockups", "done": True},
        {"id": 4, "text": "Usability test with 5 users", "done": False},
    ]
    b.log(onboarding, A.TASK_UPDATED, PRIYA, b.ago(days=12) + timedelta(minutes=2), "Added labels Design, Feature",
          "labels", "", "Design, Feature")
    b.attach(onboarding, "user_research_notes.md", _RESEARCH_NOTES, PRIYA, b.ago(days=12, hours=-1))
    b.move(onboarding, PRIYA, b.ago(days=11, hours=3), BACKLOG, TODO)
    b.log(onboarding, A.TASK_UPDATED, PRIYA, b.ago(days=11, hours=3) + timedelta(minutes=1),
          f"Set due date to {onboarding.due_date.strftime('%b %d, %Y %H:%M')}", "due_date", None,
          onboarding.due_date.strftime("%b %d, %Y %H:%M"))
    b.move(onboarding, ELENA, b.ago(days=10), TODO, IN_PROGRESS)
    # v1 was later replaced by v2, so it only survives in the history
    b.log(onboarding, A.ATTACHMENT_ADDED, ELENA, b.ago(days=8, hours=5), "Attached 'onboarding_wireframe_v1.png'",
          "attachment", None, "onboarding_wireframe_v1.png")
    palette = b.attach(onboarding, "color_palette.png", demo_images.color_palette(), ELENA, b.ago(days=8, hours=5))
    b.comment(onboarding, ELENA, b.ago(days=8, hours=4),
              "First pass of the wireframes is attached. I went with a single-column layout so the "
              "*Create your first board* button is impossible to miss.")
    b.comment(onboarding, MARCUS, b.ago(days=7, hours=2),
              "Looks great! One concern: the template picker loads every preview up-front. Can we "
              "**lazy-load** the previews so the screen renders in under 200 ms?")
    b.log(onboarding, A.TASK_UPDATED, PRIYA, b.ago(days=7), "Changed priority from Medium to High", "priority",
          "Medium", "High")
    wire_v2 = b.attach(onboarding, "onboarding_wireframe_v2.png", demo_images.onboarding_wireframe(), ELENA,
                       b.ago(days=5, hours=6))
    b.log(onboarding, A.ATTACHMENT_REMOVED, ELENA, b.ago(days=5, hours=6) + timedelta(minutes=1),
          "Removed attachment 'onboarding_wireframe_v1.png'", "attachment", "onboarding_wireframe_v1.png", None)
    b.comment(onboarding, ELENA, b.ago(days=5, hours=5),
              "Good call - **v2** uses skeleton placeholders for the previews and swaps real thumbnails in "
              "as they load. I replaced the old wireframe with v2.")
    onboarding.description = f"""## Goal

Cut time-to-first-board for new users from **~6 minutes to under 2**.

## Proposed flow

1. Welcome screen with a single *Create your first board* call to action
2. Template picker (Kanban, Sprint, Personal) with lazily loaded previews
3. Inline tour tooltips on the first board

![Onboarding wireframe v2](attachment:{wire_v2.id})

## Visual direction

Colours follow the existing design tokens - no new palette needed:

![Colour palette](attachment:{palette.id})

## Open questions

- Should the template picker be skippable?
- Do we localise the tour in v1? See the research notes attachment.
"""
    b.log(onboarding, A.TASK_UPDATED, ELENA, b.ago(days=5, hours=4), "Edited the description", "description",
          "## Goal\n\nFaster onboarding.", onboarding.description)
    b.log(onboarding, A.TASK_UPDATED, ELENA, b.ago(days=3), "Checklist: checked 'Hi-fi mockups'", "checklist",
          "2/4 done", "3/4 done")
    b.move(onboarding, PRIYA, b.ago(days=1, hours=3), IN_PROGRESS, IN_REVIEW)
    b.comment(onboarding, PRIYA, b.ago(days=1, hours=3),
              "Moving this to review. @Marcus could you sign off on the technical side by Friday?")

    # -- 3. Crash bug (in progress, urgent, blocks the CDN migration) ------
    crash = b.task(IN_PROGRESS, "Crash when importing large CSV files", b.ago(days=4), PRIYA, created_in=TODO,
                   priority=Priority.URGENT.value, labels=["Bug"], estimated_hours=5.0, assignee=MARCUS)
    crash.due_date = _working_day(1, 12)
    crash.start_date = _working_day(-2).date()
    shot = b.attach(crash, "crash_dialog.png", demo_images.crash_dialog(), PRIYA, b.ago(days=4) + timedelta(minutes=3))
    b.attach(crash, "crash_log.txt", _CRASH_LOG.encode(), PRIYA, b.ago(days=4) + timedelta(minutes=3))
    b.attach(crash, "sample_import.csv", _sample_csv(), MARCUS, b.ago(days=3, hours=2))
    crash.description = f"""## Summary

Importing a CSV with more than ~50k rows freezes the window and then crashes the app.

## Steps to reproduce

1. Open **Backups & Data** and choose *Import*
2. Pick `sample_import.csv` (attached)
3. Wait about 10 seconds

**Expected:** rows import with a progress bar.
**Actual:** the window stops responding, then the app exits.

![Crash dialog](attachment:{shot.id})

## Stack trace

```text
Traceback (most recent call last):
  File "importer.py", line 88, in import_rows
    rows = list(reader)
MemoryError
```

## Fix plan

- [x] Reproduce with the attached sample
- [ ] Stream rows instead of calling `list(reader)`
- [ ] Parse on a worker thread and report progress
- [ ] Add a regression test with 100k rows
"""
    b.log(crash, A.TASK_UPDATED, PRIYA, b.ago(days=4) + timedelta(minutes=5), "Added a description",
          "description", "", crash.description)
    b.comment(crash, MARCUS, b.ago(days=3, hours=2),
              "Reproduced. Memory climbs to ~2 GB before the crash because we materialise the whole file "
              "with `list(reader)`. Attached the file I used.")
    b.log(crash, A.TASK_UPDATED, PRIYA, b.ago(days=3), "Changed priority from High to Urgent", "priority",
          "High", "Urgent")
    b.comment(crash, PRIYA, b.ago(days=3),
              "This is blocking two enterprise trials - bumping to **Urgent**.")
    b.move(crash, MARCUS, b.ago(days=2, hours=6), TODO, IN_PROGRESS)
    b.log(crash, A.TASK_UPDATED, MARCUS, b.ago(days=2, hours=6) + timedelta(minutes=1),
          f"Set due date to {crash.due_date.strftime('%b %d, %Y %H:%M')}", "due_date", None,
          crash.due_date.strftime("%b %d, %Y %H:%M"))
    b.comment(crash, you, b.ago(hours=5),
              "I can pair on the worker-thread part tomorrow morning. *(This comment is yours - try editing or deleting it.)*")

    # -- 4. Q3 report (done) -----------------------------------------------
    report = b.task(DONE, "Publish the Q3 performance report", b.ago(days=16), PRIYA, created_in=TODO,
                    priority=Priority.MEDIUM.value, labels=["Documentation"], estimated_hours=4.0,
                    completed_at=b.ago(days=2), assignee=PRIYA)
    chart = b.attach(report, "q3_tasks_completed.png", demo_images.performance_chart(), PRIYA, b.ago(days=5))
    b.attach(report, "q3_metrics.csv", _Q3_CSV, PRIYA, b.ago(days=5))
    report.description = f"""## Highlights

- Weekly active boards up **18%** quarter over quarter
- Median task cycle time down from 4.2 to **3.1 days**
- Overdue rate at an all-time low

![Tasks completed per month, Apr-Sep](attachment:{chart.id})

| Metric | Q2 | Q3 | Change |
|---|---|---|---|
| Tasks completed | 1,240 | 1,530 | +23% |
| Avg. cycle time | 4.2 d | 3.1 d | -26% |
| Overdue rate | 9% | 6% | -3 pts |

The raw numbers are in `q3_metrics.csv`.
"""
    b.move(report, PRIYA, b.ago(days=9), TODO, IN_PROGRESS)
    b.log(report, A.TASK_UPDATED, PRIYA, b.ago(days=5), "Added a description", "description", "", report.description)
    b.move(report, PRIYA, b.ago(days=4), IN_PROGRESS, IN_REVIEW)
    b.comment(report, MARCUS, b.ago(days=3, hours=4), "Numbers check out against the analytics DB ✅")
    b.comment(report, ELENA, b.ago(days=3), "Chart colours match the brand palette - good to ship.")
    b.move(report, PRIYA, b.ago(days=2), IN_REVIEW, DONE)
    b.log(report, A.TASK_COMPLETED, PRIYA, b.ago(days=2), "Marked as complete", "is_completed", "Open", "Complete")

    # -- 5. Webhook docs ---------------------------------------------------
    docs = b.task(TODO, "Write API docs for webhooks", b.ago(days=5), MARCUS,
                  priority=Priority.MEDIUM.value, labels=["Documentation", "Feature"], estimated_hours=6.0,
                  assignee=MARCUS, start_date=_working_day(0).date(), due_date=_working_day(4),
                  checklist=[
                      {"id": 1, "text": "Event catalogue", "done": True},
                      {"id": 2, "text": "Payload reference", "done": False},
                      {"id": 3, "text": "Retry & signature verification", "done": False},
                  ])
    b.attach(docs, "webhook_payload_example.json", _WEBHOOK_JSON, MARCUS, b.ago(days=5))
    docs.description = """Document every webhook event so integrators can build without asking us.

## Example payload

```json
{
  "event": "task.moved",
  "task": { "id": 1337, "from_column": "In Review", "to_column": "Done" }
}
```

## Must cover

1. Event catalogue
2. Payload reference (full example attached)
3. **Retries** - exponential backoff, at most 5 attempts
4. **Signatures** - HMAC-SHA256 in the `X-Kanban-Signature` header
"""
    b.log(docs, A.TASK_UPDATED, MARCUS, b.ago(days=5) + timedelta(minutes=10), "Added a description",
          "description", "", docs.description)
    b.comment(docs, PRIYA, b.ago(days=4), "Can we include a copy-pasteable signature check in Python and Node?")
    b.comment(docs, MARCUS, b.ago(days=4, hours=-2), "Yes - adding both to the retry section.",
              edited=b.ago(days=4, hours=-3))
    b.log(docs, A.COMMENT_EDITED, MARCUS, b.ago(days=4, hours=-3), "Edited a comment: Yes - adding both to the retry section.",
          "comment", "Yes, will do.", "Yes - adding both to the retry section.")
    b.log(docs, A.TASK_UPDATED, MARCUS, b.ago(days=2), "Checklist: checked 'Event catalogue'", "checklist",
          "0/3 done", "1/3 done")

    # -- 6. CDN migration (blocked by the crash fix) ------------------------
    cdn = b.task(BACKLOG, "Migrate attachment storage to the CDN", b.ago(days=3), MARCUS,
                 priority=Priority.MEDIUM.value, labels=["Refactor", "Improvement"], estimated_hours=16.0,
                 assignee=MARCUS, start_date=_working_day(2).date(), due_date=_working_day(9))
    cdn.description = """> Large uploads should never block the UI thread.
> - *Storage RFC, section 2*

Move attachment blobs from local disk to the CDN while keeping the **content-addressed** layout
(`sha256` file names), so existing links keep working.

Blocked by the CSV import crash: both changes touch the same file-streaming code.
"""
    session.add(TaskDependency(task_id=cdn.id, depends_on_task_id=crash.id))
    b.log(cdn, A.DEPENDENCY_ADDED, MARCUS, b.ago(days=3) + timedelta(minutes=2),
          f"Marked as blocked by #{crash.id} '{crash.title}'", "dependency", None, crash.title)

    # -- 7. Recurring sync --------------------------------------------------
    sync_due = (b.now + timedelta(days=(4 - b.now.weekday()) % 7 or 7)).replace(hour=10, minute=0, second=0,
                                                                                  microsecond=0)
    sync = b.task(BACKLOG, "Weekly team sync", b.ago(days=20), PRIYA, priority=Priority.LOW.value,
                  is_recurring=True, due_date=sync_due, estimated_hours=1.0, assignee=PRIYA,
                  recurrence_rule_json=json.dumps({"frequency": RecurrenceFrequency.WEEKLY.value, "interval": 1}))
    b.attach(sync, "meeting_notes_template.md", _MEETING_TEMPLATE, PRIYA, b.ago(days=20))
    sync.description = """Every Friday at 10:00. Copy the attached template into a comment after each meeting.

## Agenda

1. Wins from the week
2. Blockers (anything older than 2 days gets an owner)
3. Next week's priorities
"""
    b.log(sync, A.TASK_UPDATED, PRIYA, b.ago(days=20) + timedelta(minutes=1), "Set to repeat: Weekly", "recurrence",
          "Does not repeat", "Weekly")
    b.comment(sync, PRIYA, b.ago(days=6),
              "**Notes - last week**\n\n- Wins: Q3 report drafted\n- Blockers: CSV import crash\n"
              "- Action: Marcus to investigate memory usage")

    # -- 8. Try it yourself --------------------------------------------------
    play = b.task(TODO, "✏️ Your turn: edit this card", b.ago(hours=1), you, priority=Priority.LOW.value,
                  labels=["Improvement"], assignee=you, due_date=_working_day(0))
    play.description = """This card is yours to experiment with. Nothing here matters, so go wild:

- Make some text **bold**, add a heading, or turn lines into a checklist
- Press **Ctrl+K** to add a link
- Drag an image from your file manager into this editor, or paste a screenshot
- Save, reopen, and check the **History** filter - every change you made is recorded
- Fill in the **Fields** tab, then right-click the card → **Save as Template**
"""
    b.log(play, A.TASK_UPDATED, you, b.ago(hours=1) + timedelta(minutes=1), "Added a description", "description",
          "", play.description)

    # -- 9. Capacity crunch (makes the workload view interesting) -----------
    launch = b.task(TODO, "Prepare launch screenshots & store listing", b.ago(days=2), PRIYA,
                    priority=Priority.HIGH.value, labels=["Design"], estimated_hours=14.0, assignee=ELENA,
                    start_date=_working_day(0).date(), due_date=_working_day(3))
    launch.description = """Screenshots for every supported platform plus the store copy.

> Elena is part-time (Mon/Tue). Open the **Workload** view to see why this
> deadline is a problem, then reassign or move the date.
"""
    b.log(launch, A.TASK_UPDATED, PRIYA, b.ago(days=2) + timedelta(minutes=1), "Assigned to Elena Rossi",
          "assignee", None, ELENA)

    # -- Automation showcase: one task to auto-archive, one gone stale -------
    ci = b.task(DONE, "Set up the CI pipeline", b.ago(days=30), MARCUS, created_in=TODO,
                priority=Priority.MEDIUM.value, labels=["Improvement"], estimated_hours=3.0, assignee=MARCUS,
                completed_at=datetime.now() - timedelta(days=20))
    ci.description = "Lint, tests and packaging on every push.\n\n> Completed weeks ago - the **auto-archive** policy " \
                     "will tuck it away. Find it again under **🗄 Archived**."
    b.move(ci, MARCUS, b.ago(days=25), TODO, DONE)
    b.log(ci, A.TASK_COMPLETED, MARCUS, b.ago(days=20), "Marked as complete", "is_completed", "Open", "Complete")
    charts = b.task(BACKLOG, "Evaluate charting libraries for the analytics view", b.ago(days=16), PRIYA,
                    priority=Priority.LOW.value, labels=["Feature"], estimated_hours=3.0, assignee=MARCUS)
    charts.description = "Compare two or three options for the analytics dashboard.\n\n> Nobody has touched this " \
                         "for a while - the **stale task** policy flags it with a label and a comment."
    b.comment(charts, MARCUS, b.ago(days=12), "I'll look at this once the CSV crash is fixed.")

    # Rules & policies ---------------------------------------------------------
    col_id = {i: c.id for i, c in enumerate(b.cols)}
    over_label = Label(board_id=b.board.id, name="Over estimate", color="#EF4444")
    session.add(over_label)
    session.flush()
    demo_rules = [
        ("Escalate overdue tasks", {"type": "overdue"}, [], [
            {"type": "set_priority", "priority": Priority.URGENT.value},
            {"type": "add_comment", "text": "⏰ '{title}' is overdue (was due {due}). Escalated to Urgent."},
            {"type": "notify", "text": "'{title}' is overdue"},
        ]),
        ("Ask for review when moved to review", {"type": "task_moved", "to_column_id": col_id[IN_REVIEW]}, [], [
            {"type": "add_comment", "text": "👀 {actor} moved this to {column} - ready for review."},
        ]),
        ("Triage new bugs", {"type": "label_added", "label_id": b.labels["Bug"].id}, [], [
            {"type": "set_priority", "priority": Priority.HIGH.value},
            {"type": "add_checklist", "items": "Reproduce the bug\nFind the root cause\nAdd a regression test"},
        ]),
        ("Flag time overruns", {"type": "over_estimate"}, [], [
            {"type": "add_label", "label_id": over_label.id},
            {"type": "add_comment", "text": "⚠ Logged time has exceeded the estimate - please re-estimate."},
        ]),
    ]
    for pos, (name, trigger, conditions, actions) in enumerate(demo_rules):
        session.add(AutomationRule(board_id=b.board.id, name=name, position=pos, is_enabled=True,
                                   trigger_json=json.dumps(trigger), conditions_json=json.dumps(conditions),
                                   actions_json=json.dumps(actions)))
    b.board.policies = {
        "auto_archive": {"enabled": True, "days": 14},
        "stale": {"enabled": True, "days": 10, "label": "Stale", "comment": True, "notify": True,
                  "move_to_column_id": None, "exclude_column_ids": []},
        "archive_stale": {"enabled": False, "days": 60},
    }

    # -- Custom fields & task templates ----------------------------------------
    points = b.field("Story points", "number", {"unit": "pts", "min": 0, "max": 100, "show_total": True},
                     description="Relative effort - the column headers add them up")
    component = b.field("Component", "select", {"options": [
        {"label": "Frontend", "color": "#3B82F6"}, {"label": "Backend", "color": "#10B981"},
        {"label": "Data", "color": "#F59E0B"}, {"label": "Docs", "color": "#8B5CF6"}]})
    impact = b.field("Customer impact", "rating", {"max": 5}, description="How much customers will notice")
    spec = b.field("Spec", "url", description="Design file or RFC")
    reviewer = b.field("Reviewer", "person", show_on_card=False)
    for task_obj, values in (
        (welcome, {component: "Docs", points: 1}),
        (onboarding, {component: "Frontend", points: 8, impact: 4, spec: "https://www.figma.com/file/onboarding-v2",
                      reviewer: MARCUS}),
        (crash, {component: "Data", points: 5, impact: 5}),
        (report, {component: "Docs", points: 3, reviewer: MARCUS}),
        (docs, {component: "Docs", points: 3, spec: "https://example.com/rfcs/webhooks"}),
        (cdn, {component: "Backend", points: 8, impact: 3}),
        (launch, {component: "Frontend", points: 5, impact: 4}),
        (ci, {component: "Backend", points: 2}),
        (charts, {component: "Frontend", points: 3, impact: 2}),
    ):
        for f_obj, value in values.items():
            b.set_field(task_obj, f_obj, value)
    b.log(onboarding, A.TASK_UPDATED, ELENA, b.ago(days=8), "Set Story points to 5 pts", "custom_field", None, "5 pts")
    b.log(onboarding, A.TASK_UPDATED, ELENA, b.ago(days=4), "Changed Story points from 5 pts to 8 pts",
          "custom_field", "5 pts", "8 pts")
    b.comment(onboarding, ELENA, b.ago(days=4) + timedelta(minutes=2),
              "Re-estimated to 8 points - the empty-state illustrations took longer than planned.")
    session.add(TaskTemplate(board_id=b.board.id, name="Bug report", icon="🐞", position=0, created_by=MARCUS,
                             data_json=json.dumps({
                                 "title": "Bug: ", "priority": Priority.HIGH.value, "labels": ["Bug"],
                                 "description": "## Steps to reproduce\n1. Go to …\n2. Click …\n\n## Expected\n…\n\n"
                                                "## Actual\n…\n",
                                 "checklist": ["Reproduce", "Write a failing test", "Fix", "Verify"],
                                 "fields": {"Story points": 2}}, ensure_ascii=False)))
    session.add(TaskTemplate(board_id=b.board.id, name="Weekly status report", icon="📝", position=1, created_by=PRIYA,
                             data_json=json.dumps({
                                 "title": "Status report - week {week}", "assignee": "@me", "due_in_days": 2,
                                 "labels": ["Documentation"], "column": "To Do",
                                 "description": "Written by {user} on {today}.\n\n## Done\n\n## Next\n\n"
                                                "## Risks\n",
                                 "checklist": ["Collect updates", "Write the summary", "Share with the team"],
                                 "fields": {"Component": "Docs", "Story points": 1}}, ensure_ascii=False)))

    # -- Schedule: milestones, dependency network, baseline ------------------
    beta = b.task(TODO, "🚀 Public beta", b.ago(days=10), PRIYA, priority=Priority.HIGH.value, assignee=PRIYA,
                  is_milestone=True, dates_locked=True, start_date=_working_day(7).date(), due_date=_working_day(7))
    beta.description = ("The date we announced publicly - **locked** 📌 so auto-scheduling and cascading never move "
                        "it. Everything feeding into it is on the critical path; open the **Gantt** view.")
    b.log(beta, A.TASK_UPDATED, PRIYA, b.ago(days=10) + timedelta(minutes=1), "Marked as a milestone",
          "is_milestone", "False", "True")
    ga = b.task(BACKLOG, "🏁 General availability", b.ago(days=10), PRIYA, priority=Priority.MEDIUM.value,
                assignee=PRIYA, is_milestone=True, start_date=_working_day(11).date(), due_date=_working_day(11))
    b.log(ga, A.TASK_UPDATED, PRIYA, b.ago(days=10) + timedelta(minutes=2), "Marked as a milestone",
          "is_milestone", "False", "True")
    FS_, SS_ = "FINISH_TO_START", "START_TO_START"
    schedule_links = [
        (launch, onboarding, SS_, 2, ELENA),   # screenshots can start 2 days after design starts
        (beta, onboarding, FS_, 0, PRIYA),
        (beta, launch, FS_, 0, PRIYA),
        (beta, crash, FS_, 0, PRIYA),
        (docs, crash, FS_, 0, MARCUS),         # deliberately violated: docs is planned to start before the fix
        (ga, cdn, FS_, 2, PRIYA),              # GA two working days after the CDN migration (critical path)
    ]
    for succ, pred, kind, lag, who in schedule_links:
        session.add(TaskDependency(task_id=succ.id, depends_on_task_id=pred.id, dependency_type=kind, lag_days=lag))
        linked_at = min(max(succ.created_at, pred.created_at) + timedelta(hours=1), b.now)  # after both exist
        b.log(succ, A.DEPENDENCY_ADDED, who, linked_at, f"Now depends on #{pred.id} '{pred.title}'",
              "dependency", None, pred.title)

    # A baseline saved at sprint planning; several tasks have slipped since
    plan = ProjectBaseline(board_id=b.board.id, name="Sprint plan", created_by=PRIYA, created_at=b.ago(days=6))
    session.add(plan)
    session.flush()
    planned = [  # (task, baseline start offset, baseline finish offset) in working days from today
        (onboarding, -8, 1), (crash, -4, -1), (launch, -1, 2), (docs, 0, 4), (cdn, 1, 8),
        (beta, 7, 7), (ga, 10, 10),
    ]
    for task_obj, s_off, f_off in planned:
        session.add(BaselineEntry(
            baseline_id=plan.id, task_id=task_obj.id, title=task_obj.title,
            start_date=_working_day(s_off).date(), finish_date=_working_day(f_off).date(),
            duration_days=0 if task_obj.is_milestone else f_off - s_off + 1, is_milestone=bool(task_obj.is_milestone),
        ))
    b.board.schedule_settings = {"cascade": True, "baseline_id": plan.id}

    # -- Logged time (past two weeks, local working hours) -----------------
    P, S = TimeEntrySource.POMODORO, TimeEntrySource.STOPWATCH
    b.track(onboarding, ELENA, 10, 9.5, 120, S, "User research synthesis")
    b.track(onboarding, ELENA, 9, 10, 150, S, "Low-fi wireframes")
    b.track(onboarding, ELENA, 7, 13, 100, P, "Wireframes v2 (4 pomodoros)")
    b.track(onboarding, ELENA, 3, 9, 175, S, "Hi-fi mockups")
    b.track(crash, MARCUS, 3, 14, 50, P, "Reproduce with sample file")
    b.track(crash, MARCUS, 2, 10, 100, S, "Streaming reader prototype")
    b.track(report, PRIYA, 6, 9, 90, S, "Pull numbers")
    b.track(report, PRIYA, 5, 11, 75, P, "Write highlights")
    b.track(report, PRIYA, 3, 15, 45, S, "Review fixes")
    b.track(docs, MARCUS, 4, 15, 50, P, "Event catalogue")
    b.track(docs, MARCUS, 1, 16, 40, S)
    b.track(sync, PRIYA, 7, 10, 30, S, "Weekly sync")
    b.track(welcome, PRIYA, 7, 8.5, 30, P, "Write the tour")
    b.track(ci, MARCUS, 24, 10, 150, S, "Pipeline config")
    b.finish_time()

    session.flush()
    return b.board

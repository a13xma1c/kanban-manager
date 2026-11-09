# KanbanManager — Modern Desktop Project Management Application

A powerful, responsive desktop project management and Kanban application built with **Python**, **PySide6 (Qt6)**, **SQLite**, and **SQLAlchemy**, packaged and managed with **uv**.

---

## Features

- **Multiple Workspaces & Boards**
  - Organize projects into distinct workspaces (e.g. Engineering, Marketing, Personal).
  - Create and manage multiple boards per workspace with custom theme colors and descriptions.
  - Interactive sidebar for workspace and board switching, management, and settings.

- **Drag-and-Drop Task Management**
  - Native Qt drag-and-drop (`QDrag` / `QMimeData`) for moving tasks between columns and reordering.
  - Visual insertion indicator and smooth card repositioning.
  - Interactive task cards with priority badges, label tags, checklist progress bars, due date pills, and status toggles.

- **Custom Columns & Workflow Rules**
  - Configurable Work-in-Progress (WIP) limits with visual warnings when capacity is reached.
  - Custom column types (`BACKLOG`, `TODO`, `IN_PROGRESS`, `REVIEW`, `DONE`, `CUSTOM`).
  - Strict workflow transition enforcement (configure which columns can transition to which next steps).
  - Done columns that automatically track completion timestamps.

- **Task Dependencies**
  - Blocked / Blocking prerequisite relationships.
  - Cycle detection and prevention (prevents circular dependencies).
  - Visual dependency indicators (`🔒 Blocked`, `🔗 N prereqs`) on task cards.
  - Dependency connector arrows rendered in the Timeline / Gantt view.

- **Due Dates & Scheduled Reminders**
  - Due date tracking with overdue highlights.
  - Background notification engine (`QTimer`) monitoring pending reminders.
  - Configurable reminder offsets (at time of due date, 15 min, 1 hour, 1 day prior).

- **Labels & Priority Levels**
  - Color-coded priority badges (`Urgent ★`, `High ◆`, `Medium ▲`, `Low ●`).
  - Custom color tags and labels per board.
  - Real-time search and multi-criteria filtering by priority, label, due date, and completion status.

- **Recurring Tasks**
  - Flexible recurrence frequencies (`Daily`, `Every Weekday`, `Weekly`, `Bi-weekly`, `Monthly`, `Custom interval`).
  - Automatic generation of the next task occurrence upon completing a recurring task instance.

- **Markdown & WYSIWYG Task Descriptions**
  - Descriptions are stored as Markdown and edited in either **Visual** (WYSIWYG) or **Markdown** (source, with syntax highlighting) mode; switch at any time.
  - Toolbar + shortcuts for bold, italic, strikethrough, inline code, headings, bulleted/numbered/task lists, quotes, code blocks and links.
  - Clickable task-list checkboxes, tables, and inline images in the visual editor.
  - Untouched descriptions are saved byte-for-byte, so opening and saving a task never rewrites it.
  - Task cards show a clean plain-text preview of the description.

- **File & Image Attachments**
  - Attach any file via button, drag & drop, or by pasting/dropping images straight into the description.
  - Image previews and thumbnails; insert any attached image inline (`![alt](attachment:<id>)`).
  - Open with the default app or "Save As"; per-file size limit (25 MB, `MAX_ATTACHMENT_SIZE_MB`).
  - Content-addressed storage (identical files stored once); files are deleted when no longer referenced.
  - Attachment changes are staged and applied when the task is saved.

- **Task Comments & Audit Trail**
  - Markdown comments per task; authors can edit (marked *edited*) and delete their own comments.
  - Every change is recorded with who/when and old → new values: title, description, priority, column moves, dates, estimates, labels, checklist items, recurrence, completion, dependencies, reminders, attachments and comments.
  - Activity timeline with **All / Comments / History** filters and "View changes" diffs for description and comment edits.
  - 📎 / 💬 counters on task cards; the analytics activity table shows who made each change.

- **Demo Board ("Feature Tour")**
  - New databases include a guided demo board with rich descriptions, inline images, file attachments, comment threads and a realistic history.
  - Add another any time with **✨ Add Demo Board** in the sidebar (or a workspace's right-click menu).

- **Multiple Views**
  - **Kanban Board View**: Column-based drag-and-drop workflow.
  - **Calendar View**: Month/week interactive calendar with due date chips and quick edit.
  - **Timeline / Gantt View**: Time-scaled task duration spans with dependency connector links.
  - **Analytics Dashboard**: WIP utilization, velocity metrics, completion rates, bottleneck alerts, and activity logs.

- **Local Data Storage & Backups**
  - Local SQLite database with Foreign Key constraints and WAL mode via SQLAlchemy ORM.
  - Automated and manual point-in-time database snapshots (`.db` files).
  - One-click database restoration with automatic safety snapshots.
  - Structured JSON export and import for data portability (format 1.1 also carries comments, attachments and task history; 1.0 files still import).
  - Snapshots also mirror attachment files into `backups/attachments/`, and restores bring back any missing ones.

---

## Tech Stack

- **Python**: >= 3.15
- **Package Manager**: [uv](https://github.com/astral-sh/uv)
- **GUI Framework**: PySide6 (Qt 6)
- **Database & ORM**: SQLite 3, SQLAlchemy 2.0+
- **Test Framework**: pytest

---

## Installation & Running

Ensure `uv` is installed, then run:

```bash
# Install dependencies into virtualenv
uv sync

# Run the KanbanManager application
uv run kanbanmanager
# or
uv run python -m kanbanmanager.app
```

---

## Data Location & Settings

Data lives in `~/.kanban_manager/` (override with `KANBAN_DATA_DIR`):

| Path | Contents |
|---|---|
| `kanban.db` | SQLite database |
| `kanban_attachments/` | Attachment files (content-addressed, next to the database they belong to) |
| `backups/` | Database snapshots, plus `backups/attachments/` with their attachment files |

Comments and history entries are attributed to your OS login name. Set `KANBAN_USER="Your Name"` to use a different display name.

Databases from earlier versions are upgraded automatically on startup, and so are restored snapshots.

---

## Running Tests

Run the comprehensive unit, workflow, recurrence, dependency, and UI integration test suite:

```bash
uv run pytest -v
```

---

## Keyboard Shortcuts

- `Ctrl + N` / `Cmd + N`: Create a new task in active board
- `Ctrl + F` / `Cmd + F`: Focus search filter input
- `Double-click Card`: Open task editor dialog
- `Right-click Card / Column`: Open contextual action menu
- In the description editor: `Ctrl + B` bold, `Ctrl + I` italic, `Ctrl + E` inline code, `Ctrl + K` link
- In the comment box: `Ctrl + Enter` to post

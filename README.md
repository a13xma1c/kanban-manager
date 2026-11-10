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
  - Dependency arrows in the Gantt view (finish/start-to-start/finish types with lag, critical path highlighting).

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

- **Pomodoro & Live Stopwatch Timer**
  - Start a **stopwatch** or a **Pomodoro** from a card's right-click menu or the task's **Time** tab.
  - A live timer in the status bar shows elapsed/remaining time and the task; you can pause, stop, skip a break, or discard the session there, and clicking the task name opens the task.
  - Pomodoro focus sessions are logged to the task and breaks are not. After a focus session a break starts (every Nth break is a long one), and when the break ends the next focus session waits for you. Lengths are configurable (⚙ → Pomodoro settings).
  - Timers survive restarts and crashes: a running timer is stored in the database and resumes on the next launch. When you close the app while tracking, it asks whether to stop and log the time or keep it running. Runs shorter than 30 s are treated as accidental and not logged.
  - The Time tab also takes manual entries ("1h 30m", "90m", "1.5", "1:30"), and you can edit or delete your own entries. Cards show logged vs. estimate (`⏱ 2.5/5h`, red when over) and a **● Tracking** badge while a timer runs.
  - Every logged, edited or deleted entry appears in the task's audit trail.

- **Workload & Capacity Planning View** (👥 Workload)
  - A person × week (or day) grid of planned work vs. capacity, for the current board or the whole workspace.
  - Each open task's *remaining* effort (estimate minus time logged) is spread over the assignee's working days up to its due date; overdue work lands on today. Capacity only counts days from today onward.
  - Cells are colour-coded (under 80% / 80-100% / over capacity) and also show time actually logged. A summary lists overloads, unassigned work and unestimated tasks.
  - Click a cell to see the tasks behind it; double-click one to open it, or right-click to **reassign** it (the menu shows each person's load).
  - **Team & Capacity** sets each person's weekly hours and working days (part-timers included). Tasks have an **Assignee** field in the task editor and an avatar on the card.

- **Timesheet Exporting** (Workload → Export Timesheet)
  - Pick a period (this/last week, this/last month, last 30 days or a custom range), a scope (board, workspace or everything) and a person, then preview entries or totals by day, task or person.
  - **CSV (entries)**: one row per time entry, with date, start/end, duration, decimal hours, person, workspace, board, task, labels, source and note.
  - **Summary CSV**: person × task rows with one column per day, plus totals.
  - **PDF**: a printable report grouped by person with daily subtotals.
  - Times are in local time. CSVs are UTF-8 with a BOM (opens cleanly in Excel), and cells that could be read as spreadsheet formulas are escaped.

- **Workflow Automation: Custom Event Triggers & Actions** (⚡ Automations in the stats bar, or a board's right-click menu)
  - Per-board rules: **When** *trigger* · **If** *conditions* (optional, all must match) · **Then** *actions*.
  - Event triggers (⚡, run immediately): task created (optionally in a column), moved (from/to a column), completed (including by moving into Done), reopened, priority changed, assignee changed, label added, comment posted (optionally containing text), time logged, logged time exceeding the estimate, a custom field changed (optionally to a given value).
  - Time-based triggers (⏰, checked every 5 minutes and at startup): due date within N hours, task became overdue, no activity for N days. Each fires once per task and situation, e.g. once per due date.
  - Conditions: priority (is / is not / at least / at most), column, labels, assignee (is, is not, set, empty), title contains, due date set, blocked, completed, and any custom field (is set / is empty / is / is not / contains / greater than / less than, compared the way that field's type understands values).
  - Actions: move to column (WIP and workflow rules still apply), set priority, add/remove label, assign (to a person or to whoever triggered the rule), unassign, set a due date N days out, post a comment, add checklist items, mark complete, reopen, archive, show a notification, set a custom field (for example, set *Owner* to `{actor}`), create a task from a task template. Comment, notification and field-value text can use placeholders: `{title} {assignee} {column} {priority} {due} {board} {actor} {days}`.
  - A visual rule editor with a live plain-English summary and validation (including rules that point at deleted columns or labels), ready-made templates, enable/disable, duplicate, and a **History** tab showing every run with its result.
  - Changes made by rules are attributed to `⚙ Automation: <rule name>` in each task's history. Rules can trigger other rules, but chains are depth-limited and each rule acts at most once per task per chain, so rules can't loop. A failing action (for example, a WIP limit reached) is recorded as a partial run instead of breaking what you were doing.

- **Auto-Archiving & Stale Task Policies** (⚡ Automations → Policies)
  - **Auto-archive** completed tasks N days after completion.
  - **Flag stale tasks**, meaning open tasks with no activity (no edits, moves, comments or logged time) for N days: add a label (created if needed), post a comment, show a notification, optionally move them to a column, and skip chosen columns. A task is flagged once per stale period.
  - **Archive stale tasks** after M days without activity.
  - Automation's own changes don't count as activity, so flagging a task doesn't reset its stale clock.
  - **Archive**: archived tasks disappear from every view, count and WIP limit but keep their history, comments, attachments and time. Browse, search, restore or permanently delete them under **🗄 Archived** (stats bar or board menu). Archive a task manually from its card's right-click menu.

- **Advanced Gantt, Critical Path Method & Auto-Scheduling** (📈 Gantt)
  - A task table (start, finish, working days, float) next to a time-scaled chart: zoom by days, weeks or months, with weekend shading, a today line, and completion and status colours.
  - **Critical path (CPM):** forward and backward passes over the dependency network compute each task's earliest and latest dates and total float. Critical tasks (float ≤ 0) are red, float is drawn as a tail to the latest finish, and the summary names the critical chain and the forecast finish.
  - **Dependencies:** finish-to-start, start-to-start, finish-to-finish and start-to-finish, each with a lag or lead in working days. Arrows are drawn by type. **Conflicts**, where the plan has a task too early for its dependencies, are dashed orange and listed.
  - **Interactive:** drag a bar to move it, drag its right edge to change the finish, drag the ○ handle onto another bar to link the two, and right-click to make a milestone, lock dates, edit dependencies (type and lag), add predecessors or clear dates.
  - **Auto-schedule:** proposes ASAP dates from dependencies and durations (a task's span, or its estimate ÷ hours per day) and previews every change with its reason. You apply only what you tick. Options: never move tasks earlier than planned; give dates to unscheduled tasks. Completed and 📌 locked tasks never move.
  - **Cascading date shifts:** when a task's dates change, dependents that would now violate a dependency shift automatically, transitively, keeping their durations and due times. Push-only by default, with an option to also pull dependents earlier. Shifts appear in history as `⚙ Automation: Cascading schedule`.
  - **Milestones:** zero-duration ◆ tasks, set from the task editor (Dates tab), the Gantt or a card. A milestone sits at the end of its day.
  - **Baselines:** save the plan as a named baseline, show it as ghost bars under the current plan, and compare: per-task start and finish variance in working days, plus late, early, new and removed tasks, and project-finish variance.
  - **Schedule settings** per board: working days, hours per day, an optional fixed project start, and cascading on or off (and pull).

- **My Focus: Tasks From Every Board** (🎯 My Focus in the sidebar)
  - One list of the open tasks assigned to you (or any teammate) across all boards and workspaces, grouped into **Overdue**, **Today**, **Next 7 days**, **Later** and **No due date**. Within a group, tasks are sorted by due date, then priority.
  - Each row shows its board and flags: 🔥 on the critical path (computed per board), 🔒 blocked, ◆ milestone, ⏱ timer running. Use *+ unassigned* to include unowned tasks, and the filter box to narrow the list.
  - Tick a task to complete it (recurring tasks create their next occurrence as usual). Double-click to edit. Right-click to reschedule (today, tomorrow, next Monday, in a week, or no due date), start a timer, go to its board or complete it.

- **Global Inbox & Quick Capture** (📥 Inbox, ⚡ Quick Capture)
  - **Quick Capture** is a small always-on-top window for recording a thought without leaving what you're doing. Open it with **Ctrl+Shift+Space** (configurable under ⚙) from anywhere in the app, or system-wide (see below). A live preview shows how your text is read.
  - **Syntax** (everything except the title is optional):

    | Write | Meaning |
    |---|---|
    | `tomorrow`, `fri`, `next fri`, `in 3 days`, `jun 5`, `2025-06-05`, `eow`, `eom`, `next week`, `weekend` | due date (the last date phrase wins) |
    | `3pm`, `15:00`, `at 9`, `noon`, `eod`, `tonight` | due time (a time alone means today, or tomorrow if that time has passed) |
    | `!urgent` `!high` `!medium` `!low`, `!1`–`!4`, `!!!` / `!!` | priority |
    | `#Design` | label (created on the target board if it doesn't exist) |
    | `@Marcus` / `@me` | assignee (matched against the team) |
    | `>roadmap` | destination board (prefix match; ambiguous or unknown names are flagged) |
    | `// more details` | notes that become the task description |
    | `"next friday party"` | quoted text is kept literally in the title |

  - With a `>board` (or a board chosen in the window's destination picker), the capture becomes a task in that board's *To Do* column (falling back to *Backlog*, then the first open column). Otherwise it goes to the **Inbox**.
  - **Inbox** (🎯 My Focus → 📥 Inbox tab, with a count in the sidebar): review captured items, add more inline, and **triage** them to a board and column. Select several to send them together; a single item's title can be edited on the way. Parsed due date, priority, labels and assignee carry over, and the task's history notes it came from the Inbox. You can also delete items.
  - **System-wide hotkey:** on **X11** and **Windows**, KanbanManager registers the hotkey with the OS while it is running. The ⚙ settings show whether this worked, for example if another app already owns the combination. On **Wayland** and **macOS**, apps can't grab global keys, so bind your desktop's own shortcut to:

    ```bash
    kanbanmanager --capture              # pop up Quick Capture (in the running app, or standalone)
    kanbanmanager --capture "Call Sam tomorrow 3pm !high"   # capture immediately, no window
    kanbanmanager-capture Call Sam tomorrow 3pm             # same, without quoting
    ```

    These commands hand the capture to the running app when there is one, otherwise they save straight to the database.
  - KanbanManager runs as a **single instance** per database: launching it again brings the existing window forward.

- **Multi-Board Split View** (🗂 Split View)
  - Up to four boards side by side (or stacked, via *Stack vertically*), each in a compact pane with its own board picker. ↗ opens a pane's board on its own, and ✕ closes the pane. Your layout is remembered.
  - **Drag cards between boards** to move them. The move respects the target column's WIP limit. Labels are matched by name on the target board, or created with the same colour. The task keeps its comments, attachments, time and history, and both boards log the move. Moving into or out of a done column completes or reopens the task. Automation rules on the target board fire as usual.
  - Drops now land where you release the card, in Split View and on the regular board (previously a drop always put the card at the top of the column).

- **Dynamic Custom Fields** (🧩 Fields in the stats bar, or a board's right-click menu → Custom Fields)
  - Each board defines its own fields, such as *Story points*, *Severity*, *Customer*, *Spec link* or *Publish date*. Twelve built-in types:
    - **Text** (single or multi-line, optional maximum length)
    - **Number** (decimals, unit, minimum and maximum)
    - **Currency** (symbol)
    - **Percent**
    - **Dropdown** and **Multi-select** (coloured options)
    - **Checkbox**
    - **Date** (accepts `2025-06-05`, `tomorrow`, `next fri`, `jun 5` …)
    - **Rating** (3 to 10 stars)
    - **Person**
    - **Link** and **Email** (validated, with an open button)
  - Each field can be **required**, have a **default value** for new tasks, a description shown in the editor, and be **shown on cards** or not. Fields can be reordered.
  - The task editor's **Fields** tab has an editor for each type, with inline validation. It won't save a task with a missing required field or a value that doesn't fit. Tasks created elsewhere (quick capture, automation, import) get the field's default, and cards flag any required fields still empty (`⚠ 1 required field`).
  - **Cards** show values as compact chips: `8 pts`, a coloured `Critical`, `★★★★`, `🔗 figma.com`, `✓ Reviewed`. **Number** fields can add up their values in each column header (`Σ 13 pts`).
  - The **field filter** in the filter bar offers choices that depend on the type: "Severity: is Critical", "Platforms: includes iOS", "Impact: 4+ stars", "Release: in the next 7 days", "Reviewer: is Priya", "…: is empty". The search box also matches field values.
  - Every value change is recorded in the task's history, for example "Changed Story points from 5 pts to 8 pts" (🧩).
  - **Editing a field's settings keeps your data.** Renaming a dropdown option updates every task that uses it, and removing an option clears it. Changing a field's type converts existing values, for example text "5" becomes number 5. Before anything is lost you see which tasks' values won't fit, and each affected task records the change.
  - **Moving a card to another board** (Split View) keeps its values: they go to the same-named field there. Missing fields and dropdown options are created, and values that can't be converted are dropped with a note in the history. Recurring tasks copy their values (and assignee) to the next occurrence.
  - Databases from earlier versions had a hidden free-form `custom_fields` dict per task. It is migrated on startup into real fields, with types inferred from the values.
  - **Extensible:** new field types can be registered from code; see [Extending: custom field types](#extending-custom-field-types).

- **Board & Task Templates**
  - **New Board** opens a template gallery with a preview of each template's columns, labels, fields, task templates and automation rules. You choose the board's name and colour, and whether to include the template's sample tasks. The built-in templates are:
    - **Standard Kanban** (the classic five columns)
    - **Simple To-Do**
    - **Software Sprint** (story points with column totals, components, pull-request links; bug, user-story and spike templates; a review rule)
    - **Bug Tracker** (required severity, environments, reproducible, reporter; critical bugs are escalated to Urgent automatically)
    - **Content Calendar** (channels, publish dates, authors, word counts)
    - **Hiring Pipeline** (role, rating, source, salary expectation; an offer checklist rule)
    - **Personal (GTD)** (context, energy, minutes; a weekly review template)
  - **Save any board as a template** (board right-click menu → 💾 Save as Board Template). A template captures columns (with WIP limits and types), labels, custom fields, task templates, automation rules and policies, and schedule settings. It can also include the board's open tasks as sample tasks, without dates, comments or attachments. Templates can be **exported** to a `.kbtemplate.json` file and **imported** elsewhere from the gallery.
  - **Task templates** (📋 Templates in the stats bar) pre-fill new tasks with:
    - title, description, priority and labels (created on the board if missing)
    - checklist, estimate and assignee (`@me` = whoever uses the template)
    - due and start dates as offsets in working or calendar days, with a due time
    - milestone flag, target column and custom field values
  - Titles, descriptions, checklist items and text values can use placeholders: `{date} {today} {weekday} {week} {month} {year} {user} {board}`. For example, `Status report - week {week}`.
  - A template belongs to one board or is shared by **all boards**. Fields and labels are matched by name, so shared templates work anywhere, and values for fields a board doesn't have are skipped with a note.
  - There are several ways to use task templates:
    - **Column ⋮ → New from Template**, or **📋 From Template** in a new task's editor: the editor opens pre-filled, and nothing is saved until you click Save. A template's `Bug: ` title keeps its cursor after the prefix.
    - **New Task from Template** in the templates dialog.
    - An automation rule's *Create a task from a template* action.
  - Turn an existing task into a template with **📋 Save as Template…** (task editor or card right-click menu). Tasks record which template they came from in their history.

- **Analytics, Vector Charts & PDF Reports** (📊 Analytics)
  - KPIs for the chosen period (14 days to a year): completion rate, work in progress, throughput per week, average and 85th-percentile cycle time, overdue, blocked, and columns over their WIP limit.
  - Charts, built from the audit trail so they also cover the past:
    - **cumulative flow** (tasks in each column at the end of every day)
    - **burn-up** (scope vs. done, as a task count or the sum of a number field such as story points)
    - **throughput** per week
    - **cycle-time histogram** with the average and 85% lines
    - **open tasks** by column, priority, assignee, label, and the first dropdown field (summed by story points when the board has them)
  - Every chart has a ⬇ menu: **SVG** or **PDF** (true vectors: crisp when zoomed, editable in Inkscape or Illustrator), **PNG** (2x resolution), or copy to the clipboard. **⬇ All Charts** saves them all at once.
  - **⬇ PDF Report** creates a printable A4 report:
    - the KPIs and all charts (vector)
    - the tasks of every column (assignee, due date, priority, labels, up to four compact fields shown on cards, optionally descriptions)
    - page headers, footers and page numbers
  - **Pictures of any view:**
    - **Board:** every column at full height, not just what fits on screen, using the current filters (Import / Export → Board picture).
    - **Gantt chart:** the whole schedule with a task-name column (Gantt → ⬇ Export).
    - **Calendar:** the current month (Calendar → ⬇ Export).
    - PDF and SVG keep text and shapes as vectors.

- **Calendar Integration & Sync** (📅 Calendar → 🔄 Calendar Sync, or a board's right-click menu)
  - **.ics files:** export tasks as **events** on their due date (all-day or at the due time) or as **to-dos**. The scope is a board, all boards, or your tasks. Exports include recurrence (RRULE), categories from labels, priority, status, checklist progress and optional reminders. Importing an .ics file turns events and to-dos into tasks:
    - time zones (IANA and Outlook names) are converted to local time
    - a multi-day event becomes one task spanning its days
    - a recurring event becomes a recurring task due at its next occurrence
    - importing the same file again updates those tasks instead of duplicating them, and an import can be undone
  - **Live feed:** publishes your tasks as a calendar other apps subscribe to: all boards, one board, or *My tasks*. Apple Calendar, Outlook, Thunderbird and GNOME Calendar re-read it automatically while KanbanManager runs. It listens on this computer only, unless you allow other devices on your network. The address contains a secret token, which you can replace (this breaks existing subscriptions). Web calendars such as Google Calendar can't reach a local address, so use an exported .ics file (or CalDAV) for those.
  - **Subscriptions:** a board follows a calendar and syncs every N minutes in the background (or on demand), with a history of every run. There are two kinds:
    - **iCalendar feed** (read-only): any `https://`, `webcal://` or file address, for example Google Calendar's *secret address in iCal format* or a published Outlook calendar.
      - New entries become tasks in a column you choose; only events in a time window are imported (default: 7 days back to 60 days ahead).
      - Changed entries update their task. Your own edits are kept until the calendar changes that entry.
      - Entries that disappear are archived (or deleted, or kept).
      - A task you delete isn't brought back.
    - **CalDAV calendar** (Nextcloud, iCloud, Fastmail, Radicale, Baïkal, mailbox.org …), either **two-way**, **calendar → board** or **board → calendar**, as to-dos or events.
      - Enter the server address and **Test Connection** finds your calendars.
      - The sync remembers both sides' state at the last sync, so it knows who changed what. One-sided changes are copied across. When both sides changed, the newer change wins (or the calendar or the board, as you choose), and the task's history notes the conflict.
      - Deletions propagate in both directions. Writes use ETags, so an entry changed on the server during a sync is never overwritten (it's picked up next time).
      - Entries written back keep every property KanbanManager doesn't manage (attendees, alarms, the server's own fields).
    - Changes made by a sync appear in task histories as `🔄 Calendar sync: <name>`. Syncs run in the background with their own database session, so changes they bring in don't trigger automation rules or cascading date shifts.
  - Field mapping: title ↔ SUMMARY, description ↔ DESCRIPTION, due ↔ DUE (events: DTSTART), start ↔ DTSTART, labels ↔ CATEGORIES, priority ↔ PRIORITY (1/3/5/9), done ↔ STATUS:COMPLETED (the task moves to or from the board's done column), recurrence ↔ RRULE.
  - Passwords are stored in the local database. Use an app-specific password where your provider offers one (iCloud requires it). Google Calendar's CalDAV needs OAuth, which isn't supported, so use its secret iCal address for read-only sync.

- **Import & Export Tools** (⇅ Import / Export in the sidebar, `Ctrl+Shift+E`)
  - **Export:**
    - **Excel (.xlsx)** workbooks: typed numbers and dates, a bold frozen header row, filters and sized columns, with no extra software.
    - **CSV** (UTF-8 for Excel, with formula-injection protection) of one or all boards, optionally with completed and archived tasks. Every custom field gets its own column.
    - **Markdown** documents of a board (checkbox lists with metadata, checklists and descriptions).
    - The **PDF report**, **board pictures**, **calendar files** and the **full JSON backup**.
  - **Spreadsheet import (CSV / TSV / Excel)** from Jira, Asana, Trello, GitHub, Monday, Todoist or your own sheets, in a three-step wizard:
    - **Map:** columns are matched to task fields by their headers, with presets for the common tools (including the board's custom fields, or *new text field*). The text encoding and separator are detected automatically (the separator by which one splits the rows into consistent columns, so tabs or semicolons inside cells don't confuse it).
    - **Preview:** every row is shown with how it will be read: new columns (＋), new labels, skipped rows and warnings. Values are parsed flexibly:
      - dates: `2025-06-05`, `06/05/2025` (day-first or month-first, detected or chosen), `Jun 5, 2025`, Jira's `05/Jun/25 3:45 PM`
      - priorities: *Highest*, *P1*, *!!!*
      - estimates: `1h 30m`, `2d`, Jira's seconds
      - yes/no values, lists, and `[x]` checklist items
      - unknown statuses become new columns (or go to a column you choose), and "Done"-like statuses complete the task
    - **Import:** duplicates (same title in the same column) are skipped. **Undo** removes the imported tasks and any columns, labels and fields the import created.
  - **Trello board import** (Trello: Board menu → Print, export and share → JSON) creates a new board with:
    - lists as columns (a "Done" list becomes the done column) and labels with their colours (unnamed ones are named after the colour)
    - cards with descriptions, attachment links, due and start dates, completion and the first member as assignee
    - checklists, comments (with their authors and dates), custom fields (text, number, date, checkbox, dropdown) and, if you choose, archived cards

- **Vim-Style Keyboard Navigation & Command Palette**
  - The whole board can be driven without the mouse. Plain-letter keys never fire while you type: text boxes, combos, spin boxes, lists and buttons keep their keys.
  - On the board, `j` / `k` / `h` / `l` (or the arrows) move a visible selection between cards and columns, skipping empty columns and clamping to the shortest one; `g g` / `G` jump to the top and bottom. The selection and Tab focus stay in step, so keyboard users and screen readers see the same thing.
  - With a card selected: `Enter` or `o` opens it, `x` completes or reopens it, `Shift+J/K/H/L` *moves the task itself* (down, up, to the previous or next column), `+` / `-` change its priority, `t` starts or stops a timer, `a` archives, `Delete` or `#` deletes (with confirmation; the next card takes the selection), and `n` adds a task to its column.
  - Anywhere: `/` searches, `c` quick-captures, `[` / `]` switch boards, `1`–`5` switch views (Board · Calendar · Gantt · Analytics · Workload), `Esc` clears the selection and then the filters, and `?` shows a generated overlay of every shortcut.
  - **Command palette** (`Ctrl+K` or `Ctrl+P`): type a few letters to jump to any board, page or view, run any action (new task, import/export, calendar sync, automations, appearance…), or open any task on any board by title. Matching is fuzzy - the query's letters must appear in order, with word-start and consecutive-letter bonuses, so `gcal` finds *Go to Calendar* and `ca` prefers *Quick **Ca**pture* over *alpa**ca***. An empty palette lists everything for browsing.

- **Themes & Accessibility** (🎨 Appearance in the sidebar)
  - Six built-in themes: **Dark** (the classic, and the default), **Light**, **Midnight** (true black for OLED), **Nord**, **Paper** (warm sepia) and **High contrast**, plus a **custom theme** built from ten role colors (background, surface, text, accent, success/warning/danger/info, purple, pink) with color pickers and a live preview.
  - Every other shade in the app - borders, hovers, muted text, badge tints - is derived from those roles, so a custom palette restyles the whole app coherently: dark badge backgrounds become pale tints on light themes and badge text flips to deep shades, keeping every pairing readable. Themes apply instantly to all open windows, no restart.
  - The Appearance dialog shows a **WCAG contrast report** for the selected palette; the High contrast theme passes WCAG AA on every reported pair.
  - **Font scaling** from 80% to 160% resizes every font in the app.
  - Accessibility details: task cards are focusable (`Tab` walks the board) with full screen-reader descriptions ("Task: …, priority High, due …, assigned to …, blocked"), buttons have accessible names ("Add task to Backlog", "Mark complete"), keyboard focus is marked with a visible focus ring, and the selection ring follows the keyboard.
  - Charts and PDF reports keep their own fixed light/dark print palettes so exported files look the same everywhere; exported board pictures follow the screen theme.

- **Demo Board ("Feature Tour")**
  - New databases include a guided demo board with rich descriptions, inline images, file attachments, comment threads and a realistic history, plus a small team with capacities, assignees and two weeks of logged time (one teammate is deliberately over capacity), plus four automation rules and enabled policies. On the first run one long-finished task is auto-archived and one neglected idea is flagged stale. It also includes a schedule: two milestones (a locked public beta and a GA date on the critical path), start-to-start and lagged links, one dependency conflict for Auto-schedule to fix, and a "Sprint plan" baseline that shows what has slipped. A "✏️ Your turn" card is assigned to you and due today (or the next working day), so it shows up in 🎯 My Focus, and the Inbox starts with two example captures to triage. The board also has custom fields (story points with column totals, component, customer impact, spec link, reviewer) with values and some history, plus two task templates (*Bug report*, *Weekly status report*).
  - Add another any time with **✨ Add Demo Board** in the sidebar (or a workspace's right-click menu).

- **Multiple Views**
  - **Kanban Board View**: Column-based drag-and-drop workflow.
  - **Calendar View**: Month calendar with due date chips and quick edit, calendar sync and export (see above).
  - **Gantt View**: Interactive schedule with the critical path, float, typed dependencies, milestones, baselines and auto-scheduling (see above).
  - **Analytics Dashboard**: KPIs, cumulative flow, burn-up, throughput, cycle time and breakdowns as exportable vector charts, the PDF report, and recent activity (see above).
  - **Workload View**: team capacity planning and timesheet export (see above).
  - **My Focus** and **Split View** (sidebar): your tasks from every board, and several boards side by side (see above).

- **Fast on Large Boards**
  - Refreshing the board reuses the cards and columns that didn't change instead of rebuilding them, filtering hides and re-shows cards, and the number of database queries doesn't grow with the size of the board (hot foreign keys are indexed). On a 400-card board a refresh takes about 0.2 s and a search keystroke about 0.1 s (previously 3-4 s and 2 s).
  - Regression tests keep it that way: query counts, card reuse and layout caching are checked on every run (see *Running Tests*).

- **Local Data Storage & Backups**
  - Local SQLite database with Foreign Key constraints and WAL mode via SQLAlchemy ORM.
  - Automated and manual point-in-time database snapshots (`.db` files).
  - One-click database restoration with automatic safety snapshots. A backup is checked before anything changes (damaged files and other apps' databases are refused), the safety snapshot is taken through SQLite so it includes work not yet written out of the WAL journal, and the restore itself goes through SQLite's backup API, so it's correct even while other connections are open.
  - Deleting a workspace, board or task always asks first and says what goes with it (boards and tasks, comments, attachments, logged time); archiving is suggested as the undoable alternative.
  - Structured JSON export and import for data portability (format 1.6 also carries comments, attachments, task history, assignees, time entries, team capacity, automation rules, board policies, archived tasks, dependency types and lags, milestones, locked dates, schedule settings, baselines, the Inbox, custom field definitions and values, task templates and saved board templates; older files still import). Rules are exported by column, label, field and template *name*, and baselines by task, and all of them are re-linked on import. Imported boards get exactly the exported labels and column types.
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
uv run python -m kanbanmanager
```

### Command-line tools

The same exports, imports and calendar syncs work without opening the window, for scripts, scheduled jobs, or publishing files to a shared folder:

```bash
kanbanmanager --export xlsx --board "Sprint 42" --output sprint.xlsx   # or csv / md / json
kanbanmanager --export pdf  --board "Sprint 42" --days 90              # the board report
kanbanmanager --export svg  --board "Sprint 42"                        # board picture (or png)
kanbanmanager --export ics  --events --output ~/Public/tasks.ics       # due dates of all boards (--mine, --completed)
kanbanmanager --import issues.csv --board "Sprint 42"                  # csv / tsv / xlsx / .ics / Trello .json
kanbanmanager --sync                                                   # every calendar subscription (or --sync NAME)
```

`--board` takes a board's name, its beginning, or its id. PDF and picture exports need no display. The commands work on the same database as the app, and they can run while the app is open.

---

## Data Location & Settings

Data lives in `~/.kanban_manager/` (override with `KANBAN_DATA_DIR`):

| Path | Contents |
|---|---|
| `kanban.db` | SQLite database |
| `kanban_attachments/` | Attachment files (content-addressed, next to the database they belong to) |
| `backups/` | Database snapshots, plus `backups/attachments/` with their attachment files |

Comments, history entries and tracked time are attributed to your OS login name. Set `KANBAN_USER="Your Name"` to use a different display name (use the same name you assign tasks to, so the workload view matches logged time to people).

Databases from earlier versions are upgraded automatically on startup, and so are restored snapshots.

---

## Extending: custom field types

Field types live in a registry (`kanbanmanager.core.custom_fields`). A type validates and normalises values, parses them from text, and formats them for display. It also renders card chips and can take part in filters, comparisons, totals and conversions. Register a type before the UI starts (for example in a small launcher script):

```python
import re
from kanbanmanager.core.custom_fields import FieldType, FieldValueError, register_field_type

class PhoneField(FieldType):
    key, label, icon = "phone", "Phone", "📞"      # the key is stored in the database: keep it stable
    description = "A phone number"

    def validate(self, value, config):
        text = self._text(value)                   # None for empty
        if text and not re.fullmatch(r"\+?[\d ()-]{6,}", text):
            raise FieldValueError("is not a phone number")
        return text

register_field_type(PhoneField())
```

The new type then appears in the field manager. Its values are edited in a text box (using `to_text` / `parse`), shown on cards, searched, exported and usable in automation. For a dedicated editor, subclass `ui.components.field_editors.FieldEditor` and call `register_field_editor("phone", PhoneEditor)`. If a database contains a field whose type isn't installed (for example a removed plugin), its values are kept untouched and shown read-only.

---

## Running Tests

`uv sync` installs the test tools (pytest, pytest-xdist, pytest-timeout, pytest-cov and Hypothesis - the `dev` dependency group). The suite runs headless (Qt's offscreen platform), so no display is needed.

```bash
uv run pytest -n auto                 # everything, in parallel (1-1.5 minutes on 4 cores)
uv run pytest                         # everything, one process (about 4 minutes)
uv run pytest -m "not slow"           # skip subprocess, crash-detection and large-data tests
uv run pytest -m perf -s              # only the performance budgets (prints the timings)
uv run pytest tests/test_cli.py -q    # one area
uv run pytest -n auto --cov --cov-report=term-missing   # with a coverage report
HYPOTHESIS_PROFILE=thorough uv run pytest tests/test_properties.py   # 1,000 examples per property
```

**What's covered** (about 450 tests):

| Kind | Where | What |
| --- | --- | --- |
| Unit & integration | `test_database.py`, `test_workflow.py`, `test_custom_fields.py`, `test_scheduling.py`, `test_automation.py`, `test_calendar_sync.py`, `test_exchange.py`, … | Service operations, migrations, every feature's core logic, file formats, a mock CalDAV server |
| Property-based | `test_properties.py`, `test_recurrence.py` | Laws checked against generated inputs: iCalendar escaping/folding/round trips, CSV & XLSX round trips, parsers that must never crash, the custom-field type contract for every type, working-day arithmetic, CPM and auto-scheduling invariants, recurrence rules, colour maths, fuzzy matching |
| Model-based | `test_service_model.py` | Random sequences of create / move / reorder / complete / archive / restore / delete / depend against an in-memory model of the board: card order, flags, WIP limits and the (acyclic) dependency graph must always agree |
| UI | `test_*_ui.py`, `test_interactions_ui.py`, `test_crud_ui.py` | Real widgets driven offscreen: views, dialogs, keyboard navigation, synthesised drag & drop, context menus, deletion confirmations |
| Entry points | `test_cli.py`, `test_app_entry.py` | The command-line tools and start-up (single instance, `--capture`), in-process; plus one real subprocess smoke test |
| Data safety | `test_backup.py` | Snapshots and restores in WAL mode with other connections open; damaged or foreign backup files are refused without touching your data |
| Performance | `test_performance.py` | SQL statement counts that must not grow with board size (N+1 guards), index usage, card and column reuse, layout caching, one board load at start-up, plus generous wall-clock budgets (`KANBAN_PERF_FACTOR=2` on slow machines) |
| Robustness | `test_qt_ownership.py` | A static audit for Qt ownership mistakes and repeated subprocess runs that build and destroy every window type (catches double-free crashes) |

**Test infrastructure** (`tests/conftest.py`, `tests/support.py`):

- Every test process gets its own throw-away data directory, so tests never touch `~/.kanban_manager`; one offscreen `QApplication` is shared per process.
- After each test, windows and dialogs it created are shut down and destroyed, database connections are closed, and the theme and application style are reset - tests can't leak into each other, in any order or in parallel.
- A **modal watchdog** fails a test that leaves a dialog open (instead of hanging the run), and `pytest-timeout` stops anything else that hangs after 5 minutes.
- Helpers: `seeded_manager(path)` (the demo database, copied from a per-process cache instead of re-seeded), `make_board(service, tasks=300, dependencies=40, with_dates=True)`, `count_queries(manager)`, `recording(signal)`, and `MenuCatcher` for non-blocking context menus.

---

## Keyboard Shortcuts

Press `?` in the app for this list as an overlay (it is generated from the same binding table).

**Move around the board (vim keys)**

| Keys | Action |
| --- | --- |
| `j` / `↓` · `k` / `↑` | Next / previous card |
| `h` / `←` · `l` / `→` | Column to the left / right |
| `g g` · `G` | First / last card in the column |
| `Esc` | Clear the selection, then the filters |
| `Tab` | Walk through cards and controls |

**With a card selected**

| Keys | Action |
| --- | --- |
| `Enter` / `o` | Open the task |
| `x` | Complete / reopen |
| `Shift+J` / `Shift+K` | Move the task down / up |
| `Shift+H` / `Shift+L` | Move it to the previous / next column |
| `+` / `-` | Raise / lower its priority |
| `t` | Start / stop a timer on it |
| `a` | Archive it |
| `Delete` / `#` | Delete it (confirms first) |
| `n` | New task in its column |

**Anywhere**

| Keys | Action |
| --- | --- |
| `Ctrl+K` / `Ctrl+P` | Command palette: boards, tasks, views, actions |
| `/` | Search |
| `c` | Quick capture |
| `[` / `]` | Previous / next board |
| `1` … `5` | Board · Calendar · Gantt · Analytics · Workload |
| `?` | All keyboard shortcuts |

**Other**

- `Ctrl + N` / `Cmd + N`: Create a new task in active board
- `Ctrl + F` / `Cmd + F`: Focus search filter input
- `Double-click Card`: Open task editor dialog
- `Right-click Card / Column`: Open contextual action menu
- In the description editor: `Ctrl + B` bold, `Ctrl + I` italic, `Ctrl + E` inline code, `Ctrl + K` link
- In the comment box: `Ctrl + Enter` to post
- Right-click a card: Start Stopwatch / Start Pomodoro / Stop Timer
- `Ctrl + Shift + Space`: Quick Capture (configurable; system-wide on X11/Windows, otherwise bind `kanbanmanager --capture`)
- In Quick Capture: `Enter` save and close, `Ctrl + Enter` save and add another, `Esc` close
- `Ctrl + Shift + E`: Import / Export

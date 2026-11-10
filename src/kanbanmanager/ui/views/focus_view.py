"""
My Focus & Global Inbox (cross-workspace)

* **Focus**: every open task assigned to you (or another person), across all
  workspaces and boards, grouped by urgency: Overdue, Today, Next 7 days,
  Later, No due date. Flags show critical-path 🔥, blocked 🔒, milestones ◆ and
  a running timer ⏱. Complete, open, time, reschedule or jump to the board.
* **Inbox**: quick-captured items waiting to be triaged into a board.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import capture as cap
from kanbanmanager.core.schedule_engine import ScheduleEngine
from kanbanmanager.core.signals import app_signals
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.enums import Priority
from kanbanmanager.ui.styles import CHECKBOX_QSS

GROUPS = [
    ("overdue", "⏰ Overdue", "#F87171"),
    ("today", "📅 Today", "#FBBF24"),
    ("week", "🗓 Next 7 days", "#60A5FA"),
    ("later", "🔭 Later", "#94A3B8"),
    ("nodate", "📭 No due date", "#64748B"),
]
PRIORITY_RANK = {Priority.URGENT.value: 0, Priority.HIGH.value: 1, Priority.MEDIUM.value: 2, Priority.LOW.value: 3}


def group_for(due: Optional[datetime], now: datetime) -> str:
    if due is None:
        return "nodate"
    if due < now:  # past its due moment (same rule as a card's overdue badge)
        return "overdue"
    if due.date() == now.date():
        return "today"
    if due.date() <= now.date() + timedelta(days=7):
        return "week"
    return "later"


def group_tasks(tasks: List[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Bucket focus tasks; within a bucket sort by due date, then priority, then title."""
    now = now or datetime.now()
    groups: Dict[str, List[Dict[str, Any]]] = {key: [] for key, _l, _c in GROUPS}
    for t in tasks:
        due = datetime.fromisoformat(t["due_date"]) if t.get("due_date") else None
        groups[group_for(due, now)].append(t)
    for items in groups.values():
        items.sort(key=lambda t: (t.get("due_date") or "9999", PRIORITY_RANK.get(t.get("priority"), 2),
                                  t.get("title", "").lower()))
    return groups


def due_text(iso: Optional[str], now: Optional[datetime] = None) -> str:
    if not iso:
        return ""
    due = datetime.fromisoformat(iso)
    now = now or datetime.now()
    if due.date() < now.date():
        days = (now.date() - due.date()).days
        return f"{days} day{'s' if days != 1 else ''} overdue"
    return cap.describe_due(due, now)


class TriageDialog(QDialog):
    """Choose where inbox items go."""

    def __init__(self, service: DatabaseService, items: List[Dict[str, Any]], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.items = items
        self.setWindowTitle("Send to Board" if len(items) == 1 else f"Send {len(items)} items to a board")
        self.setMinimumWidth(480)
        form = QFormLayout(self)
        self.title_edit = QLineEdit(items[0]["title"] if len(items) == 1 else "")
        if len(items) == 1:
            form.addRow("Task:", self.title_edit)
        self.board_combo = QComboBox()
        for ws in service.get_workspaces():
            for b in ws.boards:
                if not b.is_archived:
                    self.board_combo.addItem(f"{b.name}  ·  {ws.name}", b.id)
        hint = next((i["board_hint_id"] for i in items if i.get("board_hint_id")), None)
        if hint is not None and self.board_combo.findData(hint) >= 0:
            self.board_combo.setCurrentIndex(self.board_combo.findData(hint))
        self.board_combo.currentIndexChanged.connect(self._fill_columns)
        form.addRow("Board:", self.board_combo)
        self.column_combo = QComboBox()
        form.addRow("Column:", self.column_combo)
        self._fill_columns()
        note = QLabel("Due dates, priority, labels and assignees captured with the items are applied; missing "
                      "labels are created on the board.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #64748B; font-size: 11px;")
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Send")
        buttons.accepted.connect(self.send)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.created: List[int] = []

    def _fill_columns(self) -> None:
        board_id = self.board_combo.currentData()
        self.column_combo.clear()
        if board_id is None:
            return
        default = self.service.default_capture_column(board_id)
        for c in self.service.get_columns_for_board(board_id):
            self.column_combo.addItem(c.name, c.id)
        self.column_combo.setCurrentIndex(max(0, self.column_combo.findData(default)))

    def send(self) -> None:
        board_id, column_id = self.board_combo.currentData(), self.column_combo.currentData()
        if board_id is None:
            return
        for item in self.items:
            title = self.title_edit.text() if len(self.items) == 1 else None
            task = self.service.triage_inbox_item(item["id"], board_id, column_id, title=title)
            self.created.append(task.id)
        self.accept()


class FocusView(QWidget):
    open_board_requested = Signal(int)      # board_id
    capture_requested = Signal()
    capture_settings_requested = Signal()
    inbox_changed = Signal()

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None,
                 scheduler: Optional[ScheduleEngine] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.scheduler = scheduler or ScheduleEngine(self.service)
        self.running_task_id: Optional[int] = None
        self.tasks: List[Dict[str, Any]] = []
        self.inbox: List[Dict[str, Any]] = []
        self._init_ui()
        self.setStyleSheet(CHECKBOX_QSS)

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        layout.addLayout(top)
        title = QLabel("🎯 My Focus")
        title.setStyleSheet("font-size: 16px; font-weight: 800;")
        top.addWidget(title)
        top.addSpacing(8)
        self.person_combo = QComboBox()
        self.person_combo.setMinimumWidth(160)
        self.person_combo.currentIndexChanged.connect(self.refresh_focus)
        top.addWidget(self.person_combo)
        self.unassigned_chk = QCheckBox("+ unassigned")
        self.unassigned_chk.setToolTip("Also show open tasks nobody owns yet")
        self.unassigned_chk.toggled.connect(self.refresh_focus)
        top.addWidget(self.unassigned_chk)
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 Filter...")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(220)
        self.search.textChanged.connect(self._render_focus)
        top.addWidget(self.search)
        top.addStretch()
        self.capture_btn = QPushButton("⚡ Quick Capture")
        self.capture_btn.setObjectName("PrimaryButton")
        self.capture_btn.clicked.connect(self.capture_requested.emit)
        top.addWidget(self.capture_btn)
        settings_btn = QPushButton("⚙")
        settings_btn.setToolTip("Quick capture shortcut settings")
        settings_btn.clicked.connect(self.capture_settings_requested.emit)
        top.addWidget(settings_btn)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # Focus tab -------------------------------------------------------------
        focus = QWidget()
        self.tabs.addTab(focus, "🎯 Focus")
        fl = QVBoxLayout(focus)
        fl.setContentsMargins(8, 8, 8, 8)
        self.summary = QLabel("")
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setWordWrap(True)
        fl.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Task", "Board", "Due", "Priority"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            self.tree.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._task_menu)
        self.tree.itemDoubleClicked.connect(self._open_item)
        self.tree.itemChanged.connect(self._on_item_changed)
        fl.addWidget(self.tree, 1)
        hint = QLabel("Tick to complete · double-click to open · right-click to reschedule, start a timer or go "
                      "to the board")
        hint.setStyleSheet("color: #64748B; font-size: 11px;")
        fl.addWidget(hint)

        # Inbox tab -------------------------------------------------------------
        inbox = QWidget()
        self.tabs.addTab(inbox, "📥 Inbox")
        il = QVBoxLayout(inbox)
        il.setContentsMargins(8, 8, 8, 8)
        add_row = QHBoxLayout()
        il.addLayout(add_row)
        self.inbox_input = QLineEdit()
        self.inbox_input.setPlaceholderText("Capture to the inbox...   e.g. Renew passport next month !high")
        self.inbox_input.returnPressed.connect(self.add_from_input)
        add_row.addWidget(self.inbox_input, 1)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.add_from_input)
        add_row.addWidget(add_btn)
        self.inbox_tree = QTreeWidget()
        self.inbox_tree.setColumnCount(4)
        self.inbox_tree.setHeaderLabels(["Item", "Details", "Suggested board", "Captured"])
        self.inbox_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            self.inbox_tree.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.inbox_tree.setRootIsDecorated(False)
        self.inbox_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.inbox_tree.itemDoubleClicked.connect(lambda _i, _c: self.send_selected())
        self.inbox_tree.itemSelectionChanged.connect(self._update_inbox_buttons)
        il.addWidget(self.inbox_tree, 1)
        self.inbox_empty = QLabel("📭 Inbox zero. Capture anything with the ⚡ shortcut - from any app.")
        self.inbox_empty.setAlignment(Qt.AlignCenter)
        self.inbox_empty.setStyleSheet("color: #64748B; padding: 24px;")
        il.addWidget(self.inbox_empty)
        btns = QHBoxLayout()
        il.addLayout(btns)
        btns.addStretch()
        self.send_btn = QPushButton("📋 Send to Board...")
        self.send_btn.setObjectName("PrimaryButton")
        self.send_btn.clicked.connect(self.send_selected)
        btns.addWidget(self.send_btn)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setObjectName("DangerButton")
        self.delete_btn.clicked.connect(self.delete_selected)
        btns.addWidget(self.delete_btn)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def show_tab(self, which: str) -> None:
        self.tabs.setCurrentIndex(1 if which == "inbox" else 0)

    def refresh(self) -> None:
        self._fill_people()
        self.refresh_focus()
        self.refresh_inbox()

    def _fill_people(self) -> None:
        current = self.person_combo.currentData()
        self.person_combo.blockSignals(True)
        self.person_combo.clear()
        me = self.service.actor
        self.person_combo.addItem(f"👤 Me ({me})", me)
        for p in self.service.get_people():
            if p != me:
                self.person_combo.addItem(p, p)
        self.person_combo.setCurrentIndex(max(0, self.person_combo.findData(current)))
        self.person_combo.blockSignals(False)

    def refresh_focus(self) -> None:
        person = self.person_combo.currentData() or self.service.actor
        self.tasks = self.service.get_focus_tasks(person, include_unassigned=self.unassigned_chk.isChecked())
        # Critical-path flags (one CPM pass per board involved)
        critical: set = set()
        for board_id in {t["board_id"] for t in self.tasks}:
            try:
                critical |= set(self.scheduler.analyze(board_id).critical_ids)
            except Exception:
                pass
        for t in self.tasks:
            t["is_critical"] = t["id"] in critical
        self._render_focus()

    def _render_focus(self) -> None:
        needle = self.search.text().strip().lower()
        tasks = [t for t in self.tasks if not needle or needle in t["title"].lower()
                 or needle in t["board_name"].lower()]
        now = datetime.now()
        groups = group_tasks(tasks, now)
        self.tree.blockSignals(True)
        self.tree.clear()
        bold = QFont()
        bold.setBold(True)
        for key, label, color in GROUPS:
            items = groups[key]
            if not items:
                continue
            head = QTreeWidgetItem([f"{label}  ({len(items)})"])
            head.setFirstColumnSpanned(True)
            head.setFont(0, bold)
            head.setForeground(0, QColor(color))
            head.setFlags(Qt.ItemIsEnabled)
            self.tree.addTopLevelItem(head)
            for t in items:
                flags = "".join([
                    " ⏱" if t["id"] == self.running_task_id else "",
                    " 🔥" if t.get("is_critical") else "",
                    " 🔒" if t.get("is_blocked") else "",
                ])
                name = ("◆ " if t.get("is_milestone") else "") + t["title"] + flags
                try:
                    pri = Priority(t["priority"])
                except ValueError:
                    pri = Priority.MEDIUM
                row = QTreeWidgetItem([name, f"{t['board_name']}  ·  {t['workspace_name']}",
                                       due_text(t.get("due_date"), now), f"{pri.icon_char} {pri.display_name}"])
                row.setData(0, Qt.UserRole, t["id"])
                row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
                row.setCheckState(0, Qt.Unchecked)
                row.setForeground(3, QColor(pri.color))
                if key == "overdue":
                    row.setForeground(2, QColor("#F87171"))
                row.setToolTip(0, t["title"] + (f"\nAssigned to {t['assignee']}" if t.get("assignee") else
                                                 "\nUnassigned"))
                self.tree.addTopLevelItem(row)
        self.tree.blockSignals(False)
        n_over, n_today = len(groups["overdue"]), len(groups["today"])
        boards = len({t["board_id"] for t in tasks})
        parts = [f"<b>{len(tasks)}</b> open task{'s' if len(tasks) != 1 else ''} across <b>{boards}</b> "
                 f"board{'s' if boards != 1 else ''}"]
        if n_over:
            parts.append(f"<span style='color:#F87171'>{n_over} overdue</span>")
        if n_today:
            parts.append(f"<span style='color:#FBBF24'>{n_today} due today</span>")
        crit = sum(1 for t in tasks if t.get("is_critical"))
        if crit:
            parts.append(f"<span style='color:#F87171'>🔥 {crit} on a critical path</span>")
        self.summary.setText(" &nbsp;·&nbsp; ".join(parts) if tasks else
                             "🎉 Nothing assigned - enjoy the calm, or capture something new.")

    def refresh_inbox(self) -> None:
        self.inbox = self.service.get_inbox_items()
        boards = {b.id: b.name for ws in self.service.get_workspaces() for b in ws.boards}
        self.inbox_tree.clear()
        for item in self.inbox:
            details = []
            if item["due_date"]:
                details.append("📅 " + cap.describe_due(datetime.fromisoformat(item["due_date"])))
            if item["priority"]:
                details.append(Priority(item["priority"]).display_name)
            details += [f"#{l}" for l in item["labels"]]
            if item["assignee"]:
                details.append(f"@{item['assignee']}")
            if item["notes"]:
                details.append("📝")
            captured = datetime.fromisoformat(item["created_at"]).strftime("%b %d %H:%M")
            row = QTreeWidgetItem([item["title"], "  ".join(details), boards.get(item["board_hint_id"], ""),
                                   captured])
            row.setData(0, Qt.UserRole, item["id"])
            row.setToolTip(0, item["raw_text"])
            self.inbox_tree.addTopLevelItem(row)
        self.tabs.setTabText(1, f"📥 Inbox ({len(self.inbox)})" if self.inbox else "📥 Inbox")
        self.inbox_empty.setVisible(not self.inbox)
        self.inbox_tree.setVisible(bool(self.inbox))
        self._update_inbox_buttons()

    # ------------------------------------------------------------------
    # Focus actions
    # ------------------------------------------------------------------
    def _task_id(self, item: Optional[QTreeWidgetItem]) -> Optional[int]:
        return item.data(0, Qt.UserRole) if item is not None else None

    def _task(self, task_id: int) -> Optional[Dict[str, Any]]:
        return next((t for t in self.tasks if t["id"] == task_id), None)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        task_id = self._task_id(item)
        if (column == 0 and task_id is not None and item.checkState(0) == Qt.Checked
                and item.flags() & Qt.ItemIsUserCheckable):
            # Defer: completing refreshes this tree (here and via the main window), and clearing
            # it while Qt is still inside setData() for `item` is a use-after-free segfault.
            blocked = self.tree.blockSignals(True)  # setFlags() emits itemChanged too
            item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)  # no double-completion meanwhile
            self.tree.blockSignals(blocked)
            QTimer.singleShot(0, self, partial(self._complete_task, task_id))

    def _complete_task(self, task_id: int) -> None:
        app_signals.task_completed_toggled.emit(task_id, True)  # the window handles recurrence etc.
        self.refresh_focus()

    def _open_item(self, item: QTreeWidgetItem, _column: int) -> None:
        task_id = self._task_id(item)
        if task_id is not None:
            app_signals.task_edit_requested.emit(task_id)

    def reschedule(self, task_id: int, when: Optional[date]) -> None:
        task = self.service.get_task(task_id)
        if task is None:
            return
        due = None
        if when is not None:
            keep = task.due_date.time() if task.due_date else time(17, 0)
            due = datetime.combine(when, keep)
        self.service.update_task(task_id, due_date=due)
        self.refresh_focus()
        app_signals.board_refresh_requested.emit()

    def _task_menu(self, pos) -> None:
        task_id = self._task_id(self.tree.itemAt(pos))
        task = self._task(task_id) if task_id is not None else None
        if task is None:
            return
        today = date.today()
        menu = QMenu(self)
        menu.addAction("✏ Open Task", lambda: app_signals.task_edit_requested.emit(task_id))
        menu.addAction(f"📋 Go to '{task['board_name']}'".replace("&", "&&"),
                       lambda: self.open_board_requested.emit(task["board_id"]))
        menu.addSeparator()
        menu.addAction("▶ Start Stopwatch", lambda: app_signals.timer_start_requested.emit(task_id, "stopwatch"))
        menu.addAction("🍅 Start Pomodoro", lambda: app_signals.timer_start_requested.emit(task_id, "pomodoro"))
        sub = menu.addMenu("📅 Reschedule")
        sub.addAction("Today", lambda: self.reschedule(task_id, today))
        sub.addAction("Tomorrow", lambda: self.reschedule(task_id, today + timedelta(days=1)))
        sub.addAction("Next Monday", lambda: self.reschedule(task_id, today + timedelta(days=7 - today.weekday())))
        sub.addAction("In a week", lambda: self.reschedule(task_id, today + timedelta(days=7)))
        sub.addSeparator()
        sub.addAction("No due date", lambda: self.reschedule(task_id, None))
        menu.addSeparator()
        menu.addAction("✓ Mark Complete", partial(self._complete_task, task_id))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------
    # Inbox actions
    # ------------------------------------------------------------------
    def add_from_input(self) -> Optional[dict]:
        text = self.inbox_input.text().strip()
        if not text:
            return None
        try:
            saved = cap.commit_capture(self.service, text, source="inbox")
        except ValueError:
            return None
        self.inbox_input.clear()
        self.refresh_inbox()
        if saved["kind"] == "task":
            self.refresh_focus()
            app_signals.board_refresh_requested.emit()
        self.inbox_changed.emit()
        return saved

    def selected_inbox_items(self) -> List[Dict[str, Any]]:
        ids = {it.data(0, Qt.UserRole) for it in self.inbox_tree.selectedItems()}
        return [i for i in self.inbox if i["id"] in ids]

    def _update_inbox_buttons(self) -> None:
        has = bool(self.selected_inbox_items())
        self.send_btn.setEnabled(has)
        self.delete_btn.setEnabled(has)

    def send_selected(self, dialog_exec=None) -> List[int]:
        items = self.selected_inbox_items()
        if not items:
            return []
        dlg = TriageDialog(self.service, items, self)
        accepted = dialog_exec(dlg) if dialog_exec else dlg.exec()
        created = list(dlg.created) if accepted else []
        dlg.deleteLater()
        if created:
            self.refresh()
            self.inbox_changed.emit()
            app_signals.board_refresh_requested.emit()
        return created

    def delete_selected(self, confirm: bool = True) -> None:
        items = self.selected_inbox_items()
        if not items:
            return
        if confirm and QMessageBox.question(
                self, "Delete", f"Delete {len(items)} inbox item{'s' if len(items) != 1 else ''}?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        for item in items:
            self.service.delete_inbox_item(item["id"])
        self.refresh_inbox()
        self.inbox_changed.emit()

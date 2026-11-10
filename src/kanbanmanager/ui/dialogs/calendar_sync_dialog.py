"""
Calendar Sync dialog: calendar subscriptions (iCalendar feeds & CalDAV), the live
feed calendar apps can subscribe to, and .ics file import/export.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import calendar_sync as cs
from kanbanmanager.core.ical import ICalError
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.calendar_feed import (
    CalendarFeedServer,
    SyncRunner,
    load_feed_settings,
    new_token,
    save_feed_settings,
)
from kanbanmanager.ui.styles import CHECKBOX_QSS

STATUS_ICON = {"success": "✅", "partial": "⚠️", "failed": "❌", None: "·"}


def _when(iso: Optional[str]) -> str:
    if not iso:
        return "never"
    dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone()
    return dt.strftime("%b %d, %H:%M")


def fill_boards(combo: QComboBox, service: DatabaseService, selected: Optional[int] = None) -> None:
    combo.clear()
    workspaces = service.get_workspaces()
    for ws in workspaces:
        for b in ws.boards:
            if not b.is_archived:
                combo.addItem(f"📋 {b.name}" + (f"  ·  {ws.name}" if len(workspaces) > 1 else ""), b.id)
    idx = combo.findData(selected)
    combo.setCurrentIndex(max(0, idx))


def fill_columns(combo: QComboBox, service: DatabaseService, board_id: Optional[int],
                 selected: Optional[int] = None, default_text: str = "(the board's To Do column)") -> None:
    combo.clear()
    combo.addItem(default_text, None)
    if board_id:
        for c in service.get_columns_for_board(board_id):
            combo.addItem(c.name, c.id)
    combo.setCurrentIndex(max(0, combo.findData(selected)))


# ---------------------------------------------------------------------------
# Subscription editor
# ---------------------------------------------------------------------------
class SubscriptionDialog(QDialog):
    def __init__(self, service: DatabaseService, sub: Optional[Dict[str, Any]] = None,
                 parent: Optional[QWidget] = None, board_id: Optional[int] = None):
        super().__init__(parent)
        self.service = service
        self.sub = sub
        self.saved: Optional[Dict[str, Any]] = None
        self.setWindowTitle("Edit Calendar" if sub else "Add Calendar")
        self.setMinimumWidth(620)
        self.setStyleSheet(CHECKBOX_QSS)
        opts = cs.options_for(sub or {})
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        form = QFormLayout()
        root.addLayout(form)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("📅 iCalendar feed (read-only: Google, Outlook, any .ics link or file)", "ics")
        self.kind_combo.addItem("🔄 CalDAV calendar (two-way: Nextcloud, iCloud, Fastmail, Radicale …)", "caldav")
        self.kind_combo.currentIndexChanged.connect(self._on_kind)
        form.addRow("Type:", self.kind_combo)
        self.name_input = QLineEdit((sub or {}).get("name", ""))
        self.name_input.setPlaceholderText("e.g. Work calendar")
        form.addRow("Name *", self.name_input)
        self.url_input = QLineEdit((sub or {}).get("url", ""))
        self.url_input.setPlaceholderText("https://…/basic.ics, webcal://…, or a file path")
        form.addRow("Address *", self.url_input)
        self.user_input = QLineEdit((sub or {}).get("username", ""))
        form.addRow("User name:", self.user_input)
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setPlaceholderText("(unchanged)" if (sub or {}).get("has_password") else
                                               "app password recommended")
        form.addRow("Password:", self.password_input)
        test_row = QHBoxLayout()
        form.addRow("", test_row)
        self.test_btn = QPushButton("Test Connection")
        self.test_btn.clicked.connect(self.test_connection)
        test_row.addWidget(self.test_btn)
        self.calendars_combo = QComboBox()
        self.calendars_combo.setVisible(False)
        self.calendars_combo.activated.connect(self._on_calendar_picked)
        test_row.addWidget(self.calendars_combo, 1)
        self.test_lbl = QLabel("")
        self.test_lbl.setWordWrap(True)
        self.test_lbl.setStyleSheet("color: #94A3B8;")
        form.addRow("", self.test_lbl)

        self.board_combo = QComboBox()
        fill_boards(self.board_combo, service, (sub or {}).get("board_id") or board_id)
        self.board_combo.currentIndexChanged.connect(self._on_board)
        form.addRow("Board:", self.board_combo)
        self.column_combo = QComboBox()
        fill_columns(self.column_combo, service, self.board_combo.currentData(), (sub or {}).get("column_id"))
        form.addRow("New items go to:", self.column_combo)
        self.direction_combo = QComboBox()
        self.direction_combo.addItem("⇄ Two-way: changes on either side are copied to the other", "two_way")
        self.direction_combo.addItem("⇩ Calendar → board only", "pull")
        self.direction_combo.addItem("⇧ Board → calendar only", "push")
        form.addRow("Sync:", self.direction_combo)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(0, 1440)
        self.interval_spin.setSuffix(" min")
        self.interval_spin.setSpecialValueText("manually only")
        self.interval_spin.setValue(int((sub or {}).get("interval_minutes", 30)))
        form.addRow("Sync every:", self.interval_spin)

        more = QGroupBox("Options")
        root.addWidget(more)
        mf = QFormLayout(more)
        self.component_combo = QComboBox()
        self.component_combo.addItem("To-dos (VTODO) - shows in Reminders / Tasks apps", "VTODO")
        self.component_combo.addItem("Events (VEVENT) - shows in the calendar itself", "VEVENT")
        self.component_combo.setCurrentIndex(max(0, self.component_combo.findData(opts["component"])))
        mf.addRow("CalDAV items:", self.component_combo)
        inc = QHBoxLayout()
        mf.addRow("Import:", inc)
        self.events_chk = QCheckBox("events")
        self.events_chk.setChecked(bool(opts["include_events"]))
        inc.addWidget(self.events_chk)
        self.todos_chk = QCheckBox("to-dos")
        self.todos_chk.setChecked(bool(opts["include_todos"]))
        inc.addWidget(self.todos_chk)
        self.completed_chk = QCheckBox("already completed items")
        self.completed_chk.setChecked(bool(opts["import_completed"]))
        inc.addWidget(self.completed_chk)
        inc.addStretch()
        window = QHBoxLayout()
        mf.addRow("Events from:", window)
        self.past_spin = QSpinBox()
        self.past_spin.setRange(0, 3650)
        self.past_spin.setValue(int(opts["days_past"]))
        self.past_spin.setSuffix(" days ago")
        window.addWidget(self.past_spin)
        window.addWidget(QLabel("to"))
        self.future_spin = QSpinBox()
        self.future_spin.setRange(1, 3650)
        self.future_spin.setValue(int(opts["days_future"]))
        self.future_spin.setSuffix(" days ahead")
        window.addWidget(self.future_spin)
        window.addStretch()
        self.deleted_combo = QComboBox()
        self.deleted_combo.addItem("archive the task", "archive")
        self.deleted_combo.addItem("delete the task", "delete")
        self.deleted_combo.addItem("keep the task", "keep")
        self.deleted_combo.setCurrentIndex(max(0, self.deleted_combo.findData(opts["remote_deleted"])))
        mf.addRow("Removed from the calendar:", self.deleted_combo)
        self.conflict_combo = QComboBox()
        self.conflict_combo.addItem("the newer change wins", "newest")
        self.conflict_combo.addItem("the calendar wins", "remote")
        self.conflict_combo.addItem("the board wins", "local")
        self.conflict_combo.setCurrentIndex(max(0, self.conflict_combo.findData(opts["conflict"])))
        mf.addRow("Changed on both sides:", self.conflict_combo)
        push = QHBoxLayout()
        mf.addRow("Send:", push)
        self.undated_chk = QCheckBox("tasks without a due date")
        self.undated_chk.setChecked(bool(opts["push_undated"]))
        push.addWidget(self.undated_chk)
        self.push_done_chk = QCheckBox("tasks that are already done")
        self.push_done_chk.setChecked(bool(opts["push_completed"]))
        push.addWidget(self.push_done_chk)
        push.addStretch()

        self.error_lbl = QLabel("")
        self.error_lbl.setStyleSheet("color: #F87171;")
        self.error_lbl.setWordWrap(True)
        root.addWidget(self.error_lbl)
        btns = QHBoxLayout()
        root.addLayout(btns)
        btns.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("Save")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self.save)
        btns.addWidget(save)

        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData((sub or {}).get("kind", "ics"))))
        self.direction_combo.setCurrentIndex(max(0, self.direction_combo.findData((sub or {}).get("direction",
                                                                                                     "two_way"))))
        self._on_kind()

    def _on_kind(self, *_args) -> None:
        caldav = self.kind_combo.currentData() == "caldav"
        self.direction_combo.setEnabled(caldav)
        if not caldav:  # feeds are read-only; remember the choice for switching back
            if self.direction_combo.currentData() != "pull":
                self._caldav_direction = self.direction_combo.currentData()
            self.direction_combo.setCurrentIndex(self.direction_combo.findData("pull"))
        elif getattr(self, "_caldav_direction", None):
            self.direction_combo.setCurrentIndex(self.direction_combo.findData(self._caldav_direction))
            self._caldav_direction = None
        for w in (self.component_combo, self.conflict_combo, self.undated_chk, self.push_done_chk):
            w.setEnabled(caldav)
        for w in (self.events_chk, self.todos_chk, self.past_spin, self.future_spin):
            w.setEnabled(not caldav)
        self.url_input.setPlaceholderText("https://cloud.example.com/remote.php/dav/ (server, or a calendar's URL)"
                                          if caldav else "https://…/basic.ics, webcal://…, or a file path")

    def _on_board(self, *_args) -> None:
        fill_columns(self.column_combo, self.service, self.board_combo.currentData())

    def _password(self) -> Optional[str]:
        typed = self.password_input.text()
        if typed:
            return typed
        if self.sub and self.sub.get("has_password"):
            stored = self.service.get_calendar_subscription(self.sub["id"], include_secret=True)
            return stored.get("password") or None
        return None

    def test_connection(self, _checked: bool = False, transport=None) -> bool:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, message, found = cs.check_connection(self.kind_combo.currentData(), self.url_input.text(),
                                                     self.user_input.text().strip() or None, self._password(),
                                                     transport)
        finally:
            QApplication.restoreOverrideCursor()
        self.test_lbl.setText(("✅ " if ok else "❌ ") + message)
        self.calendars_combo.clear()
        self.calendars_combo.setVisible(len(found) > 1)
        for c in found:
            self.calendars_combo.addItem(f"{c['name']} ({', '.join(c['components'])})", c)
        if len(found) == 1:
            self._use_calendar(found[0])
        elif found:
            self.calendars_combo.setCurrentIndex(0)
            self._use_calendar(found[0])
        return ok

    def _on_calendar_picked(self, index: int) -> None:
        self._use_calendar(self.calendars_combo.itemData(index))

    def _use_calendar(self, cal: Dict[str, Any]) -> None:
        self.url_input.setText(cal["url"])
        if not self.name_input.text().strip():
            self.name_input.setText(cal["name"])
        comp = "VTODO" if "VTODO" in cal["components"] else "VEVENT"
        self.component_combo.setCurrentIndex(self.component_combo.findData(comp))

    def values(self) -> Dict[str, Any]:
        return {
            "name": self.name_input.text(), "kind": self.kind_combo.currentData(), "url": self.url_input.text(),
            "username": self.user_input.text(), "password": self.password_input.text() or None,
            "board_id": self.board_combo.currentData(), "column_id": self.column_combo.currentData(),
            "direction": self.direction_combo.currentData(), "interval_minutes": self.interval_spin.value(),
            "options": {
                "component": self.component_combo.currentData(), "include_events": self.events_chk.isChecked(),
                "include_todos": self.todos_chk.isChecked(), "import_completed": self.completed_chk.isChecked(),
                "days_past": self.past_spin.value(), "days_future": self.future_spin.value(),
                "remote_deleted": self.deleted_combo.currentData(), "conflict": self.conflict_combo.currentData(),
                "push_undated": self.undated_chk.isChecked(), "push_completed": self.push_done_chk.isChecked(),
            },
        }

    def save(self, _checked: bool = False) -> Optional[Dict[str, Any]]:
        values = self.values()
        url = values["url"].strip().lower()
        if values["password"] and url.startswith("http://") and not url.startswith(("http://127.", "http://localhost")):
            if QMessageBox.question(self, "Unencrypted connection", "This address uses http://, so your password "
                                    "would be sent unencrypted. Save anyway?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return None
        try:
            self.saved = self.service.save_calendar_subscription(values, self.sub["id"] if self.sub else None)
        except ValueError as exc:
            self.error_lbl.setText(f"⚠ {exc}")
            return None
        self.accept()
        return self.saved


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------
class CalendarSyncDialog(QDialog):
    def __init__(self, service: DatabaseService, feed_server: Optional[CalendarFeedServer] = None,
                 runner: Optional[SyncRunner] = None, board_id: Optional[int] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.feed_server = feed_server or CalendarFeedServer(service, self)
        self.runner = runner or SyncRunner(service, self)
        self.board_id = board_id
        self.changed = False
        self.last_import: List[int] = []
        self.setWindowTitle("Calendar Sync")
        self.setMinimumSize(860, 560)
        self.setStyleSheet(CHECKBOX_QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._build_subscriptions()
        self._build_feed()
        self._build_files()
        bottom = QHBoxLayout()
        root.addLayout(bottom)
        bottom.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        self.runner.finished.connect(self._on_sync_finished)
        self.reload()

    # Subscriptions ---------------------------------------------------------
    def _build_subscriptions(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        intro = QLabel("Bring calendar entries onto a board as tasks, or keep a board and a CalDAV calendar "
                       "in sync both ways. Syncs run in the background while KanbanManager is open.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #94A3B8;")
        lay.addWidget(intro)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", "Calendar", "Board", "Sync", "Every", "Last sync"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemDoubleClicked.connect(self.edit_selected)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.table, 1)
        btns = QHBoxLayout()
        lay.addLayout(btns)
        self.add_btn = QPushButton("＋ Add Calendar…")
        self.add_btn.setObjectName("PrimaryButton")
        self.add_btn.clicked.connect(self.add_subscription)
        btns.addWidget(self.add_btn)
        self.edit_btn = QPushButton("Edit…")
        self.edit_btn.clicked.connect(self.edit_selected)
        btns.addWidget(self.edit_btn)
        self.sync_btn = QPushButton("🔄 Sync Now")
        self.sync_btn.clicked.connect(self.sync_selected)
        btns.addWidget(self.sync_btn)
        self.history_btn = QPushButton("History")
        self.history_btn.clicked.connect(self.show_history)
        btns.addWidget(self.history_btn)
        btns.addStretch()
        self.delete_btn = QPushButton("Remove")
        self.delete_btn.setObjectName("DangerButton")
        self.delete_btn.clicked.connect(self.delete_selected)
        btns.addWidget(self.delete_btn)
        self.sync_status = QLabel("")
        self.sync_status.setStyleSheet("color: #94A3B8;")
        self.sync_status.setWordWrap(True)
        lay.addWidget(self.sync_status)
        self.tabs.addTab(page, "🔄 Subscriptions")

    def reload(self) -> None:
        self.subs = self.service.get_calendar_subscriptions()
        boards = {b.id: b.name for ws in self.service.get_workspaces() for b in ws.boards}
        selected = self.selected()
        self.table.setRowCount(len(self.subs))
        arrows = {"two_way": "⇄ both ways", "pull": "⇩ to board", "push": "⇧ to calendar"}
        for r, s in enumerate(self.subs):
            kind = "CalDAV" if s["kind"] == "caldav" else "Feed"
            cells = [STATUS_ICON.get(s["last_status"], "·"), f"{s['name']}  ({kind})", boards.get(s["board_id"], "?"),
                     arrows.get(s["direction"], s["direction"]),
                     f"{s['interval_minutes']} min" if s["interval_minutes"] else "manual",
                     f"{_when(s['last_sync_at'])}: {s['last_message']}" if s["last_sync_at"] else "never"]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, s["id"])
                if c == 5:
                    item.setToolTip(s["last_message"])
                self.table.setItem(r, c, item)
        if selected:
            ids = [s["id"] for s in self.subs]
            if selected["id"] in ids:
                self.table.selectRow(ids.index(selected["id"]))
        elif self.subs:
            self.table.selectRow(0)
        self._update_buttons()
        self._fill_feed()

    def selected(self) -> Optional[Dict[str, Any]]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if rows and 0 <= rows[0].row() < len(getattr(self, "subs", [])):
            return self.subs[rows[0].row()]
        return None

    def _update_buttons(self) -> None:
        has = self.selected() is not None
        for b in (self.edit_btn, self.sync_btn, self.history_btn, self.delete_btn):
            b.setEnabled(has)
        if self.runner.busy:
            self.sync_btn.setEnabled(False)

    def add_subscription(self, _checked: bool = False) -> Optional[Dict[str, Any]]:
        dlg = SubscriptionDialog(self.service, None, self, board_id=self.board_id)
        saved = dlg.saved if dlg.exec() else None
        dlg.deleteLater()
        if saved:
            self.changed = True
            self.reload()
            self.sync_ids([saved["id"]])
        return saved

    def edit_selected(self, *_args) -> Optional[Dict[str, Any]]:
        sub = self.selected()
        if sub is None:
            return None
        dlg = SubscriptionDialog(self.service, sub, self)
        saved = dlg.saved if dlg.exec() else None
        dlg.deleteLater()
        if saved:
            self.reload()
        return saved

    def sync_selected(self, _checked: bool = False) -> None:
        sub = self.selected()
        if sub:
            self.sync_ids([sub["id"]])

    def sync_ids(self, ids: List[int]) -> None:
        self.sync_status.setText("🔄 Syncing…")
        self.sync_btn.setEnabled(False)
        if not self.runner.run(ids):
            self.sync_status.setText("🔄 A sync is already running - this one follows right after it")

    def _on_sync_finished(self, reports) -> None:
        self.changed = True
        self.sync_status.setText("  ·  ".join(f"{r.name}: {r.describe()}" for r in reports) or "Done")
        self.reload()

    def show_history(self, _checked: bool = False) -> str:
        sub = self.selected()
        if sub is None:
            return ""
        runs = self.service.get_sync_runs(sub["id"])
        lines = []
        for r in runs:
            counts = ", ".join(f"{r[k]} {k}" for k in ("created", "updated", "removed", "pushed", "conflicts") if r[k])
            lines.append(f"{STATUS_ICON.get(r['status'])} {_when(r['started_at'])}  {counts or 'no changes'}\n"
                         f"      {r['message']}")
        text = "\n".join(lines) or "No syncs yet."
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Sync history - {sub['name']}")
        dlg.resize(640, 420)
        lay = QVBoxLayout(dlg)
        box = QPlainTextEdit(text)
        box.setReadOnly(True)
        lay.addWidget(box)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        lay.addWidget(close)
        dlg.exec()
        dlg.deleteLater()
        return text

    def delete_selected(self, _checked: bool = False, confirm: bool = True) -> bool:
        sub = self.selected()
        if sub is None:
            return False
        if confirm and QMessageBox.question(self, "Remove Calendar", f"Stop syncing '{sub['name']}'? Tasks it "
                                            "created stay on the board.", QMessageBox.Yes | QMessageBox.No,
                                            QMessageBox.No) != QMessageBox.Yes:
            return False
        self.service.delete_calendar_subscription(sub["id"])
        self.changed = True
        self.reload()
        return True

    # Live feed --------------------------------------------------------------------
    def _build_feed(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        intro = QLabel("Publish your tasks as a calendar other apps can subscribe to (Apple Calendar, Outlook, "
                       "Thunderbird, GNOME Calendar …). It updates whenever they refresh, while KanbanManager "
                       "is running. The address contains a secret - treat it like a password.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #94A3B8;")
        lay.addWidget(intro)
        form = QFormLayout()
        lay.addLayout(form)
        self.feed_enabled = QCheckBox("Publish the live calendar feed")
        form.addRow("", self.feed_enabled)
        self.feed_port = QSpinBox()
        self.feed_port.setRange(1024, 65535)
        form.addRow("Port:", self.feed_port)
        self.feed_lan = QCheckBox("Allow other devices on my network (phones, tablets)")
        form.addRow("", self.feed_lan)
        self.feed_kind = QComboBox()
        self.feed_kind.addItem("Events on the due date (every calendar app shows these)", "VEVENT")
        self.feed_kind.addItem("To-dos (task apps such as Reminders or Thunderbird Tasks)", "VTODO")
        form.addRow("Publish as:", self.feed_kind)
        self.feed_style = QComboBox()
        self.feed_style.addItem("All-day events", "all_day")
        self.feed_style.addItem("At the due time", "timed")
        form.addRow("Events:", self.feed_style)
        self.feed_completed = QCheckBox("Include completed tasks")
        form.addRow("", self.feed_completed)
        self.feed_alarm = QSpinBox()
        self.feed_alarm.setRange(0, 7 * 24 * 60)
        self.feed_alarm.setSuffix(" min before")
        self.feed_alarm.setSpecialValueText("no reminders")
        form.addRow("Reminders:", self.feed_alarm)
        row = QHBoxLayout()
        lay.addLayout(row)
        self.feed_apply = QPushButton("Apply")
        self.feed_apply.setObjectName("PrimaryButton")
        self.feed_apply.clicked.connect(self.apply_feed)
        row.addWidget(self.feed_apply)
        self.feed_token_btn = QPushButton("New Secret Address")
        self.feed_token_btn.setToolTip("Stops every existing subscription from working")
        self.feed_token_btn.clicked.connect(self.regenerate_token)
        row.addWidget(self.feed_token_btn)
        row.addStretch()
        self.feed_status = QLabel("")
        self.feed_status.setWordWrap(True)
        lay.addWidget(self.feed_status)
        self.feed_table = QTableWidget(0, 3)
        self.feed_table.setHorizontalHeaderLabels(["Feed", "Address", ""])
        self.feed_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.feed_table.verticalHeader().setVisible(False)
        self.feed_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.feed_table, 1)
        self.tabs.addTab(page, "📡 Live Feed")

    def _fill_feed(self) -> None:
        s = load_feed_settings(self.service)
        self.feed_enabled.setChecked(bool(s["enabled"]))
        self.feed_port.setValue(int(s["port"]))
        self.feed_lan.setChecked(bool(s["lan"]))
        self.feed_kind.setCurrentIndex(max(0, self.feed_kind.findData(s["kind"])))
        self.feed_style.setCurrentIndex(max(0, self.feed_style.findData(s["event_style"])))
        self.feed_completed.setChecked(bool(s["include_completed"]))
        self.feed_alarm.setValue(int(s["alarm_minutes"] or 0))
        running = self.feed_server.running
        self.feed_status.setText(("🟢 " if running else "⚪ ") + self.feed_server.status)
        if not running:
            self.feed_server.settings = s
        urls = self.feed_server.urls()
        self.feed_table.setRowCount(len(urls))
        for r, (label, http_url, webcal) in enumerate(urls):
            self.feed_table.setItem(r, 0, QTableWidgetItem(label))
            item = QTableWidgetItem(webcal)
            item.setToolTip(f"{webcal}\n{http_url}")
            self.feed_table.setItem(r, 1, item)
            copy = QPushButton("Copy")
            copy.clicked.connect(partial(self.copy_url, webcal))
            self.feed_table.setCellWidget(r, 2, copy)
        self.feed_table.setEnabled(running)

    def feed_values(self) -> Dict[str, Any]:
        return {"enabled": self.feed_enabled.isChecked(), "port": self.feed_port.value(),
                "lan": self.feed_lan.isChecked(), "kind": self.feed_kind.currentData(),
                "event_style": self.feed_style.currentData(), "include_completed": self.feed_completed.isChecked(),
                "alarm_minutes": self.feed_alarm.value()}

    def apply_feed(self, _checked: bool = False) -> bool:
        settings = save_feed_settings(self.service, self.feed_values())
        ok = True
        if settings["enabled"]:
            ok, message = self.feed_server.start(settings)
        else:
            self.feed_server.stop()
            self.feed_server.settings = settings
        self._fill_feed()
        return ok

    def regenerate_token(self, _checked: bool = False, confirm: bool = True) -> None:
        if confirm and QMessageBox.question(self, "New Secret Address", "Calendars subscribed to the current "
                                            "address will stop updating until you subscribe again. Continue?",
                                            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        settings = new_token(self.service)
        if self.feed_server.running:
            self.feed_server.start(settings)
        else:
            self.feed_server.settings = settings
        self._fill_feed()

    def copy_url(self, url: str, _checked: bool = False) -> None:
        QGuiApplication.clipboard().setText(url)
        self.feed_status.setText(f"📋 Copied {url}")

    # Files ---------------------------------------------------------------------------
    def _build_files(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        export = QGroupBox("Export an .ics file")
        lay.addWidget(export)
        ef = QFormLayout(export)
        self.export_scope = QComboBox()
        self.export_scope.addItem("This board", "board")
        self.export_scope.addItem("All boards", "all")
        self.export_scope.addItem("My tasks (all boards)", "mine")
        ef.addRow("Tasks:", self.export_scope)
        self.export_kind = QComboBox()
        self.export_kind.addItem("Events on the due date", "VEVENT")
        self.export_kind.addItem("To-dos", "VTODO")
        ef.addRow("As:", self.export_kind)
        self.export_completed = QCheckBox("Include completed tasks")
        ef.addRow("", self.export_completed)
        self.export_btn = QPushButton("Export .ics…")
        self.export_btn.clicked.connect(self.export_file)
        ef.addRow("", self.export_btn)

        imp = QGroupBox("Import an .ics file")
        lay.addWidget(imp)
        imf = QFormLayout(imp)
        self.import_board = QComboBox()
        fill_boards(self.import_board, self.service, self.board_id)
        self.import_board.currentIndexChanged.connect(
            lambda _i: fill_columns(self.import_column, self.service, self.import_board.currentData()))
        imf.addRow("Into board:", self.import_board)
        self.import_column = QComboBox()
        fill_columns(self.import_column, self.service, self.import_board.currentData())
        imf.addRow("Column:", self.import_column)
        self.import_completed = QCheckBox("Include items that are already completed")
        imf.addRow("", self.import_completed)
        row = QHBoxLayout()
        imf.addRow("", row)
        self.import_btn = QPushButton("Import .ics…")
        self.import_btn.clicked.connect(self.import_file)
        row.addWidget(self.import_btn)
        self.undo_btn = QPushButton("Undo Import")
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self.undo_import)
        row.addWidget(self.undo_btn)
        row.addStretch()
        self.files_status = QLabel("Events become tasks due on their date. Importing the same file again "
                                   "updates those tasks instead of duplicating them.")
        self.files_status.setWordWrap(True)
        self.files_status.setStyleSheet("color: #94A3B8;")
        lay.addWidget(self.files_status)
        lay.addStretch()
        self.tabs.addTab(page, "📁 .ics Files")

    def export_file(self, _checked: bool = False, path: Optional[str] = None) -> Optional[Path]:
        scope = self.export_scope.currentData()
        if scope == "board" and not self.board_id:
            scope = "all"
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Export Calendar", "kanban-tasks.ics", "iCalendar (*.ics)")
        if not path:
            return None
        text = cs.export_calendar(self.service, [self.board_id] if scope == "board" else None,
                                  person=self.service.actor if scope == "mine" else None,
                                  kind=self.export_kind.currentData(),
                                  include_completed=self.export_completed.isChecked())
        dest = Path(path)
        dest.write_text(text, encoding="utf-8", newline="")
        n = text.count("BEGIN:VEVENT") + text.count("BEGIN:VTODO")
        self.files_status.setText(f"✅ Exported {n} task{'s' if n != 1 else ''} to {dest.name}")
        return dest

    def import_file(self, _checked: bool = False, path: Optional[str] = None) -> Optional[cs.ImportSummary]:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import Calendar", "", "iCalendar (*.ics *.ical *.ifb)")
        if not path:
            return None
        if self.import_board.currentData() is None:
            self.files_status.setText("❌ Choose the board to import into")
            return None
        try:
            text = cs.fetch_text(path)
            summary = cs.import_calendar(self.service, text, self.import_board.currentData(),
                                         self.import_column.currentData(),
                                         include_completed=self.import_completed.isChecked())
        except (cs.SyncError, ICalError) as exc:
            self.files_status.setText(f"❌ {exc}")
            return None
        self.last_import = summary.task_ids
        self.undo_btn.setEnabled(bool(summary.task_ids))
        self.changed = True
        msg = f"✅ {summary.describe()}"
        if summary.warnings:
            msg += "  ·  ⚠ " + "; ".join(summary.warnings[:3])
        self.files_status.setText(msg)
        return summary

    def undo_import(self, _checked: bool = False) -> int:
        n = 0
        for task_id in self.last_import:
            if self.service.delete_task(task_id):
                n += 1
        self.last_import = []
        self.undo_btn.setEnabled(False)
        self.changed = True
        self.files_status.setText(f"↩ Removed the {n} imported task{'s' if n != 1 else ''}")
        return n

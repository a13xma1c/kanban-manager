"""
Task Time-Tracking Panel (the "Time" tab of the task dialog)

* logged vs. estimated progress
* start a stopwatch or Pomodoro on this task, pause / stop it (live clock)
* log time manually ("1h 30m", "90m", "1.5", "1:30")
* list of time entries; your own entries can be edited or deleted

Like comments, time entries are saved immediately (they are independent
records, not part of the task form).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core.time_tracker import FOCUS, LONG_BREAK, SHORT_BREAK, TimeTracker
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.enums import TimeEntrySource
from kanbanmanager.timefmt import format_duration, local_to_utc, parse_duration, utc_to_local


def manual_start_time(day: date, duration_seconds: int, now_local: Optional[datetime] = None) -> datetime:
    """When a manually logged block is assumed to have started (naive UTC).

    Today: it just finished (now - duration). Other days: it started at 09:00 local.
    """
    now_local = now_local or datetime.now()
    if day == now_local.date():
        start = now_local - timedelta(seconds=duration_seconds)
    else:
        start = datetime.combine(day, time(9, 0))
    return local_to_utc(start)


class TimeEntryDialog(QDialog):
    """Edit an existing entry's date, duration and note."""

    def __init__(self, entry: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Time Entry")
        self.entry = entry
        form = QFormLayout(self)
        start_local = utc_to_local(datetime.fromisoformat(entry["started_at"]))
        self.date_edit = QDateEdit(QDate(start_local.year, start_local.month, start_local.day))
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("ddd, MMM d yyyy")
        self.duration_edit = QLineEdit(format_duration(entry["duration_seconds"]))
        self.note_edit = QLineEdit(entry.get("note", ""))
        form.addRow("Date:", self.date_edit)
        form.addRow("Duration:", self.duration_edit)
        form.addRow("Note:", self.note_edit)
        self.error_lbl = QLabel("")
        self.error_lbl.setStyleSheet("color: #F87171;")
        form.addRow(self.error_lbl)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._start_local = start_local

    def _validate(self) -> None:
        if parse_duration(self.duration_edit.text()) is None:
            self.error_lbl.setText("Enter a duration such as 1h 30m, 90m or 1.5")
            return
        self.accept()

    def values(self) -> Dict[str, Any]:
        day = self.date_edit.date().toPython()
        start_local = datetime.combine(day, self._start_local.time())
        return {
            "duration_seconds": parse_duration(self.duration_edit.text()),
            "started_at": local_to_utc(start_local),
            "note": self.note_edit.text(),
        }


class TimePanel(QWidget):
    time_changed = Signal()

    def __init__(
        self,
        service: DatabaseService,
        task_id: Optional[int] = None,
        tracker: Optional[TimeTracker] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.service = service
        self.task_id = task_id
        self.tracker = tracker
        self._entries: List[Dict[str, Any]] = []
        self._estimate = 0.0
        self._init_ui()
        if tracker is not None:
            tracker.state_changed.connect(self._refresh_timer_row)
            tracker.tick.connect(self._update_live_clock)
            tracker.entry_logged.connect(self._on_entry_logged)
        self.reload()

    # ------------------------------------------------------------------
    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.summary_lbl = QLabel("")
        self.summary_lbl.setStyleSheet("font-size: 13px; font-weight: 600; color: #F8FAFC;")
        layout.addWidget(self.summary_lbl)
        self.progress = QProgressBar()
        self.progress.setFixedHeight(8)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        # Timer controls -------------------------------------------------
        self.timer_frame = QFrame()
        self.timer_frame.setObjectName("TimerFrame")
        self.timer_frame.setStyleSheet(
            "#TimerFrame { background-color: #0F172A; border: 1px solid #334155; border-radius: 8px; }"
        )
        tf = QHBoxLayout(self.timer_frame)
        tf.setContentsMargins(10, 8, 10, 8)
        self.live_lbl = QLabel("")
        self.live_lbl.setStyleSheet("font-size: 18px; font-weight: 800; color: #38BDF8; border: none;")
        tf.addWidget(self.live_lbl)
        self.timer_note = QLabel("")
        self.timer_note.setStyleSheet("color: #94A3B8; font-size: 11px; border: none;")
        self.timer_note.setWordWrap(True)
        tf.addWidget(self.timer_note, 1)
        self.stopwatch_btn = QPushButton("▶ Start Stopwatch")
        self.stopwatch_btn.clicked.connect(lambda: self._start("stopwatch"))
        tf.addWidget(self.stopwatch_btn)
        self.pomodoro_btn = QPushButton("🍅 Start Pomodoro")
        self.pomodoro_btn.clicked.connect(lambda: self._start("pomodoro"))
        tf.addWidget(self.pomodoro_btn)
        self.pause_btn = QPushButton("⏸ Pause")
        self.pause_btn.clicked.connect(lambda: self.tracker and self.tracker.toggle_pause())
        tf.addWidget(self.pause_btn)
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setObjectName("DangerButton")
        self.stop_btn.clicked.connect(self.stop_timer)
        tf.addWidget(self.stop_btn)
        layout.addWidget(self.timer_frame)

        # Manual entry ----------------------------------------------------
        manual = QHBoxLayout()
        self.log_lbl = QLabel("Log time:")
        manual.addWidget(self.log_lbl)
        self.duration_input = QLineEdit()
        self.duration_input.setPlaceholderText("e.g. 1h 30m")
        self.duration_input.setFixedWidth(110)
        self.duration_input.returnPressed.connect(self.log_manual)
        manual.addWidget(self.duration_input)
        self.date_input = QDateEdit(QDate.currentDate())
        self.date_input.setCalendarPopup(True)
        self.date_input.setDisplayFormat("ddd, MMM d yyyy")
        self.date_input.setMaximumDate(QDate.currentDate())
        manual.addWidget(self.date_input)
        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("What did you work on? (optional)")
        self.note_input.returnPressed.connect(self.log_manual)
        manual.addWidget(self.note_input, 1)
        self.log_btn = QPushButton("Log Time")
        self.log_btn.setObjectName("PrimaryButton")
        self.log_btn.clicked.connect(self.log_manual)
        manual.addWidget(self.log_btn)
        layout.addLayout(manual)
        self.error_lbl = QLabel("")
        self.error_lbl.setStyleSheet("color: #F87171; font-size: 11px;")
        layout.addWidget(self.error_lbl)

        # Entries ---------------------------------------------------------
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Date", "Person", "Duration", "Source", "Note"])
        header = self.table.horizontalHeader()
        for i in range(4):
            header.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.itemDoubleClicked.connect(lambda _i: self.edit_selected())
        layout.addWidget(self.table, 1)
        hint = QLabel("Double-click or right-click your own entries to edit or delete them.")
        hint.setStyleSheet("color: #64748B; font-size: 11px;")
        layout.addWidget(hint)

        self.placeholder = QLabel("⏱ Save the task to start tracking time on it.")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet("color: #64748B; padding: 30px;")
        layout.addWidget(self.placeholder, 1)
        self._content = [self.summary_lbl, self.progress, self.timer_frame, self.log_lbl, self.duration_input,
                         self.date_input, self.note_input, self.log_btn, self.table, hint]

    def detach(self) -> None:
        """Stop listening to the app-wide tracker (call when the owning dialog closes)."""
        if self.tracker is None:
            return
        for signal, slot in (
            (self.tracker.state_changed, self._refresh_timer_row),
            (self.tracker.tick, self._update_live_clock),
            (self.tracker.entry_logged, self._on_entry_logged),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self.tracker = None

    # ------------------------------------------------------------------
    def set_estimate(self, hours: float) -> None:
        self._estimate = float(hours or 0.0)
        self._refresh_summary()

    def logged_seconds(self) -> int:
        return sum(e["duration_seconds"] for e in self._entries)

    def reload(self) -> None:
        has_task = self.task_id is not None
        for w in self._content:
            w.setVisible(has_task)
        self.placeholder.setVisible(not has_task)
        if not has_task:
            return
        task = self.service.get_task(self.task_id)
        if task is not None and not self._estimate:
            self._estimate = float(task.estimated_hours or 0.0)
        self._entries = self.service.get_time_entries(task_id=self.task_id)
        self._fill_table()
        self._refresh_summary()
        self._refresh_timer_row()

    def _fill_table(self) -> None:
        rows = list(reversed(self._entries))  # newest first
        self.table.setRowCount(len(rows))
        for r, e in enumerate(rows):
            start = utc_to_local(datetime.fromisoformat(e["started_at"]))
            try:
                source = TimeEntrySource(e["source"]).display_name
            except ValueError:
                source = e["source"]
            cells = [start.strftime("%a %b %d, %H:%M"), e["user"] or "—", format_duration(e["duration_seconds"]),
                     source, e["note"]]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, e["id"])
                if c == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)

    def _refresh_summary(self) -> None:
        if self.task_id is None:
            return
        logged = self.logged_seconds()
        est = self._estimate * 3600
        if est > 0:
            pct = logged / est
            remaining = est - logged
            tail = (f"{format_duration(remaining)} remaining" if remaining > 0
                    else f"{format_duration(-remaining)} over estimate")
            self.summary_lbl.setText(f"Logged {format_duration(logged) if logged else '0m'} of "
                                     f"{format_duration(est)} estimate · {tail}")
            self.progress.setRange(0, 100)
            self.progress.setValue(min(100, int(pct * 100)))
            color = "#EF4444" if pct > 1 else ("#F59E0B" if pct >= 0.8 else "#10B981")
        else:
            self.summary_lbl.setText(f"Logged {format_duration(logged) if logged else '0m'} · no estimate set")
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            color = "#3B82F6"
        self.progress.setStyleSheet(
            f"QProgressBar {{ background-color: #334155; border: none; border-radius: 4px; }}"
            f"QProgressBar::chunk {{ background-color: {color}; border-radius: 4px; }}"
        )

    # Timer ---------------------------------------------------------------
    def _tracking_this(self) -> bool:
        t = self.tracker
        return bool(t and t.is_active and t.task_id == self.task_id)

    def _refresh_timer_row(self) -> None:
        if self.task_id is None:
            return
        t = self.tracker
        if t is None:
            self.timer_frame.setVisible(False)
            return
        mine = self._tracking_this()
        self.stopwatch_btn.setVisible(not mine)
        self.pomodoro_btn.setVisible(not mine)
        self.pause_btn.setVisible(mine)
        self.stop_btn.setVisible(mine)
        self.live_lbl.setVisible(mine)
        if mine:
            on_break = t.phase in (SHORT_BREAK, LONG_BREAK)
            self.pause_btn.setText("▶ Resume" if t.paused else "⏸ Pause")
            self.timer_note.setText(t.describe() + (" - breaks are not logged" if on_break else ""))
            self.live_lbl.setStyleSheet(
                "font-size: 18px; font-weight: 800; border: none; color: "
                + ("#94A3B8" if t.paused else "#34D399" if on_break else "#F87171" if t.phase == FOCUS else "#38BDF8")
                + ";"
            )
            self._update_live_clock()
        elif t.is_active:
            self.timer_note.setText(f"A timer is running on '{t.task_title}'. Starting one here stops it first.")
        else:
            self.timer_note.setText("Track time live, or log it manually below.")

    def _update_live_clock(self) -> None:
        if self._tracking_this():
            self.live_lbl.setText(self.tracker.display_time())

    def _start(self, mode: str) -> None:
        if self.tracker is None or self.task_id is None:
            return
        if mode == "pomodoro":
            self.tracker.start_pomodoro(self.task_id)
        else:
            self.tracker.start_stopwatch(self.task_id)

    def stop_timer(self) -> None:
        if not self._tracking_this():
            return
        elapsed = self.tracker.elapsed_seconds()
        if not self.tracker.stop() and elapsed:
            self.timer_note.setText(f"Stopped after {format_duration(elapsed)} - too short to log.")

    def _on_entry_logged(self, task_id: int, _seconds: int) -> None:
        self.time_changed.emit()
        if task_id == self.task_id:
            self.reload()

    # Manual entries -----------------------------------------------------
    def log_manual(self) -> None:
        if self.task_id is None:
            return
        seconds = parse_duration(self.duration_input.text())
        if seconds is None:
            self.error_lbl.setText("Enter a duration such as 1h 30m, 90m, 1.5 or 1:30")
            return
        if seconds > 24 * 3600:
            self.error_lbl.setText("A single entry cannot be longer than 24 hours")
            return
        day = self.date_input.date().toPython()
        self.service.add_time_entry(self.task_id, seconds, started_at=manual_start_time(day, seconds),
                                    note=self.note_input.text())
        self.error_lbl.setText("")
        self.duration_input.clear()
        self.note_input.clear()
        self.reload()
        self.time_changed.emit()

    def _selected_entry(self) -> Optional[Dict[str, Any]]:
        items = self.table.selectedItems()
        if not items:
            return None
        entry_id = items[0].data(Qt.UserRole)
        return next((e for e in self._entries if e["id"] == entry_id), None)

    def _can_edit(self, entry: Dict[str, Any]) -> bool:
        return entry["user"] == self.service.actor

    def _context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        self.table.selectRow(item.row())
        entry = self._selected_entry()
        if entry is None:
            return
        menu = QMenu(self)
        edit = menu.addAction("✏ Edit...", self.edit_selected)
        delete = menu.addAction("🗑 Delete", self.delete_selected)
        if not self._can_edit(entry):
            edit.setEnabled(False)
            delete.setEnabled(False)
            menu.addSeparator()
            menu.addAction(f"Logged by {entry['user'] or 'someone else'}").setEnabled(False)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def edit_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None or not self._can_edit(entry):
            return
        dlg = TimeEntryDialog(entry, self)
        if dlg.exec():
            self.service.update_time_entry(entry["id"], **dlg.values())
            self.reload()
            self.time_changed.emit()

    def delete_selected(self, confirm: bool = True) -> None:
        entry = self._selected_entry()
        if entry is None or not self._can_edit(entry):
            return
        if confirm:
            reply = QMessageBox.question(
                self, "Delete Time Entry",
                f"Delete {format_duration(entry['duration_seconds'])} logged on this task?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        self.service.delete_time_entry(entry["id"])
        self.reload()
        self.time_changed.emit()

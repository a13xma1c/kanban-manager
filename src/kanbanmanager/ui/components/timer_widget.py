"""
Status-bar Live Timer Widget and Pomodoro Settings Dialog

Always visible at the bottom right of the main window:

    🍅 Focus 18:22 · Fix CSV import crash   [⏸] [■] [⚙]
    ⏱ 0:42:10 · Write API docs              [⏸] [■] [⚙]
    ⏱ No timer running · ▶ Resume "Write API docs"     [⚙]
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSpinBox,
    QToolButton,
    QWidget,
)

from kanbanmanager.core.time_tracker import (
    FOCUS,
    LONG_BREAK,
    POMODORO,
    SHORT_BREAK,
    STOPWATCH,
    TimeTracker,
)

_STATE_COLORS = {
    STOPWATCH: "#38BDF8",
    FOCUS: "#F87171",
    SHORT_BREAK: "#34D399",
    LONG_BREAK: "#34D399",
}


class PomodoroSettingsDialog(QDialog):
    def __init__(self, tracker: TimeTracker, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.tracker = tracker
        self.setWindowTitle("Pomodoro Settings")
        cfg = tracker.settings()
        form = QFormLayout(self)

        def spin(value: int, lo: int, hi: int, suffix: str = " min") -> QSpinBox:
            box = QSpinBox()
            box.setRange(lo, hi)
            box.setValue(int(value))
            box.setSuffix(suffix)
            return box

        self.focus = spin(cfg["focus_minutes"], 1, 180)
        self.short_break = spin(cfg["short_break_minutes"], 1, 60)
        self.long_break = spin(cfg["long_break_minutes"], 1, 120)
        self.sessions = spin(cfg["sessions_before_long_break"], 1, 12, " sessions")
        self.auto_breaks = QCheckBox("Start breaks automatically")
        self.auto_breaks.setChecked(bool(cfg["auto_start_breaks"]))
        form.addRow("Focus length:", self.focus)
        form.addRow("Short break:", self.short_break)
        form.addRow("Long break:", self.long_break)
        form.addRow("Long break every:", self.sessions)
        form.addRow("", self.auto_breaks)
        note = QLabel("Focus time is logged to the task; breaks are not.\nChanges apply from the next phase.")
        note.setStyleSheet("color: #94A3B8; font-size: 11px;")
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self) -> None:
        self.tracker.save_settings(
            {
                "focus_minutes": self.focus.value(),
                "short_break_minutes": self.short_break.value(),
                "long_break_minutes": self.long_break.value(),
                "sessions_before_long_break": self.sessions.value(),
                "auto_start_breaks": self.auto_breaks.isChecked(),
            }
        )
        self.accept()


class TimerWidget(QFrame):
    open_task_requested = Signal(int)
    stop_requested = Signal()  # the owner stops the timer and reports what was logged

    def __init__(self, tracker: TimeTracker, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.tracker = tracker
        self.setObjectName("TimerWidget")
        self.setStyleSheet(
            "#TimerWidget { background: transparent; border: none; }"
            "#TimerWidget QToolButton { background: #1E293B; color: #E2E8F0; border: 1px solid #334155;"
            " border-radius: 4px; padding: 1px 6px; }"
            "#TimerWidget QToolButton:hover { background: #334155; }"
            # The ⚙ button opens a menu; hide Qt's automatic drop-down arrow on it
            "#TimerWidget QToolButton::menu-indicator { image: none; width: 0px; }"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(6)

        self.icon_lbl = QLabel("⏱")
        row.addWidget(self.icon_lbl)

        self.time_lbl = QLabel("")
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setBold(True)
        self.time_lbl.setFont(mono)
        row.addWidget(self.time_lbl)

        self.task_btn = QToolButton()
        self.task_btn.setToolTip("Open the task")
        self.task_btn.setStyleSheet("QToolButton { background: transparent; border: none; color: #CBD5E1; }"
                                    "QToolButton:hover { color: #F8FAFC; text-decoration: underline; }")
        self.task_btn.clicked.connect(self._open_task)
        row.addWidget(self.task_btn)

        self.primary_btn = QToolButton()
        self.primary_btn.clicked.connect(self._primary_action)
        row.addWidget(self.primary_btn)

        self.stop_btn = QToolButton()
        self.stop_btn.setText("■")
        self.stop_btn.setToolTip("Stop and log the time")
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        row.addWidget(self.stop_btn)

        self.menu_btn = QToolButton()
        self.menu_btn.setText("⚙")
        self.menu_btn.setToolTip("Timer options")
        self.menu_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.menu_btn)
        self.skip_action = menu.addAction("⏭ Skip break", self.tracker.skip_break)
        self.discard_action = menu.addAction("🗑 Discard current session", self._discard)
        menu.addSeparator()
        menu.addAction("🍅 Pomodoro settings...", self._open_settings)
        self.menu_btn.setMenu(menu)
        row.addWidget(self.menu_btn)

        tracker.state_changed.connect(self.refresh)
        tracker.tick.connect(self._update_time)
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        t = self.tracker
        active = t.is_active
        title = t.task_title if active else t.last_task_title
        if len(title) > 38:
            title = title[:36] + "…"
        self.stop_btn.setVisible(active)
        self.skip_action.setVisible(t.mode == POMODORO and t.phase in (SHORT_BREAK, LONG_BREAK))
        self.discard_action.setVisible(active)
        if not active:
            self.icon_lbl.setText("⏱")
            self.time_lbl.setText("")
            self.time_lbl.setVisible(False)
            self.task_btn.setText("No timer running")
            self.task_btn.setEnabled(False)
            self.primary_btn.setVisible(bool(t.last_task_id))
            self.primary_btn.setText("▶ Resume")
            self.primary_btn.setToolTip(f"Start the stopwatch on '{t.last_task_title}' again")
            return
        key = t.phase if t.mode == POMODORO else STOPWATCH
        color = _STATE_COLORS.get(key or "", "#E2E8F0")
        if t.paused:
            color = "#94A3B8"
        self.icon_lbl.setText({FOCUS: "🍅", SHORT_BREAK: "☕", LONG_BREAK: "🌴"}.get(t.phase or "", "⏱"))
        self.time_lbl.setVisible(True)
        self.time_lbl.setStyleSheet(f"color: {color};")
        self.task_btn.setEnabled(True)
        # Buttons treat "&" as a shortcut marker; task titles are user text, so escape it
        self.task_btn.setText(f"{t.describe()} · {title}".replace("&", "&&"))
        self.primary_btn.setVisible(True)
        if t.paused:
            on_break = t.phase in (SHORT_BREAK, LONG_BREAK)
            self.primary_btn.setText("▶ Start break" if on_break else ("▶ Start focus" if t.phase == FOCUS else "▶ Resume"))
            self.primary_btn.setToolTip("Continue")
        else:
            self.primary_btn.setText("⏸")
            self.primary_btn.setToolTip("Pause")
        self._update_time()

    def _update_time(self) -> None:
        if self.tracker.is_active:
            self.time_lbl.setText(self.tracker.display_time())

    def _primary_action(self) -> None:
        t = self.tracker
        if not t.is_active:
            if t.last_task_id:
                t.start_stopwatch(t.last_task_id)
            return
        t.toggle_pause()

    def _open_task(self) -> None:
        task_id = self.tracker.task_id or self.tracker.last_task_id
        if task_id:
            self.open_task_requested.emit(task_id)

    def _discard(self) -> None:
        self.tracker.discard()

    def _open_settings(self) -> None:
        PomodoroSettingsDialog(self.tracker, self).exec()

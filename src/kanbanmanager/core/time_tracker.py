"""
Live Stopwatch & Pomodoro Timer Engine

One timer per user, shared by the whole UI (status bar widget, task dialog,
card context menus). Work time is written to the database as TimeEntry rows:

* a running timer *is* an open TimeEntry (``ended_at`` NULL), so time is never
  lost if the app crashes or is closed while tracking; the timer resumes on
  the next start;
* pausing closes the current entry; resuming opens a new one;
* Pomodoro focus sessions are logged as work, breaks are not. After a focus
  session a break starts (optionally automatically); every Nth break is long.

The clock is injectable (``now``) so behaviour can be tested deterministically.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from kanbanmanager.config import MIN_TIME_ENTRY_SECONDS, POMODORO_DEFAULTS
from kanbanmanager.database.models import utc_now
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.timefmt import format_duration
from kanbanmanager.enums import TimeEntrySource

IDLE = "idle"
STOPWATCH = "stopwatch"
POMODORO = "pomodoro"

FOCUS = "focus"
SHORT_BREAK = "short_break"
LONG_BREAK = "long_break"

PHASE_NAMES = {FOCUS: "Focus", SHORT_BREAK: "Short break", LONG_BREAK: "Long break"}

SETTINGS_KEY = "pomodoro"


def format_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class TimeTracker(QObject):
    state_changed = Signal()               # mode/phase/pause/task changed
    tick = Signal()                        # once per second while a timer is active
    phase_finished = Signal(str, str)      # (finished phase, human message)
    entry_logged = Signal(int, int)        # (task_id, seconds) whenever work time is written

    def __init__(
        self,
        service: DatabaseService,
        parent: Optional[QObject] = None,
        now: Callable[[], datetime] = utc_now,
    ):
        super().__init__(parent)
        self.service = service
        self._now = now
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_tick)
        # Most recent task, so the idle widget can offer "resume"
        self.last_task_id: Optional[int] = None
        self.last_task_title = ""
        self._reset()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _reset(self) -> None:
        self.mode = IDLE
        self.phase: Optional[str] = None
        self.task_id: Optional[int] = None
        self.task_title = ""
        self.paused = False
        self.entry_id: Optional[int] = None
        self._segment_start: Optional[datetime] = None   # start of the currently running segment
        self._accumulated = 0.0                          # seconds from earlier segments (pauses)
        self._phase_target: Optional[int] = None          # seconds for pomodoro phases
        self.completed_focus_sessions = 0

    @property
    def is_active(self) -> bool:
        return self.mode != IDLE

    @property
    def is_tracking_work(self) -> bool:
        """True while work time is being recorded (not paused, not on a break)."""
        return self.entry_id is not None

    def settings(self) -> Dict[str, Any]:
        stored = self.service.get_setting(SETTINGS_KEY, {}) or {}
        merged = dict(POMODORO_DEFAULTS)
        merged.update({k: v for k, v in stored.items() if k in POMODORO_DEFAULTS})
        return merged

    def save_settings(self, values: Dict[str, Any]) -> None:
        clean = {k: values[k] for k in POMODORO_DEFAULTS if k in values}
        self.service.set_setting(SETTINGS_KEY, clean)

    def elapsed_seconds(self) -> float:
        """Seconds in the current stopwatch session or pomodoro phase (excluding pauses)."""
        running = (self._now() - self._segment_start).total_seconds() if self._segment_start and not self.paused else 0
        return self._accumulated + max(0.0, running)

    def remaining_seconds(self) -> Optional[float]:
        if self.mode != POMODORO or self._phase_target is None:
            return None
        return max(0.0, self._phase_target - self.elapsed_seconds())

    def display_time(self) -> str:
        remaining = self.remaining_seconds()
        return format_clock(remaining if remaining is not None else self.elapsed_seconds())

    def describe(self) -> str:
        if self.mode == IDLE:
            return "No timer running"
        label = "Stopwatch" if self.mode == STOPWATCH else PHASE_NAMES.get(self.phase or "", "Pomodoro")
        return f"{label}{' (paused)' if self.paused else ''}"

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def start_stopwatch(self, task_id: int) -> None:
        self._begin(task_id, STOPWATCH)

    def start_pomodoro(self, task_id: int) -> None:
        self._begin(task_id, POMODORO)

    def _begin(self, task_id: int, mode: str) -> None:
        if self.is_active:
            self.stop()
        task = self.service.get_task(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        self._reset()
        self.mode = mode
        self.task_id = task_id
        self.task_title = task.title
        self.last_task_id, self.last_task_title = task_id, task.title
        if mode == POMODORO:
            self.phase = FOCUS
            self._phase_target = int(self.settings()["focus_minutes"]) * 60
        self._open_segment()
        self._timer.start()
        self.state_changed.emit()

    def pause(self) -> None:
        if not self.is_active or self.paused:
            return
        if self.entry_id is not None:
            self._close_segment(min_seconds=MIN_TIME_ENTRY_SECONDS)
        elif self._segment_start is not None:  # a break: just freeze the countdown
            self._accumulated += (self._now() - self._segment_start).total_seconds()
        self._segment_start = None
        self.paused = True
        self.state_changed.emit()

    def resume(self) -> None:
        if not self.is_active or not self.paused:
            return
        self.paused = False
        if self.phase in (None, FOCUS):
            self._open_segment(keep_accumulated=True)  # work resumes: new time entry
        else:
            self._segment_start = self._now()
        self.state_changed.emit()

    def toggle_pause(self) -> None:
        self.resume() if self.paused else self.pause()

    def stop(self) -> int:
        """Stop the timer, logging any work in progress. Returns seconds of work logged by this call."""
        if not self.is_active:
            return 0
        logged = 0
        if self.entry_id is not None:
            # A whole session shorter than the minimum is treated as an accidental start
            session_total = self.elapsed_seconds()
            minimum = MIN_TIME_ENTRY_SECONDS if session_total < MIN_TIME_ENTRY_SECONDS else 0
            logged = self._close_segment(min_seconds=minimum)
        self._timer.stop()
        self._reset()
        self.state_changed.emit()
        return logged

    def discard(self) -> None:
        """Stop without logging the segment that is currently running (earlier,
        already-saved segments of a paused session are kept)."""
        if not self.is_active:
            return
        if self.entry_id is not None:
            self.service.discard_running_time_entry(self.entry_id)
            self.entry_id = None
        self._timer.stop()
        self._reset()
        self.state_changed.emit()

    def shutdown(self) -> None:
        """Stop ticking (app exit). A running entry stays open in the DB and is restored next time."""
        self._timer.stop()

    def skip_break(self) -> None:
        """End a Pomodoro break early and start the next focus session."""
        if self.mode == POMODORO and self.phase in (SHORT_BREAK, LONG_BREAK):
            self._start_phase(FOCUS)

    def start_next_focus(self) -> None:
        self.skip_break()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def restore(self) -> Optional[str]:
        """Resume a timer left running (e.g. the app was closed or crashed).

        Returns a message describing what happened, or None if nothing was running.
        """
        entry = self.service.get_running_time_entry()
        if entry is None:
            return None
        started = datetime.fromisoformat(entry["started_at"])
        elapsed = (self._now() - started).total_seconds()
        planned = entry.get("planned_seconds")
        if entry["source"] == TimeEntrySource.POMODORO.value and planned and elapsed >= planned:
            # The focus session finished while we were away: log exactly its planned length
            self.service.stop_time_entry(entry["id"], ended_at=started + timedelta(seconds=planned))
            self.entry_logged.emit(entry["task_id"], int(planned))
            self.last_task_id, self.last_task_title = entry["task_id"], entry["task_title"]
            return f"Your Pomodoro on '{entry['task_title']}' finished while KanbanManager was closed and was logged."
        self._reset()
        self.task_id = entry["task_id"]
        self.task_title = entry["task_title"]
        self.last_task_id, self.last_task_title = self.task_id, self.task_title
        self.entry_id = entry["id"]
        self._segment_start = started
        if entry["source"] == TimeEntrySource.POMODORO.value:
            self.mode, self.phase, self._phase_target = POMODORO, FOCUS, int(planned or 25 * 60)
        else:
            self.mode = STOPWATCH
        self._timer.start()
        self.state_changed.emit()
        return f"Resumed the timer on '{self.task_title}' (running for {format_duration(elapsed)})."

    def sync(self) -> None:
        """Drop local state if the running entry disappeared (e.g. its task was deleted)."""
        if self.entry_id is None:
            if self.task_id is not None and self.service.get_task(self.task_id) is None:
                self._timer.stop()
                self._reset()
                self.last_task_id, self.last_task_title = None, ""
                self.state_changed.emit()
            return
        running = self.service.get_running_time_entry()
        if running is None or running["id"] != self.entry_id:
            self._timer.stop()
            self._reset()
            if self.last_task_id and self.service.get_task(self.last_task_id) is None:
                self.last_task_id, self.last_task_title = None, ""
            self.state_changed.emit()
        elif running["task_title"] != self.task_title:
            self.task_title = self.last_task_title = running["task_title"]
            self.state_changed.emit()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _open_segment(self, keep_accumulated: bool = False) -> None:
        if not keep_accumulated:
            self._accumulated = 0.0
        planned = None
        if self.mode == POMODORO and self._phase_target:
            planned = int(max(1, self._phase_target - self._accumulated))
        source = TimeEntrySource.POMODORO if self.mode == POMODORO else TimeEntrySource.STOPWATCH
        entry = self.service.start_time_entry(self.task_id, source=source.value, planned_seconds=planned,
                                              started_at=self._now())
        self.entry_id = entry["id"]
        self._segment_start = datetime.fromisoformat(entry["started_at"])

    def _close_segment(self, min_seconds: int, ended_at: Optional[datetime] = None) -> int:
        end = ended_at or self._now()
        segment = (end - self._segment_start).total_seconds() if self._segment_start else 0
        result = self.service.stop_time_entry(self.entry_id, ended_at=end, min_seconds=min_seconds)
        self._accumulated += max(0.0, segment)
        self.entry_id = None
        if result:
            self.entry_logged.emit(result["task_id"], result["duration_seconds"])
            return int(result["duration_seconds"])
        return 0

    def _start_phase(self, phase: str, paused: bool = False) -> None:
        """Enter a Pomodoro phase. When ``paused``, nothing runs (and nothing is written)
        until resume(); a focus phase only opens a time entry once it actually runs."""
        cfg = self.settings()
        self.phase = phase
        minutes = {
            FOCUS: cfg["focus_minutes"],
            SHORT_BREAK: cfg["short_break_minutes"],
            LONG_BREAK: cfg["long_break_minutes"],
        }[phase]
        self._phase_target = int(minutes) * 60
        self._accumulated = 0.0
        self.paused = paused
        self._segment_start = None
        if not paused:
            if phase == FOCUS:
                self._open_segment()
            else:
                self._segment_start = self._now()
        self._timer.start()
        self.state_changed.emit()

    def _on_tick(self) -> None:
        if not self.is_active:
            self._timer.stop()
            return
        if self.mode == POMODORO and not self.paused and self.remaining_seconds() == 0:
            self._finish_phase()
        self.tick.emit()

    def _finish_phase(self) -> None:
        cfg = self.settings()
        finished = self.phase
        if finished == FOCUS:
            # Log exactly the planned focus length, even if the tick arrived a little late
            end = self._segment_start + timedelta(seconds=max(0.0, self._phase_target - self._accumulated))
            self._close_segment(min_seconds=0, ended_at=end)
            self.completed_focus_sessions += 1
            long_break = self.completed_focus_sessions % max(1, int(cfg["sessions_before_long_break"])) == 0
            next_phase = LONG_BREAK if long_break else SHORT_BREAK
            minutes = cfg["long_break_minutes" if long_break else "short_break_minutes"]
            message = f"Focus session complete on '{self.task_title}'. Time for a {minutes}-minute break."
            self._start_phase(next_phase, paused=not cfg["auto_start_breaks"])
        else:
            message = f"Break over - ready for the next focus session on '{self.task_title}'?"
            self._start_phase(FOCUS, paused=True)  # never start tracking work without the user
        self.phase_finished.emit(finished or "", message)

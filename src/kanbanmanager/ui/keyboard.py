"""
Vim-style keyboard navigation and the shortcut help overlay.

A single application event filter watches key presses in the main window.
Plain letters never fire while you type: anything editable (text boxes, spin
boxes, combos), buttons handling Space/Enter, and item views handling their
own navigation keys are left alone.

On the board:  j/k and ↑/↓ move between cards, h/l and ←/→ between columns,
g g / G jump to the top/bottom. Shift+J/K/H/L *moves the selected task*.
Enter or o opens it, x completes it, a archives, Delete deletes, t starts or
stops a timer, + / - change priority, n adds a task to its column.
Anywhere:      / searches, c quick-captures, [ and ] switch boards, 1-5 switch
views, ? shows all shortcuts, Esc clears the selection or the filters.

The binding table below is the single source of truth; the ? overlay and the
README's shortcut list are generated from it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

SEQUENCE_MS = 800    # how long a leading "g" waits for its second key


@dataclass(frozen=True)
class Binding:
    keys: str                      # shown in the help overlay ("j / ↓")
    description: str
    category: str
    handler: str                   # KeyboardNavigator method name
    board_only: bool = True        # needs the board page with the Kanban view


BINDINGS: List[Binding] = [
    Binding("j / ↓", "Next card", "Move around", "next_card"),
    Binding("k / ↑", "Previous card", "Move around", "previous_card"),
    Binding("h / ←", "Column to the left", "Move around", "column_left"),
    Binding("l / →", "Column to the right", "Move around", "column_right"),
    Binding("g g", "First card in the column", "Move around", "first_card"),
    Binding("G", "Last card in the column", "Move around", "last_card"),
    Binding("Esc", "Clear the selection, then the filters", "Move around", "escape", board_only=False),
    Binding("Enter / o", "Open the selected task", "Selected task", "open_task"),
    Binding("x", "Complete / reopen", "Selected task", "toggle_task"),
    Binding("Shift+J / Shift+K", "Move it down / up", "Selected task", "move_down"),
    Binding("Shift+H / Shift+L", "Move it to the next column", "Selected task", "move_left"),
    Binding("+ / -", "Raise / lower its priority", "Selected task", "priority_up"),
    Binding("t", "Start / stop a timer on it", "Selected task", "timer"),
    Binding("a", "Archive it", "Selected task", "archive"),
    Binding("Delete / #", "Delete it", "Selected task", "delete"),
    Binding("n", "New task in its column", "Selected task", "new_task"),
    Binding("/", "Search", "Anywhere", "search", board_only=False),
    Binding("c", "Quick capture", "Anywhere", "capture", board_only=False),
    Binding("[ / ]", "Previous / next board", "Anywhere", "previous_board", board_only=False),
    Binding("1 … 5", "Board · Calendar · Gantt · Analytics · Workload", "Anywhere", "view_1", board_only=False),
    Binding("?", "All keyboard shortcuts", "Anywhere", "help", board_only=False),
]

# Shortcuts registered elsewhere, listed in the overlay for completeness
OTHER_SHORTCUTS: List[Tuple[str, str, str]] = [
    ("Ctrl+K", "Command palette: boards, tasks, actions", "Anywhere"),
    ("Ctrl+N", "New task", "Anywhere"),
    ("Ctrl+F", "Search", "Anywhere"),
    ("Ctrl+Shift+E", "Import / Export", "Anywhere"),
    ("Ctrl+Shift+Space", "Quick capture (also system-wide)", "Anywhere"),
    ("Tab", "Walk through cards and controls", "Move around"),
]


def is_typing(widget: Optional[QWidget], key: int) -> bool:
    """True when the focused widget should keep this key (never steal keys while typing)."""
    if widget is None:
        return False
    if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox, QKeySequenceEdit)):
        return True
    if isinstance(widget.parentWidget(), (QAbstractSpinBox, QComboBox)):  # their internal line edit
        return True
    if isinstance(widget, QAbstractButton):
        if key == Qt.Key_Space:  # Space always activates the button
            return True
        if isinstance(widget, (QRadioButton, QCheckBox)) and key in (Qt.Key_Left, Qt.Key_Right,
                                                                     Qt.Key_Up, Qt.Key_Down):
            return True  # arrows walk radio/check groups; plain buttons don't use them
    view = widget
    while view is not None and not isinstance(view, QAbstractItemView):
        view = view.parentWidget()
    if view is not None and key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right, Qt.Key_Home, Qt.Key_End,
                                    Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter,
                                    Qt.Key_Delete):
        return True
    return False


class KeyboardNavigator(QObject):
    """The main window's vim layer.

    It filters the events of just two objects: the window itself (keys arrive there when
    nothing has focus, and when focused widgets pass them on) and whichever widget in the
    window has focus - followed through ``focusChanged``. That sees every key press first,
    even ones scroll areas would swallow, without the cost of an application-wide filter
    inspecting every paint, layout and hover event in the app.

    A key the focused widget ignores propagates to the window and reaches the filter a
    second time; that's harmless because handlers only return False when they did nothing.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._pending_g = False
        self._g_timer = QTimer(self)
        self._g_timer.setSingleShot(True)
        self._g_timer.setInterval(SEQUENCE_MS)
        self._g_timer.timeout.connect(self._clear_sequence)
        self._watched: Optional[QWidget] = None
        self._attached = True
        window.installEventFilter(self)
        QApplication.instance().focusChanged.connect(self._on_focus_changed)
        self._on_focus_changed(None, QApplication.focusWidget())

    def _on_focus_changed(self, _old, new) -> None:
        self._unwatch()
        if new is not None and new is not self.window and new.window() is self.window:
            new.installEventFilter(self)
            self._watched = new

    def _unwatch(self) -> None:
        if self._watched is not None:
            try:
                self._watched.removeEventFilter(self)
            except RuntimeError:  # the widget is already gone
                pass
            self._watched = None

    def detach(self) -> None:
        if not self._attached:
            return
        self._attached = False
        app = QApplication.instance()
        if app is not None:
            try:
                app.focusChanged.disconnect(self._on_focus_changed)
            except (RuntimeError, TypeError):
                pass
        self._unwatch()
        try:
            self.window.removeEventFilter(self)
        except RuntimeError:
            pass

    # Routing ------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.KeyPress:
            return False
        # Only react to keys delivered inside our own window (not to dialogs or other windows)
        if not isinstance(obj, QWidget) or obj.window() is not self.window:
            return False
        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return False
        if is_typing(QApplication.focusWidget(), event.key()):
            self._clear_sequence()
            return False
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        text = event.text()
        if not text and Qt.Key_A <= event.key() <= Qt.Key_Z:
            text = chr(ord("a") + event.key() - Qt.Key_A)
        if text.isalpha():  # some platforms report lowercase text even with Shift held
            text = text.upper() if shift else text.lower()
        return self.handle_key(event.key(), text, shift)

    @property
    def board(self):
        return self.window.board_view

    def _board_active(self) -> bool:
        return (self.window.pages.currentIndex() == self.window.PAGE_BOARD
                and self.window.view_stack.currentIndex() == 0)

    def handle_key(self, key: int, text: str, shift: bool) -> bool:
        """True when the key was consumed."""
        if self._pending_g:
            self._clear_sequence()
            if text == "g" and self._board_active():
                return self.first_card()
            return True  # an unfinished sequence swallows the second key
        on_board = self._board_active()
        if text == "g" and on_board:
            self._pending_g = True
            self._g_timer.start()
            return True
        table = {
            "?": self.help, "/": self.search, "c": self.capture, "[": self.previous_board, "]": self.next_board,
        }
        if text in table:
            return table[text]()
        if text and text in "12345":
            return self.switch_view(int(text) - 1)
        if key == Qt.Key_Escape:
            return self.escape()
        if not on_board:
            return False
        board_table = {
            Qt.Key_Down: self.next_card, Qt.Key_Up: self.previous_card,
            Qt.Key_Left: self.column_left, Qt.Key_Right: self.column_right,
            Qt.Key_Return: self.open_task, Qt.Key_Enter: self.open_task, Qt.Key_Delete: self.delete,
        }
        if key in board_table and not shift:
            return board_table[key]()
        letters = {
            "j": self.next_card, "k": self.previous_card, "h": self.column_left, "l": self.column_right,
            "J": self.move_down, "K": self.move_up, "H": self.move_left, "L": self.move_right,
            "G": self.last_card, "o": self.open_task, "e": self.open_task, "x": self.toggle_task,
            "a": self.archive, "#": self.delete, "t": self.timer, "n": self.new_task,
            "+": self.priority_up, "=": self.priority_up, "-": self.priority_down,
        }
        if text in letters:
            return letters[text]()
        return False

    def _clear_sequence(self) -> None:
        self._pending_g = False
        self._g_timer.stop()

    # Movement ----------------------------------------------------------------
    def next_card(self) -> bool:
        return self.board.select_move(d_row=1)

    def previous_card(self) -> bool:
        return self.board.select_move(d_row=-1)

    def column_left(self) -> bool:
        return self.board.select_move(d_col=-1)

    def column_right(self) -> bool:
        return self.board.select_move(d_col=1)

    def first_card(self) -> bool:
        return self.board.select_edge(last=False)

    def last_card(self) -> bool:
        return self.board.select_edge(last=True)

    def escape(self) -> bool:
        if self._board_active() and self.board.selected_task_id is not None:
            self.board.clear_selection()
            return True
        bar = self.window.filter_bar
        if bar.search_input.text() or any(c.currentIndex() for c in
                                          (bar.priority_combo, bar.label_combo, bar.due_combo, bar.status_combo,
                                           bar.field_combo)):
            bar.clear_filters()
            return True
        return False

    # Selected task --------------------------------------------------------------
    def open_task(self) -> bool:
        return self.board.open_selected()

    def toggle_task(self) -> bool:
        return self.board.toggle_selected()

    def move_down(self) -> bool:
        return self.board.move_selected(d_row=1)

    def move_up(self) -> bool:
        return self.board.move_selected(d_row=-1)

    def move_left(self) -> bool:
        return self.board.move_selected(d_col=-1)

    def move_right(self) -> bool:
        return self.board.move_selected(d_col=1)

    def archive(self) -> bool:
        return self.board.archive_selected()

    def delete(self) -> bool:
        return self.board.delete_selected()

    def timer(self) -> bool:
        return self.board.timer_selected()

    def priority_up(self) -> bool:
        return self.board.bump_priority(1)

    def priority_down(self) -> bool:
        return self.board.bump_priority(-1)

    def new_task(self) -> bool:
        self.board.add_task_near_selection()
        return True

    # Anywhere -----------------------------------------------------------------------
    def search(self) -> bool:
        if self.window.pages.currentIndex() != self.window.PAGE_BOARD and self.window.current_board_id:
            self.window.open_board(self.window.current_board_id)
        self.window.filter_bar.search_input.setFocus()
        self.window.filter_bar.search_input.selectAll()
        return True

    def capture(self) -> bool:
        self.window.show_quick_capture()
        return True

    def switch_view(self, index: int) -> bool:
        from kanbanmanager.enums import ViewMode

        modes = [ViewMode.BOARD, ViewMode.CALENDAR, ViewMode.TIMELINE, ViewMode.ANALYTICS, ViewMode.WORKLOAD]
        if not 0 <= index < len(modes) or not self.window.current_board_id:
            return False
        if self.window.pages.currentIndex() != self.window.PAGE_BOARD:
            self.window.open_board(self.window.current_board_id)
        self.window._on_view_mode_changed(modes[index].value)
        return True

    def _boards(self) -> List[int]:
        return [b.id for ws in self.window.service.get_workspaces() for b in ws.boards if not b.is_archived]

    def _step_board(self, step: int) -> bool:
        boards = self._boards()
        if not boards:
            return False
        current = self.window.current_board_id
        index = boards.index(current) if current in boards else 0
        self.window.open_board(boards[(index + step) % len(boards)])
        return True

    def previous_board(self) -> bool:
        return self._step_board(-1)

    def next_board(self) -> bool:
        return self._step_board(1)

    def help(self) -> bool:
        dlg = ShortcutHelpDialog(self.window)
        dlg.exec()
        dlg.deleteLater()
        return True


class ShortcutHelpDialog(QDialog):
    """The ? overlay: every shortcut, generated from the binding table."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.setMinimumSize(640, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        title = QLabel("⌨ Keyboard Shortcuts")
        title.setStyleSheet("font-size: 16px; font-weight: 800;")
        root.addWidget(title)
        hint = QLabel("Letter keys work while you're not typing in a text box. "
                      "On the board, j/k/h/l (or the arrows) walk the cards.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #94A3B8;")
        root.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background: transparent;")
        root.addWidget(scroll, 1)
        host = QWidget()
        scroll.setWidget(host)
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 8, 0, 8)
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(4)
        rows: List[Tuple[str, str, str]] = [(b.keys, b.description, b.category) for b in BINDINGS]
        rows += OTHER_SHORTCUTS
        categories = list(dict.fromkeys(category for _k, _d, category in rows))
        row = 0
        for category in categories:
            header = QLabel(category)
            header.setStyleSheet("color: #3B82F6; font-weight: 700; margin-top: 8px;")
            grid.addWidget(header, row, 0, 1, 2)
            row += 1
            for keys, description, cat in rows:
                if cat != category:
                    continue
                key_lbl = QLabel(keys)
                key_lbl.setStyleSheet("font-family: monospace; color: #F8FAFC; background-color: #1E293B;"
                                      " border: 1px solid #334155; border-radius: 4px; padding: 1px 8px;")
                grid.addWidget(key_lbl, row, 0)
                desc_lbl = QLabel(description)
                desc_lbl.setStyleSheet("color: #CBD5E1;")
                grid.addWidget(desc_lbl, row, 1)
                row += 1
        grid.setColumnStretch(1, 1)
        buttons = QHBoxLayout()
        root.addLayout(buttons)
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)

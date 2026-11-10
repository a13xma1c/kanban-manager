"""
Command palette (Ctrl+K): type a few letters to jump to a board or view, open a
task anywhere, or run an action - without touching the mouse.

Matching is fuzzy: the query's characters must appear in order, with better
scores for word starts and consecutive letters ("gcal" finds "Go to Calendar").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)


@dataclass
class Command:
    title: str
    run: Callable[[], None]
    icon: str = ""
    category: str = "Actions"
    subtitle: str = ""
    shortcut: str = ""
    keywords: str = ""

    @property
    def haystack(self) -> str:
        return f"{self.title} {self.subtitle} {self.keywords}"


def _word_start(text: str, i: int) -> bool:
    return i == 0 or not text[i - 1].isalnum()


def _match(query: str, text_low: str, prefer_word_starts: bool) -> Optional[float]:
    score, pos, previous = 0.0, 0, -2
    for ch in query:
        if ch == " ":
            previous = -2
            continue
        found = text_low.find(ch, pos)
        if found < 0:
            return None
        if prefer_word_starts and found != previous + 1 and not _word_start(text_low, found):
            i = found + 1  # a word-start hit beats a mid-word one ("ca" - "Capture", not "quiCk")
            while 0 <= i < len(text_low):
                i = text_low.find(ch, i)
                if i < 0:
                    break
                if _word_start(text_low, i):
                    found = i
                    break
                i += 1
        bonus = 2.0 if _word_start(text_low, found) else 0.0
        if found == previous + 1:
            bonus += 1.5
        score += 1.0 + bonus - found * 0.01
        previous = pos = found
        pos += 1
    return score


def fuzzy_score(query: str, text: str) -> Optional[float]:
    """Higher is better; None when the query's letters don't appear in order."""
    query = query.lower()
    text_low = text.lower()
    if not query:
        return 0.0
    candidates = [s for s in (_match(query, text_low, True), _match(query, text_low, False)) if s is not None]
    if not candidates:
        return None
    score = max(candidates)
    if text_low.startswith(query):
        score += 4
    return score


def rank(query: str, commands: List[Command], limit: int = 12) -> List[Command]:
    scored: List[Tuple[float, int, Command]] = []
    for i, command in enumerate(commands):
        s = fuzzy_score(query, command.haystack)
        if s is not None:
            scored.append((s, i, command))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [c for _s, _i, c in scored[:limit]]


class CommandPalette(QDialog):
    """``provider(query)`` returns the commands to offer (called on every keystroke)."""

    def __init__(self, provider: Callable[[str], List[Command]], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.provider = provider
        self.commands: List[Command] = []
        self.chosen: Optional[Command] = None
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setObjectName("CommandPalette")
        self.setStyleSheet("#CommandPalette { background-color: #0F172A; border: 1px solid #334155;"
                           " border-radius: 10px; }")
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a board, task or command…")
        self.input.setAccessibleName("Command palette search")
        self.input.textChanged.connect(self.refresh)
        self.input.installEventFilter(self)
        root.addWidget(self.input)
        self.list = QListWidget()
        self.list.setAccessibleName("Command palette results")
        self.list.setStyleSheet("QListWidget { border: none; background: transparent; }"
                                "QListWidget::item { padding: 6px 8px; border-radius: 6px; }"
                                "QListWidget::item:selected { background-color: #1D4ED8; color: #FFFFFF; }")
        self.list.itemActivated.connect(self._run_item)
        self.list.itemClicked.connect(self._run_item)
        root.addWidget(self.list, 1)
        self.hint = QLabel("↑↓ choose · Enter run · Esc close")
        self.hint.setStyleSheet("color: #64748B; font-size: 11px;")
        root.addWidget(self.hint)
        self.refresh("")

    def open(self, query: str = "") -> None:
        self.input.setText(query)
        self.refresh(query)
        self.show()
        self.input.setFocus()

    def refresh(self, query: str) -> None:
        self.commands = self.provider(query)
        self.list.clear()
        for command in self.commands:
            text = f"{command.icon}  {command.title}" if command.icon else command.title
            if command.subtitle:
                text += f"   —  {command.subtitle}"
            if command.shortcut:
                text += f"   ({command.shortcut})"
            item = QListWidgetItem(text)
            item.setToolTip(command.category)
            self.list.addItem(item)
        if self.commands:
            self.list.setCurrentRow(0)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.input and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_PageUp):
                row = self.list.currentRow()
                step = {Qt.Key_Down: 1, Qt.Key_Up: -1, Qt.Key_PageDown: 6, Qt.Key_PageUp: -6}[key]
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row + step)))
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self.run_current()
                return True
        return super().eventFilter(obj, event)

    def run_current(self) -> bool:
        row = self.list.currentRow()
        if not 0 <= row < len(self.commands):
            return False
        self.chosen = self.commands[row]
        self.accept()
        self.chosen.run()
        return True

    def _run_item(self, item: QListWidgetItem) -> None:
        self.list.setCurrentRow(self.list.row(item))
        self.run_current()

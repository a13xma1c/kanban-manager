"""
Quick Capture window and its settings

A small always-on-top window: type one line (see core.capture for the syntax),
watch the live preview of what was understood, press Enter. The item goes to
the chosen board (or ``>board`` in the text), otherwise to the Global Inbox.
"""
from __future__ import annotations

import html
from typing import Optional

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import capture as cap
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.colors import tint
from kanbanmanager.enums import Priority
from kanbanmanager.ui.global_hotkey import DEFAULT_HOTKEY, parse_hotkey
from kanbanmanager.ui.styles import CHECKBOX_QSS

SETTINGS_KEY = "quick_capture"
DEFAULT_SETTINGS = {"hotkey": DEFAULT_HOTKEY, "global": True}
SYNTAX_HINT = "tomorrow 3pm · !high · #label · @person · >board · // notes"


def load_settings(service: DatabaseService) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in (service.get_setting(SETTINGS_KEY, {}) or {}).items() if k in DEFAULT_SETTINGS})
    return s


def chip(text: str, color: str) -> str:
    return (f"<span style='background-color:{tint(color)}; color:{color};'>"
            f"&nbsp;{html.escape(text)}&nbsp;</span>")


class QuickCaptureDialog(QDialog):
    captured = Signal(dict)   # {"kind", "id", "title", "board_name"}

    def __init__(self, service: DatabaseService, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self._user_picked_destination = False
        self.setWindowTitle("Quick Capture")
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setMinimumWidth(640)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setObjectName("CapturePanel")
        panel.setStyleSheet(
            "#CapturePanel { background-color: #111827; border: 1px solid #3B82F6; border-radius: 12px; }"
            "#CapturePanel QLineEdit#CaptureInput { font-size: 17px; padding: 10px 12px; }"
        )
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        layout.addLayout(head)
        title = QLabel("⚡ Quick Capture")
        title.setStyleSheet("font-weight: 800; font-size: 14px; border: none;")
        head.addWidget(title)
        head.addStretch()
        head.addWidget(QLabel("to"))
        self.destination = QComboBox()
        self.destination.setMinimumWidth(220)
        self.destination.activated.connect(self._on_destination_picked)
        head.addWidget(self.destination)

        self.input = QLineEdit()
        self.input.setObjectName("CaptureInput")
        self.input.setPlaceholderText("What's on your mind?  e.g.  Send invoice fri !high #Finance @me")
        self.input.textChanged.connect(self._update_preview)
        self.input.returnPressed.connect(self.save)
        layout.addWidget(self.input)

        self.preview = QLabel("")
        self.preview.setTextFormat(Qt.RichText)
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet("border: none; color: #CBD5E1;")
        layout.addWidget(self.preview)

        foot = QHBoxLayout()
        layout.addLayout(foot)
        self.hint = QLabel(f"{SYNTAX_HINT}   ·   Enter to save, Ctrl+Enter to save & add another, Esc to close")
        self.hint.setStyleSheet("color: #64748B; font-size: 11px; border: none;")
        self.hint.setWordWrap(True)
        foot.addWidget(self.hint, 1)
        self.status = QLabel("")
        self.status.setStyleSheet("color: #34D399; font-weight: 600; border: none;")
        foot.addWidget(self.status)

        keep = QShortcut(QKeySequence("Ctrl+Return"), self)
        keep.activated.connect(lambda: self.save(keep_open=True))
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self.accept)

    # ------------------------------------------------------------------
    def summon(self, text: str = "") -> None:
        """Show centred on the screen under the mouse, ready to type."""
        self._fill_destinations()
        self._user_picked_destination = False
        self.status.setText("")
        self.input.setText(text)
        self.input.setStyleSheet("")
        self._update_preview()
        self.adjustSize()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.center() - QPoint(self.width() // 2, geo.height() // 4))
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def _fill_destinations(self) -> None:
        current = self.destination.currentData()
        self.destination.clear()
        self.destination.addItem("📥 Inbox", None)
        for ws in self.service.get_workspaces():
            for b in ws.boards:
                if not b.is_archived:
                    self.destination.addItem(f"📋 {b.name}  ·  {ws.name}", b.id)
        idx = self.destination.findData(current)
        self.destination.setCurrentIndex(max(0, idx))

    def _on_destination_picked(self, _index: int) -> None:
        self._user_picked_destination = True
        self._update_preview()

    def parse(self) -> cap.CaptureResult:
        return cap.parse_capture(self.input.text(), **cap.capture_context(self.service))

    def _update_preview(self) -> None:
        r = self.parse()
        if not self._user_picked_destination:  # follow '>board' both ways until the user picks
            idx = self.destination.findData(r.board_id) if r.board_id is not None else 0
            self.destination.setCurrentIndex(max(0, idx))
        parts = []
        if r.title:
            parts.append(f"<b style='color:#F8FAFC'>{html.escape(r.title)}</b>")
        if r.due:
            parts.append(chip(f"📅 {cap.describe_due(r.due)}", "#38BDF8"))
        if r.priority:
            p = Priority(r.priority)
            parts.append(chip(f"{p.icon_char} {p.display_name}", p.color))
        parts += [chip(f"#{l}", "#A78BFA") for l in r.labels]
        if r.assignee:
            parts.append(chip(f"@ {r.assignee}", "#F472B6"))
        if r.notes:
            parts.append(chip("📝 notes", "#94A3B8"))
        if r.warnings:
            parts.append(f"<span style='color:#FB923C'>⚠ {html.escape(r.warnings[0])}</span>")
        self.preview.setText(" &nbsp;".join(parts) if parts else
                             "<span style='color:#64748B'>Start typing - dates, priority, labels and people are "
                             "picked up automatically.</span>")

    def save(self, keep_open: bool = False) -> Optional[dict]:
        text = self.input.text().strip()
        if not text:
            self.input.setStyleSheet("border: 1px solid #EF4444;")
            return None
        board_id = self.destination.currentData()  # what the picker shows is where it goes
        try:
            saved = cap.commit_capture(self.service, text, board_id=board_id, force_inbox=board_id is None)
        except ValueError as exc:
            self.status.setStyleSheet("color: #F87171; border: none;")
            self.status.setText(str(exc))
            return None
        where = f"📋 {saved['board_name']}" if saved["kind"] == "task" else "📥 Inbox"
        self.status.setStyleSheet("color: #34D399; font-weight: 600; border: none;")
        self.status.setText(f"✓ Added to {where}")
        self.captured.emit({k: v for k, v in saved.items() if k != "result"})
        self.input.clear()
        self._user_picked_destination = False
        if not keep_open:
            self._close_timer.start(650)
        return saved


class QuickCaptureSettingsDialog(QDialog):
    def __init__(self, service: DatabaseService, status_text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.settings = load_settings(service)
        self.setWindowTitle("Quick Capture Settings")
        self.setStyleSheet(CHECKBOX_QSS)
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        self.sequence = QKeySequenceEdit(QKeySequence(self.settings["hotkey"]))
        form.addRow("Shortcut:", self.sequence)
        self.global_chk = QCheckBox("Work system-wide (even when KanbanManager is in the background)")
        self.global_chk.setChecked(bool(self.settings["global"]))
        form.addRow("", self.global_chk)
        self.status_lbl = QLabel(status_text)
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color: #94A3B8;")
        form.addRow("Status:", self.status_lbl)
        cli = QLabel("Works everywhere (including Wayland and macOS): bind a system shortcut to\n"
                     "    kanbanmanager --capture\n"
                     "or capture directly:  kanbanmanager --capture \"Call Ana tomorrow 3pm\"")
        cli.setTextInteractionFlags(Qt.TextSelectableByMouse)
        cli.setStyleSheet("color: #64748B; font-family: monospace; font-size: 11px;")
        form.addRow("", cli)
        self.error_lbl = QLabel("")
        self.error_lbl.setStyleSheet("color: #F87171;")
        form.addRow(self.error_lbl)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def value(self) -> dict:
        text = self.sequence.keySequence().toString(QKeySequence.PortableText)
        return {"hotkey": text or DEFAULT_HOTKEY, "global": self.global_chk.isChecked()}

    def save(self) -> None:
        value = self.value()
        try:
            parse_hotkey(value["hotkey"])
        except ValueError as exc:
            self.error_lbl.setText(str(exc))
            return
        self.service.set_setting(SETTINGS_KEY, value)
        self.accept()


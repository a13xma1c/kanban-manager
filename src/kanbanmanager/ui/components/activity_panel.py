"""
Task Comments & Audit-Trail Panel

A single timeline (oldest first, like a conversation) mixing Markdown comments
with the task's recorded history. Filters: All / Comments / History.

* Comments are posted immediately (they are discrete actions, not part of the
  task form). Only the author can edit or delete their own comments.
* History lists every ActivityLog entry with actor and time; description and
  comment edits can be opened as a diff.
"""
from __future__ import annotations

import difflib
import html
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.database.models import utc_now
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.enums import ActivityAction

_AVATAR_COLORS = ["#6366F1", "#3B82F6", "#06B6D4", "#10B981", "#F59E0B", "#EC4899", "#8B5CF6", "#EF4444"]

_ACTION_ICONS = {
    ActivityAction.TASK_CREATED.value: "✨",
    ActivityAction.TASK_UPDATED.value: "✏️",
    ActivityAction.TASK_MOVED.value: "➡️",
    ActivityAction.TASK_COMPLETED.value: "✅",
    ActivityAction.TASK_REOPENED.value: "↩️",
    ActivityAction.TASK_DELETED.value: "🗑",
    ActivityAction.COMMENT_ADDED.value: "💬",
    ActivityAction.COMMENT_EDITED.value: "💬",
    ActivityAction.COMMENT_DELETED.value: "🗑",
    ActivityAction.ATTACHMENT_ADDED.value: "📎",
    ActivityAction.ATTACHMENT_REMOVED.value: "📎",
    ActivityAction.DEPENDENCY_ADDED.value: "🔗",
    ActivityAction.DEPENDENCY_REMOVED.value: "🔗",
    ActivityAction.REMINDER_SET.value: "⏰",
    ActivityAction.TIME_LOGGED.value: "⏱",
    ActivityAction.TIME_ENTRY_UPDATED.value: "⏱",
    ActivityAction.TIME_ENTRY_DELETED.value: "⏱",
    ActivityAction.BOARD_UPDATED.value: "🧩",
}

# Field edits whose old/new text is worth showing as a diff
_DIFFABLE_FIELDS = {"description", "comment"}

FILTER_ALL = "all"
FILTER_COMMENTS = "comments"
FILTER_HISTORY = "history"


def _parse_utc(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def humanize_timestamp(ts: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Relative time for naive-UTC timestamps: 'just now', '5 minutes ago', 'yesterday', 'Mar 4, 2025'."""
    if ts is None:
        return ""
    now = now or utc_now()
    secs = (now - ts).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 45:
        return "just now"
    mins = int(secs // 60)
    if mins < 60:
        mins = max(1, mins)
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    hours = int(secs // 3600)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(secs // 86400)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    local = ts.replace(tzinfo=timezone.utc).astimezone()
    return local.strftime("%b %d, %Y").replace(" 0", " ")


def local_time_str(ts: Optional[datetime]) -> str:
    if ts is None:
        return ""
    return ts.replace(tzinfo=timezone.utc).astimezone().strftime("%A, %b %d, %Y at %H:%M")


def avatar_color(name: str) -> str:
    return _AVATAR_COLORS[sum(ord(c) for c in (name or "?")) % len(_AVATAR_COLORS)]


def initials(name: str) -> str:
    parts = [p for p in (name or "?").split() if p]
    if not parts:
        return "?"
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


def build_timeline(comments: List[Dict[str, Any]], activity: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    """Merge comments and history into one chronologically sorted list for the given filter."""
    items: List[Dict[str, Any]] = []
    if mode in (FILTER_ALL, FILTER_COMMENTS):
        items += [{"kind": "comment", "ts": _parse_utc(c["created_at"]), "data": c} for c in comments]
    if mode in (FILTER_ALL, FILTER_HISTORY):
        for a in activity:
            # In "All", live comments already represent their own add/edit events
            if mode == FILTER_ALL and a["action_type"] in (
                ActivityAction.COMMENT_ADDED.value,
                ActivityAction.COMMENT_EDITED.value,
            ):
                continue
            items.append({"kind": "history", "ts": _parse_utc(a["timestamp"]), "data": a})
    items.sort(key=lambda i: (i["ts"] or datetime.min, 0 if i["kind"] == "history" else 1))
    return items


class DiffDialog(QDialog):
    def __init__(self, title: str, old: str, new: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 460)
        layout = QVBoxLayout(self)
        diff = difflib.unified_diff(
            (old or "").splitlines(), (new or "").splitlines(), "before", "after", lineterm="", n=2
        )
        rows = []
        for line in diff:
            esc = html.escape(line) or "&nbsp;"
            if line.startswith("+") and not line.startswith("+++"):
                rows.append(f"<div style='background:#064E3B;color:#A7F3D0'>{esc}</div>")
            elif line.startswith("-") and not line.startswith("---"):
                rows.append(f"<div style='background:#7F1D1D;color:#FECACA'>{esc}</div>")
            elif line.startswith("@@"):
                rows.append(f"<div style='color:#93C5FD'>{esc}</div>")
            else:
                rows.append(f"<div style='color:#CBD5E1'>{esc}</div>")
        view = QLabel("<pre style='font-family:monospace'>" + "".join(rows) + "</pre>" if rows else "No textual changes.")
        view.setTextFormat(Qt.RichText)
        view.setTextInteractionFlags(Qt.TextSelectableByMouse)
        view.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(view)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class CommentEditDialog(QDialog):
    def __init__(self, body: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Comment")
        self.resize(520, 260)
        layout = QVBoxLayout(self)
        self.editor = QPlainTextEdit(body)
        layout.addWidget(self.editor, 1)
        hint = QLabel("Markdown supported")
        hint.setStyleSheet("color: #64748B; font-size: 11px;")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def body(self) -> str:
        return self.editor.toPlainText().strip()


class ActivityPanel(QWidget):
    comments_changed = Signal()

    def __init__(self, service: DatabaseService, task_id: Optional[int] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.task_id = task_id
        self.mode = FILTER_ALL
        self._comments: List[Dict[str, Any]] = []
        self._activity: List[Dict[str, Any]] = []
        self._init_ui()
        self.reload()

    # ------------------------------------------------------------------
    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.filter_bar = QWidget()
        top = QHBoxLayout(self.filter_bar)
        top.setContentsMargins(0, 0, 0, 0)
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        self.filter_buttons: Dict[str, QToolButton] = {}
        for key, label in ((FILTER_ALL, "All"), (FILTER_COMMENTS, "Comments"), (FILTER_HISTORY, "History")):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setStyleSheet(
                "QToolButton { background: #0F172A; color: #94A3B8; border: 1px solid #334155; padding: 4px 12px;"
                " border-radius: 4px; } QToolButton:checked { background: #334155; color: #F8FAFC; }"
            )
            b.clicked.connect(lambda _=False, k=key: self.set_filter(k))
            self._filter_group.addButton(b)
            self.filter_buttons[key] = b
            top.addWidget(b)
        self.filter_buttons[FILTER_ALL].setChecked(True)
        top.addStretch()
        self.summary_lbl = QLabel("")
        self.summary_lbl.setStyleSheet("color: #64748B; font-size: 11px;")
        top.addWidget(self.summary_lbl)
        layout.addWidget(self.filter_bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.timeline = QWidget()
        self.timeline_layout = QVBoxLayout(self.timeline)
        self.timeline_layout.setContentsMargins(0, 0, 6, 0)
        self.timeline_layout.setSpacing(6)
        self.timeline_layout.addStretch()
        self.scroll.setWidget(self.timeline)
        layout.addWidget(self.scroll, 1)

        self.composer = QFrame()
        self.composer.setObjectName("CommentComposer")
        self.composer.setStyleSheet(
            "#CommentComposer { background-color: #0F172A; border: 1px solid #334155; border-radius: 8px; }"
        )
        cl = QVBoxLayout(self.composer)
        cl.setContentsMargins(8, 8, 8, 8)
        cl.setSpacing(6)
        self.comment_input = QPlainTextEdit()
        self.comment_input.setPlaceholderText("Write a comment... (Markdown supported, Ctrl+Enter to post)")
        self.comment_input.setFixedHeight(72)
        cl.addWidget(self.comment_input)
        row = QHBoxLayout()
        self.as_lbl = QLabel(f"Commenting as <b>{html.escape(self.service.actor)}</b>")
        self.as_lbl.setStyleSheet("color: #64748B; font-size: 11px; border: none;")
        row.addWidget(self.as_lbl)
        row.addStretch()
        self.post_btn = QPushButton("Comment")
        self.post_btn.setObjectName("PrimaryButton")
        self.post_btn.clicked.connect(self.post_comment)
        row.addWidget(self.post_btn)
        cl.addLayout(row)
        layout.addWidget(self.composer)

        sc = QShortcut(QKeySequence("Ctrl+Return"), self.comment_input)
        sc.setContext(Qt.WidgetShortcut)
        sc.activated.connect(self.post_comment)

        self.placeholder = QLabel(
            "💬 Comments and the change history appear here once the task is created.\n"
            "Everything from that point on is recorded: edits, moves, attachments and comments."
        )
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setStyleSheet("color: #64748B; padding: 30px;")
        layout.addWidget(self.placeholder, 1)

    # ------------------------------------------------------------------
    def set_task(self, task_id: int) -> None:
        self.task_id = task_id
        self.reload()

    def set_filter(self, mode: str) -> None:
        self.mode = mode
        self.filter_buttons[mode].setChecked(True)
        self._render()

    def reload(self) -> None:
        has_task = self.task_id is not None
        self.scroll.setVisible(has_task)
        self.composer.setVisible(has_task)
        self.filter_bar.setVisible(has_task)
        self.placeholder.setVisible(not has_task)
        if not has_task:
            self._comments, self._activity = [], []
            self.summary_lbl.setText("")
            return
        self._comments = self.service.get_comments(self.task_id)
        self._activity = self.service.get_task_activity(self.task_id)
        self._render()

    def comment_count(self) -> int:
        return len(self._comments)

    def timeline_items(self) -> List[Dict[str, Any]]:
        return build_timeline(self._comments, self._activity, self.mode)

    def _render(self) -> None:
        while self.timeline_layout.count() > 1:
            item = self.timeline_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        items = self.timeline_items()
        for i, entry in enumerate(items):
            w = self._comment_widget(entry["data"]) if entry["kind"] == "comment" else self._history_widget(entry["data"])
            self.timeline_layout.insertWidget(i, w)
        if not items:
            empty = QLabel("No comments yet - start the conversation below." if self.mode == FILTER_COMMENTS
                           else "Nothing here yet.")
            empty.setStyleSheet("color: #64748B; padding: 16px;")
            empty.setAlignment(Qt.AlignCenter)
            self.timeline_layout.insertWidget(0, empty)
        n_c, n_h = len(self._comments), len(self._activity)
        self.summary_lbl.setText(f"{n_c} comment{'s' if n_c != 1 else ''} · {n_h} history event{'s' if n_h != 1 else ''}")
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    # ------------------------------------------------------------------
    def _avatar(self, name: str, size: int) -> QLabel:
        lbl = QLabel(initials(name))
        lbl.setFixedSize(size, size)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            f"background-color: {avatar_color(name)}; color: white; border-radius: {size // 2}px;"
            f" font-weight: 700; font-size: {max(9, size // 3)}px; border: none;"
        )
        return lbl

    def _time_label(self, iso: Optional[str]) -> QLabel:
        ts = _parse_utc(iso)
        lbl = QLabel(humanize_timestamp(ts))
        lbl.setToolTip(local_time_str(ts))
        lbl.setStyleSheet("color: #64748B; font-size: 11px; border: none; background: transparent;")
        return lbl

    def _comment_widget(self, c: Dict[str, Any]) -> QWidget:
        frame = QFrame()
        frame.setObjectName("CommentCard")
        frame.setStyleSheet("#CommentCard { background-color: #0F172A; border: 1px solid #334155; border-radius: 8px; }")
        outer = QHBoxLayout(frame)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(10)
        outer.addWidget(self._avatar(c["author"], 30), 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(4)
        head = QHBoxLayout()
        name = QLabel(html.escape(c["author"] or "Unknown"))
        name.setStyleSheet("color: #F8FAFC; font-weight: 700; border: none;")
        head.addWidget(name)
        head.addWidget(self._time_label(c["created_at"]))
        if c.get("is_edited"):
            edited = QLabel("(edited)")
            edited.setToolTip("Edited " + local_time_str(_parse_utc(c.get("updated_at"))))
            edited.setStyleSheet("color: #64748B; font-size: 11px; font-style: italic; border: none;")
            head.addWidget(edited)
        head.addStretch()
        if c["author"] == self.service.actor:
            for text, slot in (("Edit", lambda: self.edit_comment(c["id"])), ("Delete", lambda: self.delete_comment(c["id"]))):
                b = QToolButton()
                b.setText(text)
                b.setStyleSheet(
                    "QToolButton { color: #94A3B8; background: transparent; border: none; font-size: 11px; }"
                    "QToolButton:hover { color: #F8FAFC; text-decoration: underline; }"
                )
                b.clicked.connect(slot)
                head.addWidget(b)
        col.addLayout(head)

        body = QLabel()
        body.setTextFormat(Qt.MarkdownText)
        body.setText(c["body"])
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(Qt.TextBrowserInteraction)
        body.setStyleSheet("color: #E2E8F0; border: none; background: transparent;")
        col.addWidget(body)
        outer.addLayout(col, 1)
        return frame

    def _history_widget(self, a: Dict[str, Any]) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(6, 2, 6, 2)
        row.setSpacing(8)
        icon = QLabel("🧩" if a.get("field_name") == "custom_field" else _ACTION_ICONS.get(a["action_type"], "•"))
        icon.setFixedWidth(22)
        icon.setAlignment(Qt.AlignCenter)
        row.addWidget(icon, 0, Qt.AlignTop)
        text = html.escape(a.get("details") or a["action_type"])
        actor = a.get("actor")
        line = QLabel(f"<b style='color:#CBD5E1'>{html.escape(actor)}</b> &nbsp;<span>{text}</span>" if actor else text)
        line.setTextFormat(Qt.RichText)
        line.setWordWrap(True)
        line.setStyleSheet("color: #94A3B8; font-size: 12px;")
        row.addWidget(line, 1)
        if a.get("field_name") in _DIFFABLE_FIELDS and a.get("old_value") and a.get("new_value"):
            diff_btn = QToolButton()
            diff_btn.setText("View changes")
            diff_btn.setStyleSheet(
                "QToolButton { color: #60A5FA; background: transparent; border: none; font-size: 11px; }"
                "QToolButton:hover { text-decoration: underline; }"
            )
            diff_btn.clicked.connect(lambda: DiffDialog(
                f"Changes to {a['field_name']}", a["old_value"], a["new_value"], self).exec())
            row.addWidget(diff_btn, 0, Qt.AlignTop)
        row.addWidget(self._time_label(a["timestamp"]), 0, Qt.AlignTop)
        return w

    # ------------------------------------------------------------------
    # Comment actions
    # ------------------------------------------------------------------
    def post_comment(self) -> None:
        if self.task_id is None:
            return
        body = self.comment_input.toPlainText().strip()
        if not body:
            return
        self.service.add_comment(self.task_id, body)
        self.comment_input.clear()
        self.reload()
        self.comments_changed.emit()

    def edit_comment(self, comment_id: int) -> None:
        current = next((c for c in self._comments if c["id"] == comment_id), None)
        if not current:
            return
        dlg = CommentEditDialog(current["body"], self)
        if dlg.exec() and dlg.body():
            self.service.update_comment(comment_id, dlg.body())
            self.reload()
            self.comments_changed.emit()

    def delete_comment(self, comment_id: int, confirm: bool = True) -> None:
        if confirm:
            reply = QMessageBox.question(
                self, "Delete Comment", "Delete this comment? The deletion is kept in the task history.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        self.service.delete_comment(comment_id)
        self.reload()
        self.comments_changed.emit()

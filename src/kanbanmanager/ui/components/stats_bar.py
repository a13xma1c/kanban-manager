"""
Board Statistics and WIP Limits Summary Bar
"""
from __future__ import annotations

from typing import Optional, Dict, Any, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QFrame,
    QPushButton,
)


class StatsBar(QFrame):
    """
    Sub-header strip displaying summary statistics for the active board:
    Total tasks, completed rate, active WIP count, overdue count, blocked count.
    """

    automations_requested = Signal()
    archive_requested = Signal()
    fields_requested = Signal()
    templates_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("StatsBarWidget")
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(16)

        self.total_lbl = QLabel("Total: 0")
        self.total_lbl.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.total_lbl)

        self.wip_lbl = QLabel("In Progress: 0")
        self.wip_lbl.setStyleSheet("color: #38BDF8; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.wip_lbl)

        self.completed_lbl = QLabel("Completed: 0 (0%)")
        self.completed_lbl.setStyleSheet("color: #10B981; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.completed_lbl)

        self.overdue_lbl = QLabel("Overdue: 0")
        self.overdue_lbl.setStyleSheet("color: #EF4444; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.overdue_lbl)

        self.blocked_lbl = QLabel("Blocked: 0")
        self.blocked_lbl.setStyleSheet("color: #F59E0B; font-size: 11px; font-weight: 600;")
        layout.addWidget(self.blocked_lbl)

        layout.addStretch()

        link_style = (
            "QPushButton { background: transparent; color: #94A3B8; border: none; font-size: 11px;"
            " font-weight: 600; padding: 0 4px; }"
            "QPushButton:hover { color: #F8FAFC; text-decoration: underline; }"
        )
        self.fields_btn = QPushButton("🧩 Fields")
        self.fields_btn.setToolTip("Custom fields of this board (story points, severity, links …)")
        self.fields_btn.setStyleSheet(link_style)
        self.fields_btn.setCursor(Qt.PointingHandCursor)
        self.fields_btn.clicked.connect(self.fields_requested.emit)
        layout.addWidget(self.fields_btn)
        self.templates_btn = QPushButton("📋 Templates")
        self.templates_btn.setToolTip("Task templates of this board")
        self.templates_btn.setStyleSheet(link_style)
        self.templates_btn.setCursor(Qt.PointingHandCursor)
        self.templates_btn.clicked.connect(self.templates_requested.emit)
        layout.addWidget(self.templates_btn)
        self.automations_btn = QPushButton("⚡ Automations")
        self.automations_btn.setToolTip("Rules, auto-archiving and stale-task policies for this board")
        self.automations_btn.setStyleSheet(link_style)
        self.automations_btn.setCursor(Qt.PointingHandCursor)
        self.automations_btn.clicked.connect(self.automations_requested.emit)
        layout.addWidget(self.automations_btn)
        self.archive_btn = QPushButton("🗄 Archived")
        self.archive_btn.setToolTip("Browse and restore archived tasks")
        self.archive_btn.setStyleSheet(link_style)
        self.archive_btn.setCursor(Qt.PointingHandCursor)
        self.archive_btn.clicked.connect(self.archive_requested.emit)
        layout.addWidget(self.archive_btn)


    def update_stats(self, tasks: List[Dict[str, Any]]) -> None:
        total = len(tasks)
        completed = sum(1 for t in tasks if t.get("is_completed"))
        overdue = sum(1 for t in tasks if t.get("is_overdue"))
        blocked = sum(1 for t in tasks if t.get("is_blocked"))
        in_progress = total - completed

        pct = int((completed / total) * 100) if total > 0 else 0

        # Full and compact wording; the compact one is used when the bar is too narrow
        self._texts = {
            self.total_lbl: (f"Total Tasks: {total}", f"{total} tasks"),
            self.wip_lbl: (f"Active Tasks: {in_progress}", f"{in_progress} active"),
            self.completed_lbl: (f"Completed: {completed} ({pct}%)", f"{completed} done ({pct}%)"),
            self.overdue_lbl: (f"⚠️ Overdue: {overdue}", f"⚠️ {overdue}"),
            self.blocked_lbl: (f"🔒 Blocked: {blocked}", f"🔒 {blocked}"),
        }
        for lbl, (full, _short) in self._texts.items():
            lbl.setToolTip(full)
        self._fit_texts()

    def set_automation_counts(self, active_rules: int, active_policies: int, archived: int,
                              fields: int = 0, templates: int = 0) -> None:
        active = active_rules + active_policies
        self._button_texts = {
            self.fields_btn: (f"🧩 Fields ({fields})" if fields else "🧩 Fields", f"🧩 {fields}" if fields else "🧩"),
            self.templates_btn: (f"📋 Templates ({templates})" if templates else "📋 Templates",
                                 f"📋 {templates}" if templates else "📋"),
            self.automations_btn: (f"⚡ Automations ({active} active)" if active else "⚡ Automations",
                                   f"⚡ {active}" if active else "⚡"),
            self.archive_btn: (f"🗄 Archived ({archived})" if archived else "🗄 Archived",
                               f"🗄 {archived}" if archived else "🗄"),
        }
        self._fit_texts()

    def _fit_texts(self) -> None:
        """Use full labels when they fit, compact ones otherwise (never truncate or overlap)."""
        items = list(getattr(self, "_texts", {}).items()) + list(getattr(self, "_button_texts", {}).items())
        if not items:
            return
        margins = self.layout().contentsMargins()
        spacing = self.layout().spacing() * (self.layout().count() - 1)
        available = self.width() - margins.left() - margins.right() - spacing

        buttons = {w for w, _t in getattr(self, "_button_texts", {}).items()}

        def choice(widget, level: int) -> int:
            # 0 = everything full; 1 = compact numbers, full links; 2 = everything compact
            return 0 if level == 0 else (0 if level == 1 and widget in buttons else 1)

        def needed(level: int) -> int:
            total = 0
            for widget, texts in items:
                total += widget.fontMetrics().horizontalAdvance(texts[choice(widget, level)]) + (
                    24 if isinstance(widget, QPushButton) else 4)
            return total

        level = next((lv for lv in (0, 1) if needed(lv) <= available), 2)
        for widget, texts in items:
            widget.setText(texts[choice(widget, level)])

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_texts()

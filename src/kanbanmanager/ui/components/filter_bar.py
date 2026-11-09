"""
Top Header, Search, Filter Bar, and View Mode Switcher
"""
from __future__ import annotations

from typing import Optional, List, Dict, Any

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLineEdit,
    QComboBox,
    QPushButton,
    QLabel,
    QFrame,
    QButtonGroup,
    QSizePolicy,
)

from kanbanmanager.enums import Priority, ViewMode
from kanbanmanager.core.signals import app_signals


class FilterBar(QFrame):
    """
    Control bar providing search input, priority/label/due date filters,
    and View Mode switching (Board, Calendar, Timeline, Analytics).
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("TopHeaderBar")
        self._init_ui()

    # Narrowest a filter combo may get before its text is clipped (never overlaps)
    COMBO_MIN_WIDTH = 104
    COMBO_CHROME_WIDTH = 52  # padding + drop-down arrow around the text
    SEARCH_MIN_WIDTH = 200
    SEARCH_MAX_WIDTH = 420

    def _init_ui(self) -> None:
        # Two rows so every control keeps a usable size down to the minimum window
        # width: [search ............ view switcher] / [filters ... clear]
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 8, 16, 8)
        main_layout.setSpacing(8)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        main_layout.addLayout(top_row)
        main_layout.addLayout(filter_row)

        # ---------------------------------------------------------------------
        # Search Box (takes the free space in the top row, within limits)
        # ---------------------------------------------------------------------
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Search tasks by title or description...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMinimumWidth(self.SEARCH_MIN_WIDTH)
        self.search_input.setMaximumWidth(self.SEARCH_MAX_WIDTH)
        self.search_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.search_input.textChanged.connect(self._emit_filters)
        top_row.addWidget(self.search_input, 1)
        top_row.addStretch()

        # ---------------------------------------------------------------------
        # Priority Filter
        # ---------------------------------------------------------------------
        self.priority_combo = QComboBox()
        self.priority_combo.addItem("All Priorities", None)
        for p in Priority:
            self.priority_combo.addItem(f"{p.icon_char} {p.display_name}", p.value)
        self.priority_combo.currentIndexChanged.connect(self._emit_filters)

        # ---------------------------------------------------------------------
        # Label Filter
        # ---------------------------------------------------------------------
        self.label_combo = QComboBox()
        self.label_combo.addItem("All Labels", None)
        self.label_combo.currentIndexChanged.connect(self._emit_filters)

        # ---------------------------------------------------------------------
        # Due Date Filter
        # ---------------------------------------------------------------------
        self.due_combo = QComboBox()
        self.due_combo.addItem("All Due Dates", "all")
        self.due_combo.addItem("⚠️ Overdue", "overdue")
        self.due_combo.addItem("📅 Due Today", "today")
        self.due_combo.addItem("🗓 Due This Week", "this_week")
        self.due_combo.addItem("Has Due Date", "has_due")
        self.due_combo.currentIndexChanged.connect(self._emit_filters)

        # ---------------------------------------------------------------------
        # Status Filter (Active / Completed)
        # ---------------------------------------------------------------------
        self.status_combo = QComboBox()
        self.status_combo.addItem("All Tasks", None)
        self.status_combo.addItem("Active Only", False)
        self.status_combo.addItem("Completed Only", True)
        self.status_combo.currentIndexChanged.connect(self._emit_filters)

        for combo in (self.priority_combo, self.label_combo, self.due_combo, self.status_combo):
            # Natural width when there is room. When space is tight it may shrink, but
            # never so far that its default "All ..." text is clipped or widgets overlap.
            default_text_width = combo.fontMetrics().horizontalAdvance(combo.itemText(0))
            combo.setMinimumWidth(max(self.COMBO_MIN_WIDTH, default_text_width + self.COMBO_CHROME_WIDTH))
            combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            filter_row.addWidget(combo)

        # Reset Filter Button
        self.reset_btn = QPushButton("✕ Clear")
        self.reset_btn.setToolTip("Reset all filters")
        self.reset_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.reset_btn.clicked.connect(self.clear_filters)
        filter_row.addWidget(self.reset_btn)
        filter_row.addStretch()

        # ---------------------------------------------------------------------
        # View Mode Switcher: Board | Calendar | Timeline | Analytics
        # A segmented control whose buttons always keep their full label width.
        # ---------------------------------------------------------------------
        switcher = QFrame()
        switcher.setObjectName("ViewSwitcher")
        switcher.setStyleSheet(
            "#ViewSwitcher { background-color: #0F172A; border: 1px solid #334155; border-radius: 7px; }"
            "#ViewSwitcher QPushButton { background: transparent; color: #94A3B8; border: none;"
            " border-radius: 5px; padding: 5px 12px; font-weight: 600; }"
            "#ViewSwitcher QPushButton:hover { background-color: #1E293B; color: #F8FAFC; }"
            "#ViewSwitcher QPushButton:checked { background-color: #3B82F6; color: #FFFFFF; }"
        )
        views_layout = QHBoxLayout(switcher)
        views_layout.setContentsMargins(3, 3, 3, 3)
        views_layout.setSpacing(2)

        self.btn_group = QButtonGroup(self)
        self.btn_group.setExclusive(True)

        def view_button(text: str, tip: str) -> QPushButton:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.btn_group.addButton(btn)
            views_layout.addWidget(btn)
            return btn

        self.btn_board = view_button("📋 Board", "Kanban board view")
        self.btn_calendar = view_button("📅 Calendar", "Month/week calendar of due dates")
        self.btn_timeline = view_button("📈 Timeline", "Timeline / Gantt view with dependencies")
        self.btn_analytics = view_button("📊 Analytics", "Board metrics and activity")
        self.btn_board.setChecked(True)

        self.btn_board.clicked.connect(lambda: app_signals.view_mode_changed.emit(ViewMode.BOARD.value))
        self.btn_calendar.clicked.connect(lambda: app_signals.view_mode_changed.emit(ViewMode.CALENDAR.value))
        self.btn_timeline.clicked.connect(lambda: app_signals.view_mode_changed.emit(ViewMode.TIMELINE.value))
        self.btn_analytics.clicked.connect(lambda: app_signals.view_mode_changed.emit(ViewMode.ANALYTICS.value))

        top_row.addWidget(switcher)

    def set_board_labels(self, labels: List[Dict[str, Any]]) -> None:
        """Update available labels in label filter dropdown."""
        current_data = self.label_combo.currentData()
        self.label_combo.blockSignals(True)
        self.label_combo.clear()
        self.label_combo.addItem("All Labels", None)
        for lbl in labels:
            self.label_combo.addItem(f"🏷 {lbl['name']}", lbl['id'])

        # Restore selection if exists
        idx = self.label_combo.findData(current_data)
        if idx >= 0:
            self.label_combo.setCurrentIndex(idx)
        self.label_combo.blockSignals(False)

    def clear_filters(self) -> None:
        self.search_input.clear()
        self.priority_combo.setCurrentIndex(0)
        self.label_combo.setCurrentIndex(0)
        self.due_combo.setCurrentIndex(0)
        self.status_combo.setCurrentIndex(0)
        self._emit_filters()

    def _emit_filters(self) -> None:
        filters = {
            "search": self.search_input.text().strip(),
            "priority": self.priority_combo.currentData(),
            "label_id": self.label_combo.currentData(),
            "due_filter": self.due_combo.currentData(),
            "is_completed": self.status_combo.currentData(),
        }
        app_signals.filter_changed.emit(filters)

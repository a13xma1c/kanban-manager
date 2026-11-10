"""
Board Analytics: KPIs, cumulative flow, burn-up, throughput, cycle time and
breakdowns as vector charts (each exportable to SVG / PDF / PNG), the board
PDF report, and recent activity.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import analytics as an
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.charts import ChartWidget, export_chart
from kanbanmanager.ui.reports import build_charts, write_board_report


class AnalyticsCard(QFrame):
    """Reusable metric summary card widget."""

    def __init__(self, title: str, value: str, subtitle: str = "", color: str = "#3B82F6", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            f"AnalyticsCard {{ background-color: #1E293B; border: 1px solid #334155; border-radius: 8px; padding: 12px; }} "
            f"AnalyticsCard:hover {{ border-color: {color}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        t_lbl = QLabel(title)
        t_lbl.setStyleSheet("color: #94A3B8; font-size: 11px; font-weight: 600; text-transform: uppercase;")
        layout.addWidget(t_lbl)

        v_lbl = QLabel(value)
        v_lbl.setStyleSheet(f"color: {color}; font-size: 22px; font-weight: 800;")
        layout.addWidget(v_lbl)

        if subtitle:
            s_lbl = QLabel(subtitle)
            s_lbl.setStyleSheet("color: #64748B; font-size: 11px;")
            layout.addWidget(s_lbl)


class AnalyticsView(QWidget):
    """KPIs, flow charts and breakdowns for the current board."""

    message = Signal(str)   # status-bar text (e.g. after an export)

    CHART_ORDER = ["flow", "burnup", "throughput", "cycle", "columns", "priority", "assignee", "label", "field"]

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.current_board_id: Optional[int] = None
        self.charts: Dict[str, ChartWidget] = {}
        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(10)

        top = QHBoxLayout()
        main_layout.addLayout(top)
        title = QLabel("📊 Analytics")
        title.setStyleSheet("font-size: 16px; font-weight: 800;")
        top.addWidget(title)
        top.addStretch()
        self.period_combo = QComboBox()
        for label, days in an.PERIODS.items():
            self.period_combo.addItem(label, days)
        self.period_combo.setCurrentIndex(1)
        self.period_combo.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.period_combo)
        self.points_combo = QComboBox()
        self.points_combo.setToolTip("Burn-up and breakdowns: count tasks, or add up a number field")
        self.points_combo.currentIndexChanged.connect(self._on_points)
        top.addWidget(self.points_combo)
        self.report_btn = QPushButton("⬇ PDF Report…")
        self.report_btn.setObjectName("PrimaryButton")
        self.report_btn.setToolTip("KPIs, every chart and the task list per column, ready to print or share")
        self.report_btn.clicked.connect(self.export_report)
        top.addWidget(self.report_btn)
        self.charts_btn = QToolButton()
        self.charts_btn.setText("⬇ All Charts")
        self.charts_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.charts_btn)
        for fmt, label in (("svg", "as SVG (vector)…"), ("pdf", "as PDF (vector)…"), ("png", "as PNG…")):
            menu.addAction(label).triggered.connect(self._export_all_handler(fmt))
        self.charts_btn.setMenu(menu)
        top.addWidget(self.charts_btn)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setStyleSheet("background: transparent;")  # cascades: cards and labels sit on the page
        main_layout.addWidget(scroll_area, 1)
        container = QWidget()
        self.content_layout = QVBoxLayout(container)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(16)
        scroll_area.setWidget(container)

    def _export_all_handler(self, fmt: str):
        def handler(_checked: bool = False) -> None:
            self.export_all_charts(fmt)
        return handler

    def load_board(self, board_id: int) -> None:
        if board_id != self.current_board_id:
            self.current_board_id = board_id
            self._fill_points()
        self.refresh()

    def _fill_points(self) -> None:
        self.points_combo.blockSignals(True)
        current = self.points_combo.currentData()
        self.points_combo.clear()
        self.points_combo.addItem("Count tasks", None)
        if self.current_board_id:
            for f in self.service.get_custom_fields(self.current_board_id):
                if f["field_type"] in ("number", "currency", "rating"):
                    self.points_combo.addItem(f"Sum of {f['name']}", f["id"])
        idx = self.points_combo.findData(current) if current is not None else -1
        if idx < 0:  # story points are the usual choice when a board has them
            idx = next((i for i in range(self.points_combo.count())
                        if "point" in self.points_combo.itemText(i).lower()), 0)
        self.points_combo.setCurrentIndex(idx)
        self.points_combo.blockSignals(False)

    def _on_points(self, _index: int) -> None:
        self.refresh()

    @property
    def days(self) -> int:
        return int(self.period_combo.currentData() or 30)

    def refresh(self) -> None:
        if not self.current_board_id:
            return
        while self.content_layout.count() > 0:
            item = self.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.charts = {}
        board = self.service.get_board(self.current_board_id)
        if not board:
            return
        data = an.load_board(self.service, self.current_board_id)
        start, end = an.default_range(self.days)
        k = an.kpis(data, start, end)

        kpi_host = QWidget()
        self.content_layout.addWidget(kpi_host)
        kpi_grid = QGridLayout(kpi_host)
        kpi_grid.setContentsMargins(0, 0, 0, 0)
        kpi_grid.setSpacing(12)
        cards = [
            AnalyticsCard("Completion Rate", f"{k['completion_pct']}%", f"{k['done']} of {k['total']} tasks done",
                          "#10B981"),
            AnalyticsCard("Active WIP", str(k["open"]), "Tasks currently in the workflow", "#3B82F6"),
            AnalyticsCard("Throughput", f"{k['per_week']:g}/wk", f"{k['completed_in_period']} done in the period",
                          "#8B5CF6"),
            AnalyticsCard("Cycle Time", "–" if k["avg_cycle"] is None else f"{k['avg_cycle']:g} d",
                          "average, start to done" + (f" · 85% ≤ {k['p85_cycle']:g} d" if k["p85_cycle"] else ""),
                          "#F59E0B"),
            AnalyticsCard("Overdue Alerts", str(k["overdue"]), "Tasks past due date",
                          "#EF4444" if k["overdue"] else "#64748B"),
            AnalyticsCard("Blocked Tasks", str(k["blocked"]), "Waiting on prerequisites",
                          "#F59E0B" if k["blocked"] else "#64748B"),
        ]
        for i, card in enumerate(cards):
            kpi_grid.addWidget(card, i // 3, i % 3)
        if k["wip_over"]:
            warn = QLabel("⚠ Over the WIP limit: " + ", ".join(k["wip_over"]) + " - finish work before starting more")
            warn.setStyleSheet("color: #F87171; font-weight: 600;")
            self.content_layout.addWidget(warn)

        charts = build_charts(self.service, self.current_board_id, self.days, self.points_combo.currentData(),
                              data=data)
        grid_host = QWidget()
        self.content_layout.addWidget(grid_host)
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        for i, key in enumerate(k for k in self.CHART_ORDER if k in charts):
            widget = ChartWidget(charts[key])
            widget.exported.connect(self._on_exported)
            grid.addWidget(widget, i // 2, i % 2)
            self.charts[key] = widget

        # Recent activity
        act_frame = QFrame()
        act_frame.setStyleSheet("background-color: #1E293B; border: 1px solid #334155; border-radius: 8px; padding: 14px;")
        self.content_layout.addWidget(act_frame)
        act_layout = QVBoxLayout(act_frame)
        act_layout.setSpacing(10)
        act_title = QLabel("🕒 Recent Board Activity History")
        act_title.setStyleSheet("font-size: 14px; font-weight: 700; color: #F8FAFC;")
        act_layout.addWidget(act_title)
        logs = self.service.get_board_activities(self.current_board_id, limit=15)
        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["Timestamp", "By", "Action", "Details"])
        for c in range(3):
            table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        table.setRowCount(len(logs))
        table.setMinimumHeight(260)
        table.setStyleSheet("QTableWidget { background: #0F172A; border: none; } "
                            "QHeaderView::section { background: #1E293B; color: #94A3B8; }")
        for r, entry in enumerate(logs):
            ts = (entry.get("timestamp") or "").replace("T", " ")[:19]
            table.setItem(r, 0, QTableWidgetItem(ts))
            table.setItem(r, 1, QTableWidgetItem(entry.get("actor", "")))
            table.setItem(r, 2, QTableWidgetItem(entry.get("action_type", "")))
            table.setItem(r, 3, QTableWidgetItem(entry.get("details", "")))
        act_layout.addWidget(table)

    # Export -------------------------------------------------------------------
    def _on_exported(self, path: str) -> None:
        self.message.emit(f"⬇ Saved {Path(path).name}")

    def export_report(self, _checked: bool = False, path: Optional[str] = None) -> Optional[Path]:
        if not self.current_board_id:
            return None
        board = self.service.get_board(self.current_board_id)
        if path is None:
            safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in board.name).strip() or "board"
            path, _ = QFileDialog.getSaveFileName(self, "Export Board Report", f"{safe} report.pdf", "PDF (*.pdf)")
        if not path:
            return None
        dest = write_board_report(self.service, self.current_board_id, path, days=self.days,
                                  points_field_id=self.points_combo.currentData())
        self.message.emit(f"⬇ Saved the report to {dest.name}")
        return dest

    def export_all_charts(self, fmt: str, folder: Optional[str] = None) -> List[Path]:
        if not self.charts:
            return []
        if folder is None:
            folder = QFileDialog.getExistingDirectory(self, "Save Charts To")
        if not folder:
            return []
        out = []
        for key, widget in self.charts.items():
            safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in widget.chart.title).strip() or key
            out.append(export_chart(widget.chart, Path(folder) / f"{safe}.{fmt}", fmt))
        self.message.emit(f"⬇ Saved {len(out)} charts to {folder}")
        return out

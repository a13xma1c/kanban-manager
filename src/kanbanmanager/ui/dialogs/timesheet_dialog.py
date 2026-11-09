"""
Timesheet Preview & Export Dialog
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import timesheet as ts
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.timefmt import format_duration

SCOPE_BOARD = "board"
SCOPE_WORKSPACE = "workspace"
SCOPE_ALL = "all"
CUSTOM = "Custom range"


class TimesheetDialog(QDialog):
    def __init__(
        self,
        service: DatabaseService,
        board_id: Optional[int] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.rows: List[ts.TimesheetRow] = []
        self.setWindowTitle("Timesheet")
        self.resize(900, 600)
        self._init_ui()
        self.refresh()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()

        controls.addWidget(QLabel("Period:"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(ts.PRESETS + [CUSTOM])
        self.preset_combo.currentTextChanged.connect(self._on_preset)
        controls.addWidget(self.preset_combo)
        start, end = ts.preset_range(ts.PRESET_THIS_WEEK)
        self.start_edit = QDateEdit(QDate(start.year, start.month, start.day))
        self.end_edit = QDateEdit(QDate(end.year, end.month, end.day))
        for edit in (self.start_edit, self.end_edit):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("ddd, MMM d yyyy")
            edit.dateChanged.connect(self._on_dates_edited)
        controls.addWidget(self.start_edit)
        controls.addWidget(QLabel("to"))
        controls.addWidget(self.end_edit)

        controls.addSpacing(10)
        controls.addWidget(QLabel("Scope:"))
        self.scope_combo = QComboBox()
        if self.board_id:
            self.scope_combo.addItem("Current board", SCOPE_BOARD)
            self.scope_combo.addItem("Current workspace", SCOPE_WORKSPACE)
        self.scope_combo.addItem("All workspaces", SCOPE_ALL)
        self.scope_combo.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.scope_combo)

        controls.addWidget(QLabel("Person:"))
        self.person_combo = QComboBox()
        self.person_combo.addItem("Everyone", None)
        self.person_combo.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.person_combo)
        controls.addStretch()
        layout.addLayout(controls)

        group_row = QHBoxLayout()
        group_row.addWidget(QLabel("Show:"))
        self.group_combo = QComboBox()
        self.group_combo.addItem("All entries", ts.GROUP_ENTRIES)
        self.group_combo.addItem("Totals by day", ts.GROUP_DAY)
        self.group_combo.addItem("Totals by task", ts.GROUP_TASK)
        self.group_combo.addItem("Totals by person", ts.GROUP_PERSON)
        self.group_combo.currentIndexChanged.connect(self._render)
        group_row.addWidget(self.group_combo)
        group_row.addStretch()
        self.total_lbl = QLabel("")
        self.total_lbl.setStyleSheet("font-weight: 700; font-size: 13px;")
        group_row.addWidget(self.total_lbl)
        layout.addLayout(group_row)

        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.note_lbl = QLabel("Times are in your local time zone. Running timers appear once stopped.")
        self.note_lbl.setStyleSheet("color: #64748B; font-size: 11px;")
        buttons.addWidget(self.note_lbl)
        buttons.addStretch()
        self.csv_btn = QPushButton("Export CSV (entries)")
        self.csv_btn.clicked.connect(lambda: self.export("csv"))
        self.summary_btn = QPushButton("Export Summary CSV")
        self.summary_btn.setToolTip("One row per person and task, one column per day")
        self.summary_btn.clicked.connect(lambda: self.export("summary"))
        self.pdf_btn = QPushButton("Export PDF")
        self.pdf_btn.setObjectName("PrimaryButton")
        self.pdf_btn.clicked.connect(lambda: self.export("pdf"))
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        for b in (self.csv_btn, self.summary_btn, self.pdf_btn, close_btn):
            buttons.addWidget(b)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------
    def period(self) -> tuple[date, date]:
        return self.start_edit.date().toPython(), self.end_edit.date().toPython()

    def _on_preset(self, name: str) -> None:
        if name == CUSTOM:
            return
        start, end = ts.preset_range(name)
        for edit, d in ((self.start_edit, start), (self.end_edit, end)):
            edit.blockSignals(True)
            edit.setDate(QDate(d.year, d.month, d.day))
            edit.blockSignals(False)
        self.refresh()

    def _on_dates_edited(self) -> None:
        if self.start_edit.date() > self.end_edit.date():
            self.end_edit.blockSignals(True)
            self.end_edit.setDate(self.start_edit.date())
            self.end_edit.blockSignals(False)
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentText(CUSTOM)
        self.preset_combo.blockSignals(False)
        self.refresh()

    def _board_ids(self) -> Optional[List[int]]:
        scope = self.scope_combo.currentData()
        if scope == SCOPE_BOARD:
            return [self.board_id]
        if scope == SCOPE_WORKSPACE:
            board = self.service.get_board(self.board_id)
            return [b.id for b in self.service.get_boards_for_workspace(board.workspace_id)] if board else []
        return None

    def scope_label(self) -> str:
        scope = self.scope_combo.currentData()
        if scope in (SCOPE_BOARD, SCOPE_WORKSPACE):
            board = self.service.get_board(self.board_id)
            if board is not None:
                if scope == SCOPE_BOARD:
                    return f"Board: {board.name}"
                ws = self.service.get_workspace(board.workspace_id)
                return f"Workspace: {ws.name if ws else ''}"
        return "All workspaces"

    def refresh(self) -> None:
        start, end = self.period()
        utc_start, utc_end = ts.local_range_to_utc(start, end)
        entries = self.service.get_time_entries(board_ids=self._board_ids(), start=utc_start, end=utc_end)
        all_rows = ts.build_rows(entries)
        # Keep the person list in sync with the data in range
        people = sorted({r.person for r in all_rows}, key=str.lower)
        current = self.person_combo.currentData()
        self.person_combo.blockSignals(True)
        self.person_combo.clear()
        self.person_combo.addItem("Everyone", None)
        for p in people:
            self.person_combo.addItem(p, p)
        idx = self.person_combo.findData(current)
        self.person_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.person_combo.blockSignals(False)
        person = self.person_combo.currentData()
        self.rows = [r for r in all_rows if person is None or r.person == person]
        self._render()

    def _render(self) -> None:
        group = self.group_combo.currentData()
        if group == ts.GROUP_ENTRIES:
            headers = ["Date", "Time", "Person", "Board", "Task", "Note", "Source", "Duration"]
            data = [
                [r.day.strftime("%a %Y-%m-%d"), f"{r.start:%H:%M}-{r.end:%H:%M}", r.person, r.board, r.task,
                 r.note, r.source, format_duration(r.seconds)]
                for r in self.rows
            ]
        else:
            headers = ts.GROUP_HEADERS[group] + ["Hours", "Duration"]
            data = [list(key) + [f"{secs / 3600:.2f}", format_duration(secs)]
                    for key, secs in ts.group_rows(self.rows, group)]
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(data))
        for r, values in enumerate(data):
            for c, v in enumerate(values):
                item = QTableWidgetItem(str(v))
                if c >= len(headers) - (1 if group == ts.GROUP_ENTRIES else 2):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)
        hdr = self.table.horizontalHeader()
        for c in range(len(headers)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        stretch = headers.index("Task") if "Task" in headers else 0
        hdr.setSectionResizeMode(stretch, QHeaderView.Stretch)
        total = ts.total_seconds(self.rows)
        self.total_lbl.setText(f"Total: {format_duration(total) if total else '0m'}  ({total / 3600:.2f} h) · "
                               f"{len(self.rows)} entr{'y' if len(self.rows) == 1 else 'ies'}")
        has_rows = bool(self.rows)
        for b in (self.csv_btn, self.summary_btn, self.pdf_btn):
            b.setEnabled(has_rows)

    # ------------------------------------------------------------------
    def default_filename(self, kind: str) -> str:
        start, end = self.period()
        ext = "pdf" if kind == "pdf" else "csv"
        suffix = "_summary" if kind == "summary" else ""
        return f"timesheet_{start:%Y%m%d}-{end:%Y%m%d}{suffix}.{ext}"

    def export(self, kind: str, path: Optional[str] = None) -> Optional[Path]:
        if not self.rows:
            return None
        if path is None:
            filters = "PDF Files (*.pdf)" if kind == "pdf" else "CSV Files (*.csv)"
            path, _ = QFileDialog.getSaveFileName(self, "Export Timesheet", self.default_filename(kind), filters)
            if not path:
                return None
        start, end = self.period()
        try:
            if kind == "csv":
                dest = ts.write_entries_csv(self.rows, path)
            elif kind == "summary":
                dest = ts.write_summary_csv(self.rows, start, end, path)
            else:
                person = self.person_combo.currentText()
                dest = ts.write_pdf(ts.build_report_html(self.rows, start, end, self.scope_label(), person), path)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", f"Could not write the timesheet:\n{exc}")
            return None
        self.note_lbl.setText(f"✓ Saved {dest.name}")
        self.note_lbl.setToolTip(str(dest))
        return dest

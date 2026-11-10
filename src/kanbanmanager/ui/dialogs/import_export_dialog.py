"""
Import / Export centre: spreadsheets (CSV, Excel), Markdown, PDF reports and
board pictures, calendars, Trello boards and full JSON backups - plus the
spreadsheet import wizard (map columns -> preview -> import -> undo).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import exchange as x
from kanbanmanager.core.analytics import PERIODS
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui.styles import CHECKBOX_QSS


def _safe_name(text: str, fallback: str = "board") -> str:
    return "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in text).strip() or fallback


def _row(layout: QVBoxLayout, title: str, description: str) -> QHBoxLayout:
    """A card: title and description on the left, its controls (returned layout) on the right."""
    card = QFrame()
    card.setObjectName("IoCard")
    card.setStyleSheet("#IoCard { background-color: #1E293B; border: 1px solid #334155; border-radius: 8px; }"
                       "#IoCard QLabel, #IoCard QCheckBox { background: transparent; border: none; }")
    layout.addWidget(card)
    row = QHBoxLayout(card)
    row.setContentsMargins(12, 10, 12, 10)
    row.setSpacing(12)
    text = QVBoxLayout()
    row.addLayout(text, 1)
    head = QLabel(title)
    head.setStyleSheet("font-weight: 700;")
    text.addWidget(head)
    desc = QLabel(description)
    desc.setWordWrap(True)
    desc.setStyleSheet("color: #94A3B8; font-size: 11px;")
    text.addWidget(desc)
    buttons = QHBoxLayout()
    buttons.setSpacing(6)
    row.addLayout(buttons)
    return buttons


def _scroll_page(tabs: QTabWidget, title: str) -> QVBoxLayout:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    page = QWidget()
    scroll.setWidget(page)
    tabs.addTab(scroll, title)
    lay = QVBoxLayout(page)
    lay.setSpacing(8)
    return lay


class ImportExportDialog(QDialog):
    """Everything to get data in and out, in one place."""

    calendar_requested = Signal(str)     # "files" (.ics tab) or "feed"
    board_created = Signal(int)          # a Trello import made a new board

    def __init__(self, service: DatabaseService, board_id: Optional[int] = None, parent: Optional[QWidget] = None,
                 filters: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.service = service
        self.board_id = board_id
        self.filters = filters or {}
        self.changed = False
        self.last_result: Optional[x.ImportResult] = None
        board = service.get_board(board_id) if board_id else None
        self.board_name = board.name if board else ""
        self.setWindowTitle("Import / Export")
        self.setMinimumSize(820, 640)
        self.resize(860, 740)
        self.setStyleSheet(CHECKBOX_QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._build_export()
        self._build_import()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        bottom = QHBoxLayout()
        root.addLayout(bottom)
        bottom.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)

    # Export -----------------------------------------------------------------
    def _build_export(self) -> None:
        lay = _scroll_page(self.tabs, "⬇ Export")
        scope = QHBoxLayout()
        lay.addLayout(scope)
        scope.addWidget(QLabel("Export:"))
        self.scope_combo = QComboBox()
        if self.board_id:
            self.scope_combo.addItem(f"This board ({self.board_name})", "board")
        self.scope_combo.addItem("All boards", "all")
        scope.addWidget(self.scope_combo)
        self.completed_chk = QCheckBox("Completed tasks")
        self.completed_chk.setChecked(True)
        scope.addWidget(self.completed_chk)
        self.archived_chk = QCheckBox("Archived tasks")
        scope.addWidget(self.archived_chk)
        scope.addStretch()

        b = _row(lay, "📊 Spreadsheet", "Every task with its column, dates, labels, assignee, estimates, checklist and "
                                      "custom fields - for Excel, Numbers, Google Sheets or another tool.")
        self.xlsx_btn = QPushButton("Excel (.xlsx)…")
        self.xlsx_btn.clicked.connect(self._export_xlsx)
        b.addWidget(self.xlsx_btn)
        self.csv_btn = QPushButton("CSV…")
        self.csv_btn.clicked.connect(self._export_csv)
        b.addWidget(self.csv_btn)

        b = _row(lay, "📄 Board report (PDF)", "KPIs, cumulative flow, burn-up, throughput, cycle time and breakdowns "
                                              "as vector charts, then the tasks of every column.")
        self.period_combo = QComboBox()
        for label, days in PERIODS.items():
            self.period_combo.addItem(label, days)
        self.period_combo.setCurrentIndex(1)
        b.addWidget(self.period_combo)
        self.desc_chk = QCheckBox("with descriptions")
        b.addWidget(self.desc_chk)
        self.report_btn = QPushButton("PDF Report…")
        self.report_btn.clicked.connect(self._export_report)
        b.addWidget(self.report_btn)

        b = _row(lay, "🖼 Board picture", "The whole board as it looks - every column at full height - as a vector "
                                         "PDF or SVG, or a PNG image. Uses the current filters.")
        self.snap_pdf = QPushButton("PDF…")
        self.snap_pdf.clicked.connect(self._snapshot_pdf)
        b.addWidget(self.snap_pdf)
        self.snap_svg = QPushButton("SVG…")
        self.snap_svg.clicked.connect(self._snapshot_svg)
        b.addWidget(self.snap_svg)
        self.snap_png = QPushButton("PNG…")
        self.snap_png.clicked.connect(self._snapshot_png)
        b.addWidget(self.snap_png)

        b = _row(lay, "📝 Markdown", "A readable document of the board for wikis, READMEs and notes apps.")
        self.md_desc_chk = QCheckBox("with descriptions")
        self.md_desc_chk.setChecked(True)
        b.addWidget(self.md_desc_chk)
        self.md_btn = QPushButton("Markdown (.md)…")
        self.md_btn.clicked.connect(self._export_markdown)
        b.addWidget(self.md_btn)

        b = _row(lay, "📅 Calendar", "An .ics file of due dates, a live feed your calendar app subscribes to, or "
                                    "two-way sync with a CalDAV calendar.")
        self.cal_btn = QPushButton("Calendar Sync…")
        self.cal_btn.clicked.connect(self._open_calendar)
        b.addWidget(self.cal_btn)

        b = _row(lay, "💾 Full backup (JSON)", "Everything - workspaces, boards, history, comments, attachments, "
                                             "time, fields, templates - to move to another computer.")
        self.json_btn = QPushButton("JSON Backup…")
        self.json_btn.clicked.connect(self._export_json)
        b.addWidget(self.json_btn)
        lay.addStretch()
        for w in (self.report_btn, self.snap_pdf, self.snap_svg, self.snap_png, self.md_btn, self.period_combo,
                  self.desc_chk, self.md_desc_chk):
            w.setEnabled(bool(self.board_id))

    def _board_ids(self) -> Optional[List[int]]:
        return [self.board_id] if self.scope_combo.currentData() == "board" and self.board_id else None

    def _ask(self, caption: str, name: str, filt: str, path: Optional[str]) -> Optional[str]:
        if path is not None:
            return path
        chosen, _ = QFileDialog.getSaveFileName(self, caption, name, filt)
        return chosen or None

    def _done(self, dest: Path, what: str) -> Path:
        self.status.setText(f"✅ Saved {what} to {dest}")
        return dest

    def _default_stem(self) -> str:
        return _safe_name(self.board_name) if self._board_ids() else "kanban"

    def export_tasks(self, fmt: str, path: Optional[str] = None) -> Optional[Path]:
        path = self._ask("Export Tasks", f"{self._default_stem()} tasks.{fmt}",
                         "Excel (*.xlsx)" if fmt == "xlsx" else "CSV (*.csv)", path)
        if not path:
            return None
        headers, rows = x.task_table(self.service, self._board_ids(), self.completed_chk.isChecked(),
                                     self.archived_chk.isChecked())
        if fmt == "xlsx":
            dest = x.write_xlsx(headers, rows, path, sheet=self.board_name if self._board_ids() else "Tasks")
        else:
            dest = x.write_csv(headers, rows, path)
        return self._done(dest, f"{len(rows)} tasks")

    def _export_xlsx(self, _checked: bool = False) -> None:
        self.export_tasks("xlsx")

    def _export_csv(self, _checked: bool = False) -> None:
        self.export_tasks("csv")

    def export_report(self, path: Optional[str] = None) -> Optional[Path]:
        from kanbanmanager.ui.reports import write_board_report

        path = self._ask("Export Board Report", f"{_safe_name(self.board_name)} report.pdf", "PDF (*.pdf)", path)
        if not path or not self.board_id:
            return None
        points = next((f["id"] for f in self.service.get_custom_fields(self.board_id)
                       if f["field_type"] == "number" and "point" in f["name"].lower()), None)
        dest = write_board_report(self.service, self.board_id, path, days=int(self.period_combo.currentData()),
                                  points_field_id=points, include_descriptions=self.desc_chk.isChecked(),
                                  include_completed=self.completed_chk.isChecked())
        return self._done(dest, "the report")

    def _export_report(self, _checked: bool = False) -> None:
        self.export_report()

    def export_snapshot(self, fmt: str, path: Optional[str] = None) -> Optional[Path]:
        from kanbanmanager.ui.reports import export_board_snapshot

        filters = {"PDF": "PDF (*.pdf)", "SVG": "SVG (*.svg)", "PNG": "PNG (*.png)"}
        path = self._ask("Export Board Picture", f"{_safe_name(self.board_name)}.{fmt}", filters[fmt.upper()], path)
        if not path or not self.board_id:
            return None
        style = self.parent().styleSheet() if self.parent() is not None else None
        dest = export_board_snapshot(self.service, self.board_id, path, fmt, filters=self.filters,
                                     parent_style=style or None)
        return self._done(dest, "the board picture")

    def _snapshot_pdf(self, _checked: bool = False) -> None:
        self.export_snapshot("pdf")

    def _snapshot_svg(self, _checked: bool = False) -> None:
        self.export_snapshot("svg")

    def _snapshot_png(self, _checked: bool = False) -> None:
        self.export_snapshot("png")

    def export_markdown(self, path: Optional[str] = None) -> Optional[Path]:
        path = self._ask("Export Markdown", f"{_safe_name(self.board_name)}.md", "Markdown (*.md)", path)
        if not path or not self.board_id:
            return None
        dest = Path(path)
        dest.write_text(x.board_markdown(self.service, self.board_id, self.md_desc_chk.isChecked(),
                                         self.completed_chk.isChecked()), encoding="utf-8")
        return self._done(dest, "the Markdown document")

    def _export_markdown(self, _checked: bool = False) -> None:
        self.export_markdown()

    def export_json(self, path: Optional[str] = None) -> Optional[Path]:
        from kanbanmanager.core.backup_manager import BackupManager

        path = self._ask("Export Backup", "kanban-backup.json", "JSON (*.json)", path)
        if not path:
            return None
        dest = BackupManager(self.service.db).export_to_json(path)
        return self._done(dest, "the backup")

    def _export_json(self, _checked: bool = False) -> None:
        self.export_json()

    def _open_calendar(self, _checked: bool = False) -> None:
        self.calendar_requested.emit("files")

    # Import -----------------------------------------------------------------------
    def _build_import(self) -> None:
        lay = _scroll_page(self.tabs, "⬆ Import")
        b = _row(lay, "📊 Spreadsheet (CSV / Excel)", "From Jira, Asana, Trello, GitHub, Monday, Todoist, Excel … "
                                                      "You map the columns and preview every row before importing.")
        self.sheet_btn = QPushButton("Import Spreadsheet…")
        self.sheet_btn.setObjectName("PrimaryButton")
        self.sheet_btn.clicked.connect(self._import_sheet)
        b.addWidget(self.sheet_btn)
        b = _row(lay, "🟦 Trello board (JSON)", "Trello: Board menu → Print, export and share → Export as JSON. "
                                               "Lists, cards, labels, checklists, members, comments, due dates and "
                                               "custom fields become a new board.")
        self.trello_btn = QPushButton("Import Trello Board…")
        self.trello_btn.clicked.connect(self._import_trello)
        b.addWidget(self.trello_btn)
        self.trello_archived = QCheckBox("include archived cards")
        b.insertWidget(0, self.trello_archived)
        b = _row(lay, "📅 Calendar (.ics)", "Events and to-dos become tasks due on their date.")
        self.ics_btn = QPushButton("Import .ics…")
        self.ics_btn.clicked.connect(self._open_calendar)
        b.addWidget(self.ics_btn)
        b = _row(lay, "💾 KanbanManager backup (JSON)", "Adds the workspaces and boards of a JSON backup "
                                                       "(nothing is replaced).")
        self.json_in_btn = QPushButton("Import JSON Backup…")
        self.json_in_btn.clicked.connect(self._import_json)
        b.addWidget(self.json_in_btn)
        undo_row = QHBoxLayout()
        lay.addLayout(undo_row)
        self.undo_lbl = QLabel("")
        self.undo_lbl.setStyleSheet("color: #94A3B8;")
        undo_row.addWidget(self.undo_lbl, 1)
        self.undo_btn = QPushButton("↩ Undo Last Import")
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self.undo_last)
        undo_row.addWidget(self.undo_btn)
        lay.addStretch()

    def import_sheet(self, path: Optional[str] = None, run=None) -> Optional[x.ImportResult]:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import Spreadsheet", "",
                                                  "Spreadsheets (*.csv *.tsv *.txt *.xlsx);;All files (*)")
        if not path:
            return None
        try:
            table = x.read_table(path)
        except x.ExchangeError as exc:
            QMessageBox.warning(self, "Import Spreadsheet", str(exc))
            return None
        wizard = SpreadsheetImportWizard(self.service, table, self.board_id, self)
        result = (run or (lambda w: w.exec()))(wizard) and wizard.result_
        wizard.deleteLater()
        if result:
            self._remember(result, wizard.target_board_id)
        return result or None

    def _import_sheet(self, _checked: bool = False) -> None:
        self.import_sheet()

    def import_trello(self, path: Optional[str] = None, workspace_id: Optional[int] = None) -> Optional[int]:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import Trello Board", "", "Trello export (*.json)")
        if not path:
            return None
        try:
            data = x.read_trello(path)
        except x.ExchangeError as exc:
            QMessageBox.warning(self, "Import Trello Board", str(exc))
            return None
        if workspace_id is None:
            board = self.service.get_board(self.board_id) if self.board_id else None
            workspace_id = board.workspace_id if board else self.service.get_workspaces()[0].id
        board, result = x.import_trello(self.service, data, workspace_id, include_archived=self.trello_archived.isChecked())
        self.changed = True
        self.status.setText(f"✅ Created the board '{board.name}': {result.describe()}" +
                            (f"  ·  ⚠ {len(result.warnings)} warning(s)" if result.warnings else ""))
        self.board_created.emit(board.id)
        return board.id

    def _import_trello(self, _checked: bool = False) -> None:
        self.import_trello()

    def import_json(self, path: Optional[str] = None) -> bool:
        from kanbanmanager.core.backup_manager import BackupManager

        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import Backup", "", "JSON (*.json)")
        if not path:
            return False
        try:
            BackupManager(self.service.db).import_from_json(path)
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Import Backup", f"Could not import the backup:\n{exc}")
            return False
        self.changed = True
        self.status.setText("✅ Imported the backup - its boards are in the sidebar")
        return True

    def _import_json(self, _checked: bool = False) -> None:
        self.import_json()

    def _remember(self, result: x.ImportResult, board_id: Optional[int]) -> None:
        self.last_result = result
        self.changed = True
        self.undo_btn.setEnabled(bool(result.created))
        self.undo_lbl.setText(f"Last import: {result.describe()}")
        self.status.setText(f"✅ {result.describe()}")

    def undo_last(self, _checked: bool = False) -> int:
        if self.last_result is None:
            return 0
        n = x.undo_import(self.service, self.last_result)
        self.last_result = None
        self.changed = True
        self.undo_btn.setEnabled(False)
        self.undo_lbl.setText(f"↩ Removed {n} imported task{'s' if n != 1 else ''}")
        return n


# ---------------------------------------------------------------------------
# Spreadsheet import wizard
# ---------------------------------------------------------------------------
class SpreadsheetImportWizard(QDialog):
    """Map columns -> preview -> import."""

    def __init__(self, service: DatabaseService, table: x.Table, board_id: Optional[int],
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.table = table
        self.result_: Optional[x.ImportResult] = None
        self.setWindowTitle(f"Import {table.source or 'spreadsheet'}")
        self.setMinimumSize(900, 620)
        self.setStyleSheet(CHECKBOX_QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        info = QLabel(f"<b>{table.source}</b> · {len(table.rows)} row{'s' if len(table.rows) != 1 else ''} · "
                      f"{len(table.headers)} columns" + (f" · separator '{table.delimiter}' · {table.encoding}"
                                                         if table.delimiter != "xlsx" else " · Excel workbook"))
        root.addWidget(info)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # Page 1: mapping ---------------------------------------------------------
        map_page = QWidget()
        self.stack.addWidget(map_page)
        ml = QVBoxLayout(map_page)
        ml.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        ml.addLayout(form)
        self.board_combo = QComboBox()
        for ws in service.get_workspaces():
            for b in ws.boards:
                if not b.is_archived:
                    self.board_combo.addItem(f"📋 {b.name}", b.id)
        self.board_combo.setCurrentIndex(max(0, self.board_combo.findData(board_id)))
        self.board_combo.currentIndexChanged.connect(self._on_board)
        form.addRow("Into board:", self.board_combo)
        self.preset_combo = QComboBox()
        for name in x.PRESETS:
            self.preset_combo.addItem(name)
        self.preset_combo.currentIndexChanged.connect(self._apply_preset)
        form.addRow("Exported from:", self.preset_combo)
        self.map_table = QTableWidget(0, 3)
        self.map_table.setHorizontalHeaderLabels(["Column in the file", "Examples", "Import as"])
        head = self.map_table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.Stretch)
        head.setSectionResizeMode(2, QHeaderView.Fixed)
        self.map_table.setColumnWidth(2, 250)
        self.map_table.verticalHeader().setDefaultSectionSize(34)
        self.map_table.verticalHeader().setVisible(False)
        self.map_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        ml.addWidget(self.map_table, 1)
        opts = QFormLayout()
        ml.addLayout(opts)
        self.default_col = QComboBox()
        opts.addRow("Rows without a column go to:", self.default_col)
        self.create_cols = QCheckBox("Create columns for unknown statuses (otherwise they go to the column above)")
        self.create_cols.setChecked(True)
        opts.addRow("", self.create_cols)
        self.create_labels = QCheckBox("Create missing labels")
        self.create_labels.setChecked(True)
        opts.addRow("", self.create_labels)
        self.skip_dupes = QCheckBox("Skip rows whose title is already in the same column")
        self.skip_dupes.setChecked(True)
        opts.addRow("", self.skip_dupes)
        self.date_order = QComboBox()
        self.date_order.addItem("Detect (03/04 = March 4 unless a day is over 12)", "auto")
        self.date_order.addItem("Month first (US: 03/04/2025 = March 4)", "mdy")
        self.date_order.addItem("Day first (03/04/2025 = 3 April)", "dmy")
        opts.addRow("Dates like 03/04/2025:", self.date_order)

        # Page 2: preview ---------------------------------------------------------------
        prev_page = QWidget()
        self.stack.addWidget(prev_page)
        pl = QVBoxLayout(prev_page)
        pl.setContentsMargins(0, 0, 0, 0)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        pl.addWidget(self.summary)
        self.preview = QTableWidget(0, 8)
        self.preview.setHorizontalHeaderLabels(["Row", "Title", "Column", "Due", "Priority", "Labels", "Assignee",
                                                "Notes"])
        head = self.preview.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.Stretch)
        head.setSectionResizeMode(7, QHeaderView.Stretch)
        self.preview.verticalHeader().setVisible(False)
        self.preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        pl.addWidget(self.preview, 1)

        btns = QHBoxLayout()
        root.addLayout(btns)
        self.back_btn = QPushButton("← Back")
        self.back_btn.clicked.connect(self.back)
        btns.addWidget(self.back_btn)
        btns.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.next_btn = QPushButton("Preview →")
        self.next_btn.setObjectName("PrimaryButton")
        self.next_btn.clicked.connect(self.next)
        btns.addWidget(self.next_btn)
        self._on_board()
        self._update_buttons()

    @property
    def target_board_id(self) -> int:
        return self.board_combo.currentData()

    def _targets(self) -> List[tuple]:
        out = [(x.IGNORE, "— don't import —")] + list(x.TARGETS)
        for f in self.service.get_custom_fields(self.target_board_id):
            out.append((f"field:{f['id']}", f"🧩 {f['name']} ({f['type_label']})"))
        out.append((x.NEW_FIELD, "🧩 New text field"))
        return out

    def _on_board(self, *_args) -> None:
        self.default_col.clear()
        for c in self.service.get_columns_for_board(self.target_board_id):
            self.default_col.addItem(c.name, c.id)
        capture = self.service.default_capture_column(self.target_board_id)
        self.default_col.setCurrentIndex(max(0, self.default_col.findData(capture)))
        self._apply_preset()

    def _apply_preset(self, *_args) -> None:
        fields = self.service.get_custom_fields(self.target_board_id)
        mapping = x.guess_mapping(self.table, fields, self.preset_combo.currentText())
        targets = self._targets()
        self.map_table.setRowCount(len(self.table.headers))
        self.combos: List[QComboBox] = []
        for i, header in enumerate(self.table.headers):
            self.map_table.setItem(i, 0, QTableWidgetItem(header))
            samples = " · ".join(s.replace("\n", " ")[:40] for s in self.table.column_samples(i))
            self.map_table.setItem(i, 1, QTableWidgetItem(samples))
            combo = QComboBox()
            for key, label in targets:
                combo.addItem(label, key)
            combo.setCurrentIndex(max(0, combo.findData(mapping.get(i, x.IGNORE))))
            self.map_table.setCellWidget(i, 2, combo)
            self.combos.append(combo)

    def mapping(self) -> Dict[int, str]:
        return {i: c.currentData() for i, c in enumerate(self.combos)}

    def options(self) -> x.ImportOptions:
        return x.ImportOptions(board_id=self.target_board_id, mapping=self.mapping(),
                               default_column_id=self.default_col.currentData(),
                               create_columns=self.create_cols.isChecked(),
                               create_labels=self.create_labels.isChecked(),
                               skip_duplicates=self.skip_dupes.isChecked(), date_order=self.date_order.currentData())

    def _update_buttons(self) -> None:
        on_preview = self.stack.currentIndex() == 1
        self.back_btn.setVisible(on_preview)
        self.next_btn.setText("Import" if on_preview else "Preview →")

    def back(self, _checked: bool = False) -> None:
        self.stack.setCurrentIndex(0)
        self._update_buttons()

    def next(self, _checked: bool = False) -> None:
        if self.stack.currentIndex() == 0:
            if "title" not in self.mapping().values():
                QMessageBox.warning(self, "Import", "Choose which column holds the task titles.")
                return
            self.show_preview()
        else:
            self.run()

    def show_preview(self) -> List[x.RowPlan]:
        plans = x.plan_rows(self.service, self.table, self.options())
        columns = {c.id: c.name for c in self.service.get_columns_for_board(self.target_board_id)}
        existing_labels = {l.name.lower() for l in self.service.get_labels_for_board(self.target_board_id)}
        self.preview.setRowCount(len(plans))
        new_cols, new_labels = set(), set()
        for r, p in enumerate(plans):
            col = f"＋ {p.new_column}" if p.new_column else columns.get(p.column_id, "")
            if p.new_column:
                new_cols.add(p.new_column)
            for l in p.labels:
                if l.lower() not in existing_labels:
                    new_labels.add(l)
            notes = p.skip and f"skipped: {p.skip}" or "; ".join(p.warnings)
            cells = [str(p.line), p.title, col, p.due.strftime("%Y-%m-%d %H:%M") if p.due else "",
                     (p.priority or "").title(), ", ".join(p.labels), p.assignee or "", notes]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if p.skip:
                    item.setForeground(Qt.gray)
                elif c == 7 and p.warnings:
                    item.setForeground(Qt.darkYellow)
                self.preview.setItem(r, c, item)
        n_ok = sum(1 for p in plans if not p.skip)
        parts = [f"<b>{n_ok}</b> task{'s' if n_ok != 1 else ''} will be created"]
        skipped = len(plans) - n_ok
        if skipped:
            parts.append(f"{skipped} row{'s' if skipped != 1 else ''} skipped")
        if new_cols:
            parts.append("new columns: " + ", ".join(sorted(new_cols)))
        if new_labels and self.create_labels.isChecked():
            parts.append("new labels: " + ", ".join(sorted(new_labels)))
        warnings = sum(1 for p in plans if p.warnings and not p.skip)
        if warnings:
            parts.append(f"{warnings} row{'s' if warnings != 1 else ''} with warnings (see Notes)")
        self.summary.setText(" · ".join(parts) + ". You can undo the import afterwards.")
        self.stack.setCurrentIndex(1)
        self._update_buttons()
        return plans

    def run(self, _checked: bool = False) -> x.ImportResult:
        self.result_ = x.run_import(self.service, self.table, self.options())
        if self.result_.warnings and self.isVisible():
            QMessageBox.information(self, "Imported", f"{self.result_.describe()}.\n\nNotes:\n" +
                                    "\n".join(self.result_.warnings[:15]) +
                                    ("\n…" if len(self.result_.warnings) > 15 else ""))
        self.accept()
        return self.result_

"""
Workload & Capacity Planning View

A person x period grid (weeks or days). Each cell shows planned remaining work
against the person's capacity, coloured by utilisation, plus time actually
logged. Selecting a cell lists the tasks behind it; tasks can be opened or
reassigned from there. Team capacity is edited in the Team dialog, and
timesheets are exported from the header.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.config import DEFAULT_WEEKLY_CAPACITY_HOURS
from kanbanmanager.core.signals import app_signals
from kanbanmanager.core.timesheet import local_range_to_utc
from kanbanmanager.core.workload import (
    DAY,
    UNASSIGNED,
    WEEK,
    Cell,
    WorkloadResult,
    compute_workload,
    week_start,
)
from kanbanmanager.database.service import DatabaseService
from kanbanmanager.timefmt import format_hours
from kanbanmanager.ui.styles import CHECKBOX_QSS

STATUS_COLORS = {
    "idle": "#334155",
    "ok": "#10B981",
    "busy": "#F59E0B",
    "over": "#EF4444",
    "past": "#1E293B",
}
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

SCOPE_BOARD = "board"
SCOPE_WORKSPACE = "workspace"


def describe_days(days: List[int]) -> str:
    if days == [0, 1, 2, 3, 4]:
        return "Mon-Fri"
    if not days:
        return "no working days"
    return ", ".join(DAY_NAMES[d] for d in days)


class LoadCell(QWidget):
    """Paints one grid cell: a utilisation bar plus 'planned / capacity' and logged time."""

    def __init__(self, cell: Cell, unassigned: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cell = cell
        self.unassigned = unassigned
        self.setMinimumSize(96, 46)
        self.setToolTip(self._tooltip())

    def _tooltip(self) -> str:
        c = self.cell
        lines = [f"{c.start:%a %b %d}" + (f" - {c.end:%a %b %d}" if c.end != c.start else "")]
        if self.unassigned:
            lines.append(f"{format_hours(c.planned)} of unassigned work")
        else:
            lines.append(f"Planned {format_hours(c.planned)} of {format_hours(c.capacity)} capacity"
                         + (f" ({c.utilization:.0%})" if c.utilization is not None else ""))
        if c.logged:
            lines.append(f"Logged {format_hours(c.logged)}")
        for a in c.allocations[:6]:
            lines.append(f"• {a.title} - {format_hours(a.hours)}" + (" (overdue)" if a.is_overdue else ""))
        if len(c.allocations) > 6:
            lines.append(f"... and {len(c.allocations) - 6} more")
        return "\n".join(lines)

    def paintEvent(self, _event) -> None:
        c = self.cell
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        status = "ok" if self.unassigned and c.planned > 0 else c.status
        color = QColor("#64748B" if self.unassigned and c.planned > 0 else STATUS_COLORS.get(status, "#334155"))

        bar = QRectF(r.left(), r.bottom() - 7, r.width(), 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1E293B"))
        p.drawRoundedRect(bar, 3, 3)
        if c.planned > 0:
            frac = 1.0 if (self.unassigned or c.capacity <= 0) else min(1.0, c.planned / c.capacity)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(4.0, bar.width() * frac), bar.height()), 3, 3)

        p.setPen(QPen(QColor("#F8FAFC" if c.planned > 0 else "#64748B")))
        font = p.font()
        font.setBold(c.status == "over" and not self.unassigned)
        p.setFont(font)
        if self.unassigned:
            text = format_hours(c.planned) if c.planned > 0 else "-"
        elif c.capacity <= 0 and c.planned <= 0:
            text = "-"
        else:
            text = f"{format_hours(c.planned)} / {format_hours(c.capacity)}"
        p.drawText(QRectF(r.left(), r.top(), r.width(), 18), Qt.AlignLeft | Qt.AlignVCenter, text)
        if c.logged > 0:
            small = p.font()
            small.setBold(False)
            small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
            p.setFont(small)
            p.setPen(QPen(QColor("#38BDF8")))
            p.drawText(QRectF(r.left(), r.top() + 17, r.width(), 14), Qt.AlignLeft | Qt.AlignVCenter,
                       f"⏱ {format_hours(c.logged)} logged")
        p.end()

    def sizeHint(self) -> QSize:
        return QSize(110, 50)


class TeamDialog(QDialog):
    """Edit team members' weekly capacity and working days."""

    def __init__(self, service: DatabaseService, people: List[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Team & Capacity")
        self.setStyleSheet(CHECKBOX_QSS)
        self.resize(640, 380)
        layout = QVBoxLayout(self)
        info = QLabel("Weekly capacity is spread evenly over each person's working days. "
                      f"People not listed here are planned with {DEFAULT_WEEKLY_CAPACITY_HOURS:g}h, Mon-Fri.")
        info.setWordWrap(True)
        info.setStyleSheet("color: #94A3B8; font-size: 11px;")
        layout.addWidget(info)

        self.table = QTableWidget(0, 2 + 7)
        self.table.setHorizontalHeaderLabels(["Name", "Hours / week"] + DAY_NAMES)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        members = {m["name"]: m for m in service.get_team_members()}
        for name in sorted(set(people) | set(members), key=str.lower):
            m = members.get(name, {"weekly_capacity_hours": DEFAULT_WEEKLY_CAPACITY_HOURS,
                                   "working_days": [0, 1, 2, 3, 4]})
            self._add_row(name, m["weekly_capacity_hours"], m["working_days"])

        row = QHBoxLayout()
        add_btn = QPushButton("➕ Add Person")
        add_btn.clicked.connect(self._add_person)
        row.addWidget(add_btn)
        row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

    def _add_row(self, name: str, hours: float, days: List[int]) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        item = QTableWidgetItem(name)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(r, 0, item)
        spin = QDoubleSpinBox()
        spin.setRange(0, 168)
        spin.setDecimals(1)
        spin.setSingleStep(4)
        spin.setValue(float(hours))
        self.table.setCellWidget(r, 1, spin)
        for d in range(7):
            box = QCheckBox()
            box.setChecked(d in days)
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setAlignment(Qt.AlignCenter)
            hl.addWidget(box)
            self.table.setCellWidget(r, 2 + d, holder)

    def _add_person(self) -> None:
        name, ok = QInputDialog.getText(self, "Add Person", "Name:")
        name = name.strip()
        if ok and name:
            existing = {self.table.item(r, 0).text() for r in range(self.table.rowCount())}
            if name not in existing:
                self._add_row(name, DEFAULT_WEEKLY_CAPACITY_HOURS, [0, 1, 2, 3, 4])

    def values(self) -> List[Dict[str, Any]]:
        out = []
        for r in range(self.table.rowCount()):
            days = [d for d in range(7)
                    if self.table.cellWidget(r, 2 + d).findChild(QCheckBox).isChecked()]
            out.append({"name": self.table.item(r, 0).text(),
                        "weekly_capacity_hours": self.table.cellWidget(r, 1).value(),
                        "working_days": days})
        return out

    def _save(self) -> None:
        for v in self.values():
            self.service.upsert_team_member(v["name"], v["weekly_capacity_hours"], v["working_days"])
        self.accept()


class WorkloadView(QWidget):
    timesheet_requested = Signal()

    WEEKS_SHOWN = 6
    DAYS_SHOWN = 14

    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.current_board_id: Optional[int] = None
        self.scope = SCOPE_BOARD
        self.granularity = WEEK
        self.anchor = week_start(date.today())
        self.result: Optional[WorkloadResult] = None
        self._selected: Optional[tuple] = None  # (row index, period index)
        self._init_ui()

    # ------------------------------------------------------------------
    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # Row 1: title + actions; row 2: what/when is shown (keeps both rows readable when narrow)
        title_row = QHBoxLayout()
        title = QLabel("👥 Workload & Capacity")  # QLabel shows "&" literally
        title.setStyleSheet("font-size: 16px; font-weight: 800;")
        title_row.addWidget(title)
        title_row.addStretch()
        team_btn = QPushButton("👤 Team && Capacity...")  # "&&" = literal ampersand on a button
        team_btn.setToolTip("Set each person's weekly hours and working days")
        team_btn.clicked.connect(self.edit_team)
        title_row.addWidget(team_btn)
        export_btn = QPushButton("📤 Export Timesheet...")
        export_btn.setObjectName("PrimaryButton")
        export_btn.setToolTip("Preview logged time and export it as CSV or PDF")
        export_btn.clicked.connect(self.timesheet_requested.emit)
        title_row.addWidget(export_btn)
        layout.addLayout(title_row)

        header = QHBoxLayout()
        layout.addLayout(header)  # attach first: a parentless layout + self-capturing lambda double-frees in PySide6
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("This board", SCOPE_BOARD)
        self.scope_combo.addItem("Whole workspace", SCOPE_WORKSPACE)
        self.scope_combo.currentIndexChanged.connect(self._on_scope)
        header.addWidget(self.scope_combo)
        self.gran_combo = QComboBox()
        self.gran_combo.addItem("Weeks", WEEK)
        self.gran_combo.addItem("Days", DAY)
        self.gran_combo.currentIndexChanged.connect(self._on_granularity)
        header.addWidget(self.gran_combo)
        self.prev_btn = QPushButton("◀")
        self.prev_btn.setToolTip("Earlier")
        self.prev_btn.clicked.connect(lambda: self.shift(-1))
        self.today_btn = QPushButton("Today")
        self.today_btn.clicked.connect(self.go_today)
        self.next_btn = QPushButton("▶")
        self.next_btn.setToolTip("Later")
        self.next_btn.clicked.connect(lambda: self.shift(1))
        for b in (self.prev_btn, self.today_btn, self.next_btn):
            header.addWidget(b)
        self.range_lbl = QLabel("")
        self.range_lbl.setStyleSheet("color: #94A3B8;")
        header.addWidget(self.range_lbl)
        header.addStretch()

        self.summary_lbl = QLabel("")
        self.summary_lbl.setTextFormat(Qt.RichText)
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setStyleSheet("color: #CBD5E1;")
        layout.addWidget(self.summary_lbl)

        splitter = QSplitter(Qt.Vertical)
        self.grid = QTableWidget()
        self.grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.grid.setSelectionMode(QAbstractItemView.SingleSelection)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.grid.verticalHeader().setVisible(False)
        self.grid.setShowGrid(True)
        self.grid.setStyleSheet("QTableWidget { gridline-color: #334155; }"
                                "QTableWidget::item:selected { background-color: rgba(29, 78, 216, 85); }")
        self.grid.cellClicked.connect(self._on_cell_clicked)
        self.grid.cellDoubleClicked.connect(self._on_cell_double_clicked)
        splitter.addWidget(self.grid)

        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 6, 0, 0)
        self.detail_title = QLabel("Select a cell to see the tasks behind it.")
        self.detail_title.setStyleSheet("font-weight: 700;")
        dl.addWidget(self.detail_title)
        self.detail = QTableWidget(0, 5)
        self.detail.setHorizontalHeaderLabels(["Task", "Board", "This period", "Remaining", "Due"])
        self.detail.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 5):
            self.detail.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.detail.verticalHeader().setVisible(False)
        self.detail.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.detail.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.detail.setContextMenuPolicy(Qt.CustomContextMenu)
        self.detail.customContextMenuRequested.connect(self._detail_menu)
        self.detail.itemDoubleClicked.connect(self._open_detail_task)
        dl.addWidget(self.detail, 1)
        hint = QLabel("Double-click a task to open it · right-click to reassign it")
        hint.setStyleSheet("color: #64748B; font-size: 11px;")
        dl.addWidget(hint)
        splitter.addWidget(detail)
        splitter.setSizes([380, 220])
        layout.addWidget(splitter, 1)

        legend = QLabel(
            "<span style='color:#10B981'>■</span> under 80% &nbsp; "
            "<span style='color:#F59E0B'>■</span> 80-100% &nbsp; "
            "<span style='color:#EF4444'>■</span> over capacity &nbsp; · &nbsp;"
            "Remaining work (estimate minus time logged) is spread over working days up to each due date; "
            "overdue work lands on today."
        )
        legend.setTextFormat(Qt.RichText)
        legend.setWordWrap(True)
        legend.setStyleSheet("color: #64748B; font-size: 11px;")
        layout.addWidget(legend)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def load_board(self, board_id: int) -> None:
        self.current_board_id = board_id
        self.refresh()

    def _on_scope(self) -> None:
        self.scope = self.scope_combo.currentData()
        self.refresh(keep_selection=False)

    def _on_granularity(self) -> None:
        self.granularity = self.gran_combo.currentData()
        self.go_today()

    def go_today(self) -> None:
        self.anchor = week_start(date.today()) if self.granularity == WEEK else date.today()
        self.refresh(keep_selection=False)

    def shift(self, direction: int) -> None:
        self.anchor += timedelta(weeks=direction) if self.granularity == WEEK else timedelta(days=7 * direction)
        self.refresh(keep_selection=False)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def scope_board_ids(self) -> List[int]:
        if not self.current_board_id:
            return []
        if self.scope == SCOPE_BOARD:
            return [self.current_board_id]
        board = self.service.get_board(self.current_board_id)
        if board is None:
            return []
        return [b.id for b in self.service.get_boards_for_workspace(board.workspace_id)]

    def compute(self) -> WorkloadResult:
        board_ids = self.scope_board_ids()
        tasks: List[Dict[str, Any]] = []
        for bid in board_ids:
            board = self.service.get_board(bid)
            name = board.name if board else ""
            tasks += [dict(t.to_dict(), board_name=name) for t in self.service.get_tasks_for_board(bid)]
        count = self.WEEKS_SHOWN if self.granularity == WEEK else self.DAYS_SHOWN
        end = self.anchor + (timedelta(weeks=count) if self.granularity == WEEK else timedelta(days=count))
        utc_start, utc_end = local_range_to_utc(self.anchor, end - timedelta(days=1))
        entries = self.service.get_time_entries(board_ids=board_ids, start=utc_start, end=utc_end)
        members = {m["name"]: m for m in self.service.get_team_members()}
        return compute_workload(tasks, members, self.anchor, count, self.granularity, date.today(), entries)

    def refresh(self, keep_selection: bool = True) -> None:
        if not self.current_board_id:
            return
        if not keep_selection:
            self._selected = None  # the same grid position would now mean a different period/person
        self.result = self.compute()
        self._render_grid()
        self._render_summary()
        self._render_detail()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _render_grid(self) -> None:
        res = self.result
        periods = res.periods
        self.grid.clear()
        self.grid.setRowCount(len(res.rows))
        self.grid.setColumnCount(len(periods) + 2)
        headers = ["Person"]
        today = res.today
        for s, e in periods:
            if res.granularity == WEEK:  # two short lines so narrow columns stay readable
                label = f"{'This week' if s <= today <= e else 'Week of'}\n{s:%b %d}"
            else:
                label = f"{s:%a}\n{s:%b %d}" + (" ●" if s == today else "")
            headers.append(label)
        headers.append("Unscheduled")
        self.grid.setHorizontalHeaderLabels(headers)
        self.grid.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for c in range(1, len(periods) + 2):
            self.grid.horizontalHeader().setSectionResizeMode(c, QHeaderView.Stretch)
        self.grid.horizontalHeader().setMinimumSectionSize(96)

        for r, row in enumerate(res.rows):
            unassigned = row.name == UNASSIGNED
            if unassigned:
                sub = "no capacity - assign this work"
            elif row.is_member:
                sub = f"{row.weekly_capacity:g}h/wk · {describe_days(row.working_days)}"
            else:
                sub = f"{row.weekly_capacity:g}h/wk (default)"
            person = QTableWidgetItem(f"{row.display_name}\n{sub}")
            person.setToolTip("Double-click to edit capacity" if not unassigned else "Tasks without an assignee")
            person.setForeground(QColor("#94A3B8" if unassigned else "#F8FAFC"))
            self.grid.setItem(r, 0, person)
            for c, cell in enumerate(row.cells, start=1):
                self.grid.setItem(r, c, QTableWidgetItem(""))
                self.grid.setCellWidget(r, c, LoadCell(cell, unassigned))
            uns = QTableWidgetItem(
                (format_hours(row.unscheduled_hours) + f" · {len(row.unscheduled)} task(s)") if row.unscheduled else "-"
            )
            tip = [f"• {a.title} ({format_hours(a.hours)})" for a in row.unscheduled]
            if row.unestimated_tasks:
                tip.append(f"{row.unestimated_tasks} open task(s) without an estimate")
            uns.setToolTip("\n".join(tip) or "No unscheduled work")
            uns.setForeground(QColor("#FBBF24" if row.unscheduled else "#64748B"))
            self.grid.setItem(r, len(periods) + 1, uns)
            self.grid.setRowHeight(r, 54)
        if not res.rows:
            self.grid.setRowCount(0)

        s, e = periods[0][0], periods[-1][1]
        self.range_lbl.setText(f"{s:%b %d} - {e:%b %d, %Y}")

    def _render_summary(self) -> None:
        res = self.result
        people = [r for r in res.rows if r.name != UNASSIGNED]
        planned = sum(r.planned for r in res.rows)
        capacity = sum(r.capacity for r in people)
        logged = sum(r.logged for r in res.rows)
        parts = [f"<b>{len(people)}</b> people · <b>{format_hours(planned)}</b> planned of "
                 f"<b>{format_hours(capacity)}</b> capacity · <b>{format_hours(logged)}</b> logged in this range"]
        overloads = res.overloads()
        if overloads:
            items = []
            for row, cell in overloads[:4]:
                when = f"week of {cell.start:%b %d}" if res.granularity == WEEK else f"{cell.start:%a %b %d}"
                items.append(f"{row.display_name} ({when}: {format_hours(cell.planned)} / {format_hours(cell.capacity)})")
            more = f" and {len(overloads) - 4} more" if len(overloads) > 4 else ""
            parts.append(f"<span style='color:#F87171'>⚠ Over capacity: {', '.join(items)}{more}</span>")
        unassigned = res.row(UNASSIGNED)
        if unassigned and (unassigned.planned or unassigned.unscheduled):
            parts.append(f"<span style='color:#FBBF24'>{format_hours(unassigned.planned + unassigned.unscheduled_hours)}"
                         " of work has no assignee</span>")
        no_est = sum(r.unestimated_tasks for r in res.rows)
        if no_est:
            parts.append(f"<span style='color:#94A3B8'>{no_est} open task(s) have no estimate and are not planned</span>")
        self.summary_lbl.setText(" &nbsp;|&nbsp; ".join(parts))

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------
    def _on_cell_clicked(self, r: int, c: int) -> None:
        self._selected = (r, c)
        self._render_detail()

    def _on_cell_double_clicked(self, r: int, c: int) -> None:
        if c == 0 and self.result and r < len(self.result.rows) and self.result.rows[r].name != UNASSIGNED:
            self.edit_team()

    def _selected_allocations(self):
        if not self.result or self._selected is None:
            return None, None, []
        r, c = self._selected
        if r >= len(self.result.rows):
            return None, None, []
        row = self.result.rows[r]
        if c == 0:
            allocs = [a for cell in row.cells for a in cell.allocations] + list(row.unscheduled)
            merged: Dict[int, Any] = {}
            for a in allocs:
                if a.task_id in merged:
                    merged[a.task_id].hours += a.hours
                else:
                    merged[a.task_id] = type(a)(**{**a.__dict__})
            return row, None, sorted(merged.values(), key=lambda a: (-a.hours, a.title))
        if c == len(self.result.periods) + 1:
            return row, "unscheduled", list(row.unscheduled)
        return row, row.cells[c - 1], list(row.cells[c - 1].allocations)

    def _render_detail(self) -> None:
        row, where, allocs = self._selected_allocations()
        if row is None:
            self.detail_title.setText("Select a cell to see the tasks behind it.")
            self.detail.setRowCount(0)
            return
        if where is None:
            title = f"{row.display_name} · all planned work in view"
        elif where == "unscheduled":
            title = f"{row.display_name} · unscheduled (no due date) - add due dates to plan these"
        else:
            cell: Cell = where
            span = f"week of {cell.start:%b %d}" if self.result.granularity == WEEK else f"{cell.start:%A %b %d}"
            if row.name == UNASSIGNED:
                title = f"Unassigned · {span}: {format_hours(cell.planned)}"
            else:
                util = f" ({cell.utilization:.0%})" if cell.utilization is not None else ""
                title = (f"{row.display_name} · {span}: {format_hours(cell.planned)} planned of "
                         f"{format_hours(cell.capacity)}{util}, {format_hours(cell.logged)} logged")
        self.detail_title.setText(title)
        self.detail.setRowCount(len(allocs))
        for i, a in enumerate(allocs):
            due = a.due.strftime("%b %d") if a.due else "-"
            if a.is_overdue:
                due += " (overdue)"
            values = [a.title, a.board_name, format_hours(a.hours), format_hours(a.remaining_hours), due]
            for j, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, a.task_id)
                if a.is_overdue and j == 4:
                    item.setForeground(QColor("#F87171"))
                self.detail.setItem(i, j, item)

    def _detail_task_id(self) -> Optional[int]:
        items = self.detail.selectedItems()
        return items[0].data(Qt.UserRole) if items else None

    def _open_detail_task(self, item: QTableWidgetItem) -> None:
        app_signals.task_edit_requested.emit(item.data(Qt.UserRole))

    def _detail_menu(self, pos) -> None:
        item = self.detail.itemAt(pos)
        if item is None:
            return
        self.detail.selectRow(item.row())
        task_id = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("✏ Open Task", lambda: app_signals.task_edit_requested.emit(task_id))
        reassign = menu.addMenu("👤 Reassign to")
        current = self.service.get_task(task_id)
        current_name = (current.assignee or "") if current else ""
        for name in self.service.get_people():
            label = self._load_hint(name).replace("&", "&&")  # menu text: "&" would become a shortcut
            act = reassign.addAction(label, lambda n=name: self.reassign(task_id, n))
            act.setCheckable(True)
            act.setChecked(name == current_name)
        reassign.addSeparator()
        reassign.addAction("Unassigned", lambda: self.reassign(task_id, None))
        menu.exec(self.detail.viewport().mapToGlobal(pos))

    def _load_hint(self, name: str) -> str:
        """'Marcus Chen - 13h / 40h this period' for the reassign menu."""
        if not self.result or self._selected is None:
            return name
        row = self.result.row(name)
        _r, c = self._selected
        if row is None or not (1 <= c <= len(row.cells)):
            return name
        cell = row.cells[c - 1]
        return f"{name}  -  {format_hours(cell.planned)} / {format_hours(cell.capacity)}"

    def reassign(self, task_id: int, name: Optional[str]) -> None:
        self.service.update_task(task_id, assignee=name)  # audited like any other edit
        self.refresh()

    def edit_team(self) -> None:
        people = [r.name for r in (self.result.rows if self.result else []) if r.name != UNASSIGNED]
        dlg = TeamDialog(self.service, sorted(set(people) | set(self.service.get_people())), self)
        if dlg.exec():
            self.refresh()

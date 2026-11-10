"""
Advanced Gantt Chart: CPM critical path, float, dependencies, milestones, baselines

Left: task table (start, finish, working days, float). Right: a time-scaled
chart with weekend shading, a today line, bars at the *planned* dates
(critical = red), float tails to the late finish, baseline ghost bars,
milestone diamonds and typed dependency arrows (conflicts dashed orange).

Interaction on the chart:
* drag a bar to move it, drag its right edge to change the finish
  (dependents cascade automatically when the board allows it);
* drag the round handle at a bar's end onto another bar to link them
  (finish-to-start); right-click for milestone/lock/dependency options;
* double-click to open the task.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core import scheduling as sch
from kanbanmanager.core.schedule_engine import ScheduleEngine
from kanbanmanager.core.signals import app_signals
from kanbanmanager.ui import theme
from kanbanmanager.database.service import DatabaseService

ROW_H = 30
HEADER_H = 44
ZOOM_DAY_WIDTH = {"day": 30, "week": 14, "month": 5}

def TC(c) -> QColor:
    """A palette colour in the current theme (colours are looked up at paint time)."""
    return theme.qcolor(c if isinstance(c, str) else c.name())


C_NORMAL = "#3B82F6"
C_CRITICAL = "#EF4444"
C_DONE = "#10B981"
C_MILESTONE = "#F59E0B"
C_UNSCHEDULED = "#64748B"
C_BASELINE = "#94A3B8"
C_LINK = "#64748B"
C_CONFLICT = "#F97316"


@dataclass
class GanttRow:
    task: sch.SchedTask
    start: Optional[date]           # drawn dates (planned, or forecast for unscheduled)
    finish: Optional[date]
    placed_by_schedule: bool        # True: not planned yet, drawn where the schedule would put it
    float_days: Optional[int]
    critical: bool
    late_finish: Optional[date]
    baseline: Optional[Tuple[date, date]] = None
    driver: str = ""


# ---------------------------------------------------------------------------
# Chart widgets
# ---------------------------------------------------------------------------
class GanttModel:
    """Everything the header and canvas need to paint (shared)."""

    def __init__(self):
        self.rows: List[GanttRow] = []
        self.deps: List[sch.SchedDep] = []
        self.conflicts: set = set()          # (pred, succ) pairs violated by the plan
        self.calendar = sch.WorkCalendar()
        self.range_start = date.today() - timedelta(days=7)
        self.range_end = date.today() + timedelta(days=30)
        self.day_w = ZOOM_DAY_WIDTH["day"]
        self.zoom = "day"
        self.show_critical = True
        self.show_float = True

    def x_of(self, d: date) -> float:
        return (d - self.range_start).days * self.day_w

    def date_at(self, x: float) -> date:
        return self.range_start + timedelta(days=int(x // self.day_w))

    @property
    def width(self) -> int:
        return int(((self.range_end - self.range_start).days + 1) * self.day_w)

    def row_index(self, task_id: int) -> Optional[int]:
        return next((i for i, r in enumerate(self.rows) if r.task.id == task_id), None)

    def bar_rect(self, i: int) -> Optional[QRectF]:
        r = self.rows[i]
        if r.start is None:
            return None
        y = i * ROW_H
        if r.task.is_milestone:
            cx = self.x_of(r.start) + self.day_w / 2
            return QRectF(cx - 8, y + ROW_H / 2 - 8, 16, 16)
        x1 = self.x_of(r.start)
        x2 = self.x_of(r.finish) + self.day_w
        return QRectF(x1, y + 7, max(4.0, x2 - x1), ROW_H - 16)


class GanttHeader(QWidget):
    def __init__(self, model: GanttModel, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.model = model
        self.offset = 0
        self.setFixedHeight(HEADER_H)

    def set_offset(self, value: int) -> None:
        self.offset = value
        self.update()

    def paintEvent(self, _event) -> None:
        m = self.model
        p = QPainter(self)
        p.fillRect(self.rect(), TC("#111827"))
        p.translate(-self.offset, 0)
        p.setPen(QPen(TC("#334155")))
        p.drawLine(self.offset, HEADER_H - 1, self.offset + self.width(), HEADER_H - 1)
        font = p.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        p.setFont(font)
        d = m.range_start
        # top row: months
        while d <= m.range_end:
            month_start = d.replace(day=1)
            next_month = (month_start + timedelta(days=32)).replace(day=1)
            x1, x2 = m.x_of(max(month_start, m.range_start)), m.x_of(min(next_month, m.range_end + timedelta(days=1)))
            p.setPen(QPen(TC("#CBD5E1")))
            p.drawText(QRectF(x1 + 4, 2, max(0.0, x2 - x1 - 4), 18), Qt.AlignLeft | Qt.AlignVCenter,
                       month_start.strftime("%B %Y" if x2 - x1 > 90 else "%b"))
            p.setPen(QPen(TC("#334155")))
            p.drawLine(int(x1), 0, int(x1), HEADER_H)
            d = next_month
        # bottom row: days or weeks
        d = m.range_start
        while d <= m.range_end:
            x = m.x_of(d)
            if m.zoom == "day":
                weekend = not m.calendar.is_working(d)
                p.setPen(QPen(TC("#64748B" if weekend else "#94A3B8")))
                p.drawText(QRectF(x, 22, m.day_w, 20), Qt.AlignCenter, f"{d.day}")
            elif d.weekday() == 0:
                p.setPen(QPen(TC("#94A3B8")))
                p.drawText(QRectF(x + 2, 22, m.day_w * 7, 20), Qt.AlignLeft | Qt.AlignVCenter,
                           d.strftime("%d") if m.zoom == "month" else d.strftime("%b %d"))
                p.setPen(QPen(TC("#1F2937")))
                p.drawLine(int(x), 22, int(x), HEADER_H)
            d += timedelta(days=1)
        today_x = m.x_of(date.today()) + m.day_w / 2
        p.setPen(QPen(TC(C_CRITICAL), 2))
        p.drawLine(int(today_x), 28, int(today_x), HEADER_H)
        p.end()


class GanttCanvas(QWidget):
    move_requested = Signal(int, object, object)     # task_id, new_start, new_finish
    link_requested = Signal(int, int)                 # predecessor id, successor id
    context_requested = Signal(int, QPoint)           # task_id, global pos
    open_requested = Signal(int)

    HANDLE_R = 5

    def __init__(self, model: GanttModel, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.model = model
        self.setMouseTracking(True)
        self._drag: Optional[dict] = None
        self._hover: Optional[int] = None

    def sizeHint(self) -> QSize:
        return QSize(self.model.width, max(1, len(self.model.rows)) * ROW_H)

    def relayout(self) -> None:
        self.setFixedSize(self.model.width, max(1, len(self.model.rows)) * ROW_H + 8)
        self.update()

    # Hit testing -------------------------------------------------------
    def _hit(self, pos: QPointF) -> Tuple[Optional[int], str]:
        """(row, part) where part is 'body', 'end' (resize), 'handle' (link) or ''."""
        i = int(pos.y() // ROW_H)
        if not (0 <= i < len(self.model.rows)):
            return None, ""
        rect = self.model.bar_rect(i)
        if rect is None:
            return i, ""
        handle = QPointF(rect.right() + 9, rect.center().y())
        if (pos - handle).manhattanLength() <= self.HANDLE_R * 2:
            return i, "handle"
        if rect.adjusted(-3, -3, 3, 3).contains(pos):
            row = self.model.rows[i]
            if not row.task.is_milestone and pos.x() >= rect.right() - 6:
                return i, "end"
            return i, "body"
        return i, ""

    # Painting ----------------------------------------------------------
    def paintEvent(self, _event) -> None:
        m = self.model
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), TC("#0F172A"))
        # non-working days
        d = m.range_start
        while d <= m.range_end:
            if not m.calendar.is_working(d) and m.zoom != "month":
                p.fillRect(QRectF(m.x_of(d), 0, m.day_w, self.height()), TC("#131C2E"))
            d += timedelta(days=1)
        # row stripes
        for i in range(len(m.rows)):
            if i % 2:
                p.fillRect(QRectF(0, i * ROW_H, self.width(), ROW_H), QColor(255, 255, 255, 6))
        # today
        tx = m.x_of(date.today()) + m.day_w / 2
        p.setPen(QPen(TC(C_CRITICAL), 1, Qt.DashLine))
        p.drawLine(QPointF(tx, 0), QPointF(tx, self.height()))

        self._paint_links(p)
        for i, row in enumerate(m.rows):
            self._paint_row(p, i, row)
        if self._drag and self._drag["kind"] == "link":
            src = self._drag["origin"]
            p.setPen(QPen(TC("#F8FAFC"), 1.5, Qt.DashLine))
            p.drawLine(src, self._drag["pos"])
        p.end()

    def _paint_row(self, p: QPainter, i: int, row: GanttRow) -> None:
        m = self.model
        rect = m.bar_rect(i)
        if self._drag and self._drag.get("row") == i and self._drag["kind"] in ("move", "end"):
            rect = self._drag.get("preview", rect)
        if rect is None:
            return
        y_mid = i * ROW_H + ROW_H / 2
        # baseline ghost
        if row.baseline:
            b_start, b_finish = row.baseline
            bx1 = m.x_of(b_start)
            if row.task.is_milestone:
                cx = bx1 + m.day_w / 2
                p.setPen(QPen(TC(C_BASELINE), 1))
                p.setBrush(Qt.NoBrush)
                p.drawPolygon(QPolygonF([QPointF(cx, y_mid + 3), QPointF(cx + 5, y_mid + 8),
                                         QPointF(cx, y_mid + 13), QPointF(cx - 5, y_mid + 8)]))
            else:
                bx2 = m.x_of(b_finish) + m.day_w
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(148, 163, 184, 110))
                p.drawRoundedRect(QRectF(bx1, i * ROW_H + ROW_H - 8, max(3.0, bx2 - bx1), 4), 2, 2)
        # float tail
        if m.show_float and row.float_days and row.float_days > 0 and row.late_finish and not row.task.is_completed:
            fx = m.x_of(row.late_finish) + m.day_w
            p.setPen(QPen(TC("#475569"), 1.5))
            p.drawLine(QPointF(rect.right(), y_mid), QPointF(fx, y_mid))
            p.drawLine(QPointF(fx, y_mid - 4), QPointF(fx, y_mid + 4))

        critical = m.show_critical and row.critical
        if row.task.is_completed:
            color = TC(C_DONE)
        elif row.placed_by_schedule:
            color = TC(C_UNSCHEDULED)
        elif critical:
            color = TC(C_CRITICAL)
        elif row.task.is_milestone:
            color = TC(C_MILESTONE)
        else:
            color = TC(C_NORMAL)
        if row.task.is_milestone:
            c = rect.center()
            poly = QPolygonF([QPointF(c.x(), rect.top()), QPointF(rect.right(), c.y()),
                              QPointF(c.x(), rect.bottom()), QPointF(rect.left(), c.y())])
            p.setPen(QPen(color.lighter(130), 1))
            p.setBrush(color if not row.placed_by_schedule else Qt.NoBrush)
            p.drawPolygon(poly)
            p.setPen(QPen(TC("#E2E8F0")))
            p.drawText(QRectF(rect.right() + 14, i * ROW_H, 260, ROW_H), Qt.AlignLeft | Qt.AlignVCenter,
                       row.task.title + ("  📌" if row.task.locked else ""))
        else:
            if row.placed_by_schedule:
                p.setPen(QPen(color, 1.2, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
            else:
                p.setPen(QPen(color.darker(130), 1))
                p.setBrush(QBrush(color))
            p.drawRoundedRect(rect, 4, 4)
            label = row.task.title + ("  📌" if row.task.locked else "")
            fm = p.fontMetrics()
            if fm.horizontalAdvance(label) + 10 < rect.width():
                p.setPen(QPen(TC("#FFFFFF")))
                p.drawText(rect.adjusted(6, 0, -4, 0), Qt.AlignLeft | Qt.AlignVCenter, label)
            else:
                p.setPen(QPen(TC("#E2E8F0")))
                p.drawText(QRectF(rect.right() + 14, i * ROW_H, 300, ROW_H), Qt.AlignLeft | Qt.AlignVCenter, label)
        # link handle on hover
        if self._hover == i and not row.placed_by_schedule:
            p.setPen(QPen(TC("#F8FAFC"), 1.2))
            p.setBrush(TC("#0F172A"))
            p.drawEllipse(QPointF(rect.right() + 9, rect.center().y()), self.HANDLE_R, self.HANDLE_R)

    def _paint_links(self, p: QPainter) -> None:
        m = self.model
        index = {r.task.id: i for i, r in enumerate(m.rows)}
        for d in m.deps:
            a, b = index.get(d.pred), index.get(d.succ)
            if a is None or b is None:
                continue
            ra, rb = m.bar_rect(a), m.bar_rect(b)
            if ra is None or rb is None:
                continue
            sx = ra.left() if d.kind in (sch.SS, sch.SF) else ra.right()
            tx = rb.right() if d.kind in (sch.FF, sch.SF) else rb.left()
            sy, ty = ra.center().y(), rb.center().y()
            conflict = (d.pred, d.succ) in m.conflicts
            critical = m.show_critical and m.rows[a].critical and m.rows[b].critical
            color = TC(C_CONFLICT if conflict else (C_CRITICAL if critical else C_LINK))
            pen = QPen(color, 1.4, Qt.DashLine if conflict else Qt.SolidLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            out_dir = -1 if d.kind in (sch.SS, sch.SF) else 1
            in_dir = 1 if d.kind in (sch.FF, sch.SF) else -1
            x1 = sx + 8 * out_dir          # leave the source bar
            x2 = tx + 8 * in_dir           # approach the target bar from the correct side
            path = QPainterPath(QPointF(sx, sy))
            path.lineTo(x1, sy)
            room = (x2 >= x1) if in_dir == -1 else (x2 <= x1)
            if room:
                # simple elbow: across, down, across
                path.lineTo(x1, ty)
            else:
                # no room: drop to the row boundary, go back, then down to the target row
                mid_y = ty - ROW_H / 2 if ty > sy else ty + ROW_H / 2
                path.lineTo(x1, mid_y)
                path.lineTo(x2, mid_y)
                path.lineTo(x2, ty)
            path.lineTo(tx, ty)
            p.drawPath(path)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            head = -in_dir
            p.drawPolygon(QPolygonF([QPointF(tx, ty), QPointF(tx - 6 * head, ty - 4), QPointF(tx - 6 * head, ty + 4)]))

    # Mouse ---------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        pos = event.position()
        i, part = self._hit(pos)
        if event.button() == Qt.RightButton and i is not None and part:
            self.context_requested.emit(self.model.rows[i].task.id, event.globalPosition().toPoint())
            return
        if event.button() != Qt.LeftButton or i is None or not part:
            return super().mousePressEvent(event)
        row = self.model.rows[i]
        rect = self.model.bar_rect(i)
        if part == "handle":
            self._drag = {"kind": "link", "row": i, "origin": QPointF(rect.right() + 9, rect.center().y()), "pos": pos}
        elif row.task.locked or row.task.is_completed:
            QToolTip.showText(event.globalPosition().toPoint(),
                              "📌 This task's dates are locked" if row.task.locked else "Completed tasks can't be moved")
            return
        else:
            self._drag = {"kind": "end" if part == "end" else "move", "row": i, "x0": pos.x(), "rect": QRectF(rect),
                          "preview": QRectF(rect)}
        self.update()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        if self._drag:
            self._drag["pos"] = pos
            if self._drag["kind"] in ("move", "end"):
                dx = round((pos.x() - self._drag["x0"]) / self.model.day_w) * self.model.day_w
                r = QRectF(self._drag["rect"])
                if self._drag["kind"] == "move":
                    r.translate(dx, 0)
                else:
                    r.setRight(max(r.left() + self.model.day_w, r.right() + dx))
                self._drag["preview"] = r
                self._drag["days"] = int(dx // self.model.day_w)
            self.update()
            return
        i, part = self._hit(pos)
        hover = i if part else None
        if hover != self._hover:
            self._hover = hover
            self.update()
        self.setCursor(Qt.SizeHorCursor if part == "end" else Qt.CrossCursor if part == "handle"
                       else Qt.OpenHandCursor if part == "body" else Qt.ArrowCursor)
        if part and i is not None:
            QToolTip.showText(event.globalPosition().toPoint(), self.tooltip_for(i), self)

    def mouseReleaseEvent(self, event) -> None:
        drag, self._drag = self._drag, None
        if not drag:
            return super().mouseReleaseEvent(event)
        row = self.model.rows[drag["row"]]
        if drag["kind"] == "link":
            j, part = self._hit(event.position())
            if j is not None and part and j != drag["row"]:
                self.link_requested.emit(row.task.id, self.model.rows[j].task.id)
        else:
            days = drag.get("days", 0)
            if days and row.start is not None:
                cal = self.model.calendar
                if drag["kind"] == "move":
                    new_start = cal.next_working(row.start + timedelta(days=days))
                    if row.task.is_milestone:
                        new_finish = new_start
                    else:
                        new_finish = cal.day(cal.index_start(new_start) + max(1, row.task.duration) - 1)
                else:
                    raw = row.finish + timedelta(days=days)
                    new_finish = max(row.start, cal.day(cal.index_end(raw) - 1))
                    new_start = row.start
                self.move_requested.emit(row.task.id, new_start, new_finish)
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        i, part = self._hit(event.position())
        if i is not None and part:
            self.open_requested.emit(self.model.rows[i].task.id)

    def leaveEvent(self, _event) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()

    def tooltip_for(self, i: int) -> str:
        row = self.model.rows[i]
        t = row.task
        lines = [t.title]
        if row.start:
            if t.is_milestone:
                lines.append(f"◆ Milestone: {row.start:%a %b %d, %Y}")
            else:
                lines.append(f"{row.start:%a %b %d} → {row.finish:%a %b %d, %Y} ({t.duration} working day"
                             f"{'s' if t.duration != 1 else ''})")
        if row.placed_by_schedule:
            lines.append("Not scheduled yet - shown where the schedule would place it")
        if t.is_completed:
            lines.append("✓ Completed")
        elif row.float_days is not None:
            lines.append("🔥 On the critical path" if row.critical else f"Float: {row.float_days} working days")
        if row.driver:
            lines.append(f"Earliest start driven by: {row.driver}")
        if row.baseline:
            lines.append(f"Baseline: {row.baseline[0]:%b %d} → {row.baseline[1]:%b %d}")
        if t.locked:
            lines.append("📌 Dates locked")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------
class GanttView(QWidget):
    def __init__(self, service: Optional[DatabaseService] = None, parent: Optional[QWidget] = None,
                 engine: Optional[ScheduleEngine] = None):
        super().__init__(parent)
        self.service = service or DatabaseService()
        self.engine = engine or ScheduleEngine(self.service)
        self.current_board_id: Optional[int] = None
        self.model = GanttModel()
        self.result: Optional[sch.ScheduleResult] = None
        self.settings = dict(sch.DEFAULT_SCHEDULE_SETTINGS)
        self._syncing = False
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        layout.addLayout(top)
        title = QLabel("📈 Gantt & Schedule")
        title.setStyleSheet("font-size: 16px; font-weight: 800;")
        top.addWidget(title)
        top.addStretch()
        self.auto_btn = QPushButton("📅 Auto-schedule...")
        self.auto_btn.setToolTip("Propose dates from dependencies and durations; review before applying")
        self.auto_btn.setObjectName("PrimaryButton")
        self.auto_btn.clicked.connect(self.open_auto_schedule)
        top.addWidget(self.auto_btn)
        self.baselines_btn = QPushButton("📸 Baselines...")
        self.baselines_btn.setToolTip("Save the current plan and compare against it later")
        self.baselines_btn.clicked.connect(self.open_baselines)
        top.addWidget(self.baselines_btn)
        self.settings_btn = QPushButton("⚙ Schedule Settings...")
        self.settings_btn.clicked.connect(self.open_settings)
        top.addWidget(self.settings_btn)
        self.export_btn = QToolButton()
        self.export_btn.setText("⬇ Export")
        self.export_btn.setToolTip("The whole chart - every row and date - with task names")
        self.export_btn.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(self.export_btn)
        export_menu.addAction("PDF (vector)…").triggered.connect(self._export_pdf)
        export_menu.addAction("SVG (vector)…").triggered.connect(self._export_svg)
        export_menu.addAction("PNG…").triggered.connect(self._export_png)
        self.export_btn.setMenu(export_menu)
        top.addWidget(self.export_btn)

        controls = QHBoxLayout()
        layout.addLayout(controls)
        self.zoom_combo = QComboBox()
        for key, label in (("day", "Days"), ("week", "Weeks"), ("month", "Months")):
            self.zoom_combo.addItem(label, key)
        self.zoom_combo.currentIndexChanged.connect(self._on_zoom)
        controls.addWidget(self.zoom_combo)
        self.today_btn = QPushButton("Today")
        self.today_btn.clicked.connect(self.scroll_to_today)
        controls.addWidget(self.today_btn)
        self.critical_chk = QCheckBox("🔥 Critical path")
        self.critical_chk.setChecked(True)
        self.critical_chk.toggled.connect(self._on_display_option)
        controls.addWidget(self.critical_chk)
        self.float_chk = QCheckBox("Float")
        self.float_chk.setChecked(True)
        self.float_chk.setToolTip("Show how far each task can slip without delaying the project")
        self.float_chk.toggled.connect(self._on_display_option)
        controls.addWidget(self.float_chk)
        controls.addWidget(QLabel("Baseline:"))
        self.baseline_combo = QComboBox()
        self.baseline_combo.setMinimumWidth(150)
        self.baseline_combo.currentIndexChanged.connect(self._on_baseline_selected)
        controls.addWidget(self.baseline_combo)
        controls.addStretch()

        self.summary_lbl = QLabel("")
        self.summary_lbl.setTextFormat(Qt.RichText)
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setStyleSheet("color: #CBD5E1;")
        layout.addWidget(self.summary_lbl)

        self.splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(self.splitter, 1)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Task", "Start", "Finish", "Days", "Float"])
        self.table.horizontalHeader().setFixedHeight(HEADER_H)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 5):
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_H)
        self.table.verticalHeader().setMinimumSectionSize(ROW_H)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.itemDoubleClicked.connect(self._open_from_table)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.setMinimumWidth(330)  # keep task names and every column readable at small widths
        self.splitter.addWidget(self.table)

        chart = QWidget()
        self.splitter.addWidget(chart)
        cl = QVBoxLayout(chart)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self.header = GanttHeader(self.model)
        cl.addWidget(self.header)
        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(False)
        cl.addWidget(self.scroll, 1)
        self.canvas = GanttCanvas(self.model)
        self.scroll.setWidget(self.canvas)
        self.canvas.move_requested.connect(self.move_task)
        self.canvas.link_requested.connect(self.link_tasks)
        self.canvas.context_requested.connect(self.show_task_menu)
        self.canvas.open_requested.connect(app_signals.task_edit_requested.emit)
        self.scroll.horizontalScrollBar().valueChanged.connect(self.header.set_offset)
        self.scroll.verticalScrollBar().valueChanged.connect(self._sync_table_scroll)
        self.splitter.setSizes([340, 800])

        legend = QLabel(
            "<span style='color:#3B82F6'>■</span> task &nbsp; <span style='color:#EF4444'>■</span> critical &nbsp; "
            "<span style='color:#10B981'>■</span> done &nbsp; <span style='color:#F59E0B'>◆</span> milestone &nbsp; "
            "<span style='color:#64748B'>▭</span> not scheduled yet &nbsp; ─┤ float &nbsp; "
            "<span style='color:#94A3B8'>▂</span> baseline &nbsp; <span style='color:#F97316'>- -</span> conflict"
            " &nbsp;·&nbsp; Drag bars to move, their right edge to resize, the ○ handle onto another bar to link.")
        legend.setTextFormat(Qt.RichText)
        legend.setWordWrap(True)
        legend.setStyleSheet("color: #64748B; font-size: 11px;")
        layout.addWidget(legend)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def export_chart(self, fmt: str, path: Optional[str] = None):
        from kanbanmanager.ui.reports import export_gantt

        if path is None:
            filters = {"pdf": "PDF (*.pdf)", "svg": "SVG (*.svg)", "png": "PNG (*.png)"}
            path, _ = QFileDialog.getSaveFileName(self, "Export Gantt Chart", f"gantt.{fmt}", filters[fmt])
        if not path:
            return None
        return export_gantt(self, path, fmt)

    def _export_pdf(self, _checked: bool = False) -> None:
        self.export_chart("pdf")

    def _export_svg(self, _checked: bool = False) -> None:
        self.export_chart("svg")

    def _export_png(self, _checked: bool = False) -> None:
        self.export_chart("png")

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def load_board(self, board_id: int) -> None:
        first = board_id != self.current_board_id
        self.current_board_id = board_id
        self.refresh()
        if first:
            self.scroll_to_today()

    def refresh(self) -> None:
        if not self.current_board_id:
            return
        settings, calendar, tasks, deps = sch.load_board_schedule(self.service, self.current_board_id)
        self.settings = settings
        result = sch.analyze(tasks, deps, calendar, default_start=sch.project_start_for(settings))
        self.result = result
        baseline_map: Dict[int, Tuple[date, date]] = {}
        baseline_id = settings.get("baseline_id")
        baseline = self.service.get_baseline(baseline_id) if baseline_id else None
        if baseline:
            for e in baseline["entries"]:
                if e.get("task_id") is not None:
                    baseline_map[e["task_id"]] = (sch._to_date(e["start_date"]), sch._to_date(e["finish_date"]))

        rows: List[GanttRow] = []
        for t in tasks:
            if t.is_completed and not t.scheduled:
                continue  # finished work with no dates has no place on a schedule
            ts = result.tasks.get(t.id)
            if t.scheduled:
                start, finish, placed = t.start, t.finish, False
            elif ts is not None:
                start, finish, placed = result.start_date(t.id), result.finish_date(t.id), True
            else:
                start = finish = None
                placed = False
            rows.append(GanttRow(
                task=t, start=start, finish=finish, placed_by_schedule=placed,
                float_days=ts.total_float if ts else None, critical=bool(ts and ts.is_critical),
                late_finish=result.late_finish_date(t.id) if ts else None,
                baseline=baseline_map.get(t.id), driver=(ts.driver or "") if ts else "",
            ))
        rows.sort(key=lambda r: (r.start is None, r.placed_by_schedule, r.start or date.max, r.task.title.lower()))
        m = self.model
        m.rows = rows
        m.deps = result.deps
        m.conflicts = {(c.pred, c.succ) for c in result.conflicts}
        m.calendar = calendar
        dates = [d for r in rows for d in (r.start, r.finish, r.late_finish) if d]
        dates += [d for r in rows if r.baseline for d in r.baseline]
        lo = min(dates + [date.today()])
        hi = max(dates + [date.today()])
        m.range_start = lo - timedelta(days=lo.weekday() + 7)
        m.range_end = hi + timedelta(days=21)
        self._fill_table()
        self._fill_baselines(baseline_id)
        self._render_summary()
        self.canvas.relayout()
        self.header.update()

    def _fill_table(self) -> None:
        rows = self.model.rows
        self.table.setRowCount(len(rows))
        def when(d: Optional[date], forecast: bool) -> str:
            if d is None:
                return "-"
            return ("~" if forecast else "") + d.strftime("%b %d")  # "~" = where the schedule would put it

        for i, r in enumerate(rows):
            t = r.task
            name = ("◆ " if t.is_milestone else "") + t.title + ("  📌" if t.locked else "")
            values = [
                name,
                when(r.start, r.placed_by_schedule),
                when(r.finish, r.placed_by_schedule),
                "0" if t.is_milestone else str(t.duration),
                "✓" if t.is_completed else ("-" if r.float_days is None else str(r.float_days)),
            ]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, t.id)
                item.setToolTip(self.canvas.tooltip_for(i))
                if c == 4 and r.critical and not t.is_completed and self.model.show_critical:
                    item.setForeground(TC("#F87171"))
                if r.placed_by_schedule and c in (1, 2):
                    item.setForeground(TC("#94A3B8"))
                self.table.setItem(i, c, item)
            self.table.setRowHeight(i, ROW_H)

    def _fill_baselines(self, selected: Optional[int]) -> None:
        self.baseline_combo.blockSignals(True)
        self.baseline_combo.clear()
        self.baseline_combo.addItem("(none)", None)
        for b in self.service.get_baselines(self.current_board_id):
            self.baseline_combo.addItem(b["name"], b["id"])
        idx = self.baseline_combo.findData(selected)
        self.baseline_combo.setCurrentIndex(max(0, idx))
        self.baseline_combo.blockSignals(False)

    def _render_summary(self) -> None:
        r = self.result
        if r is None or not r.tasks:
            self.summary_lbl.setText("No tasks to schedule yet. Add start/due dates or estimates to tasks.")
            return
        titles = {t.id: t.title for t in r.inputs.values()}
        parts = [f"Forecast: <b>{r.project_start_date:%b %d}</b> → <b>{r.project_finish_date:%b %d, %Y}</b> "
                 f"({r.duration_days} working days)"]
        crit = r.critical_ids
        if crit:
            path = r.critical_path()
            chain = " → ".join(titles[i] for i in path[:4]) + (" → …" if len(path) > 4 else "")
            parts.append(f"<span style='color:#F87171'>🔥 {len(crit)} critical: {chain}</span>")
        unscheduled = sum(1 for row in self.model.rows if row.placed_by_schedule)
        if unscheduled:
            parts.append(f"<span style='color:#94A3B8'>{unscheduled} not scheduled yet</span>")
        if r.conflicts:
            parts.append(f"<span style='color:#FB923C'>⚠ {len(r.conflicts)} dependency conflict"
                         f"{'s' if len(r.conflicts) != 1 else ''}: {r.conflicts[0].describe(titles)}"
                         f"{' …' if len(r.conflicts) > 1 else ''} - use Auto-schedule to fix</span>")
        if r.cycle_ids:
            parts.append(f"<span style='color:#F87171'>{len(r.cycle_ids)} task(s) in a dependency cycle</span>")
        baseline_id = self.settings.get("baseline_id")
        if baseline_id:
            cmp = self.engine.compare(self.current_board_id, baseline_id)
            if cmp and cmp.finish_var is not None:
                late = cmp.count("late")
                tone = "#F87171" if cmp.finish_var > 0 else "#34D399"
                parts.append(f"<span style='color:{tone}'>vs baseline: finish {cmp.finish_var:+d} days, "
                             f"{late} task{'s' if late != 1 else ''} late</span>")
        self.summary_lbl.setText(" &nbsp;|&nbsp; ".join(parts))

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    def _on_zoom(self) -> None:
        self.model.zoom = self.zoom_combo.currentData()
        self.model.day_w = ZOOM_DAY_WIDTH[self.model.zoom]
        self.canvas.relayout()
        self.header.update()
        self.scroll_to_today()

    def _on_display_option(self) -> None:
        self.model.show_critical = self.critical_chk.isChecked()
        self.model.show_float = self.float_chk.isChecked()
        self._fill_table()
        self.canvas.update()

    def _on_baseline_selected(self) -> None:
        settings = dict(self.settings)
        settings["baseline_id"] = self.baseline_combo.currentData()
        self.service.set_schedule_settings(self.current_board_id, settings)
        self.refresh()

    def scroll_to_today(self) -> None:
        x = self.model.x_of(date.today())
        self.scroll.horizontalScrollBar().setValue(max(0, int(x - 3 * self.model.day_w * (7 if self.model.zoom != "day" else 1))))

    def _sync_table_scroll(self, value: int) -> None:
        self.table.verticalScrollBar().setValue(value)

    def wheelEvent(self, event) -> None:  # scrolling over the table scrolls the chart too
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().value() - event.angleDelta().y() // 2)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def move_task(self, task_id: int, new_start: date, new_finish: date) -> None:
        row = next((r for r in self.model.rows if r.task.id == task_id), None)
        if row is None:
            return
        self.service.update_task(task_id, start_date=new_start, due_date=sch.due_datetime(row.task, new_finish))
        self.refresh()
        app_signals.board_refresh_requested.emit()

    def link_tasks(self, pred_id: int, succ_id: int) -> None:
        ok, err = self.service.add_dependency(task_id=succ_id, depends_on_task_id=pred_id)
        if not ok and err:
            QMessageBox.warning(self, "Can't link tasks", err)
        self.refresh()

    def _open_from_table(self, item: QTableWidgetItem) -> None:
        app_signals.task_edit_requested.emit(item.data(Qt.UserRole))

    def _table_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is not None:
            self.show_task_menu(item.data(Qt.UserRole), self.table.viewport().mapToGlobal(pos))

    def show_task_menu(self, task_id: int, global_pos: QPoint) -> None:
        row = next((r for r in self.model.rows if r.task.id == task_id), None)
        if row is None:
            return
        t = row.task
        menu = QMenu(self)
        menu.addAction("✏ Open Task", lambda: app_signals.task_edit_requested.emit(task_id))
        menu.addSeparator()
        menu.addAction("◆ Turn into a regular task" if t.is_milestone else "◆ Make a milestone",
                       lambda: self.toggle_milestone(task_id))
        menu.addAction("🔓 Unlock dates" if t.locked else "📌 Lock dates", lambda: self.toggle_lock(task_id))
        menu.addAction("🔗 Dependencies...", lambda: self.edit_dependencies(task_id))
        pred_menu = menu.addMenu("➕ Add predecessor")
        existing = {d.pred for d in self.model.deps if d.succ == task_id}
        for other in sorted((r.task for r in self.model.rows if r.task.id != task_id and r.task.id not in existing),
                            key=lambda x: x.title.lower()):
            pred_menu.addAction(other.title.replace("&", "&&"), lambda o=other.id: self.link_tasks(o, task_id))
        if row.start and not row.placed_by_schedule and not t.locked:
            menu.addSeparator()
            menu.addAction("🗓 Clear dates", lambda: self.clear_dates(task_id))
        menu.exec(global_pos)

    def toggle_milestone(self, task_id: int) -> None:
        task = self.service.get_task(task_id)
        if task is None:
            return
        if task.is_milestone:
            self.service.update_task(task_id, is_milestone=False)
            self.refresh()
            app_signals.board_refresh_requested.emit()
            return
        # A milestone is a point in time: its due date (or start date) becomes the milestone date
        when = task.due_date
        if when is None and task.start_date is not None:
            when = datetime.combine(task.start_date, time(17, 0))
        self.service.update_task(task_id, is_milestone=True, due_date=when, start_date=when.date() if when else None)
        self.refresh()
        app_signals.board_refresh_requested.emit()

    def toggle_lock(self, task_id: int) -> None:
        task = self.service.get_task(task_id)
        if task is not None:
            self.service.update_task(task_id, dates_locked=not task.dates_locked)
            self.refresh()

    def clear_dates(self, task_id: int) -> None:
        self.service.update_task(task_id, start_date=None, due_date=None)
        self.refresh()
        app_signals.board_refresh_requested.emit()

    def edit_dependencies(self, task_id: int) -> None:
        from kanbanmanager.ui.dialogs.schedule_dialogs import DependencyDialog

        dlg = DependencyDialog(self.service, self.current_board_id, task_id, self)
        dlg.exec()
        dlg.deleteLater()
        self.refresh()
        app_signals.board_refresh_requested.emit()

    def open_auto_schedule(self) -> None:
        from kanbanmanager.ui.dialogs.schedule_dialogs import AutoScheduleDialog

        dlg = AutoScheduleDialog(self.engine, self.current_board_id, self)
        applied = dlg.exec()
        dlg.deleteLater()
        if applied:
            self.refresh()
            app_signals.board_refresh_requested.emit()

    def open_baselines(self) -> None:
        from kanbanmanager.ui.dialogs.schedule_dialogs import BaselinesDialog

        dlg = BaselinesDialog(self.engine, self.current_board_id, self)
        dlg.exec()
        dlg.deleteLater()
        self.refresh()

    def open_settings(self) -> None:
        from kanbanmanager.ui.dialogs.schedule_dialogs import ScheduleSettingsDialog

        dlg = ScheduleSettingsDialog(self.service, self.current_board_id, self)
        if dlg.exec():
            self.refresh()
        dlg.deleteLater()

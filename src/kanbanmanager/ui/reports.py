"""
Reports and view exports (PDF / SVG / PNG).

* ``build_charts`` turns core.analytics numbers into ``Chart`` objects (shared by
  the Analytics view and the PDF report).
* ``write_board_report`` writes a multi-page A4 PDF: KPIs and vector charts,
  then a task list per column (assignee, due date, priority, labels, custom
  fields, optional descriptions) - with headers, footers and page numbers.
* ``export_board_snapshot`` renders the whole board (every column at full
  height, not just what fits on screen) to PDF / SVG / PNG.
* ``export_gantt`` renders the Gantt chart with a task-name column;
  ``export_widget`` renders any view as it is on screen.

Everything painted into PDF and SVG stays vector (text, shapes); PNG is
rendered at 2x for sharpness. These need a Qt GUI application (the offscreen
platform is fine for the command line).
"""
from __future__ import annotations

import html
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QCoreApplication, QEvent, QMarginsF, QPoint, QPointF, QRectF, QSize, QSizeF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QPen,
    QTextDocument,
)
from PySide6.QtSvg import QSvgGenerator
from PySide6.QtWidgets import QApplication, QWidget

from kanbanmanager.config import APP_NAME
from kanbanmanager.core import analytics as an
from kanbanmanager.core import custom_fields as cf
from kanbanmanager.enums import Priority
from kanbanmanager.ui.charts import DARK, LIGHT, Chart, Theme, paint_chart

PAGE_MARGIN_MM = 12
PRIORITIES = {p.value for p in Priority}


# ---------------------------------------------------------------------------
# Charts from analytics
# ---------------------------------------------------------------------------
def build_charts(service, board_id: int, days: int = 30, points_field_id: Optional[int] = None,
                 today: Optional[date] = None, data: Optional[an.BoardData] = None) -> Dict[str, Chart]:
    data = data or an.load_board(service, board_id)
    start, end = an.default_range(days, today)
    charts: Dict[str, Chart] = {}

    flow = an.cumulative_flow(data, start, end)
    charts["flow"] = Chart("area", "Cumulative flow", labels=flow.days, series=flow.series,
                           subtitle="Tasks in each column at the end of every day - bands that widen are queues")

    points = next((f for f in an.number_fields(data) if f["id"] == points_field_id), None)
    bu = an.burnup(data, start, end, points["id"] if points else None)
    unit = bu.unit
    charts["burnup"] = Chart("line", "Burn-up", labels=bu.days,
                             series=[("Total scope", "#94A3B8", bu.scope), ("Done", "#10B981", bu.done)],
                             subtitle=f"Scope vs. completed ({unit}) - the gap is what's left",
                             value_format="{:g}")

    weeks = max(4, min(26, days // 7 + 1))
    tp = an.throughput(data, weeks, today)
    avg = sum(n for _d, n in tp) / len(tp) if tp else 0
    charts["throughput"] = Chart("bars", "Throughput", labels=[d for d, _n in tp],
                                 series=[("Completed", "#3B82F6", [n for _d, n in tp])],
                                 subtitle=f"Tasks completed per week (average {avg:.1f})")

    cycles = an.cycle_times(data, start, end)
    values = [c.cycle_days for c in cycles]
    markers = []
    if values:
        markers = [("avg", sum(values) / len(values), "#F59E0B"), ("85%", an.percentile(values, 85), "#EF4444")]
    charts["cycle"] = Chart("histogram", "Cycle time", series=[("Cycle time", "#8B5CF6", values)], markers=markers,
                            subtitle=f"{len(values)} task{'s' if len(values) != 1 else ''} finished - days from "
                                     "starting work to done", y_label="days")

    by_col = an.breakdown(data, "column")
    wip = {c["name"]: c["wip"] for c in data.columns}
    charts["columns"] = Chart("bars", "Open tasks by column",
                              slices=[(f"{n} (≤{wip[n]})" if wip.get(n) else n, v, c) for n, v, c in by_col],
                              subtitle="WIP limits in brackets")
    charts["priority"] = Chart("donut", "Open tasks by priority", slices=an.breakdown(data, "priority"))
    charts["assignee"] = Chart("hbars", "Open tasks by assignee", slices=an.breakdown(data, "assignee"))
    charts["label"] = Chart("hbars", "Open tasks by label", slices=an.breakdown(data, "label"))
    select = next((f for f in data.fields if f["field_type"] in ("select", "multi_select")), None)
    if select is not None:
        if points is not None:
            charts["field"] = Chart("donut", f"{points['name']} by {select['name']}",
                                    slices=an.breakdown(data, f"field:{select['id']}", sum_field_id=points["id"]),
                                    subtitle="Open work")
        else:
            charts["field"] = Chart("donut", f"Open tasks by {select['name']}",
                                    slices=an.breakdown(data, f"field:{select['id']}"))
    return charts


# ---------------------------------------------------------------------------
# PDF report
# ---------------------------------------------------------------------------
def _pdf_writer(path: Path, title: str, landscape: bool = False) -> QPdfWriter:
    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setPageOrientation(QPageLayout.Landscape if landscape else QPageLayout.Portrait)
    writer.setPageMargins(QMarginsF(PAGE_MARGIN_MM, PAGE_MARGIN_MM, PAGE_MARGIN_MM, PAGE_MARGIN_MM),
                          QPageLayout.Millimeter)
    writer.setResolution(96)
    writer.setTitle(title)
    writer.setCreator(APP_NAME)
    return writer


def _font(size: float, bold: bool = False) -> QFont:
    f = QFont()
    f.setPointSizeF(size)
    f.setBold(bold)
    return f


def _kpi_boxes(p: QPainter, rect: QRectF, items: Sequence[Tuple[str, str, str]], theme: Theme) -> None:
    n = len(items)
    gap = 8
    w = (rect.width() - gap * (n - 1)) / n
    for i, (label, value, color) in enumerate(items):
        box = QRectF(rect.left() + i * (w + gap), rect.top(), w, rect.height())
        p.setPen(QPen(QColor(theme.grid), 1))
        p.setBrush(QColor("#F8FAFC" if theme is LIGHT else theme.card))
        p.drawRoundedRect(box, 6, 6)
        p.setFont(_font(16, True))
        p.setPen(QColor(color))
        p.drawText(box.adjusted(8, 4, -8, -22), int(Qt.AlignLeft | Qt.AlignVCenter), value)
        p.setFont(_font(7.5))
        p.setPen(QColor(theme.muted))
        p.drawText(box.adjusted(6, box.height() - 24, -4, -4), int(Qt.AlignLeft | Qt.AlignVCenter),
                   p.fontMetrics().elidedText(label, Qt.ElideRight, int(box.width() - 10)))


def _task_tables_html(service, data: an.BoardData, include_descriptions: bool, include_completed: bool) -> str:
    # compact fields only (links, e-mails and long text would squeeze the task names)
    fields = [f for f in data.fields if f.get("show_on_card") and f["field_type"] not in ("url", "email")
              and not (f["field_type"] == "text" and (f.get("config") or {}).get("multiline"))][:4]
    this_year = date.today().year
    cols = data.columns
    out = ["<html><body style='font-family: sans-serif; font-size: 9pt; color: #0F172A;'>"]
    for col in cols:
        tasks = [t for t in data.tasks if t.column_id == col["id"] and (include_completed or not t.is_completed)]
        wip = f" · WIP limit {col['wip']}" if col["wip"] else ""
        out.append(f"<h3 style='color:{col['color']}; margin-top: 14px;'>{html.escape(col['name'])} "
                   f"<span style='color:#64748B; font-weight: normal;'>({len(tasks)}{wip})</span></h3>")
        if not tasks:
            out.append("<p style='color:#94A3B8;'>No tasks</p>")
            continue
        headers = ["Task", "Assignee", "Due", "Priority", "Labels"] + [f["name"] for f in fields]
        out.append("<table width='100%' cellspacing='0' cellpadding='4' style='border-collapse: collapse;'>")
        out.append("<tr>" + "".join(f"<th align='left' style='background:#E2E8F0; color:#334155;'"
                                    f"{' width=\'32%\'' if i == 0 else ''}>{html.escape(h)}</th>"
                                    for i, h in enumerate(headers)) + "</tr>")
        for i, t in enumerate(tasks):
            bg = "#FFFFFF" if i % 2 == 0 else "#F8FAFC"
            title = html.escape(t.title)
            if t.is_completed:
                title = f"<s style='color:#64748B'>{title}</s>"
            flags = []
            if t.is_blocked:
                flags.append("🔒")
            if t.is_milestone:
                flags.append("◆")
            if t.is_overdue:
                flags.append("<span style='color:#DC2626'>overdue</span>")
            values = t.field_value_map
            cells = [
                f"<b>{title}</b> {' '.join(flags)}",
                html.escape(t.assignee or ""),
                (t.due_date.strftime("%b %d") if t.due_date.year == this_year else t.due_date.strftime("%b %d, %Y"))
                if t.due_date else "",
                html.escape(Priority(t.priority).display_name) if t.priority in PRIORITIES else "",
                html.escape(", ".join(l.name for l in t.labels)),
            ] + [html.escape(cf.format_value(f, values[f["id"]])) if f["id"] in values else "" for f in fields]
            out.append(f"<tr style='background:{bg}'>" + "".join(f"<td valign='top'>{c}</td>" for c in cells) + "</tr>")
            if include_descriptions and (t.description or "").strip():
                from kanbanmanager.core.markdown_utils import markdown_to_plain

                text = markdown_to_plain(t.description)[:600]
                out.append(f"<tr style='background:{bg}'><td colspan='{len(headers)}' style='color:#475569;'>"
                           f"{html.escape(text).replace(chr(10), '<br>')}</td></tr>")
        out.append("</table>")
    out.append("</body></html>")
    return "".join(out)


def write_board_report(service, board_id: int, path: Path | str, days: int = 30,
                       points_field_id: Optional[int] = None, include_charts: bool = True,
                       include_tasks: bool = True, include_descriptions: bool = False,
                       include_completed: bool = True, today: Optional[date] = None) -> Path:
    """A printable PDF report of a board. Returns the path."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = an.load_board(service, board_id)
    start, end = an.default_range(days, today)
    theme = LIGHT
    writer = _pdf_writer(dest, f"{data.name} - report")
    page = QRectF(writer.pageLayout().paintRectPixels(writer.resolution()))
    page = QRectF(0, 0, page.width(), page.height())
    header_h, footer_h = 40.0, 22.0
    body = page.adjusted(0, header_h, 0, -footer_h)
    generated = datetime.now().strftime("%b %d, %Y %H:%M")
    pages: List[Any] = []  # callables painting a page body

    def first_page(p: QPainter) -> None:
        k = an.kpis(data, start, end)
        board = service.get_board(board_id)
        p.setFont(_font(20, True))
        p.setPen(QColor(theme.text))
        p.drawText(QRectF(body.left(), body.top(), body.width(), 34), int(Qt.AlignLeft | Qt.AlignVCenter), data.name)
        y = body.top() + 36
        if board and board.description:
            p.setFont(_font(9))
            p.setPen(QColor(theme.muted))
            r = QRectF(body.left(), y, body.width(), 40)
            p.drawText(r, int(Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop), board.description[:300])
            y += 40
        p.setFont(_font(9))
        p.setPen(QColor(theme.muted))
        p.drawText(QRectF(body.left(), y, body.width(), 16), int(Qt.AlignLeft | Qt.AlignVCenter),
                   f"Period {start:%b %d} – {end:%b %d, %Y}  ·  {k['total']} tasks, {k['open']} open")
        y += 24
        items = [("Completed", f"{k['completion_pct']}%", "#10B981"), ("Open tasks", str(k["open"]), "#3B82F6"),
                 ("Done in period", str(k["completed_in_period"]), "#8B5CF6"),
                 ("Per week", f"{k['per_week']:g}", "#0EA5E9"),
                 ("Avg cycle, days", "–" if k["avg_cycle"] is None else f"{k['avg_cycle']:g}", "#F59E0B"),
                 ("Overdue", str(k["overdue"]), "#DC2626" if k["overdue"] else "#64748B"),
                 ("Blocked", str(k["blocked"]), "#D97706" if k["blocked"] else "#64748B")]
        _kpi_boxes(p, QRectF(body.left(), y, body.width(), 62), items, theme)
        y += 76
        if k["wip_over"]:
            p.setFont(_font(9, True))
            p.setPen(QColor("#DC2626"))
            p.drawText(QRectF(body.left(), y, body.width(), 16), int(Qt.AlignLeft | Qt.AlignVCenter),
                       "⚠ Over the WIP limit: " + ", ".join(k["wip_over"]))
            y += 22
        if include_charts:
            grid(p, ["flow", "burnup", "throughput", "cycle"], y)

    charts = build_charts(service, board_id, days, points_field_id, today, data) if include_charts else {}

    def grid(p: QPainter, keys: Sequence[str], top: float) -> None:
        keys = [k for k in keys if k in charts]
        gap = 10
        w = (body.width() - gap) / 2
        rows = (len(keys) + 1) // 2
        h = min(270.0, (body.bottom() - top - gap * (rows - 1)) / max(1, rows))
        for i, key in enumerate(keys):
            r, c = divmod(i, 2)
            paint_chart(p, QRectF(body.left() + c * (w + gap), top + r * (h + gap), w, h), charts[key], theme)

    pages.append(first_page)
    if include_charts:
        rest = [k for k in ("columns", "priority", "assignee", "label", "field") if k in charts]
        if rest:
            pages.append(lambda p: (p.setFont(_font(13, True)), p.setPen(QColor(theme.text)),
                                    p.drawText(QRectF(body.left(), body.top(), body.width(), 24),
                                               int(Qt.AlignLeft | Qt.AlignVCenter), "Where the work is"),
                                    grid(p, rest, body.top() + 30)))

    doc = None
    if include_tasks:
        doc = QTextDocument()
        doc.setDefaultFont(_font(9))
        doc.setDocumentMargin(0)
        doc.setHtml(_task_tables_html(service, data, include_descriptions, include_completed))
        doc.setPageSize(QSizeF(body.width(), body.height()))
        for i in range(doc.pageCount()):
            def text_page(p: QPainter, i=i) -> None:
                p.save()
                p.translate(body.left(), body.top() - i * body.height())
                doc.drawContents(p, QRectF(0, i * body.height(), body.width(), body.height()))
                p.restore()
            pages.append(text_page)

    p = QPainter(writer)
    try:
        _paint_pages(p, writer, pages, page, footer_h, theme, data.name, generated)
    finally:
        p.end()
    return dest


def _paint_pages(p: QPainter, writer: QPdfWriter, pages, page: QRectF, footer_h: float, theme: Theme, name: str,
                 generated: str) -> None:
    total = len(pages)
    for n, paint in enumerate(pages, start=1):
        if n > 1:
            writer.newPage()
        p.setRenderHint(QPainter.Antialiasing)
        # header & footer
        p.setFont(_font(8))
        p.setPen(QColor(theme.muted))
        p.drawText(QRectF(0, 0, page.width(), 18), int(Qt.AlignLeft | Qt.AlignVCenter), f"{APP_NAME} · {name}")
        p.drawText(QRectF(0, 0, page.width(), 18), int(Qt.AlignRight | Qt.AlignVCenter), generated)
        p.setPen(QPen(QColor(theme.grid), 1))
        p.drawLine(QPointF(0, 24), QPointF(page.width(), 24))
        p.drawLine(QPointF(0, page.height() - footer_h + 4), QPointF(page.width(), page.height() - footer_h + 4))
        p.setPen(QColor(theme.muted))
        p.drawText(QRectF(0, page.height() - footer_h + 6, page.width(), footer_h - 6),
                   int(Qt.AlignRight | Qt.AlignVCenter), f"Page {n} of {total}")
        paint(p)


# ---------------------------------------------------------------------------
# Rendering widgets
# ---------------------------------------------------------------------------
def _flush() -> None:
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        QApplication.processEvents()


def render_to_file(paint, size: QSize, path: Path | str, fmt: Optional[str] = None, title: str = "",
                   background: str = "#0F172A") -> Path:
    from kanbanmanager.ui import theme as app_theme

    background = app_theme.color(background)  # pictures follow the app's theme
    """Call ``paint(painter)`` onto a ``size`` canvas and save it as PDF / SVG / PNG."""
    dest = Path(path)
    fmt = (fmt or dest.suffix.lstrip(".")).lower()
    w, h = max(1, size.width()), max(1, size.height())
    if fmt == "pdf":
        writer = QPdfWriter(str(dest))
        writer.setResolution(96)
        writer.setPageSize(QPageSize(QSizeF(w * 25.4 / 96 + 20, h * 25.4 / 96 + 20), QPageSize.Millimeter))
        writer.setPageMargins(QMarginsF(10, 10, 10, 10), QPageLayout.Millimeter)
        writer.setTitle(title)
        writer.setCreator(APP_NAME)
        p = QPainter(writer)
        try:
            area = QRectF(writer.pageLayout().paintRectPixels(writer.resolution()))
            scale = min(area.width() / w, area.height() / h)
            p.scale(scale, scale)
            p.fillRect(QRectF(0, 0, w, h), QColor(background))
            paint(p)
        finally:
            p.end()
    elif fmt == "svg":
        gen = QSvgGenerator()
        gen.setFileName(str(dest))
        gen.setSize(QSize(w, h))
        gen.setViewBox(QRectF(0, 0, w, h))
        gen.setTitle(title)
        gen.setDescription(f"Created by {APP_NAME}")
        p = QPainter(gen)
        try:
            p.fillRect(QRectF(0, 0, w, h), QColor(background))
            paint(p)
        finally:
            p.end()
    elif fmt == "png":
        img = QImage(QSize(w, h) * 2, QImage.Format_ARGB32)
        img.fill(QColor(background))
        p = QPainter(img)
        try:
            p.scale(2, 2)
            paint(p)
        finally:
            p.end()
        if not img.save(str(dest), "PNG"):
            raise OSError(f"Could not write {dest}")
    else:
        raise ValueError(f"Unknown format: {fmt}")
    return dest


def export_widget(widget: QWidget, path: Path | str, fmt: Optional[str] = None, title: str = "") -> Path:
    """Render a widget as it looks now."""
    _flush()
    size = widget.size()
    return render_to_file(lambda p: widget.render(p, QPoint(0, 0)), size, path, fmt, title)


def export_board_snapshot(service, board_id: int, path: Path | str, fmt: Optional[str] = None,
                          filters: Optional[Dict[str, Any]] = None, parent_style: Optional[str] = None) -> Path:
    """The whole board - every column at its full height - as a picture."""
    from kanbanmanager.ui.styles import get_stylesheet
    from kanbanmanager.ui.views.board_view import BoardView

    view = BoardView(service)
    view.setStyleSheet(parent_style or get_stylesheet())
    view.setAttribute(Qt.WA_DontShowOnScreen, True)
    view.current_filters = dict(filters or {})
    view.resize(1600, 900)
    view.show()
    view.load_board(board_id)
    _flush()
    from PySide6.QtWidgets import QPushButton

    for btn in view.columns_container.findChildren(QPushButton):
        if "Add Column" in btn.text():
            btn.hide()  # not part of the board's content
    columns = list(view.column_widgets.values())
    content_h = max((col.scroll_area.y() + col.drop_area.sizeHint().height() + 12 for col in columns), default=300)
    width = sum(c.width() for c in columns) + 16 * max(0, len(columns) - 1) + 32
    view.resize(max(600, width), max(400, content_h + 40))
    _flush()
    for _ in range(6):  # grow until no column needs to scroll
        extra = max((col.scroll_area.verticalScrollBar().maximum() for col in columns), default=0)
        if extra <= 0:
            break
        view.resize(view.width(), view.height() + extra + 8)
        _flush()
    container = view.columns_container
    board = service.get_board(board_id)
    dest = render_to_file(lambda p: container.render(p, QPoint(0, 0)), container.size(), path, fmt,
                          title=board.name if board else "Board")
    view.close()
    view.deleteLater()
    return dest


def export_gantt(gantt_view, path: Path | str, fmt: Optional[str] = None) -> Path:
    """The Gantt chart (header + all rows, full width) with task names on the left."""
    from kanbanmanager.ui.views.gantt_view import HEADER_H, ROW_H

    _flush()
    model = gantt_view.model
    header, canvas = gantt_view.header, gantt_view.canvas
    names_w = 300
    w = names_w + canvas.width()
    h = HEADER_H + canvas.height()

    def paint(p: QPainter) -> None:
        p.fillRect(QRectF(0, 0, names_w, h), QColor("#111827"))
        p.setPen(QColor("#CBD5E1"))
        p.setFont(_font(9, True))
        p.drawText(QRectF(10, 0, names_w - 10, HEADER_H), int(Qt.AlignLeft | Qt.AlignVCenter), "Task")
        p.setFont(_font(9))
        for i, row in enumerate(model.rows):
            y = HEADER_H + i * ROW_H
            if i % 2:
                p.fillRect(QRectF(0, y, names_w, ROW_H), QColor(255, 255, 255, 6))
            p.setPen(QColor("#EF4444" if row.critical and model.show_critical else "#F8FAFC"))
            label = ("◆ " if row.task.is_milestone else "") + row.task.title
            dates = f"{row.start:%b %d} – {row.finish:%b %d}" if row.start and row.finish else ""
            p.drawText(QRectF(10, y, names_w - 110, ROW_H), int(Qt.AlignLeft | Qt.AlignVCenter),
                       p.fontMetrics().elidedText(label, Qt.ElideRight, names_w - 120))
            p.setPen(QColor("#94A3B8"))
            p.drawText(QRectF(names_w - 105, y, 100, ROW_H), int(Qt.AlignRight | Qt.AlignVCenter), dates)
        p.setPen(QPen(QColor("#334155"), 1))
        p.drawLine(QPointF(names_w, 0), QPointF(names_w, h))
        p.save()
        p.translate(names_w, 0)
        saved = header.offset
        header.offset = 0
        header.render(p, QPoint(0, 0))
        header.offset = saved
        p.translate(0, HEADER_H)
        canvas.render(p, QPoint(0, 0))
        p.restore()

    return render_to_file(paint, QSize(int(w), int(h)), path, fmt, title="Gantt chart")


THEMES = {"dark": DARK, "light": LIGHT}
__all__ = ["build_charts", "write_board_report", "export_board_snapshot", "export_gantt", "export_widget",
           "render_to_file", "THEMES"]

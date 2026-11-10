"""
Vector charts: painted with QPainter, so the same code draws on screen and
into SVG and PDF files (true vectors: crisp at any zoom, editable in Inkscape
or Illustrator) as well as PNG.

A ``Chart`` is data plus a kind (``line``, ``area`` (stacked), ``bars``,
``hbars``, ``donut``, ``histogram``); ``paint_chart(painter, rect, chart,
theme)`` renders it. ``ChartWidget`` shows one in the UI with an export menu;
``export_chart`` writes one to a file.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from PySide6.QtCore import QMarginsF, QPointF, QRectF, QSize, QSizeF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPainterPath,
    QPdfWriter,
    QPen,
    QPolygonF,
)
from PySide6.QtSvg import QSvgGenerator
from PySide6.QtWidgets import QFileDialog, QFrame, QMenu, QToolButton, QVBoxLayout, QWidget


@dataclass(frozen=True)
class Theme:
    background: str
    text: str
    muted: str
    grid: str
    axis: str
    card: str


DARK = Theme(background="#1E293B", text="#F8FAFC", muted="#94A3B8", grid="#334155", axis="#475569", card="#1E293B")
LIGHT = Theme(background="#FFFFFF", text="#0F172A", muted="#475569", grid="#E2E8F0", axis="#94A3B8", card="#FFFFFF")


def screen_theme() -> Theme:
    """The on-screen chart colours in the app's current theme (exports keep LIGHT/DARK fixed)."""
    from kanbanmanager.ui import theme as app_theme

    return Theme(background=app_theme.color("#1E293B"), text=app_theme.color("#F8FAFC"),
                 muted=app_theme.color("#94A3B8"), grid=app_theme.color("#334155"),
                 axis=app_theme.color("#475569"), card=app_theme.color("#1E293B"))


@dataclass
class Chart:
    kind: str                                     # line | area | bars | hbars | donut | histogram
    title: str
    labels: List[Any] = field(default_factory=list)                 # x values (dates or text)
    series: List[Tuple[str, str, List[float]]] = field(default_factory=list)   # (name, color, values)
    slices: List[Tuple[str, float, str]] = field(default_factory=list)          # bars / donut
    subtitle: str = ""
    y_label: str = ""
    value_format: str = "{:g}"
    markers: List[Tuple[str, float, str]] = field(default_factory=list)         # histogram: (name, x, color)
    empty_text: str = "No data for this period yet"

    @property
    def is_empty(self) -> bool:
        if self.kind in ("bars", "hbars", "donut") and (self.slices or self.kind != "bars"):
            return not any(v for _l, v, _c in self.slices)
        if self.kind == "histogram":
            return not self.series or not self.series[0][2]
        return not self.labels or not any(any(v for v in s[2]) for s in self.series)


# ---------------------------------------------------------------------------
# Painting helpers
# ---------------------------------------------------------------------------
def _font(size: float, bold: bool = False) -> QFont:
    f = QFont()
    f.setPointSizeF(size)
    f.setBold(bold)
    return f


def _nice_max(value: float) -> Tuple[float, float]:
    """A round axis maximum and tick step (about four ticks) for ``value``."""
    if value <= 0:
        return 1.0, 1.0
    raw = value / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = mag * 10
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            step = m * mag
            break
    if value <= 4 and step < 1:  # small counts: whole-number ticks
        step = 1.0
    top = math.ceil(round(value / step, 9)) * step
    return float(top), float(step)


def _fmt_x(value: Any, span: int) -> str:
    if isinstance(value, date):
        return value.strftime("%b %d")
    return str(value)


def _text(p: QPainter, rect: QRectF, text: str, color: str, size: float, bold: bool = False,
          align=Qt.AlignLeft | Qt.AlignVCenter) -> None:
    p.setFont(_font(size, bold))
    p.setPen(QColor(color))
    p.drawText(rect, int(align), text)


def _elide(text: str, size: float, width: float) -> str:
    return QFontMetricsF(_font(size)).elidedText(text, Qt.ElideRight, width)


def _header(p: QPainter, rect: QRectF, chart: Chart, theme: Theme) -> QRectF:
    _text(p, QRectF(rect.left(), rect.top(), rect.width(), 22), chart.title, theme.text, 11, True)
    top = rect.top() + 22
    if chart.subtitle:
        _text(p, QRectF(rect.left(), top, rect.width(), 16), _elide(chart.subtitle, 8.5, rect.width()), theme.muted,
              8.5)
        top += 18
    return QRectF(rect.left(), top + 4, rect.width(), rect.bottom() - top - 4)


def _legend(p: QPainter, rect: QRectF, items: Sequence[Tuple[str, str]], theme: Theme) -> float:
    """Draw a wrapping legend at the bottom of ``rect``; returns its height."""
    fm = QFontMetricsF(_font(8.5))
    rows: List[List[Tuple[str, str, float]]] = [[]]
    x = 0.0
    for name, color in items:
        w = 14 + fm.horizontalAdvance(name) + 14
        if x + w > rect.width() and rows[-1]:
            rows.append([])
            x = 0.0
        rows[-1].append((name, color, w))
        x += w
    height = len(rows) * 16
    y = rect.bottom() - height
    for row in rows:
        x = rect.left()
        for name, color, w in row:
            p.fillRect(QRectF(x, y + 4, 9, 9), QColor(color))
            _text(p, QRectF(x + 13, y, w - 13, 16), name, theme.muted, 8.5)
            x += w
        y += 16
    return height


def _axes(p: QPainter, plot: QRectF, top: float, step: float, theme: Theme, fmt: str) -> None:
    p.setFont(_font(8))
    n = int(round(top / step)) if step else 1
    for i in range(n + 1):
        v = i * step
        y = plot.bottom() - plot.height() * (v / top if top else 0)
        p.setPen(QPen(QColor(theme.grid), 0.6))
        p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        _text(p, QRectF(plot.left() - 44, y - 8, 40, 16), fmt.format(v), theme.muted, 8,
              align=Qt.AlignRight | Qt.AlignVCenter)
    p.setPen(QPen(QColor(theme.axis), 1))
    p.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))


def _x_labels(p: QPainter, plot: QRectF, labels: Sequence[Any], theme: Theme, centered: bool = False) -> None:
    n = len(labels)
    if not n:
        return
    fm = QFontMetricsF(_font(8))
    widest = max(fm.horizontalAdvance(_fmt_x(l, n)) for l in labels) + 10
    every = max(1, math.ceil(widest / (plot.width() / max(1, n))))
    for i, label in enumerate(labels):
        last = i == n - 1
        if i % every and not (last and (n - 1) % every >= every * 0.75):
            continue  # the last label only when it doesn't crowd the previous one
        if centered:
            x = plot.left() + plot.width() * (i + 0.5) / n
        else:
            x = plot.left() + (plot.width() * i / (n - 1) if n > 1 else plot.width() / 2)
        _text(p, QRectF(x - widest / 2, plot.bottom() + 3, widest, 14), _fmt_x(label, n), theme.muted, 8,
              align=Qt.AlignHCenter | Qt.AlignTop)


# ---------------------------------------------------------------------------
# Chart kinds
# ---------------------------------------------------------------------------
def _paint_lines(p: QPainter, body: QRectF, chart: Chart, theme: Theme, stacked: bool) -> None:
    legend_h = _legend(p, body, [(n, c) for n, c, _v in chart.series], theme) if len(chart.series) > 1 else 0
    plot = QRectF(body.left() + 48, body.top() + 6, body.width() - 56, body.height() - legend_h - 30)
    n = len(chart.labels)
    if stacked:
        totals = [sum(s[2][i] for s in chart.series) for i in range(n)]
        top_val = max(totals) if totals else 0
    else:
        top_val = max((max(s[2]) for s in chart.series if s[2]), default=0)
    top, step = _nice_max(top_val)
    _axes(p, plot, top, step, theme, chart.value_format)
    _x_labels(p, plot, chart.labels, theme)

    def pt(i: int, v: float) -> QPointF:
        x = plot.left() + (plot.width() * i / (n - 1) if n > 1 else plot.width() / 2)
        return QPointF(x, plot.bottom() - plot.height() * (v / top if top else 0))

    if stacked:
        base = [0.0] * n
        for name, color, values in chart.series:
            upper = [base[i] + values[i] for i in range(n)]
            poly = QPolygonF([pt(i, upper[i]) for i in range(n)] + [pt(i, base[i]) for i in range(n - 1, -1, -1)])
            c = QColor(color)
            c.setAlpha(215)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(c))
            p.drawPolygon(poly)
            p.setPen(QPen(QColor(color).darker(115), 1))
            p.setBrush(Qt.NoBrush)
            p.drawPolyline(QPolygonF([pt(i, upper[i]) for i in range(n)]))
            base = upper
    else:
        for name, color, values in chart.series:
            path = QPainterPath()
            for i, v in enumerate(values):
                (path.moveTo if i == 0 else path.lineTo)(pt(i, v))
            p.setPen(QPen(QColor(color), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            last = pt(n - 1, values[-1])
            p.setBrush(QColor(color))
            p.drawEllipse(last, 3, 3)
            _text(p, QRectF(last.x() - 60, last.y() - 18, 58, 14), chart.value_format.format(values[-1]), color,
                  8, True, align=Qt.AlignRight | Qt.AlignVCenter)


def _paint_bars(p: QPainter, body: QRectF, chart: Chart, theme: Theme) -> None:
    """Vertical bars (``chart.slices`` or a single series over ``chart.labels``)."""
    if chart.slices:
        labels = [s[0] for s in chart.slices]
        values = [s[1] for s in chart.slices]
        colors = [s[2] for s in chart.slices]
    else:
        labels = list(chart.labels)
        values = list(chart.series[0][2]) if chart.series else []
        colors = [chart.series[0][1]] * len(values) if chart.series else []
    plot = QRectF(body.left() + 48, body.top() + 14, body.width() - 56, body.height() - 44)
    top, step = _nice_max(max(values, default=0))
    _axes(p, plot, top, step, theme, chart.value_format)
    n = len(values)
    slot = plot.width() / max(1, n)
    bar_w = min(48.0, slot * 0.68)
    for i, v in enumerate(values):
        h = plot.height() * (v / top if top else 0)
        x = plot.left() + slot * i + (slot - bar_w) / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(colors[i]))
        p.drawRoundedRect(QRectF(x, plot.bottom() - h, bar_w, h), 2.5, 2.5)
        if v:
            _text(p, QRectF(x - 10, plot.bottom() - h - 15, bar_w + 20, 14), chart.value_format.format(v),
                  theme.text, 8, True, align=Qt.AlignHCenter | Qt.AlignBottom)
    shown = [_elide(str(l) if not isinstance(l, date) else l.strftime("%b %d"), 8, slot - 2) for l in labels]
    _x_labels(p, plot, shown, theme, centered=True)


def _paint_hbars(p: QPainter, body: QRectF, chart: Chart, theme: Theme) -> None:
    items = chart.slices[:12]
    fm = QFontMetricsF(_font(8.5))
    label_w = min(body.width() * 0.38, max((fm.horizontalAdvance(str(l)) for l, _v, _c in items), default=40) + 8)
    plot = QRectF(body.left() + label_w, body.top() + 4, body.width() - label_w - 40, body.height() - 8)
    top = max((v for _l, v, _c in items), default=0) or 1
    row_h = min(26.0, plot.height() / max(1, len(items)))
    for i, (label, v, color) in enumerate(items):
        y = plot.top() + row_h * i
        _text(p, QRectF(body.left(), y, label_w - 8, row_h), _elide(str(label), 8.5, label_w - 8), theme.muted, 8.5,
              align=Qt.AlignRight | Qt.AlignVCenter)
        w = plot.width() * v / top
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.grid))
        p.drawRoundedRect(QRectF(plot.left(), y + row_h * 0.2, plot.width(), row_h * 0.6), 3, 3)
        p.setBrush(QColor(color))
        if w > 0:
            p.drawRoundedRect(QRectF(plot.left(), y + row_h * 0.2, max(w, 3), row_h * 0.6), 3, 3)
        _text(p, QRectF(plot.left() + w + 4, y, 36, row_h), chart.value_format.format(v), theme.text, 8.5, True)


def _paint_donut(p: QPainter, body: QRectF, chart: Chart, theme: Theme) -> None:
    items = [(l, v, c) for l, v, c in chart.slices if v > 0]
    total = sum(v for _l, v, _c in items)
    legend_w = min(body.width() * 0.45, 190)
    size = min(body.height() - 8, body.width() - legend_w - 16)
    ring = QRectF(body.left() + 4, body.top() + (body.height() - size) / 2, size, size)
    start = 90 * 16
    thickness = size * 0.22
    for label, v, color in items:
        span = -int(round(360 * 16 * v / total))
        pen = QPen(QColor(color), thickness, Qt.SolidLine, Qt.FlatCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        inner = ring.adjusted(thickness / 2, thickness / 2, -thickness / 2, -thickness / 2)
        p.drawArc(inner, start, span)
        start += span
    _text(p, ring, chart.value_format.format(total), theme.text, 16, True, align=Qt.AlignCenter)
    _text(p, ring.adjusted(0, size * 0.22, 0, 0), "total", theme.muted, 8, align=Qt.AlignCenter)
    x = ring.right() + 16
    y = body.top() + max(0.0, (body.height() - 18 * len(items)) / 2)
    for label, v, color in items[:10]:
        p.fillRect(QRectF(x, y + 4, 10, 10), QColor(color))
        pct = f"{v * 100 / total:.0f}%" if total else ""
        _text(p, QRectF(x + 15, y, body.right() - x - 15, 18),
              _elide(f"{label}  {chart.value_format.format(v)} ({pct})", 8.5, body.right() - x - 15), theme.muted, 8.5)
        y += 18


def _paint_histogram(p: QPainter, body: QRectF, chart: Chart, theme: Theme) -> None:
    values = chart.series[0][2] if chart.series else []
    color = chart.series[0][1] if chart.series else "#3B82F6"
    hi = max(values, default=1) or 1
    bins = min(12, max(4, int(math.sqrt(len(values))) + 2))
    width = hi / bins
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int(v / width))] += 1
    plot = QRectF(body.left() + 48, body.top() + 14, body.width() - 56, body.height() - 48)
    top, step = _nice_max(max(counts, default=0))
    _axes(p, plot, top, step, theme, "{:g}")
    slot = plot.width() / bins
    for i, c in enumerate(counts):
        h = plot.height() * (c / top if top else 0)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawRect(QRectF(plot.left() + slot * i + 1, plot.bottom() - h, slot - 2, h))
    labels = [f"{width * i:.3g}" for i in range(bins)] + [f"{hi:.3g}"]
    for i, text in enumerate(labels):
        x = plot.left() + slot * i
        _text(p, QRectF(x - 20, plot.bottom() + 3, 40, 14), text, theme.muted, 8, align=Qt.AlignHCenter | Qt.AlignTop)
    _text(p, QRectF(plot.left(), plot.bottom() + 17, plot.width(), 14), chart.y_label or "days", theme.muted, 8,
          align=Qt.AlignHCenter | Qt.AlignTop)
    for n, (name, x_val, mcolor) in enumerate(chart.markers):
        x = plot.left() + plot.width() * min(1.0, x_val / hi)
        p.setPen(QPen(QColor(mcolor), 1.5, Qt.DashLine))
        p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        label = f"{name} {x_val:.1f}"
        w = QFontMetricsF(_font(8, True)).horizontalAdvance(label) + 6
        y = plot.top() + n * 15  # one line per marker, so they never overlap
        box = QRectF(x + 3, y, w, 14) if x + 3 + w <= plot.right() else QRectF(x - 3 - w, y, w, 14)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.card))
        p.drawRect(box)
        _text(p, box, label, mcolor, 8, True, align=Qt.AlignCenter)


def paint_chart(p: QPainter, rect: QRectF, chart: Chart, theme: Theme = DARK, background: bool = True) -> None:
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    if background:
        p.setPen(QPen(QColor(theme.grid), 1))
        p.setBrush(QColor(theme.card))
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
    inner = rect.adjusted(14, 10, -14, -10)
    body = _header(p, inner, chart, theme)
    if chart.is_empty:
        _text(p, body, chart.empty_text, theme.muted, 10, align=Qt.AlignCenter)
    elif chart.kind in ("line", "area"):
        _paint_lines(p, body, chart, theme, stacked=chart.kind == "area")
    elif chart.kind == "bars":
        _paint_bars(p, body, chart, theme)
    elif chart.kind == "hbars":
        _paint_hbars(p, body, chart, theme)
    elif chart.kind == "donut":
        _paint_donut(p, body, chart, theme)
    elif chart.kind == "histogram":
        _paint_histogram(p, body, chart, theme)
    p.restore()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
EXPORT_FORMATS = {"svg": "SVG vector image (*.svg)", "pdf": "PDF document (*.pdf)", "png": "PNG image (*.png)"}


def export_chart(chart: Chart, path: Path | str, fmt: Optional[str] = None, size: QSize = QSize(960, 540),
                 theme: Theme = LIGHT) -> Path:
    """Write a chart as SVG / PDF (vector) or PNG (2x resolution). Requires a Qt GUI application."""
    dest = Path(path)
    fmt = (fmt or dest.suffix.lstrip(".")).lower()
    rect = QRectF(0, 0, size.width(), size.height())
    if fmt == "svg":
        gen = QSvgGenerator()
        gen.setFileName(str(dest))
        gen.setSize(size)
        gen.setViewBox(rect)
        gen.setTitle(chart.title)
        gen.setDescription("Created by KanbanManager")
        p = QPainter(gen)
        try:
            paint_chart(p, rect, chart, theme)
        finally:
            p.end()
    elif fmt == "pdf":
        writer = QPdfWriter(str(dest))
        writer.setTitle(chart.title)
        writer.setCreator("KanbanManager")
        writer.setResolution(96)
        writer.setPageSize(QPageSize(QSizeF(size.width() * 25.4 / 96, size.height() * 25.4 / 96),
                                     QPageSize.Millimeter))
        writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Millimeter)
        p = QPainter(writer)
        try:
            page = QRectF(writer.pageLayout().paintRectPixels(writer.resolution()))
            p.scale(page.width() / rect.width(), page.height() / rect.height())
            paint_chart(p, rect, chart, theme)
        finally:
            p.end()
    elif fmt == "png":
        img = QImage(size * 2, QImage.Format_ARGB32)
        img.fill(QColor(theme.background))
        p = QPainter(img)
        try:
            p.scale(2, 2)
            paint_chart(p, rect, chart, theme)
        finally:
            p.end()
        if not img.save(str(dest), "PNG"):
            raise OSError(f"Could not write {dest}")
    else:
        raise ValueError(f"Unknown chart format: {fmt}")
    return dest


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------
class ChartCanvas(QWidget):
    def __init__(self, chart: Chart, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.chart = chart
        self.setMinimumHeight(230)

    def set_chart(self, chart: Chart) -> None:
        self.chart = chart
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        paint_chart(p, QRectF(self.rect()), self.chart, screen_theme())
        p.end()


class ChartWidget(QFrame):
    """A chart with a small export menu (SVG / PDF / PNG / copy)."""

    exported = Signal(str)

    def __init__(self, chart: Chart, parent: Optional[QWidget] = None, height: int = 260):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.canvas = ChartCanvas(chart)
        self.canvas.setMinimumHeight(height)
        lay.addWidget(self.canvas)
        self.menu_btn = QToolButton(self.canvas)
        self.menu_btn.setText("⬇")
        self.menu_btn.setToolTip("Export this chart")
        self.menu_btn.setAutoRaise(True)
        self.menu_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.menu_btn)
        menu.addAction("Save as SVG (vector)…").triggered.connect(self._save_svg)
        menu.addAction("Save as PDF (vector)…").triggered.connect(self._save_pdf)
        menu.addAction("Save as PNG…").triggered.connect(self._save_png)
        menu.addSeparator()
        menu.addAction("Copy Image").triggered.connect(self.copy_image)
        self.menu_btn.setMenu(menu)

    @property
    def chart(self) -> Chart:
        return self.canvas.chart

    def set_chart(self, chart: Chart) -> None:
        self.canvas.set_chart(chart)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.menu_btn.move(self.canvas.width() - self.menu_btn.sizeHint().width() - 10, 8)

    def _save(self, fmt: str) -> Optional[Path]:
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in self.chart.title).strip() or "chart"
        path, _ = QFileDialog.getSaveFileName(self, "Export Chart", f"{safe}.{fmt}", EXPORT_FORMATS[fmt])
        if not path:
            return None
        dest = export_chart(self.chart, path, fmt)
        self.exported.emit(str(dest))
        return dest

    def _save_svg(self, _checked: bool = False) -> None:
        self._save("svg")

    def _save_pdf(self, _checked: bool = False) -> None:
        self._save("pdf")

    def _save_png(self, _checked: bool = False) -> None:
        self._save("png")

    def copy_image(self, _checked: bool = False) -> None:
        img = QImage(QSize(960, 540) * 2, QImage.Format_ARGB32)
        img.fill(QColor(LIGHT.background))
        p = QPainter(img)
        p.scale(2, 2)
        paint_chart(p, QRectF(0, 0, 960, 540), self.chart, LIGHT)
        p.end()
        QGuiApplication.clipboard().setImage(img)


__all__ = ["Chart", "ChartWidget", "DARK", "LIGHT", "Theme", "export_chart", "paint_chart"]

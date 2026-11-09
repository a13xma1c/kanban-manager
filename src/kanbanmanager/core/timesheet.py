"""
Timesheets: collect finished time entries for a period and export them.

* CSV (detailed): one row per time entry - for payroll/billing imports.
* CSV (summary): one row per person + task, one column per day, with totals.
* HTML / PDF: a printable report grouped by person, with daily totals.

Entries are stored in naive UTC and reported in the user's local time; an
entry belongs to the local day on which it started.
"""
from __future__ import annotations

import csv
import html
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kanbanmanager.enums import TimeEntrySource
from kanbanmanager.timefmt import format_duration

GROUP_ENTRIES = "entries"
GROUP_DAY = "day"
GROUP_TASK = "task"
GROUP_PERSON = "person"

PRESET_THIS_WEEK = "This week"
PRESET_LAST_WEEK = "Last week"
PRESET_THIS_MONTH = "This month"
PRESET_LAST_MONTH = "Last month"
PRESET_LAST_30 = "Last 30 days"


@dataclass(frozen=True)
class TimesheetRow:
    entry_id: int
    day: date
    start: datetime          # local, naive
    end: datetime            # local, naive
    seconds: int
    person: str
    workspace: str
    board: str
    task_id: int
    task: str
    labels: str
    source: str
    note: str

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------
def preset_range(name: str, today: Optional[date] = None) -> Tuple[date, date]:
    """Inclusive local date range for a named preset."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    if name == PRESET_THIS_WEEK:
        return monday, monday + timedelta(days=6)
    if name == PRESET_LAST_WEEK:
        return monday - timedelta(days=7), monday - timedelta(days=1)
    first = today.replace(day=1)
    if name == PRESET_THIS_MONTH:
        nxt = (first + timedelta(days=32)).replace(day=1)
        return first, nxt - timedelta(days=1)
    if name == PRESET_LAST_MONTH:
        last_end = first - timedelta(days=1)
        return last_end.replace(day=1), last_end
    if name == PRESET_LAST_30:
        return today - timedelta(days=29), today
    raise ValueError(f"Unknown preset: {name}")


PRESETS = [PRESET_THIS_WEEK, PRESET_LAST_WEEK, PRESET_THIS_MONTH, PRESET_LAST_MONTH, PRESET_LAST_30]


def local_range_to_utc(start: date, end: date) -> Tuple[datetime, datetime]:
    """[start 00:00, end+1 00:00) in local time, as naive UTC bounds for querying."""
    def to_utc(d: date) -> datetime:
        local = datetime.combine(d, time.min).astimezone()  # attach the local zone (DST-aware)
        return local.astimezone(timezone.utc).replace(tzinfo=None)

    return to_utc(start), to_utc(end + timedelta(days=1))


def _utc_to_local(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Rows & grouping
# ---------------------------------------------------------------------------
def build_rows(entries: List[Dict[str, Any]]) -> List[TimesheetRow]:
    rows = []
    for e in entries:
        if not e.get("ended_at"):
            continue  # running timers are not on timesheets until stopped
        start = _utc_to_local(e["started_at"])
        try:
            source = TimeEntrySource(e.get("source")).display_name
        except ValueError:
            source = e.get("source") or ""
        rows.append(
            TimesheetRow(
                entry_id=e["id"],
                day=start.date(),
                start=start,
                end=_utc_to_local(e["ended_at"]),
                seconds=int(e.get("duration_seconds") or 0),
                person=e.get("user") or "Unknown",
                workspace=e.get("workspace_name", ""),
                board=e.get("board_name", ""),
                task_id=e["task_id"],
                task=e.get("task_title", ""),
                labels=", ".join(e.get("labels") or []),
                source=source,
                note=e.get("note") or "",
            )
        )
    rows.sort(key=lambda r: (r.start, r.entry_id))
    return rows


def total_seconds(rows: List[TimesheetRow]) -> int:
    return sum(r.seconds for r in rows)


def group_rows(rows: List[TimesheetRow], by: str) -> List[Tuple[Tuple[str, ...], int]]:
    """Aggregate seconds by a key. Returns [(key columns, seconds)] sorted by key."""
    if by == GROUP_ENTRIES:
        raise ValueError("GROUP_ENTRIES is not an aggregation")
    buckets: Dict[Tuple[str, ...], int] = defaultdict(int)
    for r in rows:
        if by == GROUP_DAY:
            key = (r.day.isoformat(), r.day.strftime("%A"))
        elif by == GROUP_TASK:
            key = (r.board, f"#{r.task_id} {r.task}")
        elif by == GROUP_PERSON:
            key = (r.person,)
        else:
            raise ValueError(f"Unknown grouping: {by}")
        buckets[key] += r.seconds
    return sorted(buckets.items(), key=lambda kv: [k.lower() for k in kv[0]])


GROUP_HEADERS = {
    GROUP_DAY: ["Date", "Weekday"],
    GROUP_TASK: ["Board", "Task"],
    GROUP_PERSON: ["Person"],
}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _safe_cell(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in free-text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _hours(seconds: int) -> str:
    return f"{seconds / 3600:.2f}"


def write_entries_csv(rows: List[TimesheetRow], path: Path | str) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel opens accents/emoji correctly
    with open(dest, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Date", "Start", "End", "Duration", "Hours", "Person", "Workspace", "Board",
                    "Task ID", "Task", "Labels", "Source", "Note"])
        for r in rows:
            w.writerow([
                r.day.isoformat(), r.start.strftime("%H:%M"), r.end.strftime("%H:%M"),
                format_duration(r.seconds), _hours(r.seconds), _safe_cell(r.person), _safe_cell(r.workspace),
                _safe_cell(r.board), r.task_id, _safe_cell(r.task), _safe_cell(r.labels), r.source,
                _safe_cell(r.note),
            ])
        w.writerow([])
        w.writerow(["Total", "", "", format_duration(total_seconds(rows)), _hours(total_seconds(rows))])
    return dest


def write_summary_csv(rows: List[TimesheetRow], start: date, end: date, path: Path | str) -> Path:
    """Person x task grid with one column per day in [start, end] (hours, 2 decimals)."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    grid: Dict[Tuple[str, str, str], Dict[date, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        grid[(r.person, r.board, f"#{r.task_id} {r.task}")][r.day] += r.seconds
    with open(dest, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Person", "Board", "Task"] + [d.strftime("%a %Y-%m-%d") for d in days] + ["Total"])
        for key in sorted(grid, key=lambda k: [p.lower() for p in k]):
            per_day = grid[key]
            w.writerow([_safe_cell(k) for k in key] + [_hours(per_day.get(d, 0)) if per_day.get(d) else ""
                                                       for d in days] + [_hours(sum(per_day.values()))])
        day_totals = [sum(r.seconds for r in rows if r.day == d) for d in days]
        w.writerow(["Total", "", ""] + [_hours(t) if t else "" for t in day_totals] + [_hours(total_seconds(rows))])
    return dest


def build_report_html(
    rows: List[TimesheetRow],
    start: date,
    end: date,
    scope_label: str,
    person_label: str = "Everyone",
) -> str:
    esc = html.escape
    by_person: Dict[str, List[TimesheetRow]] = defaultdict(list)
    for r in rows:
        by_person[r.person].append(r)
    parts = [
        "<html><head><meta charset='utf-8'><style>"
        "body{font-family:sans-serif;font-size:10pt;color:#111}"
        "h1{font-size:16pt;margin:0} .meta{color:#555;margin:4px 0 14px}"
        "h2{font-size:12pt;margin:16px 0 4px;border-bottom:1px solid #999}"
        "table{border-collapse:collapse;width:100%} th,td{border:1px solid #bbb;padding:3px 5px;text-align:left}"
        "th{background:#eee} td.num{text-align:right} tr.total td{font-weight:bold;background:#f4f4f4}"
        ".nw{white-space:nowrap}"
        "</style></head><body>",
        "<h1>Timesheet</h1>",
        f"<div class='meta'>{esc(start.strftime('%b %d, %Y'))} - {esc(end.strftime('%b %d, %Y'))}"
        f" &nbsp;|&nbsp; {esc(scope_label)} &nbsp;|&nbsp; {esc(person_label)}"
        f" &nbsp;|&nbsp; Total <b>{esc(format_duration(total_seconds(rows)))}</b>"
        f" ({_hours(total_seconds(rows))} h)</div>",
    ]
    if not rows:
        parts.append("<p>No time was logged in this period.</p>")
    for person in sorted(by_person, key=str.lower):
        prow = by_person[person]
        parts.append(f"<h2>{esc(person)} - {esc(format_duration(total_seconds(prow)))}</h2>")
        parts.append("<table><tr><th>Date</th><th>Time</th><th>Board</th><th>Task</th><th>Note</th>"
                     "<th>Source</th><th>Duration</th></tr>")
        for day in sorted({r.day for r in prow}):
            day_rows = [r for r in prow if r.day == day]
            for r in day_rows:
                parts.append(
                    f"<tr><td class='nw'>{esc(r.day.strftime('%a %b %d'))}</td>"
                    f"<td class='nw'>{r.start:%H:%M}-{r.end:%H:%M}</td><td>{esc(r.board)}</td>"
                    f"<td>{esc(r.task)}</td><td>{esc(r.note)}</td><td class='nw'>{esc(r.source)}</td>"
                    f"<td class='num nw'>{esc(format_duration(r.seconds))}</td></tr>"
                )
            parts.append(
                f"<tr class='total'><td colspan='6'>{esc(day.strftime('%A %b %d'))} total</td>"
                f"<td class='num'>{esc(format_duration(total_seconds(day_rows)))}</td></tr>"
            )
        parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)


def write_html(html_text: str, path: Path | str) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html_text, encoding="utf-8")
    return dest


def write_pdf(html_text: str, path: Path | str) -> Path:
    """Render the HTML report to an A4 PDF (requires a Qt GUI application)."""
    from PySide6.QtCore import QMarginsF, QSizeF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    writer = QPdfWriter(str(dest))
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setPageMargins(QMarginsF(12, 12, 12, 12), QPageLayout.Millimeter)
    writer.setResolution(96)
    writer.setTitle("Timesheet")
    doc = QTextDocument()
    doc.setHtml(html_text)
    layout = writer.pageLayout()
    rect = layout.paintRectPixels(writer.resolution())
    doc.setPageSize(QSizeF(rect.width(), rect.height()))
    doc.print_(writer)
    return dest

"""
Import / export tools (pure Python, no Qt)

Export
* ``task_table`` -> headers + rows of every task (board, column, dates, labels,
  assignee, estimates, checklist, custom fields ...), written as
  ``write_csv`` (UTF-8 with BOM, Excel-safe) or ``write_xlsx`` (a real Excel
  workbook: typed numbers/dates, bold frozen header, filters, column widths -
  no third-party library).
* ``board_markdown`` -> a Markdown document of a board (columns, tasks with
  their metadata, checklists and descriptions) for wikis and READMEs.

Import
* ``read_table`` reads CSV (encoding and delimiter detected) or XLSX files.
* ``guess_mapping`` maps their columns to task fields by header (English names
  used by Jira, Asana, Trello, GitHub, Monday, Todoist ... and the names of the
  board's custom fields); ``PRESETS`` fine-tune it per tool.
* ``preview_rows`` / ``run_import`` parse every row (flexible dates, priorities
  such as "Highest"/"P1"/"!!!", yes/no, lists), create missing columns and
  labels, skip duplicates, and return an ``ImportResult`` that
  ``undo_import`` can roll back.
* ``import_trello`` imports a Trello board export (lists, cards, labels,
  checklists, members, comments, due dates, custom fields, archived cards).
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as xml_escape

from kanbanmanager.core import custom_fields as cf
from kanbanmanager.enums import ColumnType, Priority

DEFAULT_DUE_TIME = time(17, 0)


def safe_cell(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in free-text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


# ---------------------------------------------------------------------------
# Export: task table
# ---------------------------------------------------------------------------
BASE_COLUMNS = ["ID", "Board", "Column", "Title", "Description", "Status", "Priority", "Labels", "Assignee",
                "Start date", "Due date", "Completed at", "Estimate (h)", "Logged (h)", "Checklist", "Milestone",
                "Created", "Updated", "Archived"]


def _utc_to_local(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None, microsecond=0)


def task_table(service, board_ids: Optional[Iterable[int]] = None, include_completed: bool = True,
               include_archived: bool = False) -> Tuple[List[str], List[List[Any]]]:
    """Headers and typed rows (str, int, float, bool, date, datetime, None) of the boards' tasks."""
    boards = [b for ws in service.get_workspaces() for b in ws.boards if not b.is_archived]
    if board_ids is not None:
        wanted = set(board_ids)
        boards = [b for b in boards if b.id in wanted]
    field_names: List[str] = []
    board_fields: Dict[int, List[Dict[str, Any]]] = {}
    for b in boards:
        board_fields[b.id] = service.get_custom_fields(b.id)
        for f in board_fields[b.id]:
            if f["name"] not in field_names:
                field_names.append(f["name"])
    headers = BASE_COLUMNS + field_names
    rows: List[List[Any]] = []
    for b in boards:
        columns = {c.id: c.name for c in b.columns}
        fields = board_fields[b.id]
        for t in service.get_tasks_for_board(b.id, include_archived=include_archived):
            if t.is_completed and not include_completed:
                continue
            checklist = "\n".join(f"[{'x' if i.get('done') else ' '}] {i.get('text', '')}" for i in t.checklist)
            values = t.field_value_map
            row: List[Any] = [
                t.id, b.name, columns.get(t.column_id, ""), t.title, t.description or "",
                "Done" if t.is_completed else "Open", Priority(t.priority).display_name if t.priority in
                {p.value for p in Priority} else t.priority, ", ".join(l.name for l in t.labels), t.assignee or "",
                t.start_date, t.due_date, t.completed_at, float(t.estimated_hours or 0) or None,
                round(float(t.spent_hours or 0), 2) or None, checklist, bool(t.is_milestone),
                _utc_to_local(t.created_at), t.updated_at.replace(microsecond=0) if t.updated_at else None,
                bool(t.is_archived),
            ]
            by_name = {f["name"]: f for f in fields}
            for name in field_names:
                f = by_name.get(name)
                value = values.get(f["id"]) if f else None
                if f is None or cf.is_empty(f, value):
                    row.append(None)
                elif f["field_type"] in ("number", "currency", "percent", "rating"):
                    row.append(value)
                elif f["field_type"] == "date":
                    row.append(date.fromisoformat(value))
                elif f["field_type"] == "checkbox":
                    row.append(True)
                else:
                    row.append(cf.format_value(f, value))
            rows.append(row)
    return headers, rows


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:g}"
    return safe_cell(value)


def write_csv(headers: Sequence[str], rows: Iterable[Sequence[Any]], path: Path | str, delimiter: str = ",") -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", newline="", encoding="utf-8-sig") as fh:  # BOM: Excel opens accents/emoji correctly
        w = csv.writer(fh, delimiter=delimiter)
        w.writerow(headers)
        for row in rows:
            w.writerow([_csv_value(v) for v in row])
    return dest


# -- XLSX -------------------------------------------------------------------
def _col_letter(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


_EPOCH = datetime(1899, 12, 30)


def _excel_serial(value: Any) -> float:
    if isinstance(value, datetime):
        return (value - _EPOCH).total_seconds() / 86400.0
    return float((value - _EPOCH.date()).days)


def _xml_text(value: str) -> str:
    # strip characters XML 1.0 can't carry
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    return xml_escape(value)


def write_xlsx(headers: Sequence[str], rows: Sequence[Sequence[Any]], path: Path | str, sheet: str = "Tasks") -> Path:
    """A minimal but proper Office Open XML workbook (one sheet)."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet_name = re.sub(r"[\[\]:*?/\\]", " ", sheet)[:31] or "Tasks"
    # styles: 0 default, 1 bold header, 2 date, 3 date-time, 4 wrapped text
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/>'
              '<numFmt numFmtId="165" formatCode="yyyy-mm-dd hh:mm"/></numFmts>'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/>'
              '</fill><fill><patternFill patternType="solid"><fgColor rgb="FFE2E8F0"/></patternFill></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
              '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
              '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
              '<alignment wrapText="1" vertical="top"/></xf></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    widths = [max(8, min(60, len(str(h)) + 2)) for h in headers]
    out_rows = []

    def cell(ref: str, value: Any, header: bool = False) -> str:
        if value is None or value == "":
            return ""
        if header:
            return f'<c r="{ref}" s="1" t="inlineStr"><is><t>{_xml_text(str(value))}</t></is></c>'
        if isinstance(value, bool):
            return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value}</v></c>'
        if isinstance(value, datetime):
            return f'<c r="{ref}" s="3"><v>{_excel_serial(value):.10f}</v></c>'
        if isinstance(value, date):
            return f'<c r="{ref}" s="2"><v>{_excel_serial(value):.0f}</v></c>'
        text = str(value)
        style = ' s="4"' if "\n" in text else ""
        space = ' xml:space="preserve"' if text != text.strip() or "\n" in text else ""
        return f'<c r="{ref}"{style} t="inlineStr"><is><t{space}>{_xml_text(text)}</t></is></c>'

    out_rows.append('<row r="1">' + "".join(cell(f"{_col_letter(i)}1", h, True) for i, h in enumerate(headers))
                    + "</row>")
    for r, row in enumerate(rows, start=2):
        cells = []
        for i, value in enumerate(row):
            cells.append(cell(f"{_col_letter(i)}{r}", value))
            if isinstance(value, str):
                longest = max((len(line) for line in value.splitlines()), default=0)
                widths[i] = max(widths[i], min(60, longest + 2))
            elif isinstance(value, (date, datetime)):
                widths[i] = max(widths[i], 17 if isinstance(value, datetime) else 12)
        out_rows.append(f'<row r="{r}">' + "".join(cells) + "</row>")
    last_col, last_row = _col_letter(max(0, len(headers) - 1)), max(1, len(rows) + 1)
    last = f"{last_col}{last_row}"
    filter_ref = f"$A$1:${last_col}${last_row}"
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths))
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
                 'state="frozen"/></sheetView></sheetViews>'
                 f'<cols>{cols}</cols><sheetData>{"".join(out_rows)}</sheetData>'
                 f'<autoFilter ref="A1:{last}"/></worksheet>')
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets><sheet name="{_xml_text(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
                f'<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
                f"'{_xml_text(sheet_name)}'!{filter_ref}</definedName></definedNames>"
                '</workbook>')
    files = {
        "[Content_Types].xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                                '<Default Extension="xml" ContentType="application/xml"/>'
                                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                                '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                                '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                                '</Types>'),
        "_rels/.rels": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                        '</Relationships>'),
        "docProps/core.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                              '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                              'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                              'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                              f'<dc:title>{_xml_text(sheet_name)}</dc:title><dc:creator>KanbanManager</dc:creator>'
                              f'<dcterms:created xsi:type="dcterms:W3CDTF">{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}</dcterms:created>'
                              '</cp:coreProperties>'),
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                                       '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                                       '</Relationships>'),
        "xl/worksheets/sheet1.xml": sheet_xml,
        "xl/styles.xml": styles,
    }
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return dest


# ---------------------------------------------------------------------------
# Export: Markdown
# ---------------------------------------------------------------------------
def board_markdown(service, board_id: int, include_descriptions: bool = True, include_completed: bool = True) -> str:
    board = service.get_board(board_id)
    if board is None:
        raise ValueError("Board not found")
    fields = service.get_custom_fields(board_id)
    tasks = service.get_tasks_for_board(board_id)
    lines = [f"# {board.name}", ""]
    if board.description:
        lines += [board.description.strip(), ""]
    open_n = sum(1 for t in tasks if not t.is_completed)
    lines += [f"*{len(tasks)} tasks, {open_n} open · exported {datetime.now():%Y-%m-%d %H:%M}*", ""]
    for col in board.columns:
        col_tasks = [t for t in tasks if t.column_id == col.id and (include_completed or not t.is_completed)]
        limit = f" · WIP limit {col.wip_limit}" if col.wip_limit else ""
        lines += [f"## {col.name} ({len(col_tasks)}{limit})", ""]
        if not col_tasks:
            lines += ["*No tasks*", ""]
        for t in col_tasks:
            meta = []
            if t.priority in PRIORITY_NAMES:
                meta.append(PRIORITY_NAMES[t.priority])
            if t.due_date:
                meta.append(f"due {t.due_date:%b %d, %Y %H:%M}".replace(" 17:00", ""))
            if t.assignee:
                meta.append(f"@{t.assignee}")
            meta += [f"`{l.name}`" for l in t.labels]
            values = t.field_value_map
            for f in fields:
                if f["id"] in values and not cf.is_empty(f, values[f["id"]]):
                    meta.append(f"{f['name']}: {cf.format_value(f, values[f['id']])}")
            box = "x" if t.is_completed else " "
            title = t.title.replace("\n", " ")
            lines.append(f"- [{box}] **{title}**" + (f" — {' · '.join(meta)}" if meta else ""))
            for item in t.checklist:
                lines.append(f"    - [{'x' if item.get('done') else ' '}] {item.get('text', '')}")
            if include_descriptions and (t.description or "").strip():
                desc = re.sub(r"!\[([^\]]*)\]\(attachment:[^)]*\)", r"*(image: \1)*", t.description.strip())
                lines += [""] + ["    " + line if line.strip() else "" for line in desc.splitlines()] + [""]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


PRIORITY_NAMES = {p.value: p.display_name for p in Priority}


# ---------------------------------------------------------------------------
# Import: reading tables
# ---------------------------------------------------------------------------
@dataclass
class Table:
    headers: List[str]
    rows: List[List[str]]
    delimiter: str = ","
    encoding: str = "utf-8"
    source: str = ""

    def column_samples(self, index: int, n: int = 3) -> List[str]:
        out = []
        for row in self.rows:
            if index < len(row) and str(row[index]).strip():
                out.append(str(row[index]).strip())
                if len(out) >= n:
                    break
        return out


class ExchangeError(ValueError):
    """A file that can't be imported (message for the user)."""


def _decode(data: bytes) -> Tuple[str, str]:
    for enc in ("utf-8-sig", "utf-16"):
        if enc == "utf-16" and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            continue
        try:
            return data.decode(enc), "utf-8" if enc == "utf-8-sig" else enc
        except UnicodeDecodeError:
            continue
    return data.decode("cp1252", errors="replace"), "cp1252"


DELIMITERS = (",", ";", "\t", "|")


def detect_delimiter(text: str) -> str:
    """The delimiter that splits the first rows into the most consistent columns.

    Rows are parsed for real (quotes respected) with each candidate, so a tab or a
    semicolon *inside* a cell doesn't fool it - unlike csv.Sniffer, which counts
    characters. Ties go to the candidate giving more columns; one column means ","."""
    truncated = len(text) > 20000
    sample = text[:20000]
    best, best_score = ",", (0.0, 1)
    for delimiter in DELIMITERS:
        try:
            rows = [r for r in csv.reader(io.StringIO(sample), delimiter=delimiter) if any(c.strip() for c in r)]
        except csv.Error:
            continue
        if truncated and len(rows) > 1:
            rows = rows[:-1]  # the sample may end inside a row
        rows = rows[:50]
        if not rows or len(rows[0]) < 2:
            continue
        width = len(rows[0])
        score = (sum(1 for r in rows if len(r) == width) / len(rows), width)
        if score > best_score:
            best, best_score = delimiter, score
    return best


def parse_csv_text(text: str, delimiter: Optional[str] = None) -> Table:
    if delimiter is None:
        delimiter = detect_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ExchangeError("The file is empty")
    headers = [h.strip() or f"Column {i + 1}" for i, h in enumerate(rows[0])]
    width = len(headers)
    body = [(r + [""] * width)[:max(width, len(r))] for r in rows[1:]]
    return Table(headers, body, delimiter)


def read_table(path: Path | str) -> Table:
    """A CSV/TSV/TXT or XLSX file as a table of strings (first row = headers)."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise ExchangeError(f"Could not read {p.name}: {exc.strerror or exc}") from None
    if data[:2] == b"PK":
        table = _read_xlsx(data)
    else:
        text, encoding = _decode(data)
        table = parse_csv_text(text, "\t" if p.suffix.lower() == ".tsv" else None)
        table.encoding = encoding
    table.source = p.name
    return table


def _read_xlsx(data: bytes) -> Table:
    import xml.etree.ElementTree as ET

    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ExchangeError("Not a valid Excel (.xlsx) file") from None
    names = z.namelist()
    shared: List[str] = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
    date_styles: set = set()
    if "xl/styles.xml" in names:
        sroot = ET.fromstring(z.read("xl/styles.xml"))
        custom = {int(n.get("numFmtId")): n.get("formatCode", "") for n in sroot.findall("m:numFmts/m:numFmt", ns)}
        xfs = sroot.find("m:cellXfs", ns)
        for i, xf in enumerate(xfs.findall("m:xf", ns) if xfs is not None else []):
            fmt = int(xf.get("numFmtId", 0))
            code = custom.get(fmt, "")
            if 14 <= fmt <= 22 or 45 <= fmt <= 47 or (code and re.search(r"[dmyhs]", code.lower().replace('"', ""))
                                                      and "0" not in code):
                date_styles.add(i)
    sheet = next((n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")), None)
    if sheet is None:
        raise ExchangeError("The workbook has no sheets")
    root = ET.fromstring(z.read(sheet))
    grid: List[List[str]] = []
    for row in root.iter(f"{{{ns['m']}}}row"):
        values: Dict[int, str] = {}
        for c in row.findall("m:c", ns):
            ref = c.get("r", "")
            col = 0
            for ch in re.match(r"[A-Z]+", ref).group(0) if re.match(r"[A-Z]+", ref) else "A":
                col = col * 26 + (ord(ch) - 64)
            col -= 1
            t = c.get("t")
            v = c.findtext("m:v", default="", namespaces=ns)
            if t == "s":
                text = shared[int(v)] if v.isdigit() and int(v) < len(shared) else ""
            elif t == "inlineStr":
                text = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
            elif t == "b":
                text = "TRUE" if v == "1" else "FALSE"
            elif v and int(c.get("s", "0") or 0) in date_styles:
                try:
                    serial = float(v)
                    dt = _EPOCH + timedelta(seconds=round(serial * 86400))  # floats: 17:00 may be 16:59:59.97
                    text = dt.strftime("%Y-%m-%d %H:%M") if serial % 1 else dt.strftime("%Y-%m-%d")
                except ValueError:
                    text = v
            else:
                text = v
                if re.fullmatch(r"-?\d+\.0", text):
                    text = text[:-2]
            values[col] = text
        if values:
            width = max(values) + 1
            grid.append([values.get(i, "") for i in range(width)])
    grid = [r for r in grid if any(x.strip() for x in r)]
    if not grid:
        raise ExchangeError("The sheet is empty")
    headers = [h.strip() or f"Column {i + 1}" for i, h in enumerate(grid[0])]
    width = len(headers)
    return Table(headers, [(r + [""] * width)[:max(width, len(r))] for r in grid[1:]], delimiter="xlsx",
                 encoding="xlsx")


# ---------------------------------------------------------------------------
# Import: mapping columns
# ---------------------------------------------------------------------------
IGNORE = "ignore"
NEW_FIELD = "new_field"          # create a text custom field named after the column
TARGETS: List[Tuple[str, str]] = [
    ("title", "Title"), ("description", "Description"), ("column", "Column / status"), ("priority", "Priority"),
    ("labels", "Labels / tags"), ("assignee", "Assignee"), ("due", "Due date"), ("start", "Start date"),
    ("estimate", "Estimate (hours)"), ("estimate_seconds", "Estimate (seconds, Jira)"),
    ("completed", "Completed (yes/no or a date)"), ("checklist", "Checklist items"), ("comment", "Comment"),
]
SYNONYMS: Dict[str, List[str]] = {
    "title": ["title", "summary", "name", "task", "task name", "card name", "subject", "issue", "item", "content",
              "todo", "to do", "headline"],
    "description": ["description", "notes", "note", "details", "card description", "body", "desc", "content details"],
    "column": ["column", "status", "list", "list name", "section", "section/column", "stage", "state", "lane",
               "bucket", "bucket name", "progress"],
    "priority": ["priority", "importance", "urgency", "prio"],
    "labels": ["labels", "label", "tags", "tag", "categories", "category", "label names"],
    "assignee": ["assignee", "assigned to", "owner", "responsible", "members", "member", "assignees",
                 "assignee name", "person", "who"],
    "due": ["due", "due date", "due on", "deadline", "due_date", "end date", "finish date", "target date",
            "due at"],
    "start": ["start", "start date", "start on", "begin", "start_date", "planned start"],
    "estimate": ["estimate", "estimated hours", "estimate (h)", "hours", "time estimate", "effort (hours)",
                 "estimated time"],
    "estimate_seconds": ["original estimate", "time original estimate"],
    "completed": ["completed", "done", "is completed", "completed at", "completed on", "complete", "closed",
                  "resolved", "date completed", "resolution date"],
    "checklist": ["checklist", "checklists", "subtasks", "sub-tasks", "checklist items"],
    "comment": ["comment", "comments", "latest comment"],
}
# Extra header names per tool (all lower case); the generic synonyms apply too
PRESETS: Dict[str, Dict[str, str]] = {
    "Auto-detect": {},
    "Jira": {"summary": "title", "status": "column", "labels": "labels", "original estimate": "estimate_seconds",
             "resolved": "completed", "issue key": IGNORE, "issue id": IGNORE, "issue type": "labels",
             "component/s": "labels", "sprint": NEW_FIELD, "story points": NEW_FIELD,
             "custom field (story point estimate)": NEW_FIELD},
    "Asana": {"name": "title", "section/column": "column", "notes": "description", "completed at": "completed",
              "tags": "labels", "task id": IGNORE, "created at": IGNORE, "last modified": IGNORE},
    "Trello (CSV)": {"card name": "title", "list name": "column", "card description": "description",
                     "labels": "labels", "members": "assignee", "due date": "due", "card id": IGNORE,
                     "card url": IGNORE, "archived": IGNORE},
    "GitHub issues": {"title": "title", "body": "description", "state": "column", "labels": "labels",
                      "assignees": "assignee", "milestone": NEW_FIELD, "number": IGNORE, "url": IGNORE},
    "Todoist": {"content": "title", "description": "description", "priority": "priority", "date": "due",
                "responsible": "assignee", "type": IGNORE, "indent": IGNORE, "author": IGNORE},
}


def _norm(header: str) -> str:
    return " ".join(re.sub(r"[_\-]+", " ", header).lower().split())


def guess_mapping(table: Table, fields: Sequence[Dict[str, Any]] = (), preset: str = "Auto-detect") -> Dict[int, str]:
    """Column index -> target ("title", "field:<id>", "ignore" ...). Each single-value target is
    used once (the first matching column); labels may collect several columns."""
    preset_map = PRESETS.get(preset, {})
    by_field = {f["name"].lower(): f"field:{f['id']}" for f in fields}
    mapping: Dict[int, str] = {}
    used: set = set()
    for i, header in enumerate(table.headers):
        key = _norm(header)
        target = None
        if key in by_field:
            target = by_field[key]
        elif key in preset_map:
            target = preset_map[key]
        else:
            for t, names in SYNONYMS.items():
                if key in names:
                    target = t
                    break
        if target is None:
            target = IGNORE
        if target not in (IGNORE, NEW_FIELD, "labels") and target in used:
            target = IGNORE
        used.add(target)
        mapping[i] = target
    if "title" not in used and table.headers:  # fall back to the first text-looking column
        for i in range(len(table.headers)):
            if mapping[i] == IGNORE and any(s and not re.fullmatch(r"[\d.,:/ -]+", s) for s in table.column_samples(i)):
                mapping[i] = "title"
                break
    return mapping


# ---------------------------------------------------------------------------
# Import: parsing values
# ---------------------------------------------------------------------------
_TRUE = {"yes", "y", "true", "1", "done", "complete", "completed", "closed", "resolved", "x", "✓", "✔", "finished"}
_FALSE = {"no", "n", "false", "0", "open", "", "-", "incomplete", "not started", "todo", "to do"}
_DONE_STATUSES = {"done", "closed", "resolved", "complete", "completed", "finished", "shipped", "released"}


def parse_priority(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in ("urgent", "highest", "critical", "blocker", "p0", "p1", "!!!", "asap", "immediate", "1"):
        return Priority.URGENT.value
    if t in ("high", "major", "p2", "!!", "important", "2"):
        return Priority.HIGH.value
    if t in ("medium", "normal", "moderate", "p3", "!", "3", "default"):
        return Priority.MEDIUM.value
    if t in ("low", "lowest", "minor", "trivial", "p4", "p5", "4", "5", "someday"):
        return Priority.LOW.value
    for p in Priority:
        if t == p.value.lower() or t == p.display_name.lower():
            return p.value
    return None


def parse_bool(text: str) -> Optional[bool]:
    t = (text or "").strip().lower()
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    return None


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                        "dec"], start=1)}


def parse_datetime_text(text: str, order: str = "auto") -> Tuple[Optional[datetime], bool]:
    """(local naive datetime, has_time) from common formats; ``order`` = auto | mdy | dmy for
    numeric dates (auto = month first unless the first number is over 12)."""
    t = (text or "").strip()
    if not t:
        return None, False
    # ISO 8601 (with optional time / Z / offset)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?)?", t)
    if m:
        y, mo, d, hh, mi, ss, tz = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0))
        except ValueError:
            return None, False
        if tz:
            offset = timedelta(0) if tz == "Z" else timedelta(hours=int(tz[1:3]), minutes=int(tz[-2:])) * \
                (1 if tz[0] == "+" else -1)
            try:
                dt = (dt - offset).replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
            except (OverflowError, ValueError, OSError):  # year 1 / 9999 shifted out of range
                return None, False
        return dt, hh is not None
    time_part = None
    tm = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?$", t, re.I)
    if tm:
        hh, mi = int(tm.group(1)), int(tm.group(2))
        ampm = (tm.group(3) or "").lower().replace(".", "")
        if ampm == "pm" and hh < 12:
            hh += 12
        if ampm == "am" and hh == 12:
            hh = 0
        if hh < 24 and mi < 60:
            time_part = time(hh, mi)
        t = t[:tm.start()].strip().rstrip(",").strip()
    d = None
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", t)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        if order == "dmy" or (order == "auto" and a > 12):
            a, b = b, a
        try:
            d = date(y, a, b)
        except ValueError:
            d = None
    if d is None:
        # "Jun 5, 2025", "5 Jun 2025", "05/Jun/25", "June 5 2025", "Thu, 5 Jun 2025"
        t2 = re.sub(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s*", "", t.lower())
        m = re.fullmatch(r"([a-z]{3})[a-z]*\.?[ /-](\d{1,2})(?:st|nd|rd|th)?,?[ /-](\d{2,4})", t2) or None
        if m and m.group(1) in _MONTHS:
            y = int(m.group(3))
            d = _safe_date(y + (2000 if y < 100 else 0), _MONTHS[m.group(1)], int(m.group(2)))
        else:
            m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?[ /-]([a-z]{3})[a-z]*\.?,?[ /-](\d{2,4})", t2)
            if m and m.group(2) in _MONTHS:
                y = int(m.group(3))
                d = _safe_date(y + (2000 if y < 100 else 0), _MONTHS[m.group(2)], int(m.group(1)))
    if d is None:
        m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", t)
        if m:
            d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if d is None:
        return None, False
    return datetime.combine(d, time_part or time(0, 0)), time_part is not None


def _safe_date(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def guess_date_order(values: Iterable[str]) -> str:
    """"dmy" if some numeric date only makes sense day-first, else "mdy"."""
    for v in values:
        m = re.match(r"\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{2,4}", v or "")
        if m:
            if int(m.group(1)) > 12:
                return "dmy"
            if int(m.group(2)) > 12:
                return "mdy"
    return "mdy"


def split_list(text: str) -> List[str]:
    parts = re.split(r"[,;|\n]", text or "")
    out: List[str] = []
    for p in parts:
        p = " ".join(p.split())
        if p and p.lower() not in (x.lower() for x in out):
            out.append(p)
    return out


def parse_hours(text: str) -> Optional[float]:
    t = (text or "").strip().lower().replace(",", ".")
    if not t:
        return None
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?", t)
    if m and (m.group(1) or m.group(2)):
        return round(float(m.group(1) or 0) + int(m.group(2) or 0) / 60.0, 2)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*d(?:ays?)?", t)
    if m:
        return float(m.group(1)) * 8
    try:
        hours = float(t)
    except ValueError:
        return None
    return max(0.0, hours) if math.isfinite(hours) else None  # "inf", "nan", "1e999" aren't estimates


# ---------------------------------------------------------------------------
# Import: planning & running
# ---------------------------------------------------------------------------
@dataclass
class ImportOptions:
    board_id: int
    mapping: Dict[int, str]
    default_column_id: Optional[int] = None     # rows without a (known) column go here
    create_columns: bool = True                 # unknown status values become new columns
    create_labels: bool = True
    skip_duplicates: bool = True                # same title in the same column on the board
    date_order: str = "auto"                    # auto | mdy | dmy


@dataclass
class RowPlan:
    line: int                                   # 1-based data row number (header = row 0)
    title: str = ""
    description: str = ""
    column: Optional[str] = None                # column name from the file
    column_id: Optional[int] = None
    new_column: Optional[str] = None            # to be created
    priority: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    assignee: Optional[str] = None
    due: Optional[datetime] = None
    start: Optional[date] = None
    estimate: Optional[float] = None
    completed: bool = False
    completed_at: Optional[datetime] = None
    checklist: List[Tuple[str, bool]] = field(default_factory=list)     # (text, done)
    comments: List[str] = field(default_factory=list)
    fields: Dict[str, Any] = field(default_factory=dict)    # target key ("field:<id>" / header) -> value
    warnings: List[str] = field(default_factory=list)
    skip: Optional[str] = None                  # reason when the row won't be imported


@dataclass
class ImportResult:
    created: List[int] = field(default_factory=list)          # task ids
    columns_created: List[int] = field(default_factory=list)
    labels_created: List[int] = field(default_factory=list)
    fields_created: List[int] = field(default_factory=list)
    skipped: int = 0
    warnings: List[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [f"{len(self.created)} task{'s' if len(self.created) != 1 else ''} imported"]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.columns_created:
            parts.append(f"{len(self.columns_created)} column{'s' if len(self.columns_created) != 1 else ''} added")
        if self.labels_created:
            parts.append(f"{len(self.labels_created)} label{'s' if len(self.labels_created) != 1 else ''} added")
        if self.fields_created:
            parts.append(f"{len(self.fields_created)} field{'s' if len(self.fields_created) != 1 else ''} added")
        return ", ".join(parts)


def plan_rows(service, table: Table, opts: ImportOptions) -> List[RowPlan]:
    """Every row parsed against the board (nothing is written)."""
    columns = service.get_columns_for_board(opts.board_id)
    by_name = {c.name.lower(): c for c in columns}
    done_col = next((c for c in columns if c.is_done_column), None)
    fields = {f"field:{f['id']}": f for f in service.get_custom_fields(opts.board_id)}
    date_cols = [i for i, t in opts.mapping.items() if t in ("due", "start", "completed")]
    order = opts.date_order if opts.date_order != "auto" else guess_date_order(
        row[i] for row in table.rows for i in date_cols if i < len(row))
    existing = {(t.column_id, t.title.strip().lower()) for t in service.get_tasks_for_board(opts.board_id)}
    default_col = opts.default_column_id or service.default_capture_column(opts.board_id)
    seen_in_file: set = set()
    plans: List[RowPlan] = []
    for n, row in enumerate(table.rows, start=1):
        plan = RowPlan(line=n)
        for i, target in opts.mapping.items():
            if i >= len(row) or target == IGNORE:
                continue
            raw = str(row[i]).strip()
            if not raw:
                continue
            header = table.headers[i]
            if target == "title":
                plan.title = " ".join(raw.split())[:255]
            elif target == "description":
                plan.description = (plan.description + "\n\n" + raw).strip() if plan.description else raw
            elif target == "column":
                plan.column = raw
            elif target == "priority":
                plan.priority = parse_priority(raw)
                if plan.priority is None:
                    plan.warnings.append(f"unknown priority '{raw}'")
            elif target == "labels":
                plan.labels += [l for l in split_list(raw) if l.lower() not in (x.lower() for x in plan.labels)]
            elif target == "assignee":
                plan.assignee = split_list(raw)[0] if split_list(raw) else None
            elif target in ("due", "start"):
                dt, has_time = parse_datetime_text(raw, order)
                if dt is None:
                    plan.warnings.append(f"unreadable date '{raw}' ({header})")
                elif target == "due":
                    plan.due = dt if has_time else datetime.combine(dt.date(), DEFAULT_DUE_TIME)
                else:
                    plan.start = dt.date()
            elif target in ("estimate", "estimate_seconds"):
                hours = parse_hours(raw)
                if hours is None:
                    plan.warnings.append(f"unreadable estimate '{raw}'")
                else:
                    plan.estimate = round(hours / 3600.0, 2) if target == "estimate_seconds" else hours
            elif target == "completed":
                flag = parse_bool(raw)
                if flag is None:
                    dt, _ = parse_datetime_text(raw, order)
                    flag = dt is not None
                    if dt is not None:
                        plan.completed_at = dt
                plan.completed = plan.completed or bool(flag)
            elif target == "checklist":
                for part in re.split(r"[\n;]", raw):
                    m = re.match(r"\s*(?:[-*]\s*)?(?:\[([ xX✓])\]\s*)?(.*)", part)
                    text = m.group(2).strip()
                    if text:
                        plan.checklist.append((text, (m.group(1) or " ").strip() != ""))
            elif target == "comment":
                plan.comments.append(raw)
            elif target.startswith("field:"):
                f = fields.get(target)
                if f is None:
                    continue
                try:
                    value = cf.type_of(f).parse(raw, f["config"])
                except cf.FieldValueError as exc:
                    if f["field_type"] == "date":
                        dt, _ = parse_datetime_text(raw, order)
                        value = dt.date().isoformat() if dt else None
                        if value is None:
                            plan.warnings.append(f"{f['name']}: {exc}")
                            continue
                    elif f["field_type"] in ("select", "multi_select"):
                        value = raw  # becomes a new option
                    else:
                        plan.warnings.append(f"{f['name']}: {exc}")
                        continue
                plan.fields[target] = value
            elif target == NEW_FIELD:
                plan.fields[f"new:{header}"] = raw
        if not plan.title:
            plan.skip = "no title"
        # column
        if plan.column:
            col = by_name.get(plan.column.lower())
            if col is not None:
                plan.column_id = col.id
            elif plan.column.lower() in _DONE_STATUSES and done_col is not None:
                plan.column_id = done_col.id
            elif opts.create_columns:
                plan.new_column = " ".join(plan.column.split())[:100]
            else:
                plan.column_id = default_col
        else:
            plan.column_id = done_col.id if plan.completed and done_col is not None else default_col
        if plan.column and plan.column.lower() in _DONE_STATUSES:
            plan.completed = True
        if not plan.skip and opts.skip_duplicates:
            key = (plan.column_id if plan.new_column is None else f"new:{plan.new_column.lower()}", plan.title.lower())
            if (plan.column_id, plan.title.lower()) in existing:
                plan.skip = "already on the board"
            elif key in seen_in_file:
                plan.skip = "duplicate row"
            seen_in_file.add(key)
        plans.append(plan)
    return plans


def run_import(service, table: Table, opts: ImportOptions) -> ImportResult:
    """Create the tasks (and any missing columns, labels and fields)."""
    result = ImportResult()
    plans = plan_rows(service, table, opts)
    new_columns: Dict[str, int] = {}
    labels = {l.name.lower(): l.id for l in service.get_labels_for_board(opts.board_id)}
    fields = {f"field:{f['id']}": f for f in service.get_custom_fields(opts.board_id)}
    new_fields: Dict[str, Dict[str, Any]] = {}
    for plan in plans:
        if plan.skip:
            result.skipped += 1
            continue
        try:
            column_id = plan.column_id
            if plan.new_column:
                key = plan.new_column.lower()
                if key not in new_columns:
                    col = service.create_column(opts.board_id, plan.new_column)
                    new_columns[key] = col.id
                    result.columns_created.append(col.id)
                column_id = new_columns[key]
            label_ids = []
            for name in plan.labels:
                if name.lower() in labels:
                    label_ids.append(labels[name.lower()])
                elif opts.create_labels:
                    lbl = service.create_label(opts.board_id, name[:60], "#64748B")
                    labels[name.lower()] = lbl.id
                    result.labels_created.append(lbl.id)
                    label_ids.append(lbl.id)
            values: Dict[int, Any] = {}
            for key, value in plan.fields.items():
                if key.startswith("new:"):
                    name = key[4:]
                    if name not in new_fields:
                        existing = next((f for f in service.get_custom_fields(opts.board_id)
                                         if f["name"].lower() == name.lower()), None)
                        if existing is None:
                            existing = service.create_custom_field(opts.board_id, cf.normalize_name(name)[:60], "text",
                                                                   show_on_card=False)
                            result.fields_created.append(existing["id"])
                        new_fields[name] = existing
                    f = new_fields[name]
                else:
                    f = fields[key]
                if f["field_type"] in ("select", "multi_select"):
                    wanted = value if isinstance(value, list) else split_list(str(value)) \
                        if f["field_type"] == "multi_select" else [str(value)]
                    missing = [w for w in wanted if cf.type_of(f).option(w, f["config"]) is None]
                    if missing:
                        f = service.update_custom_field(f["id"], config=cf.merge_options(f["config"], missing))
                        fields[f"field:{f['id']}"] = f
                try:
                    values[f["id"]] = cf.validate_value(f, value)
                except cf.FieldValueError as exc:
                    plan.warnings.append(str(exc))
            task = service.create_task(
                board_id=opts.board_id, column_id=column_id, title=plan.title, description=plan.description,
                priority=plan.priority or Priority.MEDIUM.value, estimated_hours=plan.estimate or 0.0,
                start_date=plan.start, due_date=plan.due, label_ids=label_ids, assignee=plan.assignee,
                checklist=[{"id": i + 1, "text": t, "done": done or plan.completed}
                           for i, (t, done) in enumerate(plan.checklist)],
                field_values=values, origin=f"imported from {table.source or 'a spreadsheet'}")
            if plan.completed and not task.is_completed:
                service.update_task(task.id, is_completed=True, completed_at=plan.completed_at or datetime.now())
            for body in plan.comments:
                service.add_comment(task.id, body)
            result.created.append(task.id)
        except Exception as exc:  # keep going; report the row
            result.warnings.append(f"Row {plan.line} ('{plan.title}'): {exc}")
            continue
        for w in plan.warnings:
            result.warnings.append(f"Row {plan.line}: {w}")
    return result


def undo_import(service, result: ImportResult) -> int:
    """Delete what an import created: its tasks, then the columns, labels and fields it added
    that nothing else uses meanwhile. Returns the number of tasks removed."""
    n = 0
    for task_id in result.created:
        if service.delete_task(task_id):
            n += 1
    for col_id in result.columns_created:
        board_id = _board_of_column(service, col_id)
        if board_id is not None and not any(t.column_id == col_id for t in
                                            service.get_tasks_for_board(board_id, include_archived=True)):
            service.delete_column(col_id)
    for label_id in result.labels_created:
        if not _label_in_use(service, label_id):
            service.delete_label(label_id)
    for field_id in result.fields_created:
        if service.count_field_values(field_id) == 0:
            service.delete_custom_field(field_id)
    return n


def _label_in_use(service, label_id: int) -> bool:
    from kanbanmanager.database.models import task_labels

    with service.db.session_scope() as session:
        return session.query(task_labels).filter(task_labels.c.label_id == label_id).first() is not None


def _board_of_column(service, column_id: int) -> Optional[int]:
    with service.db.session_scope() as session:
        from kanbanmanager.database.models import BoardColumn

        col = session.get(BoardColumn, column_id)
        return col.board_id if col else None


# ---------------------------------------------------------------------------
# Trello
# ---------------------------------------------------------------------------
TRELLO_COLORS = {"green": "#61BD4F", "yellow": "#F2D600", "orange": "#FF9F1A", "red": "#EB5A46", "purple": "#C377E0",
                 "blue": "#0079BF", "sky": "#00C2E0", "lime": "#51E898", "pink": "#FF78CB", "black": "#344563"}
_DONE_NAMES = {"done", "complete", "completed", "finished", "shipped", "closed", "released"}


def _trello_color(color: Optional[str]) -> str:
    base = (color or "").split("_")[0]
    return TRELLO_COLORS.get(base, "#64748B")


def _trello_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None, microsecond=0) if dt.tzinfo else dt


def read_trello(path: Path | str) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ExchangeError(f"Could not read the Trello export: {exc}") from None
    if not isinstance(data, dict) or "cards" not in data or "lists" not in data:
        raise ExchangeError("This is not a Trello board export (Board menu → Print, export and share → JSON)")
    return data


def import_trello(service, data: Dict[str, Any], workspace_id: int, name: Optional[str] = None,
                  include_archived: bool = False, include_comments: bool = True) -> Tuple[Any, ImportResult]:
    """Create a board from a Trello export. Returns (board, result)."""
    result = ImportResult()
    lists = sorted([l for l in data.get("lists", []) if include_archived or not l.get("closed")],
                   key=lambda l: l.get("pos", 0))
    if not lists:
        raise ExchangeError("The Trello board has no lists")
    columns_def = []
    for l in lists:
        done = l.get("name", "").strip().lower() in _DONE_NAMES
        columns_def.append({"name": (l.get("name") or "List").strip()[:100], "column_type":
                            ColumnType.DONE.value if done else ColumnType.CUSTOM.value, "is_done_column": done})
    # unique column names
    seen: Dict[str, int] = {}
    for c in columns_def:
        base = c["name"]
        k = base.lower()
        if k in seen:
            seen[k] += 1
            c["name"] = f"{base} ({seen[k]})"
        else:
            seen[k] = 1
    label_defs, label_names = [], {}
    for lbl in data.get("labels", []):
        lname = (lbl.get("name") or "").strip() or (lbl.get("color") or "label").replace("_", " ").title()
        if lname.lower() not in (x["name"].lower() for x in label_defs):
            label_defs.append({"name": lname[:60], "color": _trello_color(lbl.get("color"))})
        label_names[lbl.get("id")] = lname[:60]
    board = service.create_board(workspace_id=workspace_id, name=(name or data.get("name") or "Trello board")[:120],
                                 description=(data.get("desc") or "")[:2000], columns_def=columns_def,
                                 labels_def=label_defs)
    cols = service.get_columns_for_board(board.id)
    col_for_list = {l.get("id"): cols[i].id for i, l in enumerate(lists)}
    labels = {l.name.lower(): l.id for l in service.get_labels_for_board(board.id)}
    members = {m.get("id"): m.get("fullName") or m.get("username") for m in data.get("members", [])}
    checklists: Dict[str, List[Dict[str, Any]]] = {}
    for cl in data.get("checklists", []):
        checklists.setdefault(cl.get("idCard"), []).append(cl)
    # custom fields
    field_map: Dict[str, Dict[str, Any]] = {}
    options_by_id: Dict[str, str] = {}
    type_map = {"text": "text", "number": "number", "date": "date", "checkbox": "checkbox", "list": "select"}
    for cfd in data.get("customFields", []):
        ftype = type_map.get(cfd.get("type"))
        if not ftype:
            continue
        config: Dict[str, Any] = {}
        if ftype == "select":
            opts = []
            for o in cfd.get("options", []):
                text = ((o.get("value") or {}).get("text") or "").strip()
                if text:
                    opts.append({"label": text, "color": _trello_color(o.get("color"))})
                    options_by_id[o.get("id")] = text
            config = {"options": opts}
        elif ftype == "number":
            config = {"decimals": 2}
        try:
            f = service.create_custom_field(board.id, cf.normalize_name(cfd.get("name") or "Field"), ftype,
                                            config=config)
        except (cf.FieldValueError, ValueError) as exc:
            result.warnings.append(f"Custom field '{cfd.get('name')}': {exc}")
            continue
        field_map[cfd.get("id")] = f
        result.fields_created.append(f["id"])
    comments: Dict[str, List[Dict[str, Any]]] = {}
    if include_comments:
        for a in data.get("actions", []):
            if a.get("type") == "commentCard":
                card_id = ((a.get("data") or {}).get("card") or {}).get("id")
                comments.setdefault(card_id, []).append(a)

    cards = sorted(data.get("cards", []), key=lambda c: (c.get("idList"), c.get("pos", 0)))
    for card in cards:
        if card.get("idList") not in col_for_list:
            result.skipped += 1
            continue
        if card.get("closed") and not include_archived:
            result.skipped += 1
            continue
        try:
            desc = card.get("desc") or ""
            links = [a for a in card.get("attachments", []) if a.get("url")]
            if links:
                desc = (desc + "\n\n**Attachments**\n" + "\n".join(f"- [{a.get('name') or a['url']}]({a['url']})"
                                                                  for a in links)).strip()
            items = []
            for cl in sorted(checklists.get(card.get("id"), []), key=lambda c: c.get("pos", 0)):
                prefix = f"{cl.get('name')}: " if len(checklists.get(card.get("id"), [])) > 1 else ""
                for it in sorted(cl.get("checkItems", []), key=lambda i: i.get("pos", 0)):
                    items.append({"id": len(items) + 1, "text": f"{prefix}{it.get('name', '')}"[:300],
                                  "done": it.get("state") == "complete"})
            label_ids = [labels[label_names[i].lower()] for i in card.get("idLabels", [])
                         if i in label_names and label_names[i].lower() in labels]
            values: Dict[int, Any] = {}
            for item in card.get("customFieldItems", []):
                f = field_map.get(item.get("idCustomField"))
                if f is None:
                    continue
                v = item.get("value") or {}
                raw: Any = None
                if f["field_type"] == "select":
                    raw = options_by_id.get(item.get("idValue"))
                elif f["field_type"] == "checkbox":
                    raw = v.get("checked") == "true"
                elif f["field_type"] == "date":
                    dt = _trello_dt(v.get("date"))
                    raw = dt.date().isoformat() if dt else None
                else:
                    raw = v.get("text") if f["field_type"] == "text" else v.get("number")
                try:
                    values[f["id"]] = cf.validate_value(f, raw)
                except cf.FieldValueError as exc:
                    result.warnings.append(f"'{card.get('name')}': {exc}")
            due = _trello_dt(card.get("due"))
            start = _trello_dt(card.get("start"))
            assignee = next((members[m] for m in card.get("idMembers", []) if members.get(m)), None)
            task = service.create_task(board_id=board.id, column_id=col_for_list[card["idList"]],
                                       title=(card.get("name") or "Untitled card")[:255], description=desc,
                                       due_date=due, start_date=start.date() if start else None,
                                       label_ids=label_ids, checklist=items, assignee=assignee,
                                       field_values=values, origin="imported from Trello")
            if card.get("dueComplete") and not task.is_completed:
                service.update_task(task.id, is_completed=True, completed_at=due or datetime.now())
            for a in sorted(comments.get(card.get("id"), []), key=lambda a: a.get("date") or ""):
                text = ((a.get("data") or {}).get("text") or "").strip()
                if text:
                    when = _trello_dt(a.get("date"))
                    author = (a.get("memberCreator") or {}).get("fullName") or "Trello"
                    service.add_comment(task.id, text, author=author,
                                        created_at=when.astimezone(timezone.utc).replace(tzinfo=None) if when else None)
            if card.get("closed"):
                service.archive_task(task.id, reason="Archived in Trello")
            result.created.append(task.id)
        except Exception as exc:
            result.warnings.append(f"Card '{card.get('name')}': {exc}")
    return service.get_board(board.id), result

"""
Quick-Capture Language (pure Python, no Qt)

One line turns into a task:

    Fix login timeout tomorrow 3pm !high #Bug @marcus >roadmap // check the SSO flow

* dates:    today, tonight, tomorrow/tmr, weekday names (mon, next fri), next week,
            weekend, eow / end of week, eom, in 3 days / in 2 weeks / in a month,
            2024-06-05, jun 5, 5 june 2025 ...  The *last* date phrase wins;
            earlier ones stay in the title ("Plan monday standup tomorrow").
* times:    3pm, 3:30pm, 15:00, at 9, noon, eod (17:00). A time alone means today,
            or tomorrow if that time has already passed.
* priority: !urgent !high !medium !low, or !1 (urgent) .. !4 (low); !!! = urgent, !! = high
* labels:   #Bug #needs-review
* assignee: @marcus (matches a known person by name or first-name prefix), @me
* board:    >roadmap (matches a board by name prefix); without it the item goes
            to the Global Inbox
* notes:    everything after " // "
* "quoted text" is always kept literally in the title.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from kanbanmanager.enums import Priority

DEFAULT_DUE_TIME = time(17, 0)

_PRIORITY_WORDS = {
    "urgent": Priority.URGENT, "1": Priority.URGENT, "!!": Priority.URGENT,
    "high": Priority.HIGH, "2": Priority.HIGH, "!": Priority.HIGH,
    "medium": Priority.MEDIUM, "med": Priority.MEDIUM, "3": Priority.MEDIUM,
    "low": Priority.LOW, "4": Priority.LOW,
}
_WEEKDAYS = {name: i for i, names in enumerate([
    ("monday", "mon"), ("tuesday", "tue", "tues"), ("wednesday", "wed"), ("thursday", "thu", "thurs"),
    ("friday", "fri"), ("saturday", "sat"), ("sunday", "sun")]) for name in names}
_MONTHS = {name: i for i, names in enumerate([
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
    ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"),
    ("november", "nov"), ("december", "dec")], start=1) for name in names}


@dataclass
class CaptureResult:
    title: str
    due: Optional[datetime] = None
    due_label: str = ""                 # the phrase that set the date, for the preview
    priority: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    assignee: Optional[str] = None
    board_id: Optional[int] = None
    board_name: Optional[str] = None
    notes: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return bool(self.title.strip())


# ---------------------------------------------------------------------------
# Tokenising
# ---------------------------------------------------------------------------
@dataclass
class _Tok:
    text: str
    literal: bool = False   # came from "quotes"
    used: bool = False

    @property
    def low(self) -> str:
        return self.text.lower().strip(",.;")


def _tokenize(text: str) -> List[_Tok]:
    tokens: List[_Tok] = []
    for m in re.finditer(r'"([^"]*)"|(\S+)', text):
        if m.group(1) is not None:
            if m.group(1).strip():
                tokens.append(_Tok(m.group(1).strip(), literal=True))
        else:
            tokens.append(_Tok(m.group(2)))
    return tokens


# ---------------------------------------------------------------------------
# Dates & times
# ---------------------------------------------------------------------------
def _parse_time(words: Sequence[str]) -> Tuple[Optional[time], int]:
    """Match a time at the start of ``words``: returns (time, words consumed)."""
    if not words:
        return None, 0
    w = words[0]
    offset = 0
    if w == "at" and len(words) > 1:
        w, offset = words[1], 1
        m = re.fullmatch(r"(\d{1,2})", w)  # "at 9"
        if m and 0 <= int(m.group(1)) <= 23:
            hour = int(m.group(1))
            if len(words) > 2 and words[2] in ("am", "pm"):
                hour = hour % 12 + (12 if words[2] == "pm" else 0)
                return time(hour, 0), 3
            return time(hour if hour >= 7 or hour == 0 else hour + 12, 0), 2  # "at 3" -> 15:00
    if w == "noon":
        return time(12, 0), offset + 1
    if w in ("eod", "end-of-day"):
        return DEFAULT_DUE_TIME, offset + 1
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)", w)
    if m:
        hour = int(m.group(1))
        if 1 <= hour <= 12:
            hour = hour % 12 + (12 if m.group(3) == "pm" else 0)
            return time(hour, int(m.group(2) or 0)), offset + 1
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", w)
    if m:
        return time(int(m.group(1)), int(m.group(2))), offset + 1
    return None, 0


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _parse_date(words: Sequence[str], today: date) -> Tuple[Optional[date], int, Optional[time]]:
    """Match a date phrase at the start of ``words``: (date, words consumed, implied time)."""
    if not words:
        return None, 0, None
    w = words[0]
    nxt = words[1] if len(words) > 1 else ""
    if w in ("today", "tod"):
        return today, 1, None
    if w == "tonight":
        return today, 1, time(20, 0)
    if w in ("tomorrow", "tmr", "tmrw"):
        return today + timedelta(days=1), 1, None
    if w in ("eow",) or (w == "end" and nxt == "of" and len(words) > 2 and words[2] == "week"):
        return today + timedelta(days=(4 - today.weekday()) % 7), (1 if w == "eow" else 3), None
    if w == "eom" or (w == "end" and nxt == "of" and len(words) > 2 and words[2] == "month"):
        last = calendar.monthrange(today.year, today.month)[1]
        return today.replace(day=last), (1 if w == "eom" else 3), None
    if w == "weekend" or (w == "this" and nxt == "weekend"):
        days = (5 - today.weekday()) % 7
        return today + timedelta(days=days), (1 if w == "weekend" else 2), None
    if w == "next" and nxt in ("week", "month"):
        if nxt == "week":
            return today + timedelta(days=7 - today.weekday()), 2, None
        return _add_months(today.replace(day=1), 1), 2, None
    if w in ("next", "on", "this") and nxt in _WEEKDAYS:
        target = _WEEKDAYS[nxt]
        if w == "next":  # that weekday in next (Monday-based) week
            next_monday = today + timedelta(days=7 - today.weekday())
            return next_monday + timedelta(days=target), 2, None
        return today + timedelta(days=(target - today.weekday()) % 7), 2, None
    if w in _WEEKDAYS:
        return today + timedelta(days=(_WEEKDAYS[w] - today.weekday()) % 7), 1, None
    if w == "in" and len(words) > 2:
        count = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}.get(nxt)
        if count is None and nxt.isdigit():
            count = int(nxt)
        unit = words[2].rstrip("s")
        if count is not None and unit in ("day", "week", "month"):
            if unit == "day":
                return today + timedelta(days=count), 3, None
            if unit == "week":
                return today + timedelta(weeks=count), 3, None
            return _add_months(today, count), 3, None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", w)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), 1, None
        except ValueError:
            return None, 0, None
    # "jun 5", "june 5 2025", "5 jun", "5 june 2025"
    def year_after(i: int) -> Tuple[Optional[int], int]:
        if len(words) > i and re.fullmatch(r"\d{4}", words[i]):
            return int(words[i]), 1
        return None, 0

    if w in _MONTHS and re.fullmatch(r"\d{1,2}(st|nd|rd|th)?", nxt):
        day = int(re.match(r"\d+", nxt).group(0))
        year, extra = year_after(2)
        return _month_day(today, _MONTHS[w], day, year), 2 + extra, None
    if re.fullmatch(r"\d{1,2}(st|nd|rd|th)?", w) and nxt in _MONTHS:
        day = int(re.match(r"\d+", w).group(0))
        year, extra = year_after(2)
        return _month_day(today, _MONTHS[nxt], day, year), 2 + extra, None
    return None, 0, None


def _month_day(today: date, month: int, day: int, year: Optional[int]) -> Optional[date]:
    try:
        if year is not None:
            return date(year, month, day)
        candidate = date(today.year, month, day)
        return candidate if candidate >= today else date(today.year + 1, month, day)  # next occurrence
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def _match_person(name: str, people: Sequence[str], me: str) -> str:
    key = name.lower().replace("_", " ").replace(".", " ").strip()
    if key in ("me", "myself"):
        return me
    compact = key.replace(" ", "")
    for p in people:
        if p.lower().replace(" ", "") == compact:
            return p
    starts = [p for p in people if p.lower().startswith(key) or p.lower().split()[0] == key]
    if len(starts) == 1:
        return starts[0]
    return name.replace("_", " ").strip().title() if key else name


def _match_board(query: str, boards: Sequence[Tuple[int, str]]) -> Tuple[Optional[int], Optional[str], bool]:
    """(board id, name, ambiguous?) for '>query'."""
    q = query.lower().replace("-", " ").replace("_", " ").strip()
    if not q:
        return None, None, False
    exact = [b for b in boards if b[1].lower() == q or b[1].lower().replace(" ", "") == q.replace(" ", "")]
    if len(exact) == 1:
        return exact[0][0], exact[0][1], False
    prefix = [b for b in boards if b[1].lower().startswith(q)
              or any(word.startswith(q) for word in b[1].lower().split())]
    if len(prefix) == 1:
        return prefix[0][0], prefix[0][1], False
    return None, None, len(prefix) > 1


def parse_capture(
    text: str,
    now: Optional[datetime] = None,
    people: Sequence[str] = (),
    boards: Sequence[Tuple[int, str]] = (),
    me: str = "",
) -> CaptureResult:
    now = now or datetime.now()
    today = now.date()
    raw = text or ""
    notes = ""
    if " // " in raw or raw.startswith("// "):
        raw, notes = (raw.split(" // ", 1) if " // " in raw else ("", raw[3:]))
    tokens = _tokenize(raw)
    result = CaptureResult(title="", notes=notes.strip())

    # Markers first (!priority, #label, @person, >board)
    for tok in tokens:
        if tok.literal or len(tok.text) < 2:
            continue
        head, body = tok.text[0], tok.text[1:].rstrip(",.;")
        if head == "!" and body.lower() in _PRIORITY_WORDS:
            result.priority = _PRIORITY_WORDS[body.lower()].value
            tok.used = True
        elif head == "#" and re.fullmatch(r"[\w][\w\-/]*", body):
            if body.lower() not in (l.lower() for l in result.labels):
                result.labels.append(body)
            tok.used = True
        elif head == "@" and re.fullmatch(r"[\w][\w.\-]*", body):
            result.assignee = _match_person(body, people, me)
            tok.used = True
        elif head == ">" and body:
            board_id, name, ambiguous = _match_board(body, boards)
            if board_id is not None:
                result.board_id, result.board_name = board_id, name
            else:
                result.warnings.append(f"'{body}' matches several boards - sending to the Inbox" if ambiguous
                                       else f"No board matches '{body}' - sending to the Inbox")
            tok.used = True

    # Date / time phrases: scan the remaining (unquoted) words; the last phrase wins
    free = [i for i, t in enumerate(tokens) if not t.used and not t.literal]
    found: List[Tuple[List[int], Optional[date], Optional[time], str]] = []
    k = 0
    while k < len(free):
        # only contiguous runs of free tokens can form a phrase
        run = [free[k]]
        while k + len(run) < len(free) and free[k + len(run)] == run[-1] + 1:
            run.append(free[k + len(run)])
        words = [tokens[i].low for i in run]
        j = 0
        while j < len(words):
            d, n, implied = _parse_date(words[j:], today)
            t, m = (None, 0)
            if d is not None:
                t, m = _parse_time(words[j + n:])
                t = t or implied
                consumed = n + m
            else:
                t, m = _parse_time(words[j:])
                consumed = m
            if consumed:
                phrase = " ".join(tokens[i].text for i in run[j:j + consumed])
                found.append((run[j:j + consumed], d, t, phrase))
                j += consumed
            else:
                j += 1
        k += len(run)
    if found:
        indexes, d, t, phrase = found[-1]
        for i in indexes:
            tokens[i].used = True
        if d is None:  # a time alone: today, or tomorrow if it has passed
            d = today if datetime.combine(today, t) > now else today + timedelta(days=1)
        result.due = datetime.combine(d, t or DEFAULT_DUE_TIME)
        result.due_label = phrase

    result.title = " ".join(t.text for t in tokens if not t.used).strip()
    if not result.title and result.notes:
        result.title, result.notes = result.notes, ""
    return result


def parse_date_text(text: str, today: Optional[date] = None) -> Optional[date]:
    """A whole string as one date phrase ("tomorrow", "next fri", "jun 5", "2025-06-05"), else None."""
    words = [w.lower().strip(",.;") for w in (text or "").split()]
    if not words:
        return None
    found, used, _time = _parse_date(words, today or date.today())
    return found if found is not None and used == len(words) else None


def describe_due(due: Optional[datetime], now: Optional[datetime] = None) -> str:
    """'Today 17:00', 'Tomorrow 15:00', 'Fri Jun 7 17:00'."""
    if due is None:
        return ""
    today = (now or datetime.now()).date()
    if due.date() == today:
        day = "Today"
    elif due.date() == today + timedelta(days=1):
        day = "Tomorrow"
    else:
        day = due.strftime("%a %b %d").replace(" 0", " ")
        if due.year != today.year:
            day += f" {due.year}"
    return f"{day} {due:%H:%M}"


# ---------------------------------------------------------------------------
# Saving (shared by the capture window, the command line and the Inbox)
# ---------------------------------------------------------------------------
CAPTURE_ORIGIN = "via Quick Capture"


def capture_context(service) -> Dict[str, object]:
    """People and boards the parser can match, from the database."""
    boards = [(b.id, b.name) for ws in service.get_workspaces() for b in ws.boards if not b.is_archived]
    return {"people": service.get_people(), "boards": boards, "me": service.actor}


def commit_capture(service, text: str, board_id: Optional[int] = None, source: str = "quick_capture",
                   now: Optional[datetime] = None, force_inbox: bool = False) -> Dict[str, object]:
    """Parse ``text`` and save it: a task on the target board (``board_id``, else ``>board`` in the
    text), otherwise an Inbox item. ``force_inbox`` ignores any ``>board`` (the user chose the Inbox).
    Returns {"kind": "task"|"inbox", "id", "title", "board_name", "result"}."""
    ctx = capture_context(service)
    result = parse_capture(text, now=now, **ctx)
    if not result.is_valid:
        raise ValueError("Type something to capture")
    target = None if force_inbox else (board_id if board_id is not None else result.board_id)
    if target is not None:
        column = service.default_capture_column(target)
        label_ids = []
        for name in result.labels:
            lbl = service.get_label_by_name(target, name, create_color="#64748B")
            if lbl is not None:
                label_ids.append(lbl.id)
        task = service.create_task(
            board_id=target, column_id=column, title=result.title, description=result.notes,
            priority=result.priority or Priority.MEDIUM.value, due_date=result.due, label_ids=label_ids,
            assignee=result.assignee, origin=CAPTURE_ORIGIN,
        )
        board = next((name for bid, name in ctx["boards"] if bid == target), "")
        return {"kind": "task", "id": task.id, "title": task.title, "board_name": board, "result": result}
    item = service.add_inbox_item(
        title=result.title, raw_text=text, notes=result.notes, priority=result.priority, due_date=result.due,
        labels=result.labels, assignee=result.assignee, source=source,
    )
    return {"kind": "inbox", "id": item["id"], "title": item["title"], "board_name": None, "result": result}

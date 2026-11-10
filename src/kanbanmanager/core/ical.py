"""
iCalendar (RFC 5545) reading and writing - no third-party dependencies.

* ``Component`` / ``Prop`` model VCALENDAR, VTODO, VEVENT, VALARM ... with
  properties kept in order, so a component read from a server can be written
  back with only the properties we manage changed (round-trip fidelity).
* ``parse`` handles line unfolding, quoted parameters, escaped text, CRLF or
  LF line ends and a byte-order mark; ``serialize`` folds lines at 75 octets
  (never inside a UTF-8 sequence) with CRLF line ends.
* Date helpers convert between iCalendar values (DATE, UTC, TZID and floating
  DATE-TIME) and the app's naive *local* datetimes.
* A compact RRULE implementation (FREQ, INTERVAL, COUNT, UNTIL, BYDAY incl.
  "2TU"/"-1FR", BYMONTHDAY, BYMONTH, EXDATE) expands recurring events and maps
  simple rules to and from the app's recurrence settings.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Tuple, Union

CRLF = "\r\n"
PRODID = "-//KanbanManager//KanbanManager 1.0//EN"
DateOrDateTime = Union[date, datetime]


class ICalError(ValueError):
    """Unreadable calendar data."""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
@dataclass
class Prop:
    name: str
    value: str                       # raw (still escaped) value text
    params: Dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The value as text (unescaped)."""
        return unescape_text(self.value)


@dataclass
class Component:
    name: str
    props: List[Prop] = field(default_factory=list)
    children: List["Component"] = field(default_factory=list)

    # Properties ------------------------------------------------------------
    def get(self, name: str) -> Optional[Prop]:
        name = name.upper()
        return next((p for p in self.props if p.name == name), None)

    def get_all(self, name: str) -> List[Prop]:
        name = name.upper()
        return [p for p in self.props if p.name == name]

    def text(self, name: str, default: Optional[str] = None) -> Optional[str]:
        p = self.get(name)
        return p.text if p is not None else default

    def set(self, name: str, value: str, params: Optional[Dict[str, str]] = None, escape: bool = False) -> None:
        """Replace (in place, keeping its position) or append a property."""
        name = name.upper()
        raw = escape_text(value) if escape else value
        existing = [i for i, p in enumerate(self.props) if p.name == name]
        if existing:
            self.props[existing[0]] = Prop(name, raw, dict(params or {}))
            for i in reversed(existing[1:]):
                del self.props[i]
        else:
            self.props.append(Prop(name, raw, dict(params or {})))

    def set_text(self, name: str, value: str, params: Optional[Dict[str, str]] = None) -> None:
        self.set(name, value, params, escape=True)

    def add(self, name: str, value: str, params: Optional[Dict[str, str]] = None, escape: bool = False) -> None:
        self.props.append(Prop(name.upper(), escape_text(value) if escape else value, dict(params or {})))

    def remove(self, name: str) -> None:
        name = name.upper()
        self.props = [p for p in self.props if p.name != name]

    # Children ------------------------------------------------------------------
    def walk(self, name: Optional[str] = None) -> Iterator["Component"]:
        for child in self.children:
            if name is None or child.name == name.upper():
                yield child
            yield from child.walk(name)

    def first(self, name: str) -> Optional["Component"]:
        return next(self.walk(name), None)


# ---------------------------------------------------------------------------
# Text escaping & folding
# ---------------------------------------------------------------------------
def escape_text(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,") \
        .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def unescape_text(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def split_list(value: str) -> List[str]:
    """A comma-separated TEXT list (CATEGORIES), honouring escaped commas."""
    items, cur, i = [], [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            cur.append(value[i:i + 2])
            i += 2
            continue
        if ch == ",":
            items.append(unescape_text("".join(cur)))
            cur = []
        else:
            cur.append(ch)
        i += 1
    items.append(unescape_text("".join(cur)))
    return [x.strip() for x in items if x.strip()]


def fold(line: str) -> str:
    """Fold a content line at 75 octets, never splitting a UTF-8 character."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    parts, start, limit = [], 0, 75
    while start < len(data):
        end = min(start + limit, len(data))
        while end < len(data) and (data[end] & 0xC0) == 0x80:  # don't cut a multi-byte sequence
            end -= 1
        parts.append(data[start:end].decode("utf-8"))
        start, limit = end, 74  # continuation lines start with a space
    return (CRLF + " ").join(parts)


def _param_value(value: str) -> str:
    return f'"{value}"' if any(c in value for c in ":;,") else value


def serialize(component: Component) -> str:
    lines = [f"BEGIN:{component.name}"]
    for p in component.props:
        params = "".join(f";{k}={_param_value(v)}" for k, v in p.params.items())
        lines.append(fold(f"{p.name}{params}:{p.value}"))
    for child in component.children:
        lines.append(serialize(child).rstrip(CRLF))
    lines.append(f"END:{component.name}")
    return CRLF.join(lines) + CRLF


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
_NAME = re.compile(r"[A-Za-z0-9-]+")


def _parse_line(line: str) -> Prop:
    m = _NAME.match(line)
    if not m:
        raise ICalError(f"Invalid line: {line[:40]!r}")
    name, i = m.group(0).upper(), m.end()
    params: Dict[str, str] = {}
    while i < len(line) and line[i] == ";":
        i += 1
        pm = _NAME.match(line, i)
        if not pm or pm.end() >= len(line) or line[pm.end()] != "=":
            raise ICalError(f"Invalid parameter in {line[:40]!r}")
        key, i = pm.group(0).upper(), pm.end() + 1
        values = []
        while True:
            if i < len(line) and line[i] == '"':
                end = line.find('"', i + 1)
                if end < 0:
                    raise ICalError(f"Unterminated quote in {line[:40]!r}")
                values.append(line[i + 1:end])
                i = end + 1
            else:
                j = i
                while j < len(line) and line[j] not in ";:,":
                    j += 1
                values.append(line[i:j])
                i = j
            if i < len(line) and line[i] == ",":
                i += 1
                continue
            break
        params[key] = ",".join(values)
    if i >= len(line) or line[i] != ":":
        raise ICalError(f"Missing ':' in {line[:40]!r}")
    return Prop(name, line[i + 1:], params)


def unfold_lines(text: str) -> List[str]:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines: List[str] = []
    for raw in text.split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def parse(text: str) -> List[Component]:
    """Top-level components (normally one VCALENDAR). Raises ICalError."""
    roots: List[Component] = []
    stack: List[Component] = []
    for line in unfold_lines(text):
        prop = _parse_line(line)
        if prop.name == "BEGIN":
            comp = Component(prop.value.strip().upper())
            (stack[-1].children if stack else roots).append(comp)
            stack.append(comp)
        elif prop.name == "END":
            name = prop.value.strip().upper()
            if not stack or stack[-1].name != name:
                raise ICalError(f"Unexpected END:{name}")
            stack.pop()
        elif stack:
            stack[-1].props.append(prop)
    if stack:
        raise ICalError(f"Missing END:{stack[-1].name}")
    if not roots:
        raise ICalError("No calendar data")
    return roots


def parse_calendar(text: str) -> Component:
    roots = parse(text)
    cal = next((r for r in roots if r.name == "VCALENDAR"), None)
    if cal is None:
        raise ICalError("Not an iCalendar file (no VCALENDAR)")
    return cal


# ---------------------------------------------------------------------------
# Dates, times, durations
# ---------------------------------------------------------------------------
_WINDOWS_ZONES = {  # the most common Outlook/Exchange zone names
    "Pacific Standard Time": "America/Los_Angeles", "Mountain Standard Time": "America/Denver",
    "Central Standard Time": "America/Chicago", "Eastern Standard Time": "America/New_York",
    "GMT Standard Time": "Europe/London", "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris", "Central Europe Standard Time": "Europe/Budapest",
    "E. Europe Standard Time": "Europe/Bucharest", "GTB Standard Time": "Europe/Bucharest",
    "India Standard Time": "Asia/Kolkata", "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo", "AUS Eastern Standard Time": "Australia/Sydney",
    "UTC": "UTC", "Coordinated Universal Time": "UTC",
}


def zone(tzid: Optional[str]):
    """A tzinfo for a TZID (IANA or common Windows names); None if unknown (treated as local)."""
    if not tzid:
        return None
    tzid = tzid.strip().strip('"')
    if tzid.upper() in ("UTC", "Z", "GMT", "ETC/UTC"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover
        return None
    for candidate in (tzid, _WINDOWS_ZONES.get(tzid), tzid.split("/", 1)[-1] if tzid.startswith("/") else None):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return None


def parse_date_value(value: str, params: Optional[Dict[str, str]] = None) -> DateOrDateTime:
    """DATE -> date; DATE-TIME -> aware datetime (UTC or TZID) or naive (floating / unknown TZID)."""
    value = (value or "").strip()
    params = params or {}
    try:
        if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
            return datetime.strptime(value[:8], "%Y%m%d").date()
        utc = value.endswith("Z")
        base = value.rstrip("Z")
        dt = datetime.strptime(base, "%Y%m%dT%H%M%S" if len(base) >= 15 else "%Y%m%dT%H%M")
    except ValueError:
        raise ICalError(f"Invalid date: {value!r}") from None
    if utc:
        return dt.replace(tzinfo=timezone.utc)
    tz = zone(params.get("TZID"))
    return dt.replace(tzinfo=tz) if tz is not None else dt


def prop_date(comp: Component, name: str) -> Optional[DateOrDateTime]:
    p = comp.get(name)
    if p is None or not p.value.strip():
        return None
    return parse_date_value(p.value.split(",")[0], p.params)


def to_local(value: DateOrDateTime) -> DateOrDateTime:
    """Aware datetimes -> naive local time (the app's convention); dates and naive stay as is."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def format_utc(dt: datetime) -> str:
    """A naive-local or aware datetime as an iCalendar UTC DATE-TIME."""
    aware = dt.astimezone() if dt.tzinfo is None else dt
    return aware.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def format_naive_utc(dt: datetime) -> str:
    """A naive *UTC* datetime (audit timestamps) as an iCalendar UTC DATE-TIME."""
    return dt.strftime("%Y%m%dT%H%M%SZ")


def format_date(d: date) -> str:
    return d.strftime("%Y%m%d")


_DUR = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(value: str) -> timedelta:
    m = _DUR.match((value or "").strip().upper())
    if not m or value.strip().upper() in ("P", "PT", "+P", "-P"):
        raise ICalError(f"Invalid duration: {value!r}")
    sign, w, d, h, mi, s = m.groups()
    td = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0))
    return -td if sign == "-" else td


def format_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    sign = "-" if secs < 0 else ""
    secs = abs(secs)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    out = f"{sign}P" + (f"{days}D" if days else "")
    if hours or minutes or seconds or not days:
        out += "T" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "") + \
            (f"{seconds}S" if seconds or not (hours or minutes) else "")
    return out


# ---------------------------------------------------------------------------
# Recurrence rules
# ---------------------------------------------------------------------------
WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def parse_rrule(value: str) -> Dict[str, str]:
    rule: Dict[str, str] = {}
    for part in (value or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            rule[k.strip().upper()] = v.strip().upper()
    if rule.get("FREQ") not in ("SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        raise ICalError(f"Invalid RRULE: {value!r}")
    return rule


def format_rrule(rule: Dict[str, str]) -> str:
    order = ["FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "COUNT", "UNTIL"]
    keys = [k for k in order if k in rule] + [k for k in rule if k not in order]
    return ";".join(f"{k}={rule[k]}" for k in keys)


def _byday(spec: str) -> List[Tuple[int, int]]:
    """"MO,2TU,-1FR" -> [(0, 0), (1, 2), (4, -1)] (weekday, ordinal; 0 = every)."""
    out = []
    for item in spec.split(","):
        m = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", item.strip())
        if m:
            out.append((WEEKDAYS.index(m.group(2)), int(m.group(1) or 0)))
    return out


def _add_months(d: date, months: int) -> Tuple[int, int]:
    total = d.year * 12 + (d.month - 1) + months
    return total // 12, total % 12 + 1


def _month_days(year: int, month: int, rule: Dict[str, str], dtstart: date) -> List[int]:
    last = calendar.monthrange(year, month)[1]
    days: List[int] = []
    if "BYMONTHDAY" in rule:
        for v in rule["BYMONTHDAY"].split(","):
            try:
                n = int(v)
            except ValueError:
                continue
            day = n if n > 0 else last + 1 + n
            if 1 <= day <= last:
                days.append(day)
    if "BYDAY" in rule:
        for wd, ordinal in _byday(rule["BYDAY"]):
            matches = [d for d in range(1, last + 1) if date(year, month, d).weekday() == wd]
            if ordinal == 0:
                days.extend(matches)
            elif -len(matches) <= ordinal <= len(matches) and ordinal != 0:
                days.append(matches[ordinal - 1] if ordinal > 0 else matches[ordinal])
    if not days:
        if dtstart.day <= last:
            days = [dtstart.day]
    return sorted(set(days))


def occurrences(dtstart: DateOrDateTime, rrule: Optional[str], window_start: DateOrDateTime,
                window_end: DateOrDateTime, exdates: Iterable[DateOrDateTime] = (), limit: int = 1000
                ) -> List[DateOrDateTime]:
    """Occurrence starts of a (possibly recurring) event within [window_start, window_end].

    ``dtstart`` and the window must be comparable (all dates, or all naive/aware datetimes)."""
    def key(v):
        return v if not isinstance(v, datetime) else v.replace(microsecond=0)

    excluded = {key(x) for x in exdates}
    if not rrule:
        return [dtstart] if window_start <= dtstart <= window_end and key(dtstart) not in excluded else []
    rule = parse_rrule(rrule)
    freq = rule["FREQ"]
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        u = parse_date_value(rule["UNTIL"])
        if isinstance(dtstart, datetime):
            if not isinstance(u, datetime):
                u = datetime.combine(u, time(23, 59, 59))
            if dtstart.tzinfo is not None and u.tzinfo is None:
                u = u.replace(tzinfo=dtstart.tzinfo)
            elif dtstart.tzinfo is None and u.tzinfo is not None:
                u = u.astimezone().replace(tzinfo=None)
        elif isinstance(u, datetime):
            u = u.date()
        until = u
    is_dt = isinstance(dtstart, datetime)
    start_day = dtstart.date() if is_dt else dtstart
    tod = dtstart.timetz() if is_dt else None

    def at(d: date) -> DateOrDateTime:
        return datetime.combine(d, tod) if is_dt else d

    def candidates() -> Iterator[date]:
        if freq == "DAILY":
            d = start_day
            while True:
                yield d
                d += timedelta(days=interval)
        elif freq == "WEEKLY":
            wds = sorted({wd for wd, _ in _byday(rule.get("BYDAY", ""))}) or [start_day.weekday()]
            week = start_day - timedelta(days=start_day.weekday())
            while True:
                for wd in wds:
                    d = week + timedelta(days=wd)
                    if d >= start_day:
                        yield d
                week += timedelta(weeks=interval)
        elif freq == "MONTHLY":
            n = 0
            while True:
                y, m = _add_months(start_day.replace(day=1), n)
                for day in _month_days(y, m, rule, start_day):
                    d = date(y, m, day)
                    if d >= start_day:
                        yield d
                n += interval
        elif freq == "YEARLY":
            months = [int(x) for x in rule.get("BYMONTH", "").split(",") if x.isdigit()] or [start_day.month]
            year = start_day.year
            while True:
                for m in sorted(months):
                    sub = dict(rule)
                    if "BYMONTHDAY" not in sub and "BYDAY" not in sub:
                        sub["BYMONTHDAY"] = str(start_day.day)
                    for day in _month_days(year, m, sub, start_day):
                        d = date(year, m, day)
                        if d >= start_day:
                            yield d
                year += interval
        else:  # sub-daily rules: treat as daily (good enough for tasks)
            d = start_day
            while True:
                yield d
                d += timedelta(days=1)

    out: List[DateOrDateTime] = []
    produced = 0
    for d in candidates():
        occ = at(d)
        if until is not None and occ > until:
            break
        if count is not None and produced >= count:
            break
        produced += 1
        if occ > window_end or produced > 100000:
            break
        if occ >= window_start and key(occ) not in excluded:
            out.append(occ)
            if len(out) >= limit:
                break
    return out


def recurrence_from_rrule(value: str) -> Optional[Dict[str, object]]:
    """The app's recurrence settings for a simple RRULE (None if it can't be expressed)."""
    try:
        rule = parse_rrule(value)
    except ICalError:
        return None
    freq, interval = rule["FREQ"], max(1, int(rule.get("INTERVAL", "1") or 1))
    out: Dict[str, object]
    if freq == "DAILY":
        out = {"frequency": "DAILY", "interval": interval} if interval == 1 else \
            {"frequency": "CUSTOM", "interval": interval, "custom_unit": "days"}
    elif freq == "WEEKLY":
        days = sorted({wd for wd, _ in _byday(rule.get("BYDAY", ""))})
        if days == [0, 1, 2, 3, 4] and interval == 1:
            out = {"frequency": "WEEKDAYS", "interval": 1}
        elif interval == 2 and len(days) <= 1:
            out = {"frequency": "BIWEEKLY", "interval": 1}
        elif interval == 1:
            out = {"frequency": "WEEKLY", "interval": 1}
            if days:
                out["days_of_week"] = days
        else:
            out = {"frequency": "CUSTOM", "interval": interval, "custom_unit": "weeks"}
    elif freq == "MONTHLY" and "BYDAY" not in rule:
        out = {"frequency": "MONTHLY", "interval": interval} if interval == 1 else \
            {"frequency": "CUSTOM", "interval": interval, "custom_unit": "months"}
    else:
        return None
    if rule.get("COUNT", "").isdigit():
        out.update({"end_type": "after_count", "max_occurrences": int(rule["COUNT"])})
    elif rule.get("UNTIL"):
        try:
            u = parse_date_value(rule["UNTIL"])
            out.update({"end_type": "end_date", "end_date": (u.date() if isinstance(u, datetime) else u).isoformat()})
        except ICalError:
            pass
    return out


def rrule_from_recurrence(rule: Dict[str, object]) -> Optional[str]:
    """An RRULE for the app's recurrence settings (None if not recurring)."""
    freq = str(rule.get("frequency") or "")
    interval = max(1, int(rule.get("interval") or 1))
    r: Dict[str, str]
    if freq == "DAILY":
        r = {"FREQ": "DAILY"}
    elif freq == "WEEKDAYS":
        r = {"FREQ": "WEEKLY", "BYDAY": "MO,TU,WE,TH,FR"}
    elif freq in ("WEEKLY", "BIWEEKLY"):
        r = {"FREQ": "WEEKLY"}
        if freq == "BIWEEKLY":
            r["INTERVAL"] = "2"
        days = rule.get("days_of_week")
        if isinstance(days, list) and days:
            r["BYDAY"] = ",".join(WEEKDAYS[int(d)] for d in sorted(set(days)) if 0 <= int(d) <= 6)
    elif freq == "MONTHLY":
        r = {"FREQ": "MONTHLY"}
    elif freq == "CUSTOM":
        unit = {"weeks": "WEEKLY", "months": "MONTHLY"}.get(str(rule.get("custom_unit") or "days"), "DAILY")
        r = {"FREQ": unit}
    else:
        return None
    if interval > 1 and "INTERVAL" not in r:
        r["INTERVAL"] = str(interval)
    if rule.get("end_type") == "after_count" and rule.get("max_occurrences"):
        remaining = int(rule["max_occurrences"]) - int(rule.get("occurrences_count") or 0)
        r["COUNT"] = str(max(1, remaining))
    elif rule.get("end_type") == "end_date" and rule.get("end_date"):
        try:
            r["UNTIL"] = format_date(date.fromisoformat(str(rule["end_date"])[:10]))
        except ValueError:
            pass
    return format_rrule(r)


# ---------------------------------------------------------------------------
# Building calendars
# ---------------------------------------------------------------------------
def new_calendar(name: str = "", refresh_minutes: Optional[int] = None) -> Component:
    cal = Component("VCALENDAR")
    cal.set("VERSION", "2.0")
    cal.set("PRODID", PRODID)
    cal.set("CALSCALE", "GREGORIAN")
    if name:
        cal.set_text("X-WR-CALNAME", name)
        cal.set_text("NAME", name)
    if refresh_minutes:
        cal.set("REFRESH-INTERVAL", format_duration(timedelta(minutes=refresh_minutes)), {"VALUE": "DURATION"})
        cal.set("X-PUBLISHED-TTL", format_duration(timedelta(minutes=refresh_minutes)))
    return cal

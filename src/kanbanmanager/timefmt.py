"""
Duration parsing & formatting (no dependencies, importable from any layer)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional


def local_to_utc(dt: datetime) -> datetime:
    """Naive local time -> naive UTC (the storage convention for time entries)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_local(dt: datetime) -> datetime:
    """Naive UTC -> naive local time (for display)."""
    return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def format_duration(seconds: float) -> str:
    """Human duration: '12s' (under a minute), '45m', '2h', '1h 30m'."""
    seconds = int(round(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes = (seconds + 30) // 60
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


def format_hours(hours: float) -> str:
    """Compact hours for planning views: '0h', '2.5h', '12h'."""
    hours = round(float(hours or 0.0), 1)
    return f"{hours:g}h"


_UNIT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?)\b", re.I)


def parse_duration(text: str) -> Optional[int]:
    """Parse user input into seconds. Accepted forms:

    '1h 30m', '1h30', '90m', '45 min', '1.5' / '1,5' (hours), '1:30' (h:mm), '2' (hours).
    Returns None for empty or unparseable input, or a non-positive duration.
    """
    raw = (text or "").strip().lower()
    if not raw:
        return None
    m = re.fullmatch(r"(\d+):([0-5]\d)", raw)
    if m:
        seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60
        return seconds or None
    m = re.fullmatch(r"\d+(?:[.,]\d+)?", raw)
    if m:
        seconds = round(float(raw.replace(",", ".")) * 3600)
        return seconds or None
    m = re.fullmatch(r"(\d+)\s*h\s*(\d+)", raw)  # "1h30"
    if m:
        seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60
        return seconds or None
    total = 0.0
    consumed = 0
    for part in _UNIT.finditer(raw):
        value = float(part.group(1).replace(",", "."))
        total += value * (3600 if part.group(2).startswith("h") else 60)
        consumed += len(part.group(0))
    leftover = _UNIT.sub("", raw).strip()
    if consumed == 0 or leftover:
        return None
    seconds = round(total)
    return seconds or None

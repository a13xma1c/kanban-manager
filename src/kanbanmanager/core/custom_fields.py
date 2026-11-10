"""
Custom Field Types (pure Python, no Qt)

Every board can define its own fields - "Story points", "Severity", "Customer",
"Spec link" ... Each field has a *type* from an extensible registry. A type
knows how to

* normalise its configuration (options, decimals, unit, rating scale ...),
* validate and canonicalise a value (``validate``) and parse one from text
  (``parse`` - used by automation, imports and the generic text editor),
* format a value for display and render compact chips for task cards,
* search, filter, compare (automation conditions) and total values,
* convert values from another type (when a field's type is changed, or a task
  moves to a board where the same-named field has a different type).

Values are plain JSON (str, int/float, bool, list of str); ``None`` means empty.

Adding a type::

    from kanbanmanager.core.custom_fields import FieldType, register_field_type

    class PhoneField(FieldType):
        key, label, icon = "phone", "Phone", "📞"
        def validate(self, value, config):
            text = self._text(value)
            if text and not re.fullmatch(r"[+\\d][\\d ()-]{5,}", text):
                raise FieldValueError("not a phone number")
            return text

    register_field_type(PhoneField())

The task editor falls back to a text box (``to_text`` / ``parse``) for types
without a dedicated Qt editor; see ``ui.components.field_editors`` to add one.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

MAX_FIELD_NAME = 60
OPTION_PALETTE = ["#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6", "#EC4899", "#06B6D4", "#64748B",
                  "#84CC16", "#F97316"]


class FieldValueError(ValueError):
    """A value that does not fit the field (the message is shown to the user)."""


@dataclass(frozen=True)
class Chip:
    """A compact rendering of a value for task cards."""
    text: str
    color: Optional[str] = None     # accent colour (select options), None = neutral
    tooltip: str = ""


# Comparison operators for automation conditions (op, label)
COMPARE_OPS: Tuple[Tuple[str, str], ...] = (
    ("is_set", "is set"), ("is_empty", "is empty"), ("is", "is"), ("is_not", "is not"),
    ("contains", "contains"), ("gt", "is greater than"), ("lt", "is less than"),
)
VALUELESS_OPS = ("is_set", "is_empty")


class FieldType:
    """Base class: a text-like type. Subclasses override what differs."""

    key: str = ""
    label: str = ""
    icon: str = "🧩"
    description: str = ""
    numeric: bool = False        # values are numbers: can be totalled and compared with > / <
    has_options: bool = False    # choice fields (select / multi-select) keep an option list in config
    editable: bool = True        # False for types whose implementation is missing (see UnknownFieldType)

    # -- configuration ------------------------------------------------------
    def default_config(self) -> Dict[str, Any]:
        return {}

    def normalize_config(self, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Known keys only, with defaults and sane bounds."""
        defaults = self.default_config()
        out = dict(defaults)
        for k, v in (config or {}).items():
            if k in defaults:
                out[k] = v
        return out

    def describe_config(self, config: Dict[str, Any]) -> str:
        """A short summary for lists ("Options: High, Low", "2 decimals, unit 'pts'")."""
        return ""

    # -- values ----------------------------------------------------------------
    @staticmethod
    def _text(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        text = str(value).strip()
        return text or None

    def validate(self, value: Any, config: Dict[str, Any]) -> Any:
        """Canonical JSON value (None = empty); raises FieldValueError."""
        return self._text(value)

    def parse(self, text: str, config: Dict[str, Any]) -> Any:
        """Value from user-typed text (empty text = empty value)."""
        return self.validate(text, config)

    def to_text(self, value: Any, config: Dict[str, Any]) -> str:
        """Editable text such that ``parse(to_text(v)) == v``."""
        return "" if value is None else str(value)

    def format(self, value: Any, config: Dict[str, Any]) -> str:
        """Display text ("" when empty)."""
        return self.to_text(value, config)

    def is_empty(self, value: Any) -> bool:
        return value is None or value == "" or value == []

    def sort_key(self, value: Any, config: Dict[str, Any]) -> Any:
        return (self.format(value, config) or "").lower()

    def chips(self, name: str, value: Any, config: Dict[str, Any]) -> List[Chip]:
        text = self.format(value, config)
        if not text:
            return []
        short = text if len(text) <= 28 else text[:27] + "…"
        return [Chip(f"{name}: {short}", None, f"{name}: {text}")]

    def matches_text(self, value: Any, needle: str, config: Dict[str, Any]) -> bool:
        return bool(needle) and needle.lower() in self.format(value, config).lower()

    # -- filters -----------------------------------------------------------------
    def filter_choices(self, config: Dict[str, Any], values_in_use: Sequence[Any] = ()) -> List[Tuple[str, str]]:
        """(key, label) pairs offered by the board's field filter."""
        return [("set", "has a value"), ("empty", "is empty")]

    def matches_filter(self, value: Any, key: str, config: Dict[str, Any]) -> bool:
        if key == "set":
            return not self.is_empty(value)
        if key == "empty":
            return self.is_empty(value)
        if key.startswith("eq:"):
            return not self.is_empty(value) and self.format(value, config).lower() == key[3:].lower()
        return False

    # -- automation conditions -------------------------------------------------
    def compare(self, value: Any, op: str, operand: str, config: Dict[str, Any]) -> bool:
        if op == "is_set":
            return not self.is_empty(value)
        if op == "is_empty":
            return self.is_empty(value)
        shown = self.format(value, config).lower()
        operand = (operand or "").strip()
        if op in ("is", "is_not"):
            try:
                target = self.parse(operand, config)
            except FieldValueError:
                target = operand
            same = value == target or shown == operand.lower()
            return same if op == "is" else not same
        if op == "contains":
            return bool(operand) and operand.lower() in shown
        return False

    # -- conversion ------------------------------------------------------------------
    def convert_from(self, value: Any, source: "FieldType", source_config: Dict[str, Any],
                     config: Dict[str, Any]) -> Any:
        """Value of another type in this type (raises FieldValueError when it doesn't fit)."""
        if source.key == self.key:
            return self.validate(value, config)
        return self.parse(source.to_text(value, source_config), config)

    # -- totals --------------------------------------------------------------------------
    def total(self, values: Iterable[Any], config: Dict[str, Any]) -> Optional[str]:
        """Formatted sum for column headers (numeric fields with ``show_total``), else None."""
        return None


# ---------------------------------------------------------------------------
# Built-in types
# ---------------------------------------------------------------------------
class TextField(FieldType):
    key, label, icon = "text", "Text", "🔤"
    description = "Free text - a name, version, reference number ..."

    def default_config(self):
        return {"multiline": False, "max_length": 0}

    def normalize_config(self, config):
        c = super().normalize_config(config)
        c["multiline"] = bool(c["multiline"])
        c["max_length"] = max(0, min(10000, int(c.get("max_length") or 0)))
        return c

    def describe_config(self, config):
        parts = ["multi-line" if config.get("multiline") else ""]
        if config.get("max_length"):
            parts.append(f"max {config['max_length']} characters")
        return ", ".join(p for p in parts if p)

    def validate(self, value, config):
        if value is None:
            return None
        text = str(value).strip() if not isinstance(value, (list, tuple)) else self._text(value)
        if not text:
            return None
        if not config.get("multiline"):
            text = " ".join(text.split())
        limit = int(config.get("max_length") or 0)
        if limit and len(text) > limit:
            raise FieldValueError(f"is longer than {limit} characters")
        return text


class NumberField(FieldType):
    key, label, icon = "number", "Number", "🔢"
    description = "A number, optionally with a unit - story points, size, count ..."
    numeric = True

    def default_config(self):
        return {"decimals": 0, "min": None, "max": None, "unit": "", "show_total": False}

    def normalize_config(self, config):
        c = super().normalize_config(config)
        c["decimals"] = max(0, min(6, int(c.get("decimals") or 0)))
        for k in ("min", "max"):
            v = c.get(k)
            c[k] = None if v in (None, "") else float(v)
            if c[k] is not None and c[k] == int(c[k]):
                c[k] = int(c[k])
        if c["min"] is not None and c["max"] is not None and c["min"] > c["max"]:
            c["min"], c["max"] = c["max"], c["min"]
        c["unit"] = str(c.get("unit") or "").strip()[:12]
        c["show_total"] = bool(c.get("show_total"))
        return c

    def describe_config(self, config):
        parts = []
        if config.get("decimals"):
            parts.append(f"{config['decimals']} decimals")
        if config.get("unit"):
            parts.append(f"unit '{config['unit']}'")
        lo, hi = config.get("min"), config.get("max")
        if lo is not None or hi is not None:
            parts.append(f"range {'' if lo is None else self._num(lo, config)}…{'' if hi is None else self._num(hi, config)}")
        if config.get("show_total"):
            parts.append("column totals")
        return ", ".join(parts)

    def _strip(self, text: str, config) -> str:
        unit = config.get("unit") or ""
        if unit and text.lower().endswith(unit.lower()):
            text = text[: -len(unit)]
        return text

    def _num(self, value: float, config) -> str:
        decimals = int(config.get("decimals") or 0)
        return f"{value:,.{decimals}f}"

    def validate(self, value, config):
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise FieldValueError("is not a number")
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            text = self._strip(str(value).strip(), config).replace(",", "").replace("_", "").replace(" ", "")
            if not text:
                return None
            try:
                number = float(text)
            except ValueError:
                raise FieldValueError(f"'{value}' is not a number") from None
        if math.isnan(number) or math.isinf(number):
            raise FieldValueError("is not a number")
        lo, hi = config.get("min"), config.get("max")
        if lo is not None and number < lo:
            raise FieldValueError(f"must be at least {self._num(lo, config)}")
        if hi is not None and number > hi:
            raise FieldValueError(f"must be at most {self._num(hi, config)}")
        decimals = int(config.get("decimals") or 0)
        number = round(number, decimals)
        return int(number) if decimals == 0 else number

    def to_text(self, value, config):
        if value is None:
            return ""
        decimals = int(config.get("decimals") or 0)
        return f"{value:.{decimals}f}"

    def format(self, value, config):
        if value is None:
            return ""
        unit = config.get("unit") or ""
        return self._num(float(value), config) + (f" {unit}" if unit else "")

    def sort_key(self, value, config):
        return float(value) if value is not None else float("-inf")

    def chips(self, name, value, config):
        text = self.format(value, config)
        if not text:
            return []
        return [Chip(text if config.get("unit") else f"{name}: {text}", None, f"{name}: {text}")]

    def compare(self, value, op, operand, config):
        if op in ("gt", "lt", "is", "is_not") and value is not None:
            try:
                target = self.validate(self._strip((operand or "").strip(), config), {**config, "min": None,
                                                                                       "max": None})
            except FieldValueError:
                return op == "is_not"
            if target is None:
                return op == "is_not"
            return {"gt": value > target, "lt": value < target, "is": value == target,
                    "is_not": value != target}[op]
        return super().compare(value, op, operand, config)

    def total(self, values, config):
        if not config.get("show_total"):
            return None
        nums = [float(v) for v in values if v is not None and not isinstance(v, bool)]
        return self.format(sum(nums), config) if nums else None


class CurrencyField(NumberField):
    key, label, icon = "currency", "Currency", "💰"
    description = "An amount of money - budget, cost, deal size ..."

    def default_config(self):
        return {"symbol": "$", "decimals": 2, "min": None, "max": None, "show_total": False}

    def normalize_config(self, config):
        c = FieldType.normalize_config(self, config)
        c["decimals"] = max(0, min(4, int(c["decimals"] if c.get("decimals") is not None else 2)))
        for k in ("min", "max"):
            v = c.get(k)
            c[k] = None if v in (None, "") else float(v)
        c["symbol"] = str(c.get("symbol") or "").strip()[:5]
        c["show_total"] = bool(c.get("show_total"))
        c["unit"] = ""
        return c

    def describe_config(self, config):
        return ", ".join(p for p in [f"symbol '{config.get('symbol')}'" if config.get("symbol") else "",
                                     "column totals" if config.get("show_total") else ""] if p)

    def _strip(self, text, config):
        symbol = config.get("symbol") or ""
        text = text.strip()
        neg = text.startswith("-")
        text = text.lstrip("-").strip()
        if symbol and text.startswith(symbol):
            text = text[len(symbol):]
        return ("-" if neg else "") + text.strip()

    def _num(self, value, config):
        decimals = int(config.get("decimals") or 0)
        symbol = config.get("symbol") or ""
        return f"{'-' if value < 0 else ''}{symbol}{abs(value):,.{decimals}f}"

    def format(self, value, config):
        return "" if value is None else self._num(float(value), config)

    def chips(self, name, value, config):
        text = self.format(value, config)
        return [Chip(f"{name}: {text}", None, f"{name}: {text}")] if text else []


class PercentField(NumberField):
    key, label, icon = "percent", "Percent", "％"
    description = "0-100 % - progress, confidence, probability ..."

    def default_config(self):
        return {"show_total": False}

    def normalize_config(self, config):
        return {"decimals": 0, "min": 0, "max": 100, "unit": "%", "show_total": False}

    def describe_config(self, config):
        return "0-100 %"

    def _strip(self, text, config):
        return text.strip().rstrip("%").strip()

    def format(self, value, config):
        return "" if value is None else f"{int(value)}%"

    def to_text(self, value, config):
        return "" if value is None else str(int(value))

    def chips(self, name, value, config):
        return [Chip(f"{name}: {int(value)}%", None, f"{name}: {int(value)}%")] if value is not None else []

    def total(self, values, config):
        return None


class RatingField(FieldType):
    key, label, icon = "rating", "Rating", "⭐"
    description = "1 to N stars - impact, confidence, interview score ..."
    numeric = True

    def default_config(self):
        return {"max": 5}

    def normalize_config(self, config):
        c = super().normalize_config(config)
        c["max"] = max(3, min(10, int(c.get("max") or 5)))
        return c

    def describe_config(self, config):
        return f"out of {config.get('max', 5)}"

    def validate(self, value, config):
        if value is None or value == "" or value == 0:
            return None
        if isinstance(value, bool):
            raise FieldValueError("is not a rating")
        if isinstance(value, (int, float)):
            n = value
        else:
            text = str(value).strip()
            if not text:
                return None
            if set(text) <= {"★", "☆", "⭐", " "}:
                n = text.count("★") + text.count("⭐")
            else:
                m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:/\s*\d+)?\s*(?:stars?)?", text.lower())
                if not m:
                    raise FieldValueError(f"'{value}' is not a rating")
                n = float(m.group(1))
            if n == 0:
                return None
        top = int(config.get("max", 5))
        if n != int(n) or not 1 <= n <= top:
            raise FieldValueError(f"must be a whole number from 1 to {top}")
        return int(n)

    def to_text(self, value, config):
        return "" if value is None else str(int(value))

    def format(self, value, config):
        if value is None:
            return ""
        top = int(config.get("max", 5))
        return "★" * int(value) + "☆" * (top - int(value))

    def sort_key(self, value, config):
        return int(value or 0)

    def chips(self, name, value, config):
        return [Chip("★" * int(value), "#FBBF24", f"{name}: {value}/{config.get('max', 5)}")] if value else []

    def filter_choices(self, config, values_in_use=()):
        top = int(config.get("max", 5))
        return super().filter_choices(config) + [(f"gte:{n}", f"{n}+ stars") for n in range(top, 0, -1)]

    def matches_filter(self, value, key, config):
        if key.startswith("gte:"):
            return value is not None and int(value) >= int(key[4:])
        return super().matches_filter(value, key, config)

    def compare(self, value, op, operand, config):
        if op in ("gt", "lt", "is", "is_not") and value is not None:
            try:
                target = self.validate(operand, config)
            except FieldValueError:
                return op == "is_not"
            target = target or 0
            return {"gt": value > target, "lt": value < target, "is": value == target,
                    "is_not": value != target}[op]
        return super().compare(value, op, operand, config)


_TRUE = {"yes", "y", "true", "1", "on", "checked", "✓", "✔", "x", "done"}
_FALSE = {"no", "n", "false", "0", "off", "unchecked", "-"}


class CheckboxField(FieldType):
    key, label, icon = "checkbox", "Checkbox", "☑"
    description = "Yes / no - reviewed, billable, customer-facing ..."

    def validate(self, value, config):
        """True when ticked; unticked is stored as "no value" (so an untouched box never shows up
        as a change, and a required checkbox means "must be ticked")."""
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return True if value else None
        if isinstance(value, (int, float)):
            return True if value else None
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return None
        raise FieldValueError(f"'{value}' is not yes or no")

    def is_empty(self, value):
        return value is not True

    def to_text(self, value, config):
        return "yes" if value is True else ""

    def format(self, value, config):
        return "Yes" if value is True else ""

    def chips(self, name, value, config):
        return [Chip(f"✓ {name}", "#10B981", f"{name}: yes")] if value is True else []

    def filter_choices(self, config, values_in_use=()):
        return [("true", "is checked"), ("false", "is not checked")]

    def matches_filter(self, value, key, config):
        if key == "true":
            return value is True
        if key == "false":
            return value is not True
        return super().matches_filter(value, key, config)

    def compare(self, value, op, operand, config):
        if op in ("is", "is_not"):
            try:
                target = bool(self.validate(operand, config))
            except FieldValueError:
                return op == "is_not"
            same = (value is True) == target
            return same if op == "is" else not same
        return super().compare(value, op, operand, config)


class DateField(FieldType):
    key, label, icon = "date", "Date", "📅"
    description = "A calendar date - release date, contract end, publish date ..."

    @staticmethod
    def _today() -> date:
        return date.today()

    def validate(self, value, config):
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value).strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            pass
        from kanbanmanager.core.capture import parse_date_text

        found = parse_date_text(text, self._today())
        if found is None:
            for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y"):
                try:
                    found = datetime.strptime(text, fmt).date()
                    break
                except ValueError:
                    continue
        if found is None:
            raise FieldValueError(f"'{value}' is not a date")
        return found.isoformat()

    def as_date(self, value) -> Optional[date]:
        try:
            return date.fromisoformat(value) if value else None
        except (TypeError, ValueError):
            return None

    def format(self, value, config):
        d = self.as_date(value)
        if d is None:
            return ""
        return d.strftime("%b %d") if d.year == self._today().year else d.strftime("%b %d, %Y")

    def sort_key(self, value, config):
        return value or ""

    def chips(self, name, value, config):
        text = self.format(value, config)
        return [Chip(f"📅 {name}: {text}", None, f"{name}: {value}")] if text else []

    def filter_choices(self, config, values_in_use=()):
        return super().filter_choices(config) + [("past", "is in the past"), ("next7", "is in the next 7 days")]

    def matches_filter(self, value, key, config):
        d = self.as_date(value)
        today = self._today()
        if key == "past":
            return d is not None and d < today
        if key == "next7":
            return d is not None and today <= d <= today + timedelta(days=7)
        return super().matches_filter(value, key, config)

    def compare(self, value, op, operand, config):
        if op in ("gt", "lt", "is", "is_not") and value is not None:
            try:
                target = self.validate(operand, config)
            except FieldValueError:
                return op == "is_not"
            if target is None:
                return op == "is_not"
            return {"gt": value > target, "lt": value < target, "is": value == target,
                    "is_not": value != target}[op]
        return super().compare(value, op, operand, config)


def _normalize_options(raw: Any) -> List[Dict[str, str]]:
    """Options with stable ids (used to follow renames), unique case-insensitive labels, colours."""
    out: List[Dict[str, str]] = []
    seen, used_ids = set(), set()
    for item in raw or []:
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get("label") or "").replace(",", " ").split())[:40]
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        oid = str(item.get("id") or "")
        if not oid or oid in used_ids:
            n = len(out) + 1
            while f"o{n}" in used_ids or any(str(i.get("id")) == f"o{n}" for i in raw if isinstance(i, dict)):
                n += 1
            oid = f"o{n}"
        used_ids.add(oid)
        color = str(item.get("color") or "")
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            color = OPTION_PALETTE[len(out) % len(OPTION_PALETTE)]
        out.append({"id": oid, "label": label, "color": color.upper()})
    return out


class SelectField(FieldType):
    key, label, icon = "select", "Dropdown", "🔽"
    description = "One choice from a list - severity, component, status, channel ..."
    has_options = True

    def default_config(self):
        return {"options": []}

    def normalize_config(self, config):
        return {"options": _normalize_options((config or {}).get("options"))}

    def describe_config(self, config):
        labels = [o["label"] for o in config.get("options", [])]
        return ("Options: " + ", ".join(labels)) if labels else "No options yet"

    def option(self, label: str, config) -> Optional[Dict[str, str]]:
        low = (label or "").strip().lower()
        return next((o for o in config.get("options", []) if o["label"].lower() == low), None)

    def _one(self, value, config) -> Optional[str]:
        text = self._text(value)
        if text is None:
            return None
        opt = self.option(text, config)
        if opt is None:
            choices = ", ".join(o["label"] for o in config.get("options", [])) or "none defined"
            raise FieldValueError(f"'{text}' is not an option ({choices})")
        return opt["label"]

    def validate(self, value, config):
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        return self._one(value, config)

    def sort_key(self, value, config):
        labels = [o["label"] for o in config.get("options", [])]
        return labels.index(value) if value in labels else len(labels)

    def chips(self, name, value, config):
        opt = self.option(value, config) if value else None
        return [Chip(opt["label"], opt["color"], f"{name}: {opt['label']}")] if opt else []

    def filter_choices(self, config, values_in_use=()):
        return [(f"eq:{o['label']}", f"is {o['label']}") for o in config.get("options", [])] + \
            super().filter_choices(config)

    def convert_from(self, value, source, source_config, config):
        if source.has_options and isinstance(value, list):
            value = value[0] if value else None
            return self.validate(value, config)
        return super().convert_from(value, source, source_config, config)


class MultiSelectField(SelectField):
    key, label, icon = "multi_select", "Multi-select", "🏷"
    description = "Several choices from a list - platforms, channels, skills ..."

    def validate(self, value, config):
        if value is None or value == "":
            return None
        items = value if isinstance(value, (list, tuple)) else str(value).split(",")
        chosen = {self._one(v, config) for v in items if self._text(v)}
        chosen.discard(None)
        order = [o["label"] for o in config.get("options", [])]
        result = [label for label in order if label in chosen]
        return result or None

    def to_text(self, value, config):
        return ", ".join(value or [])

    def is_empty(self, value):
        return not value

    def sort_key(self, value, config):
        return ", ".join(value or []).lower()

    def chips(self, name, value, config):
        out = []
        for label in value or []:
            opt = self.option(label, config)
            if opt:
                out.append(Chip(opt["label"], opt["color"], f"{name}: {', '.join(value)}"))
        return out

    def filter_choices(self, config, values_in_use=()):
        return [(f"has:{o['label']}", f"includes {o['label']}") for o in config.get("options", [])] + \
            FieldType.filter_choices(self, config)

    def matches_filter(self, value, key, config):
        if key.startswith("has:"):
            return key[4:].lower() in [v.lower() for v in value or []]
        return super().matches_filter(value, key, config)

    def compare(self, value, op, operand, config):
        if op in ("contains", "is", "is_not") and operand:
            wanted = (operand or "").strip().lower()
            has = wanted in [v.lower() for v in value or []]
            if op == "is":
                try:
                    return value == self.validate(operand, config)
                except FieldValueError:
                    return False
            if op == "is_not":
                try:
                    return value != self.validate(operand, config)
                except FieldValueError:
                    return True
            return has
        return super().compare(value, op, operand, config)

    def convert_from(self, value, source, source_config, config):
        if source.has_options:
            return self.validate(value if isinstance(value, list) else [value] if value else None, config)
        return super().convert_from(value, source, source_config, config)


class UrlField(FieldType):
    key, label, icon = "url", "Link", "🔗"
    description = "A web address - spec, design file, pull request, ticket ..."
    SCHEMES = ("http", "https", "ftp", "mailto", "file")

    def validate(self, value, config):
        text = self._text(value)
        if text is None:
            return None
        if " " in text:
            raise FieldValueError(f"'{text}' is not a link")
        if "://" not in text and not text.lower().startswith(("mailto:", "file:")):
            text = "https://" + text
        try:
            parsed = urlparse(text)
            parsed.port  # noqa: B018 - validates the port too ("host:99999" raises)
        except ValueError:  # e.g. an unbalanced "[" (IPv6 literal syntax)
            raise FieldValueError(f"'{value}' is not a link") from None
        if parsed.scheme.lower() not in self.SCHEMES:
            raise FieldValueError(f"'{text}' is not a link (use http or https)")
        if parsed.scheme.lower() in ("http", "https", "ftp") and (not parsed.netloc or "." not in parsed.netloc
                                                                 and parsed.netloc.split(":")[0] != "localhost"):
            raise FieldValueError(f"'{value}' is not a link")
        return text

    def chips(self, name, value, config):
        if not value:
            return []
        try:
            host = urlparse(value).netloc or value
        except ValueError:  # stored before validation was this strict
            host = value
        return [Chip(f"🔗 {host[4:] if host.startswith('www.') else host}", None, f"{name}: {value}")]


class EmailField(FieldType):
    key, label, icon = "email", "Email", "✉"
    description = "An email address - customer contact, candidate, vendor ..."

    def validate(self, value, config):
        text = self._text(value)
        if text is None:
            return None
        if text.lower().startswith("mailto:"):
            text = text[7:]
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
            raise FieldValueError(f"'{text}' is not an email address")
        local, domain = text.rsplit("@", 1)
        return f"{local}@{domain.lower()}"

    def chips(self, name, value, config):
        return [Chip(f"✉ {value}", None, f"{name}: {value}")] if value else []


class PersonField(FieldType):
    key, label, icon = "person", "Person", "👤"
    description = "Someone involved besides the assignee - reviewer, reporter, owner ..."

    def validate(self, value, config):
        text = self._text(value)
        return " ".join(text.split()) if text else None

    def chips(self, name, value, config):
        return [Chip(f"👤 {value}", None, f"{name}: {value}")] if value else []

    def filter_choices(self, config, values_in_use=()):
        people = sorted({v for v in values_in_use if v}, key=str.lower)
        return [(f"eq:{p}", f"is {p}") for p in people] + super().filter_choices(config)


class UnknownFieldType(FieldType):
    """Stands in for a type whose implementation is not installed (e.g. a removed plugin):
    values are kept untouched and shown as text, but can't be edited."""

    editable = False
    icon = "❔"

    def __init__(self, key: str):
        self.key = key
        self.label = f"Unknown ({key})"

    def normalize_config(self, config):
        return dict(config or {})

    def validate(self, value, config):
        return value

    def format(self, value, config):
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    def to_text(self, value, config):
        return self.format(value, config)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_REGISTRY: Dict[str, FieldType] = {}


def register_field_type(field_type: FieldType, replace: bool = False) -> FieldType:
    """Make a type available to every board. Keys are stored in the database: keep them stable."""
    if not field_type.key or not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", field_type.key):
        raise ValueError(f"invalid field type key {field_type.key!r} (lowercase letters, digits, _)")
    if field_type.key in _REGISTRY and not replace:
        raise ValueError(f"field type '{field_type.key}' is already registered")
    _REGISTRY[field_type.key] = field_type
    return field_type


def unregister_field_type(key: str) -> None:
    _REGISTRY.pop(key, None)


def get_field_type(key: str) -> FieldType:
    return _REGISTRY.get(key) or UnknownFieldType(key or "?")


def is_known_type(key: str) -> bool:
    return key in _REGISTRY


def field_types() -> List[FieldType]:
    """Registered types, in registration order."""
    return list(_REGISTRY.values())


for _t in (TextField(), NumberField(), CurrencyField(), PercentField(), SelectField(), MultiSelectField(),
           CheckboxField(), DateField(), RatingField(), PersonField(), UrlField(), EmailField()):
    register_field_type(_t)


# ---------------------------------------------------------------------------
# Helpers working on field definitions (dicts as returned by the service)
# ---------------------------------------------------------------------------
def normalize_name(name: Any) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        raise FieldValueError("A field needs a name")
    if len(text) > MAX_FIELD_NAME:
        raise FieldValueError(f"Field names are at most {MAX_FIELD_NAME} characters")
    return text


def type_of(field: Dict[str, Any]) -> FieldType:
    return get_field_type(field.get("field_type", ""))


def format_value(field: Dict[str, Any], value: Any) -> str:
    return type_of(field).format(value, field.get("config") or {})


def value_chips(field: Dict[str, Any], value: Any) -> List[Chip]:
    return type_of(field).chips(field.get("name", ""), value, field.get("config") or {})


def validate_value(field: Dict[str, Any], value: Any) -> Any:
    """Canonical value, raising FieldValueError with the field name in the message."""
    try:
        return type_of(field).validate(value, field.get("config") or {})
    except FieldValueError as exc:
        raise FieldValueError(f"{field.get('name', 'Field')}: {exc}") from None


def is_empty(field: Dict[str, Any], value: Any) -> bool:
    return type_of(field).is_empty(value)


def missing_required(fields: Sequence[Dict[str, Any]], values: Dict[int, Any]) -> List[str]:
    """Names of required fields without a value."""
    return [f["name"] for f in fields if f.get("required") and is_empty(f, values.get(f["id"]))]


def option_changes(old_config: Dict[str, Any], new_config: Dict[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    """For choice fields: ({old label: new label} renames, [removed labels]), matched by option id."""
    old = {o["id"]: o["label"] for o in (old_config or {}).get("options", [])}
    new = {o["id"]: o["label"] for o in (new_config or {}).get("options", [])}
    renamed = {old[i]: new[i] for i in old if i in new and old[i] != new[i]}
    removed = [old[i] for i in old if i not in new]
    return renamed, removed


def remap_choice_value(field_type: FieldType, value: Any, renamed: Dict[str, str], removed: Sequence[str]) -> Any:
    """A stored choice value after options were renamed/removed (None = nothing left)."""
    gone = {r.lower() for r in removed}
    if isinstance(value, list):
        out = [renamed.get(v, v) for v in value if v.lower() not in gone]
        return out or None
    if isinstance(value, str):
        if value.lower() in gone:
            return None
        return renamed.get(value, value)
    return value


def convert_value(value: Any, source: Dict[str, Any], target: Dict[str, Any]) -> Any:
    """``value`` of field ``source`` as a value of ``target`` (raises FieldValueError)."""
    src, dst = type_of(source), type_of(target)
    return dst.convert_from(value, src, source.get("config") or {}, target.get("config") or {})


def merge_options(config: Dict[str, Any], labels: Iterable[str], colors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Choice-field config with any missing ``labels`` appended as options."""
    options = list((config or {}).get("options", []))
    have = {o["label"].lower() for o in options}
    for label in labels:
        if label and label.lower() not in have:
            options.append({"label": label, "color": (colors or {}).get(label)})
            have.add(label.lower())
    return {**(config or {}), "options": _normalize_options(options)}


def column_total(field: Dict[str, Any], values: Iterable[Any]) -> Optional[str]:
    return type_of(field).total(values, field.get("config") or {})


def infer_type(values: Sequence[Any]) -> Tuple[str, Dict[str, Any]]:
    """A field type (and config) that fits legacy free-form values."""
    present = [v for v in values if v not in (None, "", [])]
    if present and all(isinstance(v, bool) for v in present):
        return "checkbox", {}
    if present and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        decimals = 0 if all(float(v) == int(v) for v in present) else 2
        return "number", NumberField().normalize_config({"decimals": decimals})
    if present and all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in present):
        labels = []
        for v in present:
            labels.extend(x for x in v if x not in labels)
        return "multi_select", MultiSelectField().normalize_config({"options": labels})
    return "text", TextField().normalize_config({"multiline": any(isinstance(v, str) and "\n" in v
                                                                  for v in present)})


def legacy_value(value: Any, field_type: str) -> Any:
    """A legacy free-form value coerced to the inferred type (non-strings become JSON text)."""
    if field_type == "text" and value is not None and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return value

"""
Property-based tests (Hypothesis) for the pure, logic-heavy modules.

Instead of hand-picked examples these state *laws* - round trips, invariants,
"never raises" contracts - and let Hypothesis search for counter-examples:

* iCalendar: text escaping, line folding, serialize/parse, durations, RRULE
  occurrences and the app-recurrence <-> RRULE mapping
* spreadsheets: CSV and XLSX round trips (with formula-injection guarding),
  the tolerant date / hours / priority parsers
* custom fields: the FieldType contract for every registered type
* scheduling: working-day calendar arithmetic, CPM and auto-scheduling
* theme colour maths, fuzzy matching, Markdown snippets, percentiles

Set ``HYPOTHESIS_PROFILE=thorough`` for many more examples per property.
"""
from __future__ import annotations

import math
import os
import string
from datetime import date, datetime, timedelta, timezone

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, example, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from kanbanmanager.core import custom_fields as cf  # noqa: E402
from kanbanmanager.core import exchange as ex  # noqa: E402
from kanbanmanager.core import ical  # noqa: E402
from kanbanmanager.core import scheduling as sch  # noqa: E402
from kanbanmanager.core.analytics import percentile  # noqa: E402
from kanbanmanager.core.markdown_utils import first_line_snippet, markdown_to_plain  # noqa: E402

settings.register_profile("default", max_examples=60, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
settings.register_profile("thorough", max_examples=1000, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

# Text as users type it: any printable unicode, plus the characters formats care about
texty = st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=120)
line_text = st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zl", "Zp")), max_size=200)
dates = st.dates(min_value=date(1990, 1, 1), max_value=date(2060, 12, 31))


# ---------------------------------------------------------------------------
# iCalendar
# ---------------------------------------------------------------------------
@given(texty)
def test_ical_text_escaping_round_trips(value):
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    assert ical.unescape_text(ical.escape_text(value)) == normalized
    escaped = ical.escape_text(value)
    assert "\n" not in escaped and "\r" not in escaped


@given(line_text)
@example("x" * 74 + "é" * 10)
@example("€" * 60)
def test_ical_folding_limits_octets_and_unfolds_to_the_original(value):
    line = "SUMMARY:" + value
    folded = ical.fold(line)
    physical = folded.split("\r\n")
    assert all(len(p.encode("utf-8")) <= 75 for p in physical), "RFC 5545: at most 75 octets per line"
    assert all(p.startswith(" ") for p in physical[1:])
    assert ical.unfold_lines(folded) == [line]


@given(st.lists(st.tuples(st.sampled_from(["SUMMARY", "DESCRIPTION", "LOCATION", "X-NOTE"]), texty), max_size=6),
       st.text(alphabet=string.ascii_letters + string.digits + "-@.", min_size=1, max_size=40))
def test_ical_serialize_then_parse_preserves_properties(props, uid):
    cal = ical.new_calendar("Board")
    todo = ical.Component("VTODO")
    todo.set("UID", uid)
    for name, value in props:
        todo.add(name, value, escape=True)
    cal.children.append(todo)
    parsed = ical.parse_calendar(ical.serialize(cal))
    back = parsed.first("VTODO")
    assert back.text("UID") == uid
    for (name, value), prop in zip(props, [p for p in back.props if p.name != "UID"]):
        assert prop.name == name
        assert prop.text == value.replace("\r\n", "\n").replace("\r", "\n")


@given(st.timedeltas(min_value=timedelta(days=-400), max_value=timedelta(days=400)))
def test_ical_durations_round_trip(td):
    td = timedelta(seconds=int(td.total_seconds()))
    assert ical.parse_duration(ical.format_duration(td)) == td


@given(st.text(max_size=20))
def test_ical_parse_duration_never_raises_anything_but_icalerror(text):
    try:
        assert isinstance(ical.parse_duration(text), timedelta)
    except ical.ICalError:
        pass


@given(dates, st.sampled_from(["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]), st.integers(1, 4),
       st.one_of(st.none(), st.integers(1, 30)),
       st.lists(st.sampled_from(ical.WEEKDAYS), max_size=4, unique=True),
       st.integers(10, 500))
def test_rrule_occurrences_are_ordered_bounded_and_respect_count(start, freq, interval, count, byday, span):
    rule = {"FREQ": freq, "INTERVAL": str(interval)}
    if byday and freq == "WEEKLY":
        rule["BYDAY"] = ",".join(byday)
    if count:
        rule["COUNT"] = str(count)
    end = start + timedelta(days=span)
    occ = ical.occurrences(start, ical.format_rrule(rule), start, end)
    assert occ == sorted(set(occ)), "strictly increasing"
    assert all(start <= d <= end for d in occ)
    if count:
        assert len(occ) <= count
    if freq == "WEEKLY" and byday:
        allowed = {ical.WEEKDAYS.index(d) for d in byday}
        assert all(d.weekday() in allowed for d in occ)
    if freq == "DAILY" and interval == 1 and not count:
        assert len(occ) == span + 1
    if occ:
        assert occ[0] >= start


recurrences = st.one_of(
    st.fixed_dictionaries({"frequency": st.just("DAILY"), "interval": st.just(1)}),
    st.fixed_dictionaries({"frequency": st.just("WEEKDAYS"), "interval": st.just(1)}),
    st.fixed_dictionaries({"frequency": st.just("WEEKLY"), "interval": st.just(1),
                           "days_of_week": st.lists(st.integers(0, 6), min_size=1, max_size=7, unique=True)
                           .map(sorted)}),
    st.fixed_dictionaries({"frequency": st.just("BIWEEKLY"), "interval": st.just(1)}),
    st.fixed_dictionaries({"frequency": st.just("MONTHLY"), "interval": st.just(1)}),
    st.fixed_dictionaries({"frequency": st.just("CUSTOM"), "interval": st.integers(2, 9),
                           "custom_unit": st.sampled_from(["days", "weeks", "months"])}),
)


@given(recurrences, st.one_of(st.none(), st.integers(1, 50), dates))
def test_app_recurrence_survives_the_rrule_round_trip(rule, end):
    rule = dict(rule)
    if isinstance(end, int):
        rule.update({"end_type": "after_count", "max_occurrences": end})
    elif isinstance(end, date):
        rule.update({"end_type": "end_date", "end_date": end.isoformat()})
    rrule = ical.rrule_from_recurrence(rule)
    assert rrule is not None
    back = ical.recurrence_from_rrule(rrule)
    assert back is not None
    # Equivalent rules may come back in their canonical spelling ("every 2 weeks" is BIWEEKLY,
    # Mon-Fri weekly is WEEKDAYS) - but they mean exactly the same RRULE
    assert ical.rrule_from_recurrence(back) == rrule, (rule, rrule, back)
    canonical = {("CUSTOM", "weeks", 2): "BIWEEKLY"}
    expected = canonical.get((rule["frequency"], rule.get("custom_unit"), rule["interval"]), rule["frequency"])
    if rule["frequency"] == "WEEKLY" and rule["days_of_week"] == [0, 1, 2, 3, 4]:
        expected = "WEEKDAYS"
    assert back["frequency"] == expected
    if back["frequency"] == rule["frequency"]:
        for key in ("interval", "custom_unit", "days_of_week"):
            assert back.get(key) == rule.get(key), (key, rule, rrule, back)
    for key in ("end_type", "max_occurrences", "end_date"):
        assert back.get(key) == rule.get(key), (key, rule, rrule, back)


# ---------------------------------------------------------------------------
# Spreadsheets
# ---------------------------------------------------------------------------
cell_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00\r"),
                    max_size=40)


@given(texty)
def test_safe_cell_neutralises_formulas_and_leaves_other_text_alone(value):
    out = ex.safe_cell(value)
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        assert out == "'" + value
    else:
        assert out == value


@given(st.lists(st.text(alphabet=string.ascii_letters + " ", min_size=1, max_size=12).map(str.strip)
                .filter(bool), min_size=1, max_size=5, unique=True),
       st.lists(st.lists(cell_text, min_size=5, max_size=5), min_size=1, max_size=8),
       st.sampled_from(["csv", "xlsx"]))
def test_our_own_exports_read_back_cell_for_cell(tmp_path_factory, headers, rows, kind):
    import re
    rows = [r[:len(headers)] for r in rows]
    path = tmp_path_factory.mktemp("x") / f"t.{kind}"
    (ex.write_csv if kind == "csv" else ex.write_xlsx)(headers, rows, path)
    table = ex.read_table(path)
    assert table.headers == headers
    if kind == "csv":
        expected = [[ex.safe_cell(c) for c in r] for r in rows]  # formulas are neutralised
    else:
        # XLSX cells are typed: text is never evaluated, so it needs no guard - but XML can't
        # carry most control characters
        expected = [[re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", c) for c in r] for r in rows]
    expected = [r for r in expected if any(c.strip() for c in r)]  # blank rows are skipped
    got = [r[:len(headers)] for r in table.rows]
    assert got == expected


@given(st.text(max_size=30))
@example("inf")
@example("1e999")
@example("nan")
def test_parse_hours_is_none_or_a_finite_non_negative_number(text):
    hours = ex.parse_hours(text)
    assert hours is None or (math.isfinite(hours) and hours >= 0), hours


@given(st.floats(0, 10_000, allow_nan=False).map(lambda f: round(f, 2)))
def test_parse_hours_reads_plain_numbers_and_units(h):
    assert ex.parse_hours(f"{h}") == pytest.approx(h)
    assert ex.parse_hours(f"{h}h") == pytest.approx(h)
    assert ex.parse_hours(f"{int(h)}h 30m") == pytest.approx(int(h) + 0.5)


@given(st.text(max_size=40))
@example("0001-01-01T00:00+01:00")
@example("9999-12-31T23:59-01:00")
@example("0001-01-01T00:30Z")
def test_parse_datetime_text_never_raises(text):
    dt, has_time = ex.parse_datetime_text(text)
    assert dt is None or isinstance(dt, datetime)
    assert isinstance(has_time, bool)


@given(st.datetimes(min_value=datetime(1971, 1, 2), max_value=datetime(2100, 1, 1)).map(
    lambda d: d.replace(second=0, microsecond=0)))
def test_parse_datetime_text_reads_iso_and_us_formats(dt):
    assert ex.parse_datetime_text(dt.strftime("%Y-%m-%d %H:%M")) == (dt, True)
    assert ex.parse_datetime_text(dt.strftime("%Y-%m-%d")) == (datetime.combine(dt.date(), datetime.min.time()),
                                                               False)
    assert ex.parse_datetime_text(dt.strftime("%m/%d/%Y"), order="mdy")[0].date() == dt.date()
    assert ex.parse_datetime_text(dt.strftime("%d/%m/%Y"), order="dmy")[0].date() == dt.date()
    assert ex.parse_datetime_text(dt.strftime("%b %d, %Y"))[0].date() == dt.date()


@given(st.text(max_size=15))
def test_parse_priority_returns_a_known_priority_or_none(text):
    from kanbanmanager.enums import Priority
    assert ex.parse_priority(text) in {None} | {p.value for p in Priority}


# ---------------------------------------------------------------------------
# Custom fields: the FieldType contract, for every registered type
# ---------------------------------------------------------------------------
raw_inputs = st.one_of(
    st.none(), st.booleans(), st.integers(-10**6, 10**6),
    st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False),
    st.text(max_size=25), st.lists(st.text(max_size=10), max_size=4),
    dates.map(lambda d: d.isoformat()),
    st.sampled_from(["Alpha", "Beta", "1", "0", "yes", "no", "https://example.com/x", "a@b.co", "3.5", "★★★", "50%"]),
)


def _configs(field_type):
    config = field_type.normalize_config({})
    if field_type.key in ("select", "multi_select"):
        config = field_type.normalize_config({"options": [{"label": "Alpha"}, {"label": "Beta"},
                                                          {"label": "Gamma, Delta"}]})
    return config


@pytest.mark.parametrize("type_key", [t.key for t in cf.field_types()])
@given(raw=raw_inputs)
def test_field_type_contract(type_key, raw):
    ftype = cf.get_field_type(type_key)
    config = _configs(ftype)
    try:
        value = ftype.validate(raw, config)
    except cf.FieldValueError:
        return
    # canonical values are stable, and their editable text parses back to them
    assert ftype.validate(value, config) == value
    text = ftype.to_text(value, config)
    assert isinstance(text, str)
    assert ftype.parse(text, config) == value, (raw, value, text)
    # displaying, sorting, chips, filters and conditions never raise on valid values
    assert isinstance(ftype.format(value, config), str)
    ftype.sort_key(value, config)
    for chip in ftype.chips("Field", value, config):
        assert chip.text
    for key, _label in ftype.filter_choices(config, [value]):
        ftype.matches_filter(value, key, config)
    for op in ("is", "is_not", "contains", "is_set", "is_empty", "gt", "lt", "before", "after"):
        ftype.compare(value, op, text, config)
    assert ftype.is_empty(value) == (value is None or value == "" or value == [])


@pytest.mark.parametrize("source_key", [t.key for t in cf.field_types()])
@given(raw=raw_inputs, target_key=st.sampled_from([t.key for t in cf.field_types()]))
def test_field_conversion_gives_a_valid_value_or_a_clean_error(source_key, raw, target_key):
    source, target = cf.get_field_type(source_key), cf.get_field_type(target_key)
    s_config, t_config = _configs(source), _configs(target)
    try:
        value = source.validate(raw, s_config)
    except cf.FieldValueError:
        return
    try:
        converted = target.convert_from(value, source, s_config, t_config)
    except cf.FieldValueError:
        return
    assert target.validate(converted, t_config) == converted


@given(st.lists(st.one_of(st.none(), st.integers(-1000, 1000), st.floats(-1e4, 1e4, allow_nan=False)), max_size=10))
def test_number_column_totals_sum_the_values(values):
    field = {"field_type": "number", "config": {"show_total": True, "decimals": 2}}
    total = cf.column_total(field, values)
    numbers = [v for v in values if v is not None]
    if total is not None:
        shown = float(total.split()[0].replace(",", ""))
        assert shown == pytest.approx(round(sum(numbers), 2), abs=0.011)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
working_sets = st.sets(st.integers(0, 6), min_size=1, max_size=7)


@given(working_sets, dates, dates)
def test_work_calendar_index_arithmetic(working, origin, d):
    cal = sch.WorkCalendar(working, origin)
    nxt = cal.next_working(d)
    assert cal.is_working(nxt) and nxt >= d and (nxt - d).days < 7
    i = cal.index_start(d)
    assert cal.day(i) == nxt
    assert cal.index_start(cal.day(i)) == i
    assert cal.add_working_days(d, 0) == nxt
    assert cal.span(d, d) == (1 if cal.is_working(d) else 0)
    assert cal.index_start(d + timedelta(days=1)) >= i, "monotonic"


@given(working_sets, dates, st.integers(-60, 60))
def test_adding_working_days_lands_on_working_days_and_counts_them(working, d, n):
    cal = sch.WorkCalendar(working, d)
    later = cal.add_working_days(d, n)
    assert cal.is_working(later)
    assert cal.index_start(later) - cal.index_start(d) == n


@st.composite
def networks(draw):
    n = draw(st.integers(1, 9))
    tasks = [sch.SchedTask(id=i + 1, title=f"T{i + 1}", duration=draw(st.integers(1, 5)), start=None, finish=None)
             for i in range(n)]
    deps = []
    for succ in range(2, n + 1):
        for pred in draw(st.sets(st.integers(1, succ - 1), max_size=3)):  # pred < succ: acyclic
            deps.append(sch.SchedDep(pred, succ, draw(st.sampled_from([sch.FS, sch.SS, sch.FF])),
                                     draw(st.integers(0, 3))))
    return tasks, deps


@given(networks())
def test_cpm_respects_every_dependency_and_float_is_never_negative(network):
    tasks, deps = network
    cal = sch.WorkCalendar(origin=date(2025, 1, 6))
    result = sch.analyze(tasks, deps, cal, default_start=date(2025, 1, 6))
    assert not result.cycle_ids and not result.conflicts
    for d in deps:
        p, s = result.tasks[d.pred], result.tasks[d.succ]
        assert s.es >= sch._required_start(d.kind, d.lag, p.es, p.ef, s.duration)
    for t in result.tasks.values():
        assert t.ef == t.es + t.duration
        assert t.total_float >= 0
    assert result.critical_ids, "a non-empty network has a critical path"
    path = result.critical_path()
    assert path and all(result.tasks[i].is_critical for i in path)


@given(networks(), st.data())
def test_auto_schedule_resolves_every_conflict(network, data):
    tasks, deps = network
    cal = sch.WorkCalendar(origin=date(2025, 1, 6))
    # give every task some (probably conflicting) planned dates first
    for t in tasks:
        start = cal.add_working_days(date(2025, 1, 6), data.draw(st.integers(0, 10)))
        t.start, t.finish = start, cal.add_working_days(start, t.duration - 1)
    proposal = sch.auto_schedule(tasks, deps, cal, project_start=date(2025, 1, 6))
    by_id = {t.id: t for t in tasks}
    for change in proposal.changes:
        by_id[change.task_id].start, by_id[change.task_id].finish = change.new_start, change.new_finish
    after = sch.analyze(list(by_id.values()), deps, cal, default_start=date(2025, 1, 6))
    assert after.conflicts == [], [c.describe({t.id: t.title for t in tasks}) for c in after.conflicts]
    assert not proposal.blocked


@given(st.lists(st.integers(1, 6), min_size=1, max_size=8))
def test_cycles_are_reported_not_looped_on(durations):
    tasks = [sch.SchedTask(id=i + 1, title=f"T{i}", duration=d, start=None, finish=None)
             for i, d in enumerate(durations)]
    n = len(tasks)
    deps = [sch.SchedDep(i, i % n + 1) for i in range(1, n + 1)]  # a ring
    result = sch.analyze(tasks, deps, sch.WorkCalendar(origin=date(2025, 1, 6)), default_start=date(2025, 1, 6))
    assert set(result.cycle_ids) == {t.id for t in tasks}


# ---------------------------------------------------------------------------
# Theme colour maths, fuzzy matching, Markdown, percentiles
# ---------------------------------------------------------------------------
hex_colors = st.integers(0, 0xFFFFFF).map(lambda n: f"#{n:06X}")


@given(hex_colors, hex_colors)
def test_contrast_ratio_is_symmetric_and_within_wcag_bounds(a, b):
    from kanbanmanager.ui import theme as th
    r = th.contrast_ratio(a, b)
    assert 1.0 <= r <= 21.0 + 1e-9
    assert r == pytest.approx(th.contrast_ratio(b, a))
    assert th.contrast_ratio(a, a) == pytest.approx(1.0)


@given(hex_colors, hex_colors, st.floats(0, 1))
def test_mix_stays_between_its_endpoints(a, b, t):
    from kanbanmanager.ui import theme as th
    m = th.mix(a, b, t)
    assert th.mix(a, b, 0).upper() == a and th.mix(a, b, 1).upper() == b
    for i in (1, 3, 5):
        lo, hi = sorted((int(a[i:i + 2], 16), int(b[i:i + 2], 16)))
        assert lo - 1 <= int(m[i:i + 2], 16) <= hi + 1


@given(st.dictionaries(st.sampled_from(["background", "surface", "text", "primary", "success", "warning", "danger",
                                        "info", "purple", "pink"]), hex_colors),
       st.floats(0.4, 1.0))
def test_any_custom_palette_produces_a_valid_colour_map(colors, contrast):
    from kanbanmanager.ui import theme as th
    mapping = th.build_map(colors, contrast)
    for src, dst in mapping.items():
        assert len(src) == 7 and src.startswith("#") and src == src.upper()
        assert len(dst) == 7 and dst.startswith("#")
        int(dst[1:], 16)


@given(st.text(alphabet=string.ascii_lowercase + " ", max_size=8), st.text(max_size=30))
def test_fuzzy_score_matches_exactly_the_in_order_subsequences(query, text):
    from kanbanmanager.ui.command_palette import fuzzy_score

    letters = [c for c in query.lower() if c != " "]
    it = iter(text.lower())
    is_subsequence = all(c in it for c in letters)
    score = fuzzy_score(query, text)
    assert (score is not None) == is_subsequence
    if query.strip() and is_subsequence:
        assert score > 0


@given(st.text(max_size=400), st.integers(4, 120))
def test_card_snippets_are_single_lines_within_the_limit(markdown, limit):
    snippet = first_line_snippet(markdown, limit)
    assert "\n" not in snippet and len(snippet) <= limit
    markdown_to_plain(markdown)  # never raises


@given(st.lists(st.floats(-1e6, 1e6, allow_nan=False), min_size=1, max_size=30), st.floats(0, 100),
       st.floats(0, 100))
def test_percentiles_are_bounded_and_monotonic(values, p, q):
    lo, hi = sorted((p, q))
    a, b = percentile(values, lo), percentile(values, hi)
    assert min(values) - 1e-6 <= a <= b + 1e-6 <= max(values) + 2e-6
    assert percentile(values, 0) == min(values) and percentile(values, 100) == max(values)


@given(st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2090, 1, 1),
                    timezones=st.just(timezone.utc)))
def test_utc_formatting_round_trips_through_the_parser(dt):
    dt = dt.replace(microsecond=0)
    assert ical.parse_date_value(ical.format_utc(dt)) == dt

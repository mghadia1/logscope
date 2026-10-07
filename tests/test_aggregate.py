"""Aggregations must agree with the full scan.

Over the same store as the search tests (overlapping segments, events in
memory, numbers written several ways), for random queries and time ranges:

- each histogram bucket holds exactly the events the scan finds in it;
- each top value's count is exactly what searching for that value finds,
  which is the promise behind grouping values the way queries match them;
- numeric stats equal those computed from the scanned events.
"""

import math
from collections.abc import Iterator

import pytest
from corpus import GRID, HOUR, T0, build_store, queries
from hypothesis import given, settings
from hypothesis import strategies as st

from logscope import aggregate
from logscope.query import And, EventView, ExistsTerm, FieldTerm, Node, parse
from logscope.store import Store
from logscope.terms import field_prefix, parse_number


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Store]:
    store = build_store(tmp_path_factory.mktemp("aggregate"))
    yield store
    store.abandon()


def scan(store: Store, query: Node | None, start: int, end: int) -> list[int]:
    """The times of every event the full scan finds."""
    events, _ = store.search(query, start, end, limit=10**9, use_index=False)
    return [e.time for e in events]


def scan_count(store: Store, query: Node | None, start: int, end: int) -> int:
    return len(scan(store, query, start, end))


def both(query: Node | None, term: Node) -> Node:
    return term if query is None else And((query, term))


ranges = st.tuples(st.integers(-2, 74), st.integers(1, 80)).map(
    lambda r: (T0 + r[0] * GRID, T0 + (r[0] + r[1]) * GRID)
)
FIELDS = ["level", "status", "attempt", "service", "http.method", "http.tags", "http"]


@settings(max_examples=200, deadline=None)
@given(
    text=queries,
    window=ranges,
    interval=st.sampled_from([GRID, 3 * GRID, HOUR, 7 * 60 * 1_000_000]),
)
def test_histogram_buckets_hold_what_the_scan_finds(
    store: Store, text: str, window: tuple[int, int], interval: int
) -> None:
    start, end = window
    query = parse(text)
    result = aggregate.histogram(store, query, start, end, interval)
    expected = [0] * len(result.counts)
    for time in scan(store, query, start, end):
        expected[(time - result.first) // interval] += 1
    assert result.counts == expected


@settings(max_examples=150, deadline=None)
@given(text=queries, window=ranges, field=st.sampled_from(FIELDS))
def test_each_top_value_counts_what_searching_for_it_finds(
    store: Store, text: str, window: tuple[int, int], field: str
) -> None:
    start, end = window
    query = parse(text)
    result = aggregate.top_values(store, query, start, end, field, size=1000)
    assert result.matched == scan_count(store, query, start, end)
    assert result.with_field == scan_count(
        store, both(query, ExistsTerm(field)), start, end
    )
    assert result.distinct == len(result.values)
    for label, count in result.values:
        found = scan_count(store, both(query, FieldTerm(field, label)), start, end)
        assert count == found, f"{field}:{label}"


@settings(max_examples=150, deadline=None)
@given(text=queries, window=ranges, field=st.sampled_from(["status", "attempt"]))
def test_numeric_stats_equal_those_of_the_scanned_events(
    store: Store, text: str, window: tuple[int, int], field: str
) -> None:
    start, end = window
    query = parse(text)
    result = aggregate.numbers(store, query, start, end, field)
    events, _ = store.search(query, start, end, limit=10**9, use_index=False)
    values: list[int | float] = []
    prefix = field_prefix(field)
    for event in events:
        # Each distinct number an event holds counts once ("503.0" and 503.0
        # in one event would be one value).
        numbers = {
            parse_number(term[len(prefix) :])
            for term in EventView(event.data).terms
            if term.startswith(prefix)
        }
        values += [n for n in numbers if n is not None]
    assert result.matched == len(events)
    assert result.count == len(values)
    if not values:
        assert (result.min, result.max, result.mean) == (None, None, None)
        return
    ordered = sorted(values)
    assert (result.min, result.max) == (ordered[0], ordered[-1])
    assert result.mean == pytest.approx(math.fsum(values) / len(values))
    for name, q in (("p50", 0.5), ("p90", 0.9), ("p95", 0.95), ("p99", 0.99)):
        assert (
            result.percentiles[name] == ordered[max(1, math.ceil(q * len(ordered))) - 1]
        )


@settings(max_examples=150, deadline=None)
@given(text=queries, window=ranges)
def test_each_field_counts_the_events_that_have_it(
    store: Store, text: str, window: tuple[int, int]
) -> None:
    start, end = window
    query = parse(text)
    result = aggregate.fields(store, query, start, end, size=1000)
    assert result.matched == scan_count(store, query, start, end)
    for field, count in result.fields:
        assert count == scan_count(store, both(query, ExistsTerm(field)), start, end)
    if result.matched:
        # Every event has at least level, service and msg.
        assert {"level", "service", "msg"} <= {f for f, _ in result.fields}


def test_histogram_without_a_query_counts_by_bisection(store: Store) -> None:
    # Whole time windows of a segment are counted from its sorted times
    # alone: no event is read.
    result = aggregate.histogram(store, None, T0, T0 + 6 * HOUR, HOUR)
    assert result.stats.events_read == store.stats()["events_in_memory"]
    assert sum(result.counts) == scan_count(store, None, T0, T0 + 6 * HOUR)


def test_top_values_group_numbers_by_value(store: Store) -> None:
    result = aggregate.top_values(store, None, 0, 2**62, "status", size=100)
    labels = [label for label, _ in result.values]
    # 503, 503.0 and "503.0" are one value; 200 and "200" are one.
    assert sorted(labels) == ["200", "404", "500", "503", "null"]


def test_too_many_buckets_is_refused(store: Store) -> None:
    with pytest.raises(ValueError, match="buckets"):
        aggregate.histogram(store, None, T0, T0 + 6 * HOUR, 1_000_000)


def test_auto_interval_picks_a_nice_width() -> None:
    assert aggregate.auto_interval(0, HOUR) == 60 * 1_000_000  # 60 buckets
    assert aggregate.auto_interval(0, 24 * HOUR) == 30 * 60 * 1_000_000

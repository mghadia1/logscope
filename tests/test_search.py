"""The index must give exactly what the full scan gives.

A store is built with events spread over six hours, flushed several times
(late events make segments overlap in time) with some left in memory; then
Hypothesis tries hundreds of queries, time ranges, limits and cursors, and
every one must return the same ids, in the same order, with the index as
without it.
"""

import json
import random
from collections.abc import Iterator
from pathlib import Path

import pytest
from corpus import HOUR, SECOND, T0, build_store, make_line, queries
from hypothesis import example, given, settings
from hypothesis import strategies as st

from logscope.events import ParsedLine
from logscope.query import parse
from logscope.store import Store


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Store]:
    store = build_store(tmp_path_factory.mktemp("search"))
    yield store
    store.abandon()


boundaries = st.sampled_from([T0 + k * 15 * 60 * SECOND for k in range(-1, 26)])


# Shapes where an inexact part (NOT around a phrase) sits inside OR, AND or
# another NOT, every time, whatever Hypothesis happens to generate.
INEXACT_SHAPES = [
    'NOT "upstream timeout" OR level:info',
    'level:info OR NOT "upstream timeout"',
    '(NOT abc-123) OR (NOT "timeout upstream")',
    'NOT (NOT "upstream timeout")',
    '"upstream timeout" OR NOT status:*',
    'level:error (NOT "job done retry" OR http:*)',
]


@settings(max_examples=600, deadline=None)
@given(
    text=queries,
    start=boundaries,
    span=st.integers(1, 26),
    limit=st.integers(1, 60),
    cursor_pick=st.one_of(st.none(), st.integers(0, 10_000)),
)
@example(text=INEXACT_SHAPES[0], start=T0, span=26, limit=60, cursor_pick=None)
@example(text=INEXACT_SHAPES[1], start=T0, span=26, limit=60, cursor_pick=None)
@example(text=INEXACT_SHAPES[2], start=T0, span=26, limit=60, cursor_pick=None)
@example(text=INEXACT_SHAPES[3], start=T0, span=26, limit=60, cursor_pick=None)
@example(text=INEXACT_SHAPES[4], start=T0, span=26, limit=60, cursor_pick=None)
@example(text=INEXACT_SHAPES[5], start=T0, span=26, limit=60, cursor_pick=None)
def test_the_index_returns_exactly_what_the_scan_returns(
    store: Store, text: str, start: int, span: int, limit: int, cursor_pick: int | None
) -> None:
    end = start + span * 15 * 60 * SECOND
    before = None
    if cursor_pick is not None:
        everything, _ = store.search(limit=10_000)
        chosen = everything[cursor_pick % len(everything)]
        before = (chosen.time, chosen.id)
    query = parse(text)
    indexed, _ = store.search(query, start, end, limit, before)
    scanned, _ = store.search(query, start, end, limit, before, use_index=False)
    assert [e.id for e in indexed] == [e.id for e in scanned]
    assert [e.data for e in indexed] == [e.data for e in scanned]


def test_the_test_store_has_overlapping_segments_and_memory(store: Store) -> None:
    stats = store.stats()
    assert stats["segments"] > 6 and stats["events_in_memory"] > 0
    segments = store._segments
    overlapping = sum(
        1
        for a in segments
        for b in segments
        if a is not b and a.min_time < b.max_time and b.min_time < a.max_time
    )
    assert overlapping > 0  # late events: what makes early stopping subtle


@pytest.fixture
def day(tmp_path: Path) -> Iterator[Store]:
    """24 hourly segments of 100 events; "needle" is in 3 events only."""
    store = Store(tmp_path)
    store.open()
    rng = random.Random(1)
    for hour in range(24):
        lines = [make_line(rng, T0 + hour * HOUR + i * SECOND) for i in range(100)]
        store.append(lines)
    for i, hour in enumerate([2, 9, 20]):
        data = {"msg": f"needle {i}"}
        store.append(
            [
                ParsedLine(
                    T0 + hour * HOUR + 50 * SECOND, data, json.dumps(data).encode()
                )
            ]
        )
    store.flush()
    yield store
    store.abandon()


def test_newest_events_read_one_segment_and_only_the_rows_returned(day: Store) -> None:
    events, stats = day.search(limit=10)
    assert len(events) == 10
    assert (stats.segments_read, stats.segments_skipped) == (1, 23)
    assert stats.events_read == 10  # the times come from the index


def test_a_time_range_skips_the_segments_outside_it(day: Store) -> None:
    events, stats = day.search(start=T0 + 5 * HOUR, end=T0 + 6 * HOUR, limit=500)
    assert len(events) == 100
    assert stats.segments_read == 1
    assert stats.segments_skipped == 23


def test_a_rare_word_reads_only_its_rows(day: Store) -> None:
    events, stats = day.search(parse("needle"), limit=100)
    assert [e.data["msg"] for e in events] == ["needle 2", "needle 1", "needle 0"]
    assert stats.events_read == 3
    assert stats.candidates == 3


def test_the_scan_reads_everything(day: Store) -> None:
    events, stats = day.search(parse("needle"), limit=100, use_index=False)
    assert len(events) == 3
    assert stats.events_read == 2403


def test_indexes_survive_a_restart_and_missing_ones_are_rebuilt(day: Store) -> None:
    first = day._segments[0]
    (day.data_dir / first.index_path).unlink()  # as a Milestone 1 segment would be
    day.abandon()
    fresh = Store(day.data_dir)
    try:
        assert fresh.open().indexed == 1
        events, stats = fresh.search(parse("needle"), limit=100)
        assert len(events) == 3 and stats.events_read == 3
    finally:
        fresh.abandon()

"""Aggregations over the events a query matches, computed from the index:
a time histogram, a field's top values, and a field's numeric statistics.

Each runs ``Store.visit_matches``, which hands over, per source (the
memtable, then each segment), exactly the rows that match. From there:

- the histogram reads only row times; when the rows are a whole time window
  of a segment, each bucket is two binary searches over its sorted times;
- top values intersect the field's postings with the matching rows;
- numeric stats count the field's indexed numbers among the matching rows.

No event is read for these, except to check candidates the index cannot
decide alone (phrases). Values are grouped the way queries match them, so
a bucket labelled ``503`` counts exactly the events ``status:503`` finds.
"""

import math
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from .index import SegmentIndex, Source
from .query import Node
from .store import Rows, SearchStats, Store
from .terms import canonical_number, exists_key, field_prefix, parse_number

SECOND = 1_000_000
# Bucket widths an automatic interval is chosen from.
NICE_INTERVALS = [
    s * SECOND
    for s in (1, 5, 10, 30, 60, 300, 600, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600)
] + [86400 * SECOND * d for d in (1, 7, 30)]
MAX_BUCKETS = 2000


@dataclass(frozen=True, slots=True)
class Histogram:
    first: int  # the first bucket's start
    interval: int  # microseconds
    counts: list[int]
    stats: SearchStats


@dataclass(frozen=True, slots=True)
class TopValues:
    matched: int  # events matching the query
    with_field: int  # ... that have the field
    distinct: int  # distinct values among them
    values: list[tuple[str, int]]  # the top ones, most frequent first
    stats: SearchStats


@dataclass(frozen=True, slots=True)
class Numbers:
    matched: int
    count: int  # numeric values (an array of numbers gives several)
    min: float | int | None
    max: float | int | None
    mean: float | None
    percentiles: dict[str, float | int]  # nearest rank: p50, p90, p95, p99
    stats: SearchStats


def auto_interval(start: int, end: int, target: int = 60) -> int:
    """The smallest nice interval giving at most ``target`` buckets."""
    for interval in NICE_INTERVALS:
        if (end - start) / interval <= target:
            return interval
    return NICE_INTERVALS[-1]


def histogram(
    store: Store, query: Node | None, start: int, end: int, interval: int
) -> Histogram:
    first = start - start % interval  # buckets align to the interval
    buckets = math.ceil((end - first) / interval)
    if buckets > MAX_BUCKETS:
        raise ValueError(
            f"{buckets} buckets; at most {MAX_BUCKETS} (choose a longer interval)"
        )
    counts = [0] * buckets

    def visit(source: Source, rows: Rows) -> None:
        times = source.times
        if isinstance(rows, range) and isinstance(source, SegmentIndex):
            if not rows:
                return
            # Sorted times: count each bucket by bisection, not row by row.
            low_bucket = (times[rows.start] - first) // interval
            high_bucket = (times[rows.stop - 1] - first) // interval
            for bucket in range(low_bucket, high_bucket + 1):
                bucket_start = first + bucket * interval
                lo = bisect_left(times, bucket_start, rows.start, rows.stop)
                hi = bisect_left(times, bucket_start + interval, rows.start, rows.stop)
                counts[bucket] += hi - lo
        else:
            for row in rows:
                counts[(times[row] - first) // interval] += 1

    stats = store.visit_matches(query, start, end, visit)
    return Histogram(first, interval, counts, stats)


def _count(postings: Sequence[int], rows: Rows) -> int:
    """How many matching rows a postings list holds."""
    if isinstance(rows, range):  # postings are ascending: bisect
        return bisect_left(postings, rows.stop) - bisect_left(postings, rows.start)
    return sum(1 for row in postings if row in rows)


def _values(source: Source, field: str) -> Iterator[tuple[str, Sequence[int]]]:
    """A field's values with their rows, one entry per value as queries
    match it: numbers by value, other values as indexed (lowercased).

    A number has several index keys ("503.0" the string has "503.0" and
    "503"), but every row holding that number has its canonical key ("503"),
    so that key's rows are the whole group, and the other spellings are
    skipped rather than merged.
    """
    prefix = field_prefix(field)
    for term, postings in source.with_prefix(prefix):
        value = term[len(prefix) :]
        number = parse_number(value)
        if number is None or canonical_number(number) == value:
            yield value, postings


def top_values(
    store: Store, query: Node | None, start: int, end: int, field: str, size: int
) -> TopValues:
    counts: Counter[str] = Counter()
    totals = {"matched": 0, "with_field": 0}

    def visit(source: Source, rows: Rows) -> None:
        totals["matched"] += len(rows)
        totals["with_field"] += _count(source.lookup(exists_key(field)), rows)
        for value, postings in _values(source, field):
            found = _count(postings, rows)
            if found:
                counts[value] += found

    stats = store.visit_matches(query, start, end, visit)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return TopValues(
        totals["matched"], totals["with_field"], len(counts), ordered[:size], stats
    )


def numbers(
    store: Store, query: Node | None, start: int, end: int, field: str
) -> Numbers:
    values: Counter[int | float] = Counter()
    matched = 0

    def visit(source: Source, rows: Rows) -> None:
        nonlocal matched
        matched += len(rows)
        for value, postings in _values(source, field):
            number = parse_number(value)
            if number is not None:
                found = _count(postings, rows)
                if found:
                    values[number] += found

    stats = store.visit_matches(query, start, end, visit)
    count = sum(values.values())
    if not count:
        return Numbers(matched, 0, None, None, None, {}, stats)
    ordered = sorted(values.items())
    total = math.fsum(number * times for number, times in ordered)
    percentiles = {}
    for name, q in (("p50", 0.5), ("p90", 0.9), ("p95", 0.95), ("p99", 0.99)):
        rank = max(1, math.ceil(q * count))  # nearest rank
        seen = 0
        for number, times in ordered:
            seen += times
            if seen >= rank:
                percentiles[name] = number
                break
    return Numbers(
        matched, count, ordered[0][0], ordered[-1][0], total / count, percentiles, stats
    )

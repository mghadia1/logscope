"""Test data shared by the search and aggregation tests: events shaped like
SurgiServe's and Gatekeeper's logs, with the awkward cases on purpose
(numbers written several ways, nested objects, arrays, a 5-minute time grid
so times tie and range boundaries fall on events), in a store whose segments
overlap in time, with some events still in memory.
"""

import json
import random
from pathlib import Path

from hypothesis import strategies as st

from logscope.events import ParsedLine, parse_time
from logscope.store import Store

T0 = parse_time("2026-10-06T18:00:00Z", "t")
SECOND = 1_000_000
HOUR = 3600 * SECOND
GRID = 5 * 60 * SECOND

LEVELS = ["info", "INFO", "warn", "error", "ERROR"]
SERVICES = ["gatekeeper", "surgiserve", "logscope"]
# "503.0" the string and 503 the number are both matched by status:503.0,
# a field term with two keys (a union).
STATUSES: list[object] = [200, "200", 404, 500, 503, 503.0, "503.0", None]
WORDS = ["upstream", "timeout", "job", "done", "retry", "abc-123", "Café"]


def make_line(rng: random.Random, time: int) -> ParsedLine:
    data: dict[str, object] = {
        "level": rng.choice(LEVELS),
        "service": rng.choice(SERVICES),
        "msg": " ".join(rng.sample(WORDS, rng.randint(1, 3))),
    }
    if rng.random() < 0.7:
        data["status"] = rng.choice(STATUSES)
    if rng.random() < 0.5:
        data["attempt"] = rng.choice([0, 1, 2, 3, "2", 2.5])  # "2": a number too
    if rng.random() < 0.3:
        data["http"] = {
            "method": rng.choice(["GET", "post"]),
            "tags": rng.sample(["a", "b", "c"], rng.randint(0, 2)),
        }
    return ParsedLine(time, data, json.dumps(data).encode())


def build_store(directory: Path) -> Store:
    """Six hours of events in five rounds, four of them flushed."""
    rng = random.Random(42)
    store = Store(directory)
    store.open()
    for flush in range(5):
        for _ in range(8):
            # On a 5-minute grid: many events share a time (the id breaks
            # the tie, also across segments) and the quarter-hour range
            # boundaries fall exactly on events. Any of the six hours, so
            # later flushes overlap earlier ones.
            times = [T0 + rng.randrange(72) * GRID for _ in range(rng.randint(1, 30))]
            store.append([make_line(rng, t) for t in times])
        if flush < 4:
            store.flush()  # the last round stays in the memtable
    return store


TERMS = [
    "level:info",
    "level:error",
    "level:warn",
    "service:gatekeeper",
    "status:200",
    "status:503",
    "status:503.0",
    "status:2e2",
    "status:null",
    "status:*",
    "status>=500",
    "status<404",
    "status<=200",
    "attempt>1",
    "attempt<=0",
    "http:*",
    "http.method:get",
    "http.tags:a",
    "upstream",
    "timeout",
    "abc-123",  # a phrase: abc then 123
    "café",
    "missing",
    '"upstream timeout"',  # sometimes adjacent, sometimes not, or reversed
    '"timeout upstream"',
    '"job done retry"',
]
PHRASES = ['"upstream timeout"', '"timeout upstream"', '"job done retry"', "abc-123"]
# Random trees of AND, OR, NOT and parentheses over those terms: whatever
# the shape, the index (with the check of inexact candidates) must agree
# with the scan.
queries = st.one_of(
    st.just(""),
    st.recursive(
        # Phrases are half the terms: they are the inexact case, where the
        # index's candidates must be checked against the events.
        st.one_of(st.sampled_from(TERMS), st.sampled_from(PHRASES)),
        lambda inner: st.one_of(
            st.lists(inner, min_size=2, max_size=3).map(lambda qs: f"({' '.join(qs)})"),
            st.lists(inner, min_size=2, max_size=3).map(
                lambda qs: f"({' OR '.join(qs)})"
            ),
            inner.map(lambda q: f"NOT {q}"),
        ),
        max_leaves=5,
    ),
)

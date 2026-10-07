import random
from pathlib import Path
from typing import Any

import pytest

from logscope import index
from logscope.events import Event
from logscope.query import parse
from logscope.terms import field_key, field_prefix, terms_of


def write(
    tmp_path: Path, rows: list[tuple[int, int, int, set[str]]]
) -> index.SegmentIndex:
    path = tmp_path / "segment.idx"
    path.write_bytes(index.build(rows))
    return index.SegmentIndex(path)


def test_rows_and_postings_round_trip(tmp_path: Path) -> None:
    rows = [
        (100, 7, 0, {"t\0a", "t\0b"}),
        (200, 9, 40, {"t\0b"}),
        (200, 12, 75, {"t\0a", "t\0c"}),
    ]
    idx = write(tmp_path, rows)
    assert (idx.rows, list(idx.times), list(idx.ids)) == (
        3,
        [100, 200, 200],
        [7, 9, 12],
    )
    assert list(idx.offsets) == [0, 40, 75]
    assert list(idx.lookup("t\0a")) == [0, 2]
    assert list(idx.lookup("t\0b")) == [0, 1]
    assert list(idx.lookup("t\0c")) == [2]
    assert list(idx.lookup("t\0missing")) == []


def test_binary_search_finds_every_term_among_many(tmp_path: Path) -> None:
    rng = random.Random(7)
    # Odd lengths and non-ASCII keep the blob unaligned before padding.
    vocabulary = sorted(
        {f"t\0{rng.randrange(10**6)}é{'x' * rng.randrange(5)}" for _ in range(3000)}
    )
    rows = [(i, i + 1, i * 10, set(rng.sample(vocabulary, 5))) for i in range(500)]
    idx = write(tmp_path, rows)
    for term in vocabulary:
        expected = [row for row, (_, _, _, terms) in enumerate(rows) if term in terms]
        assert list(idx.lookup(term)) == expected, term
    # Missing terms that sort before, between and after the real ones.
    for missing in ["", "a", "t\0", vocabulary[0] + "\0", "\U0010ffff"]:
        assert list(idx.lookup(missing)) == []


def memory_of(*events: dict[str, Any]) -> index.MemIndex:
    memory = index.MemIndex()
    for i, data in enumerate(events):
        memory.add(Event(i + 1, i, data), terms_of(data))
    return memory


MEMORY = memory_of(
    {"status": 200, "msg": "ok fine"},
    {"status": "200", "msg": "slow"},
    {"status": 500, "msg": "slow upstream"},
    {"status": "503.0", "msg": "upstream slow"},
)


@pytest.mark.parametrize(
    ("text", "rows", "exact"),
    [
        ("status:200", {0, 1}, True),  # 200 and "200": a union of spellings
        ("slow", {1, 2, 3}, True),
        ("status:200 slow", {1}, True),
        ("slow OR fine", {0, 1, 2, 3}, True),
        ("NOT slow", {0}, True),
        ("status>=500", {2, 3}, True),  # "503.0" the string is a number too
        ("status<500", {0, 1}, True),
        ("status:*", {0, 1, 2, 3}, True),
        # A phrase: the index knows both words are there, not that they are
        # next to each other in that order, so rows 2 and 3 are candidates.
        ('"slow upstream"', {2, 3}, False),
        # NOT of an inexact set: every row is a candidate, to be checked.
        ('NOT "slow upstream"', None, False),
        ('"slow upstream" status:200', set(), True),  # nothing to check
        ("", None, True),
    ],
)
def test_evaluate_gives_candidates_and_whether_they_are_exact(
    text: str, rows: set[int] | None, exact: bool
) -> None:
    assert index.evaluate(parse(text), MEMORY) == (rows, exact)


def test_prefix_lookup_finds_one_field_s_terms_in_a_segment(tmp_path: Path) -> None:
    rows = [
        (i, i + 1, 0, terms_of(data))
        for i, data in enumerate([{"status": 200}, {"status": 503}, {"stats": 1}])
    ]
    idx = write(tmp_path, rows)
    found = {term: list(r) for term, r in idx.with_prefix(field_prefix("status"))}
    assert found == {field_key("status", "200"): [0], field_key("status", "503"): [1]}
    assert list(idx.with_prefix(field_prefix("nothing"))) == []

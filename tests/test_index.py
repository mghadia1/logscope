import random
from pathlib import Path
from typing import Any

from logscope import index
from logscope.events import Event
from logscope.query import parse
from logscope.terms import terms_of


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


def test_candidate_rows_union_within_a_field_term_intersection_across() -> None:
    memory = index.MemIndex()
    events: list[dict[str, Any]] = [
        {"status": 200, "msg": "ok fine"},
        {"status": "200", "msg": "slow"},
        {"status": 500, "msg": "slow upstream"},
    ]
    for i, data in enumerate(events):
        memory.add(Event(i + 1, i, data), terms_of(data))
    assert index.candidate_rows(memory.lookup, parse("status:200")) == {0, 1}
    assert index.candidate_rows(memory.lookup, parse("slow")) == {1, 2}
    assert index.candidate_rows(memory.lookup, parse("status:200 slow")) == {1}
    assert index.candidate_rows(memory.lookup, parse("slow-upstream")) == {2}
    assert index.candidate_rows(memory.lookup, parse("nothing")) == set()
    assert index.candidate_rows(memory.lookup, []) is None  # every row

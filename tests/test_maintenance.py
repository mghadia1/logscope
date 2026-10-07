"""Merging, retention, and deleting files only when no search needs them."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from corpus import HOUR, SECOND, T0, make_line

from logscope import aggregate
from logscope.events import ParsedLine
from logscope.index import Source
from logscope.query import parse
from logscope.store import Rows, Store


def lines(*times: int) -> list[ParsedLine]:
    out = []
    for i, t in enumerate(times):
        data = {"msg": f"event {i}", "n": i}
        out.append(ParsedLine(t, data, json.dumps(data).encode()))
    return out


def flushes(store: Store, count: int, hour: int = 0) -> None:
    """``count`` flushes, each leaving one segment in ``hour``."""
    import random

    rng = random.Random(count * 100 + hour)
    for _ in range(count):
        store.append(
            [
                make_line(rng, T0 + hour * HOUR + rng.randrange(3600) * SECOND)
                for _ in range(20)
            ]
        )
        store.flush()


@pytest.fixture
def opened(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path, merge_threshold=4)
    store.open()
    yield store
    store.abandon()


def everything(store: Store) -> list[tuple[int, int]]:
    events, _ = store.search(limit=10**9)
    return [(e.time, e.id) for e in events]


def segment_files(store: Store) -> set[str]:
    return {
        p.relative_to(store.data_dir).as_posix()
        for p in (store.data_dir / "segments").rglob("*.*")
    }


def test_an_hour_with_enough_segments_is_merged_into_one(opened: Store) -> None:
    flushes(opened, 4, hour=0)
    flushes(opened, 3, hour=1)  # below the threshold: left alone
    before = everything(opened)
    counts_before = aggregate.top_values(opened, None, 0, 2**62, "level", 10).values
    merged = opened.merge()
    assert [(s.partition, s.count) for s in merged] == [("2026-10-06T18", 80)]
    by_partition = [s.partition for s in opened._segments]
    assert sorted(by_partition) == ["2026-10-06T18"] + ["2026-10-06T19"] * 3
    assert everything(opened) == before
    assert (
        aggregate.top_values(opened, None, 0, 2**62, "level", 10).values
        == counts_before
    )
    # Same answers through the index as without it, after the merge too.
    for text in ["level:error", '"upstream timeout"', "status>=500 NOT http:*"]:
        indexed, _ = opened.search(parse(text), limit=10**6)
        scanned, _ = opened.search(parse(text), limit=10**6, use_index=False)
        assert [e.id for e in indexed] == [e.id for e in scanned]
    # The old files are gone (no search was running), the new ones listed.
    listed = {p for s in opened._segments for p in (s.path, s.index_path)}
    assert segment_files(opened) == listed


def test_merging_the_same_hour_twice_never_reuses_a_name(opened: Store) -> None:
    # Two merges of one hour can cover the same id range; without a unique
    # number in the name the second would overwrite a listed file.
    flushes(opened, 4, hour=0)
    first = opened.merge()[0]
    flushes(opened, 3, hour=0)
    second = opened.merge()[0]
    assert first.path != second.path
    assert second.count == first.count + 60
    assert len(everything(opened)) == 140


def test_a_crash_during_a_merge_loses_and_duplicates_nothing(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushes(opened, 4, hour=0)
    before = everything(opened)

    def crash(*args: object) -> None:
        raise KeyboardInterrupt("killed while writing the manifest")

    monkeypatch.setattr(opened, "_write_manifest", crash)
    with pytest.raises(KeyboardInterrupt):
        opened.merge()
    monkeypatch.undo()
    opened.abandon()
    fresh = Store(opened.data_dir)
    try:
        recovery = fresh.open()
        assert len(recovery.orphans) == 2  # the merged segment and its index
        assert everything(fresh) == before
        assert len(fresh._segments) == 4
    finally:
        fresh.abandon()


def test_a_crash_after_a_merge_commits_leaves_only_orphans(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushes(opened, 4, hour=0)
    before = everything(opened)
    monkeypatch.setattr(opened, "_collect", lambda: None)  # die before deleting
    opened.merge()
    monkeypatch.undo()
    opened.abandon()
    fresh = Store(opened.data_dir)
    try:
        assert len(fresh.open().orphans) == 8  # four old segments, with indexes
        assert everything(fresh) == before
    finally:
        fresh.abandon()


def test_files_are_deleted_only_when_no_search_can_still_read_them(
    opened: Store,
) -> None:
    flushes(opened, 4, hour=0)
    flushes(opened, 1, hour=1)
    old_files = segment_files(opened)
    seen_during: list[set[str]] = []
    counted = [0]

    def visit(source: Source, rows: Rows) -> None:
        if not seen_during:
            # Mid-aggregation: merge the hour this reader still has to read.
            assert opened.merge()
            seen_during.append(segment_files(opened))
        counted[0] += len(rows)

    opened.visit_matches(None, 0, 2**62, visit)
    # During the read, the merged-away files were still there to be read...
    assert old_files <= seen_during[0]
    assert counted[0] == 100  # ...and were read: every event counted once
    # ...and once the reader finished, they were deleted.
    listed = {p for s in opened._segments for p in (s.path, s.index_path)}
    assert segment_files(opened) == listed
    assert opened.stats()["files_awaiting_deletion"] == 0


def test_retention_drops_old_segments_only(tmp_path: Path) -> None:
    store = Store(tmp_path, retention_hours=2)
    store.open()
    try:
        store.append(lines(T0, T0 + 30 * 60 * SECOND))  # hour 0
        store.append(lines(T0 + 3 * HOUR))  # hour 3
        store.flush()
        # Hour 0's segment ends at 0:30. With 2 hours kept, it goes once
        # "now" is past 2:30, not before.
        assert store.expire(T0 + 2 * HOUR + 29 * 60 * SECOND) == []
        dropped = store.expire(T0 + 2 * HOUR + 30 * 60 * SECOND + 1)
        assert [s.partition for s in dropped] == ["2026-10-06T18"]
        assert [t for t, _ in everything(store)] == [T0 + 3 * HOUR]
        listed = {p for s in store._segments for p in (s.path, s.index_path)}
        assert segment_files(store) == listed
    finally:
        store.close()


def test_retention_off_keeps_everything(opened: Store) -> None:
    flushes(opened, 1, hour=0)
    assert opened.expire(T0 + 10_000 * HOUR) == []
    assert len(everything(opened)) == 20

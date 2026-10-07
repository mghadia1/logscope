import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from logscope import store as store_module
from logscope import wal
from logscope.events import ParsedLine, parse_time
from logscope.store import Store, StoreCorruption, StoreLocked, StoreUnavailable

HOUR = 3600 * 1_000_000
T0 = parse_time("2026-10-06T21:00:00Z", "t")  # the start of an hour


def lines(*times: int, tag: str = "x") -> list[ParsedLine]:
    out = []
    for i, t in enumerate(times):
        data = {"msg": f"{tag}{i}"}
        out.append(ParsedLine(t, data, json.dumps(data).encode()))
    return out


@pytest.fixture
def opened(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path / "data")
    store.open()
    yield store
    store.abandon()


def reopen(store: Store) -> tuple[Store, store_module.Recovery]:
    """What a restart after a crash does: no flush, no tidying, then open."""
    store.abandon()
    fresh = Store(store.data_dir)
    return fresh, fresh.open()


def all_ids(store: Store) -> list[int]:
    events, _ = store.search(limit=1_000_000)
    return sorted(e.id for e in events)


def test_appended_events_are_searchable_at_once(opened: Store) -> None:
    assert opened.append(lines(T0, T0 + 1)) == (1, 2)
    assert opened.append(lines(T0 + 2)) == (3, 3)
    events, stats = opened.search()
    assert [e.id for e in events] == [3, 2, 1]  # newest first
    assert stats.events_read == 3


def test_flush_writes_one_segment_per_hour_and_a_manifest(opened: Store) -> None:
    opened.append(lines(T0 + 5, T0 + HOUR + 1, T0 + 1))
    written = opened.flush()
    assert [(s.partition, s.count) for s in written] == [
        ("2026-10-06T21", 2),
        ("2026-10-06T22", 1),
    ]
    first = opened.data_dir / written[0].path
    times = [json.loads(line)["t"] for line in first.read_text().splitlines()]
    assert times == [T0 + 1, T0 + 5]  # sorted by time within the segment
    manifest = json.loads((opened.data_dir / "MANIFEST.json").read_text())
    assert manifest["flushed_through"] == 3
    assert [s["path"] for s in manifest["segments"]] == [s.path for s in written]
    # The flushed WAL file is gone; only the new, empty one remains.
    assert [p.name for p in (opened.data_dir / "wal").iterdir()] == [f"{4:020d}.wal"]
    assert all_ids(opened) == [1, 2, 3]


def test_search_reads_segments_and_memory_together(opened: Store) -> None:
    opened.append(lines(T0, T0 + 1))
    opened.flush()
    opened.append(lines(T0 + 2))
    events, stats = opened.search()
    assert [e.id for e in events] == [3, 2, 1]
    assert stats.segments_read == 1


def test_time_range_is_half_open_and_pagination_sees_every_event(opened: Store) -> None:
    opened.append(lines(*[T0 + i for i in range(10)], T0 + 5))  # a tie at T0+5
    opened.flush()
    events, _ = opened.search(start=T0 + 2, end=T0 + 5)
    assert [e.time - T0 for e in events] == [4, 3, 2]
    seen: list[int] = []
    before = None
    while True:
        page, _ = opened.search(limit=3, before=before)
        if not page:
            break
        seen += [e.id for e in page]
        before = (page[-1].time, page[-1].id)
    assert sorted(seen) == list(range(1, 12))
    assert len(seen) == len(set(seen))


def test_unflushed_events_come_back_from_the_wal(opened: Store) -> None:
    opened.append(lines(T0, T0 + 1))
    opened.flush()
    opened.append(lines(T0 + 2, T0 + 3))
    fresh, recovery = reopen(opened)
    try:
        assert recovery.replayed == 2
        assert all_ids(fresh) == [1, 2, 3, 4]
        assert fresh.append(lines(T0 + 4)) == (5, 5)  # ids carry on
    finally:
        fresh.abandon()


def test_a_torn_tail_is_cut_and_writing_goes_on(opened: Store) -> None:
    opened.append(lines(T0))
    with open(opened._wal_dir / f"{1:020d}.wal", "ab") as file:
        file.write(wal.encode(2, [(T0, b'{"msg":"never acknowledged"}')])[:-5])
    fresh, recovery = reopen(opened)
    assert recovery.torn_bytes > 0
    assert all_ids(fresh) == [1]
    fresh.append(lines(T0 + 1))
    # The cut file is no longer the newest; it must not look damaged now.
    again, recovery = reopen(fresh)
    try:
        assert recovery.torn_bytes == 0
        assert all_ids(again) == [1, 2]
    finally:
        again.abandon()


def test_a_crash_during_a_flush_loses_and_duplicates_nothing(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The segment files are written, then the process dies before the
    # manifest names them: they are orphans, and the WAL still has the events.
    opened.append(lines(T0, T0 + HOUR))

    def crash(*args: object) -> None:
        raise KeyboardInterrupt("killed while writing the manifest")

    monkeypatch.setattr(opened, "_write_manifest", crash)
    with pytest.raises(KeyboardInterrupt):
        opened.flush()
    assert all_ids(opened) == [1, 2]  # still searchable before the restart
    monkeypatch.undo()
    fresh, recovery = reopen(opened)
    try:
        assert len(recovery.orphans) == 4  # two segments, each data + index
        assert all(not (fresh.data_dir / o).exists() for o in recovery.orphans)
        assert recovery.replayed == 2
        assert all_ids(fresh) == [1, 2]
    finally:
        fresh.abandon()


def test_a_crash_after_the_manifest_but_before_wal_cleanup(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The manifest says ids 1-2 are in segments, but their WAL file is still
    # there: replaying it would duplicate them.
    opened.append(lines(T0, T0 + 1))
    monkeypatch.setattr(opened, "_delete_wals", lambda paths: None)
    opened.flush()
    monkeypatch.undo()
    opened.append(lines(T0 + 2))
    fresh, recovery = reopen(opened)
    try:
        assert recovery.replayed == 1
        assert all_ids(fresh) == [1, 2, 3]
    finally:
        fresh.abandon()


def test_a_failed_flush_keeps_the_events_and_a_later_one_succeeds(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened.append(lines(T0))

    def disk_full(*args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(opened, "_write_manifest", disk_full)
    with pytest.raises(OSError):
        opened.flush()
    monkeypatch.undo()
    opened.append(lines(T0 + 1))
    assert [s.count for s in opened.flush()] == [2]
    fresh, _ = reopen(opened)
    try:
        assert all_ids(fresh) == [1, 2]
    finally:
        fresh.abandon()


def test_missing_ids_are_reported_not_skipped(opened: Store) -> None:
    opened.append(lines(T0))
    opened.append(lines(T0 + 1))
    opened.append(lines(T0 + 2))
    opened.abandon()
    # Remove the middle record by rewriting the file with records 1 and 3.
    path = opened._wal_dir / f"{1:020d}.wal"
    records = wal.read(path).records
    path.write_bytes(
        b"".join(
            wal.encode(r.first_id, [(t, json.dumps(e).encode()) for t, e in r.events])
            for r in (records[0], records[2])
        )
    )
    with pytest.raises(StoreCorruption, match="next id should be 2"):
        Store(opened.data_dir).open()


def test_a_second_process_cannot_open_the_same_directory(opened: Store) -> None:
    with pytest.raises(StoreLocked):
        Store(opened.data_dir).open()


def test_after_a_failed_fsync_writes_are_refused(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened.append(lines(T0))

    def failing_fsync(fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(StoreUnavailable):
        opened.append(lines(T0 + 1))
    monkeypatch.undo()
    with pytest.raises(StoreUnavailable, match="could not be written"):
        opened.append(lines(T0 + 2))
    assert opened.stats()["writable"] is False
    assert all_ids(opened) == [1]  # reads still work


def test_flush_on_close_then_nothing_to_replay(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.open()
    store.append(lines(T0, T0 + 1))
    store.close()
    fresh = Store(tmp_path)
    try:
        assert fresh.open().replayed == 0
        assert all_ids(fresh) == [1, 2]
    finally:
        fresh.close()


def test_the_flusher_flushes_when_the_memtable_is_full(tmp_path: Path) -> None:
    store = Store(tmp_path, flush_events=3, flush_interval=3600)
    store.open()
    store.start_background(period=0.01)
    try:
        store.append(lines(T0, T0 + 1, T0 + 2))
        for _ in range(500):
            if store.stats()["segments"]:
                break
            time.sleep(0.01)
        assert store.stats()["segment_events"] == 3
    finally:
        store.close()


def test_events_being_flushed_stay_searchable(
    opened: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Between the memtable being frozen and the manifest naming its
    # segments, its events are in neither place; searches must still see
    # them (from the frozen memtable), with and without the index.
    opened.append(lines(T0, T0 + 1))
    write_manifest = opened._write_manifest
    seen: dict[str, list[int]] = {}

    def search_mid_flush(segments: list[store_module.Segment], through: int) -> None:
        opened.append(lines(T0 + 2))  # arrives mid-flush: the new memtable
        for name, use_index in (("index", True), ("scan", False)):
            events, _ = opened.search(limit=10, use_index=use_index)
            seen[name] = sorted(e.id for e in events)
        write_manifest(segments, through)

    monkeypatch.setattr(opened, "_write_manifest", search_mid_flush)
    opened.flush()
    assert seen == {"index": [1, 2, 3], "scan": [1, 2, 3]}
    assert opened.stats()["events_in_memory"] == 1  # 3 is still in memory

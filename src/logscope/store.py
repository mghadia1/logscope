"""The store: write-ahead log, memtable, segments, and the manifest that
says which segments exist.

Write path
    ``append`` writes one WAL record and fsyncs it, then adds the events to
    the *memtable* (with an in-memory index), where searches see them at
    once, and hands them to live-tail subscribers.

Flush (when the memtable is big or old enough)
    The memtable is frozen and a new WAL file started. The frozen events go
    into one segment file per hour they fall in, each with an index file;
    then a new manifest lists those segments and the highest id they hold
    ("flushed through"). Only after that are the old WAL files deleted.

Maintenance (every ``maintenance_interval`` seconds, in the background)
    Segments wholly older than the retention period are dropped, and an
    hour with ``merge_threshold`` or more segments is merged into one. Both
    commit by writing a new manifest. The old files are deleted only once no
    search that could still be reading them is running (each manifest change
    is a new *version*; a search holds the version it started with).

Recovery (``open``)
    The manifest says which segments are real: any other segment or index
    file was left by a flush, merge or deletion that did not finish, and is
    deleted. WAL records past "flushed through" are replayed into the
    memtable, and a torn tail is cut off. A listed segment without an index
    gets one.

Search
    Segments are visited newest first, skipping those outside the time
    range; in each, the index gives the matching rows and their times, so
    rows are read newest first and the search stops as soon as nothing older
    can make the top ``limit``. ``use_index=False`` is the plain scan of
    Milestone 1, kept as the baseline the index is checked and measured
    against.

Every file that matters is written under a temporary name, fsynced, renamed
into place, and its directory fsynced, so a crash leaves either the old
version or the new one. Ids are global and contiguous, which is what lets
recovery check that nothing is missing.
"""

import fcntl
import heapq
import json
import logging
import os
import threading
import time
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import index, wal
from .events import EPOCH, MAX_TIME, MIN_TIME, Event, ParsedLine, now
from .query import EventView, Node, matches
from .terms import terms_of

log = logging.getLogger(__name__)

MANIFEST = "MANIFEST.json"
PARTITION = 3600 * 1_000_000  # one hour, in microseconds
# Open (memory-mapped) segment indexes kept around; each holds a file
# descriptor.
INDEX_CACHE = 256

# The rows of one source (a segment or the memtable) that match a query:
# a range when they are every row in a time window of a segment.
Rows = range | set[int]


class StoreError(Exception):
    pass


class StoreUnavailable(StoreError):
    """Writes are refused: the write-ahead log could not be written."""


class StoreLocked(StoreError):
    """Another process has the data directory open."""


class StoreCorruption(StoreError):
    """The data on disk is inconsistent in a way a crash cannot explain."""


@dataclass(frozen=True, slots=True)
class Segment:
    path: str  # the .jsonl, relative to the data directory
    partition: str
    min_time: int
    max_time: int
    min_id: int
    max_id: int
    count: int
    bytes: int

    @property
    def index_path(self) -> str:
        return self.path.removesuffix(".jsonl") + ".idx"


@dataclass(frozen=True, slots=True)
class Recovery:
    replayed: int  # events replayed from the WAL into the memtable
    torn_bytes: int  # cut off the end of the WAL
    orphans: list[str]  # segment and index files no manifest listed, deleted
    indexed: int = 0  # listed segments that had no index and got one


@dataclass(slots=True)
class SearchStats:
    segments_total: int = 0
    segments_read: int = 0  # opened: index consulted, or (scan) every row read
    segments_skipped: int = 0  # never opened: outside the time range, or older
    # than everything already found
    events_read: int = 0  # rows parsed from segment files, plus memtable
    # events looked at
    candidates: int = 0  # rows the index matched within the time range
    # Events tested against the query itself, because the index could not
    # decide alone (phrases, NOT around a phrase); every event, in a scan.
    checked: int = 0


def partition_of(time: int) -> str:
    """The UTC hour an event falls in, e.g. "2026-10-06T21"."""
    start = EPOCH + timedelta(microseconds=time - time % PARTITION)
    return start.strftime("%Y-%m-%dT%H")


class _Keys:
    """A segment index's rows as (time, id) keys, for bisect."""

    def __init__(self, segment_index: index.SegmentIndex) -> None:
        self._times = segment_index.times
        self._ids = segment_index.ids

    def __len__(self) -> int:
        return len(self._times)

    def __getitem__(self, row: int) -> tuple[int, int]:
        return self._times[row], self._ids[row]


class _Top:
    """The ``limit`` largest (time, id) keys seen, with their events."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._heap: list[tuple[tuple[int, int], Event]] = []

    def full_above(self, key: tuple[int, int]) -> bool:
        """Whether ``key`` and everything below it can no longer get in."""
        return len(self._heap) >= self.limit and key < self._heap[0][0]

    def wants(self, key: tuple[int, int]) -> bool:
        """Whether an event with ``key`` would get in (so it is worth reading)."""
        return len(self._heap) < self.limit or key > self._heap[0][0]

    def add(self, key: tuple[int, int], event: Event) -> None:
        if len(self._heap) < self.limit:
            heapq.heappush(self._heap, (key, event))
        else:
            heapq.heapreplace(self._heap, (key, event))

    def newest_first(self) -> list[Event]:
        return [event for _, event in sorted(self._heap, reverse=True)]


class Store:
    def __init__(
        self,
        data_dir: Path,
        *,
        flush_events: int = 50_000,
        flush_interval: float = 60.0,
        retention_hours: float = 0,
        merge_threshold: int = 8,
    ) -> None:
        self.data_dir = data_dir
        self.flush_events = flush_events
        self.flush_interval = flush_interval
        # 0: keep everything.
        self.retention = round(retention_hours * 3600 * 1_000_000)
        self.merge_threshold = merge_threshold
        # Guards everything below unless noted.
        self._lock = threading.Lock()
        # Held while the segment list in the manifest changes: by a flush (the
        # whole flush) and by a merge or expiry (the commit).
        self._flush_lock = threading.Lock()
        # One merge or expiry at a time.
        self._maintenance_lock = threading.Lock()
        self._memtable = index.MemIndex()
        self._memtable_since: float | None = None  # monotonic, first event
        self._frozen = index.MemIndex()  # being flushed, still searchable
        self._segments: list[Segment] = []
        self._flushed_through = 0
        self._next_id = 1
        self._next_segment = 1  # numbers new segment files, never reused
        self._wal: wal.Writer | None = None
        # Older WAL files holding events that are not in a segment yet.
        self._wal_backlog: list[Path] = []
        self._broken: str | None = None
        self._lock_fd: int | None = None
        self._stop = threading.Event()
        self._background: threading.Thread | None = None
        self._indexes: OrderedDict[str, index.SegmentIndex] = OrderedDict()
        self._indexes_lock = threading.Lock()
        # Deferred deletion: the manifest version, the searches running per
        # version they started with, and files no longer listed, with the
        # version from which they were not.
        self._version = 0
        self._readers: dict[int, int] = {}
        self._obsolete: list[tuple[int, list[str]]] = []
        self._subscribers: list[Callable[[list[Event]], None]] = []

    @property
    def _wal_dir(self) -> Path:
        return self.data_dir / "wal"

    @property
    def _segment_dir(self) -> Path:
        return self.data_dir / "segments"

    # Opening and recovery

    def open(self) -> Recovery:
        self._wal_dir.mkdir(parents=True, exist_ok=True)
        self._segment_dir.mkdir(exist_ok=True)
        self._acquire_lock()
        try:
            return self._recover()
        except BaseException:
            self._release_lock()
            raise

    def _acquire_lock(self) -> None:
        fd = os.open(self.data_dir / "LOCK", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise StoreLocked(
                f"{self.data_dir} is in use by another LogScope process"
            ) from None
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing the descriptor drops the flock
            self._lock_fd = None

    def _recover(self) -> Recovery:
        self._segments, self._flushed_through, self._next_segment = (
            self._read_manifest()
        )
        listed = {segment.path for segment in self._segments}
        listed |= {segment.index_path for segment in self._segments}
        for segment in self._segments:
            if not (self.data_dir / segment.path).is_file():
                raise StoreCorruption(
                    f"the manifest lists {segment.path}, which is missing"
                )
        orphans = []
        for path in sorted(self._segment_dir.rglob("*")):
            relative = path.relative_to(self.data_dir).as_posix()
            if path.is_file() and relative not in listed:
                path.unlink()
                orphans.append(relative)
                log.warning("deleted %s: no manifest lists it", relative)
        (self.data_dir / f"{MANIFEST}.tmp").unlink(missing_ok=True)
        indexed = 0
        for segment in self._segments:
            if not (self.data_dir / segment.index_path).is_file():
                self._index_existing(segment)
                indexed += 1

        expected = self._flushed_through + 1  # the next id the WAL must hold
        replayed = torn = 0
        files = sorted(self._wal_dir.glob("*.wal"))
        for position, path in enumerate(files):
            try:
                result = wal.read(path)
            except wal.WALCorruption as error:
                raise StoreCorruption(str(error)) from None
            if result.torn_bytes:
                if position != len(files) - 1:
                    raise StoreCorruption(
                        f"{path}: damaged record at byte {result.valid_end}; only"
                        " the newest WAL file can end in a torn write"
                    )
                log.warning(
                    "%s: cut %d bytes a crash left half-written (never acknowledged)",
                    path,
                    result.torn_bytes,
                )
                with open(path, "r+b") as file:
                    file.truncate(result.valid_end)
                    os.fsync(file.fileno())
                torn = result.torn_bytes
            holds_unflushed = False
            for record in result.records:
                if record.last_id <= self._flushed_through:
                    continue  # already in a segment
                if record.first_id != expected:
                    raise StoreCorruption(
                        f"{path}: the record at byte {record.offset} holds ids"
                        f" {record.first_id}-{record.last_id}, but the next id"
                        f" should be {expected}: events are missing"
                    )
                for i, (event_time, data) in enumerate(record.events):
                    event = Event(record.first_id + i, event_time, data)
                    self._memtable.add(event, terms_of(data))
                replayed += len(record.events)
                expected = record.last_id + 1
                holds_unflushed = True
            if holds_unflushed:
                self._wal_backlog.append(path)
            else:
                path.unlink()  # empty, or everything in it is in segments
        self._next_id = expected
        if len(self._memtable):
            self._memtable_since = time.monotonic()
        self._wal = wal.Writer(self._wal_dir / f"{self._next_id:020d}.wal")
        wal.fsync_dir(self._wal_dir)
        if replayed or torn or orphans or indexed:
            log.info(
                "recovered: %d events replayed from the WAL, %d torn bytes cut,"
                " %d orphan files deleted, %d segments indexed",
                replayed,
                torn,
                len(orphans),
                indexed,
            )
        return Recovery(replayed, torn, orphans, indexed)

    def _read_manifest(self) -> tuple[list[Segment], int, int]:
        path = self.data_dir / MANIFEST
        if not path.exists():
            return [], 0, 1
        body = json.loads(path.read_bytes())
        if body.get("version") != 1:
            raise StoreCorruption(
                f"{path}: unknown manifest version {body.get('version')!r}"
            )
        segments = [Segment(**item) for item in body["segments"]]
        # Manifests from before Milestone 4 have no segment numbers.
        return segments, int(body["flushed_through"]), int(body.get("next_segment", 1))

    def _index_existing(self, segment: Segment) -> None:
        """Index a segment written without one (by Milestone 1)."""
        rows = []
        offset = 0
        with open(self.data_dir / segment.path, "rb") as file:
            for line in file:
                item = json.loads(line)
                rows.append((item["t"], item["id"], offset, terms_of(item["e"])))
                offset += len(line)
        _write_atomic(self.data_dir / segment.index_path, index.build(rows))
        log.info("indexed %s", segment.path)

    # Writing

    def append(self, lines: Sequence[ParsedLine]) -> tuple[int, int]:
        """Store a batch durably; returns its first and last id. Once this
        returns, the batch survives a crash."""
        if not lines:
            raise ValueError("an empty batch")
        # Outside the lock: working out the terms is the slow part.
        line_terms = [terms_of(line.data) for line in lines]
        with self._lock:
            if self._broken:
                raise StoreUnavailable(self._broken)
            assert self._wal is not None, "the store is not open"
            first = self._next_id
            try:
                self._wal.append(first, [(line.time, line.encoded) for line in lines])
            except OSError as error:
                self._broken = f"the write-ahead log could not be written: {error}"
                log.error("%s; refusing writes until restarted", self._broken)
                raise StoreUnavailable(self._broken) from error
            self._next_id += len(lines)
            if not len(self._memtable):
                self._memtable_since = time.monotonic()
            events = [
                Event(first + i, line.time, line.data) for i, line in enumerate(lines)
            ]
            for event, terms in zip(events, line_terms, strict=True):
                self._memtable.add(event, terms)
            subscribers = list(self._subscribers)
        for deliver in subscribers:
            deliver(events)
        return first, first + len(lines) - 1

    def subscribe(self, deliver: Callable[[list[Event]], None]) -> None:
        """Call ``deliver`` with every batch stored from now on, from the
        thread that stored it. It must be quick and must not raise."""
        with self._lock:
            self._subscribers.append(deliver)

    def unsubscribe(self, deliver: Callable[[list[Event]], None]) -> None:
        with self._lock:
            self._subscribers.remove(deliver)

    def flush(self) -> list[Segment]:
        """Move the memtable into segment files; returns the new segments."""
        with self._flush_lock:
            with self._lock:
                if not len(self._memtable):
                    return []
                assert self._wal is not None, "the store is not open"
                # The new WAL file first: if that fails, nothing has changed.
                new_wal = wal.Writer(self._wal_dir / f"{self._next_id:020d}.wal")
                wal.fsync_dir(self._wal_dir)
                frozen, self._memtable = self._memtable, index.MemIndex()
                self._memtable_since = None
                self._frozen = frozen
                old_wals = [*self._wal_backlog, self._wal.path]
                self._wal_backlog = []
                self._wal.close()
                self._wal = new_wal
            through = frozen.events[-1].id
            try:
                written = self._write_segments(frozen.events)
                segments = [*self._segments, *written]
                self._write_manifest(segments, through)
            except BaseException:
                # Put the events back where searches and the next flush find
                # them; their WAL files are kept.
                with self._lock:
                    restored = index.MemIndex()
                    for event in [*frozen.events, *self._memtable.events]:
                        restored.add(event, terms_of(event.data))
                    self._memtable = restored
                    self._memtable_since = time.monotonic()
                    self._frozen = index.MemIndex()
                    self._wal_backlog = old_wals + self._wal_backlog
                raise
            with self._lock:
                self._segments = segments
                self._flushed_through = through
                self._frozen = index.MemIndex()
                self._version += 1
            self._delete_wals(old_wals)
            return written

    def _write_segments(self, events: list[Event]) -> list[Segment]:
        by_partition: dict[str, list[Event]] = {}
        for event in events:
            by_partition.setdefault(partition_of(event.time), []).append(event)
        written = []
        for partition, items in sorted(by_partition.items()):
            items.sort(key=lambda event: (event.time, event.id))
            rows = [
                (event.time, event.id, _segment_line(event), event.data)
                for event in items
            ]
            written.append(self._write_segment(partition, rows))
        return written

    def _write_segment(
        self, partition: str, rows: Sequence[tuple[int, int, bytes, dict[str, Any]]]
    ) -> Segment:
        """Write one segment and its index from rows of (time, id, line,
        event), already in (time, id) order; not yet in the manifest."""
        directory = self._segment_dir / partition
        if not directory.exists():
            directory.mkdir()
            wal.fsync_dir(self._segment_dir)
        index_rows = []
        offset = 0
        for event_time, event_id, line, data in rows:
            index_rows.append((event_time, event_id, offset, terms_of(data)))
            offset += len(line)
        min_id = min(event_id for _, event_id, _, _ in rows)
        max_id = max(event_id for _, event_id, _, _ in rows)
        with self._lock:
            number = self._next_segment
            self._next_segment += 1
        # The number keeps every name unique: two merges of the same hour can
        # cover the same ids, and must never overwrite a listed file.
        name = f"{number:08d}-{min_id:020d}-{max_id:020d}.jsonl"
        segment = Segment(
            path=f"segments/{partition}/{name}",
            partition=partition,
            min_time=rows[0][0],
            max_time=rows[-1][0],
            min_id=min_id,
            max_id=max_id,
            count=len(rows),
            bytes=offset,
        )
        _write_atomic(self.data_dir / segment.index_path, index.build(index_rows))
        _write_atomic(self.data_dir / segment.path, b"".join(r[2] for r in rows))
        return segment

    def _write_manifest(self, segments: list[Segment], flushed_through: int) -> None:
        with self._lock:
            next_segment = self._next_segment
        body = {
            "version": 1,
            "flushed_through": flushed_through,
            "next_segment": next_segment,
            "segments": [asdict(segment) for segment in segments],
        }
        _write_atomic(self.data_dir / MANIFEST, json.dumps(body, indent=1).encode())

    def _delete_wals(self, paths: list[Path]) -> None:
        for path in paths:
            path.unlink(missing_ok=True)
        wal.fsync_dir(self._wal_dir)

    # Merging, expiry, and deleting what no search can still read

    def merge(self) -> list[Segment]:
        """Merge every hour holding ``merge_threshold`` or more segments into
        one segment; returns the new segments."""
        with self._maintenance_lock:
            with self._lock:
                by_partition: dict[str, list[Segment]] = {}
                for segment in self._segments:
                    by_partition.setdefault(segment.partition, []).append(segment)
            merged = []
            for partition, group in sorted(by_partition.items()):
                if len(group) < self.merge_threshold:
                    continue
                new = self._merge(partition, group)
                self._commit(remove=group, add=[new])
                merged.append(new)
                log.info("merged %d segments of %s", len(group), partition)
            return merged

    def _merge(self, partition: str, group: list[Segment]) -> Segment:
        def rows_of(segment: Segment) -> Iterator[tuple[int, int, bytes, Any]]:
            with open(self.data_dir / segment.path, "rb") as file:
                for line in file:
                    item = json.loads(line)
                    yield item["t"], item["id"], line, item["e"]

        # Each segment is sorted by (time, id), so a k-way merge keeps the
        # order; the lines are copied as they are.
        rows = list(
            heapq.merge(*(rows_of(s) for s in group), key=lambda r: (r[0], r[1]))
        )
        return self._write_segment(partition, rows)

    def expire(self, now_time: int | None = None) -> list[Segment]:
        """Drop segments whose events are all older than the retention
        period; returns them."""
        if not self.retention:
            return []
        cutoff = (now() if now_time is None else now_time) - self.retention
        with self._maintenance_lock:
            with self._lock:
                old = [s for s in self._segments if s.max_time < cutoff]
            if old:
                self._commit(remove=old, add=[])
                log.info("dropped %d segments past the retention period", len(old))
            return old

    def _commit(self, remove: list[Segment], add: list[Segment]) -> None:
        """Replace segments in the manifest; delete the old files once no
        running search can be reading them."""
        removed = {segment.path for segment in remove}
        with self._flush_lock:
            segments = [s for s in self._segments if s.path not in removed] + add
            self._write_manifest(segments, self._flushed_through)
            with self._lock:
                self._segments = segments
                self._version += 1
                self._obsolete.append(
                    (
                        self._version,
                        [path for s in remove for path in (s.path, s.index_path)],
                    )
                )
        self._collect()

    def _register(self) -> int:
        """Called with the lock held: a reader of the current segments."""
        self._readers[self._version] = self._readers.get(self._version, 0) + 1
        return self._version

    def _release(self, version: int) -> None:
        with self._lock:
            self._readers[version] -= 1
            if not self._readers[version]:
                del self._readers[version]
        self._collect()

    def _collect(self) -> None:
        """Delete obsolete files once every running reader started at or
        after the version that dropped them."""
        with self._lock:
            oldest = min(self._readers, default=self._version)
            ready = [p for v, paths in self._obsolete if v <= oldest for p in paths]
            self._obsolete = [(v, ps) for v, ps in self._obsolete if v > oldest]
        for path in ready:
            with self._indexes_lock:
                self._indexes.pop(path, None)
            (self.data_dir / path).unlink(missing_ok=True)

    # Background work and shutdown

    def should_flush(self) -> bool:
        with self._lock:
            if len(self._memtable) >= self.flush_events:
                return True
            since = self._memtable_since
            return since is not None and time.monotonic() - since >= self.flush_interval

    def start_background(
        self, period: float = 1.0, maintenance_interval: float = 30.0
    ) -> None:
        """Flush when due (checked every ``period`` seconds); expire and merge
        every ``maintenance_interval`` seconds."""

        def run() -> None:
            due = time.monotonic() + maintenance_interval
            while not self._stop.wait(period):
                if self.should_flush():
                    try:
                        self.flush()
                    except Exception:
                        log.exception(
                            "flush failed; the events stay in memory and in the"
                            " WAL, and the next check retries"
                        )
                if time.monotonic() >= due:
                    due = time.monotonic() + maintenance_interval
                    try:
                        self.expire()
                        self.merge()
                    except Exception:
                        log.exception("maintenance failed; the next pass retries")

        self._background = threading.Thread(
            target=run, name="logscope-background", daemon=True
        )
        self._background.start()

    def close(self) -> None:
        """Stop the background thread, flush what is in memory, release the
        directory."""
        self._stop.set()
        if self._background is not None:
            self._background.join()
        try:
            self.flush()
        except Exception:
            log.exception("final flush failed; the WAL still holds the events")
        finally:
            self._collect()
            if self._wal is not None:
                self._wal.close()
                self._wal = None
            self._release_lock()

    def abandon(self) -> None:
        """Drop the store the way a crash would: no flush, nothing tidied.
        For tests, which then open the directory again."""
        self._stop.set()
        if self._background is not None:
            self._background.join()
        if self._wal is not None:
            self._wal.close()
            self._wal = None
        self._release_lock()

    # Reading

    def search(
        self,
        query: Node | None = None,
        start: int = MIN_TIME,
        end: int = MAX_TIME,
        limit: int = 100,
        before: tuple[int, int] | None = None,
        *,
        use_index: bool = True,
    ) -> tuple[list[Event], SearchStats]:
        """The newest ``limit`` events matching ``query`` (None: every event)
        with ``start <= time < end``, newest first; ``before`` is a (time, id)
        to continue below (pagination)."""
        if use_index:
            return self._search_indexed(query, start, end, limit, before)
        return self._search_scan(query, start, end, limit, before)

    def _search_indexed(
        self,
        query: Node | None,
        start: int,
        end: int,
        limit: int,
        before: tuple[int, int] | None,
    ) -> tuple[list[Event], SearchStats]:
        with self._lock:
            segments = list(self._segments)
            version = self._register()
            # The memtable keeps growing; take its candidates now, under the
            # lock.
            in_memory: list[tuple[Event, bool]] = []
            for memory in (self._frozen, self._memtable):
                rows, exact = index.evaluate(query, memory)
                found = range(len(memory)) if rows is None else rows
                in_memory += [(memory.events[row], exact) for row in found]
        try:
            stats = SearchStats(segments_total=len(segments))
            top = _Top(limit)
            for event, exact in in_memory:
                stats.events_read += 1
                key = (event.time, event.id)
                if (
                    start <= event.time < end
                    and (before is None or key < before)
                    and top.wants(key)
                    and (exact or self._check(query, event, stats))
                ):
                    top.add(key, event)

            # Newest first: once the top is full, a segment whose newest
            # possible key is below it, and every segment after it, can be
            # skipped.
            segments.sort(key=lambda s: (s.max_time, s.max_id), reverse=True)
            for number, segment in enumerate(segments):
                if top.full_above((segment.max_time, segment.max_id)):
                    stats.segments_skipped += len(segments) - number
                    break
                if (
                    segment.max_time < start
                    or segment.min_time >= end
                    or (
                        before is not None
                        and (segment.min_time, segment.min_id) >= before
                    )
                ):
                    stats.segments_skipped += 1
                    continue
                stats.segments_read += 1
                self._search_segment(segment, query, start, end, before, top, stats)
            return top.newest_first(), stats
        finally:
            self._release(version)

    def _search_segment(
        self,
        segment: Segment,
        query: Node | None,
        start: int,
        end: int,
        before: tuple[int, int] | None,
        top: _Top,
        stats: SearchStats,
    ) -> None:
        segment_index = self._index(segment)
        keys = _Keys(segment_index)
        # Rows are sorted by (time, id): the time range and the cursor are a
        # window of rows, found by binary search.
        low = bisect_left(keys, (start, 0))
        high = bisect_left(keys, (end, 0))
        if before is not None:
            high = min(high, bisect_left(keys, before))
        rows, exact = index.evaluate(query, segment_index)
        newest_first: Sequence[int]
        if rows is None:
            newest_first = range(high - 1, low - 1, -1)
        else:
            newest_first = sorted((r for r in rows if low <= r < high), reverse=True)
        stats.candidates += len(newest_first)
        with open(self.data_dir / segment.path, "rb") as file:
            for row in newest_first:
                key = keys[row]
                if top.full_above(key):
                    break  # every row after this one is older still
                event = self._read_row(file, segment_index, row, stats)
                if exact or self._check(query, event, stats):
                    top.add(key, event)

    @staticmethod
    def _read_row(
        file: Any, segment_index: index.SegmentIndex, row: int, stats: SearchStats
    ) -> Event:
        file.seek(segment_index.offsets[row])
        item = json.loads(file.readline())
        stats.events_read += 1
        return Event(item["id"], item["t"], item["e"])

    @staticmethod
    def _check(query: Node | None, event: Event, stats: SearchStats) -> bool:
        stats.checked += 1
        return matches(query, EventView(event.data))

    def _index(self, segment: Segment) -> index.SegmentIndex:
        with self._indexes_lock:
            found = self._indexes.get(segment.index_path)
            if found is not None:
                self._indexes.move_to_end(segment.index_path)
                return found
        opened = index.SegmentIndex(self.data_dir / segment.index_path)
        with self._indexes_lock:
            self._indexes[segment.index_path] = opened
            # Dropped from the cache, an index is unmapped once the last
            # search using it lets go.
            while len(self._indexes) > INDEX_CACHE:
                self._indexes.popitem(last=False)
        return opened

    def _search_scan(
        self,
        query: Node | None,
        start: int,
        end: int,
        limit: int,
        before: tuple[int, int] | None,
    ) -> tuple[list[Event], SearchStats]:
        """Milestone 1's search: read every event, test each one."""
        with self._lock:
            segments = list(self._segments)
            memory = [*self._frozen.events, *self._memtable.events]
            version = self._register()
        try:
            stats = SearchStats(segments_total=len(segments))

            def everything() -> Iterator[Event]:
                for segment in segments:
                    stats.segments_read += 1
                    yield from self._read_segment(segment)
                yield from memory

            def matching() -> Iterator[Event]:
                for event in everything():
                    stats.events_read += 1
                    if (
                        start <= event.time < end
                        and (before is None or (event.time, event.id) < before)
                        and self._check(query, event, stats)
                    ):
                        yield event

            found = heapq.nlargest(limit, matching(), key=lambda e: (e.time, e.id))
            return found, stats
        finally:
            self._release(version)

    def _read_segment(self, segment: Segment) -> Iterator[Event]:
        with open(self.data_dir / segment.path, "rb") as file:
            for line in file:
                item = json.loads(line)
                yield Event(item["id"], item["t"], item["e"])

    def visit_matches(
        self,
        query: Node | None,
        start: int,
        end: int,
        visit: Callable[[index.Source, Rows], None],
    ) -> SearchStats:
        """Call ``visit(source, rows)`` with the rows of each source (the
        memtable, then each segment) that match ``query`` exactly, with
        ``start <= time < end``: what aggregations are computed from.

        Rows are a ``range`` when they are every row of a segment's time
        window, so an aggregation can count them by bisection. Inexact
        candidates are checked against their events first. The memtable is
        visited with the lock held, since it keeps growing.
        """
        stats = SearchStats()
        with self._lock:
            segments = list(self._segments)
            version = self._register()
            stats.segments_total = len(segments)
            for memory in (self._frozen, self._memtable):
                rows, exact = index.evaluate(query, memory)
                found = range(len(memory)) if rows is None else rows
                stats.events_read += len(found)
                matched = {
                    row
                    for row in found
                    if start <= memory.times[row] < end
                    and (exact or self._check(query, memory.events[row], stats))
                }
                if matched:
                    visit(memory, matched)
        try:
            for segment in segments:
                if segment.max_time < start or segment.min_time >= end:
                    stats.segments_skipped += 1
                    continue
                stats.segments_read += 1
                segment_index = self._index(segment)
                matched_rows = self._segment_matches(
                    segment, segment_index, query, start, end, stats
                )
                if matched_rows:
                    visit(segment_index, matched_rows)
        finally:
            self._release(version)
        return stats

    def _segment_matches(
        self,
        segment: Segment,
        segment_index: index.SegmentIndex,
        query: Node | None,
        start: int,
        end: int,
        stats: SearchStats,
    ) -> Rows:
        times = segment_index.times
        low = bisect_left(times, start)
        high = bisect_left(times, end)
        rows, exact = index.evaluate(query, segment_index)
        if rows is None:
            window: Rows = range(low, high)
        else:
            window = {row for row in rows if low <= row < high}
        stats.candidates += len(window)
        if exact:
            return window
        checked = set()
        with open(self.data_dir / segment.path, "rb") as file:
            for row in sorted(window):
                event = self._read_row(file, segment_index, row, stats)
                if self._check(query, event, stats):
                    checked.add(row)
        return checked

    def time_bounds(self) -> tuple[int, int] | None:
        """The earliest and latest event time held, or None if empty."""
        with self._lock:
            times = [s.min_time for s in self._segments]
            times += [s.max_time for s in self._segments]
            for memory in (self._frozen, self._memtable):
                if len(memory):
                    times += [min(memory.times), max(memory.times)]
        return (min(times), max(times)) if times else None

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "next_id": self._next_id,
                "flushed_through": self._flushed_through,
                "events_in_memory": len(self._memtable) + len(self._frozen),
                "segments": len(self._segments),
                "segment_events": sum(segment.count for segment in self._segments),
                "segment_bytes": sum(segment.bytes for segment in self._segments),
                "wal_files": len(self._wal_backlog) + (self._wal is not None),
                "files_awaiting_deletion": sum(len(p) for _, p in self._obsolete),
                "retention_hours": self.retention / 3_600_000_000,
                "writable": self._broken is None,
                "unavailable_reason": self._broken,
            }


def _segment_line(event: Event) -> bytes:
    item = {"id": event.id, "t": event.time, "e": event.data}
    return json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` so a crash leaves the old or the new
    version, never a mix."""
    temporary = path.with_name(f"{path.name}.tmp")
    with open(temporary, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    wal.fsync_dir(path.parent)

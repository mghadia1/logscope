"""The store: write-ahead log, memtable, segments, and the manifest that
says which segments exist.

Write path
    ``append`` writes one WAL record and fsyncs it, then adds the events to
    the *memtable*, where searches see them at once.

Flush (when the memtable is big or old enough)
    The memtable is frozen and a new WAL file started. The frozen events go
    into one segment file per hour they fall in; then a new manifest lists
    those segments and the highest id they hold ("flushed through"). Only
    after that are the old WAL files deleted.

Recovery (``open``)
    The manifest says which segments are real: any other segment file was
    left by a flush that crashed, and is deleted. WAL records past "flushed
    through" are replayed into the memtable, and a torn tail is cut off.

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
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import wal
from .events import EPOCH, MAX_TIME, MIN_TIME, Event, ParsedLine

log = logging.getLogger(__name__)

MANIFEST = "MANIFEST.json"
PARTITION = 3600 * 1_000_000  # one hour, in microseconds


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
    path: str  # relative to the data directory
    partition: str
    min_time: int
    max_time: int
    min_id: int
    max_id: int
    count: int
    bytes: int


@dataclass(frozen=True, slots=True)
class Recovery:
    replayed: int  # events replayed from the WAL into the memtable
    torn_bytes: int  # cut off the end of the WAL
    orphans: list[str]  # segment files no manifest listed, deleted


@dataclass(slots=True)
class ScanStats:
    segments_scanned: int = 0
    events_scanned: int = 0


def partition_of(time: int) -> str:
    """The UTC hour an event falls in, e.g. "2026-10-06T21"."""
    start = EPOCH + timedelta(microseconds=time - time % PARTITION)
    return start.strftime("%Y-%m-%dT%H")


class Store:
    def __init__(
        self,
        data_dir: Path,
        *,
        flush_events: int = 50_000,
        flush_interval: float = 60.0,
    ) -> None:
        self.data_dir = data_dir
        self.flush_events = flush_events
        self.flush_interval = flush_interval
        # Guards everything below except the flusher's own fields.
        self._lock = threading.Lock()
        # One flush at a time; held for the whole flush, not just the swap.
        self._flush_lock = threading.Lock()
        self._memtable: list[Event] = []
        self._memtable_since: float | None = None  # monotonic, first event
        self._frozen: list[Event] = []  # being flushed, still searchable
        self._segments: list[Segment] = []
        self._flushed_through = 0
        self._next_id = 1
        self._wal: wal.Writer | None = None
        # Older WAL files holding events that are not in a segment yet.
        self._wal_backlog: list[Path] = []
        self._broken: str | None = None
        self._lock_fd: int | None = None
        self._stop = threading.Event()
        self._flusher: threading.Thread | None = None

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
        self._segments, self._flushed_through = self._read_manifest()
        listed = {segment.path for segment in self._segments}
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
                log.warning("deleted %s: left by a flush that did not finish", relative)
        (self.data_dir / f"{MANIFEST}.tmp").unlink(missing_ok=True)

        expected = self._flushed_through + 1  # the next id the WAL must hold
        replayed = torn = 0
        files = sorted(self._wal_dir.glob("*.wal"))
        for index, path in enumerate(files):
            try:
                result = wal.read(path)
            except wal.WALCorruption as error:
                raise StoreCorruption(str(error)) from None
            if result.torn_bytes:
                if index != len(files) - 1:
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
                self._memtable.extend(
                    Event(record.first_id + i, event_time, data)
                    for i, (event_time, data) in enumerate(record.events)
                )
                replayed += len(record.events)
                expected = record.last_id + 1
                holds_unflushed = True
            if holds_unflushed:
                self._wal_backlog.append(path)
            else:
                path.unlink()  # empty, or everything in it is in segments
        self._next_id = expected
        if self._memtable:
            self._memtable_since = time.monotonic()
        self._wal = wal.Writer(self._wal_dir / f"{self._next_id:020d}.wal")
        wal.fsync_dir(self._wal_dir)
        if replayed or torn or orphans:
            log.info(
                "recovered: %d events replayed from the WAL, %d torn bytes cut,"
                " %d orphan segment files deleted",
                replayed,
                torn,
                len(orphans),
            )
        return Recovery(replayed, torn, orphans)

    def _read_manifest(self) -> tuple[list[Segment], int]:
        path = self.data_dir / MANIFEST
        if not path.exists():
            return [], 0
        body = json.loads(path.read_bytes())
        if body.get("version") != 1:
            raise StoreCorruption(
                f"{path}: unknown manifest version {body.get('version')!r}"
            )
        segments = [Segment(**item) for item in body["segments"]]
        return segments, int(body["flushed_through"])

    # Writing

    def append(self, lines: Sequence[ParsedLine]) -> tuple[int, int]:
        """Store a batch durably; returns its first and last id. Once this
        returns, the batch survives a crash."""
        if not lines:
            raise ValueError("an empty batch")
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
            if not self._memtable:
                self._memtable_since = time.monotonic()
            self._memtable.extend(
                Event(first + i, line.time, line.data) for i, line in enumerate(lines)
            )
            return first, self._next_id - 1

    def flush(self) -> list[Segment]:
        """Move the memtable into segment files; returns the new segments."""
        with self._flush_lock:
            with self._lock:
                if not self._memtable:
                    return []
                assert self._wal is not None, "the store is not open"
                # The new WAL file first: if that fails, nothing has changed.
                new_wal = wal.Writer(self._wal_dir / f"{self._next_id:020d}.wal")
                wal.fsync_dir(self._wal_dir)
                frozen, self._memtable = self._memtable, []
                self._memtable_since = None
                self._frozen = frozen
                old_wals = [*self._wal_backlog, self._wal.path]
                self._wal_backlog = []
                self._wal.close()
                self._wal = new_wal
            through = frozen[-1].id
            try:
                written = self._write_segments(frozen)
                segments = [*self._segments, *written]
                self._write_manifest(segments, through)
            except BaseException:
                # Put the events back where searches and the next flush find
                # them; their WAL files are kept.
                with self._lock:
                    self._memtable = frozen + self._memtable
                    self._memtable_since = time.monotonic()
                    self._frozen = []
                    self._wal_backlog = old_wals + self._wal_backlog
                raise
            with self._lock:
                self._segments = segments
                self._flushed_through = through
                self._frozen = []
            self._delete_wals(old_wals)
            return written

    def _write_segments(self, events: list[Event]) -> list[Segment]:
        by_partition: dict[str, list[Event]] = {}
        for event in events:
            by_partition.setdefault(partition_of(event.time), []).append(event)
        written = []
        for partition, items in sorted(by_partition.items()):
            items.sort(key=lambda event: (event.time, event.id))
            directory = self._segment_dir / partition
            if not directory.exists():
                directory.mkdir()
                wal.fsync_dir(self._segment_dir)
            min_id = min(event.id for event in items)
            max_id = max(event.id for event in items)
            name = f"{min_id:020d}-{max_id:020d}.jsonl"
            data = b"".join(_segment_line(event) for event in items)
            _write_atomic(directory / name, data)
            written.append(
                Segment(
                    path=f"segments/{partition}/{name}",
                    partition=partition,
                    min_time=items[0].time,
                    max_time=items[-1].time,
                    min_id=min_id,
                    max_id=max_id,
                    count=len(items),
                    bytes=len(data),
                )
            )
        return written

    def _write_manifest(self, segments: list[Segment], flushed_through: int) -> None:
        body = {
            "version": 1,
            "flushed_through": flushed_through,
            "segments": [asdict(segment) for segment in segments],
        }
        _write_atomic(self.data_dir / MANIFEST, json.dumps(body, indent=1).encode())

    def _delete_wals(self, paths: list[Path]) -> None:
        for path in paths:
            path.unlink(missing_ok=True)
        wal.fsync_dir(self._wal_dir)

    # Background flushing and shutdown

    def should_flush(self) -> bool:
        with self._lock:
            if len(self._memtable) >= self.flush_events:
                return True
            since = self._memtable_since
            return since is not None and time.monotonic() - since >= self.flush_interval

    def start_flusher(self, period: float = 1.0) -> None:
        def run() -> None:
            while not self._stop.wait(period):
                if self.should_flush():
                    try:
                        self.flush()
                    except Exception:
                        log.exception(
                            "flush failed; the events stay in memory and in the"
                            " WAL, and the next check retries"
                        )

        self._flusher = threading.Thread(
            target=run, name="logscope-flusher", daemon=True
        )
        self._flusher.start()

    def close(self) -> None:
        """Stop the flusher, flush what is in memory, release the directory."""
        self._stop.set()
        if self._flusher is not None:
            self._flusher.join()
        try:
            self.flush()
        except Exception:
            log.exception("final flush failed; the WAL still holds the events")
        finally:
            if self._wal is not None:
                self._wal.close()
                self._wal = None
            self._release_lock()

    def abandon(self) -> None:
        """Drop the store the way a crash would: no flush, nothing tidied.
        For tests, which then open the directory again."""
        self._stop.set()
        if self._flusher is not None:
            self._flusher.join()
        if self._wal is not None:
            self._wal.close()
            self._wal = None
        self._release_lock()

    # Reading

    def search(
        self,
        start: int = MIN_TIME,
        end: int = MAX_TIME,
        limit: int = 100,
        before: tuple[int, int] | None = None,
    ) -> tuple[list[Event], ScanStats]:
        """The newest ``limit`` events with ``start <= time < end``, newest
        first; ``before`` is a (time, id) to continue below (pagination).

        Milestone 1 reads every segment and every event in memory: this is
        the baseline the index (Milestone 2) is measured against.
        """
        with self._lock:
            segments = list(self._segments)
            memory = [*self._frozen, *self._memtable]
        stats = ScanStats()

        def scanned() -> Iterator[Event]:
            for segment in segments:
                stats.segments_scanned += 1
                yield from self._read_segment(segment)
            yield from memory

        def matches() -> Iterator[Event]:
            for event in scanned():
                stats.events_scanned += 1
                if start <= event.time < end and (
                    before is None or (event.time, event.id) < before
                ):
                    yield event

        found = heapq.nlargest(limit, matches(), key=lambda e: (e.time, e.id))
        return found, stats

    def _read_segment(self, segment: Segment) -> Iterator[Event]:
        with open(self.data_dir / segment.path, "rb") as file:
            for line in file:
                item = json.loads(line)
                yield Event(item["id"], item["t"], item["e"])

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

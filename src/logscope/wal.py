"""The write-ahead log: every accepted batch is here, on disk, before the
API acknowledges it.

A WAL file is a sequence of records, one per ingested batch:

    "LSW1" | payload length (u32 LE) | CRC-32 of the payload (u32 LE) | payload

The payload is JSON, ``{"first_id": N, "events": [[time, event], ...]}``;
the events get the ids N, N + 1, ... in order. One record per batch means a
batch is stored whole or not at all.

A record is appended and fsynced before the batch is acknowledged, and the
next one is only written after that, so a crash can damage only the last
record, which was never acknowledged: a *torn tail*, cut off on recovery. A
damaged record with a valid one after it cannot come from a crash; that is
real damage to acknowledged data, and recovery stops rather than silently
skipping it (``WALCorruption``).
"""

import json
import os
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAGIC = b"LSW1"
HEADER = struct.Struct("<4sII")  # magic, payload length, CRC-32


class WALCorruption(Exception):
    """Acknowledged data in the log is damaged; a human has to look."""


@dataclass(frozen=True, slots=True)
class Record:
    offset: int
    first_id: int
    events: list[tuple[int, dict[str, Any]]]  # (time, event)

    @property
    def last_id(self) -> int:
        return self.first_id + len(self.events) - 1


@dataclass(frozen=True, slots=True)
class ReadResult:
    records: list[Record]
    # Where the last good record ends. Anything after it is a torn tail.
    valid_end: int
    size: int

    @property
    def torn_bytes(self) -> int:
        return self.size - self.valid_end


def encode(first_id: int, events: Sequence[tuple[int, bytes]]) -> bytes:
    """One record. ``events`` are (time, the event as encoded JSON)."""
    items = b",".join(b"[%d,%s]" % (time, data) for time, data in events)
    payload = b'{"first_id":%d,"events":[%s]}' % (first_id, items)
    return HEADER.pack(MAGIC, len(payload), zlib.crc32(payload)) + payload


def read(path: Path) -> ReadResult:
    """Every good record in ``path``; raises WALCorruption for damage that a
    crash cannot explain."""
    data = path.read_bytes()
    records: list[Record] = []
    position = 0
    while position < len(data):
        decoded = _decode_at(data, position)
        if decoded is None:
            break
        record, position = decoded
        records.append(record)
    if position < len(data) and _valid_record_after(data, position + 1):
        raise WALCorruption(
            f"{path}: damaged record at byte {position} is followed by valid"
            " records; this is not a torn write from a crash"
        )
    return ReadResult(records, position, len(data))


def _decode_at(data: bytes, offset: int) -> tuple[Record, int] | None:
    """The record at ``offset`` and where it ends, or None if it is not a
    whole, intact record."""
    if offset + HEADER.size > len(data):
        return None
    magic, length, crc = HEADER.unpack_from(data, offset)
    start = offset + HEADER.size
    end = start + length
    if magic != MAGIC or length == 0 or end > len(data):
        return None
    payload = data[start:end]
    if zlib.crc32(payload) != crc:
        return None
    try:
        body = json.loads(payload)
        first_id = body["first_id"]
        events = [(int(time), event) for time, event in body["events"]]
    except ValueError, KeyError, TypeError:
        return None
    if not isinstance(first_id, int) or not events:
        return None
    return Record(offset, first_id, events), end


def _valid_record_after(data: bytes, start: int) -> bool:
    position = data.find(MAGIC, start)
    while position != -1:
        if _decode_at(data, position) is not None:
            return True
        position = data.find(MAGIC, position + 1)
    return False


class Writer:
    """Appends records to one WAL file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = open(path, "ab")
        self._broken: str | None = None

    def append(self, first_id: int, events: Sequence[tuple[int, bytes]]) -> None:
        """Write and fsync one record. Raises OSError if either fails; after
        that the writer refuses further appends."""
        if self._broken:
            raise OSError(self._broken)
        start = self._file.tell()
        try:
            self._file.write(encode(first_id, events))
            self._file.flush()
            os.fsync(self._file.fileno())
        except OSError as error:
            # After a failed fsync the kernel may have dropped the dirty
            # pages and cleared the error, so a retry could "succeed"
            # without the data on disk (PostgreSQL's "fsyncgate"). Do not
            # retry: try to cut the partial record off, so it can never sit
            # in front of later records, and stop accepting writes.
            self._broken = f"write-ahead log append failed: {error}"
            try:
                self._file.truncate(start)
            except OSError:
                pass
            raise

    def close(self) -> None:
        self._file.close()


def fsync_dir(path: Path) -> None:
    """Make a directory's entries (new, renamed or deleted files) durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

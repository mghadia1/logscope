"""Inverted indexes: one file per segment, and one in memory for the
memtable.

A segment's index file (``.idx`` next to its ``.jsonl``), little-endian:

    header     "LSI1" | rows u32 | terms u32 | blob bytes u32
    times      rows x u64   each row's time    } rows are sorted by
    ids        rows x u64   each row's id      } (time, id)
    offsets    rows x u64   where each row starts in the .jsonl
    directory  terms x (blob offset, length, first posting, postings) u32
    blob       the terms' UTF-8 bytes, in sorted order, padded to 4 bytes
    postings   u32 row numbers, ascending within each term

It is memory-mapped, so opening it reads nothing: a lookup is a binary
search over the directory, and the times and ids let a search order and
filter rows without reading the ``.jsonl`` at all.
"""

import mmap
import struct
import sys
from array import array
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from .events import Event
from .query import FieldTerm, Term

if sys.byteorder != "little":  # arrays are written in native order
    raise ImportError("the index format is little-endian only")

MAGIC = b"LSI1"
HEADER = struct.Struct("<4sIII")
ENTRY = struct.Struct("<IIII")
_EMPTY: Sequence[int] = ()


def build(rows: Sequence[tuple[int, int, int, set[str]]]) -> bytes:
    """An index file for rows of (time, id, byte offset, terms), in segment
    order."""
    postings: dict[bytes, list[int]] = {}
    for row, (_, _, _, terms) in enumerate(rows):
        for term in terms:
            postings.setdefault(term.encode(), []).append(row)
    keys = sorted(postings)
    directory = bytearray()
    blob = bytearray()
    all_postings = array("I")
    for key in keys:
        directory += ENTRY.pack(
            len(blob), len(key), len(all_postings), len(postings[key])
        )
        blob += key
        all_postings.extend(postings[key])
    blob += b"\0" * (-len(blob) % 4)  # keep the postings 4-byte aligned
    return b"".join(
        [
            HEADER.pack(MAGIC, len(rows), len(keys), len(blob)),
            array("Q", (time for time, _, _, _ in rows)).tobytes(),
            array("Q", (event_id for _, event_id, _, _ in rows)).tobytes(),
            array("Q", (offset for _, _, offset, _ in rows)).tobytes(),
            bytes(directory),
            bytes(blob),
            all_postings.tobytes(),
        ]
    )


class SegmentIndex:
    """A memory-mapped index file."""

    def __init__(self, path: Path) -> None:
        with open(path, "rb") as file:
            self._map = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
        magic, rows, terms, blob_bytes = HEADER.unpack_from(self._map, 0)
        if magic != MAGIC:
            raise ValueError(f"{path}: not a LogScope index")
        view = memoryview(self._map)
        position = HEADER.size

        def take(size: int) -> memoryview:
            nonlocal position
            part = view[position : position + size]
            position += size
            return part

        self.rows = rows
        self.times = take(8 * rows).cast("Q")
        self.ids = take(8 * rows).cast("Q")
        self.offsets = take(8 * rows).cast("Q")
        self._directory = take(ENTRY.size * terms)
        self._blob = take(blob_bytes)
        self._postings = view[position:].cast("I")
        self._terms = terms

    def lookup(self, key: str) -> Sequence[int]:
        """The rows holding a term, ascending; binary search of the
        directory."""
        target = key.encode()
        low, high = 0, self._terms
        while low < high:
            middle = (low + high) // 2
            start, length, first, count = ENTRY.unpack_from(
                self._directory, middle * ENTRY.size
            )
            probe = bytes(self._blob[start : start + length])
            if probe < target:
                low = middle + 1
            elif probe > target:
                high = middle
            else:
                return self._postings[first : first + count]
        return _EMPTY


class MemIndex:
    """The memtable: events in arrival order, with postings by row number."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._postings: dict[str, array[int]] = {}

    def __len__(self) -> int:
        return len(self.events)

    def add(self, event: Event, terms: Iterable[str]) -> None:
        row = len(self.events)
        self.events.append(event)
        for term in terms:
            postings = self._postings.get(term)
            if postings is None:
                postings = self._postings[term] = array("I")
            postings.append(row)

    def lookup(self, key: str) -> Sequence[int]:
        return self._postings.get(key, _EMPTY)


def candidate_rows(
    lookup: Callable[[str], Sequence[int]], query: list[Term]
) -> set[int] | None:
    """The rows matching every term, or None for an empty query (every row).

    A field term matches any of its keys (a union); a text term needs all of
    its words (an intersection); the terms themselves are intersected.
    """
    result: set[int] | None = None
    for term in query:
        keys = iter(term.keys)
        rows = set(lookup(next(keys)))
        for key in keys:
            if isinstance(term, FieldTerm):
                rows.update(lookup(key))
            else:
                rows.intersection_update(lookup(key))
        result = rows if result is None else result & rows
        if not result:
            return set()
    return result

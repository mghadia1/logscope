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
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Protocol

from .events import Event
from .query import (
    And,
    ExistsTerm,
    FieldTerm,
    Node,
    Not,
    Or,
    PhraseTerm,
    RangeTerm,
    WordTerm,
)

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

    def _key(self, entry: int) -> tuple[bytes, int, int]:
        start, length, first, count = ENTRY.unpack_from(
            self._directory, entry * ENTRY.size
        )
        return bytes(self._blob[start : start + length]), first, count

    def _first_at_least(self, target: bytes) -> int:
        """The first directory entry whose term is >= target (bisection)."""
        low, high = 0, self._terms
        while low < high:
            middle = (low + high) // 2
            if self._key(middle)[0] < target:
                low = middle + 1
            else:
                high = middle
        return low

    def lookup(self, key: str) -> Sequence[int]:
        """The rows holding a term, ascending."""
        target = key.encode()
        entry = self._first_at_least(target)
        if entry < self._terms:
            term, first, count = self._key(entry)
            if term == target:
                return self._postings[first : first + count]
        return _EMPTY

    def with_prefix(self, prefix: str) -> Iterator[tuple[str, Sequence[int]]]:
        """Every term starting with ``prefix``, with its rows: the directory is
        sorted, so they are one run, found by bisection."""
        target = prefix.encode()
        for entry in range(self._first_at_least(target), self._terms):
            term, first, count = self._key(entry)
            if not term.startswith(target):
                return
            yield term.decode(), self._postings[first : first + count]


class MemIndex:
    """The memtable: events in arrival order, with postings by row number."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.times: array[int] = array("Q")  # each row's time, as a segment has
        self._postings: dict[str, array[int]] = {}

    def __len__(self) -> int:
        return len(self.events)

    @property
    def rows(self) -> int:
        return len(self.events)

    def add(self, event: Event, terms: Iterable[str]) -> None:
        row = len(self.events)
        self.events.append(event)
        self.times.append(event.time)
        for term in terms:
            postings = self._postings.get(term)
            if postings is None:
                postings = self._postings[term] = array("I")
            postings.append(row)

    def lookup(self, key: str) -> Sequence[int]:
        return self._postings.get(key, _EMPTY)

    def with_prefix(self, prefix: str) -> Iterator[tuple[str, Sequence[int]]]:
        for term, postings in self._postings.items():
            if term.startswith(prefix):
                yield term, postings


class Source(Protocol):
    """What evaluation needs from an index: a segment's, or the memtable's."""

    @property
    def rows(self) -> int: ...

    @property
    def times(self) -> Sequence[int]: ...

    def lookup(self, key: str) -> Sequence[int]: ...

    def with_prefix(self, prefix: str) -> Iterator[tuple[str, Sequence[int]]]: ...


def evaluate(node: Node | None, source: Source) -> tuple[set[int] | None, bool]:
    """The rows that may match, and whether they are exactly the matches.

    The rows are always a superset of the matches (None means every row).
    Most terms are exact. A phrase is not: the index knows its words are in
    a row, not that they are consecutive. Inexact candidates must be checked
    against the events themselves, with ``query.matches``.
    """
    rows, exact = _evaluate(node, source)
    if rows is not None and not rows:
        exact = True  # no candidates: certainly no matches
    return rows, exact


def _evaluate(node: Node | None, source: Source) -> tuple[set[int] | None, bool]:
    match node:
        case None:
            return None, True
        case FieldTerm():
            rows: set[int] = set()
            for key in node.keys:  # any one of its spellings
                rows.update(source.lookup(key))
            return rows, True
        case WordTerm() | ExistsTerm():
            return set(source.lookup(node.key)), True
        case PhraseTerm():
            keys = iter(node.keys)
            rows = set(source.lookup(next(keys)))
            for key in keys:
                rows.intersection_update(source.lookup(key))
            return rows, False
        case RangeTerm():
            rows = set()
            cut = len(node.prefix)
            for term, postings in source.with_prefix(node.prefix):
                if node.accepts(term[cut:]):
                    rows.update(postings)
            return rows, True
        case Not(child):
            child_rows, child_exact = evaluate(child, source)
            if not child_exact:
                # The complement of a superset is not a superset: every row
                # is a candidate, and each must be checked.
                return None, False
            if child_rows is None:
                return set(), True
            return set(range(source.rows)) - child_rows, True
        case And(children):
            result: set[int] | None = None
            exact = True
            for child in children:
                child_rows, child_exact = evaluate(child, source)
                exact = exact and child_exact
                if child_rows is not None:
                    result = child_rows if result is None else result & child_rows
                    if not result:
                        return set(), True
            return result, exact
        case Or(children):
            union: set[int] = set()
            exact = True
            for child in children:
                child_rows, child_exact = evaluate(child, source)
                if child_rows is None:
                    # Every row may match; exactly so only if this child is
                    # exact (then every row does match).
                    return None, child_exact
                union |= child_rows
                exact = exact and child_exact
            return union, exact
    raise TypeError(node)

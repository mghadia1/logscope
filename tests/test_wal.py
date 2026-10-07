import os
from pathlib import Path

import pytest

from logscope import wal


def batch(*names: str) -> list[tuple[int, bytes]]:
    return [(1000 + i, b'{"msg":"%s"}' % name.encode()) for i, name in enumerate(names)]


def write(path: Path, *batches: tuple[int, list[tuple[int, bytes]]]) -> list[int]:
    """Append records; return the offset where each one starts."""
    writer = wal.Writer(path)
    offsets = []
    for first_id, events in batches:
        offsets.append(path.stat().st_size)
        writer.append(first_id, events)
    writer.close()
    return offsets


def test_records_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "1.wal"
    write(path, (1, batch("a", "b")), (3, batch("c")))
    result = wal.read(path)
    assert [(r.first_id, r.last_id) for r in result.records] == [(1, 2), (3, 3)]
    assert result.records[0].events == [(1000, {"msg": "a"}), (1001, {"msg": "b"})]
    assert result.torn_bytes == 0


def test_a_record_cut_off_at_any_byte_is_a_torn_tail(tmp_path: Path) -> None:
    # A crash can stop a write anywhere. Cut the last record at every byte:
    # the records before it must survive, and the cut one must not appear.
    path = tmp_path / "1.wal"
    _, last = write(path, (1, batch("a", "b")), (3, batch("c", "d")))
    whole = path.read_bytes()
    for cut in range(last, len(whole)):
        path.write_bytes(whole[:cut])
        result = wal.read(path)
        assert [r.first_id for r in result.records] == [1], f"cut at {cut}"
        assert result.valid_end == last
        assert result.torn_bytes == cut - last


@pytest.mark.parametrize(
    "tail",
    [b"\0" * 4096, b"LSW1" + b"\0" * 100, b"\xff" * 7, b"LSW1\xff\xff\xff\xff"],
    ids=["zeros", "magic then zeros", "garbage", "huge length"],
)
def test_junk_after_the_last_record_is_a_torn_tail(tmp_path: Path, tail: bytes) -> None:
    # A file extended by a write whose data never reached the disk reads as
    # zeros; a zero-length record must not count as a record.
    path = tmp_path / "1.wal"
    write(path, (1, batch("a")))
    good = path.stat().st_size
    with open(path, "ab") as file:
        file.write(tail)
    result = wal.read(path)
    assert [r.first_id for r in result.records] == [1]
    assert result.valid_end == good


def test_a_damaged_record_followed_by_a_good_one_is_corruption(tmp_path: Path) -> None:
    # A crash only damages the last, unacknowledged record. A bad record
    # with a good one after it means acknowledged data was damaged.
    path = tmp_path / "1.wal"
    _, second, _ = write(path, (1, batch("a")), (2, batch("b")), (3, batch("c")))
    data = bytearray(path.read_bytes())
    data[second + wal.HEADER.size + 3] ^= 0xFF  # flip a byte in record 2's payload
    path.write_bytes(bytes(data))
    with pytest.raises(wal.WALCorruption, match=f"byte {second}"):
        wal.read(path)


def test_a_damaged_last_record_is_a_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "1.wal"
    _, last = write(path, (1, batch("a")), (2, batch("b")))
    data = bytearray(path.read_bytes())
    data[-2] ^= 0xFF
    path.write_bytes(bytes(data))
    result = wal.read(path)
    assert [r.first_id for r in result.records] == [1]
    assert result.valid_end == last


def test_a_failed_append_is_cut_off_and_the_writer_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "1.wal"
    writer = wal.Writer(path)
    writer.append(1, batch("a"))
    good = path.stat().st_size

    def failing_fsync(fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="Input/output error"):
        writer.append(2, batch("b"))
    monkeypatch.undo()
    # The partial record was cut off, so it cannot sit in front of later
    # records; and the writer refuses to go on (no retry after a failed fsync).
    assert path.stat().st_size == good
    with pytest.raises(OSError, match="append failed"):
        writer.append(2, batch("b"))
    writer.close()

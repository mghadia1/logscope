"""Turning a request body into events: newline-delimited JSON (NDJSON), one
event per line.

A bad line is reported with its line number and skipped; the rest of the
batch is stored. A batch over the limits is refused whole, so a client
(Milestone 6's shipper) can split it and retry.
"""

from dataclasses import dataclass

from .events import LineError, ParsedLine, parse_line


class BatchTooLarge(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BadLine:
    line: int  # 1-based
    error: str


def parse_batch(
    body: bytes,
    received: int,
    *,
    max_events: int,
    max_line_bytes: int,
    oldest: int | None = None,
) -> tuple[list[ParsedLine], list[BadLine]]:
    """``oldest``: events before this time (the retention cutoff) are
    rejected; storing them would only have them deleted at the next pass."""
    parsed: list[ParsedLine] = []
    bad: list[BadLine] = []
    for number, raw in enumerate(body.split(b"\n"), start=1):
        if not raw.strip():
            continue  # blank lines, including the one after a final newline
        if len(parsed) + len(bad) == max_events:
            raise BatchTooLarge(f"more than {max_events} events in one request")
        if len(raw) > max_line_bytes:
            bad.append(
                BadLine(number, f"{len(raw)} bytes; the limit is {max_line_bytes}")
            )
            continue
        try:
            line = parse_line(raw, received)
        except LineError as error:
            bad.append(BadLine(number, str(error)))
            continue
        if oldest is not None and line.time < oldest:
            bad.append(BadLine(number, "older than the retention period"))
            continue
        parsed.append(line)
    return parsed, bad

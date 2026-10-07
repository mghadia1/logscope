"""Events: parsing ingested lines, and their timestamps.

An event is one JSON object. Its time comes from the first of
``TIME_FIELDS`` it has: an ISO 8601 string with a time zone, or Unix
seconds. An event with none of them gets the time it was received.
Internally times are integer microseconds since the Unix epoch, UTC.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# The field names SurgiServe ("ts") and Gatekeeper ("time", Go's slog) use,
# and two other common ones.
TIME_FIELDS = ("time", "ts", "timestamp", "@timestamp")

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
# Timestamps outside 1970-2200 are rejected: a number of milliseconds read as
# seconds lands tens of thousands of years from now.
MIN_TIME = 0
MAX_TIME = (datetime(2200, 1, 1, tzinfo=UTC) - EPOCH) // _MICROSECOND

# Objects and arrays nested deeper than this are rejected. Pydantic cannot
# serialize past about 255 levels, so one deeper event would turn every
# search that returns it into a 500 (checked); real logs stay far below 64.
MAX_DEPTH = 64


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    time: int  # microseconds since the Unix epoch, UTC
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ParsedLine:
    time: int
    data: dict[str, Any]
    # The event re-encoded as compact UTF-8 JSON, ready for the write-ahead
    # log. Encoding here, per line, is also what catches the values that
    # would only fail later, when an API response is encoded.
    encoded: bytes


class LineError(ValueError):
    """A line that cannot be stored; the message says why."""


def _reject_constant(name: str) -> None:
    raise LineError(f"{name} is not valid JSON")


def parse_line(line: bytes, received: int) -> ParsedLine:
    try:
        # NaN and Infinity are accepted by Python's json module, but are not
        # JSON, and the API's responses refuse to encode them.
        data = json.loads(line, parse_constant=_reject_constant)
    except RecursionError:
        raise LineError(f"nested more than {MAX_DEPTH} levels deep") from None
    except LineError:
        raise
    except ValueError as error:  # JSONDecodeError, UnicodeDecodeError
        raise LineError(f"not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise LineError(f"not a JSON object (got {_type_name(data)})")
    if _too_deep(data):
        raise LineError(f"nested more than {MAX_DEPTH} levels deep")
    try:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    except UnicodeEncodeError:
        # "\ud800" in JSON decodes to a lone surrogate, which is not
        # encodable as UTF-8.
        raise LineError("contains an unpaired surrogate (\\ud800-\\udfff)") from None
    for field in TIME_FIELDS:
        if field in data:
            return ParsedLine(parse_time(data[field], field), data, encoded)
    return ParsedLine(received, data, encoded)


def parse_time(value: object, field: str) -> int:
    """A timestamp as microseconds since the epoch; raises LineError."""
    if isinstance(value, bool):  # bool is an int subclass: True is not 1 s
        raise LineError(f"{field}: expected a timestamp, got a boolean")
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise LineError(
                f"{field}: not an ISO 8601 timestamp: {value[:40]!r}"
            ) from None
        if moment.tzinfo is None:
            # Guessing UTC would misplace a local-time log by hours.
            raise LineError(f"{field}: timestamp has no time zone: {value[:40]!r}")
        micros = (moment - EPOCH) // _MICROSECOND
    elif isinstance(value, int | float):
        # Range-check the seconds before converting: 1e308 is finite, but
        # times a million it overflows to infinity, which round() rejects.
        if not MIN_TIME <= value < MAX_TIME / 1_000_000:
            raise LineError(
                f"{field}: {value!r} is outside 1970-2200"
                " (Unix seconds expected, not milliseconds)"
            )
        micros = round(value * 1_000_000)
    else:
        raise LineError(
            f"{field}: expected an ISO 8601 string or Unix seconds,"
            f" got {_type_name(value)}"
        )
    if not MIN_TIME <= micros < MAX_TIME:
        raise LineError(
            f"{field}: {value!r} is outside 1970-2200"
            " (Unix seconds expected, not milliseconds)"
        )
    return micros


def _too_deep(value: dict[str, Any]) -> bool:
    """Whether objects and arrays nest more than MAX_DEPTH levels; iterative,
    so the check itself cannot run out of stack."""
    stack: list[tuple[dict[str, Any] | list[Any], int]] = [(value, 1)]
    while stack:
        container, depth = stack.pop()
        if depth > MAX_DEPTH:
            return True
        children = container.values() if isinstance(container, dict) else container
        for child in children:
            if isinstance(child, dict | list):
                stack.append((child, depth + 1))
    return False


def format_time(micros: int) -> str:
    """ISO 8601 in UTC with microseconds, e.g. 2026-10-06T21:04:08.123456Z."""
    moment = EPOCH + micros * _MICROSECOND
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def now() -> int:
    return (datetime.now(UTC) - EPOCH) // _MICROSECOND


def _type_name(value: object) -> str:
    return {
        dict: "an object",
        list: "an array",
        str: "a string",
        int: "a number",
        float: "a number",
        bool: "a boolean",
        type(None): "null",
    }.get(type(value), type(value).__name__)

import json

import pytest

from logscope.events import LineError, format_time, parse_line, parse_time

RECEIVED = 1_700_000_000_000_000  # 2023-11-14T22:13:20Z


def time_of(line: dict[str, object]) -> int:
    return parse_line(json.dumps(line).encode(), RECEIVED).time


def test_gatekeeper_slog_time_with_nanoseconds() -> None:
    # Go's slog writes RFC 3339 with nanoseconds; microseconds are kept.
    t = time_of({"time": "2026-10-06T21:04:08.123456789Z", "msg": "request"})
    assert format_time(t) == "2026-10-06T21:04:08.123456Z"


def test_surgiserve_ts_with_an_offset_is_converted_to_utc() -> None:
    t = time_of({"ts": "2026-10-06T14:04:08.123-07:00", "event": "job done"})
    assert format_time(t) == "2026-10-06T21:04:08.123000Z"


def test_time_fields_are_tried_in_order() -> None:
    t = time_of({"timestamp": "2026-01-01T00:00:00Z", "time": "2026-06-01T00:00:00Z"})
    assert format_time(t).startswith("2026-06-01")


def test_unix_seconds_int_and_float() -> None:
    assert time_of({"ts": 1_700_000_000}) == 1_700_000_000_000_000
    assert time_of({"ts": 1_700_000_000.25}) == 1_700_000_000_250_000


def test_no_time_field_means_the_time_received() -> None:
    assert time_of({"msg": "no time here"}) == RECEIVED


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("2026-10-06T21:04:08", "no time zone"),
        ("yesterday", "not an ISO 8601 timestamp"),
        (True, "boolean"),
        (1_700_000_000_000, "outside 1970-2200"),  # milliseconds, not seconds
        (1e308, "outside 1970-2200"),  # finite, but overflows as microseconds
        (-1, "outside 1970-2200"),
        ([2026], "got an array"),
        (None, "got null"),
    ],
)
def test_bad_timestamps_are_rejected_with_a_reason(value: object, message: str) -> None:
    with pytest.raises(LineError, match=message):
        time_of({"time": value})


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (b"not json", "not valid JSON"),
        (b"[1, 2]", "not a JSON object \\(got an array\\)"),
        (b'"text"', "got a string"),
        (b'{"x": NaN}', "NaN is not valid JSON"),
        (b'{"x": -Infinity}', "-Infinity is not valid JSON"),
        (b'{"x": "\\ud800"}', "unpaired surrogate"),
        (b'{"x": "\xff"}', "not valid JSON"),  # not UTF-8
        (b'{"a":' * 65 + b"1" + b"}" * 65, "nested more than 64 levels"),
        (b'{"a":' + b"[" * 64 + b"]" * 64 + b"}", "nested more than 64 levels"),
    ],
    ids=lambda value: value[:24] if isinstance(value, bytes) else None,
)
def test_bad_lines_are_rejected_with_a_reason(line: bytes, message: str) -> None:
    with pytest.raises(LineError, match=message):
        parse_line(line, RECEIVED)


def test_nesting_up_to_the_limit_is_accepted() -> None:
    parse_line(b'{"a":' * 64 + b"1" + b"}" * 64, RECEIVED)
    parse_line(b'{"a":' + b"[" * 63 + b"]" * 63 + b"}", RECEIVED)


def test_the_event_is_kept_as_sent_and_encoded_compactly() -> None:
    parsed = parse_line(b'{ "msg" : "caf\\u00e9",  "n": 1 }', RECEIVED)
    assert parsed.data == {"msg": "café", "n": 1}
    assert parsed.encoded == '{"msg":"café","n":1}'.encode()


def test_format_time_is_exact() -> None:
    # Integer arithmetic, not float seconds: no rounding drift.
    assert format_time(0) == "1970-01-01T00:00:00.000000Z"
    t = parse_time("2026-10-06T21:04:08.999999Z", "time")
    assert format_time(t) == "2026-10-06T21:04:08.999999Z"

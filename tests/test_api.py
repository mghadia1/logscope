import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from logscope.app import create_app
from logscope.settings import Settings
from logscope.store import StoreLocked


def settings(data_dir: Path, **overrides: object) -> Settings:
    return Settings(data_dir=data_dir, ui_dir=data_dir / "no-ui", **overrides)  # type: ignore[arg-type]


@pytest.fixture
def api(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(settings(tmp_path / "data"))) as client:
        yield client


def ndjson(*lines: str) -> bytes:
    return "\n".join(lines).encode() + b"\n"


def test_ingest_stores_good_lines_and_reports_bad_ones(api: TestClient) -> None:
    body = ndjson(
        '{"time": "2026-10-06T21:00:00Z", "level": "INFO", "msg": "one"}',
        "",
        "not json",
        '{"time": "2026-10-06T21:00:01Z", "msg": "two"}',
        '{"time": "2026-10-06T21:00:02"}',
    )
    response = api.post("/api/ingest", content=body)
    assert response.status_code == 200
    assert response.json() == {
        "accepted": 2,
        "rejected": 2,
        "first_id": 1,
        "last_id": 2,
        "errors": [
            {
                "line": 3,
                "error": "not valid JSON: Expecting value: line 1 column 1 (char 0)",
            },
            {
                "line": 5,
                "error": "time: timestamp has no time zone: '2026-10-06T21:00:02'",
            },
        ],
    }


def test_search_returns_newest_first_with_iso_times(api: TestClient) -> None:
    api.post(
        "/api/ingest",
        content=ndjson(
            '{"ts": "2026-10-06T14:00:00-07:00", "msg": "older"}',
            '{"ts": "2026-10-06T21:00:05Z", "msg": "newer"}',
        ),
    )
    body = api.get("/api/search").json()
    assert [e["event"]["msg"] for e in body["events"]] == ["newer", "older"]
    assert body["events"][1]["time"] == "2026-10-06T21:00:00.000000Z"
    assert body["next_cursor"] is None
    assert body["stats"]["events_read"] == 2


def test_search_by_time_range_and_pages(api: TestClient) -> None:
    api.post(
        "/api/ingest",
        content=ndjson(
            *[f'{{"time": "2026-10-06T21:00:{s:02d}Z", "n": {s}}}' for s in range(10)]
        ),
    )
    ranged = api.get(
        "/api/search",
        params={"start": "2026-10-06T21:00:03Z", "end": "2026-10-06T21:00:06Z"},
    ).json()
    assert [e["event"]["n"] for e in ranged["events"]] == [5, 4, 3]

    seen, cursor = [], None
    while True:
        params: dict[str, str | int] = {"limit": 4}
        if cursor:
            params["cursor"] = cursor
        page = api.get("/api/search", params=params).json()
        seen += [e["event"]["n"] for e in page["events"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == list(range(9, -1, -1))


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"start": "tomorrow"}, "start: not an ISO 8601 timestamp"),
        ({"end": "2026-10-06T21:00:00"}, "end: timestamp has no time zone"),
        (
            {"start": "2026-10-07T00:00:00Z", "end": "2026-10-06T00:00:00Z"},
            "start must be before end",
        ),
        ({"cursor": "abc"}, "not a cursor"),
    ],
)
def test_bad_search_parameters_are_422_with_a_reason(
    api: TestClient, params: dict[str, str], detail: str
) -> None:
    response = api.get("/api/search", params=params)
    assert response.status_code == 422
    assert detail in response.json()["detail"]


def test_limit_is_bounded(api: TestClient) -> None:
    assert api.get("/api/search", params={"limit": 1001}).status_code == 422
    assert api.get("/api/search", params={"limit": 0}).status_code == 422


def test_bodies_over_the_limit_are_413_declared_or_not(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path, max_body_bytes=100))) as api:
        line = b'{"msg": "' + b"x" * 200 + b'"}\n'
        assert api.post("/api/ingest", content=line).status_code == 413
        # No Content-Length: the body arrives in chunks and is counted as read.
        chunked = api.post("/api/ingest", content=iter([line[:60], line[60:]]))
        assert chunked.status_code == 413
        assert api.get("/api/stats").json()["next_id"] == 1  # nothing stored


def test_too_many_events_is_413_and_stores_nothing(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path, max_batch_events=3))) as api:
        response = api.post("/api/ingest", content=ndjson(*['{"msg": "x"}'] * 4))
        assert response.status_code == 413
        assert "more than 3 events" in response.json()["detail"]
        assert (
            api.post("/api/ingest", content=ndjson(*['{"msg": "x"}'] * 3)).json()[
                "accepted"
            ]
            == 3
        )


def test_a_long_line_is_rejected_on_its_own(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path, max_line_bytes=50))) as api:
        body = ndjson('{"msg": "' + "x" * 60 + '"}', '{"msg": "short"}')
        result = api.post("/api/ingest", content=body).json()
        assert (result["accepted"], result["rejected"]) == (1, 1)
        assert result["errors"][0]["error"] == "71 bytes; the limit is 50"


def test_the_deepest_allowed_event_can_be_searched(api: TestClient) -> None:
    # Pydantic fails past ~255 levels; the ingest limit (64) keeps every
    # stored event serializable, so one event cannot break search.
    deep = '{"a":' * 64 + "1" + "}" * 64
    assert api.post("/api/ingest", content=deep.encode()).json()["accepted"] == 1
    assert api.get("/api/search").status_code == 200


def test_a_failed_fsync_makes_ingest_503_and_reads_still_work(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.post("/api/ingest", content=ndjson('{"msg": "kept"}'))

    def failing_fsync(fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    response = api.post("/api/ingest", content=ndjson('{"msg": "lost"}'))
    monkeypatch.undo()
    assert response.status_code == 503
    assert "could not be written" in response.json()["detail"]
    assert api.post("/api/ingest", content=ndjson('{"msg": "x"}')).status_code == 503
    assert api.get("/api/health").status_code == 200
    assert [e["event"]["msg"] for e in api.get("/api/search").json()["events"]] == [
        "kept"
    ]


def test_events_survive_a_restart(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path))) as api:
        api.post("/api/ingest", content=ndjson('{"msg": "before restart"}'))
    with TestClient(create_app(settings(tmp_path))) as api:
        events = api.get("/api/search").json()["events"]
        assert [e["event"]["msg"] for e in events] == ["before restart"]
        stats = api.get("/api/stats").json()
        assert (stats["segments"], stats["events_in_memory"]) == (1, 0)


def test_a_second_app_on_the_same_directory_does_not_start(api: TestClient) -> None:
    data_dir = api.app.state.store.data_dir  # type: ignore[attr-defined]
    with pytest.raises(StoreLocked):
        with TestClient(create_app(settings(data_dir))):
            pass


GATEKEEPER_AND_SURGISERVE = ndjson(
    # Shaped like the two earlier projects' logs: Go's slog, and SurgiServe's.
    '{"time": "2026-10-06T21:00:00.123456789Z", "level": "INFO", "msg": "request",'
    ' "service": "gatekeeper", "route": "GET /users/{id}", "status": 200}',
    '{"time": "2026-10-06T21:00:01Z", "level": "ERROR", "msg": "upstream timeout",'
    ' "service": "gatekeeper", "route": "GET /users/{id}", "status": 504}',
    '{"ts": "2026-10-06T21:00:02.000+00:00", "level": "error", "service": "surgiserve",'
    ' "event": "job failed", "job_id": "3f2a-77", "attempt": 2}',
)


@pytest.mark.parametrize("use_index", ["true", "false"])
@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("level:error", ["job failed", "upstream timeout"]),
        ("service:gatekeeper status:504", ["upstream timeout"]),
        ("timeout", ["upstream timeout"]),
        ("3f2a-77", ["job failed"]),
        ("route:GET", []),  # a field value is matched whole, not by word
        ("", ["job failed", "upstream timeout", "request"]),
    ],
)
def test_query_terms_filter_with_and_without_the_index(
    api: TestClient, q: str, expected: list[str], use_index: str
) -> None:
    api.post("/api/ingest", content=GATEKEEPER_AND_SURGISERVE)
    body = api.get("/api/search", params={"q": q, "index": use_index}).json()
    found = [e["event"].get("msg") or e["event"]["event"] for e in body["events"]]
    assert found == expected


def test_a_bad_query_is_422_with_the_position_of_the_problem(api: TestClient) -> None:
    response = api.get("/api/search", params={"q": "level:error (status>=500"})
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "this ( is never closed",
        "position": 12,
    }


@pytest.mark.parametrize("use_index", ["true", "false"])
@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ('"upstream timeout"', ["upstream timeout"]),
        ('"timeout upstream"', []),  # both words, wrong order
        ("status>=500", ["upstream timeout"]),
        ("attempt>=2", ["job failed"]),
        ("level:error NOT service:surgiserve", ["upstream timeout"]),
        ("service:surgiserve OR status<300", ["job failed", "request"]),
        ("job_id:*", ["job failed"]),
        ('route:"GET /users/{id}" NOT timeout', ["request"]),
        ("NOT (level:error OR status:*)", []),
    ],
)
def test_the_query_language_over_http(
    api: TestClient, q: str, expected: list[str], use_index: str
) -> None:
    api.post("/api/ingest", content=GATEKEEPER_AND_SURGISERVE)
    body = api.get("/api/search", params={"q": q, "index": use_index}).json()
    found = [e["event"].get("msg") or e["event"]["event"] for e in body["events"]]
    assert found == expected


def test_the_answer_shows_how_the_query_was_grouped(api: TestClient) -> None:
    body = api.get("/api/search", params={"q": "level:error OR status>=500 timeout"})
    assert body.json()["query"] == "level:error OR (status>=500 timeout)"


def test_search_stats_show_what_the_index_saved(api: TestClient) -> None:
    api.post("/api/ingest", content=GATEKEEPER_AND_SURGISERVE)
    api.app.state.store.flush()  # type: ignore[attr-defined]
    indexed = api.get("/api/search", params={"q": "timeout"}).json()["stats"]
    scanned = api.get("/api/search", params={"q": "timeout", "index": "false"}).json()[
        "stats"
    ]
    assert (indexed["events_read"], indexed["candidates"]) == (1, 1)
    assert indexed["checked"] == 0  # a word: the index alone decides
    assert (scanned["events_read"], scanned["checked"]) == (3, 3)
    phrase = api.get("/api/search", params={"q": '"upstream timeout"'}).json()
    assert phrase["stats"]["checked"] == 1  # the one candidate, checked


def test_histogram_over_http(api: TestClient) -> None:
    api.post("/api/ingest", content=GATEKEEPER_AND_SURGISERVE)
    body = api.get(
        "/api/aggregate/histogram",
        params={
            "q": "level:error",
            "start": "2026-10-06T21:00:00Z",
            "end": "2026-10-06T21:00:03Z",
            "interval": "1s",
        },
    ).json()
    assert body["interval_seconds"] == 1
    assert body["total"] == 2
    assert body["buckets"] == [
        {"start": "2026-10-06T21:00:00.000000Z", "count": 0},
        {"start": "2026-10-06T21:00:01.000000Z", "count": 1},
        {"start": "2026-10-06T21:00:02.000000Z", "count": 1},
    ]


def test_histogram_defaults_to_the_stored_range(api: TestClient) -> None:
    api.post("/api/ingest", content=GATEKEEPER_AND_SURGISERVE)
    body = api.get("/api/aggregate/histogram").json()
    assert body["total"] == 3
    assert body["interval_seconds"] == 1  # 2 s of events: the smallest step


def test_top_values_over_http(api: TestClient) -> None:
    api.post("/api/ingest", content=GATEKEEPER_AND_SURGISERVE)
    body = api.get("/api/aggregate/terms", params={"field": "level"}).json()
    assert body["values"] == [
        {"value": "error", "count": 2},  # "ERROR" and "error": one value
        {"value": "info", "count": 1},
    ]
    assert (body["matched"], body["with_field"], body["distinct"]) == (3, 3, 2)


def test_numeric_stats_over_http(api: TestClient) -> None:
    api.post("/api/ingest", content=GATEKEEPER_AND_SURGISERVE)
    body = api.get(
        "/api/aggregate/numeric", params={"field": "status", "q": "service:gatekeeper"}
    ).json()
    assert (body["count"], body["min"], body["max"], body["mean"]) == (2, 200, 504, 352)
    assert body["percentiles"] == {"p50": 200, "p90": 504, "p95": 504, "p99": 504}


@pytest.mark.parametrize(
    ("path", "params", "detail"),
    [
        ("histogram", {"interval": "5x"}, "not an interval"),
        ("histogram", {"interval": "0s"}, "not an interval"),
        (
            "histogram",
            {
                "interval": "1s",
                "start": "2026-10-06T00:00:00Z",
                "end": "2026-10-07T00:00:00Z",
            },
            "at most 2000",
        ),
        ("terms", {"field": "time"}, "the event's time"),
        ("numeric", {"field": "ts"}, "the event's time"),
    ],
)
def test_bad_aggregation_requests_are_422(
    api: TestClient, path: str, params: dict[str, str], detail: str
) -> None:
    response = api.get(f"/api/aggregate/{path}", params=params)
    assert response.status_code == 422
    assert detail in str(response.json()["detail"])


def test_events_older_than_the_retention_period_are_refused(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path, retention_hours=1))) as api:
        recent = datetime.now(UTC).isoformat()
        result = api.post(
            "/api/ingest",
            content=ndjson(
                f'{{"time": "{recent}", "msg": "kept"}}',
                '{"time": "2020-01-01T00:00:00Z", "msg": "too old"}',
            ),
        ).json()
        assert (result["accepted"], result["rejected"]) == (1, 1)
        assert result["errors"] == [
            {"line": 2, "error": "older than the retention period"}
        ]

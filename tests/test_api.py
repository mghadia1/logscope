import os
from collections.abc import Iterator
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
    assert body["stats"]["events_scanned"] == 2


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

"""The HTTP app: the JSON API under ``/api`` and the built React UI.

The UI is served by two explicit routes, ``/`` (index.html) and ``/assets``
(Vite's hashed bundles), not by a static mount at ``/``. A mount at ``/``
matches every path and method, so the file server, not the router, would
answer a request with the wrong method: ``GET`` on a POST-only route would
be a 404, and ``POST /api/health`` a 405 with no ``Allow`` header (both
checked). It is the same family of bug as Gatekeeper's catch-all 404.
"""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, aggregate
from .events import (
    MAX_TIME,
    MIN_TIME,
    TIME_FIELDS,
    Event,
    LineError,
    format_time,
    now,
    parse_time,
)
from .ingest import BatchTooLarge, parse_batch
from .query import EventView, Node, QueryError, matches
from .query import parse as parse_query
from .query import render as render_query
from .settings import Settings
from .store import SearchStats as StoreStats
from .store import Store, StoreUnavailable

# At most this many line errors are listed in an ingest response; the count
# of rejected lines is always exact.
MAX_LISTED_ERRORS = 100


class Health(BaseModel):
    status: str
    version: str


class LineErrorOut(BaseModel):
    line: int
    error: str


class IngestResult(BaseModel):
    accepted: int
    rejected: int
    # The ids given to the accepted events: first_id, ..., last_id.
    first_id: int | None
    last_id: int | None
    errors: list[LineErrorOut]


class EventOut(BaseModel):
    id: int
    time: str
    event: dict[str, Any]


class SearchStats(BaseModel):
    segments_total: int
    # Opened: the index consulted (or, without the index, every row read).
    segments_read: int
    # Never opened: outside the time range, or older than everything found.
    segments_skipped: int
    # Events parsed from segment files, plus events looked at in memory.
    events_read: int
    # Rows the index matched within the time range (0 without the index).
    candidates: int
    # Events tested against the query itself: where the index cannot decide
    # alone (a phrase), and every event without the index.
    checked: int
    took_ms: float


class SearchResult(BaseModel):
    # The query as understood, with its grouping explicit: "a OR b c" comes
    # back as "a OR (b c)".
    query: str
    events: list[EventOut]
    # Pass as ?cursor= for the next (older) page; null on the last page.
    next_cursor: str | None
    stats: SearchStats


class Bucket(BaseModel):
    start: str
    count: int


class HistogramResult(BaseModel):
    query: str
    interval_seconds: float
    total: int
    buckets: list[Bucket]
    stats: SearchStats


class ValueCount(BaseModel):
    value: str
    count: int


class TopValuesResult(BaseModel):
    query: str
    field: str
    matched: int  # events matching the query
    with_field: int  # ... that have the field
    distinct: int  # distinct values among them
    values: list[ValueCount]  # the most frequent, most first
    stats: SearchStats


class NumbersResult(BaseModel):
    query: str
    field: str
    matched: int
    count: int  # numeric values (an array of numbers counts each)
    min: int | float | None
    max: int | float | None
    mean: float | None
    percentiles: dict[str, int | float]  # nearest rank
    stats: SearchStats


class _Subscription:
    """One live-tail client. Batches are handed from the thread that stored
    them to the event loop, into a bounded queue: a client that cannot keep
    up loses events (and is told how many) rather than growing memory."""

    def __init__(self, loop: asyncio.AbstractEventLoop, batches: int) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[list[Event]] = asyncio.Queue(batches)
        self.dropped = 0

    def deliver(self, events: list[Event]) -> None:
        """Called by the store, from the ingesting thread."""
        try:
            self.loop.call_soon_threadsafe(self._put, events)
        except RuntimeError:
            pass  # the loop has closed: the server is shutting down

    def _put(self, events: list[Event]) -> None:
        try:
            self.queue.put_nowait(events)
        except asyncio.QueueFull:
            self.dropped += len(events)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(
            settings.data_dir,
            flush_events=settings.flush_events,
            flush_interval=settings.flush_interval_seconds,
            retention_hours=settings.retention_hours,
            merge_threshold=settings.merge_threshold,
        )
        # Recovery can refuse to start (another process has the directory,
        # or the data is damaged); then the app does not start either.
        await run_in_threadpool(store.open)
        store.start_background(
            maintenance_interval=settings.maintenance_interval_seconds
        )
        app.state.store = store
        # Set when shutdown begins (see end_tails), so live tails end at once
        # instead of holding the shutdown up.
        app.state.tails_end = asyncio.Event()
        app.state.loop = asyncio.get_running_loop()
        try:
            yield
        finally:
            await run_in_threadpool(store.close)

    app = FastAPI(
        title="LogScope",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(StoreUnavailable)
    async def unavailable(request: Request, error: StoreUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(error)})

    @app.get("/api/health")
    def health() -> Health:
        """Liveness: the process is up and answering."""
        return Health(status="ok", version=__version__)

    @app.post("/api/ingest")
    async def ingest(request: Request) -> IngestResult:
        """Store newline-delimited JSON events. Once this answers 200, the
        accepted events survive a crash."""
        body = await _read_body(request, settings.max_body_bytes)
        store: Store = request.app.state.store

        def parse_and_store() -> IngestResult:
            try:
                received = now()
                parsed, bad = parse_batch(
                    body,
                    received,
                    max_events=settings.max_batch_events,
                    max_line_bytes=settings.max_line_bytes,
                    oldest=received - store.retention if store.retention else None,
                )
            except BatchTooLarge as error:
                raise HTTPException(status_code=413, detail=str(error)) from None
            first_id = last_id = None
            if parsed:
                first_id, last_id = store.append(parsed)
            return IngestResult(
                accepted=len(parsed),
                rejected=len(bad),
                first_id=first_id,
                last_id=last_id,
                errors=[
                    LineErrorOut(line=b.line, error=b.error)
                    for b in bad[:MAX_LISTED_ERRORS]
                ],
            )

        # Parsing and the fsync both block; keep them off the event loop.
        return await run_in_threadpool(parse_and_store)

    @app.get("/api/search")
    def search(
        request: Request,
        q: str = "",
        start: str | None = None,
        end: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        cursor: str | None = None,
        index: bool = True,
    ) -> SearchResult:
        """Events matching ``q`` with start <= time < end, newest first.
        ``index=false`` scans every event instead (the baseline)."""
        query = _parse_query(q)
        start_time, end_time = _time_range(start, end, MIN_TIME, MAX_TIME)
        store: Store = request.app.state.store
        began = time.perf_counter()
        # One extra, to know whether there is a next page.
        events, stats = store.search(
            query, start_time, end_time, limit + 1, _cursor(cursor), use_index=index
        )
        took = (time.perf_counter() - began) * 1000
        page = events[:limit]
        next_cursor = None
        if len(events) > limit:
            next_cursor = f"{page[-1].time}:{page[-1].id}"
        return SearchResult(
            query=render_query(query),
            events=[
                EventOut(id=e.id, time=format_time(e.time), event=e.data) for e in page
            ],
            next_cursor=next_cursor,
            stats=_stats(stats, took),
        )

    @app.get("/api/aggregate/histogram")
    def histogram(
        request: Request,
        q: str = "",
        start: str | None = None,
        end: str | None = None,
        interval: str | None = None,
    ) -> HistogramResult:
        """How many events match ``q``, per time bucket. Without start and
        end, the range of the stored events; without an interval, one giving
        at most about 60 buckets."""
        query = _parse_query(q)
        store: Store = request.app.state.store
        bounds = store.time_bounds()
        low, high = bounds if bounds else (now(), now())
        start_time, end_time = _time_range(start, end, low, high + 1)
        width = (
            _interval(interval)
            if interval
            else aggregate.auto_interval(start_time, end_time)
        )
        began = time.perf_counter()
        try:
            result = aggregate.histogram(store, query, start_time, end_time, width)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        took = (time.perf_counter() - began) * 1000
        return HistogramResult(
            query=render_query(query),
            interval_seconds=width / 1_000_000,
            total=sum(result.counts),
            buckets=[
                Bucket(start=format_time(result.first + i * width), count=count)
                for i, count in enumerate(result.counts)
            ],
            stats=_stats(result.stats, took),
        )

    @app.get("/api/aggregate/terms")
    def terms(
        request: Request,
        field: Annotated[str, Query(min_length=1)],
        q: str = "",
        start: str | None = None,
        end: str | None = None,
        size: Annotated[int, Query(ge=1, le=100)] = 10,
    ) -> TopValuesResult:
        """The most frequent values of ``field`` among events matching ``q``.
        Values group the way queries match them: a value counted as "503"
        is what ``field:503`` finds."""
        _searchable_field(field)
        query = _parse_query(q)
        start_time, end_time = _time_range(start, end, MIN_TIME, MAX_TIME)
        began = time.perf_counter()
        result = aggregate.top_values(
            request.app.state.store, query, start_time, end_time, field, size
        )
        took = (time.perf_counter() - began) * 1000
        return TopValuesResult(
            query=render_query(query),
            field=field,
            matched=result.matched,
            with_field=result.with_field,
            distinct=result.distinct,
            values=[ValueCount(value=v, count=c) for v, c in result.values],
            stats=_stats(result.stats, took),
        )

    @app.get("/api/aggregate/numeric")
    def numeric(
        request: Request,
        field: Annotated[str, Query(min_length=1)],
        q: str = "",
        start: str | None = None,
        end: str | None = None,
    ) -> NumbersResult:
        """Count, min, max, mean and percentiles of the numbers in ``field``
        (numbers and numeric strings) among events matching ``q``."""
        _searchable_field(field)
        query = _parse_query(q)
        start_time, end_time = _time_range(start, end, MIN_TIME, MAX_TIME)
        began = time.perf_counter()
        result = aggregate.numbers(
            request.app.state.store, query, start_time, end_time, field
        )
        took = (time.perf_counter() - began) * 1000
        return NumbersResult(
            query=render_query(query),
            field=field,
            matched=result.matched,
            count=result.count,
            min=result.min,
            max=result.max,
            mean=result.mean,
            percentiles=result.percentiles,
            stats=_stats(result.stats, took),
        )

    @app.get("/api/tail")
    async def tail(request: Request, q: str = "") -> StreamingResponse:
        """Events matching ``q`` as they are stored, as Server-Sent Events,
        in the order they arrive (not by time). Nothing before the connection
        is replayed."""
        query = _parse_query(q)
        store: Store = request.app.state.store
        subscription = _Subscription(
            asyncio.get_running_loop(), settings.tail_queue_batches
        )
        store.subscribe(subscription.deliver)

        async def stream() -> AsyncIterator[str]:
            ending = asyncio.ensure_future(request.app.state.tails_end.wait())
            try:
                yield ": tailing\n\n"
                while True:
                    getting = asyncio.ensure_future(subscription.queue.get())
                    await asyncio.wait(
                        {getting, ending},
                        timeout=settings.tail_heartbeat_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not getting.done():
                        getting.cancel()
                        if ending.done():
                            # The server is shutting down: say so, and end.
                            yield _sse("end", {"reason": "server shutting down"})
                            return
                        yield ": keep-alive\n\n"
                        continue
                    batch = getting.result()
                    if subscription.dropped:
                        yield _sse("dropped", {"events": subscription.dropped})
                        subscription.dropped = 0
                    for event in batch:
                        if matches(query, EventView(event.data)):
                            yield _sse("event", _event_json(event), event.id)
            finally:
                ending.cancel()
                store.unsubscribe(subscription.deliver)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/stats")
    def stats(request: Request) -> dict[str, Any]:
        """What the store holds: events in memory and in segments, WAL files."""
        store: Store = request.app.state.store
        return store.stats()

    _serve_ui(app, settings.ui_dir)
    return app


async def _read_body(request: Request, limit: int) -> bytes:
    """The request body, refusing (413) anything over ``limit`` bytes, whether
    or not the client declared its length."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"body over {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(status_code=413, detail=f"body over {limit} bytes")
    return bytes(body)


def end_tails(app: FastAPI) -> None:
    """End every live tail now. Called when shutdown begins (serve.py's
    signal handler), from any thread: uvicorn waits for open responses before
    the app's own shutdown, and a tail would otherwise never finish."""
    loop = getattr(app.state, "loop", None)
    if loop is not None:
        try:
            loop.call_soon_threadsafe(app.state.tails_end.set)
        except RuntimeError:
            pass  # the loop has already closed


def _parse_query(text: str) -> Node | None:
    try:
        return parse_query(text)
    except QueryError as error:
        # The position lets a client point at the problem.
        raise HTTPException(
            status_code=422,
            detail={"error": error.message, "position": error.position},
        ) from None


def _time_range(
    start: str | None, end: str | None, default_start: int, default_end: int
) -> tuple[int, int]:
    start_time = _query_time(start, "start", default_start)
    end_time = _query_time(end, "end", default_end)
    if start_time >= end_time:
        raise HTTPException(status_code=422, detail="start must be before end")
    return start_time, end_time


_INTERVAL = re.compile(r"(\d+)([smhd]?)")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _interval(text: str) -> int:
    """An interval like 30s, 5m, 1h or 1d (or plain seconds), in
    microseconds."""
    found = _INTERVAL.fullmatch(text)
    if found is None or int(found[1]) == 0:
        raise HTTPException(
            status_code=422, detail=f"not an interval: {text!r} (try 30s, 5m, 1h)"
        )
    return int(found[1]) * _UNITS[found[2]] * 1_000_000


def _searchable_field(field: str) -> None:
    if field in TIME_FIELDS:
        raise HTTPException(
            status_code=422,
            detail=f"{field} is the event's time: use the histogram",
        )


def _stats(stats: StoreStats, took: float) -> SearchStats:
    return SearchStats(
        segments_total=stats.segments_total,
        segments_read=stats.segments_read,
        segments_skipped=stats.segments_skipped,
        events_read=stats.events_read,
        candidates=stats.candidates,
        checked=stats.checked,
        took_ms=round(took, 3),
    )


def _event_json(event: Event) -> dict[str, Any]:
    return {"id": event.id, "time": format_time(event.time), "event": event.data}


def _sse(kind: str, data: dict[str, Any], event_id: int | None = None) -> str:
    """One Server-Sent Event. The JSON is on one line, as the format needs."""
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {kind}\ndata: {json.dumps(data)}\n\n"


def _query_time(value: str | None, name: str, default: int) -> int:
    if value is None:
        return default
    try:
        return parse_time(value, name)
    except LineError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


def _cursor(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    event_time, _, event_id = value.partition(":")
    if not (event_time.isdigit() and event_id.isdigit()):
        raise HTTPException(status_code=422, detail=f"not a cursor: {value!r}")
    return int(event_time), int(event_id)


def _serve_ui(app: FastAPI, ui_dir: Path) -> None:
    index = ui_dir / "index.html"
    assets = ui_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    def ui() -> FileResponse:
        if not index.is_file():
            raise HTTPException(
                status_code=404,
                detail="UI not built: run `make ui`, or use the Vite dev server",
            )
        # index.html names the current hashed bundles, so it must not be
        # cached; the bundles themselves can be.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


app = create_app()

"""The HTTP app: the JSON API under ``/api`` and the built React UI.

The UI is served by two explicit routes, ``/`` (index.html) and ``/assets``
(Vite's hashed bundles), not by a static mount at ``/``. A mount at ``/``
matches every path and method, so the file server, not the router, would
answer a request with the wrong method: ``GET`` on a POST-only route would
be a 404, and ``POST /api/health`` a 405 with no ``Allow`` header (both
checked). It is the same family of bug as Gatekeeper's catch-all 404.
"""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .events import MAX_TIME, MIN_TIME, LineError, format_time, now, parse_time
from .ingest import BatchTooLarge, parse_batch
from .settings import Settings
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
    segments_scanned: int
    events_scanned: int
    took_ms: float


class SearchResult(BaseModel):
    events: list[EventOut]
    # Pass as ?cursor= for the next (older) page; null on the last page.
    next_cursor: str | None
    stats: SearchStats


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(
            settings.data_dir,
            flush_events=settings.flush_events,
            flush_interval=settings.flush_interval_seconds,
        )
        # Recovery can refuse to start (another process has the directory,
        # or the data is damaged); then the app does not start either.
        await run_in_threadpool(store.open)
        store.start_flusher()
        app.state.store = store
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
                parsed, bad = parse_batch(
                    body,
                    now(),
                    max_events=settings.max_batch_events,
                    max_line_bytes=settings.max_line_bytes,
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
        start: str | None = None,
        end: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        cursor: str | None = None,
    ) -> SearchResult:
        """Events with start <= time < end, newest first."""
        start_time = _query_time(start, "start", MIN_TIME)
        end_time = _query_time(end, "end", MAX_TIME)
        if start_time >= end_time:
            raise HTTPException(status_code=422, detail="start must be before end")
        store: Store = request.app.state.store
        began = time.perf_counter()
        # One extra, to know whether there is a next page.
        events, stats = store.search(start_time, end_time, limit + 1, _cursor(cursor))
        took = (time.perf_counter() - began) * 1000
        page = events[:limit]
        next_cursor = None
        if len(events) > limit:
            next_cursor = f"{page[-1].time}:{page[-1].id}"
        return SearchResult(
            events=[
                EventOut(id=e.id, time=format_time(e.time), event=e.data) for e in page
            ],
            next_cursor=next_cursor,
            stats=SearchStats(
                segments_scanned=stats.segments_scanned,
                events_scanned=stats.events_scanned,
                took_ms=round(took, 3),
            ),
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

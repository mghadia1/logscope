# LogScope

A log search engine, built from the storage up. Services send JSON log
lines over HTTP; LogScope will write them to an append-only log, organize
them into time-partitioned segments with an inverted index, and answer
searches and aggregations through an API and a React UI. Its first real
data will be the JSON logs of two earlier projects, SurgiServe and
Gatekeeper.

It deliberately does not use Elasticsearch, OpenSearch, Loki, ClickHouse or
SQLite's full-text search: the point is to build the parts they provide.

> **Status:** Milestone 0 of 7, the skeleton. There is no ingestion, storage
> or search yet: the API answers `/api/health` and serves the UI shell.

## Status

| # | Milestone | Status |
|---|-----------|--------|
| 0 | Skeleton: FastAPI `/api/health`, React (Vite, JavaScript) shell served by the API, ruff + mypy --strict + pytest, ESLint + Vitest, CI, one-container Compose | Done |
| 1 | Ingestion and storage: `POST /api/ingest`, append-only write-ahead log with crash recovery, time-partitioned segments, search by time range by scanning | Not started |
| 2 | Inverted index per segment; segment time bounds to skip segments; indexed results checked against the scan | Not started |
| 3 | Query language (`level:error service:gatekeeper status>=500 "upstream timeout" NOT route:/healthz`), positioned errors; property test: index equals scan | Not started |
| 4 | Aggregations, live tail over Server-Sent Events, retention, merging small segments | Not started |
| 5 | React UI: query box, results, histogram, field sidebar, live tail | Not started |
| 6 | Real logs (SurgiServe, Gatekeeper) through a shipper; measured ingest rate and query latency, index vs scan | Not started |
| 7 | Architecture diagram, design decisions, failures found, benchmarks | Not started |

## Quickstart

Needs Docker.

```bash
make up        # build and start; waits until the health check passes
open http://localhost:8200
make down
```

`make smoke` brings the stack up, checks it over HTTP from outside, and
tears it down.

## Development

Needs Python 3.14 and Node 20.19 or newer.

```bash
make install   # .venv with the API and dev tools; the UI's node_modules
make check     # ruff, mypy --strict, pytest, ESLint, Vitest, UI build
make dev-api   # API on :8000, reloading on changes
make dev-ui    # UI on :5173, forwarding /api to :8000
```

## Layout

| Path | What |
|------|------|
| `src/logscope/app.py` | The FastAPI app: the API under `/api`, and the built UI |
| `src/logscope/settings.py` | Configuration from `LOGSCOPE_*` environment variables |
| `src/logscope/healthcheck.py` | Docker's health check (the image has no curl) |
| `ui/` | The React UI (JavaScript, Vite) |
| `scripts/smoke.sh` | Checks a running stack over HTTP |

## Decisions so far

- **One container.** The UI is built in a Node stage of the Dockerfile and
  only its static files reach the Python image, which serves them. No Node
  at runtime, no second service, and the browser talks to one origin.
- **The UI is served by explicit routes,** `/` and `/assets`, not a static
  mount at `/`. A mount at `/` matches every path and method, so the file
  server would answer wrong-method API calls: a `GET` on a POST-only route
  would be a 404, and `POST /api/health` a 405 with no `Allow` header (both
  checked). A test pins the 405 and its `Allow` header.
- **JavaScript, not TypeScript, for the UI:** TypeScript is not yet a
  language I can write and explain unaided.

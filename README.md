# LogScope

A log search engine, built from the storage up. Services send JSON log
lines over HTTP; LogScope writes them to an append-only write-ahead log,
organizes them into time-partitioned segment files, and answers searches
through an API and (later) a React UI. Its first real data will be the JSON
logs of two earlier projects, SurgiServe and Gatekeeper.

It deliberately does not use Elasticsearch, OpenSearch, Loki, ClickHouse or
SQLite's full-text search: the point is to build the parts they provide.

> **Status:** Milestones 0-1 of 7. Events can be ingested, survive crashes,
> and be searched by time range, by scanning everything. There is no index
> or query language yet (Milestones 2-3), and the UI is still a shell.

## Status

| # | Milestone | Status |
|---|-----------|--------|
| 0 | Skeleton: FastAPI `/api/health`, React (Vite, JavaScript) shell served by the API, ruff + mypy --strict + pytest, ESLint + Vitest, CI, one-container Compose | Done |
| 1 | Ingestion and storage: `POST /api/ingest`, append-only write-ahead log with crash recovery, time-partitioned segments, search by time range by scanning | Done |
| 2 | Inverted index per segment; segment time bounds to skip segments; indexed results checked against the scan | Not started |
| 3 | Query language (`level:error service:gatekeeper status>=500 "upstream timeout" NOT route:/healthz`), positioned errors; property test: index equals scan | Not started |
| 4 | Aggregations, live tail over Server-Sent Events, retention, merging small segments | Not started |
| 5 | React UI: query box, results, histogram, field sidebar, live tail | Not started |
| 6 | Real logs (SurgiServe, Gatekeeper) through a shipper; measured ingest rate and query latency, index vs scan | Not started |
| 7 | Architecture diagram, design decisions, failures found, benchmarks | Not started |

## Quickstart

Needs Docker.

```bash
make up        # build and start on :8200; waits until the health check passes
curl -X POST --data-binary @- localhost:8200/api/ingest <<'EOF'
{"time": "2026-10-06T21:00:00Z", "level": "INFO", "msg": "hello"}
EOF
curl 'localhost:8200/api/search?start=2026-10-06T00:00:00Z'
make down      # the data stays in a Docker volume; `make reset` deletes it
```

`make smoke` starts a separate, empty stack on :8201, checks it over HTTP
from outside (including killing the container with SIGKILL and checking
that nothing acknowledged was lost), and deletes it.

## API

**`POST /api/ingest`**: newline-delimited JSON, one event (a JSON object)
per line.

- An event's time comes from the first of `time`, `ts`, `timestamp` or
  `@timestamp` it has: an ISO 8601 string **with a time zone**, or Unix
  seconds. With none of them, it gets the time it was received.
- A bad line (not JSON, not an object, a timestamp without a time zone,
  NaN, nested over 64 levels, over 64 KiB) is rejected with its line number;
  the rest are stored. The answer lists the accepted ids and up to 100
  errors:
  `{"accepted": 2, "rejected": 1, "first_id": 1, "last_id": 2, "errors": [{"line": 2, "error": "not valid JSON: ..."}]}`
- A body over 8 MiB or 10,000 events is refused whole (413), so a client can
  split it. Whether the client declared the length or not: the body is
  counted as it is read.
- **A 200 means the accepted events are on disk** (fsynced) and survive a
  crash. If the disk fails, the answer is 503 and writes stay refused until
  a restart.

**`GET /api/search?start=&end=&limit=&cursor=`**: events with
`start <= time < end` (ISO 8601 with a time zone; both optional), newest
first, at most `limit` (1-1000, default 100). Pass `next_cursor` back as
`cursor` for the next page. The answer includes how much was scanned.

**`GET /api/stats`**: events in memory and in segments, WAL files, whether
writes are accepted.

**`GET /api/health`**: liveness only.

## How storage works

```
POST /api/ingest
   │  one record per batch: "LSW1" | length | CRC-32 | JSON
   ▼
 wal/00000000000000000123.wal ── fsync ──► 200 OK
   │
   ▼
 memtable (in memory, searchable at once)
   │  flush: 50,000 events, or the oldest has waited 60 s
   ▼
 segments/2026-10-06T21/…-….jsonl   one file per hour per flush
 MANIFEST.json                      which segments exist, and "flushed through" id
```

- **Every batch is one WAL record**, written and fsynced before the 200. The
  next record is written only after that, so a crash can damage only the
  last record, which was never acknowledged: recovery cuts that *torn tail*
  off.
- **A damaged record with a good one after it is not a crash** (acknowledged
  data was damaged), so recovery refuses to start and says where, rather
  than skipping it.
- **The manifest is the commit point of a flush.** Segment files are
  written first, then a new manifest names them and the highest id they
  hold; only then are old WAL files deleted. A crash before the manifest
  leaves orphan segment files, deleted on start, and the WAL still has the
  events; a crash after it leaves WAL records that recovery skips.
- **Ids are global and contiguous,** so recovery can check that every id is
  in a segment or the WAL. A gap is reported, not skipped.
- **Every file that matters is replaced atomically:** written under a
  temporary name, fsynced, renamed, and its directory fsynced.
- **One process per data directory,** enforced with a lock file; a second
  one refuses to start.
- **After a failed fsync, writes stop.** The kernel may have dropped the
  unwritten pages and cleared the error, so a retry could "succeed" without
  the data on disk (PostgreSQL's "fsyncgate"). The partial record is cut
  off, and ingest answers 503 until a restart.

Search in this milestone reads every segment and every event in memory:
deliberately, as the baseline the index in Milestone 2 is measured against.

### How it is tested

- Every byte position: the last WAL record is cut at each one, and recovery
  must keep exactly the records before it.
- Crashes injected at each step of a flush (before the manifest, after it
  but before the WAL cleanup), a failed flush followed by a good one, a
  missing record in the middle of the WAL, a second process, a failed fsync.
- **SIGKILL of a real process** while it ingests and flushes constantly:
  every batch it acknowledged must be there after recovery, once, with no
  gap in the ids; four kill-and-recover rounds per test. A one-off run of
  160 kills (40 seeds) lost and duplicated nothing.
- The smoke test SIGKILLs the container itself and checks that an event
  held only in the WAL comes back.

What is **not** tested: power loss. A SIGKILL leaves the operating system's
page cache intact; a power cut does not. On macOS, `fsync` does not even
flush the drive's cache (`F_FULLFSYNC` does); in the Docker image the
fsync goes to Linux, then through Docker Desktop's virtual disk.

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
| `src/logscope/events.py` | Parsing a line into an event; timestamps |
| `src/logscope/ingest.py` | Splitting a request body into lines; per-line errors |
| `src/logscope/wal.py` | The write-ahead log: record format, torn-tail detection |
| `src/logscope/store.py` | Memtable, flushes, segments, manifest, recovery, scan search |
| `src/logscope/settings.py` | Configuration from `LOGSCOPE_*` environment variables |
| `ui/` | The React UI (JavaScript, Vite) |
| `scripts/smoke.sh` | Checks a running stack over HTTP, including a SIGKILL |
| `tests/test_crash.py` | The SIGKILL test |

## Decisions so far

- **One container.** The UI is built in a Node stage of the Dockerfile and
  only its static files reach the Python image, which serves them.
- **The UI is served by explicit routes,** `/` and `/assets`, not a static
  mount at `/`: a mount at `/` lets the file server answer wrong-method API
  calls (a `GET` on a POST-only route becomes a 404, `POST /api/health` a
  405 with no `Allow` header; both checked).
- **Timestamps without a time zone are rejected,** not assumed UTC:
  guessing would misplace a local-time log by hours.
- **Bad lines are rejected one by one; oversized batches whole.** A bad line
  is the sender's data problem and retrying will not fix it; a batch too
  big can be split and retried.
- **JSON lines for segments, for now.** Readable and easy to check by eye;
  compression and an index come later.
- **JavaScript, not TypeScript, for the UI:** TypeScript is not yet a
  language I can write and explain unaided.

## Failures found while building it

1. **One deeply nested event would have broken search.** Pydantic cannot
   serialize past about 255 levels, so a single event nested deeper would
   make every search that returned it a 500. Found by checking before the
   API existed; ingest now rejects nesting over 64 levels, and a test
   searches an event at the limit.
2. **The editable install silently did nothing.** In an iCloud-synced
   folder, macOS marks everything inside `.venv` hidden (it comes back
   within a second of clearing it), and Python 3.13+ skips hidden `.pth`
   files, so `import logscope` failed outside pytest. `make dev-api` passes
   `--app-dir src` instead.
3. **The smoke test could not run twice.** Its data volume outlived it, so a
   second run found the first run's events. It now runs as its own Compose
   project with its own volume, deleted afterwards, so it never touches the
   data of `make up` either.

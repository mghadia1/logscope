# LogScope

A log search engine, built from the storage up. Services send JSON log
lines over HTTP; LogScope writes them to an append-only write-ahead log,
organizes them into time-partitioned segment files, and answers searches
through an API and (later) a React UI. Its first real data will be the JSON
logs of two earlier projects, SurgiServe and Gatekeeper.

It deliberately does not use Elasticsearch, OpenSearch, Loki, ClickHouse or
SQLite's full-text search: the point is to build the parts they provide.

> **Status:** Milestones 0-4 of 7. Events can be ingested, survive crashes,
> and be searched with a small query language through an inverted index;
> counted over time, by value and as numbers; and followed live. Old data
> expires and small segments are merged. The UI is still a shell
> (Milestone 5), and nothing has been measured yet (Milestone 6).

## Status

| # | Milestone | Status |
|---|-----------|--------|
| 0 | Skeleton: FastAPI `/api/health`, React (Vite, JavaScript) shell served by the API, ruff + mypy --strict + pytest, ESLint + Vitest, CI, one-container Compose | Done |
| 1 | Ingestion and storage: `POST /api/ingest`, append-only write-ahead log with crash recovery, time-partitioned segments, search by time range by scanning | Done |
| 2 | Inverted index per segment; segment time bounds to skip segments; indexed results checked against the scan | Done |
| 3 | Query language (`level:error service:gatekeeper status>=500 "upstream timeout" NOT route:/healthz`), positioned errors; property test: index equals scan | Done |
| 4 | Aggregations, live tail over Server-Sent Events, retention, merging small segments | Done |
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
curl -G localhost:8200/api/search --data-urlencode 'q=level:info hello'
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

**`GET /api/search?q=&start=&end=&limit=&cursor=&index=`**: events
matching `q` with `start <= time < end` (ISO 8601 with a time zone; both
optional), newest first, at most `limit` (1-1000, default 100). Pass
`next_cursor` back as `cursor` for the next page. `index=false` scans every
event instead (the baseline). The answer shows the query as it was
understood, with its grouping explicit (`a OR b c` comes back as
`a OR (b c)`), and says how many segments were read or skipped, how many
events were read, and how many had to be checked against the query.

A bad query is a 422 that says what is wrong and where:
`{"detail": {"error": "this ( is never closed", "position": 12}}`.

### The query language

```
level:error service:gatekeeper status>=500 "upstream timeout" NOT route:/healthz
```

| Term | Matches |
|------|---------|
| `field:value` | the field's whole value, case-insensitively: `level:error` finds "ERROR". Numbers match by value, whether written as numbers or as numeric strings: `status:503` finds 503, 503.0, "503" and "503.0" |
| `field:"a value"` | the same, quoted for spaces or `( ) "`: `route:"GET /users/{id}"` |
| `field:*` | the field is present (also an enclosing object: `http:*`) |
| `field>=N`, `>N`, `<=N`, `<N` | a numeric range, over numbers and numeric strings |
| `word` | the word anywhere in the event; `3f2a-77` (several parts) is a phrase |
| `"a phrase"` | the words, consecutively, within one value |

Combined, tightest first: `NOT x`; `x y` or `x AND y` (both); `x OR y`
(either); parentheses to group. `AND`, `OR` and `NOT` are operators only in
capitals: "not" is a word. Nested fields use dots (`http.method`); an
array's elements count as the array's field. The timestamp fields are
searched with `start` and `end`, not in `q`.

### Aggregations

All three take `q`, `start` and `end` like a search, count every matching
event (not a page), and say what they read.

**`GET /api/aggregate/histogram?interval=`**: matching events per time
bucket (`30s`, `5m`, `1h`, `1d`; by default the smallest of a list of round
widths giving at most about 60 buckets, over the stored range when `start`
and `end` are left out; at most 2,000 buckets).

**`GET /api/aggregate/terms?field=&size=`**: a field's most frequent values
among the matching events, with how many matched and how many had the
field. Values group the way queries match them, so the count shown for
`503` is exactly what `status:503` finds (503, 503.0 and "503" are one
value; "ERROR" and "error" are one).

**`GET /api/aggregate/numeric?field=`**: count, min, max, mean and the 50th,
90th, 95th and 99th percentiles (nearest rank, exact) of a field's numbers,
including numeric strings.

### Live tail

**`GET /api/tail?q=`**: Server-Sent Events: each newly stored event that
matches `q`, as `event: event` with its id, time and body, in the order they
arrive (not by time). Nothing stored before the connection is replayed. A
comment every 15 s keeps an idle connection open. A client that cannot keep
up gets `event: dropped` with how many events it missed, rather than the
server buffering without limit; when the server shuts down, `event: end`.

**`GET /api/stats`**: events in memory and in segments, WAL files, files
waiting to be deleted, the retention period, whether writes are accepted.

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
 segments/2026-10-06T21/…-….jsonl   one file per hour per flush,
                    …-….idx     with its index
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

### Merging, retention, and when files are deleted

Every 30 seconds, in the background:

- **Retention** (`LOGSCOPE_RETENTION_HOURS`, off by default) drops segments
  whose events are all older than the period. Ingest refuses events already
  past it, rather than storing what the next pass would delete.
- **Merging**: an hour with 8 or more segments (a flush every minute makes
  60 an hour) is merged into one: a k-way merge of the already sorted
  segments, the lines copied as they are, and a new index. Each merge of a
  busy hour rewrites what that hour holds so far; a size-tiered policy would
  write less, at the cost of more segments to search.
- Both commit by writing a new manifest, like a flush. **The old files are
  deleted only when no running search can still need them.** Every
  manifest change is a new version; a search records the version it
  started from; a file dropped at version *v* is deleted once every running
  search started at *v* or later. Deleting at once would break a search
  already part-way through: skipping a merged-away file would silently miss
  its events, which now live in a segment that search never saw.
- **New segment files are numbered** (`00000012-<first id>-<last id>`), and
  numbers are never reused. Two merges of one hour can cover the same ids;
  named by ids alone, the second would overwrite a file the manifest still
  lists.

## How search works

```
segments, newest first ──► outside the time range? skip
        │                  older than the top `limit` found so far? stop
        ▼
 segment index (.idx, memory-mapped)
   times + ids of every row ──► binary search: the rows in the time range
   term directory (sorted)  ──► binary search: a term's rows, or (ranges)
        │                       the run of one field's values
        │  the query tree, bottom up: candidate rows + "exact or not"
        ▼
 candidates, newest first ──► read the row ──► not exact? check it
                                               against the query
```

- **One function decides what an event contains,** `terms_of`, and both the
  index and the full scan use it. So they cannot disagree about what
  matches; only the index's machinery (writing, binary search,
  intersecting, stopping early) can be wrong, and that is what the tests
  compare with the scan.
- **Rows in a segment are sorted by (time, id),** and the index keeps each
  row's time and id. A search orders and filters rows without reading
  them, reads only the ones it returns, and stops once nothing older can
  make the top `limit`.
- **The index narrows; it does not have to decide.** Each part of a query
  gives a set of candidate rows that contains every match, and says
  whether it is exact. Most terms are exact. A phrase is not: the index
  knows its words are in a row, not that they are consecutive. That
  carries up the tree: `NOT` of something inexact cannot be a complement,
  so every row becomes a candidate; `AND` and `OR` are exact only if all
  their parts are. Inexact candidates are checked against the event with
  the same function the full scan uses.
- **Events still in memory** have their own in-memory index, so a search
  sees them at once, including those being flushed.
- **Ranges walk one field's values:** the directory is sorted, so a field's
  terms are one run, found by bisection, and each value is compared. A
  field with mostly distinct values (durations) makes that run as long as
  the segment, which makes ranges over such fields the slowest kind of
  query; a sorted numeric column per field would fix it.
- **Not indexed:** the timestamp fields (the time range covers them), and
  string values over 256 characters as exact values (they are still
  indexed word by word; asking for one exactly is refused, not missed).

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
- **The SIGKILL test merges too:** the killed process merges any hour with
  two segments, continuously, so kills land mid-merge; the test fails if no
  merge finished, so it cannot quietly stop testing them. A one-off run of
  80 more kills (20 seeds) with merging lost and duplicated nothing.
- Crashes before and after a merge's manifest; two merges of one hour; a
  merge in the middle of an aggregation, whose old files must stay readable
  until it finishes and be deleted after; retention's exact cutoff.
- **Aggregations against the scan:** for random queries and time ranges,
  every histogram bucket holds exactly the events the scan finds in it;
  every top value's count is exactly what searching for that value finds;
  numeric stats equal those of the scanned events. Six planted bugs (a
  bucket edge, an off-by-one in counting, values not grouped by number,
  events outside the time range, unchecked phrase candidates, every
  spelling of a number counted) each make them fail.
- **The live tail against a real server** (uvicorn in a thread): new
  matching events arrive and others do not; keep-alives; a client hanging
  up is unsubscribed; a SIGTERM ends open tails at once and the memtable is
  still flushed; and, to show why that matters, with uvicorn's default an
  open tail keeps the server running after it was asked to stop.
- **The index against the scan:** Hypothesis generates 600 combinations of
  a random query tree (AND, OR, NOT, phrases, ranges, fields that exist),
  time range, limit and cursor over a store whose segments overlap in time
  (late events), with some events still in memory; the index must return
  the same events in the same order as the full scan. Ten deliberately
  planted bugs (a tie-break, both ends of the time range, a union, the
  reading order, and five in how exactness combines) each make it fail.
- **The parser:** precedence, every term, 16 errors with their exact
  positions, and a Hypothesis property: any query, shown back and parsed
  again, gives the same tree.
- A search during a flush, with events both in the memtable being flushed
  and in the new one.

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
| `src/logscope/store.py` | Memtable, flushes, segments, manifest, recovery, search |
| `src/logscope/terms.py` | What an event is indexed under (`terms_of`) |
| `src/logscope/query.py` | The query language: lexer, parser, positioned errors; matching one event |
| `src/logscope/index.py` | Segment index files (build, memory-mapped lookup); the in-memory index; evaluating a query to candidates |
| `src/logscope/aggregate.py` | Histogram, top values and numeric stats, from the index |
| `src/logscope/serve.py` | Running uvicorn, ending live tails as soon as a shutdown signal arrives |
| `src/logscope/settings.py` | Configuration from `LOGSCOPE_*` environment variables |
| `ui/` | The React UI (JavaScript, Vite) |
| `scripts/smoke.sh` | Checks a running stack over HTTP, including a SIGKILL |
| `tests/test_crash.py` | The SIGKILL test |
| `tests/test_search.py` | The index against the scan (Hypothesis) |
| `tests/test_query.py` | The parser, its errors, and matching |
| `tests/test_aggregate.py` | Aggregations against the scan (Hypothesis) |
| `tests/test_maintenance.py` | Merging, retention, deferred deletion |
| `tests/test_tail.py` | The live tail against a real uvicorn server |

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
- **JSON lines for segments, beside a binary index.** The data stays
  readable by eye; the index is compact, memory-mapped, and needs no
  parsing to open.
- **Field values match case-insensitively, numbers by value.** `level:info`
  finds "INFO" (SurgiServe writes "info", Gatekeeper "INFO"), and
  `status:503` finds 503, 503.0, "503" and "503.0". A value is a number only
  in JSON's syntax, so `code:007` matches the string "007", not 7.
- **Operators only in capitals.** "not" and "or" are common in log messages;
  only `NOT` and `OR` are operators.
- **`status:>=500` is an error, not a search.** Other tools write ranges that
  way; here it would silently be an exact match on the text ">=500", so the
  error says to drop the colon.
- **Aggregations read the index, not the events.** A histogram with no
  query counts each bucket of a segment by two binary searches over its
  sorted times; top values intersect the field's postings with the
  matching rows. A number's canonical key ("503") holds every row with that
  number, however it was written, so a value's count is one postings list,
  not a union. (A union was there first; a planted bug showed it changed
  nothing, and why.)
- **A live tail matches each event in the event loop.** Fine for a few
  clients; many clients at a high ingest rate would want that work moved
  off the loop.
- **The answer shows the parsed query,** with its grouping explicit, so
  `a OR b c` meaning `a OR (b c)` is never a surprise.
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
4. **The test comparing the index with the scan caught nothing.** It passed
   400 generated cases, and still passed with each of three bugs planted on
   purpose. Its data could not reach them: events were spread over 21,600
   seconds, so equal times at segment edges almost never happened; few sat
   exactly on a range boundary; and no query value had two spellings. With
   times on a 5-minute grid and values like `503.0`, all five planted bugs
   now fail it. It happened again with the query language: a planted bug
   in how `OR` treats an inexact part survived, because random queries
   rarely put `NOT "a phrase"` inside an `OR`. Phrases are now half the
   generated terms, and six such shapes run every time.
5. **`status:503` and `status>=503` disagreed about "503.0".** Ranges read
   strings that are numbers as numbers; exact matches compared spellings.
   A test of one event against both caught it; a string that is a number
   now also gets its number's index key, so the two agree.
6. **`docker compose stop` killed LogScope before it could flush.** With a
   live tail open, a clean restart lost the shutdown flush (the events came
   back only by WAL replay). Two causes, both found by the smoke test: a
   tail never ends, and uvicorn waits for open responses before running
   the app's shutdown; and on this machine (Docker Desktop, Compose v5.0.2)
   `docker compose stop` sent SIGKILL after about a second, not Docker's
   usual ten (exit 137, checked by hand). Now a SIGTERM ends open tails at
   once, uvicorn cuts off anything else after 5 s, and `compose.yaml` sets
   `stop_grace_period: 15s` rather than relying on a default.
7. **The SIGKILL test had stopped testing merges.** It ran with merging on,
   but each run was killed after a tenth of a second, before any merge
   finished, and it passed anyway. A new check that merges really happened
   failed at once; with a lower threshold and longer runs, they do.

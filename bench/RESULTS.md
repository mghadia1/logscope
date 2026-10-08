# Load test results

One run, 2026-10-07 local time, at commit 74c630f:
`bench/results/20261008T015350Z/` (directories are stamped in UTC).
MacBook Air M2, 8 GB; Docker Desktop VM with 8 CPUs and 3.8 GB; LogScope in
one container, the shipper and the query client in the same Compose
network, on the same laptop. The data: 157,452 real log lines (50 MB) from
Gatekeeper (eb3d046) and SurgiServe (1645d27, on its fake model), described
in the README. Everything ran twice, the second pass in reverse order.

The laptop was short of memory throughout: 7.0 GB of swap in use at the
start and 7.4 GB at the end. The swap check passed before the start (0 MB
swapped out in 10 s), and each run is judged on its own: an ingest, or one
query's repetitions (about 18 s with the scan), is invalid if the host
swapped in more than 50 MB during it. **29 of 34 runs passed.** The 5
that failed are reported nowhere, and a query is reported only if both its
passes passed. **This is a laptop, not a server benchmark.**

## What it shows

**1. One shipper sends about 12,700 to 13,600 events per second.** Every
event accepted, none rejected, no retries, each batch fsynced to the
write-ahead log before the shipper is answered:

| Batch setting | Batches | Pass 1 | Pass 2 |
|---|---|---|---|
| 100 lines | 1,575 | not reported (61.4 MB swapped in) | 10,938 events/s |
| 1,000 lines | 158 | 12,716 events/s | 12,842 events/s |
| 5,000 lines | 49 | 13,025 events/s | 13,571 events/s |

The "5,000" batches averaged about 3,200 lines: the shipper also caps a
batch at 1 MiB, and these lines average 320 bytes. Past 1,000 lines a
batch, batch size barely matters; at 100 it costs about 2 s more over the
whole corpus. Rough arithmetic from those runs (not a profile): about
1.5 ms per extra batch and about 77 µs per event, so the rate is set by
per-event work. Whether that work is mostly the shipper's or the server's
was not measured.

**2. With the index, searches took 0.26 to 6.1 ms; the full scan of the
same data took 0.6 to 3.7 s.** The server's own time, median of 5
repetitions, for the 7 searches whose runs all passed, each returning the
newest 100 matches (or all, if fewer). The index read at most 101 events
from disk; the scan read all 157,452. Both returned the same number of
results in every run. The scan is LogScope's own Milestone 1 search, kept
as the baseline: this compares LogScope with and without its index, not
with any other product.

**3. The index's cost follows how common the value is, not how many
results come back.** Every one of these returns at most 100 events, yet:

| Search | Events that match | Index, ms (pass 1, 2) |
|---|---|---|
| `request_id:...` | 1 | 0.26, 0.31 |
| `status:504` | 1,557 | 1.29, 1.52 |
| `rate_limited:true` | 13,083 | 1.46, 1.52 |
| `level:info` | 155,620 (99%) | 3.01, 4.53 |
| `duration_ms>400` | 1,557 | 6.07, 6.01 |

The reason is in `index.py`: each term's postings become a Python set of
row numbers before the newest 100 are picked, so a value in 99% of the
events costs a 155,620-entry set. A range is the slowest because it walks
the term directory for every distinct value of the field (8,937 durations)
to find the 1,145 above 400. Both are explanations from the code that fit
the ordering; neither was profiled separately, and neither was tuned.

**4. The UI's aggregations never read an event.** The timeline, the field
list and a field's top values took 0.05 to 0.13 ms on the server, from the
index alone (two binary searches per bucket for the timeline); about 1 ms
as the client saw it, so the HTTP round trip is most of it.

## What it does not show

- **Four of the 14 queries are not reported:** field and range (`route` +
  `status>=500`), `NOT level:info`, and both SurgiServe queries. Each had a
  pass-2 run that swapped in 51 to 645 MB. Their timings are in
  `results.json`, flagged, and quoted nowhere.
- **Batches of 100 have one valid run, not two.**
- **The first page only.** Every search asked for the newest 100. A search
  that must touch every match (an aggregation over a query) was not timed:
  the UI aggregations here had no query.
- **One size of store:** 157,452 events in 3 hourly segments, 56 MB of event
  files. How the index scales with more events or more segments was not
  measured, and neither was the index's size on disk.
- **One shipper, sending one batch at a time.** Not concurrent ingest, and
  not ingest while searches run.
- **fsync on Docker Desktop's virtual disk.** It says nothing about speed on
  other disks, or about surviving power loss, which is not tested at all.
- **SurgiServe's logs came from its fake model** (real service logs, no real
  inference).

## The run before this one

`results/20261008T014120Z` (commit 8f14605), about 12 minutes earlier. Its
six ingest runs all passed (10,584 and 11,197 events/s at 100 lines; 12,922
and 12,731 at 1,000; 13,376 and 13,339 at "5,000"), close to this run's
figures; they are not merged into the numbers above. Its query passes both
failed the swap check, which then covered a whole 3-minute pass and could
not pass on a laptop that swaps in about 50 MB a minute at idle. The check
was changed to one per query after seeing that, and the whole benchmark
was run again; that run's `NOTE.md` has the details, and none of its query
timings are used.

## Full table

Medians of 5 repetitions per pass, in ms; "server" is LogScope's own
`took_ms`, "client" the query client's wall time in the same network.

| Query | Found | Index, server (pass 1, 2) | Index, client (pass 1, 2) | Scan, server (pass 1, 2) | Swap-in MB (pass 1, 2) |
|---|---|---|---|---|---|
| newest 100, no query | 100 | 0.59, 0.56 | 2.50, 2.29 | 622, 615 | 15.4, 2.6 |
| `status:504` | 100 | 1.29, 1.52 | 3.64, 4.12 | 3,428, 3,514 | 2.5, 23.1 |
| `level:info` | 100 | 3.01, 4.53 | 5.13, 15.64 | 3,435, 3,704 | 4.7, 25.2 |
| `request_id:...` | 1 | 0.26, 0.31 | 2.19, 1.86 | 3,362, 3,513 | 6.7, 12.9 |
| `rate_limited:true` | 100 | 1.46, 1.52 | 3.75, 4.62 | 3,408, 3,438 | 0.6, 15.8 |
| `route:"GET /slow" status>=500` | 100 | not reported | not reported | not reported | 0.8, **68.0** |
| `duration_ms>400` | 100 | 6.07, 6.01 | 13.71, 9.91 | 3,618, 3,704 | 8.3, 17.0 |
| `NOT level:info` | 100 | not reported | not reported | not reported | 6.7, **51.2** |
| `event:upload.rejected` | 82 | not reported | not reported | not reported | 1.5, **344.5** |
| `service:worker event:job.succeeded` | 100 | not reported | not reported | not reported | 4.3, **644.6** |
| `"connection refused"` | 100 | 2.81, 2.85 | 5.89, 5.47 | 2,772, 2,766 | 6.2, 13.7 |
| UI timeline (histogram) | 157,452 | 0.06, 0.05 | 1.08, 0.94 | - | 0.4, 11.4 |
| UI field list | 157,452 | 0.13, 0.12 | 0.99, 0.92 | - | 3.0, 17.1 |
| UI top values: `route` | 157,452 | 0.10, 0.10 | 0.87, 0.95 | - | 6.1, 2.5 |

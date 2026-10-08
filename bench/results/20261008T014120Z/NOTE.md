# Superseded: the query timings here are invalid

Run at commit 8f14605. The host passed the swap check before the start
(0 MB swapped out in 10 s), and all six ingest runs passed theirs (at most
27.4 MB swapped in). Both query passes failed: 233.6 MB and 636.0 MB
swapped in, against a 50 MB limit. **Their timings are not used anywhere.**

Why: the swap check covered a whole query pass, about 3 minutes of
requests, while the limit came from the earlier benchmarks' 15-second
runs. This laptop swapped in about 50 MB a minute while idle right after
this run (6 to 22 MB per 10 s; 7.1 GB of swap in use), so a 3-minute pass
could not pass. Swap in use also grew from 6.3 to 7.6 GB during the run, as
Docker's VM grew, so the benchmark adds to the pressure itself; pass 2's
636 MB is more than idle swapping explains.

What changed after this run: the query phase starts one client per query,
so each query's repetitions (about 18 s) are a run of their own for the
swap check, close to Gatekeeper's 15-s runs. That change was made after
seeing this failure; the files here are left as they were written, and the
whole benchmark was run again at the new commit.

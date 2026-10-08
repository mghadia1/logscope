# Explain it yourself

Questions to answer out loud, without notes, before LogScope goes near a
resume or an interview. There are no answers here: the explanation has to
be yours. LogScope was built with Claude, so the bar is explaining why it
is built this way and what went wrong, not only what the code does.

## Run it first

1. `make up`, `make demo`, open <http://localhost:8200>, and search
   `status>=500`. Then run the same search with `curl` and read the `stats`
   in the answer: which segments were read or skipped, how many events
   were read, how many checked? Why are those numbers what they are?
2. `make smoke`. Pick three of its checks and say what each one proves.
   Which one kills the container, and what has to come back afterwards?
3. Ship a file with `python -m logscope.ship --file ... --checkpoint ...`
   (in this folder, with `PYTHONPATH=src`), `kill -9` it part-way, and
   start it again. Did every line arrive? Can
   any arrive twice, and why at most one batch?

## Design choices (pick one to defend)

4. A 200 from `/api/ingest` promises what, exactly? Why is the batch
   fsynced before answering, and what would change if it were fsynced
   after? (`wal.py`, `store.py`)
5. Recovery cuts off a damaged last record, but refuses to start when a
   damaged record has a good one after it. Why treat the two differently?
6. After a failed fsync, LogScope refuses every write until it is
   restarted, instead of retrying. What is "fsyncgate", and why would a
   retry that succeeds be dangerous?
7. What is the manifest's job? Walk through a crash at each step of a
   flush and say what recovery finds each time.
8. Why are event ids global and contiguous? What can recovery prove
   because they are?
9. The index and the full scan both call `terms_of`. What does that give
   the property test, and which bug could that test never catch?
10. A phrase is "inexact". What does that mean, and why does
    `NOT "connection refused"` make every row in the time range a
    candidate?
11. Why can a merged-away file not be deleted at once? Describe the search
    that would silently go wrong, and how manifest versions prevent it.
12. Segment file names already contain the first and last id. Why do they
    also need a number that is never reused?
13. A live-tail client that cannot keep up is sent `dropped`. What would
    happen to the server without that bounded queue?
14. Why does `status:503` find "503.0", and why does `code:007` not find 7?
15. Why does the shipper checkpoint after a batch is delivered, not before?
    What does the other order lose, and when?
16. What does the SIGKILL test prove about durability, and what does it not?
    Why is a power cut different from a killed process?

## Failure modes (pick one to tell as a story)

17. The test comparing the index with the scan passed with three bugs
    planted in it. Why could its data not reach them, and what changed?
18. `docker compose stop` lost the shutdown flush while a live tail was
    open. There were two causes: name both, and the fix for each.
19. The SIGKILL test of the shipper passed with a planted
    checkpoint-before-send bug. Why do random kills miss it, and what test
    catches it?
20. Why did the editable install silently do nothing in this folder?
21. The benchmark's swap check "could never pass". Why not? What was
    changed, and why was the whole benchmark run again rather than the
    first run's numbers relabelled?

## Numbers you should be able to source

22. "12,716 to 13,571 events per second": what exactly was measured (how
    many shippers, how many batches, what was fsynced), and what does it
    not say about how much LogScope can take?
23. "0.26 to 6.1 ms with the index, against 0.6 to 3.7 s for the full
    scan": what is the scan, why is this not a comparison with
    Elasticsearch, and why are only 7 searches in that range?
24. `level:info` took more than ten times as long as a search for one
    request id, and both return at most 100 events. Point at the code that
    explains it, and say how you would fix it.
25. The "5,000-line" batches were about 3,200 lines. Where did that come
    from, and how was it noticed?
26. Which numbers from the first full run (`20261008T014120Z`) may you
    quote, and why?

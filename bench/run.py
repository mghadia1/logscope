"""LogScope's benchmark: ingest rate, and query latency with and without the
index, on real logs.

    python3 bench/run.py [--reps 5] [--batch-sizes 100,1000,5000] [--allow-swapping]

Needs the corpus (made by bench/capture.py: real Gatekeeper and SurgiServe
logs, in $LOGSCOPE_BENCH_CORPUS, by default ~/.cache/logscope-bench/corpus).
Everything runs as its own Compose project (logscope-bench), with the
client in the same network, so Docker Desktop's port forwarding is never
measured.

Ingest: the whole corpus, shipped by the real shipper (python -m
logscope.ship --once) into an empty store, at each batch size; events per
second by the shipper's own clock. Every run starts from a fresh volume.

Queries: the corpus ingested once (batch 1000); the store restarted, so
everything is flushed into segments, and given time for a maintenance pass
(merges); then every query, N times with the index and N times as the full
scan, alternately. Latency is the server's own measure (took_ms), the
client's wall time beside it. The aggregations behind the UI's timeline,
field list and top values have no scan mode and are timed alone.

Method, from the SurgiServe and Gatekeeper benchmarks: two passes, the
second in reverse order, so a machine that drifts as it warms affects every
configuration alike; refuses to start while the host is swapping; records
the host's swap-ins during every run and marks one with more than 50 MB of
them invalid.

Writes bench/results/<timestamp>/results.json and results.md.
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(
    os.environ.get(
        "LOGSCOPE_BENCH_CORPUS", Path.home() / ".cache/logscope-bench/corpus"
    )
)
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "logscope-bench",
    "-f",
    "compose.yaml",
    "-f",
    "bench/compose.bench.yaml",
]
SWAP_TOLERANCE_MB = 50.0
SETTLE_SECONDS = 40  # after the restart: one maintenance pass (every 30 s)


def sh(*command: str) -> str:
    env = {**os.environ, "LOGSCOPE_PORT": "8203", "LOGSCOPE_BENCH_CORPUS": str(CORPUS)}
    return subprocess.run(
        command, cwd=ROOT, env=env, check=True, capture_output=True, text=True
    ).stdout


def host_swap_mb() -> tuple[float, float]:
    out = sh("vm_stat")
    page = int(out.split("page size of ")[1].split(" bytes")[0])
    counts = {}
    for line in out.splitlines():
        name, _, value = line.partition(":")
        if name in ("Swapouts", "Swapins"):
            counts[name] = int(value.strip().rstrip(".")) * page / 2**20
    return counts["Swapouts"], counts["Swapins"]


def fresh_stack() -> None:
    sh(*COMPOSE, "down", "--volumes", "--timeout", "15")
    sh(*COMPOSE, "up", "--detach", "--wait", "logscope")


def in_network(*command: str) -> str:
    return sh(
        *COMPOSE, "--profile", "bench", "run", "--rm", "--no-deps", "client", *command
    )


def ship(batch: int) -> dict[str, object]:
    out = in_network(
        "python",
        "-m",
        "logscope.ship",
        "--url",
        "http://logscope:8000",
        "--file",
        "/corpus/all.jsonl",
        "--once",
        "--batch-events",
        str(batch),
    )
    return json.loads(out.strip().splitlines()[-1])


def measured(run: dict[str, object], swapped_in: float) -> dict[str, object]:
    return {
        **run,
        "host_swapped_in_mb": round(swapped_in, 1),
        "valid": swapped_in <= SWAP_TOLERANCE_MB,
    }


def ingest_runs(batch_sizes: list[int]) -> list[dict[str, object]]:
    runs = []
    for run_pass, order in ((1, batch_sizes), (2, list(reversed(batch_sizes)))):
        for batch in order:
            fresh_stack()
            _, before = host_swap_mb()
            result = ship(batch)
            _, after = host_swap_mb()
            rate = result["accepted"] / result["seconds"]
            run = measured(
                {
                    "pass": run_pass,
                    "batch": batch,
                    **result,
                    "events_per_second": round(rate),
                },
                after - before,
            )
            print(
                f"  ingest pass {run_pass} batch {batch:5d}:"
                f" {result['accepted']} events in {result['seconds']:.1f} s"
                f" = {rate:,.0f}/s  swap-in {run['host_swapped_in_mb']} MB",
                flush=True,
            )
            runs.append(run)
    return runs


def corpus_queries() -> list[dict[str, object]]:
    """The queries, from what the corpus actually holds: a real request id,
    the real error text of the offline backend."""
    request_ids = []
    error_text = None
    with open(CORPUS / "gatekeeper.jsonl", "rb") as file:
        for line in file:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if "request_id" in event:
                request_ids.append(event["request_id"])
            if error_text is None and "connection refused" in str(
                event.get("error", "")
            ):
                error_text = "connection refused"
    needle = request_ids[len(request_ids) // 2]
    # From the corpus: Gatekeeper logs every request at INFO (only
    # "instance ejected" is WARN), so failures are found by status; SurgiServe's
    # "service" is api or worker, and this run logged no errors.
    search = [
        ("newest 100, no query", ""),
        ("a rare value: status:504", "status:504"),
        ("a common value: level:info", "level:info"),
        ("one request: request_id", f"request_id:{needle}"),
        ("a boolean: rate_limited:true", "rate_limited:true"),
        ("field and range: route + status>=500", 'route:"GET /slow" status>=500'),
        ("a range over durations: duration_ms>400", "duration_ms>400"),
        ("NOT: NOT level:info", "NOT level:info"),
        ("SurgiServe: event:upload.rejected", "event:upload.rejected"),
        (
            "SurgiServe: service:worker event:job.succeeded",
            "service:worker event:job.succeeded",
        ),
    ]
    if error_text:
        search.append((f'a phrase: "{error_text}"', f'"{error_text}"'))
    queries: list[dict[str, object]] = [
        {
            "name": name,
            "path": "/api/search",
            "params": {"q": q, "limit": 100},
            "compare_scan": True,
        }
        for name, q in search
    ]
    queries += [
        {
            "name": "UI timeline: histogram, no query",
            "path": "/api/aggregate/histogram",
            "params": {},
        },
        {
            "name": "UI fields: field list",
            "path": "/api/aggregate/fields",
            "params": {"size": 40},
        },
        {
            "name": "UI top values: route",
            "path": "/api/aggregate/terms",
            "params": {"field": "route"},
        },
    ]
    return queries


def query_runs(reps: int) -> tuple[list[dict[str, object]], dict[str, object]]:
    fresh_stack()
    loaded = ship(1000)
    sh(*COMPOSE, "restart", "logscope")  # flush everything into segments
    sh(*COMPOSE, "up", "--detach", "--wait", "logscope")
    time.sleep(SETTLE_SECONDS)
    stats_url = "http://logscope:8000/api/stats"
    store = json.loads(
        in_network(
            "python",
            "-c",
            "import json, sys, urllib.request;"
            f"json.dump(json.load(urllib.request.urlopen({stats_url!r})), sys.stdout)",
        )
    )
    print(
        f"  store: {store['segment_events']} events in {store['segments']} segments,"
        f" {store['events_in_memory']} in memory",
        flush=True,
    )
    queries = corpus_queries()
    runs = []
    for run_pass, order in ((1, queries), (2, list(reversed(queries)))):
        plan = {"base": "http://logscope:8000", "reps": reps, "queries": order}
        _, before = host_swap_mb()
        results = json.loads(
            in_network("python", "/bench/client.py", json.dumps(plan))
            .strip()
            .splitlines()[-1]
        )
        _, after = host_swap_mb()
        swapped = after - before
        for result in results:
            runs.append(measured({"pass": run_pass, **result}, swapped))
        print(f"  queries pass {run_pass}: done, swap-in {swapped:.1f} MB", flush=True)
    return runs, {"loaded": loaded, "store": store}


def median(values: list[float]) -> float:
    return round(statistics.median(values), 3)


def summarize(runs: list[dict[str, object]]) -> list[dict[str, object]]:
    rows = []
    for name in dict.fromkeys(run["name"] for run in runs):
        these = [run for run in runs if run["name"] == name]
        row: dict[str, object] = {
            "name": name,
            "all_valid": all(r["valid"] for r in these),
        }
        for mode in ("index", "scan"):
            if mode not in these[0]["runs"]:
                continue
            took = {
                r["pass"]: median([x["took_ms"] for x in r["runs"][mode]])
                for r in these
            }
            wall = {
                r["pass"]: median([x["wall_ms"] for x in r["runs"][mode]])
                for r in these
            }
            first = these[0]["runs"][mode][0]
            row[mode] = {
                "took_ms_by_pass": took,
                "wall_ms_by_pass": wall,
                "events_read": first["events_read"],
                "checked": first["checked"],
                "found": first["found"],
            }
        rows.append(row)
    return rows


def table(rows: list[dict[str, object]]) -> str:
    lines = [
        "| Query | Found | Index ms (pass 1, 2) | Scan ms (pass 1, 2)"
        " | Events read, index / scan | Valid |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        index = row["index"]
        scan = row.get("scan")
        took = ", ".join(f"{v:.2f}" for v in index["took_ms_by_pass"].values())
        scan_took = (
            ", ".join(f"{v:.0f}" for v in scan["took_ms_by_pass"].values())
            if scan
            else "-"
        )
        read = (
            f"{index['events_read']:,} / {scan['events_read']:,}"
            if scan
            else f"{index['events_read']:,}"
        )
        lines.append(
            f"| {row['name']} | {index['found']} | {took} | {scan_took} | {read}"
            f" | {'yes' if row['all_valid'] else 'NO'} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--batch-sizes", default="100,1000,5000")
    parser.add_argument(
        "--allow-swapping",
        action="store_true",
        help="run even if the host is swapping (runs are still flagged)",
    )
    args = parser.parse_args()
    if not (CORPUS / "manifest.json").exists():
        sys.exit("no corpus: run python3 bench/capture.py first")
    corpus = json.loads((CORPUS / "manifest.json").read_text())

    before, _ = host_swap_mb()
    time.sleep(10)
    swapping = host_swap_mb()[0] - before
    print(f"host swapped out {swapping:.0f} MB in the last 10 s", flush=True)
    if swapping > SWAP_TOLERANCE_MB and not args.allow_swapping:
        sys.exit(
            "the host is swapping; free memory and try again"
            " (or --allow-swapping to check the runner)"
        )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "bench" / "results" / stamp
    out_dir.mkdir(parents=True)
    print("building", flush=True)
    sh(*COMPOSE, "--profile", "bench", "build")
    try:
        print("ingest", flush=True)
        ingest = ingest_runs([int(b) for b in args.batch_sizes.split(",")])
        print("queries", flush=True)
        queries, setup = query_runs(args.reps)
    finally:
        sh(*COMPOSE, "down", "--volumes", "--timeout", "15")

    info = json.loads(sh("docker", "info", "--format", "{{json .}}"))
    rows = summarize(queries)
    record = {
        "environment": {
            "date": datetime.now(UTC).isoformat(timespec="seconds"),
            "git_commit": sh("git", "rev-parse", "--short", "HEAD").strip(),
            "machine": sh("sysctl", "-n", "hw.model").strip(),
            "chip": sh("sysctl", "-n", "machdep.cpu.brand_string").strip(),
            "memory_gb": int(sh("sysctl", "-n", "hw.memsize")) / 2**30,
            "macos": platform.mac_ver()[0],
            "docker_vm": {
                "cpus": info["NCPU"],
                "memory_gb": round(info["MemTotal"] / 2**30, 1),
            },
        },
        "corpus": corpus,
        "settings": {
            "reps": args.reps,
            "batch_sizes": args.batch_sizes,
            "order": "forward pass, then reverse",
        },
        "ingest": ingest,
        "query_setup": setup,
        "query_summary": rows,
        "queries": queries,
    }
    (out_dir / "results.json").write_text(json.dumps(record, indent=2) + "\n")
    ingest_lines = "\n".join(
        f"- pass {r['pass']}, batch {r['batch']}:"
        f" {r['accepted']:,} events in {r['seconds']} s"
        f" = {r['events_per_second']:,}/s; valid: {'yes' if r['valid'] else 'NO'}"
        for r in ingest
    )
    (out_dir / "results.md").write_text(
        "## Ingest\n\n"
        + ingest_lines
        + "\n\n## Queries (median of the repetitions)\n\n"
        + table(rows)
    )
    print(ingest_lines)
    print(table(rows))
    print(f"written to {out_dir}")


if __name__ == "__main__":
    main()

"""Run inside the benchmark's Compose network (see run.py): time a plan of
queries against LogScope and print the results as one JSON line.

    python /bench/client.py '<plan as JSON>'

The plan: {"base": URL, "reps": N, "queries": [{"name", "path", "params",
"compare_scan"}]}. Each repetition of a search runs it with the index and,
if compare_scan, without, alternately, so both see the same conditions.
Standard library only (it runs in the LogScope image).
"""

import json
import sys
import time
import urllib.parse
import urllib.request


def timed(url: str) -> dict[str, object]:
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=600) as response:
        body = json.load(response)
    wall = (time.perf_counter() - started) * 1000
    stats = body.get("stats", {})
    return {
        "wall_ms": round(wall, 3),
        "took_ms": stats.get("took_ms"),
        "events_read": stats.get("events_read"),
        "checked": stats.get("checked"),
        "segments_read": stats.get("segments_read"),
        "segments_total": stats.get("segments_total"),
        "found": len(body["events"])
        if "events" in body
        else body.get("total", body.get("matched")),
    }


def main() -> None:
    plan = json.loads(sys.argv[1])
    results = []
    for query in plan["queries"]:
        modes = ["index", "scan"] if query.get("compare_scan") else ["index"]
        runs: dict[str, list[dict[str, object]]] = {mode: [] for mode in modes}
        for _ in range(plan["reps"]):
            for mode in modes:
                params = dict(query["params"])
                if mode == "scan":
                    params["index"] = "false"
                url = f"{plan['base']}{query['path']}?{urllib.parse.urlencode(params)}"
                runs[mode].append(timed(url))
        results.append({"name": query["name"], "runs": runs})
    print(json.dumps(results))


if __name__ == "__main__":
    main()

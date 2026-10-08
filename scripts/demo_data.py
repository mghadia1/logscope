"""Load synthetic demo events into a running LogScope.

    python3 scripts/demo_data.py [--url http://localhost:8200] [--events 6000]

The events are made up: shaped like Gatekeeper's access logs (Go's slog)
and SurgiServe's job logs, spread over the last six hours, with a burst of
upstream timeouts an hour ago so the timeline has something to show. They
exist to try the UI; real logs come from `make capture` and the shipper
(README, "Shipping logs"). Standard library only.
"""

import argparse
import json
import random
import urllib.request
from datetime import UTC, datetime, timedelta

ROUTES = ["GET /users/{id}", "GET /orders/{id}", "POST /orders", "GET /healthz"]


def gatekeeper(rng: random.Random, at: datetime, incident: bool) -> dict[str, object]:
    route = rng.choice(ROUTES)
    failed = incident and route != "GET /healthz" and rng.random() < 0.6
    status = 504 if failed else rng.choices([200, 404, 429, 502], [90, 5, 3, 2])[0]
    event: dict[str, object] = {
        "time": at.isoformat().replace("+00:00", "Z"),
        "level": "ERROR" if status >= 500 else "WARN" if status >= 400 else "INFO",
        "msg": "upstream timeout" if status == 504 else "request",
        "service": "gatekeeper",
        "route": route,
        "status": status,
        "duration_ms": round(rng.lognormvariate(3, 0.6) * (40 if failed else 1), 1),
        "request_id": f"{rng.getrandbits(48):012x}",
        "backend": rng.choice(["users-1", "users-2", "orders"]),
    }
    if route == "GET /healthz":
        event["level"] = "INFO"
        event["msg"] = "request"
        event["status"] = 200
    return event


def surgiserve(rng: random.Random, at: datetime, incident: bool) -> dict[str, object]:
    failed = rng.random() < (0.3 if incident else 0.03)
    return {
        "ts": at.isoformat(timespec="milliseconds"),
        "level": "error" if failed else "info",
        "service": "surgiserve",
        "logger": "surgiserve.worker",
        "event": "job failed" if failed else rng.choice(["job done", "job started"]),
        "job_id": f"{rng.getrandbits(32):08x}-{rng.getrandbits(16):04x}",
        "attempt": rng.choice([1, 1, 1, 2, 3]) if failed else 1,
        "inference_ms": round(rng.gauss(85, 12), 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8200")
    parser.add_argument("--events", type=int, default=6000)
    args = parser.parse_args()
    rng = random.Random(7)
    now = datetime.now(UTC)
    incident = (now - timedelta(hours=1, minutes=10), now - timedelta(minutes=55))
    lines = []
    for _ in range(args.events):
        if rng.random() < 0.08:  # extra events inside the incident window
            at = incident[0] + (incident[1] - incident[0]) * rng.random()
        else:
            at = now - timedelta(seconds=rng.uniform(0, 6 * 3600))
        during = incident[0] <= at <= incident[1]
        make = gatekeeper if rng.random() < 0.7 else surgiserve
        lines.append(json.dumps(make(rng, at, during)))
    accepted = 0
    for start in range(0, len(lines), 2000):
        body = ("\n".join(lines[start : start + 2000]) + "\n").encode()
        request = urllib.request.Request(
            f"{args.url}/api/ingest", data=body, method="POST"
        )
        with urllib.request.urlopen(request) as response:
            accepted += json.load(response)["accepted"]
    print(f"{accepted} synthetic demo events stored")


if __name__ == "__main__":
    main()

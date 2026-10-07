"""Capture real logs from the two earlier projects, for the benchmark.

    python3 bench/capture.py [--gatekeeper-requests 150000] [--surgiserve-jobs 1500]

Both run as they do in their own repositories (Docker Compose, from
../gatekeeper and ../surgiserve), under traffic sent from here, and their
containers' JSON logs are saved:

- Gatekeeper: both gateway replicas, the echo backends, Redis rate
  limiting. The traffic mixes what the gateway does: proxied requests (most,
  each with its own API key, so the rate limit lets them through), a
  hammered key (429s), unknown paths (404), wrong methods (405), an offline
  backend (502) and a backend slower than its timeout (504).
- SurgiServe: the API and a worker, uploading a test image and a few bad
  uploads, and looking up jobs that do not exist. The worker runs with
  ``SURGISERVE_MODEL=fake`` (no weights, no PyTorch): these are SurgiServe's
  real logs, but the segmentation itself is the fake model's.

Writes {gatekeeper,surgiserve,all}.jsonl and manifest.json (counts,
SHA-256, the commits that produced them) to $LOGSCOPE_BENCH_CORPUS, by
default ~/.cache/logscope-bench/corpus: outside the repository, because the
repository sits in an iCloud-synced folder and 100 MB of logs is nothing to
upload. The manifest is copied into each benchmark result. Standard library
only.
"""

import argparse
import hashlib
import json
import os
import random
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECTS = HERE.parents[1]
CORPUS = Path(
    os.environ.get(
        "LOGSCOPE_BENCH_CORPUS", Path.home() / ".cache/logscope-bench/corpus"
    )
)


def sh(*command: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        command,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def request(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> int:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return 0


def gatekeeper_traffic(count: int) -> Counter[int]:
    rng = random.Random(11)
    statuses: Counter[int] = Counter()
    lock = threading.Lock()

    def one(i: int) -> None:
        base = "http://localhost:8080" if i % 2 else "http://localhost:8081"
        key = {"X-API-Key": f"capture-{uuid.uuid4().hex[:12]}"}
        roll = rng.random()
        if roll < 0.70:
            status = request("GET", f"{base}/users/{rng.randint(1, 5000)}", headers=key)
        elif roll < 0.74:
            status = request("POST", f"{base}/users", b'{"name": "x"}', key)
        elif roll < 0.84:
            status = request(
                "GET", f"{base}/api/orders/{rng.randint(1, 900)}", headers=key
            )
        elif roll < 0.85:
            status = request("GET", f"{base}/slow?sleep=1s", headers=key)  # 504
        elif roll < 0.87:
            status = request("GET", f"{base}/offline/x", headers=key)  # 502
        elif roll < 0.90:
            status = request("GET", f"{base}/nope/{rng.randint(1, 50)}", headers=key)
        elif roll < 0.91:
            status = request("DELETE", f"{base}/users/1", headers=key)  # 405
        else:
            status = request(
                "GET", f"{base}/users/1", headers={"X-API-Key": "capture-hammer"}
            )
        with lock:
            statuses[status] += 1

    with ThreadPoolExecutor(max_workers=24) as pool:
        list(pool.map(one, range(count)))
    return statuses


def multipart(image: bytes, name: str, content_type: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        + image
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def surgiserve_traffic(count: int, image: bytes) -> Counter[int]:
    rng = random.Random(12)
    statuses: Counter[int] = Counter()
    lock = threading.Lock()
    api = "http://localhost:8000"

    def one(i: int) -> None:
        roll = rng.random()
        if roll < 0.90:
            body, kind = multipart(image, "frame.png", "image/png")
        elif roll < 0.95:
            body, kind = multipart(b"not an image", "frame.png", "image/png")  # 422
        else:
            status = request("GET", f"{api}/jobs/{uuid.uuid4()}")  # 404
            with lock:
                statuses[status] += 1
            return
        status = request("POST", f"{api}/jobs", body, {"Content-Type": kind})
        with lock:
            statuses[status] += 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, range(count)))
    return statuses


def save_logs(project: Path, services: list[str], out: Path) -> int:
    text = sh(
        "docker",
        "compose",
        "logs",
        "--no-log-prefix",
        "--no-color",
        *services,
        cwd=project,
    )
    lines = [line for line in text.splitlines() if line.strip()]
    out.write_text("\n".join(lines) + "\n")
    return len(lines)


def describe(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    lines = data.splitlines()
    json_objects = 0
    for line in lines:
        try:
            json_objects += isinstance(json.loads(line), dict)
        except ValueError:
            pass
    return {
        "lines": len(lines),
        "json_objects": json_objects,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gatekeeper-requests", type=int, default=150_000)
    parser.add_argument("--surgiserve-jobs", type=int, default=1_500)
    args = parser.parse_args()
    CORPUS.mkdir(parents=True, exist_ok=True)
    gatekeeper = PROJECTS / "gatekeeper"
    surgiserve = PROJECTS / "surgiserve"
    manifest: dict[str, object] = {
        "captured": datetime.now(UTC).isoformat(timespec="seconds")
    }

    print("gatekeeper: starting", flush=True)
    sh("docker", "compose", "down", "--volumes", cwd=gatekeeper)
    sh("docker", "compose", "up", "--build", "--detach", "--wait", cwd=gatekeeper)
    try:
        started = time.monotonic()
        statuses = gatekeeper_traffic(args.gatekeeper_requests)
        print(
            f"gatekeeper: {sum(statuses.values())} requests in"
            f" {time.monotonic() - started:.0f} s: {dict(sorted(statuses.items()))}",
            flush=True,
        )
        time.sleep(2)  # the last access-log lines
        save_logs(
            gatekeeper, ["gatekeeper", "gatekeeper-2"], CORPUS / "gatekeeper.jsonl"
        )
        manifest["gatekeeper"] = {
            "commit": sh("git", "rev-parse", "--short", "HEAD", cwd=gatekeeper).strip(),
            "requests_sent": sum(statuses.values()),
            "statuses": {str(k): v for k, v in sorted(statuses.items())},
            **describe(CORPUS / "gatekeeper.jsonl"),
        }
    finally:
        sh("docker", "compose", "down", "--volumes", cwd=gatekeeper)

    print("surgiserve: starting (fake model)", flush=True)
    fake = {"SURGISERVE_MODEL": "fake"}
    sh("docker", "compose", "down", "--volumes", cwd=surgiserve, env=fake)
    sh(
        "docker",
        "compose",
        "up",
        "--build",
        "--detach",
        "--wait",
        cwd=surgiserve,
        env=fake,
    )
    try:
        image = (
            surgiserve / "tests" / "fixtures" / "checkerboard-256.png"
        ).read_bytes()
        started = time.monotonic()
        statuses = surgiserve_traffic(args.surgiserve_jobs, image)
        time.sleep(10)  # let the worker finish the queue
        print(
            f"surgiserve: {sum(statuses.values())} requests in"
            f" {time.monotonic() - started:.0f} s: {dict(sorted(statuses.items()))}",
            flush=True,
        )
        save_logs(surgiserve, ["api", "worker"], CORPUS / "surgiserve.jsonl")
        manifest["surgiserve"] = {
            "commit": sh("git", "rev-parse", "--short", "HEAD", cwd=surgiserve).strip(),
            "model": "SURGISERVE_MODEL=fake: real service logs, no real inference",
            "requests_sent": sum(statuses.values()),
            "statuses": {str(k): v for k, v in sorted(statuses.items())},
            **describe(CORPUS / "surgiserve.jsonl"),
        }
    finally:
        sh("docker", "compose", "down", "--volumes", cwd=surgiserve, env=fake)

    combined = (CORPUS / "gatekeeper.jsonl").read_bytes() + (
        CORPUS / "surgiserve.jsonl"
    ).read_bytes()
    (CORPUS / "all.jsonl").write_bytes(combined)
    manifest["all"] = describe(CORPUS / "all.jsonl")
    (CORPUS / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()

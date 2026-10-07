"""Kill a real process with SIGKILL while it ingests, flushes and merges,
then recover: every acknowledged batch must be there, once, with no gaps.

The child appends batches as fast as it can, with a flush threshold small
enough that flushes are constantly running, and prints each batch's ids
after ``append`` returns (the moment the API would answer 200). The parent
kills it at a random point, so the kill lands anywhere: mid-append,
mid-fsync, mid-flush, mid-merge, between the segments and the manifest.
"""

import os
import random
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from logscope.store import Store

SRC = Path(__file__).resolve().parents[1] / "src"

CHILD = """
import json, random, sys, threading
from pathlib import Path
from logscope.events import ParsedLine
from logscope.store import Store

store = Store(
    Path(sys.argv[1]), flush_events=150, flush_interval=0.05, merge_threshold=2
)
store.open()
# Merges run constantly too, so a kill can land in the middle of one; each
# is reported, so the test knows merges really ran.
printing = threading.Lock()
def say(text):
    with printing:
        sys.stdout.write(text + "\\n")
        sys.stdout.flush()
merge = store.merge
def reporting_merge():
    merged = merge()
    if merged:
        say(f"merged {len(merged)}")
    return merged
store.merge = reporting_merge
store.start_background(period=0.002, maintenance_interval=0.005)
rng = random.Random(int(sys.argv[2]))
t0 = 1_791_320_400_000_000  # 2026-10-06T21:00:00Z
while True:
    lines = []
    for _ in range(rng.randint(1, 40)):
        data = {"msg": "x" * rng.randint(0, 200), "n": rng.random()}
        t = t0 + rng.randint(0, 3 * 3600 * 1_000_000)  # spread over 3 hours
        lines.append(ParsedLine(t, data, json.dumps(data).encode()))
    first, last = store.append(lines)
    say(f"{first} {last}")
"""


def run_and_kill(data_dir: Path, rng: random.Random) -> tuple[set[int], int]:
    """Run the child, kill it at a random moment; return the acknowledged ids
    and how many merges it finished."""
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(data_dir), str(rng.randrange(1 << 30))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        text=True,
    )
    assert child.stdout is not None
    acked: set[int] = set()
    merges = 0

    def read(line: str) -> None:
        nonlocal merges
        if line.startswith("merged"):
            merges += int(line.split()[1])
        else:
            first, last = map(int, line.split())
            acked.update(range(first, last + 1))

    wanted = rng.randint(100, 600)
    for line in child.stdout:
        read(line)
        wanted -= 1
        if wanted == 0:
            break
    child.send_signal(signal.SIGKILL)
    # Batches acknowledged before the kill but not read yet count too.
    rest, errors = child.communicate()
    for line in rest.splitlines():
        read(line)
    assert child.returncode == -signal.SIGKILL, errors
    return acked, merges


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_sigkill_loses_no_acknowledged_event(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    acked: set[int] = set()
    merges = 0
    for _ in range(4):  # crash, recover and carry on, four times over
        newly_acked, newly_merged = run_and_kill(tmp_path, rng)
        acked |= newly_acked
        merges += newly_merged
        store = Store(tmp_path)
        store.open()
        try:
            events, _ = store.search(limit=10_000_000)
            ids = [e.id for e in events]
            stats = store.stats()
        finally:
            store.abandon()
        assert len(ids) == len(set(ids)), "an event was stored twice"
        missing = acked - set(ids)
        assert not missing, f"{len(missing)} acknowledged events lost"
        # Unacknowledged batches may survive (written, fsynced, killed before
        # the ack was printed), but never with a gap before them.
        assert sorted(ids) == list(range(1, len(ids) + 1))
    # Across the four runs, flushes and merges must have happened, or they
    # went untested.
    assert stats["segments"] > 0, "no flush ran, so flushes were not tested"
    assert merges > 0, "no merge finished, so merges were not tested"

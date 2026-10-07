"""Kill a real process with SIGKILL while it ingests and flushes, then
recover: every acknowledged batch must be there, once, with no gaps.

The child appends batches as fast as it can, with a flush threshold small
enough that flushes are constantly running, and prints each batch's ids
after ``append`` returns (the moment the API would answer 200). The parent
kills it at a random point, so the kill lands anywhere: mid-append,
mid-fsync, mid-flush, between the segments and the manifest.
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
import json, random, sys
from pathlib import Path
from logscope.events import ParsedLine
from logscope.store import Store

store = Store(Path(sys.argv[1]), flush_events=150, flush_interval=0.05)
store.open()
store.start_flusher(period=0.002)
rng = random.Random(int(sys.argv[2]))
t0 = 1_791_320_400_000_000  # 2026-10-06T21:00:00Z
while True:
    lines = []
    for _ in range(rng.randint(1, 40)):
        data = {"msg": "x" * rng.randint(0, 200), "n": rng.random()}
        t = t0 + rng.randint(0, 5 * 3600 * 1_000_000)  # spread over 5 hours
        lines.append(ParsedLine(t, data, json.dumps(data).encode()))
    first, last = store.append(lines)
    print(first, last, flush=True)
"""


def run_and_kill(data_dir: Path, rng: random.Random) -> set[int]:
    """Run the child, kill it at a random moment; return the acknowledged ids."""
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(data_dir), str(rng.randrange(1 << 30))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        text=True,
    )
    assert child.stdout is not None
    acked: set[int] = set()
    wanted = rng.randint(20, 200)
    for line in child.stdout:
        first, last = map(int, line.split())
        acked.update(range(first, last + 1))
        wanted -= 1
        if wanted == 0:
            break
    child.send_signal(signal.SIGKILL)
    # Batches acknowledged before the kill but not read yet count too.
    rest, errors = child.communicate()
    for line in rest.splitlines():
        first, last = map(int, line.split())
        acked.update(range(first, last + 1))
    assert child.returncode == -signal.SIGKILL, errors
    return acked


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_sigkill_loses_no_acknowledged_event(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    acked: set[int] = set()
    for _ in range(4):  # crash, recover and carry on, four times over
        acked |= run_and_kill(tmp_path, rng)
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
    # Across the four runs, flushes must have happened, or they went untested.
    assert stats["segments"] > 0, "no flush ran, so flushes were not tested"

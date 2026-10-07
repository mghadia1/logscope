import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from servers import Running

from logscope import ship
from logscope.ship import Counts, FatalError, Interrupted, Options, Sender


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ship, "RETRY_BASE_SECONDS", 0.001)


class FakeIngest:
    """A stand-in for /api/ingest answering with a scripted status: a list
    consumed in order (then 200), or a function of the request's lines."""

    def __init__(
        self,
        script: list[int] | Callable[[list[bytes]], int] | None = None,
        port: int = 0,
    ) -> None:
        self.script = script if script is not None else []
        self.received: list[list[bytes]] = []  # the batches answered 200
        self.requests = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                lines = body.rstrip(b"\n").split(b"\n")
                fake.requests += 1
                if callable(fake.script):
                    status = fake.script(lines)
                else:
                    status = fake.script.pop(0) if fake.script else 200
                payload = b"{}"
                if status == 200:
                    fake.received.append(lines)
                    payload = json.dumps(
                        {
                            "accepted": len(lines),
                            "rejected": 0,
                            "errors": [],
                            "first_id": 1,
                            "last_id": 1,
                        }
                    ).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def lines(self) -> list[bytes]:
        return [line for batch in self.received for line in batch]


@pytest.fixture
def fake() -> Iterator[Callable[..., FakeIngest]]:
    started: list[FakeIngest] = []

    def start(*args: object, **kwargs: object) -> FakeIngest:
        server = FakeIngest(*args, **kwargs)  # type: ignore[arg-type]
        started.append(server)
        return server

    yield start
    for server in started:
        server.close()


LINES = [json.dumps({"msg": f"line {i}"}).encode() for i in range(10)]


def test_lines_pass_through_and_others_are_wrapped() -> None:
    assert ship.prepare(b'{"msg": "ok"}\n') == (b'{"msg": "ok"}', False)
    assert ship.prepare(b"\n") == (b"", False)
    wrapped, was = ship.prepare(b"panic: runtime error\r\n")
    assert was and json.loads(wrapped) == {
        "message": "panic: runtime error",
        "unparsed": True,
    }
    assert json.loads(ship.prepare(b"[1, 2]\n")[0])["message"] == "[1, 2]"


def test_server_errors_are_retried_until_delivered(
    fake: Callable[..., FakeIngest],
) -> None:
    server = fake([503, 500, 429])
    counts = Counts()
    Sender(server.url, counts).send(LINES)
    assert server.lines() == LINES  # delivered once, whole
    assert (counts.retries, counts.accepted) == (3, 10)


def test_a_batch_too_large_is_split_until_it_fits(
    fake: Callable[..., FakeIngest],
) -> None:
    server = fake(lambda lines: 413 if len(lines) > 3 else 200)
    counts = Counts()
    Sender(server.url, counts).send(LINES)
    assert server.lines() == LINES  # in order, each once
    assert all(len(batch) <= 3 for batch in server.received)
    # 10 -> 5 + 5; each 5 -> 2 + 3: three splits, four batches.
    assert (counts.splits, counts.batches) == (3, 4)


def test_one_line_over_the_limit_is_skipped_not_fatal(
    fake: Callable[..., FakeIngest],
) -> None:
    server = fake(lambda lines: 413 if any(b"huge" in line for line in lines) else 200)
    counts = Counts()
    lines = [LINES[0], b'{"msg": "huge"}', LINES[1]]
    Sender(server.url, counts).send(lines)
    assert server.lines() == [LINES[0], LINES[1]]
    assert counts.rejected == 1


def test_a_client_error_stops_the_shipper(fake: Callable[..., FakeIngest]) -> None:
    server = fake([404])
    with pytest.raises(FatalError, match="404"):
        Sender(server.url, Counts()).send(LINES)


def test_a_server_that_is_not_up_yet_is_waited_for(
    fake: Callable[..., FakeIngest],
) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    late: list[FakeIngest] = []
    threading.Timer(0.3, lambda: late.append(fake(port=port))).start()
    counts = Counts()
    Sender(f"http://127.0.0.1:{port}", counts).send(LINES)
    assert late[0].lines() == LINES
    assert counts.retries > 0


def test_stopping_during_retries_gives_up_promptly(
    fake: Callable[..., FakeIngest], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ship, "RETRY_BASE_SECONDS", 10.0)  # long waits...
    server = fake(lambda lines: 503)
    stopping = threading.Event()
    threading.Timer(0.2, stopping.set).start()
    started = time.monotonic()
    with pytest.raises(Interrupted):
        Sender(server.url, Counts(), stopping=stopping).send(LINES)
    assert time.monotonic() - started < 2  # ...cut short by the stop


# Following a file


def collect(
    path: Path, offset: int = 0, inode: int | None = None
) -> list[tuple[bytes, int]]:
    return [(line, end) for line, end, _ in ship.follow(path, offset, inode, once=True)]


def test_once_reads_every_line_including_a_last_one_without_newline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.log"
    path.write_bytes(b"one\ntwo\nthree")
    assert collect(path) == [(b"one\n", 4), (b"two\n", 8), (b"three", 13)]


def test_a_checkpoint_resumes_only_on_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_bytes(b"one\ntwo\n")
    inode = path.stat().st_ino
    assert collect(path, 4, inode) == [(b"two\n", 8)]
    assert collect(path, 4, inode + 1) == [(b"one\n", 4), (b"two\n", 8)]  # rotated


def test_following_waits_for_whole_lines_and_survives_rotation(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_bytes(b"first\n")
    stopping = threading.Event()
    seen: list[bytes] = []

    def run() -> None:
        for line, _, _ in ship.follow(
            path, 0, None, once=False, poll=0.02, stopping=stopping
        ):
            seen.append(line)

    reader = threading.Thread(target=run)
    reader.start()

    def wait_for(count: int) -> None:
        deadline = time.monotonic() + 5
        while len(seen) < count:
            assert time.monotonic() < deadline, seen
            time.sleep(0.01)

    wait_for(1)
    with open(path, "ab") as file:
        file.write(b"half ")  # a line being written...
        file.flush()
        time.sleep(0.1)
        file.write(b"done\n")  # ...is read only once complete
    wait_for(2)
    os.rename(path, tmp_path / "app.log.1")  # rotated: a new file appears
    path.write_bytes(b"after rotation\n")
    wait_for(3)
    path.write_bytes(b"cut\n")  # truncated in place: read again from 0
    wait_for(4)
    stopping.set()
    reader.join(5)
    assert seen == [b"first\n", b"half done\n", b"after rotation\n", b"cut\n"]


# The loop


def test_batches_by_count_and_by_time(fake: Callable[..., FakeIngest]) -> None:
    server = fake()
    counts = Counts()
    source = iter([(line, i + 1, 7) for i, line in enumerate(LINES)])
    delivered: list[tuple[int, int]] = []
    ship.ship(
        source,
        Options(server.url, batch_events=4, flush_seconds=60),
        counts,
        on_delivered=lambda offset, inode: delivered.append((offset, inode)),
    )
    assert [len(batch) for batch in server.received] == [4, 4, 2]
    assert delivered == [(4, 7), (8, 7), (10, 7)]  # checkpointed per batch

    # A lone line is still sent once it has waited flush_seconds.
    lone = fake()
    lines_coming: list[tuple[bytes, int, int]] = []

    def slow_source() -> Iterator[tuple[bytes, int, int]]:
        yield LINES[0], 1, 7
        time.sleep(0.5)  # nothing more for a while
        lines_coming.append((LINES[1], 2, 7))
        yield LINES[1], 2, 7

    sent_at: list[float] = []
    started = time.monotonic()
    ship.ship(
        slow_source(),
        Options(lone.url, batch_events=100, flush_seconds=0.1),
        Counts(),
        on_delivered=lambda offset, inode: sent_at.append(time.monotonic() - started),
    )
    assert [len(batch) for batch in lone.received] == [1, 1]
    assert sent_at[0] < 0.45  # the first line did not wait for the second


def test_the_checkpoint_never_runs_ahead_of_delivery() -> None:
    # The process "dies" while sending the third batch: the checkpoint must
    # cover the two batches delivered, not the one in flight, or a restart
    # would skip lines never stored. (A kill at a random moment rarely lands
    # in that window, so this is checked directly.)
    class DiesOnThirdBatch(Sender):
        def send(self, lines: list[bytes]) -> None:
            self.counts.batches += 1
            if self.counts.batches == 3:
                raise KeyboardInterrupt("killed mid-send")

    counts = Counts()
    delivered: list[int] = []
    source = iter([(line, i + 1, 7) for i, line in enumerate(LINES)])
    with pytest.raises(KeyboardInterrupt):
        ship.ship(
            source,
            Options("http://unused", batch_events=3, flush_seconds=60),
            counts,
            on_delivered=lambda offset, inode: delivered.append(offset),
            sender=DiesOnThirdBatch("http://unused", counts),
        )
    assert delivered == [3, 6]


# End to end: the shipper as a separate process, against a real LogScope.

SRC = Path(__file__).resolve().parents[1] / "src"


def run_shipper(url: str, args: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "logscope.ship", "--url", url, *args],
        env={**os.environ, "PYTHONPATH": str(SRC)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def stored_messages(running: Running) -> Counter[str]:
    events, _ = running.store.search(limit=10**6)
    return Counter(str(e.data.get("msg") or e.data.get("message")) for e in events)


@pytest.fixture
def running(tmp_path: Path) -> Iterator[Running]:
    server = Running(tmp_path, grace=1)
    yield server
    server.stop()


def test_a_file_is_shipped_once_and_a_rerun_sends_nothing_new(
    running: Running, tmp_path: Path
) -> None:
    log_file = tmp_path / "app.log"
    log_file.write_text(
        "".join(
            json.dumps({"msg": f"m{i}", "time": "2026-10-06T21:00:00Z"}) + "\n"
            for i in range(250)
        )
        + "plain startup line\n"
    )
    checkpoint = tmp_path / "app.ckpt"
    args = [
        "--file",
        str(log_file),
        "--checkpoint",
        str(checkpoint),
        "--once",
        "--batch-events",
        "100",
    ]
    first = run_shipper(running.url, args)
    out, err = first.communicate(timeout=30)
    report = json.loads(out.decode().splitlines()[-1])
    assert (
        report["read"],
        report["accepted"],
        report["wrapped"],
        report["batches"],
    ) == (251, 251, 1, 3)
    messages = stored_messages(running)
    assert len(messages) == 251 and set(messages.values()) == {1}
    assert messages["plain startup line"] == 1  # wrapped, not lost
    # Run again from the checkpoint: nothing is sent twice.
    second = run_shipper(running.url, args)
    out, _ = second.communicate(timeout=30)
    assert json.loads(out.decode().splitlines()[-1])["read"] == 0
    assert sum(stored_messages(running).values()) == 251


def test_a_killed_shipper_resends_at_most_one_batch(
    running: Running, tmp_path: Path
) -> None:
    # SIGKILL the shipper while it sends, then rerun it from its checkpoint:
    # every line must arrive, and only lines from the one batch delivered
    # but not yet checkpointed may arrive twice.
    log_file = tmp_path / "app.log"
    total = 20_000
    log_file.write_text(
        "".join(
            json.dumps({"msg": f"m{i}", "time": "2026-10-06T21:00:00Z"}) + "\n"
            for i in range(total)
        )
    )
    checkpoint = tmp_path / "app.ckpt"
    args = [
        "--file",
        str(log_file),
        "--checkpoint",
        str(checkpoint),
        "--once",
        "--batch-events",
        "50",
    ]
    first = run_shipper(running.url, args)
    deadline = time.monotonic() + 30
    while (
        not checkpoint.exists()
        or json.loads(checkpoint.read_text())["offset"] < 200_000
    ):
        assert time.monotonic() < deadline, "the shipper made no progress"
        time.sleep(0.01)
    first.send_signal(signal.SIGKILL)
    first.wait()
    partly = sum(stored_messages(running).values())
    assert 0 < partly < total
    second = run_shipper(running.url, args)
    second.communicate(timeout=60)
    messages = stored_messages(running)
    assert len(messages) == total  # every line arrived
    duplicated = sum(count - 1 for count in messages.values())
    assert duplicated <= 50  # at most the one unacknowledged batch

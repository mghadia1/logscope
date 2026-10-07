"""The live tail, against a real uvicorn server in a thread: an endless
response, a client hanging up, and the server shutting down with a tail
still open are things an in-process test client cannot show."""

import asyncio
import json
import queue
import signal
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn

from logscope.app import _Subscription, create_app
from logscope.events import Event
from logscope.serve import Server
from logscope.settings import Settings
from logscope.store import Store


class Running:
    def __init__(self, tmp_path: Path, grace: float | None) -> None:
        settings = Settings(
            data_dir=tmp_path / "data",
            ui_dir=tmp_path / "no-ui",
            tail_heartbeat_seconds=0.2,
        )
        self.app = create_app(settings)
        self.data_dir = settings.data_dir
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            timeout_graceful_shutdown=grace,  # type: ignore[arg-type]
        )
        self.server = Server(config, self.app)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    @property
    def store(self) -> Store:
        store: Store = self.app.state.store
        return store

    def ingest(self, *events: dict[str, object]) -> None:
        body = "\n".join(json.dumps(e) for e in events).encode()
        response = httpx2.post(f"{self.url}/api/ingest", content=body)
        assert response.status_code == 200

    def stop(self, wait: float = 10) -> bool:
        """Ask the server to stop, without a signal; whether it did within
        ``wait`` seconds."""
        self.server.should_exit = True
        self.thread.join(wait)
        return not self.thread.is_alive()

    def terminate(self, wait: float = 10) -> bool:
        """What SIGTERM does (as Docker stops the container)."""
        self.server.handle_exit(signal.SIGTERM, None)
        self.thread.join(wait)
        return not self.thread.is_alive()


class Tail:
    """A tail connection read by a thread into a queue of lines."""

    def __init__(self, url: str, q: str = "") -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.client = httpx2.Client(timeout=30)
        self.response = self.client.send(
            self.client.build_request("GET", f"{url}/api/tail", params={"q": q}),
            stream=True,
        )
        assert self.response.status_code == 200
        assert self.response.headers["content-type"].startswith("text/event-stream")
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        assert self.next_comment() == ": tailing"  # subscribed from here on

    def _read(self) -> None:
        try:
            for line in self.response.iter_lines():
                self.lines.put(line)
        except Exception:
            pass  # the connection closed

    def next_line(self, timeout: float = 5) -> str:
        return self.lines.get(timeout=timeout)

    def next_comment(self, timeout: float = 5) -> str:
        """The next comment line, skipping the blank lines ending messages."""
        while True:
            line = self.next_line(timeout)
            if line:
                return line

    def next_event(self, timeout: float = 5) -> tuple[str, str, dict[str, Any]]:
        """(id, kind, data) of the next event, skipping comments."""
        fields: dict[str, str] = {}
        while True:
            line = self.next_line(timeout)
            if line.startswith(":"):
                continue
            if line == "" and fields:
                return fields.get("id", ""), fields["event"], json.loads(fields["data"])
            name, _, value = line.partition(": ")
            fields[name] = value

    def close(self) -> None:
        self.response.close()
        self.client.close()


@pytest.fixture
def running(tmp_path: Path) -> Iterator[Running]:
    server = Running(tmp_path, grace=1)
    yield server
    server.stop()


def test_the_tail_sends_new_matching_events(running: Running) -> None:
    tail = Tail(running.url, q="level:error")
    try:
        running.ingest(
            {"level": "info", "msg": "skipped"},
            {"level": "ERROR", "msg": "first error"},
            {"level": "info", "msg": "skipped too"},
        )
        running.ingest({"level": "error", "msg": "second error"})
        event_id, kind, data = tail.next_event()
        assert (event_id, kind) == ("2", "event")
        assert data["event"] == {"level": "ERROR", "msg": "first error"}
        assert data["time"].endswith("Z")
        assert tail.next_event()[2]["event"]["msg"] == "second error"
    finally:
        tail.close()


def test_an_idle_tail_gets_keep_alives(running: Running) -> None:
    tail = Tail(running.url)
    try:
        assert tail.next_comment(timeout=2) == ": keep-alive"
    finally:
        tail.close()


def test_a_client_that_hangs_up_is_unsubscribed(running: Running) -> None:
    tail = Tail(running.url)
    assert len(running.store._subscribers) == 1
    tail.close()
    deadline = time.monotonic() + 5
    while running.store._subscribers:
        assert time.monotonic() < deadline, "still subscribed after the client left"
        time.sleep(0.05)


def test_a_bad_query_is_refused_before_streaming(running: Running) -> None:
    response = httpx2.get(f"{running.url}/api/tail", params={"q": "a (b"})
    assert response.status_code == 422
    assert response.json()["detail"]["position"] == 2


def test_sigterm_ends_open_tails_at_once_and_flushes(tmp_path: Path) -> None:
    server = Running(tmp_path, grace=30)  # the grace period is not what helps
    server.ingest({"msg": "in memory at shutdown"})
    tail = Tail(server.url)
    started = time.monotonic()
    try:
        assert server.terminate(wait=10), "the server did not stop"
        assert time.monotonic() - started < 2
        assert tail.next_event()[1] == "end"  # told why the stream ended
    finally:
        tail.close()
    store = Store(server.data_dir)
    try:
        assert store.open().replayed == 0  # flushed on shutdown, not replayed
    finally:
        store.close()


def test_without_a_signal_the_grace_period_cuts_tails_off(tmp_path: Path) -> None:
    # Uvicorn waits for open requests before running the app's shutdown,
    # which is where the memtable is flushed; a tail never ends on its own.
    # Asked to stop some other way than a signal, the grace period cuts it
    # off, and the flush still happens.
    server = Running(tmp_path, grace=1)
    server.ingest({"msg": "in memory at shutdown"})
    tail = Tail(server.url)
    started = time.monotonic()
    try:
        assert server.stop(wait=10), "the server did not stop"
        assert time.monotonic() - started < 5
    finally:
        tail.close()
    store = Store(server.data_dir)
    try:
        assert store.open().replayed == 0  # flushed on shutdown, not replayed
        events, _ = store.search()
        assert [e.data["msg"] for e in events] == ["in memory at shutdown"]
    finally:
        store.close()


def test_without_a_grace_period_an_open_tail_blocks_shutdown(tmp_path: Path) -> None:
    # Why serve.py sets one: with uvicorn's default (wait forever), the
    # server is still running long after it was asked to stop.
    server = Running(tmp_path, grace=None)
    tail = Tail(server.url)
    try:
        assert not server.stop(wait=2), "stopped although a tail was open"
    finally:
        tail.close()  # the client leaving ends the tail; now it can stop
    assert server.stop(wait=10)


def test_a_slow_subscriber_drops_whole_batches_and_counts_them() -> None:
    async def scenario() -> tuple[int, int]:
        subscription = _Subscription(asyncio.get_running_loop(), batches=1)
        batch = [Event(i, i, {}) for i in range(3)]
        for _ in range(3):  # from another thread, as the store does
            await asyncio.to_thread(subscription.deliver, batch)
        await asyncio.sleep(0.05)  # let the loop run the hand-overs
        return subscription.queue.qsize(), subscription.dropped

    assert asyncio.run(scenario()) == (1, 6)

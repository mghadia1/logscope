"""Ship log lines into LogScope: ``python -m logscope.ship``.

    python -m logscope.ship --file /var/log/app.jsonl [--checkpoint app.ckpt]
    docker compose logs --no-log-prefix -f gatekeeper | python -m logscope.ship

Reads lines from a file (following it as it grows, through rotation and
truncation) or from standard input, and posts them to ``/api/ingest`` in
batches: when a batch reaches ``--batch-events`` lines or ``--batch-bytes``,
or its first line has waited ``--flush-seconds``.

What it promises: **every line is delivered at least once.**

- A network error, a timeout or a 5xx (LogScope answers 503 when its disk
  fails) is retried with exponential backoff and jitter, until it succeeds.
- A 413 (a batch over the server's limits) splits the batch in half and
  sends each half.
- Lines the server rejects (a timestamp without a time zone, ...) are
  counted and reported, not retried: resending will not fix them.
- A line that is not a JSON object (a startup message, a Go panic) is
  wrapped as ``{"message": <line>, "unparsed": true}`` rather than lost.
- With ``--file``, the offset of the last line delivered is checkpointed
  after each batch, so a restarted shipper continues where it stopped. A
  crash between a delivery and its checkpoint resends that batch: at least
  once, never zero times.

Standard library only.
"""

import argparse
import json
import logging
import os
import queue
import random
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger("logscope.ship")

RETRY_BASE_SECONDS = 0.5
RETRY_CAP_SECONDS = 30.0


class FatalError(Exception):
    """The server refused in a way retrying cannot fix (a 4xx other than
    413): a wrong URL, a bug. The shipper stops."""


class Interrupted(Exception):
    """Asked to stop while a batch was still undelivered. It was not
    checkpointed, so the next run sends it again."""


@dataclass
class Counts:
    read: int = 0
    wrapped: int = 0  # not JSON objects, sent wrapped
    accepted: int = 0
    rejected: int = 0
    batches: int = 0
    retries: int = 0
    splits: int = 0
    first_errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.read} lines read, {self.accepted} stored, {self.rejected}"
            f" rejected, {self.wrapped} wrapped; {self.batches} batches,"
            f" {self.retries} retries, {self.splits} splits"
        )


def prepare(line: bytes) -> tuple[bytes, bool]:
    """A line ready to send (empty for a blank line, which is skipped), and
    whether it had to be wrapped."""
    text = line.rstrip(b"\r\n")
    if not text.strip():
        return b"", False
    try:
        if isinstance(json.loads(text), dict):
            return text, False
    except ValueError:
        pass
    message = text.decode("utf-8", errors="replace")
    wrapped = json.dumps({"message": message, "unparsed": True}, ensure_ascii=False)
    return wrapped.encode(), True


class Sender:
    """Posts batches, retrying and splitting as the module docstring says."""

    def __init__(
        self,
        url: str,
        counts: Counts,
        *,
        timeout: float = 30.0,
        stopping: threading.Event | None = None,
    ) -> None:
        self.endpoint = url.rstrip("/") + "/api/ingest"
        self.counts = counts
        self.timeout = timeout
        self.stopping = stopping or threading.Event()

    def send(self, lines: list[bytes]) -> None:
        """Deliver ``lines``; returns once the server has stored (or
        rejected) every one of them."""
        attempt = 0
        while True:
            status, body = self._post(b"\n".join(lines) + b"\n")
            if status == 200:
                result = json.loads(body)
                self.counts.batches += 1
                self.counts.accepted += result["accepted"]
                self.counts.rejected += result["rejected"]
                for error in result["errors"]:
                    if len(self.counts.first_errors) < 20:
                        self.counts.first_errors.append(error["error"])
                return
            if status == 413:
                if len(lines) == 1:
                    # One line bigger than a whole request may be: it can
                    # never be stored; skip it rather than stop for good.
                    self.counts.rejected += 1
                    self.counts.first_errors.append(
                        "a line over the server's body limit"
                    )
                    return
                self.counts.splits += 1
                half = len(lines) // 2
                self.send(lines[:half])
                self.send(lines[half:])
                return
            if 400 <= status < 500 and status != 429:
                raise FatalError(f"{self.endpoint} answered {status}: {body[:200]!r}")
            # A network error (status 0), 429 or 5xx: wait and try again,
            # unless asked to stop.
            attempt += 1
            self.counts.retries += 1
            delay = random.uniform(
                0, min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * 2**attempt)
            )
            log.warning(
                "ingest failed (%s); retry %d in %.1f s", status or body, attempt, delay
            )
            if self.stopping.wait(delay):
                raise Interrupted

    def _post(self, data: bytes) -> tuple[int, bytes]:
        request = urllib.request.Request(
            self.endpoint,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-ndjson"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, OSError) as error:
            return 0, str(error).encode()


# Reading


def follow(
    path: Path,
    offset: int,
    inode: int | None,
    *,
    once: bool,
    poll: float = 0.2,
    stopping: threading.Event | None = None,
) -> Iterator[tuple[bytes, int, int]]:
    """Yield (line, offset after it, inode) for each complete line of
    ``path``, from ``offset`` (if the file is still ``inode``), following
    the file as it grows. A rotated file (a new inode) or a truncated one is
    read from the start. With ``once``, stop at the end of the file."""
    stopping = stopping or threading.Event()
    file: BinaryIO | None = None
    current = None
    partial = b""
    while not stopping.is_set():
        if file is None:
            try:
                file = open(path, "rb")
            except FileNotFoundError:
                if once:
                    return
                stopping.wait(poll)
                continue
            current = os.fstat(file.fileno()).st_ino
            if current == inode:
                file.seek(offset)
            else:
                offset = 0  # a different file: from its start
            partial = b""
        line = file.readline()
        if line.endswith(b"\n"):
            line, partial = partial + line, b""
            offset += len(line)
            assert current is not None
            yield line, offset, current
            continue
        partial += line  # a line still being written; wait for the rest
        if once:
            if partial:  # the file's last line, without a final newline
                assert current is not None
                yield partial, offset + len(partial), current
            return
        try:
            stat = path.stat()
        except FileNotFoundError:
            stat = None
        if stat is None or stat.st_ino != current or stat.st_size < offset:
            # Rotated away or truncated: finish with this file, open anew.
            file.close()
            file = None
            inode = None
            continue
        stopping.wait(poll)
    if file is not None:
        file.close()


def read_checkpoint(path: Path) -> tuple[int, int | None]:
    try:
        saved = json.loads(path.read_text())
        return int(saved["offset"]), int(saved["inode"])
    except FileNotFoundError, ValueError, KeyError:
        return 0, None


def write_checkpoint(path: Path, offset: int, inode: int) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"offset": offset, "inode": inode}))
    os.replace(temporary, path)


# The loop


@dataclass
class Options:
    url: str
    batch_events: int = 1000
    batch_bytes: int = 1024 * 1024
    flush_seconds: float = 1.0


def ship(
    source: Iterator[tuple[bytes, int, int]],
    options: Options,
    counts: Counts,
    *,
    on_delivered: Callable[[int, int], None] = lambda offset, inode: None,
    stopping: threading.Event | None = None,
    sender: Sender | None = None,
) -> None:
    """Batch lines from ``source`` and send them. ``on_delivered(offset,
    inode)`` is called after each batch, for the checkpoint. A reader thread
    feeds a queue, so a batch is flushed on time even while no line comes."""
    stopping = stopping or threading.Event()
    sender = sender or Sender(options.url, counts, stopping=stopping)
    lines: queue.Queue[tuple[bytes, int, int] | None] = queue.Queue(maxsize=10_000)

    def read() -> None:
        try:
            for item in source:
                lines.put(item)
        finally:
            lines.put(None)  # the end of the source

    threading.Thread(target=read, name="logscope-ship-reader", daemon=True).start()
    batch: list[bytes] = []
    size = 0
    started = 0.0
    last: tuple[int, int] | None = None

    def flush() -> None:
        nonlocal batch, size
        if batch:
            sender.send(batch)
            if last is not None:
                on_delivered(*last)
        batch, size = [], 0

    while True:
        if stopping.is_set():
            flush()
            return
        due = started + options.flush_seconds - time.monotonic() if batch else 0.5
        try:
            item = lines.get(timeout=max(0.0, min(0.5, due)))
        except queue.Empty:
            if batch and time.monotonic() - started >= options.flush_seconds:
                flush()
            continue
        if item is None:
            flush()
            return
        raw, offset, inode = item
        counts.read += 1
        prepared, wrapped = prepare(raw)
        if not prepared.strip():
            last = (offset, inode)
            continue
        counts.wrapped += wrapped
        if not batch:
            started = time.monotonic()
        batch.append(prepared)
        size += len(prepared) + 1
        last = (offset, inode)
        if len(batch) >= options.batch_events or size >= options.batch_bytes:
            flush()


def _stdin() -> Iterator[tuple[bytes, int, int]]:
    offset = 0
    for line in sys.stdin.buffer:
        offset += len(line)
        yield line, offset, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://localhost:8200")
    parser.add_argument("--file", type=Path, help="follow this file (default: stdin)")
    parser.add_argument("--checkpoint", type=Path, help="with --file: resume from here")
    parser.add_argument(
        "--once", action="store_true", help="with --file: stop at its end"
    )
    parser.add_argument("--batch-events", type=int, default=1000)
    parser.add_argument("--batch-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--flush-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    stopping = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stopping.set())
    counts = Counts()
    options = Options(args.url, args.batch_events, args.batch_bytes, args.flush_seconds)
    checkpoint: Path | None = args.checkpoint if args.file else None

    def delivered(offset: int, inode: int) -> None:
        if checkpoint is not None:
            write_checkpoint(checkpoint, offset, inode)

    if args.file:
        offset, inode = read_checkpoint(checkpoint) if checkpoint else (0, None)
        source = follow(args.file, offset, inode, once=args.once, stopping=stopping)
    else:
        source = _stdin()
    started = time.monotonic()
    status = 0
    try:
        ship(source, options, counts, on_delivered=delivered, stopping=stopping)
    except FatalError as error:
        log.error("%s", error)
        status = 1
    except Interrupted:
        log.info("stopped with a batch undelivered; it is sent again next time")
    elapsed = time.monotonic() - started
    log.info("%s in %.1f s", counts.summary(), elapsed)
    for message in counts.first_errors[:5]:
        log.info("rejected: %s", message)
    # One machine-readable line for scripts (the benchmark reads it).
    report = {k: v for k, v in vars(counts).items() if k != "first_errors"}
    print(json.dumps({**report, "seconds": round(elapsed, 3)}))
    return status


if __name__ == "__main__":
    sys.exit(main())

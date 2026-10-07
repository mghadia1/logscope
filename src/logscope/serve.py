"""Run the API: ``python -m logscope.serve``.

One process, one worker: the store keeps its memtable in memory and takes a
lock on the data directory, so a second worker would fail to start.
"""

import logging
import os
from types import FrameType

import uvicorn
from fastapi import FastAPI

from .app import app, end_tails

# Uvicorn waits for open responses before running the app's shutdown (where
# the memtable is flushed). Live tails are ended as soon as the signal
# arrives; anything still open after this long is cut off.
SHUTDOWN_GRACE_SECONDS = 5


class Server(uvicorn.Server):
    """Uvicorn, ending live tails the moment shutdown is asked for."""

    def __init__(self, config: uvicorn.Config, served: FastAPI) -> None:
        super().__init__(config)
        self.served = served

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        end_tails(self.served)
        super().handle_exit(sig, frame)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    config = uvicorn.Config(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
    )
    Server(config, app).run()


if __name__ == "__main__":
    main()

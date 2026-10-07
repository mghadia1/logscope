"""A real LogScope server (uvicorn in a thread) for tests that need real
HTTP: endless responses, client hang-ups, shutdowns, other processes."""

import json
import signal
import threading
import time
from pathlib import Path

import httpx2
import uvicorn

from logscope.app import create_app
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

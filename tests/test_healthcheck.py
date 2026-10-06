import socket
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from logscope import healthcheck


@pytest.fixture
def api_answering() -> Iterator[Callable[[int], int]]:
    """Start a local server that answers every GET with a status; return its port."""
    servers: list[HTTPServer] = []

    def start(status: int) -> int:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(status)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_healthy_when_the_api_answers_200(
    monkeypatch: pytest.MonkeyPatch, api_answering: Callable[[int], int]
) -> None:
    monkeypatch.setenv("PORT", str(api_answering(200)))
    assert healthcheck.main() == 0


def test_unhealthy_on_an_error_status(
    monkeypatch: pytest.MonkeyPatch, api_answering: Callable[[int], int]
) -> None:
    monkeypatch.setenv("PORT", str(api_answering(503)))
    assert healthcheck.main() == 1


def test_unhealthy_when_nothing_listens(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("PORT", str(port))
    assert healthcheck.main() == 1

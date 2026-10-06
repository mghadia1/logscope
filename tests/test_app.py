from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from logscope import __version__
from logscope.app import create_app
from logscope.settings import Settings


@pytest.fixture
def built_ui(tmp_path: Path) -> Path:
    """A stand-in for ui/dist: an index page and one hashed bundle."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(
        '<!doctype html><script src="/assets/index-abc123.js"></script>'
    )
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log('hi')")
    return tmp_path


def client(ui_dir: Path) -> TestClient:
    return TestClient(create_app(Settings(ui_dir=ui_dir)))


def test_health_reports_ok_and_the_version(tmp_path: Path) -> None:
    response = client(tmp_path).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_unknown_api_path_is_a_json_404(built_ui: Path) -> None:
    response = client(built_ui).get("/api/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_wrong_method_is_405_not_404(built_ui: Path) -> None:
    # The UI routes must not shadow the API. With a static mount at "/", the
    # file server would answer this: a 405 with no Allow header.
    response = client(built_ui).post("/api/health")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"


def test_serves_the_built_ui(built_ui: Path) -> None:
    c = client(built_ui)
    index = c.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert index.headers["cache-control"] == "no-cache"
    assert "/assets/index-abc123.js" in index.text

    bundle = c.get("/assets/index-abc123.js")
    assert bundle.status_code == 200
    assert bundle.text == "console.log('hi')"


def test_without_a_built_ui_the_root_says_how_to_build_it(tmp_path: Path) -> None:
    response = client(tmp_path / "missing").get("/")
    assert response.status_code == 404
    assert "make ui" in response.json()["detail"]
    # The API still works.
    assert client(tmp_path / "missing").get("/api/health").status_code == 200

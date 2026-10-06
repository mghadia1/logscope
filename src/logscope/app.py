"""The HTTP app: the JSON API under ``/api`` and the built React UI.

The UI is served by two explicit routes, ``/`` (index.html) and ``/assets``
(Vite's hashed bundles), not by a static mount at ``/``. A mount at ``/``
matches every path and method, so the file server, not the router, would
answer a request with the wrong method: ``GET`` on a POST-only route would
be a 404, and ``POST /api/health`` a 405 with no ``Allow`` header (both
checked). It is the same family of bug as Gatekeeper's catch-all 404.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .settings import Settings


class Health(BaseModel):
    status: str
    version: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(
        title="LogScope",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )

    @app.get("/api/health")
    def health() -> Health:
        """Liveness: the process is up and answering."""
        return Health(status="ok", version=__version__)

    _serve_ui(app, settings.ui_dir)
    return app


def _serve_ui(app: FastAPI, ui_dir: Path) -> None:
    index = ui_dir / "index.html"
    assets = ui_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    def ui() -> FileResponse:
        if not index.is_file():
            raise HTTPException(
                status_code=404,
                detail="UI not built: run `make ui`, or use the Vite dev server",
            )
        # index.html names the current hashed bundles, so it must not be
        # cached; the bundles themselves can be.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


app = create_app()

"""Application factory. All API routes live under ``/api``."""

import os
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import callscore_core
from callscore_api import deps, errors
from callscore_api.routers import (
    analysts,
    backtests,
    benchmarks,
    changes,
    leaderboard,
    meta,
    methodologies,
    overview,
    runs,
    scenarios,
    sectors,
    securities,
)

_DEFAULT_WEB_DIST = Path(__file__).resolve().parents[5] / "web" / "dist"


def create_app(web_dist: Path | None = None) -> FastAPI:
    app = FastAPI(title="callscore API", lifespan=deps.lifespan)
    errors.register_error_handlers(app)
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": callscore_core.__version__}

    app.include_router(router)
    for sub in (
        meta.router,
        runs.router,
        scenarios.router,
        methodologies.router,
        analysts.router,
        backtests.router,
        benchmarks.router,
        leaderboard.router,
        securities.router,
        overview.router,
        sectors.router,
        changes.router,
    ):
        app.include_router(sub, prefix="/api")

    dist = (web_dist or Path(os.environ.get("CALLSCORE_WEB_DIST", _DEFAULT_WEB_DIST))).resolve()
    if dist.is_dir():
        index = dist / "index.html"
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404)
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_relative_to(dist) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index)

    return app

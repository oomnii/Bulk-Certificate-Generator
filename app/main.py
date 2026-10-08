"""FastAPI application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.api.certificates import router as certificates_router
from app.api.jobs import router as jobs_router
from app.config import Settings, get_settings
from app.database import configure_database, dispose_database, init_db, session_scope
from app.logging_config import configure_logging
from app.middleware import RequestSizeLimitMiddleware
from app.schemas import HealthResponse
from app.services.pdf_renderer import register_fonts

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    configure_logging(active_settings.log_level)
    configure_database(active_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        register_fonts()
        init_db()
        active_settings.storage_path.mkdir(parents=True, exist_ok=True)
        yield
        dispose_database()

    app = FastAPI(
        title="Bulk Certificate Generator",
        version=__version__,
        summary="Generate certificate PDFs for many recipients in one request.",
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=active_settings.max_request_bytes)
    app.include_router(jobs_router, prefix=API_PREFIX)
    app.include_router(certificates_router, prefix=API_PREFIX)

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    def health() -> HealthResponse | JSONResponse:
        database = "ok"
        try:
            with session_scope() as session:
                session.execute(text("SELECT 1"))
        except Exception:
            database = "unavailable"
        storage = "ok" if active_settings.storage_path.is_dir() else "unavailable"
        body = HealthResponse(
            status="ok" if database == "ok" and storage == "ok" else "degraded",
            database=database,
            storage=storage,
        )
        if body.status != "ok":
            return JSONResponse(status_code=503, content=body.model_dump())
        return body

    return app


app = create_app()

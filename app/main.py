"""Application factory.

Infrastructure is created once per process in the lifespan and stored on the
application state, then injected into routes as typed dependencies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app import __version__
from app.api import health
from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware
from app.core.redis import RedisClient
from app.db.session import Database

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the lifecycle of shared infrastructure."""
    settings: Settings = app.state.settings
    app.state.database = Database(settings)
    app.state.redis = RedisClient(settings)
    logger.info(
        "app.startup",
        extra={
            "event": "app.startup",
            "environment": settings.environment,
            "version": __version__,
        },
    )
    try:
        yield
    finally:
        await app.state.redis.close()
        await app.state.database.dispose()
        logger.info("app.shutdown", extra={"event": "app.shutdown"})


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a configured application instance."""
    resolved = settings or get_settings()
    configure_logging(resolved)

    app = FastAPI(
        title=resolved.app_name,
        version=__version__,
        summary="Multi-tenant AI customer engagement platform for WhatsApp Business",
        docs_url="/docs" if resolved.docs_enabled else None,
        redoc_url="/redoc" if resolved.docs_enabled else None,
        openapi_url="/openapi.json" if resolved.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = resolved

    app.add_middleware(RequestContextMiddleware, header_name=resolved.request_id_header)
    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[resolved.request_id_header],
        )

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_router, prefix=resolved.api_v1_prefix)

    return app


app = create_app()

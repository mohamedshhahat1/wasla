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
from app.core.limits import BodySizeLimitMiddleware, RequestTimeoutMiddleware
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
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

    # Ordering matters and is deliberate. Middleware added later runs first,
    # so the body limit is outermost: an oversized request is refused before
    # anything else touches it, including the request logger. The timeout sits
    # inside it, and the request context innermost, so a timed-out request still
    # gets a request id in its log line.
    # Innermost of the three, so every response leaves with these headers -
    # including the ones an exception handler produces, which is where a
    # stack trace would otherwise be served without them.
    app.add_middleware(
        SecurityHeadersMiddleware,
        trusted_proxies=frozenset(resolved.trusted_proxy_ips),
    )
    app.add_middleware(RequestContextMiddleware, header_name=resolved.request_id_header)
    app.add_middleware(
        RequestTimeoutMiddleware,
        timeout_seconds=resolved.request_timeout_seconds,
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=resolved.max_request_bytes,
        webhook_max_bytes=resolved.webhook_max_request_bytes,
    )
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

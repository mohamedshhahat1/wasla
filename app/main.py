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
from app.api import health, metrics
from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.limits import BodySizeLimitMiddleware, RequestTimeoutMiddleware
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.core.redis import RedisClient
from app.core.request_metrics import RequestMetricsMiddleware
from app.core.request_tracing import RequestTracingMiddleware
from app.core.telemetry import set_counter_sink
from app.core.tracing import API_SERVICE_NAME, configure_tracing, shutdown_tracing
from app.db.session import Database
from app.integrations.email import require_delivery_verification

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the lifecycle of shared infrastructure."""
    settings: Settings = app.state.settings
    # Before anything else in the lifespan, so a start-up that needs tracing
    # and cannot have it fails here rather than serving untraced traffic. With
    # `TRACING_ENABLED` off - the default - this builds nothing and returns.
    configure_tracing(settings, service_name=API_SERVICE_NAME)
    app.state.database = Database(settings)
    app.state.redis = RedisClient(settings)
    # Cross-process counters go to the same Redis everything else uses. Set
    # here rather than passed through every provider client, which would make
    # each of them take an argument it uses for nothing but counting.
    set_counter_sink(app.state.redis.client if settings.metrics_enabled else None)
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
        # Cleared before the client closes, so nothing counts into a pool that
        # is going away.
        set_counter_sink(None)
        await app.state.redis.close()
        await app.state.database.dispose()
        # Last, so spans covering the teardown above still have somewhere to go.
        shutdown_tracing()
        logger.info("app.shutdown", extra={"event": "app.shutdown"})


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a configured application instance."""
    resolved = settings or get_settings()
    configure_logging(resolved)
    # The API half of the email configuration, checked in the process that
    # needs it rather than in `Settings` - which every process builds, and
    # which would therefore force the webhook secret into the worker container
    # too (ADR-063). `build_email_provider` does the same for the credential
    # only the worker needs.
    require_delivery_verification(resolved)

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
    # Innermost of everything, so the duration it measures is the handler's and
    # the status it records is the one the handler produced rather than one a
    # layer above rewrote. It is added first for that reason: middleware added
    # later runs earlier.
    if resolved.metrics_enabled:
        app.add_middleware(
            RequestMetricsMiddleware,
            # The scrape and the liveness probe. Counting either would make the
            # busiest route in the exposition one nobody is served by, and a
            # scraper counting its own scrapes is a closed loop.
            exclude_paths=frozenset({"/metrics", "/health/live"}),
        )
    # Outside the metrics middleware, so everything it measures happens inside
    # the server span, and a request rejected before routing still produces
    # one. Added after it for that reason: middleware added later runs earlier.
    app.add_middleware(
        RequestTracingMiddleware,
        exclude_paths=frozenset({"/metrics", "/health/live"}),
    )
    # Innermost of the three below, so every response leaves with these headers -
    # including the ones an exception handler produces, which is where a
    # stack trace would otherwise be served without them.
    app.add_middleware(
        SecurityHeadersMiddleware,
        trusted_proxies=resolved.trusted_proxy_ips,
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
    # Unversioned and unprefixed, like the health probes: a scraper's path is
    # part of the deployment's shape rather than of the product's API, and
    # moving it under `/api/v1` would put it behind the public proxy's
    # catch-all rather than beside the paths nginx already treats specially.
    app.include_router(metrics.router)
    app.include_router(api_router, prefix=resolved.api_v1_prefix)

    return app


app = create_app()

"""One span per served request, named for the route rather than the URL.

The same `route` problem `app/core/request_metrics.py` solves, and the same
answer: a span named `POST /api/v1/leads/{lead_id}` is one name out of a bounded
set, and `POST /api/v1/leads/8f3c…` is a name per lead. A trace backend groups,
searches and aggregates by span name, so an unbounded one is the tracing
equivalent of an unbounded metric label — except that here the identifier is
also *content*, visible to whoever operates the collector.

The route is not known until Starlette has matched one, which is after the
handler has been called. So the span opens under a provisional name and is
renamed on the way out: a span's name is not read until it is exported, and
export happens after the span ends.

This middleware sits *outside* the metrics middleware, so that everything the
metrics middleware measures happens inside the span, and a request that the
body-size limit or the timeout rejects still produces one.

No inbound trace context is honoured — see `app/core/tracing.py` for why every
API request starts a new trace.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.request_metrics import UNMATCHED, normalise_method, route_template
from app.core.tracing import HTTP_METHOD, HTTP_ROUTE, HTTP_STATUS, SpanKind, span


class RequestTracingMiddleware(BaseHTTPMiddleware):
    """Opens the server span every other span in a request hangs from."""

    def __init__(self, app: ASGIApp, *, exclude_paths: frozenset[str] = frozenset()) -> None:
        super().__init__(app)
        # The scrape and the liveness probe, excluded for the reason the
        # metrics middleware excludes them: an orchestrator's probe every few
        # seconds would be the most common trace in the backend and would say
        # nothing about the product.
        self._excluded = exclude_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self._excluded:
            return await call_next(request)

        method = normalise_method(request.method)
        # Named for the placeholder the metrics middleware already uses for a
        # request that matched nothing, so a 404 keeps it and every unrouted
        # request is one span name rather than one per path a scanner tried.
        with span(UNMATCHED, kind=SpanKind.SERVER, attributes={HTTP_METHOD: method}) as active:
            try:
                response = await call_next(request)
            finally:
                # In `finally`, so a request that raised still gets its route.
                # `span` has already recorded the exception's class name as the
                # status by the time this runs on the failing path.
                route = route_template(request)
                active.set_attribute(HTTP_ROUTE, route)
                active.update_name(f"{method} {route}")
            active.set_attribute(HTTP_STATUS, response.status_code)
            return response


__all__ = ["RequestTracingMiddleware"]

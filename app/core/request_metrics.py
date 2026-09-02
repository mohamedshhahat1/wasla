"""Counting requests without turning every URL into a time series.

The whole difficulty of HTTP metrics is the `route` label. `/api/v1/leads/{id}`
is one series; `/api/v1/leads/<the id of every lead anybody ever opened>` is a
series per lead, and a scraper that meets one of those does not recover on its
own. So the label is read from the route Starlette *matched*, never from the
path that was requested: `request.scope["route"].path` is by construction one
of the application's own route templates, and there are 124 of them.

Two paths do not match a route at all. A request for something that does not
exist gets `__unmatched__`, and a request rejected before routing - an
oversized body, a timed-out handler - is counted by whichever middleware
answered it, under the same label, because neither reached a route. One extra
series, bounded, and it keeps 404 floods from inventing series named after
whatever a scanner asked for.

This middleware is added *innermost* so its timing covers the handler and its
status is the one the handler produced. The security headers, the request id
and the body limit sit outside it and are measured by the proxy, which is the
layer that knows about them.
"""

from __future__ import annotations

import contextlib
from time import perf_counter

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp

from app.core.telemetry import HTTP_IN_FLIGHT, observe_http

# What a request that matched no route is counted as. One series, whatever was
# asked for.
UNMATCHED: str = "__unmatched__"

# The methods worth their own series. Anything else - a scanner's `PROPFIND`,
# a malformed verb - collapses here, because the method is attacker-controlled
# and a label domain must not be.
KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
OTHER_METHOD: str = "OTHER"


def route_template(request: Request) -> str:
    """The matched route's path, or the placeholder for one that matched none.

    FastAPI puts the matched route on the scope before calling the endpoint,
    and `BaseHTTPMiddleware` shares that scope dictionary, so reading it after
    `call_next` gets the route the router chose. A request that matched none
    leaves it absent, which is the 404 case.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None) if isinstance(route, Route) else None
    return path if isinstance(path, str) and path else UNMATCHED


def normalise_method(method: str) -> str:
    upper = method.upper()
    return upper if upper in KNOWN_METHODS else OTHER_METHOD


class RequestMetricsMiddleware(BaseHTTPMiddleware):
    """Records rate, status class and latency for every served request."""

    def __init__(self, app: ASGIApp, *, exclude_paths: frozenset[str] = frozenset()) -> None:
        super().__init__(app)
        # The scrape itself, and the liveness probe an orchestrator runs every
        # few seconds. Counting either would make the busiest route in the
        # exposition the one nobody is served by.
        self._excluded = exclude_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self._excluded:
            return await call_next(request)

        method = normalise_method(request.method)
        started = perf_counter()
        _adjust_in_flight(1.0)
        try:
            response = await call_next(request)
        except Exception:
            # The exception handlers turn this into a 500 further out, so it is
            # counted as one here rather than left uncounted.
            observe_http(
                method=method,
                route=route_template(request),
                status_code=500,
                duration_seconds=perf_counter() - started,
            )
            raise
        else:
            observe_http(
                method=method,
                route=route_template(request),
                status_code=response.status_code,
                duration_seconds=perf_counter() - started,
            )
            return response
        finally:
            _adjust_in_flight(-1.0)


def _adjust_in_flight(amount: float) -> None:
    # A gauge with no labels has nothing to reject, but this sits on the
    # request path and instrumentation never gets to fail one.
    with contextlib.suppress(Exception):
        HTTP_IN_FLIGHT.add(amount)


__all__ = [
    "KNOWN_METHODS",
    "OTHER_METHOD",
    "UNMATCHED",
    "RequestMetricsMiddleware",
    "normalise_method",
    "route_template",
]

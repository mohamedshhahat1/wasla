"""The scrape endpoint.

**Who may read this, and how that is enforced.** Nothing here authenticates,
and that is a decision rather than an omission (ADR-070). The API container
publishes no port: in `docker-compose.prod.yml` only nginx is reachable from
outside the internal network, and `nginx.conf` refuses `/metrics` on the public
listener rather than proxying it. A scraper therefore reaches this by sitting
on the internal network, which is the same access control the API's own
`8000` already relies on for everything else it serves.

A shared bearer token was considered and rejected. Every scraper in a
deployment would hold the same one, it would need distributing to whatever
collects metrics, and it would protect a document that by construction carries
no customer data - `app.core.metrics` refuses an identifier as a label, and
there is a test that proves it. A credential whose loss costs nothing is a
credential that gets treated as if losing it costs nothing.

What it does expose is operational shape: route names, request rates, queue
depths, provider error counts. That is worth keeping off the internet, which
is what the nginx refusal is for.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.route import CommittingRoute
from app.core.dependencies import RedisDep, SettingsDep
from app.services.metrics_service import MetricsService

router = APIRouter(route_class=CommittingRoute, tags=["health"])

# Prometheus' text exposition content type. The version parameter is part of
# the contract; a scraper reads it to decide how to parse the body.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get(
    "/metrics",
    summary="Operational metrics in Prometheus text format",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {"content": {"text/plain": {}}},
        status.HTTP_404_NOT_FOUND: {"description": "Metrics are disabled on this deployment."},
    },
    include_in_schema=False,
)
async def metrics(redis: RedisDep, settings: SettingsDep) -> Response:
    """Render every signal this deployment publishes.

    404 rather than 403 when disabled, because "this deployment does not serve
    metrics" and "you may not read them" are different answers and only the
    first one is true. A 403 would also confirm the endpoint exists, which is
    the one thing a disabled endpoint should not do.
    """
    if not settings.metrics_enabled:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    body = await MetricsService(redis.client).render()
    return Response(content=body, media_type=CONTENT_TYPE)

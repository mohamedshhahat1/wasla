"""Health endpoints.

Liveness answers "is the process alive" and must never depend on PostgreSQL or
Redis, otherwise an orchestrator restarts healthy containers during a database
blip. Readiness answers "can this instance serve traffic" and does check the
dependencies required to do so.
"""

from __future__ import annotations

from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app import __version__
from app.api.route import CommittingRoute
from app.core.dependencies import DatabaseDep, RedisDep, SettingsDep
from app.schemas.health import (
    ComponentStatus,
    HealthSummaryResponse,
    LivenessResponse,
    ReadinessResponse,
)
from app.services.health_service import HealthService

router = APIRouter(route_class=CommittingRoute, prefix="/health", tags=["health"])


def get_health_service(
    database: DatabaseDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> HealthService:
    """Assemble the readiness probes for this deployment."""
    timeout = settings.health_check_timeout_seconds
    return HealthService(
        {
            "postgresql": partial(database.check, timeout),
            "redis": partial(redis.check, timeout),
        }
    )


HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]


@router.get(
    "",
    response_model=HealthSummaryResponse,
    summary="Service identity and status",
)
async def health_summary(settings: SettingsDep) -> HealthSummaryResponse:
    """Cheap status summary. Performs no dependency checks."""
    return HealthSummaryResponse(
        app=settings.app_name,
        version=__version__,
        environment=settings.environment,
    )


@router.get(
    "/live",
    response_model=LivenessResponse,
    summary="Liveness probe",
)
async def liveness() -> LivenessResponse:
    """Liveness must not depend on external systems."""
    return LivenessResponse()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(health_service: HealthServiceDep, response: Response) -> ReadinessResponse:
    """Verify every dependency required to serve traffic."""
    report = await health_service.check_readiness()
    if not report.is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status=report.status,
        components=[
            ComponentStatus(
                name=component.name,
                status=component.status,
                duration_ms=component.duration_ms,
                detail=component.detail,
            )
            for component in report.components
        ],
    )

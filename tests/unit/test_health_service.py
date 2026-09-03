"""Health service tests."""

from __future__ import annotations

import asyncio
from time import perf_counter

from app.core.exceptions import DependencyUnavailableError
from app.services.health_service import HealthService


async def _healthy() -> None:
    return None


async def test_report_is_ready_when_every_probe_passes() -> None:
    report = await HealthService({"postgresql": _healthy, "redis": _healthy}).check_readiness()

    assert report.is_ready
    assert report.status == "ok"
    assert [component.name for component in report.components] == ["postgresql", "redis"]
    assert all(component.duration_ms >= 0 for component in report.components)


async def test_domain_failure_marks_only_that_component_down() -> None:
    async def unavailable() -> None:
        raise DependencyUnavailableError("Redis is unavailable.", details={"dependency": "redis"})

    report = await HealthService({"postgresql": _healthy, "redis": unavailable}).check_readiness()

    assert not report.is_ready
    assert report.status == "degraded"
    components = {component.name: component for component in report.components}
    assert components["postgresql"].status == "up"
    assert components["redis"].status == "down"
    assert components["redis"].detail == "Redis is unavailable."


async def test_unexpected_failure_is_contained_and_not_leaked() -> None:
    async def boom() -> None:
        raise RuntimeError("asyncpg internal detail")

    report = await HealthService({"postgresql": boom}).check_readiness()

    component = report.components[0]
    assert component.status == "down"
    assert component.detail == "Dependency probe failed."
    assert "asyncpg" not in (component.detail or "")


async def test_probes_run_concurrently() -> None:
    async def slow() -> None:
        await asyncio.sleep(0.05)

    started = perf_counter()
    report = await HealthService({"a": slow, "b": slow, "c": slow}).check_readiness()
    elapsed = perf_counter() - started

    assert report.is_ready
    assert elapsed < 0.12

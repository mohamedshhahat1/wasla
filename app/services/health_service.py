"""Readiness evaluation.

Probes run concurrently and every failure is contained: a readiness check
reports a degraded dependency, it never raises. That keeps the endpoint useful
precisely when infrastructure is misbehaving.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Literal

from app.core.exceptions import WaslaError
from app.core.logging import get_logger

logger = get_logger(__name__)

Probe = Callable[[], Awaitable[None]]
ComponentState = Literal["up", "down"]


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """Result of one dependency probe."""

    name: str
    status: ComponentState
    duration_ms: float
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Aggregate readiness across all probed dependencies."""

    components: tuple[ComponentHealth, ...]

    @property
    def is_ready(self) -> bool:
        return all(component.status == "up" for component in self.components)

    @property
    def status(self) -> Literal["ok", "degraded"]:
        return "ok" if self.is_ready else "degraded"


class HealthService:
    """Runs readiness probes for the dependencies of this deployment."""

    def __init__(self, probes: Mapping[str, Probe]) -> None:
        self._probes = dict(probes)

    async def check_readiness(self) -> HealthReport:
        results = await asyncio.gather(
            *(self._probe(name, probe) for name, probe in self._probes.items())
        )
        return HealthReport(components=tuple(results))

    async def _probe(self, name: str, probe: Probe) -> ComponentHealth:
        started = perf_counter()
        try:
            await probe()
        except WaslaError as exc:
            return ComponentHealth(
                name=name,
                status="down",
                duration_ms=self._elapsed_ms(started),
                detail=exc.message,
            )
        except Exception as exc:
            # Never leak driver internals through a public endpoint.
            logger.exception(
                "health.probe_failed",
                extra={
                    "event": "health.probe_failed",
                    "component": name,
                    "reason": type(exc).__name__,
                },
            )
            return ComponentHealth(
                name=name,
                status="down",
                duration_ms=self._elapsed_ms(started),
                detail="Dependency probe failed.",
            )
        return ComponentHealth(name=name, status="up", duration_ms=self._elapsed_ms(started))

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((perf_counter() - started) * 1000, 2)

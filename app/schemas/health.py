"""Health endpoint schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ComponentStatus(BaseModel):
    """Outcome of a single dependency probe."""

    name: str = Field(examples=["postgresql"])
    status: Literal["up", "down"]
    duration_ms: float = Field(ge=0, examples=[1.42])
    detail: str | None = Field(default=None, description="Safe failure summary when down.")


class HealthSummaryResponse(BaseModel):
    """Service identity. Performs no dependency checks."""

    status: Literal["ok"] = "ok"
    app: str
    version: str
    environment: str


class LivenessResponse(BaseModel):
    """Process is running and able to serve requests."""

    status: Literal["alive"] = "alive"


class ReadinessResponse(BaseModel):
    """Aggregate readiness plus per-dependency detail."""

    status: Literal["ok", "degraded"]
    components: list[ComponentStatus]

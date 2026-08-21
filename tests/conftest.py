"""Shared test fixtures.

httpx's ASGITransport does not run lifespan events, so infrastructure is
injected onto the application state directly. The suite therefore needs no
PostgreSQL, Redis, Meta or OpenAI credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError
from app.main import create_app


class FakeDependency:
    """Stands in for the database or Redis client."""

    def __init__(self, *, name: str, healthy: bool = True) -> None:
        self.name = name
        self.healthy = healthy
        self.calls = 0

    async def check(self, timeout: float | None = None) -> None:
        self.calls += 1
        if not self.healthy:
            raise DependencyUnavailableError(
                f"{self.name} is unavailable.",
                details={"dependency": self.name},
            )

    async def close(self) -> None:
        return None

    async def dispose(self) -> None:
        return None


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
    )


@pytest.fixture
def fake_database() -> FakeDependency:
    return FakeDependency(name="postgresql")


@pytest.fixture
def fake_redis() -> FakeDependency:
    return FakeDependency(name="redis")


@pytest.fixture
def app(
    settings: Settings,
    fake_database: FakeDependency,
    fake_redis: FakeDependency,
) -> FastAPI:
    application = create_app(settings)
    application.state.database = fake_database
    application.state.redis = fake_redis
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as http_client:
        yield http_client

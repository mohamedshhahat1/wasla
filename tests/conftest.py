"""Shared test fixtures.

httpx's ASGITransport does not run lifespan events, so infrastructure is
injected onto the application state directly. The suite therefore needs no
PostgreSQL, Redis, Meta or OpenAI credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError
from app.main import create_app


class FakeRedisCommands:
    """The only Redis command a request issues: enqueueing work.

    Reserving, releasing and dead-lettering belong to the worker, which is
    covered by its own fake. Implementing them here would invite a route to
    start using them.
    """

    def __init__(self) -> None:
        self.pushed: dict[str, list[str]] = {}

    async def rpush(self, key: str, value: str) -> int:
        queue = self.pushed.setdefault(key, [])
        queue.append(value)
        return len(queue)


class FakeDependency:
    """Stands in for the database or Redis client.

    The check signature mirrors the real clients so the fake stays honest.
    """

    def __init__(self, *, name: str, healthy: bool = True) -> None:
        self.name = name
        self.healthy = healthy
        self.calls = 0
        self.commands = FakeRedisCommands()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Mirrors Database.session, yielding a session bound to nothing.

        Routes that reject a request before touching the database - an absent
        bearer token, a failed authorization check - resolve their session
        dependency all the same, so one has to exist. It is deliberately
        unbound: a test that reaches an actual query fails loudly here instead
        of silently passing against a stub, and belongs in the PostgreSQL-backed
        suite or should override the service dependency outright.
        """
        yield AsyncSession()

    @property
    def client(self) -> FakeRedisCommands:
        """Mirrors RedisClient.client, which routes that queue work reach for."""
        return self.commands

    async def check(self, timeout_seconds: float | None = None) -> None:
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

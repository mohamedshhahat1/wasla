"""Shared test fixtures.

httpx's ASGITransport does not run lifespan events, so infrastructure is
injected onto the application state directly. The suite therefore needs no
PostgreSQL, Redis, Meta or OpenAI credentials.
"""

from __future__ import annotations

import os

# Set before anything imports `app`, and that ordering is load-bearing.
# `app/main.py` builds a `Settings` at module scope, and the settings validator
# now requires a real `JWT_SECRET` in every environment except `test` - so
# importing the application with no `ENVIRONMENT` set would raise during
# collection rather than run the suite. Pinning it here makes the suite
# self-sufficient no matter what the developer's shell happens to hold, and
# `setdefault` leaves an explicit override alone.
os.environ.setdefault("ENVIRONMENT", "test")

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError
from app.db.models.billing import LimitKey
from app.main import create_app
from app.services.entitlement_service import Entitlement


class FakeRedisCommands:
    """The Redis commands a request issues: enqueueing work, and counting it.

    Reserving, releasing and dead-lettering belong to the worker, which is
    covered by its own fake. Implementing them here would invite a route to
    start using them.

    The counter commands exist because the rate limiter runs inside the request
    path. They are only reached by tests that switch limiting on - the default
    settings fixture leaves it off.
    """

    def __init__(self) -> None:
        self.pushed: dict[str, list[str]] = {}
        self.counters: dict[str, int] = {}
        self.expiries: dict[str, int] = {}

    async def rpush(self, key: str, value: str) -> int:
        queue = self.pushed.setdefault(key, [])
        queue.append(value)
        return len(queue)

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiries[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        return self.expiries.get(key, -1)


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
        # Off by default here, and deliberately. A limiter counting across a
        # file makes every test in it order-dependent: the eleventh login would
        # fail for a reason the test never mentions. The limiter has its own
        # tests, which switch it on.
        rate_limit_enabled=False,
    )


@pytest.fixture
def fake_database() -> FakeDependency:
    return FakeDependency(name="postgresql")


@pytest.fixture
def fake_redis() -> FakeDependency:
    return FakeDependency(name="redis")


class AllowingEntitlements:
    """An entitlement service that permits everything, and queries nothing.

    Routes that create a limited resource declare a plan-limit guard, and that
    guard reads the database - a count of agents, a sum of usage. The fake
    session here is deliberately unbound, so without this override every
    endpoint test that merely stubs its own service would fail on a query it is
    not about.

    Allowing by default is the right bias for these tests: they exist to pin
    routing, shapes and roles. What a limit actually does is proved against real
    rows in `tests/integration/test_entitlements.py`, and the refusals are
    proved in `test_plan_enforcement.py` by overriding this again.
    """

    async def check(self, key, *, additional: int = 1) -> Entitlement:
        return Entitlement(key=key, limit=None, used=0, allowed=True)

    async def require(self, key, *, additional: int = 1) -> Entitlement:
        return await self.check(key, additional=additional)

    async def allows(self, key, *, additional: int = 1) -> bool:
        return True

    async def snapshot(self, keys=None) -> list[Entitlement]:
        return [await self.check(key, additional=0) for key in LimitKey]


@pytest.fixture
def app(
    settings: Settings,
    fake_database: FakeDependency,
    fake_redis: FakeDependency,
) -> FastAPI:
    application = create_app(settings)
    application.state.database = fake_database
    application.state.redis = fake_redis
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as http_client:
        yield http_client

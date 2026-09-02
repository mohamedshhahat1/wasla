"""Async database engine and session management.

One ``Database`` instance is created per process during application startup and
stored on the application state, so the engine and its connection pool are
shared. Sessions are short-lived and request-scoped.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class Database:
    """Owns the async engine and hands out sessions."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine = create_async_engine(
            settings.database_url,
            echo=settings.database_echo,
            pool_pre_ping=True,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout,
            pool_recycle=settings.database_pool_recycle_seconds,
            connect_args=self._connect_args(settings),
        )
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @staticmethod
    def _connect_args(settings: Settings) -> dict[str, Any]:
        """asyncpg-specific connect arguments, skipped for other drivers."""
        if "asyncpg" not in settings.database_url:
            return {}
        return {
            "timeout": settings.database_connect_timeout_seconds,
            "server_settings": {"application_name": settings.app_name.lower()},
        }

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, committing on success and rolling back on failure.

        Used directly by the workers, where this *is* the unit of work. Requests
        take a different path: they commit inside the handler chain rather than
        here, because a commit in this teardown finishes after the response has
        already been sent (see `app.core.dependencies.get_session`). The commit
        below is then a no-op for them - the transaction is already closed - and
        the rollback still does its job on the way out of an exception.
        """
        session = self._session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def check(self, timeout_seconds: float | None = None) -> None:
        """Verify connectivity. Raises DependencyUnavailableError on failure."""
        limit = self._settings.health_check_timeout_seconds
        if timeout_seconds is not None:
            limit = timeout_seconds
        try:
            async with asyncio.timeout(limit):
                await self._select_one()
        except Exception as exc:
            logger.warning(
                "health.database_unavailable",
                extra={"event": "health.database_unavailable", "reason": type(exc).__name__},
            )
            raise DependencyUnavailableError(
                "PostgreSQL is unavailable.",
                details={"dependency": "postgresql"},
            ) from exc

    async def _select_one(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        """Close all pooled connections."""
        await self._engine.dispose()


@asynccontextmanager
async def released(session: AsyncSession) -> AsyncIterator[None]:
    """Hand this session's connection back while something slow happens.

    For calls that take a network round trip to somebody else's API. A pooled
    connection held across an inference is a connection no other worker can
    use, and it makes the effective concurrency of an agent turn
    `pool_size + max_overflow` rather than the queue depth - which is the
    bottleneck this exists to remove (ADR-080).

    **It commits.** That is not a side effect to work around, it is the
    mechanism: SQLAlchemy returns a connection to the pool when the transaction
    ends, so nothing short of ending it releases anything. Two consequences
    the caller has to mean:

    - Everything staged so far becomes durable. Callers therefore use this at a
      point where what is staged is a finished unit of work - a sentiment
      reading and the request that paid for it, a tool's rows and its audit
      entry - and never mid-write.
    - The transaction that resumes afterwards is a *new* one, so anything read
      before the call is a snapshot from before it. `expire_on_commit=False`
      keeps those objects readable, which is what makes the snapshot usable;
      what it does not do is make it fresh. State that may have changed while
      the provider was thinking has to be read again, deliberately, by the
      caller that cares.

    Nothing may touch the session inside the block. Doing so silently opens a
    new transaction and checks a connection straight back out, which is the
    regression `tests/integration/test_provider_session_lifetime.py` exists to
    catch.
    """
    await session.commit()
    yield

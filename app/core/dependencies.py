"""Dependency injection providers.

Infrastructure lives on the application state and is injected into routes as
typed dependencies, so services never reach for globals and tests can swap
implementations wholesale.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.config import Settings
from app.core.redis import RedisClient
from app.db.session import Database


def get_settings_from_state(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def get_redis(request: Request) -> RedisClient:
    return cast(RedisClient, request.app.state.redis)


# Where the request's session is parked so the route wrapper can find it. A
# dependency cannot hand anything back to the layer above it, and the commit has
# to happen there - see `CommittingRoute`.
SESSION_STATE_ATTRIBUTE = "db_session"


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped session.

    The commit does **not** happen here, and that is the point. This is a
    ``yield`` dependency, so its teardown runs after the response has already
    reached the client - measurably so: with a commit costing a network round
    trip and an fsync, the API answers ``201 Created`` tens of milliseconds
    before the write is durable.

    Two consequences, and the second is the serious one. A client that acts on
    the response immediately can read back nothing - a token minted by
    ``/auth/register`` was rejected for ~50 ms, which looks like a broken
    product. And a commit that *fails* after the response has been sent leaves
    the caller holding a success for something that did not happen.

    So the session is parked on the request and committed by
    :class:`~app.api.route.CommittingRoute`, which runs inside the handler
    chain. The context manager below still owns the rollback: an exception
    unwinds through it and the transaction is discarded, which is exactly where
    that belongs.
    """
    database = get_database(request)
    async with database.session() as session:
        setattr(request.state, SESSION_STATE_ATTRIBUTE, session)
        yield session


def get_request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


SettingsDep = Annotated[Settings, Depends(get_settings_from_state)]
DatabaseDep = Annotated[Database, Depends(get_database)]
RedisDep = Annotated[RedisClient, Depends(get_redis)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RequestIdDep = Annotated[str | None, Depends(get_request_id)]

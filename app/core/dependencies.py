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


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped session that commits or rolls back on exit."""
    database = get_database(request)
    async with database.session() as session:
        yield session


def get_request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


SettingsDep = Annotated[Settings, Depends(get_settings_from_state)]
DatabaseDep = Annotated[Database, Depends(get_database)]
RedisDep = Annotated[RedisClient, Depends(get_redis)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RequestIdDep = Annotated[str | None, Depends(get_request_id)]

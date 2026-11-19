"""Migration 0141 against a database holding accounts and invitations (MG7-04).

Built from scratch to 0140 and seeded with plain SQL - never through the
models, which map the columns 0141 adds and would name them in their INSERT.
Upgraded to 0141: the rows are kept, the three columns are empty, the
language constraint refuses a language the product does not speak, and a
hard-deleted workspace empties the pointer to it. Downgraded and upgraded
again: the rows survive the round trip.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _alembic(url: str, action: str, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["wasla_database_url"] = url
    getattr(command, action)(config, revision)


async def _admin(admin_url: str, statement: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


async def _run(url: str, statement: str, **params: Any) -> list[tuple[Any, ...]]:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


async def _seed(url: str) -> dict[str, uuid.UUID]:
    ids = {name: uuid.uuid4() for name in ("user", "tenant", "spare", "invitation")}
    for tenant, slug in ((ids["tenant"], "mg7-kept"), (ids["spare"], "mg7-spare")):
        await _run(
            url,
            "INSERT INTO tenants (id, name, slug, status, timezone) "
            "VALUES (:id, 'MG7', :slug, 'active', 'Africa/Cairo')",
            id=tenant,
            slug=f"{slug}-{uuid.uuid4().hex[:6]}",
        )
    await _run(
        url,
        "INSERT INTO users (id, email, is_active) VALUES (:id, :email, true)",
        id=ids["user"],
        email=f"mg7-{uuid.uuid4().hex[:8]}@example.test",
    )
    await _run(
        url,
        "INSERT INTO tenant_invitations (id, tenant_id, email, role, status, token_hash, "
        "expires_at) VALUES (:id, :tenant, 'invitee@example.test', 'member', 'pending', "
        ":hash, now() + interval '7 days')",
        id=ids["invitation"],
        tenant=ids["tenant"],
        hash=uuid.uuid4().hex,
    )
    return ids


async def _state(url: str, ids: dict[str, uuid.UUID]) -> tuple[Any, ...]:
    user = await _run(url, "SELECT id, email FROM users WHERE id = :id", id=ids["user"])
    invitation = await _run(
        url, "SELECT id, email FROM tenant_invitations WHERE id = :id", id=ids["invitation"]
    )
    return (user, invitation)


def test_0141_keeps_rows_and_guards_its_columns(database_url: str) -> None:
    name = f"wasla_mg7_{uuid.uuid4().hex[:12]}"
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    try:
        _alembic(target, "upgrade", "0140")
        ids = asyncio.run(_seed(target))
        before = asyncio.run(_state(target, ids))

        _alembic(target, "upgrade", "0141")
        assert asyncio.run(_state(target, ids)) == before
        assert asyncio.run(
            _run(
                target, "SELECT locale, last_workspace_id FROM users WHERE id = :id", id=ids["user"]
            )
        ) == [(None, None)]
        assert asyncio.run(
            _run(
                target,
                "SELECT locale FROM tenant_invitations WHERE id = :id",
                id=ids["invitation"],
            )
        ) == [(None,)]

        # Every language the product speaks is accepted; any other is refused.
        for code in ("en", "ar", "fr", "tr"):
            asyncio.run(
                _run(target, "UPDATE users SET locale = :c WHERE id = :id", c=code, id=ids["user"])
            )
        with pytest.raises(IntegrityError):
            asyncio.run(
                _run(target, "UPDATE users SET locale = 'de' WHERE id = :id", id=ids["user"])
            )
        with pytest.raises(IntegrityError):
            asyncio.run(
                _run(
                    target,
                    "UPDATE tenant_invitations SET locale = 'de' WHERE id = :id",
                    id=ids["invitation"],
                )
            )

        # A hard-deleted workspace (which the product never does) empties the
        # pointer instead of leaving it dangling or refusing the delete.
        asyncio.run(
            _run(
                target,
                "UPDATE users SET last_workspace_id = :t WHERE id = :id",
                t=ids["spare"],
                id=ids["user"],
            )
        )
        asyncio.run(_run(target, "DELETE FROM tenants WHERE id = :t", t=ids["spare"]))
        assert asyncio.run(
            _run(target, "SELECT last_workspace_id FROM users WHERE id = :id", id=ids["user"])
        ) == [(None,)]

        _alembic(target, "downgrade", "0140")
        assert asyncio.run(_state(target, ids)) == before
        columns = asyncio.run(
            _run(
                target,
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE (table_name = 'users' AND column_name IN ('locale', 'last_workspace_id')) "
                "OR (table_name = 'tenant_invitations' AND column_name = 'locale')",
            )
        )
        assert columns == []

        _alembic(target, "upgrade", "0141")
        assert asyncio.run(_state(target, ids)) == before
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))

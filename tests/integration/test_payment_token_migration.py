"""Prove the one-shot plaintext-to-encrypted migration against PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.exceptions import DependencyUnavailableError
from app.db.models.payment_method import PaymentMethod
from tests.payment_tokens import ENCRYPTION_KEY, FINGERPRINT_KEY, PROTECTOR

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_TOKEN = "synthetic-legacy-card-handle"


def _upgrade(url: str, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["wasla_database_url"] = url
    command.upgrade(config, revision)


def _downgrade(url: str, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["wasla_database_url"] = url
    command.downgrade(config, revision)


async def _database_command(admin_url: str, statement: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


async def _seed_legacy_method(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            tenant_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, status) "
                    "VALUES (:id, 'Migration Probe', :slug, 'active')"
                ),
                {"id": tenant_id, "slug": f"migration-{uuid.uuid4().hex[:8]}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO payment_methods "
                    "(id, tenant_id, provider, provider_token, status, is_default) "
                    "VALUES (:id, :tenant_id, 'paymob', :token, 'active', true)"
                ),
                {"id": uuid.uuid4(), "tenant_id": tenant_id, "token": SYNTHETIC_TOKEN},
            )
    finally:
        await engine.dispose()


async def _assert_migration_state(url: str, *, protected: bool) -> None:
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session:
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == ("0069" if protected else "0068")
            if protected:
                method = (await session.execute(select(PaymentMethod))).scalar_one()
                assert method.provider_token != SYNTHETIC_TOKEN
                assert PROTECTOR.open(method) == SYNTHETIC_TOKEN
                assert len(method.token_fingerprint) == 64
            else:
                count = await session.scalar(
                    text("SELECT count(*) FROM payment_methods WHERE provider_token LIKE 'v1.%'")
                )
                assert count == 0
    finally:
        await engine.dispose()


def test_existing_card_is_preserved_and_downgrade_cannot_expose_it(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = f"wasla_token_mig_{uuid.uuid4().hex[:12]}"
    assert name.startswith("wasla_token_mig_")
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_database_command(admin, f"CREATE DATABASE {name}"))
    try:
        _upgrade(target, "0068")
        asyncio.run(_seed_legacy_method(target))

        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEYS", raising=False)
        monkeypatch.delenv("PAYMENT_TOKEN_FINGERPRINT_KEY", raising=False)
        with pytest.raises(DependencyUnavailableError):
            _upgrade(target, "0069")
        asyncio.run(_assert_migration_state(target, protected=False))

        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", ENCRYPTION_KEY)
        monkeypatch.setenv("PAYMENT_TOKEN_FINGERPRINT_KEY", FINGERPRINT_KEY)
        _upgrade(target, "0069")
        asyncio.run(_assert_migration_state(target, protected=True))
        with pytest.raises(RuntimeError, match="Refusing to downgrade protected payment tokens"):
            _downgrade(target, "0068")
        asyncio.run(_assert_migration_state(target, protected=True))
    finally:
        asyncio.run(_database_command(admin, f"DROP DATABASE {name} WITH (FORCE)"))

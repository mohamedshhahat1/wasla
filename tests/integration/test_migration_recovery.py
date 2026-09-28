"""Billing downgrades refuse to drop commercial records; a half-applied migration recovers (DB-019).

Two hazards the database audit found in the migrations that carry money.

**Downgrades that dropped records.** 0071's downgrade dropped incidents,
adjustments and plan history with no guard; 0072's guarded top-up invoices but
not platform grants, products or custom plans; 0073's guarded offer invoices
but not the offers themselves. Each now counts what it would drop and refuses,
changing nothing.

**Enum additions outside the transaction.** A migration that ends with an
`autocommit_block()` has committed its DDL before the block runs and is stamped
only after it, so a failure inside the block leaves the DDL in place and the
version unstamped; rerunning then fails on "already exists". The recovery is
docs/RUNBOOK.md "A migration stopped half-way": confirm the objects exist,
`alembic stamp` the revision, and continue. This proves that state is reachable
and that path gets out of it.

Everything happens in a database of its own, walked up and down the revision
history; the suite's schema is not touched.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from scripts.db_preflight import verification_problems
from tests.integration.conftest import REPOSITORY_ROOT

pytestmark = pytest.mark.integration


def _run(url: str, *statements: str, autocommit: bool = False) -> list[object]:
    async def go() -> list[object]:
        options = {"isolation_level": "AUTOCOMMIT"} if autocommit else {}
        engine = create_async_engine(url, poolclass=NullPool, **options)
        results: list[object] = []
        try:
            async with engine.begin() as connection:
                for statement in statements:
                    result = await connection.execute(text(statement))
                    results.append(result.scalar() if result.returns_rows else None)
        finally:
            await engine.dispose()
        return results

    return asyncio.run(go())


def _config(url: str) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["wasla_database_url"] = url
    return config


@pytest.fixture
def scratch(prepared_database: str) -> Iterator[str]:
    base = make_url(prepared_database)
    name = f"{base.database}_recovery_{uuid.uuid4().hex[:8]}"
    _run(prepared_database, f'CREATE DATABASE "{name}" TEMPLATE template0', autocommit=True)
    url = base.set(database=name).render_as_string(hide_password=False)
    _run(url, "CREATE EXTENSION vector", "CREATE EXTENSION pgcrypto")
    try:
        yield url
    finally:
        _run(prepared_database, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', autocommit=True)


def _version(url: str) -> object:
    return _run(url, "SELECT version_num FROM alembic_version")[0]


def _refused(url: str, revision: str, config: Config, *expected: str) -> None:
    before = _version(url)
    with pytest.raises(RuntimeError) as refusal:
        command.downgrade(config, revision)
    message = str(refusal.value)
    assert "Refusing to downgrade" in message and "Nothing has been changed" in message
    for fragment in expected:
        assert fragment in message
    assert _version(url) == before


def test_a_half_applied_migration_recovers_and_downgrades_keep_the_ledger(scratch: str) -> None:
    config = _config(scratch)
    command.upgrade(config, "0073")

    # --- 0073 committed, enum labels added, version never stamped.
    _run(scratch, "UPDATE alembic_version SET version_num = '0072'")
    with pytest.raises(ProgrammingError, match="already exists"):
        command.upgrade(config, "0073")
    labels = _run(
        scratch,
        "SELECT count(*) FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid"
        " WHERE t.typname = 'audit_action' AND e.enumlabel = 'billing_custom_plan_offered'",
    )[0]
    assert labels, "the enum labels were committed before the stamp"
    # The runbook's recovery: the objects are there, so record that they are.
    assert _run(scratch, "SELECT to_regclass('custom_plan_offers') IS NOT NULL")[0] is True
    command.stamp(config, "0073")
    assert _version(scratch) == "0073"

    _run(
        scratch,
        "INSERT INTO tenants (id, name, slug, status)"
        " VALUES (gen_random_uuid(), 'Recovery', 'recovery', 'active')",
        "INSERT INTO plans (id, code, name, price, currency, interval, trial_days, limits,"
        " is_public, is_active, sort_order, scope, tenant_id) SELECT gen_random_uuid(),"
        " 'custom-recovery', 'Custom', 500, 'EGP', 'monthly', 0, '{}', false, true, 0,"
        " 'tenant', id FROM tenants WHERE slug = 'recovery'",
        "INSERT INTO plan_versions (id, plan_id, version, name, price, currency, interval,"
        " trial_days, limits, effective_at, created_at) SELECT gen_random_uuid(), id, 1,"
        " 'Custom', 500, 'EGP', 'monthly', 0, '{}', now(), now() FROM plans"
        " WHERE code = 'custom-recovery'",
        "INSERT INTO custom_plan_offers (id, tenant_id, plan_id, plan_version_id, status,"
        " reason, declined_at) SELECT gen_random_uuid(), p.tenant_id, p.id, v.id,"
        " 'declined', 'negotiated', now() FROM plans p JOIN plan_versions v ON v.plan_id = p.id"
        " WHERE p.code = 'custom-recovery'",
    )

    # --- 0073 keeps a declined offer: a record of what was proposed.
    _refused(scratch, "0072", config, "custom plan offers: 1")
    _run(scratch, "DELETE FROM custom_plan_offers")
    command.downgrade(config, "0072")

    # --- 0072 keeps custom plans, products and grants.
    _refused(scratch, "0071", config, "custom plans: 1")
    _run(
        scratch,
        "DELETE FROM plan_versions WHERE plan_id IN"
        " (SELECT id FROM plans WHERE code = 'custom-recovery')",
        "DELETE FROM plans WHERE code = 'custom-recovery'",
        "INSERT INTO topup_products (id, code, name, entitlement_key, quantity, price,"
        " currency, scope, is_active, is_public, validity_policy) VALUES"
        " (gen_random_uuid(), 'messages-1000', 'Messages', 'period_messages', 1000, 100,"
        " 'EGP', 'global', true, true, 'current_period_end')",
    )
    _refused(scratch, "0071", config, "top-up products: 1")
    _run(scratch, "DELETE FROM topup_products")
    command.downgrade(config, "0071")

    # --- 0071 keeps incidents: money's evidence.
    _run(
        scratch,
        "INSERT INTO billing_incidents (id, kind, status, dedupe_key) VALUES"
        " (gen_random_uuid(), 'unknown_callback', 'open', 'recovery-test')",
    )
    _refused(scratch, "0070", config, "billing incidents: 1")
    _run(scratch, "DELETE FROM billing_incidents")
    command.downgrade(config, "0070")

    # --- And back to head, with every rule enforced.
    command.upgrade(config, "head")

    async def problems() -> list[object]:
        engine = create_async_engine(scratch, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                return list(await verification_problems(connection))
        finally:
            await engine.dispose()

    assert asyncio.run(problems()) == []

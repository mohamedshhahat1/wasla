"""The runtime role cannot rewrite the evidence it writes (DB-006).

ADR-033 promises an append-only audit trail with "no update or delete path
anywhere", and ADR-112 relies on billing incidents as the durable record of
every refused duplicate payment. The database audit found both rewritable by
the application's own role: `DELETE FROM audit_logs` as `wasla_runtime`
answered `DELETE 1`, and an incident could be deleted or have its amount
changed. A compromised API or worker - or a buggy path - could erase who
deleted a workspace, or that a customer was charged twice.

Now, for the role `provision_runtime_db_role.py` creates:

- `audit_logs`: `SELECT` and `INSERT` only.
- `billing_incidents`: `SELECT`, `INSERT`, and `UPDATE` of the resolution
  columns only - status, resolved_at, resolved_by, resolution_note,
  updated_at. No `DELETE`.

And for every role, the owner included, a trigger (migration 0076) keeps an
incident's evidence - kind, workspace, invoice, payment, provider identifiers,
amount, detail, when it was raised - as it was raised, and keeps a resolved
incident resolved. The migration identity keeps emergency authority over audit
rows (and needs it: test and repair teardown delete them), which is the
separation `provision_runtime_db_role.py` already draws.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.errors import sqlstate
from scripts.provision_runtime_db_role import provision

pytestmark = pytest.mark.integration

INSUFFICIENT_PRIVILEGE = "42501"
INTEGRITY = "23000"


@dataclass
class Roles:
    owner: AsyncEngine
    runtime: AsyncEngine
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    audit_id: uuid.UUID
    incident_id: uuid.UUID


@pytest_asyncio.fixture
async def roles(prepared_database: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Roles]:
    """A provisioned runtime role, and one committed audit row and incident."""
    suffix = uuid.uuid4().hex[:12]
    role_name = f"wasla_runtime_ledger_{suffix}"
    migration_url = make_url(prepared_database)
    runtime_url = migration_url.set(username=role_name, password=f"test-{suffix}")
    owner = create_async_engine(migration_url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", migration_url.render_as_string(False))
    monkeypatch.setenv("DATABASE_URL", runtime_url.render_as_string(False))
    await provision()
    await provision()  # a repeat release must not widen anything back

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    audit_id, incident_id = uuid.uuid4(), uuid.uuid4()
    async with owner.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO tenants (id, name, slug, status) VALUES (:id, 'Ledger roles',"
                " :slug, 'active')"
            ),
            {"id": tenant_id, "slug": f"ledger-roles-{suffix}"},
        )
        await connection.execute(
            text(
                "INSERT INTO users (id, email, hashed_password, is_active, full_name) VALUES"
                " (:id, :email, 'x', true, 'Operator')"
            ),
            {"id": user_id, "email": f"ledger-roles-{suffix}@example.com"},
        )
        await connection.execute(
            text(
                "INSERT INTO audit_logs (id, tenant_id, action, actor_kind, target_type,"
                " occurred_at) VALUES (:id, :tenant, 'workspace_purged', 'system', 'tenant',"
                " now())"
            ),
            {"id": audit_id, "tenant": tenant_id},
        )
        await connection.execute(
            text(
                "INSERT INTO billing_incidents (id, tenant_id, kind, status, dedupe_key, amount,"
                " currency, detail, created_at, updated_at) VALUES (:id, :tenant,"
                " 'duplicate_payment', 'open', :key, 99.00, 'EGP', 'A second payment.', now(),"
                " now())"
            ),
            {"id": incident_id, "tenant": tenant_id, "key": f"duplicate_payment:{suffix}"},
        )
    runtime = create_async_engine(runtime_url)
    try:
        yield Roles(owner, runtime, tenant_id, user_id, audit_id, incident_id)
    finally:
        await runtime.dispose()
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM billing_incidents WHERE tenant_id = :t"), {"t": tenant_id}
            )
            await connection.execute(
                text("DELETE FROM audit_logs WHERE tenant_id = :t"), {"t": tenant_id}
            )
            await connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
            await connection.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
            await connection.exec_driver_sql(f"DROP OWNED BY {role_name}")
            await connection.exec_driver_sql(f"DROP ROLE {role_name}")
        await owner.dispose()


async def _refused(engine: AsyncEngine, statement: str, params: Mapping[str, object]) -> str:
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await connection.execute(text(statement), params)
    return sqlstate(refused.value) or ""


async def test_the_runtime_role_appends_to_the_audit_trail_and_nothing_else(
    roles: Roles,
) -> None:
    async with roles.runtime.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO audit_logs (id, tenant_id, action, actor_kind, target_type,"
                " occurred_at) VALUES (:id, :tenant, 'workspace_purged', 'system', 'tenant',"
                " now())"
            ),
            {"id": uuid.uuid4(), "tenant": roles.tenant_id},
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM audit_logs WHERE tenant_id = :t"),
                {"t": roles.tenant_id},
            )
            == 2
        )
    params = {"id": roles.audit_id}
    for statement in (
        "UPDATE audit_logs SET action = 'user_deleted' WHERE id = :id",
        "UPDATE audit_logs SET metadata = '{}'::jsonb WHERE id = :id",
        "DELETE FROM audit_logs WHERE id = :id",
        "TRUNCATE audit_logs",
    ):
        assert await _refused(roles.runtime, statement, params) == INSUFFICIENT_PRIVILEGE


async def test_the_runtime_role_can_resolve_an_incident_and_nothing_more(roles: Roles) -> None:
    async with roles.runtime.begin() as connection:
        await connection.execute(
            text(
                "UPDATE billing_incidents SET status = 'resolved', resolved_at = now(),"
                " resolved_by = :user, resolution_note = 'Refunded.', updated_at = now()"
                " WHERE id = :id"
            ),
            {"id": roles.incident_id, "user": roles.user_id},
        )
    params = {"id": roles.incident_id}
    for statement in (
        "UPDATE billing_incidents SET amount = 1.00 WHERE id = :id",
        "UPDATE billing_incidents SET kind = 'refund_requested' WHERE id = :id",
        "UPDATE billing_incidents SET detail = 'nothing happened' WHERE id = :id",
        "UPDATE billing_incidents SET tenant_id = NULL WHERE id = :id",
        "DELETE FROM billing_incidents WHERE id = :id",
    ):
        assert await _refused(roles.runtime, statement, params) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "change",
    [
        "amount = 1.00",
        "kind = 'refund_requested'",
        "detail = 'nothing happened'",
        "dedupe_key = 'rewritten'",
        "provider_transaction_id = 'another'",
        "created_at = now() - interval '1 day'",
    ],
)
async def test_no_role_rewrites_an_incidents_evidence(roles: Roles, change: str) -> None:
    """Not even the owner: the evidence is the incident."""
    assert (
        await _refused(
            roles.owner,
            f"UPDATE billing_incidents SET {change} WHERE id = :id",  # noqa: S608
            {"id": roles.incident_id},
        )
        == INTEGRITY
    )


async def test_a_resolved_incident_stays_resolved(roles: Roles) -> None:
    async with roles.owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE billing_incidents SET status = 'resolved', resolved_at = now()"
                " WHERE id = :id"
            ),
            {"id": roles.incident_id},
        )
    assert (
        await _refused(
            roles.owner,
            "UPDATE billing_incidents SET status = 'open', resolved_at = NULL WHERE id = :id",
            {"id": roles.incident_id},
        )
        == INTEGRITY
    )

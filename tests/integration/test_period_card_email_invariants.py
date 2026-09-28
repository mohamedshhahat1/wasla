"""Periods run forwards, one default card, one account per address.

DB-012: the audit wrote a subscription whose period ended before it began, and
an invoice likewise, and both were accepted. DB-018: a second active default
card was accepted, and two first cards saved at once could both become the
default - the renewal would then charge whichever `LIMIT 1` happened to return.
DB-025: `Ada@x` beside `ada@x` was accepted by the database, which relied on
the application lower-casing every write.

Direct SQL proves the refusals (23514 for a CHECK, 23505 for a unique index).
The races run on real connections, really committing, at a barrier.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.errors import sqlstate
from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
from app.db.models.tenant import Tenant
from app.integrations.billing.checkout import SavedPaymentMethod
from app.services.payment_method_service import PaymentMethodService, remember_saved_method
from tests.payment_tokens import SETTINGS, saved_card

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 27, 10, tzinfo=UTC)
CHECK = "23514"
UNIQUE = "23505"
RUNS = 10


async def _refused(session: AsyncSession, statement: str, params: dict[str, Any]) -> str:
    with pytest.raises(DBAPIError) as refused:
        async with session.begin_nested():
            await session.execute(text(statement), params)
    return sqlstate(refused.value) or ""


async def _tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name="Invariants", slug=f"invariants-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    return tenant


# ------------------------------------------------------------------ DB-012


async def test_a_subscription_period_cannot_run_backwards(db_session: AsyncSession) -> None:
    """Y12."""
    plan = await db_session.scalar(text("SELECT id FROM plans LIMIT 1"))
    if plan is None:
        plan = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO plans (id, code, name, price, currency, interval, trial_days,"
                " limits, is_public, is_active, sort_order, scope, revision) VALUES (:id, :code,"
                " 'P', 0, 'EGP', 'monthly', 0, '{}', true, true, 0, 'public', 1)"
            ),
            {"id": plan, "code": f"inv-{uuid.uuid4().hex[:8]}"},
        )
    statement = (
        "INSERT INTO subscriptions (id, tenant_id, plan_id, status, current_period_start,"
        " current_period_end, usage_period_start, usage_period_end, cancel_at_period_end,"
        " ended_at, revision) VALUES (:id, :tenant, :plan, :status, :start, :end, :start, :end,"
        " false, :ended, 1)"
    )

    async def row(end: datetime, *, ended: datetime | None = None) -> dict[str, Any]:
        # One subscription per workspace, so each row gets its own.
        return {
            "id": uuid.uuid4(),
            "tenant": (await _tenant(db_session)).id,
            "plan": plan,
            "status": "active" if ended is None else "cancelled",
            "start": NOW,
            "end": end,
            "ended": ended,
        }

    # A live period runs forwards: neither reversed nor of no length.
    assert await _refused(db_session, statement, await row(NOW - timedelta(days=1))) == CHECK
    assert await _refused(db_session, statement, await row(NOW)) == CHECK
    # An ended subscription's period end is the moment service stopped. An
    # immediate cancellation at the instant its period began, or a void of a
    # period already paid in advance, legitimately end at or before the start.
    async with db_session.begin_nested():
        await db_session.execute(text(statement), await row(NOW, ended=NOW))
    async with db_session.begin_nested():
        await db_session.execute(
            text(statement), await row(NOW - timedelta(days=2), ended=NOW - timedelta(days=2))
        )


async def test_an_invoice_period_cannot_run_backwards(db_session: AsyncSession) -> None:
    """Y13, and the degenerate zero-length period that stays legal."""
    tenant = await _tenant(db_session)
    statement = (
        "INSERT INTO invoices (id, tenant_id, status, purpose, plan_code, amount_due,"
        " amount_paid, currency, period_start, period_end, lines, collection_attempts,"
        " revision) VALUES (:id, :tenant, 'open', 'checkout', 'pro', 0, 0, 'EGP', :start,"
        " :end, '[]', 0, 1)"
    )
    reversed_period = {
        "id": uuid.uuid4(),
        "tenant": tenant.id,
        "start": NOW,
        "end": NOW - timedelta(days=1),
    }
    assert await _refused(db_session, statement, reversed_period) == CHECK
    await db_session.execute(
        text(statement), {"id": uuid.uuid4(), "tenant": tenant.id, "start": NOW, "end": NOW}
    )


# ------------------------------------------------------------------ DB-025


@pytest.mark.parametrize("tombstoned", [False, True])
async def test_an_address_belongs_to_one_account_whatever_its_case(
    db_session: AsyncSession, tombstoned: bool
) -> None:
    """Y44 - and a closed account's address stays reserved (DB-021)."""
    local = f"ada-{uuid.uuid4().hex[:8]}"
    await db_session.execute(
        text(
            "INSERT INTO users (id, email, hashed_password, is_active, deleted_at, token_version)"
            " VALUES (:id, :email, 'x', true, :deleted, 0)"
        ),
        {
            "id": uuid.uuid4(),
            "email": f"{local}@example.com",
            "deleted": NOW if tombstoned else None,
        },
    )
    state = await _refused(
        db_session,
        "INSERT INTO users (id, email, hashed_password, is_active, token_version)"
        " VALUES (:id, :email, 'x', true, 0)",
        {"id": uuid.uuid4(), "email": f"{local.upper()}@Example.COM"},
    )
    assert state == UNIQUE


# ------------------------------------------------------------------ DB-018


async def test_a_workspace_has_one_active_default_card(db_session: AsyncSession) -> None:
    """Y28; a revoked default makes room for another."""
    tenant = await _tenant(db_session)
    first = saved_card(tenant_id=tenant.id, token="tok_first", is_default=True)
    db_session.add(first)
    await db_session.flush()
    second = saved_card(tenant_id=tenant.id, token="tok_second")
    db_session.add(second)
    await db_session.flush()
    assert (
        await _refused(
            db_session,
            "UPDATE payment_methods SET is_default = true WHERE id = :id",
            {"id": second.id},
        )
        == UNIQUE
    )
    # Revoking the default and choosing another never collide.
    await db_session.execute(
        text("UPDATE payment_methods SET status = 'revoked', revoked_at = now() WHERE id = :id"),
        {"id": first.id},
    )
    await db_session.execute(
        text("UPDATE payment_methods SET is_default = true WHERE id = :id"), {"id": second.id}
    )


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(prepared_database, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _workspace(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with maker() as session:
        tenant = Tenant(name="Cards", slug=f"cards-{uuid.uuid4().hex[:10]}")
        session.add(tenant)
        await session.commit()
        return tenant.id


async def _erase(maker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    async with maker() as session:
        await session.execute(delete(PaymentMethod).where(PaymentMethod.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


async def _defaults(maker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> int:
    async with maker() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(PaymentMethod)
                .where(PaymentMethod.tenant_id == tenant_id)
                .where(PaymentMethod.is_default.is_(True))
                .where(PaymentMethod.status == PaymentMethodStatus.ACTIVE)
            )
            or 0
        )


async def test_first_cards_saved_at_once_make_exactly_one_default(
    committing: async_sessionmaker[AsyncSession],
) -> None:
    """Ten concurrent first saves, `RUNS` times: every card kept, one default."""
    for _ in range(RUNS):
        tenant_id = await _workspace(committing)
        barrier = asyncio.Barrier(10)

        async def save(index: int, tenant: uuid.UUID = tenant_id, gate: Any = barrier) -> bool:
            async with committing() as session:
                await gate.wait()
                _, created = await remember_saved_method(
                    session,
                    settings=SETTINGS,
                    tenant_id=tenant,
                    provider="paymob",
                    saved=SavedPaymentMethod(
                        token=f"tok_{tenant.hex[:8]}_{index}",
                        provider_token_id=f"ptk_{index}",
                        masked_pan="xxxx-2346",
                        brand="MasterCard",
                    ),
                )
                await session.commit()
                return created

        try:
            created = await asyncio.gather(*(save(i) for i in range(10)))
            assert all(created)
            assert await _defaults(committing, tenant_id) == 1
        finally:
            await _erase(committing, tenant_id)


async def test_choosing_the_default_at_once_leaves_exactly_one(
    committing: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(RUNS):
        tenant_id = await _workspace(committing)
        async with committing() as session:
            cards = [
                saved_card(
                    tenant_id=tenant_id, token=f"tok_{tenant_id.hex[:6]}_{i}", is_default=i == 0
                )
                for i in range(3)
            ]
            session.add_all(cards)
            await session.commit()
            ids = [card.id for card in cards]
        barrier = asyncio.Barrier(6)

        async def choose(
            card: uuid.UUID, tenant: uuid.UUID = tenant_id, gate: Any = barrier
        ) -> None:
            async with committing() as session:
                await gate.wait()
                await PaymentMethodService(session, tenant_id=tenant).make_default(card)
                await session.commit()

        try:
            await asyncio.gather(*(choose(ids[i % 3]) for i in range(6)))
            assert await _defaults(committing, tenant_id) == 1
            async with committing() as session:
                chosen = await PaymentMethodService(session, tenant_id=tenant_id).default_method()
                again = await PaymentMethodService(session, tenant_id=tenant_id).default_method()
            assert chosen is not None and again is not None and chosen.id == again.id
        finally:
            await _erase(committing, tenant_id)

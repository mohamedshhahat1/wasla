# ruff: noqa: F811 - the concurrency harness fixtures are imported by name.
"""Withdrawing a grant under real concurrency (PLAT-G1, ADR-132).

Every racer runs on its own committed connection. A barrier lines up the
starts; a gate lines up the decisions, so each order is exercised on purpose
rather than by luck:

- **the connection decides first**: it holds the workspace's capacity lock,
  having counted the grant, until the withdrawal is provably blocked on that
  same lock; it commits; the withdrawal then judges the connections that
  exist and opens a reduction with its grace;
- **the withdrawal decides first**: having taken the slot away and judged the
  workspace under the lock, it waits until the connection is provably blocked
  on the lock; the connection then counts without the grant and is refused.

Each activation records, inside its own transaction and under the lock, the
active connections and the capacity in force the moment before it commits;
none may exceed it. Whatever the order, the workspace ends either fitting its
capacity or with an open reduction explaining why not (E01), and the
withdrawal itself disables nothing. Two staff withdrawing the same grant at
once withdraw it once, audited once.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import ConflictError
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import LimitKey
from app.db.models.channel import Channel
from app.db.models.channel_capacity import (
    CapacityReductionCause,
    CapacityReductionStatus,
    ChannelCapacityReduction,
)
from app.db.models.enums import PlatformRole
from app.db.models.topup import TopupPurchase, TopupStatus
from app.db.models.user import User
from app.platform.topup_admin import TopupAdmin
from app.schemas.topup import TopupGrantWithdraw
from app.services.capacity_reduction import ChannelCapacityReductions
from app.services.channel_capacity import ChannelCapacityExceededError, ChannelCapacityGuard
from app.services.channel_connection_service import ChannelConnectionService
from app.services.entitlement_service import EntitlementService
from tests.integration.test_channel_capacity_concurrency import (  # noqa: F401 - fixtures
    World,
    _active,
    _advisory_waiters,
    _final,
    _grant,
    _poll,
    _registry,
    _seed,
    _together,
    _world,
    maker,
    worlds,
)

pytestmark = pytest.mark.integration


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        jwt_secret="grant-withdrawal-race-secret-not-for-deployment",
        # Every world here holds a subscription; the default plan is never read.
        default_plan_code="starter",
        channel_capacity_grace_days=7,
    )


async def _staff(maker: async_sessionmaker[AsyncSession]) -> User:
    async with maker() as session:
        staff = User(
            email=f"race-staff-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            is_active=True,
            email_verified_at=datetime.now(UTC),
            platform_role=PlatformRole.PLATFORM_ADMIN,
        )
        session.add(staff)
        await session.commit()
        return staff


async def _forget(maker: async_sessionmaker[AsyncSession], staff: User) -> None:
    async with maker() as session:
        await session.execute(delete(AuditLog).where(AuditLog.actor_id == staff.id))
        await session.execute(delete(User).where(User.id == staff.id))
        await session.commit()


async def _granted(maker: async_sessionmaker[AsyncSession], world: World) -> uuid.UUID:
    """One general slot granted until the period ends."""
    await _grant(maker, world, quantity=1, channel=None, expires=world.period_end)
    async with maker() as session:
        found = await session.scalar(
            select(TopupPurchase.id).where(TopupPurchase.tenant_id == world.tenant_id)
        )
        assert found is not None
        return found


def _withdraw(
    maker: async_sessionmaker[AsyncSession], world: World, grant: uuid.UUID, staff: User
) -> Callable[[], Awaitable[str]]:
    async def run() -> str:
        async with maker() as session:
            revision = await session.scalar(
                select(TopupPurchase.revision).where(TopupPurchase.id == grant)
            )
            assert revision is not None
            try:
                await TopupAdmin(session, settings=_settings()).withdraw_grant(
                    grant,
                    TopupGrantWithdraw(
                        tenant_id=world.tenant_id,
                        reason="Granted in error.",
                        expected_revision=revision,
                    ),
                    actor=staff,
                )
            except ConflictError:
                await session.rollback()
                return "withdrawal:conflict"
            await session.commit()
            return "withdrawal:withdrawn"

    return run


def _connect(maker: async_sessionmaker[AsyncSession], world: World) -> Callable[[], Awaitable[str]]:
    """Connect an Instagram account; record active and the capacity in force at its commit."""

    async def run() -> str:
        async with maker() as session:
            try:
                await ChannelConnectionService(
                    session, tenant_id=world.tenant_id, default_plan_code=None, registry=_registry()
                ).connect(channel=Channel.INSTAGRAM, external_account_id=f"race-{uuid.uuid4().hex}")
            except ChannelCapacityExceededError:
                await session.rollback()
                return "connect:refused"
            capacity = await EntitlementService(
                session, tenant_id=world.tenant_id, default_plan_code=None
            ).check(LimitKey.CHANNEL_CONNECTIONS, additional=0)
            assert capacity.limit is not None
            world.commits.append(
                ("instagram", await _active(session, world.tenant_id), capacity.limit)
            )
            await session.commit()
            return "connect:created"

    return run


async def _settled(maker: async_sessionmaker[AsyncSession], world: World) -> dict[str, Any]:
    async with maker() as session:
        limit = (
            await EntitlementService(
                session, tenant_id=world.tenant_id, default_plan_code=None
            ).check(LimitKey.CHANNEL_CONNECTIONS, additional=0)
        ).limit
        open_ = await session.scalar(
            select(ChannelCapacityReduction)
            .where(ChannelCapacityReduction.tenant_id == world.tenant_id)
            .where(ChannelCapacityReduction.status == CapacityReductionStatus.PENDING_SELECTION)
        )
        withdrawals = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AuditAction.BILLING_TOPUP_GRANT_WITHDRAWN)
            .where(AuditLog.tenant_id == world.tenant_id)
        )
        disabled = await session.scalar(
            select(func.count())
            .select_from(ChannelCapacityReduction)
            .where(ChannelCapacityReduction.tenant_id == world.tenant_id)
            .where(ChannelCapacityReduction.disabled_connection_ids.is_not(None))
        )
    return {
        "active": await _final(maker, world),
        "limit": limit,
        "open": open_,
        "withdrawals": withdrawals,
        "disabled_by_a_reduction": disabled,
    }


@pytest.mark.parametrize("first", ["connect", "withdrawal"])
async def test_a_withdrawal_racing_a_connect_never_leaves_more_active_than_the_capacity_in_force(
    maker: async_sessionmaker[AsyncSession],
    worlds: list[World],
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    """One slot plus a granted one, one connection: a second connect races the withdrawal."""
    world = await _world(maker, slots=1)
    worlds.append(world)
    grant = await _granted(maker, world)
    await _seed(maker, world, 1)
    staff = await _staff(maker)
    decided: list[str] = []

    original_reserve = ChannelCapacityGuard.reserve_or_refuse
    original_boundary = ChannelCapacityReductions.boundary
    original_withdraw = TopupAdmin.withdraw_grant

    async def lock_waiter() -> bool:
        return await _advisory_waiters(maker) >= 1

    async def withdraw(self: TopupAdmin, *args: Any, **kwargs: Any) -> Any:
        if first == "connect":
            # Not before the connection has decided, holding the lock.
            async def connect_decided() -> bool:
                return "connect" in decided

            await _poll(connect_decided, what="the connection to decide first")
        return await original_withdraw(self, *args, **kwargs)

    async def reserve(self: ChannelCapacityGuard, channel: Channel) -> Any:
        if first == "withdrawal":
            # Not before the withdrawal has decided, holding the lock.
            async def withdrawal_decided() -> bool:
                return "withdrawal" in decided

            await _poll(withdrawal_decided, what="the withdrawal to decide first")
        try:
            slot = await original_reserve(self, channel)
        except ChannelCapacityExceededError:
            decided.append("connect")
            raise
        decided.append("connect")
        if first == "connect":
            # Holding the lock, having counted the grant, until the
            # withdrawal is provably blocked on the same lock.
            await _poll(lock_waiter, what="the withdrawal to block on the capacity lock")
        return slot

    async def boundary(self: ChannelCapacityReductions, **kwargs: Any) -> Any:
        reduction = await original_boundary(self, **kwargs)
        decided.append("withdrawal")
        if first == "withdrawal":
            # Holding the lock, the slot gone, until the connection is
            # provably blocked on the same lock.
            await _poll(lock_waiter, what="the connection to block on the capacity lock")
        return reduction

    monkeypatch.setattr(TopupAdmin, "withdraw_grant", withdraw)
    monkeypatch.setattr(ChannelCapacityGuard, "reserve_or_refuse", reserve)
    monkeypatch.setattr(ChannelCapacityReductions, "boundary", boundary)
    try:
        outcomes = await _together(_withdraw(maker, world, grant, staff), _connect(maker, world))
        settled = await _settled(maker, world)
    finally:
        await _forget(maker, staff)

    assert sorted(decided) == ["connect", "withdrawal"], "every racer reached its decision"
    assert decided[0] == first
    assert "withdrawal:withdrawn" in outcomes
    # At every commit, no more active than the capacity in force at that commit.
    assert all(active <= capacity for _, active, capacity in world.commits), world.commits
    assert settled["limit"] == 1
    assert settled["withdrawals"] == 1
    assert settled["disabled_by_a_reduction"] == 0, "the withdrawal disables nothing"
    if first == "connect":
        assert outcomes.count("connect:created") == 1
        assert settled["active"] == 2
        reduction = settled["open"]
        assert reduction is not None
        assert (reduction.cause, reduction.topup_purchase_id) == (
            CapacityReductionCause.GRANT_WITHDRAWN,
            grant,
        )
        assert reduction.grace_ends_at - reduction.effective_at == timedelta(days=7)
    else:
        assert outcomes.count("connect:refused") == 1
        assert settled["active"] == 1
        assert settled["open"] is None
    # E01 for this workspace: over its capacity only with a reduction open.
    assert settled["active"] <= settled["limit"] or settled["open"] is not None


async def test_two_staff_withdrawing_the_same_grant_withdraw_it_once(
    maker: async_sessionmaker[AsyncSession], worlds: list[World], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(maker, slots=1)
    worlds.append(world)
    grant = await _granted(maker, world)
    alice, bob = await _staff(maker), await _staff(maker)
    arrived: list[str] = []
    original = TopupAdmin.withdraw_grant

    async def arriving(self: TopupAdmin, *args: Any, **kwargs: Any) -> Any:
        # Both have read the same revision before either decides.
        arrived.append("staff")

        async def both_here() -> bool:
            return len(arrived) >= 2

        await _poll(both_here, what="both withdrawals to arrive")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(TopupAdmin, "withdraw_grant", arriving)
    try:
        outcomes = await _together(
            _withdraw(maker, world, grant, alice), _withdraw(maker, world, grant, bob)
        )
        settled = await _settled(maker, world)
        async with maker() as session:
            purchase = await session.get(TopupPurchase, grant)
            assert purchase is not None
            status, revision = purchase.status, purchase.revision
    finally:
        await _forget(maker, alice)
        await _forget(maker, bob)

    assert sorted(outcomes) == ["withdrawal:conflict", "withdrawal:withdrawn"]
    assert len(arrived) == 2
    assert status is TopupStatus.WITHDRAWN
    assert revision == 2, "changed once"
    assert settled["withdrawals"] == 1

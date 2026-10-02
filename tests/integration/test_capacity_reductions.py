"""The capacity-reduction lifecycle: boundary, grace, owner choice, automatic fallback.

ENT-14, ENT-15, ENT-16 (ADR-131). The boundary is crossed by the real billing
worker (`BillingWorker.run_once`, run in the test's own transaction), never by
calling the service directly: a scheduled downgrade is applied by its roll-over,
a channel top-up by its expiry sweep. Owners choose through the real service
and through the real ASGI route. Connections are made through the real capacity
guard and `ChannelConnectionService`, under a registry that operates every
synthetic channel.

The shape of most tests, from the spec: Business (10 connections, five channel
types) holding seven connections - oldest first: WhatsApp, Telegram, Instagram,
WhatsApp, Messenger, TikTok, WhatsApp - schedules Pro (3 connections, three
types).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.lifecycle import refusal_now
from app.api.dependencies import ActiveWorkspace, get_active_workspace
from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models.agent import Agent, AgentStatus
from app.db.models.agent_turn import TurnOutcome
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import LimitKey, Plan, ScheduledChangeSource, SubscriptionStatus
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ConnectionDisabledReason,
    ConnectionStatus,
)
from app.db.models.channel_capacity import (
    CapacityReductionCause,
    CapacityReductionStatus,
    ChannelCapacityReduction,
)
from app.db.models.conversation import Contact, Conversation
from app.db.models.email import OutboundEmail
from app.db.models.enums import MembershipStatus, PlatformRole, TenantRole
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.invoice import Payment
from app.db.models.lead import ActorKind
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupStatus
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.main import create_app
from app.platform.topup_admin import TopupAdmin
from app.repositories.whatsapp_repository import WhatsAppAccountRepository
from app.schemas.topup import TopupRefundReview
from app.services.capacity_reduction import CapacitySelectionError, ChannelCapacityReductions
from app.services.channel_capacity import (
    ChannelCapacityExceededError,
    ChannelCapacityGuard,
    ChannelTypeNotAllowedError,
)
from app.services.channel_connection_service import ChannelConnectionService
from app.services.entitlement_service import EntitlementService
from app.services.plan_catalog import PlanCatalog
from app.services.subscription_service import SubscriptionService
from app.services.whatsapp_account_service import WhatsAppAccountService
from app.workers.billing_worker import BillingWorker
from tests.billing_fixtures import add_owner
from tests.channel_fakes import SyntheticAdapter
from tests.fakes import as_database
from tests.integration.plan_catalogue import own_plan
from tests.integration.topup_harness import (
    Paymob,
    apply,
    base_now,
    buy,
    callback,
    product,
)
from tests.integration.topup_harness import workspace as topup_workspace
from tests.media_harness import SessionHandle

pytestmark = pytest.mark.integration

FIVE = ["whatsapp", "instagram", "messenger", "telegram", "tiktok"]
THREE = ["whatsapp", "instagram", "messenger"]
WA, IG, MS, TG, TT = (
    Channel.WHATSAPP,
    Channel.INSTAGRAM,
    Channel.MESSENGER,
    Channel.TELEGRAM,
    Channel.TIKTOK,
)
# Oldest first: the order `ownership_started_at` gives them.
ORDER = (WA, TG, IG, WA, MS, TT, WA)
GRACE = timedelta(days=7)
PENDING = CapacityReductionStatus.PENDING_SELECTION


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        jwt_secret="capacity-reduction-secret-not-for-deployment",
        rate_limit_enabled=False,
        default_plan_code="starter",
        channel_capacity_grace_days=7,
    )


def _worker(session: AsyncSession, settings: Settings | None = None) -> BillingWorker:
    """The real billing worker, in this test's transaction."""
    return BillingWorker(
        database=as_database(SessionHandle(session)), settings=settings or _settings()
    )


def _mailing() -> Settings:
    """`_settings`, with the email outbox on - for the owners' notices."""
    return _settings().model_copy(
        update={
            "email_enabled": True,
            "email_provider": "fake",
            "email_from": "no-reply@example.com",
            "app_public_url": "https://app.example.com",
        }
    )


def _registry() -> ChannelRegistry:
    adapters: dict[Channel, ChannelAdapter] = {WA: WhatsAppAdapter()}
    for channel in (IG, MS, TG, TT):
        adapters[channel] = cast(ChannelAdapter, SyntheticAdapter(channel))
    return ChannelRegistry(adapters)


def _neutral(session: AsyncSession, tenant: Tenant) -> ChannelConnectionService:
    return ChannelConnectionService(
        session, tenant_id=tenant.id, default_plan_code="starter", registry=_registry()
    )


def _reductions(session: AsyncSession, tenant: Tenant) -> ChannelCapacityReductions:
    return ChannelCapacityReductions(
        session, tenant_id=tenant.id, default_plan_code="starter", grace=GRACE
    )


async def _plans(session: AsyncSession) -> tuple[Plan, Plan]:
    """Business (10, five types) and Pro (3, three types) - placeholders of this suite's own."""
    await own_plan(
        session,
        code="starter",
        price=Decimal("0.00"),
        limits={"channel_connections": 1},
        allowed_channel_types=["whatsapp"],
    )
    business = await own_plan(
        session,
        code="business",
        price=Decimal("0.00"),
        limits={"channel_connections": 10},
        allowed_channel_types=FIVE,
    )
    pro = await own_plan(
        session,
        code="pro",
        price=Decimal("0.00"),
        limits={"channel_connections": 3},
        allowed_channel_types=THREE,
    )
    return business, pro


async def _workspace(
    session: AsyncSession, plan: Plan, *, name: str = "Reduction Co"
) -> tuple[Tenant, User]:
    tenant = Tenant(name=name, slug=f"reduce-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    owner = await add_owner(session, tenant)
    await SubscriptionService(session, tenant_id=tenant.id).start(
        plan_code=plan.code, now=datetime.now(UTC) - timedelta(days=1), self_service=False
    )
    return tenant, owner


async def _number(session: AsyncSession, tenant: Tenant) -> uuid.UUID:
    slot = await ChannelCapacityGuard(
        session, tenant_id=tenant.id, default_plan_code="starter"
    ).reserve_or_refuse(WA)
    account = await WhatsAppAccountRepository(session, tenant_id=tenant.id).connect(
        phone_number_id=f"1{uuid.uuid4().int % 10**11:011d}",
        waba_id="waba-reduce",
        display_phone_number="+20 100 000 0007",
        slot=slot,
    )
    return account.id


async def _connect(
    session: AsyncSession, tenant: Tenant, owner: User, channel: Channel
) -> uuid.UUID:
    if channel is WA:
        return await _number(session, tenant)
    connection = await _neutral(session, tenant).connect(
        channel=channel, external_account_id=f"{channel.value}-{uuid.uuid4().hex[:8]}", actor=owner
    )
    return connection.id


async def _seven(session: AsyncSession, tenant: Tenant, owner: User) -> list[uuid.UUID]:
    """Seven connections in ORDER, oldest first, an hour apart."""
    ids = [await _connect(session, tenant, owner, channel) for channel in ORDER]
    start = datetime.now(UTC) - timedelta(days=30)
    for index, (connection_id, channel) in enumerate(zip(ids, ORDER, strict=True)):
        moment = start + timedelta(hours=index)
        if channel is WA:
            # A number's lifecycle lives on its row; the mirror carries it over.
            await session.execute(
                update(WhatsAppAccount)
                .where(WhatsAppAccount.id == connection_id)
                .values(ownership_started_at=moment)
            )
        else:
            await session.execute(
                update(ChannelConnection)
                .where(ChannelConnection.id == connection_id)
                .values(ownership_started_at=moment)
            )
    await session.flush()
    return ids


async def _schedule(session: AsyncSession, tenant: Tenant, plan: Plan) -> None:
    version = await PlanCatalog(session).current_version(plan)
    assert version is not None
    await SubscriptionService(session, tenant_id=tenant.id).schedule_change(
        version=version,
        price=None,
        source=ScheduledChangeSource.DOWNGRADE,
        reason="Downgrade to Pro.",
    )


async def _boundary(session: AsyncSession, tenant: Tenant) -> datetime:
    """Cross the term end with the real billing worker; returns the moment it ran at."""
    subscription = await SubscriptionService(session, tenant_id=tenant.id).get()
    assert subscription is not None
    at = subscription.current_period_end + timedelta(minutes=1)
    await _worker(session).run_once(now=at)
    return at


async def _active(session: AsyncSession, tenant: Tenant) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(ChannelConnection.id)
        .where(ChannelConnection.tenant_id == tenant.id)
        .where(ChannelConnection.status == ConnectionStatus.ACTIVE)
        .where(ChannelConnection.released_at.is_(None))
        .execution_options(populate_existing=True)
    )
    return set(rows)


async def _connections(session: AsyncSession, tenant: Tenant) -> dict[uuid.UUID, ChannelConnection]:
    rows = await session.scalars(
        select(ChannelConnection)
        .where(ChannelConnection.tenant_id == tenant.id)
        .execution_options(populate_existing=True)
    )
    return {row.id: row for row in rows}


async def _reduction(session: AsyncSession, tenant: Tenant) -> ChannelCapacityReduction | None:
    found: ChannelCapacityReduction | None = await session.scalar(
        select(ChannelCapacityReduction)
        .where(ChannelCapacityReduction.tenant_id == tenant.id)
        .order_by(ChannelCapacityReduction.created_at.desc())
        .execution_options(populate_existing=True)
    )
    return found


async def _seven_on_business_with_pro_scheduled(
    session: AsyncSession,
) -> tuple[Tenant, User, list[uuid.UUID]]:
    business, pro = await _plans(session)
    tenant, owner = await _workspace(session, business)
    ids = await _seven(session, tenant, owner)
    await _schedule(session, tenant, pro)
    return tenant, owner, ids


def _never_released_or_deleted(
    before: set[uuid.UUID], rows: dict[uuid.UUID, ChannelConnection]
) -> None:
    """E10: the flow disables; it never releases and never deletes."""
    assert before <= set(rows), "a connection was deleted"
    assert all(rows[connection_id].released_at is None for connection_id in before)


# -------------------------------------------------------- ENT-14: downgrade


async def test_a_scheduled_downgrade_refuses_an_eighth_and_the_boundary_opens_a_grace(
    db_session: AsyncSession,
) -> None:
    tenant, owner, ids = await _seven_on_business_with_pro_scheduled(db_session)

    # Seven fit Business; an eighth fits Business but not the Pro that is coming.
    with pytest.raises(ChannelCapacityExceededError) as eighth:
        await _connect(db_session, tenant, owner, MS)
    assert eighth.value.details is not None
    assert "scheduled_change" in eighth.value.details
    assert await _active(db_session, tenant) == set(ids)

    at = await _boundary(db_session, tenant)

    reduction = await _reduction(db_session, tenant)
    assert reduction is not None
    assert (reduction.cause, reduction.status) == (CapacityReductionCause.DOWNGRADE, PENDING)
    assert reduction.effective_at == at
    assert reduction.grace_ends_at == at + GRACE
    assert (reduction.target_general, reduction.target_typed) == (3, {})
    assert reduction.target_allowed_types == THREE
    # Every connection keeps working through the grace...
    assert await _active(db_session, tenant) == set(ids)
    # ...and no new connection or re-enable fits.
    with pytest.raises(ChannelCapacityExceededError):
        await _connect(db_session, tenant, owner, IG)
    state = await EntitlementService(
        db_session, tenant_id=tenant.id, default_plan_code="starter", clock=lambda: at
    ).check(LimitKey.CHANNEL_CONNECTIONS, additional=0)
    assert (state.limit, state.used, state.over_limit, state.remaining) == (3, 7, True, 0)


async def test_the_owner_keeps_three_and_the_other_four_are_disabled_never_released(
    db_session: AsyncSession,
) -> None:
    tenant, owner, ids = await _seven_on_business_with_pro_scheduled(db_session)
    await _boundary(db_session, tenant)
    reduction = await _reduction(db_session, tenant)
    assert reduction is not None
    service = _reductions(db_session, tenant)
    wa1, tg, ig, wa2, ms, tt, wa3 = ids

    with pytest.raises(CapacitySelectionError, match="does not fit") as four:
        await service.select([wa1, ig, wa2, ms], expected_revision=reduction.revision, actor=owner)
    assert (four.value.status_code, four.value.error_code) == (
        422,
        "channel_capacity_selection_invalid",
    )
    with pytest.raises(CapacitySelectionError, match="telegram"):
        await service.select([wa1, tg, ig], expected_revision=reduction.revision, actor=owner)
    with pytest.raises(ConflictError):
        await service.select([wa1, ig, ms], expected_revision=reduction.revision + 5, actor=owner)
    assert await _active(db_session, tenant) == set(ids), "a refused selection disables nothing"

    outcome = await service.select([wa1, ig, ms], expected_revision=reduction.revision, actor=owner)

    assert outcome.applied
    assert set(outcome.disabled) == {tg, wa2, tt, wa3}
    assert await _active(db_session, tenant) == {wa1, ig, ms}
    rows = await _connections(db_session, tenant)
    _never_released_or_deleted(set(ids), rows)
    for connection_id in (tg, wa2, tt, wa3):
        row = rows[connection_id]
        assert row.status is ConnectionStatus.DISABLED
        assert row.disabled_reason is ConnectionDisabledReason.CAPACITY_REDUCTION
        assert row.disabled_by == owner.id
    # A disabled number keeps its row and its claim: it is not released.
    for number in (wa2, wa3):
        account = await db_session.get(WhatsAppAccount, number, populate_existing=True)
        assert account is not None and account.status is WhatsAppAccountStatus.DISABLED
    resolved = await _reduction(db_session, tenant)
    assert resolved is not None
    assert resolved.status is CapacityReductionStatus.RESOLVED_BY_OWNER
    assert resolved.resolved_by == owner.id
    assert set(resolved.kept_connection_ids or []) == {wa1, ig, ms}
    assert set(resolved.disabled_connection_ids or []) == {tg, wa2, tt, wa3}
    # E09: every disable names its reduction, on its own audit entry.
    entries = list(
        await db_session.scalars(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant.id)
            .where(
                AuditLog.action.in_(
                    [
                        AuditAction.CHANNEL_CONNECTION_DISABLED,
                        AuditAction.WHATSAPP_ACCOUNT_DISABLED,
                    ]
                )
            )
        )
    )
    assert {entry.target_id for entry in entries} == {tg, wa2, tt, wa3}
    assert all((entry.meta or {}).get("reduction_id") == str(reduction.id) for entry in entries)
    assert all((entry.meta or {}).get("reason") == "capacity_reduction" for entry in entries)


async def test_a_preselection_made_ahead_is_applied_at_the_boundary_without_a_grace(
    db_session: AsyncSession,
) -> None:
    tenant, owner, ids = await _seven_on_business_with_pro_scheduled(db_session)
    wa1, tg, ig, wa2, ms, tt, wa3 = ids
    subscription = await SubscriptionService(db_session, tenant_id=tenant.id).get()
    assert subscription is not None

    saved = await _reductions(db_session, tenant).select(
        [wa2, ms, wa3], expected_revision=subscription.revision, actor=owner
    )

    assert (saved.applied, saved.disabled) == (False, ())
    assert await _active(db_session, tenant) == set(ids), "nothing changes before the boundary"

    at = await _boundary(db_session, tenant)

    reduction = await _reduction(db_session, tenant)
    assert reduction is not None
    assert reduction.status is CapacityReductionStatus.RESOLVED_BY_OWNER
    assert reduction.grace_ends_at == reduction.effective_at == at, "no grace: it was chosen"
    assert await _active(db_session, tenant) == {wa2, ms, wa3}
    assert await _reductions(db_session, tenant).preselected() == []
    _never_released_or_deleted(set(ids), await _connections(db_session, tenant))


async def test_with_no_choice_the_fallback_disables_disallowed_types_then_the_newest(
    db_session: AsyncSession,
) -> None:
    """M-E21, M-E22, M-E23: not before the grace ends; types first; the oldest kept."""
    tenant, _owner, ids = await _seven_on_business_with_pro_scheduled(db_session)
    wa1, tg, ig, wa2, ms, tt, wa3 = ids
    at = await _boundary(db_session, tenant)

    await _worker(db_session).run_once(now=at + GRACE - timedelta(seconds=1))
    assert await _active(db_session, tenant) == set(ids), "a second early, nothing is disabled"
    reduction = await _reduction(db_session, tenant)
    assert reduction is not None and reduction.status is PENDING

    await _worker(db_session).run_once(now=at + GRACE + timedelta(seconds=1))

    assert await _active(db_session, tenant) == {wa1, ig, wa2}
    rows = await _connections(db_session, tenant)
    _never_released_or_deleted(set(ids), rows)
    for connection_id in (tg, ms, tt, wa3):
        row = rows[connection_id]
        assert row.status is ConnectionStatus.DISABLED
        assert row.disabled_reason is ConnectionDisabledReason.CAPACITY_REDUCTION_AUTOMATIC
        assert row.disabled_by is None
    resolved = await _reduction(db_session, tenant)
    assert resolved is not None
    assert resolved.status is CapacityReductionStatus.RESOLVED_AUTOMATICALLY
    assert set(resolved.kept_connection_ids or []) == {wa1, ig, wa2}
    # Running again changes nothing: the reduction is closed.
    await _worker(db_session).run_once(now=at + GRACE + timedelta(hours=1))
    assert await _active(db_session, tenant) == {wa1, ig, wa2}


async def test_a_disabled_connection_is_enabled_again_only_through_the_guard(
    db_session: AsyncSession,
) -> None:
    tenant, owner, ids = await _seven_on_business_with_pro_scheduled(db_session)
    wa1, tg, ig, wa2, ms, tt, wa3 = ids
    await _boundary(db_session, tenant)
    reduction = await _reduction(db_session, tenant)
    assert reduction is not None
    await _reductions(db_session, tenant).select(
        [wa1, ig, ms], expected_revision=reduction.revision, actor=owner
    )
    neutral = _neutral(db_session, tenant)

    with pytest.raises(ChannelCapacityExceededError):
        await neutral.enable(wa2, actor=owner)
    await neutral.disable(ms, actor=owner)
    await neutral.enable(wa2, actor=owner)
    await neutral.disable(wa1, actor=owner)
    with pytest.raises(ChannelTypeNotAllowedError):
        # A slot is free, but TikTok is not in Pro.
        await neutral.enable(tt, actor=owner)
    assert await _active(db_session, tenant) == {ig, wa2}


# ----------------------------------------------------- ENT-15: other causes


async def _paid_channel_slots(
    session: AsyncSession, *, quantity: int, transaction: int
) -> tuple[Tenant, User, Paymob]:
    """A Starter workspace (one slot, three types) that bought `quantity` general slots."""
    now = base_now()
    starter = await own_plan(
        session,
        code="starter",
        price=Decimal("0.00"),
        limits={"channel_connections": 1},
        allowed_channel_types=THREE,
    )
    tenant, owner, _ = await topup_workspace(session, now=now, plan_code=starter.code)
    paymob = Paymob()
    item = await product(
        session, entitlement=TopupEntitlement.CHANNEL_CONNECTIONS, quantity=quantity, price="150.00"
    )
    _, payment = await buy(session, tenant, owner, paymob, item, now=now)
    signed = callback(payment, transaction=transaction)
    await apply(session, tenant.id, paymob.provider(), signed, now=now)
    return tenant, owner, paymob


async def test_an_expiring_channel_topup_opens_the_same_flow(db_session: AsyncSession) -> None:
    """1 + 2 -> 1 with three active: the expiry sweep opens a reduction, nothing disabled."""
    tenant, owner, _ = await _paid_channel_slots(db_session, quantity=2, transaction=940_000_001)
    ids = [await _connect(db_session, tenant, owner, channel) for channel in (WA, IG, MS)]

    at = await _boundary(db_session, tenant)

    reduction = await _reduction(db_session, tenant)
    assert reduction is not None
    assert (reduction.cause, reduction.status) == (CapacityReductionCause.TOPUP_EXPIRED, PENDING)
    assert reduction.grace_ends_at == at + GRACE
    assert reduction.target_general == 1
    assert await _active(db_session, tenant) == set(ids)


async def test_slots_bought_during_the_grace_close_the_reduction(
    db_session: AsyncSession,
) -> None:
    tenant, owner, paymob = await _paid_channel_slots(
        db_session, quantity=2, transaction=940_000_101
    )
    ids = [await _connect(db_session, tenant, owner, channel) for channel in (WA, IG, MS)]
    at = await _boundary(db_session, tenant)
    assert (await _reduction(db_session, tenant)) is not None

    # A new term's top-up, bought and paid during the grace.
    item = await product(
        db_session, entitlement=TopupEntitlement.CHANNEL_CONNECTIONS, quantity=2, price="150.00"
    )
    _, payment = await buy(db_session, tenant, owner, paymob, item, now=at + timedelta(hours=1))
    signed = callback(payment, transaction=940_000_102)
    await apply(db_session, tenant.id, paymob.provider(), signed, now=at + timedelta(hours=1))

    reduction = await _reduction(db_session, tenant)
    assert reduction is not None
    assert reduction.status is CapacityReductionStatus.NO_LONGER_NEEDED
    assert reduction.disabled_connection_ids == []
    assert await _active(db_session, tenant) == set(ids)
    await _worker(db_session).run_once(now=at + GRACE + timedelta(minutes=1))
    assert await _active(db_session, tenant) == set(ids)


async def test_a_withdrawn_refund_review_opens_a_reduction(db_session: AsyncSession) -> None:
    tenant, owner, paymob = await _paid_channel_slots(
        db_session, quantity=2, transaction=940_000_201
    )
    ids = [await _connect(db_session, tenant, owner, channel) for channel in (WA, IG, MS)]
    purchase = (
        await db_session.execute(select(TopupPurchase).where(TopupPurchase.tenant_id == tenant.id))
    ).scalar_one()
    payment = await db_session.get(Payment, purchase.payment_id)
    assert payment is not None
    refund = callback(payment, transaction=940_000_201, refunded_cents=15_000)
    await apply(db_session, tenant.id, paymob.provider(), refund, now=base_now())
    await db_session.refresh(purchase)
    assert purchase.status is TopupStatus.REFUND_REVIEW
    assert await _reduction(db_session, tenant) is None, "under review, the slots still count"

    staff = User(
        email=f"staff-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    db_session.add(staff)
    await db_session.flush()
    await TopupAdmin(db_session, settings=_settings()).review_refund(
        purchase.id,
        TopupRefundReview(
            decision="withdraw", reason="Refunded in full.", expected_revision=purchase.revision
        ),
        actor=staff,
    )

    reduction = await _reduction(db_session, tenant)
    assert reduction is not None
    assert (reduction.cause, reduction.status) == (CapacityReductionCause.TOPUP_WITHDRAWN, PENDING)
    assert await _active(db_session, tenant) == set(ids)


# ------------------------------------------------- ENT-16: never for suspension


async def test_a_suspended_workspace_over_capacity_opens_nothing_and_disables_nothing(
    db_session: AsyncSession,
) -> None:
    """M-E26's killer."""
    business, _pro = await _plans(db_session)
    tenant, owner = await _workspace(db_session, business)
    ids = await _seven(db_session, tenant, owner)
    subscription = await SubscriptionService(db_session, tenant_id=tenant.id).get()
    assert subscription is not None
    subscription.status = SubscriptionStatus.SUSPENDED
    await db_session.flush()

    state = await EntitlementService(
        db_session, tenant_id=tenant.id, default_plan_code="starter"
    ).check(LimitKey.CHANNEL_CONNECTIONS, additional=0)
    assert (state.limit, state.used, state.over_limit, state.remaining) == (1, 7, True, 0)
    with pytest.raises(ChannelCapacityExceededError):
        await _connect(db_session, tenant, owner, WA)
    for moment in (datetime.now(UTC), subscription.current_period_end + timedelta(days=30)):
        await _worker(db_session).run_once(now=moment)
    await _reductions(db_session, tenant).boundary(cause=CapacityReductionCause.DOWNGRADE)

    assert await _reduction(db_session, tenant) is None
    assert await _active(db_session, tenant) == set(ids)
    with pytest.raises(ConflictError):
        await _reductions(db_session, tenant).select(
            ids[:1], expected_revision=subscription.revision, actor=owner
        )


# ------------------------------------------------- isolation and the shared path


async def test_another_workspaces_connection_in_a_selection_is_not_found(
    db_session: AsyncSession,
) -> None:
    """M-E32's killer."""
    tenant, owner, ids = await _seven_on_business_with_pro_scheduled(db_session)
    business = await db_session.scalar(select(Plan).where(Plan.code == "business"))
    assert business is not None
    other, other_owner = await _workspace(db_session, business, name="Other Co")
    theirs = await _connect(db_session, other, other_owner, WA)
    await _boundary(db_session, tenant)
    reduction = await _reduction(db_session, tenant)
    assert reduction is not None

    with pytest.raises(NotFoundError):
        await _reductions(db_session, tenant).select(
            [ids[0], ids[2], theirs], expected_revision=reduction.revision, actor=owner
        )
    assert await _active(db_session, tenant) == set(ids)
    assert theirs in await _active(db_session, other)


async def _conversation_with_follow_up(
    session: AsyncSession, tenant: Tenant, account_id: uuid.UUID
) -> tuple[Conversation, FollowUp]:
    contact = Contact(tenant_id=tenant.id, wa_id=f"20155{uuid.uuid4().int % 10**7:07d}")
    session.add(contact)
    await session.flush()
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account_id,
        channel=WA,
        last_inbound_at=datetime.now(UTC),
    )
    session.add(conversation)
    await session.flush()
    follow_up = FollowUp(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        scheduled_at=datetime.now(UTC) + timedelta(hours=2),
        body="Still interested?",
        created_by_kind=ActorKind.AGENT,
    )
    session.add(follow_up)
    await session.flush()
    return conversation, follow_up


async def test_a_disable_cancels_pending_follow_ups_and_the_queued_ai_turn_is_refused(
    db_session: AsyncSession,
) -> None:
    """One disable path (ENT-14): the reduction's disables and a person's alike."""
    tenant, owner, ids = await _seven_on_business_with_pro_scheduled(db_session)
    wa1, tg, ig, wa2, ms, tt, wa3 = ids
    conversation, follow_up = await _conversation_with_follow_up(db_session, tenant, wa3)
    agent = Agent(
        tenant_id=tenant.id,
        name="Helper",
        is_default=True,
        status=AgentStatus.ACTIVE,
        model="gpt-4o-mini",
        system_prompt="Answer briefly.",
    )
    db_session.add(agent)
    await db_session.flush()

    async def refusal() -> TurnOutcome | None:
        return await refusal_now(
            db_session,
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            agent_id=agent.id,
            channels=_registry(),
        )

    async def state() -> tuple[FollowUpStatus | None, str | None]:
        row = (
            await db_session.execute(
                select(FollowUp.status, FollowUp.cancelled_reason).where(
                    FollowUp.id == follow_up.id
                )
            )
        ).one()
        return row[0], row[1]

    at = await _boundary(db_session, tenant)
    # Through the grace the newest number still answers.
    assert await refusal() is None
    assert await state() == (FollowUpStatus.PENDING, None)

    await _worker(db_session).run_once(now=at + GRACE + timedelta(seconds=1))

    assert await state() == (FollowUpStatus.CANCELLED, "connection_disabled")
    # The turn a customer's next message would queue is refused before any
    # hold, provider call or charge.
    assert await refusal() is TurnOutcome.SUPPRESSED_CHANNEL


async def test_a_person_disabling_a_number_cancels_its_pending_follow_ups(
    db_session: AsyncSession,
) -> None:
    """The semantics the baseline measured as B9 change: a manual disable cancels too."""
    business, _pro = await _plans(db_session)
    tenant, owner = await _workspace(db_session, business)
    number = await _number(db_session, tenant)
    _conversation, follow_up = await _conversation_with_follow_up(db_session, tenant, number)

    await WhatsAppAccountService(session=db_session, default_plan_code="starter").set_status(
        tenant_id=tenant.id,
        account_id=number,
        status=WhatsAppAccountStatus.DISABLED,
        actor=owner,
    )

    await db_session.refresh(follow_up)
    assert (follow_up.status, follow_up.cancelled_reason) == (
        FollowUpStatus.CANCELLED,
        "connection_disabled",
    )


# --------------------------------------------------------------- notifications


async def test_owners_are_told_ahead_at_the_boundary_and_two_days_before(
    db_session: AsyncSession,
) -> None:
    tenant, owner, _ids = await _seven_on_business_with_pro_scheduled(db_session)
    subscription = await SubscriptionService(db_session, tenant_id=tenant.id).get()
    assert subscription is not None

    async def templates() -> list[str]:
        rows = await db_session.scalars(
            select(OutboundEmail.template)
            .where(OutboundEmail.tenant_id == tenant.id)
            .where(OutboundEmail.template.like("channel_capacity_%"))
            .order_by(OutboundEmail.created_at)
        )
        return list(rows)

    await _worker(db_session, _mailing()).run_once(
        now=subscription.current_period_end - timedelta(days=3)
    )
    await _worker(db_session, _mailing()).run_once(
        now=subscription.current_period_end - timedelta(days=2)
    )
    assert await templates() == ["channel_capacity_scheduled"], "told once, ahead"

    at = subscription.current_period_end + timedelta(minutes=1)
    await _worker(db_session, _mailing()).run_once(now=at)
    assert await templates() == [
        "channel_capacity_scheduled",
        "channel_capacity_reduction_started",
    ]

    await _worker(db_session, _mailing()).run_once(now=at + GRACE - timedelta(hours=49))
    assert len(await templates()) == 2
    await _worker(db_session, _mailing()).run_once(now=at + GRACE - timedelta(hours=47))
    await _worker(db_session, _mailing()).run_once(now=at + GRACE - timedelta(hours=46))
    assert await templates() == [
        "channel_capacity_scheduled",
        "channel_capacity_reduction_started",
        "channel_capacity_reduction_warning",
    ]
    reduction = await _reduction(db_session, tenant)
    assert reduction is not None
    assert reduction.notified_at is not None and reduction.warned_at is not None
    recipients = await db_session.scalars(
        select(func.count()).select_from(OutboundEmail).where(OutboundEmail.user_id == owner.id)
    )
    assert recipients.one() >= 3


# ------------------------------------------------------------------- the API


class _Infra:
    def __init__(self) -> None:
        self.commands = self

    @property
    def client(self) -> _Infra:
        return self.commands

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return -1

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


@pytest.fixture
def app(db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(_settings())
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://wasla.test") as c:
        yield c


def _act_as(app: FastAPI, tenant: Tenant, owner: User, role: TenantRole) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: ActiveWorkspace(
        user=owner,
        membership=Membership(
            id=uuid.uuid4(),
            user_id=owner.id,
            tenant_id=tenant.id,
            role=role,
            status=MembershipStatus.ACTIVE,
        ),
        tenant=tenant,
    )


async def test_the_capacity_page_and_the_selection_route(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    tenant, owner, ids = await _seven_on_business_with_pro_scheduled(db_session)
    wa1, tg, ig, wa2, ms, tt, wa3 = ids
    _act_as(app, tenant, owner, TenantRole.TENANT_OWNER)

    ahead = await http.get("/api/v1/billing/channel-capacity")
    assert ahead.status_code == 200, ahead.text
    body: dict[str, Any] = ahead.json()
    assert (body["effective_limit"], body["active"], body["over_limit"]) == (10, 7, False)
    assert body["scheduled"]["effective_limit"] == 3
    assert body["scheduled"]["fits"] is False
    assert body["reduction"] is None
    assert set(body["automatic_fallback"]["keep"]) == {str(wa1), str(ig), str(wa2)}

    await _boundary(db_session, tenant)
    during = (await http.get("/api/v1/billing/channel-capacity")).json()
    assert during["reduction"]["status"] == "pending_selection"
    assert during["reduction"]["cause"] == "downgrade"
    assert during["over_limit"] is True
    revision = during["selection_revision"]
    assert revision == during["reduction"]["revision"]

    too_many = await http.post(
        "/api/v1/billing/channel-capacity/selection",
        json={"keep": [str(wa1), str(ig), str(ms), str(wa2)], "expected_revision": revision},
    )
    assert too_many.status_code == 422, too_many.text
    assert too_many.json()["error"]["code"] == "channel_capacity_selection_invalid"
    unknown = await http.post(
        "/api/v1/billing/channel-capacity/selection",
        json={"keep": [str(wa1), str(uuid.uuid4())], "expected_revision": revision},
    )
    assert unknown.status_code == 404, unknown.text

    chosen = await http.post(
        "/api/v1/billing/channel-capacity/selection",
        json={"keep": [str(wa1), str(ig), str(ms)], "expected_revision": revision},
    )
    assert chosen.status_code == 200, chosen.text
    result = chosen.json()
    assert result["applied"] is True
    assert set(result["disabled"]) == {str(tg), str(wa2), str(tt), str(wa3)}
    assert result["capacity"]["active"] == 3
    assert result["capacity"]["over_limit"] is False
    assert result["capacity"]["reduction"] is None

    _act_as(app, tenant, owner, TenantRole.MEMBER)
    member = await http.post(
        "/api/v1/billing/channel-capacity/selection",
        json={"keep": [str(wa1)], "expected_revision": revision},
    )
    assert member.status_code == 403

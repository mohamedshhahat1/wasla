# ruff: noqa: F811 - the capacity-reduction harness fixtures are imported by name.
"""Withdrawing a platform grant before it ends (PLAT-G1, ADR-132).

Through the real route - `POST /platform/billing/topup-purchases/{id}/withdraw`
on the real ASGI app - wherever a request is the thing under test; the
capacity boundary it reaches is the real `ChannelCapacityReductions.boundary`,
and the grace is ended by the real billing worker (`BillingWorker.run_once`),
never by calling the fallback directly. Connections are made through the real
capacity guard.

What is proved:

- the grant stops counting in the request that withdraws it: the guard refuses
  a connection that fitted a moment before;
- a withdrawal that leaves the workspace fitting opens nothing; one that does
  not opens a `grant_withdrawn` reduction with the 7-day grace, naming the
  grant, and disables nothing - the fallback disables the newest after the
  grace, keeps the oldest, releases and deletes nothing;
- a typed grant takes only its own channel's slot with it;
- an AI-turn grant lowers the allowance at once, recorded usage stays, and the
  next turn over the new limit is handed to a person by the real AI worker
  with no provider call;
- refused: a paid purchase, a second withdrawal, another workspace's grant, a
  tenant role; and every withdrawal is audited with its reason, before and
  after.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import LimitKey, Subscription
from app.db.models.channel import Channel
from app.db.models.channel_capacity import (
    CapacityReductionCause,
    CapacityReductionStatus,
    ChannelCapacityReduction,
)
from app.db.models.conversation import ConversationMode
from app.db.models.enums import MembershipStatus, PlatformRole, TenantRole
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupStatus
from app.db.models.usage import UsageEvent, UsageEventType, UsageUnit
from app.db.models.user import User
from app.platform.topup_admin import TopupAdmin
from app.schemas.topup import TopupGrantCreate, TopupGrantWithdraw
from app.services.channel_capacity import ChannelCapacityExceededError
from app.services.entitlement_service import EntitlementService
from app.services.subscription_service import SubscriptionService
from app.workers.ai_worker import QUOTA_HANDOFF_REASON
from scripts.omnichannel_invariants import entitlement_violations
from tests.integration.ai_harness import FakeProviders, TurnRunner
from tests.integration.plan_catalogue import own_plan
from tests.integration.test_capacity_reductions import (  # noqa: F401 - fixtures
    GRACE,
    IG,
    MS,
    PENDING,
    THREE,
    WA,
    _active,
    _connect,
    _connections,
    _never_released_or_deleted,
    _paid_channel_slots,
    _reduction,
    _reductions,
    _settings,
    _worker,
    app,
    http,
)
from tests.integration.test_platform_billing_api import _act_as as _act_as_user
from tests.integration.test_platform_billing_api import _user
from tests.integration.topup_harness import base_now
from tests.integration.topup_harness import workspace as topup_workspace

pytestmark = pytest.mark.integration


async def _staff(session: AsyncSession) -> User:
    """A platform admin with a verified address: refused only for what a test means."""
    return await _user(session, role=PlatformRole.PLATFORM_ADMIN)


BASE = "/api/v1/platform/billing"
NEW_LEDGER = (
    "e16_withdrawn_grant_still_counting",
    "e17_withdrawn_grant_without_its_audit",
    "e18_grant_withdrawn_reduction_without_its_grant",
)


async def _starter(session: AsyncSession, *, connections: int = 1) -> None:
    await own_plan(
        session,
        code="starter",
        price=Decimal("0.00"),
        limits={"channel_connections": connections, "period_ai_turns": 2},
        allowed_channel_types=THREE,
    )


async def _granted(
    session: AsyncSession,
    staff: User,
    *,
    quantity: int = 1,
    channel: Channel | None = None,
    key: TopupEntitlement = TopupEntitlement.CHANNEL_CONNECTIONS,
    tenant: Tenant | None = None,
) -> tuple[Tenant, User, uuid.UUID]:
    """A Starter workspace (one slot, three types) holding a platform grant."""
    owner: User | None = None
    if tenant is None:
        await _starter(session)
        tenant, owner, _ = await topup_workspace(session, now=base_now(), plan_code="starter")
    subscription = await SubscriptionService(session, tenant_id=tenant.id).get()
    assert subscription is not None
    granted = await TopupAdmin(session, settings=_settings()).grant(
        tenant.id,
        TopupGrantCreate(
            entitlement_key=key,
            quantity=quantity,
            channel_type=channel,
            reason="Launch goodwill.",
            expected_subscription_revision=subscription.revision,
        ),
        actor=staff,
    )
    return tenant, owner if owner is not None else staff, granted.id


async def _withdraw(
    http: AsyncClient,
    purchase_id: uuid.UUID,
    tenant: Tenant,
    revision: int,
    *,
    reason: str = "Granted to the wrong workspace.",
) -> Any:
    return await http.post(
        f"{BASE}/topup-purchases/{purchase_id}/withdraw",
        json={"tenant_id": str(tenant.id), "reason": reason, "expected_revision": revision},
    )


async def _purchase(session: AsyncSession, purchase_id: uuid.UUID) -> TopupPurchase:
    found = await session.get(TopupPurchase, purchase_id, populate_existing=True)
    assert found is not None
    return found


async def _limit(session: AsyncSession, tenant: Tenant, key: LimitKey) -> tuple[int | None, int]:
    state = await EntitlementService(
        session, tenant_id=tenant.id, default_plan_code="starter"
    ).check(key, additional=0)
    return state.limit, state.used


async def _ledger(session: AsyncSession) -> dict[str, int]:
    await session.flush()
    found = await entitlement_violations(await session.connection(), read_only=False)
    return {name: found[name] for name in NEW_LEDGER}


async def _withdrawals(session: AsyncSession, purchase_id: uuid.UUID) -> list[AuditLog]:
    return list(
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.BILLING_TOPUP_GRANT_WITHDRAWN)
            .where(AuditLog.target_id == purchase_id)
        )
    )


# ---------------------------------------------------------- it stops counting


async def test_staff_withdraw_a_grant_and_it_stops_counting_at_once(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, owner, grant = await _granted(db_session, staff)
    await _connect(db_session, tenant, owner, WA)
    assert await _limit(db_session, tenant, LimitKey.CHANNEL_CONNECTIONS) == (2, 1)
    _act_as_user(app, staff)

    response = await _withdraw(http, grant, tenant, (await _purchase(db_session, grant)).revision)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["purchase"]["status"] == "withdrawn"
    assert body["purchase"]["withdrawn_by"] == str(staff.id)
    assert body["purchase"]["withdrawal_reason"] == "Granted to the wrong workspace."
    assert body["purchase"]["withdrawn_at"] is not None
    assert (body["effective_limit_before"], body["effective_limit_after"]) == (2, 1)
    assert body["reduction"] is None
    # In force at once: the slot the grant gave is gone, so a second
    # connection that fitted a moment ago is refused by the guard.
    assert await _limit(db_session, tenant, LimitKey.CHANNEL_CONNECTIONS) == (1, 1)
    with pytest.raises(ChannelCapacityExceededError):
        await _connect(db_session, tenant, owner, IG)
    purchase = await _purchase(db_session, grant)
    # The snapshot is untouched: key, quantity, channel, expiry as granted.
    assert (purchase.entitlement_key, purchase.quantity, purchase.channel_type) == (
        TopupEntitlement.CHANNEL_CONNECTIONS,
        1,
        None,
    )
    assert purchase.ended_at is not None
    read = await http.get(f"{BASE}/topup-purchases/{grant}")
    assert read.status_code == 200
    assert read.json()["status"] == "withdrawn"
    assert read.json()["withdrawn_by"] == str(staff.id)
    assert await _ledger(db_session) == dict.fromkeys(NEW_LEDGER, 0)


async def test_withdrawing_a_capacity_grant_that_still_fits_opens_nothing(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, owner, grant = await _granted(db_session, staff, quantity=2)
    ids = {await _connect(db_session, tenant, owner, WA)}
    _act_as_user(app, staff)

    response = await _withdraw(http, grant, tenant, (await _purchase(db_session, grant)).revision)

    assert response.status_code == 200, response.text
    assert response.json()["reduction"] is None
    assert await _reduction(db_session, tenant) is None
    assert await _active(db_session, tenant) == ids


# ------------------------------------------------- it opens the same grace


async def test_withdrawing_a_capacity_grant_that_no_longer_fits_opens_a_reduction_with_a_grace(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, owner, grant = await _granted(db_session, staff, quantity=2)
    oldest, middle, newest = [await _connect(db_session, tenant, owner, ch) for ch in (WA, IG, MS)]
    ids = {oldest, middle, newest}
    _act_as_user(app, staff)

    response = await _withdraw(http, grant, tenant, (await _purchase(db_session, grant)).revision)

    assert response.status_code == 200, response.text
    body = response.json()
    reduction = await _reduction(db_session, tenant)
    assert reduction is not None
    assert body["reduction"]["id"] == str(reduction.id)
    assert (reduction.cause, reduction.status) == (CapacityReductionCause.GRANT_WITHDRAWN, PENDING)
    assert reduction.topup_purchase_id == grant
    assert reduction.grace_ends_at - reduction.effective_at == GRACE
    assert reduction.target_general == 1
    # Nothing is disabled by the withdrawal itself; the grace keeps everything.
    assert await _active(db_session, tenant) == ids
    # The owner can choose during the grace: the page shows what the fallback
    # would do and the revision a selection names.
    preview = await _reductions(db_session, tenant).fallback_preview()
    assert preview is not None
    assert {row.id for row in preview.keep} == {oldest}
    assert await _ledger(db_session) == dict.fromkeys(NEW_LEDGER, 0)

    # A second before the grace ends, the real billing worker disables nothing.
    await _worker(db_session).run_once(now=reduction.grace_ends_at - timedelta(seconds=1))
    assert await _active(db_session, tenant) == ids

    await _worker(db_session).run_once(now=reduction.grace_ends_at + timedelta(seconds=1))

    assert await _active(db_session, tenant) == {oldest}
    rows = await _connections(db_session, tenant)
    _never_released_or_deleted(ids, rows)
    resolved = await _reduction(db_session, tenant)
    assert resolved is not None
    assert resolved.status is CapacityReductionStatus.RESOLVED_AUTOMATICALLY
    assert set(resolved.disabled_connection_ids or []) == {middle, newest}
    assert await _ledger(db_session) == dict.fromkeys(NEW_LEDGER, 0)


async def test_an_owner_resolves_a_withdrawal_reduction_during_the_grace(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, owner, grant = await _granted(db_session, staff, quantity=1)
    first, second = [await _connect(db_session, tenant, owner, ch) for ch in (WA, IG)]
    _act_as_user(app, staff)
    response = await _withdraw(http, grant, tenant, (await _purchase(db_session, grant)).revision)
    assert response.status_code == 200, response.text
    reduction = await _reduction(db_session, tenant)
    assert reduction is not None and reduction.status is PENDING

    # The owner keeps the newer one - their choice, not the fallback's.
    outcome = await _reductions(db_session, tenant).select(
        [second], expected_revision=reduction.revision, actor=owner
    )

    assert outcome.applied and outcome.disabled == (first,)
    resolved = await _reduction(db_session, tenant)
    assert resolved is not None
    assert resolved.status is CapacityReductionStatus.RESOLVED_BY_OWNER
    assert resolved.cause is CapacityReductionCause.GRANT_WITHDRAWN
    _never_released_or_deleted({first, second}, await _connections(db_session, tenant))


async def test_withdrawing_a_typed_grant_only_affects_its_channel(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, owner, instagram = await _granted(db_session, staff, channel=Channel.INSTAGRAM)
    _, _, messenger = await _granted(db_session, staff, channel=Channel.MESSENGER, tenant=tenant)
    for channel in (WA, IG, MS):
        await _connect(db_session, tenant, owner, channel)
    _act_as_user(app, staff)

    response = await _withdraw(
        http, messenger, tenant, (await _purchase(db_session, messenger)).revision
    )

    assert response.status_code == 200, response.text
    reduction = await _reduction(db_session, tenant)
    assert reduction is not None
    # Messenger's typed slot is gone; Instagram's is exactly what it was.
    assert (reduction.target_general, reduction.target_typed) == (1, {"instagram": 1})
    assert (await _purchase(db_session, instagram)).status is TopupStatus.GRANTED
    capacity = await EntitlementService(
        db_session, tenant_id=tenant.id, default_plan_code="starter"
    ).channel_capacity()
    assert (capacity.general, dict(capacity.typed)) == (1, {Channel.INSTAGRAM: 1})


# ------------------------------------------------------------- AI turns


async def test_withdrawing_an_ai_turn_grant_lowers_the_limit_and_keeps_recorded_usage(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, _, grant = await _granted(
        db_session, staff, quantity=3, key=TopupEntitlement.PERIOD_AI_TURNS
    )
    for _ in range(4):
        db_session.add(
            UsageEvent(
                tenant_id=tenant.id,
                event_type=UsageEventType.AI_TURN,
                quantity=1,
                unit=UsageUnit.COUNT,
                occurred_at=datetime.now(UTC),
                channel=Channel.WHATSAPP,
            )
        )
    await db_session.flush()
    assert await _limit(db_session, tenant, LimitKey.PERIOD_AI_TURNS) == (5, 4)
    _act_as_user(app, staff)

    response = await _withdraw(http, grant, tenant, (await _purchase(db_session, grant)).revision)

    assert response.status_code == 200, response.text
    assert (
        response.json()["effective_limit_before"],
        response.json()["effective_limit_after"],
    ) == (
        5,
        2,
    )
    assert response.json()["reduction"] is None
    # The allowance is two again; the four turns already recorded stay recorded.
    assert await _limit(db_session, tenant, LimitKey.PERIOD_AI_TURNS) == (2, 4)
    recorded = await db_session.scalar(
        select(func.count())
        .select_from(UsageEvent)
        .where(UsageEvent.tenant_id == tenant.id, UsageEvent.event_type == UsageEventType.AI_TURN)
    )
    assert recorded == 4
    assert await _reduction(db_session, tenant) is None


async def test_the_next_ai_turn_after_withdrawal_is_handed_off_when_the_limit_is_reached(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The real AI worker: two turns on a grant, the grant withdrawn, the third handed off."""
    code = await ai_turns.plan({LimitKey.PERIOD_AI_TURNS.value: 1})
    workspace = await ai_turns.workspace()
    async with ai_turns.database.session() as session:
        await SubscriptionService(session, tenant_id=workspace.tenant_id).start(
            plan_code=code, now=datetime.now(UTC) - timedelta(hours=1), self_service=False
        )
        staff = await _staff(session)
        ai_turns.users.append(staff.id)
        subscription = await SubscriptionService(session, tenant_id=workspace.tenant_id).get()
        assert subscription is not None
        granted = await TopupAdmin(session, settings=ai_turns.settings).grant(
            workspace.tenant_id,
            TopupGrantCreate(
                entitlement_key=TopupEntitlement.PERIOD_AI_TURNS,
                quantity=1,
                reason="Two turns for the demo.",
                expected_subscription_revision=subscription.revision,
            ),
            actor=staff,
        )
    try:
        for index in range(2):
            conversation, ids = await ai_turns.write(
                workspace, [f"question {index}"], wa_id=f"20155510{index:04d}"
            )
            await ai_turns.answer(workspace, conversation, ids[0])
        assert (await ai_turns.usage(workspace.tenant_id)).get("ai_turn") == 2
        answered = ai_providers.inference

        async with ai_turns.database.session() as session:
            purchase = await session.get(TopupPurchase, granted.id)
            assert purchase is not None
            result = await TopupAdmin(session, settings=ai_turns.settings).withdraw_grant(
                granted.id,
                TopupGrantWithdraw(
                    tenant_id=workspace.tenant_id,
                    reason="The demo is over.",
                    expected_revision=purchase.revision,
                ),
                actor=staff,
            )
        assert (result.effective_limit_before, result.effective_limit_after) == (2, 1)

        conversation, ids = await ai_turns.write(workspace, ["one more"], wa_id="201555109999")
        await ai_turns.answer(workspace, conversation, ids[0])

        assert ai_providers.inference == answered, "no provider call for a turn with no allowance"
        handed = await ai_turns.conversation(conversation)
        assert handed.mode is ConversationMode.HUMAN
        assert handed.handoff_reason == QUOTA_HANDOFF_REASON
        assert (await ai_turns.usage(workspace.tenant_id)).get("ai_turn") == 2
    finally:
        async with ai_turns.database.session() as session:
            await session.execute(
                delete(TopupPurchase).where(TopupPurchase.tenant_id == workspace.tenant_id)
            )
            await session.execute(
                delete(Subscription).where(Subscription.tenant_id == workspace.tenant_id)
            )


# ------------------------------------------------------------- refusals


async def test_a_paid_purchase_cannot_be_withdrawn(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    tenant, _owner, _paymob = await _paid_channel_slots(
        db_session, quantity=1, transaction=940_000_811
    )
    purchase = (
        await db_session.execute(select(TopupPurchase).where(TopupPurchase.tenant_id == tenant.id))
    ).scalar_one()
    assert purchase.status is TopupStatus.GRANTED
    _act_as_user(app, await _staff(db_session))

    response = await _withdraw(http, purchase.id, tenant, purchase.revision)

    assert response.status_code == 409, response.text
    assert "refunded" in response.text
    assert (await _purchase(db_session, purchase.id)).status is TopupStatus.GRANTED


async def test_a_second_withdrawal_is_409_and_changes_nothing(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, _, grant = await _granted(db_session, staff)
    _act_as_user(app, staff)
    revision = (await _purchase(db_session, grant)).revision
    assert (await _withdraw(http, grant, tenant, revision)).status_code == 200
    after_first = await _purchase(db_session, grant)
    first_state = (after_first.revision, after_first.withdrawn_at, after_first.withdrawal_reason)

    again = await _withdraw(http, grant, tenant, revision, reason="Once more, by mistake.")
    current = await _withdraw(http, grant, tenant, after_first.revision, reason="And again.")

    assert again.status_code == 409, again.text
    assert current.status_code == 409, current.text
    after = await _purchase(db_session, grant)
    assert (after.revision, after.withdrawn_at, after.withdrawal_reason) == first_state
    assert len(await _withdrawals(db_session, grant)) == 1


async def test_another_workspaces_grant_is_404(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, _, grant = await _granted(db_session, staff)
    other = Tenant(name="Other", slug=f"other-{uuid.uuid4().hex[:10]}")
    db_session.add(other)
    await db_session.flush()
    _act_as_user(app, staff)
    revision = (await _purchase(db_session, grant)).revision

    wrong = await _withdraw(http, grant, other, revision)
    unknown = await _withdraw(http, uuid.uuid4(), tenant, revision)

    assert (wrong.status_code, unknown.status_code) == (404, 404)
    assert (await _purchase(db_session, grant)).status is TopupStatus.GRANTED
    assert await _withdrawals(db_session, grant) == []


@pytest.mark.parametrize(
    "role", [TenantRole.MEMBER, TenantRole.TENANT_ADMIN, TenantRole.TENANT_OWNER]
)
async def test_a_tenant_role_cannot_withdraw(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, role: TenantRole
) -> None:
    staff = await _staff(db_session)
    tenant, _, grant = await _granted(db_session, staff)
    person = await _user(db_session)
    db_session.add(
        Membership(
            tenant_id=tenant.id, user_id=person.id, role=role, status=MembershipStatus.ACTIVE
        )
    )
    await db_session.flush()
    _act_as_user(app, person)

    response = await _withdraw(http, grant, tenant, (await _purchase(db_session, grant)).revision)

    assert response.status_code == 403, response.text
    # Refused for the platform role it lacks - its address is verified.
    assert response.json()["error"]["code"] == "permission_denied"
    assert (await _purchase(db_session, grant)).status is TopupStatus.GRANTED


async def test_every_withdrawal_is_audited_with_reason_before_and_after(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, owner, grant = await _granted(db_session, staff, quantity=1)
    await _connect(db_session, tenant, owner, WA)
    await _connect(db_session, tenant, owner, IG)
    _act_as_user(app, staff)

    response = await _withdraw(
        http, grant, tenant, (await _purchase(db_session, grant)).revision, reason="Wrong tenant."
    )

    assert response.status_code == 200, response.text
    [entry] = await _withdrawals(db_session, grant)
    meta = entry.meta or {}
    assert (entry.actor_id, entry.tenant_id, entry.target_type) == (
        staff.id,
        tenant.id,
        "topup_purchase",
    )
    assert meta["actor_role"] == "platform_admin"
    assert meta["reason"] == "Wrong tenant."
    assert (meta["before"]["status"], meta["after"]["status"]) == ("granted", "withdrawn")
    assert (meta["before"]["effective_limit"], meta["after"]["effective_limit"]) == (2, 1)
    assert meta["after"]["withdrawn_at"] is not None
    assert (meta["entitlement_key"], meta["quantity"]) == ("channel_connections", 1)
    reduction = await db_session.scalar(
        select(ChannelCapacityReduction).where(ChannelCapacityReduction.tenant_id == tenant.id)
    )
    assert reduction is not None and meta["reduction_id"] == str(reduction.id)
    assert "request_id" in meta

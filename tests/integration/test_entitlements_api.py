# ruff: noqa: F811 - the commercial API harness fixtures are imported by name.
"""What a workspace and platform staff read about AI turns and channel capacity.

ENT-01, ENT-05, ENT-14 through the real ASGI app: `GET /billing/entitlements`
reports one AI allowance for the whole workspace - used, held, remaining, and
`used_by_channel` for display - and channel capacity with where its slots come
from; `GET /platform/billing/tenants/{id}/summary` shows staff the same figures
and the workspace's latest capacity reduction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditActorKind
from app.db.models.billing import LimitKey
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
from app.db.models.enums import PlatformRole, TenantRole
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEvent, UsageEventType, UsageUnit
from tests.billing_fixtures import add_owner
from tests.channel_plans import plan_with_channels, subscribe
from tests.integration.test_channel_topups import _bought, _catalogue, _neutral, _number
from tests.integration.test_commercial_api import (  # noqa: F401 - fixtures
    _as_member,
    _staff,
    app,
    http,
    intentions,
)
from tests.integration.topup_harness import Paymob, base_now, workspace

pytestmark = pytest.mark.integration

BILLING = "/api/v1/billing"
PLATFORM = "/api/v1/platform/billing"


def _key(entitlements: list[dict[str, Any]], key: LimitKey) -> dict[str, Any]:
    [row] = [row for row in entitlements if row["key"] == key.value]
    return row


async def test_one_ai_allowance_reads_used_held_remaining_and_its_channels(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Section 16's example: 742 used of 1,000, from every channel together."""
    now = base_now()
    tenant = Tenant(name="Turns", slug=f"turns-{uuid.uuid4().hex[:8]}")
    db_session.add(tenant)
    await db_session.flush()
    owner = await add_owner(db_session, tenant)
    plan = await plan_with_channels(db_session, limits={LimitKey.PERIOD_AI_TURNS.value: 1_000})
    await subscribe(db_session, tenant.id, plan)
    for channel, used in (
        (Channel.WHATSAPP, 500),
        (Channel.INSTAGRAM, 200),
        (Channel.MESSENGER, 42),
    ):
        db_session.add(
            UsageEvent(
                tenant_id=tenant.id,
                event_type=UsageEventType.AI_TURN,
                quantity=used,
                unit=UsageUnit.COUNT,
                occurred_at=now - timedelta(minutes=5),
                channel=channel,
                connection_id=uuid.uuid4(),
            )
        )
    await db_session.flush()
    _as_member(app, tenant, owner, TenantRole.MEMBER)

    response = await http.get(f"{BILLING}/entitlements")

    assert response.status_code == 200, response.text
    turns = _key(response.json(), LimitKey.PERIOD_AI_TURNS)
    assert (turns["used"], turns["held"], turns["limit"], turns["remaining"]) == (742, 0, 1000, 258)
    assert (turns["enforced"], turns["over_limit"]) == (True, False)
    assert turns["used_by_channel"] == [
        {"channel": "instagram", "used": 200},
        {"channel": "messenger", "used": 42},
        {"channel": "whatsapp", "used": 500},
    ]
    # One meter: its channels add up to the workspace's figure, and no other
    # key carries a breakdown by channel.
    assert sum(row["used"] for row in turns["used_by_channel"]) == turns["used"]
    messages = _key(response.json(), LimitKey.PERIOD_MESSAGES)
    assert (messages["used_by_channel"], messages["held"]) == (None, 0)


async def test_channel_capacity_reads_included_purchased_and_active_by_channel(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Section 17's example through the API: Starter 1 + purchased 2 = 3 / 3."""
    now = base_now()
    starter, _ = await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="starter")
    await _bought(
        db_session,
        tenant,
        owner,
        Paymob(),
        quantity=2,
        eligible=(starter,),
        now=now,
        transaction=930_100_001,
    )
    await _number(db_session, tenant)
    neutral = _neutral(db_session, tenant)
    await neutral.connect(channel=Channel.INSTAGRAM, external_account_id="ig-api", actor=owner)
    await neutral.connect(channel=Channel.MESSENGER, external_account_id="pg-api", actor=owner)
    _as_member(app, tenant, owner, TenantRole.MEMBER)

    response = await http.get(f"{BILLING}/entitlements")

    assert response.status_code == 200, response.text
    slots = _key(response.json(), LimitKey.CHANNEL_CONNECTIONS)
    assert (slots["base_limit"], slots["topup_limit"], slots["platform_grant_limit"]) == (1, 2, 0)
    assert (slots["effective_limit"], slots["used"], slots["remaining"]) == (3, 3, 0)
    assert (slots["over_limit"], slots["enforced"], slots["kind"]) == (False, True, "capacity")
    capacity = slots["channel_capacity"]
    assert capacity["active_by_channel"] == [
        {"channel": "whatsapp", "count": 1},
        {"channel": "instagram", "count": 1},
        {"channel": "messenger", "count": 1},
    ]
    assert capacity["typed_slots"] == []
    assert "whatsapp_numbers" not in {row["key"] for row in response.json()}


async def test_staff_read_the_same_figures_and_the_latest_reduction(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    now = base_now()
    starter, _ = await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="starter")
    await _number(db_session, tenant)
    db_session.add(
        ChannelCapacityReduction(
            tenant_id=tenant.id,
            cause=CapacityReductionCause.TOPUP_EXPIRED,
            status=CapacityReductionStatus.PENDING_SELECTION,
            target_general=1,
            target_typed={},
            target_allowed_types=["whatsapp"],
            effective_at=now,
            grace_ends_at=now + timedelta(days=7),
        )
    )
    await db_session.flush()
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)

    response = await http.get(f"{PLATFORM}/tenants/{tenant.id}/summary")

    assert response.status_code == 200, response.text
    summary = response.json()
    slots = _key(summary["entitlements"], LimitKey.CHANNEL_CONNECTIONS)
    assert (slots["effective_limit"], slots["used"]) == (1, 1)
    assert slots["channel_capacity"]["active_by_channel"] == [{"channel": "whatsapp", "count": 1}]
    turns = _key(summary["entitlements"], LimitKey.PERIOD_AI_TURNS)
    assert (turns["used"], turns["held"], turns["used_by_channel"]) == (0, 0, [])
    reduction = summary["channel_capacity_reduction"]
    assert (reduction["status"], reduction["cause"]) == ("pending_selection", "topup_expired")
    assert reduction["grace_ends_at"] is not None


async def test_a_preview_counts_who_a_smaller_capacity_or_fewer_types_would_leave_over(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Section 29: per proposed value, the workspaces whose active connections
    exceed it and the workspaces holding a connection of a type it removes."""
    plan = await plan_with_channels(db_session, limits={LimitKey.CHANNEL_CONNECTIONS.value: 10})
    big = Tenant(name="Big", slug=f"big-{uuid.uuid4().hex[:8]}")
    small = Tenant(name="Small", slug=f"small-{uuid.uuid4().hex[:8]}")
    db_session.add_all([big, small])
    await db_session.flush()
    for tenant, channels in (
        (big, (Channel.WHATSAPP, Channel.INSTAGRAM, Channel.MESSENGER, Channel.TIKTOK)),
        (small, (Channel.WHATSAPP,)),
    ):
        await subscribe(db_session, tenant.id, plan)
        for channel in channels:
            db_session.add(
                ChannelConnection(
                    id=uuid.uuid4(),
                    tenant_id=tenant.id,
                    channel=channel,
                    external_account_id=f"{channel.value}-{uuid.uuid4().hex[:8]}",
                    status=ConnectionStatus.ACTIVE,
                    ownership_started_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
    await db_session.flush()
    await _staff(app, db_session, PlatformRole.PLATFORM_ADMIN)

    preview = await http.post(
        f"{PLATFORM}/plans/{plan.id}/versions/preview",
        json={
            "price": "0.00",
            "currency": "EGP",
            "interval": "monthly",
            "limits": {LimitKey.CHANNEL_CONNECTIONS.value: 3},
            "allowed_channel_types": ["whatsapp", "instagram", "messenger"],
        },
    )

    assert preview.status_code == 200, preview.text
    body = preview.json()
    [capacity] = [row for row in body["limits"] if row["key"] == "channel_connections"]
    assert (capacity["old"], capacity["new"], capacity["workspaces_above_new_limit"]) == (10, 3, 1)
    types = body["channel_types"]
    assert types["removed"] == ["telegram", "tiktok"]
    assert types["workspaces_holding_a_removed_type"] == 1


async def test_the_neutral_connection_list_says_what_takes_a_slot_and_why_one_does_not(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Section 30: every channel's connections in one list - active and disabled,
    with the reason - never a released one, never another workspace's."""
    now = base_now()
    await _catalogue(db_session)
    tenant, owner, _ = await workspace(db_session, now=now, plan_code="pro")
    rival, _, _ = await workspace(db_session, now=now, plan_code="pro", name="Rival")
    await _number(db_session, tenant)
    await _number(db_session, rival)
    neutral = _neutral(db_session, tenant)
    kept = await neutral.connect(
        channel=Channel.INSTAGRAM, external_account_id="ig-list", actor=owner
    )
    paused = await neutral.connect(
        channel=Channel.MESSENGER, external_account_id="pg-list", actor=owner
    )
    await neutral.disable(
        paused.id,
        reason=ConnectionDisabledReason.CAPACITY_REDUCTION,
        actor=owner,
        actor_kind=AuditActorKind.USER,
    )
    # The slot the disable freed.
    gone = await neutral.connect(
        channel=Channel.MESSENGER, external_account_id="pg-gone", actor=owner
    )
    await neutral.release(gone.id, actor=owner)
    _as_member(app, tenant, owner, TenantRole.MEMBER)

    listed = await http.get("/api/v1/channel-connections")
    instagram = await http.get("/api/v1/channel-connections", params={"channel": "instagram"})

    assert listed.status_code == 200, listed.text
    rows = {row["id"]: row for row in listed.json()}
    assert len(rows) == 3, "the number, Instagram and the disabled page; not the released one"
    assert str(gone.id) not in rows
    assert rows[str(kept.id)]["counts_toward_capacity"] is True
    assert (rows[str(paused.id)]["status"], rows[str(paused.id)]["disabled_reason"]) == (
        "disabled",
        "capacity_reduction",
    )
    assert rows[str(paused.id)]["counts_toward_capacity"] is False
    assert [row["id"] for row in instagram.json()] == [str(kept.id)]

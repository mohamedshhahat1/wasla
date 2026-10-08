# ruff: noqa: F811 - the capacity-reduction harness fixtures are imported by name.
"""The small filters a staff screen needs (PLAT-G4..G8; ADR-132).

Through the real ASGI app, as platform staff. Each filter is additive - every
existing parameter keeps its meaning - and each test shows the filter
narrowing to exactly the rows it should, against rows it must leave out.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.channel import Channel
from app.db.models.topup import SELLABLE_TOPUP_ENTITLEMENTS, TopupEntitlement
from app.services.subscription_service import SubscriptionService
from tests.integration.plan_catalogue import own_plan
from tests.integration.test_capacity_reductions import app, http  # noqa: F401 - fixtures
from tests.integration.test_grant_withdrawal import _granted, _staff
from tests.integration.test_platform_billing_api import _act_as as _act_as_user
from tests.integration.topup_harness import base_now, product
from tests.integration.topup_harness import workspace as topup_workspace

pytestmark = pytest.mark.integration

BASE = "/api/v1/platform/billing"


async def test_subscriptions_filter_by_plan_version(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    await own_plan(db_session, code="starter", price=Decimal("0.00"), limits={"agents": 1})
    await own_plan(db_session, code="pro", price=Decimal("0.00"), limits={"agents": 5})
    on_starter, _, _ = await topup_workspace(db_session, now=base_now(), plan_code="starter")
    on_pro, _, _ = await topup_workspace(db_session, now=base_now(), plan_code="pro")
    starter = await SubscriptionService(db_session, tenant_id=on_starter.id).get()
    pro = await SubscriptionService(db_session, tenant_id=on_pro.id).get()
    assert starter is not None and pro is not None
    assert starter.plan_version_id != pro.plan_version_id
    _act_as_user(app, await _staff(db_session))

    async def tenants(**params: Any) -> set[str]:
        response = await http.get(f"{BASE}/subscriptions", params={"limit": 100, **params})
        assert response.status_code == 200, response.text
        return {item["tenant_id"] for item in response.json()["items"]}

    found = await tenants(plan_version_id=str(starter.plan_version_id))
    assert str(on_starter.id) in found and str(on_pro.id) not in found
    # Combined with the filters that were there before.
    assert await tenants(plan_version_id=str(pro.plan_version_id), tenant_id=str(on_pro.id)) == {
        str(on_pro.id)
    }
    assert (
        await tenants(plan_version_id=str(pro.plan_version_id), tenant_id=str(on_starter.id))
        == set()
    )
    assert await tenants(plan_version_id=str(uuid.uuid4())) == set()


async def test_features_marks_the_seven_topup_eligible_keys(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    _act_as_user(app, await _staff(db_session))

    response = await http.get(f"{BASE}/features")

    assert response.status_code == 200, response.text
    eligible = {item["key"] for item in response.json() if item["topup_eligible"]}
    assert eligible == {member.value for member in SELLABLE_TOPUP_ENTITLEMENTS}
    assert len(eligible) == 7
    assert "whatsapp_numbers" not in eligible and "allowed_channel_types" not in eligible
    assert "agents" not in eligible


async def test_audit_logs_filter_by_target_and_page_with_a_stable_cursor(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    target, other = uuid.uuid4(), uuid.uuid4()
    moment = datetime.now(UTC) - timedelta(minutes=5)
    # Seven entries about one product at exactly the same moment, and one about another.
    for index in range(7):
        db_session.add(
            AuditLog(
                action=AuditAction.BILLING_TOPUP_UPDATED,
                actor_kind=AuditActorKind.PLATFORM_STAFF,
                actor_id=staff.id,
                target_type="topup_product",
                target_id=target,
                target_label=f"edit-{index}",
                occurred_at=moment,
            )
        )
    db_session.add(
        AuditLog(
            action=AuditAction.BILLING_TOPUP_UPDATED,
            actor_kind=AuditActorKind.PLATFORM_STAFF,
            actor_id=staff.id,
            target_type="topup_product",
            target_id=other,
            occurred_at=moment,
        )
    )
    await db_session.flush()
    _act_as_user(app, staff)

    seen: list[str] = []
    params: dict[str, Any] = {"target_type": "topup_product", "target_id": str(target), "limit": 3}
    pages = 0
    while True:
        response = await http.get("/api/v1/platform/audit-logs", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        pages += 1
        if not page:
            break
        assert all(item["target_id"] == str(target) for item in page)
        seen += [item["id"] for item in page]
        last = page[-1]
        params = {**params, "before_occurred_at": last["occurred_at"], "before_id": last["id"]}
        assert pages < 10

    assert len(seen) == 7, "no entry skipped"
    assert len(set(seen)) == 7, "no entry repeated"
    assert seen == sorted(seen, reverse=True), "newest first, then by id"
    by_type = await http.get(
        "/api/v1/platform/audit-logs", params={"target_type": "topup_product", "limit": 200}
    )
    assert {item["target_id"] for item in by_type.json()} >= {str(target), str(other)}
    half = await http.get(
        "/api/v1/platform/audit-logs", params={"before_occurred_at": moment.isoformat()}
    )
    assert half.status_code == 422


async def test_topups_filter_by_code_and_search(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    tag = uuid.uuid4().hex[:6]
    slots = await product(
        db_session,
        entitlement=TopupEntitlement.CHANNEL_CONNECTIONS,
        quantity=1,
        code=f"slots-{tag}",
    )
    turns = await product(
        db_session, entitlement=TopupEntitlement.PERIOD_AI_TURNS, quantity=100, code=f"turns-{tag}"
    )
    turns.name = f"Busy Season {tag}"
    await db_session.flush()
    _act_as_user(app, await _staff(db_session))

    async def codes(**params: Any) -> set[str]:
        response = await http.get(f"{BASE}/topups", params={"limit": 100, **params})
        assert response.status_code == 200, response.text
        return {item["code"] for item in response.json()["items"]}

    assert await codes(code=slots.code) == {slots.code}
    assert await codes(code=slots.code.upper()) == {slots.code}
    assert await codes(code=f"slots-{tag}x") == set()
    assert await codes(search=f"SLOTS-{tag}") == {slots.code}
    assert await codes(search=f"season {tag}") == {turns.code}
    assert await codes(search=tag) == {slots.code, turns.code}
    # Wildcards an operator types are literal characters, not patterns.
    assert await codes(search=f"%{tag}") == set()
    assert await codes(search=f"slots_{tag}") == set()
    too_long = await http.get(f"{BASE}/topups", params={"search": "x" * 51})
    assert too_long.status_code == 422


async def test_topup_purchases_filter_by_channel_type(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, _, instagram = await _granted(db_session, staff, channel=Channel.INSTAGRAM)
    _, _, messenger = await _granted(db_session, staff, channel=Channel.MESSENGER, tenant=tenant)
    _, _, general = await _granted(db_session, staff, tenant=tenant)
    _act_as_user(app, staff)

    async def ids(**params: Any) -> set[str]:
        response = await http.get(
            f"{BASE}/topup-purchases", params={"tenant_id": str(tenant.id), **params}
        )
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["items"]}

    assert await ids() == {str(instagram), str(messenger), str(general)}
    assert await ids(channel_type="instagram") == {str(instagram)}
    assert await ids(channel_type="messenger", source="platform_grant") == {str(messenger)}
    assert await ids(channel_type="telegram") == set()
    assert (
        await http.get(f"{BASE}/topup-purchases", params={"channel_type": "fax"})
    ).status_code == 422

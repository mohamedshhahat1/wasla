# ruff: noqa: F811 - the workspace lifecycle fixtures are imported by name.
"""What the campaigns page may show about a number's messaging limit (LIM-07).

Aggregates of the whole business portfolio - every number, every workspace on
it - and nothing that names another workspace. A member may read it; another
workspace's number is not found.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TenantRole
from app.db.models.business_initiated import BusinessInitiatedSend
from app.db.models.messaging_limit import WhatsAppBusinessPortfolio
from app.db.models.whatsapp import WhatsAppAccount
from tests.integration.test_workspace_lifecycle import (  # noqa: F401 - fixtures
    API,
    _join,
    _login,
    _select_workspace,
    _user,
    _workspace,
    app,
    http,
    lifecycle_settings,
)

pytestmark = [pytest.mark.integration, pytest.mark.messaging_limit]


async def _number(
    session: AsyncSession, tenant_id: uuid.UUID, portfolio: WhatsAppBusinessPortfolio
) -> WhatsAppAccount:
    suffix = uuid.uuid4().hex[:10]
    account = WhatsAppAccount(
        tenant_id=tenant_id,
        phone_number_id=f"pn-{suffix}",
        waba_id=portfolio.meta_key,
        display_phone_number="+201000000000",
        portfolio_id=portfolio.id,
    )
    session.add(account)
    await session.flush()
    return account


async def test_a_member_reads_the_portfolio_aggregate_and_nothing_of_another_workspace(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _user(db_session, email="limit-owner@example.com")
    member = await _user(db_session, email="limit-member@example.com")
    neighbour = await _user(db_session, email="limit-neighbour@example.com")
    tenant = await _workspace(db_session, slug="limit-mine", owner=owner)
    other = await _workspace(db_session, slug="limit-theirs", owner=neighbour)
    await _join(db_session, tenant=tenant, user=member, role=TenantRole.MEMBER)
    now = datetime.now(UTC)
    portfolio = WhatsAppBusinessPortfolio(
        key_kind="waba",
        meta_key=f"waba-{uuid.uuid4().hex[:8]}",
        tier="TIER_250",
        messaging_limit=250,
        source="api",
        checked_at=now,
        changed_at=now,
    )
    db_session.add(portfolio)
    await db_session.flush()
    mine = await _number(db_session, tenant.id, portfolio)
    theirs = await _number(db_session, other.id, portfolio)
    for index in range(150):
        db_session.add(
            BusinessInitiatedSend(
                portfolio_id=portfolio.id,
                recipient=f"20100{index:05d}",
                sent_at=now - timedelta(hours=2) + timedelta(seconds=index),
                source="campaign" if index % 2 else "follow_up",
                tenant_id=tenant.id if index < 100 else other.id,
            )
        )
    db_session.add(
        BusinessInitiatedSend(
            portfolio_id=portfolio.id,
            recipient="20100099999",
            sent_at=now - timedelta(hours=30),
            source="campaign",
            tenant_id=tenant.id,
        )
    )
    await db_session.flush()

    headers = await _select_workspace(http, await _login(http, member.email), "limit-mine")
    response = await http.get(f"{API}/whatsapp/accounts/{mine.id}/messaging-limit", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "TIER_250" and body["messaging_limit"] == 250 and body["known"]
    assert body["campaign_share"] == 0.8
    assert body["campaign_budget"] == 200
    assert body["used_last_24h"] == 150, "the whole portfolio, both workspaces, not the old row"
    assert body["campaign_available_now"] == 50
    assert body["next_capacity_at"] is not None
    assert body["next_capacity_at_local"] is not None
    assert body["shared_with_other_numbers"] is True
    assert body["shared_with_other_workspaces"] is True
    flattened = response.text
    for secret in (str(other.id), other.slug, str(theirs.id), theirs.phone_number_id):
        assert secret not in flattened

    missing = await http.get(
        f"{API}/whatsapp/accounts/{theirs.id}/messaging-limit", headers=headers
    )
    assert missing.status_code == 404


async def test_a_number_never_read_says_so(http: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _user(db_session, email="limit-unknown@example.com")
    tenant = await _workspace(db_session, slug="limit-unknown", owner=owner)
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"pn-{uuid.uuid4().hex[:10]}",
        waba_id="waba-never",
        display_phone_number="+201000000000",
    )
    db_session.add(account)
    await db_session.flush()
    headers = await _select_workspace(http, await _login(http, owner.email), "limit-unknown")

    body = (
        await http.get(f"{API}/whatsapp/accounts/{account.id}/messaging-limit", headers=headers)
    ).json()

    assert body["known"] is False
    assert body["messaging_limit"] is None and body["campaign_budget"] is None
    assert body["campaign_available_now"] is None
    assert body["used_last_24h"] == 0

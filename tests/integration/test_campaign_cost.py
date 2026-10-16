"""Rate cards entered by platform staff, and a campaign's cost estimate (COST-01, COST-02)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update

from app.api.dependencies import CurrentUser, get_current_user
from app.core.security import TokenClaims, TokenType
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.campaign import CampaignRecipient, RecipientStatus
from app.db.models.enums import PlatformRole, TenantRole
from app.db.models.rate_card import WhatsAppRateCard
from app.db.models.user import User
from app.db.session import Database
from tests.integration.campaign_api import API, client_for
from tests.integration.campaign_harness import (
    USER_DOMAIN,
    FakeMeta,
    compose,
    erase,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parents[1] / "fixtures" / "whatsapp_rate_card_usd_2026-10-01.csv"
CARDS = f"{API}/platform/whatsapp-rate-cards"


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    _, built = worker(prepared_database, FakeMeta(), name="cost")
    await quiesce(built)
    try:
        yield built
    finally:
        async with built.session_factory() as session:
            await session.execute(
                delete(AuditLog).where(
                    AuditLog.action.in_(
                        [
                            AuditAction.RATE_CARD_UPLOADED,
                            AuditAction.RATE_CARD_ACTIVATED,
                            AuditAction.PLATFORM_BILLING_READ,
                        ]
                    )
                )
            )
            await session.execute(delete(WhatsAppRateCard))
            await session.commit()
        await quiesce(built)
        await erase(built)
        await built.dispose()


def _unique(prefix: str) -> list[str]:
    """Numbers starting `prefix`, unique to this run (contacts' numbers are unique)."""
    stem = f"{uuid.uuid4().int % 10**6:06d}"
    return [f"{prefix}{stem}"]


async def _staff(
    database: Database, *, role: PlatformRole | None = PlatformRole.PLATFORM_ADMIN
) -> dict[Any, Any]:
    async with database.session_factory() as session:
        user = User(
            email=f"staff-{uuid.uuid4().hex[:8]}{USER_DOMAIN}",
            is_active=True,
            email_verified_at=datetime.now(UTC),
            platform_role=role,
        )
        session.add(user)
        await session.commit()
    claims = TokenClaims(
        subject=user.id,
        token_type=TokenType.ACCESS,
        token_id=uuid.uuid4(),
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tenant_id=None,
    )
    return {get_current_user: lambda: CurrentUser(user=user, claims=claims)}


def _card(content: bytes | None = None, **query: str) -> dict[str, Any]:
    return {
        "files": {"file": ("usd.csv", content or FIXTURE.read_bytes(), "text/csv")},
        "params": {"currency": "USD", "effective_from": "2026-10-01", **query},
    }


async def test_staff_upload_preview_and_activate_a_card(database: Database) -> None:
    workspace = await seed(database, recipients=1)
    overrides = await _staff(database)
    async with client_for(database, workspace, overrides=overrides) as staff:
        uploaded = await staff.post(CARDS, **_card())
        assert uploaded.status_code == 201, uploaded.text
        card = uploaded.json()
        assert card["activated_at"] is None
        assert card["markets"] >= 40
        egypt = next(
            r for r in card["rates"] if r["market"] == "Egypt" and r["category"] == "marketing"
        )
        assert egypt["rate"] == "0.064400"
        assert card["changes_from_active"] == []

        activated = await staff.post(f"{CARDS}/{card['id']}/activate")
        assert activated.status_code == 200 and activated.json()["activated_at"] is not None

        changed = FIXTURE.read_bytes().replace(b"Egypt,USD,0.0644", b"Egypt,USD,0.0700")
        second = (await staff.post(CARDS, **_card(changed, effective_from="2027-01-01"))).json()
        assert second["changes_from_active"] == [
            {"market": "Egypt", "category": "marketing", "old": "0.064400", "new": "0.070000"}
        ]
        listed = (await staff.get(CARDS)).json()
        assert [item["id"] for item in listed][:2] == [second["id"], card["id"]]
        one = await staff.get(f"{CARDS}/{card['id']}")
        assert one.status_code == 200 and len(one.json()["rates"]) == len(card["rates"])
        async with database.session_factory() as session:
            reads = await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == AuditAction.PLATFORM_BILLING_READ)
                .where(AuditLog.meta["resource"].astext.like("billing.rate_card%"))
            )
        assert reads == 2

        malformed = await staff.post(
            CARDS, **_card(b"Market,Currency,Marketing\nEgypt,USD,cheap\n")
        )
        assert malformed.status_code == 422
        assert malformed.json()["error"]["code"] == "rate_card_invalid"
        assert malformed.json()["error"]["details"]["line"] == 2
        no_other = await staff.post(CARDS, **_card(b"Market,Currency,Marketing\nEgypt,USD,0.06\n"))
        assert no_other.status_code == 422

    # Somebody signed in with no platform role - whatever their workspace role.
    ordinary = await _staff(database, role=None)
    async with client_for(database, workspace, overrides=ordinary) as workspace_user:
        assert (await workspace_user.get(CARDS)).status_code == 403
        assert (await workspace_user.post(CARDS, **_card())).status_code == 403


async def test_an_estimate_prices_each_market_exactly_or_says_there_is_no_card(
    database: Database,
) -> None:
    workspace = await seed(
        database,
        recipients=5,
        numbers=[
            *_unique("2010011"),
            *_unique("2010022"),
            *_unique("9665011"),
            *_unique("999123"),
            *_unique("2010033"),
        ],
    )
    campaign = await compose(database, workspace, schedule=False)
    async with database.session_factory() as session:
        await session.execute(
            update(CampaignRecipient)
            .where(CampaignRecipient.contact_id == workspace.contact_ids[4])
            .values(status=RecipientStatus.SKIPPED, reason_code="opted_out")
        )
        await session.commit()

    async with client_for(database, workspace) as admin:
        none = await admin.get(f"{API}/campaigns/{campaign}/cost-estimate")
        assert none.status_code == 200, none.text
        assert none.json()["total"] is None and none.json()["reason"] == "no_rate_card"

    overrides = await _staff(database)
    async with client_for(database, workspace, overrides=overrides) as staff:
        card = (await staff.post(CARDS, **_card())).json()
        await staff.post(f"{CARDS}/{card['id']}/activate")

    async with client_for(database, workspace) as admin:
        estimate = (await admin.get(f"{API}/campaigns/{campaign}/cost-estimate")).json()
    lines = {line["market"]: line for line in estimate["per_market"]}
    assert lines["Egypt"]["recipients"] == 2
    assert Decimal(lines["Egypt"]["subtotal"]) == Decimal("0.1288")
    assert Decimal(lines["Saudi Arabia"]["subtotal"]) == Decimal("0.0576")
    assert Decimal(lines["Other"]["subtotal"]) == Decimal("0.0604")
    assert Decimal(estimate["total"]) == Decimal("0.2468")
    assert estimate["currency"] == "USD" and estimate["label"] == "estimate"
    assert estimate["recipients_counted"] == 4
    assert estimate["excluded"] == {"opted_out": 1}
    assert estimate["rate_card"]["id"] == card["id"]
    assert any("Wasla does not charge" in item for item in estimate["assumptions"])

    async with client_for(database, workspace, role=TenantRole.MEMBER) as member:
        assert (await member.get(f"{API}/campaigns/{campaign}/cost-estimate")).status_code == 403


async def test_before_the_audience_the_eligible_people_are_priced(database: Database) -> None:
    workspace = await seed(
        database,
        recipients=3,
        numbers=[*_unique("2010011"), *_unique("2010022"), *_unique("9665011")],
    )
    overrides = await _staff(database)
    async with client_for(database, workspace, overrides=overrides) as staff:
        card = (await staff.post(CARDS, **_card())).json()
        await staff.post(f"{CARDS}/{card['id']}/activate")
    async with client_for(database, workspace) as admin:
        created = await admin.post(
            f"{API}/campaigns",
            json={
                "account_id": str(workspace.account_id),
                "template_id": str(workspace.template_id),
                "name": "Draft",
            },
        )
        estimate = (await admin.get(f"{API}/campaigns/{created.json()['id']}/cost-estimate")).json()
    assert estimate["recipients_counted"] == 3
    assert Decimal(estimate["total"]) == Decimal("0.0644") * 2 + Decimal("0.0576")

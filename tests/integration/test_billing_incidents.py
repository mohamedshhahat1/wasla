"""Billing incidents are durable, deduplicated and counted (BILL-14, BILL-15).

The alerts on duplicate payments, mismatched callbacks and permanent charge
failures read `wasla_billing_incidents_total`. An incident written to the table
but never counted would sit in the operator queue while every alert stayed
quiet, which is the silence BILL-14 was about (mutation R-BILL-14).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import telemetry
from app.db.models.billing_incident import BillingIncident, BillingIncidentKind
from app.db.models.tenant import Tenant
from app.services.billing_incident_service import raise_incident

pytestmark = pytest.mark.integration


async def test_an_incident_is_recorded_once_and_counted_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    counted: list[str] = []

    async def record(kind: str) -> None:
        counted.append(kind)

    monkeypatch.setattr(telemetry, "record_billing_incident", record)
    tenant = Tenant(name="Incidents", slug=f"incidents-{uuid.uuid4().hex[:10]}")
    db_session.add(tenant)
    await db_session.flush()

    for _ in range(2):
        await raise_incident(
            db_session,
            kind=BillingIncidentKind.DUPLICATE_PAYMENT,
            dedupe_key=f"payment:{tenant.id}:txn-1",
            tenant_id=tenant.id,
            provider="paymob",
            provider_transaction_id="txn-1",
            amount=Decimal("99.00"),
            currency="EGP",
            detail="A second success for a paid invoice.",
        )

    rows = await db_session.scalar(
        select(func.count())
        .select_from(BillingIncident)
        .where(BillingIncident.tenant_id == tenant.id)
    )
    assert rows == 1, "the same fact is one incident, however often it is reported"
    assert counted == ["duplicate_payment"], "and one sample for the alert to fire on"

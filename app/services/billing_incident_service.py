"""Raising and resolving billing incidents - see `app.db.models.billing_incident`.

Raised in the transaction that made the decision, so the incident and the
decision commit together or not at all. Idempotent on a dedupe key the raiser
builds from the identifiers of what happened: a callback delivered three times
raises one incident, and the unique constraint rather than a read decides a
race between two deliveries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.billing_incident import (
    MAX_INCIDENT_DETAIL_LENGTH,
    MAX_INCIDENT_KEY_LENGTH,
    BillingIncident,
    BillingIncidentKind,
    BillingIncidentStatus,
)

logger = get_logger(__name__)


async def raise_incident(
    session: AsyncSession,
    *,
    kind: BillingIncidentKind,
    dedupe_key: str,
    tenant_id: uuid.UUID | None,
    payment_id: uuid.UUID | None = None,
    invoice_id: uuid.UUID | None = None,
    provider: str | None = None,
    provider_transaction_id: str | None = None,
    amount: Decimal | None = None,
    currency: str | None = None,
    detail: str | None = None,
    resolved: bool = False,
    now: datetime | None = None,
) -> BillingIncident:
    """Record one incident, or return the one already recorded for this fact.

    `resolved=True` is for the incidents that document something already put
    right - a payment recovered by reconciliation - so they are visible to an
    operator without sitting in the open queue.
    """
    key = f"{kind.value}:{dedupe_key}"[:MAX_INCIDENT_KEY_LENGTH]
    moment = now if now is not None else datetime.now(UTC)
    incident = BillingIncident(
        tenant_id=tenant_id,
        kind=kind,
        status=BillingIncidentStatus.RESOLVED if resolved else BillingIncidentStatus.OPEN,
        dedupe_key=key,
        payment_id=payment_id,
        invoice_id=invoice_id,
        provider=provider,
        provider_transaction_id=provider_transaction_id,
        amount=amount,
        currency=currency,
        detail=detail[:MAX_INCIDENT_DETAIL_LENGTH] if detail else None,
        resolved_at=moment if resolved else None,
    )
    try:
        async with session.begin_nested():
            session.add(incident)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(BillingIncident).where(BillingIncident.dedupe_key == key)
        )
        if existing is None:  # pragma: no cover - the row that just blocked us
            raise
        return existing

    # Imported here so `core.telemetry` stays importable without services.
    from app.core.telemetry import record_billing_incident

    await record_billing_incident(kind.value)
    logger.warning(
        "billing.incident_raised",
        extra={
            "event": "billing.incident_raised",
            "kind": kind.value,
            "tenant_id": str(tenant_id) if tenant_id else None,
            "payment_id": str(payment_id) if payment_id else None,
            "invoice_id": str(invoice_id) if invoice_id else None,
            "resolved": resolved,
        },
    )
    return incident


__all__ = ["raise_incident"]

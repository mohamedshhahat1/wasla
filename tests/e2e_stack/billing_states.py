"""Put an e2e workspace into a billing state the API cannot reach in a few calls (FE-8, EC8-04).

    python -m tests.e2e_stack.billing_states saved-card <slug>   a saved card, the default
    python -m tests.e2e_stack.billing_states refunded <slug>     a paid checkout, refunded

Test-only, like the rest of ``tests.e2e_stack``: nothing under ``app/`` imports it
(``tests/unit/test_e2e_gate_states.py`` holds that for the whole package). The
journeys run it with ``docker exec wasla-fe-e2e-api``. Every other billing state
FE-8 draws is reached through the API and the fake Paymob page: a plan bought
(``outcome=paid``), declined, pending or confirmed late; a scheduled downgrade and a
cancellation at period end (the owner's own routes); a renewal owed
(``gate_states past-due``); a custom offer (the platform's own route, as staff).

A card is saved only by a TOKEN callback, which the fake page does not send, so it is
written here - sealed with the deployment's own key, exactly as the callback would
seal it, with the fake's last four (``2346``). Each command prints one JSON line
read back from the rows it wrote.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.core.config import Settings

USAGE = "usage: python -m tests.e2e_stack.billing_states (saved-card | refunded) <slug>"
COMMANDS = frozenset({"saved-card", "refunded"})


async def _tenant_id(session: AsyncSession, slug: str) -> uuid.UUID:
    from app.db.models import Tenant

    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        raise SystemExit(f"no workspace with the slug {slug!r}")
    return tenant.id


async def saved_card(
    session: AsyncSession, slug: str, settings: Settings | None = None
) -> dict[str, object]:
    """One active card, the workspace's default: brand MasterCard, last four 2346.

    Sealed with the deployment's own keys; a test passes its own settings.
    """
    from app.core.config import get_settings
    from app.db.models.payment_method import PaymentMethod, PaymentMethodStatus
    from app.services.payment_token_service import PaymentTokenProtector

    tenant_id = await _tenant_id(session, slug)
    method_id = uuid.uuid4()
    sealed = PaymentTokenProtector.from_settings(settings or get_settings()).seal(
        f"e2e-card-token-{method_id.hex}",
        provider="paymob",
        tenant_id=tenant_id,
        method_id=method_id,
    )
    existing = await session.scalars(
        select(PaymentMethod).where(
            PaymentMethod.tenant_id == tenant_id, PaymentMethod.is_default.is_(True)
        )
    )
    for method in existing:
        method.is_default = False
    session.add(
        PaymentMethod(
            id=method_id,
            tenant_id=tenant_id,
            provider="paymob",
            provider_token=sealed.ciphertext,
            token_fingerprint=sealed.fingerprint,
            masked_pan="2346",
            brand="MasterCard",
            status=PaymentMethodStatus.ACTIVE,
            is_default=True,
        )
    )
    await session.flush()
    return {"payment_method_id": str(method_id), "masked_pan": "2346", "is_default": True}


async def refunded(session: AsyncSession, slug: str) -> dict[str, object]:
    """A paid checkout invoice whose one payment was refunded in full."""
    from app.db.models.invoice import (
        Invoice,
        InvoicePurpose,
        InvoiceStatus,
        Payment,
        PaymentStatus,
    )

    tenant_id = await _tenant_id(session, slug)
    now = datetime.now(UTC)
    invoice = Invoice(
        tenant_id=tenant_id,
        status=InvoiceStatus.PAID,
        purpose=InvoicePurpose.CHECKOUT,
        plan_code="pro",
        amount_due=Decimal("99.00"),
        amount_paid=Decimal("99.00"),
        currency="EGP",
        period_start=now - timedelta(days=3),
        period_end=now + timedelta(days=27),
        lines=[],
        paid_at=now - timedelta(days=3),
        issued_at=now - timedelta(days=3),
    )
    session.add(invoice)
    await session.flush()
    payment = Payment(
        tenant_id=tenant_id,
        invoice_id=invoice.id,
        status=PaymentStatus.REFUNDED,
        amount=Decimal("99.00"),
        currency="EGP",
        provider="paymob",
        provider_reference=str(190_000_000 + now.microsecond),
        refunded_amount=Decimal("99.00"),
        refunded_at=now,
        processed_at=now - timedelta(days=3),
    )
    session.add(payment)
    await session.flush()
    return {"invoice_id": str(invoice.id), "payment_id": str(payment.id), "status": "refunded"}


def parse(argv: list[str]) -> tuple[str, str]:
    """(command, slug) or the usage, before any connection is opened."""
    if len(argv) == 2 and argv[0] in COMMANDS:
        return argv[0], argv[1]
    raise SystemExit(USAGE)


async def run(argv: list[str]) -> dict[str, object]:
    from app.core.config import get_settings
    from app.db.session import Database

    command, slug = parse(argv)
    database = Database(get_settings())
    try:
        async with database.session() as session:
            result = await (saved_card if command == "saved-card" else refunded)(session, slug)
            await session.commit()
        return result
    finally:
        await database.dispose()


def main(argv: list[str]) -> None:
    print(json.dumps(asyncio.run(run(argv))))  # noqa: T201 - this script's output is its interface


if __name__ == "__main__":
    main(sys.argv[1:])

"""Collection by a human, which is how most business-to-business billing works.

This is not a stub or a test double. A great many SaaS companies invoice their
larger customers and get paid by bank transfer weeks later, and this provider is
the honest model of that: it records that an invoice is awaiting payment and
never claims to have collected anything.

That is why `charge` returns `PENDING` rather than `SUCCEEDED`. A provider that
pretended a transfer had arrived would put a paid invoice in front of a finance
team that has not paid, which is worse than no billing at all. Marking it paid
is a deliberate act by somebody who has seen the money, through the platform
API — and that act is recorded as its own payment row.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from app.core.logging import get_logger
from app.db.models.invoice import PaymentStatus
from app.integrations.billing.base import ChargeOutcome

logger = get_logger(__name__)

MANUAL_PROVIDER: Final = "manual"


class ManualProvider:
    """Records what is owed and waits for a person to confirm payment."""

    @property
    def name(self) -> str:
        return MANUAL_PROVIDER

    async def charge(
        self,
        *,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        description: str,
    ) -> ChargeOutcome:
        """Record the attempt as awaiting a human.

        No network call, and nothing to fail. The idempotency key is still
        returned as the reference so the payment row can be matched to this
        invoice exactly as it would be to a processor's charge id.
        """
        logger.info(
            "billing.manual_charge_recorded",
            extra={
                "event": "billing.manual_charge_recorded",
                "amount": str(amount),
                "currency": currency,
                "reference": idempotency_key,
            },
        )
        return ChargeOutcome(
            status=PaymentStatus.PENDING,
            amount=amount,
            reference=idempotency_key,
        )

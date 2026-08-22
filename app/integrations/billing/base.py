"""What a payment provider must do, and nothing more.

The surface is deliberately tiny. Every processor has an enormous API, and
adopting any of it into the domain is how a system ends up unable to change
processors: the moment a service knows what a "payment intent" or a "charge
capture" is, it is that provider's system rather than ours.

So a provider does one thing — try to collect an amount for an invoice, and say
what happened. Subscriptions, plans, periods and entitlements are Wasla's, not
the provider's, because they are what the product means and a processor's model
of them is always subtly different.

Two rules the implementations must keep:

**A charge is idempotent on the reference we give it.** Every provider supports
this, and the alternative is charging a customer twice when a request times out
and is retried.

**A refusal is a result, not an exception.** A declined card is an ordinary
outcome that produces a `Payment` row and a message for the customer. Exceptions
are for the provider being unreachable or misconfigured, which is our problem
rather than theirs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.core.exceptions import ExternalServiceError
from app.db.models.invoice import PaymentStatus


class ProviderError(ExternalServiceError):
    """The provider could not be reached or is misconfigured.

    Not raised for a declined payment: that is a `ChargeOutcome`, because it is
    an answer rather than a failure to get one.
    """

    message = "The payment provider could not be reached."


@dataclass(frozen=True, slots=True)
class ChargeOutcome:
    """What came back from an attempt to collect.

    `reference` is the provider's own identifier for the attempt, stored so a
    dispute can be traced to their records - and so a retry with the same
    idempotency key can be recognised rather than charged again.
    """

    status: PaymentStatus
    amount: Decimal
    reference: str | None = None
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is PaymentStatus.SUCCEEDED


@runtime_checkable
class PaymentProvider(Protocol):
    """The one thing Wasla asks of a payment processor."""

    @property
    def name(self) -> str:
        """Stored on the invoice and the payment, so a row says who handled it."""
        ...

    async def charge(
        self,
        *,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
        description: str,
    ) -> ChargeOutcome:
        """Attempt to collect `amount`.

        `idempotency_key` is ours and is stable for the attempt: calling twice
        with the same key must collect once. A decline returns an outcome; only
        an unreachable or misconfigured provider raises.
        """
        ...

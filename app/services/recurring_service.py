"""Collecting a renewal from a card already on file.

The half of recurring billing that happens without anybody watching. A period
ends, the sweep issues an invoice, and this decides whether that invoice can be
taken from a saved card instead of emailed to somebody and waited on.

Four refusals stand between a due invoice and a charge, and each one is a way
this could otherwise take money it should not:

1. **The provider must be able to.** Charging a saved card is a capability the
   processor gates per merchant. Without it, nothing here runs and renewals are
   collected the way they were before - an invoice and an email.
2. **The subscription must still be live.** A cancelled or expired workspace is
   never charged. This is the one that matters most: an automatic debit against
   somebody who has left is the worst thing a billing system can do.
3. **There must be a card.** An active, default, unrevoked one.
4. **The attempt budget must not be spent.** A card that has declined three
   times is not going to work on the fourth, and a processor treats a merchant
   that keeps trying as one to look at.

The charge itself settles nothing. Like every other payment here, the outcome
arrives on a signed callback, and `CheckoutService.apply` decides what it
means - so an automatic renewal and a customer paying a link converge on
exactly the same settlement path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.billing import Subscription
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.integrations.billing.base import ProviderError
from app.integrations.billing.checkout import (
    RecurringProvider,
    RecurringUnavailableError,
    SavedMethodCharge,
)
from app.repositories.invoice_repository import InvoiceRepository, PaymentRepository
from app.repositories.payment_method_repository import PaymentMethodRepository

logger = get_logger(__name__)

# How many times a card is asked for one invoice before the platform stops and
# leaves it to a person. Three because a decline is usually a fact about the
# card rather than a moment - expired, blocked, empty - and a processor reads a
# merchant that retries indefinitely as one worth investigating.
MAX_COLLECTION_ATTEMPTS: Final = 3

# Spacing between attempts. Widening rather than fixed, because the commonest
# recoverable decline is a temporary funds problem and a customer needs days
# rather than minutes to fix it.
RETRY_BACKOFF: Final[tuple[timedelta, ...]] = (
    timedelta(days=1),
    timedelta(days=3),
)


@dataclass(frozen=True, slots=True)
class CollectionOutcome:
    """What one attempt at an automatic renewal did.

    `charged` means a request reached the processor, not that money moved -
    that is decided later by the callback. `reason` is why nothing was
    attempted, and is the field an operator reads when a renewal did not go
    out.
    """

    charged: bool
    reason: str | None = None
    payment_id: uuid.UUID | None = None


# Reasons an invoice was passed over. A closed vocabulary because they are
# counted and logged rather than read as prose.
NO_PROVIDER: Final = "no_provider"
NOT_SUPPORTED: Final = "not_supported"
NOT_SERVING: Final = "not_serving"
NO_CARD: Final = "no_card"
NOT_COLLECTIBLE: Final = "not_collectible"
ATTEMPTS_EXHAUSTED: Final = "attempts_exhausted"
NOT_DUE: Final = "not_due"
PROVIDER_REFUSED: Final = "provider_refused"


class RecurringService:
    """Automatic collection for one workspace."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider: RecurringProvider | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._provider = provider
        self._invoices = InvoiceRepository(session, tenant_id=tenant_id)
        self._payments = PaymentRepository(session, tenant_id=tenant_id)
        self._methods = PaymentMethodRepository(session, tenant_id=tenant_id)

    @property
    def available(self) -> bool:
        """Whether this deployment can debit a saved card at all.

        False is a supported state, not a fault: the product bills by invoice
        when it is, which is how it billed before saved cards existed.
        """
        return self._provider is not None and self._provider.can_charge_saved_methods

    async def collect(
        self,
        invoice: Invoice,
        *,
        subscription: Subscription | None,
        now: datetime | None = None,
    ) -> CollectionOutcome:
        """Try to take one outstanding invoice from the workspace's card."""
        moment = now if now is not None else datetime.now(UTC)

        refusal = self._refusal(invoice, subscription=subscription, now=moment)
        if refusal is not None:
            return CollectionOutcome(charged=False, reason=refusal)

        method = await self._methods.default_method()
        if method is None:
            return CollectionOutcome(charged=False, reason=NO_CARD)

        # `_refusal` returned None, which it only does when a provider is
        # present and able. Read into a local so the narrowing survives without
        # an `assert`, which vanishes under -O.
        provider = self._provider
        if provider is None:  # pragma: no cover - guarded by `_refusal`
            return CollectionOutcome(charged=False, reason=NO_PROVIDER)

        payment = await self._claim_attempt(invoice, method_id=method.id, now=moment)
        if payment is None:
            return CollectionOutcome(charged=False, reason=NOT_DUE)

        try:
            reference = await provider.charge_saved_method(
                SavedMethodCharge(
                    # Our id, quoted home by the callback, exactly as at
                    # checkout. The settlement path does not know or care that
                    # nobody was watching.
                    reference=str(payment.id),
                    token=method.provider_token,
                    amount=payment.amount,
                    currency=payment.currency,
                    description=f"{invoice.plan_code} plan",
                )
            )
        except RecurringUnavailableError:
            # The account cannot do this after all. The attempt is rolled back
            # to `pending` with no provider reference, so nothing looks charged.
            payment.failure_reason = "Automatic collection is not enabled for this account."
            return CollectionOutcome(charged=False, reason=NOT_SUPPORTED, payment_id=payment.id)
        except ProviderError as error:
            payment.status = PaymentStatus.FAILED
            payment.failure_reason = "The provider refused the automatic charge."
            payment.processed_at = moment
            logger.warning(
                "billing.recurring_charge_failed",
                extra={
                    "event": "billing.recurring_charge_failed",
                    "tenant_id": str(self._tenant_id),
                    "invoice_id": str(invoice.id),
                    "payment_id": str(payment.id),
                    "retryable": error.retryable,
                },
            )
            return CollectionOutcome(
                charged=False,
                reason=PROVIDER_REFUSED,
                payment_id=payment.id,
            )

        payment.provider_intent_reference = reference
        await self._session.flush()

        logger.info(
            "billing.recurring_charge_requested",
            extra={
                "event": "billing.recurring_charge_requested",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice.id),
                "payment_id": str(payment.id),
                "attempt": invoice.collection_attempts,
                "amount": str(payment.amount),
                "currency": payment.currency,
            },
        )
        return CollectionOutcome(charged=True, payment_id=payment.id)

    def _refusal(
        self,
        invoice: Invoice,
        *,
        subscription: Subscription | None,
        now: datetime,
    ) -> str | None:
        """Why this invoice must not be charged, or None to go ahead."""
        if self._provider is None:
            return NO_PROVIDER
        if not self._provider.can_charge_saved_methods:
            return NOT_SUPPORTED
        if invoice.status is not InvoiceStatus.OPEN or invoice.outstanding <= 0:
            return NOT_COLLECTIBLE
        if subscription is None or not subscription.is_serving:
            # The refusal that matters most. A cancelled or expired workspace
            # is never debited, whatever the invoice says - taking money from
            # somebody who has left is not a billing error, it is the thing
            # customers never forgive.
            return NOT_SERVING
        if invoice.collection_attempts >= MAX_COLLECTION_ATTEMPTS:
            return ATTEMPTS_EXHAUSTED
        if invoice.next_collection_at is not None and invoice.next_collection_at > now:
            return NOT_DUE
        return None

    async def _claim_attempt(
        self,
        invoice: Invoice,
        *,
        method_id: uuid.UUID,
        now: datetime,
    ) -> Payment | None:
        """Take this attempt, or discover that another worker already has.

        The claim is a payment row whose idempotency key names the invoice and
        the attempt number, so two workers sweeping at once cannot both charge:
        one inserts and the other hits `UNIQUE(tenant_id, idempotency_key)`.
        Counting the attempt *before* the provider is called is deliberate - a
        request that times out has still been made, and a scheme counts it.
        """
        attempt = invoice.collection_attempts + 1
        key = f"auto:{invoice.id}:{attempt}"

        try:
            async with self._session.begin_nested():
                payment = self._payments.record(
                    invoice_id=invoice.id,
                    status=PaymentStatus.PENDING,
                    amount=invoice.outstanding,
                    currency=invoice.currency,
                    provider=self._provider.name if self._provider else "unknown",
                    provider_reference=None,
                    idempotency_key=key,
                )
                payment.is_automatic = True
                payment.payment_method_id = method_id
                await self._session.flush()
        except IntegrityError:
            logger.info(
                "billing.recurring_attempt_already_claimed",
                extra={
                    "event": "billing.recurring_attempt_already_claimed",
                    "invoice_id": str(invoice.id),
                    "attempt": attempt,
                },
            )
            return None

        invoice.collection_attempts = attempt
        invoice.next_collection_at = self._next_attempt_at(attempt, now=now)
        await self._session.flush()
        return payment

    @staticmethod
    def _next_attempt_at(attempt: int, *, now: datetime) -> datetime | None:
        """When to try again, or None once the budget is spent.

        None rather than a far-future date, so "we have stopped trying" is a
        state an operator can query for rather than infer from arithmetic.
        """
        if attempt >= MAX_COLLECTION_ATTEMPTS:
            return None
        return now + RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]

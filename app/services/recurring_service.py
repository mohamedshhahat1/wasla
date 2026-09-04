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

## The order the writes happen in

This used to be one transaction: claim, insert the payment, count the attempt,
call Paymob, commit. A process that stopped existing between the call and the
commit had moved a customer's money while PostgreSQL rolled back every record
that it had, and the next sweep read an untouched invoice and charged the same
card again (WSL-01). Neither guard helped: `SKIP LOCKED` protects against a
second worker rather than a dying one, and the idempotency key lived on the row
that was rolled back.

So it is three transactions now, and the commits between them are the design
rather than an implementation detail (ADR-088):

    TX1   claim the invoice, insert the attempt, count it        -> COMMIT
    TX2   mark it requested                                      -> COMMIT
    --    call Paymob. No transaction open, no connection held,
          no row locked.
    TX3   record what came back                                  -> COMMIT

**TX2 is what makes the difference between the two crash windows nameable.**
Before it commits, a dying worker leaves an attempt nobody has been asked
about, which is safe to close and hand its budget back. After it commits, a
dying worker leaves an attempt that may have taken money, which nothing may
touch until a callback or a lookup says what happened. Those are opposite
answers, and the only thing separating them is a write that happens before the
request is built.

The remaining gap - dying between TX2's commit and the socket - resolves the
safe way by construction: the attempt says a charge may have happened when it
did not, and reconciliation discovers that from the provider rather than
assuming it.
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
from app.db.models.invoice import (
    CollectionState,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.integrations.billing.base import ProviderError
from app.integrations.billing.checkout import (
    ChargeNotSentError,
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
# The provider was reached and would not accept the request, before anything
# money-moving was sent. Distinct from `PROVIDER_REFUSED` only in being
# transient: the attempt is given back rather than spent.
NOT_SENT: Final = "not_sent"
# No answer came back. **Not a failure** - the difference between this and
# `PROVIDER_REFUSED` is the difference between an invoice that waits and a card
# that is charged twice.
OUTCOME_UNKNOWN: Final = "outcome_unknown"


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

        # TX1 is durable from here. Everything below reads the payment through
        # its own identity rather than through the invoice, because the invoice
        # row is a snapshot from before these commits and nothing after this
        # point needs it.
        payment_id = payment.id
        attempt = invoice.collection_attempts
        charge = SavedMethodCharge(
            # Our id, quoted home by the callback, exactly as at checkout, and
            # committed before this object exists. The settlement path does not
            # know or care that nobody was watching, and neither does
            # reconciliation - the name is the same either way, and it does not
            # change if this process is replaced.
            reference=str(payment_id),
            token=method.provider_token,
            amount=payment.amount,
            currency=payment.currency,
            description=f"{invoice.plan_code} plan",
        )

        # TX2. "A charge may have been sent for this invoice" becomes a fact
        # PostgreSQL holds, before anything can send one.
        payment.collection_state = CollectionState.REQUESTED
        await self._session.commit()

        logger.info(
            "billing.collection_provider_started",
            extra={
                "event": "billing.collection_provider_started",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice.id),
                "payment_id": str(payment_id),
                "attempt": attempt,
            },
        )

        try:
            reference = await provider.charge_saved_method(charge)
        except RecurringUnavailableError:
            # The account cannot do this after all, and the check happens
            # before any request leaves - so nothing was sent and the attempt
            # is given back rather than counted.
            return await self._abandon(
                payment_id,
                reason=NOT_SUPPORTED,
                detail="Automatic collection is not enabled for this account.",
            )
        except ChargeNotSentError as error:
            # The failure happened while describing the payment, before the
            # request that moves money. Provably nothing was charged, so this
            # attempt closes and its budget returns rather than blocking the
            # invoice behind a lookup about a request that never left.
            return await self._abandon(
                payment_id,
                reason=PROVIDER_REFUSED if not error.retryable else NOT_SENT,
                detail="The provider would not accept the charge request.",
            )
        except ProviderError as error:
            if error.retryable:
                # A timeout, a reset connection, a 5xx: no answer came back,
                # and a request that was not answered is not a request that was
                # not carried out. **This is the branch that must not guess.**
                # The attempt stays `requested` and unresolved, the invoice
                # stays uncollectible, and reconciliation owns it from here.
                return await self._leave_unknown(payment_id, invoice_id=invoice.id)
            # A refusal, which is an answer. The provider read the request and
            # would not perform it, so no money moved and the attempt is
            # finished - the budget is spent, because the card was tried.
            return await self._record_refusal(payment_id, invoice_id=invoice.id, now=moment)
        except Exception:
            # An exception nobody has classified. Treated exactly as an
            # unanswered request, because that is the assumption whose worst
            # case is a delay rather than a second debit.
            return await self._leave_unknown(payment_id, invoice_id=invoice.id)

        # TX3. The request was accepted; what it *did* is still the callback's
        # to say, so the attempt stays `requested` and `pending`.
        stored = await self._payments.get_by_id(payment_id)
        if stored is not None:
            stored.provider_intent_reference = reference
            await self._session.commit()

        logger.info(
            "billing.recurring_charge_requested",
            extra={
                "event": "billing.recurring_charge_requested",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice.id),
                "payment_id": str(payment_id),
                "attempt": attempt,
                "amount": str(charge.amount),
                "currency": charge.currency,
            },
        )
        return CollectionOutcome(charged=True, payment_id=payment_id)

    async def _abandon(
        self,
        payment_id: uuid.UUID,
        *,
        reason: str,
        detail: str,
    ) -> CollectionOutcome:
        """Close an attempt that provably never reached the provider.

        The one path that returns an attempt to the budget, and it is only
        reachable where the code knows no money-moving request was made: the
        capability check, which runs before anything is sent, and a failure
        while the payment was still being described. Everything else stays
        unresolved.

        The payment is marked `failed` rather than deleted - the history of a
        renewal that could not be attempted is worth as much as the history of
        one that was declined - but the invoice is put back exactly where it
        was, so the customer is not charged one attempt for a request that was
        never made.
        """
        payment = await self._payments.get_by_id(payment_id)
        if payment is None:  # pragma: no cover - it was committed a moment ago
            return CollectionOutcome(charged=False, reason=reason)

        payment.status = PaymentStatus.FAILED
        payment.collection_state = CollectionState.ABANDONED
        payment.failure_reason = detail
        invoice = await self._invoices.get_by_id(payment.invoice_id)
        if invoice is not None and invoice.collection_attempts > 0:
            invoice.collection_attempts -= 1
            invoice.next_collection_at = None
        await self._session.commit()

        logger.info(
            "billing.collection_attempt_abandoned",
            extra={
                "event": "billing.collection_attempt_abandoned",
                "tenant_id": str(self._tenant_id),
                "payment_id": str(payment_id),
                "reason": reason,
            },
        )
        return CollectionOutcome(charged=False, reason=reason, payment_id=payment_id)

    async def _leave_unknown(
        self,
        payment_id: uuid.UUID,
        *,
        invoice_id: uuid.UUID,
    ) -> CollectionOutcome:
        """Leave an attempt whose outcome nobody knows exactly where it is.

        Deliberately writes almost nothing. The attempt is already durable and
        already says `requested`, which is the whole of what is true: a request
        was made and no answer came back. Marking it `failed` here would be the
        old bug wearing different clothes - a payment the callback could then
        never settle, because `failed -> succeeded` is not a legal move.

        The attempt is *not* returned to the budget. A request that may have
        landed has been made, and a scheme counts it.
        """
        logger.warning(
            "billing.collection_outcome_unknown",
            extra={
                "event": "billing.collection_outcome_unknown",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice_id),
                "payment_id": str(payment_id),
            },
        )
        return CollectionOutcome(charged=False, reason=OUTCOME_UNKNOWN, payment_id=payment_id)

    async def _record_refusal(
        self,
        payment_id: uuid.UUID,
        *,
        invoice_id: uuid.UUID,
        now: datetime,
    ) -> CollectionOutcome:
        """Record a charge the provider read and would not perform."""
        payment = await self._payments.get_by_id(payment_id)
        if payment is None:  # pragma: no cover - it was committed a moment ago
            return CollectionOutcome(charged=False, reason=PROVIDER_REFUSED)

        payment.status = PaymentStatus.FAILED
        payment.collection_state = CollectionState.SETTLED
        payment.failure_reason = "The provider refused the automatic charge."
        payment.processed_at = now
        await self._session.commit()

        logger.warning(
            "billing.recurring_charge_failed",
            extra={
                "event": "billing.recurring_charge_failed",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice_id),
                "payment_id": str(payment_id),
            },
        )
        return CollectionOutcome(charged=False, reason=PROVIDER_REFUSED, payment_id=payment_id)

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
        """Take this attempt durably, or discover that somebody already has.

        Two database constraints decide this rather than any check written
        here, and they answer two different questions:

        `UNIQUE(tenant_id, idempotency_key)` on `auto:{invoice}:{attempt}` says
        one attempt number is claimed once. `uq_payments_unresolved_collection`
        - partial and on `invoice_id` alone - says **an invoice may have at
        most one attempt whose outcome nobody knows**, which is the constraint
        that closes WSL-01. The first stops two workers claiming attempt three;
        the second stops anybody starting attempt three while attempt two may
        already have taken the money.

        Counting the attempt before the provider is called is deliberate and
        unchanged: a request that times out has still been made, and a scheme
        counts it. What is new is that the count and the row are durable before
        anything can be sent, and that a request which provably never left
        hands the count back (`_abandon`).

        **This commits.** The claim is worth nothing uncommitted - that was the
        defect - so the transaction ends here and the caller resumes in a new
        one holding a payment that PostgreSQL already knows about.
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
                payment.collection_state = CollectionState.CLAIMED
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
        await self._session.commit()

        logger.info(
            "billing.collection_attempt_created",
            extra={
                "event": "billing.collection_attempt_created",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice.id),
                "payment_id": str(payment.id),
                "attempt": attempt,
            },
        )
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

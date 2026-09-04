"""Giving a customer their money back.

The one billing operation that moves money *out*, which is why almost all of
this file is about refusing to do it. Four things have to be true before a
processor is asked, and every one of them is read from the database rather than
from the request:

- the payment exists **and belongs to this workspace**
- it actually collected money, and still holds some
- it was collected by the provider this deployment is configured for
- no reversal has been asked for already

The amount is never accepted from a caller. It is the payment's own unreturned
balance, computed here, so there is no field anybody can send to be given back
more than they paid. That also decides the partial-refund question: Wasla has no
credit notes and no way to represent "half of March", so a refund returns what
is left of one payment and nothing else. Enforcing full-remainder semantics is
the honest version of not supporting partial refunds - the alternative is
inventing a concept the invoice model cannot render.

**A refund is requested here and confirmed elsewhere.** This records that the
provider accepted the reversal; it does not mark the money returned. That
happens in `CheckoutService.apply`, when a signed callback says the reversal
went through - the same path a refund issued from the provider's own dashboard
arrives on, which is why there is only one place that writes `refunded_amount`.
Telling a customer their money is back because an API call returned 200 would be
believing a request instead of a settlement.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.audit import AuditAction
from app.db.models.invoice import Payment
from app.db.models.user import User
from app.integrations.billing.base import ProviderError
from app.integrations.billing.checkout import CheckoutProvider, RefundRequest
from app.repositories.invoice_repository import InvoiceRepository, PaymentRepository
from app.services.audit_service import AuditTrail

logger = get_logger(__name__)

MAX_REASON_LENGTH: Final = 300


class RefundService:
    """Reversals for one workspace's payments."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider: CheckoutProvider | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._provider = provider
        self._payments = PaymentRepository(session, tenant_id=tenant_id)
        self._invoices = InvoiceRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    async def refund(
        self,
        payment_id: uuid.UUID,
        *,
        actor: User | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> Payment:
        """Ask the provider to return what is left of one payment.

        Returns the payment with the request recorded on it. Its status is
        deliberately unchanged: it still says `succeeded`, because the money
        has not come back yet and saying otherwise would be a lie the customer
        could read.
        """
        if self._provider is None:
            raise ValidationError("No payment provider is configured.")

        moment = now if now is not None else datetime.now(UTC)
        payment = await self._refundable(payment_id)
        amount = payment.amount - payment.refunded_amount

        # Written *and committed* before the provider is called, so a reversal
        # that is accepted and then lost to a crashed process still leaves a
        # row saying a refund was asked for. Flushing was not enough and used
        # to be all this did: a flush is undone by the same rollback that loses
        # everything else, so the record of the request died with the request
        # and the next one reversed the same money again - the refund-shaped
        # version of WSL-01 (ADR-088).
        payment.refund_requested_at = moment
        self._audit.record(
            AuditAction.PAYMENT_REFUND_REQUESTED,
            actor=actor,
            target_type="payment",
            target_id=payment.id,
            meta={
                "amount": str(amount),
                "currency": payment.currency,
                "reason": reason[:MAX_REASON_LENGTH] if reason else None,
            },
        )
        await self._session.commit()

        try:
            outcome = await self._provider.refund(
                RefundRequest(
                    # The provider's own id for the transaction that collected
                    # the money, read off the row. A caller cannot name a
                    # transaction, so a caller cannot reverse somebody else's.
                    transaction_reference=str(payment.provider_reference),
                    amount=amount,
                    currency=payment.currency,
                    reason=reason[:MAX_REASON_LENGTH] if reason else None,
                )
            )
        except ProviderError as error:
            if not error.retryable:
                # An answer, and it was no. The provider read the request and
                # would not perform it, so nothing is reversing and the record
                # of having asked is withdrawn - which is what lets somebody
                # fix the cause and ask again.
                payment.refund_requested_at = None
                await self._session.commit()
            # Otherwise the request is left standing, because a provider that
            # did not answer may still be reversing the money. `_refundable`
            # refuses the next attempt until a callback says what happened,
            # which turns a silent double refund into a refusal somebody looks
            # at.
            logger.warning(
                "billing.refund_failed",
                extra={
                    "event": "billing.refund_failed",
                    "tenant_id": str(self._tenant_id),
                    "payment_id": str(payment.id),
                    "retryable": error.retryable,
                },
            )
            raise

        payment.refund_reference = outcome.provider_reference
        await self._session.flush()

        logger.info(
            "billing.refund_requested",
            extra={
                "event": "billing.refund_requested",
                "tenant_id": str(self._tenant_id),
                "payment_id": str(payment.id),
                "amount": str(amount),
                "currency": payment.currency,
                "refund_reference": outcome.provider_reference,
            },
        )
        return payment

    async def _refundable(self, payment_id: uuid.UUID) -> Payment:
        """The payment, if reversing it is a thing that makes sense.

        Tenant-scoped through the repository, which is the isolation boundary:
        another workspace's payment id answers not-found, exactly as an
        invented one does, so a caller cannot learn which ids are real by
        reading the refusal.
        """
        payment = await self._payments.get_by_id(payment_id)
        if payment is None:
            raise NotFoundError("No such payment.")
        if not payment.is_refundable:
            # Covers a pending attempt, a declined one, and one already given
            # back. All three are "there is no money here to return", and
            # separating them in the message tells a caller nothing they can
            # act on that the payment's own status does not already say.
            raise ConflictError("This payment cannot be refunded.")
        if not payment.provider_reference:
            # Collected, but with no transaction recorded against it. That is a
            # payment somebody entered by hand for a bank transfer, and a
            # processor cannot reverse money it never took.
            raise ConflictError("This payment was not collected through a payment provider.")
        if self._provider is not None and payment.provider != self._provider.name:
            raise ConflictError("This payment was collected by a different provider.")
        if payment.refund_requested_at is not None and payment.refunded_at is None:
            # A reversal is outstanding: asked for, and not yet confirmed by a
            # callback. Asking again would reverse the same money twice.
            #
            # Keyed on the *request* rather than on `refund_reference`, which
            # is what this used to check. The reference is written after the
            # provider answers, so a process that died between the answer and
            # the commit left no reference - and the next request reversed
            # money that was already on its way back. The request is committed
            # before the provider is called, so it survives that (ADR-088).
            raise ConflictError("A refund has already been requested for this payment.")
        return payment

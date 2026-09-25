"""Giving a customer their money back.

The one billing operation that moves money *out*, which is why almost all of
this file is about refusing to do it. Four things have to be true before a
processor is asked, and every one of them is read from the database rather than
from the request:

- the payment exists **and belongs to this workspace**
- it actually collected money, and still holds some
- it was collected by the provider this deployment is configured for
- no reversal has been asked for already

**Only platform staff move money back** (BILL-13). A workspace owner used to
be able to refund their own payment in full at any time - twenty-nine days of
Pro, then the whole price back. The owner's endpoint now files a *request*
(`request_review`) that moves nothing; `refund` is reached only from the
platform's billing API, by an owner or admin, with a reason.

The amount is the operator's decision within bounds the database already
knows: positive, and no more than what is left of the payment. A partial
refund is a goodwill credit - the customer keeps the period - and a full one
withdraws what the payment bought once the provider confirms it (see
`CheckoutService._apply_reversal`).

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
from decimal import Decimal
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.telemetry import record_billing_refund
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing_incident import BillingIncidentKind
from app.db.models.invoice import Payment
from app.db.models.user import User
from app.integrations.billing.base import ProviderError
from app.integrations.billing.checkout import CheckoutProvider, RefundRequest
from app.repositories.invoice_repository import InvoiceRepository, PaymentRepository
from app.services.audit_service import AuditTrail
from app.services.billing_incident_service import raise_incident

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

    async def request_review(
        self,
        payment_id: uuid.UUID,
        *,
        actor: User | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> Payment:
        """A workspace owner asks for money back. Nothing moves (BILL-13).

        Recorded as an open billing incident a platform operator works through,
        and on the workspace's trail. One open request per payment - asking
        twice is the same request.
        """
        moment = now if now is not None else datetime.now(UTC)
        payment = await self._refundable(payment_id)
        await raise_incident(
            self._session,
            kind=BillingIncidentKind.REFUND_REQUESTED,
            dedupe_key=f"{payment.id}:{payment.refunded_amount}",
            tenant_id=self._tenant_id,
            payment_id=payment.id,
            invoice_id=payment.invoice_id,
            provider=payment.provider,
            provider_transaction_id=payment.provider_reference,
            amount=payment.amount - payment.refunded_amount,
            currency=payment.currency,
            detail=(reason or "Refund requested by the workspace owner.")[:MAX_REASON_LENGTH],
            now=moment,
        )
        self._audit.record(
            AuditAction.PAYMENT_REFUND_REVIEW_REQUESTED,
            actor=actor,
            target_type="payment",
            target_id=payment.id,
            meta={"reason": reason[:MAX_REASON_LENGTH] if reason else None},
        )
        await record_billing_refund("review_requested")
        return payment

    async def refund(
        self,
        payment_id: uuid.UUID,
        *,
        actor: User | None = None,
        reason: str | None = None,
        now: datetime | None = None,
        amount: Decimal | None = None,
        currency: str | None = None,
        expected_revision: int | None = None,
    ) -> Payment:
        """Ask the provider to return some or all of one payment. Platform only.

        Returns the payment with the request recorded on it. Its status is
        deliberately unchanged: it still says `succeeded`, because the money
        has not come back yet. Entitlements move only when the provider's
        signed callback confirms the reversal - never because a request was
        made.
        """
        if self._provider is None:
            raise ValidationError("No payment provider is configured.")

        moment = now if now is not None else datetime.now(UTC)
        payment = await self._refundable(payment_id)
        if expected_revision is not None and payment.revision != expected_revision:
            raise ConflictError(
                f"The payment has changed (revision {payment.revision}); reload it and retry."
            )
        remaining = payment.amount - payment.refunded_amount
        amount = remaining if amount is None else amount
        if amount <= 0:
            raise ValidationError("A refund must be for a positive amount.")
        if amount > remaining:
            raise ValidationError(f"Only {remaining} of this payment can still be refunded.")
        if currency is not None and currency.upper() != payment.currency.upper():
            raise ValidationError(f"This payment was collected in {payment.currency}.")

        # Written *and committed* before the provider is called, so a reversal
        # that is accepted and then lost to a crashed process still leaves a
        # row saying a refund was asked for. Flushing was not enough and used
        # to be all this did: a flush is undone by the same rollback that loses
        # everything else, so the record of the request died with the request
        # and the next one reversed the same money again - the refund-shaped
        # version of WSL-01 (ADR-088).
        payment.refund_requested_at = moment
        # The refunded *total* this request brings the payment to. What the
        # confirming callback is compared with, and what marks the request as
        # outstanding until it arrives.
        payment.refund_requested_amount = payment.refunded_amount + amount
        self._audit.record(
            AuditAction.PAYMENT_REFUND_REQUESTED,
            actor=actor,
            actor_kind=AuditActorKind.PLATFORM_STAFF if actor is not None else None,
            target_type="payment",
            target_id=payment.id,
            meta={
                "amount": str(amount),
                "currency": payment.currency,
                "partial": amount < remaining,
                "original_payment_amount": str(payment.amount),
                "provider_reference": payment.provider_reference,
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
                payment.refund_requested_amount = None
                await self._session.commit()
                await record_billing_refund("refused")
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
        await record_billing_refund("requested")

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
        if payment.refund_requested_amount is not None or (
            payment.refund_requested_at is not None and payment.refunded_at is None
        ):
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

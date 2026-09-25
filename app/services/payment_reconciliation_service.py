"""Finding out what became of a charge nobody heard the answer to.

A collection attempt and the money it may have moved live in two systems, and
no transaction spans them. `RecurringService` deals with one direction - commit
the attempt, *then* ask Paymob - and this is the other: a row that says a
charge may have happened, and does not know whether it did.

Every recovery here starts from that sentence. A payment in `REQUESTED` says:
Wasla decided to debit exactly this card, for exactly this amount, under
exactly this reference, and does not yet know the outcome. It is the durable
half of ADR-088, and it is what makes the crash window recoverable instead of
invisible.

## Two things resolve an attempt, and they are the same thing

A signed callback and a lookup by reference are Paymob describing one
transaction by two routes. So they are translated by one function - `_event` in
the Paymob client - and applied by one path, `CheckoutService.apply`, whose
`payment_events` insert is claimed under `UNIQUE(provider, provider_event_id)`.

That is the whole answer to the callback-versus-reconciler race, and it is a
database constraint rather than a check. Both routes build the same event id
for the same fact, so whichever arrives second is refused by the unique index
and reports `duplicate`. One settlement, one invoice paid, one plan granted,
however many things noticed at once.

## What this deliberately is not

**It never asks the provider for a list.** There is no "show me every
transaction since Tuesday and let me work out which are mine". The only
references this module knows are the ones a committed row names, for the same
reason `MediaUploadReconciler` never lists the bucket: a sweep driven by what
the *provider* thinks exists would act on rows PostgreSQL could not confirm,
and acting by absence of evidence about money is how a customer gets charged
twice.

**It never re-sends a charge.** Nothing here can move money. The most it does
is record what Paymob said, or hand an attempt back to an invoice when Paymob
says it never received one - and the sweep that then picks the invoice up makes
a *new* attempt with a new number and a new reference, through the ordinary
path with the ordinary budget.

## The five answers, and why none may be merged

    answered, succeeded   settle, exactly as a callback would
    answered, failed      record the decline; the budget is spent
    pending               the provider is still working. Ask again.
    not found             evidence the request never arrived - and only
                          evidence. Weighed against how long the attempt
                          has been outstanding, never acted on at once.
    unreachable           nothing was learned. **Not "not found."** A
                          provider that is down, read as one that never
                          received the request, re-charges every card in
                          flight during an outage.

The fourth and fifth are the pair that matters, and they are the same pair
`MediaStorage.exists` raises for rather than returning False.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.telemetry import record_hosted_reconciliation
from app.db.models.billing_incident import BillingIncidentKind
from app.db.models.invoice import CollectionState, Payment, PaymentStatus
from app.integrations.billing.checkout import (
    CallbackEvent,
    ChargeInquiry,
    ChargeInquiryProvider,
    CheckoutProvider,
    InquiryVerdict,
)
from app.repositories.invoice_repository import PlatformPaymentRepository
from app.services.billing_incident_service import raise_incident
from app.services.checkout_service import APPLIED, DECLINED, CheckoutService

logger = get_logger(__name__)


def _aware(moment: datetime) -> datetime:
    """A timestamp as an aware UTC one, whatever the driver handed back.

    `DateTime(timezone=True)` columns come back aware from asyncpg and naive
    from some drivers, and the comparisons below decide whether a card may be
    charged again - so the one place that could get it wrong does it once.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


class Verdict(StrEnum):
    """What one unresolved attempt turned out to be.

    A bounded set, because it is a metric label. Six values, for ever, and no
    identifier among them.
    """

    SETTLED = "settled"
    FAILED = "failed"
    ABANDONED = "abandoned"
    STILL_PENDING = "still_pending"
    NOT_FOUND = "not_found"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """What one pass did.

    Every number is a count of *logical* outcomes rather than of attempts, so
    two workers running at once produce one increment between them for a row
    only one of them claimed.
    """

    settled: int = 0
    failed: int = 0
    abandoned: int = 0
    still_pending: int = 0
    not_found: int = 0
    unreachable: int = 0

    @property
    def examined(self) -> int:
        return (
            self.settled
            + self.failed
            + self.abandoned
            + self.still_pending
            + self.not_found
            + self.unreachable
        )


class PaymentReconciler:
    """Settles collection attempts whose provider never reported back.

    Not tenant-scoped, in the same shape as `MediaUploadReconciler` and for the
    same reason: this is a platform sweep and nothing reachable from a request
    constructs it. Every settlement it performs still goes through a
    tenant-scoped `CheckoutService` built from the workspace on the row, so a
    reconciled payment can only ever affect the workspace that owns it.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        provider: CheckoutProvider,
        default_plan_code: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._provider = provider
        # Needed only to store a card recovered by Card Token Inquiry, which is
        # sealed with the deployment's token keys.
        self._settings = settings
        # Carried only to hand on. An inquiry answers with whatever the
        # transaction has become, and "refunded" is one of the answers - so
        # this path can reach `_apply_reversal` and needs to know where a
        # withdrawn grant lands, exactly as the callback path does (ADR-096).
        self._default_plan_code = default_plan_code
        self._payments = PlatformPaymentRepository(session)
        # Narrowed once, at construction, and held as its own name. The
        # alternative is an `isinstance` at every call site or an `assert` that
        # vanishes under -O; this way the type is settled where the object
        # arrives, which is also where the answer is interesting.
        self._inquirer = provider if isinstance(provider, ChargeInquiryProvider) else None

    @property
    def available(self) -> bool:
        """Whether this deployment can ask the provider anything.

        False is a supported state and not a fault - Paymob's inquiry API takes
        a credential a deployment need not hold. What it costs is recovery: an
        attempt whose callback never arrives stays unresolved, its invoice is
        never charged again, and the backlog is a metric rather than a
        duplicate debit.
        """
        return self._inquirer is not None and self._inquirer.can_inquire

    async def run(
        self,
        *,
        now: datetime | None = None,
        grace_seconds: float,
        lease_seconds: float,
        abandon_after_seconds: float,
        limit: int,
    ) -> ReconciliationOutcome:
        """One pass over attempts old enough to be nobody's live work.

        The grace period is the whole of the concurrency story with the worker
        that made the attempt. One started thirty seconds ago belongs to a job
        that is very probably between its request and its finalisation, and
        asking about it there races a settlement about to happen anyway.
        Waiting is cheaper and more obviously correct than coordinating.

        Each attempt is claimed, leased and committed in its own transaction
        before the provider is asked, so no row lock and no pooled connection
        is held across the lookup - the property the collection path was
        changed to have, applied to its recovery as well.
        """
        moment = now or datetime.now(UTC)
        if not self.available:
            return ReconciliationOutcome()

        counts: dict[Verdict, int] = {}
        for _ in range(limit):
            verdict = await self._settle_one(
                now=moment,
                older_than=moment - timedelta(seconds=grace_seconds),
                lease_before=moment - timedelta(seconds=lease_seconds),
                abandon_before=moment - timedelta(seconds=abandon_after_seconds),
            )
            if verdict is None:
                break
            counts[verdict] = counts.get(verdict, 0) + 1
            if verdict is Verdict.UNREACHABLE:
                # The provider is not answering. Asking it about the next
                # forty-nine attempts produces the same answer more slowly, and
                # every row is still here next pass.
                break

        return ReconciliationOutcome(
            settled=counts.get(Verdict.SETTLED, 0),
            failed=counts.get(Verdict.FAILED, 0),
            abandoned=counts.get(Verdict.ABANDONED, 0),
            still_pending=counts.get(Verdict.STILL_PENDING, 0),
            not_found=counts.get(Verdict.NOT_FOUND, 0),
            unreachable=counts.get(Verdict.UNREACHABLE, 0),
        )

    async def _settle_one(
        self,
        *,
        now: datetime,
        older_than: datetime,
        lease_before: datetime,
        abandon_before: datetime,
    ) -> Verdict | None:
        """Claim one attempt, ask about it, and act. None when there is none.

        Three transactions, and the shape is deliberately the one the
        collection path uses:

            TX1  claim the attempt and lease it            -> COMMIT
            --   ask Paymob. No lock held, no connection held.
            TX2  apply what it said                        -> COMMIT

        The lease committed in TX1 is what a second reconciler skips and what a
        crash here leaves behind: a row that becomes claimable again when the
        lease elapses, without anything existing to notice that it should.
        """
        claimed = await self._payments.claim_for_reconciliation(
            provider=self._provider.name,
            older_than=older_than,
            lease_before=lease_before,
            now=now,
        )
        if claimed is None:
            await self._session.rollback()
            return None

        payment_id = claimed.id
        tenant_id = claimed.tenant_id
        invoice_id = claimed.invoice_id
        created_at = claimed.created_at
        state = claimed.collection_state
        await self._session.commit()

        inquiry = await self._ask(payment_id)

        if inquiry.verdict is InquiryVerdict.UNREACHABLE:
            return Verdict.UNREACHABLE
        if inquiry.verdict is InquiryVerdict.UNSUPPORTED:  # pragma: no cover - gated by `available`
            return Verdict.UNREACHABLE
        if inquiry.verdict is InquiryVerdict.PENDING:
            return Verdict.STILL_PENDING
        if inquiry.verdict is InquiryVerdict.NOT_FOUND:
            return await self._absent(
                payment_id,
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                created_at=created_at,
                state=state,
                abandon_before=abandon_before,
                now=now,
            )

        event = inquiry.event
        if event is None:  # pragma: no cover - `answered` carries one by construction
            return Verdict.STILL_PENDING
        return await self._apply(event, payment_id=payment_id, tenant_id=tenant_id, now=now)

    async def _ask(self, payment_id: uuid.UUID) -> ChargeInquiry:
        """Ask the provider about one reference, holding nothing.

        The reference is the payment id, which was committed before Paymob
        could be told about it and has not changed since. That is what makes
        this question askable at all: a name invented after the fact would name
        nothing the provider had ever seen.
        """
        inquirer = self._inquirer
        if inquirer is None:  # pragma: no cover - `run` returns early without one
            return ChargeInquiry(verdict=InquiryVerdict.UNSUPPORTED)
        return await inquirer.inquire_charge(str(payment_id))

    async def _apply(
        self,
        event: CallbackEvent,
        *,
        payment_id: uuid.UUID,
        tenant_id: uuid.UUID,
        now: datetime,
    ) -> Verdict:
        """Apply a provider answer through the ordinary settlement path.

        `CheckoutService.apply` and nothing else, because a lookup and a
        callback are the same fact and must not be able to mean different
        things. Its `payment_events` insert decides the race with a callback
        that may be arriving this second: identical event ids, one unique
        index, one winner.

        A `duplicate` is a success, not a problem. It means the callback got
        there first and the invoice is already settled, which is precisely the
        outcome this exists to reach.
        """
        service = CheckoutService(
            self._session,
            tenant_id=tenant_id,
            provider=self._provider,
            default_plan_code=self._default_plan_code,
        )
        outcome = await service.apply(event, now=now)

        payment = await self._payments.get_by_id(payment_id)
        if payment is not None and payment.status is not PaymentStatus.PENDING:
            # Resolved, whoever resolved it. The collection state follows the
            # payment rather than the outcome word, so a duplicate that found
            # the work already done still closes the attempt.
            payment.collection_state = CollectionState.SETTLED
        await self._session.commit()

        settled = payment is not None and payment.status is PaymentStatus.SUCCEEDED
        logger.info(
            "billing.collection_reconciled",
            extra={
                "event": "billing.collection_reconciled",
                "tenant_id": str(tenant_id),
                "payment_id": str(payment_id),
                "outcome": outcome,
            },
        )
        return Verdict.SETTLED if settled else Verdict.FAILED

    async def _absent(
        self,
        payment_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        invoice_id: uuid.UUID,
        created_at: datetime,
        state: CollectionState | None,
        abandon_before: datetime,
        now: datetime,
    ) -> Verdict:
        """The provider has no record of this reference. Decide carefully.

        Two things produce this answer and only one of them is safe to act on:
        a request that never arrived, and a provider that has not finished
        making the event visible. Nothing distinguishes them in the response,
        so time does - the attempt is closed only once it has been outstanding
        long enough that "still indexing" has stopped being a candidate.

        An attempt still in `CLAIMED` needs none of that waiting. The move to
        `REQUESTED` commits before the request is built, so a claimed attempt
        provably predates any request at all, whatever Paymob would say.

        Closing hands the attempt back to the invoice, so the ordinary sweep
        makes a new one - a new number, a new reference, the same budget it
        would have had. Nothing here re-sends anything.
        """
        if state is not CollectionState.CLAIMED and _aware(created_at) > abandon_before:
            # Too soon to believe. The row keeps its lease, and the next pass
            # asks again.
            logger.info(
                "billing.collection_reference_unknown",
                extra={
                    "event": "billing.collection_reference_unknown",
                    "tenant_id": str(tenant_id),
                    "payment_id": str(payment_id),
                },
            )
            await self._session.commit()
            return Verdict.NOT_FOUND

        payment = await self._payments.get_by_id(payment_id)
        if payment is None:  # pragma: no cover - claimed a moment ago
            await self._session.rollback()
            return Verdict.NOT_FOUND

        payment.status = PaymentStatus.FAILED
        payment.collection_state = CollectionState.ABANDONED
        payment.failure_reason = "The provider has no record of this charge."
        payment.processed_at = now
        await self._payments.release_attempt(invoice_id=invoice_id)
        await self._session.commit()

        logger.warning(
            "billing.collection_attempt_abandoned",
            extra={
                "event": "billing.collection_attempt_abandoned",
                "tenant_id": str(tenant_id),
                "payment_id": str(payment_id),
                "reason": "not_found",
            },
        )
        return Verdict.ABANDONED

    # ------------------------------------------------ hosted checkouts (BILL-09)

    async def run_hosted(
        self,
        *,
        now: datetime | None = None,
        grace_seconds: float,
        lease_seconds: float,
        max_age_seconds: float,
        limit: int,
    ) -> dict[str, int]:
        """Ask about hosted-checkout payments whose callback never came.

        A customer paid on the provider's page and Wasla never heard: before
        this, the payment stayed `pending` for ever and the customer had paid
        for nothing (BILL-09). The same lease protocol as the automatic path,
        and the same settlement path as a callback - an inquiry's answer is
        translated by the function that reads callbacks and applied by
        `CheckoutService.apply`, so the two cannot settle one payment twice.

        Nothing here can charge anybody. The worst it does is ask.
        """
        moment = now or datetime.now(UTC)
        counts: dict[str, int] = {}
        if not self.available:
            return counts
        older_than = moment - timedelta(seconds=grace_seconds)
        for _ in range(limit):
            claimed = await self._payments.claim_hosted_for_reconciliation(
                provider=self._provider.name,
                older_than=older_than,
                newer_than=moment - timedelta(seconds=max_age_seconds),
                lease_before=moment - timedelta(seconds=lease_seconds),
                now=moment,
            )
            if claimed is None:
                await self._session.rollback()
                break
            payment_id, tenant_id = claimed.id, claimed.tenant_id
            await self._session.commit()
            verdict = await self.reconcile_hosted(payment_id, tenant_id=tenant_id, now=moment)
            counts[verdict] = counts.get(verdict, 0) + 1
            if verdict == "unreachable":
                break

        oldest = await self._payments.oldest_pending_hosted_at(
            provider=self._provider.name, older_than=older_than
        )
        age = max((moment - _aware(oldest)).total_seconds(), 0.0) if oldest else 0.0
        await record_hosted_reconciliation(counts, oldest_pending_seconds=age)
        return counts

    async def reconcile_hosted(
        self,
        payment_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        now: datetime,
    ) -> str:
        """One hosted payment: ask, and apply through the callback path."""
        inquiry = await self._ask(payment_id)
        if inquiry.verdict in (InquiryVerdict.UNREACHABLE, InquiryVerdict.UNSUPPORTED):
            return "unreachable"
        if inquiry.verdict is InquiryVerdict.PENDING:
            return "still_pending"
        if inquiry.verdict is InquiryVerdict.NOT_FOUND or inquiry.event is None:
            return "not_found"

        service = CheckoutService(
            self._session,
            tenant_id=tenant_id,
            provider=self._provider,
            default_plan_code=self._default_plan_code,
        )
        outcome = await service.apply(inquiry.event, now=now)
        payment = await self._payments.get_by_id(payment_id)
        recovered = (
            outcome == APPLIED and payment is not None and payment.status is PaymentStatus.SUCCEEDED
        )
        if recovered and payment is not None:
            # Visible, and closed: nothing is owed, but an operator reading the
            # ledger must be able to see that this payment was found by asking
            # rather than by being told.
            await raise_incident(
                self._session,
                kind=BillingIncidentKind.RECOVERED_BY_RECONCILIATION,
                dedupe_key=str(payment_id),
                tenant_id=tenant_id,
                payment_id=payment_id,
                invoice_id=payment.invoice_id,
                provider=self._provider.name,
                provider_transaction_id=payment.provider_reference,
                amount=payment.amount,
                currency=payment.currency,
                detail="A hosted payment whose callback never arrived, settled by inquiry.",
                resolved=True,
                now=now,
            )
            await self._recover_saved_card(payment, tenant_id=tenant_id)
        await self._session.commit()
        logger.info(
            "billing.hosted_payment_reconciled",
            extra={
                "event": "billing.hosted_payment_reconciled",
                "tenant_id": str(tenant_id),
                "payment_id": str(payment_id),
                "outcome": outcome,
            },
        )
        if recovered:
            return "settled"
        if outcome == DECLINED:
            return "declined"
        return "still_pending"

    async def _recover_saved_card(self, payment: Payment, *, tenant_id: uuid.UUID) -> None:
        """Ask for the card saved under a recovered checkout's order, if any.

        Card Token Inquiry is the documented recovery for a TOKEN callback that
        never arrived. Best effort: storing a card is idempotent on its
        fingerprint, so a TOKEN callback that did arrive changes nothing, and a
        card that cannot be recovered is a customer invoiced by e-mail.
        """
        order = payment.provider_order_id
        inquire = getattr(self._provider, "inquire_saved_method", None)
        if not order or inquire is None or self._settings is None:
            return
        saved = await inquire(str(order))
        if saved is None:
            return
        from app.services.payment_method_service import remember_saved_method

        await remember_saved_method(
            self._session,
            settings=self._settings,
            tenant_id=tenant_id,
            provider=self._provider.name,
            saved=saved,
        )

    async def reconcile_payment(self, payment_id: uuid.UUID, *, now: datetime) -> str:
        """An operator asked about one payment (spec: reconciliation action).

        Routes to the logic the sweeps use: an unresolved automatic attempt is
        asked about and settled or closed exactly as the automatic pass would,
        a pending hosted payment exactly as the hosted pass would. **Nothing
        here can send a charge** - the most it does is ask.
        """
        if not self.available:
            return "unsupported"
        payment = await self._payments.get_by_id(payment_id)
        if payment is None:
            return "not_found"
        tenant_id = payment.tenant_id
        if payment.is_unresolved_collection:
            inquiry = await self._ask(payment_id)
            if inquiry.verdict in (InquiryVerdict.UNREACHABLE, InquiryVerdict.UNSUPPORTED):
                return Verdict.UNREACHABLE.value
            if inquiry.verdict is InquiryVerdict.PENDING:
                return Verdict.STILL_PENDING.value
            if inquiry.verdict is InquiryVerdict.NOT_FOUND or inquiry.event is None:
                absent = await self._absent(
                    payment_id,
                    tenant_id=tenant_id,
                    invoice_id=payment.invoice_id,
                    created_at=payment.created_at,
                    state=payment.collection_state,
                    abandon_before=now - timedelta(days=1),
                    now=now,
                )
                return absent.value
            applied = await self._apply(
                inquiry.event, payment_id=payment_id, tenant_id=tenant_id, now=now
            )
            return applied.value
        if not payment.is_automatic and payment.status is PaymentStatus.PENDING:
            return await self.reconcile_hosted(payment_id, tenant_id=tenant_id, now=now)
        return "nothing_to_do"

    async def unresolved_count(self) -> int:
        """How many attempts are still waiting for an answer."""
        return await self._payments.unresolved_count(provider=self._provider.name)

    async def oldest_unresolved_seconds(self, *, now: datetime) -> float:
        """How long the oldest unanswered attempt has been waiting.

        Zero when there are none, which is the honest reading: there is no
        oldest, so nothing has been waiting.
        """
        oldest = await self._payments.oldest_unresolved_at(provider=self._provider.name)
        if oldest is None:
            return 0.0
        age = (now - _aware(oldest)).total_seconds()
        return max(age, 0.0)


__all__ = [
    "PaymentReconciler",
    "ReconciliationOutcome",
    "Verdict",
]

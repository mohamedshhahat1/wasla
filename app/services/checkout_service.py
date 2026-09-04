"""Starting a hosted checkout, and applying the callback that answers it.

Two halves of one flow, kept in one module because they are the two ends of the
same state machine and reading either without the other is misleading.

The rule that shapes everything here: **the browser is never believed.** The
customer chooses a plan code or names one of their own invoices, and nothing
else. The amount, the currency and the workspace are read from the database and
the authenticated session, the reference the provider quotes back is one we
generated, and the payment is only settled by a callback whose signature
checked out. A customer returning to the site with `?success=true` changes
nothing; there is deliberately no endpoint that would let it.

Every state change goes through the transition tables in
`db/models/invoice.py`. That is not ceremony: the statuses on the applying side
arrive from *outside*, and a late, out-of-order or forged-but-signed callback
claiming a payment succeeded after it was refunded would otherwise settle an
invoice twice.

The word "Paymob" appears nowhere below. This service talks to a
`CheckoutProvider`, which is a protocol in `integrations/billing/checkout.py`,
and the day a second processor is added it is constructed instead (ADR-031,
ADR-044).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
    WaslaError,
)
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import Plan, Subscription, SubscriptionStatus
from app.db.models.invoice import (
    CollectionState,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    invoice_may_move,
    payment_may_move,
)
from app.db.models.payment_event import MAX_DETAIL_LENGTH, PaymentEvent
from app.db.models.user import User
from app.integrations.billing.checkout import (
    CallbackEvent,
    CheckoutProvider,
    CheckoutRequest,
    EventKind,
)
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository
from app.repositories.invoice_repository import InvoiceRepository, PaymentRepository
from app.services.audit_service import AuditTrail
from app.services.subscription_service import SubscriptionService, add_interval

logger = get_logger(__name__)

# What a recorded callback did, in one word. Read by filtering, so a closed
# vocabulary rather than a message; `PaymentEvent.detail` carries the why.
#
# The distinction between the last three is the one worth keeping straight.
# `MISMATCHED` means the provider told us something about money that disagrees
# with what we asked for. `NO_CHANGE` means we believed it and it said nothing
# new. `REFUSED` means we believed it and it asked for a move the rules forbid,
# which is the interesting one: a signed callback trying to un-refund a payment
# lands here, and so does a genuine late delivery arriving out of order.
APPLIED: Final = "applied"
DUPLICATE: Final = "duplicate"
UNMATCHED: Final = "unmatched"
MISMATCHED: Final = "mismatched"
NO_CHANGE: Final = "no_change"
REFUSED: Final = "refused"

# The two subscription statuses a settled payment lifts, and nothing else
# (ADR-059, ADR-061). Both mean the platform is waiting for exactly this money:
# `PAST_DUE` is a workspace being chased, `SUSPENDED` is one whose grace ran
# out. A cancellation and an expiry are decisions somebody made, and a payment
# against an old invoice must not undo one - which is why this is a closed set
# rather than "any status that is not active".
_RECOVERABLE_STATUSES: Final[frozenset[SubscriptionStatus]] = frozenset(
    {
        SubscriptionStatus.PAST_DUE,
        SubscriptionStatus.SUSPENDED,
    }
)


@dataclass(frozen=True, slots=True)
class StartedCheckout:
    """Where to send the customer, and what it is for.

    The provider's client secret is deliberately absent. It is a bearer value
    for one payment page: it belongs in the URL the customer follows and
    nowhere else, least of all in a response body a client might log.
    """

    redirect_url: str
    payment_id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    currency: str


class CheckoutService:
    """Issues hosted checkouts and applies the callbacks that settle them."""

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
        self._invoices = InvoiceRepository(session, tenant_id=tenant_id)
        self._payments = PaymentRepository(session, tenant_id=tenant_id)
        self._plans = PlanRepository(session)
        self._subscriptions = SubscriptionRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session)

    # ------------------------------------------------------------- starting

    async def start(
        self,
        *,
        plan_code: str | None = None,
        invoice_id: uuid.UUID | None = None,
        actor: User | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> StartedCheckout:
        """Open a payment page, either for a plan or for an invoice already due.

        Exactly one of `plan_code` and `invoice_id`. Naming a plan is somebody
        choosing what to buy; naming an invoice is somebody paying a renewal
        this system issued for them, and the second is what makes the billing
        cycle actually collectible rather than merely recorded.

        The order matters. The invoice and the pending payment are written
        *before* the provider is called, so the reference handed to the
        provider is a row that already exists: a callback can never arrive for
        a payment this system has not heard of because the customer was fast.

        The provider call is the last thing, and the caller commits afterwards.
        A provider that succeeds and a commit that then fails leaves an
        intention nobody will pay against, which costs nothing; the reverse
        ordering would leave a customer at a payment page for an invoice that
        does not exist.
        """
        if self._provider is None:
            raise ValidationError("No payment provider is configured.")
        if (plan_code is None) == (invoice_id is None):
            raise ValidationError("Name either a plan or an invoice, not both.")

        moment = now if now is not None else datetime.now(UTC)
        await self._refuse_repeat(idempotency_key)

        if invoice_id is not None:
            invoice = await self._collectible_invoice(invoice_id)
            description = f"{invoice.plan_code} plan"
        else:
            plan = await self._priced_plan(str(plan_code))
            subscription = await self._subscriptions.get()
            invoice = await self._open_invoice(
                plan=plan,
                subscription=subscription,
                now=moment,
            )
            description = f"{plan.name} plan"

        # Flushed before `outstanding` is read. Column defaults are applied at
        # INSERT, so a freshly added invoice has `amount_paid` of None until
        # then and the subtraction inside `outstanding` fails - which is a
        # confusing way to learn that the row is not real yet.
        await self._session.flush()

        payment = await self._new_attempt(
            invoice,
            provider_name=self._provider.name,
            idempotency_key=idempotency_key,
        )

        session = await self._provider.create_checkout(
            CheckoutRequest(
                # Our id, quoted back by the provider, and the whole mapping
                # from a callback to this row. Fresh for every attempt because
                # the provider documents this reference as unique - which is
                # also why a retried request cannot reuse an earlier page and
                # is refused instead. See `_refuse_repeat`.
                reference=str(payment.id),
                amount=invoice.outstanding,
                currency=invoice.currency,
                description=description,
                customer_email=actor.email if actor else None,
                customer_name=actor.full_name if actor else None,
                # Correlation only, and nothing that would matter if disclosed:
                # this travels to a third party and comes back through a
                # request anybody can send at our webhook.
                metadata={"invoice_id": str(invoice.id)},
            )
        )
        payment.provider_intent_reference = session.provider_reference
        invoice.provider = self._provider.name
        await self._session.flush()

        logger.info(
            "billing.checkout_started",
            extra={
                "event": "billing.checkout_started",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice.id),
                "payment_id": str(payment.id),
                "provider": self._provider.name,
                "amount": str(invoice.outstanding),
                "currency": invoice.currency,
                # Never the redirect URL: it carries the client secret.
            },
        )
        return StartedCheckout(
            redirect_url=session.redirect_url,
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount=invoice.outstanding,
            currency=invoice.currency,
        )

    async def _new_attempt(
        self,
        invoice: Invoice,
        *,
        provider_name: str,
        idempotency_key: str | None,
    ) -> Payment:
        """The pending payment this checkout will collect against.

        Written before the provider is called, so the reference handed over is
        a row that already exists.

        The savepoint is here for the idempotency key. `_refuse_repeat` reads
        first and produces the good error message, but a read cannot decide two
        requests that arrive together - both see nothing and both proceed, and
        the constraint catches the loser at flush. Left unhandled that surfaces
        as an integrity error and a 500, which is the wrong answer to a
        customer whose browser retried: the request was refused for a reason
        the API has a word for.
        """
        try:
            async with self._session.begin_nested():
                payment = self._payments.record(
                    invoice_id=invoice.id,
                    status=PaymentStatus.PENDING,
                    amount=invoice.outstanding,
                    currency=invoice.currency,
                    provider=provider_name,
                    # No reference yet. It is the *transaction* id, which does
                    # not exist until somebody actually pays; the unique
                    # constraint on (provider, provider_reference) treats NULLs
                    # as distinct, so several abandoned attempts can coexist.
                    provider_reference=None,
                    idempotency_key=idempotency_key,
                )
                await self._session.flush()
        except IntegrityError:
            if not idempotency_key:
                # Nothing else on this row is unique while it is pending, so a
                # violation here with no key is something unexplained rather
                # than the race this handles. Re-raised rather than reported as
                # a conflict, because a conflict would be a guess.
                raise
            raise ConflictError(
                "A checkout has already been started for this request. "
                "Read its status rather than starting another."
            ) from None
        return payment

    async def _refuse_repeat(self, idempotency_key: str | None) -> None:
        """Stop a retried request from becoming a second payment page.

        Refused rather than replayed, and that is forced by a decision made
        earlier: the response contains a URL carrying the provider's client
        secret, and that secret is deliberately never stored (ADR-044). A
        replay would therefore have to fetch a *new* page from the provider
        under the same reference, and the provider documents that reference as
        unique - so there is no honest replay available.

        Refusing is the better half of the trade anyway. The caller learns its
        first request was accepted and can read the payment's status, which is
        the thing it actually wanted to know; creating a second intention would
        leave two live payment pages for one invoice and no way to tell a
        customer which of them to use.

        The read below is a courtesy that produces the good error message. The
        guarantee is the unique constraint on `(tenant_id, idempotency_key)`,
        which is what decides two simultaneous retries.
        """
        if not idempotency_key:
            return
        existing = await self._payments.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            raise ConflictError(
                "A checkout has already been started for this request. "
                "Read its status rather than starting another."
            )

    async def _priced_plan(self, plan_code: str) -> Plan:
        """The plan a customer may pay for, priced by us.

        `is_public` is enforced here as it is in `SubscriptionService`: a
        checkout is another door onto plan selection, and a door that skipped
        the check would let somebody pay the Enterprise price - or, worse, the
        Enterprise *limits* at whatever price that row happens to carry.
        """
        plan = await self._plans.get_by_code(plan_code)
        if plan is None or not plan.is_active or not plan.is_public:
            # The same refusal for all three, as elsewhere: distinguishing them
            # confirms which private codes are real.
            raise ValidationError("No such plan.")
        if plan.price <= 0:
            # A free plan has nothing to collect. Sending somebody to a payment
            # page for zero is a confusing dead end, and providers refuse it.
            raise ValidationError("That plan does not require payment.")
        return plan

    async def _collectible_invoice(self, invoice_id: uuid.UUID) -> Invoice:
        """One of this workspace's invoices, if there is money left on it.

        Tenant-scoped through the repository, so another workspace's invoice id
        is indistinguishable from one that does not exist - a caller must not
        learn which invoice ids are real by being told a different refusal.
        """
        invoice = await self._invoices.get_by_id(invoice_id)
        if invoice is None:
            raise NotFoundError("No such invoice.")
        if invoice.status is InvoiceStatus.PAID:
            raise ConflictError("This invoice has already been paid.")
        if invoice.status in (InvoiceStatus.VOID, InvoiceStatus.DRAFT):
            # A withdrawn bill and an unissued one are both things nobody has
            # been asked for. Collecting against either would be charging for
            # something we never sent.
            raise ConflictError("This invoice cannot be collected.")
        if invoice.outstanding <= 0:
            raise ConflictError("Nothing is outstanding on this invoice.")
        return invoice

    async def _open_invoice(
        self,
        *,
        plan: Plan,
        subscription: Subscription | None,
        now: datetime,
    ) -> Invoice:
        """The invoice this checkout collects, reusing one if it is already open.

        Reuse rather than issue-per-attempt, and the constraint decides it
        either way: `UNIQUE(tenant_id, period_start)` means a second attempt at
        the same period cannot create a second invoice. Somebody who abandons a
        checkout and starts another gets a second *payment* against one
        invoice, which is exactly what the payments table is for - attempts are
        rows, and the history is what a dispute turns on.
        """
        if subscription is not None:
            period_start = subscription.current_period_start
            period_end = subscription.current_period_end
        else:
            # Truncated to the day, and that is the whole reason this branch
            # exists. `UNIQUE(tenant_id, period_start)` is what stops a
            # workspace being billed twice for one period, and a period start
            # of `now` defeats it completely: two checkouts a second apart get
            # timestamps differing by microseconds, so the constraint sees two
            # different periods and every abandoned attempt leaves an invoice.
            period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            period_end = add_interval(period_start, plan.interval)

        existing = await self._invoices.get_for_period(period_start=period_start)
        if existing is not None:
            return self._reprice(existing, plan=plan, period_end=period_end)

        try:
            async with self._session.begin_nested():
                created = self._invoices.create(
                    subscription_id=subscription.id if subscription else None,
                    status=InvoiceStatus.OPEN,
                    plan_code=plan.code,
                    amount_due=plan.price,
                    currency=plan.currency,
                    period_start=period_start,
                    period_end=period_end,
                    lines=self._lines(plan),
                )
                await self._session.flush()
        except IntegrityError:
            # Two checkouts started at once and the other one won the period.
            # The constraint is doing exactly its job; this re-reads rather
            # than failing, so the loser collects against the same invoice
            # instead of answering 500 to a customer who did nothing wrong.
            existing = await self._invoices.get_for_period(period_start=period_start)
            if existing is None:  # pragma: no cover - the row that just blocked us
                raise
            return self._reprice(existing, plan=plan, period_end=period_end)
        return created

    def _reprice(self, invoice: Invoice, *, plan: Plan, period_end: datetime) -> Invoice:
        """Point an untouched invoice at the plan the customer actually chose.

        Only while nothing has been collected. Once money has arrived the
        invoice is a record of what was paid rather than a statement of what
        will be owed, and re-pricing it would silently move somebody's money
        from one thing onto another - which this system cannot undo, because it
        does not issue credits.
        """
        if invoice.status is InvoiceStatus.PAID:
            raise ConflictError("This period has already been paid.")
        if invoice.is_terminal:
            raise ConflictError("This invoice is settled and cannot be collected.")
        if invoice.plan_code != plan.code:
            if invoice.amount_paid > 0:
                raise ConflictError(
                    "This period has a part-paid invoice for another plan.",
                )
            invoice.plan_code = plan.code
            invoice.amount_due = plan.price
            invoice.currency = plan.currency
            invoice.period_end = period_end
            invoice.lines = self._lines(plan)
        return invoice

    @staticmethod
    def _lines(plan: Plan) -> list[dict[str, object]]:
        """The invoice as it will be read back, with the price copied in.

        Copied rather than joined, following the invoice model: a plan repriced
        next month must not change what this month's invoice says.
        """
        return [
            {
                "kind": "subscription",
                "description": f"{plan.name} plan",
                "amount": str(plan.price),
                "quantity": 1,
            }
        ]

    # ------------------------------------------------------------- applying

    async def apply(self, event: CallbackEvent, *, now: datetime | None = None) -> str:
        """Apply one verified callback, exactly once, and say what it did.

        The caller has already authenticated the event; everything here is
        about whether it may be *believed*, which is a different question. Five
        refusals stand between a verified callback and a settled invoice:

        1. **It must be new.** The `payment_events` insert is the claim, and
           the unique constraint decides races rather than a preceding read.
        2. **It must name a payment we issued**, by a reference we generated.
        3. **That payment must belong to this workspace.** A callback cannot
           reach across a tenant boundary even if a reference leaked.
        4. **The figures must match what we asked for.** A provider reporting a
           different amount or currency is not settling this invoice, whatever
           it says.
        5. **The move it asks for must be legal.** A signed callback claiming a
           refunded payment succeeded is refused by the transition table rather
           than believed because it was signed.

        Returns the outcome word, which the endpoint turns into a response that
        is the same for all of them.
        """
        moment = now if now is not None else datetime.now(UTC)
        payment = await self._matching_payment(event)

        record = await self._claim(event, payment=payment, now=moment)
        if record is None:
            return DUPLICATE

        outcome, detail = await self._decide(event, payment=payment, now=moment)
        record.outcome = outcome
        record.detail = detail[:MAX_DETAIL_LENGTH] if detail else None
        record.processed_at = moment
        await self._session.flush()

        logger.info(
            "billing.callback_processed",
            extra={
                "event": "billing.callback_processed",
                "tenant_id": str(self._tenant_id),
                "provider_event_id": event.event_id,
                "event_type": event.event_type,
                "payment_id": str(payment.id) if payment else None,
                "outcome": outcome,
                "detail": detail,
            },
        )
        return outcome

    async def _decide(
        self,
        event: CallbackEvent,
        *,
        payment: Payment | None,
        now: datetime,
    ) -> tuple[str, str | None]:
        """What this callback is allowed to change, and what it changed."""
        if payment is None:
            return UNMATCHED, "No payment matches this reference."

        invoice = await self._invoices.get_by_id(payment.invoice_id)
        if invoice is None or invoice.tenant_id != self._tenant_id:
            # Belt and braces: the payment repository is already tenant-scoped,
            # so reaching here means the two disagree, and a disagreement about
            # who owns money is not something to resolve in favour of acting.
            return UNMATCHED, "The payment's invoice is not this workspace's."

        if event.currency.upper() != invoice.currency.upper():
            return MISMATCHED, f"Expected {invoice.currency}, was told {event.currency}."

        if event.kind in (EventKind.REFUNDED, EventKind.VOIDED):
            return self._apply_reversal(event, payment=payment, invoice=invoice, now=now)
        return await self._apply_collection(event, payment=payment, invoice=invoice, now=now)

    async def _apply_collection(
        self,
        event: CallbackEvent,
        *,
        payment: Payment,
        invoice: Invoice,
        now: datetime,
    ) -> tuple[str, str | None]:
        """A callback reporting what happened to an attempt at collecting."""
        if event.amount != payment.amount:
            # A provider that says it collected a different amount than we
            # asked for has done something we do not understand, and settling
            # the invoice anyway would paper over it.
            logger.warning(
                "billing.callback_amount_mismatch",
                extra={
                    "event": "billing.callback_amount_mismatch",
                    "payment_id": str(payment.id),
                    "expected_amount": str(payment.amount),
                    "reported_amount": str(event.amount),
                },
            )
            payment.failure_reason = "The provider reported a different amount."
            return MISMATCHED, f"Expected {payment.amount}, was told {event.amount}."

        if event.status is payment.status:
            return NO_CHANGE, f"Already {payment.status.value}."
        if not payment_may_move(payment.status, event.status):
            logger.warning(
                "billing.callback_illegal_transition",
                extra={
                    "event": "billing.callback_illegal_transition",
                    "payment_id": str(payment.id),
                    "from_status": payment.status.value,
                    "to_status": event.status.value,
                },
            )
            return REFUSED, f"{payment.status.value} cannot become {event.status.value}."

        payment.status = event.status
        payment.provider_reference = event.provider_transaction_id
        payment.failure_reason = event.failure_reason
        payment.processed_at = now
        if payment.is_unresolved_collection:
            # An automatic attempt has just learned its outcome, so the invoice
            # behind it stops being blocked. Written here rather than by the
            # collection path because *this* is where the answer arrives - the
            # charge request only ever asked (ADR-088).
            #
            # A callback that reaches a still-`claimed` attempt is unusual and
            # not impossible: the worker committed its claim, was killed before
            # marking it requested, and Paymob answered a request it had
            # already received. Closing it is right in both cases, and the
            # attempt count stays spent because a charge demonstrably happened.
            payment.collection_state = CollectionState.SETTLED

        if not event.succeeded:
            return APPLIED, f"Payment {event.status.value}."
        return await self._settle(invoice, payment=payment, now=now)

    def _apply_reversal(
        self,
        event: CallbackEvent,
        *,
        payment: Payment,
        invoice: Invoice,
        now: datetime,
    ) -> tuple[str, str | None]:
        """A callback reporting that money we collected has gone back.

        Arrives whether or not this system asked for it: a refund issued from
        the provider's own dashboard produces the same notification as one
        `RefundService` requested, and both have to land in the same place or
        the ledger stops matching the bank.

        The refunded total is taken from the provider's running total where it
        gives one, because a payment can be reversed in parts and each
        notification carries the cumulative figure. Falling back to the
        reversal's own amount covers the callback about the refund transaction
        itself, which reports what *that* transaction moved.
        """
        refunded = event.refunded_amount if event.refunded_amount else event.amount
        if refunded <= 0 or refunded > payment.amount:
            return MISMATCHED, f"Refund of {refunded} against a payment of {payment.amount}."
        if payment.status not in (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED):
            return REFUSED, f"{payment.status.value} was never collected."
        if refunded <= payment.refunded_amount:
            return NO_CHANGE, f"Already refunded {payment.refunded_amount}."

        returned = refunded - payment.refunded_amount
        payment.refunded_amount = refunded
        payment.refunded_at = now
        if refunded >= payment.amount and payment_may_move(payment.status, PaymentStatus.REFUNDED):
            payment.status = PaymentStatus.REFUNDED

        # The invoice holds less money than it did. `amount_paid` is what we
        # have, not what was once sent, so an invoice no longer covered stops
        # being paid - see `INVOICE_TRANSITIONS`.
        invoice.amount_paid = invoice.amount_paid - returned
        if invoice.amount_paid < 0:  # pragma: no cover - guarded by the checks above
            invoice.amount_paid = Decimal("0.00")
        if (
            invoice.status is InvoiceStatus.PAID
            and invoice.amount_paid < invoice.amount_due
            and invoice_may_move(invoice.status, InvoiceStatus.OPEN)
        ):
            invoice.status = InvoiceStatus.OPEN
            invoice.paid_at = None

        self._audit.record(
            AuditAction.PAYMENT_REFUNDED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            tenant_id=self._tenant_id,
            target_type="payment",
            target_id=payment.id,
            meta={
                "amount": str(returned),
                "refunded_total": str(refunded),
                "currency": invoice.currency,
                "kind": event.kind.value,
            },
        )
        logger.info(
            "billing.refund_applied",
            extra={
                "event": "billing.refund_applied",
                "tenant_id": str(self._tenant_id),
                "payment_id": str(payment.id),
                "invoice_id": str(invoice.id),
                "amount": str(returned),
            },
        )
        return APPLIED, f"Refunded {returned}."

    def _provider_name(self) -> str:
        return self._provider.name if self._provider is not None else "unknown"

    async def _matching_payment(self, event: CallbackEvent) -> Payment | None:
        """The payment this callback names, if it is ours.

        By our own reference first, and never by anything the provider chose to
        put in a field we do not control. The tenant filter on the repository
        is what stops a callback naming another workspace's payment from being
        applied to this one.

        The fallback matters for reversals. A refund produces a callback about
        the transaction it reverses, and that notification is documented to
        carry the parent's id rather than necessarily carrying our reference
        home - so a payment is also findable by the transaction id we recorded
        ourselves when the money arrived. Both routes go through identifiers
        this system wrote down; neither trusts a name the caller invented.
        """
        if event.reference:
            try:
                payment_id = uuid.UUID(event.reference)
            except ValueError:
                payment_id = None
            if payment_id is not None:
                found = await self._payments.get_by_id(payment_id)
                if found is not None:
                    return found

        for transaction_id in (event.parent_transaction_id, event.provider_transaction_id):
            if not transaction_id:
                continue
            found = await self._payments.get_by_transaction(
                provider=self._provider_name(),
                transaction_id=transaction_id,
            )
            if found is not None:
                return found
        return None

    async def _claim(
        self,
        event: CallbackEvent,
        *,
        payment: Payment | None,
        now: datetime,
    ) -> PaymentEvent | None:
        """Take ownership of this event, or report that somebody already has.

        Returns the row to fill in, or None when another delivery owns it.

        The outcome is written as unresolved and corrected once there is one.
        Claiming first is what makes two simultaneous deliveries safe, and it
        is why a crash between the claim and the decision leaves a row saying
        nothing happened - which is exactly what did happen.

        A savepoint, because a unique violation poisons the transaction it
        happens in and this one has an invoice to settle afterwards. The nested
        block is released on success and rolled back on the collision, leaving
        the outer transaction usable either way.
        """
        record = PaymentEvent(
            provider=self._provider_name(),
            provider_event_id=event.event_id,
            provider_transaction_id=event.provider_transaction_id,
            event_type=event.event_type,
            payment_id=payment.id if payment is not None else None,
            outcome=NO_CHANGE,
            received_at=now,
            processed_at=None,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush()
        except IntegrityError:
            logger.info(
                "billing.callback_duplicate",
                extra={
                    "event": "billing.callback_duplicate",
                    "provider_event_id": event.event_id,
                },
            )
            return None
        return record

    async def _settle(
        self,
        invoice: Invoice,
        *,
        payment: Payment,
        now: datetime,
    ) -> tuple[str, str | None]:
        """Money arrived: mark the invoice paid, and grant what it was for.

        **This is the authoritative point at which a paid plan is granted**
        (ADR-059). Nothing a client sends can reach it: the only caller is
        `apply`, which runs behind an HMAC over the provider's own payload, and
        the plan it grants is read from the invoice this system wrote before
        the customer was ever sent to a payment page.

        Still deliberately narrow. Paying settles an invoice; it does not
        resubscribe, revive or extend anything. See `_apply_purchased_plan` for
        exactly which subscriptions move and which are left alone.
        """
        if invoice.is_terminal:
            # A second payment against an invoice that is already finished.
            # Recorded and refused rather than added: it means the customer has
            # paid twice, which is a refund to issue rather than a balance to
            # increase.
            logger.warning(
                "billing.settlement_refused",
                extra={
                    "event": "billing.settlement_refused",
                    "invoice_id": str(invoice.id),
                    "payment_id": str(payment.id),
                    "status": invoice.status.value,
                },
            )
            return REFUSED, f"Invoice is already {invoice.status.value}."

        invoice.amount_paid = invoice.amount_paid + payment.amount
        invoice.provider_reference = payment.provider_reference
        if invoice.amount_paid >= invoice.amount_due and invoice_may_move(
            invoice.status, InvoiceStatus.PAID
        ):
            invoice.status = InvoiceStatus.PAID
            invoice.paid_at = now

        subscription = await self._subscriptions.get()
        if (
            subscription is not None
            and subscription.id == invoice.subscription_id
            and subscription.status in _RECOVERABLE_STATUSES
        ):
            # The only two statuses a payment changes on its own, and the list
            # is closed on purpose. Both mean "the platform is waiting for this
            # money": `PAST_DUE` is a workspace being chased, `SUSPENDED` is
            # one whose grace ran out (ADR-061). Settling the bill is precisely
            # the condition each was waiting on, so lifting them is the whole
            # point of the payment rather than a side effect of it.
            #
            # A trial stays a trial, a cancellation stays cancelled and an
            # expiry stays expired: paying an invoice is not a request to
            # resubscribe, and treating it as one would revive a subscription
            # somebody deliberately ended (ADR-059). That is why this reads a
            # closed set rather than "not active".
            #
            # Ordered before `_apply_purchased_plan` so a suspended workspace
            # that pays for a *different* plan gets it: the row is no longer
            # terminal by the time the plan transition is considered.
            previous = subscription.status
            subscription.status = SubscriptionStatus.ACTIVE
            logger.info(
                "billing.subscription_restored",
                extra={
                    "event": "billing.subscription_restored",
                    "tenant_id": str(self._tenant_id),
                    "invoice_id": str(invoice.id),
                    "from_status": previous.value,
                },
            )

        await self._apply_purchased_plan(invoice, subscription=subscription, now=now)

        self._audit.record(
            AuditAction.PAYMENT_RECORDED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            tenant_id=self._tenant_id,
            target_type="invoice",
            target_id=invoice.id,
            meta={
                "payment_id": str(payment.id),
                "amount": str(payment.amount),
                "currency": invoice.currency,
                "provider": payment.provider,
            },
        )
        logger.info(
            "billing.payment_applied",
            extra={
                "event": "billing.payment_applied",
                "tenant_id": str(self._tenant_id),
                "payment_id": str(payment.id),
                "invoice_id": str(invoice.id),
                "status": invoice.status.value,
            },
        )
        return APPLIED, f"Invoice {invoice.status.value}."

    async def _apply_purchased_plan(
        self,
        invoice: Invoice,
        *,
        subscription: Subscription | None,
        now: datetime,
    ) -> None:
        """Move the workspace onto the plan this invoice was raised for.

        The counterpart to the self-service refusal in `SubscriptionService`:
        a priced plan cannot be asked for, so this is where one is granted. The
        plan is named by `invoice.plan_code`, which `_open_invoice` copied from
        the plan the customer chose *before* the provider was called - so the
        grant is decided by a row this system wrote, never by anything in the
        callback. The callback says only that the money arrived.

        The transition itself is `SubscriptionService.change_plan`, called with
        `self_service=False`. Reused rather than reimplemented, and that is the
        point: period arithmetic, trial clearing, the cancellation reset and the
        audit entry are one state machine with one owner, and a second copy here
        would be the parallel billing machine this fix exists to avoid.

        Four cases are deliberately left alone:

        - **No subscription.** There is nothing to move and creating one here
          would mean inventing trial and period rules in a settlement path.
          Every workspace gets one at registration, so this is the misconfigured
          deployment where `DEFAULT_PLAN_CODE` names no plan - and where limits
          are already unenforced. Logged loudly rather than guessed at.
        - **A renewal.** The invoice names the plan the workspace is already on,
          so there is no transition; the period rolls over in the billing sweep,
          which is the thing that understands periods.
        - **A terminal subscription.** Cancelled and expired stay that way.
          Paying an old invoice is not a request to resubscribe, which is the
          rule `_settle` already follows for status.
        - **A retired plan code.** The catalogue row is gone, so there is
          nothing to grant. The invoice is still paid and the money is still
          recorded.

        Failure is contained. A plan that cannot be applied must not undo a
        settlement that has already happened: the customer's money arrived, the
        invoice says so, and a grant that did not land is something an operator
        can put right - whereas an exception here would roll back the record of
        the payment itself.
        """
        if subscription is None:
            logger.warning(
                "billing.paid_plan_without_subscription",
                extra={
                    "event": "billing.paid_plan_without_subscription",
                    "tenant_id": str(self._tenant_id),
                    "invoice_id": str(invoice.id),
                    "plan_code": invoice.plan_code,
                },
            )
            return
        if subscription.is_terminal:
            return

        plan = await self._plans.get_by_code(invoice.plan_code)
        if plan is None or plan.id == subscription.plan_id:
            return

        try:
            await SubscriptionService(self._session, tenant_id=self._tenant_id).change_plan(
                plan_code=invoice.plan_code,
                now=now,
                # The platform granting what was paid for, not a customer
                # choosing. This is the only caller besides registration that
                # passes it, and it is what lets a priced plan through the gate
                # in `_require_plan`.
                self_service=False,
                # No actor: nobody pressed anything. A callback is the provider
                # telling us money moved, so the audit entry `change_plan`
                # writes is a system observation.
                actor=None,
            )
        except WaslaError:
            logger.exception(
                "billing.paid_plan_not_applied",
                extra={
                    "event": "billing.paid_plan_not_applied",
                    "tenant_id": str(self._tenant_id),
                    "invoice_id": str(invoice.id),
                    "plan_code": invoice.plan_code,
                },
            )
            return

        logger.info(
            "billing.paid_plan_applied",
            extra={
                "event": "billing.paid_plan_applied",
                "tenant_id": str(self._tenant_id),
                "invoice_id": str(invoice.id),
                "plan_code": invoice.plan_code,
            },
        )

    async def require_payment(self, payment_id: uuid.UUID) -> Payment:
        """One payment of this workspace's, or a 404.

        Tenant-scoped through the repository, so another workspace's payment id
        is indistinguishable from one that does not exist.
        """
        payment = await self._payments.get_by_id(payment_id)
        if payment is None:
            raise NotFoundError("No such payment.")
        return payment

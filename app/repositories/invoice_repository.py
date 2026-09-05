"""Data access for invoices and payments.

Scoped like every other workspace-owned table, with the usual platform-facing
exception kept in a separate class so it is visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ColumnElement, func, select, update

from app.core.pagination import Cursor
from app.db.models.billing import Subscription, SubscriptionStatus
from app.db.models.invoice import (
    UNRESOLVED_COLLECTION_STATES,
    CollectionState,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.repositories.base import BaseRepository, TenantScopedRepository


def _has_unresolved_attempt() -> ColumnElement[bool]:
    """Whether this invoice already has a collection attempt nobody has resolved.

    The eligibility half of what closes WSL-01. `uq_payments_unresolved_collection`
    makes a second unresolved attempt impossible; this makes the sweep stop
    before it tries, so an invoice whose outcome is unknown is passed over
    quietly instead of producing an integrity error on every pass.

    Correlated rather than joined, because an invoice with no payments must
    still be claimable and a join would drop it.
    """
    return (
        select(Payment.id)
        .where(Payment.invoice_id == Invoice.id)
        .where(Payment.collection_state.in_(UNRESOLVED_COLLECTION_STATES))
        .exists()
    )


@dataclass(frozen=True, slots=True)
class RevenueTotal:
    """Money recognised in one currency, and how many invoices it came from."""

    currency: str
    amount: Decimal
    invoices: int


class InvoiceRepository(TenantScopedRepository[Invoice]):
    """One workspace's invoices."""

    model = Invoice

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Invoice.tenant_id == self.tenant_id

    async def get_by_id(self, invoice_id: uuid.UUID) -> Invoice | None:
        return await self._first(self._select().where(Invoice.id == invoice_id))

    async def require_by_id(self, invoice_id: uuid.UUID) -> Invoice:
        return await self._require(self._select().where(Invoice.id == invoice_id))

    async def get_for_period(self, *, period_start: datetime) -> Invoice | None:
        """The invoice already issued for this period, if there is one.

        The unique constraint is the real guard against billing a customer
        twice for March; this lookup exists so a second sweep is a no-op rather
        than an integrity error.
        """
        return await self._first(self._select().where(Invoice.period_start == period_start))

    async def has_other_settled_cover(
        self,
        *,
        invoice_id: uuid.UUID,
        plan_code: str,
        at: datetime,
    ) -> bool:
        """Whether some *other* invoice still pays for this plan right now.

        The query behind "unless another valid settlement independently covers
        it" (ADR-096). A reversal withdraws the grant its own invoice made, and
        this is what stops it withdrawing one somebody else's money is holding
        up: a workspace that paid for the current period on a second invoice
        has bought the plan twice over, and taking it away because the first
        was refunded would be the wrong direction of the same bug.

        Three predicates, and each is load-bearing:

        - **money is still on it** - `amount_paid > 0` rather than
          `status = PAID`, because a part-paid invoice is part cover and a
          reversed one drops out of this set the moment its balance does.
        - **it is not void.** A voided invoice is a record that something was
          cancelled, not a claim on the period.
        - **its period contains `at`.** Cover means cover *now*. Without it the
          test is "have you ever paid for this plan", and every refund after
          the first would be free.
        """
        statement = (
            self._select()
            .where(Invoice.id != invoice_id)
            .where(Invoice.plan_code == plan_code)
            .where(Invoice.status != InvoiceStatus.VOID)
            .where(Invoice.amount_paid > 0)
            .where(Invoice.period_start <= at)
            .where(Invoice.period_end > at)
            .limit(1)
        )
        return await self._first(statement) is not None

    async def list_invoices(
        self,
        *,
        limit: int = 50,
        after: Cursor | None = None,
    ) -> list[Invoice]:
        """Newest first, which is the only order anybody reads invoices in."""
        statement = self._select().order_by(Invoice.period_start.desc(), Invoice.id.desc())
        if after is not None and after.sort_value is not None:
            statement = statement.where(
                (Invoice.period_start < after.sort_value)
                | ((Invoice.period_start == after.sort_value) & (Invoice.id < after.id))
            )
        return await self._all(statement.limit(limit))

    def create(
        self,
        *,
        subscription_id: uuid.UUID | None,
        plan_code: str,
        amount_due: Decimal,
        currency: str,
        period_start: datetime,
        period_end: datetime,
        lines: list[dict[str, object]],
        status: InvoiceStatus = InvoiceStatus.DRAFT,
    ) -> Invoice:
        return self.add(
            Invoice(
                tenant_id=self.tenant_id,
                subscription_id=subscription_id,
                status=status,
                plan_code=plan_code,
                amount_due=amount_due,
                currency=currency,
                period_start=period_start,
                period_end=period_end,
                lines=lines,
            )
        )


class PaymentRepository(TenantScopedRepository[Payment]):
    """One workspace's payment attempts."""

    model = Payment

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Payment.tenant_id == self.tenant_id

    async def get_by_id(self, payment_id: uuid.UUID) -> Payment | None:
        """One payment of this workspace's.

        Tenant-scoped like everything on this repository, which is what makes a
        callback naming another workspace's payment resolve to nothing rather
        than to that payment. The scoping is the isolation boundary here, not a
        convenience.
        """
        return await self._first(self._select().where(Payment.id == payment_id))

    async def count_abandoned(self, *, invoice_id: uuid.UUID) -> int:
        """How many attempts on this invoice never reached the provider.

        The widening in `RecurringService._abandon` is keyed on this rather
        than on a counter column, because these rows are already the record:
        each one is a committed payment in `ABANDONED`. A column beside them
        could disagree with them, and disagreeing with the ledger is the one
        thing a billing counter must not do.
        """
        statement = (
            select(func.count(Payment.id))
            .where(self._tenant_filter())
            .where(Payment.invoice_id == invoice_id)
            .where(Payment.collection_state == CollectionState.ABANDONED)
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def list_for_invoice(self, invoice_id: uuid.UUID) -> list[Payment]:
        """Every attempt, oldest first: the history is the point.

        Two attempts written in the same transaction share a `created_at` -
        PostgreSQL's `now()` is fixed for the transaction - so their order
        between themselves falls to a random primary key. That is a limitation
        of the ordering rather than of the record: real attempts are separated
        by a customer updating a card, and no attempt is ever lost.
        """
        return await self._all(
            self._select()
            .where(Payment.invoice_id == invoice_id)
            .order_by(Payment.created_at, Payment.id)
        )

    async def get_by_idempotency_key(self, key: str) -> Payment | None:
        """A payment already created for this caller's request key.

        Tenant-scoped, matching the unique constraint: one workspace's key
        never finds another's attempt.
        """
        return await self._first(self._select().where(Payment.idempotency_key == key))

    async def get_by_transaction(self, *, provider: str, transaction_id: str) -> Payment | None:
        """The attempt a provider transaction settled, if we have it.

        The fallback path for a reversal callback. A refund names the
        transaction it reverses rather than carrying our own reference home, so
        this is how such an event is tied back to a payment - still by an
        identifier we recorded ourselves when the money arrived, and still
        tenant-scoped.
        """
        return await self._first(
            self._select()
            .where(Payment.provider == provider)
            .where(Payment.provider_reference == transaction_id)
        )

    async def get_by_reference(self, *, provider: str, reference: str) -> Payment | None:
        """Find an attempt by the provider's own identifier.

        What makes a repeated webhook a no-op instead of a second payment.
        """
        return await self._first(
            self._select()
            .where(Payment.provider == provider)
            .where(Payment.provider_reference == reference)
        )

    def record(
        self,
        *,
        invoice_id: uuid.UUID,
        status: PaymentStatus,
        amount: Decimal,
        currency: str,
        provider: str,
        provider_reference: str | None = None,
        failure_reason: str | None = None,
        processed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> Payment:
        return self.add(
            Payment(
                tenant_id=self.tenant_id,
                invoice_id=invoice_id,
                status=status,
                amount=amount,
                currency=currency,
                provider=provider,
                provider_reference=provider_reference,
                failure_reason=failure_reason,
                processed_at=processed_at,
                idempotency_key=idempotency_key,
                refunded_amount=Decimal("0.00"),
            )
        )


class PlatformInvoiceRepository(BaseRepository[Invoice]):
    """Invoices across every workspace, for platform revenue reporting.

    Deliberately unscoped and deliberately its own class, like the platform
    usage and subscription readers.
    """

    model = Invoice

    async def revenue(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[RevenueTotal]:
        """What was actually collected in a window, by currency.

        Grouped by currency rather than summed into one figure: adding dollars
        to euros produces a number that is wrong in a way nobody can see. Only
        `paid` invoices count - an issued invoice is a hope, not revenue.
        """
        statement = (
            select(
                Invoice.currency,
                func.coalesce(func.sum(Invoice.amount_paid), 0),
                func.count(),
            )
            .where(Invoice.status == InvoiceStatus.PAID)
            .group_by(Invoice.currency)
            .order_by(Invoice.currency)
        )
        if since is not None:
            statement = statement.where(Invoice.paid_at >= since)
        if until is not None:
            statement = statement.where(Invoice.paid_at < until)

        result = await self.session.execute(statement)
        return [
            RevenueTotal(currency=row[0], amount=Decimal(row[1]), invoices=int(row[2]))
            for row in result.all()
        ]

    async def outstanding(self) -> list[RevenueTotal]:
        """What has been billed and not paid, by currency."""
        statement = (
            select(
                Invoice.currency,
                func.coalesce(func.sum(Invoice.amount_due - Invoice.amount_paid), 0),
                func.count(),
            )
            .where(Invoice.status == InvoiceStatus.OPEN)
            .group_by(Invoice.currency)
            .order_by(Invoice.currency)
        )
        result = await self.session.execute(statement)
        return [
            RevenueTotal(currency=row[0], amount=Decimal(row[1]), invoices=int(row[2]))
            for row in result.all()
        ]

    async def claim_by_id(
        self,
        invoice_id: uuid.UUID,
        *,
        max_attempts: int,
    ) -> Invoice | None:
        """Claim one invoice still worth a collection attempt.

        The predicate is repeated from `claim_collectible` rather than trusted
        from it: between the batch and this call the invoice may have been
        settled by a callback or attempted by another worker, and either makes
        a charge here wrong. Re-asking under the lock is what makes "one
        attempt reaches the provider" a property of the row (ADR-082).

        That now includes "and nobody is waiting to hear what the last attempt
        did". An invoice with an unresolved attempt is not collectible at any
        price - a second charge while the first outcome is unknown is the
        duplicate debit this whole protocol exists to prevent (ADR-088).
        """
        return await self._first(
            self._select()
            .where(Invoice.id == invoice_id)
            .where(Invoice.status == InvoiceStatus.OPEN)
            .where(Invoice.collection_attempts < max_attempts)
            .where(~_has_unresolved_attempt())
            .with_for_update(skip_locked=True, of=Invoice)
        )

    async def claim_overdue_pair(
        self,
        *,
        invoice_id: uuid.UUID,
        subscription_id: uuid.UUID,
        before: datetime,
        subscription_status: SubscriptionStatus,
        skip_unresolved: bool = False,
    ) -> tuple[Invoice, Subscription] | None:
        """Claim one overdue invoice and its subscription together.

        The single-row form of `claim_overdue`, with the same predicates, for
        the transaction that actually performs the transition. Both rows are
        locked because the invoice is the evidence and the subscription is what
        changes; claiming only one of them would let two workers holding two
        invoices of the same workspace both move it (ADR-082).
        """
        statement = (
            select(Invoice, Subscription)
            .join(Subscription, Subscription.id == Invoice.subscription_id)
            .where(Invoice.id == invoice_id)
            .where(Subscription.id == subscription_id)
            .where(Invoice.status == InvoiceStatus.OPEN)
            .where(Invoice.issued_at.is_not(None))
            .where(Invoice.issued_at < before)
            .where(Subscription.status == subscription_status)
            .with_for_update(skip_locked=True, of=(Invoice, Subscription))
        )
        if skip_unresolved:
            statement = statement.where(~_has_unresolved_attempt())
        row = (await self.session.execute(statement)).first()
        if row is None:
            return None
        invoice, subscription = row
        return invoice, subscription

    async def claim_collectible(
        self,
        *,
        before: datetime,
        max_attempts: int,
        limit: int = 200,
    ) -> Sequence[Invoice]:
        """Claim open invoices an automatic charge may be attempted against.

        Still a wide net rather than a precise one. Whether a particular invoice
        *should* be charged - a live subscription, a usable card - is decided by
        `RecurringService`, because those are billing rules and belong with the
        rules. This query's job is to avoid loading the whole table, and to hand
        each invoice to exactly one worker.

        `next_collection_at IS NULL` is included on purpose: that is the state
        of an invoice nobody has tried yet, which is exactly the one a first
        attempt is for.

        **An invoice with an unresolved attempt is excluded**, and that is the
        eligibility half of ADR-088. `next_collection_at` alone cannot express
        it: an attempt whose answer never arrived leaves a schedule that comes
        due on time while the question of whether a card was already debited is
        still open, and charging on schedule is exactly the wrong move. The
        state of the last attempt decides, not the clock.

        **`max_attempts` is passed in, not decided here** (ADR-082). It is the
        service's `MAX_COLLECTION_ATTEMPTS`, and the filter exists because
        without it a spent invoice stays eligible for ever - `next_collection_at`
        returns to NULL when the budget runs out - and takes a slot in every
        future batch. With a fixed limit and enough spent invoices, the ones
        behind them are never reached at all. The rule still lives in one place;
        this is the query being told what it is.

        `FOR UPDATE OF invoices SKIP LOCKED`, so two workers charge two
        different cards rather than the same one twice. The invoice is what a
        collection attempt belongs to, so the invoice is what is locked.
        """
        return await self._all(
            self._select()
            .where(Invoice.status == InvoiceStatus.OPEN)
            .where(Invoice.subscription_id.is_not(None))
            .where(Invoice.collection_attempts < max_attempts)
            .where(~_has_unresolved_attempt())
            .where((Invoice.next_collection_at.is_(None)) | (Invoice.next_collection_at <= before))
            .order_by(Invoice.period_start)
            .limit(limit)
            .with_for_update(skip_locked=True, of=Invoice)
        )

    async def claim_overdue(
        self,
        *,
        before: datetime,
        subscription_status: SubscriptionStatus,
        limit: int = 200,
        skip_unresolved: bool = False,
    ) -> list[tuple[Invoice, Subscription]]:
        """Claim overdue invoices whose subscription is in a given state.

        What both dunning sweeps read. Keyed on `issued_at` rather than
        `period_start`, because the grace a customer gets should run from the
        day they were asked for money - an invoice for a period that ended
        weeks ago is not weeks overdue if it was only issued yesterday.

        Invoices with no `issued_at` are excluded. Those are drafts and
        checkout-created rows that nobody has been billed for, and chasing
        somebody for a bill they were never sent is worse than not chasing.

        **`subscription_status` is in the query rather than checked after, and
        that is a starvation fix rather than a tidy-up** (ADR-082). Chasing an
        invoice moves its *subscription* to `PAST_DUE` and leaves the invoice
        exactly as overdue as it was. With the status checked in the worker the
        invoice stayed eligible for ever and kept its place at the front of
        every future batch, so a deployment with more than `limit` already-
        chased invoices never reached the ones behind them. Expressed here, a
        processed row leaves the eligible set and a pass can drain.

        **Both rows are locked, which is why the pair is returned.** A workspace
        can have several overdue invoices, so two workers claiming one each
        would both transition the same subscription, write two audit rows and
        race on the notice's idempotency key. The transition belongs to the
        subscription, so the subscription is claimed too - and `SKIP LOCKED`
        sends the second worker to the next workspace instead of queueing it
        behind the first.
        """
        statement = (
            select(Invoice, Subscription)
            .join(Subscription, Subscription.id == Invoice.subscription_id)
            .where(Invoice.status == InvoiceStatus.OPEN)
            .where(Invoice.issued_at.is_not(None))
            .where(Invoice.issued_at < before)
            .where(Subscription.status == subscription_status)
            .order_by(Invoice.issued_at)
            .limit(limit)
            .with_for_update(skip_locked=True, of=(Invoice, Subscription))
        )
        # `skip_unresolved` belongs to the suspension phase and to nothing else.
        # An invoice whose last collection attempt has no outcome may already
        # have been paid, and cutting somebody off over money they may have
        # sent is the second harm WSL-01 produced - the audit named it
        # alongside the duplicate charge (ADR-088).
        #
        # Chasing is deliberately not guarded. `PAST_DUE` still serves, and the
        # notice is what gets a person to look at an attempt nobody can
        # resolve - which is exactly what such an attempt needs.
        if skip_unresolved:
            statement = statement.where(~_has_unresolved_attempt())
        rows = await self.session.execute(statement)
        return [(invoice, subscription) for invoice, subscription in rows]

    async def due_for_period(
        self,
        *,
        period_start: datetime,
        limit: int = 200,
    ) -> Sequence[Invoice]:
        """Open invoices for a period, for a collection sweep."""
        return await self._all(
            self._select()
            .where(Invoice.status == InvoiceStatus.OPEN)
            .where(Invoice.period_start == period_start)
            .order_by(Invoice.period_start)
            .limit(limit)
        )


class PlatformPaymentRepository(BaseRepository[Payment]):
    """Collection attempts across every workspace, for reconciliation.

    Deliberately unscoped and deliberately its own class, in the same shape as
    `PlatformInvoiceRepository` and `PlatformMediaRepository` and for the same
    reason: this is a platform sweep over every workspace and nothing reachable
    from a request constructs it.

    The authorization question does not arise; the *ownership* question still
    does, and it is answered by the row. An attempt is reconciled because a row
    carrying its `tenant_id` says a charge may have been made, and every
    settlement that follows goes through a tenant-scoped service built from
    that value.
    """

    model = Payment

    async def claim_for_reconciliation(
        self,
        *,
        provider: str,
        older_than: datetime,
        lease_before: datetime,
        now: datetime,
    ) -> Payment | None:
        """Take the oldest attempt nobody knows the outcome of, and lease it.

        One row rather than a batch, because there is a provider round trip
        between claiming and deciding, and a batch would hold every claimed
        row for the sum of every lookup in it.

        **The lease is the claim, and it is committed by the caller before the
        lookup happens.** `FOR UPDATE SKIP LOCKED` alone would mean holding a
        row lock across a call to somebody else's API, which is exactly the
        thing the collection path was changed to stop doing; and a lock is held
        by a process, so it says nothing once that process is gone. Writing
        `reconciled_at` before asking gives both properties at once: a second
        worker skips a row someone is already asking about, and a worker that
        dies mid-lookup leaves one that becomes claimable again when the lease
        elapses - with no reaper needed to notice.

        `older_than` keeps this away from attempts a live worker is still
        finishing; `lease_before` is what a previous claim has to be older
        than. Both are computed by the caller from one clock, so a test can
        pin them.
        """
        statement = (
            select(Payment)
            .where(Payment.provider == provider)
            .where(Payment.collection_state.in_(UNRESOLVED_COLLECTION_STATES))
            .where(Payment.created_at < older_than)
            .where((Payment.reconciled_at.is_(None)) | (Payment.reconciled_at < lease_before))
            .order_by(Payment.created_at)
            .limit(1)
            .with_for_update(skip_locked=True, of=Payment)
        )
        payment = await self._first(statement)
        if payment is None:
            return None
        payment.reconciled_at = now
        await self.session.flush()
        return payment

    async def get_by_id(self, payment_id: uuid.UUID) -> Payment | None:
        """One attempt by id, across workspaces.

        Used only to re-read a row the reconciler already claimed, after the
        transaction that claimed it has ended. The tenant is read off the row,
        never supplied.
        """
        return await self._first(select(Payment).where(Payment.id == payment_id))

    async def unresolved_count(self, *, provider: str) -> int:
        """How many attempts are waiting for an answer, platform-wide.

        A level rather than an event, and the number an operator watches: it is
        normally zero, and a value that stays above zero means callbacks are
        not arriving or the provider cannot be asked.
        """
        statement = (
            select(func.count())
            .select_from(Payment)
            .where(Payment.provider == provider)
            .where(Payment.collection_state.in_(UNRESOLVED_COLLECTION_STATES))
        )
        return int(await self.session.scalar(statement) or 0)

    async def oldest_unresolved_at(self, *, provider: str) -> datetime | None:
        """When the oldest unanswered attempt was made, or None if there is none.

        Age is what makes the backlog alertable. One attempt outstanding for a
        minute is a callback in flight; one outstanding for a day is an invoice
        nobody can collect and possibly a customer who has already paid.
        """
        statement = (
            select(func.min(Payment.created_at))
            .where(Payment.provider == provider)
            .where(Payment.collection_state.in_(UNRESOLVED_COLLECTION_STATES))
        )
        oldest = await self.session.scalar(statement)
        return oldest if isinstance(oldest, datetime) else None

    async def release_attempt(self, *, invoice_id: uuid.UUID) -> None:
        """Hand an attempt back to an invoice whose charge was never sent.

        Only reachable where the provider has said it has no record of the
        reference, which is the one conclusion that means no money moved. The
        counter goes down and the schedule is cleared, so the ordinary sweep
        picks the invoice up again and makes a *new* attempt with a new number
        and a new reference - rather than resuming one whose reference the
        provider may yet decide it knows about.

        Guarded rather than blind: an attempt count that has already been
        adjusted must not go negative.
        """
        await self.session.execute(
            update(Invoice)
            .where(Invoice.id == invoice_id)
            .where(Invoice.collection_attempts > 0)
            .values(collection_attempts=Invoice.collection_attempts - 1, next_collection_at=None)
        )

"""Day-to-day billing operations for platform staff (BILL-10, -12, -13, -15).

Everything an operator needed SQL for: moving a subscriber, cancelling or
resuming one, recording a bank transfer, voiding a bill, refunding a payment,
finding a payment whose callback never arrived, and working through the
incidents the settlement engine raises.

The rule every method follows: **locate across workspaces, then act through
the tenant-scoped service.** The first lookup is the one unscoped read; from
there the workspace comes off the row and every change goes through the same
services a workspace's own requests use, so an operator action cannot reach a
second workspace and cannot take a path the customer's money does not.

Optimistic concurrency: every mutation of an existing subscription, invoice or
payment names the revision it was based on, and the row is locked while it is
compared. Every mutation is audited with actor, role, reason, before and after.

Nothing here can charge a card. Reconciliation asks; refunds reverse what was
collected; a manual payment records money an operator has seen.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.billing import (
    BillingAdjustment,
    BillingAdjustmentKind,
    Plan,
    PlanVersion,
    ScheduledChangeSource,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.billing_incident import (
    BillingIncident,
    BillingIncidentKind,
    BillingIncidentStatus,
)
from app.db.models.invoice import (
    UNRESOLVED_COLLECTION_STATES,
    Invoice,
    InvoicePurpose,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.db.models.user import User
from app.integrations.billing import build_checkout_provider
from app.platform.billing_audit import record_platform_billing
from app.repositories.invoice_repository import InvoiceRepository
from app.schemas.platform_billing import (
    ChangeMode,
    FinancialBasis,
    IncidentRead,
    InvoiceVoid,
    ManualPaymentCreate,
    PlatformInvoiceRead,
    PlatformPaymentRead,
    PlatformRefundCreate,
    PlatformSubscriptionRead,
    ReconciliationCategory,
    ReconciliationRunResult,
    ReconciliationView,
    SubscriptionCancel,
    SubscriptionChangePlan,
    SubscriptionResume,
    TimelineEntry,
)
from app.services import billing_calendar
from app.services.invoice_service import InvoiceService
from app.services.payment_reconciliation_service import PaymentReconciler
from app.services.plan_catalog import PlanCatalog
from app.services.refund_service import RefundService
from app.services.subscription_service import SubscriptionService

# A hosted payment still pending after this is worth an operator's attention.
STUCK_HOSTED_AFTER: Final = timedelta(minutes=15)
# An issued invoice still open after this is "stuck".
STUCK_INVOICE_AFTER: Final = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    total: int


def _subscription_state(subscription: Subscription) -> dict[str, Any]:
    return {
        "status": subscription.status.value,
        "plan_id": str(subscription.plan_id),
        "plan_version_id": (
            str(subscription.plan_version_id) if subscription.plan_version_id else None
        ),
        "current_period_start": subscription.current_period_start.isoformat(),
        "current_period_end": subscription.current_period_end.isoformat(),
        "cancel_at_period_end": subscription.cancel_at_period_end,
        "scheduled_plan_version_id": (
            str(subscription.scheduled_plan_version_id)
            if subscription.scheduled_plan_version_id
            else None
        ),
        "revision": subscription.revision,
    }


def _invoice_state(invoice: Invoice) -> dict[str, Any]:
    return {
        "status": invoice.status.value,
        "purpose": invoice.purpose.value,
        "amount_due": str(invoice.amount_due),
        "amount_paid": str(invoice.amount_paid),
        "revision": invoice.revision,
    }


def _payment_state(payment: Payment) -> dict[str, Any]:
    return {
        "status": payment.status.value,
        "amount": str(payment.amount),
        "refunded_amount": str(payment.refunded_amount),
        "provider": payment.provider,
        "provider_transaction_id": payment.provider_reference,
        "revision": payment.revision,
    }


async def _page[T](
    session: AsyncSession, statement: Select[tuple[T]], *, limit: int, offset: int
) -> Page[T]:
    total = int(await session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = (await session.scalars(statement.limit(limit).offset(offset))).all()
    return Page(items=list(rows), total=total)


class PlatformBillingOperations:
    """Subscriptions, invoices, payments, refunds and reconciliation, platform-wide."""

    def __init__(self, session: AsyncSession, *, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # ------------------------------------------------------- subscriptions

    async def list_subscriptions(
        self,
        *,
        tenant_id: uuid.UUID | None,
        plan_code: str | None,
        status: SubscriptionStatus | None,
        cancel_at_period_end: bool | None,
        renews_before: datetime | None,
        renews_after: datetime | None,
        limit: int,
        offset: int,
    ) -> Page[PlatformSubscriptionRead]:
        statement = select(Subscription)
        if tenant_id is not None:
            statement = statement.where(Subscription.tenant_id == tenant_id)
        if plan_code:
            statement = statement.join(Plan, Plan.id == Subscription.plan_id).where(
                Plan.code == plan_code.strip().lower()
            )
        if status is not None:
            statement = statement.where(Subscription.status == status)
        if cancel_at_period_end is not None:
            statement = statement.where(Subscription.cancel_at_period_end.is_(cancel_at_period_end))
        if renews_before is not None:
            statement = statement.where(Subscription.current_period_end < renews_before)
        if renews_after is not None:
            statement = statement.where(Subscription.current_period_end >= renews_after)
        page = await _page(
            self._session,
            statement.order_by(Subscription.current_period_end, Subscription.id),
            limit=limit,
            offset=offset,
        )
        return Page(
            items=[await self._read_subscription(row) for row in page.items], total=page.total
        )

    async def get_subscription(self, subscription_id: uuid.UUID) -> PlatformSubscriptionRead:
        return await self._read_subscription(await self._require_subscription(subscription_id))

    async def timeline(self, subscription_id: uuid.UUID) -> list[TimelineEntry]:
        """What happened to a subscription and its money, oldest first."""
        subscription = await self._require_subscription(subscription_id)
        entries: list[TimelineEntry] = []
        audits = (
            await self._session.scalars(
                select(AuditLog)
                .where(AuditLog.tenant_id == subscription.tenant_id)
                .where(AuditLog.target_type.in_(["subscription", "invoice", "payment"]))
                .order_by(AuditLog.occurred_at.desc())
                .limit(200)
            )
        ).all()
        entries.extend(
            TimelineEntry(
                occurred_at=row.occurred_at,
                kind="audit",
                action=row.action.value,
                actor=row.actor_label,
                target_id=row.target_id,
                detail=row.meta,
            )
            for row in audits
        )
        invoices = (
            await self._session.scalars(
                select(Invoice)
                .where(Invoice.subscription_id == subscription.id)
                .order_by(Invoice.created_at.desc())
                .limit(100)
            )
        ).all()
        entries.extend(
            TimelineEntry(
                occurred_at=row.created_at,
                kind="invoice",
                action=f"{row.purpose.value}:{row.status.value}",
                actor=None,
                target_id=row.id,
                detail={
                    "amount_due": str(row.amount_due),
                    "period_start": row.period_start.isoformat(),
                    "period_end": row.period_end.isoformat(),
                },
            )
            for row in invoices
        )
        return sorted(entries, key=lambda entry: entry.occurred_at)

    async def change_plan(
        self,
        subscription_id: uuid.UUID,
        payload: SubscriptionChangePlan,
        *,
        actor: User,
        now: datetime | None = None,
    ) -> PlatformSubscriptionRead:
        """Move a subscriber to a version - scheduled, or now with its funding named.

        `NEXT_RENEWAL` schedules the change for the period end: a cheaper
        version applies at the boundary, a pricier one is billed at the
        boundary and adopted once paid. `NOW` to a priced version must say
        what pays for it - a manual payment the operator has seen, or a
        complimentary grant recorded as one - and never fakes a Paymob payment.
        """
        moment = now if now is not None else datetime.now(UTC)
        subscription = await self._lock_subscription(
            subscription_id, expected_revision=payload.expected_revision
        )
        version = await self._session.get(PlanVersion, payload.plan_version_id)
        if version is None:
            raise NotFoundError("No such plan version.")
        # Another workspace's custom plan is refused before anything else is
        # considered (ADR-113): 422 `custom_plan_not_available_for_workspace`.
        await PlanCatalog(self._session).require_available(
            version, tenant_id=subscription.tenant_id
        )
        before = _subscription_state(subscription)
        service = SubscriptionService(self._session, tenant_id=subscription.tenant_id)

        if payload.mode is ChangeMode.NEXT_RENEWAL:
            await service.schedule_change(
                version=version,
                source=ScheduledChangeSource.OPERATOR,
                reason=payload.reason,
                now=moment,
                actor=actor,
                actor_kind=AuditActorKind.PLATFORM_STAFF,
            )
            action = AuditAction.SUBSCRIPTION_PLAN_CHANGE_SCHEDULED
        elif version.price <= 0:
            await service.apply_purchase(version=version, now=moment)
            action = AuditAction.SUBSCRIPTION_PLAN_CHANGED
        elif payload.financial_basis is FinancialBasis.COMPLIMENTARY:
            ends = payload.complimentary_until or billing_calendar.add_interval(
                moment, version.interval
            )
            if ends <= moment:
                raise ValidationError("complimentary_until must be in the future.")
            self._session.add(
                BillingAdjustment(
                    tenant_id=subscription.tenant_id,
                    subscription_id=subscription.id,
                    kind=BillingAdjustmentKind.COMPLIMENTARY_GRANT,
                    plan_version_id=version.id,
                    reason=payload.reason,
                    actor_id=actor.id,
                    starts_at=moment,
                    ends_at=ends,
                )
            )
            await service.apply_purchase(version=version, now=moment)
            action = AuditAction.SUBSCRIPTION_COMPLIMENTARY_GRANT
        elif payload.financial_basis is FinancialBasis.MANUAL_PAYMENT:
            details = payload.manual_payment
            if details is None:
                raise ValidationError("A manual payment needs its details.")
            if details.amount != version.price or details.currency.upper() != version.currency:
                raise ValidationError(
                    f"A manual payment for this version must be {version.price} {version.currency}."
                )
            plan = await self._session.get(Plan, version.plan_id)
            invoices = InvoiceRepository(self._session, tenant_id=subscription.tenant_id)
            invoice = invoices.create(
                subscription_id=subscription.id,
                plan_code=plan.code if plan is not None else "unknown",
                amount_due=version.price,
                currency=version.currency,
                period_start=moment,
                period_end=billing_calendar.add_interval(moment, version.interval),
                lines=[
                    {
                        "kind": "subscription",
                        "description": f"{version.name} plan",
                        "amount": str(version.price),
                        "quantity": 1,
                        "plan_version": version.version,
                        "interval": version.interval.value,
                    }
                ],
                status=InvoiceStatus.OPEN,
                purpose=InvoicePurpose.MANUAL,
                plan_version_id=version.id,
            )
            invoice.issued_at = moment
            invoice.created_at = moment
            await self._session.flush()
            await InvoiceService(self._session, tenant_id=subscription.tenant_id).record_payment(
                invoice_id=invoice.id,
                amount=details.amount,
                provider=details.method,
                reference=details.reference,
                now=moment,
                currency=details.currency,
                actor=actor,
            )
            action = AuditAction.SUBSCRIPTION_PLAN_CHANGED
        else:
            raise ValidationError(
                "Applying a priced plan now needs a financial_basis: manual_payment or "
                "complimentary."
            )

        await self._session.flush()
        record_platform_billing(
            self._session,
            action,
            actor=actor,
            reason=payload.reason,
            target_type="subscription",
            target_id=subscription.id,
            tenant_id=subscription.tenant_id,
            before=before,
            after=_subscription_state(subscription),
            extra={
                "mode": payload.mode.value,
                "financial_basis": (
                    payload.financial_basis.value if payload.financial_basis else None
                ),
                "plan_version_id": str(version.id),
            },
        )
        return await self._read_subscription(subscription)

    async def cancel_subscription(
        self,
        subscription_id: uuid.UUID,
        payload: SubscriptionCancel,
        *,
        actor: User,
    ) -> PlatformSubscriptionRead:
        subscription = await self._lock_subscription(
            subscription_id, expected_revision=payload.expected_revision
        )
        before = _subscription_state(subscription)
        await SubscriptionService(self._session, tenant_id=subscription.tenant_id).cancel(
            immediately=payload.immediately, actor=actor
        )
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.SUBSCRIPTION_CANCELLED,
            actor=actor,
            reason=payload.reason,
            target_type="subscription",
            target_id=subscription.id,
            tenant_id=subscription.tenant_id,
            before=before,
            after=_subscription_state(subscription),
        )
        return await self._read_subscription(subscription)

    async def resume_subscription(
        self,
        subscription_id: uuid.UUID,
        payload: SubscriptionResume,
        *,
        actor: User,
    ) -> PlatformSubscriptionRead:
        subscription = await self._lock_subscription(
            subscription_id, expected_revision=payload.expected_revision
        )
        before = _subscription_state(subscription)
        await SubscriptionService(self._session, tenant_id=subscription.tenant_id).resume(
            actor=actor
        )
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.SUBSCRIPTION_RESUMED,
            actor=actor,
            reason=payload.reason,
            target_type="subscription",
            target_id=subscription.id,
            tenant_id=subscription.tenant_id,
            before=before,
            after=_subscription_state(subscription),
        )
        return await self._read_subscription(subscription)

    # ------------------------------------------------------------ invoices

    async def list_invoices(
        self,
        *,
        tenant_id: uuid.UUID | None,
        subscription_id: uuid.UUID | None,
        status: InvoiceStatus | None,
        purpose: InvoicePurpose | None,
        plan_code: str | None,
        issued_from: datetime | None,
        issued_until: datetime | None,
        overdue_before: datetime | None,
        limit: int,
        offset: int,
    ) -> Page[PlatformInvoiceRead]:
        statement = select(Invoice)
        if tenant_id is not None:
            statement = statement.where(Invoice.tenant_id == tenant_id)
        if subscription_id is not None:
            statement = statement.where(Invoice.subscription_id == subscription_id)
        if status is not None:
            statement = statement.where(Invoice.status == status)
        if purpose is not None:
            statement = statement.where(Invoice.purpose == purpose)
        if plan_code:
            statement = statement.where(Invoice.plan_code == plan_code.strip().lower())
        if issued_from is not None:
            statement = statement.where(Invoice.issued_at >= issued_from)
        if issued_until is not None:
            statement = statement.where(Invoice.issued_at < issued_until)
        if overdue_before is not None:
            statement = statement.where(Invoice.status == InvoiceStatus.OPEN).where(
                Invoice.issued_at < overdue_before
            )
        page = await _page(
            self._session,
            statement.order_by(Invoice.created_at.desc(), Invoice.id),
            limit=limit,
            offset=offset,
        )
        return Page(
            items=[PlatformInvoiceRead.from_model(row) for row in page.items], total=page.total
        )

    async def get_invoice(self, invoice_id: uuid.UUID) -> PlatformInvoiceRead:
        return PlatformInvoiceRead.from_model(await self._require_invoice(invoice_id))

    async def record_manual_payment(
        self,
        invoice_id: uuid.UUID,
        payload: ManualPaymentCreate,
        *,
        actor: User,
    ) -> PlatformPaymentRead:
        """Money an operator has seen arrive, settled like any other (BILL-10)."""
        invoice = await self._lock_invoice(invoice_id)
        before = _invoice_state(invoice)
        payment = await InvoiceService(self._session, tenant_id=invoice.tenant_id).record_payment(
            invoice_id=invoice.id,
            amount=payload.amount,
            provider=payload.method,
            reference=payload.reference,
            now=payload.occurred_at,
            currency=payload.currency,
            expected_revision=payload.expected_revision,
            recover_uncollectible=payload.recover_uncollectible,
            actor=actor,
        )
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.PAYMENT_RECORDED,
            actor=actor,
            reason=payload.reason,
            target_type="invoice",
            target_id=invoice.id,
            tenant_id=invoice.tenant_id,
            before=before,
            after=_invoice_state(invoice),
            extra={
                "payment_id": str(payment.id),
                "method": payload.method,
                "reference": payload.reference,
                "source": "manual",
            },
        )
        return PlatformPaymentRead.from_model(payment)

    async def void_invoice(
        self,
        invoice_id: uuid.UUID,
        payload: InvoiceVoid,
        *,
        actor: User,
    ) -> PlatformInvoiceRead:
        invoice = await self._lock_invoice(invoice_id)
        before = _invoice_state(invoice)
        voided = await InvoiceService(self._session, tenant_id=invoice.tenant_id).void(
            invoice.id,
            reason=payload.reason,
            subscription_policy=payload.subscription_policy,
            expected_revision=payload.expected_revision,
            actor=actor,
        )
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.INVOICE_VOIDED,
            actor=actor,
            reason=payload.reason,
            target_type="invoice",
            target_id=invoice.id,
            tenant_id=invoice.tenant_id,
            before=before,
            after=_invoice_state(voided),
            extra={"subscription_policy": payload.subscription_policy},
        )
        return PlatformInvoiceRead.from_model(voided)

    # ------------------------------------------------------------ payments

    async def list_payments(
        self,
        *,
        tenant_id: uuid.UUID | None,
        invoice_id: uuid.UUID | None,
        status: PaymentStatus | None,
        provider: str | None,
        automatic: bool | None,
        limit: int,
        offset: int,
    ) -> Page[PlatformPaymentRead]:
        statement = select(Payment)
        if tenant_id is not None:
            statement = statement.where(Payment.tenant_id == tenant_id)
        if invoice_id is not None:
            statement = statement.where(Payment.invoice_id == invoice_id)
        if status is not None:
            statement = statement.where(Payment.status == status)
        if provider:
            statement = statement.where(Payment.provider == provider)
        if automatic is not None:
            statement = statement.where(Payment.is_automatic.is_(automatic))
        page = await _page(
            self._session,
            statement.order_by(Payment.created_at.desc(), Payment.id),
            limit=limit,
            offset=offset,
        )
        return Page(
            items=[PlatformPaymentRead.from_model(row) for row in page.items], total=page.total
        )

    async def get_payment(self, payment_id: uuid.UUID) -> PlatformPaymentRead:
        return PlatformPaymentRead.from_model(await self._require_payment(payment_id))

    async def refund(
        self,
        payment_id: uuid.UUID,
        payload: PlatformRefundCreate,
        *,
        actor: User,
    ) -> PlatformPaymentRead:
        """Ask the provider to return some or all of a payment (BILL-13).

        The only door that moves money back. Entitlements follow only the
        provider's signed confirmation.
        """
        payment = await self._require_payment(payment_id)
        before = _payment_state(payment)
        service = RefundService(
            self._session,
            tenant_id=payment.tenant_id,
            provider=build_checkout_provider(self._settings),
        )
        refunded = await service.refund(
            payment.id,
            actor=actor,
            reason=payload.reason,
            amount=payload.amount,
            currency=payload.currency,
            expected_revision=payload.expected_revision,
        )
        record_platform_billing(
            self._session,
            AuditAction.PAYMENT_REFUND_REQUESTED,
            actor=actor,
            reason=payload.reason,
            target_type="payment",
            target_id=payment.id,
            tenant_id=payment.tenant_id,
            before=before,
            after=_payment_state(refunded),
            extra={
                "requested_amount": str(payload.amount),
                "provider_refund_reference": refunded.refund_reference,
            },
        )
        return PlatformPaymentRead.from_model(refunded)

    # ------------------------------------------------------- reconciliation

    async def reconciliation(self, *, now: datetime | None = None) -> ReconciliationView:
        moment = now if now is not None else datetime.now(UTC)
        hosted = (
            select(Payment)
            .where(Payment.is_automatic.is_(False))
            .where(Payment.status == PaymentStatus.PENDING)
            .where(Payment.created_at < moment - STUCK_HOSTED_AFTER)
        )
        automatic = select(Payment).where(
            Payment.collection_state.in_(UNRESOLVED_COLLECTION_STATES)
        )
        incidents = select(BillingIncident).where(
            BillingIncident.status == BillingIncidentStatus.OPEN
        )
        stuck = (
            select(Invoice)
            .where(Invoice.status == InvoiceStatus.OPEN)
            .where(Invoice.issued_at < moment - STUCK_INVOICE_AFTER)
        )

        async def count(statement: Select[Any]) -> int:
            return int(
                await self._session.scalar(select(func.count()).select_from(statement.subquery()))
                or 0
            )

        kind_rows = await self._session.execute(
            select(BillingIncident.kind, func.count())
            .where(
                or_(
                    BillingIncident.status == BillingIncidentStatus.OPEN,
                    BillingIncident.kind == BillingIncidentKind.RECOVERED_BY_RECONCILIATION,
                )
            )
            .group_by(BillingIncident.kind)
        )
        by_kind: dict[BillingIncidentKind, int] = {row[0]: int(row[1]) for row in kind_rows.all()}
        categories = [
            ReconciliationCategory(
                category="pending_hosted_payment",
                count=await count(hosted),
                description="Hosted checkouts pending past the grace period.",
            ),
            ReconciliationCategory(
                category="unresolved_automatic_attempt",
                count=await count(automatic),
                description="Automatic charges whose outcome is unknown; no card is re-charged.",
            ),
            ReconciliationCategory(
                category="stuck_invoice",
                count=await count(stuck),
                description="Issued invoices open for more than thirty days.",
            ),
        ] + [
            ReconciliationCategory(
                category=kind.value,
                count=int(by_kind.get(kind, 0)),
                description=f"Billing incidents of kind {kind.value}.",
            )
            for kind in BillingIncidentKind
        ]
        return ReconciliationView(
            generated_at=moment,
            categories=categories,
            pending_hosted_payments=[
                PlatformPaymentRead.from_model(row)
                for row in (
                    await self._session.scalars(hosted.order_by(Payment.created_at).limit(20))
                ).all()
            ],
            unresolved_automatic_attempts=[
                PlatformPaymentRead.from_model(row)
                for row in (
                    await self._session.scalars(automatic.order_by(Payment.created_at).limit(20))
                ).all()
            ],
            open_incidents=[
                IncidentRead.from_model(row)
                for row in (
                    await self._session.scalars(
                        incidents.order_by(BillingIncident.created_at.desc()).limit(50)
                    )
                ).all()
            ],
        )

    async def run_reconciliation(
        self,
        payment_id: uuid.UUID,
        *,
        actor: User,
        now: datetime | None = None,
    ) -> ReconciliationRunResult:
        """Ask the provider about one payment and apply the answer. Never charges."""
        moment = now if now is not None else datetime.now(UTC)
        payment = await self._require_payment(payment_id)
        provider = build_checkout_provider(self._settings)
        if provider is None:
            raise ValidationError("No payment provider is configured.")
        record_platform_billing(
            self._session,
            AuditAction.BILLING_RECONCILIATION_STARTED,
            actor=actor,
            reason="Operator-requested provider inquiry.",
            target_type="payment",
            target_id=payment.id,
            tenant_id=payment.tenant_id,
            before=_payment_state(payment),
        )
        tenant_id = payment.tenant_id
        await self._session.commit()
        reconciler = PaymentReconciler(
            session=self._session,
            provider=provider,
            default_plan_code=self._settings.default_plan_code,
            settings=self._settings,
        )
        verdict = await reconciler.reconcile_payment(payment_id, now=moment)
        refreshed = await self._session.get(Payment, payment_id)
        if refreshed is not None:
            await self._session.refresh(refreshed)
        record_platform_billing(
            self._session,
            AuditAction.BILLING_RECONCILIATION_RESOLVED,
            actor=actor,
            reason="Operator-requested provider inquiry.",
            target_type="payment",
            target_id=payment_id,
            tenant_id=tenant_id,
            after=_payment_state(refreshed) if refreshed is not None else None,
            extra={"verdict": verdict},
        )
        return ReconciliationRunResult(
            payment_id=payment_id,
            verdict=verdict,
            payment=PlatformPaymentRead.from_model(refreshed) if refreshed is not None else None,
        )

    # ------------------------------------------------------------ incidents

    async def list_incidents(
        self,
        *,
        status: BillingIncidentStatus | None,
        kind: BillingIncidentKind | None,
        tenant_id: uuid.UUID | None,
        limit: int,
        offset: int,
    ) -> Page[IncidentRead]:
        statement = select(BillingIncident)
        if status is not None:
            statement = statement.where(BillingIncident.status == status)
        if kind is not None:
            statement = statement.where(BillingIncident.kind == kind)
        if tenant_id is not None:
            statement = statement.where(BillingIncident.tenant_id == tenant_id)
        page = await _page(
            self._session,
            statement.order_by(BillingIncident.created_at.desc()),
            limit=limit,
            offset=offset,
        )
        return Page(items=[IncidentRead.from_model(row) for row in page.items], total=page.total)

    async def resolve_incident(
        self,
        incident_id: uuid.UUID,
        *,
        note: str,
        actor: User,
        now: datetime | None = None,
    ) -> IncidentRead:
        incident = await self._session.scalar(
            select(BillingIncident).where(BillingIncident.id == incident_id).with_for_update()
        )
        if incident is None:
            raise NotFoundError("No such incident.")
        if incident.status is BillingIncidentStatus.RESOLVED:
            raise ConflictError("This incident is already resolved.")
        incident.status = BillingIncidentStatus.RESOLVED
        incident.resolved_at = now if now is not None else datetime.now(UTC)
        incident.resolved_by = actor.id
        incident.resolution_note = note
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.BILLING_INCIDENT_RESOLVED,
            actor=actor,
            reason=note,
            target_type="billing_incident",
            target_id=incident.id,
            tenant_id=incident.tenant_id,
            extra={"kind": incident.kind.value},
        )
        return IncidentRead.from_model(incident)

    # --------------------------------------------------------------- helpers

    async def _read_subscription(self, subscription: Subscription) -> PlatformSubscriptionRead:
        plan = await self._session.get(Plan, subscription.plan_id)
        version = (
            await self._session.get(PlanVersion, subscription.plan_version_id)
            if subscription.plan_version_id
            else None
        )
        return PlatformSubscriptionRead.build(
            subscription,
            plan_code=plan.code if plan is not None else None,
            version=version.version if version is not None else None,
        )

    async def _require_subscription(self, subscription_id: uuid.UUID) -> Subscription:
        subscription = await self._session.get(Subscription, subscription_id)
        if subscription is None:
            raise NotFoundError("No such subscription.")
        return subscription

    async def _lock_subscription(
        self, subscription_id: uuid.UUID, *, expected_revision: int
    ) -> Subscription:
        subscription = await self._session.scalar(
            select(Subscription).where(Subscription.id == subscription_id).with_for_update()
        )
        if subscription is None:
            raise NotFoundError("No such subscription.")
        await self._session.refresh(subscription)
        if subscription.revision != expected_revision:
            raise ConflictError(
                f"The subscription has changed (revision {subscription.revision}); "
                "reload it and retry."
            )
        return subscription

    async def _require_invoice(self, invoice_id: uuid.UUID) -> Invoice:
        invoice = await self._session.get(Invoice, invoice_id)
        if invoice is None:
            raise NotFoundError("No such invoice.")
        return invoice

    async def _lock_invoice(self, invoice_id: uuid.UUID) -> Invoice:
        invoice = await self._session.scalar(
            select(Invoice).where(Invoice.id == invoice_id).with_for_update()
        )
        if invoice is None:
            raise NotFoundError("No such invoice.")
        await self._session.refresh(invoice)
        return invoice

    async def _require_payment(self, payment_id: uuid.UUID) -> Payment:
        payment = await self._session.get(Payment, payment_id)
        if payment is None:
            raise NotFoundError("No such payment.")
        return payment


__all__ = ["Page", "PlatformBillingOperations"]

"""Creating and assigning a workspace's custom plan (ADR-113).

An orchestration over what already exists, and nothing more:

    validate the workspace
      -> PlanCatalogAdmin.create        (a TENANT plan and its immutable v1)
      -> PlatformBillingOperations.change_plan   (optional: now, or at renewal)
      -> audit
      -> the full commercial picture, previewed before anything was written

No second plan model, no second assignment path. A *priced* custom plan applied
now is never granted for free: it needs a manual payment the operator has seen,
a complimentary grant recorded as one, or the customer's own checkout - in which
case nothing is assigned here and settlement applies the plan when the money is
confirmed. Changing a custom plan's price, interval or limits later is an
ordinary version publish, and its subscriber stays on the version they hold
until migrated.

A custom plan sets the seven limits in `CUSTOM_PLAN_KEYS` and inherits every
other limit (agents, owned workspaces) from the plan the workspace holds, so a
custom plan never silently makes something unlimited.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError, WaslaError
from app.core.logging import get_logger
from app.core.telemetry import record_custom_plan
from app.db.models.audit import AuditAction
from app.db.models.billing import (
    RESOURCE_LIMITS,
    LimitKey,
    PlanScope,
    PlanVersion,
    Subscription,
)
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.platform.billing_audit import record_platform_billing
from app.platform.billing_operations import PlatformBillingOperations
from app.platform.plan_admin import PlanCatalogAdmin
from app.repositories.billing_repository import (
    PlanRepository,
    PlanVersionRepository,
    SubscriptionRepository,
)
from app.schemas.custom_plan import (
    CUSTOM_PLAN_KEYS,
    AssignmentMode,
    CurrentPlanRead,
    CustomPlanAssignment,
    CustomPlanBasis,
    CustomPlanCreate,
    CustomPlanPreview,
    CustomPlanPreviewRequest,
    CustomPlanResult,
    CustomPlanTenantRead,
    CustomPlanTerms,
    EstimatedCharge,
    LimitComparison,
)
from app.schemas.platform_billing import (
    ChangeMode,
    PlanCreate,
    PlanVersionRead,
    SubscriptionChangePlan,
)
from app.services import billing_calendar
from app.services.entitlement_service import EntitlementService
from app.services.plan_catalog import PlanCatalog

logger = get_logger(__name__)


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


class CustomPlanAdmin:
    """Preview, create and assign one workspace's custom plan."""

    def __init__(self, session: AsyncSession, *, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._plans = PlanRepository(session)
        self._catalog = PlanCatalog(session)

    # --------------------------------------------------------------- preview

    async def preview(
        self,
        tenant_id: uuid.UUID,
        payload: CustomPlanPreviewRequest,
        *,
        now: datetime | None = None,
    ) -> CustomPlanPreview:
        """What these terms would mean for this workspace. Writes nothing."""
        moment = now if now is not None else datetime.now(UTC)
        try:
            result = await self._preview(tenant_id, payload, now=moment)
        except WaslaError:
            await record_custom_plan("preview", "refused")
            raise
        await record_custom_plan("preview", "succeeded")
        return result

    async def _preview(
        self,
        tenant_id: uuid.UUID,
        payload: CustomPlanTerms,
        *,
        now: datetime,
        basis: CustomPlanBasis | None = None,
        assigning: bool = True,
        complimentary_until: datetime | None = None,
    ) -> CustomPlanPreview:
        tenant = await self._tenant(tenant_id)
        subscription = await SubscriptionRepository(self._session, tenant_id=tenant.id).get()
        current = await self._current_terms(subscription)
        entitlements = EntitlementService(
            self._session,
            tenant_id=tenant.id,
            default_plan_code=self._settings.default_plan_code,
            clock=lambda: now,
        )
        proposed = payload.limits()
        warnings: list[str] = []
        comparisons: list[LimitComparison] = []
        for key in CUSTOM_PLAN_KEYS:
            standing = await entitlements.check(key, additional=0)
            old = current.limit_for(key) if current is not None else None
            new = proposed[key.value]
            topped = standing.topup_limit + standing.grant_limit
            below = new is not None and standing.used > new
            if below:
                warnings.append(
                    f"{key.value}: {standing.used} already in use against a proposed {new}. "
                    "Nothing is removed; adding more is refused until usage fits."
                )
            comparisons.append(
                LimitComparison(
                    key=key,
                    kind="capacity" if key in RESOURCE_LIMITS else "usage",
                    current=old,
                    proposed=new,
                    difference=new - old if new is not None and old is not None else None,
                    used=standing.used,
                    active_topups=topped,
                    proposed_effective=None if new is None else new + topped,
                    below_current_usage=below,
                )
            )
        if current is None:
            warnings.append(
                "The workspace holds no plan to inherit agent and workspace limits from; "
                "the custom plan leaves them unlimited."
            )
        if proposed[LimitKey.PERIOD_MESSAGES.value] is not None:
            warnings.append(
                "period_messages is metered, never enforced: no customer message is refused "
                "over it."
            )

        effective_at: datetime | None
        if payload.assignment_mode is AssignmentMode.NOW:
            effective_at = now
        elif subscription is not None:
            effective_at = subscription.current_period_end
        else:
            effective_at = None
            warnings.append(
                "The workspace has no subscription, so there is no renewal to wait for."
            )

        return CustomPlanPreview(
            tenant=CustomPlanTenantRead(
                id=tenant.id, name=tenant.name, slug=tenant.slug, status=tenant.status
            ),
            current_plan=await self._current_read(current),
            proposed_code=payload.code.strip().lower(),
            proposed_name=payload.name,
            proposed_price=_money(payload.price),
            currency=payload.currency,
            interval=payload.billing_interval,
            limits=comparisons,
            inherited_limits=self._inherited(current),
            effective_mode=payload.assignment_mode,
            effective_at=effective_at,
            estimated_next_charge=self._next_charge(
                payload,
                subscription=subscription,
                now=now,
                basis=basis,
                assigning=assigning,
                complimentary_until=complimentary_until,
            ),
            warnings=warnings,
        )

    def _next_charge(
        self,
        payload: CustomPlanTerms,
        *,
        subscription: Subscription | None,
        now: datetime,
        basis: CustomPlanBasis | None,
        assigning: bool,
        complimentary_until: datetime | None,
    ) -> EstimatedCharge | None:
        """The next money this plan would ask the customer for, and when."""
        if not assigning:
            return None
        price = _money(payload.price)
        if payload.price <= 0:
            return EstimatedCharge(
                amount="0.00", currency=payload.currency, due_at=None, basis="none"
            )
        if payload.assignment_mode is AssignmentMode.NEXT_RENEWAL:
            due = subscription.current_period_end if subscription is not None else None
            return EstimatedCharge(
                amount=price, currency=payload.currency, due_at=due, basis="renewal"
            )
        if basis is CustomPlanBasis.COMPLIMENTARY:
            until = complimentary_until or billing_calendar.add_interval(
                now, payload.billing_interval
            )
            return EstimatedCharge(
                amount=price, currency=payload.currency, due_at=until, basis="complimentary"
            )
        if basis is CustomPlanBasis.MANUAL_PAYMENT:
            return EstimatedCharge(
                amount=price, currency=payload.currency, due_at=now, basis="manual_payment"
            )
        return EstimatedCharge(
            amount=price, currency=payload.currency, due_at=now, basis="checkout"
        )

    # ---------------------------------------------------------------- create

    async def create(
        self,
        tenant_id: uuid.UUID,
        payload: CustomPlanCreate,
        *,
        actor: User,
        now: datetime | None = None,
    ) -> CustomPlanResult:
        """Create the custom plan, and assign or schedule it if asked."""
        moment = now if now is not None else datetime.now(UTC)
        try:
            result = await self._create(tenant_id, payload, actor=actor, now=moment)
        except WaslaError:
            await record_custom_plan("create", "refused")
            raise
        except Exception:
            await record_custom_plan("create", "failed")
            raise
        await record_custom_plan("create", "succeeded")
        if result.assignment.status == "assigned":
            await record_custom_plan("assign", "succeeded")
        elif result.assignment.status == "scheduled":
            await record_custom_plan("schedule", "succeeded")
        return result

    async def _create(
        self,
        tenant_id: uuid.UUID,
        payload: CustomPlanCreate,
        *,
        actor: User,
        now: datetime,
    ) -> CustomPlanResult:
        tenant = await self._tenant(tenant_id)
        subscription = await SubscriptionRepository(self._session, tenant_id=tenant.id).get()
        if payload.assign_to_tenant and subscription is None:
            raise ConflictError("The workspace has no subscription to put on this plan.")
        if (
            payload.effective_at is not None
            and subscription is not None
            and payload.assign_to_tenant
        ):
            takes_effect = (
                now
                if payload.assignment_mode is AssignmentMode.NOW
                else subscription.current_period_end
            )
            moment = (
                payload.effective_at
                if payload.effective_at.tzinfo
                else payload.effective_at.replace(tzinfo=UTC)
            )
            if moment > takes_effect:
                raise ValidationError(
                    "effective_at is after the assignment would take effect; the workspace "
                    "cannot be put on terms that are not yet in force."
                )

        preview = await self._preview(
            tenant.id,
            payload,
            now=now,
            basis=payload.financial_basis,
            assigning=payload.assign_to_tenant,
            complimentary_until=payload.complimentary_until,
        )
        current = await self._current_terms(subscription)
        limits: dict[str, int | None] = {
            key: value for key, value in self._inherited(current).items() if value is not None
        }
        limits.update({key: value for key, value in payload.limits().items() if value is not None})

        plans = PlanCatalogAdmin(self._session)
        plan_read = await plans.create(
            PlanCreate(
                code=payload.code,
                name=payload.name,
                description=payload.description,
                price=payload.price,
                currency=payload.currency,
                interval=payload.billing_interval,
                limits=limits,
                effective_at=payload.effective_at,
                scope=PlanScope.TENANT,
                tenant_id=tenant.id,
                is_public=False,
                reason=payload.reason,
            ),
            actor=actor,
            now=now,
        )
        version = await PlanVersionRepository(self._session).latest(plan_read.id)
        if version is None:  # pragma: no cover - create() has just written v1
            raise NotFoundError("The custom plan has no version.")

        assignment = CustomPlanAssignment(mode=None, status="not_assigned", subscription=None)
        if payload.assign_to_tenant and subscription is not None:
            assignment = await self._assign(
                payload, subscription=subscription, version=version, actor=actor, now=now
            )

        plan_read = await plans.get(plan_read.id)
        return CustomPlanResult(
            plan=plan_read,
            version=PlanVersionRead.from_model(version),
            assignment=assignment,
            preview=preview,
        )

    async def _assign(
        self,
        payload: CustomPlanCreate,
        *,
        subscription: Subscription,
        version: PlanVersion,
        actor: User,
        now: datetime,
    ) -> CustomPlanAssignment:
        """Put the workspace on version 1 through the ordinary subscription change."""
        if (
            payload.assignment_mode is AssignmentMode.NOW
            and payload.price > 0
            and payload.financial_basis is CustomPlanBasis.CUSTOMER_CHECKOUT
        ):
            # Nothing is granted here. The owner buys it at checkout - it is on
            # their catalogue and on nobody else's - and settlement applies it.
            self._audit_assignment(
                AuditAction.BILLING_CUSTOM_PLAN_ASSIGNMENT_SCHEDULED,
                payload,
                subscription=subscription,
                version=version,
                actor=actor,
                extra={"awaiting": "customer_checkout"},
            )
            return CustomPlanAssignment(
                mode=payload.assignment_mode,
                status="awaiting_customer_checkout",
                subscription=None,
            )

        operations = PlatformBillingOperations(self._session, settings=self._settings)
        changed = await operations.change_plan(
            subscription.id,
            SubscriptionChangePlan(
                plan_version_id=version.id,
                mode=(
                    ChangeMode.NOW
                    if payload.assignment_mode is AssignmentMode.NOW
                    else ChangeMode.NEXT_RENEWAL
                ),
                financial_basis=payload.operator_basis,
                manual_payment=payload.manual_payment,
                complimentary_until=payload.complimentary_until,
                reason=payload.reason,
                expected_revision=payload.expected_subscription_revision or 0,
            ),
            actor=actor,
            now=now,
        )
        scheduled = payload.assignment_mode is AssignmentMode.NEXT_RENEWAL
        self._audit_assignment(
            (
                AuditAction.BILLING_CUSTOM_PLAN_ASSIGNMENT_SCHEDULED
                if scheduled
                else AuditAction.BILLING_CUSTOM_PLAN_ASSIGNED
            ),
            payload,
            subscription=subscription,
            version=version,
            actor=actor,
            extra={"subscription_revision": changed.revision},
        )
        return CustomPlanAssignment(
            mode=payload.assignment_mode,
            status="scheduled" if scheduled else "assigned",
            subscription=changed,
        )

    def _audit_assignment(
        self,
        action: AuditAction,
        payload: CustomPlanCreate,
        *,
        subscription: Subscription,
        version: PlanVersion,
        actor: User,
        extra: dict[str, Any],
    ) -> None:
        record_platform_billing(
            self._session,
            action,
            actor=actor,
            reason=payload.reason,
            target_type="subscription",
            target_id=subscription.id,
            tenant_id=subscription.tenant_id,
            target_label=payload.code,
            after={
                "plan_version_id": str(version.id),
                "version": version.version,
                "price": str(version.price),
                "interval": version.interval.value,
                "mode": payload.assignment_mode.value,
                "financial_basis": (
                    payload.financial_basis.value if payload.financial_basis else None
                ),
            },
            extra=extra,
        )

    # --------------------------------------------------------------- helpers

    async def _tenant(self, tenant_id: uuid.UUID) -> Tenant:
        tenant = await self._session.get(Tenant, tenant_id)
        if tenant is None or tenant.deleted_at is not None:
            raise NotFoundError("No such workspace.")
        return tenant

    async def _current_terms(self, subscription: Subscription | None) -> PlanVersion | None:
        """The version the workspace is held to now, or the default plan's."""
        if subscription is not None:
            return await self._catalog.pinned_version(subscription)
        code = self._settings.default_plan_code
        plan = await self._plans.get_by_code(code) if code else None
        return await self._catalog.current_version(plan) if plan is not None else None

    async def _current_read(self, version: PlanVersion | None) -> CurrentPlanRead | None:
        if version is None:
            return None
        plan = await self._plans.get_by_id(version.plan_id)
        if plan is None:  # pragma: no cover - RESTRICT makes this unreachable
            return None
        return CurrentPlanRead(
            plan_id=plan.id,
            code=plan.code,
            name=version.name,
            scope=plan.scope,
            version=version.version,
            price=_money(version.price),
            currency=version.currency,
            interval=version.interval,
        )

    @staticmethod
    def _inherited(current: PlanVersion | None) -> dict[str, int | None]:
        """The limits a custom plan does not set, kept from the plan held now."""
        return {
            key.value: (current.limit_for(key) if current is not None else None)
            for key in LimitKey
            if key not in CUSTOM_PLAN_KEYS
        }


__all__ = ["CustomPlanAdmin"]

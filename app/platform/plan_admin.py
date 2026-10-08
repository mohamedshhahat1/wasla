"""Running the plan catalogue without SQL (BILL-12).

Before this there was no way to create, reprice, retire or inspect a plan except
by editing `plans` with SQL or shipping a migration - no validation, no audit,
no optimistic concurrency, and a price edit silently re-priced every existing
subscriber's next renewal.

The contract this service enforces:

* **A plan's identity is stable.** Its `code` is written once and never
  renamed; name, description, visibility and order are presentation and may
  change (`update`).
* **Entitlements change only by publishing a version** (`create_version`), and
  **prices only by publishing a price** (`create_price`) and retiring the old
  one (`retire_price`) - never by editing a row (ADR-116). A version may be
  sold monthly and yearly at once, with the same limits on both.
  Versions and prices are immutable. A new version applies to *new* checkouts from its
  `effective_at`; existing subscribers stay on the version they hold until an
  operator schedules a migration (`schedule_migration`), which is applied at
  each subscriber's own next renewal and only adopted once that renewal is
  paid.
* **Every change is serialised and audited.** The plan row is locked for the
  change, the operator's `expected_revision` / `expected_version` must match
  (409 otherwise, never a silent last-write-wins), and the audit entry records
  actor, role, reason, before and after.
* **Retire, don't delete.** Deactivating stops new checkouts and changes nobody
  who already holds the plan. Only a plan nothing has ever referenced can be
  deleted, and only by a platform owner.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, NoReturn

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.audit import AuditAction
from app.db.models.billing import (
    RESOURCE_LIMITS,
    SERVING_STATUSES,
    BillingInterval,
    LimitKey,
    Plan,
    PlanPrice,
    PlanScope,
    PlanVersion,
    PlanVersionMigration,
    Subscription,
)
from app.db.models.channel import Channel
from app.db.models.invoice import Invoice
from app.db.models.tenant import Tenant
from app.db.models.topup import SELLABLE_TOPUP_ENTITLEMENTS
from app.db.models.user import User
from app.platform.billing_audit import record_platform_billing
from app.repositories.billing_repository import (
    PlanPriceRepository,
    PlanVersionMigrationRepository,
    PlanVersionRepository,
)
from app.schemas.platform_billing import (
    ChannelTypesChange,
    FeatureRead,
    LimitChange,
    MigrationRead,
    PlanCreate,
    PlanMigrationCreate,
    PlanPriceCreate,
    PlanPriceRead,
    PlanPriceRetire,
    PlanUpdate,
    PlanVersionCreate,
    PlanVersionPreview,
    PlanVersionPreviewRequest,
    PlanVersionRead,
    PlatformPlanRead,
    _Terms,
)
from app.services.entitlement_terms import ordered, term_channel_types, term_limit
from app.services.plan_catalog import PlanCatalog

# The entitlement keys Wasla actually enforces, and how (spec: feature catalog).
# Written out rather than derived: "how is this enforced" is a statement about
# code paths, and the catalogue must say what is true of them.
FEATURES: Final[tuple[FeatureRead, ...]] = (
    FeatureRead(
        key=LimitKey.AGENTS.value,
        description="AI agents a workspace may configure.",
        unit="count",
        kind="hard_limit",
        enforcement="Refused on create (402), under a per-workspace advisory lock.",
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.CHANNEL_CONNECTIONS.value,
        description=(
            "Active channel connections on every channel - WhatsApp numbers, Instagram "
            "accounts, Pages - each one slot; disabled and released ones free theirs. "
            "Plus general and channel-typed top-ups and grants (ADR-131)."
        ),
        unit="count",
        kind="hard_limit",
        enforcement=(
            "Refused on connect, enable and reconnect with 409 channel_capacity_exceeded, "
            "before any provider call and again under a per-workspace advisory lock. "
            "A capacity reduction is resolved by the owner's selection or, after the "
            "grace, by keeping the oldest; nothing is released or deleted."
        ),
        concurrency_safe=True,
    ),
    FeatureRead(
        key="allowed_channel_types",
        description=(
            "Which channel types a workspace may connect and automate - a set of labels, "
            "not a number, and never a top-up target (ADR-131)."
        ),
        unit="channel labels",
        kind="channel_policy",
        enforcement=(
            "Refused on connect, enable and reconnect with 409 channel_type_not_allowed; "
            "AI turns, campaigns and follow-ups on a connection of a type the plan in force "
            "does not allow are refused as channel_not_in_plan and never charged. Inbound "
            "and a person's own reply are never refused."
        ),
        concurrency_safe=True,
        unlimited="every label named",
    ),
    FeatureRead(
        key="whatsapp_numbers",
        description="Retired: WhatsApp numbers are channel connections (ADR-131).",
        unit="count",
        kind="retired",
        enforcement=(
            "Refused on every new version, custom plan and top-up product; read as "
            "channel_connections on versions published before ADR-131."
        ),
        concurrency_safe=True,
        replaced_by=LimitKey.CHANNEL_CONNECTIONS.value,
    ),
    FeatureRead(
        key=LimitKey.TEAM_MEMBERS.value,
        description="Active members plus open invitations, which reserve a seat.",
        unit="count",
        kind="hard_limit",
        enforcement="Refused on invite and reinstate (402), under a per-workspace advisory lock.",
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.KNOWLEDGE_DOCUMENTS.value,
        description="Knowledge-base documents, whatever their indexing state.",
        unit="count",
        kind="hard_limit",
        enforcement="Refused on submit (402), under a per-workspace advisory lock.",
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.STORAGE_BYTES.value,
        description="Bytes of media currently held in object storage.",
        unit="bytes",
        kind="hard_limit",
        enforcement="Reserved on upload under an advisory lock; inbound media over it is skipped.",
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.PERIOD_AI_TURNS.value,
        description=(
            "Customer turns an AI agent answered in the usage cycle - one allowance for the "
            "whole workspace, every channel drawing from it (ADR-131)."
        ),
        unit="turns per period",
        kind="hard_limit",
        enforcement=(
            "Held at engagement under a per-workspace advisory lock, counting turns still "
            "generating, and charged only for a usable outcome - a reply or an executed "
            "handoff; a failed or empty generation gives its hold back. With none left no "
            "provider is called and the conversation is handed to a person."
        ),
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.PERIOD_CAMPAIGN_MESSAGES.value,
        description="Campaign messages in the period, reserved when a campaign is scheduled.",
        unit="messages per period",
        kind="hard_limit",
        enforcement=(
            "Refused on schedule (402) for the whole audience, counting other live "
            "campaigns' unsent recipients, under an advisory lock."
        ),
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.PERIOD_MESSAGES.value,
        description="Messages sent and received on every channel in the usage cycle.",
        unit="messages per period",
        kind="meter_only",
        enforcement=(
            "Metered, never enforced: an inbound customer message is never refused for a "
            "business's billing (ADR-030). Not a hard quota."
        ),
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.OWNED_WORKSPACES.value,
        description="Live workspaces one account may own. No plan currently sets a value.",
        unit="workspaces per account",
        kind="account_limit",
        enforcement=(
            "Checked on workspace creation by WorkspaceEntitlementService; unset means only the "
            "absolute safety limit applies."
        ),
        concurrency_safe=False,
    ),
)

# Tables whose rows count against a resource limit, for the preview's "who is
# already above the proposed limit". Keyed by limit, the same predicates
# `EntitlementService._resource_count` uses.
_RESOURCE_COUNT_SQL: Final[dict[LimitKey, str]] = {
    LimitKey.AGENTS: "SELECT tenant_id, count(*) AS used FROM agents GROUP BY tenant_id",
    LimitKey.CHANNEL_CONNECTIONS: (
        "SELECT tenant_id, count(*) AS used FROM channel_connections "
        "WHERE status = 'active' AND released_at IS NULL GROUP BY tenant_id"
    ),
    LimitKey.TEAM_MEMBERS: (
        "SELECT tenant_id, count(*) AS used FROM memberships "
        "WHERE status = 'active' GROUP BY tenant_id"
    ),
    LimitKey.KNOWLEDGE_DOCUMENTS: (
        "SELECT tenant_id, count(*) AS used FROM documents GROUP BY tenant_id"
    ),
}


def _terms(
    version: PlanVersion | None, prices: list[PlanPrice] | None = None
) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "version": version.version,
        "name": version.name,
        "price": str(version.price),
        "currency": version.currency,
        "interval": version.interval.value,
        "prices": [_price(price) for price in prices or []],
        "limits": dict(version.limits or {}),
        "allowed_channel_types": version.allowed_channel_types,
        "effective_at": version.effective_at.isoformat(),
    }


def _price(price: PlanPrice) -> dict[str, Any]:
    """A price as the audit trail records it."""
    return {
        "id": str(price.id),
        "billing_interval": price.billing_interval.value,
        "interval_count": price.interval_count,
        "amount": str(price.amount),
        "currency": price.currency,
        "active": price.is_active,
    }


def _identity(plan: Plan) -> dict[str, Any]:
    return {
        "code": plan.code,
        "name": plan.name,
        "description": plan.description,
        "scope": plan.scope.value if plan.scope is not None else None,
        "tenant_id": str(plan.tenant_id) if plan.tenant_id else None,
        "is_public": plan.is_public,
        "is_active": plan.is_active,
        "sort_order": plan.sort_order,
        "revision": plan.revision,
    }


@dataclass(frozen=True, slots=True)
class PlanPage:
    items: list[PlatformPlanRead]
    total: int


class PlanCatalogAdmin:
    """The platform operator's view of, and control over, the plan catalogue."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._versions = PlanVersionRepository(session)
        self._prices = PlanPriceRepository(session)
        self._migrations = PlanVersionMigrationRepository(session)
        self._catalog = PlanCatalog(session)

    # ----------------------------------------------------------------- reads

    @staticmethod
    def features() -> list[FeatureRead]:
        eligible = {member.value for member in SELLABLE_TOPUP_ENTITLEMENTS}
        return [
            feature.model_copy(update={"topup_eligible": feature.key in eligible})
            for feature in FEATURES
        ]

    async def list_plans(
        self,
        *,
        active: bool | None = None,
        public: bool | None = None,
        code: str | None = None,
        currency: str | None = None,
        scope: PlanScope | None = None,
        tenant_id: uuid.UUID | None = None,
        limit: int,
        offset: int,
    ) -> PlanPage:
        statement = select(Plan)
        if active is not None:
            statement = statement.where(Plan.is_active.is_(active))
        if public is not None:
            statement = statement.where(Plan.is_public.is_(public))
        if scope is not None:
            statement = statement.where(Plan.scope == scope)
        if tenant_id is not None:
            statement = statement.where(Plan.tenant_id == tenant_id)
        if code:
            statement = statement.where(Plan.code == code.strip().lower())
        if currency:
            statement = statement.where(Plan.currency == currency.strip().upper())
        total = int(
            await self._session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        )
        plans = (
            await self._session.scalars(
                statement.order_by(Plan.sort_order, Plan.code).limit(limit).offset(offset)
            )
        ).all()
        counts = await self._versions.count_subscribers()
        items = [await self._read(plan, counts=counts) for plan in plans]
        return PlanPage(items=items, total=total)

    async def get(self, plan_id: uuid.UUID) -> PlatformPlanRead:
        plan = await self._require(plan_id)
        return await self._read(plan, counts=await self._versions.count_subscribers())

    async def versions(self, plan_id: uuid.UUID) -> list[PlanVersionRead]:
        plan = await self._require(plan_id)
        await self._catalog.current_version(plan)  # materialise a legacy plan's first version
        counts = await self._versions.count_subscribers()
        versions = await self._versions.list_for_plan(plan.id)
        prices = await self._prices_by_version(versions)
        return [
            PlanVersionRead.from_model(
                version,
                subscribers=counts.get(version.id, 0),
                prices=prices.get(version.id, []),
            )
            for version in versions
        ]

    async def _read(self, plan: Plan, *, counts: dict[uuid.UUID, int]) -> PlatformPlanRead:
        current = await self._catalog.current_version(plan)
        latest = await self._versions.latest(plan.id)
        versions = await self._versions.list_for_plan(plan.id)
        plan_counts = {version.id: counts.get(version.id, 0) for version in versions}
        return PlatformPlanRead.build(
            plan,
            current=current,
            latest=latest,
            counts=plan_counts,
            prices=await self._prices_by_version(versions),
        )

    async def _prices_by_version(
        self, versions: list[PlanVersion]
    ) -> dict[uuid.UUID, list[PlanPrice]]:
        """Every price of these versions, retired ones included, by version."""
        await self._session.flush()
        grouped: dict[uuid.UUID, list[PlanPrice]] = {}
        for price in await self._prices.for_versions([version.id for version in versions]):
            grouped.setdefault(price.plan_version_id, []).append(price)
        return grouped

    # ------------------------------------------------------------- mutations

    async def create(
        self, payload: PlanCreate, *, actor: User, now: datetime | None = None
    ) -> PlatformPlanRead:
        """A new plan and its version 1. `code` is permanent.

        A `tenant` plan is a custom plan (ADR-113): it names one existing
        workspace, is never public, and can never be held by another.
        """
        moment = now if now is not None else datetime.now(UTC)
        code = payload.code.strip().lower()
        if await self._session.scalar(select(Plan.id).where(Plan.code == code)) is not None:
            raise ConflictError("A plan with that code already exists.")
        scope = payload.resolved_scope
        if (
            payload.tenant_id is not None
            and await self._session.get(Tenant, payload.tenant_id) is None
        ):
            raise NotFoundError("No such workspace.")
        effective = self._effective(payload.effective_at, moment)
        headline = payload.headline
        plan = Plan(
            code=code,
            name=payload.name,
            description=payload.description,
            price=headline.amount if headline is not None else Decimal("0.00"),
            currency=payload.currency,
            interval=_published_interval(payload),
            trial_days=payload.trial_days,
            limits=dict(payload.limits),
            allowed_channel_types=[channel.value for channel in payload.allowed_channel_types],
            scope=scope,
            tenant_id=payload.tenant_id,
            is_public=scope is PlanScope.PUBLIC,
            is_active=True,
            sort_order=payload.sort_order,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(plan)
                await self._session.flush()
        except IntegrityError:
            raise ConflictError("A plan with that code already exists.") from None
        version, prices = await self._publish(
            plan,
            number=1,
            name=payload.name,
            terms=payload,
            effective_at=effective,
            now=moment,
            actor=actor,
            reason=payload.reason,
        )
        record_platform_billing(
            self._session,
            (
                AuditAction.BILLING_CUSTOM_PLAN_CREATED
                if plan.is_custom
                else AuditAction.BILLING_PLAN_CREATED
            ),
            actor=actor,
            reason=payload.reason,
            target_type="plan",
            target_id=plan.id,
            tenant_id=plan.tenant_id,
            target_label=plan.code,
            after={**_identity(plan), "terms": _terms(version, prices)},
        )
        return await self.get(plan.id)

    async def update(
        self, plan_id: uuid.UUID, payload: PlanUpdate, *, actor: User
    ) -> PlatformPlanRead:
        """Presentation only: name, description, visibility, order."""
        plan = await self._lock(plan_id, expected_revision=payload.expected_revision)
        if plan.is_custom and payload.is_public:
            # A custom plan is one workspace's; publishing it would offer one
            # company's negotiated terms to every other (ADR-113).
            raise ValidationError("A custom plan is never public.")
        before = _identity(plan)
        if payload.name is not None:
            plan.name = payload.name
        if payload.description is not None:
            plan.description = payload.description
        if payload.is_public is not None:
            plan.is_public = payload.is_public
        if payload.sort_order is not None:
            plan.sort_order = payload.sort_order
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.BILLING_PLAN_UPDATED,
            actor=actor,
            reason=payload.reason,
            target_type="plan",
            target_id=plan.id,
            target_label=plan.code,
            before=before,
            after=_identity(plan),
        )
        return await self.get(plan.id)

    async def set_active(
        self,
        plan_id: uuid.UUID,
        *,
        active: bool,
        expected_revision: int,
        reason: str,
        actor: User,
    ) -> PlatformPlanRead:
        """Offer a plan to new customers, or stop offering it.

        Deactivation changes nobody who already holds the plan: they keep their
        version and keep renewing on it until they leave or are migrated.
        """
        plan = await self._lock(plan_id, expected_revision=expected_revision)
        if plan.is_active is active:
            raise ConflictError(f"The plan is already {'active' if active else 'inactive'}.")
        before = _identity(plan)
        plan.is_active = active
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.BILLING_PLAN_ACTIVATED if active else AuditAction.BILLING_PLAN_DEACTIVATED,
            actor=actor,
            reason=reason,
            target_type="plan",
            target_id=plan.id,
            target_label=plan.code,
            before=before,
            after=_identity(plan),
        )
        return await self.get(plan.id)

    async def delete(self, plan_id: uuid.UUID, *, reason: str, actor: User) -> None:
        """Hard-delete a plan nothing has ever referenced. Platform owner only.

        Refused (409) if any subscription, invoice, scheduled change or cohort
        migration names the plan or one of its versions: those are financial
        history, and a referenced plan is retired, never deleted.
        """
        plan = await self._lock(plan_id)
        references = (
            await self._session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.plan_id == plan.id)
            )
            or 0
        )
        references += (
            await self._session.scalar(
                select(func.count()).select_from(Invoice).where(Invoice.plan_code == plan.code)
            )
            or 0
        )
        references += (
            await self._session.scalar(
                select(func.count())
                .select_from(PlanVersionMigration)
                .where(PlanVersionMigration.plan_id == plan.id)
            )
            or 0
        )
        if references:
            raise ConflictError(
                "This plan is referenced by subscriptions, invoices or migrations. "
                "Deactivate it instead."
            )
        snapshot = _identity(plan)
        await self._session.delete(plan)
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.BILLING_PLAN_DELETED,
            actor=actor,
            reason=reason,
            target_type="plan",
            target_id=plan_id,
            target_label=snapshot["code"],
            before=snapshot,
        )

    async def create_version(
        self,
        plan_id: uuid.UUID,
        payload: PlanVersionCreate,
        *,
        actor: User,
        now: datetime | None = None,
    ) -> PlanVersionRead:
        """Publish new commercial terms for *new* customers.

        `expected_version` must be the latest version number: an operator who
        edits from a stale view gets 409 rather than silently overwriting a
        price somebody else just published. Existing subscribers are not
        touched - see `schedule_migration`.
        """
        moment = now if now is not None else datetime.now(UTC)
        plan = await self._lock(plan_id)
        await self._catalog.current_version(plan)
        latest = await self._versions.latest(plan.id)
        current_number = latest.version if latest is not None else 0
        if payload.expected_version != current_number:
            raise ConflictError(
                f"The plan is at version {current_number}, not {payload.expected_version}. "
                "Reload it and publish again."
            )
        before = _terms(latest, await self._catalog.prices(latest) if latest else None)
        version, prices = await self._publish(
            plan,
            number=current_number + 1,
            name=payload.name or plan.name,
            terms=payload,
            effective_at=self._effective(payload.effective_at, moment),
            now=moment,
            actor=actor,
            reason=payload.reason,
        )
        # The plan row mirrors the most recently published terms for anybody
        # reading the table directly. Nothing that charges or enforces reads
        # them (see `Plan`).
        plan.price = version.price
        plan.currency = version.currency
        plan.interval = version.interval
        plan.trial_days = version.trial_days
        plan.limits = dict(version.limits)
        plan.allowed_channel_types = list(version.allowed_channel_types or [])
        await self._session.flush()
        record_platform_billing(
            self._session,
            (
                AuditAction.BILLING_CUSTOM_PLAN_VERSION_CREATED
                if plan.is_custom
                else AuditAction.BILLING_PLAN_VERSION_CREATED
            ),
            actor=actor,
            reason=payload.reason,
            target_type="plan",
            target_id=plan.id,
            tenant_id=plan.tenant_id,
            target_label=plan.code,
            before=before,
            after=_terms(version, prices),
        )
        return PlanVersionRead.from_model(version, prices=prices)

    async def _publish(
        self,
        plan: Plan,
        *,
        number: int,
        name: str,
        terms: _Terms,
        effective_at: datetime,
        now: datetime,
        actor: User,
        reason: str,
    ) -> tuple[PlanVersion, list[PlanPrice]]:
        """Write one version and every price it is sold at (ADR-116).

        The version row carries the headline price - monthly where there is
        one - and the database publishes it as the version's first price in
        the same statement. Every other price is inserted beside it. One
        version, one set of limits, however many terms it is sold on.
        """
        headline = terms.headline
        version = PlanVersion(
            plan_id=plan.id,
            version=number,
            name=name,
            price=headline.amount if headline is not None else Decimal("0.00"),
            currency=terms.currency,
            interval=_published_interval(terms),
            trial_days=terms.trial_days,
            limits=dict(terms.limits),
            # Required on every version published since ADR-131 (a trigger
            # refuses NULL): the plan says which channel types it sells.
            allowed_channel_types=[channel.value for channel in terms.allowed_channel_types],
            effective_at=effective_at,
            created_at=now,
            created_by=actor.id,
            reason=reason,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(version)
                await self._session.flush()
        except IntegrityError:
            raise ConflictError("Another version was published at the same moment.") from None
        for spec in terms.resolved_prices:
            if headline is not None and spec.slot == headline.slot:
                continue
            self._session.add(
                PlanPrice(
                    plan_version_id=version.id,
                    billing_interval=spec.billing_interval,
                    interval_count=spec.interval_count,
                    amount=spec.amount,
                    currency=spec.currency,
                    created_at=now,
                    created_by=actor.id,
                    reason=reason,
                )
            )
        await self._session.flush()
        return version, await self._catalog.prices(version, active_only=False)

    # ------------------------------------------------------------------ prices

    async def version(self, version_id: uuid.UUID) -> PlanVersionRead:
        """One version with every price it has had, retired ones marked."""
        version = await self._require_version(version_id)
        counts = await self._versions.count_subscribers()
        return PlanVersionRead.from_model(
            version,
            subscribers=counts.get(version.id, 0),
            prices=await self._catalog.prices(version, active_only=False),
        )

    async def prices(
        self, version_id: uuid.UUID, *, active: bool | None = None
    ) -> list[PlanPriceRead]:
        """A version's price history: active and retired, oldest term first."""
        version = await self._require_version(version_id)
        rows = await self._catalog.prices(version, active_only=active is True)
        if active is False:
            rows = [row for row in rows if not row.is_active]
        return [
            PlanPriceRead.from_model(row, references=await self._prices.references(row.id))
            for row in rows
        ]

    async def price(self, price_id: uuid.UUID) -> PlanPriceRead:
        row = await self._prices.get_by_id(price_id)
        if row is None:
            raise NotFoundError("No such price.")
        return PlanPriceRead.from_model(row, references=await self._prices.references(row.id))

    async def create_price(
        self,
        version_id: uuid.UUID,
        payload: PlanPriceCreate,
        *,
        actor: User,
        now: datetime | None = None,
    ) -> PlanPriceRead:
        """Publish a new price for a version - monthly or yearly, one code path.

        The way to add a yearly price to Business v4 without copying v4, and
        the second half of changing a price: retire the old one, create this.
        Refused (422) for a free version, a retired plan, a version a newer
        one has already replaced, and a currency other than the version's;
        refused (409) when the slot - version, term, currency - already has
        an active price. Nobody already subscribed is moved onto it.
        """
        moment = now if now is not None else datetime.now(UTC)
        version = await self._require_version(version_id)
        plan = await self._lock(version.plan_id)
        await self._require_sellable(plan, version, now=moment)
        if payload.currency != version.currency:
            raise ValidationError(f"This version is priced in {version.currency}.")
        existing = await self._prices.active_for_slot(
            version.id,
            interval=payload.billing_interval,
            interval_count=payload.interval_count,
            currency=payload.currency,
        )
        if existing is not None:
            raise ConflictError(
                "This version already has an active price for that term. Retire it first; "
                "a price is never edited.",
                details={"price_id": str(existing.id)},
            )
        created = PlanPrice(
            plan_version_id=version.id,
            billing_interval=payload.billing_interval,
            interval_count=payload.interval_count,
            amount=payload.amount,
            currency=payload.currency,
            created_at=moment,
            created_by=actor.id,
            reason=payload.reason,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(created)
                await self._session.flush()
        except IntegrityError:
            raise ConflictError(
                "Another price for that term was published at the same moment."
            ) from None
        self._audit_price(
            AuditAction.BILLING_PLAN_PRICE_CREATED,
            plan=plan,
            version=version,
            price=created,
            actor=actor,
            reason=payload.reason,
            after=_price(created),
        )
        return PlanPriceRead.from_model(
            created, references=await self._prices.references(created.id)
        )

    async def retire_price(
        self,
        price_id: uuid.UUID,
        payload: PlanPriceRetire,
        *,
        actor: User,
        now: datetime | None = None,
    ) -> PlanPriceRead:
        """Stop selling a price to new customers. Never deletes it (ADR-116).

        Every subscription, scheduled change, invoice and offer that names it
        keeps it: a subscriber on 2,990 a year still renews at 2,990 after
        3,290 is published, until an operator migrates them deliberately.
        """
        moment = now if now is not None else datetime.now(UTC)
        row = await self._prices.lock(price_id)
        if row is None:
            raise NotFoundError("No such price.")
        if not row.is_active:
            raise ConflictError("This price is already retired.")
        version = await self._require_version(row.plan_version_id)
        plan = await self._lock(version.plan_id)
        before = _price(row)
        row.retired_at = moment
        row.retired_by = actor.id
        row.retirement_reason = payload.reason
        await self._session.flush()
        references = await self._prices.references(row.id)
        self._audit_price(
            AuditAction.BILLING_PLAN_PRICE_RETIRED,
            plan=plan,
            version=version,
            price=row,
            actor=actor,
            reason=payload.reason,
            before=before,
            after=_price(row),
            extra={"still_referenced": references},
        )
        return PlanPriceRead.from_model(row, references=references)

    @staticmethod
    def refuse_price_change() -> NoReturn:
        """A published price is never edited: retire it and publish another."""
        raise ConflictError(
            "A price is immutable. Retire it with POST /platform/billing/prices/{id}/retire "
            "and create the new one with POST /platform/billing/plan-versions/{id}/prices; "
            "existing subscribers keep the price they hold."
        )

    async def _require_sellable(self, plan: Plan, version: PlanVersion, *, now: datetime) -> None:
        """Whether a new price may be published on `version` at all."""
        if version.is_free:
            raise ValidationError(
                "A free plan version is not sold and has no prices. Publish a priced version."
            )
        if not plan.is_active:
            raise ValidationError("A retired plan cannot be given a new price.")
        current = await self._catalog.current_version(plan, at=now)
        superseded = version.effective_at <= now and (current is None or current.id != version.id)
        if superseded:
            raise ValidationError(
                "A newer version of this plan has replaced this one; price the current version."
            )

    def _audit_price(
        self,
        action: AuditAction,
        *,
        plan: Plan,
        version: PlanVersion,
        price: PlanPrice,
        actor: User,
        reason: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        record_platform_billing(
            self._session,
            action,
            actor=actor,
            reason=reason,
            target_type="plan_price",
            target_id=price.id,
            tenant_id=plan.tenant_id,
            target_label=plan.code,
            before=before,
            after=after,
            extra={
                "plan_id": str(plan.id),
                "plan_code": plan.code,
                "custom_plan": plan.is_custom,
                "plan_version_id": str(version.id),
                "version": version.version,
                "billing_interval": price.billing_interval.value,
                "interval_count": price.interval_count,
                "amount": str(price.amount),
                "currency": price.currency,
                **(extra or {}),
            },
        )

    async def preview(
        self,
        plan_id: uuid.UUID,
        payload: PlanVersionPreviewRequest,
    ) -> PlanVersionPreview:
        """What publishing these terms would mean. Writes nothing."""
        plan = await self._require(plan_id)
        current = await self._catalog.current_version(plan)
        serving = [status.value for status in SERVING_STATUSES]
        active = int(
            await self._session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.plan_id == plan.id)
                .where(Subscription.status.in_(serving))
            )
            or 0
        )
        on_current = 0
        if current is not None:
            on_current = int(
                await self._session.scalar(
                    select(func.count())
                    .select_from(Subscription)
                    .where(Subscription.plan_version_id == current.id)
                    .where(Subscription.status.in_(serving))
                )
                or 0
            )
        scheduled = await self._scheduled_for_migration(plan)
        changes: list[LimitChange] = []
        for key in LimitKey:
            # As the current version is enforced - a legacy one's number limit
            # is its channel capacity (ENT-05).
            old = term_limit(current, key) if current is not None else None
            raw = payload.limits.get(key.value)
            new = raw if isinstance(raw, int) else None
            if old == new:
                continue
            above = None
            if key in RESOURCE_LIMITS and new is not None and key in _RESOURCE_COUNT_SQL:
                above = await self._above(plan, key, new)
            changes.append(LimitChange(key=key, old=old, new=new, workspaces_above_new_limit=above))
        headline = payload.headline
        return PlanVersionPreview(
            plan_id=plan.id,
            current_version=current.version if current is not None else None,
            proposed_price=f"{headline.amount if headline is not None else 0:.2f}",
            current_price=f"{current.price:.2f}" if current is not None else None,
            currency=payload.currency,
            interval=_published_interval(payload),
            active_subscriptions=active,
            subscriptions_on_current_version=on_current,
            subscriptions_staying_on_old_versions=active,
            subscriptions_scheduled_for_migration=scheduled,
            limits=changes,
            channel_types=await self._channel_types_change(
                plan, current, payload.allowed_channel_types
            ),
            note=(
                "Publishing changes no existing subscriber. They stay on their current "
                "version until a migration is scheduled, which applies at each one's next "
                "renewal and only once that renewal is paid."
            ),
        )

    async def schedule_migration(
        self,
        plan_id: uuid.UUID,
        payload: PlanMigrationCreate,
        *,
        actor: User,
    ) -> MigrationRead:
        """Move a cohort from one version to another at each next renewal.

        With `confirm` false, answers how many subscriptions would move and
        writes nothing. With `confirm` true, records one migration row; the
        billing sweep applies it to each subscriber at their own boundary, so
        no request ever mutates thousands of rows.
        """
        plan = await self._lock(plan_id)
        versions = {v.version: v for v in await self._versions.list_for_plan(plan.id)}
        source = versions.get(payload.from_version)
        target = versions.get(payload.to_version)
        if source is None or target is None:
            raise NotFoundError("No such version of this plan.")
        if source.id == target.id:
            raise ValidationError("A migration must move to a different version.")
        await self._refuse_unsold_terms(source, target)
        affected = int(
            await self._session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.plan_version_id == source.id)
                .where(Subscription.status.in_([status.value for status in SERVING_STATUSES]))
            )
            or 0
        )
        if not payload.confirm:
            return MigrationRead.build(
                None,
                plan_id=plan.id,
                from_version=source.version,
                to_version=target.version,
                reason=payload.reason,
                affected=affected,
            )
        if await self._migrations.live_from(source.id) is not None:
            raise ConflictError("A migration out of that version is already scheduled.")
        migration = PlanVersionMigration(
            plan_id=plan.id,
            from_version_id=source.id,
            to_version_id=target.id,
            reason=payload.reason,
            created_by=actor.id,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(migration)
                await self._session.flush()
        except IntegrityError:
            raise ConflictError("A migration out of that version is already scheduled.") from None
        record_platform_billing(
            self._session,
            AuditAction.BILLING_PLAN_MIGRATION_SCHEDULED,
            actor=actor,
            reason=payload.reason,
            target_type="plan",
            target_id=plan.id,
            target_label=plan.code,
            before={"version": source.version, "price": str(source.price)},
            after={"version": target.version, "price": str(target.price)},
            extra={"affected_subscriptions": affected, "mode": payload.mode.value},
        )
        return MigrationRead.build(
            migration,
            plan_id=plan.id,
            from_version=source.version,
            to_version=target.version,
            reason=payload.reason,
            affected=affected,
        )

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _effective(requested: datetime | None, now: datetime) -> datetime:
        if requested is None:
            return now
        moment = requested if requested.tzinfo else requested.replace(tzinfo=UTC)
        if moment < now:
            raise ValidationError("effective_at cannot be in the past.")
        return moment

    async def _refuse_unsold_terms(self, source: PlanVersion, target: PlanVersion) -> None:
        """A migration keeps each subscriber on their billing term (ADR-116).

        A monthly subscriber migrates to the target's monthly price and a
        yearly one to its yearly price. A target that does not sell a term the
        source's subscribers hold is refused, rather than silently moving
        somebody to a term they never chose.
        """
        if target.is_free:
            return
        held = await self._session.execute(
            select(PlanPrice.billing_interval, PlanPrice.interval_count)
            .join(Subscription, Subscription.plan_price_id == PlanPrice.id)
            .where(Subscription.plan_version_id == source.id)
            .where(Subscription.status.in_([status.value for status in SERVING_STATUSES]))
            .distinct()
        )
        missing = []
        for interval, count in held.all():
            if (
                await self._catalog.price_for_term(target, interval=interval, interval_count=count)
                is None
            ):
                missing.append(f"{count} {interval.value}")
        if missing:
            raise ValidationError(
                "The target version is not sold on every billing term its subscribers hold: "
                + ", ".join(sorted(missing))
                + ". Publish those prices on it first."
            )

    async def _require_version(self, version_id: uuid.UUID) -> PlanVersion:
        version = await self._versions.get_by_id(version_id)
        if version is None:
            raise NotFoundError("No such plan version.")
        return version

    async def _require(self, plan_id: uuid.UUID) -> Plan:
        plan = await self._session.get(Plan, plan_id)
        if plan is None:
            raise NotFoundError("No such plan.")
        return plan

    async def _lock(self, plan_id: uuid.UUID, *, expected_revision: int | None = None) -> Plan:
        """The plan row, locked for this transaction, at the revision expected."""
        plan = await self._session.scalar(select(Plan).where(Plan.id == plan_id).with_for_update())
        if plan is None:
            raise NotFoundError("No such plan.")
        await self._session.refresh(plan)
        if expected_revision is not None and plan.revision != expected_revision:
            raise ConflictError(
                f"The plan has changed (revision {plan.revision}); reload it and retry."
            )
        return plan

    async def _scheduled_for_migration(self, plan: Plan) -> int:
        version_ids = [v.id for v in await self._versions.list_for_plan(plan.id)]
        if not version_ids:
            return 0
        scheduled = int(
            await self._session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.scheduled_plan_version_id.in_(version_ids))
            )
            or 0
        )
        live = await self._session.scalars(
            select(PlanVersionMigration.from_version_id)
            .where(PlanVersionMigration.plan_id == plan.id)
            .where(PlanVersionMigration.cancelled_at.is_(None))
        )
        sources = list(live)
        if sources:
            scheduled += int(
                await self._session.scalar(
                    select(func.count())
                    .select_from(Subscription)
                    .where(Subscription.plan_version_id.in_(sources))
                    .where(Subscription.scheduled_plan_version_id.is_(None))
                )
                or 0
            )
        return scheduled

    async def _channel_types_change(
        self, plan: Plan, current: PlanVersion | None, proposed: list[Channel]
    ) -> ChannelTypesChange:
        """What the proposed channel types remove, and who holds a removed one."""
        old = term_channel_types(current) if current is not None else None
        new = set(proposed)
        removed = [channel for channel in ordered(old or ()) if channel not in new]
        added = [channel for channel in ordered(new) if old is None or channel not in old]
        holders = 0
        if removed:
            holders = int(
                await self._session.scalar(
                    text(
                        "SELECT count(DISTINCT c.tenant_id) FROM channel_connections c "
                        "JOIN subscriptions s ON s.tenant_id = c.tenant_id "
                        "WHERE s.plan_id = :plan "
                        "AND s.status IN ('trialing', 'active', 'past_due') "
                        "AND c.status = 'active' AND c.released_at IS NULL "
                        "AND c.channel::text = ANY(:removed)"
                    ),
                    {"plan": plan.id, "removed": [channel.value for channel in removed]},
                )
                or 0
            )
        return ChannelTypesChange(
            old=ordered(old) if old is not None else None,
            new=ordered(new),
            removed=removed,
            added=added,
            workspaces_holding_a_removed_type=holders,
        )

    async def _above(self, plan: Plan, key: LimitKey, limit: int) -> int:
        """Serving subscribers of this plan already above `limit` for `key`."""
        statement = text(
            f"SELECT count(*) FROM ({_RESOURCE_COUNT_SQL[key]}) usage "  # noqa: S608 - constant SQL
            "JOIN subscriptions s ON s.tenant_id = usage.tenant_id "
            "WHERE s.plan_id = :plan AND s.status IN ('trialing', 'active', 'past_due') "
            "AND usage.used > :limit"
        )
        return int(await self._session.scalar(statement, {"plan": plan.id, "limit": limit}) or 0)


def _published_interval(terms: _Terms) -> BillingInterval:
    """The interval a version row records: its headline price's, else monthly."""
    headline = terms.headline
    if headline is not None:
        return headline.billing_interval
    return terms.interval if terms.interval is not None else BillingInterval.MONTHLY


__all__ = ["FEATURES", "PlanCatalogAdmin", "PlanPage"]

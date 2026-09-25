"""Running the plan catalogue without SQL (BILL-12).

Before this there was no way to create, reprice, retire or inspect a plan except
by editing `plans` with SQL or shipping a migration - no validation, no audit,
no optimistic concurrency, and a price edit silently re-priced every existing
subscriber's next renewal.

The contract this service enforces:

* **A plan's identity is stable.** Its `code` is written once and never
  renamed; name, description, visibility and order are presentation and may
  change (`update`).
* **Commercial terms change only by publishing a version** (`create_version`).
  Versions are immutable. A new version applies to *new* checkouts from its
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
from typing import Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models.audit import AuditAction
from app.db.models.billing import (
    RESOURCE_LIMITS,
    SERVING_STATUSES,
    LimitKey,
    Plan,
    PlanVersion,
    PlanVersionMigration,
    Subscription,
)
from app.db.models.invoice import Invoice
from app.db.models.user import User
from app.platform.billing_audit import record_platform_billing
from app.repositories.billing_repository import (
    PlanVersionMigrationRepository,
    PlanVersionRepository,
)
from app.schemas.platform_billing import (
    FeatureRead,
    LimitChange,
    MigrationRead,
    PlanCreate,
    PlanMigrationCreate,
    PlanUpdate,
    PlanVersionCreate,
    PlanVersionPreview,
    PlanVersionPreviewRequest,
    PlanVersionRead,
    PlatformPlanRead,
)
from app.services.plan_catalog import PlanCatalog

# The entitlement keys Wasla actually enforces, and how (spec: feature catalog).
# Written out rather than derived: "how is this enforced" is a statement about
# code paths, and the catalogue must say what is true of them.
FEATURES: Final[tuple[FeatureRead, ...]] = (
    FeatureRead(
        key=LimitKey.AGENTS,
        description="AI agents a workspace may configure.",
        unit="count",
        kind="hard_limit",
        enforcement="Refused on create (402), under a per-workspace advisory lock.",
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.WHATSAPP_NUMBERS,
        description="Connected WhatsApp numbers (disabled and released numbers do not count).",
        unit="count",
        kind="hard_limit",
        enforcement="Refused on connect (402), under a per-workspace advisory lock.",
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.TEAM_MEMBERS,
        description="Active members plus open invitations, which reserve a seat.",
        unit="count",
        kind="hard_limit",
        enforcement="Refused on invite and reinstate (402), under a per-workspace advisory lock.",
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.KNOWLEDGE_DOCUMENTS,
        description="Knowledge-base documents, whatever their indexing state.",
        unit="count",
        kind="hard_limit",
        enforcement="Refused on submit (402), under a per-workspace advisory lock.",
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.STORAGE_BYTES,
        description="Bytes of media currently held in object storage.",
        unit="bytes",
        kind="hard_limit",
        enforcement="Reserved on upload under an advisory lock; inbound media over it is skipped.",
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.PERIOD_AI_TURNS,
        description="Customer turns an AI agent answered in the billing period.",
        unit="turns per period",
        kind="hard_limit",
        enforcement="Consumed under an advisory lock; exhausted hands the conversation to a human.",
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.PERIOD_CAMPAIGN_MESSAGES,
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
        key=LimitKey.PERIOD_MESSAGES,
        description="WhatsApp messages sent and received in the billing period.",
        unit="messages per period",
        kind="meter_only",
        enforcement=(
            "Metered, never enforced: an inbound customer message is never refused for a "
            "business's billing (ADR-030). Not a hard quota."
        ),
        concurrency_safe=True,
    ),
    FeatureRead(
        key=LimitKey.OWNED_WORKSPACES,
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
    LimitKey.WHATSAPP_NUMBERS: (
        "SELECT tenant_id, count(*) AS used FROM whatsapp_accounts "
        "WHERE status <> 'disabled' AND released_at IS NULL GROUP BY tenant_id"
    ),
    LimitKey.TEAM_MEMBERS: (
        "SELECT tenant_id, count(*) AS used FROM memberships "
        "WHERE status = 'active' GROUP BY tenant_id"
    ),
    LimitKey.KNOWLEDGE_DOCUMENTS: (
        "SELECT tenant_id, count(*) AS used FROM documents GROUP BY tenant_id"
    ),
}


def _terms(version: PlanVersion | None) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "version": version.version,
        "name": version.name,
        "price": str(version.price),
        "currency": version.currency,
        "interval": version.interval.value,
        "limits": dict(version.limits or {}),
        "effective_at": version.effective_at.isoformat(),
    }


def _identity(plan: Plan) -> dict[str, Any]:
    return {
        "code": plan.code,
        "name": plan.name,
        "description": plan.description,
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
        self._migrations = PlanVersionMigrationRepository(session)
        self._catalog = PlanCatalog(session)

    # ----------------------------------------------------------------- reads

    @staticmethod
    def features() -> list[FeatureRead]:
        return list(FEATURES)

    async def list_plans(
        self,
        *,
        active: bool | None = None,
        public: bool | None = None,
        code: str | None = None,
        currency: str | None = None,
        limit: int,
        offset: int,
    ) -> PlanPage:
        statement = select(Plan)
        if active is not None:
            statement = statement.where(Plan.is_active.is_(active))
        if public is not None:
            statement = statement.where(Plan.is_public.is_(public))
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
        return [
            PlanVersionRead.from_model(version, subscribers=counts.get(version.id, 0))
            for version in await self._versions.list_for_plan(plan.id)
        ]

    async def _read(self, plan: Plan, *, counts: dict[uuid.UUID, int]) -> PlatformPlanRead:
        current = await self._catalog.current_version(plan)
        latest = await self._versions.latest(plan.id)
        plan_counts = {
            version.id: counts.get(version.id, 0)
            for version in await self._versions.list_for_plan(plan.id)
        }
        return PlatformPlanRead.build(plan, current=current, latest=latest, counts=plan_counts)

    # ------------------------------------------------------------- mutations

    async def create(
        self, payload: PlanCreate, *, actor: User, now: datetime | None = None
    ) -> PlatformPlanRead:
        """A new plan and its version 1. `code` is permanent."""
        moment = now if now is not None else datetime.now(UTC)
        code = payload.code.strip().lower()
        if await self._session.scalar(select(Plan.id).where(Plan.code == code)) is not None:
            raise ConflictError("A plan with that code already exists.")
        effective = self._effective(payload.effective_at, moment)
        plan = Plan(
            code=code,
            name=payload.name,
            description=payload.description,
            price=payload.price,
            currency=payload.currency,
            interval=payload.interval,
            trial_days=payload.trial_days,
            limits=dict(payload.limits),
            is_public=payload.is_public,
            is_active=True,
            sort_order=payload.sort_order,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(plan)
                await self._session.flush()
        except IntegrityError:
            raise ConflictError("A plan with that code already exists.") from None
        version = PlanVersion(
            plan_id=plan.id,
            version=1,
            name=payload.name,
            price=payload.price,
            currency=payload.currency,
            interval=payload.interval,
            trial_days=payload.trial_days,
            limits=dict(payload.limits),
            effective_at=effective,
            created_at=moment,
            created_by=actor.id,
            reason=payload.reason,
        )
        self._session.add(version)
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.BILLING_PLAN_CREATED,
            actor=actor,
            reason=payload.reason,
            target_type="plan",
            target_id=plan.id,
            target_label=plan.code,
            after={**_identity(plan), "terms": _terms(version)},
        )
        return await self.get(plan.id)

    async def update(
        self, plan_id: uuid.UUID, payload: PlanUpdate, *, actor: User
    ) -> PlatformPlanRead:
        """Presentation only: name, description, visibility, order."""
        plan = await self._lock(plan_id, expected_revision=payload.expected_revision)
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
        before = _terms(latest)
        version = PlanVersion(
            plan_id=plan.id,
            version=current_number + 1,
            name=payload.name or plan.name,
            price=payload.price,
            currency=payload.currency,
            interval=payload.interval,
            trial_days=payload.trial_days,
            limits=dict(payload.limits),
            effective_at=self._effective(payload.effective_at, moment),
            created_at=moment,
            created_by=actor.id,
            reason=payload.reason,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(version)
                await self._session.flush()
        except IntegrityError:
            raise ConflictError("Another version was published at the same moment.") from None
        # The plan row mirrors the most recently published terms for anybody
        # reading the table directly. Nothing that charges or enforces reads
        # them (see `Plan`).
        plan.price = version.price
        plan.currency = version.currency
        plan.interval = version.interval
        plan.trial_days = version.trial_days
        plan.limits = dict(version.limits)
        await self._session.flush()
        record_platform_billing(
            self._session,
            AuditAction.BILLING_PLAN_VERSION_CREATED,
            actor=actor,
            reason=payload.reason,
            target_type="plan",
            target_id=plan.id,
            target_label=plan.code,
            before=before,
            after=_terms(version),
        )
        return PlanVersionRead.from_model(version)

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
            old = current.limit_for(key) if current is not None else None
            raw = payload.limits.get(key.value)
            new = raw if isinstance(raw, int) else None
            if old == new:
                continue
            above = None
            if key in RESOURCE_LIMITS and new is not None and key in _RESOURCE_COUNT_SQL:
                above = await self._above(plan, key, new)
            changes.append(LimitChange(key=key, old=old, new=new, workspaces_above_new_limit=above))
        return PlanVersionPreview(
            plan_id=plan.id,
            current_version=current.version if current is not None else None,
            proposed_price=f"{payload.price:.2f}",
            current_price=f"{current.price:.2f}" if current is not None else None,
            currency=payload.currency,
            interval=payload.interval,
            active_subscriptions=active,
            subscriptions_on_current_version=on_current,
            subscriptions_staying_on_old_versions=active,
            subscriptions_scheduled_for_migration=scheduled,
            limits=changes,
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

    async def _above(self, plan: Plan, key: LimitKey, limit: int) -> int:
        """Serving subscribers of this plan already above `limit` for `key`."""
        statement = text(
            f"SELECT count(*) FROM ({_RESOURCE_COUNT_SQL[key]}) usage "  # noqa: S608 - constant SQL
            "JOIN subscriptions s ON s.tenant_id = usage.tenant_id "
            "WHERE s.plan_id = :plan AND s.status IN ('trialing', 'active', 'past_due') "
            "AND usage.used > :limit"
        )
        return int(await self._session.scalar(statement, {"plan": plan.id, "limit": limit}) or 0)


__all__ = ["FEATURES", "PlanCatalogAdmin", "PlanPage"]

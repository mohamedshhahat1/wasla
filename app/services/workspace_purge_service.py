"""Erasing a deleted workspace's data once its retention window has passed.

Deletion is a tombstone: access stops and the rows stay. That is the right
*access-control* answer and it is not erasure, and calling it erasure is the
claim docs/SECURITY.md exists to avoid making. This is the other half - the
thing that makes "deleted" eventually mean "gone".

## The classification

Twenty-nine tables carry a `tenant_id`, and they do not all get the same
treatment. Sorting them is the whole design; the code below is the sorting made
executable.

**Erased.** The customer's business data - what the workspace was *for*.
Conversations and their messages, media rows, sentiment, contacts, leads and
their notes and activities, follow-ups, campaigns and their recipients, agents
and their tool grants, knowledge bases with their documents and chunks,
WhatsApp accounts, templates and inbound events, outbound email rows, and the
invitations that were never accepted. None of it answers a question anybody is
entitled to ask after the customer has gone and the window has passed.

**Kept, for ever.** `invoices`, `payments` - and `payment_events`, which hangs
off payments rather than off the tenant. These are accounting records. A tax
authority, a chargeback, a reconciliation against the processor's own ledger and
a dispute all arrive after a customer leaves, and all of them need the row. A
purge that took them would be destroying financial history to satisfy a
retention policy that nobody wrote down as requiring it.

`audit_logs` is kept for the same class of reason and a different one: it is the
record of who did what, including who deleted this workspace, and a purge that
erased it would erase its own justification. `subscriptions` stays because the
invoices reference it and an invoice whose plan cannot be resolved is a worse
record than one that can.

**Revoked, not merely deleted.** `payment_methods` holds a provider card token.
It is already revoked at deletion time by `WorkspaceService._wind_down_billing`,
which is the point at which it stops being usable; the rows are erased here.
`whatsapp_accounts` holds an encrypted access token and goes with the rest.

**Erased, after an anonymisation design was tried and rejected.**
`usage_events` and `analytics_events` are aggregate counters that also feed
platform-wide reporting, and the appealing idea is to detach them - set
`tenant_id` to NULL, keep the totals, lose the customer. Both columns are
`NOT NULL`, so that would need a migration making them nullable, and it would
buy a dashboard's historical continuity at the price of the invariant tenant
isolation actually rests on: *every tenant-scoped row has a tenant*. Two of the
highest-volume tables in the schema would gain a state in which a row belongs to
nobody, and every query over them would acquire a case it has never had to
handle. That is a bad trade for a reporting nicety, so the rows are erased with
the rest.

**The honest consequence**: platform-wide historical totals shift downward when
a purge runs, because the purged workspace's contribution goes with it. An
operator comparing last quarter's message count before and after a purge sees
different numbers. Recorded here rather than discovered later - if that matters
to the product, the fix is a periodic roll-up into a table with no `tenant_id`
at all, not a nullable one on these.

**Left alone.** `memberships` - already revoked at deletion, and the row is what
records who was in the workspace and when they were removed. It carries no
customer content.

## What this is not

It is not a right-to-erasure implementation for a *person*: that is an account
question, not a workspace one, and the two have different scopes. And it is not
a legal opinion. The retention window is a **product default**; whether it
satisfies any particular regulation is a question for somebody qualified to
answer it, and this module deliberately makes the policy configurable rather
than asserting that thirty days is correct.

## Idempotency

`tenants.purged_at` is the record. It is set in the same transaction as the
deletes, so a worker that dies halfway leaves the row unpurged and the whole
pass runs again - every statement here is a `DELETE ... WHERE tenant_id = ...`,
which is naturally idempotent, so re-running is safe rather than merely
tolerable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.telemetry import observe_lifecycle_event
from app.db.models import Tenant
from app.db.models.audit import AuditAction, AuditActorKind
from app.services.audit_service import AuditTrail

logger = get_logger(__name__)

# Erased outright, in this order. Children before parents: these are real
# foreign keys, and most of them cascade - but relying on a cascade to erase
# customer data means the policy lives in the schema where nobody reviewing a
# retention question would look for it. Naming every table is what makes the
# classification auditable.
PURGED_TABLES: tuple[str, ...] = (
    "message_sentiments",
    "message_media",
    "messages",
    "conversations",
    "campaign_recipients",
    "campaigns",
    "follow_ups",
    "lead_activities",
    "lead_notes",
    "leads",
    "contacts",
    "document_chunks",
    "documents",
    "knowledge_bases",
    "agent_tools",
    "agents",
    "whatsapp_events",
    "whatsapp_templates",
    "whatsapp_accounts",
    "payment_methods",
    "tenant_invitations",
    "email_messages",
    # Aggregate counters. See the classification above for why these are erased
    # rather than detached from the workspace.
    "usage_events",
    "analytics_events",
)

# Named so the test that guards this classification can assert the partition is
# total: every tenant-scoped table is either purged or deliberately kept, and a
# table added later belongs to one list or the other before it can ship.
RETAINED_TABLES: frozenset[str] = frozenset(
    {
        "invoices",
        "payments",
        "subscriptions",
        "audit_logs",
        "memberships",
    }
)


@dataclass(frozen=True, slots=True)
class PurgeOutcome:
    """What one workspace's purge removed."""

    tenant_id: uuid.UUID
    rows_deleted: int


class WorkspacePurgeService:
    """Erases the operational data of one already-deleted workspace."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_due(self, *, now: datetime, limit: int = 20) -> list[Tenant]:
        """Workspaces whose retention has run out and that are not yet purged.

        `FOR UPDATE SKIP LOCKED`, matching every other sweep in this codebase:
        two workers divide the cohort rather than taking turns, and a row one is
        already erasing is skipped rather than waited for.

        The three predicates are the whole eligibility rule, and each is load
        bearing. `deleted_at IS NOT NULL` - only tombstoned workspaces are ever
        touched, so a live workspace cannot be reached by this code path
        whatever the other columns say. `purge_due_at <= now` - the retention
        window has passed. `purged_at IS NULL` - it has not already been done,
        which is the idempotency guard.
        """
        statement = (
            select(Tenant)
            .where(Tenant.deleted_at.is_not(None))
            .where(Tenant.purged_at.is_(None))
            .where(Tenant.purge_due_at.is_not(None))
            .where(Tenant.purge_due_at <= now)
            .order_by(Tenant.purge_due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(statement)).scalars())

    async def purge(self, tenant: Tenant, *, now: datetime | None = None) -> PurgeOutcome:
        """Erase one workspace's operational data.

        **Refuses anything that is not due**, and does so loudly rather than
        returning an empty outcome. Being asked to purge a live workspace is a
        defect in the caller, and the safe response to "erase this thing I
        should not be able to reach" is to refuse, not to do nothing quietly and
        let the next caller try again.

        Everything happens in the caller's transaction, `purged_at` included, so
        a crash halfway leaves the workspace unpurged and the pass repeats. Each
        statement is keyed on `tenant_id`, so a repeat is a no-op rather than an
        error, and no statement here can reach another workspace's rows.
        """
        moment = now if now is not None else datetime.now(UTC)
        if tenant.deleted_at is None:
            raise ValueError("Refusing to purge a workspace that is not deleted.")
        if tenant.purged_at is not None:
            # Already done. Not an error - two workers can legitimately reach
            # the same row through a race the lock resolves - but nothing to do.
            return PurgeOutcome(tenant_id=tenant.id, rows_deleted=0)
        if tenant.purge_due_at is None or tenant.purge_due_at > moment:
            raise ValueError("Refusing to purge a workspace before its retention has passed.")

        deleted = 0
        for table in PURGED_TABLES:
            # Table names come from the module-level tuple above and never from
            # a caller, so the interpolation reaches only names written in this
            # file. The tenant id is bound.
            result = await self._session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :tenant_id"),  # noqa: S608
                {"tenant_id": tenant.id},
            )
            # `CursorResult.rowcount`, which `Result` does not declare - a raw
            # `text()` DELETE always returns the cursor variant, and counting
            # what was erased is worth the narrowing.
            deleted += cast("CursorResult[Any]", result).rowcount or 0

        tenant.purged_at = moment
        AuditTrail(self._session).record(
            AuditAction.WORKSPACE_PURGED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="tenant",
            target_id=tenant.id,
            # The slug, because this entry outlives everything that could be
            # joined to. Recorded with no `tenant_id` of its own: the workspace
            # it names has just had its data erased, and filing the record of
            # that erasure inside the workspace's own trail would be filing it
            # in the thing being erased.
            target_label=tenant.slug,
            meta={"rows_deleted": deleted},
        )
        await self._session.flush()

        observe_lifecycle_event(operation="workspace_purge", outcome="success")
        logger.info(
            "workspace.purged",
            extra={
                "event": "workspace.purged",
                "tenant_id": str(tenant.id),
                "rows_deleted": deleted,
            },
        )
        return PurgeOutcome(tenant_id=tenant.id, rows_deleted=deleted)


async def purge_media_objects(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    """Storage keys the purge is about to orphan, read before the rows go.

    **A database delete does not free an object.** `message_media` holds keys
    into the object store; deleting the rows leaves the files, which is the
    difference between a purge and a purge that looks finished.

    Read as keys and handed to the caller rather than deleted here, because
    object-store deletion is network I/O and this service runs inside a
    transaction. The worker does the removal after the commit, where a failure
    can be retried without holding a lock - and where a failure leaves orphaned
    objects rather than a workspace that is half-erased in the database.
    """
    rows = await session.execute(
        text("SELECT storage_key FROM message_media WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    return [key for (key,) in rows if key]

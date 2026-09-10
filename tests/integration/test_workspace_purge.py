"""Retention and purge: what "deleted" eventually means.

Deletion is a tombstone - access stops, rows stay. That is the right
access-control answer and it is not erasure. This suite is about the second
half: the retention window, the sweep that erases when it passes, and the
classification that decides what is erased and what is kept for ever.

Three properties carry it, and each has tests here.

**Nothing is erased early.** The deadline is stamped at deletion from the
configured retention, and the sweep will not touch a workspace before it.

**Financial and audit records survive.** Invoices, payments, subscriptions and
the audit trail are outside the purge by design; a purge that took them would
be destroying accounting history to satisfy a retention policy nobody wrote
down as requiring it.

**It is idempotent and cannot reach sideways.** `purged_at` is the record, and
every statement is keyed on one `tenant_id`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.models import (
    Membership,
    Tenant,
    TenantRole,
    User,
)
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.billing import BillingInterval, Plan, Subscription, SubscriptionStatus
from app.db.models.conversation import Contact, Conversation
from app.db.models.enums import TenantStatus
from app.db.models.invoice import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.db.models.whatsapp import WhatsAppAccount
from app.services.workspace_purge_service import (
    PURGED_TABLES,
    RETAINED_TABLES,
    WorkspacePurgeService,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


async def _tenant(
    session: AsyncSession,
    *,
    slug: str,
    deleted_at: datetime | None = None,
    purge_due_at: datetime | None = None,
    purged_at: datetime | None = None,
) -> Tenant:
    tenant = Tenant(
        name=slug.title(),
        slug=slug,
        status=TenantStatus.ACTIVE,
        deleted_at=deleted_at,
        purge_due_at=purge_due_at,
        purged_at=purged_at,
    )
    session.add(tenant)
    await session.flush()
    return tenant


async def _business_data(session: AsyncSession, tenant: Tenant) -> Contact:
    """What a workspace is actually for: a number, a customer, a conversation.

    The WhatsApp account is here as well as the conversation it hangs off,
    because it is the row that carries an *encrypted access token* - so this
    covers the credential-bearing table as well as the customer-content ones.
    """
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"pn-{tenant.slug}",
        waba_id=f"waba-{tenant.slug}",
        display_phone_number="+20 100 000 0001",
    )
    session.add(account)
    contact = Contact(
        tenant_id=tenant.id, wa_id=f"2010{tenant.slug[:8]}", display_name="A Customer"
    )
    session.add_all([account, contact])
    await session.flush()
    session.add(Conversation(tenant_id=tenant.id, contact_id=contact.id, account_id=account.id))
    await session.flush()
    return contact


async def _money(session: AsyncSession, tenant: Tenant) -> tuple[Invoice, Payment]:
    plan = await session.scalar(select(Plan).where(Plan.code == "purge-tier"))
    if plan is None:
        plan = Plan(
            code="purge-tier",
            name="Purge Tier",
            price=Decimal("50.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={},
        )
        session.add(plan)
        await session.flush()
    session.add(
        Subscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            status=SubscriptionStatus.CANCELLED,
            current_period_start=NOW - timedelta(days=60),
            current_period_end=NOW - timedelta(days=30),
        )
    )
    invoice = Invoice(
        tenant_id=tenant.id,
        status=InvoiceStatus.PAID,
        plan_code=plan.code,
        amount_due=Decimal("50.00"),
        amount_paid=Decimal("50.00"),
        currency="EGP",
        period_start=NOW - timedelta(days=60),
        period_end=NOW - timedelta(days=30),
        lines=[],
    )
    session.add(invoice)
    await session.flush()
    payment = Payment(
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        amount=Decimal("50.00"),
        currency="EGP",
        status=PaymentStatus.SUCCEEDED,
        provider="paymob",
        provider_reference=f"txn-{tenant.slug}",
    )
    session.add(payment)
    await session.flush()
    return invoice, payment


async def _count(session: AsyncSession, table: str, tenant: Tenant) -> int:
    result = await session.execute(
        text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),  # noqa: S608
        {"t": tenant.id},
    )
    return int(result.scalar() or 0)


# ------------------------------------------------------------ eligibility


async def test_the_classification_covers_every_tenant_scoped_table(
    db_session: AsyncSession,
) -> None:
    """A table added later must be sorted before it can ship.

    The whole design is the classification, and a table that is in neither list
    is one nobody decided about - it would silently survive every purge, which
    is the failure mode that turns a retention policy into a claim.
    """
    from app.db.models import Base

    scoped = {name for name, table in Base.metadata.tables.items() if "tenant_id" in table.columns}
    unsorted_tables = scoped - set(PURGED_TABLES) - RETAINED_TABLES
    assert not unsorted_tables, (
        "these tenant-scoped tables are neither purged nor deliberately retained; "
        f"add them to one list in workspace_purge_service: {sorted(unsorted_tables)}"
    )
    assert not (set(PURGED_TABLES) | RETAINED_TABLES) - scoped


async def test_a_live_workspace_is_never_eligible(db_session: AsyncSession) -> None:
    """The first predicate, and the one that matters most.

    Whatever the other columns say, a workspace that was never deleted cannot
    be reached by this code path.
    """
    tenant = await _tenant(db_session, slug="alive", purge_due_at=NOW - timedelta(days=1))
    service = WorkspacePurgeService(db_session)

    assert [row.id for row in await service.claim_due(now=NOW)] == []
    with pytest.raises(ValueError, match="not deleted"):
        await service.purge(tenant, now=NOW)


async def test_a_workspace_inside_its_retention_window_is_not_eligible(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(
        db_session,
        slug="too-soon",
        deleted_at=NOW - timedelta(days=5),
        purge_due_at=NOW + timedelta(days=25),
    )
    service = WorkspacePurgeService(db_session)

    assert [row.id for row in await service.claim_due(now=NOW)] == []
    with pytest.raises(ValueError, match="before its retention"):
        await service.purge(tenant, now=NOW)

    contact_count = await _count(db_session, "contacts", tenant)
    assert contact_count == 0  # nothing was touched


async def test_deleting_a_workspace_stamps_a_retention_deadline(
    db_session: AsyncSession,
) -> None:
    """Stamped at deletion, so shortening the setting cannot erase data early."""
    from app.core.config import Settings
    from app.services.workspace_service import WorkspaceService

    settings = Settings(
        _env_file=None,
        environment="test",
        cors_origins=[],
        rate_limit_enabled=False,
        default_plan_code="",
        workspace_deletion_retention_days=14,
    )
    owner = User(
        email="stamps@example.com",
        hashed_password=hash_password("correct horse battery staple"),
        is_active=True,
        email_verified_at=NOW,
    )
    db_session.add(owner)
    await db_session.flush()
    tenant = await _tenant(db_session, slug="stamped")
    db_session.add(Membership(tenant_id=tenant.id, user_id=owner.id, role=TenantRole.TENANT_OWNER))
    await db_session.flush()

    await WorkspaceService(db_session, settings=settings).delete(
        tenant_id=tenant.id,
        actor=owner,
        confirmation="stamped",
    )

    await db_session.refresh(tenant)
    assert tenant.purge_due_at is not None
    assert tenant.deleted_at is not None
    assert abs((tenant.purge_due_at - tenant.deleted_at) - timedelta(days=14)) < timedelta(
        seconds=5
    )
    assert tenant.purged_at is None


# ----------------------------------------------------------------- purging


async def test_purging_erases_business_data_and_keeps_the_money(
    db_session: AsyncSession,
) -> None:
    """The classification, proved on real rows.

    The negative half is the important one: an accounting record that a purge
    removed is not recoverable, and the customer is gone, so nobody notices
    until a tax question arrives.
    """
    tenant = await _tenant(
        db_session,
        slug="due-now",
        deleted_at=NOW - timedelta(days=31),
        purge_due_at=NOW - timedelta(days=1),
    )
    await _business_data(db_session, tenant)
    invoice, payment = await _money(db_session, tenant)

    outcome = await WorkspacePurgeService(db_session).purge(tenant, now=NOW)

    assert outcome.rows_deleted > 0
    assert await _count(db_session, "contacts", tenant) == 0
    assert await _count(db_session, "conversations", tenant) == 0
    # The credential-bearing row goes too.
    assert await _count(db_session, "whatsapp_accounts", tenant) == 0

    # And the records that outlive the customer.
    await db_session.refresh(invoice)
    await db_session.refresh(payment)
    assert invoice.status is InvoiceStatus.PAID
    assert payment.provider_reference == f"txn-{tenant.slug}"
    assert await _count(db_session, "subscriptions", tenant) == 1

    await db_session.refresh(tenant)
    assert tenant.purged_at == NOW
    assert tenant.is_purged


async def test_the_purge_is_audited_as_a_system_act(db_session: AsyncSession) -> None:
    """Nobody typed this. The trail should not suggest somebody did.

    Recorded with no `tenant_id` of its own: the workspace it names has just had
    its data erased, and filing the record of the erasure inside the workspace's
    own trail would be filing it in the thing being erased.
    """
    tenant = await _tenant(
        db_session,
        slug="audited-purge",
        deleted_at=NOW - timedelta(days=31),
        purge_due_at=NOW - timedelta(days=1),
    )
    await WorkspacePurgeService(db_session).purge(tenant, now=NOW)

    rows = await db_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.WORKSPACE_PURGED)
    )
    entry = next(iter(rows.scalars()))
    assert entry.actor_kind is AuditActorKind.SYSTEM
    assert entry.actor_id is None
    assert entry.target_label == "audited-purge"
    assert entry.tenant_id is None


async def test_purging_twice_is_a_no_op(db_session: AsyncSession) -> None:
    """`purged_at` is the idempotency key.

    Two workers can legitimately reach the same row through a race the lock
    resolves, so a second call is not an error - it does nothing.
    """
    tenant = await _tenant(
        db_session,
        slug="twice",
        deleted_at=NOW - timedelta(days=31),
        purge_due_at=NOW - timedelta(days=1),
    )
    await _business_data(db_session, tenant)
    service = WorkspacePurgeService(db_session)

    first = await service.purge(tenant, now=NOW)
    second = await service.purge(tenant, now=NOW + timedelta(hours=1))

    assert first.rows_deleted > 0
    assert second.rows_deleted == 0
    await db_session.refresh(tenant)
    # The original timestamp, not the second call's.
    assert tenant.purged_at == NOW
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AuditAction.WORKSPACE_PURGED)
        )
    ) == 1


async def test_one_workspaces_purge_cannot_reach_another(db_session: AsyncSession) -> None:
    """Tenant isolation, in the one operation whose whole job is deleting rows."""
    doomed = await _tenant(
        db_session,
        slug="doomed-purge",
        deleted_at=NOW - timedelta(days=31),
        purge_due_at=NOW - timedelta(days=1),
    )
    neighbour = await _tenant(db_session, slug="neighbour-purge")
    await _business_data(db_session, doomed)
    await _business_data(db_session, neighbour)

    await WorkspacePurgeService(db_session).purge(doomed, now=NOW)

    assert await _count(db_session, "contacts", doomed) == 0
    assert await _count(db_session, "contacts", neighbour) == 1
    assert await _count(db_session, "conversations", neighbour) == 1
    await db_session.refresh(neighbour)
    assert neighbour.purged_at is None


async def test_claim_due_finds_only_what_is_actually_due(db_session: AsyncSession) -> None:
    """The sweep's eligibility rule, with every near-miss beside it."""
    due = await _tenant(
        db_session,
        slug="claim-due",
        deleted_at=NOW - timedelta(days=31),
        purge_due_at=NOW - timedelta(days=1),
    )
    await _tenant(db_session, slug="claim-live")
    await _tenant(
        db_session,
        slug="claim-early",
        deleted_at=NOW - timedelta(days=1),
        purge_due_at=NOW + timedelta(days=29),
    )
    await _tenant(
        db_session,
        slug="claim-done",
        deleted_at=NOW - timedelta(days=60),
        purge_due_at=NOW - timedelta(days=30),
        purged_at=NOW - timedelta(days=29),
    )
    # A tombstone from before the columns existed: no deadline, so invisible
    # until the 0050 backfill gives it one.
    await _tenant(db_session, slug="claim-legacy", deleted_at=NOW - timedelta(days=90))

    claimed = await WorkspacePurgeService(db_session).claim_due(now=NOW)

    assert [row.slug for row in claimed] == [due.slug]

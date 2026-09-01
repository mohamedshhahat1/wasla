"""The audit trail, written by the actions that generate it.

An audit log is read when somebody is asking a hostile question, so the claims
worth testing are the ones that hold up under one: the entry names a person
rather than an id, it survives that person being deleted, it does not exist when
the action was rolled back, and a platform administrator acting on a workspace
appears in *that workspace's* trail rather than only in the platform's.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import BillingInterval, Plan
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccountStatus
from app.integrations.billing import MANUAL_PROVIDER
from app.repositories.audit_repository import (
    AuditLogRepository,
    PlatformAuditLogRepository,
)
from app.services.audit_service import AuditTrail
from app.services.subscription_service import SubscriptionService
from app.services.whatsapp_account_service import WhatsAppAccountService
from tests.fake_ownership import FakeOwnershipVerifier

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


async def _tenant(session, slug: str = "acme") -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _user(session, email: str = "owner@acme-example.com", **kwargs) -> User:
    user = User(email=email, hashed_password="x", full_name="Owner", **kwargs)
    session.add(user)
    await session.flush()
    return user


async def _entries(session, tenant):
    return await AuditLogRepository(session, tenant_id=tenant.id).list_entries()


# ------------------------------------------------------------- the recorder


async def test_an_entry_names_the_person_not_their_id(db_session):
    """ "user 8f3c… did something" is useless precisely when somebody asks."""
    tenant = await _tenant(db_session)
    user = await _user(db_session)

    AuditTrail(db_session, tenant_id=tenant.id).record(
        AuditAction.MEMBER_INVITED,
        actor=user,
        target_type="invitation",
        target_label="colleague@example.com",
    )
    await db_session.flush()

    entry = (await _entries(db_session, tenant))[0]
    assert entry.actor_label == "owner@acme-example.com"
    assert entry.target_label == "colleague@example.com"


async def test_an_entry_survives_the_actor_being_deleted(db_session):
    """Deleting an account must not erase what it did - which is exactly what
    somebody would do if it worked."""
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    AuditTrail(db_session, tenant_id=tenant.id).record(
        AuditAction.MEMBER_INVITED,
        actor=user,
    )
    await db_session.flush()

    await db_session.delete(user)
    await db_session.flush()

    entry = (await _entries(db_session, tenant))[0]
    assert entry.actor_id is None
    # The copy is what keeps it readable.
    assert entry.actor_label == "owner@acme-example.com"


async def test_an_action_by_nobody_is_recorded_as_the_system(db_session):
    """ "Nobody did this, time did" is a real answer to "who cancelled my plan"."""
    tenant = await _tenant(db_session)

    AuditTrail(db_session, tenant_id=tenant.id).record(AuditAction.SUBSCRIPTION_CANCELLED)
    await db_session.flush()

    entry = (await _entries(db_session, tenant))[0]
    assert entry.actor_kind is AuditActorKind.SYSTEM
    assert entry.actor_label == "system"


async def test_a_platform_action_needs_no_workspace(db_session):
    """A platform administrator acts across workspaces rather than inside one,
    and those acts are the ones most worth recording."""
    staff = await _user(db_session, email="staff@example.com")
    staff.platform_role = None
    AuditTrail(db_session).record(
        AuditAction.INVOICE_VOIDED,
        actor=staff,
        actor_kind=AuditActorKind.PLATFORM_STAFF,
    )
    await db_session.flush()

    entries = await PlatformAuditLogRepository(db_session).list_entries()
    assert entries[0].tenant_id is None
    assert entries[0].actor_kind is AuditActorKind.PLATFORM_STAFF


async def test_an_entry_that_rolled_back_did_not_happen(db_session):
    """A log claiming somebody did something they did not is worse than no log,
    because it is believed."""
    tenant = await _tenant(db_session)
    tenant_id = tenant.id
    await db_session.commit()

    AuditTrail(db_session, tenant_id=tenant_id).record(AuditAction.CAMPAIGN_SCHEDULED)
    await db_session.flush()
    await db_session.rollback()

    assert await AuditLogRepository(db_session, tenant_id=tenant_id).list_entries() == []


def _account_service(session) -> WhatsAppAccountService:
    """The real service, with ownership verification faked but still required.

    Built through the fake rather than omitted, because a service with no
    verifier refuses to connect anything - which is the behaviour under test
    elsewhere, and would make these audit assertions unreachable.
    """
    return WhatsAppAccountService(
        session=session,
        ownership=FakeOwnershipVerifier().owns("109876543210"),
    )


# -------------------------------------------------- written by real actions


async def test_connecting_a_number_is_recorded(db_session):
    tenant = await _tenant(db_session)
    user = await _user(db_session)

    await _account_service(db_session).connect(
        tenant_id=tenant.id,
        phone_number_id="109876543210",
        access_token="a-token-the-fake-verifier-accepts",
        waba_id="555000111",
        actor=user,
    )
    await db_session.flush()

    entry = (await _entries(db_session, tenant))[0]
    assert entry.action is AuditAction.WHATSAPP_ACCOUNT_CONNECTED
    # The number a person would recognise, not the opaque id Meta uses.
    assert entry.target_label == "+201000000000"


async def test_disabling_a_number_is_a_different_action_from_enabling(db_session):
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    service = _account_service(db_session)
    account = await service.connect(
        tenant_id=tenant.id,
        phone_number_id="109876543210",
        access_token="a-token-the-fake-verifier-accepts",
        waba_id="555000111",
        actor=user,
    )

    await service.set_status(
        tenant_id=tenant.id,
        account_id=account.id,
        status=WhatsAppAccountStatus.DISABLED,
        actor=user,
    )
    await service.set_status(
        tenant_id=tenant.id,
        account_id=account.id,
        status=WhatsAppAccountStatus.ACTIVE,
        actor=user,
    )
    await db_session.flush()

    actions = [entry.action for entry in await _entries(db_session, tenant)]
    assert AuditAction.WHATSAPP_ACCOUNT_DISABLED in actions
    assert AuditAction.WHATSAPP_ACCOUNT_ENABLED in actions


async def test_starting_a_subscription_is_recorded_with_its_plan(db_session):
    tenant = await _tenant(db_session)
    user = await _user(db_session)
    plan = Plan(
        code="pro",
        name="Pro",
        price=Decimal("99.00"),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        limits={},
    )
    db_session.add(plan)
    await db_session.flush()

    await SubscriptionService(db_session, tenant_id=tenant.id).start(
        plan_code="pro",
        now=NOW,
        actor=user,
        # A priced plan is granted by settlement rather than chosen (ADR-059).
        self_service=False,
    )
    await db_session.flush()

    entry = (await _entries(db_session, tenant))[0]
    assert entry.action is AuditAction.SUBSCRIPTION_STARTED
    assert entry.target_label == "pro"


async def test_a_subscription_started_by_registration_is_recorded_as_the_system(db_session):
    """Nobody chose it: the workspace was put on the default plan at signup."""
    tenant = await _tenant(db_session)
    plan = Plan(
        code="starter",
        name="Starter",
        price=Decimal("0.00"),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        limits={},
    )
    db_session.add(plan)
    await db_session.flush()

    await SubscriptionService(db_session, tenant_id=tenant.id).start(
        plan_code="starter",
        now=NOW,
    )
    await db_session.flush()

    entry = (await _entries(db_session, tenant))[0]
    assert entry.actor_kind is AuditActorKind.SYSTEM


async def test_a_platform_payment_appears_in_the_workspaces_own_trail(db_session):
    """The customer is entitled to see who marked their invoice paid."""
    from app.db.models.invoice import Invoice, InvoiceStatus
    from app.platform.platform_billing import PlatformBillingService

    tenant = await _tenant(db_session)
    staff = await _user(db_session, email="staff@example.com")
    invoice = Invoice(
        tenant_id=tenant.id,
        status=InvoiceStatus.OPEN,
        plan_code="pro",
        amount_due=Decimal("99.00"),
        amount_paid=Decimal("0.00"),
        currency="USD",
        period_start=NOW,
        period_end=NOW,
        lines=[],
    )
    db_session.add(invoice)
    await db_session.flush()

    await PlatformBillingService(db_session).record_payment(
        invoice_id=invoice.id,
        amount=Decimal("99.00"),
        provider=MANUAL_PROVIDER,
        actor=staff,
    )
    await db_session.flush()

    entry = (await _entries(db_session, tenant))[0]
    assert entry.action is AuditAction.PAYMENT_RECORDED
    assert entry.actor_kind is AuditActorKind.PLATFORM_STAFF
    assert entry.actor_label == "staff@example.com"


# ----------------------------------------------------------------- reading


async def test_one_workspace_cannot_read_anothers_trail(db_session):
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    AuditTrail(db_session, tenant_id=acme.id).record(AuditAction.CAMPAIGN_SCHEDULED)
    await db_session.flush()

    assert await _entries(db_session, rival) == []
    assert len(await _entries(db_session, acme)) == 1


async def test_the_platform_reader_sees_every_workspace_and_the_platform(db_session):
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    AuditTrail(db_session, tenant_id=acme.id).record(AuditAction.CAMPAIGN_SCHEDULED)
    AuditTrail(db_session, tenant_id=rival.id).record(AuditAction.CAMPAIGN_CANCELLED)
    AuditTrail(db_session).record(AuditAction.INVOICE_VOIDED)
    await db_session.flush()

    entries = await PlatformAuditLogRepository(db_session).list_entries()
    assert len(entries) == 3


async def test_the_platform_reader_can_be_narrowed_to_one_workspace(db_session):
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    AuditTrail(db_session, tenant_id=acme.id).record(AuditAction.CAMPAIGN_SCHEDULED)
    AuditTrail(db_session, tenant_id=rival.id).record(AuditAction.CAMPAIGN_CANCELLED)
    await db_session.flush()

    entries = await PlatformAuditLogRepository(db_session).list_entries(tenant_id=acme.id)
    assert [entry.tenant_id for entry in entries] == [acme.id]


async def test_entries_can_be_filtered_by_action(db_session):
    tenant = await _tenant(db_session)
    trail = AuditTrail(db_session, tenant_id=tenant.id)
    trail.record(AuditAction.CAMPAIGN_SCHEDULED)
    trail.record(AuditAction.MEMBER_INVITED)
    await db_session.flush()

    entries = await AuditLogRepository(db_session, tenant_id=tenant.id).list_entries(
        actions=[AuditAction.MEMBER_INVITED],
    )
    assert [entry.action for entry in entries] == [AuditAction.MEMBER_INVITED]


async def test_entries_can_be_filtered_by_actor(db_session):
    tenant = await _tenant(db_session)
    one = await _user(db_session, email="one@example.com")
    two = await _user(db_session, email="two@example.com")
    trail = AuditTrail(db_session, tenant_id=tenant.id)
    trail.record(AuditAction.CAMPAIGN_SCHEDULED, actor=one)
    trail.record(AuditAction.CAMPAIGN_CANCELLED, actor=two)
    await db_session.flush()

    entries = await AuditLogRepository(db_session, tenant_id=tenant.id).list_entries(
        actor_id=two.id,
    )
    assert [entry.actor_label for entry in entries] == ["two@example.com"]


async def test_there_is_no_way_to_edit_an_entry(db_session):
    """The repository offers no update and no delete: handing somebody the
    ability to edit the record of what they did defeats the purpose."""
    repository = AuditLogRepository(db_session, tenant_id=uuid.uuid4())
    assert not hasattr(repository, "update")
    assert not hasattr(repository, "delete")

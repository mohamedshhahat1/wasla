"""WhatsApp repository behaviour against a real PostgreSQL database.

The properties under test are the ones a fake cannot honestly assert: that the
platform-wide uniqueness of a phone number is enforced by the database and not
only by a repository read, that workspace scoping actually filters rows, and
that replaying a webhook event stores one row rather than two.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, TenantIsolationError
from app.db.models import (
    Tenant,
    WhatsAppAccount,
    WhatsAppAccountStatus,
    WhatsAppEvent,
    WhatsAppEventKind,
    WhatsAppEventState,
)
from app.repositories import (
    WhatsAppAccountDirectory,
    WhatsAppAccountRepository,
    WhatsAppEventRepository,
)

pytestmark = pytest.mark.integration

PHONE_NUMBER_ID = "109876543210"
OTHER_PHONE_NUMBER_ID = "209876543210"
WABA_ID = "555000111"
DISPLAY_NUMBER = "+201000000000"
PAYLOAD = {"messages": [{"id": "wamid.one", "text": {"body": "hello"}}]}


async def _tenant(session, *, slug: str) -> Tenant:
    tenant = Tenant(name=slug.capitalize(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _account(session, *, tenant: Tenant, phone_number_id: str) -> WhatsAppAccount:
    account = await WhatsAppAccountRepository(session, tenant_id=tenant.id).connect(
        phone_number_id=phone_number_id,
        waba_id=WABA_ID,
        display_phone_number=DISPLAY_NUMBER,
    )
    await session.flush()
    return account


async def test_connect_persists_an_active_account(db_session):
    tenant = await _tenant(db_session, slug="acme")

    account = await WhatsAppAccountRepository(db_session, tenant_id=tenant.id).connect(
        phone_number_id=PHONE_NUMBER_ID,
        waba_id=WABA_ID,
        display_phone_number=DISPLAY_NUMBER,
        display_name="Acme Support",
    )
    await db_session.flush()
    await db_session.refresh(account)

    assert account.id is not None
    assert account.tenant_id == tenant.id
    assert account.status is WhatsAppAccountStatus.ACTIVE
    assert account.is_active is True
    # The refresh is what makes the server-side default readable; the account
    # API serialises created_at straight after connecting.
    assert account.created_at is not None


async def test_a_number_cannot_be_claimed_by_two_workspaces(db_session):
    first = await _tenant(db_session, slug="first")
    second = await _tenant(db_session, slug="second")
    await _account(db_session, tenant=first, phone_number_id=PHONE_NUMBER_ID)

    with pytest.raises(ConflictError):
        await WhatsAppAccountRepository(db_session, tenant_id=second.id).connect(
            phone_number_id=PHONE_NUMBER_ID,
            waba_id=WABA_ID,
            display_phone_number=DISPLAY_NUMBER,
        )


async def test_the_database_rejects_a_duplicate_number(db_session):
    """The repository read is the fast path; the constraint is the guarantee."""
    first = await _tenant(db_session, slug="first")
    second = await _tenant(db_session, slug="second")
    await _account(db_session, tenant=first, phone_number_id=PHONE_NUMBER_ID)

    db_session.add(
        WhatsAppAccount(
            tenant_id=second.id,
            phone_number_id=PHONE_NUMBER_ID,
            waba_id=WABA_ID,
            display_phone_number=DISPLAY_NUMBER,
            status=WhatsAppAccountStatus.ACTIVE,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()

    await db_session.rollback()


async def test_the_directory_resolves_a_number_across_workspaces(db_session):
    """The one unscoped lookup: inbound traffic has no workspace yet."""
    tenant = await _tenant(db_session, slug="acme")
    account = await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    found = await WhatsAppAccountDirectory(db_session).get_by_phone_number_id(PHONE_NUMBER_ID)
    assert found is not None
    assert found.id == account.id
    assert found.tenant_id == tenant.id

    assert await WhatsAppAccountDirectory(db_session).get_by_phone_number_id("404") is None


async def test_accounts_are_invisible_to_another_workspace(db_session):
    owner = await _tenant(db_session, slug="owner")
    outsider = await _tenant(db_session, slug="outsider")
    account = await _account(db_session, tenant=owner, phone_number_id=PHONE_NUMBER_ID)

    outsider_repository = WhatsAppAccountRepository(db_session, tenant_id=outsider.id)
    assert await outsider_repository.get_by_id(account.id) is None
    assert await outsider_repository.list_all() == []

    with pytest.raises(TenantIsolationError):
        await outsider_repository.require_by_id(account.id)

    owner_repository = WhatsAppAccountRepository(db_session, tenant_id=owner.id)
    assert [row.id for row in await owner_repository.list_all()] == [account.id]


async def test_recording_the_same_event_twice_stores_one_row(db_session):
    tenant = await _tenant(db_session, slug="acme")
    account = await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)
    repository = WhatsAppEventRepository(db_session, tenant_id=tenant.id)
    received_at = datetime.now(UTC)

    first, created = await repository.record(
        account_id=account.id,
        event_id="wamid.one",
        kind=WhatsAppEventKind.MESSAGE,
        payload=PAYLOAD,
        received_at=received_at,
    )
    await db_session.flush()
    assert created is True
    assert first.state is WhatsAppEventState.RECEIVED

    second, created_again = await repository.record(
        account_id=account.id,
        event_id="wamid.one",
        kind=WhatsAppEventKind.MESSAGE,
        payload=PAYLOAD,
        received_at=received_at,
    )
    await db_session.flush()

    assert created_again is False
    assert second.id == first.id

    total = await db_session.scalar(select(func.count()).select_from(WhatsAppEvent))
    assert total == 1


async def test_one_workspace_cannot_suppress_anothers_event(db_session):
    """Idempotency is per workspace, so a shared event id stores twice."""
    first = await _tenant(db_session, slug="first")
    second = await _tenant(db_session, slug="second")
    first_account = await _account(db_session, tenant=first, phone_number_id=PHONE_NUMBER_ID)
    second_account = await _account(
        db_session,
        tenant=second,
        phone_number_id=OTHER_PHONE_NUMBER_ID,
    )
    received_at = datetime.now(UTC)

    for tenant, account in ((first, first_account), (second, second_account)):
        _, created = await WhatsAppEventRepository(db_session, tenant_id=tenant.id).record(
            account_id=account.id,
            event_id="wamid.shared",
            kind=WhatsAppEventKind.MESSAGE,
            payload=PAYLOAD,
            received_at=received_at,
        )
        assert created is True

    await db_session.flush()

    total = await db_session.scalar(select(func.count()).select_from(WhatsAppEvent))
    assert total == 2


async def test_the_database_rejects_a_duplicate_event_for_one_workspace(db_session):
    tenant = await _tenant(db_session, slug="acme")
    account = await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)
    received_at = datetime.now(UTC)

    for _ in range(2):
        db_session.add(
            WhatsAppEvent(
                tenant_id=tenant.id,
                account_id=account.id,
                event_id="wamid.one",
                kind=WhatsAppEventKind.MESSAGE,
                state=WhatsAppEventState.RECEIVED,
                payload=PAYLOAD,
                received_at=received_at,
            )
        )

    with pytest.raises(IntegrityError):
        await db_session.flush()

    await db_session.rollback()


async def test_events_are_listed_newest_first(db_session):
    tenant = await _tenant(db_session, slug="acme")
    account = await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)
    repository = WhatsAppEventRepository(db_session, tenant_id=tenant.id)
    base = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    for offset, event_id in enumerate(("wamid.old", "wamid.mid", "wamid.new")):
        await repository.record(
            account_id=account.id,
            event_id=event_id,
            kind=WhatsAppEventKind.MESSAGE,
            payload=PAYLOAD,
            received_at=base.replace(minute=offset),
        )
    await db_session.flush()

    recent = await repository.list_recent(limit=2)
    assert [event.event_id for event in recent] == ["wamid.new", "wamid.mid"]

"""Keyset paging against PostgreSQL.

The property worth proving is the one offset pagination cannot offer: walking a
collection that is being written to underneath the reader still yields every row
exactly once. That needs a real database, because it turns on how PostgreSQL
orders nulls and ties.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.services.inbox_service import InboxService

pytestmark = pytest.mark.integration

START = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


async def _workspace(session, *, slug="acme"):
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{slug}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    session.add(account)
    await session.flush()
    return tenant, account


async def _conversation(session, *, tenant, account, index, last_message_at):
    contact = Contact(tenant_id=tenant.id, wa_id=f"2012345678{index:02d}")
    session.add(contact)
    await session.flush()
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        last_message_at=last_message_at,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def _walk(inbox, *, limit, lister):
    """Page all the way through, collecting ids and guarding against a loop."""
    seen: list = []
    cursor = None
    for _ in range(50):
        page = await lister(inbox, limit, cursor)
        seen.extend(row.id for row in page.items)
        if page.next_cursor is None:
            return seen
        cursor = page.next_cursor
    raise AssertionError("pagination did not terminate")


async def _list_conversations(inbox, limit, cursor):
    return await inbox.list_conversations(limit=limit, cursor=cursor)


def _list_messages(conversation_id):
    async def lister(inbox, limit, cursor):
        return await inbox.list_messages(
            conversation_id=conversation_id,
            limit=limit,
            cursor=cursor,
        )

    return lister


async def test_paging_visits_every_conversation_exactly_once(db_session):
    tenant, account = await _workspace(db_session)
    for index in range(7):
        await _conversation(
            db_session,
            tenant=tenant,
            account=account,
            index=index,
            last_message_at=START - timedelta(minutes=index),
        )
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    seen = await _walk(inbox, limit=3, lister=_list_conversations)

    assert len(seen) == 7
    assert len(set(seen)) == 7


async def test_pages_stay_in_most_recent_first_order(db_session):
    tenant, account = await _workspace(db_session)
    expected = []
    for index in range(6):
        conversation = await _conversation(
            db_session,
            tenant=tenant,
            account=account,
            index=index,
            last_message_at=START - timedelta(minutes=index),
        )
        expected.append(conversation.id)
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    seen = await _walk(inbox, limit=2, lister=_list_conversations)

    assert seen == expected


async def test_conversations_sharing_an_instant_are_not_skipped(db_session):
    """The id tiebreaker is what makes the ordering total.

    Without it the boundary between two rows at the same instant is arbitrary,
    which is another way of saying one of them can fall through it.
    """
    tenant, account = await _workspace(db_session)
    for index in range(6):
        await _conversation(
            db_session,
            tenant=tenant,
            account=account,
            index=index,
            last_message_at=START,
        )
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    seen = await _walk(inbox, limit=2, lister=_list_conversations)

    assert len(seen) == 6
    assert len(set(seen)) == 6


async def test_a_conversation_with_no_messages_sorts_last_and_is_still_reached(db_session):
    """A descending sort would otherwise put nulls first, ahead of live traffic."""
    tenant, account = await _workspace(db_session)
    silent = await _conversation(
        db_session,
        tenant=tenant,
        account=account,
        index=0,
        last_message_at=None,
    )
    for index in range(1, 5):
        await _conversation(
            db_session,
            tenant=tenant,
            account=account,
            index=index,
            last_message_at=START - timedelta(minutes=index),
        )
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    seen = await _walk(inbox, limit=2, lister=_list_conversations)

    assert len(seen) == 5
    assert seen[-1] == silent.id


async def test_several_silent_conversations_all_page_through(db_session):
    """The null block needs its own keyset, since null is not comparable."""
    tenant, account = await _workspace(db_session)
    for index in range(5):
        await _conversation(
            db_session,
            tenant=tenant,
            account=account,
            index=index,
            last_message_at=None,
        )
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    seen = await _walk(inbox, limit=2, lister=_list_conversations)

    assert len(set(seen)) == 5


async def test_a_conversation_arriving_mid_walk_never_duplicates_an_earlier_row(db_session):
    """The reason for keyset paging at all.

    An offset would shift under the insert and hand the reader a row it has
    already seen.
    """
    tenant, account = await _workspace(db_session)
    for index in range(4):
        await _conversation(
            db_session,
            tenant=tenant,
            account=account,
            index=index,
            last_message_at=START - timedelta(minutes=index),
        )
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    first = await inbox.list_conversations(limit=2)
    # Newest of all, so an offset-based reader would be pushed backwards by it.
    await _conversation(
        db_session,
        tenant=tenant,
        account=account,
        index=99,
        last_message_at=START + timedelta(minutes=5),
    )
    second = await inbox.list_conversations(limit=2, cursor=first.next_cursor)

    assert {row.id for row in first.items}.isdisjoint({row.id for row in second.items})


async def test_the_last_page_offers_no_cursor(db_session):
    tenant, account = await _workspace(db_session)
    for index in range(3):
        await _conversation(
            db_session,
            tenant=tenant,
            account=account,
            index=index,
            last_message_at=START - timedelta(minutes=index),
        )
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    page = await inbox.list_conversations(limit=10)

    assert len(page.items) == 3
    assert page.next_cursor is None


async def test_paging_never_crosses_into_another_workspace(db_session):
    mine, my_account = await _workspace(db_session, slug="mine")
    theirs, their_account = await _workspace(db_session, slug="theirs")
    for index in range(3):
        await _conversation(
            db_session,
            tenant=mine,
            account=my_account,
            index=index,
            last_message_at=START - timedelta(minutes=index),
        )
    for index in range(3):
        await _conversation(
            db_session,
            tenant=theirs,
            account=their_account,
            index=index,
            last_message_at=START - timedelta(minutes=index),
        )
    inbox = InboxService(session=db_session, tenant_id=mine.id)

    seen = await _walk(inbox, limit=1, lister=_list_conversations)

    assert len(seen) == 3


async def test_a_cursor_from_another_workspace_still_reaches_nothing(db_session):
    """The cursor is a position, not an authorisation.

    It is only ever applied inside a tenant-scoped query, so replaying one taken
    from another workspace can widen nothing.
    """
    mine, my_account = await _workspace(db_session, slug="mine")
    theirs, their_account = await _workspace(db_session, slug="theirs")
    for index in range(4):
        await _conversation(
            db_session,
            tenant=theirs,
            account=their_account,
            index=index,
            last_message_at=START - timedelta(minutes=index),
        )
    await _conversation(
        db_session,
        tenant=mine,
        account=my_account,
        index=0,
        last_message_at=START - timedelta(hours=1),
    )

    their_page = await InboxService(
        session=db_session,
        tenant_id=theirs.id,
    ).list_conversations(limit=2)
    mine_page = await InboxService(session=db_session, tenant_id=mine.id).list_conversations(
        limit=2,
        cursor=their_page.next_cursor,
    )

    assert all(row.tenant_id == mine.id for row in mine_page.items)


async def test_paging_visits_every_message_exactly_once(db_session):
    tenant, account = await _workspace(db_session)
    conversation = await _conversation(
        db_session,
        tenant=tenant,
        account=account,
        index=0,
        last_message_at=START,
    )
    for index in range(7):
        db_session.add(
            Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                wa_message_id=f"wamid.{index}",
                direction=MessageDirection.INBOUND,
                kind=MessageKind.TEXT,
                status=MessageStatus.RECEIVED,
                body=f"message {index}",
            )
        )
    await db_session.flush()
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    seen = await _walk(inbox, limit=3, lister=_list_messages(conversation.id))

    assert len(seen) == 7
    assert len(set(seen)) == 7

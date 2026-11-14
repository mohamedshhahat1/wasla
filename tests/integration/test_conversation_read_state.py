"""The team's shared read state, the preview and the first reply (UR, ADR-198).

Against PostgreSQL, through the real routes, on the inbox contract's fixture:

- a conversation is read for every member once one member has seen it, and
  says who saw it; only the customer's messages are ever unread;
- the read position moves forward only, except by `/unread`;
- the list carries a capped count and a preview of the newest message in the
  same number of statements for 1 row and for 50;
- `unread=true` narrows the one ordering and binds the cursor;
- the summary counts each unread customer message once, not closed, capped;
- a member's reply marks read and takes an unassigned conversation, through the
  ordinary assignment - never a conversation somebody already owns.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.dependencies import (
    ActiveWorkspaceDep,
    ChannelRegistryDep,
    get_media_storage,
    get_messaging_service,
)
from app.core import events
from app.core.config import Settings
from app.core.dependencies import SessionDep, get_settings_from_state
from app.db.models import Membership, MembershipStatus, User
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.conversation import (
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.enums import TenantRole
from app.db.session import Database
from app.main import create_app
from app.schemas.conversation import EXCERPT_LENGTH, excerpt
from app.services.messaging_service import MessagingService
from app.services.read_state_service import ReadStateService
from tests.conftest import FakeDependency
from tests.integration.campaign_harness import FakeMeta, erase, harness_settings, seed, worker
from tests.integration.test_inbox_contract import (
    LIST,
    Inbox,
    MemoryStorage,
    Shop,
    _person,
    _second_account,
    customer,
    message,
    statements,
)

pytestmark = pytest.mark.integration

SUMMARY = f"{LIST}/unread-summary"


@pytest_asyncio.fixture
async def desk(
    prepared_database: str, settings: Settings, fake_redis: FakeDependency
) -> AsyncIterator[Inbox]:
    """The inbox contract's two workspaces, with sends answered by a fake Meta."""
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="read-state")
    application: FastAPI = create_app(settings)
    application.state.database = database
    application.state.redis = fake_redis
    application.dependency_overrides[get_settings_from_state] = lambda: settings
    storage = MemoryStorage()
    application.dependency_overrides[get_media_storage] = lambda: storage
    http = meta.client("read-state")
    sending = harness_settings(prepared_database)

    def messaging(
        session: SessionDep, workspace: ActiveWorkspaceDep, channels: ChannelRegistryDep
    ) -> MessagingService:
        return MessagingService(
            session=session,
            settings=sending,
            tenant_id=workspace.tenant.id,
            channels=channels,
            http=http,
        )

    application.dependency_overrides[get_messaging_service] = messaging

    shops: list[Shop] = []
    for _ in range(2):
        workspace = await seed(database, recipients=0)
        shops.append(
            Shop(
                tenant_id=workspace.tenant_id,
                account_id=workspace.account_id,
                second_account_id=await _second_account(database, workspace.tenant_id),
                owner=await _person(
                    database, settings, workspace.tenant_id, TenantRole.TENANT_OWNER
                ),
                admin=await _person(
                    database, settings, workspace.tenant_id, TenantRole.TENANT_ADMIN
                ),
                member=await _person(database, settings, workspace.tenant_id, TenantRole.MEMBER),
            )
        )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://wasla.test"
        ) as client:
            yield Inbox(client, database, storage, shops[0], shops[1])
    finally:
        await http.aclose()
        await erase(database)
        await database.dispose()


def recent() -> datetime:
    """Inside the 24-hour window by the wall clock the send policy reads."""
    return datetime.now(UTC) - timedelta(minutes=5)


async def writer(desk: Inbox, *, shop: Shop | None = None, **values: Any) -> Conversation:
    shop = shop or desk.mine
    values.setdefault("name", "Nour Hassan")
    values.setdefault("phone", f"2010{uuid.uuid4().int % 10**8:08d}")
    values.setdefault("last_message_at", recent())
    return await customer(desk.database, shop.tenant_id, shop.account_id, **values)


async def outbound(
    database: Database,
    conversation: Conversation,
    *,
    origin: MessageOrigin = MessageOrigin.AGENT,
    body: str = "a reply",
    sent_by_id: uuid.UUID | None = None,
) -> Message:
    async with database.session_factory() as session:
        row = Message(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            direction=MessageDirection.OUTBOUND,
            kind=MessageKind.TEXT,
            status=MessageStatus.SENT,
            origin=origin,
            body=body,
            sent_by_id=sent_by_id,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def detail(desk: Inbox, conversation: Conversation, headers: dict[str, str]) -> Any:
    response = await desk.client.get(f"{LIST}/{conversation.id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def mark(
    desk: Inbox, conversation: Conversation, message_id: uuid.UUID, headers: dict[str, str]
) -> Any:
    return await desk.client.post(
        f"{LIST}/{conversation.id}/read",
        json={"up_to_message_id": str(message_id)},
        headers=headers,
    )


async def stored_conversation(database: Database, conversation_id: uuid.UUID) -> Conversation:
    async with database.session_factory() as session:
        return (
            await session.execute(select(Conversation).where(Conversation.id == conversation_id))
        ).scalar_one()


# --------------------------------------------------------------- shared read


async def test_unread_is_shared_by_the_team_and_says_who_saw_it(desk: Inbox) -> None:
    """UR-T2."""
    shop = desk.mine
    conversation = await writer(desk)
    for _ in range(3):
        newest, _ = await message(desk.database, conversation, body="are you there?")
    before = await detail(desk, conversation, shop.owner.headers)
    assert before["unread_count"] == 3
    assert before["read"] is None

    answer = await mark(desk, conversation, newest.id, shop.member.headers)
    assert answer.status_code == 200, answer.text
    assert answer.json()["unread_count"] == 0
    assert answer.json()["read"]["by"]["user_id"] == str(shop.member.user_id)

    for person in (shop.owner, shop.admin, shop.member):
        seen = await detail(desk, conversation, person.headers)
        assert seen["unread_count"] == 0
        assert seen["read"]["message_id"] == str(newest.id)
        assert seen["read"]["by"]["user_id"] == str(shop.member.user_id)
    listed = await desk.client.get(LIST, headers=shop.owner.headers)
    row = next(item for item in listed.json()["items"] if item["id"] == str(conversation.id))
    assert row["unread_count"] == 0


async def test_only_the_customers_messages_are_ever_unread(desk: Inbox) -> None:
    """UR-T3: an AI or a colleague's reply never counts, and the AI never marks read."""
    conversation = await writer(desk)
    await message(desk.database, conversation, body="hello")
    await outbound(desk.database, conversation, origin=MessageOrigin.AGENT)
    await outbound(
        desk.database,
        conversation,
        origin=MessageOrigin.HUMAN,
        sent_by_id=desk.mine.owner.user_id,
    )
    body = await detail(desk, conversation, desk.mine.owner.headers)
    assert body["unread_count"] == 1
    assert body["read"] is None
    assert body["last_message"]["direction"] == "outbound"


async def test_nothing_read_counts_every_customer_message_and_the_count_is_capped(
    desk: Inbox,
) -> None:
    """UR-T4."""
    conversation = await writer(desk)
    async with desk.database.session_factory() as session:
        for index in range(101):
            session.add(
                Message(
                    tenant_id=conversation.tenant_id,
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND,
                    kind=MessageKind.TEXT,
                    status=MessageStatus.RECEIVED,
                    origin=MessageOrigin.CUSTOMER,
                    body=f"message {index}",
                )
            )
        await session.commit()
    assert (await detail(desk, conversation, desk.mine.owner.headers))["unread_count"] == 100


async def test_the_read_position_moves_forward_only(desk: Inbox) -> None:
    """UR-T5: an older or equal message changes nothing, not even who and when."""
    shop = desk.mine
    conversation = await writer(desk)
    first, _ = await message(desk.database, conversation, body="one")
    second, _ = await message(desk.database, conversation, body="two")
    assert (await mark(desk, conversation, second.id, shop.owner.headers)).status_code == 200
    stamped = (await detail(desk, conversation, shop.owner.headers))["read"]

    for older in (first.id, second.id):
        again = await mark(desk, conversation, older, shop.member.headers)
        assert again.status_code == 200
        assert again.json()["read"] == stamped
    assert (await detail(desk, conversation, shop.owner.headers))["read"] == stamped


async def test_a_message_of_another_conversation_or_workspace_is_not_found(desk: Inbox) -> None:
    """UR-T5, UR-T1: no oracle."""
    conversation = await writer(desk)
    elsewhere = await writer(desk)
    theirs = await writer(desk, shop=desk.theirs)
    await message(desk.database, conversation, body="mine")
    foreign, _ = await message(desk.database, elsewhere, body="other")
    alien, _ = await message(desk.database, theirs, body="theirs")
    for message_id in (foreign.id, alien.id, uuid.uuid4()):
        refused = await mark(desk, conversation, message_id, desk.mine.owner.headers)
        assert refused.status_code == 404
        assert refused.json()["error"]["code"] == "not_found"
    assert (await stored_conversation(desk.database, conversation.id)).last_read_at is None


async def test_read_state_never_crosses_workspaces(desk: Inbox) -> None:
    """UR-T1."""
    theirs = await writer(desk, shop=desk.theirs)
    newest, _ = await message(desk.database, theirs, body="theirs")
    for headers in (desk.mine.owner.headers, desk.mine.member.headers):
        refused = await mark(desk, theirs, newest.id, headers)
        assert refused.status_code == 404
        unread = await desk.client.post(f"{LIST}/{theirs.id}/unread", headers=headers)
        assert unread.status_code == 404
    mine = await desk.client.get(LIST, params={"unread": "true"}, headers=desk.mine.owner.headers)
    assert str(theirs.id) not in {item["id"] for item in mine.json()["items"]}
    summary = (await desk.client.get(SUMMARY, headers=desk.mine.owner.headers)).json()
    other = (await desk.client.get(SUMMARY, headers=desk.theirs.owner.headers)).json()
    assert other["unread_messages"] >= 1
    assert summary["unread_messages"] == 0


async def test_every_role_may_mark_read_and_a_revoked_member_may_not(desk: Inbox) -> None:
    """UR-T11."""
    shop = desk.mine
    conversation = await writer(desk)
    newest, _ = await message(desk.database, conversation, body="hi")
    for person in (shop.member, shop.admin, shop.owner):
        assert (await mark(desk, conversation, newest.id, person.headers)).status_code == 200
    async with desk.database.session_factory() as session:
        membership = (
            await session.execute(
                select(Membership).where(
                    Membership.user_id == shop.member.user_id,
                    Membership.tenant_id == shop.tenant_id,
                )
            )
        ).scalar_one()
        membership.status = MembershipStatus.REVOKED
        await session.commit()
    refused = await mark(desk, conversation, newest.id, shop.member.headers)
    assert refused.status_code in (401, 403, 404)
    assert refused.json()["error"]["code"] in ("membership_revoked", "not_found", "unauthenticated")


# ------------------------------------------------------------ the preview


def test_an_excerpt_is_cut_at_a_whole_character_with_whitespace_collapsed() -> None:
    """UR-T7."""
    assert excerpt(None) is None
    assert excerpt("   \n\t ") is None
    assert excerpt("one\n\n  two\tthree") == "one two three"
    emoji = "🙂" * (EXCERPT_LENGTH + 5)
    assert excerpt(emoji) == "🙂" * EXCERPT_LENGTH
    arabic = "بَ" * EXCERPT_LENGTH  # a letter and its mark, again and again
    cut = excerpt(arabic)
    assert cut is not None and len(cut) <= EXCERPT_LENGTH
    assert not cut.endswith("ب"), "a letter lost its mark"
    long_text = "word " * 60
    assert len(excerpt(long_text) or "") <= EXCERPT_LENGTH


async def test_the_preview_of_each_kind_of_message(desk: Inbox) -> None:
    """UR-T7 through the list."""
    headers = desk.mine.owner.headers

    async def newest(conversation: Conversation) -> Any:
        return (await detail(desk, conversation, headers))["last_message"]

    text = await writer(desk)
    await message(desk.database, text, body="Hello\n  there " + "x" * 200)
    preview = await newest(text)
    assert preview["kind"] == "text" and preview["direction"] == "inbound"
    assert preview["excerpt"].startswith("Hello there x") and len(preview["excerpt"]) == 120

    captioned = await writer(desk)
    await message(desk.database, captioned, kind=MessageKind.IMAGE, body="the kitchen")
    assert (await newest(captioned))["excerpt"] == "the kitchen"

    bare = await writer(desk)
    await message(desk.database, bare, kind=MessageKind.AUDIO, body=None)
    preview = await newest(bare)
    assert preview["kind"] == "audio" and preview["excerpt"] is None

    empty = await writer(desk)
    assert await newest(empty) is None


async def test_an_excerpt_and_a_mark_are_never_written_to_a_log(
    desk: Inbox, caplog: pytest.LogCaptureFixture
) -> None:
    """UR-T8."""
    secret = "QX-ZEBRA private words"
    conversation = await writer(desk)
    newest, _ = await message(desk.database, conversation, body=secret)
    caplog.set_level(logging.DEBUG)
    await desk.client.get(LIST, headers=desk.mine.owner.headers)
    await mark(desk, conversation, newest.id, desk.mine.owner.headers)
    await desk.client.post(f"{LIST}/{conversation.id}/unread", headers=desk.mine.owner.headers)
    assert caplog.records
    for record in caplog.records:
        assert "QX-ZEBRA" not in repr(record.__dict__) + record.getMessage(), record.name


async def test_a_page_reads_its_read_state_in_the_same_statements_for_1_and_50(
    desk: Inbox,
) -> None:
    """UR-T6, with the unread filter too."""
    shop = desk.theirs
    for index in range(50):
        conversation = await writer(desk, shop=shop, name=f"Customer {index}")
        await message(desk.database, conversation, body=f"hello {index}")
    for params in ({}, {"unread": "true"}):
        counts = []
        for limit in (1, 50):
            with statements(desk.database) as seen:
                response = await desk.client.get(
                    LIST, params={"limit": limit, **params}, headers=shop.owner.headers
                )
            assert response.status_code == 200
            assert len(response.json()["items"]) == limit
            counts.append(len(seen))
        assert counts[0] == counts[1], (params, counts)


# ------------------------------------------------------- the unread filter


async def test_unread_only_lists_conversations_with_an_unseen_customer_message(
    desk: Inbox,
) -> None:
    """UR-T15."""
    headers = desk.mine.owner.headers
    waiting = await writer(desk, name="Waiting")
    await message(desk.database, waiting, body="hello?")
    seen = await writer(desk, name="Seen")
    newest, _ = await message(desk.database, seen, body="thanks")
    await mark(desk, seen, newest.id, headers)
    quiet = await writer(desk, name="Quiet")
    await outbound(desk.database, quiet)

    everything = await desk.client.get(LIST, headers=headers)
    absent = await desk.client.get(LIST, params={"unread": "false"}, headers=headers)
    assert [i["id"] for i in everything.json()["items"]] == [
        i["id"] for i in absent.json()["items"]
    ]
    only = await desk.client.get(LIST, params={"unread": "true"}, headers=headers)
    assert [item["id"] for item in only.json()["items"]] == [str(waiting.id)]
    for view in ({"status": "all"}, {"mode": "ai"}, {"q": "Wait"}, {"assigned_to": "none"}):
        narrowed = await desk.client.get(LIST, params={"unread": "true", **view}, headers=headers)
        assert narrowed.status_code == 200, view
        assert [item["id"] for item in narrowed.json()["items"]] == [str(waiting.id)], view


async def test_the_unread_filter_binds_its_cursor(desk: Inbox) -> None:
    """UR-T16."""
    headers = desk.mine.owner.headers
    for index in range(5):
        conversation = await writer(
            desk, name=f"Pager {index}", last_message_at=recent() - timedelta(minutes=index)
        )
        await message(desk.database, conversation, body="hi")
    first = await desk.client.get(LIST, params={"unread": "true", "limit": 2}, headers=headers)
    cursor = first.json()["next_cursor"]
    assert cursor is not None
    crossed = await desk.client.get(LIST, params={"cursor": cursor, "limit": 2}, headers=headers)
    assert crossed.status_code == 422
    assert crossed.json()["error"]["code"] == "cursor_view_mismatch"
    plain = (await desk.client.get(LIST, params={"limit": 2}, headers=headers)).json()
    if plain["next_cursor"] is not None:
        reverse = await desk.client.get(
            LIST, params={"cursor": plain["next_cursor"], "unread": "true"}, headers=headers
        )
        assert reverse.json()["error"]["code"] == "cursor_view_mismatch"
    caught_up = await desk.client.get(
        LIST,
        params={"unread": "true", "updated_since": datetime.now(UTC).isoformat()},
        headers=headers,
    )
    assert caught_up.json()["error"]["code"] == "inbox_view_with_updated_since"

    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"unread": "true", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (await desk.client.get(LIST, params=params, headers=headers)).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 5


# ------------------------------------------------------------- the summary


async def test_the_summary_counts_each_unseen_customer_message_once_not_closed(
    desk: Inbox,
) -> None:
    """UR-T17."""
    from app.db.models.conversation import ConversationStatus

    headers = desk.mine.member.headers
    one = await writer(desk)
    await message(desk.database, one, body="a")
    await message(desk.database, one, body="b")
    two = await writer(desk)
    await message(desk.database, two, body="c")
    closed = await writer(desk, status=ConversationStatus.CLOSED)
    await message(desk.database, closed, body="d")
    answer = await desk.client.get(SUMMARY, headers=headers)
    assert answer.status_code == 200
    assert answer.json() == {"unread_messages": 3, "unread_conversations": 2}
    owner = await desk.client.get(SUMMARY, headers=desk.mine.owner.headers)
    assert owner.json() == answer.json()

    with statements(desk.database) as seen:
        await desk.client.get(SUMMARY, headers=headers)
    assert len([s for s in seen if "messages" in s]) == 1


# ---------------------------------------------------------- mark as unread


async def test_mark_as_unread_makes_the_newest_customer_message_count_again(
    desk: Inbox,
) -> None:
    """UR-T18."""
    shop = desk.mine
    conversation = await writer(desk)
    await message(desk.database, conversation, body="one")
    newest, _ = await message(desk.database, conversation, body="two")
    await outbound(desk.database, conversation)
    await mark(desk, conversation, newest.id, shop.owner.headers)

    answer = await desk.client.post(f"{LIST}/{conversation.id}/unread", headers=shop.member.headers)
    assert answer.status_code == 200, answer.text
    assert answer.json()["unread_count"] == 1
    assert answer.json()["read"]["by"]["user_id"] == str(shop.member.user_id)
    assert (await detail(desk, conversation, shop.owner.headers))["unread_count"] == 1

    forward = await mark(desk, conversation, newest.id, shop.owner.headers)
    assert forward.json()["unread_count"] == 0

    silent = await writer(desk)
    await outbound(desk.database, silent)
    nothing = await desk.client.post(f"{LIST}/{silent.id}/unread", headers=shop.owner.headers)
    assert nothing.status_code == 200
    assert nothing.json() == {"unread_count": 0, "read": None}


# ------------------------------------------------------------- the stream


async def test_a_move_emits_conversation_updated_with_read_and_a_no_op_emits_nothing(
    desk: Inbox,
) -> None:
    """UR-T14."""
    shop = desk.mine
    conversation = await writer(desk)
    newest, _ = await message(desk.database, conversation, body="hi")
    async with desk.database.session_factory() as session:
        actor = await session.get(User, shop.owner.user_id)
        assert actor is not None
        reads = ReadStateService(session=session, tenant_id=shop.tenant_id)
        await reads.mark_read(
            conversation_id=conversation.id, up_to_message_id=newest.id, actor=actor
        )
        staged = list(session.info.get(events._PENDING, []))
        session.info.get(events._PENDING, []).clear()
        await reads.mark_read(
            conversation_id=conversation.id, up_to_message_id=newest.id, actor=actor
        )
        again = list(session.info.get(events._PENDING, []))
        await session.rollback()
    moved = [hint for hint in staged if hint.type == "conversation.updated"]
    assert moved and moved[0].data["fields"] == ["read"]
    assert not [hint for hint in again if hint.type == "conversation.updated"]


# ----------------------------------------------------------- the first reply


async def send(
    desk: Inbox, conversation: Conversation, headers: dict[str, str], **extra: Any
) -> Any:
    key = extra.pop("key", str(uuid.uuid4()))
    return await desk.client.post(
        f"{LIST}/{conversation.id}/messages",
        json={"body": extra.pop("body", "On our way.")},
        headers={**headers, "Idempotency-Key": key},
    )


async def assignments(database: Database, conversation_id: uuid.UUID) -> list[AuditLog]:
    async with database.session_factory() as session:
        return list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.target_id == conversation_id,
                        AuditLog.action.in_(
                            [AuditAction.CONVERSATION_ASSIGNED, AuditAction.CONVERSATION_REASSIGNED]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_the_first_members_reply_takes_an_unassigned_conversation(desk: Inbox) -> None:
    """UR-T12, UR-T13."""
    shop = desk.mine
    conversation = await writer(desk)
    await message(desk.database, conversation, body="one")
    newest, _ = await message(desk.database, conversation, body="two")

    sent = await send(desk, conversation, shop.member.headers)
    assert sent.status_code == 201, sent.text
    body = await detail(desk, conversation, shop.owner.headers)
    assert body["assigned_to_id"] == str(shop.member.user_id)
    assert body["unread_count"] == 0
    assert body["read"]["message_id"] == str(newest.id)
    assert body["read"]["by"]["user_id"] == str(shop.member.user_id)
    assert len(await assignments(desk.database, conversation.id)) == 1


async def test_a_reply_never_takes_a_conversation_somebody_owns(desk: Inbox) -> None:
    """UR-T12."""
    shop = desk.mine
    conversation = await writer(desk, assigned_to_id=shop.owner.user_id)
    await message(desk.database, conversation, body="hi")
    assert (await send(desk, conversation, shop.member.headers)).status_code == 201
    body = await detail(desk, conversation, shop.owner.headers)
    assert body["assigned_to_id"] == str(shop.owner.user_id)
    assert body["unread_count"] == 0
    assert not await assignments(desk.database, conversation.id)


async def test_a_replayed_send_neither_assigns_nor_marks_again(desk: Inbox) -> None:
    """UR-T12: an idempotent replay is the same send."""
    shop = desk.mine
    conversation = await writer(desk)
    newest, _ = await message(desk.database, conversation, body="hi")
    key = str(uuid.uuid4())
    assert (await send(desk, conversation, shop.member.headers, key=key)).status_code == 201
    stamped = (await detail(desk, conversation, shop.owner.headers))["read"]
    # Somebody unassigns it in between; the replay must not take it again.
    await desk.client.post(
        f"{LIST}/{conversation.id}/assignment",
        json={"assigned_to_id": None, "expected_assigned_to_id": str(shop.member.user_id)},
        headers=shop.owner.headers,
    )
    replay = await send(desk, conversation, shop.member.headers, key=key)
    assert replay.status_code == 201
    body = await detail(desk, conversation, shop.owner.headers)
    assert body["assigned_to_id"] is None
    assert body["read"] == stamped


async def test_the_ais_reply_neither_assigns_nor_marks_read(desk: Inbox) -> None:
    """UR-T3, UR-T12 at the service: an agent's message is not a member's reply."""
    shop = desk.mine
    conversation = await writer(desk)
    await message(desk.database, conversation, body="hi")
    reply = await outbound(desk.database, conversation, origin=MessageOrigin.AGENT)
    async with desk.database.session_factory() as session:
        actor = await session.get(User, shop.owner.user_id)
        assert actor is not None
        from app.services.inbox_service import InboxService

        await ReadStateService(session=session, tenant_id=shop.tenant_id).after_member_reply(
            message=reply,
            actor=actor,
            inbox=InboxService(session=session, tenant_id=shop.tenant_id),
            replayed=False,
        )
        await session.commit()
    stored = await stored_conversation(desk.database, conversation.id)
    assert stored.assigned_to_id is None and stored.last_read_at is None


async def test_two_members_replying_at_once_leave_one_assignee(desk: Inbox) -> None:
    """UR-T12: the first commit wins, and both messages are written."""
    import asyncio

    shop = desk.mine
    conversation = await writer(desk)
    await message(desk.database, conversation, body="hi")
    answers = await asyncio.gather(
        send(desk, conversation, shop.member.headers, body="first"),
        send(desk, conversation, shop.admin.headers, body="second"),
    )
    assert [answer.status_code for answer in answers] == [201, 201]
    body = await detail(desk, conversation, shop.owner.headers)
    assert body["assigned_to_id"] in (str(shop.member.user_id), str(shop.admin.user_id))
    assert len(await assignments(desk.database, conversation.id)) == 1

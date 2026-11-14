"""The inbox contract FE-4 reads (FEB-03, FEB-04, FEB-05; ADR-197).

Against PostgreSQL, through the real routes:

- a message says which files it carries - the id the download route takes and
  what a screen needs to draw it - and nothing about where they are stored;
- a conversation says who is on the other side - the channel's name for them,
  their number when the channel gave one, their open lead - and never a
  business-scoped id as if it were a number;
- the inbox's views (status, assigned_to, mode, q) narrow one ordering, page
  without gap or repeat, and a cursor of one view is refused by another;
- each workspace sees its own rows only, and nothing is read per row.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from app.api.dependencies import get_media_storage
from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.core.security import create_access_token
from app.db.models import Membership, MembershipStatus, User
from app.db.models.channel import (
    Channel,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
    IdentitySource,
)
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.enums import TenantRole
from app.db.models.lead import Lead, LeadStatus
from app.db.models.media import MediaStatus, MediaStorageState, MessageMedia
from app.db.models.sentiment import ConversationPriority
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.main import create_app
from app.schemas.conversation import MediaAvailability, TranscriptStatus, media_availability
from tests.conftest import FakeDependency
from tests.integration.campaign_harness import FakeMeta, erase, seed, worker

pytestmark = pytest.mark.integration

LIST = "/api/v1/conversations"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"a photograph of a kitchen" * 8
# Customer-controlled text: markup, a right-to-left mark, Arabic and an emoji.
UNTRUSTED_NAME = '<b onmouseover="x">نور</b>‏ 🙂'
# Synthetic numbers and ids, nobody's.
PHONE_A = "201055501234"
BSUID = "EG.0synthetic0bsuid"
FORBIDDEN_KEYS = (
    "storage_key",
    "wa_media_id",
    "locator",
    "content_hash",
    "last_error",
    "purge_started_at",
    "url",
)


class MemoryStorage:
    """The media store, in memory: the bytes the download route serves."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes:
        return self.objects[key]


@dataclass
class Person:
    user_id: uuid.UUID
    headers: dict[str, str]


@dataclass
class Shop:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    second_account_id: uuid.UUID
    owner: Person
    admin: Person
    member: Person
    named: dict[str, Conversation] = field(default_factory=dict)


@dataclass
class Inbox:
    client: AsyncClient
    database: Database
    storage: MemoryStorage
    mine: Shop
    theirs: Shop


async def _person(
    database: Database, settings: Settings, tenant_id: uuid.UUID, role: TenantRole
) -> Person:
    async with database.session_factory() as session:
        user = User(
            email=f"{role.value}-{uuid.uuid4().hex[:8]}@inbox.test",
            hashed_password="x",
            is_active=True,
            email_verified_at=NOW,
        )
        session.add(user)
        await session.flush()
        session.add(
            Membership(
                tenant_id=tenant_id, user_id=user.id, role=role, status=MembershipStatus.ACTIVE
            )
        )
        await session.commit()
        token, _ = create_access_token(
            settings=settings,
            subject=user.id,
            tenant_id=tenant_id,
            token_version=user.token_version,
        )
        return Person(user.id, {"Authorization": f"Bearer {token}"})


async def _second_account(database: Database, tenant_id: uuid.UUID) -> uuid.UUID:
    async with database.session_factory() as session:
        suffix = uuid.uuid4().hex[:10]
        account = WhatsAppAccount(
            tenant_id=tenant_id,
            phone_number_id=f"pn2-{suffix}",
            waba_id=f"waba2-{suffix}",
            display_phone_number="+201000000002",
        )
        session.add(account)
        await session.commit()
        return account.id


async def customer(
    database: Database,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    name: str | None,
    phone: str | None = None,
    bsuid: str | None = None,
    status: ConversationStatus = ConversationStatus.OPEN,
    mode: ConversationMode = ConversationMode.AI,
    assigned_to_id: uuid.UUID | None = None,
    priority: ConversationPriority = ConversationPriority.NORMAL,
    last_message_at: datetime | None = NOW,
    lead: bool = False,
) -> Conversation:
    """A customer and their conversation, with explicit times (no wall clock)."""
    async with database.session_factory() as session:
        contact = Contact(tenant_id=tenant_id, wa_id=phone, display_name=name)
        session.add(contact)
        await session.flush()
        participant: uuid.UUID | None = None
        if bsuid is not None:
            identity = ContactIdentity(
                tenant_id=tenant_id,
                contact_id=contact.id,
                channel=Channel.WHATSAPP,
                kind=IdentityKind.BSUID,
                scope=IdentityScope.PROVIDER_ACCOUNT,
                scope_ref="waba-synthetic",
                value=bsuid,
                source=IdentitySource.PROVIDER,
            )
            session.add(identity)
            await session.flush()
            participant = identity.id
        conversation = Conversation(
            tenant_id=tenant_id,
            contact_id=contact.id,
            account_id=account_id,
            status=status,
            mode=mode,
            assigned_to_id=assigned_to_id,
            priority=priority,
            last_message_at=last_message_at,
            last_inbound_at=last_message_at,
        )
        if participant is not None:
            conversation.participant_identity_id = participant
        session.add(conversation)
        if lead:
            session.add(
                Lead(tenant_id=tenant_id, contact_id=contact.id, name=name, status=LeadStatus.NEW)
            )
        await session.commit()
        await session.refresh(conversation)
        return conversation


async def message(
    database: Database,
    conversation: Conversation,
    *,
    kind: MessageKind = MessageKind.TEXT,
    body: str | None = "hello",
    files: list[dict[str, Any]] | None = None,
) -> tuple[Message, list[MessageMedia]]:
    async with database.session_factory() as session:
        row = Message(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            kind=kind,
            status=MessageStatus.RECEIVED,
            origin=MessageOrigin.CUSTOMER,
            body=body,
        )
        session.add(row)
        await session.flush()
        media: list[MessageMedia] = []
        for position, spec in enumerate(files or []):
            item = MessageMedia(
                tenant_id=conversation.tenant_id,
                message_id=row.id,
                conversation_id=conversation.id,
                position=position,
                **spec,
            )
            session.add(item)
            media.append(item)
        await session.commit()
        for item in media:
            await session.refresh(item)
        await session.refresh(row)
        return row, media


def stored(key: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": MediaStatus.READY,
        "storage_state": MediaStorageState.STORED,
        "storage_key": key,
        "mime_type": "image/png",
        "byte_size": len(PNG),
        "content_hash": "0" * 64,
        "wa_media_id": f"meta-{key}",
        **extra,
    }


@pytest_asyncio.fixture
async def inbox(
    prepared_database: str, settings: Settings, fake_redis: FakeDependency
) -> AsyncIterator[Inbox]:
    _, database = worker(prepared_database, FakeMeta(), name="inbox-contract")
    application: FastAPI = create_app(settings)
    application.state.database = database
    application.state.redis = fake_redis
    application.dependency_overrides[get_settings_from_state] = lambda: settings
    storage = MemoryStorage()
    application.dependency_overrides[get_media_storage] = lambda: storage
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
        await erase(database)
        await database.dispose()


async def twelve(inbox: Inbox) -> dict[str, Conversation]:
    """The twelve conversations every view is judged against (IV-T1, IV-T2)."""
    shop, db = inbox.mine, inbox.database
    t, a = shop.tenant_id, shop.account_id

    def at(minutes: int) -> datetime:
        return NOW - timedelta(minutes=minutes)

    rows = {
        "nour": await customer(
            db,
            t,
            a,
            name="Nour Hassan",
            phone=PHONE_A,
            assigned_to_id=shop.owner.user_id,
            last_message_at=at(1),
            lead=True,
        ),
        "unassigned": await customer(
            db, t, a, name="Omar Said", phone="201055500002", last_message_at=at(2)
        ),
        "pending": await customer(
            db,
            t,
            a,
            name="Pending Person",
            phone="201055500003",
            status=ConversationStatus.PENDING,
            assigned_to_id=shop.member.user_id,
            last_message_at=at(3),
        ),
        "closed": await customer(
            db,
            t,
            a,
            name="Closed Customer",
            phone="201055500004",
            status=ConversationStatus.CLOSED,
            last_message_at=at(4),
        ),
        "human": await customer(
            db,
            t,
            a,
            name="Human Mode",
            phone="201055500005",
            mode=ConversationMode.HUMAN,
            assigned_to_id=shop.admin.user_id,
            last_message_at=at(5),
        ),
        "second": await customer(
            db,
            t,
            shop.second_account_id,
            name="Second Number",
            phone="201055500006",
            last_message_at=at(6),
        ),
        "bsuid": await customer(db, t, a, name=None, bsuid=BSUID, last_message_at=at(7)),
        "untrusted": await customer(
            db, t, a, name=UNTRUSTED_NAME, phone="201055500008", last_message_at=at(8)
        ),
        "urgent": await customer(
            db,
            t,
            a,
            name="Urgent Case",
            phone="201055500009",
            priority=ConversationPriority.URGENT,
            last_message_at=at(9),
        ),
        "tie_a": await customer(
            db, t, a, name="Tie One", phone="201055500010", last_message_at=at(10)
        ),
        "tie_b": await customer(
            db, t, a, name="Tie Two", phone="201055500011", last_message_at=at(10)
        ),
        "silent": await customer(
            db, t, a, name="Never Wrote", phone="201055500012", last_message_at=None
        ),
    }
    # The other workspace: the same name and the same number - another person.
    theirs = inbox.theirs
    await customer(
        db,
        theirs.tenant_id,
        theirs.account_id,
        name="Nour Hassan",
        phone=PHONE_A,
        last_message_at=at(0),
    )
    return rows


def _default_order(rows: dict[str, Conversation], keep: Any = None) -> list[str]:
    """`ORDER BY last_message_at DESC NULLS LAST, id DESC` over the rows `keep` admits."""
    chosen = [row for row in rows.values() if keep is None or keep(row)]
    floor = datetime.min.replace(tzinfo=UTC)
    chosen.sort(key=lambda row: (row.last_message_at or floor, row.id), reverse=True)
    return [str(row.id) for row in chosen]


async def _ids(client: AsyncClient, headers: dict[str, str], **params: Any) -> list[str]:
    response = await client.get(LIST, params={"limit": 100, **params}, headers=headers)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def _walk(
    client: AsyncClient, headers: dict[str, str], limit: int, **params: Any
) -> list[str]:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(50):
        query = {"limit": limit, **params, **({"cursor": cursor} if cursor else {})}
        response = await client.get(LIST, params=query, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("the walk did not end")


@contextmanager
def statements(database: Database) -> Iterator[list[str]]:
    """Every SQL statement the application sends while the block runs."""
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


# ---------------------------------------------------------------- FEB-03


async def _media_thread(inbox: Inbox) -> tuple[Conversation, dict[str, MessageMedia]]:
    shop, db = inbox.mine, inbox.database
    conversation = await customer(
        db, shop.tenant_id, shop.account_id, name="Media", phone="201055509999"
    )
    inbox.storage.objects["k-photo"] = PNG
    await message(db, conversation, body="just text")
    _, (photo,) = await message(
        db, conversation, kind=MessageKind.IMAGE, body="the kitchen", files=[stored("k-photo")]
    )
    _, (pending,) = await message(
        db,
        conversation,
        kind=MessageKind.DOCUMENT,
        body=None,
        files=[
            {
                "status": MediaStatus.PENDING,
                "mime_type": "application/pdf",
                "filename": "quote.pdf",
                "wa_media_id": "meta-pending",
            }
        ],
    )
    _, (voice,) = await message(
        db,
        conversation,
        kind=MessageKind.AUDIO,
        body=None,
        files=[
            stored(
                "k-voice",
                mime_type="audio/ogg",
                is_voice=True,
                transcript="I want a kitchen by May.",
            )
        ],
    )
    _, (failed,) = await message(
        db,
        conversation,
        kind=MessageKind.VIDEO,
        body=None,
        files=[
            {
                "status": MediaStatus.FAILED,
                "mime_type": "video/mp4",
                "wa_media_id": "meta-f",
                "last_error": "provider said no",
            }
        ],
    )
    _, (purged,) = await message(
        db,
        conversation,
        kind=MessageKind.IMAGE,
        body=None,
        files=[
            {
                "status": MediaStatus.READY,
                "storage_state": MediaStorageState.PURGED,
                "mime_type": "image/png",
                "purge_started_at": NOW,
            }
        ],
    )
    return conversation, {
        "photo": photo,
        "pending": pending,
        "voice": voice,
        "failed": failed,
        "purged": purged,
    }


def _by_id(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for message in messages for item in message["media"]}


async def test_a_media_message_names_its_file_and_the_download_route_serves_that_id(
    inbox: Inbox,
) -> None:
    conversation, files = await _media_thread(inbox)
    headers = inbox.mine.member.headers
    page = await inbox.client.get(f"{LIST}/{conversation.id}/messages", headers=headers)
    assert page.status_code == 200, page.text
    items = _by_id(page.json()["items"])

    photo = items[str(files["photo"].id)]
    assert photo == {
        "id": str(files["photo"].id),
        "kind": "image",
        "mime_type": "image/png",
        "filename": None,
        "byte_size": len(PNG),
        "is_voice": False,
        "transcript_status": "none",
        "transcript": None,
        "status": "available",
        "position": 0,
        "caption": "the kitchen",
    }
    download = await inbox.client.get(
        f"{LIST}/{conversation.id}/media/{photo['id']}", headers=headers
    )
    assert download.status_code == 200
    assert download.content == PNG
    assert download.headers["Cache-Control"] == "private, no-store"
    assert download.headers["X-Content-Type-Options"] == "nosniff"

    assert items[str(files["pending"].id)]["status"] == "pending"
    assert items[str(files["pending"].id)]["filename"] == "quote.pdf"
    assert items[str(files["pending"].id)]["transcript_status"] == "pending"
    voice = items[str(files["voice"].id)]
    assert (voice["kind"], voice["is_voice"], voice["transcript_status"], voice["transcript"]) == (
        "audio",
        True,
        "ready",
        "I want a kitchen by May.",
    )
    assert items[str(files["failed"].id)]["status"] == "failed"
    assert items[str(files["failed"].id)]["transcript_status"] == "failed"
    assert items[str(files["purged"].id)]["status"] == "unavailable"
    texts = [m for m in page.json()["items"] if m["kind"] == "text"]
    assert texts and all(m["media"] == [] for m in texts)


async def test_no_message_says_where_or_how_a_file_is_stored(inbox: Inbox) -> None:
    conversation, files = await _media_thread(inbox)
    response = await inbox.client.get(
        f"{LIST}/{conversation.id}/messages", headers=inbox.mine.owner.headers
    )
    body = response.text
    for item in _by_id(response.json()["items"]).values():
        assert not set(item) & set(FORBIDDEN_KEYS)
    for value in (
        "k-photo",
        "k-voice",
        "meta-k-photo",
        "meta-pending",
        "meta-f",
        "provider said no",
        "0" * 64,
    ):
        assert value not in body


def test_every_storage_state_and_status_has_a_public_word() -> None:
    """A new state added to the model fails here until it is given one (MD-T2)."""
    for state in MediaStorageState:
        for status in MediaStatus:
            item = MessageMedia(storage_state=state, status=status)
            word = media_availability(item)
            assert word in set(MediaAvailability)
            if state is MediaStorageState.STORED:
                assert word is MediaAvailability.AVAILABLE
            if state in {
                MediaStorageState.PURGED,
                MediaStorageState.PURGING,
                MediaStorageState.MISMATCHED,
            }:
                assert word is MediaAvailability.UNAVAILABLE
    assert {member.value for member in TranscriptStatus} == {"none", "pending", "ready", "failed"}


async def test_another_workspaces_file_never_appears_and_is_still_not_found(inbox: Inbox) -> None:
    mine, theirs, db = inbox.mine, inbox.theirs, inbox.database
    own = await customer(db, mine.tenant_id, mine.account_id, name="Mine", phone="201055500100")
    await message(db, own, body="mine")
    other = await customer(
        db, theirs.tenant_id, theirs.account_id, name="Theirs", phone="201055500100"
    )
    inbox.storage.objects["k-theirs"] = PNG
    _, (their_file,) = await message(db, other, kind=MessageKind.IMAGE, files=[stored("k-theirs")])
    page = await inbox.client.get(f"{LIST}/{own.id}/messages", headers=mine.owner.headers)
    assert str(their_file.id) not in page.text
    refused = await inbox.client.get(
        f"{LIST}/{own.id}/media/{their_file.id}", headers=mine.owner.headers
    )
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "not_found"


async def test_a_page_of_messages_reads_its_files_in_one_query(inbox: Inbox) -> None:
    shop, db = inbox.mine, inbox.database
    conversation = await customer(
        db, shop.tenant_id, shop.account_id, name="Many", phone="201055500200"
    )
    for index in range(50):
        inbox.storage.objects[f"k-{index}"] = PNG
        await message(db, conversation, kind=MessageKind.IMAGE, files=[stored(f"k-{index}")])
    url = f"{LIST}/{conversation.id}/messages"
    counts = []
    for limit in (1, 50):
        with statements(db) as seen:
            response = await inbox.client.get(
                url, params={"limit": limit}, headers=shop.owner.headers
            )
        assert response.status_code == 200
        assert len(response.json()["items"]) == limit
        assert all(len(item["media"]) == 1 for item in response.json()["items"])
        counts.append(len(seen))
    assert counts[0] == counts[1], counts


# ---------------------------------------------------------------- FEB-04


async def test_a_conversation_names_who_is_on_the_other_side(inbox: Inbox) -> None:
    rows = await twelve(inbox)
    nour = rows["nour"]
    listed = await inbox.client.get(LIST, headers=inbox.mine.member.headers)
    item = next(row for row in listed.json()["items"] if row["id"] == str(nour.id))
    detail = (await inbox.client.get(f"{LIST}/{nour.id}", headers=inbox.mine.member.headers)).json()
    for participant in (item["participant"], detail["participant"]):
        assert participant["channel"] == "whatsapp"
        assert participant["kind"] == "phone"
        assert participant["contact_id"] == str(nour.contact_id)
        assert participant["display_name"] == "Nour Hassan"
        assert participant["handle"] == f"+{PHONE_A}"
        assert participant["lead_id"] is not None


async def test_a_customer_known_only_by_a_business_scoped_id_has_no_handle(inbox: Inbox) -> None:
    rows = await twelve(inbox)
    response = await inbox.client.get(
        f"{LIST}/{rows['bsuid'].id}", headers=inbox.mine.owner.headers
    )
    assert response.status_code == 200
    participant = response.json()["participant"]
    assert (participant["kind"], participant["handle"], participant["display_name"]) == (
        "bsuid",
        None,
        None,
    )
    assert BSUID not in response.text


async def test_a_name_is_returned_as_the_customer_typed_it(inbox: Inbox) -> None:
    rows = await twelve(inbox)
    response = await inbox.client.get(
        f"{LIST}/{rows['untrusted'].id}", headers=inbox.mine.member.headers
    )
    assert response.json()["participant"]["display_name"] == UNTRUSTED_NAME


async def test_every_role_reads_the_same_participant_the_contact_picker_already_shows(
    inbox: Inbox,
) -> None:
    rows = await twelve(inbox)
    seen = []
    for person in (inbox.mine.owner, inbox.mine.admin, inbox.mine.member):
        detail = await inbox.client.get(f"{LIST}/{rows['nour'].id}", headers=person.headers)
        picker = await inbox.client.get(
            "/api/v1/contacts", params={"q": "Nour"}, headers=person.headers
        )
        assert picker.status_code == 200
        assert PHONE_A in [item["wa_id"] for item in picker.json()["items"]]
        seen.append(detail.json()["participant"])
    assert seen[0] == seen[1] == seen[2]


async def test_every_route_answering_a_conversation_carries_its_participant(inbox: Inbox) -> None:
    rows = await twelve(inbox)
    target = rows["unassigned"]
    owner = inbox.mine.owner
    url = f"{LIST}/{target.id}"
    answers = [
        await inbox.client.post(
            f"{url}/priority", json={"priority": "high"}, headers=owner.headers
        ),
        await inbox.client.post(
            f"{url}/assignment",
            json={"assigned_to_id": str(owner.user_id), "expected_assigned_to_id": None},
            headers=owner.headers,
        ),
        await inbox.client.post(f"{url}/mode", json={"mode": "human"}, headers=owner.headers),
        await inbox.client.post(f"{url}/close", headers=owner.headers),
        await inbox.client.post(f"{url}/reopen", headers=owner.headers),
    ]
    for answer in answers:
        assert answer.status_code == 200, answer.text
        assert answer.json()["participant"]["display_name"] == "Omar Said"
        assert answer.json()["participant"]["handle"] == "+201055500002"


async def test_a_page_of_conversations_reads_its_participants_in_one_query(inbox: Inbox) -> None:
    shop, db = inbox.theirs, inbox.database
    for index in range(50):
        await customer(
            db,
            shop.tenant_id,
            shop.account_id,
            name=f"Customer {index}",
            phone=f"2010555{index:05d}",
            lead=index % 2 == 0,
            last_message_at=NOW - timedelta(minutes=index),
        )
    counts = []
    for limit in (1, 50):
        with statements(db) as seen:
            response = await inbox.client.get(
                LIST, params={"limit": limit}, headers=shop.owner.headers
            )
        assert response.status_code == 200
        assert len(response.json()["items"]) == limit
        counts.append(len(seen))
    assert counts[0] == counts[1], counts


async def test_another_workspaces_customer_is_never_a_participant(inbox: Inbox) -> None:
    await twelve(inbox)
    mine = await _ids(inbox.client, inbox.mine.owner.headers, status="all")
    theirs = await _ids(inbox.client, inbox.theirs.owner.headers, status="all")
    assert not set(mine) & set(theirs)
    assert len(theirs) == 1


# ---------------------------------------------------------------- FEB-05


async def test_the_default_view_is_unchanged(inbox: Inbox) -> None:
    rows = await twelve(inbox)
    expected = _default_order(rows, lambda row: row.status is not ConversationStatus.CLOSED)
    assert await _ids(inbox.client, inbox.mine.member.headers) == expected
    assert await _walk(inbox.client, inbox.mine.member.headers, 4) == expected


async def test_each_view_and_pairs_of_them(inbox: Inbox) -> None:
    rows = await twelve(inbox)
    shop = inbox.mine
    me = shop.member

    def ids(*names: str) -> set[str]:
        return {str(rows[name].id) for name in names}

    not_closed = set(rows) - {"closed"}
    cases: list[tuple[dict[str, Any], set[str]]] = [
        ({"status": "open"}, ids(*(not_closed - {"pending"}))),
        ({"status": "pending"}, ids("pending")),
        ({"status": "closed"}, ids("closed")),
        ({"status": "all"}, ids(*rows)),
        ({"assigned_to": "me"}, ids("pending")),
        ({"assigned_to": "none"}, ids(*(not_closed - {"nour", "pending", "human"}))),
        ({"assigned_to": str(shop.owner.user_id)}, ids("nour")),
        ({"mode": "human"}, ids("human")),
        ({"mode": "ai"}, ids(*(not_closed - {"human"}))),
        ({"connection_id": str(shop.second_account_id)}, ids("second")),
        ({"q": "nour"}, ids("nour")),
        ({"q": "NOUR has"}, ids("nour")),
        ({"q": f"+{PHONE_A[:2]} {PHONE_A[2:]}"}, ids("nour")),
        ({"q": "55500006"}, ids("second")),
        (
            {"status": "all", "assigned_to": "none"},
            ids(*(set(rows) - {"nour", "pending", "human"})),
        ),
        ({"status": "open", "mode": "human"}, ids("human")),
        ({"assigned_to": "none", "priority": "urgent"}, ids("urgent")),
        ({"status": "closed", "q": "Closed"}, ids("closed")),
    ]
    for params, expected in cases:
        got = await _ids(inbox.client, me.headers, **params)
        assert set(got) == expected, params
        assert got == [i for i in _default_order(rows) if i in expected], params


async def test_every_view_pages_without_gap_or_repeat(inbox: Inbox) -> None:
    rows = await twelve(inbox)
    headers = inbox.mine.owner.headers
    for params in ({}, {"status": "all"}, {"assigned_to": "none"}, {"mode": "ai"}, {"q": "er"}):
        whole = await _ids(inbox.client, headers, **params)
        walked = await _walk(inbox.client, headers, 3, **params)
        assert walked == whole, params
        assert len(set(walked)) == len(walked)
    assert len(await _ids(inbox.client, headers, status="all")) == len(rows)


async def test_a_cursor_of_one_view_is_refused_by_another(inbox: Inbox) -> None:
    await twelve(inbox)
    headers = inbox.mine.owner.headers

    async def cursor_of(**params: Any) -> str:
        body = (await inbox.client.get(LIST, params={"limit": 2, **params}, headers=headers)).json()
        assert body["next_cursor"]
        return str(body["next_cursor"])

    default = await cursor_of()
    everything = await cursor_of(status="all")
    for cursor, params in (
        (default, {"status": "all"}),
        (everything, {}),
        (everything, {"status": "closed"}),
        (await cursor_of(q="Tie"), {"q": "Nour"}),
    ):
        refused = await inbox.client.get(LIST, params={"cursor": cursor, **params}, headers=headers)
        assert refused.status_code == 422, (cursor, params)
        assert refused.json()["error"]["code"] == "cursor_view_mismatch"
    assert "Tie" not in await cursor_of(q="Tie")


async def test_a_conversation_whose_activity_moves_between_pages_is_never_read_twice(
    inbox: Inbox,
) -> None:
    """It moves to the top: a walk already past the top does not see it again."""
    rows = await twelve(inbox)
    headers = inbox.mine.owner.headers
    first = (await inbox.client.get(LIST, params={"limit": 3}, headers=headers)).json()
    async with inbox.database.session_factory() as session:
        moved = await session.get(Conversation, rows["silent"].id)
        assert moved is not None
        moved.last_message_at = NOW + timedelta(minutes=5)
        await session.commit()
    rest = await _walk(inbox.client, headers, 3, cursor=first["next_cursor"])
    walked = [item["id"] for item in first["items"]] + rest
    assert len(set(walked)) == len(walked)
    assert str(rows["silent"].id) not in walked


async def test_views_never_reach_another_workspace(inbox: Inbox) -> None:
    rows = await twelve(inbox)
    headers = inbox.mine.owner.headers
    theirs = inbox.theirs
    assert await _ids(inbox.client, headers, assigned_to=str(theirs.owner.user_id)) == []
    assert await _ids(inbox.client, headers, assigned_to=str(uuid.uuid4())) == []
    assert await _ids(inbox.client, headers, connection_id=str(theirs.account_id)) == []
    assert await _ids(inbox.client, headers, q="Nour Hassan") == [str(rows["nour"].id)]
    assert await _ids(inbox.client, headers, q=PHONE_A) == [str(rows["nour"].id)]


async def test_assigned_to_me_is_the_caller_for_every_role(inbox: Inbox) -> None:
    rows = await twelve(inbox)
    shop = inbox.mine
    expected = {"owner": "nour", "admin": "human", "member": "pending"}
    for role, person in (("owner", shop.owner), ("admin", shop.admin), ("member", shop.member)):
        assert await _ids(inbox.client, person.headers, assigned_to="me") == [
            str(rows[expected[role]].id)
        ]


async def test_a_search_is_never_written_to_a_log(
    inbox: Inbox, caplog: pytest.LogCaptureFixture
) -> None:
    await twelve(inbox)
    caplog.set_level(logging.DEBUG)
    number = f"+{PHONE_A}"
    response = await inbox.client.get(LIST, params={"q": number}, headers=inbox.mine.owner.headers)
    assert response.status_code == 200
    assert caplog.records, "nothing was captured; the request log is expected"
    for record in caplog.records:
        text = repr(record.__dict__) + record.getMessage()
        assert PHONE_A not in text, record.name


async def test_a_search_out_of_bounds_has_its_own_code(inbox: Inbox) -> None:
    headers = inbox.mine.owner.headers
    for term in ("x", "  y  ", "z" * 101):
        refused = await inbox.client.get(LIST, params={"q": term}, headers=headers)
        assert refused.status_code == 422, term
        assert refused.json()["error"]["code"] == "search_term_invalid"
    assert (
        await inbox.client.get(LIST, params={"q": "  ab  "}, headers=headers)
    ).status_code == 200


async def test_a_view_is_not_combined_with_catching_up(inbox: Inbox) -> None:
    headers = inbox.mine.owner.headers
    refused = await inbox.client.get(
        LIST,
        params={"updated_since": NOW.isoformat(), "status": "all"},
        headers=headers,
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "inbox_view_with_updated_since"
    unknown = await inbox.client.get(LIST, params={"assigned_to": "someone"}, headers=headers)
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "validation_error"

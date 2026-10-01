"""Who a message is from, and who a reply goes to (OMNI-001, OMNI-002, OMNI-004).

Driven through the production write path - `WhatsAppIngestionService` on a
real PostgreSQL - with Meta's documented payload shapes, and through the
production send path with Meta's API faked at the HTTP client.

The properties, each reproduced against the audit's HEAD before it was fixed:

- **A username sender is a sender** (OMNI-002, probe P2). Meta omits the phone
  number for a user with a username who has not written in 30 days; the parser
  dropped the whole message. Now it is stored, under the business-scoped id,
  and answered to that id (mutant M13).
- **Identities link only by what the provider asserted together** (ADR-118).
  A phone and a business-scoped id named in one signed payload are one person;
  two contacts are never merged because of a name (M8), and an identifier is
  never matched across workspaces (M4) or across provider scopes (M1).
- **The workspace comes from the connection, never the customer** (M14).
- **A reply goes to the conversation's participant, never the contact's
  phone** (OMNI-004, M3).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.channel import ContactIdentity, IdentityKind, IdentityScope
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.services import messaging_service as messaging_module
from app.services.channel_ingestion_service import IngestionOutcome
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.queue import AgentJob, AgentQueue

pytestmark = pytest.mark.integration

# Synthetic identifiers only - no real person's number or id. The phone
# numbers use an unallocated-looking range, and the business-scoped ids follow
# Meta's documented `CC.` + alphanumeric shape.
PHONE = "201000000101"
OTHER_PHONE = "201000000102"
BSUID = "EG.0synthetic0user0one"
OTHER_BSUID = "EG.0synthetic0user0two"
WABA = "waba-synthetic-1"
OTHER_WABA = "waba-synthetic-2"


class RecordingQueue:
    """Takes the agent jobs a delivery hands off."""

    def __init__(self) -> None:
        self.jobs: list[AgentJob] = []

    async def enqueue(self, job: AgentJob) -> None:
        self.jobs.append(job)


class Meta:
    """Meta's messages endpoint, answering every send and keeping each request."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "messages": [{"id": f"wamid.out.{uuid.uuid4().hex}"}],
                },
            )

        return httpx.MockTransport(handle)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        meta_access_token="test-access-token",
    )


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch) -> Iterator[Meta]:
    fake = Meta()
    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=fake.transport()),
    )
    yield fake


async def _workspace(
    session: AsyncSession, *, waba: str = WABA, phone_number_id: str | None = None
) -> tuple[Tenant, WhatsAppAccount]:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Identity {tag}", slug=f"identity-{tag}")
    session.add(tenant)
    await session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=phone_number_id or f"PN-{tag}",
        waba_id=waba,
        display_phone_number="+20 100 000 0000",
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(account)
    await session.flush()
    return tenant, account


async def _number(session: AsyncSession, tenant: Tenant, *, waba: str) -> WhatsAppAccount:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"PN-{uuid.uuid4().hex[:10]}",
        waba_id=waba,
        display_phone_number="+20 100 000 0001",
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(account)
    await session.flush()
    return account


def _delivery(
    account: WhatsAppAccount,
    *,
    phone: str | None = None,
    bsuid: str | None = None,
    name: str | None = None,
    username: str | None = None,
    text: str = "hello",
    wamid: str | None = None,
) -> dict[str, Any]:
    """One inbound text message in Meta's documented shape.

    `phone` absent is the username case: Meta sends `from_user_id` and the
    contact's `user_id`, and no `from` or `wa_id` at all.
    """
    sender: dict[str, Any] = {}
    contact: dict[str, Any] = {}
    if phone is not None:
        sender["from"] = phone
        contact["wa_id"] = phone
    if bsuid is not None:
        sender["from_user_id"] = bsuid
        contact["user_id"] = bsuid
    profile: dict[str, Any] = {}
    if name is not None:
        profile["name"] = name
    if username is not None:
        profile["username"] = username
    if profile:
        contact["profile"] = profile
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": account.waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": account.phone_number_id},
                            "contacts": [contact],
                            "messages": [
                                {
                                    "id": wamid or f"wamid.{uuid.uuid4().hex}",
                                    **sender,
                                    "type": "text",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def _ingest(
    session: AsyncSession, payload: dict[str, Any], queue: RecordingQueue | None = None
) -> IngestionOutcome:
    return await WhatsAppIngestionService(
        session=session, queue=cast("AgentQueue", queue or RecordingQueue())
    ).ingest(payload)


async def _identities(session: AsyncSession, tenant: Tenant) -> list[ContactIdentity]:
    rows = await session.execute(
        select(ContactIdentity)
        .where(ContactIdentity.tenant_id == tenant.id)
        .order_by(ContactIdentity.created_at, ContactIdentity.id)
    )
    return list(rows.scalars().all())


async def _contacts(session: AsyncSession, tenant: Tenant) -> list[Contact]:
    rows = await session.execute(
        select(Contact).where(Contact.tenant_id == tenant.id).order_by(Contact.created_at)
    )
    return list(rows.scalars().all())


async def _conversation_of(session: AsyncSession, message_wamid: str) -> Conversation:
    conversation = await session.scalar(
        select(Conversation)
        .join(Message, Message.conversation_id == Conversation.id)
        .where(Message.wa_message_id == message_wamid)
    )
    assert conversation is not None
    return conversation


# ------------------------------------------------ username senders (OMNI-002)


async def test_a_username_sender_is_stored_and_answered_by_business_scoped_id(
    db_session: AsyncSession,
) -> None:
    """Probe P2: at the audit's HEAD this delivery produced no event, no
    message and no contact - `ignored=1` - silently and permanently."""
    tenant, account = await _workspace(db_session)
    queue = RecordingQueue()
    wamid = f"wamid.{uuid.uuid4().hex}"

    outcome = await _ingest(
        db_session,
        _delivery(account, bsuid=BSUID, username="synthetic.user", wamid=wamid),
        queue,
    )

    assert (outcome.stored, outcome.ignored) == (1, 0)
    (contact,) = await _contacts(db_session, tenant)
    assert contact.wa_id is None
    (identity,) = await _identities(db_session, tenant)
    assert (identity.kind, identity.value) == (IdentityKind.BSUID, BSUID)
    # Unique per business portfolio: scoped to the number's WhatsApp Business
    # Account, never to the workspace alone.
    assert (identity.scope, identity.scope_ref) == (IdentityScope.PROVIDER_ACCOUNT, WABA)
    conversation = await _conversation_of(db_session, wamid)
    assert conversation.participant_identity_id == identity.id
    # And it is somebody's turn to answer.
    assert [job.conversation_id for job in queue.jobs] == [conversation.id]


async def test_a_phone_and_a_business_scoped_id_named_together_are_one_person(
    db_session: AsyncSession,
) -> None:
    tenant, account = await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"

    await _ingest(db_session, _delivery(account, phone=PHONE, bsuid=BSUID, wamid=wamid))

    (contact,) = await _contacts(db_session, tenant)
    assert contact.wa_id == PHONE
    kinds = {(identity.kind, identity.value) for identity in await _identities(db_session, tenant)}
    assert kinds == {(IdentityKind.PHONE, PHONE), (IdentityKind.BSUID, BSUID)}
    # A phone, when Meta names one, is what the conversation addresses.
    conversation = await _conversation_of(db_session, wamid)
    participant = await db_session.get(ContactIdentity, conversation.participant_identity_id)
    assert participant is not None and participant.kind is IdentityKind.PHONE


async def test_a_business_scoped_id_arriving_later_joins_the_phones_contact(
    db_session: AsyncSession,
) -> None:
    """The provider asserted the pair; the same thread continues."""
    tenant, account = await _workspace(db_session)
    first, second = f"wamid.{uuid.uuid4().hex}", f"wamid.{uuid.uuid4().hex}"

    await _ingest(db_session, _delivery(account, phone=PHONE, wamid=first))
    outcome = await _ingest(db_session, _delivery(account, phone=PHONE, bsuid=BSUID, wamid=second))

    assert outcome.paired_identities == 1
    assert len(await _contacts(db_session, tenant)) == 1
    assert (await _conversation_of(db_session, first)).id == (
        await _conversation_of(db_session, second)
    ).id


async def test_a_phone_arriving_later_joins_the_username_senders_contact(
    db_session: AsyncSession,
) -> None:
    tenant, account = await _workspace(db_session)
    first, second = f"wamid.{uuid.uuid4().hex}", f"wamid.{uuid.uuid4().hex}"

    await _ingest(db_session, _delivery(account, bsuid=BSUID, wamid=first))
    outcome = await _ingest(db_session, _delivery(account, phone=PHONE, bsuid=BSUID, wamid=second))

    assert outcome.paired_identities == 1
    (contact,) = await _contacts(db_session, tenant)
    assert contact.wa_id == PHONE
    # The conversation keeps the identity it was opened with (ADR-119).
    conversation = await _conversation_of(db_session, second)
    participant = await db_session.get(ContactIdentity, conversation.participant_identity_id)
    assert participant is not None and participant.kind is IdentityKind.BSUID


async def test_identifiers_held_by_two_contacts_are_never_merged(db_session: AsyncSession) -> None:
    """M8's harder half: the provider names a phone held by one contact and a
    business-scoped id held by another. Nothing is merged - the message goes
    to the business-scoped id's contact, and the conflict is counted."""
    tenant, account = await _workspace(db_session)
    await _ingest(db_session, _delivery(account, phone=PHONE))
    await _ingest(db_session, _delivery(account, bsuid=BSUID))
    before = {contact.id: contact.wa_id for contact in await _contacts(db_session, tenant)}
    wamid = f"wamid.{uuid.uuid4().hex}"

    outcome = await _ingest(db_session, _delivery(account, phone=PHONE, bsuid=BSUID, wamid=wamid))

    assert outcome.identity_conflicts == 1
    after = {contact.id: contact.wa_id for contact in await _contacts(db_session, tenant)}
    assert after == before, "no contact gained, lost or changed an identifier"
    holder = await db_session.scalar(
        select(ContactIdentity.contact_id).where(
            ContactIdentity.tenant_id == tenant.id, ContactIdentity.value == BSUID
        )
    )
    assert (await _conversation_of(db_session, wamid)).contact_id == holder


async def test_the_same_display_name_never_links_two_senders(db_session: AsyncSession) -> None:
    """M8: a name is not an identifier."""
    tenant, account = await _workspace(db_session)

    await _ingest(db_session, _delivery(account, bsuid=BSUID, name="Nour"))
    await _ingest(db_session, _delivery(account, bsuid=OTHER_BSUID, name="Nour"))

    assert len(await _contacts(db_session, tenant)) == 2


# ------------------------------------------------------- scopes (M1, M4, M14)


async def test_a_business_scoped_id_is_scoped_by_the_business_account(
    db_session: AsyncSession,
) -> None:
    """M1: two numbers under two WhatsApp Business Accounts are two business
    portfolios, and the same id value there is two people. Under one account
    it is one person, with one conversation per number (ADR-119)."""
    tenant, first = await _workspace(db_session, waba=WABA)
    same_portfolio = await _number(db_session, tenant, waba=WABA)
    other_portfolio = await _number(db_session, tenant, waba=OTHER_WABA)
    a, b, c = (f"wamid.{uuid.uuid4().hex}" for _ in range(3))

    await _ingest(db_session, _delivery(first, bsuid=BSUID, wamid=a))
    await _ingest(db_session, _delivery(same_portfolio, bsuid=BSUID, wamid=b))
    await _ingest(db_session, _delivery(other_portfolio, bsuid=BSUID, wamid=c))

    one = await _conversation_of(db_session, a)
    two = await _conversation_of(db_session, b)
    three = await _conversation_of(db_session, c)
    assert one.contact_id == two.contact_id
    assert one.id != two.id and one.account_id != two.account_id
    assert three.contact_id != one.contact_id
    assert len(await _identities(db_session, tenant)) == 2


async def test_an_identifier_is_never_matched_across_workspaces(db_session: AsyncSession) -> None:
    """M4: the same phone and id in another workspace - even under the same
    account id string - is another workspace's customer."""
    acme, acme_number = await _workspace(db_session, waba=WABA)
    rival, rival_number = await _workspace(db_session, waba=WABA)

    await _ingest(db_session, _delivery(acme_number, phone=PHONE, bsuid=BSUID))
    await _ingest(db_session, _delivery(rival_number, phone=PHONE, bsuid=BSUID))

    for tenant in (acme, rival):
        (contact,) = await _contacts(db_session, tenant)
        assert contact.wa_id == PHONE
        assert {identity.contact_id for identity in await _identities(db_session, tenant)} == {
            contact.id
        }


async def test_the_workspace_is_the_numbers_not_the_customers(db_session: AsyncSession) -> None:
    """M14: a customer known to workspace A writing to workspace B's number is
    B's conversation. Nothing about the sender chooses the workspace."""
    acme, acme_number = await _workspace(db_session)
    rival, rival_number = await _workspace(db_session)
    await _ingest(db_session, _delivery(acme_number, phone=PHONE))
    wamid = f"wamid.{uuid.uuid4().hex}"

    await _ingest(db_session, _delivery(rival_number, phone=PHONE, wamid=wamid))

    conversation = await _conversation_of(db_session, wamid)
    assert conversation.tenant_id == rival.id
    assert conversation.account_id == rival_number.id
    assert len(await _contacts(db_session, acme)) == 1


# ----------------------------------------------------- addressing (OMNI-004)


async def _reply(
    session: AsyncSession, settings: Settings, tenant: Tenant, conversation: Conversation
) -> Message:
    return await MessagingService(
        session=session, settings=settings, tenant_id=tenant.id
    ).send_text(
        conversation_id=conversation.id, body="Thanks for writing.", origin=MessageOrigin.HUMAN
    )


async def test_a_reply_to_a_username_sender_goes_to_their_business_scoped_id(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    """M13's other half: stored is not enough - the reply must be addressable."""
    tenant, account = await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"
    await _ingest(db_session, _delivery(account, bsuid=BSUID, wamid=wamid))

    message = await _reply(db_session, settings, tenant, await _conversation_of(db_session, wamid))

    assert message.status is MessageStatus.SENT
    (body,) = meta.bodies
    assert body["recipient"] == BSUID
    assert "to" not in body


async def test_a_reply_goes_to_the_participant_even_after_the_contact_gains_a_phone(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    """M3: the conversation was opened with the business-scoped id and stays
    pinned to it. Routing by the contact's `wa_id` - which pairing has since
    filled - would send to an address this thread never used."""
    tenant, account = await _workspace(db_session)
    first = f"wamid.{uuid.uuid4().hex}"
    await _ingest(db_session, _delivery(account, bsuid=BSUID, wamid=first))
    await _ingest(db_session, _delivery(account, phone=PHONE, bsuid=BSUID))
    conversation = await _conversation_of(db_session, first)
    assert (await db_session.get(Contact, conversation.contact_id)).wa_id == PHONE  # type: ignore[union-attr]

    await _reply(db_session, settings, tenant, conversation)

    (body,) = meta.bodies
    assert body.get("recipient") == BSUID
    assert "to" not in body


async def test_a_reply_to_a_phone_conversation_is_sent_to_the_phone(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    tenant, account = await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"
    await _ingest(db_session, _delivery(account, phone=PHONE, bsuid=BSUID, wamid=wamid))

    await _reply(db_session, settings, tenant, await _conversation_of(db_session, wamid))

    (body,) = meta.bodies
    assert body["to"] == PHONE
    assert "recipient" not in body


async def test_a_status_naming_only_the_business_scoped_id_is_projected(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    """Meta omits `recipient_id` for a username recipient; the status still
    names the message, which is all projection needs."""
    tenant, account = await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"
    await _ingest(db_session, _delivery(account, bsuid=BSUID, wamid=wamid))
    sent = await _reply(db_session, settings, tenant, await _conversation_of(db_session, wamid))
    status = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": account.waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": account.phone_number_id},
                            "statuses": [
                                {
                                    "id": sent.wa_message_id,
                                    "status": "delivered",
                                    "recipient_user_id": BSUID,
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    outcome = await _ingest(db_session, status)

    assert (outcome.stored, outcome.ignored) == (1, 0)
    await db_session.refresh(sent)
    assert sent.status is MessageStatus.DELIVERED


async def test_every_conversation_is_pinned_to_an_identity_of_its_own_contact(
    db_session: AsyncSession,
) -> None:
    """The invariant the participant key enforces, read back after a mixed run."""
    tenant, account = await _workspace(db_session)
    await _ingest(db_session, _delivery(account, phone=PHONE))
    await _ingest(db_session, _delivery(account, bsuid=BSUID))
    await _ingest(db_session, _delivery(account, phone=OTHER_PHONE, bsuid=OTHER_BSUID))

    mismatched = await db_session.scalar(
        select(func.count())
        .select_from(Conversation)
        .join(ContactIdentity, ContactIdentity.id == Conversation.participant_identity_id)
        .where(
            Conversation.tenant_id == tenant.id,
            (ContactIdentity.contact_id != Conversation.contact_id)
            | (ContactIdentity.channel != Conversation.channel)
            | (ContactIdentity.tenant_id != Conversation.tenant_id),
        )
    )
    total = await db_session.scalar(
        select(func.count()).select_from(Conversation).where(Conversation.tenant_id == tenant.id)
    )
    assert (total, mismatched) == (3, 0)
    inbound = await db_session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.tenant_id == tenant.id, Message.direction == MessageDirection.INBOUND)
    )
    assert inbound == 3

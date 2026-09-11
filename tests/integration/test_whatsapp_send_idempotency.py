"""A retried send reaches the customer once, and a paused template does not.

Two guards on the same choke point, both of which protect a customer from
something a member of the workspace did not intend.

**`Idempotency-Key`.** A double-clicked button, a retried mobile request or a
proxy replay put two copies of the same message on a customer's phone, because
the outbound API took nothing that could tell a repeat from a second message.
Meta's send endpoint offers no idempotency key either, so it has to be solved
here (MSG-15).

The key is explicit and never inferred. Sending the same words twice is
something people legitimately do - "are you there?" twice is two messages - so
suppressing a duplicate body within a time window would silently swallow real
intent. `test_two_sends_of_the_same_words_without_a_key_are_two_messages` is
what pins that, and it is as important as the deduplication tests.

**The template registry.** Campaigns and follow-ups both refused a template the
registry records as withdrawn; `POST /conversations/{id}/messages/template`
refused nothing, so a member could spend the account's rejected-template
attempts one at a time - and those attempts are what costs a workspace its
number (MSG-10).

Provider calls are counted against a real socket, so "one message" means one
connection was accepted rather than one mock assertion.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.exceptions import ConflictError, ValidationError
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationStatus,
    Message,
    MessageDeliveryState,
    MessageDirection,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
)
from app.integrations.whatsapp import client as client_module
from app.services.messaging_service import MessagingService

pytestmark = pytest.mark.integration

Handler = Callable[[bytes], bytes | None]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class CountingGraph:
    """A socket standing in for Meta, answering success and counting calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.port = _free_port()
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> CountingGraph:
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=self.port)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.calls += 1
        body = json.dumps(
            {
                "messages": [{"id": f"wamid.{uuid.uuid4().hex}"}],
                "contacts": [{"wa_id": "201555000999"}],
            }
        ).encode()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader.read(65536), timeout=5)
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
            )
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


@contextlib.asynccontextmanager
async def _graph(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[CountingGraph]:
    async with CountingGraph() as fake:
        monkeypatch.setattr(client_module, "GRAPH_BASE_URL", f"http://127.0.0.1:{fake.port}")
        yield fake


class RefusingGraph(CountingGraph):
    """Meta declining, with a specific error envelope and a call count."""

    def __init__(self, status: int, body: dict[str, object]) -> None:
        super().__init__()
        self._status = status
        self._body = body

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.calls += 1
        payload = json.dumps(self._body).encode()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader.read(65536), timeout=5)
            writer.write(
                f"HTTP/1.1 {self._status} X\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload
            )
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


@contextlib.asynccontextmanager
async def _refusing_graph(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: dict[str, object],
) -> AsyncIterator[RefusingGraph]:
    async with RefusingGraph(status, body) as fake:
        monkeypatch.setattr(client_module, "GRAPH_BASE_URL", f"http://127.0.0.1:{fake.port}")
        yield fake


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    """A plain pool, because the guarded one refuses loopback by design."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        yield client


async def _conversation(
    session: AsyncSession,
) -> tuple[Tenant, Conversation, WhatsAppAccount]:
    slug = f"idem-{uuid.uuid4().hex[:8]}"
    tenant = Tenant(name="Idem", slug=slug)
    session.add(tenant)
    await session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{slug}",
        waba_id="555000444",
        display_phone_number="+201000000004",
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    contact = Contact(tenant_id=tenant.id, wa_id=f"2015{uuid.uuid4().int % 10_000_000:07d}")
    session.add_all([account, contact])
    await session.flush()
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        status=ConversationStatus.OPEN,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(conversation)
    await session.flush()
    return tenant, conversation, account


def _messaging(
    session: AsyncSession,
    tenant: Tenant,
    http: httpx.AsyncClient,
) -> MessagingService:
    settings = Settings(_env_file=None, environment="test", meta_access_token="a-token")
    return MessagingService(
        session=session,
        settings=settings,
        tenant_id=tenant.id,
        http=http,
    )


async def _outbound_count(session: AsyncSession, tenant: Tenant) -> int:
    rows = (
        (
            await session.execute(
                select(Message).where(
                    Message.tenant_id == tenant.id,
                    Message.direction == MessageDirection.OUTBOUND,
                )
            )
        )
        .scalars()
        .all()
    )
    return len(rows)


async def test_a_repeated_send_with_one_key_reaches_the_customer_once(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """The double-clicked button, which is what this exists for.

    Both assertions are needed. One row means the transcript is right; one
    provider call means the customer's phone is right, and it is the second
    that a duplicate would show up on.
    """
    tenant, conversation, account = await _conversation(db_session)
    key = f"key-{uuid.uuid4().hex}"

    async with _graph(monkeypatch) as fake:
        service = _messaging(db_session, tenant, http)
        first = await service.send_text(
            conversation_id=conversation.id, body="are you there?", idempotency_key=key
        )
        second = await service.send_text(
            conversation_id=conversation.id, body="are you there?", idempotency_key=key
        )

    assert fake.calls == 1
    assert first.id == second.id
    assert await _outbound_count(db_session, tenant) == 1
    # The replay returns the *original* result, including the provider id, so a
    # caller that retried is told what actually happened rather than being
    # handed a fresh pending row.
    assert second.wa_message_id == first.wa_message_id


async def test_two_sends_of_the_same_words_without_a_key_are_two_messages(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """The decision, written as a test: no content-based deduplication.

    Asking twice is something people do, and a system that silently swallowed
    the second would be worse than one that occasionally sends a duplicate -
    the duplicate is visible and the swallowed message is not.
    """
    tenant, conversation, account = await _conversation(db_session)

    async with _graph(monkeypatch) as fake:
        service = _messaging(db_session, tenant, http)
        await service.send_text(conversation_id=conversation.id, body="are you there?")
        await service.send_text(conversation_id=conversation.id, body="are you there?")

    assert fake.calls == 2
    assert await _outbound_count(db_session, tenant) == 2


async def test_different_keys_for_the_same_words_are_two_messages(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """The key says which requests are the same request, and nothing else does."""
    tenant, conversation, account = await _conversation(db_session)

    async with _graph(monkeypatch) as fake:
        service = _messaging(db_session, tenant, http)
        await service.send_text(
            conversation_id=conversation.id,
            body="are you there?",
            idempotency_key=f"key-{uuid.uuid4().hex}",
        )
        await service.send_text(
            conversation_id=conversation.id,
            body="are you there?",
            idempotency_key=f"key-{uuid.uuid4().hex}",
        )

    assert fake.calls == 2
    assert await _outbound_count(db_session, tenant) == 2


async def test_reusing_a_key_for_different_words_is_refused(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """Silently returning the old message would be the worst answer available.

    It would tell the caller their new message was sent when it was not. A
    conflict says what happened, and the usual cause - a key generated once and
    then held across an edit - is a caller bug worth surfacing.
    """
    tenant, conversation, account = await _conversation(db_session)
    key = f"key-{uuid.uuid4().hex}"

    async with _graph(monkeypatch) as fake:
        service = _messaging(db_session, tenant, http)
        await service.send_text(
            conversation_id=conversation.id, body="first thing", idempotency_key=key
        )
        with pytest.raises(ConflictError):
            await service.send_text(
                conversation_id=conversation.id, body="something else", idempotency_key=key
            )

    assert fake.calls == 1
    assert await _outbound_count(db_session, tenant) == 1


async def test_two_keys_in_two_workspaces_do_not_collide(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """Keys are generated by clients, so the scope has to be the workspace.

    A global constraint would let one workspace's chosen key suppress another's
    message, which is a cross-tenant defect wearing an idempotency hat.
    """
    first_tenant, first_conversation, _ = await _conversation(db_session)
    second_tenant, second_conversation, _ = await _conversation(db_session)
    key = f"key-{uuid.uuid4().hex}"

    async with _graph(monkeypatch) as fake:
        await _messaging(db_session, first_tenant, http).send_text(
            conversation_id=first_conversation.id, body="hello", idempotency_key=key
        )
        await _messaging(db_session, second_tenant, http).send_text(
            conversation_id=second_conversation.id, body="hello", idempotency_key=key
        )

    assert fake.calls == 2
    assert await _outbound_count(db_session, first_tenant) == 1
    assert await _outbound_count(db_session, second_tenant) == 1


async def test_concurrent_submissions_of_one_key_send_once(
    prepared_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read is the fast path; the unique constraint is the guarantee.

    Sequential deduplication is satisfied by the read alone, so the test above
    would pass with no constraint at all. These two requests are in flight
    together, in **separate transactions on separate connections** - which is
    what a double-clicked button actually produces, two requests the server
    handles independently - so both miss the read and one of them has to lose
    at the insert and return the winner's row.

    Two sessions rather than two coroutines on one, because an `AsyncSession`
    is not safe to drive concurrently and a `gather` over one would be testing
    SQLAlchemy's refusal rather than PostgreSQL's constraint.

    Committed rather than rolled back, so the two connections can see each
    other at all; the tenant is removed afterwards and cascades take the rest.
    """
    engine = create_async_engine(prepared_database, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    key = f"key-{uuid.uuid4().hex}"

    async with maker() as setup:
        tenant, conversation, _ = await _conversation(setup)
        tenant_id, conversation_id = tenant.id, conversation.id
        await setup.commit()

    try:
        barrier = asyncio.Barrier(2)

        async def submit() -> uuid.UUID:
            async with (
                httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client,
                maker() as session,
            ):
                settings = Settings(_env_file=None, environment="test", meta_access_token="a-token")
                service = MessagingService(
                    session=session,
                    settings=settings,
                    tenant_id=tenant_id,
                    http=client,
                )
                await barrier.wait()
                message = await service.send_text(
                    conversation_id=conversation_id,
                    body="are you there?",
                    idempotency_key=key,
                )
                await session.commit()
                return message.id

        async with _graph(monkeypatch) as fake:
            results = await asyncio.gather(*(submit() for _ in range(2)))

        assert fake.calls == 1
        assert results[0] == results[1]
        async with maker() as reader:
            rows = (
                (
                    await reader.execute(
                        select(Message).where(
                            Message.tenant_id == tenant_id,
                            Message.direction == MessageDirection.OUTBOUND,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
    finally:
        async with maker() as cleanup:
            tenant_row = await cleanup.get(Tenant, tenant_id)
            if tenant_row is not None:
                await cleanup.delete(tenant_row)
            await cleanup.commit()
        await engine.dispose()


async def test_a_human_reply_is_recorded_as_a_human_reply(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """The transcript says what produced each line, rather than implying it.

    `sent_by_id` was the only evidence, and it gets two of the four producers
    wrong: a campaign carries its creator and read as a human reply, and a
    follow-up carries nobody and read as an AI reply (MSG-16). This and the
    campaign and follow-up tests elsewhere pin all four.
    """
    tenant, conversation, _ = await _conversation(db_session)

    async with _graph(monkeypatch):
        message = await _messaging(db_session, tenant, http).send_text(
            conversation_id=conversation.id,
            body="hello",
            sent_by_id=None,
            origin=MessageOrigin.HUMAN,
        )

    assert message.origin is MessageOrigin.HUMAN


async def test_an_agent_reply_is_recorded_as_an_agent_reply(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """Both of these carry no `sent_by_id`, so only the column tells them apart.

    A follow-up and an AI reply were indistinguishable in the transcript, and
    they are different things: one answers what a customer just said, the other
    arrives because they stopped saying anything.
    """
    tenant, conversation, _ = await _conversation(db_session)

    async with _graph(monkeypatch):
        agent_reply = await _messaging(db_session, tenant, http).send_text(
            conversation_id=conversation.id,
            body="an answer",
            origin=MessageOrigin.AGENT,
        )
        nudge = await _messaging(db_session, tenant, http).send_text(
            conversation_id=conversation.id,
            body="still there?",
            origin=MessageOrigin.FOLLOW_UP,
        )

    assert agent_reply.sent_by_id is None
    assert nudge.sent_by_id is None
    assert agent_reply.origin is MessageOrigin.AGENT
    assert nudge.origin is MessageOrigin.FOLLOW_UP


async def _template(
    session: AsyncSession,
    tenant: Tenant,
    account: WhatsAppAccount,
    *,
    status: TemplateStatus,
) -> WhatsAppTemplate:
    """A registry row as a sync would leave it, bound to the number it is for."""
    template = WhatsAppTemplate(
        tenant_id=tenant.id,
        account_id=account.id,
        name=f"tpl_{uuid.uuid4().hex[:8]}",
        language="en",
        category=TemplateCategory.UTILITY,
        status=status,
        synced_at=datetime.now(UTC),
    )
    session.add(template)
    await session.flush()
    return template


@pytest.mark.parametrize(
    "status",
    [TemplateStatus.PAUSED, TemplateStatus.REJECTED, TemplateStatus.DISABLED],
)
async def test_a_manual_send_of_a_withdrawn_template_is_refused(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
    status: TemplateStatus,
) -> None:
    """The guard the automated paths had and the manual route did not.

    Meta refuses these itself, so no policy violation reaches a customer - but
    the account accrues exactly the rejected-template attempts the campaign and
    follow-up paths are careful to avoid, and those attempts are what costs a
    workspace its number (MSG-10).
    """
    tenant, conversation, account = await _conversation(db_session)
    template = await _template(db_session, tenant, account, status=status)

    async with _graph(monkeypatch) as fake:
        with pytest.raises(ValidationError):
            await _messaging(db_session, tenant, http).send_template(
                conversation_id=conversation.id,
                name=template.name,
                language=template.language,
            )

    # Refused before the network, so the attempt costs the account nothing.
    assert fake.calls == 0
    assert await _outbound_count(db_session, tenant) == 0


async def test_metas_refusal_of_a_template_is_written_back_to_the_registry(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """One rejection teaches the registry, instead of one per recipient.

    Sync is an administrative action nobody performs on a schedule, so a
    template Meta pauses stays `APPROVED` locally until somebody clicks it -
    and every follow-up and campaign using it is refused by Meta one at a time
    in the meantime (MSG-24).

    Recorded as `PAUSED` whatever the code, because pausing is the reversible
    state and a sync is what establishes which of the three Meta actually
    means. Overstating it would make a template Meta un-pauses look
    permanently dead.
    """
    tenant, conversation, account = await _conversation(db_session)
    template = await _template(db_session, tenant, account, status=TemplateStatus.APPROVED)
    refusal = {"error": {"code": 132015, "type": "OAuthException", "message": "paused"}}

    async with _refusing_graph(monkeypatch, 400, refusal) as fake:
        message = await _messaging(db_session, tenant, http).send_template(
            conversation_id=conversation.id,
            name=template.name,
            language=template.language,
        )

    assert fake.calls == 1
    # Nothing was delivered, and that is known - so the send is an ordinary
    # undelivered one rather than an unknown.
    assert message.status is MessageStatus.FAILED
    assert message.delivery_state is MessageDeliveryState.UNDELIVERED

    await db_session.refresh(template)
    assert template.status is TemplateStatus.PAUSED
    assert template.rejection_reason is not None
    assert "132015" in template.rejection_reason

    # And the next send of it is refused locally, without asking Meta again.
    async with _refusing_graph(monkeypatch, 400, refusal) as second:
        with pytest.raises(ValidationError):
            await _messaging(db_session, tenant, http).send_template(
                conversation_id=conversation.id,
                name=template.name,
                language=template.language,
            )
    assert second.calls == 0


async def test_an_ambiguous_refusal_leaves_the_registry_alone(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """Only codes that unambiguously mean "this template" are written back.

    Marking a template invalid on a guess takes a working template away from a
    workspace, and getting it back needs a manual sync. A bad parameter is
    about this *message*, not about the template.
    """
    tenant, conversation, account = await _conversation(db_session)
    template = await _template(db_session, tenant, account, status=TemplateStatus.APPROVED)
    refusal = {"error": {"code": 131009, "type": "OAuthException", "message": "bad param"}}

    async with _refusing_graph(monkeypatch, 400, refusal):
        await _messaging(db_session, tenant, http).send_template(
            conversation_id=conversation.id,
            name=template.name,
            language=template.language,
        )

    await db_session.refresh(template)
    assert template.status is TemplateStatus.APPROVED
    assert template.rejection_reason is None


async def test_a_refusal_does_not_invent_a_registry_row(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """A template the registry has never heard of stays unheard of.

    Creating a row from a refusal would turn one rejection into a permanent
    local block on a name this workspace may never have synced - and "unknown"
    is deliberately allowed to send.
    """
    tenant, conversation, _ = await _conversation(db_session)
    refusal = {"error": {"code": 132015, "type": "OAuthException", "message": "paused"}}

    async with _refusing_graph(monkeypatch, 400, refusal):
        await _messaging(db_session, tenant, http).send_template(
            conversation_id=conversation.id,
            name="never_synced",
            language="en",
        )

    rows = (
        (
            await db_session.execute(
                select(WhatsAppTemplate).where(WhatsAppTemplate.tenant_id == tenant.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_an_approved_template_still_sends(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """The negative control: the guard refuses the withdrawn, not the working."""
    tenant, conversation, account = await _conversation(db_session)
    template = await _template(db_session, tenant, account, status=TemplateStatus.APPROVED)

    async with _graph(monkeypatch) as fake:
        message = await _messaging(db_session, tenant, http).send_template(
            conversation_id=conversation.id,
            name=template.name,
            language=template.language,
        )

    assert fake.calls == 1
    assert message.template_name == template.name


async def test_a_template_the_registry_has_never_heard_of_is_allowed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """The asymmetry, pinned so it is not "tidied up" into a stricter rule.

    A workspace that has not synced cannot be told apart from one whose
    template does not exist, and refusing both would lose every
    template-bearing message the first workspace has. Campaigns apply the
    stricter rule on top, because setting one up is a deliberate act that can
    afford to require a sync first.
    """
    tenant, conversation, account = await _conversation(db_session)

    async with _graph(monkeypatch) as fake:
        message = await _messaging(db_session, tenant, http).send_template(
            conversation_id=conversation.id,
            name="never_synced",
            language="en",
        )

    assert fake.calls == 1
    assert message.template_name == "never_synced"

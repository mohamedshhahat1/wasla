"""What a send records when Meta behaves badly, against a real socket.

A local HTTP server stands in for `graph.facebook.com` and is instructed to
misbehave in a specific way per test. Real sockets rather than a mocked client,
because two of the behaviours being checked are *transport* failures - a reset
connection, a half-written response - which a mock cannot produce and which is
precisely why they were missed.

The distinction every assertion here turns on is the one ADR-093 is built
around, and it is not "did it work":

    UNDELIVERED  nothing reached the customer, and that is *known*.
                 A caller may send again.
    REQUESTED    Meta may already have delivered this. Terminal.
                 The one thing that must not follow is another send.

Three classifications were on the wrong side of that line, and two of them in
the direction that puts a second copy on somebody's phone:

* a `200` whose body carried no usable message id was recorded `UNDELIVERED`,
  although a `200` is Meta saying it accepted the message (MSG-07);
* a reset connection escaped the taxonomy entirely as a raw `httpx` exception
  (MSG-08);
* an invalid credential was indistinguishable from a bad parameter, so a sweep
  discovered the same dead token once per recipient (MSG-18).
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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationStatus,
    MessageDeliveryState,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.whatsapp import client as client_module
from app.integrations.whatsapp.client import ProviderAuthError
from app.services.messaging_service import WHATSAPP_TEXT_MAX_CHARS, MessagingService

pytestmark = pytest.mark.integration

# What one handler does with a connection. Returning None means "close it
# without answering", which is how a reset is produced.
Handler = Callable[[bytes], bytes | None]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _response(status: int, body: object) -> bytes:
    payload = json.dumps(body).encode()
    head = (
        f"HTTP/1.1 {status} X\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    return head + payload


class FakeGraph:
    """A socket that counts calls and answers however it was told to.

    Counting is the point. "One provider call" here means one connection was
    accepted, not one mock assertion - so a retry loop that fired three times
    cannot be mistaken for one that fired once.
    """

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.calls = 0
        self.port = _free_port()
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> FakeGraph:
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
        with contextlib.suppress(Exception):
            request = await asyncio.wait_for(reader.read(65536), timeout=5)
            reply = self._handler(request)
            if reply is not None:
                writer.write(reply)
                await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


@contextlib.asynccontextmanager
async def _graph(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> AsyncIterator[FakeGraph]:
    """Point the client's base URL at a local socket for the duration.

    The base URL is patched rather than the client injected, so the real
    `WhatsAppClient` - its retry loop, its timeouts, its classification - is
    what runs.
    """
    async with FakeGraph(handler) as fake:
        monkeypatch.setattr(client_module, "GRAPH_BASE_URL", f"http://127.0.0.1:{fake.port}")
        yield fake


async def _conversation(session: AsyncSession) -> tuple[Tenant, Conversation]:
    slug = f"outcomes-{uuid.uuid4().hex[:8]}"
    tenant = Tenant(name="Outcomes", slug=slug)
    session.add(tenant)
    await session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{slug}",
        waba_id="555000333",
        display_phone_number="+201000000003",
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
    return tenant, conversation


def _messaging(
    session: AsyncSession,
    tenant: Tenant,
    http: httpx.AsyncClient,
) -> MessagingService:
    """The real service over a plain transport.

    The client it would build for itself resolves once and refuses anything but
    `https` to a public address (`app.core.net`), which is correct and is what
    makes this file need an injected pool: a loopback socket is exactly what
    that guard exists to refuse. What is under test here is the *real*
    `WhatsAppClient` - its retry loop, its status handling, its
    classification - so only the transport is substituted.
    """
    settings = Settings(
        _env_file=None,
        environment="test",
        meta_access_token="a-platform-token",
    )
    return MessagingService(
        session=session,
        settings=settings,
        tenant_id=tenant.id,
        http=http,
    )


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        yield client


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("no messages array", {"messaging_product": "whatsapp"}),
        ("empty messages array", {"messages": []}),
        ("a message with no id", {"messages": [{}]}),
    ],
)
async def test_a_2xx_without_a_usable_id_stays_unresolved_rather_than_failed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
    label: str,
    body: dict[str, object],
) -> None:
    """The classification that could put a message on a phone twice.

    Meta answered `200`, which is Meta saying it took the message. Recording
    that as a definite failure is the one answer that licenses a new send, and
    a campaign or follow-up acting on it sends the customer a second copy.
    """
    tenant, conversation = await _conversation(db_session)

    async with _graph(monkeypatch, lambda _: _response(200, body)) as fake:
        message = await _messaging(db_session, tenant, http).send_text(
            conversation_id=conversation.id,
            body=f"hello ({label})",
            origin=MessageOrigin.HUMAN,
        )

    assert fake.calls == 1
    assert message.status is MessageStatus.PENDING
    assert message.delivery_state is MessageDeliveryState.REQUESTED
    # The property the campaign and follow-up sweeps read, and the reason they
    # abandon rather than retry.
    assert message.delivery_uncertain is True
    assert message.wa_message_id is None


async def test_a_2xx_with_an_unreadable_body_is_also_unresolved(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """Same reasoning: the status code already said Meta accepted it."""
    tenant, conversation = await _conversation(db_session)

    def garbage(_: bytes) -> bytes:
        payload = b"not json at all"
        return (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + payload
        )

    async with _graph(monkeypatch, garbage) as fake:
        message = await _messaging(db_session, tenant, http).send_text(
            conversation_id=conversation.id,
            body="hello",
            origin=MessageOrigin.HUMAN,
        )

    assert fake.calls == 1
    assert message.delivery_state is MessageDeliveryState.REQUESTED


async def test_a_connection_reset_mid_response_is_unresolved_not_an_escape(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """The request left this process and no answer came back.

    Before `TransportError` was caught, this propagated as a raw
    `httpx.RemoteProtocolError` past `_attempt` - which catches only this
    package's own types - and out of `_dispatch` entirely, so the caller got an
    unclassified error and a campaign batch stopped where it stood.
    """
    tenant, conversation = await _conversation(db_session)

    async with _graph(monkeypatch, lambda _: None) as fake:
        message = await _messaging(db_session, tenant, http).send_text(
            conversation_id=conversation.id,
            body="hello",
            origin=MessageOrigin.HUMAN,
        )

    # Not retried: the request may have reached Meta, so a second attempt is
    # the one thing that could duplicate it.
    assert fake.calls == 1
    assert message.status is MessageStatus.PENDING
    assert message.delivery_state is MessageDeliveryState.REQUESTED


async def test_a_5xx_is_still_unresolved(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """The behaviour that was already right, pinned so it stays right.

    Everything above moved *towards* this classification, so a change that
    accidentally moved this one away would undo the reason they moved.
    """
    tenant, conversation = await _conversation(db_session)

    async with _graph(monkeypatch, lambda _: _response(500, {"error": {"code": 1}})) as fake:
        message = await _messaging(db_session, tenant, http).send_text(
            conversation_id=conversation.id,
            body="hello",
            origin=MessageOrigin.HUMAN,
        )

    assert fake.calls == 1
    assert message.delivery_state is MessageDeliveryState.REQUESTED


async def test_an_ordinary_rejection_is_recorded_as_undelivered(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """The negative control for every "stays REQUESTED" test above.

    Meta read this request and declined it - here, the recipient is outside the
    service window - so nothing was delivered and that is *known*. Without this
    test, marking everything uncertain would satisfy the rest of the file while
    making the delivery state useless.
    """
    tenant, conversation = await _conversation(db_session)
    rejection = {"error": {"code": 131047, "type": "OAuthException", "message": "outside"}}

    async with _graph(monkeypatch, lambda _: _response(400, rejection)) as fake:
        message = await _messaging(db_session, tenant, http).send_text(
            conversation_id=conversation.id,
            body="hello",
            origin=MessageOrigin.HUMAN,
        )

    assert fake.calls == 1
    assert message.status is MessageStatus.FAILED
    assert message.delivery_state is MessageDeliveryState.UNDELIVERED
    assert message.delivery_uncertain is False


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, 190),
        (403, 190),
        # Meta returns `code 190` on a 400 as readily as on a 401, and the code
        # is the authoritative signal.
        (400, 190),
        # A 401 with no code at all is still unambiguous from the status alone.
        (401, None),
    ],
)
async def test_a_refused_credential_is_its_own_failure(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
    status: int,
    code: int | None,
) -> None:
    """Not this message's problem, so it is not filed against this message.

    The row is still honestly `UNDELIVERED` - Meta declined before reading it,
    so nothing reached the customer - but the failure is raised so a campaign
    can stop rather than burn one attempt budget per recipient discovering the
    same dead token (MSG-18).
    """
    tenant, conversation = await _conversation(db_session)
    error: dict[str, object] = {"type": "OAuthException", "message": "bad token"}
    if code is not None:
        error["code"] = code

    async with _graph(monkeypatch, lambda _: _response(status, {"error": error})) as fake:
        with pytest.raises(ProviderAuthError):
            await _messaging(db_session, tenant, http).send_text(
                conversation_id=conversation.id,
                body="hello",
                origin=MessageOrigin.HUMAN,
            )

    assert fake.calls == 1
    # No credential material anywhere in what the caller is told.
    assert "token" not in ProviderAuthError.message.lower()


async def test_a_bare_403_is_not_treated_as_a_dead_credential(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """Meta uses 403 for permission problems about the number, not the token.

    Treating every 403 as a refused credential would stop a whole campaign for
    a condition that should have failed one recipient.
    """
    tenant, conversation = await _conversation(db_session)
    rejection = {"error": {"code": 131009, "type": "OAuthException", "message": "bad param"}}

    async with _graph(monkeypatch, lambda _: _response(403, rejection)):
        message = await _messaging(db_session, tenant, http).send_text(
            conversation_id=conversation.id,
            body="hello",
            origin=MessageOrigin.HUMAN,
        )

    assert message.status is MessageStatus.FAILED
    assert message.delivery_state is MessageDeliveryState.UNDELIVERED


async def test_an_over_long_reply_never_reaches_the_provider(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """The agent path does not go through a request schema, so this is the cap.

    An over-long reply used to be sent whole, refused by Meta with a 400,
    recorded as failed, and the customer received nothing - after the workspace
    had already paid for the inference, with no alert to say so (MSG-25).

    Refused before anything is staged, so it costs no row and no provider call.
    """
    tenant, conversation = await _conversation(db_session)

    async with _graph(monkeypatch, lambda _: _response(200, {"messages": [{"id": "x"}]})) as fake:
        with pytest.raises(ValidationError):
            await _messaging(db_session, tenant, http).send_text(
                conversation_id=conversation.id,
                body="x" * (WHATSAPP_TEXT_MAX_CHARS + 1),
                origin=MessageOrigin.AGENT,
            )

    assert fake.calls == 0


async def test_a_reply_at_exactly_the_limit_is_sent(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    http: httpx.AsyncClient,
) -> None:
    """The boundary, from the allowed side.

    Meta's limit is inclusive, so refusing at exactly the limit would be this
    system inventing a stricter rule than the provider's and silently losing
    the longest legitimate replies.
    """
    tenant, conversation = await _conversation(db_session)
    accepted = {"messages": [{"id": "wamid.LIMIT"}], "contacts": [{"wa_id": "2015"}]}

    async with _graph(monkeypatch, lambda _: _response(200, accepted)) as fake:
        message = await _messaging(db_session, tenant, http).send_text(
            conversation_id=conversation.id,
            body="x" * WHATSAPP_TEXT_MAX_CHARS,
            origin=MessageOrigin.AGENT,
        )

    assert fake.calls == 1
    assert message.status is MessageStatus.SENT
    assert message.wa_message_id == "wamid.LIMIT"

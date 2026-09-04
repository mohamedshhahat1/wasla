"""When Meta may deliver a message, relative to what this system has committed.

`MessagingService._dispatch` used to stage the message row, flush it, call Meta
with the transaction still open, and commit afterwards. Two things followed, and
both were reproduced against this same PostgreSQL before anything changed:

    === 1. connection lifetime, pool_size=1 max_overflow=0 ===
    send parked inside the Meta call : yes
    database connections checked out : 1
    verdict                          : HELD across the provider call

    === 2. Meta accepts, then the process stops before COMMIT ===
    messages Meta accepted           : 1
    outbound rows Wasla can find     : 0
    RESULT: the customer has a message Wasla has no record of.

The second is the one that matters. A message on somebody's phone that this
system has no row for is unfindable - nobody can be told it went, nobody can
decide what to do about it, and the follow-up or campaign that asked for it
looks untouched and asks again.

**Real commits, on a pool of this file's own.** The suite's `db_session` joins
the test's transaction as a savepoint, which would make a committed send
indistinguishable from an uncommitted one - exactly the property under test.

**A barrier, not a stopwatch.** The send announces itself on arrival at the
provider and waits, so "the connection was given back" is observed at a moment
the test chose rather than inferred from a duration.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import QueuePool

from app.core.config import Settings
from app.core.exceptions import RateLimitedError
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDeliveryState,
    MessageDirection,
    MessageStatus,
)
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.integrations.whatsapp.client import (
    SendNotAttemptedError,
    SentMessage,
    UncertainDeliveryError,
)
from app.services.follow_up_service import FollowUpService
from app.services.messaging_service import MessagingService

pytestmark = pytest.mark.integration

PIXEL = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class Provider:
    """A WhatsApp client double, recording what it was asked and answering how told.

    Every method the three send paths reach. `parked` lets a test observe the
    process at the instant the request is in flight, which is where the
    connection question is asked.
    """

    def __init__(
        self,
        *,
        outcome: Exception | None = None,
        upload_outcome: Exception | None = None,
        parked: asyncio.Event | None = None,
        may_return: asyncio.Event | None = None,
    ) -> None:
        self._outcome = outcome
        self._upload_outcome = upload_outcome
        self._parked = parked
        self._may_return = may_return
        self.sends = 0
        self.uploads = 0

    async def _answer(self) -> SentMessage:
        self.sends += 1
        if self._parked is not None:
            self._parked.set()
        if self._may_return is not None:
            await self._may_return.wait()
        if self._outcome is not None:
            raise self._outcome
        return SentMessage(message_id=f"wamid.{uuid.uuid4().hex[:12]}", recipient=None, raw={})

    async def send_text(self, **keywords: Any) -> SentMessage:
        return await self._answer()

    async def send_template(self, **keywords: Any) -> SentMessage:
        return await self._answer()

    async def send_media(self, **keywords: Any) -> SentMessage:
        return await self._answer()

    async def upload_media(self, **keywords: Any) -> str:
        self.uploads += 1
        if self._upload_outcome is not None:
            raise self._upload_outcome
        return f"media-{uuid.uuid4().hex[:10]}"


def _settings(database_url: str) -> Settings:
    """One connection, no overflow, and a short wait.

    A generous pool proves nothing about the second send: it would get a
    connection whether or not the first gave one back. With exactly one, an
    observation of zero checked out while a send is parked at the provider is
    only reachable if the connection was released.
    """
    return Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        database_pool_size=1,
        database_max_overflow=0,
        database_pool_timeout=5,
        jwt_secret="delivery-protocol-secret-not-for-deployment",
        meta_access_token="platform-token-for-this-file",
    )


@pytest_asyncio.fixture
async def one_connection(prepared_database: str) -> AsyncIterator[Database]:
    database = Database(_settings(prepared_database))
    try:
        yield database
    finally:
        await database.dispose()


@pytest_asyncio.fixture
async def workspace(one_connection: Database) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """A committed conversation inside its service window, removed afterwards."""
    factory = one_connection.session_factory
    suffix = uuid.uuid4().hex[:8]
    async with factory() as session:
        tenant = Tenant(name="Delivery", slug=f"delivery-{suffix}")
        session.add(tenant)
        await session.flush()
        account = WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"phone-{suffix}",
            waba_id="555000111",
            display_phone_number="+201000000000",
        )
        contact = Contact(tenant_id=tenant.id, wa_id=f"2010{suffix}")
        session.add_all([account, contact])
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            contact_id=contact.id,
            account_id=account.id,
            last_inbound_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(conversation)
        await session.commit()
        created = (tenant.id, conversation.id)

    try:
        yield created
    finally:
        async with factory() as session:
            await session.execute(delete(Tenant).where(Tenant.id == created[0]))
            await session.commit()


def _messaging(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    provider: Provider,
    database_url: str = "",
) -> MessagingService:
    """The service as it ships, with the provider client replaced.

    The double goes in at `_client`, which is the seam where the real one is
    built from the account's credential - so everything above it, including the
    whole delivery protocol, is the code that runs in production.
    """
    service = MessagingService(
        session=session,
        settings=_settings(database_url),
        tenant_id=tenant_id,
    )

    @asynccontextmanager
    async def client(account: WhatsAppAccount) -> AsyncIterator[Provider]:
        yield provider

    service._client = client  # type: ignore[assignment,method-assign]
    return service


def _pool(database: Database) -> QueuePool:
    pool = database.engine.pool
    assert isinstance(pool, QueuePool)
    return pool


async def _outbound(
    database: Database,
    conversation_id: uuid.UUID,
    *,
    body: str | None = None,
) -> list[Message]:
    """What another transaction can see of this conversation's sends."""
    async with database.session_factory() as session:
        query = select(Message).where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.OUTBOUND,
        )
        if body is not None:
            query = query.where(Message.body == body)
        return list((await session.execute(query)).scalars().all())


# ------------------------------------------------------- the intent is durable


async def test_outbound_send_intent_is_committed_before_meta_call(
    one_connection: Database,
    workspace: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The core invariant: a row naming this send exists before Meta can deliver.

    Observed from a *different* transaction while the send is parked at the
    provider, because "committed" means visible to somebody else and nothing
    less.
    """
    tenant_id, conversation_id = workspace
    parked = asyncio.Event()
    may_return = asyncio.Event()
    provider = Provider(parked=parked, may_return=may_return)

    async def send() -> None:
        async with one_connection.session() as session:
            service = _messaging(session, tenant_id=tenant_id, provider=provider)
            await service.send_text(conversation_id=conversation_id, body="Parked.")

    running = asyncio.create_task(send())
    try:
        await asyncio.wait_for(parked.wait(), timeout=20)

        (intent,) = await _outbound(one_connection, conversation_id)
        assert intent.delivery_state is MessageDeliveryState.REQUESTED
        assert intent.status is MessageStatus.PENDING
        assert intent.wa_message_id is None
    finally:
        may_return.set()
        await running

    (settled,) = await _outbound(one_connection, conversation_id)
    assert settled.delivery_state is MessageDeliveryState.SENT
    assert settled.status is MessageStatus.SENT
    assert settled.wa_message_id is not None


async def test_meta_wait_holds_no_database_connection(
    one_connection: Database,
    workspace: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The pool proof, in the shape ADR-080 gave the agent turn.

    Written to fail if the provider call moves back inside the transaction: on
    a pool of one, a held connection makes this a 1.
    """
    tenant_id, conversation_id = workspace
    parked = asyncio.Event()
    may_return = asyncio.Event()
    provider = Provider(parked=parked, may_return=may_return)

    async def send() -> None:
        async with one_connection.session() as session:
            service = _messaging(session, tenant_id=tenant_id, provider=provider)
            await service.send_text(conversation_id=conversation_id, body="Parked.")

    running = asyncio.create_task(send())
    try:
        await asyncio.wait_for(parked.wait(), timeout=20)
        assert _pool(one_connection).checkedout() == 0
    finally:
        may_return.set()
        await running


async def test_meta_success_then_process_crash_leaves_recoverable_send_state(
    one_connection: Database,
    workspace: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The failure the old order produced, and what the new one leaves instead.

    The worker is killed after Meta accepted and before anything about that
    answer was committed - simulated by rolling the session back, which is what
    a dead process leaves behind. The send must still be findable.
    """
    tenant_id, conversation_id = workspace
    provider = Provider()

    session = one_connection.session_factory()
    service = _messaging(session, tenant_id=tenant_id, provider=provider)
    await service.send_text(conversation_id=conversation_id, body="Your order shipped.")
    await session.rollback()
    await session.close()

    assert provider.sends == 1
    (row,) = await _outbound(one_connection, conversation_id, body="Your order shipped.")
    assert row.delivery_state is MessageDeliveryState.REQUESTED
    assert row.status is MessageStatus.PENDING
    # The outcome is lost, which is the safe direction: the row says a message
    # may have gone out, and nothing will send another on its own initiative.
    assert row.wa_message_id is None


# ----------------------------------------------------------- unknown outcomes


@pytest.mark.parametrize(
    ("failure", "state", "status"),
    [
        (
            UncertainDeliveryError("WhatsApp did not respond in time."),
            MessageDeliveryState.REQUESTED,
            MessageStatus.PENDING,
        ),
        (
            SendNotAttemptedError("WhatsApp rejected the message."),
            MessageDeliveryState.UNDELIVERED,
            MessageStatus.FAILED,
        ),
        (
            RateLimitedError("WhatsApp is rate limiting this account."),
            MessageDeliveryState.UNDELIVERED,
            MessageStatus.FAILED,
        ),
    ],
    ids=["uncertain", "rejected", "rate-limited"],
)
async def test_an_unknown_outcome_is_not_recorded_as_a_failure(
    one_connection: Database,
    workspace: tuple[uuid.UUID, uuid.UUID],
    failure: Exception,
    state: MessageDeliveryState,
    status: MessageStatus,
) -> None:
    """`failed` is a claim about a customer's phone. Only some failures may make it.

    A rejection and a rate limit are Meta reading the request and declining it,
    so nothing was delivered and the row says so. A timeout is not, and calling
    it `failed` is what let the sweeps retry it.
    """
    tenant_id, conversation_id = workspace
    provider = Provider(outcome=failure)

    async with one_connection.session() as session:
        service = _messaging(session, tenant_id=tenant_id, provider=provider)
        message = await service.send_text(conversation_id=conversation_id, body="Hello.")
        assert message.delivery_state is state

    (row,) = await _outbound(one_connection, conversation_id)
    assert row.delivery_state is state
    assert row.status is status
    assert row.delivery_uncertain is (state is MessageDeliveryState.REQUESTED)


async def test_meta_timeout_is_not_blindly_retried(
    one_connection: Database,
    workspace: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A follow-up whose send timed out is finished, not scheduled again.

    Retrying is the behaviour that puts a second copy of the same nudge on
    somebody's phone, and it is what the old code did: every failure arrived as
    `ExternalServiceError` and every one of them was retryable.
    """
    tenant_id, conversation_id = workspace
    provider = Provider(outcome=UncertainDeliveryError("WhatsApp did not respond in time."))

    async with one_connection.session() as session:
        follow_up = FollowUp(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
            status=FollowUpStatus.PENDING,
            body="Still interested?",
        )
        session.add(follow_up)
        await session.flush()

        service = FollowUpService(
            session=session,
            tenant_id=tenant_id,
            messaging=_messaging(session, tenant_id=tenant_id, provider=provider),
        )
        outcome = await service.dispatch(follow_up)

    assert provider.sends == 1
    assert outcome.status is FollowUpStatus.FAILED
    assert follow_up.message_id is not None, "the send it may have made is named on the row"

    # And a second sweep over the same row sends nothing.
    async with one_connection.session() as session:
        reread = await session.get(FollowUp, follow_up.id)
        assert reread is not None
        service = FollowUpService(
            session=session,
            tenant_id=tenant_id,
            messaging=_messaging(session, tenant_id=tenant_id, provider=provider),
        )
        await service.dispatch(reread)

    assert provider.sends == 1


async def test_explicit_meta_rejection_can_follow_existing_retry_policy(
    one_connection: Database,
    workspace: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The companion, so the refusal above is not simply "never retry anything".

    Meta reading the request and declining it means nothing was delivered.
    That is a failure the follow-up policy may try again, and it does.
    """
    tenant_id, conversation_id = workspace
    provider = Provider(outcome=SendNotAttemptedError("WhatsApp rejected the message."))

    async with one_connection.session() as session:
        follow_up = FollowUp(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
            status=FollowUpStatus.PENDING,
            body="Still interested?",
        )
        session.add(follow_up)
        await session.flush()

        service = FollowUpService(
            session=session,
            tenant_id=tenant_id,
            messaging=_messaging(session, tenant_id=tenant_id, provider=provider),
        )
        outcome = await service.dispatch(follow_up)
        assert outcome.status is FollowUpStatus.PENDING
        assert follow_up.attempts == 1
        # Unlinked, because the message it staged is a finished undelivered
        # send and the next attempt makes a new one.
        assert follow_up.message_id is None

        reread = await session.get(FollowUp, follow_up.id)
        assert reread is not None
        reread.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
        await service.dispatch(reread)

    assert provider.sends == 2


# ------------------------------------------------- every kind, one protocol


async def test_template_send_preserves_same_delivery_protocol(
    one_connection: Database,
    workspace: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A template is the send that reaches people outside the service window."""
    tenant_id, conversation_id = workspace
    parked = asyncio.Event()
    may_return = asyncio.Event()
    provider = Provider(parked=parked, may_return=may_return)

    async def send() -> None:
        async with one_connection.session() as session:
            service = _messaging(session, tenant_id=tenant_id, provider=provider)
            await service.send_template(
                conversation_id=conversation_id,
                name="order_update",
                language="en",
            )

    running = asyncio.create_task(send())
    try:
        await asyncio.wait_for(parked.wait(), timeout=20)
        assert _pool(one_connection).checkedout() == 0
        (intent,) = await _outbound(one_connection, conversation_id)
        assert intent.delivery_state is MessageDeliveryState.REQUESTED
    finally:
        may_return.set()
        await running

    (settled,) = await _outbound(one_connection, conversation_id)
    assert settled.delivery_state is MessageDeliveryState.SENT


async def test_outbound_media_send_preserves_same_delivery_protocol(
    one_connection: Database,
    workspace: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A file is two Meta requests, and only the second one can reach anybody."""
    tenant_id, conversation_id = workspace
    parked = asyncio.Event()
    may_return = asyncio.Event()
    provider = Provider(parked=parked, may_return=may_return)

    async def send() -> None:
        async with one_connection.session() as session:
            service = _messaging(session, tenant_id=tenant_id, provider=provider)
            await service.send_media(
                conversation_id=conversation_id,
                content=PIXEL,
                mime_type="image/png",
                caption="Here it is.",
            )

    running = asyncio.create_task(send())
    try:
        await asyncio.wait_for(parked.wait(), timeout=20)
        assert provider.uploads == 1, "the file is uploaded before the send is requested"
        assert _pool(one_connection).checkedout() == 0
        (intent,) = await _outbound(one_connection, conversation_id)
        assert intent.delivery_state is MessageDeliveryState.REQUESTED
    finally:
        may_return.set()
        await running

    (settled,) = await _outbound(one_connection, conversation_id)
    assert settled.delivery_state is MessageDeliveryState.SENT


async def test_a_failed_upload_is_an_undelivered_send_rather_than_an_unknown(
    one_connection: Database,
    workspace: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Why the media path has two phases at all.

    Uploading a file creates a handle and delivers nothing, so a failure there
    is knowably not a delivery. Collapsing it into the send would make every
    failed upload an unresolved message somebody has to read a conversation to
    settle.
    """
    tenant_id, conversation_id = workspace
    provider = Provider(upload_outcome=UncertainDeliveryError("Meta did not answer."))

    async with one_connection.session() as session:
        service = _messaging(session, tenant_id=tenant_id, provider=provider)
        message = await service.send_media(
            conversation_id=conversation_id,
            content=PIXEL,
            mime_type="image/png",
        )

    assert provider.sends == 0
    assert message.delivery_state is MessageDeliveryState.UNDELIVERED
    (row,) = await _outbound(one_connection, conversation_id)
    assert row.status is MessageStatus.FAILED

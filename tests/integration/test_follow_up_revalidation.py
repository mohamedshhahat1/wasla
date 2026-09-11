"""What a follow-up re-reads before it sends, and what it does when it must not.

A follow-up is scheduled and then waits, usually for half an hour and sometimes
for days. Everything it depends on can change in that gap, so the question is
not whether the nudge was right when it was scheduled but whether it is still
right now.

It re-read the conversation's status, the service window, the template and the
account, and neither of the two things that most obviously make a nudge wrong:

**A colleague has taken the conversation over.** Handing a conversation to a
person is the product's documented way to stop the AI, and the follow-up path
does not go through the orchestrator where that is enforced - so it stopped
nothing. A customer mid-conversation with a human received "Still there?" from
the machine, and the colleague had no way to prevent it, because taking the
conversation over *was* the prevention (MSG-05).

**The customer asked not to be marketed at.** The campaign sweep re-reads this
at delivery precisely so somebody who opts out mid-campaign does not receive
the rest of it. The follow-up path never read it (MSG-06).

Every send here goes through a stub that *counts calls*. The assertion that
matters is `sends == 0`, not the follow-up's recorded status: a guard that set
the status correctly after asking Meta to deliver the message would satisfy the
second and fail the customer.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.campaign import OptOutSource
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
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.services.follow_up_service import FollowUpService
from app.services.inbox_service import InboxService
from app.workers.follow_up_worker import FollowUpWorker

pytestmark = pytest.mark.integration


class CountingMessaging:
    """Sends nothing, and remembers how many times it was asked to.

    The count is the assertion. A follow-up that reaches this stub would have
    reached Meta in production, and the customer would have the message.
    """

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self.sends = 0

    def window_open(self, conversation: Conversation) -> bool:
        return True

    async def send_text(
        self,
        *,
        conversation_id: uuid.UUID,
        body: str,
        **kwargs: Any,
    ) -> Message:
        self.sends += 1
        message = Message(
            tenant_id=self._tenant_id,
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            kind=MessageKind.TEXT,
            status=MessageStatus.SENT,
            origin=MessageOrigin.AGENT,
        )
        self._session.add(message)
        await self._session.flush()
        return message

    async def send_template(self, **kwargs: Any) -> Message:  # pragma: no cover
        raise AssertionError("These follow-ups are inside the service window.")


class SessionHandle:
    """Hands the worker the test's own session, so its writes roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


async def _scenario(
    session: AsyncSession,
    *,
    mode: ConversationMode = ConversationMode.AI,
    opted_out: bool = False,
) -> tuple[Tenant, Conversation, FollowUp]:
    """A due follow-up on an open conversation inside the service window."""
    slug = f"revalidate-{uuid.uuid4().hex[:8]}"
    tenant = Tenant(name="Revalidate", slug=slug)
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{slug}",
        waba_id="555000222",
        display_phone_number="+201000000001",
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    contact = Contact(
        tenant_id=tenant.id,
        wa_id=f"2015{uuid.uuid4().int % 10_000_000:07d}",
        marketing_opt_out_at=datetime.now(UTC) if opted_out else None,
        opt_out_source=OptOutSource.CUSTOMER if opted_out else None,
    )
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        status=ConversationStatus.OPEN,
        mode=mode,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(conversation)
    await session.flush()

    follow_up = FollowUp(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=5),
        body="Still there?",
    )
    session.add(follow_up)
    await session.flush()
    return tenant, conversation, follow_up


def _service(
    session: AsyncSession,
    tenant: Tenant,
    messaging: CountingMessaging,
) -> FollowUpService:
    return FollowUpService(session=session, tenant_id=tenant.id, messaging=messaging)  # type: ignore[arg-type]


async def test_a_follow_up_on_a_human_owned_conversation_sends_nothing(
    db_session: AsyncSession,
) -> None:
    """The guard at dispatch, which is the one that survives the race.

    `InboxService.set_mode` cancels pending nudges at handover, and that covers
    the ordinary case - but the claim commits, so a handover landing between
    the sweep claiming this row and the send leaving cannot be caught there.
    This is the guard that catches it.
    """
    tenant, conversation, follow_up = await _scenario(db_session, mode=ConversationMode.HUMAN)
    messaging = CountingMessaging(db_session, tenant.id)

    outcome = await _service(db_session, tenant, messaging).dispatch(follow_up)

    assert messaging.sends == 0
    assert outcome.status is FollowUpStatus.SKIPPED
    assert follow_up.status is FollowUpStatus.SKIPPED
    # Skipped rather than failed: the conversation does not stop being
    # human-owned by itself, so retrying would queue a message that can never
    # legally go out.
    assert follow_up.attempts == 0
    assert conversation.mode is ConversationMode.HUMAN


async def test_a_follow_up_to_an_opted_out_customer_sends_nothing(
    db_session: AsyncSession,
) -> None:
    """Opt-out covers follow-ups, and this is where that decision is written.

    The codebase applies opt-out unevenly on purpose: a campaign honours it, an
    AI reply does not, and both are right - refusing marketing is not refusing
    an answer to your own question. A follow-up is filed with the campaign,
    because nobody asked for it and it arrives precisely because the
    conversation has gone quiet.
    """
    tenant, conversation, follow_up = await _scenario(db_session, opted_out=True)
    messaging = CountingMessaging(db_session, tenant.id)

    outcome = await _service(db_session, tenant, messaging).dispatch(follow_up)

    assert messaging.sends == 0
    assert outcome.status is FollowUpStatus.SKIPPED
    assert follow_up.attempts == 0


async def test_an_ordinary_follow_up_still_goes_out(db_session: AsyncSession) -> None:
    """The negative control, and it is not optional.

    Both guards above are satisfiable by never sending anything. This is what
    says the feature still works.
    """
    tenant, _, follow_up = await _scenario(db_session)
    messaging = CountingMessaging(db_session, tenant.id)

    outcome = await _service(db_session, tenant, messaging).dispatch(follow_up)

    assert messaging.sends == 1
    assert outcome.status is FollowUpStatus.SENT


async def test_taking_a_conversation_over_cancels_the_nudge_waiting_on_it(
    db_session: AsyncSession,
) -> None:
    """The first of the two layers, at the moment the colleague acts.

    Cancelled rather than left to be declined later, so the pending nudge stops
    existing and the colleague can see it will not arrive.
    """
    tenant, conversation, follow_up = await _scenario(db_session)

    await InboxService(session=db_session, tenant_id=tenant.id).set_mode(
        conversation_id=conversation.id,
        mode=ConversationMode.HUMAN,
        handoff_reason="Taking this one.",
    )
    await db_session.flush()
    await db_session.refresh(follow_up)

    assert follow_up.status is FollowUpStatus.CANCELLED
    assert follow_up.cancelled_reason == "A colleague took the conversation over."


async def test_handing_back_to_the_ai_does_not_cancel_anything(
    db_session: AsyncSession,
) -> None:
    """Only the handover cancels. Resuming the AI is not a reason to stop a nudge.

    Without this, `set_mode` cancelling unconditionally would quietly delete a
    follow-up every time somebody handed a conversation back, which is the
    opposite of what resuming the AI means.
    """
    tenant, conversation, follow_up = await _scenario(db_session, mode=ConversationMode.HUMAN)

    await InboxService(session=db_session, tenant_id=tenant.id).set_mode(
        conversation_id=conversation.id,
        mode=ConversationMode.AI,
    )
    await db_session.flush()
    await db_session.refresh(follow_up)

    assert follow_up.status is FollowUpStatus.PENDING


async def test_the_sweep_sends_nothing_when_the_takeover_lands_after_the_claim(
    db_session: AsyncSession,
) -> None:
    """The race, driven through the real worker rather than the service.

    The claim commits its lease and releases its lock, so between the sweep
    picking this row up and the send leaving there is a window in which a
    colleague can take the conversation over. The dispatch-time re-read is what
    closes it, and this drives the whole loop to prove the re-read is on the
    path the worker actually takes.
    """
    tenant, conversation, follow_up = await _scenario(db_session)
    stubs: list[CountingMessaging] = []

    def factory(session: AsyncSession, tenant_id: uuid.UUID) -> CountingMessaging:
        stub = CountingMessaging(session, tenant_id)
        stubs.append(stub)
        return stub

    # The takeover happens after the row became due and before the sweep runs,
    # which is the same ordering the race produces and is the only part of it a
    # test can make deterministic.
    conversation.mode = ConversationMode.HUMAN
    await db_session.flush()

    worker = FollowUpWorker(
        database=SessionHandle(db_session),  # type: ignore[arg-type]
        settings=object(),  # type: ignore[arg-type]
        messaging_factory=factory,  # type: ignore[arg-type]
    )
    handled = await worker.run_once()
    # Flushed before the re-read: the worker writes through this same session
    # and `refresh` issues its own SELECT, so an unflushed change would be
    # invisible and this test would pass for the wrong reason.
    await db_session.flush()
    await db_session.refresh(follow_up)

    # The sweep genuinely picked this row up, so `sends == 0` below is the
    # guard refusing rather than the claim finding nothing.
    assert handled == 1
    assert sum(stub.sends for stub in stubs) == 0
    assert follow_up.status is FollowUpStatus.SKIPPED

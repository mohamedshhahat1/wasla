"""Follow-ups against real PostgreSQL.

The properties here cannot be checked without the database. `SKIP LOCKED` is the
whole concurrency story and only PostgreSQL implements it. The one-pending-nudge
rule is a partial unique index rather than a service check. And whether a reply
actually cancels a scheduled message depends on rows, not on intent.

Sending is driven through a stub messaging service rather than Meta: what is
under test is which branch the compliance logic takes, not whether httpx works.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ExternalServiceError, TenantIsolationError, ValidationError
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDeliveryState,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.follow_up import MAX_ATTEMPTS, FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.follow_up_repository import DueFollowUpClaim, FollowUpRepository
from app.schemas.follow_up import FollowUpRead
from app.services.follow_up_service import FollowUpService

pytestmark = pytest.mark.integration

SOON = timedelta(minutes=30)


async def _tenant(session: AsyncSession, *, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _conversation(
    session: AsyncSession,
    *,
    tenant: Tenant,
    wa_id: str = "201000000001",
    last_inbound_at: datetime | None = None,
) -> Conversation:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{tenant.slug}-{wa_id[-4:]}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant.id, wa_id=wa_id)
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        status=ConversationStatus.OPEN,
        mode=ConversationMode.AI,
        last_inbound_at=last_inbound_at,
    )
    session.add(conversation)
    await session.flush()
    return conversation


class StubMessaging:
    """Stands in for MessagingService, recording which path was taken.

    `window_open` is answered from the conversation exactly as the real service
    does, because that decision is the thing under test.
    """

    def __init__(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, outcome: str = "sent"
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self.outcome = outcome
        self.texts: list[str] = []
        self.templates: list[tuple[str, str]] = []

    def window_open(self, conversation: Conversation) -> bool:
        from app.services.messaging_service import SERVICE_WINDOW

        if conversation.last_inbound_at is None:
            return False
        return datetime.now(UTC) - conversation.last_inbound_at <= SERVICE_WINDOW

    async def _record(
        self,
        conversation_id: uuid.UUID,
        *,
        kind: MessageKind,
        link: Callable[[Message], None] | None = None,
    ) -> Message:
        """The real protocol's shape, minus the commits (ADR-093).

        `link` is honoured rather than ignored, because the real service calls
        it inside the transaction that commits the send intent - and a stub
        that dropped it would leave the follow-up looking untouched in exactly
        the state the guard exists for.

        `"uncertain"` is Meta not answering: `PENDING` and `REQUESTED`, which is
        what a read timeout leaves behind.
        """
        uncertain = self.outcome == "uncertain"
        rejected = self.outcome == "rejected"
        message = Message(
            tenant_id=self._tenant_id,
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            kind=kind,
            status=(
                MessageStatus.PENDING
                if uncertain
                else MessageStatus.FAILED
                if rejected
                else MessageStatus.SENT
            ),
            delivery_state=(
                MessageDeliveryState.REQUESTED
                if uncertain
                else MessageDeliveryState.UNDELIVERED
                if rejected
                else MessageDeliveryState.SENT
            ),
            failure_reason="Meta said no." if rejected else None,
        )
        self._session.add(message)
        await self._session.flush()
        if link is not None:
            link(message)
        return message

    async def send_text(
        self,
        *,
        conversation_id: uuid.UUID,
        body: str,
        link: Callable[[Message], None] | None = None,
        **kwargs: Any,
    ) -> Message:
        if self.outcome == "raise":
            raise ExternalServiceError("The network went away.")
        self.texts.append(body)
        return await self._record(conversation_id, kind=MessageKind.TEXT, link=link)

    async def send_template(
        self,
        *,
        conversation_id: uuid.UUID,
        name: str,
        language: str,
        components: Sequence[Any] | None = None,
        link: Callable[[Message], None] | None = None,
    ) -> Message:
        if self.outcome == "raise":
            raise ExternalServiceError("The network went away.")
        self.templates.append((name, language))
        return await self._record(conversation_id, kind=MessageKind.TEMPLATE, link=link)


def _service(
    session: AsyncSession, tenant: Tenant, *, messaging: StubMessaging | None = None
) -> FollowUpService:
    """A service with its sending collaborator injected.

    The stub goes in through the constructor rather than by patching a module
    attribute, so what runs here is the service exactly as it ships.
    """
    return FollowUpService(
        session=session,
        tenant_id=tenant.id,
        messaging=messaging,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------- tenant isolation


async def test_one_workspace_cannot_read_another_workspaces_follow_up(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    rival = await _tenant(db_session, slug="rival")
    conversation = await _conversation(db_session, tenant=acme)

    follow_up = await FollowUpService(session=db_session, tenant_id=acme.id).schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()

    with pytest.raises(TenantIsolationError):
        await FollowUpService(session=db_session, tenant_id=rival.id).get(follow_up.id)


async def test_a_follow_up_list_never_crosses_workspaces(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    rival = await _tenant(db_session, slug="rival")
    acme_conversation = await _conversation(db_session, tenant=acme, wa_id="201000000011")
    rival_conversation = await _conversation(db_session, tenant=rival, wa_id="201000000012")

    await FollowUpService(session=db_session, tenant_id=acme.id).schedule(
        conversation_id=acme_conversation.id,
        delay=SOON,
        body="Acme nudge",
    )
    await FollowUpService(session=db_session, tenant_id=rival.id).schedule(
        conversation_id=rival_conversation.id,
        delay=SOON,
        body="Rival nudge",
    )
    await db_session.flush()

    page = await FollowUpService(session=db_session, tenant_id=acme.id).list_follow_ups()

    assert [row.body for row in page.items] == ["Acme nudge"]


async def test_a_follow_up_cannot_be_scheduled_on_another_workspaces_conversation(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    rival = await _tenant(db_session, slug="rival")
    conversation = await _conversation(db_session, tenant=acme)
    await db_session.flush()

    with pytest.raises(TenantIsolationError):
        await FollowUpService(session=db_session, tenant_id=rival.id).schedule(
            conversation_id=conversation.id,
            delay=SOON,
            body="Should not land.",
        )


# ----------------------------------------------------------- one pending per conversation


async def test_scheduling_twice_reschedules_rather_than_queueing_a_second(
    db_session: AsyncSession,
) -> None:
    """Otherwise an agent that schedules every turn stacks up notifications."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)
    service = FollowUpService(session=db_session, tenant_id=acme.id)

    first = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Tomorrow?",
    )
    second = await service.schedule(
        conversation_id=conversation.id,
        delay=timedelta(days=7),
        body="Next week?",
    )
    await db_session.flush()

    assert first.id == second.id
    assert second.body == "Next week?"
    assert second.scheduled_at > first.created_at + timedelta(days=6)

    page = await service.list_follow_ups()
    assert len(page.items) == 1


async def test_the_database_itself_refuses_a_second_pending_follow_up(
    db_session: AsyncSession,
) -> None:
    """The service check is a courtesy; this index is the guarantee."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)
    when = datetime.now(UTC) + SOON

    db_session.add_all(
        [
            FollowUp(
                tenant_id=acme.id,
                conversation_id=conversation.id,
                scheduled_at=when,
                body="One",
            ),
            FollowUp(
                tenant_id=acme.id,
                conversation_id=conversation.id,
                scheduled_at=when,
                body="Two",
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_a_conversation_can_be_followed_up_again_after_the_first_is_done(
    db_session: AsyncSession,
) -> None:
    """Partial index: a finished nudge releases the slot."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)
    service = FollowUpService(session=db_session, tenant_id=acme.id)

    first = await service.schedule(conversation_id=conversation.id, delay=SOON, body="One")
    await db_session.flush()
    await service.cancel(follow_up_id=first.id)
    await db_session.flush()

    second = await service.schedule(conversation_id=conversation.id, delay=SOON, body="Two")
    await db_session.flush()

    assert second.id != first.id
    assert first.status is FollowUpStatus.CANCELLED


# ------------------------------------------------------------------- scheduling rules


async def test_a_follow_up_needs_something_to_send(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)

    with pytest.raises(ValidationError, match="message to send"):
        await FollowUpService(session=db_session, tenant_id=acme.id).schedule(
            conversation_id=conversation.id,
            delay=SOON,
        )


@pytest.mark.parametrize("delay", [timedelta(seconds=5), timedelta(days=400)])
async def test_a_delay_outside_the_bounds_is_refused(
    db_session: AsyncSession,
    delay: timedelta,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)

    with pytest.raises(ValidationError):
        await FollowUpService(session=db_session, tenant_id=acme.id).schedule(
            conversation_id=conversation.id,
            delay=delay,
            body="Still there?",
        )


async def test_a_closed_conversation_cannot_be_followed_up(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)
    conversation.status = ConversationStatus.CLOSED
    await db_session.flush()

    with pytest.raises(ValidationError, match="closed"):
        await FollowUpService(session=db_session, tenant_id=acme.id).schedule(
            conversation_id=conversation.id,
            delay=SOON,
            body="Still there?",
        )


async def test_an_absolute_time_is_accepted_and_stored(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)
    when = datetime.now(UTC) + timedelta(days=2)

    follow_up = await FollowUpService(session=db_session, tenant_id=acme.id).schedule(
        conversation_id=conversation.id,
        scheduled_at=when,
        body="Tuesday nudge",
    )
    await db_session.flush()

    assert follow_up.scheduled_at == when


# --------------------------------------------------------------- cancellation on reply


async def test_a_customer_reply_cancels_the_waiting_nudge(db_session: AsyncSession) -> None:
    """The nudge existed because they went quiet; they have stopped being quiet."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)
    service = FollowUpService(session=db_session, tenant_id=acme.id)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()

    cancelled = await service.cancel_for_conversation(conversation_id=conversation.id)
    await db_session.flush()

    assert cancelled == 1
    assert follow_up.status is FollowUpStatus.CANCELLED
    assert follow_up.cancelled_at is not None
    assert follow_up.cancelled_reason == "The customer replied."


async def test_cancelling_a_conversation_with_nothing_waiting_is_harmless(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)

    cancelled = await FollowUpService(
        session=db_session, tenant_id=acme.id
    ).cancel_for_conversation(conversation_id=conversation.id)

    assert cancelled == 0


async def test_cancelling_an_already_sent_follow_up_changes_nothing(
    db_session: AsyncSession,
) -> None:
    """Losing that race is not the caller's mistake, and their intent holds."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)
    service = FollowUpService(session=db_session, tenant_id=acme.id)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()
    follow_up.status = FollowUpStatus.SENT
    await db_session.flush()

    result = await service.cancel(follow_up_id=follow_up.id)

    assert result.status is FollowUpStatus.SENT
    assert result.cancelled_at is None


async def test_cancellation_does_not_reach_another_workspace(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    rival = await _tenant(db_session, slug="rival")
    conversation = await _conversation(db_session, tenant=acme)

    follow_up = await FollowUpService(session=db_session, tenant_id=acme.id).schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()

    cancelled = await FollowUpService(
        session=db_session, tenant_id=rival.id
    ).cancel_for_conversation(conversation_id=conversation.id)
    await db_session.flush()

    assert cancelled == 0
    assert follow_up.status is FollowUpStatus.PENDING


# -------------------------------------------------------------------- due claiming


async def test_only_follow_ups_whose_time_has_come_are_claimed(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    due_conversation = await _conversation(db_session, tenant=acme, wa_id="201000000021")
    later_conversation = await _conversation(db_session, tenant=acme, wa_id="201000000022")
    now = datetime.now(UTC)

    db_session.add_all(
        [
            FollowUp(
                tenant_id=acme.id,
                conversation_id=due_conversation.id,
                scheduled_at=now - timedelta(minutes=1),
                body="Due",
            ),
            FollowUp(
                tenant_id=acme.id,
                conversation_id=later_conversation.id,
                scheduled_at=now + timedelta(hours=2),
                body="Later",
            ),
        ]
    )
    await db_session.flush()

    claimed = await DueFollowUpClaim(db_session).claim_due(now=now)

    assert [row.body for row in claimed] == ["Due"]


async def test_finished_follow_ups_are_never_claimed(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    now = datetime.now(UTC)

    for index, status in enumerate(
        [
            FollowUpStatus.SENT,
            FollowUpStatus.CANCELLED,
            FollowUpStatus.FAILED,
            FollowUpStatus.SKIPPED,
        ]
    ):
        conversation = await _conversation(db_session, tenant=acme, wa_id=f"20100000003{index}")
        db_session.add(
            FollowUp(
                tenant_id=acme.id,
                conversation_id=conversation.id,
                scheduled_at=now - timedelta(minutes=5),
                status=status,
                body=status.value,
            )
        )
    await db_session.flush()

    assert await DueFollowUpClaim(db_session).claim_due(now=now) == []


async def test_the_claim_crosses_workspaces_because_the_worker_does(
    db_session: AsyncSession,
) -> None:
    """The one unscoped query in the codebase, and this is why it exists."""
    acme = await _tenant(db_session, slug="acme")
    rival = await _tenant(db_session, slug="rival")
    now = datetime.now(UTC)

    for tenant in (acme, rival):
        conversation = await _conversation(db_session, tenant=tenant, wa_id="201000000041")
        db_session.add(
            FollowUp(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                scheduled_at=now - timedelta(minutes=1),
                body=f"{tenant.slug} nudge",
            )
        )
    await db_session.flush()

    claimed = await DueFollowUpClaim(db_session).claim_due(now=now)

    assert {row.tenant_id for row in claimed} == {acme.id, rival.id}


async def test_a_claim_is_bounded(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    now = datetime.now(UTC)

    for index in range(5):
        conversation = await _conversation(db_session, tenant=acme, wa_id=f"20100000005{index}")
        db_session.add(
            FollowUp(
                tenant_id=acme.id,
                conversation_id=conversation.id,
                scheduled_at=now - timedelta(minutes=index + 1),
                body=f"Nudge {index}",
            )
        )
    await db_session.flush()

    claimed = await DueFollowUpClaim(db_session).claim_due(now=now, limit=2)

    assert len(claimed) == 2


async def test_the_claim_takes_the_oldest_first(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    now = datetime.now(UTC)

    for index in range(3):
        conversation = await _conversation(db_session, tenant=acme, wa_id=f"20100000006{index}")
        db_session.add(
            FollowUp(
                tenant_id=acme.id,
                conversation_id=conversation.id,
                scheduled_at=now - timedelta(hours=index + 1),
                body=f"Nudge {index}",
            )
        )
    await db_session.flush()

    claimed = await DueFollowUpClaim(db_session).claim_due(now=now, limit=1)

    # Nudge 2 is three hours overdue; it waited longest.
    assert claimed[0].body == "Nudge 2"


# ------------------------------------------------------- window and template compliance


async def test_inside_the_window_a_follow_up_sends_free_text(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(
        db_session,
        tenant=acme,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=1),
    )
    messaging = StubMessaging(db_session, tenant_id=acme.id)
    service = _service(db_session, acme, messaging=messaging)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()

    outcome = await service.dispatch(follow_up)

    assert outcome.status is FollowUpStatus.SENT
    assert messaging.texts == ["Still there?"]
    assert messaging.templates == []
    assert follow_up.message_id is not None


async def test_outside_the_window_a_follow_up_uses_its_template(db_session: AsyncSession) -> None:
    """Meta accepts approved templates only once the 24 hours have passed."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(
        db_session,
        tenant=acme,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=30),
    )
    messaging = StubMessaging(db_session, tenant_id=acme.id)
    service = _service(db_session, acme, messaging=messaging)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
        template_name="gentle_nudge",
        template_language="ar",
    )
    await db_session.flush()

    outcome = await service.dispatch(follow_up)

    assert outcome.status is FollowUpStatus.SENT
    assert messaging.templates == [("gentle_nudge", "ar")]
    assert messaging.texts == []


async def test_outside_the_window_without_a_template_it_is_skipped(
    db_session: AsyncSession,
) -> None:
    """Not sent, and not a failure: sending would breach WhatsApp's rules."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(
        db_session,
        tenant=acme,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=30),
    )
    messaging = StubMessaging(db_session, tenant_id=acme.id)
    service = _service(db_session, acme, messaging=messaging)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()

    outcome = await service.dispatch(follow_up)

    assert outcome.status is FollowUpStatus.SKIPPED
    assert messaging.texts == []
    assert messaging.templates == []
    # The reason is on the row, so a workspace can see why nothing went out.
    assert follow_up.last_error is not None
    assert "service window" in follow_up.last_error


async def test_a_conversation_the_customer_never_wrote_in_has_no_window(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme, last_inbound_at=None)
    messaging = StubMessaging(db_session, tenant_id=acme.id)
    service = _service(db_session, acme, messaging=messaging)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()

    outcome = await service.dispatch(follow_up)

    assert outcome.status is FollowUpStatus.SKIPPED


async def test_a_conversation_closed_before_the_due_time_is_skipped(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(
        db_session,
        tenant=acme,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=1),
    )
    messaging = StubMessaging(db_session, tenant_id=acme.id)
    service = _service(db_session, acme, messaging=messaging)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()
    conversation.status = ConversationStatus.CLOSED
    await db_session.flush()

    outcome = await service.dispatch(follow_up)

    assert outcome.status is FollowUpStatus.SKIPPED
    assert messaging.texts == []


async def test_a_skipped_follow_up_is_not_retried(db_session: AsyncSession) -> None:
    """Terminal on purpose: the window does not reopen on its own."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(
        db_session,
        tenant=acme,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=30),
    )
    messaging = StubMessaging(db_session, tenant_id=acme.id)
    service = _service(db_session, acme, messaging=messaging)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()
    await service.dispatch(follow_up)
    await db_session.flush()

    assert await DueFollowUpClaim(db_session).claim_due(now=datetime.now(UTC)) == []


# ------------------------------------------------------------------ failure handling


async def test_a_rejected_send_is_retried_until_the_attempts_run_out(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(
        db_session,
        tenant=acme,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=1),
    )
    messaging = StubMessaging(db_session, tenant_id=acme.id, outcome="rejected")
    service = _service(db_session, acme, messaging=messaging)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()

    for attempt in range(1, MAX_ATTEMPTS):
        outcome = await service.dispatch(follow_up)
        assert outcome.status is FollowUpStatus.PENDING
        assert follow_up.attempts == attempt
        # Pushed out, or the next sweep would burn every attempt at once.
        assert follow_up.scheduled_at > datetime.now(UTC)

    final = await service.dispatch(follow_up)

    assert final.status is FollowUpStatus.FAILED
    assert follow_up.attempts == MAX_ATTEMPTS
    assert follow_up.last_error is not None


async def test_a_network_failure_is_recorded_rather_than_raised(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(
        db_session,
        tenant=acme,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=1),
    )
    messaging = StubMessaging(db_session, tenant_id=acme.id, outcome="raise")
    service = _service(db_session, acme, messaging=messaging)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()

    outcome = await service.dispatch(follow_up)

    assert outcome.status is FollowUpStatus.PENDING
    assert follow_up.attempts == 1
    assert follow_up.last_error is not None


async def test_dispatching_something_already_finished_does_nothing(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(
        db_session,
        tenant=acme,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=1),
    )
    messaging = StubMessaging(db_session, tenant_id=acme.id)
    service = _service(db_session, acme, messaging=messaging)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()
    await service.cancel(follow_up_id=follow_up.id)
    await db_session.flush()

    outcome = await service.dispatch(follow_up)

    assert outcome.status is FollowUpStatus.CANCELLED
    assert messaging.texts == []


# --------------------------------------------------------------------- attribution


async def test_a_follow_up_records_who_scheduled_it(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)

    follow_up = await FollowUpService(session=db_session, tenant_id=acme.id).schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
        reason="The customer said they would think about it.",
        created_by_kind=ActorKind.AGENT,
    )
    await db_session.flush()

    assert follow_up.created_by_kind is ActorKind.AGENT
    assert follow_up.created_by_id is None
    assert follow_up.reason == "The customer said they would think about it."


async def test_pagination_walks_every_follow_up_exactly_once(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    service = FollowUpService(session=db_session, tenant_id=acme.id)

    for index in range(7):
        conversation = await _conversation(db_session, tenant=acme, wa_id=f"20100000007{index}")
        await service.schedule(
            conversation_id=conversation.id,
            delay=timedelta(minutes=30 + index),
            body=f"Nudge {index}",
        )
    await db_session.flush()

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    while True:
        page = await service.list_follow_ups(limit=3, cursor=cursor)
        seen.extend(row.id for row in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert len(seen) == 7
    assert len(set(seen)) == 7


async def test_a_status_filter_narrows_the_list(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    service = FollowUpService(session=db_session, tenant_id=acme.id)

    kept = await _conversation(db_session, tenant=acme, wa_id="201000000081")
    dropped = await _conversation(db_session, tenant=acme, wa_id="201000000082")
    await service.schedule(conversation_id=kept.id, delay=SOON, body="Kept")
    cancelled = await service.schedule(conversation_id=dropped.id, delay=SOON, body="Cancelled")
    await db_session.flush()
    await service.cancel(follow_up_id=cancelled.id)
    await db_session.flush()

    page = await service.list_follow_ups(statuses=(FollowUpStatus.PENDING,))

    assert [row.body for row in page.items] == ["Kept"]


async def test_the_repository_finds_the_pending_nudge_for_a_conversation(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme)
    service = FollowUpService(session=db_session, tenant_id=acme.id)

    scheduled = await service.schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()

    found = await FollowUpRepository(db_session, tenant_id=acme.id).get_pending_for_conversation(
        conversation.id
    )

    assert found is not None
    assert found.id == scheduled.id


# -------------------------------------------------- cancellation through the webhook


async def test_an_inbound_webhook_cancels_the_waiting_nudge(db_session: AsyncSession) -> None:
    """The whole path, not just the service.

    Cancellation lives on the inbound path so that a reply stops the nudge
    before any sweep can send it. Driving it through `WhatsAppIngestionService`
    is what proves the wiring exists, rather than only the method that does it.
    """
    from app.services.whatsapp_service import WhatsAppIngestionService

    acme = await _tenant(db_session, slug="acme")
    account = WhatsAppAccount(
        tenant_id=acme.id,
        phone_number_id="phone-acme-inbound",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=acme.id, wa_id="201555000111")
    db_session.add_all([account, contact])
    await db_session.flush()

    conversation = Conversation(
        tenant_id=acme.id,
        contact_id=contact.id,
        account_id=account.id,
        status=ConversationStatus.OPEN,
        mode=ConversationMode.AI,
    )
    db_session.add(conversation)
    await db_session.flush()

    follow_up = await FollowUpService(session=db_session, tenant_id=acme.id).schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()
    assert follow_up.status is FollowUpStatus.PENDING

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-acme-inbound"},
                            "contacts": [{"wa_id": "201555000111", "profile": {"name": "Ahmed"}}],
                            "messages": [
                                {
                                    "id": "wamid.reply.1",
                                    "from": "201555000111",
                                    "type": "text",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "text": {"body": "Yes, still interested."},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }

    outcome = await WhatsAppIngestionService(session=db_session).ingest(payload)
    await db_session.flush()

    assert outcome.cancelled_follow_ups == 1
    status: FollowUpStatus = follow_up.status
    assert status is FollowUpStatus.CANCELLED
    assert follow_up.cancelled_reason == "The customer replied."


async def test_a_delivery_status_does_not_cancel_a_nudge(db_session: AsyncSession) -> None:
    """Only the customer speaking counts. Our own message being delivered is not
    a reply, and treating it as one would cancel every follow-up we scheduled.
    """
    from app.services.whatsapp_service import WhatsAppIngestionService

    acme = await _tenant(db_session, slug="acme")
    account = WhatsAppAccount(
        tenant_id=acme.id,
        phone_number_id="phone-acme-status",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=acme.id, wa_id="201555000222")
    db_session.add_all([account, contact])
    await db_session.flush()

    conversation = Conversation(
        tenant_id=acme.id,
        contact_id=contact.id,
        account_id=account.id,
        status=ConversationStatus.OPEN,
        mode=ConversationMode.AI,
    )
    db_session.add(conversation)
    await db_session.flush()

    follow_up = await FollowUpService(session=db_session, tenant_id=acme.id).schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Still there?",
    )
    await db_session.flush()

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-acme-status"},
                            "statuses": [
                                {
                                    "id": "wamid.out.1",
                                    "status": "delivered",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "recipient_id": "201555000222",
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }

    outcome = await WhatsAppIngestionService(session=db_session).ingest(payload)
    await db_session.flush()

    assert outcome.cancelled_follow_ups == 0
    assert follow_up.status is FollowUpStatus.PENDING


async def test_a_new_follow_up_can_be_serialised_without_a_further_flush(
    db_session: AsyncSession,
) -> None:
    """The 500 a stubbed endpoint test cannot see.

    `POST /api/v1/follow-ups` returns the row the service just staged, and the
    request commits afterwards — so the primary key default and the
    server-default timestamps must already be there. Building the response
    schema here is exactly what the route does.
    """
    tenant = await _tenant(db_session, slug="serialisable-nudge")
    conversation = await _conversation(db_session, tenant=tenant, wa_id="201000000042")

    follow_up = await _service(db_session, tenant).schedule(
        conversation_id=conversation.id,
        delay=SOON,
        body="Just checking in.",
    )

    read = FollowUpRead.from_model(follow_up)
    assert read.id is not None
    assert read.created_at is not None
    assert read.updated_at is not None

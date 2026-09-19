"""A colleague rescheduling or cancelling a follow-up the sweep is sending (CRM-09, CRM-10).

The sweep claims due rows, commits the claim, and then re-takes and sends each
row in a transaction of its own; the send commits its intent before Meta is
asked (ADR-093). A colleague's reschedule or cancel can land anywhere in that
sequence, and the audit found two of those places wrong:

- a reschedule inside the lease was overwritten by it, and the nudge went out
  *now* with the rescheduled text (CRM-09);
- a cancel racing the send answered "cancelled", and the row ended `CANCELLED`
  naming a message the customer had received (CRM-10).

Every test drives the real worker's claim and re-take against committed rows,
with a messaging stand-in that behaves like the real one where it matters: it
stages the message, lets the follow-up link it, **commits** - the send intent -
and only then "asks Meta", at a point the test controls. `sends` counts the
messages that reached "Meta", which is what the customer would have.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.db.models.conversation import (
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind
from app.repositories.follow_up_repository import DueFollowUpClaim
from app.services.follow_up_service import DISPATCH_IN_PROGRESS, FollowUpService
from app.workers.follow_up_worker import FollowUpWorker
from tests.integration.crm_harness import CrmWorld, World

pytestmark = pytest.mark.integration

THURSDAY = timedelta(days=3)


class GatedMessaging:
    """Sends like `MessagingService`, with the moments between steps held open.

    `hold_before_intent` keeps the re-take's row lock (the intent is not yet
    committed); `hold_before_meta` keeps the send between its committed intent
    and Meta's answer. `sends` is what reached Meta.
    """

    def __init__(self) -> None:
        self.hold_before_intent = asyncio.Event()
        self.hold_before_meta = asyncio.Event()
        self.hold_before_intent.set()
        self.hold_before_meta.set()
        self.reached_intent = asyncio.Event()
        self.intent_committed = asyncio.Event()
        self.sends = 0
        self.bodies: list[str] = []

    def factory(self, session: AsyncSession, tenant_id: uuid.UUID) -> _Bound:
        return _Bound(self, session, tenant_id)


class _Bound:
    def __init__(self, gate: GatedMessaging, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._gate = gate
        self._session = session
        self._tenant_id = tenant_id

    def window_open(self, conversation: Conversation) -> bool:
        return True

    async def send_text(
        self,
        *,
        conversation_id: uuid.UUID,
        body: str,
        link: Any = None,
        **_: Any,
    ) -> Message:
        message = Message(
            tenant_id=self._tenant_id,
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            kind=MessageKind.TEXT,
            status=MessageStatus.PENDING,
            origin=MessageOrigin.FOLLOW_UP,
            body=body,
        )
        self._session.add(message)
        await self._session.flush()
        if link is not None:
            link(message)
        self._gate.reached_intent.set()
        await asyncio.wait_for(self._gate.hold_before_intent.wait(), timeout=20)
        # The send intent, committed before Meta is asked - which is what ends
        # the re-take's row lock, exactly as `released` does in production.
        await self._session.commit()
        self._gate.intent_committed.set()
        await asyncio.wait_for(self._gate.hold_before_meta.wait(), timeout=20)
        self._gate.sends += 1
        self._gate.bodies.append(body)
        message.status = MessageStatus.SENT
        message.wa_message_id = f"wamid.{uuid.uuid4().hex[:12]}"
        message.sent_at = datetime.now(UTC)
        await self._session.flush()
        return message

    async def send_template(self, **_: Any) -> Message:  # pragma: no cover
        raise AssertionError("inside the service window")


async def _due(crm: CrmWorld, world: World, *, kind: ActorKind = ActorKind.USER) -> uuid.UUID:
    async with crm.session() as session:
        follow_up = FollowUp(
            tenant_id=world.tenant_id,
            conversation_id=world.conversation_id,
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
            body="Still interested?",
            created_by_kind=kind,
            created_by_id=world.alice.id if kind is ActorKind.USER else None,
        )
        session.add(follow_up)
        await session.commit()
        return follow_up.id


async def _claim(crm: CrmWorld) -> dict[uuid.UUID, uuid.UUID | None]:
    """The sweep's TX1: claim what is due and commit the claim."""
    async with crm.session() as session:
        claimed = await DueFollowUpClaim(session).claim_due(
            now=datetime.now(UTC),
            lease_until=datetime.now(UTC) + timedelta(minutes=5),
        )
        tokens = {row.id: row.claim_token for row in claimed}
        await session.commit()
    return tokens


def _worker(crm: CrmWorld, gate: GatedMessaging) -> FollowUpWorker:
    return FollowUpWorker(
        database=crm.database,
        settings=object(),  # type: ignore[arg-type]
        messaging_factory=gate.factory,  # type: ignore[arg-type]
    )


async def _reschedule(crm: CrmWorld, world: World, *, text: str) -> FollowUp:
    async with crm.session() as session:
        follow_up = await FollowUpService(session=session, tenant_id=world.tenant_id).schedule(
            conversation_id=world.conversation_id,
            scheduled_at=datetime.now(UTC) + THURSDAY,
            body=text,
            created_by_id=world.alice.id,
        )
        await session.commit()
        return follow_up


# ------------------------------------------------------------- CRM-09


async def test_a_reschedule_inside_the_lease_takes_the_row_back_from_the_sweep(
    crm: CrmWorld,
) -> None:
    """P4c: claimed at 10:00, moved to Thursday, and nothing goes out now."""
    world = await crm.world()
    follow_up_id = await _due(crm, world)
    tokens = await _claim(crm)
    assert tokens[follow_up_id] is not None

    rescheduled = await _reschedule(crm, world, text="new text for Thursday")
    assert rescheduled.id == follow_up_id

    gate = GatedMessaging()
    handled = await _worker(crm, gate)._dispatch_one(
        follow_up_id, world.tenant_id, tokens[follow_up_id]
    )

    assert handled is False
    assert gate.sends == 0
    row = await crm.follow_up(follow_up_id)
    assert row.status is FollowUpStatus.PENDING
    assert row.body == "new text for Thursday"
    assert row.scheduled_at > datetime.now(UTC) + THURSDAY - timedelta(minutes=5)
    assert row.claim_token is None
    assert row.message_id is None


async def test_a_reschedule_holding_the_row_makes_the_retake_step_over_it(
    crm: CrmWorld,
) -> None:
    """The re-take skips a row a colleague is changing, and sends nothing of it."""
    world = await crm.world()
    follow_up_id = await _due(crm, world)
    tokens = await _claim(crm)

    async with crm.session() as colleague:
        await FollowUpService(session=colleague, tenant_id=world.tenant_id).schedule(
            conversation_id=world.conversation_id,
            scheduled_at=datetime.now(UTC) + THURSDAY,
            body="new text for Thursday",
            created_by_id=world.alice.id,
        )
        # Uncommitted: the colleague holds the row while the sweep arrives.
        gate = GatedMessaging()
        handled = await _worker(crm, gate)._dispatch_one(
            follow_up_id, world.tenant_id, tokens[follow_up_id]
        )
        await colleague.commit()

    assert handled is False
    assert gate.sends == 0
    row = await crm.follow_up(follow_up_id)
    assert (row.status, row.body) == (FollowUpStatus.PENDING, "new text for Thursday")


async def test_a_reschedule_of_a_nudge_already_being_sent_is_refused(
    crm: CrmWorld,
) -> None:
    """Once the send intent commits the customer may have it; moving it would be false."""
    world = await crm.world()
    follow_up_id = await _due(crm, world)
    tokens = await _claim(crm)
    gate = GatedMessaging()
    gate.hold_before_meta.clear()

    dispatch = asyncio.create_task(
        _worker(crm, gate)._dispatch_one(follow_up_id, world.tenant_id, tokens[follow_up_id])
    )
    try:
        await asyncio.wait_for(gate.intent_committed.wait(), timeout=20)
        with pytest.raises(ConflictError) as refused:
            await _reschedule(crm, world, text="too late")
    finally:
        gate.hold_before_meta.set()
    assert await dispatch is True

    assert refused.value.error_code == DISPATCH_IN_PROGRESS
    row = await crm.follow_up(follow_up_id)
    assert (row.status, row.body) == (FollowUpStatus.SENT, "Still interested?")
    assert gate.bodies == ["Still interested?"]


# ------------------------------------------------------------- CRM-10


async def test_a_cancel_inside_the_lease_stops_the_send(crm: CrmWorld) -> None:
    world = await crm.world()
    follow_up_id = await _due(crm, world)
    tokens = await _claim(crm)

    async with crm.session() as colleague:
        cancelled = await FollowUpService(session=colleague, tenant_id=world.tenant_id).cancel(
            follow_up_id=follow_up_id, reason="Spoke to them by phone."
        )
        await colleague.commit()
    assert cancelled.status is FollowUpStatus.CANCELLED

    gate = GatedMessaging()
    handled = await _worker(crm, gate)._dispatch_one(
        follow_up_id, world.tenant_id, tokens[follow_up_id]
    )

    assert handled is False
    assert gate.sends == 0
    row = await crm.follow_up(follow_up_id)
    assert row.status is FollowUpStatus.CANCELLED
    assert row.message_id is None and row.sent_at is None


async def test_a_cancel_racing_the_send_waits_and_is_told_the_truth(crm: CrmWorld) -> None:
    """P4d: the cancel blocks on the re-take's lock, then sees a committed send."""
    world = await crm.world()
    follow_up_id = await _due(crm, world)
    tokens = await _claim(crm)
    gate = GatedMessaging()
    gate.hold_before_intent.clear()

    dispatch = asyncio.create_task(
        _worker(crm, gate)._dispatch_one(follow_up_id, world.tenant_id, tokens[follow_up_id])
    )
    await asyncio.wait_for(gate.reached_intent.wait(), timeout=20)

    async def cancel() -> FollowUp:
        async with crm.session() as colleague:
            try:
                return await FollowUpService(session=colleague, tenant_id=world.tenant_id).cancel(
                    follow_up_id=follow_up_id, reason="Not needed"
                )
            finally:
                await colleague.rollback()

    racing = asyncio.create_task(cancel())
    try:
        # Provably blocked behind the re-take, not merely later than it.
        await crm.lock_waiter()
    finally:
        gate.hold_before_intent.set()
    with pytest.raises(ConflictError) as refused:
        await racing
    assert await dispatch is True

    assert refused.value.error_code == DISPATCH_IN_PROGRESS
    row = await crm.follow_up(follow_up_id)
    assert row.status is FollowUpStatus.SENT
    assert row.cancelled_at is None and row.cancelled_reason is None
    assert row.message_id is not None and row.sent_at is not None
    assert gate.sends == 1


async def test_cancelling_after_the_send_reports_that_it_was_sent(crm: CrmWorld) -> None:
    world = await crm.world()
    follow_up_id = await _due(crm, world)
    gate = GatedMessaging()
    await _worker(crm, gate).run_once()
    assert (await crm.follow_up(follow_up_id)).status is FollowUpStatus.SENT

    async with crm.session() as colleague:
        answer = await FollowUpService(session=colleague, tenant_id=world.tenant_id).cancel(
            follow_up_id=follow_up_id
        )
        await colleague.commit()

    assert answer.status is FollowUpStatus.SENT
    assert (await crm.follow_up(follow_up_id)).status is FollowUpStatus.SENT
    assert gate.sends == 1


# ---------------------------------------------------- claim ownership


async def test_a_worker_holding_a_superseded_claim_sends_nothing(crm: CrmWorld) -> None:
    """R17: the re-take requires the exact token, not merely a pending row."""
    world = await crm.world()
    follow_up_id = await _due(crm, world)
    await _claim(crm)

    gate = GatedMessaging()
    handled = await _worker(crm, gate)._dispatch_one(follow_up_id, world.tenant_id, uuid.uuid4())

    assert handled is False
    assert gate.sends == 0
    assert (await crm.follow_up(follow_up_id)).status is FollowUpStatus.PENDING


async def test_the_claim_no_longer_moves_the_scheduled_time(crm: CrmWorld) -> None:
    """`scheduled_at` is only ever what somebody asked for; the lease is its own column."""
    world = await crm.world()
    follow_up_id = await _due(crm, world)
    before = (await crm.follow_up(follow_up_id)).scheduled_at
    await _claim(crm)

    row = await crm.follow_up(follow_up_id)
    assert row.scheduled_at == before
    assert row.claim_token is not None
    assert row.claimed_until is not None and row.claimed_until > datetime.now(UTC)
    # A second sweep inside the lease does not claim it again.
    assert follow_up_id not in await _claim(crm)

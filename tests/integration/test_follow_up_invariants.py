"""The follow-up, header and realtime invariants count what they claim to (non-vacuity).

FU-01..FU-08 and CR-16..CR-17 in `scripts/omnichannel_invariants.py`: each test
starts from a healthy sequence step (or a sent campaign recipient), writes
exactly one violation inside the test's rolled-back transaction and asserts
that invariant - and only it - counts one more. Every moment is written out:
`now()` is the transaction's start, the same for every row of a test.

RT-I01 reads the realtime streams in Redis and asks the database who owns each
id a hint names: a hint naming another workspace's conversation counts one.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.business_initiated import BusinessInitiatedSend
from app.db.models.campaign import Campaign, RecipientStatus
from app.db.models.campaign_media import CampaignHeaderMedia
from app.db.models.conversation import (
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
)
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.follow_up_sequence import FollowUpSequenceRun
from app.db.models.follow_up_settings import FollowUpSettings
from app.db.models.lead import ActorKind
from app.db.models.messaging_limit import WhatsAppBusinessPortfolio
from app.db.models.tenant import Tenant
from scripts.omnichannel_invariants import (
    REALTIME_CHECK,
    campaign_violations,
    follow_up_violations,
    realtime_violations,
)
from tests.integration.test_campaign_invariants import _header_file, _image_header, _world
from tests.redis_url import shared_redis_url

pytestmark = pytest.mark.integration

#: The anchor of every healthy step: an AI reply an hour ago.
ANCHOR = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)


async def _counts(session: AsyncSession) -> dict[str, int]:
    await session.flush()
    connection = await session.connection()
    return {
        **await follow_up_violations(connection, read_only=False),
        **await campaign_violations(connection, read_only=False),
    }


async def _added(session: AsyncSession, before: dict[str, int]) -> dict[str, int]:
    after = await _counts(session)
    return {name: after[name] - before[name] for name in after if after[name] != before[name]}


def _message(
    conversation: Conversation, *, at: datetime, inbound: bool, template: bool = False
) -> Message:
    return Message(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        wa_message_id=f"wamid.{uuid.uuid4().hex}",
        direction=MessageDirection.INBOUND if inbound else MessageDirection.OUTBOUND,
        kind=MessageKind.TEMPLATE if template else MessageKind.TEXT,
        body=None if template else "words",
        template_name="spring_offer" if template else None,
        template_language="ar_EG" if template else None,
        origin=MessageOrigin.CUSTOMER if inbound else MessageOrigin.FOLLOW_UP,
        created_at=at,
    )


async def _healthy_step(
    session: AsyncSession, slug: str, *, sent: timedelta = timedelta(minutes=5)
) -> tuple[Conversation, FollowUpSequenceRun, FollowUp]:
    """A conversation whose customer wrote, the AI answered (the anchor), and step 1 went out."""
    _, _, conversation = await _world(session, slug)
    session.add(_message(conversation, at=ANCHOR - timedelta(minutes=1), inbound=True))
    run = FollowUpSequenceRun(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        anchor_at=ANCHOR,
        state="ended",
        end_reason="completed",
        ended_at=ANCHOR + sent,
        steps_sent=1,
    )
    session.add(run)
    await session.flush()
    step = await _step(session, conversation, run, index=1, at=ANCHOR + sent)
    return conversation, run, step


async def _step(
    session: AsyncSession,
    conversation: Conversation,
    run: FollowUpSequenceRun,
    *,
    index: int,
    at: datetime,
    template: bool = False,
) -> FollowUp:
    outbound = _message(conversation, at=at, inbound=False, template=template)
    session.add(outbound)
    await session.flush()
    step = FollowUp(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        scheduled_at=at,
        requested_at=at,
        origin="sequence",
        sequence_run_id=run.id,
        step_index=index,
        anchor_at=run.anchor_at,
        status=FollowUpStatus.SENT,
        body=None if template else "Still interested?",
        created_by_kind=ActorKind.SYSTEM,
        sent_at=at + timedelta(seconds=1),
        message_id=outbound.id,
    )
    session.add(step)
    await session.flush()
    return step


# ------------------------------------------------------------------ FU-01..FU-08


async def test_fu01_a_step_sent_after_the_customer_replied(db_session: AsyncSession) -> None:
    conversation, _, _ = await _healthy_step(db_session, "fu01")
    before = await _counts(db_session)

    db_session.add(_message(conversation, at=ANCHOR + timedelta(minutes=3), inbound=True))

    assert await _added(db_session, before) == {"fu01_step_sent_after_the_customer_replied": 1}


async def test_fu02_a_step_sent_while_a_person_owned_it(db_session: AsyncSession) -> None:
    conversation, _, _ = await _healthy_step(db_session, "fu02")

    def audit(action: AuditAction, minutes: int) -> AuditLog:
        return AuditLog(
            tenant_id=conversation.tenant_id,
            action=action,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="conversation",
            target_id=conversation.id,
            occurred_at=ANCHOR + timedelta(minutes=minutes),
        )

    # Taken over and handed back before the step: healthy.
    db_session.add(audit(AuditAction.CONVERSATION_TAKEN_OVER, 1))
    db_session.add(audit(AuditAction.CONVERSATION_RELEASED_TO_AI, 2))
    before = await _counts(db_session)

    db_session.add(audit(AuditAction.CONVERSATION_TAKEN_OVER, 3))

    assert await _added(db_session, before) == {
        "fu02_step_sent_while_a_person_owned_the_conversation": 1
    }


async def test_fu03_a_step_sent_past_the_window_less_the_margin(
    db_session: AsyncSession,
) -> None:
    # 23 h 29 min after the customer's last message: inside 24 h less 30 min.
    late = timedelta(hours=23, minutes=28)
    conversation, run, _ = await _healthy_step(db_session, "fu03", sent=late)
    before = await _counts(db_session)

    # A second step two minutes later: 23 h 31 min, inside 24 h but past the margin.
    await _step(db_session, conversation, run, index=2, at=ANCHOR + late + timedelta(minutes=2))

    assert await _added(db_session, before) == {"fu03_step_sent_past_the_window_less_the_margin": 1}


async def test_fu04_a_step_sent_as_a_template(db_session: AsyncSession) -> None:
    conversation, run, _ = await _healthy_step(db_session, "fu04")
    before = await _counts(db_session)

    await _step(
        db_session, conversation, run, index=2, at=ANCHOR + timedelta(minutes=35), template=True
    )

    assert await _added(db_session, before) == {"fu04_step_sent_as_a_template": 1}


async def test_fu05_a_sixth_step_or_a_second_active_run(db_session: AsyncSession) -> None:
    conversation, run, _ = await _healthy_step(db_session, "fu05")
    for index in range(2, 6):
        await _step(
            db_session, conversation, run, index=index, at=ANCHOR + timedelta(minutes=5 * index)
        )
    before = await _counts(db_session)

    await _step(db_session, conversation, run, index=6, at=ANCHOR + timedelta(minutes=30))

    assert await _added(db_session, before) == {"fu05_run_over_its_steps_or_two_active_runs": 1}


async def test_fu05_two_active_runs_in_one_conversation(db_session: AsyncSession) -> None:
    conversation, _, _ = await _healthy_step(db_session, "fu05b")

    def active() -> FollowUpSequenceRun:
        return FollowUpSequenceRun(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            anchor_at=ANCHOR + timedelta(minutes=10),
            state="active",
        )

    db_session.add(active())
    before = await _counts(db_session)

    # The partial unique index refuses this; the invariant is the second line.
    await db_session.execute(text("DROP INDEX uq_follow_up_sequence_runs_active_conversation"))
    db_session.add(active())

    assert await _added(db_session, before) == {"fu05_run_over_its_steps_or_two_active_runs": 1}


async def test_fu06_a_ledger_row_for_a_free_form_message(db_session: AsyncSession) -> None:
    conversation, _, step = await _healthy_step(db_session, "fu06")
    portfolio = WhatsAppBusinessPortfolio(key_kind="waba", meta_key=f"fu06-{uuid.uuid4().hex}")
    db_session.add(portfolio)
    template = _message(conversation, at=ANCHOR, inbound=False, template=True)
    db_session.add(template)
    await db_session.flush()

    def row(message_id: uuid.UUID | None) -> BusinessInitiatedSend:
        return BusinessInitiatedSend(
            portfolio_id=portfolio.id,
            recipient="201000000001",
            sent_at=ANCHOR,
            source="follow_up",
            tenant_id=conversation.tenant_id,
            message_id=message_id,
        )

    db_session.add(row(template.id))
    before = await _counts(db_session)

    db_session.add(row(step.message_id))

    assert await _added(db_session, before) == {"fu06_ledger_row_for_a_free_form_message": 1}


async def test_fu07_a_template_follow_up_sent_in_quiet_hours(db_session: AsyncSession) -> None:
    conversation, _, _ = await _healthy_step(db_session, "fu07")
    tenant = await db_session.get(Tenant, conversation.tenant_id)
    assert tenant is not None
    # Quiet hours 22:00-09:00 in the workspace's zone; the settings existed two days before.
    tenant.campaign_quiet_hours_enabled = True
    tenant.campaign_quiet_hours_start = time(22, 0)
    tenant.campaign_quiet_hours_end = time(9, 0)
    db_session.add(
        FollowUpSettings(
            tenant_id=tenant.id,
            enabled=True,
            channel_types=["whatsapp"],
            steps=[{"delay_minutes": 5, "guidance": None}],
            version=1,
            created_at=ANCHOR - timedelta(days=2),
        )
    )
    await db_session.flush()
    await db_session.execute(
        update(Tenant).where(Tenant.id == tenant.id).values(timezone="Africa/Cairo")
    )
    noon = await _local(db_session, time(12, 0))
    night = await _local(db_session, time(23, 30))

    def template_follow_up(at: datetime) -> FollowUp:
        return FollowUp(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            scheduled_at=at,
            requested_at=at,
            status=FollowUpStatus.SENT,
            template_name="come_back",
            template_language="ar_EG",
            created_by_kind=ActorKind.USER,
            sent_at=at,
        )

    db_session.add(template_follow_up(noon))
    before = await _counts(db_session)

    db_session.add(template_follow_up(night))

    assert await _added(db_session, before) == {"fu07_template_follow_up_sent_in_quiet_hours": 1}


async def _local(session: AsyncSession, wall: time) -> datetime:
    """Yesterday at `wall` in Cairo, as an instant - the database's own tz rules."""
    found = await session.scalar(
        text(
            "SELECT ((now() AT TIME ZONE 'Africa/Cairo')::date - 1 + CAST(:wall AS time))"
            " AT TIME ZONE 'Africa/Cairo'"
        ),
        {"wall": wall},
    )
    assert isinstance(found, datetime)
    return found


async def test_fu08_a_requested_time_that_moved(db_session: AsyncSession) -> None:
    conversation, _, _ = await _healthy_step(db_session, "fu08")
    asked = ANCHOR + timedelta(hours=2)
    waited = FollowUp(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        scheduled_at=asked + timedelta(hours=8),
        requested_at=asked,
        deferred_reason="quiet_hours",
        deferrals=[{"reason": "quiet_hours", "until": "x", "at": "y"}],
        status=FollowUpStatus.PENDING,
        body="Hello again",
        created_by_kind=ActorKind.USER,
    )
    db_session.add(waited)
    await db_session.flush()
    before = await _counts(db_session)

    # What M-S24 would do: the wait overwrote the request.
    waited.requested_at = waited.scheduled_at

    assert await _added(db_session, before) == {"fu08_requested_time_changed": 1}


# ------------------------------------------------------------------ CR-16, CR-17


def _file(campaign: Campaign, **kwargs: Any) -> CampaignHeaderMedia:
    """`_header_file`, with a storage key of its own: a campaign here has several files."""
    found = _header_file(campaign, **kwargs)
    found.storage_key = f"{found.tenant_id}/2026/10/{uuid.uuid4().hex}.bin"
    return found


async def test_cr16_a_recipient_sent_another_workspaces_file(db_session: AsyncSession) -> None:
    from tests.integration.test_campaigns import _tenant

    campaign, recipient, _ = await _world(db_session, "cr16")
    await _image_header(db_session, campaign)
    mine = _file(campaign)
    stranger = await _tenant(db_session, slug="cr16-other")
    theirs = _file(campaign, tenant_id=stranger.id)
    db_session.add_all([mine, theirs])
    await db_session.flush()
    campaign.header_media_id = mine.id
    recipient.status = RecipientStatus.SENT
    recipient.sent_at = datetime.now(UTC)
    await db_session.flush()
    before = await _counts(db_session)

    await db_session.execute(text("SET LOCAL session_replication_role = replica"))
    recipient.header_media_id = theirs.id
    await db_session.flush()
    await db_session.execute(text("SET LOCAL session_replication_role = origin"))

    assert await _added(db_session, before) == {
        "cr16_recipient_header_file_of_another_workspace_or_kind": 1
    }


async def test_cr16_a_recipient_sent_a_file_of_the_wrong_kind(db_session: AsyncSession) -> None:
    campaign, recipient, _ = await _world(db_session, "cr16k")
    await _image_header(db_session, campaign)
    image = _file(campaign)
    document = _file(campaign, kind="document")
    db_session.add_all([image, document])
    await db_session.flush()
    campaign.header_media_id = image.id
    recipient.status = RecipientStatus.SENT
    recipient.sent_at = datetime.now(UTC)
    await db_session.flush()
    before = await _counts(db_session)

    recipient.header_media_id = document.id

    assert await _added(db_session, before) == {
        "cr16_recipient_header_file_of_another_workspace_or_kind": 1
    }


async def test_cr17_a_recipient_sent_without_the_file_its_header_needs(
    db_session: AsyncSession,
) -> None:
    campaign, recipient, _ = await _world(db_session, "cr17")
    image = _file(campaign)
    db_session.add(image)
    await db_session.flush()
    campaign.header_media_id = image.id
    await _image_header(db_session, campaign)
    recipient.status = RecipientStatus.SENT
    recipient.sent_at = datetime.now(UTC)
    await db_session.flush()
    before = await _counts(db_session)

    campaign.header_media_id = None

    assert await _added(db_session, before) == {"cr17_header_files_over_the_maximum_or_missing": 1}


async def test_cr17_a_per_recipient_campaign_over_the_maximum(db_session: AsyncSession) -> None:
    campaign, _, _ = await _world(db_session, "cr17m")
    files = [_file(campaign) for _ in range(21)]
    db_session.add_all(files)
    await db_session.flush()
    campaign.header_media_mode = "per_recipient"
    campaign.header_media_id = files[0].id
    campaign.header_media_rules = [
        {"when": {"field": "lead.interest", "equals": str(n)}, "header_media_id": str(f.id)}
        for n, f in enumerate(files[1:20])
    ]
    await db_session.flush()
    before = await _counts(db_session)

    campaign.header_media_rules = [
        *campaign.header_media_rules,
        {"when": {"field": "lead.interest", "equals": "21"}, "header_media_id": str(files[20].id)},
    ]

    assert await _added(db_session, before) == {"cr17_header_files_over_the_maximum_or_missing": 1}


# ------------------------------------------------------------------ RT-I01


async def test_rt_i01_a_hint_naming_another_workspaces_conversation(
    db_session: AsyncSession,
) -> None:
    url = shared_redis_url()
    _, _, mine = await _world(db_session, "rti01")
    _, _, theirs = await _world(db_session, "rti01-other")
    await db_session.flush()
    redis = Redis.from_url(url, decode_responses=True)
    key = events.stream_key(mine.tenant_id)
    try:
        await redis.delete(key)

        async def hint(conversation: Conversation) -> None:
            data = {"conversation_id": str(conversation.id), "message_id": str(uuid.uuid4())}
            await redis.xadd(key, {"type": "message.created", "data": json.dumps(data)})

        async def count() -> int:
            found = await realtime_violations(await db_session.connection(), redis)
            return found[REALTIME_CHECK]

        await hint(mine)
        before = await count()
        await hint(theirs)
        assert await count() - before == 1
    finally:
        await redis.delete(key)
        await redis.aclose()

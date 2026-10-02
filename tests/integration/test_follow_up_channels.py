"""Follow-ups are judged against the channel they will be sent on (OMNI-039).

Scheduling used to validate against WhatsApp alone - 4,096 characters and the
WhatsApp template registry - and the AI tool advised "1440 for tomorrow, 10080 for
next week" on every channel. On a byte-bounded channel a body was accepted that
dispatch then refused and retried as if transient; on a channel with no automated
way out of its window, a next-week nudge was a guaranteed skip.

Driven through `FollowUpService` against PostgreSQL, on the synthetic channel
(1,000 bytes, a 7-day window, no templates), its Messenger-shaped variant (a
24-hour window; the human-agent tag is a person's, not a follow-up's), and
WhatsApp.

Mutants this suite kills: M-O19 (scheduling validates against 4,096 characters
again) and M-O20 (a dispatch-time policy refusal retried).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import SCHEDULE_FOLLOW_UP_TOOL, build_default_registry
from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.conversation import Conversation
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.tenant import Tenant
from app.integrations.openai.types import ToolSpec
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.follow_up_service import FollowUpService
from app.services.messaging_service import MessagingService
from tests.channel_fakes import SyntheticAdapter, synthetic_payload
from tests.channel_plans import allow_channels
from tests.integration.test_omnichannel_operations import _customer, _number, _tenant

pytestmark = pytest.mark.integration

ARABIC_1320_BYTES = "مرحبا " * 120  # 720 characters, 1,320 bytes


def _registry(adapter: SyntheticAdapter) -> ChannelRegistry:
    return ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            adapter.channel: cast(ChannelAdapter, adapter),
        },
        unmetered=True,
    )


async def _synthetic_conversation(session: AsyncSession, adapter: SyntheticAdapter) -> Conversation:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Nudges {tag}", slug=f"nudges-{tag}")
    session.add(tenant)
    await session.flush()
    # A nudge goes out on a channel only the plan in force includes (ENT-16).
    await allow_channels(session, tenant.id)
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=adapter.channel,
        external_account_id=f"syn-{tag}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=30),
    )
    session.add(connection)
    await session.flush()
    await ChannelIngestionService(session=session, adapter=cast(ChannelAdapter, adapter)).ingest(
        adapter.parse(
            synthetic_payload(
                connection.external_account_id,
                {
                    "type": "message",
                    "id": f"m.{tag}",
                    "from": "igsid-nudge",
                    "at": int(datetime.now(UTC).timestamp()),
                    "text": "Let me think about it",
                },
            )
        )
    )
    await session.flush()
    conversation = await session.scalar(
        select(Conversation).where(Conversation.account_id == connection.id)
    )
    assert conversation is not None
    return conversation


def _service(
    session: AsyncSession, settings: Settings, adapter: SyntheticAdapter, tenant_id: uuid.UUID
) -> FollowUpService:
    registry = _registry(adapter)
    return FollowUpService(
        session=session,
        tenant_id=tenant_id,
        messaging=MessagingService(
            session=session, settings=settings, tenant_id=tenant_id, channels=registry
        ),
        channels=registry,
    )


async def _pending(session: AsyncSession, conversation: Conversation) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(FollowUp)
            .where(FollowUp.conversation_id == conversation.id)
        )
        or 0
    )


async def test_a_body_over_the_channels_byte_limit_is_refused_at_scheduling(
    db_session: AsyncSession, settings: Settings
) -> None:
    adapter = SyntheticAdapter()
    conversation = await _synthetic_conversation(db_session, adapter)
    service = _service(db_session, settings, adapter, conversation.tenant_id)
    assert len(ARABIC_1320_BYTES) == 720 and len(ARABIC_1320_BYTES.encode()) == 1_320

    with pytest.raises(ValidationError, match="at most 1000 bytes"):
        await service.schedule(
            conversation_id=conversation.id, delay=timedelta(hours=1), body=ARABIC_1320_BYTES
        )

    assert await _pending(db_session, conversation) == 0


async def test_a_nudge_past_the_window_is_refused_where_nothing_may_follow(
    db_session: AsyncSession, settings: Settings
) -> None:
    adapter = SyntheticAdapter()
    conversation = await _synthetic_conversation(db_session, adapter)
    service = _service(db_session, settings, adapter, conversation.tenant_id)

    with pytest.raises(ValidationError, match="reply window closes"):
        await service.schedule(
            conversation_id=conversation.id, delay=timedelta(days=8), body="Checking in"
        )
    accepted = await service.schedule(
        conversation_id=conversation.id, delay=timedelta(days=6), body="Checking in"
    )

    assert accepted.status is FollowUpStatus.PENDING


async def test_a_human_agent_tag_does_not_carry_a_follow_up_past_the_window(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Messenger-shaped: 24 hours for anybody automated; the 7-day tag is a person's."""
    adapter = SyntheticAdapter(Channel.MESSENGER, tagged=True)
    conversation = await _synthetic_conversation(db_session, adapter)
    service = _service(db_session, settings, adapter, conversation.tenant_id)

    with pytest.raises(ValidationError, match="reply window closes"):
        await service.schedule(
            conversation_id=conversation.id, delay=timedelta(days=2), body="Checking in"
        )


async def test_a_template_is_refused_where_the_channel_has_none(
    db_session: AsyncSession, settings: Settings
) -> None:
    adapter = SyntheticAdapter()
    conversation = await _synthetic_conversation(db_session, adapter)
    service = _service(db_session, settings, adapter, conversation.tenant_id)

    with pytest.raises(ValidationError, match="has no message templates"):
        await service.schedule(
            conversation_id=conversation.id,
            delay=timedelta(hours=1),
            template_name="spring_sale",
            template_language="en",
        )


async def test_whatsapps_next_week_nudges_are_unchanged(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    _, conversation = await _customer(db_session, tenant, account, "201000000581")
    service = FollowUpService(session=db_session, tenant_id=tenant.id, settings=settings)

    with_template = await service.schedule(
        conversation_id=conversation.id,
        delay=timedelta(days=7),
        body="Checking in",
        template_name="checkin",
        template_language="en",
    )
    text_only = await service.schedule(
        conversation_id=conversation.id, delay=timedelta(days=7), body="Checking in"
    )

    assert with_template.status is FollowUpStatus.PENDING
    assert text_only.status is FollowUpStatus.PENDING


async def test_a_policy_refusal_at_dispatch_is_terminal(
    db_session: AsyncSession, settings: Settings
) -> None:
    """A row written before this check - or by anything that bypassed it."""
    adapter = SyntheticAdapter()
    conversation = await _synthetic_conversation(db_session, adapter)
    service = _service(db_session, settings, adapter, conversation.tenant_id)
    follow_up = FollowUp(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        status=FollowUpStatus.PENDING,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
        body=ARABIC_1320_BYTES,
        created_by_kind="user",
    )
    db_session.add(follow_up)
    await db_session.flush()

    result = await service.dispatch(follow_up)

    assert result.follow_up.status is FollowUpStatus.SKIPPED
    assert result.follow_up.claim_token is None
    assert "1000 bytes" in (result.follow_up.last_error or "")
    assert adapter.log.sent == []


async def test_a_transient_failure_at_dispatch_is_still_retried(
    db_session: AsyncSession, settings: Settings
) -> None:
    adapter = SyntheticAdapter()
    adapter.log.refuse = True
    conversation = await _synthetic_conversation(db_session, adapter)
    service = _service(db_session, settings, adapter, conversation.tenant_id)
    follow_up = await service.schedule(
        conversation_id=conversation.id, delay=timedelta(minutes=1), body="Checking in"
    )
    follow_up.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    result = await service.dispatch(follow_up)

    assert result.follow_up.status is FollowUpStatus.PENDING
    assert result.follow_up.attempts == 1
    assert result.follow_up.scheduled_at > datetime.now(UTC)


def test_the_tool_is_described_as_each_channel_allows() -> None:
    registry = build_default_registry()
    whatsapp = registry.specs([SCHEDULE_FOLLOW_UP_TOOL], policy=WhatsAppAdapter().policy)
    synthetic = registry.specs([SCHEDULE_FOLLOW_UP_TOOL], policy=SyntheticAdapter().policy)
    tagged = registry.specs(
        [SCHEDULE_FOLLOW_UP_TOOL], policy=SyntheticAdapter(Channel.MESSENGER, tagged=True).policy
    )

    def parameters(spec: list[ToolSpec]) -> dict[str, Any]:
        (only,) = spec
        return cast(dict[str, Any], only.parameters["properties"])

    assert "10080 for next week" in str(parameters(whatsapp)["delay_minutes"]["description"])
    assert parameters(whatsapp)["delay_minutes"]["maximum"] > 10_080
    assert parameters(synthetic)["delay_minutes"]["maximum"] == 7 * 24 * 60
    assert parameters(tagged)["delay_minutes"]["maximum"] == 24 * 60
    assert "24 hours" in str(parameters(tagged)["delay_minutes"]["description"])
    assert parameters(synthetic)["message"]["maxLength"] == 1_000
    assert "1000 bytes" in str(parameters(synthetic)["message"]["description"])

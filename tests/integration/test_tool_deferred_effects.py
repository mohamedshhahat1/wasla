"""A message a tool arranged, delivered after the workspace stopped being served.

`schedule_follow_up` is the only tool with a customer-visible effect, and the
effect is deferred: a row waits on a timer and a worker sends it hours later.
Every other pre-send fact was re-read at that moment - the conversation's
closure, its mode, the customer's opt-out, the template's standing, the service
window - and the workspace's own lifecycle was not. So an agent scheduled a
nudge, the platform suspended the workspace for abuse or non-payment or the
customer deleted it, and the nudge was delivered anyway: a model-composed
WhatsApp message leaving a workspace Wasla had stopped serving (TOOL-04).

Two guards now, and the order matters. The lifecycle transition cancels the
pending agent nudges it can see, which handles the ordinary case and lets a
colleague see that nothing is queued. The dispatch re-read is authoritative,
because a transition landing after the sweep has claimed a row cannot be caught
by the first one.

The assertion that matters throughout is `sends == 0`, counted at a stub that
stands where Meta stands. A guard that recorded the right status *after* asking
Meta to deliver would satisfy a status assertion and fail the customer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
)
from app.db.models.enums import TenantStatus
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.services.follow_up_service import FollowUpService
from tests.integration.test_follow_up_revalidation import CountingMessaging

pytestmark = pytest.mark.integration


async def _workspace(
    session: AsyncSession,
    *,
    status: TenantStatus = TenantStatus.ACTIVE,
    deleted: bool = False,
    mode: ConversationMode = ConversationMode.AI,
) -> tuple[Tenant, Conversation]:
    slug = f"deferred-{uuid.uuid4().hex[:8]}"
    tenant = Tenant(
        name="Deferred",
        slug=slug,
        status=status,
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{slug}",
        waba_id="555000333",
        display_phone_number="+201000000002",
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    contact = Contact(tenant_id=tenant.id, wa_id=f"2016{uuid.uuid4().int % 10_000_000:07d}")
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
    return tenant, conversation


async def _due_nudge(
    session: AsyncSession,
    tenant: Tenant,
    conversation: Conversation,
    *,
    kind: ActorKind = ActorKind.AGENT,
) -> FollowUp:
    follow_up = FollowUp(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=5),
        body="Still interested?",
        created_by_kind=kind,
    )
    session.add(follow_up)
    await session.flush()
    return follow_up


def _service(
    session: AsyncSession,
    tenant: Tenant,
    messaging: CountingMessaging,
) -> FollowUpService:
    return FollowUpService(session=session, tenant_id=tenant.id, messaging=messaging)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("case", "status", "deleted"),
    [
        ("suspended", TenantStatus.SUSPENDED, False),
        ("soft-deleted", TenantStatus.ACTIVE, True),
    ],
)
async def test_a_workspace_that_is_not_served_sends_no_automated_message(
    db_session: AsyncSession, case: str, status: TenantStatus, deleted: bool
) -> None:
    tenant, conversation = await _workspace(db_session, status=status, deleted=deleted)
    follow_up = await _due_nudge(db_session, tenant, conversation)
    messaging = CountingMessaging(db_session, tenant.id)

    # Non-vacuity: the nudge really was pending and really was due.
    before = follow_up.status
    assert before is FollowUpStatus.PENDING
    assert follow_up.scheduled_at < datetime.now(UTC)

    outcome = await _service(db_session, tenant, messaging).dispatch(follow_up)

    assert messaging.sends == 0
    assert outcome.status is FollowUpStatus.SKIPPED
    # Terminal, not postponed. A nudge suppressed during a suspension must not
    # arrive weeks later as a surprise if the workspace is restored
    # (PD-TOOLS-06): the customer has moved on and the message is about a
    # conversation they no longer remember.
    assert follow_up.status is FollowUpStatus.SKIPPED
    assert follow_up.attempts == 0


async def test_an_active_workspace_still_sends_its_follow_ups(
    db_session: AsyncSession,
) -> None:
    """The negative control. The guard above is satisfiable by never sending."""
    tenant, conversation = await _workspace(db_session)
    follow_up = await _due_nudge(db_session, tenant, conversation)
    messaging = CountingMessaging(db_session, tenant.id)

    outcome = await _service(db_session, tenant, messaging).dispatch(follow_up)

    assert messaging.sends == 1
    assert outcome.status is FollowUpStatus.SENT


async def test_suspending_a_workspace_cancels_the_ai_nudges_it_is_holding(
    db_session: AsyncSession,
) -> None:
    """The first guard: the rows stop waiting, so a colleague can see they will not arrive."""
    tenant, conversation = await _workspace(db_session)
    agent_nudge = await _due_nudge(db_session, tenant, conversation)

    cancelled = await FollowUpService(
        session=db_session, tenant_id=tenant.id
    ).cancel_agent_follow_ups(reason="The workspace was suspended.")

    assert cancelled == 1
    assert agent_nudge.status is FollowUpStatus.CANCELLED
    assert agent_nudge.cancelled_reason == "The workspace was suspended."


async def test_a_colleagues_own_follow_up_is_not_cancelled_by_a_suspension(
    db_session: AsyncSession,
) -> None:
    """Only what the AI decided. A person's work is not the platform's to discard."""
    tenant, conversation = await _workspace(db_session)
    theirs = await _due_nudge(db_session, tenant, conversation, kind=ActorKind.USER)

    cancelled = await FollowUpService(
        session=db_session, tenant_id=tenant.id
    ).cancel_agent_follow_ups(reason="The workspace was suspended.")

    assert cancelled == 0
    assert theirs.status is FollowUpStatus.PENDING


async def test_an_agent_cannot_schedule_a_nudge_on_a_human_owned_conversation(
    db_session: AsyncSession,
) -> None:
    """Handing a conversation over stops the AI, at scheduling as well (TOOL-06)."""
    from app.core.exceptions import ConflictError

    tenant, conversation = await _workspace(db_session, mode=ConversationMode.HUMAN)
    service = FollowUpService(session=db_session, tenant_id=tenant.id)

    with pytest.raises(ConflictError):
        await service.schedule(
            conversation_id=conversation.id,
            delay=timedelta(minutes=30),
            body="Still there?",
            created_by_kind=ActorKind.AGENT,
        )

    remaining = (await service.list_follow_ups(statuses=(FollowUpStatus.PENDING,))).items
    assert remaining == []


async def test_a_colleague_may_still_schedule_on_a_conversation_they_own(
    db_session: AsyncSession,
) -> None:
    """The control for the guard above: the rule is about the AI, not the feature."""
    tenant, conversation = await _workspace(db_session, mode=ConversationMode.HUMAN)
    service = FollowUpService(session=db_session, tenant_id=tenant.id)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=timedelta(minutes=30),
        body="Calling you back.",
        created_by_kind=ActorKind.USER,
    )

    assert follow_up.status is FollowUpStatus.PENDING
    assert follow_up.created_by_kind is ActorKind.USER


async def test_a_nudge_an_agent_scheduled_is_recorded_as_the_agents(
    db_session: AsyncSession,
) -> None:
    """Attribution is what separates an AI action from a colleague's (TM23).

    It survived a mutation that recorded every agent follow-up as a person's,
    which is the difference between "the AI decided to chase this customer" and
    "somebody on the team did" everywhere the CRM shows it.
    """
    tenant, conversation = await _workspace(db_session)
    service = FollowUpService(session=db_session, tenant_id=tenant.id)

    follow_up = await service.schedule(
        conversation_id=conversation.id,
        delay=timedelta(minutes=30),
        body="Still interested?",
        created_by_kind=ActorKind.AGENT,
    )
    await db_session.flush()
    await db_session.refresh(follow_up)

    assert follow_up.created_by_kind is ActorKind.AGENT
    assert follow_up.created_by_id is None

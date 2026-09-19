"""The CRM invariant sweep, over a population built to break them (item 54).

A sweep over an empty database proves nothing, so this one first builds, in
committed transactions and through the real services, every shape the CRM
audit found violating an invariant: several workspaces; manual takeovers and
automatic handoffs, including ones that raced; conversations assigned,
reassigned and released; a colleague removed while owning work; human lead
edits racing agent extraction; a lead won while another move raced it; a lost
lead reopened behind a newer one; colleague and agent follow-ups, rescheduled
and cancelled inside the sweep's lease and while being sent. It asserts the
population is really there, then that every invariant in `crm_invariants`
holds over it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.exceptions import ConflictError, ValidationError
from app.db.models.analytics import AnalyticsEvent, AnalyticsEventType, AnalyticsSource
from app.db.models.enums import TenantRole
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind, Lead, LeadStatus
from app.services.follow_up_service import FollowUpService
from app.services.inbox_service import InboxService
from app.services.lead_service import ExtractedLead, LeadService, LeadUpdate
from app.services.membership_service import MembershipService
from tests.integration.crm_harness import CrmWorld, World
from tests.integration.crm_invariants import sweep
from tests.integration.test_crm_follow_up_claims import GatedMessaging, _claim, _due, _worker

pytestmark = pytest.mark.integration


async def _ownership(crm: CrmWorld, world: World) -> None:
    """Takeover, a raced automatic handoff, release, and a second handoff."""
    async with crm.session() as colleague, crm.session() as automation:
        await InboxService(session=colleague, tenant_id=world.tenant_id).take_over(
            conversation_id=world.conversation_id, actor=world.alice, reason="VIP"
        )
        racing = asyncio.create_task(
            InboxService(session=automation, tenant_id=world.tenant_id).hand_off(
                conversation_id=world.conversation_id,
                reason="Escalated automatically",
                source=AnalyticsSource.SENTIMENT,
            )
        )
        await crm.lock_waiter()
        await colleague.commit()
        await racing
        await automation.commit()
    async with crm.session() as session:
        inbox = InboxService(session=session, tenant_id=world.tenant_id)
        await inbox.release_to_ai(conversation_id=world.conversation_id, actor=world.alice)
        await inbox.hand_off(
            conversation_id=world.conversation_id,
            reason="I cannot help with that.",
            source=AnalyticsSource.AGENT,
        )
        await inbox.assign(
            conversation_id=world.other_conversation_id,
            assigned_to_id=world.bob.id,
            expected_assigned_to_id=None,
            actor=world.owner,
        )
        await session.commit()


async def _leads(crm: CrmWorld, world: World) -> None:
    """Human edits racing extraction, then a won deal racing another move."""
    async with crm.session() as person, crm.session() as agent:
        await LeadService(session=person, tenant_id=world.tenant_id).update_lead(
            lead_id=world.lead_id,
            actor_id=world.alice.id,
            update=LeadUpdate(email="corrected@example.com"),
        )
        racing = asyncio.create_task(
            LeadService(session=agent, tenant_id=world.tenant_id).capture_from_conversation(
                conversation_id=world.conversation_id,
                extracted=ExtractedLead(name="Ahmed H.", email="old.address@example.com"),
            )
        )
        await crm.lock_waiter()
        await person.commit()
        await racing
        await agent.commit()
    async with crm.session() as session:
        service = LeadService(session=session, tenant_id=world.tenant_id)
        await service.change_status(
            lead_id=world.lead_id, status=LeadStatus.QUALIFIED, actor_id=world.owner.id
        )
        await service.assign(
            lead_id=world.lead_id,
            assigned_to_id=world.bob.id,
            expected_assigned_to_id=None,
            actor_id=world.owner.id,
        )
        await session.commit()
    async with crm.session() as winning, crm.session() as losing:
        await LeadService(session=winning, tenant_id=world.tenant_id).change_status(
            lead_id=world.lead_id, status=LeadStatus.WON, actor_id=world.alice.id
        )
        racing_move = asyncio.create_task(
            LeadService(session=losing, tenant_id=world.tenant_id).change_status(
                lead_id=world.lead_id, status=LeadStatus.PROPOSAL, actor_id=world.bob.id
            )
        )
        await crm.lock_waiter()
        await winning.commit()
        with pytest.raises(ValidationError):
            await racing_move
        await losing.rollback()


async def _lost_and_reopened(crm: CrmWorld, world: World) -> None:
    """A lost lead whose customer opened a newer one: the reopen is refused."""
    async with crm.session() as session:
        service = LeadService(session=session, tenant_id=world.tenant_id)
        lead = await service.create_lead(
            actor_id=world.owner.id, contact_id=world.other_contact_id, name="First"
        )
        await session.commit()
    async with crm.session() as session:
        service = LeadService(session=session, tenant_id=world.tenant_id)
        await service.change_status(lead_id=lead.id, status=LeadStatus.LOST, actor_id=None)
        await session.commit()
    async with crm.session() as session:
        await LeadService(session=session, tenant_id=world.tenant_id).create_lead(
            actor_id=world.owner.id, contact_id=world.other_contact_id, name="Second"
        )
        await session.commit()
    async with crm.session() as session:
        with pytest.raises(ConflictError):
            await LeadService(session=session, tenant_id=world.tenant_id).change_status(
                lead_id=lead.id, status=LeadStatus.NEW, actor_id=None
            )
        await session.rollback()


async def _follow_ups(crm: CrmWorld, world: World) -> None:
    """Colleague and agent nudges: rescheduled, cancelled and sent across the lease."""
    rescheduled = await _due(crm, world)
    tokens = await _claim(crm)
    async with crm.session() as session:
        await FollowUpService(session=session, tenant_id=world.tenant_id).schedule(
            conversation_id=world.conversation_id,
            scheduled_at=datetime.now(UTC) + timedelta(days=3),
            body="Thursday",
            created_by_id=world.alice.id,
        )
        await session.commit()
    await _worker(crm, GatedMessaging())._dispatch_one(
        rescheduled, world.tenant_id, tokens[rescheduled]
    )

    async with crm.session() as session:
        agent_nudge = FollowUp(
            tenant_id=world.tenant_id,
            conversation_id=world.other_conversation_id,
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
            body="Agent nudge",
            created_by_kind=ActorKind.AGENT,
        )
        session.add(agent_nudge)
        await session.commit()
    gate = GatedMessaging()
    gate.hold_before_meta.clear()
    tokens = await _claim(crm)
    dispatch = asyncio.create_task(
        _worker(crm, gate)._dispatch_one(agent_nudge.id, world.tenant_id, tokens[agent_nudge.id])
    )
    await asyncio.wait_for(gate.intent_committed.wait(), timeout=20)
    async with crm.session() as session:
        with pytest.raises(ConflictError):
            await FollowUpService(session=session, tenant_id=world.tenant_id).cancel(
                follow_up_id=agent_nudge.id
            )
        await session.rollback()
    gate.hold_before_meta.set()
    await dispatch


async def _removal(crm: CrmWorld, world: World) -> None:
    """Bob owns a conversation and a lead and has a reminder waiting; he is removed."""
    async with crm.session() as session:
        await FollowUpService(session=session, tenant_id=world.tenant_id).schedule(
            conversation_id=world.other_conversation_id,
            delay=timedelta(hours=2),
            body="Bob's reminder",
            created_by_id=world.bob.id,
        )
        await session.commit()
    async with crm.session() as session:
        await MembershipService(session=session, tenant_id=world.tenant_id).revoke(
            actor=world.owner, actor_role=TenantRole.TENANT_OWNER, user_id=world.bob.id
        )
        await session.commit()


async def test_every_crm_invariant_holds_over_a_population_built_to_break_them(
    crm: CrmWorld,
) -> None:
    worlds = [await crm.world() for _ in range(3)]
    for world in worlds:
        await _leads(crm, world)
        await _ownership(crm, world)
        await _lost_and_reopened(crm, world)
        await _follow_ups(crm, world)
        await _removal(crm, world)
    tenants = [world.tenant_id for world in worlds]

    async with crm.session() as session:
        population = {
            "handoffs": await session.scalar(
                select(func.count())
                .select_from(AnalyticsEvent)
                .where(
                    AnalyticsEvent.tenant_id.in_(tenants),
                    AnalyticsEvent.event_type == AnalyticsEventType.HANDOFF,
                )
            ),
            "won_leads": await session.scalar(
                select(func.count())
                .select_from(Lead)
                .where(Lead.tenant_id.in_(tenants), Lead.status == LeadStatus.WON)
            ),
            "cancelled_reminders": await session.scalar(
                select(func.count())
                .select_from(FollowUp)
                .where(
                    FollowUp.tenant_id.in_(tenants),
                    FollowUp.cancelled_reason == "member_revoked",
                )
            ),
            "sent_follow_ups": await session.scalar(
                select(func.count())
                .select_from(FollowUp)
                .where(FollowUp.tenant_id.in_(tenants), FollowUp.status == FollowUpStatus.SENT)
            ),
            "rescheduled_pending": await session.scalar(
                select(func.count())
                .select_from(FollowUp)
                .where(FollowUp.tenant_id.in_(tenants), FollowUp.body == "Thursday")
            ),
        }
        # Non-vacuous: every shape the sweep is about exists, in every workspace.
        assert population == {
            "handoffs": 6,
            "won_leads": 3,
            "cancelled_reminders": 3,
            "sent_follow_ups": 3,
            "rescheduled_pending": 3,
        }

        violations = await sweep(session, tenants)

    assert violations == dict.fromkeys(violations, 0)

"""Human CRM writes racing each other, on real PostgreSQL (TG-4).

The CRM audit found not one lock, version check or state precondition on any
human CRM write path, and every race in its matrix was invisible to the suite
because the suite had no concurrent test of a human write at all. These are
those races, made permanent.

Every test runs two real sessions on two real connections and forces the
interleaving: the first writer takes its lock and stops; the second is started
and `lock_waiter` proves it is *blocked on the first's lock* - not merely
later - before the first commits. That is the difference between a race test
and two sequential calls, and it is what lets a mutation that removes the lock
fail here rather than pass by luck.

What each one asserts is the invariant the finding broke, read back from the
committed database: who owns the conversation, what the reason says, how many
handoffs and audit rows exist, what the lead says and who is protected.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from app.core.exceptions import ConflictError, TenantIsolationError, ValidationError
from app.db.models.analytics import AnalyticsSource
from app.db.models.audit import AuditAction
from app.db.models.conversation import ConversationMode
from app.db.models.enums import MembershipStatus, TenantRole
from app.db.models.lead import LeadActivityKind, LeadStatus
from app.db.models.membership import Membership
from app.services.inbox_service import STALE_ASSIGNMENT, InboxService
from app.services.lead_service import STALE_LEAD_STATUS, ExtractedLead, LeadService, LeadUpdate
from app.services.membership_service import MembershipService
from tests.integration.crm_harness import CrmWorld, World

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------- takeover


async def test_two_takeovers_make_one_handoff_and_the_first_colleague_keeps_it(
    crm: CrmWorld,
) -> None:
    """CRM-03: both used to commit, the reason was the last writer's, two events."""
    world = await crm.world()
    async with crm.session() as first, crm.session() as second:
        taken = await InboxService(session=first, tenant_id=world.tenant_id).take_over(
            conversation_id=world.conversation_id,
            actor=world.alice,
            reason="alice: VIP - call personally",
        )
        assert taken.changed

        racing = asyncio.create_task(
            InboxService(session=second, tenant_id=world.tenant_id).take_over(
                conversation_id=world.conversation_id,
                actor=world.bob,
                reason="bob: pricing question",
            )
        )
        await crm.lock_waiter()
        await first.commit()
        lost = await racing
        await second.commit()

    assert lost.changed is False
    conversation = await crm.conversation(world)
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.handoff_reason == "alice: VIP - call personally"
    assert conversation.assigned_to_id == world.alice.id
    assert await crm.handoffs(world) == 1
    taken_over = await crm.audit(world, AuditAction.CONVERSATION_TAKEN_OVER)
    assert len(taken_over) == 1
    assert taken_over[0].actor_id == world.alice.id


async def test_taking_over_an_owned_conversation_steals_nothing(
    crm: CrmWorld,
) -> None:
    """PD-CRM-3 / PD-CRM-5: HUMAN -> HUMAN is a no-op, an empty reason included."""
    world = await crm.world()
    async with crm.session() as session:
        await InboxService(session=session, tenant_id=world.tenant_id).take_over(
            conversation_id=world.conversation_id,
            actor=world.alice,
            reason="alice's reason",
        )
        await session.commit()
    async with crm.session() as session:
        again = await InboxService(session=session, tenant_id=world.tenant_id).take_over(
            conversation_id=world.conversation_id,
            actor=world.bob,
            reason=None,
        )
        await session.commit()

    assert again.changed is False
    conversation = await crm.conversation(world)
    assert conversation.handoff_reason == "alice's reason"
    assert conversation.assigned_to_id == world.alice.id
    assert await crm.handoffs(world) == 1
    assert len(await crm.audit(world, AuditAction.CONVERSATION_TAKEN_OVER)) == 1


async def test_an_automated_handoff_that_loses_to_a_takeover_changes_nothing(
    crm: CrmWorld,
) -> None:
    """CRM-02 at the transition itself: the colleague committed first."""
    world = await crm.world()
    async with crm.session() as colleague, crm.session() as automation:
        await InboxService(session=colleague, tenant_id=world.tenant_id).take_over(
            conversation_id=world.conversation_id,
            actor=world.alice,
            reason="VIP - call personally",
        )
        racing = asyncio.create_task(
            InboxService(session=automation, tenant_id=world.tenant_id).hand_off(
                conversation_id=world.conversation_id,
                reason="Escalated automatically: the customer sounds angry.",
                source=AnalyticsSource.SENTIMENT,
            )
        )
        await crm.lock_waiter()
        await colleague.commit()
        lost = await racing
        await automation.commit()

    assert lost.changed is False
    conversation = await crm.conversation(world)
    assert conversation.handoff_reason == "VIP - call personally"
    assert conversation.assigned_to_id == world.alice.id
    assert await crm.handoffs(world) == 1


async def test_an_automated_handoff_does_not_invent_an_owner(
    crm: CrmWorld,
) -> None:
    """PD-CRM-3: automation hands over; it assigns nobody, and keeps an existing owner."""
    world = await crm.world()
    async with crm.session() as session:
        inbox = InboxService(session=session, tenant_id=world.tenant_id)
        moved = await inbox.hand_off(
            conversation_id=world.conversation_id,
            reason="I cannot help with refunds.",
            source=AnalyticsSource.AGENT,
        )
        await session.commit()

    assert moved.changed
    conversation = await crm.conversation(world)
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.assigned_to_id is None
    [entry] = await crm.audit(world, AuditAction.CONVERSATION_TAKEN_OVER)
    assert entry.actor_kind.value == "agent"
    assert entry.meta is not None and entry.meta["source"] == "agent"
    # The reason's text stays on the conversation row and out of the trail.
    assert "refunds" not in str(entry.meta)


# --------------------------------------------------------------- assignment


async def test_two_colleagues_self_assigning_at_once_one_wins_and_one_is_told(
    crm: CrmWorld,
) -> None:
    """CRM-04: both used to be told they owned the customer."""
    world = await crm.world()
    async with crm.session() as first, crm.session() as second:
        await InboxService(session=first, tenant_id=world.tenant_id).assign(
            conversation_id=world.conversation_id,
            assigned_to_id=world.alice.id,
            expected_assigned_to_id=None,
            actor=world.alice,
        )
        racing = asyncio.create_task(
            InboxService(session=second, tenant_id=world.tenant_id).assign(
                conversation_id=world.conversation_id,
                assigned_to_id=world.bob.id,
                expected_assigned_to_id=None,
                actor=world.bob,
            )
        )
        await crm.lock_waiter()
        await first.commit()
        with pytest.raises(ConflictError) as refused:
            await racing
        await second.rollback()

    assert refused.value.error_code == STALE_ASSIGNMENT
    assert (await crm.conversation(world)).assigned_to_id == world.alice.id
    assert len(await crm.audit(world, AuditAction.CONVERSATION_ASSIGNED)) == 1


async def test_a_stale_unassign_cannot_erase_a_newer_reassignment(
    crm: CrmWorld,
) -> None:
    """CRM-04: alice's screen still says she owns it; the owner moved it to bob."""
    world = await crm.world()
    async with crm.session() as session:
        await InboxService(session=session, tenant_id=world.tenant_id).assign(
            conversation_id=world.conversation_id,
            assigned_to_id=world.alice.id,
            expected_assigned_to_id=None,
            actor=world.alice,
        )
        await session.commit()

    async with crm.session() as manager, crm.session() as stale:
        await InboxService(session=manager, tenant_id=world.tenant_id).assign(
            conversation_id=world.conversation_id,
            assigned_to_id=world.bob.id,
            expected_assigned_to_id=world.alice.id,
            actor=world.owner,
        )
        racing = asyncio.create_task(
            InboxService(session=stale, tenant_id=world.tenant_id).assign(
                conversation_id=world.conversation_id,
                assigned_to_id=None,
                expected_assigned_to_id=world.alice.id,
                actor=world.alice,
            )
        )
        await crm.lock_waiter()
        await manager.commit()
        with pytest.raises(ConflictError):
            await racing
        await stale.rollback()

    assert (await crm.conversation(world)).assigned_to_id == world.bob.id
    [reassigned] = await crm.audit(world, AuditAction.CONVERSATION_REASSIGNED)
    assert reassigned.meta is not None
    assert reassigned.meta["previous_assignee_id"] == str(world.alice.id)
    assert reassigned.meta["new_assignee_id"] == str(world.bob.id)
    assert await crm.audit(world, AuditAction.CONVERSATION_UNASSIGNED) == []


# ------------------------------------------------------ removal vs assignment


async def test_an_assignment_that_reaches_the_member_first_is_undone_by_their_removal(
    crm: CrmWorld,
) -> None:
    """CRM-11, assignment first: the removal waits for it, then releases it."""
    world = await crm.world()
    async with crm.session() as assigning, crm.session() as removing:
        await InboxService(session=assigning, tenant_id=world.tenant_id).assign(
            conversation_id=world.conversation_id,
            assigned_to_id=world.bob.id,
            expected_assigned_to_id=None,
            actor=world.alice,
        )
        racing = asyncio.create_task(
            MembershipService(session=removing, tenant_id=world.tenant_id).revoke(
                actor=world.owner,
                actor_role=TenantRole.TENANT_OWNER,
                user_id=world.bob.id,
            )
        )
        await crm.lock_waiter()
        await assigning.commit()
        await racing
        await removing.commit()

    assert (await crm.conversation(world)).assigned_to_id is None
    assert await _membership_status(crm, world, world.bob.id) is MembershipStatus.REVOKED
    [released] = await crm.audit(world, AuditAction.CONVERSATION_UNASSIGNED)
    assert released.meta is not None and released.meta["cause"] == "member_revoked"


async def test_an_assignment_that_reaches_the_member_second_is_refused(
    crm: CrmWorld,
) -> None:
    """CRM-11, removal first: the assignment waits, then finds no active member."""
    world = await crm.world()
    async with crm.session() as removing, crm.session() as assigning:
        await MembershipService(session=removing, tenant_id=world.tenant_id).revoke(
            actor=world.owner,
            actor_role=TenantRole.TENANT_OWNER,
            user_id=world.bob.id,
        )
        racing = asyncio.create_task(
            InboxService(session=assigning, tenant_id=world.tenant_id).assign(
                conversation_id=world.conversation_id,
                assigned_to_id=world.bob.id,
                expected_assigned_to_id=None,
                actor=world.alice,
            )
        )
        await crm.lock_waiter()
        await removing.commit()
        with pytest.raises(TenantIsolationError):
            await racing
        await assigning.rollback()

    assert (await crm.conversation(world)).assigned_to_id is None


async def test_a_lead_assignment_racing_the_removal_lands_on_nobody(
    crm: CrmWorld,
) -> None:
    """CRM-11 for the pipeline: the same serialisation protects lead ownership."""
    world = await crm.world()
    async with crm.session() as assigning, crm.session() as removing:
        await LeadService(session=assigning, tenant_id=world.tenant_id).assign(
            lead_id=world.lead_id,
            assigned_to_id=world.bob.id,
            expected_assigned_to_id=None,
            actor_id=world.owner.id,
        )
        racing = asyncio.create_task(
            MembershipService(session=removing, tenant_id=world.tenant_id).revoke(
                actor=world.owner,
                actor_role=TenantRole.TENANT_OWNER,
                user_id=world.bob.id,
            )
        )
        await crm.lock_waiter()
        await assigning.commit()
        await racing
        await removing.commit()

    assert (await crm.lead(world)).assigned_to_id is None
    kinds = [row.kind for row in await crm.timeline(world)]
    assert kinds[-2:] == [LeadActivityKind.ASSIGNED, LeadActivityKind.UNASSIGNED]


# ------------------------------------------------------------ lead fields


async def test_an_extraction_cannot_overwrite_a_correction_committed_before_it_applied(
    crm: CrmWorld,
) -> None:
    """CRM-06 (P2d): the model composed the old address; the colleague corrected it."""
    world = await crm.world()
    async with crm.session() as person, crm.session() as agent:
        # The turn's transaction is already open - it began before the tool ran,
        # exactly as a turn's does - so a timeline ordered by transaction start
        # would put the agent's later write first (CRM-16).
        await agent.execute(text("SELECT 1"))
        await LeadService(session=person, tenant_id=world.tenant_id).update_lead(
            lead_id=world.lead_id,
            actor_id=world.alice.id,
            update=LeadUpdate(email="corrected@example.com"),
        )
        racing = asyncio.create_task(
            LeadService(session=agent, tenant_id=world.tenant_id).capture_from_conversation(
                conversation_id=world.conversation_id,
                extracted=ExtractedLead(email="old.address@example.com", interest="finishing"),
            )
        )
        await crm.lock_waiter()
        await person.commit()
        capture = await racing
        await agent.commit()

    lead = await crm.lead(world)
    assert lead.email == "corrected@example.com"
    assert "email" in lead.human_verified_fields
    assert lead.interest == "finishing"
    assert "email" not in capture.changed_fields
    # The timeline agrees with the row: the person's edit, then the agent's,
    # and the agent's does not claim the email (CRM-16).
    timeline = [(row.actor_kind.value, row.summary) for row in await crm.timeline(world)]
    assert timeline[-2:] == [("user", "Updated email."), ("agent", "Agent updated interest.")]


async def test_two_colleagues_editing_different_fields_both_stay_verified(
    crm: CrmWorld,
) -> None:
    """CRM-06 (P3e): the verified list is a union, never a stale overwrite."""
    world = await crm.world()
    async with crm.session() as first, crm.session() as second:
        await LeadService(session=first, tenant_id=world.tenant_id).update_lead(
            lead_id=world.lead_id,
            actor_id=world.alice.id,
            update=LeadUpdate(name="A. Hassan"),
        )
        racing = asyncio.create_task(
            LeadService(session=second, tenant_id=world.tenant_id).update_lead(
                lead_id=world.lead_id,
                actor_id=world.bob.id,
                update=LeadUpdate(email="ahmed@corp.example"),
            )
        )
        await crm.lock_waiter()
        await first.commit()
        await racing
        await second.commit()

    assert (await crm.lead(world)).human_verified_fields == ["email", "name"]

    async with crm.session() as agent:
        capture = await LeadService(
            session=agent, tenant_id=world.tenant_id
        ).capture_from_conversation(
            conversation_id=world.conversation_id,
            extracted=ExtractedLead(name="A. H.", email="guess@example.com"),
        )
        await agent.commit()

    lead = await crm.lead(world)
    assert (lead.name, lead.email) == ("A. Hassan", "ahmed@corp.example")
    assert capture.changed_fields == frozenset()


# ------------------------------------------------------------ lead status


async def test_won_and_proposal_from_one_state_exactly_one_succeeds(
    crm: CrmWorld,
) -> None:
    """CRM-07 (P3f): a won deal used to return to the pipeline with closed_at set."""
    world = await crm.world()
    async with crm.session() as session:
        await LeadService(session=session, tenant_id=world.tenant_id).change_status(
            lead_id=world.lead_id, status=LeadStatus.QUALIFIED, actor_id=world.owner.id
        )
        await session.commit()

    async with crm.session() as winning, crm.session() as losing:
        await LeadService(session=winning, tenant_id=world.tenant_id).change_status(
            lead_id=world.lead_id,
            status=LeadStatus.WON,
            expected_status=LeadStatus.QUALIFIED,
            actor_id=world.alice.id,
        )
        racing = asyncio.create_task(
            LeadService(session=losing, tenant_id=world.tenant_id).change_status(
                lead_id=world.lead_id,
                status=LeadStatus.PROPOSAL,
                expected_status=LeadStatus.QUALIFIED,
                actor_id=world.bob.id,
            )
        )
        await crm.lock_waiter()
        await winning.commit()
        with pytest.raises(ConflictError) as refused:
            await racing
        await losing.rollback()

    assert refused.value.error_code == STALE_LEAD_STATUS
    lead = await crm.lead(world)
    assert lead.status is LeadStatus.WON
    assert lead.closed_at is not None
    exits = [
        row.data
        for row in await crm.timeline(world)
        if row.kind is LeadActivityKind.STATUS_CHANGED
        and row.data
        and row.data["from"] == "qualified"
    ]
    assert exits == [{"from": "qualified", "to": "won", "reason": None}]


async def test_a_stale_move_without_a_precondition_cannot_leave_won(
    crm: CrmWorld,
) -> None:
    """CRM-07: judged against the committed status even when the caller sends none."""
    world = await crm.world()
    async with crm.session() as session:
        service = LeadService(session=session, tenant_id=world.tenant_id)
        await service.change_status(
            lead_id=world.lead_id, status=LeadStatus.QUALIFIED, actor_id=world.owner.id
        )
        await session.commit()

    async with crm.session() as winning, crm.session() as losing:
        await LeadService(session=winning, tenant_id=world.tenant_id).change_status(
            lead_id=world.lead_id, status=LeadStatus.WON, actor_id=world.alice.id
        )
        racing = asyncio.create_task(
            LeadService(session=losing, tenant_id=world.tenant_id).change_status(
                lead_id=world.lead_id, status=LeadStatus.PROPOSAL, actor_id=world.bob.id
            )
        )
        await crm.lock_waiter()
        await winning.commit()
        with pytest.raises(ValidationError):
            await racing
        await losing.rollback()

    lead = await crm.lead(world)
    assert lead.status is LeadStatus.WON
    assert lead.closed_at is not None


# ---------------------------------------------------------------- helpers


async def _membership_status(crm: CrmWorld, world: World, user_id: uuid.UUID) -> MembershipStatus:
    async with crm.session() as session:
        status = await session.scalar(
            select(Membership.status).where(
                Membership.tenant_id == world.tenant_id,
                Membership.user_id == user_id,
            )
        )
        assert status is not None
        return status

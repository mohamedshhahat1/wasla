"""Lead management against real PostgreSQL.

The properties worth proving here cannot be checked without the database. The
duplicate-lead guarantee is a partial unique index, not a service check, so a
mock would only prove the mock. Cross-tenant isolation is a predicate on a
query. And the activity log's value depends on rows actually landing.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, TenantIsolationError, ValidationError
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
)
from app.db.models.enums import TenantRole
from app.db.models.lead import (
    ActorKind,
    Lead,
    LeadActivityKind,
    LeadSource,
    LeadStatus,
)
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.lead_repository import LeadFilters, LeadRepository
from app.services.lead_service import ExtractedLead, LeadService, LeadUpdate

pytestmark = pytest.mark.integration


async def _tenant(session: AsyncSession, *, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _user(
    session: AsyncSession,
    *,
    tenant: Tenant,
    email: str,
    role: TenantRole = TenantRole.MEMBER,
) -> User:
    user = User(email=email, hashed_password="x" * 60, is_active=True)
    session.add(user)
    await session.flush()
    session.add(Membership(user_id=user.id, tenant_id=tenant.id, role=role))
    await session.flush()
    return user


async def _conversation(session: AsyncSession, *, tenant: Tenant, wa_id: str) -> Conversation:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{tenant.slug}",
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
    )
    session.add(conversation)
    await session.flush()
    return conversation


def _service(session: AsyncSession, tenant: Tenant) -> LeadService:
    return LeadService(session=session, tenant_id=tenant.id)


# --------------------------------------------------------------- tenant isolation


async def test_one_workspace_cannot_read_another_workspaces_lead(db_session: AsyncSession) -> None:
    """The property this whole system is built to guarantee."""
    acme = await _tenant(db_session, slug="acme")
    rival = await _tenant(db_session, slug="rival")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")

    lead = await _service(db_session, acme).create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()

    with pytest.raises(TenantIsolationError):
        await _service(db_session, rival).get_lead(lead.id)


async def test_a_lead_list_never_crosses_workspaces(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    rival = await _tenant(db_session, slug="rival")
    acme_owner = await _user(db_session, tenant=acme, email="a@example.com")
    rival_owner = await _user(db_session, tenant=rival, email="b@example.com")

    await _service(db_session, acme).create_lead(actor_id=acme_owner.id, name="Acme customer")
    await _service(db_session, rival).create_lead(actor_id=rival_owner.id, name="Rival customer")
    await db_session.flush()

    page = await _service(db_session, acme).list_leads()

    assert [lead.name for lead in page.items] == ["Acme customer"]


async def test_a_search_cannot_reach_across_workspaces(db_session: AsyncSession) -> None:
    """A filter must narrow within the tenant, never widen beyond it."""
    # Acme needs no user of its own here: the point is that its empty pipeline
    # stays empty even when the search term matches a lead next door.
    acme = await _tenant(db_session, slug="acme")
    rival = await _tenant(db_session, slug="rival")
    rival_owner = await _user(db_session, tenant=rival, email="b@example.com")

    await _service(db_session, rival).create_lead(actor_id=rival_owner.id, name="Ahmed Hassan")
    await db_session.flush()

    page = await _service(db_session, acme).list_leads(filters=LeadFilters(search="Ahmed"))

    assert page.items == []


async def test_statistics_count_only_the_callers_workspace(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    rival = await _tenant(db_session, slug="rival")
    acme_owner = await _user(db_session, tenant=acme, email="a@example.com")
    rival_owner = await _user(db_session, tenant=rival, email="b@example.com")

    await _service(db_session, acme).create_lead(actor_id=acme_owner.id, name="One")
    for index in range(3):
        await _service(db_session, rival).create_lead(actor_id=rival_owner.id, name=f"R{index}")
    await db_session.flush()

    statistics = await _service(db_session, acme).statistics()

    assert statistics.total == 1


async def test_a_note_cannot_be_added_to_another_workspaces_lead(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    rival = await _tenant(db_session, slug="rival")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    intruder = await _user(db_session, tenant=rival, email="intruder@example.com")

    lead = await _service(db_session, acme).create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()

    with pytest.raises(TenantIsolationError):
        await _service(db_session, rival).add_note(
            lead_id=lead.id,
            body="Should not land.",
            author_id=intruder.id,
        )


async def test_a_lead_cannot_be_assigned_to_someone_outside_the_workspace(
    db_session: AsyncSession,
) -> None:
    """The id arrives in a request body, so membership is verified not assumed."""
    acme = await _tenant(db_session, slug="acme")
    rival = await _tenant(db_session, slug="rival")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    outsider = await _user(db_session, tenant=rival, email="outsider@example.com")

    lead = await _service(db_session, acme).create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()

    with pytest.raises(TenantIsolationError):
        await _service(db_session, acme).assign(
            lead_id=lead.id,
            assigned_to_id=outsider.id,
            actor_id=owner.id,
        )


# ------------------------------------------------------------ duplicate prevention


async def test_an_agent_updates_the_open_lead_rather_than_creating_another(
    db_session: AsyncSession,
) -> None:
    """The idempotency that keeps a chatty conversation from making five leads."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000001")
    service = _service(db_session, acme)

    first = await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(name="Ahmed"),
    )
    second = await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(interest="Apartment finishing"),
    )
    await db_session.flush()

    assert first.id == second.id
    assert second.name == "Ahmed"
    assert second.interest == "Apartment finishing"

    page = await service.list_leads()
    assert len(page.items) == 1


async def test_creating_a_second_open_lead_for_a_customer_is_refused(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000002")
    service = _service(db_session, acme)

    await service.create_lead(actor_id=owner.id, contact_id=conversation.contact_id)
    await db_session.flush()

    with pytest.raises(ConflictError):
        await service.create_lead(actor_id=owner.id, contact_id=conversation.contact_id)


async def test_the_database_itself_refuses_a_second_open_lead(db_session: AsyncSession) -> None:
    """The service check is a courtesy; this index is the guarantee.

    Two webhook deliveries can be in flight at once, and only a constraint
    settles that race.
    """
    acme = await _tenant(db_session, slug="acme")
    contact = Contact(tenant_id=acme.id, wa_id="201000000003")
    db_session.add(contact)
    await db_session.flush()

    db_session.add_all(
        [
            Lead(tenant_id=acme.id, contact_id=contact.id, source=LeadSource.AGENT),
            Lead(tenant_id=acme.id, contact_id=contact.id, source=LeadSource.AGENT),
        ]
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_a_returning_customer_starts_a_fresh_lead(db_session: AsyncSession) -> None:
    """A closed lead releases the slot; reanimating it would lose the old deal."""
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000004")
    service = _service(db_session, acme)

    first = await service.create_lead(actor_id=owner.id, contact_id=conversation.contact_id)
    await db_session.flush()
    await service.change_status(
        lead_id=first.id,
        status=LeadStatus.QUALIFIED,
        actor_id=owner.id,
    )
    await service.change_status(lead_id=first.id, status=LeadStatus.WON, actor_id=owner.id)
    await db_session.flush()

    second = await service.create_lead(actor_id=owner.id, contact_id=conversation.contact_id)
    await db_session.flush()

    assert second.id != first.id
    assert first.status is LeadStatus.WON


async def test_leads_entered_without_a_contact_do_not_collide(db_session: AsyncSession) -> None:
    """The index is partial, so a null contact is not a shared slot."""
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    await service.create_lead(actor_id=owner.id, name="Walk-in one")
    await service.create_lead(actor_id=owner.id, name="Walk-in two")
    await db_session.flush()

    page = await service.list_leads()
    assert len(page.items) == 2


# --------------------------------------------------------- AI never overwrites a human


async def test_extraction_does_not_overwrite_what_a_person_entered(
    db_session: AsyncSession,
) -> None:
    """The rule the whole provenance mechanism exists for."""
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000005")
    service = _service(db_session, acme)

    lead = await service.create_lead(
        actor_id=owner.id,
        contact_id=conversation.contact_id,
        name="Ahmed Hassan",
    )
    await db_session.flush()

    await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(name="Ahmad", interest="Apartment finishing"),
    )
    await db_session.flush()

    # The human's spelling survives; the blank field is filled in.
    assert lead.name == "Ahmed Hassan"
    assert lead.interest == "Apartment finishing"


async def test_an_agent_may_correct_its_own_earlier_guess(db_session: AsyncSession) -> None:
    """Protection covers human entries only, or the AI could never improve."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000006")
    service = _service(db_session, acme)

    await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(interest="Something vague"),
    )
    lead = await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(interest="150m apartment finishing"),
    )
    await db_session.flush()

    assert lead.interest == "150m apartment finishing"


async def test_a_field_a_person_deliberately_cleared_stays_cleared(
    db_session: AsyncSession,
) -> None:
    """ "This customer has no email" is knowledge, not an empty slot to fill."""
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000007")
    service = _service(db_session, acme)

    lead = await service.create_lead(
        actor_id=owner.id,
        contact_id=conversation.contact_id,
        email="typo@example.com",
    )
    await db_session.flush()
    await service.update_lead(
        lead_id=lead.id,
        actor_id=owner.id,
        update=LeadUpdate(email=None),
    )
    await db_session.flush()

    await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(email="guessed@example.com"),
    )
    await db_session.flush()

    assert lead.email is None


async def test_confirming_an_unchanged_value_still_protects_it(db_session: AsyncSession) -> None:
    """A person vouching for what is already there is still a person vouching."""
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000008")
    service = _service(db_session, acme)

    lead = await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(name="Ahmed"),
    )
    await db_session.flush()

    # The same value a person now confirms by hand.
    await service.update_lead(lead_id=lead.id, actor_id=owner.id, update=LeadUpdate(name="Ahmed"))
    await db_session.flush()

    await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(name="Ahmad"),
    )
    await db_session.flush()

    assert lead.name == "Ahmed"
    assert "name" in lead.human_verified_fields


async def test_an_agent_cannot_reach_judgement_fields(db_session: AsyncSession) -> None:
    """Status and score are decisions, and extraction offers no route to them."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000009")
    service = _service(db_session, acme)

    lead = await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(name="Ahmed"),
    )
    await db_session.flush()

    assert lead.status is LeadStatus.NEW
    assert lead.score == 0
    assert lead.assigned_to_id is None


async def test_extraction_stops_once_a_colleague_takes_over(db_session: AsyncSession) -> None:
    """A job queued before a handoff must not edit the record after it."""
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000010")
    conversation.mode = ConversationMode.HUMAN
    await db_session.flush()

    with pytest.raises(ConflictError):
        await _service(db_session, acme).capture_from_conversation(
            conversation_id=conversation.id,
            extracted=ExtractedLead(name="Ahmed"),
        )


async def test_a_bad_extracted_value_is_dropped_without_losing_the_rest(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000011")

    lead = await _service(db_session, acme).capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(name="Ahmed", email="not-an-email", budget_amount="500k"),
    )
    await db_session.flush()

    assert lead.name == "Ahmed"
    assert lead.email is None
    assert lead.budget_amount is None


async def test_a_captured_lead_records_where_it_came_from(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000012")

    lead = await _service(db_session, acme).capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(name="Ahmed"),
    )
    await db_session.flush()

    assert lead.source is LeadSource.AGENT
    assert lead.conversation_id == conversation.id
    assert lead.contact_id == conversation.contact_id


# ------------------------------------------------------------------ state transitions


async def test_a_lead_moves_through_the_pipeline_and_is_timestamped(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    lead = await service.create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()

    await service.change_status(lead_id=lead.id, status=LeadStatus.QUALIFIED, actor_id=owner.id)
    assert lead.qualified_at is not None
    assert lead.closed_at is None

    await service.change_status(lead_id=lead.id, status=LeadStatus.WON, actor_id=owner.id)
    assert lead.closed_at is not None


async def test_an_illegal_transition_is_refused(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    lead = await service.create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()
    await service.change_status(lead_id=lead.id, status=LeadStatus.QUALIFIED, actor_id=owner.id)
    await service.change_status(lead_id=lead.id, status=LeadStatus.WON, actor_id=owner.id)
    await db_session.flush()

    with pytest.raises(ValidationError):
        await service.change_status(lead_id=lead.id, status=LeadStatus.NEW, actor_id=owner.id)


async def test_reopening_a_lost_lead_clears_its_closing_date(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    lead = await service.create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()
    await service.change_status(lead_id=lead.id, status=LeadStatus.LOST, actor_id=owner.id)
    await db_session.flush()
    assert lead.closed_at is not None

    await service.change_status(lead_id=lead.id, status=LeadStatus.NEW, actor_id=owner.id)

    assert lead.closed_at is None


async def test_setting_the_current_status_again_changes_nothing(db_session: AsyncSession) -> None:
    """Retried jobs must be safe, and must not litter the timeline."""
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    lead = await service.create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()

    await service.change_status(lead_id=lead.id, status=LeadStatus.NEW, actor_id=owner.id)
    await db_session.flush()

    page = await service.list_activity(lead_id=lead.id)
    kinds = [activity.kind for activity in page.items]
    assert LeadActivityKind.STATUS_CHANGED not in kinds


# ------------------------------------------------------------------- activity log


async def test_every_change_leaves_an_auditable_trace(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    lead = await service.create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()
    await service.update_lead(
        lead_id=lead.id,
        actor_id=owner.id,
        update=LeadUpdate(interest="Apartment finishing"),
    )
    await service.change_status(lead_id=lead.id, status=LeadStatus.CONTACTED, actor_id=owner.id)
    await service.assign(lead_id=lead.id, assigned_to_id=owner.id, actor_id=owner.id)
    await service.add_note(lead_id=lead.id, body="Called, will call back.", author_id=owner.id)
    await db_session.flush()

    page = await service.list_activity(lead_id=lead.id)
    kinds = {activity.kind for activity in page.items}

    assert kinds == {
        LeadActivityKind.CREATED,
        LeadActivityKind.FIELDS_UPDATED,
        LeadActivityKind.STATUS_CHANGED,
        LeadActivityKind.ASSIGNED,
        LeadActivityKind.NOTE_ADDED,
    }


async def test_the_trail_says_who_made_each_change(db_session: AsyncSession) -> None:
    """A person and an agent must be distinguishable after the fact."""
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000013")
    service = _service(db_session, acme)

    lead = await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(name="Ahmed"),
    )
    await db_session.flush()
    await service.update_lead(lead_id=lead.id, actor_id=owner.id, update=LeadUpdate(name="Ahmed H"))
    await db_session.flush()

    page = await service.list_activity(lead_id=lead.id)
    by_kind = {activity.kind: activity for activity in page.items}

    assert by_kind[LeadActivityKind.CREATED].actor_kind is ActorKind.AGENT
    assert by_kind[LeadActivityKind.CREATED].actor_id is None
    assert by_kind[LeadActivityKind.FIELDS_UPDATED].actor_kind is ActorKind.USER
    assert by_kind[LeadActivityKind.FIELDS_UPDATED].actor_id == owner.id


async def test_an_update_records_the_previous_value(db_session: AsyncSession) -> None:
    """ "Why does this say half a million" needs an answer."""
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    lead = await service.create_lead(actor_id=owner.id, budget_amount=100000)
    await db_session.flush()
    await service.update_lead(
        lead_id=lead.id,
        actor_id=owner.id,
        update=LeadUpdate(budget_amount=500000),
    )
    await db_session.flush()

    page = await service.list_activity(lead_id=lead.id)
    updated = next(a for a in page.items if a.kind is LeadActivityKind.FIELDS_UPDATED)

    assert updated.data is not None
    # Decimals are stringified so the record survives JSONB.
    assert updated.data["budget_amount"] == {"from": "100000.00", "to": "500000.00"}


async def test_a_skipped_extraction_is_recorded_not_silent(db_session: AsyncSession) -> None:
    """Someone has to be able to see that the AI tried and was refused."""
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000014")
    service = _service(db_session, acme)

    lead = await service.create_lead(
        actor_id=owner.id,
        contact_id=conversation.contact_id,
        name="Ahmed Hassan",
    )
    await db_session.flush()
    await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(name="Ahmad", interest="Finishing"),
    )
    await db_session.flush()

    page = await service.list_activity(lead_id=lead.id)
    agent_update = next(
        a
        for a in page.items
        if a.kind is LeadActivityKind.FIELDS_UPDATED and a.actor_kind is ActorKind.AGENT
    )

    assert agent_update.data is not None
    assert agent_update.data["skipped_verified"] == ["name"]


# ------------------------------------------------------ notes, scoring and assignment


async def test_a_note_is_attached_and_attributed(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    lead = await service.create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()
    await service.add_note(lead_id=lead.id, body="  Wants a call Tuesday.  ", author_id=owner.id)
    await db_session.flush()

    page = await service.list_notes(lead_id=lead.id)

    assert page.items[0].body == "Wants a call Tuesday."
    assert page.items[0].author_kind is ActorKind.USER


async def test_an_empty_note_is_refused(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    lead = await service.create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()

    with pytest.raises(ValidationError):
        await service.add_note(lead_id=lead.id, body="   ", author_id=owner.id)


async def test_a_score_is_clamped_rather_than_rejected(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    lead = await service.create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()
    await service.set_score(lead_id=lead.id, score=900, actor_id=owner.id)

    assert lead.score == 100


async def test_an_assignment_can_be_cleared(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    lead = await service.create_lead(actor_id=owner.id, name="Ahmed")
    await db_session.flush()
    await service.assign(lead_id=lead.id, assigned_to_id=owner.id, actor_id=owner.id)
    await service.assign(lead_id=lead.id, assigned_to_id=None, actor_id=owner.id)
    await db_session.flush()

    assert lead.assigned_to_id is None
    page = await service.list_activity(lead_id=lead.id)
    assert LeadActivityKind.UNASSIGNED in {a.kind for a in page.items}


# --------------------------------------------------------- filtering and pagination


async def test_filters_narrow_by_status_and_assignment(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    assigned = await service.create_lead(actor_id=owner.id, name="Assigned")
    unassigned = await service.create_lead(actor_id=owner.id, name="Unassigned")
    await db_session.flush()
    await service.assign(lead_id=assigned.id, assigned_to_id=owner.id, actor_id=owner.id)
    await service.change_status(
        lead_id=unassigned.id,
        status=LeadStatus.CONTACTED,
        actor_id=owner.id,
    )
    await db_session.flush()

    only_unassigned = await service.list_leads(filters=LeadFilters(unassigned_only=True))
    only_contacted = await service.list_leads(filters=LeadFilters(statuses=(LeadStatus.CONTACTED,)))

    assert [lead.name for lead in only_unassigned.items] == ["Unassigned"]
    assert [lead.name for lead in only_contacted.items] == ["Unassigned"]


async def test_a_search_matches_across_the_fields_a_rep_would_try(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    await service.create_lead(
        actor_id=owner.id,
        name="Ahmed Hassan",
        email="ahmed@example.com",
        interest="Apartment finishing",
    )
    await service.create_lead(actor_id=owner.id, name="Sara Nabil")
    await db_session.flush()

    by_name = await service.list_leads(filters=LeadFilters(search="hassan"))
    by_interest = await service.list_leads(filters=LeadFilters(search="finishing"))

    assert [lead.name for lead in by_name.items] == ["Ahmed Hassan"]
    assert [lead.name for lead in by_interest.items] == ["Ahmed Hassan"]


async def test_a_wildcard_in_a_search_is_treated_as_text(db_session: AsyncSession) -> None:
    """Otherwise searching for "%" returns the entire pipeline."""
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    await service.create_lead(actor_id=owner.id, name="Ahmed")
    await service.create_lead(actor_id=owner.id, name="100% deposit paid")
    await db_session.flush()

    page = await service.list_leads(filters=LeadFilters(search="100%"))

    assert [lead.name for lead in page.items] == ["100% deposit paid"]


async def test_tags_filter_by_containment(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    await service.create_lead(actor_id=owner.id, name="Hot", tags=["Hot", "Cairo"])
    await service.create_lead(actor_id=owner.id, name="Cold", tags=["cairo"])
    await db_session.flush()

    page = await service.list_leads(filters=LeadFilters(tags=("hot",)))

    assert [lead.name for lead in page.items] == ["Hot"]


async def test_pagination_walks_every_lead_exactly_once(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    for index in range(7):
        await service.create_lead(actor_id=owner.id, name=f"Lead {index}")
    await db_session.flush()

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    while True:
        page = await service.list_leads(limit=3, cursor=cursor)
        seen.extend(lead.id for lead in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert len(seen) == 7
    assert len(set(seen)) == 7


async def test_statistics_report_the_pipeline(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    won = await service.create_lead(actor_id=owner.id, name="Won")
    await service.create_lead(actor_id=owner.id, name="Open one")
    await service.create_lead(actor_id=owner.id, name="Open two")
    await db_session.flush()
    await service.change_status(lead_id=won.id, status=LeadStatus.QUALIFIED, actor_id=owner.id)
    await service.change_status(lead_id=won.id, status=LeadStatus.WON, actor_id=owner.id)
    await service.assign(lead_id=won.id, assigned_to_id=owner.id, actor_id=owner.id)
    await db_session.flush()

    statistics = await service.statistics()

    assert statistics.total == 3
    assert statistics.open_leads == 2
    assert statistics.unassigned == 2
    assert statistics.by_status[LeadStatus.WON] == 1


async def test_a_budget_survives_a_round_trip_as_a_decimal(db_session: AsyncSession) -> None:
    """Money read back as a float would eventually disagree with the customer."""
    acme = await _tenant(db_session, slug="acme")
    owner = await _user(db_session, tenant=acme, email="owner@example.com")
    service = _service(db_session, acme)

    lead = await service.create_lead(
        actor_id=owner.id,
        budget_amount="500000",
        budget_currency="egp",
    )
    await db_session.flush()
    # Refreshed from the database rather than read off the instance, so this
    # asserts what PostgreSQL stored and not what Python still had in memory.
    await db_session.refresh(lead)

    stored = await LeadRepository(db_session, tenant_id=acme.id).require_by_id(lead.id)

    assert stored.budget_amount == Decimal("500000.00")
    assert stored.budget_currency == "EGP"


# ---------------------------------------------------------------- the agent tool


async def test_the_agent_tool_captures_a_lead_end_to_end(db_session: AsyncSession) -> None:
    """The tool, not just the service beneath it.

    Driven through `ToolRegistry.run`, so argument validation, the handler and
    the service all take part - which is what a model actually reaches.
    """
    from app.agents.registry import RECORD_LEAD_TOOL, ToolContext, build_default_registry

    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000020")

    output = await build_default_registry().run(
        name=RECORD_LEAD_TOOL,
        arguments={
            "name": "Ahmed Hassan",
            "interest": "150m apartment finishing",
            "budget_amount": 500000,
            "budget_currency": "EGP",
        },
        context=ToolContext(
            tenant_id=acme.id,
            conversation_id=conversation.id,
            session=db_session,
        ),
    )
    await db_session.flush()

    assert "saved" in output.lower()

    page = await _service(db_session, acme).list_leads()
    lead = page.items[0]
    assert lead.name == "Ahmed Hassan"
    assert lead.budget_amount == Decimal("500000.00")
    assert lead.budget_currency == "EGP"
    assert lead.source is LeadSource.AGENT


async def test_the_agent_tool_is_idempotent_across_a_conversation(db_session: AsyncSession) -> None:
    from app.agents.registry import RECORD_LEAD_TOOL, ToolContext, build_default_registry

    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000021")
    registry = build_default_registry()
    context = ToolContext(
        tenant_id=acme.id,
        conversation_id=conversation.id,
        session=db_session,
    )

    for arguments in ({"name": "Ahmed"}, {"interest": "Finishing"}, {"phone": "01001234567"}):
        await registry.run(name=RECORD_LEAD_TOOL, arguments=arguments, context=context)
    await db_session.flush()

    page = await _service(db_session, acme).list_leads()
    assert len(page.items) == 1
    assert page.items[0].phone == "01001234567"


async def test_the_agent_tool_says_so_when_it_was_given_nothing(db_session: AsyncSession) -> None:
    """A call with no details must not create an empty lead."""
    from app.agents.registry import RECORD_LEAD_TOOL, ToolContext, build_default_registry

    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000022")

    output = await build_default_registry().run(
        name=RECORD_LEAD_TOOL,
        arguments={},
        context=ToolContext(
            tenant_id=acme.id,
            conversation_id=conversation.id,
            session=db_session,
        ),
    )
    await db_session.flush()

    assert "nothing was saved" in output.lower()
    page = await _service(db_session, acme).list_leads()
    assert page.items == []


async def test_the_agent_tool_stops_when_a_colleague_has_taken_over(
    db_session: AsyncSession,
) -> None:
    """A queued job running after a handoff must not edit the record.

    The tool answers in words the model can act on rather than raising, because
    a raised domain error would become a 500 somewhere it does not belong.
    """
    from app.agents.registry import RECORD_LEAD_TOOL, ToolContext, build_default_registry

    acme = await _tenant(db_session, slug="acme")
    conversation = await _conversation(db_session, tenant=acme, wa_id="201000000023")
    conversation.mode = ConversationMode.HUMAN
    await db_session.flush()

    output = await build_default_registry().run(
        name=RECORD_LEAD_TOOL,
        arguments={"name": "Ahmed"},
        context=ToolContext(
            tenant_id=acme.id,
            conversation_id=conversation.id,
            session=db_session,
        ),
    )

    assert "colleague" in output.lower()
    page = await _service(db_session, acme).list_leads()
    assert page.items == []

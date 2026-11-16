"""Each e2e customer-state helper makes the state it names, read back through the API (EC5-04)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models.consent import ContactConsentEvent
from app.db.models.lead import Lead, LeadStatus
from tests.e2e_stack import customer_states
from tests.integration.test_e2e_inbox_states import no_people_left
from tests.integration.test_inbox_contract import Inbox, customer, inbox

pytestmark = pytest.mark.integration

__all__ = ["inbox", "no_people_left"]

CONTACTS = "/api/v1/contacts"
LEADS = "/api/v1/leads"


def test_a_malformed_command_is_the_usage_before_any_connection() -> None:
    for argv in ([], ["agent-lead"], ["agent-lead", "not-a-uuid"], ["rename", str(uuid.uuid4())]):
        with pytest.raises(SystemExit) as stopped:
            customer_states.parse(argv)
        assert stopped.value.code == customer_states.USAGE


async def test_every_customer_state_reads_back_as_the_screens_draw_it(
    no_people_left: None, inbox: Inbox
) -> None:
    shop, db = inbox.mine, inbox.database
    conversation = await customer(
        db, shop.tenant_id, shop.account_id, name="States Test", phone="201055508877"
    )
    async with db.session_factory() as session:
        lead = Lead(
            tenant_id=shop.tenant_id,
            contact_id=conversation.contact_id,
            name="States Test",
            status=LeadStatus.NEW,
            human_verified_fields=["name", "interest"],
        )
        session.add(lead)
        await session.commit()
        lead_id = lead.id
    async with db.session_factory() as session:
        identity = await customer_states.second_identity(session, conversation.contact_id)
        agent = await customer_states.agent_lead(session, lead_id)
        await session.commit()

    headers = shop.member.headers
    contact = (
        await inbox.client.get(f"{CONTACTS}/{conversation.contact_id}", headers=headers)
    ).json()
    assert sorted((item["kind"], item["value"]) for item in contact["identities"]) == [
        ("bsuid", identity["value"]),
        ("phone", "201055508877"),
    ]

    read = (await inbox.client.get(f"{LEADS}/{lead_id}", headers=headers)).json()
    assert read["interest"] == customer_states.AGENT_INTEREST
    assert read["human_verified_fields"] == ["name"], "filled by the agent, confirmed by nobody"
    notes = (await inbox.client.get(f"{LEADS}/{lead_id}/notes", headers=headers)).json()["items"]
    assert [(note["id"], note["author_kind"], note["author_id"]) for note in notes] == [
        (agent["note_id"], "agent", None)
    ]
    activity = (await inbox.client.get(f"{LEADS}/{lead_id}/activity", headers=headers)).json()
    assert sorted((item["kind"], item["actor_kind"]) for item in activity["items"]) == [
        ("fields_updated", "agent"),
        ("note_added", "agent"),
        ("unassigned", "system"),
    ]

    # An opt-out the history never saw: recorded now, then made thirty days old.
    await inbox.client.post(
        f"{CONTACTS}/{conversation.contact_id}/opt-out",
        json={"channel": "whatsapp"},
        headers=headers,
    )
    async with db.session_factory() as session:
        old = await customer_states.old_opt_out(session, conversation.contact_id)
        await session.commit()
        assert not (await session.scalars(select(ContactConsentEvent))).all()
    history = (
        await inbox.client.get(
            f"{CONTACTS}/{conversation.contact_id}/consent-events", headers=headers
        )
    ).json()["items"]
    assert [(item["before_history"], item["id"]) for item in history] == [(True, None)]
    assert history[0]["at"].replace("Z", "+00:00") == old["opted_out_at"]

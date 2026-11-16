"""Put an e2e contact or lead into a state the API cannot reach in a few calls (FE-5, EC5-04).

    python -m tests.e2e_stack.customer_states second-identity <contact-id>
        the contact is also known by a business-scoped id (a second identity)
    python -m tests.e2e_stack.customer_states agent-lead <lead-id>
        what the AI and the system leave on a lead: an interest the agent filled
        (not confirmed by a person), a note by the AI, an activity by the agent
        and one by the system
    python -m tests.e2e_stack.customer_states old-opt-out <contact-id>
        a WhatsApp opt-out recorded thirty days ago with no history row: one
        older than the consent history

Test-only, like the rest of ``tests.e2e_stack``: nothing under ``app/`` imports it
(``tests/unit/test_e2e_gate_states.py`` holds that for the package). The journeys
run it with ``docker exec wasla-fe-e2e-api``. Everything else FE-5's journeys need
is made through the API and the fake inbound webhook - a contact, a contact with
a Turkish name, a stop word's opt-out, a lead in any status, a member's note, a
confirmed field, an assignment, a withdrawn membership.

Rows are written through the application's models. Each command prints one JSON
line naming what it made, read back from the database.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

COMMANDS: Final = ("second-identity", "agent-lead", "old-opt-out")
USAGE: Final = (
    "usage: python -m tests.e2e_stack.customer_states "
    "(second-identity <contact-id> | agent-lead <lead-id> | old-opt-out <contact-id>)"
)
#: A business-scoped id in Meta's documented shape; nobody's.
BSUID_PREFIX: Final = "EG.e2e"
AGENT_INTEREST: Final = "Kitchen finishing, 12 square metres"
AGENT_NOTE: Final = "The customer asked for a quote and prefers a call after 5 pm."
OPT_OUT_AGE: Final = timedelta(days=30)


def parse(argv: list[str]) -> tuple[str, uuid.UUID]:
    """(command, target) or the usage, before any connection is opened."""
    if len(argv) == 2 and argv[0] in COMMANDS:
        try:
            return argv[0], uuid.UUID(argv[1])
        except ValueError:
            pass
    raise SystemExit(USAGE)


async def second_identity(session: AsyncSession, contact_id: uuid.UUID) -> dict[str, object]:
    """A WhatsApp business-scoped id beside whatever the contact already has."""
    from app.db.models.channel import (
        Channel,
        ContactIdentity,
        IdentityKind,
        IdentityScope,
        IdentitySource,
    )
    from app.db.models.conversation import Contact

    contact = await session.scalar(select(Contact).where(Contact.id == contact_id))
    if contact is None:
        raise SystemExit(f"no contact {contact_id}")
    value = f"{BSUID_PREFIX}{contact.id.hex[:12]}"
    session.add(
        ContactIdentity(
            tenant_id=contact.tenant_id,
            contact_id=contact.id,
            channel=Channel.WHATSAPP,
            kind=IdentityKind.BSUID,
            scope=IdentityScope.PROVIDER_ACCOUNT,
            scope_ref="waba-e2e",
            value=value,
            source=IdentitySource.PROVIDER,
        )
    )
    await session.flush()
    return {"contact_id": str(contact.id), "kind": "bsuid", "value": value}


async def agent_lead(session: AsyncSession, lead_id: uuid.UUID) -> dict[str, object]:
    """An agent-filled interest, an AI note, and an agent's and the system's activity."""
    from app.db.models.lead import ActorKind, Lead, LeadActivity, LeadActivityKind, LeadNote

    lead = await session.scalar(select(Lead).where(Lead.id == lead_id))
    if lead is None:
        raise SystemExit(f"no lead {lead_id}")
    previous = lead.interest
    lead.interest = AGENT_INTEREST
    # Filled by the agent, so not a field a person confirmed.
    lead.human_verified_fields = [name for name in lead.human_verified_fields if name != "interest"]
    lead.last_activity_at = datetime.now(UTC)
    note = LeadNote(
        tenant_id=lead.tenant_id,
        lead_id=lead.id,
        author_id=None,
        author_kind=ActorKind.AGENT,
        body=AGENT_NOTE,
    )
    session.add(note)
    await session.flush()
    session.add_all(
        [
            LeadActivity(
                tenant_id=lead.tenant_id,
                lead_id=lead.id,
                kind=LeadActivityKind.FIELDS_UPDATED,
                summary="Agent updated interest.",
                actor_kind=ActorKind.AGENT,
                data={"interest": {"from": previous, "to": AGENT_INTEREST}},
            ),
            LeadActivity(
                tenant_id=lead.tenant_id,
                lead_id=lead.id,
                kind=LeadActivityKind.NOTE_ADDED,
                summary="Note added.",
                actor_kind=ActorKind.AGENT,
                data={"note_id": str(note.id)},
            ),
            LeadActivity(
                tenant_id=lead.tenant_id,
                lead_id=lead.id,
                kind=LeadActivityKind.UNASSIGNED,
                summary="Assignment cleared: the owner left the workspace.",
                actor_kind=ActorKind.SYSTEM,
                data={"from": None, "to": None, "cause": "member_revoked"},
            ),
        ]
    )
    await session.flush()
    return {"lead_id": str(lead.id), "note_id": str(note.id), "interest": AGENT_INTEREST}


async def old_opt_out(session: AsyncSession, contact_id: uuid.UUID) -> dict[str, object]:
    """A standing WhatsApp opt-out, thirty days old, that the consent history never saw."""
    from app.db.models.campaign import OptOutSource, OptOutVia
    from app.db.models.channel import Channel
    from app.db.models.consent import ContactChannelConsent, ContactConsentEvent
    from app.db.models.conversation import Contact

    contact = await session.scalar(select(Contact).where(Contact.id == contact_id))
    if contact is None:
        raise SystemExit(f"no contact {contact_id}")
    moment = datetime.now(UTC) - OPT_OUT_AGE
    consent = await session.scalar(
        select(ContactChannelConsent).where(
            ContactChannelConsent.tenant_id == contact.tenant_id,
            ContactChannelConsent.contact_id == contact.id,
            ContactChannelConsent.channel == Channel.WHATSAPP,
        )
    )
    if consent is None:
        consent = ContactChannelConsent(
            tenant_id=contact.tenant_id, contact_id=contact.id, channel=Channel.WHATSAPP
        )
        session.add(consent)
    consent.marketing_opt_out_at = moment
    consent.opt_out_source = OptOutSource.CUSTOMER
    consent.opt_out_via = OptOutVia.MESSAGE
    remembered = (
        await session.scalars(
            select(ContactConsentEvent).where(
                ContactConsentEvent.tenant_id == contact.tenant_id,
                ContactConsentEvent.contact_id == contact.id,
            )
        )
    ).all()
    for event in remembered:
        await session.delete(event)
    await session.flush()
    return {"contact_id": str(contact.id), "opted_out_at": moment.isoformat()}


async def run(argv: list[str]) -> dict[str, object]:
    from app.core.config import get_settings
    from app.db.session import Database

    command, target = parse(argv)
    database = Database(get_settings())
    try:
        async with database.session() as session:
            if command == "second-identity":
                made = await second_identity(session, target)
            elif command == "agent-lead":
                made = await agent_lead(session, target)
            else:
                made = await old_opt_out(session, target)
            await session.commit()
        return made
    finally:
        await database.dispose()


def main(argv: list[str]) -> None:
    print(json.dumps(asyncio.run(run(argv))))  # noqa: T201 - this script's output is its interface


if __name__ == "__main__":
    main(sys.argv[1:])

"""Read-only evidence and invariants for the omnichannel foundation (0082-0084).

    python -m scripts.omnichannel_invariants census   # what the data holds; never fails
    python -m scripts.omnichannel_invariants verify   # invariants; exit 1 on any violation

The URL is `INVARIANTS_DATABASE_URL`, else `DATABASE_URL`. Meant for a replica or
a restored copy as much as for the primary: every statement is a `SELECT`, run
inside a transaction that is `READ ONLY` before its first query, so it works -
and can only work - under `default_transaction_read_only = on`.

**Counts only.** Nothing printed is an identifier, a phone number, a
business-scoped id or a word anybody wrote. An operator who needs the rows
behind a count runs the query by hand, deliberately.

`census` is the audit's operator checks (Q1-Q8) restated for the neutral schema.
Q1 is time-sensitive: it reads the raw inbound payloads that still exist, and
retention clears them 30 days after processing (DB-011), so the phone and
business-scoped id pairs Meta asserted in old deliveries are only countable
while those payloads last.

`verify` is the foundation's invariants, every one of which a healthy database
answers with zero: each number has its connection, each contact its phone
identity, each conversation a participant of its own contact on its own
channel, each outbound message a route that agrees end to end (the wrong-channel
oracle), each agent turn a customer's message as its trigger (the echo oracle),
each file its own message's workspace and conversation, and no identity scoped
twice. Most are also enforced by keys; this says so of the data, and is what
proves a backfill or a restore whole.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    query: str


# ------------------------------------------------------------------ census

CENSUS: tuple[Check, ...] = (
    # Q1 - the phone/business-scoped-id evidence still held in raw payloads.
    Check(
        "q1_message_events_with_payload",
        "SELECT count(*) FROM whatsapp_events WHERE kind = 'message' AND payload IS NOT NULL",
    ),
    Check(
        "q1_payloads_naming_phone_only",
        "SELECT count(*) FROM whatsapp_events WHERE kind = 'message' AND payload IS NOT NULL"
        " AND payload ? 'from' AND NOT payload ? 'from_user_id'",
    ),
    Check(
        "q1_payloads_naming_business_scoped_id_only",
        "SELECT count(*) FROM whatsapp_events WHERE kind = 'message' AND payload IS NOT NULL"
        " AND payload ? 'from_user_id' AND NOT payload ? 'from'",
    ),
    Check(
        "q1_payloads_naming_both",
        "SELECT count(*) FROM whatsapp_events WHERE kind = 'message' AND payload IS NOT NULL"
        " AND payload ? 'from_user_id' AND payload ? 'from'",
    ),
    # Q1, as recorded: contacts holding a phone and a business-scoped id.
    Check(
        "q1_contacts_with_phone_and_business_scoped_id",
        "SELECT count(*) FROM contacts c WHERE"
        " EXISTS (SELECT 1 FROM contact_identities p WHERE p.tenant_id = c.tenant_id"
        " AND p.contact_id = c.id AND p.channel = 'whatsapp' AND p.kind = 'phone')"
        " AND EXISTS (SELECT 1 FROM contact_identities b WHERE b.tenant_id = c.tenant_id"
        " AND b.contact_id = c.id AND b.channel = 'whatsapp' AND b.kind = 'bsuid')",
    ),
    Check(
        "q1_contacts_with_business_scoped_id_only",
        "SELECT count(*) FROM contacts c WHERE c.wa_id IS NULL AND EXISTS ("
        " SELECT 1 FROM contact_identities b WHERE b.tenant_id = c.tenant_id"
        " AND b.contact_id = c.id AND b.kind = 'bsuid')",
    ),
    Check(
        "q1_identities_attached_by_provider_pairing",
        "SELECT count(*) FROM contact_identities WHERE source = 'provider_pairing'",
    ),
    # Q2 - contacts whose WhatsApp phone is not a plain number, or absent.
    Check(
        "q2_contacts_with_non_numeric_phone",
        "SELECT count(*) FROM contacts WHERE wa_id IS NOT NULL AND wa_id !~ '^[0-9]+$'",
    ),
    Check("q2_contacts_without_phone", "SELECT count(*) FROM contacts WHERE wa_id IS NULL"),
    # Q3 - people talking to numbers of more than one WhatsApp Business
    # Account in one workspace: several business-scoped ids each, expected.
    Check(
        "q3_contacts_spanning_business_accounts",
        "SELECT count(*) FROM (SELECT v.tenant_id, v.contact_id FROM conversations v"
        " JOIN whatsapp_accounts a ON a.tenant_id = v.tenant_id AND a.id = v.account_id"
        " GROUP BY v.tenant_id, v.contact_id HAVING count(DISTINCT a.waba_id) > 1) s",
    ),
    # Q4 - provider ids shared by an inbound and an outbound row.
    Check(
        "q4_echo_shaped_ids_same_connection",
        "SELECT count(*) FROM messages i JOIN messages o ON o.tenant_id = i.tenant_id"
        " AND o.connection_id = i.connection_id AND o.wa_message_id = i.wa_message_id"
        " AND o.id <> i.id WHERE i.direction = 'inbound' AND o.direction = 'outbound'",
    ),
    # Q5 - conversations on released or paused connections: history.
    Check(
        "q5_conversations_on_released_connections",
        "SELECT count(*) FROM conversations v JOIN channel_connections k"
        " ON k.tenant_id = v.tenant_id AND k.id = v.account_id WHERE k.released_at IS NOT NULL",
    ),
    Check(
        "q5_conversations_on_inactive_connections",
        "SELECT count(*) FROM conversations v JOIN channel_connections k"
        " ON k.tenant_id = v.tenant_id AND k.id = v.account_id WHERE k.status <> 'active'",
    ),
    # Q6 - conversations without a message: the state probe Y1 produced.
    Check(
        "q6_conversations_without_messages",
        "SELECT count(*) FROM conversations v WHERE NOT EXISTS ("
        " SELECT 1 FROM messages m WHERE m.tenant_id = v.tenant_id"
        " AND m.conversation_id = v.id)",
    ),
    # Q7 - locator lengths, and messages carrying several files.
    Check(
        "q7_locators_over_255",
        "SELECT count(*) FROM message_media WHERE char_length(locator) > 255",
    ),
    Check(
        "q7_messages_with_several_files",
        "SELECT count(*) FROM (SELECT message_id FROM message_media"
        " GROUP BY message_id HAVING count(*) > 1) s",
    ),
    # Q8 - shared tables still keyed into a WhatsApp table. Campaigns and their
    # templates are WhatsApp's by design; anything else is a regression.
    Check(
        "q8_shared_keys_into_whatsapp_tables",
        "SELECT count(*) FROM pg_constraint k JOIN pg_namespace n ON n.oid = k.connamespace"
        " WHERE n.nspname = current_schema() AND k.contype = 'f'"
        " AND k.confrelid::regclass::text LIKE 'whatsapp%'"
        " AND k.conrelid::regclass::text NOT LIKE 'whatsapp%'",
    ),
    Check(
        "connections_by_channel_whatsapp",
        "SELECT count(*) FROM channel_connections WHERE channel = 'whatsapp'",
    ),
    Check(
        "connections_other_channels",
        "SELECT count(*) FROM channel_connections WHERE channel <> 'whatsapp'",
    ),
    Check(
        "connections_credential_refused",
        "SELECT count(*) FROM channel_connections WHERE health = 'auth_failed'",
    ),
)

# -------------------------------------------------------------- invariants

INVARIANTS: tuple[Check, ...] = (
    # Shared-id backfill and mirror: each number is its connection, same facts.
    Check(
        "number_without_its_connection",
        "SELECT count(*) FROM whatsapp_accounts a WHERE NOT EXISTS ("
        " SELECT 1 FROM channel_connections c WHERE c.id = a.id AND c.tenant_id = a.tenant_id"
        " AND c.channel = 'whatsapp' AND c.external_account_id = a.phone_number_id"
        " AND c.status::text = a.status::text"
        " AND c.ownership_started_at = a.ownership_started_at"
        " AND c.released_at IS NOT DISTINCT FROM a.released_at"
        " AND c.ownership_verified_at IS NOT DISTINCT FROM a.ownership_verified_at)",
    ),
    Check(
        "whatsapp_connection_without_its_number",
        "SELECT count(*) FROM channel_connections c WHERE c.channel = 'whatsapp'"
        " AND NOT EXISTS (SELECT 1 FROM whatsapp_accounts a WHERE a.id = c.id)",
    ),
    Check(
        "live_external_account_claimed_twice",
        "SELECT count(*) FROM (SELECT channel, external_account_id FROM channel_connections"
        " WHERE released_at IS NULL GROUP BY channel, external_account_id"
        " HAVING count(*) > 1) s",
    ),
    # Identity backfill: wa_id and the phone identity are one fact.
    Check(
        "contact_without_its_phone_identity",
        "SELECT count(*) FROM contacts c WHERE c.wa_id IS NOT NULL AND NOT EXISTS ("
        " SELECT 1 FROM contact_identities i WHERE i.tenant_id = c.tenant_id"
        " AND i.contact_id = c.id AND i.channel = 'whatsapp' AND i.kind = 'phone'"
        " AND i.value = c.wa_id)",
    ),
    Check(
        "phone_identity_disagreeing_with_its_contact",
        "SELECT count(*) FROM contact_identities i JOIN contacts c"
        " ON c.tenant_id = i.tenant_id AND c.id = i.contact_id"
        " WHERE i.channel = 'whatsapp' AND i.kind = 'phone'"
        " AND c.wa_id IS DISTINCT FROM i.value",
    ),
    Check(
        "contact_without_any_identity",
        "SELECT count(*) FROM contacts c WHERE NOT EXISTS ("
        " SELECT 1 FROM contact_identities i WHERE i.tenant_id = c.tenant_id"
        " AND i.contact_id = c.id)",
    ),
    Check(
        "identity_scoped_twice",
        "SELECT count(*) FROM (SELECT tenant_id, channel, kind, scope, scope_ref, value"
        " FROM contact_identities GROUP BY tenant_id, channel, kind, scope, scope_ref, value"
        " HAVING count(*) > 1) s",
    ),
    Check(
        "identity_of_another_workspaces_contact",
        "SELECT count(*) FROM contact_identities i WHERE NOT EXISTS ("
        " SELECT 1 FROM contacts c WHERE c.id = i.contact_id AND c.tenant_id = i.tenant_id)",
    ),
    # A business-scoped id is scoped by a WhatsApp Business Account this
    # workspace connects through; any other scope could never be looked up.
    Check(
        "business_scoped_id_outside_the_workspaces_accounts",
        "SELECT count(*) FROM contact_identities i WHERE i.kind = 'bsuid'"
        " AND NOT (i.scope = 'provider_account' AND EXISTS ("
        " SELECT 1 FROM whatsapp_accounts a WHERE a.tenant_id = i.tenant_id"
        " AND a.waba_id = i.scope_ref))",
    ),
    # Conversations: connection and participant agree with the conversation.
    Check(
        "conversation_on_a_connection_not_its_own",
        "SELECT count(*) FROM conversations v WHERE NOT EXISTS ("
        " SELECT 1 FROM channel_connections k WHERE k.tenant_id = v.tenant_id"
        " AND k.id = v.account_id AND k.channel = v.channel)",
    ),
    Check(
        "conversation_participant_not_its_contacts",
        "SELECT count(*) FROM conversations v WHERE NOT EXISTS ("
        " SELECT 1 FROM contact_identities i WHERE i.tenant_id = v.tenant_id"
        " AND i.contact_id = v.contact_id AND i.id = v.participant_identity_id"
        " AND i.channel = v.channel)",
    ),
    # The wrong-channel oracle: every outbound message traces to a
    # conversation, its connection and its participant, and all four agree on
    # workspace and channel; and the participant is a kind the channel can
    # address. No send can then have chosen its address from the contact.
    Check(
        "outbound_message_with_a_disagreeing_route",
        "SELECT count(*) FROM messages m WHERE m.direction = 'outbound' AND NOT EXISTS ("
        " SELECT 1 FROM conversations v"
        " JOIN channel_connections k ON k.tenant_id = v.tenant_id AND k.id = v.account_id"
        " AND k.channel = v.channel"
        " JOIN contact_identities i ON i.tenant_id = v.tenant_id"
        " AND i.contact_id = v.contact_id AND i.id = v.participant_identity_id"
        " AND i.channel = v.channel"
        " WHERE v.tenant_id = m.tenant_id AND v.id = m.conversation_id"
        " AND k.id = m.connection_id"
        " AND (v.channel <> 'whatsapp' OR i.kind IN ('phone', 'bsuid')))",
    ),
    Check(
        "message_on_a_connection_not_its_conversations",
        "SELECT count(*) FROM messages m WHERE NOT EXISTS ("
        " SELECT 1 FROM conversations v WHERE v.tenant_id = m.tenant_id"
        " AND v.id = m.conversation_id AND v.account_id = m.connection_id)",
    ),
    Check(
        "provider_message_id_twice_on_one_connection",
        "SELECT count(*) FROM (SELECT tenant_id, connection_id, wa_message_id FROM messages"
        " WHERE wa_message_id IS NOT NULL GROUP BY tenant_id, connection_id, wa_message_id"
        " HAVING count(*) > 1) s",
    ),
    # The echo oracle: no agent turn was ever claimed or owed for a message
    # that is not a customer's inbound message of the turn's conversation.
    # A turn whose message retention has since erased is not judged.
    Check(
        "agent_turn_triggered_by_a_non_customer_message",
        "SELECT count(*) FROM agent_turns t JOIN messages m"
        " ON m.tenant_id = t.tenant_id AND m.id = t.trigger_message_id"
        " WHERE m.conversation_id <> t.conversation_id OR m.direction <> 'inbound'"
        " OR m.origin <> 'customer'",
    ),
    Check(
        "echo_event_projected_as_a_message",
        "SELECT count(*) FROM whatsapp_events e JOIN messages m"
        " ON m.tenant_id = e.tenant_id AND m.connection_id = e.account_id"
        " AND m.wa_message_id = e.event_id WHERE e.kind = 'echo'"
        " AND m.direction = 'inbound' AND m.origin = 'customer'",
    ),
    # Files: the workspace, conversation and message agree; positions unique.
    Check(
        "file_not_in_its_messages_conversation_or_workspace",
        "SELECT count(*) FROM message_media f WHERE NOT EXISTS ("
        " SELECT 1 FROM messages m WHERE m.tenant_id = f.tenant_id"
        " AND m.conversation_id = f.conversation_id AND m.id = f.message_id)",
    ),
    Check(
        "file_position_used_twice",
        "SELECT count(*) FROM (SELECT message_id, position FROM message_media"
        " GROUP BY message_id, position HAVING count(*) > 1) s",
    ),
    # Events: every one names a connection of its own workspace and channel.
    Check(
        "event_on_a_connection_not_its_own",
        "SELECT count(*) FROM whatsapp_events e WHERE NOT EXISTS ("
        " SELECT 1 FROM channel_connections k WHERE k.tenant_id = e.tenant_id"
        " AND k.id = e.account_id AND k.channel = e.channel)",
    ),
    # Campaigns: a copy goes to its own contact's identity, through its own
    # campaign's number.
    Check(
        "campaign_recipient_identity_not_its_contacts",
        "SELECT count(*) FROM campaign_recipients r WHERE r.participant_identity_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM contact_identities i WHERE i.tenant_id = r.tenant_id"
        " AND i.contact_id = r.contact_id AND i.id = r.participant_identity_id)",
    ),
    Check(
        "campaign_recipient_conversation_on_another_connection",
        "SELECT count(*) FROM campaign_recipients r JOIN campaigns k"
        " ON k.tenant_id = r.tenant_id AND k.id = r.campaign_id"
        " JOIN conversations v ON v.tenant_id = r.tenant_id AND v.id = r.conversation_id"
        " WHERE v.account_id <> k.account_id OR v.contact_id <> r.contact_id",
    ),
)


async def _counts(
    connection: AsyncConnection, checks: Sequence[Check], *, read_only: bool
) -> dict[str, int]:
    if read_only:
        # Before the first statement, so the run can do nothing else.
        await connection.execute(text("SET TRANSACTION READ ONLY"))
    found: dict[str, int] = {}
    for check in checks:
        found[check.name] = int((await connection.execute(text(check.query))).scalar_one())
    return found


async def census(connection: AsyncConnection, *, read_only: bool = True) -> dict[str, int]:
    """The evidence counts, in the caller's transaction - made read-only unless it has written.

    `read_only=False` is for a caller sweeping its own uncommitted rows (a test
    inside one transaction); every statement here is a `SELECT` either way.
    """
    return await _counts(connection, CENSUS, read_only=read_only)


async def violations(connection: AsyncConnection, *, read_only: bool = True) -> dict[str, int]:
    """Every invariant's violation count. All zero on a healthy database."""
    return await _counts(connection, INVARIANTS, read_only=read_only)


async def _run(command: str, url: str) -> int:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection, connection.begin():
            if command == "census":
                found = await census(connection)
            else:
                found = await violations(connection)
    finally:
        await engine.dispose()
    for name, count in found.items():
        sys.stdout.write(f"{name}: {count}\n")
    if command == "verify":
        broken = sum(1 for count in found.values() if count)
        sys.stdout.write(
            f"omnichannel_invariants verify: {'ok' if not broken else f'{broken} violated'}\n"
        )
        return 1 if broken else 0
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("census", "verify"):
        sys.stderr.write("usage: python -m scripts.omnichannel_invariants census|verify\n")
        return 64
    url = os.environ.get("INVARIANTS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.stderr.write("omnichannel_invariants: INVARIANTS_DATABASE_URL or DATABASE_URL\n")
        return 64
    return asyncio.run(_run(argv[0], url))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main(sys.argv[1:]))

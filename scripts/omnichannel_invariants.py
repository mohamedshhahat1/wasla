"""Read-only evidence and invariants for the omnichannel foundation (0082-0084).

    python -m scripts.omnichannel_invariants census   # what the data holds; never fails
    python -m scripts.omnichannel_invariants verify   # invariants; exit 1 on any violation
    python -m scripts.omnichannel_invariants recover-button-opt-outs --dry-run
    python -m scripts.omnichannel_invariants recover-button-opt-outs --apply

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

`verify` also runs the entitlement ledger, E01-E15 (ADR-131): active
connections fit the capacity in force and its channel types, AI turns are
charged once and only for a usable outcome and no hold outlives its TTL and the
sweep, every capacity reduction disables - never releases or deletes - and says
so on the audit trail, the retired `whatsapp_numbers` key is on nothing new,
and every marketing opt-out names its channel. Those checks read the clock, the
deployment's `DEFAULT_PLAN_CODE` and `AI_TURN_HOLD_TTL_SECONDS` from the
environment the command runs in.

`recover-button-opt-outs` is the one command here that can write, and only with
`--apply` (OMNI-030). It replays the "stop" taps still held in retained raw
payloads through the one opt-out writer, with the provenance `replay`; the dry
run (the default) is read-only like everything else. It must run on production
**before the 30-day payload redaction removes the evidence**. Output is counts
per workspace id; never a phone, a business-scoped id or a message.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from app.repositories.entitlement_census import CAPACITY_CENSUS_SQL

# The deployment defaults the entitlement checks bind (`app.core.config`),
# overridable from the environment the command runs in.
DEFAULT_PLAN_CODE = os.environ.get("DEFAULT_PLAN_CODE", "starter")
DEFAULT_HOLD_TTL_SECONDS = int(os.environ.get("AI_TURN_HOLD_TTL_SECONDS", "900"))
# The billing worker's pause between passes (`billing_worker.POLL_SECONDS`, held
# equal by a unit test): how late its bookkeeping may be without being wrong.
SWEEP_SECONDS = 600


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    query: str
    # Whether the statement binds `:now`, `:default_plan_code`,
    # `:hold_cutoff_seconds` and `:sweep_seconds` - the entitlement checks,
    # whose answer depends on the clock, the deployment's default plan, the AI
    # turn hold TTL and how late the billing sweep may be.
    bound: bool = False


# The English stop phrases, as SQL literals, for the Q6 census - written out so
# every statement here is a fixed string; a unit test holds them equal to
# `STOP_WORDS`. Arabic phrases need the matcher's letter folding and are counted
# by the replay command, which uses `is_stop_request` itself.
STOP_PHRASES_SQL = (
    "'no more messages', 'opt out', 'optout', 'stop', 'stop promotions', 'unsubscribe'"
)

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
    # The final audit's Q5 and Q6 (OMNI-030): exposure the old parser may
    # already have left. Taps stored with no words, and retained "stop" taps
    # whose customer is not opted out - which `recover-button-opt-outs`
    # replays, and which must be run before retention redacts the payloads.
    Check(
        "q5_inbound_interactive_without_text",
        "SELECT count(*) FROM messages WHERE direction = 'inbound'"
        " AND kind = 'interactive' AND body IS NULL",
    ),
    Check(
        "q6_retained_stop_taps_without_opt_out",
        "SELECT count(*) FROM whatsapp_events e"
        " JOIN messages m ON m.tenant_id = e.tenant_id AND m.connection_id = e.account_id"
        " AND m.wa_message_id = e.event_id AND m.direction = 'inbound'"
        " JOIN conversations v ON v.tenant_id = m.tenant_id AND v.id = m.conversation_id"
        # The tap was on WhatsApp: its consent there is what it speaks for (ENT-19).
        " LEFT JOIN contact_channel_consents k ON k.tenant_id = v.tenant_id"
        " AND k.contact_id = v.contact_id AND k.channel = 'whatsapp'"
        " WHERE e.kind = 'message' AND e.payload IS NOT NULL"
        " AND k.marketing_opt_out_at IS NULL"
        " AND lower(btrim(coalesce(e.payload #>> '{button,text}',"
        " e.payload #>> '{interactive,button_reply,title}',"
        " e.payload #>> '{interactive,list_reply,title}'))) IN ('no more messages',"
        " 'opt out', 'optout', 'stop', 'stop promotions', 'unsubscribe')",
    ),
    # Deliveries the workspace-wide key refused, kept as evidence (OMNI-043).
    Check(
        "collision_evidence_events",
        "SELECT count(*) FROM whatsapp_events WHERE event_id LIKE 'collision:%'",
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
    # A processed message event projected onto its message, found under the
    # event's own id on its own connection (OMNI-032, the audit's Q3). What
    # inbound recovery and the stranded-media sweep rely on.
    Check(
        "message_event_without_its_projected_message",
        "SELECT count(*) FROM whatsapp_events e WHERE e.kind = 'message'"
        " AND e.state = 'processed' AND NOT EXISTS (SELECT 1 FROM messages m"
        " WHERE m.tenant_id = e.tenant_id AND m.connection_id = e.account_id"
        " AND m.wa_message_id = e.event_id)",
    ),
    # Collision evidence is evidence: failed, so never recovered or projected
    # (OMNI-043).
    Check(
        "collision_evidence_not_failed",
        "SELECT count(*) FROM whatsapp_events"
        " WHERE event_id LIKE 'collision:%' AND state <> 'failed'",
    ),
    # A tap keeps its words beside its payload (OMNI-030).
    Check(
        "inbound_tap_without_its_words",
        "SELECT count(*) FROM messages WHERE direction = 'inbound'"
        " AND action_source IS NOT NULL AND action_title IS NOT NULL AND body IS NULL",
    ),
    # An AI reply delivered on a channel whose policy requires the automation
    # disclosure, on a conversation that has never recorded one (OMNI-041).
    # Messenger and Instagram are the channels that require it.
    Check(
        "ai_reply_on_a_disclosure_channel_never_disclosed",
        "SELECT count(*) FROM conversations c WHERE c.channel IN ('messenger', 'instagram')"
        " AND c.automation_disclosed_at IS NULL AND EXISTS (SELECT 1 FROM messages m"
        " WHERE m.tenant_id = c.tenant_id AND m.conversation_id = c.id"
        " AND m.direction = 'outbound' AND m.origin = 'agent'"
        " AND m.delivery_state = 'sent')",
    ),
    # The window anchor is the newest thing the customer said (OMNI-036, the
    # audit's Q4). A late delivery used to move it backwards; on production
    # this counts conversations that already happened to.
    Check(
        "window_anchor_older_than_newest_inbound",
        "SELECT count(*) FROM conversations c WHERE c.last_inbound_at < ("
        " SELECT max(m.sent_at) FROM messages m WHERE m.tenant_id = c.tenant_id"
        " AND m.conversation_id = c.id AND m.direction = 'inbound')",
    ),
    Check(
        "campaign_recipient_conversation_on_another_connection",
        "SELECT count(*) FROM campaign_recipients r JOIN campaigns k"
        " ON k.tenant_id = r.tenant_id AND k.id = r.campaign_id"
        " JOIN conversations v ON v.tenant_id = r.tenant_id AND v.id = r.conversation_id"
        " WHERE v.account_id <> k.account_id OR v.contact_id <> r.contact_id",
    ),
)


@dataclass(frozen=True, slots=True)
class Bindings:
    """What the entitlement checks are judged against (ADR-131)."""

    now: datetime | None = None
    default_plan_code: str = DEFAULT_PLAN_CODE
    hold_ttl_seconds: int = DEFAULT_HOLD_TTL_SECONDS

    def values(self) -> dict[str, object]:
        return {
            "now": self.now or datetime.now(UTC),
            "default_plan_code": self.default_plan_code,
            # A hold is released by the billing worker's next pass after its
            # TTL; one past both has outlived the sweep.
            "hold_cutoff_seconds": self.hold_ttl_seconds + SWEEP_SECONDS,
            "sweep_seconds": SWEEP_SECONDS,
        }


# --------------------------------------------------------- entitlements
#
# ADR-131's ledger, E01-E15. Each is zero on a healthy database. Several are
# also enforced by a key, a constraint or a trigger; this says so of the data.

# What explains a workspace holding more connections than its capacity, or one
# of a type its plan does not allow (E01, E02): an open reduction (ENT-14,
# ENT-15); a subscription that is not served (ENT-16 - nothing is disabled for
# that); or a channel slot that stopped counting by the clock within the last
# sweep, whose boundary the billing worker's next pass judges.
_EXPLAINED = (
    "(EXISTS (SELECT 1 FROM channel_capacity_reductions r"
    " WHERE r.tenant_id = census.tenant_id AND r.status = 'pending_selection')"
    " OR (NOT census.serving AND EXISTS (SELECT 1 FROM subscriptions s"
    " WHERE s.tenant_id = census.tenant_id))"
    " OR EXISTS (SELECT 1 FROM topup_purchases tp WHERE tp.tenant_id = census.tenant_id"
    " AND tp.entitlement_key::text IN ('channel_connections', 'whatsapp_numbers')"
    " AND tp.status::text IN ('granted', 'refund_review') AND tp.ended_at IS NULL"
    " AND tp.expires_at <= CAST(:now AS timestamptz)"
    " AND tp.expires_at > CAST(:now AS timestamptz) - make_interval(secs => :sweep_seconds)))"
)
# Outcomes no generation produced (ENT-02): never charged, whatever else is true.
_NOT_CHARGEABLE = (
    "('escalated', 'empty_response', 'nothing_to_answer', 'quota_blocked', 'channel_not_in_plan')"
)
_REDUCTION_DISABLE = "('capacity_reduction', 'capacity_reduction_automatic')"
_DISABLED = "('channel_connection_disabled', 'whatsapp_account_disabled')"
_RELEASED = "('channel_connection_released', 'whatsapp_account_released')"

ENTITLEMENT_INVARIANTS: tuple[Check, ...] = (
    # E01 - active connections fit the capacity in force (ENT-05, ENT-08),
    # unless an open reduction, a subscription not served or an expiry the
    # sweep has not reached yet explains it.
    Check(
        "e01_connections_over_capacity_unexplained",
        f"SELECT count(*) FROM ({CAPACITY_CENSUS_SQL}) census"  # noqa: S608 - module constants
        f" WHERE census.over_limit AND NOT {_EXPLAINED}",
        bound=True,
    ),
    # E02 - no active connection of a type the plan in force does not allow
    # (ENT-09), unless explained the same way.
    Check(
        "e02_connection_of_a_type_not_allowed_unexplained",
        f"SELECT count(*) FROM ({CAPACITY_CENSUS_SQL}) census"  # noqa: S608 - module constants
        f" WHERE census.outside_allowed_types AND NOT {_EXPLAINED}",
        bound=True,
    ),
    # E03 - no typed channel slot sold or granted for a type the plan did not
    # allow then (ENT-12), judged against what the sale's audit entry recorded.
    Check(
        "e03_typed_slot_for_a_type_not_allowed",
        "SELECT count(*) FROM audit_logs a"
        " WHERE a.action IN ('billing_topup_checkout_created', 'billing_topup_platform_granted')"
        " AND a.metadata ->> 'entitlement_key' = 'channel_connections'"
        " AND a.metadata ->> 'channel_type' IS NOT NULL"
        " AND a.metadata ? 'allowed_channel_types'"
        " AND NOT (a.metadata -> 'allowed_channel_types') ? (a.metadata ->> 'channel_type')",
    ),
    # E04 - no channel top-up sold to a workspace whose plan the product was
    # not offered to then (ENT-13); an empty list is every plan.
    Check(
        "e04_channel_topup_sold_to_an_ineligible_plan",
        "SELECT count(*) FROM audit_logs a"
        " WHERE a.action = 'billing_topup_checkout_created'"
        " AND a.metadata ->> 'entitlement_key' = 'channel_connections'"
        " AND jsonb_typeof(a.metadata -> 'eligible_plan_ids') = 'array'"
        " AND jsonb_array_length(a.metadata -> 'eligible_plan_ids') > 0"
        " AND (a.metadata ->> 'plan_id' IS NULL"
        " OR NOT (a.metadata -> 'eligible_plan_ids') ? (a.metadata ->> 'plan_id'))",
    ),
    # E05 - at most one ai_turn charge per turn (ENT-02); a partial unique
    # index enforces it too.
    Check(
        "e05_turn_charged_twice",
        "SELECT count(*) FROM (SELECT tenant_id, agent_turn_id FROM usage_events"
        " WHERE event_type = 'ai_turn' AND agent_turn_id IS NOT NULL"
        " GROUP BY tenant_id, agent_turn_id HAVING count(*) > 1) s",
    ),
    # E06 - no charge for a turn whose outcome no generation produced, nor for
    # one its own row does not record as charged (ENT-02).
    Check(
        "e06_charge_for_a_turn_not_chargeable",
        "SELECT count(*) FROM usage_events e JOIN agent_turns t"  # noqa: S608 - module constants
        " ON t.tenant_id = e.tenant_id AND t.id = e.agent_turn_id"
        " WHERE e.event_type = 'ai_turn'"
        f" AND (t.outcome::text IN {_NOT_CHARGEABLE}"
        " OR t.charge_state IS DISTINCT FROM 'charged')",
    ),
    # E07 - no hold open past its TTL and the sweep that follows it (ENT-03).
    Check(
        "e07_hold_outliving_its_ttl_and_the_sweep",
        "SELECT count(*) FROM agent_turns WHERE charge_state = 'held'"
        " AND held_at < CAST(:now AS timestamptz)"
        " - make_interval(secs => :hold_cutoff_seconds)",
        bound=True,
    ),
    # E08 - a turn charged without its ai_turn event: charged turns and charges
    # are the same set, by turn (E06 is the other direction).
    Check(
        "e08_charged_turn_without_its_charge",
        "SELECT count(*) FROM agent_turns t WHERE t.charge_state = 'charged'"
        " AND NOT EXISTS (SELECT 1 FROM usage_events e WHERE e.tenant_id = t.tenant_id"
        " AND e.agent_turn_id = t.id AND e.event_type = 'ai_turn')",
    ),
    # E09 - every connection a reduction disabled names that reduction on its
    # disable's audit entry, and the reduction lists it (ENT-14).
    Check(
        "e09_reduction_disable_without_its_audit",
        "SELECT count(*) FROM channel_connections c"  # noqa: S608 - module constants
        f" WHERE c.status = 'disabled' AND c.disabled_reason::text IN {_REDUCTION_DISABLE}"
        " AND NOT EXISTS (SELECT 1 FROM audit_logs a"
        " JOIN channel_capacity_reductions r ON r.tenant_id = a.tenant_id"
        " AND r.id::text = a.metadata ->> 'reduction_id'"
        " WHERE a.tenant_id = c.tenant_id AND a.target_id = c.id"
        f" AND a.action IN {_DISABLED} AND c.id = ANY (r.disabled_connection_ids))",
    ),
    # E10 - the reduction flow never released or deleted a connection: each one
    # a reduction lists still exists and was disabled - not released - by it.
    Check(
        "e10_reduction_released_or_deleted_a_connection",
        "SELECT count(*) FROM channel_capacity_reductions r"  # noqa: S608 - module constants
        " CROSS JOIN LATERAL unnest(coalesce(r.disabled_connection_ids, '{}')) AS disabled(id)"
        " LEFT JOIN channel_connections c ON c.tenant_id = r.tenant_id AND c.id = disabled.id"
        " WHERE c.id IS NULL"
        " OR NOT EXISTS (SELECT 1 FROM audit_logs a WHERE a.tenant_id = r.tenant_id"
        f" AND a.target_id = disabled.id AND a.action IN {_DISABLED}"
        " AND a.metadata ->> 'reduction_id' = r.id::text)"
        " OR EXISTS (SELECT 1 FROM audit_logs a WHERE a.tenant_id = r.tenant_id"
        f" AND a.target_id = disabled.id AND a.action IN {_RELEASED}"
        " AND a.metadata ? 'reduction_id')",
    ),
    # E11 - one open reduction per workspace; a partial unique index too.
    Check(
        "e11_more_than_one_open_reduction",
        "SELECT count(*) FROM (SELECT tenant_id FROM channel_capacity_reductions"
        " WHERE status = 'pending_selection' GROUP BY tenant_id HAVING count(*) > 1) s",
    ),
    # E12 - the retired key on nothing written since ADR-131 (ENT-05): no
    # version or plan that states channel types, no product at all, and no
    # purchase typed like a channel slot.
    Check(
        "e12_retired_whatsapp_numbers_key_in_use",
        "SELECT (SELECT count(*) FROM plan_versions WHERE allowed_channel_types IS NOT NULL"
        " AND limits ? 'whatsapp_numbers')"
        " + (SELECT count(*) FROM plans WHERE allowed_channel_types IS NOT NULL"
        " AND limits ? 'whatsapp_numbers')"
        " + (SELECT count(*) FROM topup_products WHERE entitlement_key::text = 'whatsapp_numbers')"
        " + (SELECT count(*) FROM topup_purchases WHERE entitlement_key::text = 'whatsapp_numbers'"
        " AND channel_type IS NOT NULL)",
    ),
    # E13 - every marketing opt-out names its channel and who decided (ENT-19).
    Check(
        "e13_opt_out_without_channel_or_source",
        "SELECT count(*) FROM contact_channel_consents WHERE channel IS NULL"
        " OR (marketing_opt_out_at IS NOT NULL AND opt_out_source IS NULL)",
    ),
    # E14 - no campaign copy sent to somebody already opted out on the
    # campaign's channel (ENT-19).
    Check(
        "e14_campaign_copy_sent_after_an_opt_out_on_its_channel",
        "SELECT count(*) FROM campaign_recipients r"
        " JOIN campaigns k ON k.tenant_id = r.tenant_id AND k.id = r.campaign_id"
        " JOIN channel_connections cc ON cc.tenant_id = k.tenant_id AND cc.id = k.account_id"
        " JOIN messages m ON m.tenant_id = r.tenant_id AND m.id = r.message_id"
        " JOIN contact_channel_consents ct ON ct.tenant_id = r.tenant_id"
        " AND ct.contact_id = r.contact_id AND ct.channel = cc.channel"
        " WHERE r.status = 'sent' AND ct.marketing_opt_out_at IS NOT NULL"
        " AND m.created_at > ct.marketing_opt_out_at",
    ),
    # E15 - every plan version a workspace is pinned to, or scheduled onto,
    # resolves its channel types (ENT-09): unstated (WhatsApp alone) or a set
    # of distinct labels of the channel vocabulary. A trigger refuses anything
    # else on insert; this says so of every version actually in use.
    Check(
        "e15_pinned_version_with_unreadable_channel_types",
        "SELECT count(DISTINCT v.id) FROM subscriptions s JOIN plan_versions v"
        " ON v.id IN (s.plan_version_id, s.scheduled_plan_version_id)"
        " WHERE v.allowed_channel_types IS NOT NULL"
        " AND (array_position(v.allowed_channel_types, NULL) IS NOT NULL"
        " OR NOT v.allowed_channel_types::text[] <@ enum_range(NULL::channel_kind)::text[]"
        " OR cardinality(v.allowed_channel_types)"
        " <> (SELECT count(DISTINCT label) FROM unnest(v.allowed_channel_types) label))",
    ),
)


async def _counts(
    connection: AsyncConnection,
    checks: Sequence[Check],
    *,
    read_only: bool,
    bindings: Bindings | None = None,
) -> dict[str, int]:
    if read_only:
        # Before the first statement, so the run can do nothing else.
        await connection.execute(text("SET TRANSACTION READ ONLY"))
    values = (bindings or Bindings()).values()
    found: dict[str, int] = {}
    for check in checks:
        statement = text(check.query)
        result = await (
            connection.execute(statement, values) if check.bound else connection.execute(statement)
        )
        found[check.name] = int(result.scalar_one())
    return found


async def census(connection: AsyncConnection, *, read_only: bool = True) -> dict[str, int]:
    """The evidence counts, in the caller's transaction - made read-only unless it has written.

    `read_only=False` is for a caller sweeping its own uncommitted rows (a test
    inside one transaction); every statement here is a `SELECT` either way.
    """
    return await _counts(connection, CENSUS, read_only=read_only)


async def violations(
    connection: AsyncConnection, *, read_only: bool = True, bindings: Bindings | None = None
) -> dict[str, int]:
    """Every invariant's violation count. All zero on a healthy database."""
    return await _counts(
        connection, INVARIANTS + ENTITLEMENT_INVARIANTS, read_only=read_only, bindings=bindings
    )


async def entitlement_violations(
    connection: AsyncConnection, *, read_only: bool = True, bindings: Bindings | None = None
) -> dict[str, int]:
    """The entitlement ledger alone (E01-E15). All zero on a healthy database."""
    return await _counts(connection, ENTITLEMENT_INVARIANTS, read_only=read_only, bindings=bindings)


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


async def _recover(url: str, *, apply: bool) -> int:
    """Replay retained stop taps; read-only unless `apply` (OMNI-030)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.opt_out_recovery import recover_button_opt_outs

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            if not apply:
                await session.execute(text("SET TRANSACTION READ ONLY"))
            report = await recover_button_opt_outs(session, apply=apply)
            if apply:
                await session.commit()
            else:
                await session.rollback()
    finally:
        await engine.dispose()
    verb = "applied" if apply else "would_apply"
    for tenant_id, counts in sorted(report.by_workspace.items(), key=lambda item: str(item[0])):
        sys.stdout.write(
            f"workspace {tenant_id}: candidates {counts.candidates}, {verb} {counts.applied}, "
            f"already_opted_out {counts.already_opted_out}, "
            f"skipped_newer_resume {counts.skipped_newer_resume}, "
            f"no_projected_message {counts.no_projected_message}\n"
        )
    sys.stdout.write(
        f"recover-button-opt-outs {'apply' if apply else 'dry-run'}: taps_read {report.taps_read}, "
        f"candidates {report.total('candidates')}, {verb} {report.total('applied')}, "
        f"already_opted_out {report.total('already_opted_out')}, "
        f"skipped_newer_resume {report.total('skipped_newer_resume')}\n"
    )
    return 0


USAGE = (
    "usage: python -m scripts.omnichannel_invariants census|verify\n"
    "       python -m scripts.omnichannel_invariants recover-button-opt-outs [--dry-run|--apply]\n"
)


def main(argv: list[str]) -> int:
    url = os.environ.get("INVARIANTS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if argv and argv[0] == "recover-button-opt-outs":
        mode = argv[1:] or ["--dry-run"]
        if mode not in (["--dry-run"], ["--apply"]):
            sys.stderr.write(USAGE)
            return 64
        if not url:
            sys.stderr.write("omnichannel_invariants: INVARIANTS_DATABASE_URL or DATABASE_URL\n")
            return 64
        return asyncio.run(_recover(url, apply=mode == ["--apply"]))
    if len(argv) != 1 or argv[0] not in ("census", "verify"):
        sys.stderr.write(USAGE)
        return 64
    if not url:
        sys.stderr.write("omnichannel_invariants: INVARIANTS_DATABASE_URL or DATABASE_URL\n")
        return 64
    return asyncio.run(_run(argv[0], url))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main(sys.argv[1:]))

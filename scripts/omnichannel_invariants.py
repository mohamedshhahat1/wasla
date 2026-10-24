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

`verify` also runs the entitlement ledger, E01-E18 (ADR-131, ADR-132): active
connections fit the capacity in force and its channel types, AI turns are
charged once and only for a usable outcome and no hold outlives its TTL and the
sweep, every capacity reduction disables - never releases or deletes - and says
so on the audit trail, the retired `whatsapp_numbers` key is on nothing new,
every marketing opt-out names its channel, and a grant staff withdrew stops
counting, is audited and is named by the reduction it opened. Those checks read the clock, the
deployment's `DEFAULT_PLAN_CODE` and `AI_TURN_HOLD_TTL_SECONDS` from the
environment the command runs in.

`verify` also runs the campaigns' CR-01..CR-17 and the follow-ups' FU-01..FU-08
(ADR-151..ADR-154): no automatic step after the customer replied, after a
takeover, past the window less `FOLLOW_UP_WINDOW_MARGIN_SECONDS`, as a template,
or beyond `FOLLOW_UP_SEQUENCE_MAX_STEPS`; no ledger row for a free-form message;
no template follow-up inside quiet hours; no requested time moved by a wait.
With `INVARIANTS_REDIS_URL` (or `REDIS_URL`) set it adds RT-I01 (ADR-155): no
realtime hint in one workspace's stream naming another workspace's row.

`recover-button-opt-outs` is the one command here that can write, and only with
`--apply` (OMNI-030). It replays the "stop" taps still held in retained raw
payloads through the one opt-out writer, with the provenance `replay`; the dry
run (the default) is read-only like everything else. It must run on production
**before the 30-day payload redaction removes the evidence**. Output is counts
per workspace id; never a phone, a business-scoped id or a message.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.session import utc_connect_args
from app.integrations.whatsapp.policy import SERVICE_WINDOW as WHATSAPP_SERVICE_WINDOW
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
    "'no more messages', 'opt out', 'opt-out', 'optout', 'please stop', 'remove me',"
    " 'stop', 'stop please', 'stop promotions', 'unsub', 'unsubscribe'"
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
        " 'opt out', 'opt-out', 'optout', 'please stop', 'remove me',"
        " 'stop', 'stop please', 'stop promotions', 'unsub', 'unsubscribe')",
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
# ADR-131's ledger, E01-E15, and ADR-132's E16-E18. Each is zero on a healthy database. Several are
# also enforced by a key, a constraint or a trigger; this says so of the data.

# What explains a workspace holding more connections than its capacity, or one
# of a type its plan does not allow (E01, E02): an open reduction (ENT-14,
# ENT-15); a subscription that is not served (ENT-16 - nothing is disabled for
# that); or a channel slot that stopped counting by the clock within the last
# sweep, whose boundary the billing worker's next pass judges. A grant staff
# withdrew (PLAT-G1) opens its reduction in the withdrawal's own transaction, so
# the open-reduction clause - `cause = 'grant_withdrawn'` among the others -
# explains it from the moment it stops counting.
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

# The format of a campaign template's header, upper case, or NULL: `t` is the
# template row.
_HEADER_FORMAT = (
    "(SELECT upper(component ->> 'format') FROM"
    " jsonb_array_elements(COALESCE(t.components, '[]'::jsonb)) component"
    " WHERE upper(component ->> 'type') = 'HEADER' LIMIT 1)"
)
CAMPAIGN_HEADER_MEDIA_MAX_FILES = int(os.environ.get("CAMPAIGN_HEADER_MEDIA_MAX_FILES", "20"))
FOLLOW_UP_SEQUENCE_MAX_STEPS = int(os.environ.get("FOLLOW_UP_SEQUENCE_MAX_STEPS", "5"))
FOLLOW_UP_WINDOW_MARGIN_SECONDS = int(os.environ.get("FOLLOW_UP_WINDOW_MARGIN_SECONDS", "1800"))
# The customer service window every supported channel's policy module states
# (WhatsApp: `SERVICE_WINDOW`); read from it, never a literal here.
SERVICE_WINDOW_SECONDS = int(WHATSAPP_SERVICE_WINDOW.total_seconds())

# Campaigns (CR-01..CR-17, ADR-134..ADR-154): what the campaigns audit counted
# by hand after its probes, kept here so a restore or a deployment can be
# checked the same way. Every one is zero on a healthy database.
CAMPAIGN_INVARIANTS: tuple[Check, ...] = (
    # CR-01: one campaign put two of its messages in one conversation - two
    # copies on one phone (CMP-01). Read from the campaign meter, which names
    # every message a campaign sent; the recipient row only names the last.
    Check(
        "cr01_conversation_with_two_messages_of_one_campaign",
        "SELECT count(*) FROM (SELECT u.tenant_id, u.metadata ->> 'campaign_id',"
        " m.conversation_id FROM usage_events u JOIN messages m ON m.tenant_id = u.tenant_id"
        " AND m.id = CAST(u.metadata ->> 'message_id' AS uuid)"
        " WHERE u.event_type = 'campaign_message' GROUP BY 1, 2, 3 HAVING count(*) > 1) s",
    ),
    # CR-02: a campaign metered a different number of messages than it has
    # recipients sent - the doubled meter of CMP-01 (audit: 392 vs 340).
    Check(
        "cr02_campaign_meters_differ_from_recipients_sent",
        "SELECT count(*) FROM campaigns c WHERE (SELECT count(*) FROM usage_events u"
        " WHERE u.tenant_id = c.tenant_id AND u.event_type = 'campaign_message'"
        " AND u.metadata ->> 'campaign_id' = CAST(c.id AS text))"
        " <> (SELECT count(*) FROM campaign_recipients r WHERE r.campaign_id = c.id"
        " AND r.status = 'sent')",
    ),
    # CR-03: a pending recipient linked to a message that provably never left
    # (CMP-11): it would have been abandoned on resume instead of sent.
    Check(
        "cr03_pending_recipient_linked_to_an_undelivered_message",
        "SELECT count(*) FROM campaign_recipients r JOIN messages m"
        " ON m.tenant_id = r.tenant_id AND m.id = r.message_id"
        " WHERE r.status = 'pending' AND m.delivery_state = 'undelivered'",
    ),
    # CR-04: a recipient added after its campaign was scheduled - an audience
    # that grew past the reservation taken (CMP-03, CMP-04).
    Check(
        "cr04_recipient_added_after_its_campaign_was_scheduled",
        "SELECT count(*) FROM campaign_recipients r WHERE EXISTS (SELECT 1 FROM audit_logs a"
        " WHERE a.target_id = r.campaign_id AND a.action = 'campaign_scheduled'"
        " AND a.occurred_at < r.created_at)",
    ),
    # CR-05: a campaign still scheduled or running for a workspace that is not
    # served - suspended, deleted, or its subscription suspended (CMP-02, CMQ-11).
    Check(
        "cr05_live_campaign_of_a_workspace_not_served",
        "SELECT count(*) FROM campaigns c JOIN tenants t ON t.id = c.tenant_id"
        " LEFT JOIN subscriptions s ON s.tenant_id = c.tenant_id"
        " WHERE c.status IN ('scheduled', 'running') AND (t.status <> 'active'"
        " OR t.deleted_at IS NOT NULL OR s.status = 'suspended')",
    ),
    # CR-06: a live campaign on a template that is not marketing (CMP-09).
    Check(
        "cr06_live_campaign_on_a_template_that_is_not_marketing",
        "SELECT count(*) FROM campaigns c JOIN whatsapp_templates w ON w.id = c.template_id"
        " WHERE c.status IN ('scheduled', 'running') AND w.category <> 'marketing'",
    ),
    # CR-07: a recipient sent after opting out on the campaign's channel, with
    # no resume since (ENT-19, R2).
    Check(
        "cr07_recipient_sent_after_opting_out_on_that_channel",
        "SELECT count(*) FROM campaign_recipients r JOIN campaigns c ON c.id = r.campaign_id"
        " JOIN channel_connections cc ON cc.id = c.account_id"
        " JOIN contact_channel_consents k ON k.tenant_id = r.tenant_id"
        " AND k.contact_id = r.contact_id AND k.channel = cc.channel"
        " WHERE r.status = 'sent' AND k.marketing_opt_out_at < r.sent_at"
        " AND (k.resumed_at IS NULL OR k.resumed_at < k.marketing_opt_out_at)",
    ),
    # CR-08: a live campaign whose template's header is an image, a video or a
    # document, with no header file or one of another kind (HM-03): it would
    # be sent without the header Meta approved, or refused for everybody.
    Check(
        "cr08_live_media_header_campaign_without_a_matching_file",
        "SELECT count(*) FROM campaigns c JOIN whatsapp_templates w ON w.id = c.template_id"
        " CROSS JOIN LATERAL (SELECT upper(e ->> 'format') AS format FROM jsonb_array_elements("
        "CASE WHEN jsonb_typeof(w.components) = 'array' THEN w.components ELSE '[]'::jsonb END"
        ") e WHERE upper(e ->> 'type') = 'HEADER' LIMIT 1) h"
        " LEFT JOIN campaign_header_media a ON a.tenant_id = c.tenant_id"
        " AND a.id = c.header_media_id"
        " WHERE c.status IN ('scheduled', 'running', 'paused')"
        " AND h.format IN ('IMAGE', 'VIDEO', 'DOCUMENT')"
        " AND (c.header_media_id IS NULL OR a.kind IS DISTINCT FROM lower(h.format))",
    ),
    # CR-09: Meta's id for a campaign's header file uploaded through another
    # number than the one the campaign sends from (HM-04, HM-05).
    Check(
        "cr09_header_media_uploaded_through_another_number",
        "SELECT count(*) FROM campaigns c WHERE c.header_provider_media_id IS NOT NULL"
        " AND c.header_provider_account_id IS DISTINCT FROM c.account_id",
    ),
    # CR-10: a campaign naming another workspace's header file - defence in
    # depth beside `fk_campaigns_tenant_header_media` (HM-02).
    Check(
        "cr10_campaign_naming_another_workspaces_header_file",
        "SELECT count(*) FROM campaigns c JOIN campaign_header_media a"
        " ON a.id = c.header_media_id WHERE a.tenant_id <> c.tenant_id",
    ),
    # CR-11: two marketing templates accepted for one contact of one workspace
    # within 24 hours - from campaigns or by hand, never a test (FREQ-01,
    # CMQ-03). Counted per pair whose later send was made under the cap (its
    # message carries the claim): two sends from before 0113 are history the
    # cap never judged, while one from before it and one after is a breach
    # (CPF-04).
    Check(
        "cr11_two_marketing_messages_to_one_contact_within_a_day",
        "WITH marketing AS (SELECT m.id, m.tenant_id, v.contact_id, m.sent_at,"
        " m.marketing_cap_claimed_at IS NOT NULL AS claimed"
        " FROM messages m JOIN conversations v ON v.tenant_id = m.tenant_id"
        " AND v.id = m.conversation_id"
        " WHERE m.direction = 'outbound' AND m.origin IN ('campaign', 'human')"
        " AND m.sent_at IS NOT NULL AND m.delivery_state <> 'undelivered'"
        " AND EXISTS (SELECT 1 FROM whatsapp_templates t WHERE t.tenant_id = m.tenant_id"
        " AND t.account_id = v.account_id AND t.name = m.template_name"
        " AND t.language = m.template_language AND t.category = 'marketing'))"
        " SELECT count(*) FROM marketing a JOIN marketing b ON b.tenant_id = a.tenant_id"
        " AND b.contact_id = a.contact_id AND b.id <> a.id AND b.claimed"
        " AND b.sent_at >= a.sent_at"
        " AND (b.sent_at > a.sent_at OR b.id > a.id)"
        " AND b.sent_at < a.sent_at + interval '24 hours'",
    ),
    # CR-12: a campaign's ledger row past its budget - the distinct people of
    # its portfolio in the 24 hours ending at it, every source counted, above
    # floor(share x limit) recorded with it (LIM-03, LIM-04). Unlimited rows
    # record no budget and are not checked.
    Check(
        "cr12_campaign_send_past_the_portfolio_budget",
        "SELECT count(*) FROM business_initiated_sends b WHERE b.source = 'campaign'"
        " AND b.budget_at_send IS NOT NULL AND (SELECT count(DISTINCT x.recipient)"
        " FROM business_initiated_sends x WHERE x.portfolio_id = b.portfolio_id"
        " AND x.sent_at > b.sent_at - interval '24 hours' AND x.sent_at <= b.sent_at)"
        " > b.budget_at_send",
    ),
    # CR-13: a campaign naming another workspace's segment - defence in depth
    # beside `fk_campaigns_tenant_audience_segment` (PG-06).
    Check(
        "cr13_campaign_naming_another_workspaces_segment",
        "SELECT count(*) FROM campaigns c JOIN campaign_segments s"
        " ON s.id = c.audience_segment_id WHERE s.tenant_id <> c.tenant_id",
    ),
    # CR-14: a test send counted as the campaign's - a recipient linked to a
    # `campaign_test` message, or a campaign meter naming one (PG-09).
    Check(
        "cr14_test_message_counted_as_a_campaign_send",
        "SELECT (SELECT count(*) FROM campaign_recipients r JOIN messages m"
        " ON m.tenant_id = r.tenant_id AND m.id = r.message_id WHERE m.origin = 'campaign_test')"
        " + (SELECT count(*) FROM usage_events u JOIN messages m ON m.tenant_id = u.tenant_id"
        " AND m.id = CAST(u.metadata ->> 'message_id' AS uuid)"
        " WHERE u.event_type = 'campaign_message' AND m.origin = 'campaign_test')",
    ),
    # CR-15: a sent recipient whose own values were resolved for somebody else
    # (PER-02): another contact's name in this person's message.
    Check(
        "cr15_recipient_sent_values_resolved_for_another_contact",
        "SELECT count(*) FROM campaign_recipients r WHERE r.status = 'sent'"
        " AND r.rendered_parameters IS NOT NULL"
        " AND r.rendered_parameters ->> 'contact_id' IS DISTINCT FROM CAST(r.contact_id AS text)",
    ),
    # CR-16: a sent recipient whose header file - their own (HMR-01) or the
    # campaign's - belongs to another workspace, or is not of the kind the
    # template's header takes.
    Check(
        "cr16_recipient_header_file_of_another_workspace_or_kind",
        "SELECT count(*) FROM campaign_recipients r"  # noqa: S608 - module constants
        " JOIN campaigns c ON c.id = r.campaign_id"
        " JOIN campaign_header_media h ON h.id = COALESCE(r.header_media_id, c.header_media_id)"
        " JOIN whatsapp_templates t ON t.id = c.template_id"
        f" WHERE r.status = 'sent' AND (h.tenant_id <> r.tenant_id"
        f" OR upper(h.kind) IS DISTINCT FROM {_HEADER_FORMAT})",
    ),
    # CR-17: a per-recipient campaign naming more distinct files than a
    # campaign may (HMR-01), or a sent recipient with no file although the
    # template's header needs one.
    Check(
        "cr17_header_files_over_the_maximum_or_missing",
        "SELECT (SELECT count(*) FROM campaigns c WHERE c.header_media_mode = 'per_recipient'"  # noqa: S608 - module constants
        " AND (SELECT count(DISTINCT x) FROM ("
        " SELECT r.header_media_id AS x FROM campaign_recipients r WHERE r.campaign_id = c.id"
        " UNION SELECT CAST(rule ->> 'header_media_id' AS uuid)"
        " FROM jsonb_array_elements(COALESCE(c.header_media_rules, '[]'::jsonb)) rule"
        " UNION SELECT c.header_media_id) files WHERE x IS NOT NULL)"
        f" > {CAMPAIGN_HEADER_MEDIA_MAX_FILES})"
        " + (SELECT count(*) FROM campaign_recipients r JOIN campaigns c ON c.id = r.campaign_id"
        " JOIN whatsapp_templates t ON t.id = c.template_id"
        f" WHERE r.status = 'sent' AND {_HEADER_FORMAT} IN ('IMAGE', 'VIDEO', 'DOCUMENT')"
        " AND COALESCE(r.header_media_id, c.header_media_id) IS NULL)",
    ),
)

# The moment a follow-up was sent: its message, staged in the send intent's
# own transaction before the provider call. `sent_at` is written after the
# call, and a customer's reply that raced the call is not a violation.
_SENT_MOMENT = (
    "COALESCE((SELECT om.created_at FROM messages om WHERE om.id = f.message_id"
    " AND om.tenant_id = f.tenant_id), f.sent_at)"
)
_STEP_SENT = "f.origin = 'sequence' AND f.status = 'sent'"

# Follow-ups (FU-01..FU-08, ADR-151..ADR-153): what the stop rules, the window
# rule, the step limit, the ledger rule, quiet hours and the kept request time
# promise. Every one is zero on a healthy database.
FOLLOW_UP_INVARIANTS: tuple[Check, ...] = (
    # FU-01: a sequence step sent although the customer had written after the
    # step's anchor - a reply stops the run (FUS-06).
    Check(
        "fu01_step_sent_after_the_customer_replied",
        f"SELECT count(*) FROM follow_ups f WHERE {_STEP_SENT}"  # noqa: S608 - module constants
        " AND EXISTS (SELECT 1 FROM messages m WHERE m.tenant_id = f.tenant_id"
        " AND m.conversation_id = f.conversation_id AND m.direction = 'inbound'"
        f" AND m.created_at > f.anchor_at AND m.created_at < {_SENT_MOMENT})",
    ),
    # FU-02: a sequence step sent while a person owned the conversation: a
    # takeover after the anchor with no hand-back to the AI before the send.
    Check(
        "fu02_step_sent_while_a_person_owned_the_conversation",
        f"SELECT count(*) FROM follow_ups f WHERE {_STEP_SENT}"  # noqa: S608 - module constants
        " AND EXISTS (SELECT 1 FROM audit_logs a WHERE a.tenant_id = f.tenant_id"
        " AND a.target_id = f.conversation_id AND a.action = 'conversation_taken_over'"
        f" AND a.occurred_at > f.anchor_at AND a.occurred_at < {_SENT_MOMENT}"
        " AND NOT EXISTS (SELECT 1 FROM audit_logs b WHERE b.tenant_id = a.tenant_id"
        " AND b.target_id = a.target_id AND b.action = 'conversation_released_to_ai'"
        f" AND b.occurred_at > a.occurred_at AND b.occurred_at < {_SENT_MOMENT}))",
    ),
    # FU-03: a sequence step sent later than the customer's window minus the
    # margin (FUS-05): 24 hours after their last message before the send.
    Check(
        "fu03_step_sent_past_the_window_less_the_margin",
        f"SELECT count(*) FROM follow_ups f WHERE {_STEP_SENT}"  # noqa: S608 - module constants
        f" AND {_SENT_MOMENT} > COALESCE((SELECT max(m.created_at) FROM messages m"
        " WHERE m.tenant_id = f.tenant_id AND m.conversation_id = f.conversation_id"
        f" AND m.direction = 'inbound' AND m.created_at <= {_SENT_MOMENT}),"
        " '-infinity'::timestamptz)"
        f" + interval '{SERVICE_WINDOW_SECONDS} seconds'"
        f" - interval '{FOLLOW_UP_WINDOW_MARGIN_SECONDS} seconds'",
    ),
    # FU-04: a sequence step sent as a template - a step is always free-form,
    # never a template and never a message tag (FUS-05).
    Check(
        "fu04_step_sent_as_a_template",
        f"SELECT count(*) FROM follow_ups f WHERE {_STEP_SENT}"  # noqa: S608 - module constants
        " AND (f.template_name IS NOT NULL OR EXISTS (SELECT 1 FROM messages om"
        " WHERE om.id = f.message_id AND om.tenant_id = f.tenant_id"
        " AND (om.kind = 'template' OR om.template_name IS NOT NULL)))",
    ),
    # FU-05: a run that sent more steps than a run may have, or a conversation
    # with two active runs at once (FUS-02, FUS-03).
    Check(
        "fu05_run_over_its_steps_or_two_active_runs",
        "SELECT (SELECT count(*) FROM follow_up_sequence_runs r"  # noqa: S608 - module constants
        " WHERE (SELECT count(*) FROM follow_ups f WHERE f.sequence_run_id = r.id"
        f" AND f.status = 'sent') > {FOLLOW_UP_SEQUENCE_MAX_STEPS})"
        " + (SELECT count(*) FROM (SELECT conversation_id FROM follow_up_sequence_runs"
        " WHERE state = 'active' GROUP BY tenant_id, conversation_id HAVING count(*) > 1) s)",
    ),
    # FU-06: a business-initiated ledger row naming a free-form message:
    # only templates count toward Meta's portfolio limit (FLIM-03).
    Check(
        "fu06_ledger_row_for_a_free_form_message",
        "SELECT count(*) FROM business_initiated_sends b JOIN messages m"
        " ON m.id = b.message_id AND m.tenant_id = b.tenant_id"
        " WHERE m.kind <> 'template' AND m.template_name IS NULL",
    ),
    # FU-07: a template follow-up sent inside the workspace's quiet hours
    # (FQH-01), in the workspace's zone. Counted from the workspace's
    # follow-up settings onwards - before them nothing bound follow-ups to quiet
    # hours - and against the quiet hours in force now: an edit after the send
    # can raise it, which the audit log explains.
    Check(
        "fu07_template_follow_up_sent_in_quiet_hours",
        "SELECT count(*) FROM follow_ups f JOIN tenants t ON t.id = f.tenant_id"
        " JOIN follow_up_settings fs ON fs.tenant_id = f.tenant_id"
        " WHERE f.status = 'sent' AND f.template_name IS NOT NULL"
        " AND t.campaign_quiet_hours_enabled AND f.sent_at >= fs.created_at"
        " AND CASE WHEN t.campaign_quiet_hours_start < t.campaign_quiet_hours_end"
        " THEN CAST(f.sent_at AT TIME ZONE t.timezone AS time) >= t.campaign_quiet_hours_start"
        " AND CAST(f.sent_at AT TIME ZONE t.timezone AS time) < t.campaign_quiet_hours_end"
        " ELSE CAST(f.sent_at AT TIME ZONE t.timezone AS time) >= t.campaign_quiet_hours_start"
        " OR CAST(f.sent_at AT TIME ZONE t.timezone AS time) < t.campaign_quiet_hours_end END",
    ),
    # FU-08: a follow-up whose requested time moved (FQH-04). A wait moves only
    # `scheduled_at`, and always later: so with no wait and no failed attempt
    # the two are equal, and after a wait the request is earlier.
    Check(
        "fu08_requested_time_changed",
        "SELECT count(*) FROM follow_ups f WHERE"
        " (f.attempts = 0 AND COALESCE(jsonb_array_length(f.deferrals), 0) = 0"
        " AND f.scheduled_at <> f.requested_at)"
        " OR (COALESCE(jsonb_array_length(f.deferrals), 0) > 0"
        " AND f.requested_at >= f.scheduled_at)",
    ),
)

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
    # E16 - no withdrawn grant counts toward any capacity or allowance
    # (PLAT-G1): a purchase that records a withdrawal holds a status the
    # entitlement queries never count.
    Check(
        "e16_withdrawn_grant_still_counting",
        "SELECT count(*) FROM topup_purchases"
        " WHERE (withdrawn_at IS NOT NULL OR status::text = 'withdrawn')"
        " AND status::text IN ('granted', 'refund_review')",
    ),
    # E17 - every withdrawn grant has its audit entry: who, why, before, after.
    Check(
        "e17_withdrawn_grant_without_its_audit",
        "SELECT count(*) FROM topup_purchases tp WHERE tp.status::text = 'withdrawn'"
        " AND NOT EXISTS (SELECT 1 FROM audit_logs a WHERE a.tenant_id = tp.tenant_id"
        " AND a.target_id = tp.id AND a.action = 'billing_topup_grant_withdrawn')",
    ),
    # E18 - every reduction a withdrawal opened names a withdrawn platform
    # grant of its own workspace; a CHECK makes it name one at all.
    Check(
        "e18_grant_withdrawn_reduction_without_its_grant",
        "SELECT count(*) FROM channel_capacity_reductions r"
        " WHERE r.cause::text = 'grant_withdrawn' AND NOT EXISTS (SELECT 1 FROM topup_purchases tp"
        " WHERE tp.id = r.topup_purchase_id AND tp.tenant_id = r.tenant_id"
        " AND tp.source::text = 'platform_grant' AND tp.status::text = 'withdrawn')",
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
        connection,
        INVARIANTS + ENTITLEMENT_INVARIANTS + CAMPAIGN_INVARIANTS + FOLLOW_UP_INVARIANTS,
        read_only=read_only,
        bindings=bindings,
    )


async def campaign_violations(
    connection: AsyncConnection, *, read_only: bool = True
) -> dict[str, int]:
    """The campaign invariants alone (CR-01..CR-17). All zero on a healthy database."""
    return await _counts(connection, CAMPAIGN_INVARIANTS, read_only=read_only)


async def follow_up_violations(
    connection: AsyncConnection, *, read_only: bool = True
) -> dict[str, int]:
    """The follow-up invariants alone (FU-01..FU-08). All zero on a healthy database."""
    return await _counts(connection, FOLLOW_UP_INVARIANTS, read_only=read_only)


#: The id fields a realtime hint may carry, and the table each names.
_HINT_IDS: Final[dict[str, str]] = {
    "conversation_id": "conversations",
    "message_id": "messages",
    "campaign_id": "campaigns",
}
REALTIME_CHECK = "rt_i01_hint_naming_another_workspace"


async def realtime_violations(connection: AsyncConnection, redis: Redis) -> dict[str, int]:
    """RT-I01: hints in a workspace's stream that name another workspace's rows.

    Reads every workspace stream (`wasla:rt:events:*`) and asks the database,
    per id a hint carries, which workspace owns it. An id no table holds (a
    row since deleted) is not counted.
    """
    from app.core.events import STREAM_PREFIX

    named: dict[str, set[tuple[str, str]]] = {table: set() for table in _HINT_IDS.values()}
    async for key in redis.scan_iter(match=f"{STREAM_PREFIX}*"):
        stream = key.decode() if isinstance(key, bytes) else str(key)
        tenant = stream.removeprefix(STREAM_PREFIX)
        for _entry_id, fields in await redis.xrange(stream):
            values = {
                (k.decode() if isinstance(k, bytes) else k): (
                    v.decode() if isinstance(v, bytes) else v
                )
                for k, v in fields.items()
            }
            data = json.loads(values.get("data", "{}"))
            for field_name, table in _HINT_IDS.items():
                if data.get(field_name):
                    named[table].add((tenant, str(data[field_name])))
    broken = 0
    for table, pairs in named.items():
        for tenant, row_id in pairs:
            owner = (
                await connection.execute(
                    text(
                        f"SELECT CAST(tenant_id AS text) FROM {table} WHERE id = :id"  # noqa: S608
                    ),
                    {"id": uuid.UUID(row_id)},
                )
            ).scalar_one_or_none()
            if owner is not None and owner != tenant:
                broken += 1
    return {REALTIME_CHECK: broken}


async def entitlement_violations(
    connection: AsyncConnection, *, read_only: bool = True, bindings: Bindings | None = None
) -> dict[str, int]:
    """The entitlement ledger alone (E01-E18). All zero on a healthy database."""
    return await _counts(connection, ENTITLEMENT_INVARIANTS, read_only=read_only, bindings=bindings)


async def _run(command: str, url: str, redis_url: str | None = None) -> int:
    engine = create_async_engine(url, poolclass=NullPool, connect_args=utc_connect_args(url))
    try:
        async with engine.connect() as connection, connection.begin():
            if command == "census":
                found = await census(connection)
            else:
                found = await violations(connection)
                if redis_url:
                    # RT-I01 reads the realtime streams too, when there is a
                    # Redis to read them from.
                    redis = Redis.from_url(redis_url)
                    try:
                        found |= await realtime_violations(connection, redis)
                    finally:
                        await redis.aclose()
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

    engine = create_async_engine(url, poolclass=NullPool, connect_args=utc_connect_args(url))
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
    redis_url = os.environ.get("INVARIANTS_REDIS_URL") or os.environ.get("REDIS_URL")
    return asyncio.run(_run(argv[0], url, redis_url))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main(sys.argv[1:]))

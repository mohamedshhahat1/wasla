"""Backfill the last day's sends: each contact's marketing claim, and the ledger.

Revision ID: 0120
Revises: 0119

CPF-04, found by the populated upgrade. 0111 created the ledger empty and 0113
added `contacts.last_marketing_sent_at` empty, so for the first 24 hours after
the deploy neither could see what was sent just before it:

- **the per-customer cap** (FREQ-01): a customer who received a campaign
  minutes before the upgrade could receive another minutes after (CR-11 = 1);
- **Meta's limit** (LIM-03, LIM-04): the people messaged before the upgrade are
  in Meta's moving window but were not in Wasla's, so the first day's budget
  started from zero.

Both are filled from the last 24 hours of messages - only the last day
matters, an older send no longer counts toward either.

**The claim** - every contact sent a marketing template gets the latest such
send: the definition CR-11 counts (outbound, from a campaign or a person, never
a test, accepted and not provably undelivered, on a template the conversation's
number holds as MARKETING). A value already set and newer is kept.

**The ledger** - every accepted template message on a WhatsApp number, whatever
its category or origin, one row each, under the number's portfolio (created
unknown and keyed by its WABA, as `portfolio_for_waba` does; the number is
linked to it). A template sent inside the person's service window is counted
too: whether the window was open at that moment is not recorded, and counting
somebody who did not count is the safe error (LIM-03). A message already in
the ledger is skipped, so a re-run adds nothing.

**Online.** Every statement is bounded by one day's template messages, not by
the tables it touches, under a bounded `lock_timeout`.

**Downgrade** changes nothing: the claims are 0113's column and the rows 0111's
table, and their own downgrades refuse while any is there.
"""

from __future__ import annotations

from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"

# The template messages of the last day that Meta accepted.
_RECENT = (
    "m.direction = 'outbound' AND m.kind = 'template'"
    " AND m.sent_at IS NOT NULL AND m.sent_at > now() - interval '24 hours'"
    " AND m.delivery_state IS DISTINCT FROM 'undelivered'"
)
_MESSAGES = (
    " FROM messages m JOIN conversations v ON v.tenant_id = m.tenant_id"
    " AND v.id = m.conversation_id"
)
# A number that sent one of them.
_SENT_ON_NUMBER = f"EXISTS (SELECT 1{_MESSAGES} WHERE {_RECENT} AND v.account_id = a.id)"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(
        "UPDATE contacts c SET last_marketing_sent_at = recent.sent_at"  # noqa: S608 - module constants
        f" FROM (SELECT v.tenant_id, v.contact_id, max(m.sent_at) AS sent_at{_MESSAGES}"
        f" WHERE {_RECENT} AND m.origin IN ('campaign', 'human')"
        " AND EXISTS (SELECT 1 FROM whatsapp_templates t WHERE t.tenant_id = m.tenant_id"
        " AND t.account_id = v.account_id AND t.name = m.template_name"
        " AND t.language = m.template_language AND t.category = 'marketing')"
        " GROUP BY v.tenant_id, v.contact_id) recent"
        " WHERE c.tenant_id = recent.tenant_id AND c.id = recent.contact_id"
        " AND (c.last_marketing_sent_at IS NULL OR c.last_marketing_sent_at < recent.sent_at)"
    )
    op.execute(
        "INSERT INTO whatsapp_business_portfolios (id, key_kind, meta_key)"  # noqa: S608 - module constants
        " SELECT gen_random_uuid(), 'waba', w.waba_id FROM (SELECT DISTINCT a.waba_id"
        f" FROM whatsapp_accounts a WHERE a.portfolio_id IS NULL AND {_SENT_ON_NUMBER}) w"
        " ON CONFLICT ON CONSTRAINT uq_whatsapp_business_portfolios_key DO NOTHING"
    )
    op.execute(
        "UPDATE whatsapp_accounts a SET portfolio_id = p.id FROM whatsapp_business_portfolios p"  # noqa: S608 - module constants
        " WHERE a.portfolio_id IS NULL AND p.key_kind = 'waba' AND p.meta_key = a.waba_id"
        f" AND {_SENT_ON_NUMBER}"
    )
    op.execute(
        "INSERT INTO business_initiated_sends (id, portfolio_id, recipient, sent_at, source,"  # noqa: S608 - module constants
        " tenant_id, campaign_id, message_id)"
        " SELECT gen_random_uuid(), a.portfolio_id, i.value, m.sent_at,"
        " CASE m.origin::text WHEN 'campaign' THEN 'campaign'"
        " WHEN 'campaign_test' THEN 'campaign_test' WHEN 'human' THEN 'manual_template'"
        " WHEN 'follow_up' THEN 'follow_up' ELSE 'other' END,"
        f" m.tenant_id, r.campaign_id, m.id{_MESSAGES}"
        " JOIN whatsapp_accounts a ON a.id = v.account_id"
        " JOIN contact_identities i ON i.id = v.participant_identity_id"
        " LEFT JOIN campaign_recipients r ON r.tenant_id = m.tenant_id AND r.message_id = m.id"
        f" WHERE {_RECENT} AND a.portfolio_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM business_initiated_sends b WHERE b.message_id = m.id)"
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    # Data only: 0111's and 0113's downgrades decide whether the rows may go.
    pass

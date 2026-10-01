"""Backfill the neutral keys on the largest tables, in bounded batches.

Revision ID: 0083
Revises: 0082

Four backfills, each walked in primary-key order in batches of `BATCH` rows,
**each batch its own transaction**: no statement holds more than a batch's row
locks, and nothing here holds a table lock at all (OMNI-004, OMNI-005,
OMNI-009). Every one is idempotent - it only fills what is still empty - so a
run that stops part-way is finished by running it again.

1. `conversations.participant_identity_id` - the contact's WhatsApp phone
   identity. Before 0082 every conversation was a WhatsApp conversation
   addressed through `contacts.wa_id`, which is exactly that identity, so this
   records the address each conversation was already using. Nothing is chosen.
2. `messages.connection_id` - the conversation's connection.
3. `message_media.locator` - the WhatsApp handle as a `handle` locator.
4. `campaign_recipients.participant_identity_id` - the participant of the
   recipient's conversation on the campaign's number, where one exists.

**Refused, before anything is written**, if a conversation's contact holds no
WhatsApp phone identity: there is then no deterministic address to record, and
0084's NOT NULL could never hold. After the backfill the same questions are
asked again, so a count that 0084 would trip over is reported here, by name.

Downgrade writes nothing: the columns belong to 0082, whose downgrade drops them.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None

# Rows per transaction. Small enough that a batch's row locks are gone in
# milliseconds; large enough that a table of tens of millions finishes.
BATCH = 5_000

# Below every generated UUID, so the walk starts at the beginning.
FIRST = uuid.UUID(int=0)

PRECHECKS = (
    (
        "conversations whose contact holds no WhatsApp phone identity",
        "SELECT count(*) FROM conversations c WHERE c.participant_identity_id IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM contact_identities i WHERE i.tenant_id = c.tenant_id"
        " AND i.contact_id = c.contact_id AND i.channel = c.channel AND i.kind = 'phone')",
    ),
)

POSTCHECKS = (
    (
        "conversations still without a participant",
        "SELECT count(*) FROM conversations WHERE participant_identity_id IS NULL",
    ),
    (
        "messages still without a connection",
        "SELECT count(*) FROM messages WHERE connection_id IS NULL",
    ),
)

# (table, UPDATE for one batch of that table's ids). Each statement fills only
# what is still empty, so a repeated batch changes nothing.
BACKFILLS = (
    (
        "conversations",
        "UPDATE conversations c SET participant_identity_id = i.id"
        " FROM contact_identities i"
        " WHERE c.id = ANY(:ids) AND c.participant_identity_id IS NULL"
        " AND i.tenant_id = c.tenant_id AND i.contact_id = c.contact_id"
        " AND i.channel = c.channel AND i.kind = 'phone'",
    ),
    (
        "messages",
        "UPDATE messages m SET connection_id = c.account_id"
        " FROM conversations c"
        " WHERE m.id = ANY(:ids) AND m.connection_id IS NULL"
        " AND c.id = m.conversation_id AND c.tenant_id = m.tenant_id",
    ),
    (
        "message_media",
        "UPDATE message_media SET locator_kind = 'handle', locator = wa_media_id"
        " WHERE id = ANY(:ids) AND wa_media_id IS NOT NULL AND locator IS NULL",
    ),
    (
        "campaign_recipients",
        "UPDATE campaign_recipients r SET participant_identity_id = c.participant_identity_id"
        " FROM campaigns k, conversations c"
        " WHERE r.id = ANY(:ids) AND r.participant_identity_id IS NULL"
        " AND k.id = r.campaign_id AND k.tenant_id = r.tenant_id"
        " AND c.tenant_id = r.tenant_id AND c.contact_id = r.contact_id"
        " AND c.account_id = k.account_id AND c.participant_identity_id IS NOT NULL",
    ),
)


def _refuse(checks: tuple[tuple[str, str], ...], preamble: str) -> None:
    connection = op.get_bind()
    found = []
    for label, query in checks:
        count = connection.exec_driver_sql(query).scalar_one()
        if count:
            found.append(f"{label}: {count}")
    if found:
        raise RuntimeError(
            f"{preamble} (docs/RUNBOOK.md, 'Omnichannel foundation (0082-0084)'):\n  "
            + "\n  ".join(found)
        )


def _walk(table: str, update: str) -> int:
    """Run `update` over every id of `table`, `BATCH` ids at a time, in id order.

    Keyset rather than `WHERE column IS NULL LIMIT n`: the second rescans the
    rows it has already filled on every batch, which on the largest table in
    the schema turns a linear backfill into a quadratic one.
    """
    bind = op.get_bind()
    after = FIRST
    changed = 0
    while True:
        ids = list(
            bind.execute(
                sa.text(
                    f"SELECT id FROM {table} WHERE id > :after"  # noqa: S608 - table is a module constant
                    " ORDER BY id LIMIT :limit"
                ),
                {"after": after, "limit": BATCH},
            ).scalars()
        )
        if not ids:
            return changed
        result = bind.execute(sa.text(update), {"ids": ids})
        changed += result.rowcount or 0
        after = ids[-1]


def upgrade() -> None:
    _refuse(PRECHECKS, "Conversations exist whose address cannot be determined; nothing written")
    with op.get_context().autocommit_block():
        for table, update in BACKFILLS:
            _walk(table, update)
    _refuse(POSTCHECKS, "The neutral key backfill is incomplete")


def downgrade() -> None:
    """Nothing to undo: 0082's downgrade drops these columns."""

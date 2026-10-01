"""Make the neutral keys the database's: connection, participant, provider identity, files.

Revision ID: 0084
Revises: 0083

Switches authority to the columns 0082 added and 0083 filled (OMNI-003,
OMNI-004, OMNI-005, OMNI-007, OMNI-009, OMNI-022; ADR-117 to ADR-120):

- A conversation's key names its **connection** - `(tenant_id, account_id,
  channel)` into `channel_connections` - instead of the WhatsApp number. The
  values do not change: a number and its connection share an id.
- A conversation's **participant** belongs to its contact, in its workspace, on
  its channel; and every conversation has one.
- A message's **connection** is its conversation's, and a provider message id is
  unique per connection - the scope the neutral path judges dedup by. The
  workspace-wide key stays until the compatibility cleanup (ADR-120).
- An event's connection is a real connection in the event's own workspace, and
  an event id is unique per connection.
- A **file**'s message is in the file's conversation and workspace (OMNI-022:
  the audit inserted one naming another workspace's message, and it was
  accepted), and a message may carry several, ordered.
- A campaign recipient's pinned identity is its own contact's, in its own workspace.

**Online, in this order.** Every new unique index is built `CONCURRENTLY`,
dropping an INVALID leftover of its own name first so a retry finishes. The keys
are added `NOT VALID` - a moment's lock, no scan - and committed; each is then
validated **in a transaction of its own**, which takes only a SHARE UPDATE
EXCLUSIVE lock and lets reads and writes continue while it scans. NOT NULL is
set through a validated CHECK, which PostgreSQL uses instead of scanning again.
Only then are the superseded keys dropped.

**Refused before anything is changed** if a row exists that a new key would
refuse: a conversation whose connection or participant disagrees with it, a
message whose connection is not its conversation's, an event on a connection
that is not its workspace's, a file naming another workspace's message or
conversation.

**Downgrade** restores the keys this replaced and refuses while a row exists
that they cannot hold: a conversation or event on a connection that is not a
WhatsApp number, or a message carrying more than one file.

**The downgrade is one transaction, and deliberately not online.** A downgrade
is a rollback, and the property a rollback needs is that it happens entirely or
not at all. Alembic runs a whole `downgrade` in one transaction, and an
autocommit block commits everything before it: built online, this revision's
key changes would commit and then 0082's guard, refusing a contact that has no
phone number, would roll back only the version rows - leaving the database at
"0084" with 0084's keys gone. Transactional, a refusal anywhere in the run undoes
all of it, at the price of write locks held while the restored keys validate.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"

PRECHECKS = (
    (
        "conversations whose connection is missing, another workspace's or another channel's",
        "SELECT count(*) FROM conversations c WHERE NOT EXISTS (SELECT 1 FROM channel_connections k"
        " WHERE k.tenant_id = c.tenant_id AND k.id = c.account_id AND k.channel = c.channel)",
    ),
    (
        "conversations whose participant is not their contact's identity on their channel",
        "SELECT count(*) FROM conversations c WHERE c.participant_identity_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM contact_identities i WHERE i.tenant_id = c.tenant_id"
        " AND i.contact_id = c.contact_id AND i.id = c.participant_identity_id"
        " AND i.channel = c.channel)",
    ),
    (
        "messages whose connection is not their conversation's",
        "SELECT count(*) FROM messages m WHERE m.connection_id IS NOT NULL AND NOT EXISTS ("
        " SELECT 1 FROM conversations c WHERE c.tenant_id = m.tenant_id"
        " AND c.id = m.conversation_id AND c.account_id = m.connection_id)",
    ),
    (
        "events whose connection is missing, another workspace's or another channel's",
        "SELECT count(*) FROM whatsapp_events e WHERE NOT EXISTS (SELECT 1 FROM"
        " channel_connections k WHERE k.tenant_id = e.tenant_id AND k.id = e.account_id"
        " AND k.channel = e.channel)",
    ),
    (
        "files naming a message outside their own conversation or workspace",
        "SELECT count(*) FROM message_media f WHERE NOT EXISTS (SELECT 1 FROM messages m"
        " WHERE m.tenant_id = f.tenant_id AND m.conversation_id = f.conversation_id"
        " AND m.id = f.message_id)",
    ),
    (
        "files naming a conversation outside their own workspace",
        "SELECT count(*) FROM message_media f WHERE NOT EXISTS (SELECT 1 FROM conversations c"
        " WHERE c.tenant_id = f.tenant_id AND c.id = f.conversation_id)",
    ),
    (
        "campaign recipients addressed to an identity that is not their contact's",
        "SELECT count(*) FROM campaign_recipients r WHERE r.participant_identity_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM contact_identities i WHERE i.tenant_id = r.tenant_id"
        " AND i.contact_id = r.contact_id AND i.id = r.participant_identity_id)",
    ),
)

DOWNGRADE_PRECHECKS = (
    (
        "conversations on a connection that is not a WhatsApp number",
        "SELECT count(*) FROM conversations c WHERE NOT EXISTS (SELECT 1 FROM whatsapp_accounts a"
        " WHERE a.tenant_id = c.tenant_id AND a.id = c.account_id)",
    ),
    (
        "events on a connection that is not a WhatsApp number",
        "SELECT count(*) FROM whatsapp_events e WHERE NOT EXISTS (SELECT 1 FROM whatsapp_accounts a"
        " WHERE a.id = e.account_id)",
    ),
    (
        "messages carrying more than one file",
        "SELECT count(*) FROM (SELECT message_id FROM message_media GROUP BY message_id"
        " HAVING count(*) > 1) s",
    ),
)

# (name, table, columns, unique). Built CONCURRENTLY; the unique ones are then
# attached as constraints, which is what the models declare.
INDEXES = (
    (
        "uq_conversations_tenant_id_id_account_id",
        "conversations",
        "tenant_id, id, account_id",
        True,
    ),
    (
        "uq_messages_tenant_id_conversation_id_id",
        "messages",
        "tenant_id, conversation_id, id",
        True,
    ),
    (
        "uq_messages_tenant_id_connection_id_wa_message_id",
        "messages",
        "tenant_id, connection_id, wa_message_id",
        True,
    ),
    (
        "uq_whatsapp_events_tenant_id_account_id_event_id",
        "whatsapp_events",
        "tenant_id, account_id, event_id",
        True,
    ),
    ("uq_message_media_message_id_position", "message_media", "message_id, position", True),
    (
        "ix_conversations_tenant_id_account_id_last_message_at",
        "conversations",
        "tenant_id, account_id, last_message_at DESC NULLS LAST, id DESC",
        False,
    ),
    (
        "ix_conversations_participant_identity_id",
        "conversations",
        "participant_identity_id",
        False,
    ),
)

PARTIAL_INDEXES = (
    (
        "ix_campaign_recipients_participant_identity_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_campaign_recipients_participant_identity_id"
        " ON campaign_recipients (participant_identity_id)"
        " WHERE participant_identity_id IS NOT NULL",
    ),
)

# (name, child, columns, parent, parent columns, ON DELETE clause).
KEYS = (
    (
        "fk_conversations_tenant_connection",
        "conversations",
        "tenant_id, account_id, channel",
        "channel_connections",
        "tenant_id, id, channel",
        " ON DELETE CASCADE",
    ),
    (
        "fk_conversations_tenant_participant",
        "conversations",
        "tenant_id, contact_id, participant_identity_id, channel",
        "contact_identities",
        "tenant_id, contact_id, id, channel",
        "",
    ),
    (
        "fk_messages_tenant_conversation_connection",
        "messages",
        "tenant_id, conversation_id, connection_id",
        "conversations",
        "tenant_id, id, account_id",
        " ON DELETE CASCADE",
    ),
    (
        "fk_message_media_tenant_message",
        "message_media",
        "tenant_id, conversation_id, message_id",
        "messages",
        "tenant_id, conversation_id, id",
        " ON DELETE CASCADE",
    ),
    (
        "fk_message_media_tenant_conversation",
        "message_media",
        "tenant_id, conversation_id",
        "conversations",
        "tenant_id, id",
        " ON DELETE CASCADE",
    ),
    (
        "fk_whatsapp_events_tenant_connection",
        "whatsapp_events",
        "tenant_id, account_id, channel",
        "channel_connections",
        "tenant_id, id, channel",
        " ON DELETE CASCADE",
    ),
    (
        "fk_campaign_recipients_tenant_participant",
        "campaign_recipients",
        "tenant_id, contact_id, participant_identity_id",
        "contact_identities",
        "tenant_id, contact_id, id",
        " ON DELETE CASCADE",
    ),
)

# NOT NULL through a validated CHECK: SET NOT NULL then trusts the CHECK rather
# than scanning the table again under an exclusive lock.
NOT_NULL = (
    ("conversations", "participant_identity_id"),
    ("messages", "connection_id"),
)

# 0082 added these NOT VALID; validated here with everything else.
MEDIA_CHECKS = (
    "ck_message_media_position_non_negative",
    "ck_message_media_locator_shape",
    "ck_message_media_locator_bounded",
)

# What the new keys replace, and how to put each back.
SUPERSEDED_KEYS = (
    (
        "fk_conversations_tenant_account",
        "conversations",
        "FOREIGN KEY (tenant_id, account_id) REFERENCES whatsapp_accounts (tenant_id, id)"
        " ON DELETE CASCADE",
    ),
    (
        "fk_message_media_message_id_messages",
        "message_media",
        "FOREIGN KEY (message_id) REFERENCES messages (id) ON DELETE CASCADE",
    ),
    (
        "fk_message_media_conversation_id_conversations",
        "message_media",
        "FOREIGN KEY (conversation_id) REFERENCES conversations (id) ON DELETE CASCADE",
    ),
    (
        "fk_whatsapp_events_account_id_whatsapp_accounts",
        "whatsapp_events",
        "FOREIGN KEY (account_id) REFERENCES whatsapp_accounts (id) ON DELETE CASCADE",
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
            f"{preamble} (docs/RUNBOOK.md, 'Omnichannel foundation (0082-0084)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )


def _drop_if_invalid(name: str) -> None:
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM pg_index x JOIN pg_class c ON c.oid = x.indexrelid"
                " WHERE c.relname = :name AND NOT x.indisvalid"
            ),
            {"name": name},
        )
        .scalar_one()
    )
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def _is_constraint(name: str) -> bool:
    found: int = (
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM pg_constraint WHERE conname = :name"), {"name": name}
        )
        .scalar_one()
    )
    return found > 0


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    _refuse(PRECHECKS, "Rows exist that the neutral keys would refuse")

    with op.get_context().autocommit_block():
        for name, table, columns, unique in INDEXES:
            _drop_if_invalid(name)
            kind = "UNIQUE INDEX" if unique else "INDEX"
            op.execute(f"CREATE {kind} CONCURRENTLY IF NOT EXISTS {name} ON {table} ({columns})")
        for name, statement in PARTIAL_INDEXES:
            _drop_if_invalid(name)
            op.execute(statement)

    # One short transaction: attach the unique indexes, add every key and
    # CHECK without scanning, commit.
    for name, table, _, unique in INDEXES:
        if unique and not _is_constraint(name):
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE USING INDEX {name}")
    # Each skipped if a retry finds it already there.
    for name, child, columns, parent, parent_columns, ondelete in KEYS:
        if not _is_constraint(name):
            op.execute(
                f"ALTER TABLE {child} ADD CONSTRAINT {name} FOREIGN KEY ({columns})"
                f" REFERENCES {parent} ({parent_columns}){ondelete} NOT VALID"
            )
    for table, column in NOT_NULL:
        if not _is_constraint(f"ck_{table}_{column}_present"):
            op.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_{column}_present"
                f" CHECK ({column} IS NOT NULL) NOT VALID"
            )

    # Each validation in a transaction of its own: SHARE UPDATE EXCLUSIVE,
    # which reads and writes pass straight through.
    with op.get_context().autocommit_block():
        for name, child, *_ in KEYS:
            op.execute(f"ALTER TABLE {child} VALIDATE CONSTRAINT {name}")
        for table, column in NOT_NULL:
            op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT ck_{table}_{column}_present")
        for name in MEDIA_CHECKS:
            op.execute(f"ALTER TABLE message_media VALIDATE CONSTRAINT {name}")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    for table, column in NOT_NULL:
        op.alter_column(table, column, nullable=False)
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_{column}_present")
    for name, table, _ in SUPERSEDED_KEYS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    # Superseded by `(message_id, position)`: a message may carry several files.
    op.execute("ALTER TABLE message_media DROP CONSTRAINT IF EXISTS uq_message_media_message_id")
    op.execute("RESET lock_timeout")


def _drop_invalid_in_transaction(name: str) -> None:
    """`_drop_if_invalid`, for the downgrade's one transaction: no CONCURRENTLY."""
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM pg_index x JOIN pg_class c ON c.oid = x.indexrelid"
                " WHERE c.relname = :name AND NOT x.indisvalid"
            ),
            {"name": name},
        )
        .scalar_one()
    )
    if invalid:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def downgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    _refuse(
        DOWNGRADE_PRECHECKS,
        "Rows exist that the keys before 0084 cannot hold; downgrading would discard them",
    )

    # One transaction from here to the end of the run (see the module note).
    _drop_invalid_in_transaction("uq_message_media_message_id")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_message_media_message_id"
        " ON message_media (message_id)"
    )
    if not _is_constraint("uq_message_media_message_id"):
        op.execute(
            "ALTER TABLE message_media ADD CONSTRAINT uq_message_media_message_id"
            " UNIQUE USING INDEX uq_message_media_message_id"
        )
    for name, table, definition in SUPERSEDED_KEYS:
        if not _is_constraint(name):
            # Validated as it is added: inside one transaction there is no
            # later moment in which NOT VALID would buy anything.
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} {definition}")

    for table, column in NOT_NULL:
        op.alter_column(table, column, nullable=True)
    for name, child, *_ in reversed(KEYS):
        op.execute(f"ALTER TABLE {child} DROP CONSTRAINT IF EXISTS {name}")
    for name, table, _, unique in reversed(INDEXES):
        if unique:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    for name, *_ in reversed(INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for name, _ in PARTIAL_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute("RESET lock_timeout")

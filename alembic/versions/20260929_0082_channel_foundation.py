"""Neutral channel primitives: connections, contact identities, and the columns that name them.

Revision ID: 0082
Revises: 0081

The omnichannel foundation's schema (OMNI-001, OMNI-002, OMNI-003, OMNI-004,
OMNI-005, OMNI-009; ADR-117, ADR-118). **Additive.** Nothing existing is dropped,
re-keyed or rewritten here, and WhatsApp traffic keeps flowing through the same
rows while it runs:

1. **`channel_connections`**, one row per connection, with *the same id as its
   WhatsApp number*, so every `conversations.account_id`,
   `whatsapp_events.account_id` and `campaigns.account_id` is already a valid
   connection id. Backfilled from `whatsapp_accounts`, and kept equal to it by
   the trigger `trg_whatsapp_accounts_channel_connection` for every writer.
2. **`contact_identities`**, backfilled with exactly one WhatsApp phone identity
   for each contact - its own `wa_id`, and nothing else. Nothing is matched,
   merged or inferred: not by name, email, lead phone or display name. The
   trigger `trg_contacts_phone_identity` keeps `wa_id` and the phone identity
   equal for every writer.
3. **`contacts.wa_id` becomes nullable**, because a WhatsApp user with a
   username can write without Meta telling the business their number
   (OMNI-002). Such a contact holds a business-scoped identity and no phone.
4. **`conversations.channel`** (every existing row is WhatsApp; the default
   says so without rewriting the table) and **`participant_identity_id`**,
   nullable here, backfilled by 0083 and enforced by 0084. The trigger
   `trg_conversations_default_participant` pins a new conversation whose
   writer names no participant to its contact's only identity on the channel.
5. **`messages.connection_id`**, nullable here, backfilled by 0083 and enforced
   by 0084. The message sequence trigger now assigns it from the conversation
   in the same statement that assigns the position.
6. **`message_media.position`** (every existing row is the first), and a
   provider-neutral locator (`locator_kind`, `locator`, `locator_expires_at`).
7. **`whatsapp_events.channel`** and the event kind `echo`.
8. **`campaign_recipients.participant_identity_id`**, the identity a copy is
   addressed to.

**Online.** The two backfills read before any lock on a table traffic writes to.
Each trigger is created after its backfill and followed by a catch-up pass, so a
row written in between is covered either way; from then on the trigger covers
it. The column additions are metadata-only (a constant default is not a table
rewrite) and come last, so the brief exclusive locks they need are held for
milliseconds before the commit. New CHECKs are added `NOT VALID` and validated
by 0084 in transactions of their own. `lock_timeout` is bounded so an ALTER
queued behind a long transaction fails and is retried rather than queueing every
request behind it.

**Nothing ambiguous is guessed.** A number claimed live twice, a number or a
contact with an empty identifier, a conversation whose number belongs to another
workspace: each is counted first, and the migration refuses with nothing changed
(docs/RUNBOOK.md, "Omnichannel foundation (0082-0084)").

**Downgrade refuses** while any row holds state the pre-0082 schema cannot
represent - a contact without a phone, an identity that is not a contact's own
phone, a connection that is not a WhatsApp number, a second attachment on a
message, a URL locator, an echo event. Dropping those would discard data.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None

# How long an ALTER may wait for a lock before the migration gives up and is
# retried. Long enough for an ordinary request to finish, short enough that a
# statement queued behind a long transaction does not become the queue.
LOCK_TIMEOUT = "15s"

ENUMS: dict[str, tuple[str, ...]] = {
    "channel_kind": ("whatsapp", "instagram", "messenger"),
    "connection_status": ("active", "disabled", "released"),
    "connection_health": (
        "ok",
        "auth_failed",
        "permission_missing",
        "rate_limited",
        "webhook_unhealthy",
    ),
    "identity_kind": ("phone", "bsuid", "psid", "igsid"),
    "identity_scope": ("workspace", "provider_account", "connection"),
    "identity_source": ("backfill", "provider", "provider_pairing"),
    "media_locator_kind": ("handle", "url"),
}

PRECHECKS = (
    (
        "WhatsApp numbers claimed live by more than one row",
        "SELECT count(*) FROM (SELECT phone_number_id FROM whatsapp_accounts"
        " WHERE released_at IS NULL GROUP BY phone_number_id HAVING count(*) > 1) d",
    ),
    (
        "WhatsApp numbers with an empty phone number id",
        "SELECT count(*) FROM whatsapp_accounts WHERE phone_number_id = ''",
    ),
    (
        "contacts with an empty WhatsApp id",
        "SELECT count(*) FROM contacts WHERE wa_id = ''",
    ),
    (
        "conversations whose number is another workspace's or missing",
        "SELECT count(*) FROM conversations c WHERE NOT EXISTS ("
        " SELECT 1 FROM whatsapp_accounts a WHERE a.id = c.account_id"
        " AND a.tenant_id = c.tenant_id)",
    ),
    (
        "conversations whose contact is another workspace's or missing",
        "SELECT count(*) FROM conversations c WHERE NOT EXISTS ("
        " SELECT 1 FROM contacts k WHERE k.id = c.contact_id AND k.tenant_id = c.tenant_id)",
    ),
)

# Afterwards, and inside the same transaction: every number has its connection
# and every contact its phone identity. A count here is a bug in this file.
POSTCHECKS = (
    (
        "WhatsApp numbers without their connection",
        "SELECT count(*) FROM whatsapp_accounts a WHERE NOT EXISTS ("
        " SELECT 1 FROM channel_connections c WHERE c.id = a.id AND c.tenant_id = a.tenant_id"
        " AND c.channel = 'whatsapp' AND c.external_account_id = a.phone_number_id)",
    ),
    (
        "contacts without their WhatsApp phone identity",
        "SELECT count(*) FROM contacts c WHERE c.wa_id IS NOT NULL AND NOT EXISTS ("
        " SELECT 1 FROM contact_identities i WHERE i.tenant_id = c.tenant_id"
        " AND i.contact_id = c.id AND i.channel = 'whatsapp' AND i.kind = 'phone'"
        " AND i.value = c.wa_id)",
    ),
)

DOWNGRADE_PRECHECKS = (
    (
        "contacts with no WhatsApp phone number",
        "SELECT count(*) FROM contacts WHERE wa_id IS NULL",
    ),
    (
        "identities that are not a contact's own WhatsApp phone number",
        "SELECT count(*) FROM contact_identities i WHERE NOT (i.channel = 'whatsapp'"
        " AND i.kind = 'phone' AND EXISTS (SELECT 1 FROM contacts c WHERE c.id = i.contact_id"
        " AND c.tenant_id = i.tenant_id AND c.wa_id = i.value))",
    ),
    (
        "connections that are not a WhatsApp number",
        "SELECT count(*) FROM channel_connections c WHERE c.channel <> 'whatsapp'"
        " OR NOT EXISTS (SELECT 1 FROM whatsapp_accounts a WHERE a.id = c.id)",
    ),
    (
        "messages carrying more than one file",
        "SELECT count(*) FROM message_media WHERE position > 0",
    ),
    (
        "files located by URL",
        "SELECT count(*) FROM message_media WHERE locator_kind = 'url'",
    ),
    (
        "echo events",
        "SELECT count(*) FROM whatsapp_events WHERE kind::text = 'echo'",
    ),
)

# Frozen copies of `app.db.models.channel`. A change there needs a migration.
WHATSAPP_CONNECTION_MIRROR_FUNCTION = """
CREATE OR REPLACE FUNCTION wasla_mirror_whatsapp_connection() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM channel_connections WHERE id = OLD.id;
        RETURN OLD;
    END IF;
    INSERT INTO channel_connections (
        id, tenant_id, channel, external_account_id, status,
        ownership_started_at, ownership_verified_at, released_at,
        created_at, updated_at
    ) VALUES (
        NEW.id, NEW.tenant_id, 'whatsapp', NEW.phone_number_id, NEW.status::text::connection_status,
        NEW.ownership_started_at, NEW.ownership_verified_at, NEW.released_at,
        NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (id) DO UPDATE SET
        tenant_id = EXCLUDED.tenant_id,
        external_account_id = EXCLUDED.external_account_id,
        status = EXCLUDED.status,
        ownership_started_at = EXCLUDED.ownership_started_at,
        ownership_verified_at = EXCLUDED.ownership_verified_at,
        released_at = EXCLUDED.released_at,
        updated_at = EXCLUDED.updated_at
    WHERE channel_connections.channel = 'whatsapp';
    RETURN NEW;
END;
$$
"""

WHATSAPP_CONNECTION_MIRROR_TRIGGER = """
CREATE TRIGGER trg_whatsapp_accounts_channel_connection
AFTER INSERT OR UPDATE OR DELETE ON whatsapp_accounts
FOR EACH ROW EXECUTE FUNCTION wasla_mirror_whatsapp_connection()
"""

CONTACT_PHONE_IDENTITY_FUNCTION = """
CREATE OR REPLACE FUNCTION wasla_contact_phone_identity() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    owner uuid;
BEGIN
    IF NEW.wa_id IS NULL THEN
        RETURN NEW;
    END IF;
    INSERT INTO contact_identities (
        id, tenant_id, contact_id, channel, kind, scope, scope_ref, connection_id, value, source
    ) VALUES (
        gen_random_uuid(), NEW.tenant_id, NEW.id, 'whatsapp', 'phone', 'workspace', '', NULL,
        NEW.wa_id, 'provider'
    )
    ON CONFLICT ON CONSTRAINT uq_contact_identities_scoped_value DO NOTHING;
    SELECT contact_id INTO owner
      FROM contact_identities
     WHERE tenant_id = NEW.tenant_id AND channel = 'whatsapp' AND kind = 'phone'
       AND scope = 'workspace' AND scope_ref = '' AND value = NEW.wa_id;
    IF owner IS DISTINCT FROM NEW.id THEN
        RAISE EXCEPTION 'uq_contact_identities_scoped_value: phone of % held by %', NEW.id, owner
            USING ERRCODE = 'unique_violation', CONSTRAINT = 'uq_contact_identities_scoped_value';
    END IF;
    RETURN NEW;
END;
$$
"""

CONTACT_PHONE_IDENTITY_TRIGGER = """
CREATE TRIGGER trg_contacts_phone_identity
AFTER INSERT OR UPDATE OF wa_id ON contacts
FOR EACH ROW EXECUTE FUNCTION wasla_contact_phone_identity()
"""

CONVERSATION_PARTICIPANT_FUNCTION = """
CREATE OR REPLACE FUNCTION wasla_default_conversation_participant() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    candidates uuid[];
BEGIN
    IF NEW.participant_identity_id IS NOT NULL THEN
        RETURN NEW;
    END IF;
    SELECT array_agg(id) INTO candidates
      FROM contact_identities
     WHERE tenant_id = NEW.tenant_id AND contact_id = NEW.contact_id
       AND channel = NEW.channel;
    IF array_length(candidates, 1) = 1 THEN
        NEW.participant_identity_id = candidates[1];
    ELSE
        NEW.participant_identity_id = NEW.id;
    END IF;
    RETURN NEW;
END;
$$
"""

CONVERSATION_PARTICIPANT_TRIGGER = """
CREATE TRIGGER trg_conversations_default_participant
BEFORE INSERT ON conversations
FOR EACH ROW EXECUTE FUNCTION wasla_default_conversation_participant()
"""

# The message sequence, now also assigning the conversation's connection. The
# frozen copy of `MESSAGE_SEQUENCE_FUNCTION` at 0082.
MESSAGE_SEQUENCE_FUNCTION = """
CREATE OR REPLACE FUNCTION wasla_assign_message_sequence() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    conversation_connection uuid;
BEGIN
    UPDATE conversations
       SET last_message_sequence = last_message_sequence + 1
     WHERE id = NEW.conversation_id
       AND tenant_id = NEW.tenant_id
    RETURNING last_message_sequence, account_id INTO NEW.sequence, conversation_connection;
    NEW.sequence = COALESCE(NEW.sequence, 0);
    NEW.connection_id = COALESCE(NEW.connection_id, conversation_connection, NEW.conversation_id);
    RETURN NEW;
END;
$$
"""

# What the downgrade puts back: the 0080 function, byte for byte.
PREVIOUS_MESSAGE_SEQUENCE_FUNCTION = """
CREATE OR REPLACE FUNCTION wasla_assign_message_sequence() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
BEGIN
    UPDATE conversations
       SET last_message_sequence = last_message_sequence + 1
     WHERE id = NEW.conversation_id
       AND tenant_id = NEW.tenant_id
    RETURNING last_message_sequence INTO NEW.sequence;
    NEW.sequence = COALESCE(NEW.sequence, 0);
    RETURN NEW;
END;
$$
"""

CONNECTION_BACKFILL = """
INSERT INTO channel_connections (
    id, tenant_id, channel, external_account_id, status,
    ownership_started_at, ownership_verified_at, released_at, created_at, updated_at
)
SELECT id, tenant_id, 'whatsapp', phone_number_id, status::text::connection_status,
       ownership_started_at, ownership_verified_at, released_at, created_at, updated_at
  FROM whatsapp_accounts
ON CONFLICT (id) DO NOTHING
"""

# One phone identity per contact, from its own `wa_id`, in its own workspace.
# Deterministic: nothing about the contact but its own row is consulted.
IDENTITY_BACKFILL = """
INSERT INTO contact_identities (
    id, tenant_id, contact_id, channel, kind, scope, scope_ref, connection_id, value, source,
    created_at, updated_at
)
SELECT gen_random_uuid(), c.tenant_id, c.id, 'whatsapp', 'phone', 'workspace', '', NULL,
       c.wa_id, 'backfill', now(), now()
  FROM contacts c
 WHERE c.wa_id IS NOT NULL
   AND NOT EXISTS (
       SELECT 1 FROM contact_identities i
        WHERE i.tenant_id = c.tenant_id AND i.contact_id = c.id
          AND i.channel = 'whatsapp' AND i.kind = 'phone'
   )
ON CONFLICT ON CONSTRAINT uq_contact_identities_scoped_value DO NOTHING
"""

# Validated by 0084, in transactions of their own.
MEDIA_CHECKS = (
    ("ck_message_media_position_non_negative", "position >= 0"),
    ("ck_message_media_locator_shape", "(locator_kind IS NULL) = (locator IS NULL)"),
    (
        "ck_message_media_locator_bounded",
        "locator IS NULL OR char_length(locator) <= 4096",
    ),
)


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


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


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    _refuse(
        PRECHECKS,
        "Rows exist that the neutral connection and identity model cannot map without guessing",
    )

    # First, and only here: `ADD VALUE` needs a block of its own, and an
    # autocommit block commits everything before it. At the start, that is only
    # the previous revision's already-finished work; at the end it would commit
    # this revision's DDL ahead of its `alembic_version` row, so a refusal later
    # in the same run would leave tables that the version says do not exist.
    # Appended, like every label since 0037: a PostgreSQL enum cannot drop one,
    # so the downgrade leaves it and a re-upgrade finds it (`IF NOT EXISTS`).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE whatsapp_event_kind ADD VALUE IF NOT EXISTS 'echo'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")

    bind = op.get_bind()
    for name in ENUMS:
        _enum(name).create(bind, checkfirst=True)

    # ------------------------------------------------------------ connections
    op.create_table(
        "channel_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tenants.id", ondelete="CASCADE", name="fk_channel_connections_tenant_id_tenants"
            ),
            nullable=False,
        ),
        sa.Column("channel", _enum("channel_kind"), nullable=False),
        sa.Column("external_account_id", sa.String(128), nullable=False),
        sa.Column("status", _enum("connection_status"), nullable=False),
        sa.Column("ownership_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ownership_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health", _enum("connection_health"), nullable=False, server_default="ok"),
        sa.Column("health_reason", sa.String(64), nullable=True),
        sa.Column("health_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credential_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("send_window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("send_window_count", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_channel_connections"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_channel_connections_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "id", "channel", name="uq_channel_connections_tenant_id_id_channel"
        ),
        sa.CheckConstraint(
            "external_account_id <> ''",
            name=op.f("ck_channel_connections_external_account_id_present"),
        ),
        sa.CheckConstraint(
            "send_window_count >= 0",
            name=op.f("ck_channel_connections_send_window_count_non_negative"),
        ),
    )
    op.create_index("ix_channel_connections_tenant_id", "channel_connections", ["tenant_id"])
    op.create_index(
        "uq_channel_connections_live_external_account",
        "channel_connections",
        ["channel", "external_account_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "ix_channel_connections_external_account_tenure",
        "channel_connections",
        ["channel", "external_account_id", "ownership_started_at"],
    )
    # Read only: nothing here locks `whatsapp_accounts` against writers.
    op.execute(CONNECTION_BACKFILL)

    # ------------------------------------------------------------- identities
    op.create_table(
        "contact_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tenants.id", ondelete="CASCADE", name="fk_contact_identities_tenant_id_tenants"
            ),
            nullable=False,
        ),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", _enum("channel_kind"), nullable=False),
        sa.Column("kind", _enum("identity_kind"), nullable=False),
        sa.Column("scope", _enum("identity_scope"), nullable=False),
        sa.Column("scope_ref", sa.String(128), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("source", _enum("identity_source"), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_contact_identities"),
        sa.UniqueConstraint(
            "tenant_id",
            "channel",
            "kind",
            "scope",
            "scope_ref",
            "value",
            name="uq_contact_identities_scoped_value",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_contact_identities_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "contact_id", "id", name="uq_contact_identities_tenant_id_contact_id_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "contact_id",
            "id",
            "channel",
            name="uq_contact_identities_tenant_id_contact_id_id_channel",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_contact_identities_tenant_contact",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["channel_connections.tenant_id", "channel_connections.id"],
            name="fk_contact_identities_tenant_connection",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("value <> ''", name=op.f("ck_contact_identities_value_present")),
        sa.CheckConstraint(
            "(scope = 'workspace' AND scope_ref = '' AND connection_id IS NULL)"
            " OR (scope = 'provider_account' AND scope_ref <> '' AND connection_id IS NULL)"
            " OR (scope = 'connection' AND connection_id IS NOT NULL"
            " AND scope_ref = connection_id::text)",
            name=op.f("ck_contact_identities_scope_shape"),
        ),
    )
    op.create_index(
        "ix_contact_identities_tenant_id_contact_id",
        "contact_identities",
        ["tenant_id", "contact_id"],
    )
    op.create_index("ix_contact_identities_connection_id", "contact_identities", ["connection_id"])
    op.create_index(
        "uq_contact_identities_one_whatsapp_phone",
        "contact_identities",
        ["tenant_id", "contact_id"],
        unique=True,
        postgresql_where=sa.text("channel = 'whatsapp' AND kind = 'phone'"),
    )
    # Read only on `contacts`.
    op.execute(IDENTITY_BACKFILL)

    # --------------------------------------------------------------- triggers
    # Each takes its table's trigger lock, which waits for writers in flight
    # and holds new ones until the commit. The catch-up pass then covers any
    # row written between the backfill above and the lock.
    op.execute(WHATSAPP_CONNECTION_MIRROR_FUNCTION)
    op.execute(WHATSAPP_CONNECTION_MIRROR_TRIGGER)
    op.execute(CONNECTION_BACKFILL)
    op.execute(CONTACT_PHONE_IDENTITY_FUNCTION)
    op.execute(CONTACT_PHONE_IDENTITY_TRIGGER)
    op.execute(IDENTITY_BACKFILL)
    _refuse(POSTCHECKS, "The connection or identity backfill is incomplete")

    # ---------------------------------------------------- metadata-only ALTERs
    op.alter_column("contacts", "wa_id", nullable=True)

    op.add_column(
        "conversations",
        sa.Column("channel", _enum("channel_kind"), nullable=False, server_default="whatsapp"),
    )
    op.add_column(
        "conversations",
        sa.Column("participant_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(CONVERSATION_PARTICIPANT_FUNCTION)
    op.execute(CONVERSATION_PARTICIPANT_TRIGGER)

    op.add_column(
        "messages",
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(MESSAGE_SEQUENCE_FUNCTION)

    op.add_column(
        "message_media",
        # A bare literal, as the model declares it: `'0'` would be stored as
        # `'0'::smallint` and the two schemas would disagree on the text.
        sa.Column("position", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "message_media",
        sa.Column("locator_kind", _enum("media_locator_kind"), nullable=True),
    )
    op.add_column("message_media", sa.Column("locator", sa.Text(), nullable=True))
    op.add_column(
        "message_media",
        sa.Column("locator_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    for name, condition in MEDIA_CHECKS:
        op.execute(f"ALTER TABLE message_media ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID")

    op.add_column(
        "whatsapp_events",
        sa.Column("channel", _enum("channel_kind"), nullable=False, server_default="whatsapp"),
    )
    op.add_column(
        "campaign_recipients",
        sa.Column("participant_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    _refuse(
        DOWNGRADE_PRECHECKS,
        "Rows hold state a pre-0082 schema cannot represent; downgrading would discard it",
    )

    op.drop_column("campaign_recipients", "participant_identity_id")
    op.drop_column("whatsapp_events", "channel")
    for name, _ in reversed(MEDIA_CHECKS):
        op.execute(f"ALTER TABLE message_media DROP CONSTRAINT IF EXISTS {name}")
    op.drop_column("message_media", "locator_expires_at")
    op.drop_column("message_media", "locator")
    op.drop_column("message_media", "locator_kind")
    op.drop_column("message_media", "position")

    op.execute(PREVIOUS_MESSAGE_SEQUENCE_FUNCTION)
    op.drop_column("messages", "connection_id")

    op.execute("DROP TRIGGER IF EXISTS trg_conversations_default_participant ON conversations")
    op.execute("DROP FUNCTION IF EXISTS wasla_default_conversation_participant()")
    op.drop_column("conversations", "participant_identity_id")
    op.drop_column("conversations", "channel")

    op.execute("DROP TRIGGER IF EXISTS trg_contacts_phone_identity ON contacts")
    op.execute("DROP FUNCTION IF EXISTS wasla_contact_phone_identity()")
    op.alter_column("contacts", "wa_id", nullable=False)

    op.execute(
        "DROP TRIGGER IF EXISTS trg_whatsapp_accounts_channel_connection ON whatsapp_accounts"
    )
    op.execute("DROP FUNCTION IF EXISTS wasla_mirror_whatsapp_connection()")

    op.drop_table("contact_identities")
    op.drop_table("channel_connections")

    bind = op.get_bind()
    for name in reversed(tuple(ENUMS)):
        _enum(name).drop(bind, checkfirst=True)
    op.execute("RESET lock_timeout")

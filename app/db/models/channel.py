"""Channels, the connections a workspace holds on them, and who writes from the other end.

Two primitives every customer messaging channel needs, stated without the
vocabulary of any one provider (OMNI-001, OMNI-003, ADR-117):

- **`ChannelConnection`** is one thing a workspace has connected - a WhatsApp
  number today, an Instagram account or a Facebook Page when those exist. It
  owns what is true of a connection on every channel: which workspace holds it,
  since when, whether it is paused or given up, and whether its credential still
  works. What only one provider has - a WhatsApp Business Account, a phone number
  id, an ownership proof against Meta - stays on that provider's own table
  (`whatsapp_accounts`), which shares the connection's id.
- **`ContactIdentity`** is one address a provider uses for a person: a WhatsApp
  phone number, a WhatsApp business-scoped user id, later a Messenger PSID or an
  Instagram IGSID. The contact is the person the CRM knows; an identity is how a
  provider names them, and a person may have several. Uniqueness is always
  within a workspace *and* within the scope the provider guarantees the value
  in - never on the value alone (OMNI-001).

**The shared id is deliberate.** Every existing `whatsapp_accounts` row has a
connection row with the same id, so `conversations.account_id`,
`whatsapp_events.account_id` and `campaigns.account_id` were already valid
connection ids the moment the table existed, and no conversation was rewritten
to re-point its key (ADR-117).

**During the compatibility window the WhatsApp row is where lifecycle is
written**, and a trigger on `whatsapp_accounts` mirrors it here in the same
statement (`WHATSAPP_CONNECTION_MIRROR`). Every writer of a number - the claim
flow, release, a fixture, an operator's SQL - therefore keeps its connection row
current without having to remember to, which is the same reason the message
sequence lives in a trigger (AI-01). The columns only this table has - health,
credential expiry, the send window - are never touched by the mirror.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    Connection,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

# A provider's name for a connection: a WhatsApp phone number id, a Page id, an
# Instagram account id. Meta's are numeric strings well inside this; the bound
# is a deliberate ceiling rather than a guess at a format.
MAX_EXTERNAL_ACCOUNT_ID_LENGTH: Final = 128

# A provider's name for a person. A WhatsApp business-scoped user id is a
# two-letter country, a dot and up to 128 characters (131 in all), and a parent
# id adds `ENT.`; a PSID or IGSID is shorter. 255 holds every documented form
# with room, and anything longer is refused at the adapter as not being an
# identifier any provider issued - counted, never silently dropped.
MAX_IDENTITY_VALUE_LENGTH: Final = 255

# What a scope reference may hold: a WhatsApp Business Account id, or a
# connection id as text.
MAX_SCOPE_REF_LENGTH: Final = 128

# Why a connection is unhealthy, in a word an operator can act on. Bounded, and
# never a provider's own error text.
MAX_HEALTH_REASON_LENGTH: Final = 64


class Channel(StrEnum):
    """The customer messaging channels Wasla's model can represent.

    A label here is vocabulary, not support. WhatsApp is the only channel with
    an adapter, a webhook route, a connect flow, a policy and a credential
    resolver; a connection on any other channel has none of those, and every
    path that would act on one refuses rather than guessing (ADR-117). Adding
    Instagram or Messenger is adapter work - the model does not change.
    """

    WHATSAPP = "whatsapp"
    INSTAGRAM = "instagram"
    MESSENGER = "messenger"


class ConnectionStatus(StrEnum):
    """Whether a connection carries traffic. The same three states as a number.

    `DISABLED` is a pause and `RELEASED` gives the claim up, for the reasons
    `WhatsAppAccountStatus` records.
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    RELEASED = "released"


class ConnectionHealth(StrEnum):
    """Whether a connection can actually be used, separate from whether it may.

    `status` is a workspace's decision; this is a fact the provider told us.
    Only `AUTH_FAILED` is written today - a send or a file fetch the provider
    refused because of the credential - and it clears on the next success. The
    others are vocabulary for signals no current channel produces, so a later
    adapter records them in a column that already exists rather than inventing
    one (OMNI-012).
    """

    OK = "ok"
    AUTH_FAILED = "auth_failed"
    PERMISSION_MISSING = "permission_missing"
    RATE_LIMITED = "rate_limited"
    WEBHOOK_UNHEALTHY = "webhook_unhealthy"


class IdentityKind(StrEnum):
    """What kind of address an identity value is."""

    #: A WhatsApp phone number - `wa_id`, `from`.
    PHONE = "phone"
    #: A WhatsApp business-scoped user id - `user_id`, `from_user_id`.
    BSUID = "bsuid"
    #: A Messenger page-scoped id. Vocabulary only; no adapter writes one.
    PSID = "psid"
    #: An Instagram-scoped id. Vocabulary only; no adapter writes one.
    IGSID = "igsid"


class IdentityScope(StrEnum):
    """Within what a provider guarantees an identity value names one person.

    `scope_ref` names the particular scope: nothing for the workspace, the
    provider account for `PROVIDER_ACCOUNT`, the connection id for `CONNECTION`.
    """

    #: The whole workspace. A phone number names one person wherever it
    #: arrives, which is how `contacts.wa_id` has always behaved.
    WORKSPACE = "workspace"
    #: One provider account. A WhatsApp business-scoped user id is unique per
    #: business portfolio and user; the WhatsApp Business Account the message
    #: arrived through belongs to exactly one portfolio, so it is a safe
    #: stand-in - one that can only ever over-split a person, never merge two.
    PROVIDER_ACCOUNT = "provider_account"
    #: One connection. A Messenger PSID is page-scoped, an IGSID account-scoped.
    CONNECTION = "connection"


class IdentitySource(StrEnum):
    """How an identity came to belong to its contact.

    There is no "matched" source, and that absence is the rule: an identity is
    attached to a contact only because it was the contact's own column, because
    it was the sender of a signed provider event, or because the provider named
    it in the same signed payload as an identity the contact already had.
    Display names, emails and phone numbers typed into a lead never link
    anything (ADR-048, ADR-049, ADR-118).
    """

    #: `contacts.wa_id` as it stood when this table was created (0082).
    BACKFILL = "backfill"
    #: The sender of a signed inbound event.
    PROVIDER = "provider"
    #: Named by the provider together with an identity this contact already
    #: held, in one signed payload - a WhatsApp phone and business-scoped id.
    PROVIDER_PAIRING = "provider_pairing"


CHANNEL_TYPE = _enum_type(Channel, name="channel_kind")
CONNECTION_STATUS_TYPE = _enum_type(ConnectionStatus, name="connection_status")
CONNECTION_HEALTH_TYPE = _enum_type(ConnectionHealth, name="connection_health")
IDENTITY_KIND_TYPE = _enum_type(IdentityKind, name="identity_kind")
IDENTITY_SCOPE_TYPE = _enum_type(IdentityScope, name="identity_scope")
IDENTITY_SOURCE_TYPE = _enum_type(IdentitySource, name="identity_source")


class ChannelConnection(Base, TenantScopedMixin, TimestampMixin):
    """One connection a workspace holds on one channel.

    Routing asks this table, never a customer's identifier: an inbound event
    names the connection it arrived on (`external_account_id` - the phone number
    id for WhatsApp), the tenure interval `[ownership_started_at, released_at)`
    says who held it at the event's instant (ADR-101), and only then is the
    sender looked up, inside that workspace.
    """

    __tablename__ = "channel_connections"
    __table_args__ = (
        Index("ix_channel_connections_tenant_id", "tenant_id"),
        # One live claim per provider connection, platform-wide: the ADR-037
        # rule, stated for every channel. Released rows leave it, which is what
        # lets a number or a page move without deleting anybody's history.
        Index(
            "uq_channel_connections_live_external_account",
            "channel",
            "external_account_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        # Who held a connection when an event happened (ADR-101). Released rows
        # are what this exists to find, so the live-only index cannot serve it.
        Index(
            "ix_channel_connections_external_account_tenure",
            "channel",
            "external_account_id",
            "ownership_started_at",
        ),
        # Targets of the tenant-agreed keys conversations and events hold
        # (ADR-100). The second carries the channel, so a conversation's
        # channel and its connection's channel are the same fact in the
        # database rather than two facts somebody has to keep equal.
        UniqueConstraint("tenant_id", "id", name="uq_channel_connections_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "id", "channel", name="uq_channel_connections_tenant_id_id_channel"
        ),
        CheckConstraint("external_account_id <> ''", name="external_account_id_present"),
        CheckConstraint("send_window_count >= 0", name="send_window_count_non_negative"),
    )

    # No generated default. A WhatsApp connection takes its number's id, which
    # the mirror copies; a connection of any other kind is created by its own
    # adapter's connect flow, which chooses the id deliberately.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    channel: Mapped[Channel] = mapped_column(CHANNEL_TYPE, nullable=False)
    external_account_id: Mapped[str] = mapped_column(
        String(MAX_EXTERNAL_ACCOUNT_ID_LENGTH), nullable=False
    )
    status: Mapped[ConnectionStatus] = mapped_column(CONNECTION_STATUS_TYPE, nullable=False)
    ownership_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ownership_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # What the provider last said about using this connection (OMNI-012).
    health: Mapped[ConnectionHealth] = mapped_column(
        CONNECTION_HEALTH_TYPE,
        nullable=False,
        default=ConnectionHealth.OK,
        server_default=ConnectionHealth.OK.value,
    )
    health_reason: Mapped[str | None] = mapped_column(
        String(MAX_HEALTH_REASON_LENGTH), nullable=True
    )
    health_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the credential this connection sends with stops working, where the
    # provider says. Null for a credential without one - WhatsApp's
    # system-user tokens, the platform fallback.
    credential_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The per-connection send allowance (OMNI-017). A window and how much of it
    # has been spent, shared by every bulk sender on this connection; see
    # `ConnectionThroughput`. Unused while no allowance is configured.
    send_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    send_window_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    @property
    def is_active(self) -> bool:
        return self.status is ConnectionStatus.ACTIVE and self.released_at is None

    @property
    def is_released(self) -> bool:
        return self.released_at is not None

    def held_at(self, instant: datetime) -> bool:
        """Whether this workspace held the connection at `instant` - half-open, as ADR-101."""
        if instant < self.ownership_started_at:
            return False
        return self.released_at is None or instant < self.released_at


class ContactIdentity(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One address a provider uses for one of a workspace's contacts.

    Unique on `(tenant, channel, kind, scope, scope_ref, value)`, which is the
    whole of the identity rule: the same value in two workspaces is two people,
    and so is the same value on two channels or in two provider scopes. A value
    is never unique on its own and never unique per workspace alone (OMNI-001).

    A WhatsApp contact holds at most one phone identity, and it is always the
    contact's `wa_id` - a trigger on `contacts` keeps the two equal for every
    writer during the compatibility window (`CONTACT_PHONE_IDENTITY`).
    """

    __tablename__ = "contact_identities"
    __table_args__ = (
        Index("ix_contact_identities_tenant_id_contact_id", "tenant_id", "contact_id"),
        Index("ix_contact_identities_connection_id", "connection_id"),
        UniqueConstraint(
            "tenant_id",
            "channel",
            "kind",
            "scope",
            "scope_ref",
            "value",
            name="uq_contact_identities_scoped_value",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_contact_identities_tenant_id_id"),
        # The target of a campaign recipient's pinned identity: one of that
        # recipient's own contact's identities, in its workspace.
        UniqueConstraint(
            "tenant_id", "contact_id", "id", name="uq_contact_identities_tenant_id_contact_id_id"
        ),
        # The target of the conversation's participant key: the identity a
        # conversation addresses belongs to that conversation's contact, in its
        # workspace, on its channel - three agreements the database checks.
        UniqueConstraint(
            "tenant_id",
            "contact_id",
            "id",
            "channel",
            name="uq_contact_identities_tenant_id_contact_id_id_channel",
        ),
        # At most one WhatsApp phone per contact, so `contacts.wa_id` and the
        # phone identity can never disagree about which number a person has.
        Index(
            "uq_contact_identities_one_whatsapp_phone",
            "tenant_id",
            "contact_id",
            unique=True,
            postgresql_where=text("channel = 'whatsapp' AND kind = 'phone'"),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_contact_identities_tenant_contact",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["channel_connections.tenant_id", "channel_connections.id"],
            name="fk_contact_identities_tenant_connection",
            ondelete="CASCADE",
        ),
        CheckConstraint("value <> ''", name="value_present"),
        # The scope and its reference agree, so a lookup can never be asked a
        # question the row cannot answer: a workspace identity names no scope,
        # a provider-account identity names one, and a connection identity
        # names its connection - as a key, and as the reference.
        CheckConstraint(
            "(scope = 'workspace' AND scope_ref = '' AND connection_id IS NULL)"
            " OR (scope = 'provider_account' AND scope_ref <> '' AND connection_id IS NULL)"
            " OR (scope = 'connection' AND connection_id IS NOT NULL"
            " AND scope_ref = connection_id::text)",
            name="scope_shape",
        ),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[Channel] = mapped_column(CHANNEL_TYPE, nullable=False)
    kind: Mapped[IdentityKind] = mapped_column(IDENTITY_KIND_TYPE, nullable=False)
    scope: Mapped[IdentityScope] = mapped_column(IDENTITY_SCOPE_TYPE, nullable=False)
    scope_ref: Mapped[str] = mapped_column(String(MAX_SCOPE_REF_LENGTH), nullable=False)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    value: Mapped[str] = mapped_column(String(MAX_IDENTITY_VALUE_LENGTH), nullable=False)
    source: Mapped[IdentitySource] = mapped_column(IDENTITY_SOURCE_TYPE, nullable=False)


# ---------------------------------------------------------------- triggers
#
# Each function below exists in a migration as a frozen copy (0082); a change
# here needs a migration of its own, exactly as the message sequence does.

#: Keeps a WhatsApp number's connection row equal to the number, for every
#: writer. Insert and update upsert the lifecycle columns under the number's
#: own id; delete removes the connection, whose own keys then cascade what
#: named it. The columns only a connection has are never written here.
WHATSAPP_CONNECTION_MIRROR_FUNCTION: Final = """
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

WHATSAPP_CONNECTION_MIRROR_TRIGGER: Final = """
CREATE TRIGGER trg_whatsapp_accounts_channel_connection
AFTER INSERT OR UPDATE OR DELETE ON whatsapp_accounts
FOR EACH ROW EXECUTE FUNCTION wasla_mirror_whatsapp_connection()
"""

#: Keeps a contact's WhatsApp phone identity equal to its `wa_id`, for every
#: writer. A phone already belonging to a different contact is refused rather
#: than skipped: two contacts claiming one number is the merge this model
#: exists to make impossible, and silence would hide it.
CONTACT_PHONE_IDENTITY_FUNCTION: Final = """
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

CONTACT_PHONE_IDENTITY_TRIGGER: Final = """
CREATE TRIGGER trg_contacts_phone_identity
AFTER INSERT OR UPDATE OF wa_id ON contacts
FOR EACH ROW EXECUTE FUNCTION wasla_contact_phone_identity()
"""

#: A conversation whose writer named no participant is pinned to its contact's
#: only identity on the conversation's channel - and only when there is exactly
#: one. With none, or with two, there is no deterministic answer and none is
#: guessed: the row gets its own id as a participant, which names no identity,
#: so the participant key refuses it under its own name. The same device the
#: message sequence uses (position 0, never persisted), for the same reason - a
#: refusal should say which rule was broken, not describe a symptom of it.
CONVERSATION_PARTICIPANT_FUNCTION: Final = """
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

CONVERSATION_PARTICIPANT_TRIGGER: Final = """
CREATE TRIGGER trg_conversations_default_participant
BEFORE INSERT ON conversations
FOR EACH ROW EXECUTE FUNCTION wasla_default_conversation_participant()
"""


def install_whatsapp_connection_mirror(
    _target: object, connection: Connection, **_kwargs: Any
) -> None:
    """Create the mirror beside `whatsapp_accounts` in a model-built schema."""
    connection.exec_driver_sql(WHATSAPP_CONNECTION_MIRROR_FUNCTION)
    connection.exec_driver_sql(WHATSAPP_CONNECTION_MIRROR_TRIGGER)


def install_contact_phone_identity(_target: object, connection: Connection, **_kwargs: Any) -> None:
    """Create the phone-identity trigger beside `contacts` in a model-built schema."""
    connection.exec_driver_sql(CONTACT_PHONE_IDENTITY_FUNCTION)
    connection.exec_driver_sql(CONTACT_PHONE_IDENTITY_TRIGGER)


def install_conversation_participant(
    _target: object, connection: Connection, **_kwargs: Any
) -> None:
    """Create the participant default beside `conversations` in a model-built schema."""
    connection.exec_driver_sql(CONVERSATION_PARTICIPANT_FUNCTION)
    connection.exec_driver_sql(CONVERSATION_PARTICIPANT_TRIGGER)


__all__ = [
    "CHANNEL_TYPE",
    "MAX_EXTERNAL_ACCOUNT_ID_LENGTH",
    "MAX_IDENTITY_VALUE_LENGTH",
    "MAX_SCOPE_REF_LENGTH",
    "Channel",
    "ChannelConnection",
    "ConnectionHealth",
    "ConnectionStatus",
    "ContactIdentity",
    "IdentityKind",
    "IdentityScope",
    "IdentitySource",
]

"""WhatsApp Business accounts - the WhatsApp extension of a channel connection.

A WhatsApp number is a `ChannelConnection` with the same id. What only WhatsApp
has lives here: the phone number id, the WhatsApp Business Account, the display
number, the verified name, the ownership proof and the credential. What every
connection has - which workspace holds it and since when, whether it is paused
or given up - is written here during the compatibility window and mirrored to
`channel_connections` by a trigger in the same statement (ADR-117).

The raw inbound event log that used to be defined here is channel-neutral now
(`app.db.models.channel_event`); `WhatsAppEvent` and its enums remain as names
for it.

Two rules govern the account row.

**The credential is encrypted or absent, never plaintext.** ADR-009 refused to
store a Meta token here at all until encryption at rest existed; ADR-034 built
it. The column name says so, because a query result or a support screenshot
must not be mistakable for a usable credential.

**A claim on a number is backed by proof, and the proof is recorded.** The
platform-wide uniqueness of `phone_number_id` says only that nobody else holds
the number; `ownership_verified_at` says the workspace holding it proved
control of it to Meta at claim time. See ADR-037.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.channel import install_whatsapp_connection_mirror
from app.db.models.channel_event import (
    CHANNEL_EVENT_KIND_TYPE,
    CHANNEL_EVENT_STATE_TYPE,
    ChannelEvent,
    ChannelEventKind,
    ChannelEventState,
)
from app.db.models.enums import _enum_type


class WhatsAppAccountStatus(StrEnum):
    """Whether Wasla should accept and send traffic for this number.

    `DISABLED` is a pause: the workspace still holds the claim, and nobody else
    may take the number. `RELEASED` gives the claim up - the row stays so its
    conversations and messages survive, but the number is free for another
    workspace to prove and claim. The two are kept apart because a support
    request to "turn it off for a week" and one to "hand it back" have opposite
    consequences for everyone else on the platform.
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    RELEASED = "released"


# The inbound event log's names from before it served every channel. Aliases,
# not copies: one class, one table, one enum per database type.
WhatsAppEventKind = ChannelEventKind
WhatsAppEventState = ChannelEventState
WhatsAppEvent = ChannelEvent

WHATSAPP_ACCOUNT_STATUS_TYPE = _enum_type(
    WhatsAppAccountStatus,
    name="whatsapp_account_status",
)
WHATSAPP_EVENT_KIND_TYPE = CHANNEL_EVENT_KIND_TYPE
WHATSAPP_EVENT_STATE_TYPE = CHANNEL_EVENT_STATE_TYPE


class WhatsAppAccount(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A WhatsApp Business phone number connected by one workspace.

    `phone_number_id` is unique among *live* claims, not per workspace. That is
    the property the tenant resolver depends on: one number can never be held by
    two workspaces at once, so inbound traffic is attributed with certainty.

    The uniqueness is a partial index rather than a constraint, and that is what
    makes handing a number back possible. A released row keeps its history -
    conversations and messages cascade from its connection - while no longer
    occupying the number. A plain `UNIQUE(phone_number_id)` would force the
    choice between deleting a customer's conversation history and never letting
    a number move, which is not a choice anyone should have to make.
    """

    __tablename__ = "whatsapp_accounts"
    # The tenant index is restated here rather than inherited. Declaring
    # __table_args__ in a class body replaces the one TenantScopedMixin
    # contributes, so omitting it drops the index from the metadata while
    # migration 0003 still creates it, and `alembic check` fails.
    __table_args__ = (
        Index(
            "uq_whatsapp_accounts_live_phone_number_id",
            "phone_number_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        Index("ix_whatsapp_accounts_tenant_id", "tenant_id"),
        # Who held this number when an event happened. Ordered by claim time
        # so the historical resolver walks a number's claims newest first and
        # stops at the interval containing the event (ADR-101). Released rows
        # are the ones this query exists to find, so the partial live-only
        # index above cannot serve it.
        Index(
            "ix_whatsapp_accounts_phone_number_id_ownership_started_at",
            "phone_number_id",
            "ownership_started_at",
        ),
        # The target of the composite foreign key that pins a conversation to
        # an account in its own workspace (ADR-100). Not a new uniqueness claim
        # - `id` is already the primary key - but a composite foreign key can
        # only reference a uniquely constrained set of columns.
        UniqueConstraint("tenant_id", "id", name="uq_whatsapp_accounts_tenant_id_id"),
    )

    phone_number_id: Mapped[str] = mapped_column(String(64), nullable=False)
    waba_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # What Meta calls this business on this number. Recorded from the ownership
    # check rather than typed by the person connecting, so it can be shown next
    # to the number as something the platform confirmed rather than something a
    # customer asserted.
    verified_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[WhatsAppAccountStatus] = mapped_column(
        WHATSAPP_ACCOUNT_STATUS_TYPE,
        nullable=False,
        default=WhatsAppAccountStatus.ACTIVE,
    )
    # When this workspace last proved to Meta that it controls this number
    # (ADR-037). Nullable only for rows written before ownership proof existed;
    # every new claim sets it, and nothing may connect without it.
    ownership_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # When this workspace's claim on the number began. The other half of
    # `released_at`, and together they are the tenure interval inbound routing
    # asks about: `[ownership_started_at, released_at)`.
    #
    # A column of its own rather than `created_at`, which coincides with it
    # today only because `connect` is this table's one writer. Attributing a
    # customer's message to the right workspace is not a decision to rest on a
    # generic audit timestamp continuing to mean something specific (ADR-101).
    #
    # `connect` sets it explicitly, at the instant it decides the claim is
    # granted. The server default is for every other way a row can come into
    # existence - a fixture, a seed - where "the claim began when the row did"
    # is both true and the only defensible answer.
    ownership_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Set when the workspace gives the number up. Non-null takes the row out of
    # the uniqueness index above, which is what frees the number.
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # The workspace's own Meta token, encrypted (ADR-034, superseding ADR-009).
    # Never plaintext, never returned by any API, and never logged: the column
    # name says "encrypted" so a query result, a backup or a support screenshot
    # cannot be mistaken for a usable credential.
    #
    # Nullable, because a workspace without one falls back to the platform
    # token - which is how every workspace worked before this column existed.
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_active(self) -> bool:
        return self.status is WhatsAppAccountStatus.ACTIVE and self.released_at is None

    def held_at(self, instant: datetime) -> bool:
        """Whether this workspace held the number at `instant`.

        Half-open on purpose: `[ownership_started_at, released_at)`. A claim
        and the release that precedes it can share a timestamp to the
        microsecond when a number moves quickly, and a closed interval would
        make that instant belong to two workspaces at once - which is the one
        answer this method must never give.
        """
        if instant < self.ownership_started_at:
            return False
        return self.released_at is None or instant < self.released_at

    @property
    def is_released(self) -> bool:
        """Whether the claim on this number has been given up."""
        return self.released_at is not None

    @property
    def ownership_verified(self) -> bool:
        """Whether control of this number has been proven to Meta (ADR-037).

        False on rows claimed before proof existed. Named to match the API
        field exactly, like `has_own_credential`, because it is one of the few
        properties a response reads straight off the row.

        Exposed as a boolean rather than left for a caller to infer from a null
        timestamp: the security state of a number is not something an operator
        should have to deduce, and the set of unverified rows is the migration
        list (ADR-041).
        """
        return self.ownership_verified_at is not None

    @property
    def has_own_credential(self) -> bool:
        """Whether this number sends with the workspace's own token.

        The only thing about the credential any caller outside the messaging
        path is allowed to learn.
        """
        return self.access_token_encrypted is not None


# The connection row every number has (ADR-117), for a model-built schema. A
# migrated one gets the trigger from 0082.
event.listen(WhatsAppAccount.__table__, "after_create", install_whatsapp_connection_mirror)


__all__ = [
    "WHATSAPP_ACCOUNT_STATUS_TYPE",
    "WHATSAPP_EVENT_KIND_TYPE",
    "WHATSAPP_EVENT_STATE_TYPE",
    "WhatsAppAccount",
    "WhatsAppAccountStatus",
    "WhatsAppEvent",
    "WhatsAppEventKind",
    "WhatsAppEventState",
]

"""WhatsApp Business accounts and the raw inbound event log.

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

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
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


class WhatsAppEventKind(StrEnum):
    """What Meta sent. `UNSUPPORTED` is kept rather than dropped."""

    MESSAGE = "message"
    STATUS = "status"
    UNSUPPORTED = "unsupported"


class WhatsAppEventState(StrEnum):
    """How far an event has travelled through processing."""

    RECEIVED = "received"
    PROCESSED = "processed"
    FAILED = "failed"


WHATSAPP_ACCOUNT_STATUS_TYPE = _enum_type(
    WhatsAppAccountStatus,
    name="whatsapp_account_status",
)
WHATSAPP_EVENT_KIND_TYPE = _enum_type(WhatsAppEventKind, name="whatsapp_event_kind")
WHATSAPP_EVENT_STATE_TYPE = _enum_type(WhatsAppEventState, name="whatsapp_event_state")


class WhatsAppAccount(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A WhatsApp Business phone number connected by one workspace.

    `phone_number_id` is unique among *live* claims, not per workspace. That is
    the property the tenant resolver depends on: one number can never be held by
    two workspaces at once, so inbound traffic is attributed with certainty.

    The uniqueness is a partial index rather than a constraint, and that is what
    makes handing a number back possible. A released row keeps its history -
    conversations and messages cascade from this table - while no longer
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

    @property
    def is_released(self) -> bool:
        """Whether the claim on this number has been given up."""
        return self.released_at is not None

    @property
    def has_own_credential(self) -> bool:
        """Whether this number sends with the workspace's own token.

        The only thing about the credential any caller outside the messaging
        path is allowed to learn.
        """
        return self.access_token_encrypted is not None


class WhatsAppEvent(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One raw webhook event, stored before it is interpreted.

    The payload is kept whole because Meta adds fields over time and a webhook
    that discards what it does not yet understand cannot be replayed later.

    `UNIQUE(tenant_id, event_id)` is the idempotency guarantee. It is scoped by
    workspace rather than globally so one workspace's traffic can never suppress
    another's. See ADR-011.
    """

    __tablename__ = "whatsapp_events"
    # See WhatsAppAccount: the inherited tenant index does not survive a
    # class-body __table_args__, so it is restated.
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "event_id",
            name="uq_whatsapp_events_tenant_id_event_id",
        ),
        Index("ix_whatsapp_events_tenant_id", "tenant_id"),
        Index("ix_whatsapp_events_account_id", "account_id"),
        Index("ix_whatsapp_events_tenant_id_state", "tenant_id", "state"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[WhatsAppEventKind] = mapped_column(WHATSAPP_EVENT_KIND_TYPE, nullable=False)
    state: Mapped[WhatsAppEventState] = mapped_column(
        WHATSAPP_EVENT_STATE_TYPE,
        nullable=False,
        default=WhatsAppEventState.RECEIVED,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)

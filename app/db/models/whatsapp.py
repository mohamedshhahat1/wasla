"""WhatsApp Business accounts and the raw inbound event log.

The account row carries no Meta credential on purpose. A per-workspace access
token in a plain column would hand a live sending capability to anyone with a
database dump; until there is encryption at rest, outbound calls use the
platform credential from configuration. See ADR-009.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type


class WhatsAppAccountStatus(StrEnum):
    """Whether Wasla should accept and send traffic for this number."""

    ACTIVE = "active"
    DISABLED = "disabled"


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

    `phone_number_id` is unique platform-wide, not per workspace. That is the
    property the tenant resolver depends on: one number can never belong to two
    workspaces, so inbound traffic is attributed with certainty.
    """

    __tablename__ = "whatsapp_accounts"
    # The tenant index is restated here rather than inherited. Declaring
    # __table_args__ in a class body replaces the one TenantScopedMixin
    # contributes, so omitting it drops the index from the metadata while
    # migration 0003 still creates it, and `alembic check` fails.
    __table_args__ = (
        UniqueConstraint("phone_number_id", name="uq_whatsapp_accounts_phone_number_id"),
        Index("ix_whatsapp_accounts_tenant_id", "tenant_id"),
    )

    phone_number_id: Mapped[str] = mapped_column(String(64), nullable=False)
    waba_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[WhatsAppAccountStatus] = mapped_column(
        WHATSAPP_ACCOUNT_STATUS_TYPE,
        nullable=False,
        default=WhatsAppAccountStatus.ACTIVE,
    )

    @property
    def is_active(self) -> bool:
        return self.status is WhatsAppAccountStatus.ACTIVE


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

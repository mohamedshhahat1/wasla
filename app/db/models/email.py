"""The email outbox: what should be sent, and what happened when we tried.

Email is never sent inside an HTTP request or a domain transaction (ADR-042).
The action that decides an email should exist writes a row here in the same
transaction as itself, so the two share a fate: a rollback takes the email
with it, and a provider outage delays delivery without failing the action.
The email worker claims rows and delivers them afterwards.

Two properties are load-bearing:

**The idempotency key is a constraint, not a convention.** Every business
email carries a deterministic key built from the domain id that caused it,
and the unique constraint is what makes a retried request, a replayed event
or a sweep that ran twice produce one row however the callers race.

**The context is cleared at the end.** For a reset or an invitation the
context briefly carries the very link being delivered - that is the outbox's
job - but a message that has been sent or has permanently failed has no
reason to keep it, so terminal transitions empty it. The exposure of a token
in this table is bounded by the life of the send, not the life of the row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

MAX_TEMPLATE_LENGTH: Final = 80
MAX_IDEMPOTENCY_KEY_LENGTH: Final = 200
MAX_ERROR_CODE_LENGTH: Final = 100
MAX_ERROR_MESSAGE_LENGTH: Final = 500
MAX_SUPPRESSION_REASON_LENGTH: Final = 50


class EmailStatus(StrEnum):
    """Where a message stands.

    `SENT` means the provider accepted it; `DELIVERED` means the provider's
    webhook later said it arrived. The two are separate because acceptance is
    what this system can prove synchronously and delivery is hearsay that
    arrives later or never - collapsing them would make every send lie about
    one or the other.
    """

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"


# Statuses from which a row never moves again on its own. `SENT` is absent
# deliberately: a delivery or bounce event can still arrive for it.
TERMINAL_EMAIL_STATUSES: Final[frozenset[EmailStatus]] = frozenset(
    {
        EmailStatus.DELIVERED,
        EmailStatus.FAILED,
    }
)

EMAIL_STATUS_TYPE = _enum_type(EmailStatus, name="email_status")


class OutboundEmail(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One transactional email the product decided to send.

    Tenant-owned when the email is about a workspace (an invitation, an
    invoice) and global when it is about an account (a password reset), which
    is why `tenant_id` is nullable rather than `TenantScopedMixin`. There is
    deliberately no read API over this table at all - no route lists, retries
    or resends emails - so tenant isolation over it has no surface to defend.
    """

    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_email_messages_idempotency_key"),
        # The worker's sweep: pending rows whose moment has arrived.
        Index("ix_email_messages_status_available_at", "status", "available_at"),
        Index("ix_email_messages_tenant_id", "tenant_id"),
        # The webhook resolves provider events back to rows through this.
        Index("ix_email_messages_provider_message_id", "provider_message_id"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
    )
    # The recipient's account, when they have one. SET NULL rather than
    # CASCADE: the record that a notice was sent outlives the account it was
    # sent to, the same reasoning the audit log applies to actors.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    template: Mapped[str] = mapped_column(String(MAX_TEMPLATE_LENGTH), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    # String values only, written by `EmailOutbox`. Cleared on terminal
    # transitions - see the module docstring for why.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[EmailStatus] = mapped_column(
        EMAIL_STATUS_TYPE,
        nullable=False,
        default=EmailStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # When the next attempt may happen. Enqueue sets it to now; a transient
    # failure pushes it into the future with backoff.
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(MAX_IDEMPOTENCY_KEY_LENGTH),
        nullable=False,
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(MAX_ERROR_CODE_LENGTH),
        nullable=True,
    )
    last_error_message: Mapped[str | None] = mapped_column(
        String(MAX_ERROR_MESSAGE_LENGTH),
        nullable=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        # The recipient is deliberately absent: reprs reach logs.
        return f"OutboundEmail(template={self.template!r}, status={self.status!r})"


class EmailSuppression(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An address the platform must stop writing to.

    Written by the provider webhook on a hard bounce or a complaint, read by
    the worker before every send. Deliberately separate from any auth state:
    a bounced address says the mailbox is unreachable, not that the account
    holding it did anything - suppression never disables an account.
    """

    __tablename__ = "email_suppressions"
    __table_args__ = (
        UniqueConstraint("recipient", name="uq_email_suppressions_recipient"),
    )

    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(MAX_SUPPRESSION_REASON_LENGTH), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"EmailSuppression(reason={self.reason!r})"

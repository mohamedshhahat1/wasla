"""Who did what, to what, and when.

An audit log answers questions after the fact, usually hostile ones: who
disconnected that number, who let this person into the workspace, who marked
that invoice paid. Three properties make it able to.

**It is append-only and nothing may edit it.** There is no update path and no
`updated_at`. A log somebody can rewrite answers nothing.

**It records what was true at the time.** The actor's email and the target's
description are copied onto the row, not joined for. A person who is deleted,
renamed or removed from a workspace must not make last quarter's entry
unreadable — and a row that says "user 8f3c… did something to lead 91ab…" is
useless precisely when it is needed.

**A platform action is logged exactly like a workspace one.** `tenant_id` is
nullable because a platform administrator acts across workspaces rather than
inside one, and the platform owner is explicitly not exempt (claude.md §8).

What it is *not* is an analytics event. `analytics_events` counts things for a
dashboard and is derived where possible (ADR-028); this records deliberate acts
by people, is never derived, and is kept when the thing it describes is gone.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

MAX_ACTOR_LABEL_LENGTH: Final = 320
MAX_TARGET_LABEL_LENGTH: Final = 200
MAX_ACTION_LENGTH: Final = 80


class AuditActorKind(StrEnum):
    """Who performed the action.

    `SYSTEM` covers a worker acting on its own schedule - a subscription the
    sweep expired, an invoice it issued. Those are recorded because "nobody did
    this, time did" is a real and useful answer to "who cancelled my plan".
    """

    USER = "user"
    PLATFORM_STAFF = "platform_staff"
    SYSTEM = "system"


class AuditAction(StrEnum):
    """What was done.

    A closed vocabulary rather than free text, because an audit log is read by
    filtering it. Free-text actions become a dozen spellings of the same event
    and a search that silently misses half of them.

    Deliberately narrow: only actions somebody could be *asked about later*.
    Reading a page is not audited - it would bury the entries that matter under
    a million that do not, and it is the wrong tool for that question anyway.

    Where a family of outcomes shares one question - "did this verification
    fail, and why" - the family gets one action and the outcome travels in
    `meta`. Splitting it into a member per reason would grow this enum without
    making any query easier to write.
    """

    # Access to a workspace
    MEMBER_INVITED = "member_invited"
    INVITATION_REVOKED = "invitation_revoked"
    INVITATION_ACCEPTED = "invitation_accepted"
    # Withdrawing and restoring access (ADR-038). `MEMBER_LEFT` is separate
    # from `MEMBER_REMOVED` on purpose: "who threw them out" and "they walked"
    # are different answers to the same question, and collapsing them would
    # make an administrator look responsible for somebody else's decision.
    MEMBER_REMOVED = "member_removed"
    MEMBER_LEFT = "member_left"
    MEMBER_REINSTATED = "member_reinstated"

    # The account itself, and the sessions it holds (ADR-036). Platform-level
    # rather than workspace-level: an account is a global identity, so disabling
    # one reaches every workspace that person belongs to. Recorded with no
    # tenant, which is what makes them visible in the platform trail.
    USER_DISABLED = "user_disabled"
    USER_ENABLED = "user_enabled"
    USER_SESSIONS_REVOKED = "user_sessions_revoked"
    # The name of an event, not a credential. Nothing here ever holds one.
    PASSWORD_CHANGED = "password_changed"  # noqa: S105
    # A reset token from the email flow was consumed and the password replaced
    # (ADR-042). Only completion is audited: the request is unauthenticated,
    # so recording it would let anyone write into a stranger's trail, and its
    # volume would bury the entries that matter.
    PASSWORD_RESET_COMPLETED = "password_reset_completed"  # noqa: S105
    # A refresh token that had already been spent was presented again
    # (ADR-039). The most security-relevant entry in this vocabulary: it is
    # either a leak being replayed or a client bug, and the first is worth
    # waking somebody for. The token itself is never recorded - only that a
    # replay happened and what the version was raised to.
    REFRESH_TOKEN_REUSED = "refresh_token_reused"  # noqa: S105

    # Proving control of the address on an account
    # (docs/EMAIL_VERIFICATION.md). Unlike a password reset, the request half
    # *is* audited: sending requires a session, so nobody can write into a
    # stranger's trail by asking, and "who asked for a code" is a real question
    # when an address turns out to be wrong.
    EMAIL_VERIFICATION_REQUESTED = "email_verification_requested"
    # A code was accepted and `users.email_verified_at` was set. Grants
    # nothing: this records an account-integrity fact, not a privilege change.
    EMAIL_VERIFIED = "email_verified"
    # A code was rejected. `meta["reason"]` separates a wrong code from an
    # expired one, an exhausted challenge, a superseded one, a malformed
    # submission and a refused rate limit - one action, because they are all
    # answers to "why did verification not work", and a burst of them against
    # one account is the signal worth alerting on whatever the reason says.
    EMAIL_VERIFICATION_FAILED = "email_verification_failed"

    # The channel a business talks to its customers through
    WHATSAPP_ACCOUNT_CONNECTED = "whatsapp_account_connected"
    WHATSAPP_ACCOUNT_DISABLED = "whatsapp_account_disabled"
    WHATSAPP_ACCOUNT_ENABLED = "whatsapp_account_enabled"
    # The claim on a number was given up, freeing it for another workspace to
    # prove and claim (ADR-037). Distinct from disabling, which keeps it.
    WHATSAPP_ACCOUNT_RELEASED = "whatsapp_account_released"
    # Control of a number already held was proven to Meta (ADR-041). The entry
    # that closes out a row claimed before proof existed, and the one an
    # operator reads to show that a legacy number has since been established.
    WHATSAPP_ACCOUNT_VERIFIED = "whatsapp_account_verified"

    # Money
    SUBSCRIPTION_STARTED = "subscription_started"
    SUBSCRIPTION_PLAN_CHANGED = "subscription_plan_changed"
    SUBSCRIPTION_CANCELLED = "subscription_cancelled"
    SUBSCRIPTION_RESUMED = "subscription_resumed"
    PAYMENT_RECORDED = "payment_recorded"
    INVOICE_VOIDED = "invoice_voided"

    # Writing to many customers at once
    CAMPAIGN_SCHEDULED = "campaign_scheduled"
    CAMPAIGN_CANCELLED = "campaign_cancelled"


AUDIT_ACTOR_KIND_TYPE = _enum_type(AuditActorKind, name="audit_actor_kind")
AUDIT_ACTION_TYPE = _enum_type(AuditAction, name="audit_action")


class AuditLog(Base, UUIDPrimaryKeyMixin):
    """One recorded act."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        # The workspace's own trail, newest first. Nullable tenant rows - the
        # platform's - are excluded by the index and read through the second.
        Index("ix_audit_logs_tenant_id_occurred_at", "tenant_id", "occurred_at"),
        Index("ix_audit_logs_occurred_at", "occurred_at"),
        Index("ix_audit_logs_action_occurred_at", "action", "occurred_at"),
        Index("ix_audit_logs_actor_id", "actor_id"),
    )

    # Nullable: a platform administrator acts across workspaces rather than
    # inside one, and those acts are the ones most worth recording.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[AuditAction] = mapped_column(AUDIT_ACTION_TYPE, nullable=False)
    actor_kind: Mapped[AuditActorKind] = mapped_column(AUDIT_ACTOR_KIND_TYPE, nullable=False)
    # `SET NULL`, never `CASCADE`: deleting an account must not erase what that
    # account did. The label below is what keeps the row readable afterwards.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Copied at write time - an email, or "system". A join would produce a blank
    # column for exactly the deleted account somebody is asking about.
    actor_label: Mapped[str | None] = mapped_column(
        String(MAX_ACTOR_LABEL_LENGTH),
        nullable=True,
    )
    # What was acted on, as an opaque type and id plus a human label. Not a
    # foreign key: the target may be a row in any of a dozen tables, and half
    # the interesting entries describe something that has since been deleted.
    target_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    target_label: Mapped[str | None] = mapped_column(
        String(MAX_TARGET_LABEL_LENGTH),
        nullable=True,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Whatever makes the entry explicable later: the plan somebody moved to, the
    # reason given for a void. Never a credential, and never a customer's words.
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"AuditLog(action={self.action!r}, actor={self.actor_label!r})"

"""Campaigns: one approved template, sent to many people who asked to hear from you.

Two tables, and the split is the same one media and sentiment make. `campaigns`
is what a person configured and can see the state of; `campaign_recipients` is
one row per person it is owed to, which is where idempotency, retries and
per-person outcomes live.

The recipient row is the load-bearing part. A campaign of ten thousand people is
ten thousand chances for a worker to die halfway, and the only way a restart can
know what it already sent is a row per person with a state on it. Counting
progress in a column on the campaign would be a number that drifts from reality
the first time a process is killed between the send and the increment.

`UNIQUE(campaign_id, contact_id)` is the idempotency key, exactly as
`message_sentiments.message_id` is for a reading. A person appears in a campaign
once, and the database is what guarantees it rather than a check in a service —
because the check would have to hold across every worker replica at once.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

MAX_CAMPAIGN_NAME_LENGTH: Final = 200
MAX_ERROR_LENGTH: Final = 500

# How fast one campaign may write, and the ceiling a workspace may raise it to.
# This is not Meta's throughput limit, which is far higher. It is a quality
# limit: a number that suddenly writes to ten thousand people is exactly the
# pattern that collects blocks, and a blocked number takes the whole business
# down rather than one campaign.
DEFAULT_MESSAGES_PER_MINUTE: Final = 60
MAX_MESSAGES_PER_MINUTE: Final = 600
MIN_MESSAGES_PER_MINUTE: Final = 1

# A recipient this many failures deep is not going to succeed. Low, like a
# follow-up's, because every attempt is a message that might actually arrive.
MAX_RECIPIENT_ATTEMPTS: Final = 3


class CampaignStatus(StrEnum):
    """Where a campaign is in its life.

    `PAUSED` and `CANCELLED` are deliberately different. A paused campaign is
    stopped and resumable — its remaining recipients keep waiting. A cancelled
    one is finished: what has been sent has been sent, and the rest never will
    be. Collapsing them would mean a person hesitating over a send has no way
    back that is not destructive.

    `FAILED` is for a campaign that could not run at all — its template was
    withdrawn, its number was disabled — as distinct from one that ran and had
    some recipients fail, which completes.
    """

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RecipientStatus(StrEnum):
    """What happened to one person's copy.

    `SKIPPED` is a policy outcome and is never retried: the contact opted out,
    or something about them made the send impermissible. `FAILED` is an attempt
    that broke and may work next time. The distinction is the follow-up's, for
    the follow-up's reason — retrying a policy refusal is a loop against a wall.
    """

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class OptOutSource(StrEnum):
    """Who decided this person should stop receiving campaigns.

    Recorded because the two are not equivalent. A customer's own "stop" is
    theirs to reverse and a workspace should think hard before overriding it; a
    colleague's is an operational note about someone they spoke to.
    """

    CUSTOMER = "customer"
    TEAM = "team"


# Statuses in which a campaign is finished and will never send again.
TERMINAL_CAMPAIGN_STATUSES: Final[frozenset[CampaignStatus]] = frozenset(
    {
        CampaignStatus.COMPLETED,
        CampaignStatus.CANCELLED,
        CampaignStatus.FAILED,
    },
)

CAMPAIGN_STATUS_TYPE = _enum_type(CampaignStatus, name="campaign_status")
RECIPIENT_STATUS_TYPE = _enum_type(RecipientStatus, name="recipient_status")
OPT_OUT_SOURCE_TYPE = _enum_type(OptOutSource, name="opt_out_source")


class Campaign(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One broadcast: an approved template, a number to send from, an audience.

    There is no free-text body here and there never will be. Outside the 24-hour
    service window Meta accepts approved templates only, and a campaign is by
    definition to people who are outside it — the ones inside it are having a
    conversation, and writing to them through a broadcast is not what a
    conversation is for.

    `audience` records the filter the recipient list was built from. It is kept
    after materialisation, unused by the send, so a person can read what was
    targeted months later without reconstructing it from ten thousand rows.
    """

    __tablename__ = "campaigns"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        Index("ix_campaigns_tenant_id", "tenant_id"),
        Index("ix_campaigns_tenant_id_status", "tenant_id", "status"),
        Index("ix_campaigns_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_campaigns_account_id", "account_id"),
        Index("ix_campaigns_template_id", "template_id"),
        # The worker's only query: campaigns whose moment has come, soonest
        # first. Partial, because a completed campaign is history and scanning
        # it would make every sweep slower as the table grows.
        Index(
            "ix_campaigns_due",
            "scheduled_at",
            postgresql_where=text("status IN ('scheduled', 'running')"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Cascaded with the account, which is also what deletes the template rows.
    # A campaign without its template has nothing to send and no way to say what
    # it would have said.
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_templates.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(MAX_CAMPAIGN_NAME_LENGTH), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[CampaignStatus] = mapped_column(
        CAMPAIGN_STATUS_TYPE,
        nullable=False,
        default=CampaignStatus.DRAFT,
    )

    # The template's body parameters, in order. One list for the whole campaign:
    # per-recipient personalisation needs a source of per-recipient facts that
    # nothing here has yet.
    variables: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    audience: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    audience_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    messages_per_minute: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_MESSAGES_PER_MINUTE,
    )

    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When this campaign may next write. Set forward by a sweep that has used up
    # its allowance, which is how the rate limit survives a restart: a worker
    # that dies does not hand the next one permission to send a burst.
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(String(MAX_ERROR_LENGTH), nullable=True)

    @property
    def is_running(self) -> bool:
        return self.status is CampaignStatus.RUNNING

    @property
    def is_finished(self) -> bool:
        return self.status in TERMINAL_CAMPAIGN_STATUSES


class CampaignRecipient(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One person's copy of a campaign, and what became of it.

    Materialised up front rather than discovered as the campaign runs. A list
    computed lazily would change under the campaign's feet — a contact who
    writes in halfway through would silently drop out of an audience defined by
    "has not written recently" — and nobody could answer "who was this sent to"
    afterwards.
    """

    __tablename__ = "campaign_recipients"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "contact_id",
            name="uq_campaign_recipients_campaign_id_contact_id",
        ),
        Index("ix_campaign_recipients_tenant_id", "tenant_id"),
        Index("ix_campaign_recipients_campaign_id_status", "campaign_id", "status"),
        Index("ix_campaign_recipients_contact_id", "contact_id"),
        # What the worker claims from, and the only query that runs ten thousand
        # times. Partial: everything else in the table is finished work.
        Index(
            "ix_campaign_recipients_pending",
            "campaign_id",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Filled when the send happens, because that is when the conversation is
    # resolved or created. Nulled rather than cascaded: deleting a conversation
    # must not erase the record that a campaign message went to this person.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[RecipientStatus] = mapped_column(
        RECIPIENT_STATUS_TYPE,
        nullable=False,
        default=RecipientStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(MAX_ERROR_LENGTH), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_pending(self) -> bool:
        return self.status is RecipientStatus.PENDING

    @property
    def is_exhausted(self) -> bool:
        return self.attempts >= MAX_RECIPIENT_ATTEMPTS


__all__ = [
    "CAMPAIGN_STATUS_TYPE",
    "DEFAULT_MESSAGES_PER_MINUTE",
    "MAX_CAMPAIGN_NAME_LENGTH",
    "MAX_MESSAGES_PER_MINUTE",
    "MAX_RECIPIENT_ATTEMPTS",
    "MIN_MESSAGES_PER_MINUTE",
    "OPT_OUT_SOURCE_TYPE",
    "RECIPIENT_STATUS_TYPE",
    "TERMINAL_CAMPAIGN_STATUSES",
    "Campaign",
    "CampaignRecipient",
    "CampaignStatus",
    "OptOutSource",
    "RecipientStatus",
]

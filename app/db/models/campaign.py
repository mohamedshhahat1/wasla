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
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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


class CampaignStopReason(StrEnum):
    """Why a campaign is paused or failed, in words a program can read (CMP-02, CMP-23).

    `last_error` keeps its sentence for a person; this is the closed vocabulary
    an operator filters on and a metric is labelled by. One field that says
    why a campaign is not sending.
    """

    #: The platform suspended the workspace (CMP-02).
    WORKSPACE_SUSPENDED = "workspace_suspended"
    #: The workspace's owner deleted it; it is inside its retention (CMP-02).
    WORKSPACE_DELETED = "workspace_deleted"
    #: The subscription is suspended for an unpaid invoice (CMQ-11).
    SUBSCRIPTION_SUSPENDED = "subscription_suspended"
    #: A person paused it.
    ADMIN_PAUSED = "admin_paused"
    #: The template is no longer in the registry.
    TEMPLATE_REMOVED = "template_removed"
    #: The registry does not hold it as approved.
    TEMPLATE_NOT_APPROVED = "template_not_approved"
    #: Meta refused a send because the template is paused or gone (132001/15/16).
    TEMPLATE_WITHDRAWN = "template_withdrawn"
    #: The template is not a marketing template (CMP-09).
    TEMPLATE_CATEGORY = "template_category"
    #: Meta refused the template's parameters (132000/132012/131008, CMP-05).
    TEMPLATE_PARAMETERS_REFUSED = "template_parameters_refused"
    #: The sending number is disabled.
    CONNECTION_DISABLED = "connection_disabled"
    #: The credential is missing, or Meta refused it.
    CREDENTIAL_REFUSED = "credential_refused"
    #: Meta refused to send through the connection (OMNI-035).
    CONNECTION_REFUSED = "connection_refused"
    #: Meta could not be reached for longer than the bound (CMP-07).
    PROVIDER_UNREACHABLE = "provider_unreachable"
    #: Every recent batch raised (CMP-20).
    REPEATED_BATCH_FAILURE = "repeated_batch_failure"
    #: The header's stored file is gone or is not what was stored (HM-06).
    HEADER_MEDIA_UNAVAILABLE = "header_media_unavailable"
    #: Meta refused the header's file, on upload or on a send twice (HM-06).
    HEADER_MEDIA_REFUSED = "header_media_refused"
    #: Meta dropped a message its portfolio pacing held (135000): the portfolio
    #: is under quality review and cannot send templates (LIM-08).
    META_QUALITY_REVIEW = "meta_quality_review"


MAX_STOP_REASON_LENGTH: Final = 40


class CampaignWaitReason(StrEnum):
    """Why a live campaign is sending nothing right now, and will by itself (TZ-03, LIM-04).

    A wait is not a pause: the campaign stays `scheduled` or `running`, no
    recipient spends an attempt or changes state, no lease is held, and the
    worker comes back at `resumes_at` without anybody doing anything. Cleared
    by the next batch that sends, and by every transition out of the live
    statuses.
    """

    #: It is night in the workspace's own time (TZ-03).
    QUIET_HOURS = "quiet_hours"
    #: The business portfolio's share of Meta's messaging limit is used for
    #: this moving 24 hours (LIM-04).
    DAILY_LIMIT = "daily_limit"
    #: Everybody left was sent a marketing message in the last 24 hours
    #: (FREQ-01).
    CONTACT_CAP = "contact_cap"
    #: Meta's messaging limit for the sending number has never been read
    #: (LIM-01): fail closed until it is.
    LIMIT_UNKNOWN = "limit_unknown"


# Meta's media ids are short numeric strings; the bound is the one inbound
# handles use (`MAX_MEDIA_HANDLE_LENGTH`).
MAX_PROVIDER_MEDIA_ID_LENGTH: Final = 255


class RecipientReason(StrEnum):
    """Why a recipient ended as it did, in words a program can read (CMP-13).

    `last_error` keeps its sentence for display; this is what a listing is
    filtered by. Null on a recipient that was sent, and on rows recorded
    before reason codes existed.
    """

    #: Opted out on the campaign's channel since the audience was built.
    OPTED_OUT = "opted_out"
    #: The contact was erased.
    CONTACT_GONE = "contact_gone"
    #: Its conversation is on another number, or with another contact.
    OTHER_CONNECTION = "other_connection"
    #: The conversation no longer addresses the identity materialised.
    IDENTITY_CHANGED = "identity_changed"
    #: The plan in force does not include the campaign's channel (ENT-16).
    CHANNEL_NOT_IN_PLAN = "channel_not_in_plan"
    #: Meta refused the send: the person stopped marketing messages (131050).
    RECIPIENT_OPTED_OUT_AT_PROVIDER = "recipient_opted_out_at_provider"
    #: Meta's per-user marketing limit (131049); Meta says wait 24 hours.
    MARKETING_LIMIT_PER_USER = "marketing_limit_per_user"
    #: Meta could not deliver to this person (131026).
    UNDELIVERABLE = "undeliverable"
    #: The send's outcome could not be known; never sent again (ADR-093).
    DELIVERY_UNCONFIRMED = "delivery_unconfirmed"
    #: Every attempt failed.
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    #: Meta refused the template's parameters for this send (CMP-05).
    TEMPLATE_PARAMETERS_REFUSED = "template_parameters_refused"
    #: Still pending when the campaign's deadline passed (LIM-06).
    SEND_WINDOW_CLOSED = "send_window_closed"
    #: Sent a marketing message less than 24 hours earlier, and the deadline
    #: came first (FREQ-01).
    CONTACT_FREQUENCY_CAP = "contact_frequency_cap"


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


class OptOutVia(StrEnum):
    """By which evidence an opt-out reached Wasla (OMNI-030, OMNI-046).

    Beside `OptOutSource`, which says who decided: a customer's tapped button,
    their stop word and the provider's own preference record are all the
    customer's decision, arriving by different routes - and which route is what
    an operator counts, and what tells a replay apart from a live request.
    """

    #: The whole message was a stop word.
    MESSAGE = "message"
    #: A tapped button: its words were a stop phrase, or its payload one the
    #: workspace marked as the marketing opt-out.
    REPLY_ACTION = "reply_action"
    #: The provider's own record of the person's preference (WhatsApp
    #: `user_preferences`, `marketing_messages: stop`).
    PROVIDER_PREFERENCE = "provider_preference"
    #: The provider refused a send because the person stopped marketing
    #: messages (WhatsApp error 131050).
    PROVIDER_REFUSAL = "provider_refusal"
    #: Recovered from retained evidence by the operator command, after the
    #: live path had missed it.
    REPLAY = "replay"
    #: Recorded by a colleague through the API.
    TEAM = "team"
    #: The provider accepted a send and later reported it failed because the
    #: person stopped marketing messages (131050 on a `failed` status, CMP-10).
    #: Last, because 0105 appends the label (`ALTER TYPE ... ADD VALUE`).
    PROVIDER_STATUS = "provider_status"


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
OPT_OUT_VIA_TYPE = _enum_type(OptOutVia, name="opt_out_via")


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
        # The target of a tenant-composite foreign key (ADR-100): a create key
        # names a campaign of its own workspace (PG-08). 0115.
        Index("uq_campaigns_tenant_id_id", "tenant_id", "id", unique=True),
        # Which campaigns a segment was used by (PG-06). Partial: most none.
        Index(
            "ix_campaigns_audience_segment_id",
            "audience_segment_id",
            postgresql_where=text("audience_segment_id IS NOT NULL"),
        ),
        Index("ix_campaigns_template_id", "template_id"),
        # Which campaigns name an asset: retention keeps a live campaign's
        # file (HM-07), and the asset's foreign key is checked through it.
        # Partial, because most campaigns have no media header. 0107.
        Index(
            "ix_campaigns_header_media_id",
            "header_media_id",
            postgresql_where=text("header_media_id IS NOT NULL"),
        ),
        # The worker's only query: campaigns whose moment has come, soonest
        # first. Partial, because a completed campaign is history and scanning
        # it would make every sweep slower as the table grows.
        Index(
            "ix_campaigns_due",
            "scheduled_at",
            postgresql_where=text("status IN ('scheduled', 'running')"),
        ),
        CheckConstraint(
            "status_reason IS NULL OR status_reason IN ("
            + ", ".join(f"'{reason.value}'" for reason in CampaignStopReason)
            + ")",
            name="status_reason_known",
        ),
        CheckConstraint(
            "waiting_reason IS NULL OR waiting_reason IN ("
            + ", ".join(f"'{reason.value}'" for reason in CampaignWaitReason)
            + ")",
            name="waiting_reason_known",
        ),
        CheckConstraint(
            "consecutive_batch_failures >= 0", name="consecutive_batch_failures_non_negative"
        ),
        # A deadline comes after the start it ends (LIM-06). 0112.
        CheckConstraint(
            "send_until IS NULL OR scheduled_at IS NULL OR send_until > scheduled_at",
            name="send_until_after_start",
        ),
        # The send rate the schema and the service already bound, held by the
        # column too (CMP-22). 0106.
        CheckConstraint(
            f"messages_per_minute BETWEEN {MIN_MESSAGES_PER_MINUTE} AND {MAX_MESSAGES_PER_MINUTE}",
            name="messages_per_minute_bounded",
        ),
        # The sending number is one of the campaign's own workspace (ADR-100's
        # pattern, CMP-22). 0106. The template's twin needs a (tenant_id, id)
        # key on `whatsapp_templates` that does not exist: accepted residual.
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["whatsapp_accounts.tenant_id", "whatsapp_accounts.id"],
            name="fk_campaigns_tenant_account",
            ondelete="CASCADE",
        ),
        # The header's file is one of the campaign's own workspace (HM-02).
        # No cascade: an asset a campaign names is not deleted from under it;
        # the workspace purge deletes campaigns first (0107).
        ForeignKeyConstraint(
            ["tenant_id", "header_media_id"],
            ["campaign_header_media.tenant_id", "campaign_header_media.id"],
            name="fk_campaigns_tenant_header_media",
        ),
        # The connection Meta's media id was uploaded through, of this
        # workspace (HM-04, CR-09).
        # The segment its audience rules were copied from, of its own workspace
        # (PG-06, CR-13). No action on delete: deleting a segment clears the
        # reference first, and the campaign keeps the rules it copied. 0116.
        ForeignKeyConstraint(
            ["tenant_id", "audience_segment_id"],
            ["campaign_segments.tenant_id", "campaign_segments.id"],
            name="fk_campaigns_tenant_audience_segment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "header_provider_account_id"],
            ["whatsapp_accounts.tenant_id", "whatsapp_accounts.id"],
            name="fk_campaigns_tenant_header_provider_account",
            ondelete="CASCADE",
        ),
        # Meta's id, when it is recorded, comes with when and through which
        # connection it was uploaded, and only for a campaign with an asset.
        CheckConstraint(
            "(header_provider_media_id IS NULL AND header_provider_uploaded_at IS NULL "
            "AND header_provider_account_id IS NULL) "
            "OR (header_provider_media_id IS NOT NULL AND header_provider_uploaded_at IS NOT NULL "
            "AND header_provider_account_id IS NOT NULL AND header_media_id IS NOT NULL)",
            name="header_provider_complete",
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
    # The header's and the buttons' send-time parameters, for a template whose
    # stored components need them (CMP-05): `{"header": [...], "buttons":
    # [{"index", "value"}]}`. Null for a body-only template. Bounded by the
    # request schema, like `variables`.
    template_parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

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
    # Which claim currently owns this campaign's sending (CMP-01, ADR-134). Set
    # by the sweep's claim together with the lease in `next_send_at`, renewed
    # per recipient, and cleared when the batch ends. A worker proceeds only
    # while the row still carries its own token, so a worker whose lease was
    # taken over stops at its next recipient rather than working on.
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(String(MAX_ERROR_LENGTH), nullable=True)
    # Why the campaign is paused or failed, from `CampaignStopReason` (CMP-02).
    # Text with a CHECK rather than a native enum, so the vocabulary can grow
    # without an autocommit migration. Cleared when it is scheduled again.
    status_reason: Mapped[str | None] = mapped_column(String(MAX_STOP_REASON_LENGTH), nullable=True)
    # When Meta first could not be reached in the current run of unreachable
    # batches (CMP-07). An outage costs time, not the audience: the campaign
    # waits, spending no attempts, and is paused `provider_unreachable` once
    # this is older than the bound. Cleared by any batch that reaches Meta.
    provider_unreachable_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Batches in a row that raised out of `dispatch_batch` (CMP-20). Reset by
    # a batch that completes; at the bound the campaign is paused
    # `repeated_batch_failure` instead of being retried for ever.
    consecutive_batch_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # The file a media-header template is sent with (HM-02). Fixed at
    # composition: nothing changes it afterwards.
    header_media_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # What Meta calls that file, uploaded once per campaign and connection by
    # the worker holding the campaign's lease (HM-04), and when - re-uploaded
    # before Meta forgets it (HM-05). Never in a response, a log or the audit.
    header_provider_media_id: Mapped[str | None] = mapped_column(
        String(MAX_PROVIDER_MEDIA_ID_LENGTH), nullable=True
    )
    header_provider_uploaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    header_provider_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # Why a live campaign is sending nothing right now, from `CampaignWaitReason`
    # (TZ-03, LIM-04), and when it will by itself. Text with a CHECK, like
    # `status_reason`. Null while sending, and in every other status. 0109.
    waiting_reason: Mapped[str | None] = mapped_column(
        String(MAX_STOP_REASON_LENGTH), nullable=True
    )
    resumes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the campaign stops trying (LIM-06): whoever is still pending then is
    # skipped `send_window_closed` and the campaign completes. An offer that
    # ends on Friday stops on Friday, however long the limit spread it. 0112.
    send_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A draft's edit counter (PG-03): an edit names the version it was made
    # from, and an edit made from an older one is refused 409. Bumped by the
    # draft edit and by writing the audience, never by the worker. 0115.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # The saved segment the audience's rules were copied from (PG-06); the
    # rules themselves are in `audience`. Null when written from filters alone,
    # or when the segment was deleted since. 0116.
    audience_segment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

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
        # Analytics: broadcast outcomes in a window, across every campaign.
        # The per-campaign indexes serve the worker; this serves the report.
        Index("ix_campaign_recipients_tenant_id_created_at", "tenant_id", "created_at"),
        # What deleting a message or a conversation looks up: every deleted
        # row fires a SET NULL here, and without these each one scanned every
        # workspace's recipients (DB-002). Partial - only rows that name one.
        Index(
            "ix_campaign_recipients_message_id",
            "message_id",
            postgresql_where=text("message_id IS NOT NULL"),
        ),
        Index(
            "ix_campaign_recipients_conversation_id",
            "conversation_id",
            postgresql_where=text("conversation_id IS NOT NULL"),
        ),
        # What the worker claims from, and the only query that runs ten thousand
        # times. Partial: everything else in the table is finished work.
        Index(
            "ix_campaign_recipients_pending",
            "campaign_id",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
        # A contact's latest accepted copies, newest first: what a reply or an
        # opt-out is attributed to (PG-10). Partial: unsent rows never are. 0118.
        Index(
            "ix_campaign_recipients_tenant_id_contact_id_sent_at",
            "tenant_id",
            "contact_id",
            text("sent_at DESC"),
            postgresql_where=text("sent_at IS NOT NULL"),
        ),
        Index(
            "ix_campaign_recipients_participant_identity_id",
            "participant_identity_id",
            postgresql_where=text("participant_identity_id IS NOT NULL"),
        ),
        CheckConstraint(
            "reason_code IS NULL OR reason_code IN ("
            + ", ".join(f"'{reason.value}'" for reason in RecipientReason)
            + ")",
            name="reason_code_known",
        ),
        # The identity this copy is addressed to: one of this recipient's own
        # contact's identities, in its own workspace (OMNI-004).
        ForeignKeyConstraint(
            ["tenant_id", "contact_id", "participant_identity_id"],
            [
                "contact_identities.tenant_id",
                "contact_identities.contact_id",
                "contact_identities.id",
            ],
            name="fk_campaign_recipients_tenant_participant",
            ondelete="CASCADE",
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
    # Exactly who this copy is addressed to: the identity pinned on the
    # contact's conversation with the campaign's connection, recorded when the
    # audience is materialised (OMNI-004). A contact may hold a phone number and
    # a business-scoped id, or more; this is the one, so "who was this sent to"
    # is answerable from the row. The send refuses a conversation that no
    # longer addresses it rather than choosing again. Null only on rows
    # materialised before 0082 whose conversation could not be found.
    participant_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    status: Mapped[RecipientStatus] = mapped_column(
        RECIPIENT_STATUS_TYPE,
        nullable=False,
        default=RecipientStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(MAX_ERROR_LENGTH), nullable=True)
    # Why it ended as it did, from `RecipientReason` (CMP-13). Text with a
    # CHECK, like `campaigns.status_reason`. Null when sent, and on rows
    # recorded before reason codes.
    reason_code: Mapped[str | None] = mapped_column(String(MAX_STOP_REASON_LENGTH), nullable=True)
    # Not to be asked before this moment (CMP-18): Meta's pair rate limit
    # (131056) is about this person and this number, so the person waits and
    # the rest of the campaign goes on. The claim skips it until then.
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # This person's own values, resolved once when the audience was written
    # (PER-02): `{"contact_id", "body", "header", "fallbacks", "body_too_long"}`.
    # The send uses exactly these. Null for a campaign with nothing personal.
    # A copy of a name per row: retention is CMQ-08. 0114.
    rendered_parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The customer answered on the campaign's number within the attribution
    # window after this copy was accepted - this copy being their latest
    # (PG-10). Recorded once, at ingestion. 0118.
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The customer opted out within the window after this copy, their latest.
    opted_out_after_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_pending(self) -> bool:
        return self.status is RecipientStatus.PENDING

    @property
    def is_exhausted(self) -> bool:
        return self.attempts >= MAX_RECIPIENT_ATTEMPTS


#: The longest `Idempotency-Key` a campaign create accepts (PG-08).
MAX_CREATE_KEY_LENGTH: Final = 128


class CampaignCreateKey(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """A retried "create the draft" names the draft it already made (PG-08).

    The composer's first step sends an `Idempotency-Key`; the same key in the
    same workspace within a day returns that draft instead of making a second.
    `request_hash` is the request it was first used with: the key reused for a
    different request is refused rather than answered with a draft the person
    did not ask for. Rows older than a day are deleted by the retention sweep.
    0115.
    """

    __tablename__ = "campaign_create_keys"
    __table_args__ = (
        Index("ix_campaign_create_keys_tenant_id", "tenant_id"),
        Index("ix_campaign_create_keys_created_at", "created_at"),
        Index("ix_campaign_create_keys_campaign_id", "campaign_id"),
        UniqueConstraint("tenant_id", "key", name="uq_campaign_create_keys_tenant_id_key"),
        UniqueConstraint("tenant_id", "id", name="uq_campaign_create_keys_tenant_id_id"),
        # The draft is one of the key's own workspace (ADR-100); deleting the
        # draft deletes its key.
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.id"],
            name="fk_campaign_create_keys_tenant_campaign",
            ondelete="CASCADE",
        ),
    )

    key: Mapped[str] = mapped_column(String(MAX_CREATE_KEY_LENGTH), nullable=False)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CampaignTestSend(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """One test of a campaign, sent to an admin's own contact (PG-09). 0117.

    Not a recipient: the campaign's list, statistics and per-contact cap never
    see it (CR-14). It is what a workspace's daily allowance of tests is
    counted from, and the link between the test message and its campaign.
    """

    __tablename__ = "campaign_test_sends"
    __table_args__ = (
        Index("ix_campaign_test_sends_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_campaign_test_sends_campaign_id", "campaign_id"),
        Index(
            "ix_campaign_test_sends_message_id",
            "message_id",
            postgresql_where=text("message_id IS NOT NULL"),
        ),
        UniqueConstraint("tenant_id", "id", name="uq_campaign_test_sends_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.id"],
            name="fk_campaign_test_sends_tenant_campaign",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_campaign_test_sends_tenant_contact",
            ondelete="CASCADE",
        ),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    sent_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


__all__ = [
    "CAMPAIGN_STATUS_TYPE",
    "DEFAULT_MESSAGES_PER_MINUTE",
    "MAX_CAMPAIGN_NAME_LENGTH",
    "MAX_CREATE_KEY_LENGTH",
    "MAX_MESSAGES_PER_MINUTE",
    "MAX_RECIPIENT_ATTEMPTS",
    "MIN_MESSAGES_PER_MINUTE",
    "OPT_OUT_SOURCE_TYPE",
    "RECIPIENT_STATUS_TYPE",
    "TERMINAL_CAMPAIGN_STATUSES",
    "Campaign",
    "CampaignCreateKey",
    "CampaignRecipient",
    "CampaignStatus",
    "CampaignStopReason",
    "CampaignTestSend",
    "CampaignWaitReason",
    "OptOutSource",
    "RecipientReason",
    "RecipientStatus",
]

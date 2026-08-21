"""Leads, the notes people write on them, and the record of what happened.

Three tables with three different jobs. A lead is the current state of a sales
opportunity and is edited in place. A note is something a person or an agent
wrote and is never edited afterwards. An activity is an append-only fact about
what changed, which is what makes a lead auditable: the lead row says the status
is `qualified`, and the activity log says who decided that and when.

The separation matters because an AI writes to these tables. When a model
extracts a budget from a sentence, someone has to be able to see that the model
did it, what the value was before, and on what evidence - and none of that
survives if extraction just overwrites a column.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

# A lead score is a percentage of confidence, not an unbounded tally. Bounding
# it in one place keeps a model that returns 900 from producing a lead that
# sorts above every real one.
MIN_SCORE: Final = 0
MAX_SCORE: Final = 100


class LeadStatus(StrEnum):
    """Where an opportunity sits in the pipeline.

    `WON` and `LOST` are terminal. Reopening a lost lead is allowed because
    customers do come back; a won one stays won, and the next deal with that
    customer is a new lead rather than an edit to the old one.
    """

    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    PROPOSAL = "proposal"
    WON = "won"
    LOST = "lost"


class LeadSource(StrEnum):
    """How the lead first reached the workspace."""

    WHATSAPP = "whatsapp"
    AGENT = "agent"
    MANUAL = "manual"
    IMPORT = "import"


class ActorKind(StrEnum):
    """Who caused a change.

    Kept separate from the actor id because an AI agent and the system have no
    user row, and a null id alone would not say which of the two it was.
    """

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class LeadActivityKind(StrEnum):
    """What happened to a lead. The vocabulary of the timeline."""

    CREATED = "created"
    STATUS_CHANGED = "status_changed"
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    FIELDS_UPDATED = "fields_updated"
    NOTE_ADDED = "note_added"
    SCORE_CHANGED = "score_changed"


LEAD_STATUS_TYPE = _enum_type(LeadStatus, name="lead_status")
LEAD_SOURCE_TYPE = _enum_type(LeadSource, name="lead_source")
ACTOR_KIND_TYPE = _enum_type(ActorKind, name="actor_kind")
LEAD_ACTIVITY_KIND_TYPE = _enum_type(LeadActivityKind, name="lead_activity_kind")

# Statuses that close a lead. An opportunity in one of these is finished, so it
# no longer occupies the "one active lead per customer" slot.
TERMINAL_STATUSES: Final[frozenset[LeadStatus]] = frozenset(
    {LeadStatus.WON, LeadStatus.LOST},
)

# Which moves are legal. Written as a graph rather than as scattered `if`
# statements so the rule can be read, and tested, in one place. A pipeline that
# permits every move is not a pipeline, and one that permits none cannot record
# a customer who says no immediately - so `LOST` is reachable from anywhere that
# is not already terminal.
ALLOWED_TRANSITIONS: Final[dict[LeadStatus, frozenset[LeadStatus]]] = {
    LeadStatus.NEW: frozenset(
        {LeadStatus.CONTACTED, LeadStatus.QUALIFIED, LeadStatus.LOST},
    ),
    LeadStatus.CONTACTED: frozenset(
        {LeadStatus.QUALIFIED, LeadStatus.PROPOSAL, LeadStatus.LOST},
    ),
    LeadStatus.QUALIFIED: frozenset(
        {LeadStatus.PROPOSAL, LeadStatus.WON, LeadStatus.LOST},
    ),
    LeadStatus.PROPOSAL: frozenset({LeadStatus.WON, LeadStatus.LOST}),
    # Won is final. A returning customer is a new opportunity, and rewriting the
    # old row would destroy the record of the deal that closed.
    LeadStatus.WON: frozenset(),
    # Reopening a lost lead is the one way back, and it returns to the start of
    # the pipeline rather than to wherever it left off.
    LeadStatus.LOST: frozenset({LeadStatus.NEW}),
}

# Columns an AI agent is allowed to fill in. Everything outside this set -
# status, score, assignment, tags - is a judgement a person or an explicit rule
# makes, not something to infer from a sentence a customer typed.
AGENT_WRITABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {"name", "email", "phone", "interest", "budget_amount", "budget_currency"},
)


class Lead(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A sales opportunity belonging to one workspace.

    `contact_id` is nullable because a lead can be entered by hand before anyone
    has messaged, and `conversation_id` because not every lead comes from a
    conversation. When both are set they are the origin, not a live link: moving
    a conversation must not silently move the lead with it.

    At most one *active* lead exists per contact, enforced by a partial unique
    index in the migration. That is what stops an agent creating a fresh lead on
    every message, and it is a database constraint rather than a service check
    because two webhook deliveries can be in flight at once.
    """

    __tablename__ = "leads"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        Index("ix_leads_tenant_id", "tenant_id"),
        Index("ix_leads_tenant_id_status", "tenant_id", "status"),
        Index("ix_leads_tenant_id_assigned_to_id", "tenant_id", "assigned_to_id"),
        # The list is ordered by this pair, and the cursor compares on it.
        Index("ix_leads_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_leads_contact_id", "contact_id"),
        Index("ix_leads_conversation_id", "conversation_id"),
        Index("ix_leads_tags", "tags", postgresql_using="gin"),
        # One open opportunity per customer. Partial, so a customer who bought
        # last year can have a second lead this year, and so leads entered by
        # hand without a contact never collide with each other.
        Index(
            "uq_leads_active_contact",
            "tenant_id",
            "contact_id",
            unique=True,
            postgresql_where=text(
                "contact_id IS NOT NULL AND status <> 'won' AND status <> 'lost'"
            ),
        ),
    )

    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )

    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    interest: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Numeric rather than float: money compared or summed as binary floating
    # point eventually disagrees with the customer's own arithmetic.
    budget_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    budget_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    status: Mapped[LeadStatus] = mapped_column(
        LEAD_STATUS_TYPE,
        nullable=False,
        default=LeadStatus.NEW,
    )
    source: Mapped[LeadSource] = mapped_column(
        LEAD_SOURCE_TYPE,
        nullable=False,
        default=LeadSource.MANUAL,
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=MIN_SCORE)

    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)),
        nullable=False,
        default=list,
    )
    # Workspace-defined extras. JSONB rather than a table of key/value rows
    # because nothing queries across them yet, and a column that is read whole
    # does not need to be joined.
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )

    # Field names a person has set by hand. An agent skips anything listed here,
    # which is what keeps extraction from overwriting what someone confirmed.
    # Stored on the row rather than derived from the activity log, because the
    # check runs on every extraction and must not require a scan.
    human_verified_fields: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)),
        nullable=False,
        default=list,
    )

    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    @property
    def is_closed(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def can_transition_to(self, status: LeadStatus) -> bool:
        """Whether this lead may move to `status`.

        Staying put is always allowed. Treating a no-op as an illegal move would
        make an idempotent retry fail, and retries are how the queue works.
        """
        if status is self.status:
            return True
        return status in ALLOWED_TRANSITIONS[self.status]


class LeadNote(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Something a person or an agent wrote about a lead.

    Internal: notes are never sent to the customer. `author_id` is null when an
    agent or the system wrote it, which `author_kind` disambiguates.
    """

    __tablename__ = "lead_notes"
    __table_args__ = (
        Index("ix_lead_notes_tenant_id", "tenant_id"),
        Index("ix_lead_notes_lead_id_created_at", "lead_id", "created_at"),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    author_kind: Mapped[ActorKind] = mapped_column(
        ACTOR_KIND_TYPE,
        nullable=False,
        default=ActorKind.USER,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)


class LeadActivity(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One append-only fact about what happened to a lead.

    Never updated and never deleted while its lead exists. The timeline is the
    audit trail, and an audit trail that can be edited is not one - so the
    service writes rows here and has no method that changes them.

    `data` holds the before and after of whatever changed. It is JSONB because
    the shape differs per activity kind, and inventing a column for every field
    a lead might one day carry would be worse.
    """

    __tablename__ = "lead_activities"
    __table_args__ = (
        Index("ix_lead_activities_tenant_id", "tenant_id"),
        Index("ix_lead_activities_lead_id_created_at", "lead_id", "created_at"),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[LeadActivityKind] = mapped_column(LEAD_ACTIVITY_KIND_TYPE, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_kind: Mapped[ActorKind] = mapped_column(
        ACTOR_KIND_TYPE,
        nullable=False,
        default=ActorKind.SYSTEM,
    )
    summary: Mapped[str] = mapped_column(String(300), nullable=False)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


def clamp_score(value: int) -> int:
    """Hold a score inside its bounds rather than rejecting it.

    Called on values an AI produced. Refusing an out-of-range score would throw
    away a usable lead over a detail the customer never sees, so it is pinned to
    the nearest end instead.
    """
    return max(MIN_SCORE, min(MAX_SCORE, value))

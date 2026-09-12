"""One logical agent turn, so the same customer message is answered once.

A queue entry is not a turn. The queue's job is to deliver work at least once,
and it is deliberately built that way: a producer that published and then failed
to commit leaves a job behind, `InboundRecoveryWorker` re-derives the same work
from durable state, and both of those are the *correct* behaviour for a system
that must not silently drop a customer's message. What the queue cannot answer
is whether two envelopes naming one conversation are two customers speaking or
one customer's message arriving twice, because the envelope is the only thing it
can see.

This table answers it. A turn is identified by the inbound message it is
answering, and the unique constraint is what makes "answer this message" a thing
that can happen once (WQ-01).

**Why not the conversation.** Two separate messages in one conversation are two
turns and must both be answered; keying on the conversation would swallow the
second. The trigger is the message, so a customer who writes twice is answered
twice and a message delivered twice is answered once.

**Why not an idempotency key on the send alone.** A key on the outbound message
stops the second *reply* and nothing else: the second job still runs a sentiment
classification, still runs the inference, still executes whatever tools the
agent decided to call, and still bills the workspace for all of it. The send key
is kept as well - see `MessagingService.send_text` - but it is the second line,
not the first.

**Why three states rather than a boolean.** The moment that matters is not "has
this turn finished" but "has anything left this process", which is the same
distinction the queue's reservation stages draw and for the same reason:

``CLAIMED``
    One attempt owns this turn and nothing outside the database has happened
    yet. Lease-protected, so a concurrent duplicate refuses it and a retry after
    a pre-provider crash may adopt it once the lease has gone. Repeating the
    work from here is free.

``ENGAGED``
    A provider may already have been called and a customer may already have a
    reply. Terminal for every other attempt, for ever, without a lease -
    exactly as `ReservationStage.ENGAGED` is terminal on the queue (ADR-074).
    Adopting one of these is the duplicate reply the whole design exists to
    prevent.

``COMPLETED``
    The turn ran to its end, whatever that end was: a reply sent, a handoff, or
    a deliberate silence. Nothing further is owed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type


class AgentTurnState(StrEnum):
    """How far one logical turn has got, and therefore who may still run it."""

    CLAIMED = "claimed"
    ENGAGED = "engaged"
    COMPLETED = "completed"


AGENT_TURN_STATE_TYPE = _enum_type(AgentTurnState, name="agent_turn_state")

#: States from which no second attempt may ever proceed. `CLAIMED` is absent
#: deliberately: it means nothing has left the process, so a worker that died
#: before engaging must be able to hand its turn on rather than stranding the
#: customer's message unanswered for ever.
TERMINAL_AGENT_TURN_STATES = frozenset({AgentTurnState.ENGAGED, AgentTurnState.COMPLETED})


class AgentTurn(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """The record that one inbound message is being, or has been, answered."""

    __tablename__ = "agent_turns"
    __table_args__ = (
        # The whole point of the table. Two queue envelopes naming one inbound
        # message cannot both insert this row, so exactly one of them runs the
        # inference, the tools and the send.
        UniqueConstraint(
            "tenant_id",
            "trigger_message_id",
            name="uq_agent_turns_tenant_id_trigger_message_id",
        ),
        # Restated, not inherited: see TenantScopedMixin.
        Index("ix_agent_turns_tenant_id", "tenant_id"),
        # What an operator view or a later sweep reads: turns still holding a
        # claim. Partial, because on a healthy deployment this is a handful of
        # rows out of every turn the platform has ever run, and a full index
        # over that table would be paid for continuously to answer a question
        # nobody usually has.
        Index(
            "ix_agent_turns_unfinished",
            "claim_expires_at",
            postgresql_where=text("state = 'claimed'"),
        ),
        # A turn belongs to a conversation in its own workspace (ADR-100).
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_agent_turns_tenant_conversation",
            ondelete="CASCADE",
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The inbound message this turn is answering. Not a foreign key to
    # `messages`: a turn is evidence that work happened, and it must survive the
    # message being erased by retention or by a workspace purge. The workspace
    # scoping above is what keeps it honest.
    trigger_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[AgentTurnState] = mapped_column(
        AGENT_TURN_STATE_TYPE,
        nullable=False,
        default=AgentTurnState.CLAIMED,
    )
    # Which worker holds it. An identifier for a log line, never an authority:
    # nothing decides anything by comparing this to itself.
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # When a `CLAIMED` turn stops being somebody's. Null once the turn has
    # engaged, because an engaged turn is never adoptable and a lease on it
    # would be a lease that must never expire.
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    engaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def finished(self) -> bool:
        """Whether this turn is done and owes nothing further."""
        return self.state is AgentTurnState.COMPLETED


__all__ = [
    "AGENT_TURN_STATE_TYPE",
    "TERMINAL_AGENT_TURN_STATES",
    "AgentTurn",
    "AgentTurnState",
]

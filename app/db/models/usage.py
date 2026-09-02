"""What a workspace consumed, one append-only row at a time.

Usage is the input to billing, so the rules it is written under matter more than
the shape of the table:

- **A row is staged in the same transaction as the work it describes.** A turn
  that rolls back is not billed, and a message that committed is always counted.
  Nothing here commits; the caller's unit of work decides whether both survive
  together. Recording usage in a session of its own would produce exactly the
  two failures a customer notices - being charged for a reply that was never
  sent, and a bill that quietly under-counts.
- **Exactly once comes from upstream, not from a check here.** Every metered
  path already has an idempotency key of its own: the WhatsApp event id, the
  message row, the media row, `UNIQUE(campaign_id, contact_id)`. A retry that
  re-does the work re-counts it because it *is* work; a retry that skips the
  work writes nothing.
- **The unit is a property of the event type, never of the caller.** A caller
  that could pass its own unit is a caller that can put seconds in a token
  column, and no aggregate afterwards can tell that happened.

Append-only means append-only: there is no `updated_at`, because there is no
operation that would ever set one. A correction is a new row, not an edit -
which is also what makes a monthly total reproducible after the fact.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import BigInteger, DateTime, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type


class UsageEventType(StrEnum):
    """A metered thing that happened.

    Tokens are counted as two types rather than one with a direction column,
    because they are priced differently by every provider and are therefore
    summed separately in every report that matters.
    """

    WHATSAPP_MESSAGE_RECEIVED = "whatsapp_message_received"
    WHATSAPP_MESSAGE_SENT = "whatsapp_message_sent"
    AI_REQUEST = "ai_request"
    # Ruff reads a member named *_TOKEN as a credential. These are units of
    # language-model billing.
    AI_INPUT_TOKEN = "ai_input_token"  # noqa: S105
    AI_OUTPUT_TOKEN = "ai_output_token"  # noqa: S105
    RAG_QUERY = "rag_query"
    MEDIA_PROCESSING = "media_processing"
    VOICE_TRANSCRIPTION = "voice_transcription"
    STORAGE_USED = "storage_used"
    LEAD_CREATED = "lead_created"
    CONVERSATION_CREATED = "conversation_created"
    CAMPAIGN_MESSAGE = "campaign_message"
    API_REQUEST = "api_request"


class UsageUnit(StrEnum):
    """What a quantity is counted in.

    Stored on the row rather than looked up at read time, so a total carries its
    own unit and an aggregate that mixed two of them is visible instead of
    silently wrong.
    """

    COUNT = "count"
    TOKEN = "token"  # noqa: S105 - a unit, not a credential
    BYTE = "byte"
    # Declared, and deliberately unwritten for now. Metering audio by
    # duration needs a duration, and the configured transcription models
    # report none; inferring one from a compressed byte count would put a
    # fabricated number in a bill. See VOICE_TRANSCRIPTION below.
    SECOND = "second"


# The unit each event type is measured in. Declared once, here, and applied by
# the recorder - a caller never supplies a unit, so seconds cannot end up in a
# token total.
EVENT_UNITS: Final[dict[UsageEventType, UsageUnit]] = {
    UsageEventType.WHATSAPP_MESSAGE_RECEIVED: UsageUnit.COUNT,
    UsageEventType.WHATSAPP_MESSAGE_SENT: UsageUnit.COUNT,
    UsageEventType.AI_REQUEST: UsageUnit.COUNT,
    UsageEventType.AI_INPUT_TOKEN: UsageUnit.TOKEN,
    UsageEventType.AI_OUTPUT_TOKEN: UsageUnit.TOKEN,
    UsageEventType.RAG_QUERY: UsageUnit.COUNT,
    UsageEventType.MEDIA_PROCESSING: UsageUnit.COUNT,
    # A count of recordings, not their length. `gpt-4o-mini-transcribe` and
    # its siblings answer in plain JSON with no duration field, and the
    # verbose format that carries one is not accepted by those models. One
    # transcription is a fact; seconds would be a guess.
    UsageEventType.VOICE_TRANSCRIPTION: UsageUnit.COUNT,
    UsageEventType.STORAGE_USED: UsageUnit.BYTE,
    UsageEventType.LEAD_CREATED: UsageUnit.COUNT,
    UsageEventType.CONVERSATION_CREATED: UsageUnit.COUNT,
    UsageEventType.CAMPAIGN_MESSAGE: UsageUnit.COUNT,
    UsageEventType.API_REQUEST: UsageUnit.COUNT,
}

USAGE_EVENT_TYPE = _enum_type(UsageEventType, name="usage_event_type")
USAGE_UNIT_TYPE = _enum_type(UsageUnit, name="usage_unit")


class UsageEvent(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """One metered occurrence in one workspace."""

    __tablename__ = "usage_events"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        Index("ix_usage_events_tenant_id", "tenant_id"),
        # Every aggregate is "this workspace, this window", optionally narrowed
        # to one type. Leading with the tenant and the time makes the whole-
        # dashboard query a range scan; adding the type in front of the
        # timestamp in the second index makes the per-meter one the same.
        Index("ix_usage_events_tenant_id_occurred_at", "tenant_id", "occurred_at"),
        # `quantity` and `unit` are carried in the index rather than fetched
        # from the table, which turns the entitlement check into an index-only
        # scan (ADR-081). It is the hottest read in the system - every agent
        # turn asks it, twice, and the reservation asks it while holding the
        # workspace's advisory lock - and it is a `SUM`, so before this it
        # visited the heap once per row to read two columns it could have been
        # handed. Measured on 3.9M rows: 9.4ms to 7.1ms, `Heap Fetches: 0`.
        Index(
            "ix_usage_events_tenant_id_event_type_occurred_at",
            "tenant_id",
            "event_type",
            "occurred_at",
            postgresql_include=["quantity", "unit"],
        ),
        # The platform dashboard sums across every workspace for a window, which
        # no tenant-leading index can serve.
        Index("ix_usage_events_occurred_at", "occurred_at"),
    )

    event_type: Mapped[UsageEventType] = mapped_column(USAGE_EVENT_TYPE, nullable=False)
    # BigInteger, not Integer. A token count is per-request and small, but the
    # column also carries bytes of stored media, and a busy workspace passes two
    # billion of those in a year.
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    unit: Mapped[UsageUnit] = mapped_column(USAGE_UNIT_TYPE, nullable=False)
    # When the metered thing happened, which is not always when the row was
    # written: a worker draining a backlog records what it is replaying.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Whatever makes a line item explicable after the fact - the model that was
    # billed, the conversation it belonged to. Never load-bearing: nothing reads
    # a key out of here to make a decision, so adding one is always safe.
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return (
            f"UsageEvent(tenant_id={self.tenant_id!r}, "
            f"event_type={self.event_type!r}, quantity={self.quantity!r})"
        )


def unit_for(event_type: UsageEventType) -> UsageUnit:
    """The unit this event type is always measured in.

    A `KeyError` here means a member was added to the enum without deciding what
    it counts, which is a question that has to be answered before anything can
    be summed. Failing at import beats a total in mixed units.
    """
    return EVENT_UNITS[event_type]

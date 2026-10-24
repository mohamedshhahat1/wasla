"""A workspace's automatic follow-up settings: the owner's reminders (FUS-01, ADR-151).

One row per workspace. The owner's card ("إعدادات المتابعة") shows it: a
toggle, the platforms the reminders run on, and up to five reminders, each "after
N minutes / hours" of silence. The steps are the reminders in order; a step's
delay is read when that step is scheduled, so an edit applies to every later
step of every run and never moves a step already waiting.

**A workspace with no row has sequences off.** Every workspace created through
the service gets the product default at creation - on, WhatsApp, one reminder
after five minutes - and migration 0122 wrote the same row for every workspace
that existed. A workspace that somehow has no row (one written directly, as a
test fixture does) is treated as "not configured": nothing is sent for it.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import Boolean, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: The fewest and most minutes a reminder may wait: five minutes to 23 hours, so
#: one step by itself can never be scheduled outside a 24-hour window.
MIN_STEP_DELAY_MINUTES: Final = 5
MAX_STEP_DELAY_MINUTES: Final = 23 * 60
#: Reminders per workspace (FUS-01, FUS-03).
MAX_STEPS: Final = 5
#: The longest instruction to the AI a reminder may carry.
MAX_GUIDANCE_LENGTH: Final = 500

#: What a workspace created through the service starts with (FUS-01).
DEFAULT_ENABLED: Final = True
DEFAULT_CHANNEL_TYPES: Final[tuple[str, ...]] = ("whatsapp",)
DEFAULT_STEPS: Final[tuple[dict[str, Any], ...]] = ({"delay_minutes": 5, "guidance": None},)


class FollowUpSettings(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One workspace's reminders: on or off, on which platforms, after how long."""

    __tablename__ = "follow_up_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_follow_up_settings_tenant_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Channel type values (`Channel`), a subset of the ones with an adapter.
    channel_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # `[{delay_minutes, guidance}]`, in order, at most `MAX_STEPS`.
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    # The edit counter the card sends back as If-Match (FUS-01).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

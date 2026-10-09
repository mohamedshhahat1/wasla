"""A workspace that must come down to fewer channel connections (ENT-14, ENT-15).

When the capacity in force falls below the connections a workspace holds - a
downgrade taking effect, a channel top-up or grant ending, a refund withdrawn, a
migration adopted - nothing is disabled at once. One durable row records that
the workspace must come down, to what, and by when:

``pending_selection``
    The grace (``CHANNEL_CAPACITY_GRACE_DAYS``, 7 by default) is running. Every
    connection keeps working; no new connection or re-enable fits; an owner
    chooses which connections to keep.
``resolved_by_owner``
    An owner chose - during the grace, or ahead of it with a pre-selection that
    still fitted at the boundary. The others were disabled.
``resolved_automatically``
    The grace ran out with no choice. Connections of a type the plan no longer
    allows were disabled first, then the newest, keeping the oldest.
``no_longer_needed``
    Capacity came back - a top-up, a grant, an upgrade - or the owner disabled
    enough themselves. Nothing was disabled by the flow.

Disabling is never releasing: claim, credential, conversations and contacts
stay, and an owner may enable a connection again when a slot is free. One open
reduction per workspace, by a partial unique index; a newer cause adjusts the
open one's target rather than opening a second.

Suspension, cancellation and expiry never open one (ENT-16).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    RevisionedMixin,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from app.db.models.enums import _enum_type


class CapacityReductionCause(StrEnum):
    """What brought the capacity in force below the active connections (ENT-15)."""

    DOWNGRADE = "downgrade"
    TOPUP_EXPIRED = "topup_expired"
    TOPUP_WITHDRAWN = "topup_withdrawn"
    GRANT_EXPIRED = "grant_expired"
    MIGRATION = "migration"
    # Staff withdrew a platform grant before it ended (PLAT-G1); the reduction
    # names the grant in `topup_purchase_id`. Appended, as ADD VALUE appends.
    GRANT_WITHDRAWN = "grant_withdrawn"


class CapacityReductionStatus(StrEnum):
    """Where one reduction stands; only ``PENDING_SELECTION`` is open."""

    PENDING_SELECTION = "pending_selection"
    RESOLVED_BY_OWNER = "resolved_by_owner"
    RESOLVED_AUTOMATICALLY = "resolved_automatically"
    NO_LONGER_NEEDED = "no_longer_needed"


CAPACITY_REDUCTION_CAUSE_TYPE = _enum_type(
    CapacityReductionCause, name="channel_capacity_reduction_cause"
)
CAPACITY_REDUCTION_STATUS_TYPE = _enum_type(
    CapacityReductionStatus, name="channel_capacity_reduction_status"
)


class ChannelCapacityReduction(
    Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, RevisionedMixin
):
    """One workspace's obligation to come down to a smaller channel capacity."""

    __tablename__ = "channel_capacity_reductions"
    __table_args__ = (
        # Restated, not inherited: see TenantScopedMixin.
        Index("ix_channel_capacity_reductions_tenant_id", "tenant_id"),
        # One open reduction per workspace: a second cause adjusts the first.
        Index(
            "uq_channel_capacity_reductions_open",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'pending_selection'"),
        ),
        # What the billing worker's grace phase claims. Partial: open ones only.
        Index(
            "ix_channel_capacity_reductions_grace_ends_at",
            "grace_ends_at",
            postgresql_where=text("status = 'pending_selection'"),
        ),
        CheckConstraint("grace_ends_at >= effective_at", name="grace_after_effective"),
        CheckConstraint(
            "(status = 'pending_selection') = (resolved_at IS NULL)",
            name="resolved_when_closed",
        ),
        CheckConstraint("target_general >= 0", name="target_general_non_negative"),
        # A reduction a withdrawn grant opened names that grant, and only that
        # cause names one (PLAT-G1, E18).
        CheckConstraint(
            "(cause = 'grant_withdrawn') = (topup_purchase_id IS NOT NULL)",
            name="withdrawn_grant_named",
        ),
    )

    cause: Mapped[CapacityReductionCause] = mapped_column(
        CAPACITY_REDUCTION_CAUSE_TYPE, nullable=False
    )
    status: Mapped[CapacityReductionStatus] = mapped_column(
        CAPACITY_REDUCTION_STATUS_TYPE,
        nullable=False,
        default=CapacityReductionStatus.PENDING_SELECTION,
    )
    # The capacity the workspace must fit, as last judged: general slots, typed
    # slots by channel, and the channel types the plan allows. A snapshot for
    # the record - every decision re-reads the capacity in force.
    target_general: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_typed: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    target_allowed_types: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    grace_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the owners were told the grace began, and warned 48 h before its end.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # What the resolution kept and disabled, for the record.
    kept_connection_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    disabled_connection_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    # The platform grant whose withdrawal opened this reduction (PLAT-G1).
    # RESTRICT: a purchase is financial history and is never deleted, and a
    # purge erases the reduction first.
    topup_purchase_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "topup_purchases.id",
            ondelete="RESTRICT",
            # Named: the convention's name is one byte over PostgreSQL's limit.
            name="fk_channel_capacity_reductions_topup_purchase",
        ),
        nullable=True,
    )

    @property
    def is_open(self) -> bool:
        return self.status is CapacityReductionStatus.PENDING_SELECTION


class ChannelCapacityPreselection(Base, TenantScopedMixin):
    """One connection an owner chose to keep at the next capacity boundary (ENT-14).

    Made while a downgrade is scheduled, or while the capacity at the end of the
    term will not hold every connection. Applied at the boundary if it still
    fits; otherwise, and once a boundary has passed either way, discarded. The
    connection key carries the workspace, so a pre-selection can only ever name
    a connection of its own workspace.
    """

    __tablename__ = "channel_capacity_preselections"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "connection_id", name="pk_channel_capacity_preselections"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["channel_connections.tenant_id", "channel_connections.id"],
            name="fk_channel_capacity_preselections_tenant_connection",
            ondelete="CASCADE",
        ),
        Index("ix_channel_capacity_preselections_tenant_id", "tenant_id"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    selected_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "CAPACITY_REDUCTION_CAUSE_TYPE",
    "CAPACITY_REDUCTION_STATUS_TYPE",
    "CapacityReductionCause",
    "CapacityReductionStatus",
    "ChannelCapacityPreselection",
    "ChannelCapacityReduction",
]

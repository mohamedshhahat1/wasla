"""Tenant model.

A tenant is one customer workspace, and the isolation boundary for every piece
of business data in the platform.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import TENANT_STATUS_TYPE, TenantStatus

#: Quiet hours every workspace starts with (TZ-03): on, 22:00 to 09:00 in the
#: workspace's own time zone - a window that crosses midnight.
DEFAULT_QUIET_HOURS_START: Final = time(22, 0)
DEFAULT_QUIET_HOURS_END: Final = time(9, 0)


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A customer workspace.

    Tenants are soft-deleted: business data outlives the decision to close an
    account, and billing or audit questions arrive after the fact.
    """

    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_tenants_slug"),
        # Quiet hours are a window, never an empty one, to the minute (TZ-03).
        CheckConstraint(
            "campaign_quiet_hours_start <> campaign_quiet_hours_end",
            name="campaign_quiet_hours_window",
        ),
        CheckConstraint(
            "date_part('second', campaign_quiet_hours_start) = 0"
            " AND date_part('second', campaign_quiet_hours_end) = 0",
            name="campaign_quiet_hours_minutes",
        ),
        # The purge sweep's only query: tombstoned, due, and not yet done. A
        # partial index rather than a plain one, because the rows it must find
        # are a vanishing fraction of the table and stay that way - a workspace
        # is deleted once and purged once, and every live workspace is excluded
        # by the predicate rather than scanned past.
        Index(
            "ix_tenants_purge_due",
            "purge_due_at",
            postgresql_where=(
                "deleted_at IS NOT NULL AND purged_at IS NULL AND purge_due_at IS NOT NULL"
            ),
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[TenantStatus] = mapped_column(
        TENANT_STATUS_TYPE,
        nullable=False,
        default=TenantStatus.ACTIVE,
    )

    # When the tombstone's retention window runs out and the operational data
    # becomes eligible for erasure (ADR-098). Stamped at deletion from the
    # configured retention, and stored rather than computed from `deleted_at`
    # plus a setting: the promise made to a customer is the one that was in
    # force on the day they left, and an operator shortening the setting must
    # not retroactively bring forward the erasure of data already tombstoned.
    #
    # NULL on a workspace that was never deleted, and on one tombstoned before
    # this column existed - the backfill in migration 0050 fills the second.
    purge_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    # When the erasure actually finished. The idempotency key for the sweep:
    # set once, checked before every pass, so a worker restarting mid-retention
    # cannot purge the same workspace twice - and so "has this been erased" is
    # answerable without inferring it from the absence of rows in a dozen
    # tables.
    purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    # Where the workspace is, as an IANA zone name (TZ-01, ADR-145). Chosen at
    # onboarding - every route that creates a workspace requires it - and the
    # zone every time a campaign respects is computed in: quiet hours, the
    # days a delivery plan spreads over, the local times a page shows.
    # Validated against `app.core.timezones`; no default, so nothing creates a
    # workspace without deciding. 0108 gave every earlier one `Africa/Cairo`.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    # When no campaign may write to this workspace's customers, in the
    # workspace's own time (TZ-03, ADR-145): the customer's protection, so it
    # binds every campaign send and nothing else - not an AI or human reply,
    # not a follow-up, not a test send. On by default, 22:00 to 09:00; a
    # window whose start is later than its end crosses midnight. 0109.
    campaign_quiet_hours_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    campaign_quiet_hours_start: Mapped[time] = mapped_column(
        Time, nullable=False, default=DEFAULT_QUIET_HOURS_START, server_default=text("'22:00'")
    )
    campaign_quiet_hours_end: Mapped[time] = mapped_column(
        Time, nullable=False, default=DEFAULT_QUIET_HOURS_END, server_default=text("'09:00'")
    )
    # The workspace's own wording of the automation disclosure, by language
    # (`{"en": ..., "ar": ...}`), where it has replaced Wasla's (OMNI-041).
    # Only the wording: the obligation itself cannot be switched off.
    automation_disclosure: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)

    @property
    def is_purged(self) -> bool:
        """Whether this workspace's operational data has been erased.

        Distinct from `is_deleted`, and the distinction is the whole point of
        the retention design: deletion stops access, and this stops existence.
        Calling a tombstone "erasure" is the claim docs/SECURITY.md exists to
        avoid making.
        """
        return self.purged_at is not None

    @property
    def is_active(self) -> bool:
        """True only while the workspace may be used.

        Suspension and soft deletion are separate states, and either one is
        enough to stop service.
        """
        return self.status is TenantStatus.ACTIVE and self.deleted_at is None

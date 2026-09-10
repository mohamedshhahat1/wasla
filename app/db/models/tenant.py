"""Tenant model.

A tenant is one customer workspace, and the isolation boundary for every piece
of business data in the platform.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import TENANT_STATUS_TYPE, TenantStatus


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A customer workspace.

    Tenants are soft-deleted: business data outlives the decision to close an
    account, and billing or audit questions arrive after the fact.
    """

    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_tenants_slug"),
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

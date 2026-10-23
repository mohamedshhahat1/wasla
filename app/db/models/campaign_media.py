"""The picture, video or document a campaign's template header carries (HM-01, ADR-144).

A marketing template approved with an IMAGE, VIDEO or DOCUMENT header cannot be
sent without one, and Meta takes it either as a link it fetches or as the id of
a file uploaded to it. Wasla never hands out a link to its bucket (MEDIA.md: no
presigned URL is issued for WhatsApp), so the workspace uploads the file here,
Wasla stores it through the same write protocol as every attachment (ADR-087),
and the campaign worker uploads it to Meta itself - once per campaign and
connection, not once per person (HM-04).

**Its own table, not a row of `message_media`.** That table's rows belong to a
message in a conversation: both keys are NOT NULL and composite with the
workspace, `UNIQUE(message_id, position)` is its replay guard, and media
recovery releases an agent turn on the row's conversation. An asset has no
message and no conversation, so holding one there would mean relaxing two NOT
NULL keys every reader relies on. What is shared is everything about the
object: the key layout (`build_key`), the `storage_state` lifecycle and its
CHECK, `UNIQUE(storage_key)`, the storage capacity sum, upload reconciliation,
retention and the purge ledger.

**Immutable.** A new picture is a new asset. A campaign references one by
`(tenant_id, id)`, so it can never name another workspace's file.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.filenames import MAX_FILENAME_LENGTH
from app.core.media_types import MAX_MIME_TYPE_LENGTH
from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.media import MEDIA_STORAGE_STATE_TYPE, MediaStorageState


class HeaderMediaKind(StrEnum):
    """Which template header format an asset can fill (HM-03).

    The template's `format` in lower case. LOCATION is not here: it carries no
    file, and campaigns still refuse it (DEFERRED).
    """

    IMAGE = "image"
    VIDEO = "video"
    DOCUMENT = "document"


MAX_HEADER_MEDIA_KIND_LENGTH: Final = 16


class CampaignHeaderMedia(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One stored file a campaign's template header can be sent with."""

    __tablename__ = "campaign_header_media"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        Index("ix_campaign_header_media_tenant_id", "tenant_id"),
        # The key campaigns reference, so a campaign cannot name another
        # workspace's asset even through a writer that skipped the scoped
        # repository (ADR-100's pattern).
        UniqueConstraint("tenant_id", "id", name="uq_campaign_header_media_tenant_id_id"),
        # One row owns one object, as in `message_media` (ADR-087).
        UniqueConstraint("storage_key", name="uq_campaign_header_media_storage_key"),
        # Retention's query: fully stored assets, oldest first (ADR-078).
        Index(
            "ix_campaign_header_media_retention",
            "created_at",
            postgresql_where=text("storage_state = 'stored'"),
        ),
        # Reconciliation's query: intents whose write never reported back.
        Index(
            "ix_campaign_header_media_pending_upload",
            "upload_started_at",
            postgresql_where=text("storage_state = 'pending'"),
        ),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind.value}'" for kind in HeaderMediaKind) + ")",
            name="kind_known",
        ),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        # The lifecycle `message_media` enforces, for the same reason: a
        # consumer trusts `storage_state` alone.
        CheckConstraint(
            "(storage_state = 'absent' AND storage_key IS NULL) "
            "OR (storage_state = 'pending' AND storage_key IS NOT NULL "
            "AND upload_started_at IS NOT NULL AND purge_started_at IS NULL) "
            "OR (storage_state = 'stored' AND storage_key IS NOT NULL "
            "AND purge_started_at IS NULL) "
            "OR (storage_state = 'purging' AND storage_key IS NOT NULL "
            "AND purge_started_at IS NOT NULL) "
            "OR (storage_state = 'purged' AND storage_key IS NULL "
            "AND purge_started_at IS NOT NULL) "
            "OR (storage_state = 'mismatched' AND storage_key IS NOT NULL)",
            name="storage_state",
        ),
    )

    kind: Mapped[str] = mapped_column(String(MAX_HEADER_MEDIA_KIND_LENGTH), nullable=False)
    # The canonical type resolved from the file's own bytes (ADR-076), never
    # what a browser claimed.
    mime_type: Mapped[str] = mapped_column(String(MAX_MIME_TYPE_LENGTH), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    # A document's name as the customer's phone shows it (Meta's `filename`).
    # Display only: never a path, a key or a header.
    filename: Mapped[str | None] = mapped_column(String(MAX_FILENAME_LENGTH), nullable=True)
    # SHA-256 of the bytes, committed before the object is written and
    # checked again on every read the worker makes (HM-06).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    storage_state: Mapped[MediaStorageState] = mapped_column(
        MEDIA_STORAGE_STATE_TYPE,
        nullable=False,
        default=MediaStorageState.ABSENT,
        server_default=MediaStorageState.ABSENT.value,
    )
    upload_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purge_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Deleted from the workspace's template header library (HMT-01): no longer
    # listed or usable; the retention sweep purges it. Refused while anything
    # live names it.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_stored(self) -> bool:
        return self.storage_state is MediaStorageState.STORED

    @property
    def is_usable(self) -> bool:
        return self.is_stored and self.deleted_at is None


class TemplateHeaderUpload(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """Meta's id for one header file through one connection (HMR-02, HMT-02).

    A person's and a follow-up's template share one upload per (file,
    connection), `campaign_id` null; a campaign keeps its own per (campaign,
    file, connection), uploaded under its lease. Reused while it is fresher
    than `CAMPAIGN_HEADER_MEDIA_REFRESH_SECONDS`. The provider's id never
    leaves the server (HM-08).
    """

    __tablename__ = "template_header_uploads"
    __table_args__ = (
        Index("ix_template_header_uploads_tenant_id", "tenant_id"),
        Index(
            "uq_template_header_uploads_file_connection",
            "tenant_id",
            "header_media_id",
            "account_id",
            unique=True,
            postgresql_where=text("campaign_id IS NULL"),
        ),
        Index(
            "uq_template_header_uploads_campaign_file",
            "campaign_id",
            "header_media_id",
            "account_id",
            unique=True,
            postgresql_where=text("campaign_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "header_media_id"],
            ["campaign_header_media.tenant_id", "campaign_header_media.id"],
            name="fk_template_header_uploads_tenant_header_media",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["whatsapp_accounts.tenant_id", "whatsapp_accounts.id"],
            name="fk_template_header_uploads_tenant_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.id"],
            name="fk_template_header_uploads_tenant_campaign",
            ondelete="CASCADE",
        ),
    )

    header_media_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider_media_id: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "MAX_HEADER_MEDIA_KIND_LENGTH",
    "CampaignHeaderMedia",
    "HeaderMediaKind",
    "TemplateHeaderUpload",
]

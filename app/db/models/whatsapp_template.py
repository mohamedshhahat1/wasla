"""Approved WhatsApp message templates, mirrored from Meta.

Wasla does not own these rows. Meta does: a template is drafted and approved in
the WhatsApp Business Manager, and what is stored here is a copy of what Meta
says about it at the moment of the last sync. Nothing in this table can approve
a template, and nothing here should ever be edited by hand.

The copy earns its place because the alternative is asking Meta on every send.
Two things need the answer — a follow-up leaving the service window and a
campaign about to write to ten thousand people — and both need it in a
transaction, not behind a network call that can rate-limit or time out.

Being a copy has one consequence worth stating plainly: the registry can be
stale. Meta pauses a template that draws complaints without telling anyone, so a
row saying `approved` means "approved when we last looked". Sends are still
attempted against Meta's own answer, and a rejection is recorded as one; the
registry stops the sends that are *obviously* wrong, not every one.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

MAX_TEMPLATE_NAME_LENGTH: Final = 512
MAX_LANGUAGE_LENGTH: Final = 16

# `{{1}}` in a positional template, `{{name}}` in a named one. Meta accepts both
# and a business may have either, so the count is taken from whichever appears
# rather than from the format Meta labels the template with.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


class TemplateCategory(StrEnum):
    """What Meta filed the template under.

    The category is not cosmetic: `MARKETING` is the only one a campaign may
    use, and it is the one WhatsApp charges for and rate-limits hardest.
    `UNKNOWN` catches a category Meta adds after this was written, which must
    never silently read as one of the others.
    """

    MARKETING = "marketing"
    UTILITY = "utility"
    AUTHENTICATION = "authentication"
    UNKNOWN = "unknown"


class TemplateStatus(StrEnum):
    """Where the template stands with Meta.

    Only `APPROVED` may be sent. The rest are kept rather than dropped: a
    workspace whose template was rejected needs to see that it was, and a
    template Meta paused for quality is the likeliest explanation for a campaign
    that suddenly stops delivering.

    `UNKNOWN` is the landing place for any status Meta introduces later. It is
    deliberately not sendable — an unrecognised state must fail closed.
    """

    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    PAUSED = "paused"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


TEMPLATE_CATEGORY_TYPE = _enum_type(TemplateCategory, name="template_category")
TEMPLATE_STATUS_TYPE = _enum_type(TemplateStatus, name="template_status")


def count_placeholders(text: str | None) -> int:
    """How many distinct variables a template's body expects.

    Distinct rather than total: `{{1}} … {{1}}` is one variable used twice, and
    Meta is given one parameter for it. Counting occurrences would make a
    perfectly valid campaign look mismatched.
    """
    if not text:
        return 0
    return len({match.group(1) for match in PLACEHOLDER.finditer(text)})


class WhatsAppTemplate(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One approved (or not) template on one connected number's account.

    Identity is `(name, language)` within an account, which is Meta's own: a
    template is one name with a translation per language, and the pair is what a
    send names. The account is part of the key as well because two numbers in
    one workspace can belong to different WhatsApp Business accounts with
    genuinely different template sets.
    """

    __tablename__ = "whatsapp_templates"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "account_id",
            "name",
            "language",
            name="uq_whatsapp_templates_tenant_id_account_id_name_language",
        ),
        Index("ix_whatsapp_templates_tenant_id", "tenant_id"),
        Index("ix_whatsapp_templates_tenant_id_status", "tenant_id", "status"),
        Index("ix_whatsapp_templates_account_id", "account_id"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Meta's own id for the template. Recorded for support conversations and
    # never used as a key here: it is absent from older responses, and identity
    # is the name and language pair regardless.
    meta_template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    name: Mapped[str] = mapped_column(String(MAX_TEMPLATE_NAME_LENGTH), nullable=False)
    language: Mapped[str] = mapped_column(String(MAX_LANGUAGE_LENGTH), nullable=False)
    category: Mapped[TemplateCategory] = mapped_column(
        TEMPLATE_CATEGORY_TYPE,
        nullable=False,
        default=TemplateCategory.UNKNOWN,
    )
    status: Mapped[TemplateStatus] = mapped_column(
        TEMPLATE_STATUS_TYPE,
        nullable=False,
        default=TemplateStatus.UNKNOWN,
    )

    # The body as Meta renders it, placeholders and all. Kept so an interface
    # can show what a campaign is about to send without a round trip, and so a
    # variable count can be checked against something a person can read.
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The whole component list, unedited. Meta adds component kinds over time
    # and a registry that discards what it does not yet understand cannot be
    # resynced into usefulness later.
    components: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    variable_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    quality_rating: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_sendable(self) -> bool:
        """Whether Meta accepted this template the last time we looked."""
        return self.status is TemplateStatus.APPROVED

    @property
    def is_marketing(self) -> bool:
        return self.category is TemplateCategory.MARKETING


__all__ = [
    "MAX_LANGUAGE_LENGTH",
    "MAX_TEMPLATE_NAME_LENGTH",
    "TEMPLATE_CATEGORY_TYPE",
    "TEMPLATE_STATUS_TYPE",
    "TemplateCategory",
    "TemplateStatus",
    "WhatsAppTemplate",
    "count_placeholders",
]

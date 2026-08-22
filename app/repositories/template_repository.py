"""Data access for the mirrored template registry."""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement

from app.db.models.whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
)
from app.repositories.base import TenantScopedRepository


class WhatsAppTemplateRepository(TenantScopedRepository[WhatsAppTemplate]):
    """Templates of one workspace."""

    model = WhatsAppTemplate

    def _tenant_filter(self) -> ColumnElement[bool]:
        return WhatsAppTemplate.tenant_id == self.tenant_id

    async def get_by_id(self, template_id: uuid.UUID) -> WhatsAppTemplate | None:
        return await self._first(self._select().where(WhatsAppTemplate.id == template_id))

    async def require_by_id(self, template_id: uuid.UUID) -> WhatsAppTemplate:
        return await self._require(self._select().where(WhatsAppTemplate.id == template_id))

    async def get_by_name(
        self,
        *,
        account_id: uuid.UUID,
        name: str,
        language: str,
    ) -> WhatsAppTemplate | None:
        """The row for the identity a send actually names."""
        return await self._first(
            self._select().where(
                WhatsAppTemplate.account_id == account_id,
                WhatsAppTemplate.name == name,
                WhatsAppTemplate.language == language,
            )
        )

    async def find_anywhere(self, *, name: str, language: str) -> WhatsAppTemplate | None:
        """The same template, on whichever of the workspace's accounts has it.

        Used where the caller knows a name and a language but not which number
        will carry the message — a follow-up, which is bound to a conversation
        rather than to an account the scheduler chose. Still tenant-scoped: this
        can only ever see the workspace's own rows.
        """
        return await self._first(
            self._select()
            .where(
                WhatsAppTemplate.name == name,
                WhatsAppTemplate.language == language,
            )
            .order_by(WhatsAppTemplate.status, WhatsAppTemplate.id)
        )

    async def list_templates(
        self,
        *,
        account_id: uuid.UUID | None = None,
        status: TemplateStatus | None = None,
        category: TemplateCategory | None = None,
        limit: int = 100,
    ) -> list[WhatsAppTemplate]:
        """Alphabetical by name, then language, so a list reads predictably.

        Not paged by cursor, unlike conversations or leads. A WhatsApp Business
        account holds tens of templates, not thousands, and Meta itself caps how
        many a business may have.
        """
        query = self._select()
        if account_id is not None:
            query = query.where(WhatsAppTemplate.account_id == account_id)
        if status is not None:
            query = query.where(WhatsAppTemplate.status == status)
        if category is not None:
            query = query.where(WhatsAppTemplate.category == category)
        return await self._all(
            query.order_by(WhatsAppTemplate.name, WhatsAppTemplate.language).limit(limit)
        )

    async def list_for_account(self, account_id: uuid.UUID) -> list[WhatsAppTemplate]:
        return await self._all(self._select().where(WhatsAppTemplate.account_id == account_id))

    def create(
        self,
        *,
        account_id: uuid.UUID,
        name: str,
        language: str,
    ) -> WhatsAppTemplate:
        """Stage a template row. The tenant comes from this repository."""
        return self.add(
            WhatsAppTemplate(
                tenant_id=self.tenant_id,
                account_id=account_id,
                name=name,
                language=language,
                category=TemplateCategory.UNKNOWN,
                status=TemplateStatus.UNKNOWN,
                variable_count=0,
            )
        )


__all__ = ["WhatsAppTemplateRepository"]

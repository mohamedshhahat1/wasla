"""A workspace's knowledge-retrieval switches, read and changed (G9).

The deployment's settings carry the locked defaults; a workspace's
`tenants.knowledge_settings` overrides the switches in `WORKSPACE_SWITCHES` and
nothing else. Cut-offs (the tiers, the margin) and the re-rank model stay the
deployment's: they come from a calibration, not from a preference.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models.audit import AuditAction
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.audit_service import AuditTrail
from app.services.retrieval_policy import WORKSPACE_SWITCHES, KnowledgeSwitches, resolve


class KnowledgeSettingsService:
    """One workspace's retrieval switches."""

    def __init__(self, *, session: AsyncSession, tenant_id: uuid.UUID, settings: Settings) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._settings = settings
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    async def overrides(self) -> dict[str, Any]:
        value = await self._session.scalar(
            select(Tenant.knowledge_settings).where(Tenant.id == self._tenant_id)
        )
        return dict(value or {})

    async def switches(self) -> KnowledgeSwitches:
        return resolve(
            self._settings,
            await self.overrides(),
            embedding_model=self._settings.openai_embedding_model,
        )

    async def view(self) -> dict[str, Any]:
        """What the workspace set, and what is in effect."""
        switches = await self.switches()
        effective = {
            "hybrid": switches.policy.hybrid,
            "rerank": switches.policy.rerank,
            "confidence_tiers": switches.policy.tiers_active,
            "number_grounding": switches.features.number_grounding,
            "pre_retrieval": switches.features.pre_retrieval,
            "parallel_sentiment": switches.features.parallel_sentiment,
        }
        return {"overrides": await self.overrides(), "effective": effective}

    async def update(self, changes: dict[str, Any], *, actor: User | None) -> dict[str, Any]:
        """Apply `changes` (null clears a switch back to the default), audited."""
        unknown = sorted(set(changes) - set(WORKSPACE_SWITCHES))
        if unknown:
            raise ValidationError("Unknown knowledge settings.", details={"unknown": unknown})
        tenant = await self._session.scalar(
            select(Tenant).where(Tenant.id == self._tenant_id).with_for_update()
        )
        if tenant is None:
            raise ValidationError("This workspace does not exist.")
        current = dict(tenant.knowledge_settings or {})
        for name, value in changes.items():
            if value is None:
                current.pop(name, None)
            else:
                current[name] = value
        tenant.knowledge_settings = current or None
        await self._session.flush()
        self._audit.record(
            AuditAction.KNOWLEDGE_SETTINGS_UPDATED,
            actor=actor,
            target_type="tenant",
            target_id=self._tenant_id,
            # Switch names and values: settings, never content.
            meta={"changes": changes},
        )
        return await self.view()


__all__ = ["KnowledgeSettingsService"]

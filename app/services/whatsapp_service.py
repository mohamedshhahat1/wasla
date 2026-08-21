"""Inbound WhatsApp ingestion.

The webhook's only job: resolve the workspace, store the event once, project it,
return. No AI, no media fetching, no outbound calls.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppEventKind
from app.integrations.whatsapp.payload import (
    DeliveryStatus,
    InboundMessage,
    parse_webhook,
)
from app.repositories.whatsapp_repository import (
    WhatsAppAccountDirectory,
    WhatsAppEventRepository,
)
from app.services.conversation_service import ConversationProjectionService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """What happened to a webhook delivery. Every event lands in exactly one."""

    stored: int = 0
    duplicates: int = 0
    unknown_accounts: int = 0
    inactive_accounts: int = 0
    ignored: int = 0


class WhatsAppIngestionService:
    """Turns one webhook delivery into stored events and conversation rows."""

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session
        self._directory = WhatsAppAccountDirectory(session)
        self._repositories: dict[uuid.UUID, WhatsAppEventRepository] = {}
        self._projections: dict[uuid.UUID, ConversationProjectionService] = {}
        self._accounts: dict[str, WhatsAppAccount | None] = {}

    async def ingest(self, payload: Mapping[str, Any]) -> IngestionOutcome:
        envelope = parse_webhook(payload)
        stored = duplicates = unknown = inactive = 0
        ignored = envelope.ignored

        # Messages and statuses are handled by the same loop because the storage
        # rules are identical; only the projection differs.
        sources: list[InboundMessage | DeliveryStatus] = [
            *envelope.messages,
            *envelope.statuses,
        ]

        for source in sources:
            account = await self._account(source.phone_number_id)
            if account is None:
                # Someone else's number, or one connected then removed. Not an
                # error for us to report: Meta cannot fix it by retrying.
                unknown += 1
                logger.warning(
                    "whatsapp.unknown_phone_number_id",
                    extra={"phone_number_id": source.phone_number_id},
                )
                continue

            if not account.is_active:
                inactive += 1
                logger.info(
                    "whatsapp.account_disabled",
                    extra={"phone_number_id": source.phone_number_id},
                )
                continue

            is_message = isinstance(source, InboundMessage)
            repository = self._repository(account.tenant_id)
            _, created = await repository.record(
                account_id=account.id,
                event_id=source.event_id,
                kind=WhatsAppEventKind.MESSAGE if is_message else WhatsAppEventKind.STATUS,
                payload=source.raw,
                # A missing timestamp still needs a value to order by; arrival
                # time is the honest fallback.
                received_at=source.timestamp or datetime.now(UTC),
            )
            if not created:
                # A replay. Projecting again would duplicate a message or
                # re-advance a status, so the event stops here.
                duplicates += 1
                continue

            stored += 1
            projection = self._projection(account.tenant_id)
            if isinstance(source, InboundMessage):
                await projection.project_message(account_id=account.id, message=source)
            else:
                await projection.project_status(status=source)

        if stored:
            await self._session.flush()

        return IngestionOutcome(
            stored=stored,
            duplicates=duplicates,
            unknown_accounts=unknown,
            inactive_accounts=inactive,
            ignored=ignored,
        )

    async def _account(self, phone_number_id: str) -> WhatsAppAccount | None:
        """Resolve once per delivery; a batch usually shares one number."""
        if phone_number_id not in self._accounts:
            self._accounts[phone_number_id] = await self._directory.get_by_phone_number_id(
                phone_number_id
            )
        return self._accounts[phone_number_id]

    def _repository(self, tenant_id: uuid.UUID) -> WhatsAppEventRepository:
        repository = self._repositories.get(tenant_id)
        if repository is None:
            repository = WhatsAppEventRepository(self._session, tenant_id=tenant_id)
            self._repositories[tenant_id] = repository
        return repository

    def _projection(self, tenant_id: uuid.UUID) -> ConversationProjectionService:
        projection = self._projections.get(tenant_id)
        if projection is None:
            projection = ConversationProjectionService(
                session=self._session,
                tenant_id=tenant_id,
            )
            self._projections[tenant_id] = projection
        return projection

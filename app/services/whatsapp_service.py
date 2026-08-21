"""Inbound WhatsApp ingestion.

The webhook's only job: resolve the workspace, store the event once, return.
No AI, no media fetching, no outbound calls.
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
from app.integrations.whatsapp.payload import parse_webhook
from app.repositories.whatsapp_repository import (
    WhatsAppAccountDirectory,
    WhatsAppEventRepository,
)

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
    """Turns one webhook delivery into stored events."""

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session
        self._directory = WhatsAppAccountDirectory(session)
        self._repositories: dict[uuid.UUID, WhatsAppEventRepository] = {}
        self._accounts: dict[str, WhatsAppAccount | None] = {}

    async def ingest(self, payload: Mapping[str, Any]) -> IngestionOutcome:
        envelope = parse_webhook(payload)
        stored = duplicates = unknown = inactive = 0
        ignored = envelope.ignored

        events: list[tuple[str, str, WhatsAppEventKind, dict[str, Any], datetime | None]] = [
            (
                message.phone_number_id,
                message.event_id,
                WhatsAppEventKind.MESSAGE,
                message.raw,
                message.timestamp,
            )
            for message in envelope.messages
        ]
        events.extend(
            (
                status.phone_number_id,
                status.event_id,
                WhatsAppEventKind.STATUS,
                status.raw,
                status.timestamp,
            )
            for status in envelope.statuses
        )

        for phone_number_id, event_id, kind, raw, timestamp in events:
            account = await self._account(phone_number_id)
            if account is None:
                # Someone else's number, or one connected then removed. Not an
                # error for us to report: Meta cannot fix it by retrying.
                unknown += 1
                logger.warning(
                    "whatsapp.unknown_phone_number_id",
                    extra={"phone_number_id": phone_number_id},
                )
                continue

            if not account.is_active:
                inactive += 1
                logger.info(
                    "whatsapp.account_disabled",
                    extra={"phone_number_id": phone_number_id},
                )
                continue

            repository = self._repository(account.tenant_id)
            _, created = await repository.record(
                account_id=account.id,
                event_id=event_id,
                kind=kind,
                payload=raw,
                # A missing timestamp still needs a value to order by; arrival
                # time is the honest fallback.
                received_at=timestamp or datetime.now(UTC),
            )
            if created:
                stored += 1
            else:
                duplicates += 1

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

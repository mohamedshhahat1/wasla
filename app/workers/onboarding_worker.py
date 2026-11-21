"""The WhatsApp onboarding worker: Coexistence's syncs, history and contacts (ADR-204).

Time-triggered, so it polls PostgreSQL rather than blocking on a queue, in the
same shape as the follow-up, retention and upload loops (ADR-022). Two kinds of
work, both owed by rows written in another transaction and both idempotent:

- **the one-time syncs** (SY): a Coexistence number's contacts and history are
  each requested from Meta once, inside Meta's 24 hours. The intent is the row
  itself - `sync_next_attempt_at`, set in the onboarding's own transaction - so
  an onboarding that commits always has its syncs owed, and one that rolls back
  never does. A request Meta accepted is never sent again; one that failed is
  retried with backoff until the deadline and never after.
- **the projection** (HI, CT): history chunks and address-book deliveries the
  webhook stored as received are projected here. Per message id per
  connection, so a chunk twice, or chunks out of order, change nothing; a pass
  that dies part-way resumes from the stored events.

Registered beside the inbound recovery sweep, whose events it complements.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.session import Database
from app.integrations.whatsapp.onboarding import MetaOnboardingClient, OnboardingClient
from app.services.coexistence_service import (
    CoexistenceProjector,
    ProjectionTally,
    SyncTally,
    request_due_syncs,
)
from app.services.credential_service import CredentialService

logger = get_logger(__name__)

POLL_SECONDS: Final = 30.0
PROJECTION_BATCH: Final = 50


class OnboardingWorker:
    """Polls for owed syncs and unprojected Coexistence deliveries."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        client: OnboardingClient | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._database = database
        self._settings = settings
        self._credentials = CredentialService(settings)
        self._client = client if client is not None else self._build_client(settings)
        self._poll_seconds = poll_seconds
        self._running = False
        self._stopping = asyncio.Event()

    @staticmethod
    def _build_client(settings: Settings) -> OnboardingClient | None:
        if not (settings.meta_app_id and settings.meta_app_secret):
            return None
        return MetaOnboardingClient(
            app_id=settings.meta_app_id,
            app_secret=settings.meta_app_secret,
            api_version=settings.meta_onboarding_api_version,
        )

    async def run_forever(self) -> None:
        self._running = True
        self._stopping.clear()
        logger.info("onboarding_worker.started")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("onboarding_worker.pass_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("onboarding_worker.stopped")

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self, *, now: datetime | None = None) -> tuple[SyncTally, ProjectionTally]:
        """One pass: request what is owed, then project what has arrived."""
        moment = now or datetime.now(UTC)
        syncs = SyncTally()
        if self._client is not None:
            async with self._database.session() as session:
                syncs = await request_due_syncs(
                    session, client=self._client, credentials=self._credentials, now=moment
                )
        async with self._database.session() as session:
            projected = await CoexistenceProjector(session, now=moment).run(limit=PROJECTION_BATCH)
        if projected.events:
            logger.info(
                "onboarding_worker.projected",
                extra={
                    "event": "onboarding_worker.projected",
                    "imported": projected.imported,
                    "duplicates": projected.duplicates,
                    "skipped": projected.skipped,
                    "contacts_applied": projected.contacts_applied,
                    "contacts_waiting": projected.contacts_waiting,
                },
            )
        return syncs, projected


__all__ = ["OnboardingWorker"]

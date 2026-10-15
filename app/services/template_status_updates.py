"""Meta's `message_template_status_update` webhook, applied to the registry (CMQ-05, ADR-142).

Wasla learned that Meta paused or disabled a template only when a send was
refused (CAMPAIGNS_OUTBOUND_MESSAGING_AUDIT.md section 9). Meta reports the
change itself on the WhatsApp Business Account's
`message_template_status_update` field ("Message templates", developers.facebook.com,
read 2026-10-13; EXT-CMP-07), and this applies it: the registry row for
(account, name, language) takes the status the sync would give the same word,
through `META_STATUSES`. No periodic sync is added - that stays correct by
design.

**Routed by the account, never by anything the sender controls.** The event
names the WhatsApp Business Account (`entry.id`), and every connected number
on that account in any workspace has its own registry row - each is updated,
inside its own workspace.

**An older event never overwrites newer knowledge.** Webhooks are retried for
days and arrive out of order; an event dated before the row was last written -
a sync, or a later event - changes nothing. A status word the sync mapping
does not know changes nothing either: an event is news of a change, and an
unrecognised one is not evidence the template stopped being sendable.

**Subscribing the field in the Meta App is an operator's action**, not this
code's (docs/WHATSAPP.md); until then nothing arrives here.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.telemetry import record_template_status_update
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.whatsapp.payload import TemplateStatusEvent
from app.repositories.template_repository import WhatsAppTemplateRepository
from app.services.template_service import META_STATUSES

logger = get_logger(__name__)

TEMPLATE_WEBHOOK = "template_webhook"


async def apply_template_status_events(
    session: AsyncSession, events: Iterable[TemplateStatusEvent]
) -> int:
    """Apply each event to the registry rows it names. Returns how many rows changed."""
    changed = 0
    for event in events:
        status = META_STATUSES.get(event.event.strip().upper())
        if status is None:
            logger.info(
                "whatsapp.template_event_unmapped",
                extra={"event": "whatsapp.template_event_unmapped"},
            )
            continue
        # Platform-level, like resolving a phone number id: the webhook has
        # no workspace until the account names one.
        accounts = (
            await session.execute(
                select(WhatsAppAccount).where(WhatsAppAccount.waba_id == event.waba_id)
            )
        ).scalars()
        for account in accounts:
            templates = WhatsAppTemplateRepository(session, tenant_id=account.tenant_id)
            row = await templates.get_by_name(
                account_id=account.id, name=event.name, language=event.language
            )
            if row is None:
                logger.info(
                    "whatsapp.template_event_unknown_template",
                    extra={
                        "event": "whatsapp.template_event_unknown_template",
                        "account_id": str(account.id),
                    },
                )
                continue
            if event.at is not None and row.updated_at is not None and event.at < row.updated_at:
                # Older than what the row already says: a replay, or a late
                # delivery behind a sync.
                continue
            if row.status is status:
                continue
            row.status = status
            changed += 1
            logger.warning(
                "whatsapp.template_status_updated",
                extra={
                    "event": "whatsapp.template_status_updated",
                    "template_id": str(row.id),
                    "status": status.value,
                },
            )
            await record_template_status_update(TEMPLATE_WEBHOOK)
    return changed


__all__ = ["TEMPLATE_WEBHOOK", "apply_template_status_events"]

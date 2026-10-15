"""Acting on what a provider says later about a message it accepted (CMP-10, CMQ-05, ADR-142).

A send Meta accepts is `sent`; Meta can still report it `failed` afterwards, and
its status webhook names why with a code. Two codes carry facts that outlive
the message:

- **131050** - the person stopped marketing messages. Meta says do not retry.
  On a campaign or marketing message it is consent evidence, recorded through
  the one opt-out writer (`record_opt_out`) on the conversation's channel,
  `via=provider_status`, exactly as the same code on a send already is.
- **132015 / 132016 / 132001** - the template was paused, disabled or is gone.
  Template pacing holds a new marketing template's messages and, on negative
  feedback, pauses it and fails each held message with 132015 ("Template
  pacing", developers.facebook.com, read 2026-10-13; EXT-CMP-08). The registry
  is written back to `PAUSED`, as a refusal on a send already does, so the
  next campaign batch stops instead of discovering it per recipient.

The registry write-back lives here once, for both sources: a refused send
(`MessagingService`) and a later status (`ConversationService`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.telemetry import record_opt_outs, record_template_status_update
from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.channel import Channel
from app.db.models.conversation import Message, MessageOrigin
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus
from app.integrations.whatsapp.client import MARKETING_OPTED_OUT_CODE, TEMPLATE_WITHDRAWN_CODES
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.template_repository import WhatsAppTemplateRepository
from app.services.opt_out import record_opt_out

logger = get_logger(__name__)

#: Meta's code for a person who stopped marketing messages, and its codes for a
#: template that may not be sent - the client's own constants, one definition.
MARKETING_OPTED_OUT: Final = MARKETING_OPTED_OUT_CODE
TEMPLATE_WITHDRAWN: Final = TEMPLATE_WITHDRAWN_CODES

SEND_WRITEBACK: Final = "send_writeback"
STATUS_WEBHOOK: Final = "status_webhook"


async def withdraw_template(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    language: str,
    code: int,
    source: str,
) -> bool:
    """Write Meta's refusal of a template back onto the registry row, if we hold one.

    Only a template the registry already knows is updated. Creating a row for
    one it has never heard of would turn a single refusal into a permanent
    local block on a name this workspace may never have synced, and "unknown"
    is deliberately allowed to send (`refusal_reason_for`).

    `PAUSED` rather than `REJECTED` or `DISABLED`, whatever the code: pausing
    is the reversible state, and a sync is what establishes which of the three
    Meta actually means. Returns whether the registry changed.
    """
    template = await WhatsAppTemplateRepository(session, tenant_id=tenant_id).find_anywhere(
        name=name, language=language
    )
    if template is None or template.status is TemplateStatus.PAUSED:
        return False
    template.status = TemplateStatus.PAUSED
    template.rejection_reason = f"WhatsApp refused this template (code {code})."
    logger.warning(
        "whatsapp.template_withdrawn",
        extra={
            "event": "whatsapp.template_withdrawn",
            "template_id": str(template.id),
            "meta_code": code,
            "source": source,
        },
    )
    await record_template_status_update(source)
    return True


async def _is_marketing(session: AsyncSession, tenant_id: uuid.UUID, message: Message) -> bool:
    """A campaign copy, or a template the registry files as marketing."""
    if message.origin is MessageOrigin.CAMPAIGN:
        return True
    if not message.template_name or not message.template_language:
        return False
    template = await WhatsAppTemplateRepository(session, tenant_id=tenant_id).find_anywhere(
        name=message.template_name, language=message.template_language
    )
    return template is not None and template.category is TemplateCategory.MARKETING


async def act_on_failed_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    message: Message,
    channel: Channel,
    code: int | None,
    at: datetime | None = None,
) -> None:
    """What a `failed` status with `code` means beyond this one message (CMP-10)."""
    if code is None:
        return
    if code == MARKETING_OPTED_OUT and await _is_marketing(session, tenant_id, message):
        conversation = await ConversationRepository(session, tenant_id=tenant_id).get_by_id(
            message.conversation_id
        )
        if conversation is None:
            return
        if await record_opt_out(
            session,
            tenant_id=tenant_id,
            contact_id=conversation.contact_id,
            channel=channel,
            source=OptOutSource.CUSTOMER,
            via=OptOutVia.PROVIDER_STATUS,
            at=at or datetime.now(UTC),
        ):
            await record_opt_outs({OptOutVia.PROVIDER_STATUS.value: 1})
        return
    if code in TEMPLATE_WITHDRAWN and message.template_name and message.template_language:
        await withdraw_template(
            session,
            tenant_id=tenant_id,
            name=message.template_name,
            language=message.template_language,
            code=code,
            source=STATUS_WEBHOOK,
        )


__all__ = [
    "MARKETING_OPTED_OUT",
    "SEND_WRITEBACK",
    "STATUS_WEBHOOK",
    "TEMPLATE_WITHDRAWN",
    "act_on_failed_status",
    "withdraw_template",
]

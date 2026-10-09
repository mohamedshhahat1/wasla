"""Recover opt-outs the live path missed, from retained webhook evidence (OMNI-030).

Until OMNI-030 a customer tapping "Stop promotions" under a marketing template
was stored with no text and opted nobody out. The raw delivery is kept for 30
days after processing (DB-011) and then redacted, so for that long the tap is
still on record - and this replays it through the one opt-out writer, with the
provenance `replay`.

The rules, each one a test:

- **Dry run by default.** `apply=False` reads and counts; nothing is written.
- **Counts and safe identifiers only.** The report names workspaces by id and
  counts per outcome; never a phone, a business-scoped id or a word anybody
  wrote.
- **Idempotent.** A contact already opted out is left exactly as it is, so a
  second apply changes nothing.
- **A newer resume wins.** A contact re-admitted to campaigns after the tap - a
  colleague clearing the opt-out, or the customer resuming through the provider
  - is not opted out again on the strength of older evidence.
- **Dated by the tap**, not by the replay: when the customer asked is what a
  dispute about a marketing message turns on.

Unscoped by design, like the inbound sweeps: an operator command reading every
workspace's retained events. No API route reaches it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.channel import Channel
from app.db.models.channel_event import ChannelEvent, ChannelEventKind
from app.db.models.conversation import Contact, Conversation, MessageDirection, MessageOrigin
from app.integrations.whatsapp.payload import reply_action
from app.repositories.conversation_repository import MessageRepository
from app.repositories.template_repository import WhatsAppTemplateRepository
from app.services.opt_out import is_stop_request, record_opt_out

logger = get_logger(__name__)

#: The raw message types a tap can be stored under.
TAP_TYPES = ("button", "interactive")
#: Events read per query; the sweep pages by id.
BATCH = 500


@dataclass(slots=True)
class WorkspaceCounts:
    """What the replay found in one workspace."""

    candidates: int = 0
    applied: int = 0
    already_opted_out: int = 0
    skipped_newer_resume: int = 0
    no_projected_message: int = 0


@dataclass(slots=True)
class RecoveryReport:
    """Totals and per-workspace counts. Nothing in here identifies a person."""

    applied_mode: bool
    taps_read: int = 0
    by_workspace: dict[uuid.UUID, WorkspaceCounts] = field(default_factory=dict)
    # Contacts a dry run has already counted, so it reports what an apply would
    # write rather than one line per tap.
    _counted: set[uuid.UUID] = field(default_factory=set)

    def total(self, name: str) -> int:
        return sum(getattr(counts, name) for counts in self.by_workspace.values())


async def recover_button_opt_outs(session: AsyncSession, *, apply: bool) -> RecoveryReport:
    """Find retained stop taps whose contact was never opted out; opt them out if `apply`."""
    report = RecoveryReport(applied_mode=apply)
    after: uuid.UUID | None = None
    while True:
        query = (
            select(ChannelEvent)
            .where(
                ChannelEvent.channel == Channel.WHATSAPP,
                ChannelEvent.kind == ChannelEventKind.MESSAGE,
                ChannelEvent.payload.is_not(None),
                ChannelEvent.payload["type"].astext.in_(TAP_TYPES),
            )
            .order_by(ChannelEvent.id)
            .limit(BATCH)
        )
        if after is not None:
            query = query.where(ChannelEvent.id > after)
        events = (await session.scalars(query)).all()
        if not events:
            break
        for event in events:
            report.taps_read += 1
            await _replay(session, event, report, apply=apply)
        after = events[-1].id
    if apply:
        await session.flush()
    logger.info(
        "opt_out_recovery.finished",
        extra={
            "event": "opt_out_recovery.finished",
            "apply": apply,
            "taps_read": report.taps_read,
            "candidates": report.total("candidates"),
            "applied": report.total("applied"),
        },
    )
    return report


async def _replay(
    session: AsyncSession,
    event: ChannelEvent,
    report: RecoveryReport,
    *,
    apply: bool,
) -> None:
    payload: Mapping[str, Any] = event.payload or {}
    message_type = payload.get("type")
    if not isinstance(message_type, str):
        return
    action = reply_action(payload, message_type)
    if action is None:
        return
    is_stop = is_stop_request(action.title)
    if not is_stop and action.id_or_payload is not None:
        is_stop = await WhatsAppTemplateRepository(
            session, tenant_id=event.tenant_id
        ).marks_opt_out_payload(account_id=event.account_id, payload=action.id_or_payload)
    if not is_stop:
        return

    counts = report.by_workspace.setdefault(event.tenant_id, WorkspaceCounts())
    message = await MessageRepository(session, tenant_id=event.tenant_id).find_provider_message(
        connection_id=event.account_id, provider_message_id=event.event_id
    )
    if (
        message is None
        or message.direction is not MessageDirection.INBOUND
        or message.origin is not MessageOrigin.CUSTOMER
    ):
        counts.no_projected_message += 1
        return
    contact = await session.scalar(
        select(Contact)
        .join(
            Conversation,
            (Conversation.contact_id == Contact.id) & (Conversation.tenant_id == Contact.tenant_id),
        )
        .where(
            Conversation.id == message.conversation_id, Conversation.tenant_id == event.tenant_id
        )
    )
    if contact is None:  # pragma: no cover - the message's conversation is keyed
        counts.no_projected_message += 1
        return

    counts.candidates += 1
    tapped_at = message.sent_at or event.received_at or datetime.now(UTC)
    if contact.marketing_opt_out_at is not None or contact.id in report._counted:
        counts.already_opted_out += 1
        return
    if contact.marketing_resumed_at is not None and contact.marketing_resumed_at >= tapped_at:
        counts.skipped_newer_resume += 1
        return
    if apply and record_opt_out(
        contact, source=OptOutSource.CUSTOMER, via=OptOutVia.REPLAY, at=tapped_at
    ):
        counts.applied += 1
    elif not apply:
        # What an apply would write, counted by a dry run.
        report._counted.add(contact.id)
        counts.applied += 1


__all__ = ["RecoveryReport", "WorkspaceCounts", "recover_button_opt_outs"]

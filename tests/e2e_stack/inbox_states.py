"""Put an e2e conversation into an inbox state the API cannot reach in a few calls (FE-4, EC4-04).

    python -m tests.e2e_stack.inbox_states window-closed <conversation-id>
        the customer last wrote 25 hours ago: free replies are closed
    python -m tests.e2e_stack.inbox_states templates <slug>
        an approved UTILITY and an approved MARKETING template, one body parameter each
    python -m tests.e2e_stack.inbox_states media <conversation-id> <pending|failed|unavailable>
        an inbound file in that state (no bytes are ever fetched for it)
    python -m tests.e2e_stack.inbox_states voice-note <conversation-id>
        an inbound voice note, stored, with a ready transcript

Test-only, like the rest of ``tests.e2e_stack``: nothing under ``app/`` imports it
(``tests/unit/test_e2e_gate_states.py`` holds that for the package). The journeys
run it with ``docker exec wasla-fe-e2e-api``. Everything else FE-4's journeys need
is made through the API and the fake inbound webhook - inbound text, image,
document and audio (an *available* file), opt-out, assignment, mode, priority,
close, reopen, a lead.

Rows are written through the application's models, and media bytes through the
deployment's own store (``build_media_storage``). Each command prints one JSON
line naming what it made, read back from the database.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.db.models.conversation import Conversation, Message
    from app.db.models.media import MessageMedia

USAGE: Final = (
    "usage: python -m tests.e2e_stack.inbox_states "
    "(window-closed <conversation-id> | templates <slug> | "
    "media <conversation-id> (pending|failed|unavailable) | voice-note <conversation-id>)"
)
MEDIA_STATES: Final = ("pending", "failed", "unavailable")
#: A tiny OGG/Opus header: what a voice note's bytes start with (detection only).
VOICE_BYTES: Final = b"OggS\x00\x02" + b"\x00" * 20 + b"OpusHead" + b"\x01" * 32
TRANSCRIPT: Final = "Hello, I would like to ask about the price of the kitchen."


def parse(argv: list[str]) -> tuple[str, str, str | None]:
    """(command, target, state) or the usage, before any connection is opened."""
    if len(argv) == 2 and argv[0] in {"window-closed", "voice-note"}:
        _uuid(argv[1])
        return argv[0], argv[1], None
    if len(argv) == 2 and argv[0] == "templates":
        return argv[0], argv[1], None
    if len(argv) == 3 and argv[0] == "media" and argv[2] in MEDIA_STATES:
        _uuid(argv[1])
        return argv[0], argv[1], argv[2]
    raise SystemExit(USAGE)


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise SystemExit(USAGE) from None


async def _conversation(session: AsyncSession, conversation_id: str) -> Conversation:
    from app.db.models.conversation import Conversation

    row = await session.scalar(
        select(Conversation).where(Conversation.id == uuid.UUID(conversation_id))
    )
    if row is None:
        raise SystemExit(f"no conversation {conversation_id}")
    return row


async def window_closed(session: AsyncSession, conversation_id: str) -> dict[str, object]:
    """The customer's last message, and the conversation's window, 25 hours ago."""
    from app.db.models.conversation import Message, MessageDirection

    conversation = await _conversation(session, conversation_id)
    moment = datetime.now(UTC) - timedelta(hours=25)
    conversation.last_inbound_at = moment
    if conversation.last_message_at is None or conversation.last_message_at > moment:
        conversation.last_message_at = moment
    inbound = (
        await session.scalars(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.direction == MessageDirection.INBOUND,
            )
        )
    ).all()
    for message in inbound:
        message.created_at = moment
    return {"conversation_id": conversation_id, "last_inbound_at": moment.isoformat()}


async def templates(session: AsyncSession, slug: str) -> dict[str, object]:
    """One approved UTILITY and one approved MARKETING template, each with one body parameter."""
    from app.db.models import Tenant
    from app.db.models.whatsapp import WhatsAppAccount
    from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus, WhatsAppTemplate

    tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        raise SystemExit(f"no workspace with the slug {slug!r}")
    account = await session.scalar(
        select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id).limit(1)
    )
    if account is None:
        raise SystemExit("the workspace has no connected number")
    made: dict[str, object] = {}
    for category, name, text in (
        (TemplateCategory.UTILITY, "order_update", "Your order {{1}} is on its way."),
        (TemplateCategory.MARKETING, "spring_offer", "Hello {{1}}, our spring offer is here."),
    ):
        session.add(
            WhatsAppTemplate(
                tenant_id=tenant.id,
                account_id=account.id,
                name=name,
                language="en",
                category=category,
                status=TemplateStatus.APPROVED,
                body_text=text,
                variable_count=1,
                components=[{"type": "BODY", "text": text}],
            )
        )
        made[category.value] = name
    return made


async def _inbound_file(
    session: AsyncSession,
    conversation_id: str,
    *,
    kind: str,
    mime_type: str,
    filename: str | None,
    body: str | None,
) -> tuple[Message, MessageMedia]:
    from app.db.models.conversation import (
        Message,
        MessageDirection,
        MessageKind,
        MessageOrigin,
        MessageStatus,
    )
    from app.db.models.media import MessageMedia

    conversation = await _conversation(session, conversation_id)
    message = Message(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        kind=MessageKind(kind),
        status=MessageStatus.RECEIVED,
        origin=MessageOrigin.CUSTOMER,
        body=body,
    )
    session.add(message)
    await session.flush()
    media = MessageMedia(
        tenant_id=conversation.tenant_id,
        message_id=message.id,
        conversation_id=conversation.id,
        position=0,
        mime_type=mime_type,
        filename=filename,
        byte_size=0,
    )
    session.add(media)
    conversation.last_message_at = datetime.now(UTC)
    conversation.last_inbound_at = conversation.last_message_at
    return message, media


async def media(session: AsyncSession, conversation_id: str, state: str) -> dict[str, object]:
    """An inbound document in `state`: pending, failed or unavailable (purged)."""
    from app.db.models.media import MediaStatus, MediaStorageState

    message, item = await _inbound_file(
        session,
        conversation_id,
        kind="document",
        mime_type="application/pdf",
        filename=f"{state}-quote.pdf",
        body=None,
    )
    if state == "pending":
        item.status = MediaStatus.PENDING
        item.storage_state = MediaStorageState.ABSENT
    elif state == "failed":
        item.status = MediaStatus.FAILED
        item.storage_state = MediaStorageState.ABSENT
    else:
        item.status = MediaStatus.READY
        item.storage_state = MediaStorageState.PURGED
        item.purge_started_at = datetime.now(UTC)
        item.byte_size = 2048
    await session.flush()
    return {"message_id": str(message.id), "media_id": str(item.id), "state": state}


async def voice_note(session: AsyncSession, conversation_id: str) -> dict[str, object]:
    """An inbound voice note, its bytes in the deployment's store, transcript ready."""
    from app.core.config import get_settings
    from app.core.storage import build_key, build_media_storage
    from app.db.models.media import MediaStatus, MediaStorageState

    message, item = await _inbound_file(
        session,
        conversation_id,
        kind="audio",
        mime_type="audio/ogg",
        filename=None,
        body=None,
    )
    key = build_key(tenant_id=message.tenant_id, mime_type="audio/ogg")
    await build_media_storage(get_settings()).put_at(
        key=key, data=VOICE_BYTES, mime_type="audio/ogg"
    )
    item.status = MediaStatus.READY
    item.storage_state = MediaStorageState.STORED
    item.storage_key = key
    item.byte_size = len(VOICE_BYTES)
    item.is_voice = True
    item.transcript = TRANSCRIPT
    await session.flush()
    return {"message_id": str(message.id), "media_id": str(item.id), "transcript": "ready"}


async def run(argv: list[str]) -> dict[str, object]:
    from app.core.config import get_settings
    from app.db.session import Database

    command, target, state = parse(argv)
    database = Database(get_settings())
    try:
        async with database.session() as session:
            if command == "window-closed":
                made = await window_closed(session, target)
            elif command == "templates":
                made = await templates(session, target)
            elif command == "media" and state is not None:
                made = await media(session, target, state)
            else:
                made = await voice_note(session, target)
            await session.commit()
        return made
    finally:
        await database.dispose()


def main(argv: list[str]) -> None:
    print(json.dumps(asyncio.run(run(argv))))  # noqa: T201 - this script's output is its interface


if __name__ == "__main__":
    main(sys.argv[1:])

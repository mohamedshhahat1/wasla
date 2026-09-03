"""Media arriving through the webhook, against PostgreSQL.

What only a real database proves here: that a replayed delivery does not add a
second file row, that the constraint saying one-file-per-message is really on
the table, and that two workspaces receiving the same customer's photograph keep
their copies apart.
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation import MessageKind
from app.db.models.media import MediaStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.conversation_repository import MessageRepository
from app.repositories.media_repository import MediaRepository
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.media_queue import MediaJob
from app.workers.queue import AgentJob
from tests.fakes import as_agent_queue, as_media_queue

pytestmark = pytest.mark.integration

PHONE_NUMBER_ID = "109876543210"
OTHER_PHONE_NUMBER_ID = "209876543210"
CUSTOMER = "201234567890"
SENT_AT = "1786000000"


async def _tenant(session: AsyncSession, *, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _account(
    session: AsyncSession,
    *,
    tenant: Tenant,
    phone_number_id: str,
) -> WhatsAppAccount:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=phone_number_id,
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    session.add(account)
    await session.flush()
    return account


def _media_delivery(
    *,
    phone_number_id: str = PHONE_NUMBER_ID,
    message_id: str = "wamid.image",
    message_type: str = "image",
    descriptor: dict[str, Any] | None = None,
    caption: str | None = None,
) -> dict[str, Any]:
    body = descriptor if descriptor is not None else {"id": "media-1", "mime_type": "image/jpeg"}
    if caption is not None:
        body = {**body, "caption": caption}

    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Nour"}}],
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": CUSTOMER,
                                    "type": message_type,
                                    "timestamp": SENT_AT,
                                    message_type: body,
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


async def test_an_image_is_stored_with_a_pending_media_row(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")
    await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    outcome = await WhatsAppIngestionService(session=db_session).ingest(_media_delivery())
    assert outcome.stored == 1

    message = await MessageRepository(db_session, tenant_id=tenant.id).get_by_wa_message_id(
        "wamid.image"
    )
    assert message is not None
    assert message.kind is MessageKind.IMAGE

    media = await MediaRepository(db_session, tenant_id=tenant.id).get_for_message(message.id)
    assert media is not None
    assert media.status is MediaStatus.PENDING
    assert media.wa_media_id == "media-1"
    assert media.mime_type == "image/jpeg"
    assert media.conversation_id == message.conversation_id
    # Nothing has been read yet, and the row says so rather than guessing.
    assert media.transcript is None
    assert media.storage_key is None


async def test_a_caption_lands_on_the_message_and_not_on_the_media(
    db_session: AsyncSession,
) -> None:
    """The words are the customer's; the file's contents are not."""
    tenant = await _tenant(db_session, slug="acme")
    await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    await WhatsAppIngestionService(session=db_session).ingest(
        _media_delivery(caption="how much is this one?")
    )

    message = await MessageRepository(db_session, tenant_id=tenant.id).get_by_wa_message_id(
        "wamid.image"
    )
    assert message is not None
    assert message.body == "how much is this one?"

    media = await MediaRepository(db_session, tenant_id=tenant.id).get_for_message(message.id)
    assert media is not None
    assert media.transcript is None


async def test_a_replayed_delivery_does_not_add_a_second_file(db_session: AsyncSession) -> None:
    """Meta retries. One message must never end up with two attachments."""
    tenant = await _tenant(db_session, slug="acme")
    await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)
    service = WhatsAppIngestionService(session=db_session)

    first = await service.ingest(_media_delivery())
    second = await service.ingest(_media_delivery())

    assert first.stored == 1
    assert second.stored == 0
    assert second.duplicates == 1

    message = await MessageRepository(db_session, tenant_id=tenant.id).get_by_wa_message_id(
        "wamid.image"
    )
    assert message is not None
    media = await MediaRepository(db_session, tenant_id=tenant.id).list_for_conversation(
        message.conversation_id
    )
    assert len(media) == 1


async def test_a_voice_note_is_recorded_as_one(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")
    await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    await WhatsAppIngestionService(session=db_session).ingest(
        _media_delivery(
            message_id="wamid.voice",
            message_type="audio",
            descriptor={"id": "media-9", "mime_type": "audio/ogg; codecs=opus", "voice": True},
        )
    )

    message = await MessageRepository(db_session, tenant_id=tenant.id).get_by_wa_message_id(
        "wamid.voice"
    )
    assert message is not None
    assert message.kind is MessageKind.AUDIO

    media = await MediaRepository(db_session, tenant_id=tenant.id).get_for_message(message.id)
    assert media is not None
    assert media.is_voice is True
    assert media.mime_type == "audio/ogg"


async def test_a_text_message_creates_no_media_row(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")
    await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    await WhatsAppIngestionService(session=db_session).ingest(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                                "messages": [
                                    {
                                        "id": "wamid.text",
                                        "from": CUSTOMER,
                                        "type": "text",
                                        "timestamp": SENT_AT,
                                        "text": {"body": "hello"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    )

    message = await MessageRepository(db_session, tenant_id=tenant.id).get_by_wa_message_id(
        "wamid.text"
    )
    assert message is not None
    media = await MediaRepository(db_session, tenant_id=tenant.id).get_for_message(message.id)
    assert media is None


async def test_two_workspaces_keep_their_copies_apart(db_session: AsyncSession) -> None:
    """The same customer photographs the same thing for two businesses."""
    acme = await _tenant(db_session, slug="acme")
    globex = await _tenant(db_session, slug="globex")
    await _account(db_session, tenant=acme, phone_number_id=PHONE_NUMBER_ID)
    await _account(db_session, tenant=globex, phone_number_id=OTHER_PHONE_NUMBER_ID)

    service = WhatsAppIngestionService(session=db_session)
    await service.ingest(_media_delivery(message_id="wamid.acme"))
    await service.ingest(
        _media_delivery(message_id="wamid.globex", phone_number_id=OTHER_PHONE_NUMBER_ID)
    )

    acme_message = await MessageRepository(db_session, tenant_id=acme.id).get_by_wa_message_id(
        "wamid.acme"
    )
    assert acme_message is not None

    # Globex's repository must not see Acme's file, even by direct message id.
    other = await MediaRepository(db_session, tenant_id=globex.id).get_for_message(acme_message.id)
    assert other is None
    mine = await MediaRepository(db_session, tenant_id=acme.id).get_for_message(acme_message.id)
    assert mine is not None


async def test_an_unresolved_count_is_scoped_to_the_workspace(db_session: AsyncSession) -> None:
    acme = await _tenant(db_session, slug="acme")
    globex = await _tenant(db_session, slug="globex")
    await _account(db_session, tenant=acme, phone_number_id=PHONE_NUMBER_ID)
    await _account(db_session, tenant=globex, phone_number_id=OTHER_PHONE_NUMBER_ID)

    await WhatsAppIngestionService(session=db_session).ingest(_media_delivery())
    message = await MessageRepository(db_session, tenant_id=acme.id).get_by_wa_message_id(
        "wamid.image"
    )
    assert message is not None

    acme_media = MediaRepository(db_session, tenant_id=acme.id)
    globex_media = MediaRepository(db_session, tenant_id=globex.id)

    assert await acme_media.count_unresolved(message.conversation_id) == 1
    assert await globex_media.count_unresolved(message.conversation_id) == 0


async def test_a_sticker_is_read_as_an_image(db_session: AsyncSession) -> None:
    """Meta gives stickers their own type; nothing downstream needs it apart.

    A sticker is a small image and is described the same way. The raw event
    keeps the distinction for anything that later wants it.
    """
    tenant = await _tenant(db_session, slug="acme")
    await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    await WhatsAppIngestionService(session=db_session).ingest(
        _media_delivery(
            message_id="wamid.sticker",
            message_type="sticker",
            descriptor={"id": "media-7", "mime_type": "image/webp"},
        )
    )

    message = await MessageRepository(db_session, tenant_id=tenant.id).get_by_wa_message_id(
        "wamid.sticker"
    )
    assert message is not None
    assert message.kind is MessageKind.IMAGE

    media = await MediaRepository(db_session, tenant_id=tenant.id).get_for_message(message.id)
    assert media is not None
    assert media.mime_type == "image/webp"


class RecordingQueue:
    """Stands in for a Redis queue and remembers what was put on it."""

    def __init__(self) -> None:
        self.jobs: list[object] = []

    async def enqueue(self, job: AgentJob) -> None:
        self.jobs.append(job)


async def test_a_photograph_is_queued_for_reading_not_for_answering(
    db_session: AsyncSession,
) -> None:
    """The ordering the phase turns on.

    An agent asked to answer now would see a message with no words in it. The
    media worker enqueues the agent job once there is something to answer with.
    """
    tenant = await _tenant(db_session, slug="acme")
    await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    agents = RecordingQueue()
    media_queue = RecordingQueue()
    outcome = await WhatsAppIngestionService(
        session=db_session,
        queue=as_agent_queue(agents),
        media_queue=as_media_queue(media_queue),
    ).ingest(_media_delivery(caption="how much?"))

    assert outcome.stored == 1
    assert outcome.media_queued == 1
    assert outcome.queued == 0
    assert agents.jobs == []
    assert len(media_queue.jobs) == 1

    message = await MessageRepository(db_session, tenant_id=tenant.id).get_by_wa_message_id(
        "wamid.image"
    )
    assert message is not None
    media = await MediaRepository(db_session, tenant_id=tenant.id).get_for_message(message.id)
    assert media is not None
    queued = media_queue.jobs[0]
    assert isinstance(queued, MediaJob)
    assert queued.media_id == media.id


async def test_a_text_message_is_still_queued_for_answering(db_session: AsyncSession) -> None:
    """Nothing about the ordinary path changes."""
    tenant = await _tenant(db_session, slug="acme")
    await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    agents = RecordingQueue()
    media_queue = RecordingQueue()
    outcome = await WhatsAppIngestionService(
        session=db_session,
        queue=as_agent_queue(agents),
        media_queue=as_media_queue(media_queue),
    ).ingest(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                                "messages": [
                                    {
                                        "id": "wamid.plain",
                                        "from": CUSTOMER,
                                        "type": "text",
                                        "timestamp": SENT_AT,
                                        "text": {"body": "hello"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    )

    assert outcome.queued == 1
    assert outcome.media_queued == 0
    assert len(agents.jobs) == 1
    assert media_queue.jobs == []


async def test_two_photographs_are_two_reading_jobs(db_session: AsyncSession) -> None:
    """One job per file, unlike agent jobs, which are one per conversation.

    Two photographs are two things to read; collapsing them would leave one
    unread and the conversation waiting on it forever.
    """
    tenant = await _tenant(db_session, slug="acme")
    await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    media_queue = RecordingQueue()
    service = WhatsAppIngestionService(session=db_session, media_queue=as_media_queue(media_queue))
    await service.ingest(_media_delivery(message_id="wamid.one"))
    await service.ingest(_media_delivery(message_id="wamid.two"))

    assert len(media_queue.jobs) == 2

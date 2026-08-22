"""Media arriving through the webhook, against PostgreSQL.

What only a real database proves here: that a replayed delivery does not add a
second file row, that the constraint saying one-file-per-message is really on
the table, and that two workspaces receiving the same customer's photograph keep
their copies apart.
"""

import pytest

from app.db.models.conversation import MessageKind
from app.db.models.media import MediaStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.conversation_repository import MessageRepository
from app.repositories.media_repository import MediaRepository
from app.services.whatsapp_service import WhatsAppIngestionService

pytestmark = pytest.mark.integration

PHONE_NUMBER_ID = "109876543210"
OTHER_PHONE_NUMBER_ID = "209876543210"
CUSTOMER = "201234567890"
SENT_AT = "1786000000"


async def _tenant(session, *, slug):
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _account(session, *, tenant, phone_number_id):
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
    phone_number_id=PHONE_NUMBER_ID,
    message_id="wamid.image",
    message_type="image",
    descriptor=None,
    caption=None,
):
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


async def test_an_image_is_stored_with_a_pending_media_row(db_session):
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


async def test_a_caption_lands_on_the_message_and_not_on_the_media(db_session):
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


async def test_a_replayed_delivery_does_not_add_a_second_file(db_session):
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


async def test_a_voice_note_is_recorded_as_one(db_session):
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


async def test_a_text_message_creates_no_media_row(db_session):
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


async def test_two_workspaces_keep_their_copies_apart(db_session):
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


async def test_an_unresolved_count_is_scoped_to_the_workspace(db_session):
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

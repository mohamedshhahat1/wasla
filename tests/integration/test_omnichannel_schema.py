"""The channel foundation's rules, as the database enforces them (ADR-117 to ADR-120).

Every rule here is a key or a trigger, checked against PostgreSQL directly and
not through a service, so a writer that forgets one - a script, a future
adapter, a migration - is refused by the database rather than trusted. Each
refusal is asserted by the constraint's own name, because "an IntegrityError"
would also be raised by a broken fixture.

The audit's catalog probes are kept here as regressions: X3 (a provider id on
two numbers of one workspace), X7 (a second attachment on one message) and X8
(a file row naming another workspace's message).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ConnectionStatus,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
    IdentitySource,
)
from app.db.models.channel_event import ChannelEvent, ChannelEventKind
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
)
from app.db.models.media import MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.db.models.whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
)

pytestmark = pytest.mark.integration


async def _tenant(session: AsyncSession) -> Tenant:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Schema {tag}", slug=f"schema-{tag}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _number(session: AsyncSession, tenant: Tenant) -> WhatsAppAccount:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"PN-{uuid.uuid4().hex[:10]}",
        waba_id="waba-schema",
        display_phone_number="+20 100 000 0000",
    )
    session.add(account)
    await session.flush()
    return account


async def _contact(session: AsyncSession, tenant: Tenant, wa_id: str | None) -> Contact:
    contact = Contact(tenant_id=tenant.id, wa_id=wa_id)
    session.add(contact)
    await session.flush()
    return contact


def _phone() -> str:
    return f"2010{uuid.uuid4().int % 10**8:08d}"


async def _refused(session: AsyncSession, constraint: str, *rows: object) -> None:
    """Adding `rows` fails, on `constraint`, and leaves the session usable."""
    with pytest.raises(IntegrityError) as refused:
        async with session.begin_nested():
            session.add_all(rows)
            await session.flush()
    assert constraint in str(refused.value.orig), str(refused.value.orig)


async def _bsuid(
    session: AsyncSession, tenant: Tenant, contact: Contact, value: str = "EG.0schema"
) -> ContactIdentity:
    identity = ContactIdentity(
        tenant_id=tenant.id,
        contact_id=contact.id,
        channel=Channel.WHATSAPP,
        kind=IdentityKind.BSUID,
        scope=IdentityScope.PROVIDER_ACCOUNT,
        scope_ref="waba-schema",
        value=value,
        source=IdentitySource.PROVIDER,
    )
    session.add(identity)
    await session.flush()
    return identity


async def _conversation(
    session: AsyncSession, tenant: Tenant, account: WhatsAppAccount, contact: Contact
) -> Conversation:
    conversation = Conversation(tenant_id=tenant.id, contact_id=contact.id, account_id=account.id)
    session.add(conversation)
    await session.flush()
    return conversation


def _inbound(tenant: Tenant, conversation: Conversation, wamid: str | None = None) -> Message:
    return Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        wa_message_id=wamid or f"wamid.{uuid.uuid4().hex}",
        direction=MessageDirection.INBOUND,
        kind=MessageKind.TEXT,
        body="hi",
        origin=MessageOrigin.CUSTOMER,
    )


# ------------------------------------------------ connections (ADR-117)


async def test_a_number_is_mirrored_as_its_connection_through_its_lifecycle(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)

    async def mirrored() -> ChannelConnection | None:
        found: ChannelConnection | None = await db_session.scalar(
            select(ChannelConnection)
            .where(ChannelConnection.id == account.id)
            .execution_options(populate_existing=True)
        )
        return found

    connection = await mirrored()
    assert connection is not None
    assert (connection.tenant_id, connection.channel, connection.external_account_id) == (
        tenant.id,
        Channel.WHATSAPP,
        account.phone_number_id,
    )
    assert connection.status is ConnectionStatus.ACTIVE

    account.status = WhatsAppAccountStatus.DISABLED
    await db_session.flush()
    connection = await mirrored()
    assert connection is not None and connection.status is ConnectionStatus.DISABLED

    released = datetime.now(UTC)
    account.status = WhatsAppAccountStatus.RELEASED
    account.released_at = released
    await db_session.flush()
    connection = await mirrored()
    assert connection is not None and connection.released_at == released

    await db_session.delete(account)
    await db_session.flush()
    assert await mirrored() is None


async def test_a_live_claim_on_one_external_account_is_unique_per_channel(
    db_session: AsyncSession,
) -> None:
    acme, rival = await _tenant(db_session), await _tenant(db_session)
    now = datetime.now(UTC)
    first = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=acme.id,
        channel=Channel.INSTAGRAM,
        external_account_id="ig-account-1",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=now,
    )
    db_session.add(first)
    await db_session.flush()

    await _refused(
        db_session,
        "uq_channel_connections_live_external_account",
        ChannelConnection(
            id=uuid.uuid4(),
            tenant_id=rival.id,
            channel=Channel.INSTAGRAM,
            external_account_id="ig-account-1",
            status=ConnectionStatus.ACTIVE,
            ownership_started_at=now,
        ),
    )
    # The same external id on another channel is another account.
    db_session.add(
        ChannelConnection(
            id=uuid.uuid4(),
            tenant_id=rival.id,
            channel=Channel.MESSENGER,
            external_account_id="ig-account-1",
            status=ConnectionStatus.ACTIVE,
            ownership_started_at=now,
        )
    )
    await db_session.flush()


# -------------------------------------------------- identities (ADR-118)


async def test_a_contacts_phone_is_its_whatsapp_phone_identity(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    phone = _phone()

    contact = await _contact(db_session, tenant, phone)

    (identity,) = (
        await db_session.execute(
            select(ContactIdentity).where(ContactIdentity.contact_id == contact.id)
        )
    ).scalars()
    assert (
        identity.channel,
        identity.kind,
        identity.scope,
        identity.scope_ref,
        identity.value,
    ) == (
        Channel.WHATSAPP,
        IdentityKind.PHONE,
        IdentityScope.WORKSPACE,
        "",
        phone,
    )


async def test_a_phone_another_contact_holds_is_refused_by_name(db_session: AsyncSession) -> None:
    """Two contacts claiming one number is the merge the model refuses."""
    tenant = await _tenant(db_session)
    phone = _phone()
    holder = await _contact(db_session, tenant, None)
    db_session.add(
        ContactIdentity(
            tenant_id=tenant.id,
            contact_id=holder.id,
            channel=Channel.WHATSAPP,
            kind=IdentityKind.PHONE,
            scope=IdentityScope.WORKSPACE,
            scope_ref="",
            value=phone,
            source=IdentitySource.PROVIDER,
        )
    )
    await db_session.flush()

    await _refused(
        db_session, "uq_contact_identities_scoped_value", Contact(tenant_id=tenant.id, wa_id=phone)
    )


async def test_a_contact_keeps_one_whatsapp_phone(db_session: AsyncSession) -> None:
    """`wa_id` and the phone identity never disagree, so a contact's number is
    set once; a changed number is a deferred provider flow, not an edit."""
    tenant = await _tenant(db_session)
    contact = await _contact(db_session, tenant, _phone())

    with pytest.raises(IntegrityError) as refused:
        async with db_session.begin_nested():
            contact.wa_id = _phone()
            await db_session.flush()
    assert "uq_contact_identities_one_whatsapp_phone" in str(refused.value.orig)


async def test_an_identity_names_its_scope_consistently(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    contact = await _contact(db_session, tenant, None)

    await _refused(
        db_session,
        "scope_shape",
        ContactIdentity(
            tenant_id=tenant.id,
            contact_id=contact.id,
            channel=Channel.WHATSAPP,
            kind=IdentityKind.BSUID,
            scope=IdentityScope.PROVIDER_ACCOUNT,
            scope_ref="",
            value="EG.0noscope",
            source=IdentitySource.PROVIDER,
        ),
    )


# ------------------------------------------------ participants (ADR-119)


async def test_a_conversation_is_pinned_to_its_contacts_only_identity(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    contact = await _contact(db_session, tenant, _phone())

    conversation = await _conversation(db_session, tenant, account, contact)

    identity = await db_session.get(ContactIdentity, conversation.participant_identity_id)
    assert identity is not None
    assert (identity.contact_id, identity.kind) == (contact.id, IdentityKind.PHONE)


async def test_no_participant_is_guessed_between_two_identities(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    contact = await _contact(db_session, tenant, _phone())
    await _bsuid(db_session, tenant, contact)

    await _refused(
        db_session,
        "fk_conversations_tenant_participant",
        Conversation(tenant_id=tenant.id, contact_id=contact.id, account_id=account.id),
    )


async def test_a_participant_must_be_the_conversations_own_contacts(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    mine = await _contact(db_session, tenant, _phone())
    theirs = await _contact(db_session, tenant, None)
    stranger = await _bsuid(db_session, tenant, theirs)

    await _refused(
        db_session,
        "fk_conversations_tenant_participant",
        Conversation(
            tenant_id=tenant.id,
            contact_id=mine.id,
            account_id=account.id,
            participant_identity_id=stranger.id,
        ),
    )


async def test_a_participant_must_be_of_the_conversations_channel(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.INSTAGRAM,
        external_account_id=f"ig-{uuid.uuid4().hex[:8]}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC),
    )
    db_session.add(connection)
    await db_session.flush()
    contact = await _contact(db_session, tenant, None)
    instagram = ContactIdentity(
        tenant_id=tenant.id,
        contact_id=contact.id,
        channel=Channel.INSTAGRAM,
        kind=IdentityKind.IGSID,
        scope=IdentityScope.CONNECTION,
        scope_ref=str(connection.id),
        connection_id=connection.id,
        value="igsid-schema",
        source=IdentitySource.PROVIDER,
    )
    db_session.add(instagram)
    await db_session.flush()

    await _refused(
        db_session,
        "fk_conversations_tenant_participant",
        Conversation(
            tenant_id=tenant.id,
            contact_id=contact.id,
            account_id=account.id,
            participant_identity_id=instagram.id,
        ),
    )


async def test_a_conversations_channel_is_its_connections(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    contact = await _contact(db_session, tenant, _phone())

    await _refused(
        db_session,
        "fk_conversations_tenant_connection",
        Conversation(
            tenant_id=tenant.id,
            contact_id=contact.id,
            account_id=account.id,
            channel=Channel.INSTAGRAM,
        ),
    )


async def test_a_conversation_cannot_sit_on_another_workspaces_connection(
    db_session: AsyncSession,
) -> None:
    acme, rival = await _tenant(db_session), await _tenant(db_session)
    rivals = await _number(db_session, rival)
    contact = await _contact(db_session, acme, _phone())

    await _refused(
        db_session,
        "fk_conversations_tenant_connection",
        Conversation(tenant_id=acme.id, contact_id=contact.id, account_id=rivals.id),
    )


# ---------------------------------------- messages and events (ADR-120)


async def test_a_message_takes_its_conversations_connection(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    conversation = await _conversation(
        db_session, tenant, account, await _contact(db_session, tenant, _phone())
    )

    message = _inbound(tenant, conversation)
    db_session.add(message)
    await db_session.flush()

    assert message.connection_id == account.id


async def test_a_message_cannot_name_another_connection(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    account, other = await _number(db_session, tenant), await _number(db_session, tenant)
    conversation = await _conversation(
        db_session, tenant, account, await _contact(db_session, tenant, _phone())
    )
    message = _inbound(tenant, conversation)
    message.connection_id = other.id

    await _refused(db_session, "fk_messages_tenant_conversation_connection", message)


async def test_probe_x3_a_provider_id_is_still_unique_per_workspace_until_o8(
    db_session: AsyncSession,
) -> None:
    """X3: the workspace-wide key stands through the compatibility window
    (ADR-120); ingestion treats the second arrival as a collision before it
    reaches it. The per-connection key is the one that stays."""
    tenant = await _tenant(db_session)
    first, second = await _number(db_session, tenant), await _number(db_session, tenant)
    one = await _conversation(
        db_session, tenant, first, await _contact(db_session, tenant, _phone())
    )
    two = await _conversation(
        db_session, tenant, second, await _contact(db_session, tenant, _phone())
    )
    wamid = f"wamid.{uuid.uuid4().hex}"
    db_session.add(_inbound(tenant, one, wamid))
    await db_session.flush()

    await _refused(db_session, "uq_messages_tenant_id_wa_message_id", _inbound(tenant, two, wamid))
    await _refused(db_session, "uq_messages_tenant_id", _inbound(tenant, one, wamid))


async def test_an_event_cannot_be_filed_on_another_workspaces_connection(
    db_session: AsyncSession,
) -> None:
    acme, rival = await _tenant(db_session), await _tenant(db_session)
    rivals = await _number(db_session, rival)

    await _refused(
        db_session,
        "fk_whatsapp_events_tenant_connection",
        ChannelEvent(
            tenant_id=acme.id,
            account_id=rivals.id,
            event_id=f"wamid.{uuid.uuid4().hex}",
            kind=ChannelEventKind.MESSAGE,
            payload={"seeded": True},
            received_at=datetime.now(UTC),
        ),
    )


# ------------------------------------------------ files (OMNI-009, 022)


async def _message_with_conversation(
    session: AsyncSession, tenant: Tenant
) -> tuple[Conversation, Message]:
    account = await _number(session, tenant)
    conversation = await _conversation(
        session, tenant, account, await _contact(session, tenant, _phone())
    )
    message = _inbound(tenant, conversation)
    session.add(message)
    await session.flush()
    return conversation, message


def _file(
    tenant: Tenant, conversation: Conversation, message: Message, *, position: int = 0
) -> MessageMedia:
    return MessageMedia(
        tenant_id=tenant.id,
        message_id=message.id,
        conversation_id=conversation.id,
        position=position,
        wa_media_id=f"media-{uuid.uuid4().hex[:8]}",
        mime_type="image/png",
        byte_size=0,
        attempts=0,
        is_voice=False,
    )


async def test_probe_x7_a_message_holds_several_files_in_order(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    conversation, message = await _message_with_conversation(db_session, tenant)

    db_session.add_all(
        [_file(tenant, conversation, message), _file(tenant, conversation, message, position=1)]
    )
    await db_session.flush()

    await _refused(
        db_session,
        "uq_message_media_message_id_position",
        _file(tenant, conversation, message, position=1),
    )


async def test_probe_x8_a_file_cannot_name_another_workspaces_message(
    db_session: AsyncSession,
) -> None:
    """X8 was accepted at the audit's HEAD: single-column keys let a file row
    filed under one workspace name another's message and conversation."""
    acme, rival = await _tenant(db_session), await _tenant(db_session)
    conversation, message = await _message_with_conversation(db_session, rival)

    await _refused(
        db_session,
        "fk_message_media_tenant_message",
        _file(acme, conversation, message),
    )


async def test_a_file_cannot_name_a_message_of_another_conversation(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    one, _ = await _message_with_conversation(db_session, tenant)
    _, message = await _message_with_conversation(db_session, tenant)

    await _refused(db_session, "fk_message_media_tenant_message", _file(tenant, one, message))


# ---------------------------------------------- campaigns (OMNI-004)


async def test_a_campaign_recipient_names_an_identity_of_its_own_contact(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    template = WhatsAppTemplate(
        tenant_id=tenant.id,
        account_id=account.id,
        name="welcome",
        language="en",
        category=TemplateCategory.MARKETING,
        status=TemplateStatus.APPROVED,
    )
    db_session.add(template)
    await db_session.flush()
    campaign = Campaign(
        tenant_id=tenant.id,
        account_id=account.id,
        template_id=template.id,
        name="Launch",
        status=CampaignStatus.DRAFT,
        scheduled_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(campaign)
    await db_session.flush()
    mine = await _contact(db_session, tenant, _phone())
    theirs = await _contact(db_session, tenant, _phone())
    their_phone = await db_session.scalar(
        select(ContactIdentity.id).where(ContactIdentity.contact_id == theirs.id)
    )

    await _refused(
        db_session,
        "fk_campaign_recipients_tenant_participant",
        CampaignRecipient(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            contact_id=mine.id,
            participant_identity_id=their_phone,
        ),
    )
